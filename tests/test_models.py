"""helper 按本机 claude CLI 探测模型（零费用的 /model）并上报；服务端据此组装分组的模型列表。"""

from __future__ import annotations

import json
import stat
import sys
import time

import pytest

from claude_bridge import REPO, __version__
from claude_bridge.service import latest_label
from claude_bridge.worker import Worker, WorkerConfig, model_display_name
from test_worker import FakeClient

ALIASES = {"sonnet": "Sonnet 5", "opus": "Opus 5.5", "haiku": "Haiku 4.5", "fable": "Fable 5.1", "best": "Fable 5.1",
           "sonnet[1m]": "Sonnet 5", "opus[1m]": "Opus 5.5 (1M context)"}
PINNED = {"claude-opus-5-5": "Opus 5.5", "claude-opus-5": "Opus 5"}


def fake_cli(tmp_path, version="2.1.280 (Claude Code)", broken=False):
    """Answers like the real CLI: `--version`, `-p /model [--model X]`, `-p "/model <id>"`; logs every argv."""
    script = tmp_path / "claude"
    script.write_text(f"""#!{sys.executable}
import json, sys
argv = sys.argv[1:]
open({str(tmp_path / 'calls.jsonl')!r}, 'a').write(json.dumps(argv) + '\\n')
if {broken!r}:
    sys.exit(2)
if argv == ['--version']:
    print(open({str(tmp_path / 'version.txt')!r}).read().strip()); sys.exit(0)
assert '--no-session-persistence' in argv and argv[argv.index('--output-format') + 1] == 'json', argv
cmd = argv[argv.index('-p') + 1]
model = argv[argv.index('--model') + 1] if '--model' in argv else None
aliases, pinned = {ALIASES!r}, {PINNED!r}
if cmd == '/model':
    cur = aliases.get(model, 'Sonnet 5') if model else 'Sonnet 5'
    text = f"Current model: `{{cur}}` (effort: xhigh)"
    if not model:
        text += "\\nUsage: /model <name>. Available: " + ", ".join(list(aliases) + ['opusplan', 'default']) + ", or a full model ID."
else:
    mid = cmd.split(' ', 1)[1]
    text = f"Set model to `{{pinned[mid]}}` for this session only" if mid in pinned else f"Model '{{mid}}' not found"
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": text, "total_cost_usd": 0, "num_turns": 0}}))
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "version.txt").write_text(version)
    return str(script)


class ModelClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.reports: list[dict] = []

    def model_candidates(self):
        return ["claude-opus-5-5", "claude-opus-5", "claude-opus-9-9"]

    def report_models(self, report):
        self.reports.append(report)


def calls(tmp_path):
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


def test_model_display_name():
    assert model_display_name("Current model: `Opus 5.5` (effort: xhigh)") == "Opus 5.5"
    assert model_display_name("Set model to `Opus 5` for this session only") == "Opus 5"
    assert model_display_name("Model 'claude-opus-9-9' not found") is None
    assert model_display_name(None) is None


def test_probe_models_reports_aliases_and_recognised_pinned(tmp_path):
    w = Worker(ModelClient(), WorkerConfig(claude_bin=fake_cli(tmp_path), cwd=tmp_path))
    rep = w.probe_models(["claude-opus-5-5", "claude-opus-5", "claude-opus-9-9"])
    assert rep["cli_version"] == "2.1.280 (Claude Code)" and rep["default_name"] == "Sonnet 5"
    assert rep["bridge_version"] == __version__
    # default / opusplan dropped; best (= fable) and sonnet[1m] (= sonnet) folded into the first alias with that name
    assert [(a["id"], a["name"]) for a in rep["aliases"]] == [
        ("sonnet", "Sonnet 5"), ("opus", "Opus 5.5"), ("haiku", "Haiku 4.5"), ("fable", "Fable 5.1"), ("opus[1m]", "Opus 5.5 (1M context)")]
    assert rep["pinned"] == [{"id": "claude-opus-5-5", "name": "Opus 5.5"}, {"id": "claude-opus-5", "name": "Opus 5"}]
    assert all("--no-session-persistence" in c for c in calls(tmp_path) if c != ["--version"])


def test_probe_uses_the_workers_own_model_for_the_default(tmp_path):
    w = Worker(ModelClient(), WorkerConfig(claude_bin=fake_cli(tmp_path), cwd=tmp_path, model="haiku"))
    assert w.probe_models([])["default_name"] == "Haiku 4.5"


def test_probe_with_broken_cli_raises(tmp_path):
    w = Worker(ModelClient(), WorkerConfig(claude_bin=fake_cli(tmp_path, broken=True), cwd=tmp_path))
    with pytest.raises(RuntimeError):
        w.probe_models([])


def test_maybe_probe_runs_once_then_again_when_the_cli_changes(tmp_path):
    client = ModelClient()
    w = Worker(client, WorkerConfig(claude_bin=fake_cli(tmp_path), cwd=tmp_path, version_check_interval=0))
    w.maybe_probe_models(block=True)
    assert len(client.reports) == 1 and client.reports[0]["pinned"][0]["id"] == "claude-opus-5-5"
    w.maybe_probe_models(block=True)           # same version, interval not reached: nothing new
    assert len(client.reports) == 1
    (tmp_path / "version.txt").write_text("2.1.300 (Claude Code)")
    w.maybe_probe_models(block=True)           # CLI upgraded → probe again
    assert len(client.reports) == 2 and client.reports[1]["cli_version"] == "2.1.300 (Claude Code)"


def test_failed_probe_is_retried_later_not_every_loop(tmp_path):
    client = ModelClient()
    w = Worker(client, WorkerConfig(claude_bin=fake_cli(tmp_path, broken=True), cwd=tmp_path, version_check_interval=600))
    w.maybe_probe_models(block=True)
    assert client.reports == [] and w._next_probe > time.monotonic() + 500
    n = len(calls(tmp_path))
    w.maybe_probe_models(block=True)
    assert len(calls(tmp_path)) == n


def test_probe_can_be_disabled(tmp_path):
    client = ModelClient()
    w = Worker(client, WorkerConfig(claude_bin=fake_cli(tmp_path), cwd=tmp_path, model_probe=False))
    w.maybe_probe_models(block=True)
    assert client.reports == [] and not (tmp_path / "calls.jsonl").exists()


def test_latest_label():
    assert latest_label("Opus 5.5") == "最新 Opus（5.5）"
    assert latest_label("Opus 5.5 (1M context)") == "最新 Opus（5.5 · 1M）"
    assert latest_label("Sonnet 5") == "最新 Sonnet（5）"


REPORT = {"cli_version": "2.1.280 (Claude Code)", "default_name": "Sonnet 5",
          "aliases": [{"id": "opus", "name": "Opus 5.5"}, {"id": "opus[1m]", "name": "Opus 5.5 (1M context)"}],
          "pinned": [{"id": "claude-opus-5-5", "name": "Opus 5.5"}]}


def test_settings_use_the_reported_models_with_groups(web, agent):
    before = web.get("/api/settings").json()
    assert before["models_info"] == {"source": "static"} and [m["id"] for m in before["models"]] == ["", "sonnet"]
    assert agent.get("/api/agent/models").json() == {"candidates": ["sonnet"]}  # the host's static ids, minus "default"
    assert web.post("/api/agent/models", json=REPORT).status_code == 401       # helper route needs the token
    assert agent.post("/api/agent/models", json=REPORT).status_code == 200

    s = web.get("/api/settings").json()
    assert s["models_info"]["source"] == "helper" and s["models_info"]["cli_version"] == "2.1.280 (Claude Code)"
    assert s["models"] == [
        {"id": "", "label": "默认（helper：Sonnet 5）"},
        {"id": "opus", "label": "最新 Opus（5.5）", "group": "跟随 CLI 最新"},
        {"id": "opus[1m]", "label": "最新 Opus（5.5 · 1M）", "group": "跟随 CLI 最新"},
        {"id": "claude-opus-5-5", "label": "Opus 5.5", "group": "固定版本"},
    ]
    # an alias is a first-class choice: saved and handed to the helper as-is
    assert web.put("/api/settings", json={"model": "opus"}).json()["chat"]["model"] == "opus"


def test_empty_report_keeps_the_static_list(web, agent):
    agent.post("/api/agent/models", json={"cli_version": "x", "aliases": [], "pinned": []})
    assert web.get("/api/settings").json()["models_info"] == {"source": "static"}


def test_versions_show_server_and_helper(web, agent):
    b = web.get("/api/settings").json()["bridge"]
    assert b == {"version": __version__, "repo": REPO, "helper_version": None}   # helper has not reported yet
    agent.post("/api/agent/models", json={**REPORT, "bridge_version": "0.1.9"})
    assert web.get("/api/settings").json()["bridge"]["helper_version"] == "0.1.9"   # → "helper not restarted yet"
    assert web.get("/api/status").json()["bridge"]["version"] == __version__
