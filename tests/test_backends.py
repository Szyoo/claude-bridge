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
        {"id": "deepseek-v4-pro", "name": "Pro"}], "pricing": PRICING, "peak": PEAK, "has_key": True}
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


def test_key_entered_on_the_page(env):
    b, client, agent = env
    no_key = {**INFO, "has_key": False}
    assert agent.post("/api/agent/backends", json={"backends": [no_key]}).status_code == 200
    me = client()
    r = me.post("/api/send", json={"text": "hi", "scope": "@ds", "new_thread": True})
    assert r.status_code == 400 and "API key" in r.json()["detail"]
    assert client("user").put("/api/backends/ds/key", json={"key": "sk-x"}).status_code == 400
    assert client("other-admin").put("/api/backends/ds/key", json={"key": "sk-x"}).status_code == 400
    assert client("me-demoted").put("/api/backends/ds/key", json={"key": "sk-x"}).status_code == 400
    assert me.put("/api/backends/ds/key", json={"key": "has space"}).status_code == 400
    r = me.put("/api/backends/ds/key", json={"key": "sk-secret-abcd"})
    assert r.status_code == 200 and r.json() == {"source": "page", "hint": "abcd"}
    assert "sk-secret" not in json.dumps(b.service.backend_usage("1"))  # never back to a browser
    synced = agent.post("/api/agent/config", json={"worker": "w", "concurrency": 1, "local_concurrency": 1, "running": 0}).json()
    assert synced["backend_keys"] == {"ds": "sk-secret-abcd"}
    assert me.post("/api/send", json={"text": "hi", "scope": "@ds", "new_thread": True}).status_code in (200, 201)
    assert me.put("/api/backends/ds/key", json={"key": None}).json() == {"source": None, "hint": ""}


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


def test_worker_uses_the_page_key_when_its_file_has_none(tmp_path):
    backends = {"ds": Backend.from_dict("ds", {"owners": ["1"], "models": [{"id": "deepseek-flash"}],
                                               "env": {"ANTHROPIC_BASE_URL": "https://api.example.com/anthropic"}})}
    assert backends["ds"].public("ds")["has_key"] is False

    class Client:
        keys: dict = {}

        def sync_config(self, report):
            return {"concurrency": None, "backend_keys": self.keys}

    client = Client()
    w = Worker(client=client, config=WorkerConfig(backends=backends))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="API key"):
        w.config_for({"scope": "@ds", "owner": "1"})
    client.keys = {"ds": "sk-page-1"}
    w.maybe_sync_config(force=True)
    assert w.config_for({"scope": "@ds", "owner": "1"}).extra_env["ANTHROPIC_AUTH_TOKEN"] == "sk-page-1"
    assert "sk-page-1" not in json.dumps(w.backend_report()["backends"])


def test_backend_profile_overrides_the_scope_profile(tmp_path):
    base = {"owners": ["1"], "models": [{"id": "deepseek-v4-pro[1m]"}],
            "env": {"ANTHROPIC_BASE_URL": "https://api.example.com/anthropic", "ANTHROPIC_AUTH_TOKEN": "sk-x"}}
    ds = Backend.from_dict("ds", {**base, "profile": {"chat_timeout": 21600, "idle_timeout": 2400, "system_prompt": "逆向"}})
    profiles = {"code": WorkerProfile(cwd=str(tmp_path / "{owner}" / "{project}"), system_prompt="编程", chat_timeout=60)}
    w = Worker(client=None, config=WorkerConfig(backends={"ds": ds}, profiles=profiles))  # type: ignore[arg-type]
    cfg = w.config_for({"scope": "code:app@ds", "owner": "1"})
    assert (cfg.chat_timeout, cfg.idle_timeout, cfg.system_prompt) == (21600, 2400, "逆向")
    assert str(cfg.cwd).endswith("1/app")  # the directory still comes from the scope
    claude = w.config_for({"scope": "code:app", "owner": "1"})
    assert (claude.chat_timeout, claude.system_prompt) == (60, "编程")
    with pytest.raises(ValueError, match="cwd"):
        Backend.from_dict("ds", {**base, "profile": {"cwd": "/tmp"}})
    with pytest.raises(ValueError, match="unknown"):
        Backend.from_dict("ds", {**base, "profile": {"nope": 1}})


