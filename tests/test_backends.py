"""Worker backends: another provider (Anthropic-compatible) in its own "<scope>@<name>" threads, for chosen admins."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from claude_bridge.backends import is_peak, price_for, split_backend, turn_cost
from claude_bridge.callers import Callers
from claude_bridge.errors import BridgeError, QuotaExceeded
from claude_bridge.local_usage import TranscriptScanner
from claude_bridge.pricing import is_external, usage_cost
from claude_bridge.principal import Principal
from claude_bridge.server import BridgeConfig, create_bridge
from claude_bridge.store import BridgeStore, utc_text
from claude_bridge.worker import Backend, Worker, WorkerConfig, WorkerProfile, load_backends

PEAK = {"utc_hours": [[1, 4], [6, 10]], "weekdays_only": True, "multiplier": 2}
PRICING = {"flash": {"input": 0.15, "cache_read": 0.003, "output": 0.6}}
INFO = {"name": "ds", "label": "DeepSeek", "owners": ["1"], "models": [{"id": "deepseek-flash[1m]", "name": "Flash (1M)"},
        {"id": "deepseek-v4-pro", "name": "Pro"}], "pricing": PRICING, "peak": PEAK}
BALANCE = {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "42.00",
                                                    "granted_balance": "0", "topped_up_balance": "42.00"}]}

WHO = {
    "me": Principal(owner="1", admin=True, name="me"),
    "me-demoted": Principal(owner="1", admin=False, name="me"),
    "other-admin": Principal(owner="2", admin=True, name="a2"),
    "user": Principal(owner="3", admin=False, name="u3"),
}


@pytest.fixture
def env(tmp_path):
    def auth(x_who: str = Header("me")) -> Principal:
        return WHO[x_who]

    def no_quota(p: Principal) -> None:
        raise QuotaExceeded("Claude 额度已用完")

    cfg = BridgeConfig(scopes=None, model_choices=[{"id": "", "label": "默认"}], check_quota=no_quota)
    b = create_bridge(store=BridgeStore(tmp_path / "b.db"), config=cfg, browser_auth=auth, agent_token="tok")
    app = FastAPI()
    b.mount(app, browser_prefix="/api", agent_prefix="/api/agent", static_prefix=None)
    agent = TestClient(app, headers={"Authorization": "Bearer tok"})
    assert agent.post("/api/agent/backends", json={"backends": [INFO], "balances": {"ds": BALANCE}}).status_code == 200

    def client(who: str = "me") -> TestClient:
        return TestClient(app, headers={"X-Who": who})

    return b, client, agent


def last_payload(b):
    return b.service.store.recent_jobs(1, None)[0]["payload"]


def test_split_backend():
    assert split_backend("@ds") == ("", "ds") and split_backend("code:app@ds") == ("code:app", "ds")
    assert split_backend("code:app") == ("code:app", "") and split_backend("") == ("", "")


def test_only_listed_admins_get_the_switch_and_its_settings(env):
    b, client, _ = env
    me = client()
    s = me.get("/api/settings").json()
    assert s["backends"] == [{"name": "ds", "label": "DeepSeek"}]
    assert [m["id"] for m in s["models"]] == [""]  # Claude's own menu, no backend models mixed in
    ds = me.get("/api/settings", params={"scope": "@ds"}).json()
    assert [m["id"] for m in ds["models"]] == ["deepseek-flash[1m]", "deepseek-v4-pro"]
    assert ds["chat"]["model"] == "deepseek-flash[1m]"  # default: the backend's first model
    assert me.put("/api/settings", params={"scope": "@ds"}, json={"model": "deepseek-v4-pro"}).json()["chat"]["model"] == "deepseek-v4-pro"
    assert me.get("/api/settings").json()["chat"]["model"] == ""  # Claude's settings are separate
    assert me.put("/api/settings", params={"scope": "@ds"}, json={"model": "opus"}).status_code == 400

    for who in ("me-demoted", "other-admin", "user"):
        c = client(who)
        assert c.get("/api/settings").json()["backends"] == []
        assert c.get("/api/settings", params={"scope": "@ds"}).status_code == 400
        assert c.put("/api/settings", params={"scope": "@ds"}, json={"model": ""}).status_code == 400
        r = c.post("/api/send", json={"text": "hi", "scope": "@ds", "new_thread": True})
        assert r.status_code == 400 and "管理员" in r.json()["detail"]


def test_backend_threads_are_their_own_list_and_skip_the_claude_quota(env):
    b, client, _ = env
    me = client()
    r = me.post("/api/send", json={"text": "hi", "scope": "code:app@ds", "new_thread": True})
    assert r.status_code in (200, 201), r.text  # the Claude quota guard (no_quota) is not asked
    p = last_payload(b)
    assert p["scope"] == "code:app@ds" and p["settings"]["model"] == "deepseek-flash[1m]"
    ds_threads = me.get("/api/threads", params={"scope": "code:app@ds"}).json()["items"]
    claude_threads = me.get("/api/threads", params={"scope": "code:app"}).json()["items"]
    assert r.json()["thread"] in [t["id"] for t in ds_threads]
    assert r.json()["thread"] not in [t["id"] for t in claude_threads]
    assert {t["scope"] for t in ds_threads} == {"code:app@ds"} and {t["scope"] for t in claude_threads} <= {"code:app"}
    assert me.post("/api/send", json={"text": "hi", "scope": "code:app", "new_thread": True}).status_code == 429  # Claude still guarded
    bad = me.post("/api/send", json={"text": "x", "scope": "@ds", "new_thread": True, "options": {"model": "opus"}})
    assert bad.status_code == 400 and "没有模型" in bad.json()["detail"]


def test_demoted_admin_cannot_continue_a_backend_thread(env):
    b, client, _ = env
    tid = client().post("/api/send", json={"text": "hi", "scope": "@ds", "new_thread": True}).json()["thread"]
    b.service.store._x("UPDATE bridge_jobs SET status='done'")
    b.service.store._x("UPDATE bridge_messages SET status='done'")
    r = client("me-demoted").post(f"/api/threads/{tid}/messages", json={"text": "again"})
    assert r.status_code == 400


def test_callers_cannot_use_backend_scopes(env):
    b, _, _ = env
    with pytest.raises(BridgeError):
        Callers(b.service.store).add("bot", owner="1", scopes=["@ds"])


def test_backend_usage_goes_to_its_own_ledger(env):
    b, client, agent = env
    me = client()
    me.post("/api/send", json={"text": "hi", "scope": "@ds", "new_thread": True})
    job = agent.post("/api/agent/jobs/next", json={"worker": "w", "kinds": ["chat"], "wait": 0}).json()["job"]
    usage = {"model": "deepseek-flash", "input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
             "output_tokens": 1_000_000, "cache_creation_input_tokens": 0}
    assert agent.post(f"/api/agent/jobs/{job['id']}/events", json={"events": [{"type": "usage", "data": usage}]}).status_code == 200
    assert b.service.store.usage_totals(utc_text(0)) == {}  # nothing in the Claude ledger
    me_info = b.service.backend_usage("1")[0]
    day = me_info["usage"]["day"]
    assert day["turns"] == 1 and day["input_tokens"] == 1_000_000 and day["cache_read_tokens"] == 1_000_000
    assert day["cost_usd"] in (pytest.approx(0.753), pytest.approx(1.506))  # off-peak or peak
    assert me_info["balance"] == BALANCE
    assert b.service.backend_usage("2") == []
    rl = {"status": "allowed", "five_hour": {"utilization": 0.9, "resets_at": None}, "seven_day": None}
    agent.post(f"/api/agent/jobs/{job['id']}/events", json={"events": [{"type": "rate_limit", "data": rl}]})
    assert "utilization" not in (b.service.account_limits()["five_hour"])  # not the Claude subscription's


def test_peak_hours_and_prices():
    mon = datetime(2026, 11, 16, 2, 30, tzinfo=UTC)
    assert is_peak(mon, PEAK) and not is_peak(mon.replace(hour=5), PEAK) and is_peak(mon.replace(hour=9, minute=59), PEAK)
    assert not is_peak(datetime(2026, 11, 14, 2, 30, tzinfo=UTC), PEAK)  # Saturday
    assert not is_peak(mon, {})
    assert price_for(PRICING, "deepseek-flash[1m]") == PRICING["flash"] and price_for(PRICING, "other") is None
    u = {"input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert turn_cost(INFO, "deepseek-flash", u, mon.replace(hour=5)) == (pytest.approx(0.753), False)
    assert turn_cost(INFO, "deepseek-flash", u, mon) == (pytest.approx(1.506), True)


def test_external_models_never_cost_claude_quota():
    assert is_external("deepseek-flash") and is_external("deepseek-v4-pro")
    for m in ("claude-opus-5-5", "opus", "sonnet[1m]", "us.anthropic.claude-sonnet-4-5", "", "<synthetic>", "default"):
        assert not is_external(m)
    assert usage_cost("deepseek-flash", {"input_tokens": 10_000, "output_tokens": 5_000}) == 0.0


def test_local_scan_skips_backend_turns(tmp_path):
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)

    def line(mid, model):
        return json.dumps({"type": "assistant", "timestamp": "2026-11-15T00:00:00Z", "cwd": "/x",
                           "message": {"id": mid, "model": model, "usage": {"input_tokens": 100, "output_tokens": 50}}})
    (root / "s.jsonl").write_text(line("m1", "deepseek-flash") + "\n" + line("m2", "claude-sonnet-5-5") + "\n")
    rows = TranscriptScanner(root=tmp_path / "projects").scan(now=1_763_164_800 + 60)
    assert [r["model"] for r in rows] == ["claude-sonnet-5-5"]


def test_worker_backend_scope_env_and_report(tmp_path, monkeypatch):
    f = tmp_path / "backends.json"
    f.write_text(json.dumps({"_comment": "x", "ds": {
        "label": "DeepSeek", "owners": [1], "pricing": PRICING, "peak": PEAK, "balance_url": "https://api.example.com/user/balance",
        "env": {"ANTHROPIC_BASE_URL": "https://api.example.com/anthropic", "ANTHROPIC_AUTH_TOKEN": "sk-secret-123"},
        "models": [{"id": "deepseek-flash[1m]", "name": "Flash (1M)"}]}}))
    backends = load_backends(f)
    assert backends["ds"].owners == ["1"]
    with pytest.raises(ValueError):
        Backend.from_dict("x", {"env": {}, "models": [], "owners": []})

    profiles = {"code": WorkerProfile(cwd=str(tmp_path / "code" / "{owner}" / "{project}"), permission_mode="bypassPermissions")}
    w = Worker(client=None, config=WorkerConfig(backends=backends, profiles=profiles, extra_env={"A": "1"}))  # type: ignore[arg-type]
    assert w.config_for({"scope": "", "owner": "1"}) is w.config  # Claude: untouched
    cfg = w.config_for({"scope": "code:app@ds", "owner": "1"})
    assert cfg.cwd == tmp_path / "code" / "u1" / "app" and cfg.permission_mode == "bypassPermissions"  # same project dir
    assert cfg.extra_env == {"A": "1", "ANTHROPIC_BASE_URL": "https://api.example.com/anthropic", "ANTHROPIC_AUTH_TOKEN": "sk-secret-123"}
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "claude-key")
    env = w._session_env(cfg)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-secret-123"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env  # Claude credentials stay home
    assert w._session_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth"
    with pytest.raises(RuntimeError, match="不能使用"):
        w.config_for({"scope": "@ds", "owner": "2"})
    with pytest.raises(RuntimeError, match="没有配置"):
        w.config_for({"scope": "@nope", "owner": "1"})

    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return BALANCE

    def fake_get(url, headers, timeout):
        seen.update(url=url, auth=headers["Authorization"])
        return Resp()

    monkeypatch.setattr("claude_bridge.worker.requests.get", fake_get)
    rep = w.backend_report()
    assert rep["balances"] == {"ds": BALANCE} and seen == {"url": "https://api.example.com/user/balance", "auth": "Bearer sk-secret-123"}
    assert rep["backends"][0]["pricing"] == PRICING and rep["backends"][0]["owners"] == ["1"]
    assert "sk-secret-123" not in json.dumps(rep)  # the API key never leaves the worker