def test_local_projects_are_the_backends_own_folders(tmp_path):
    def auth(x_who: str = Header("me")) -> Principal:
        return WHO[x_who]

    store = BridgeStore(tmp_path / "b.db")
    holder = {}
    cfg = BridgeConfig(projects=True, scopes=lambda scope, owner: scope == "" or (
        scope.startswith("code:") and holder["b"].service.project_ready(owner, scope[5:])))
    b = holder["b"] = create_bridge(store=store, config=cfg, browser_auth=auth, agent_token="tok")
    app = FastAPI()
    b.mount(app, browser_prefix="/api", agent_prefix="/api/agent", static_prefix=None)
    agent = TestClient(app, headers={"Authorization": "Bearer tok"})
    me, other = TestClient(app, headers={"X-Who": "me"}), TestClient(app, headers={"X-Who": "other-admin"})
    agent.post("/api/agent/backends", json={"backends": [{**INFO, "local_projects": True}], "balances": {}})
    agent.post("/api/agent/config", json={"worker": "w", "concurrency": 1, "local_concurrency": 1, "running": 0,
                                          "backend_projects": {"ds": [{"name": "teardown", "path": "/x/GitHub/teardown",
                                                                       "git": True, "branch": "main", "mtime": 1}]}})
    d = me.get("/api/projects", params={"backend": "ds"}).json()
    assert d["local"] is True and [(p["name"], p["local"], p["info"]["branch"]) for p in d["items"]] == [("teardown", True, "main")]
    assert me.get("/api/projects").json()["items"] == []  # Claude's own (per-owner) projects stay apart
    assert other.get("/api/projects", params={"backend": "ds"}).status_code == 400  # not an owner of the backend

    assert me.post("/api/send", json={"text": "hi", "scope": "code:teardown@ds", "new_thread": True}).status_code in (200, 201)
    assert me.post("/api/send", json={"text": "hi", "scope": "code:nope@ds", "new_thread": True}).status_code == 400
    assert me.get("/api/projects", params={"backend": "ds"}).json()["items"][0]["threads"] == 1

    assert me.delete("/api/projects/teardown", params={"backend": "ds"}).status_code == 400
    assert me.post("/api/projects", params={"backend": "ds"}, json={"name": "teardown"}).status_code == 400
    r = me.post("/api/projects", params={"backend": "ds"}, json={"clone_url": "https://github.com/a/new-repo.git"})
    assert r.status_code == 202 and r.json()["project"]["status"] == "creating"
    job = last_payload(b)
    assert job["scope"] == "code:new-repo@ds" and job["owner"] == "1" and job["row_owner"] == "1@ds"
    names = [p["name"] for p in me.get("/api/projects", params={"backend": "ds"}).json()["items"]]
    assert names == ["new-repo", "teardown"]


def test_worker_runs_local_projects_in_the_backends_folder(tmp_path):
    root = tmp_path / "GitHub"
    (root / "teardown" / ".git").mkdir(parents=True)
    (root / "teardown" / ".git" / "HEAD").write_text("ref: refs/heads/dev\n")
    (root / ".hidden").mkdir()
    ds = Backend.from_dict("ds", {"owners": ["1"], "models": [{"id": "m"}], "projects_root": str(root),
                                  "env": {"ANTHROPIC_BASE_URL": "https://api.example.com/anthropic", "ANTHROPIC_AUTH_TOKEN": "k"}})
    assert [(p["name"], p["branch"], p["git"]) for p in ds.list_projects()] == [("teardown", "dev", True)]
    assert ds.public("ds")["local_projects"] is True
    profiles = {"code": WorkerProfile(cwd=str(tmp_path / "iso" / "{owner}" / "{project}"))}
    w = Worker(client=None, config=WorkerConfig(backends={"ds": ds}, profiles=profiles))  # type: ignore[arg-type]
    assert w.config_for({"scope": "code:teardown@ds", "owner": "1"}).cwd == root / "teardown"
    assert not (tmp_path / "iso").exists()  # no isolated directory made for it
    with pytest.raises(RuntimeError, match="没有项目文件夹"):
        w.config_for({"scope": "code:gone@ds", "owner": "1"})
    assert w._project_path({"scope": "code:new@ds", "name": "new", "owner": "1", "action": "create"}) == root / "new"
    with pytest.raises(RuntimeError, match="不会删除"):
        w._project_path({"scope": "code:teardown@ds", "name": "teardown", "owner": "1", "action": "delete"})
    with pytest.raises(RuntimeError, match="不能使用"):
        w._project_path({"scope": "code:new@ds", "name": "new", "owner": "2", "action": "create"})


def test_texts_name_the_backend_that_answers(env):
    b, _, _ = env
    svc = b.service
    assert svc.assistant_name("code:app@ds") == "DeepSeek" and svc.assistant_name("code:app") == "Claude"
    svc.config.new_thread_notice = "新对话已开始，Claude 不再记得之前的内容。"
    tid = svc.new_thread("@ds", owner="1")
    assert svc.store.messages(tid)[0]["content"] == "新对话已开始，DeepSeek 不再记得之前的内容。"


def test_static_files_are_revalidated(tmp_path):
    b = create_bridge(store=BridgeStore(tmp_path / "b.db"), config=BridgeConfig(), browser_auth=lambda: WHO["me"], agent_token="t")
    app = FastAPI()
    b.mount(app, browser_prefix="/api", agent_prefix="/api/agent", static_prefix="/static/bridge")
    r = TestClient(app).get("/static/bridge/bridge-widget.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and r.headers.get("etag")
