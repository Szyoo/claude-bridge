"""Spend-rate watch, the emergency stop, and the deployment plugin hooks."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from claude_bridge import accounts as accounts_mod
from claude_bridge.accounts import Accounts
from claude_bridge.multiuser import create_multiuser_app
from claude_bridge.plugins import load_plugin
from claude_bridge.store import BridgeStore

PW = "correct-horse"


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_mod, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture
def app(tmp_path):
    store = BridgeStore(tmp_path / "h.db")
    acc = Accounts(store)
    acc.create("boss", PW, role="admin")
    acc.create("alice", PW)
    acc.create("bob", PW)
    store.close()
    return create_multiuser_app(db_path=tmp_path / "h.db", agent_token="tok", tz="UTC")


def login(app, username):
    c = TestClient(app, follow_redirects=False)
    assert c.post("/login", data={"username": username, "password": PW}).status_code == 303
    return c


@pytest.fixture
def agent(app):
    return TestClient(app, headers={"Authorization": "Bearer tok"})


def next_job(agent):
    return agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}).json()["job"]


def spend(usd):  # Sonnet 5.5 output: $10 / Mtok
    return {"model": "claude-sonnet-5-5", "input_tokens": 0, "output_tokens": round(usd * 100_000)}


def uids(app):
    return {u["username"]: str(u["id"]) for u in app.state.accounts.users()}


def test_live_spend_flags_a_fast_spender(app, agent):
    boss, alice = login(app, "boss"), login(app, "alice")
    boss.put("/api/admin/quota", json={"cap_5h_usd": 10, "guard_5h_pct": 50})  # alice's 5h allowance: $5
    assert alice.post("/api/send", json={"text": "big job"}).status_code == 201
    job = next_job(agent)
    # a minute in, the running turn has spent $1.20 (24% of her allowance): reported live, never stored as an event
    r = agent.post(f"/api/agent/jobs/{job['id']}/events", json={"events": [{"type": "live_usage", "data": {"by_model": [spend(1.2)]}}]})
    assert r.json()["cancel"] is False
    tid = alice.get("/api/threads").json()["items"][0]["id"]
    assert not [e for m in alice.get(f"/api/threads/{tid}").json()["messages"] for e in m.get("events", []) if e["type"] == "live_usage"]
    svc = app.state.bridge.service
    assert svc.is_hot(uids(app)["alice"])

    guard = boss.get("/api/admin/guard").json()
    assert [h["name"] for h in guard["hot"]] == ["alice（alice）"] and "24%" in guard["hot"][0]["reason"]
    assert guard["hot"][0]["recent_usd"] == pytest.approx(1.2) and guard["hot"][0]["allowance_usd"] == pytest.approx(5.0)

    # the final booking only adds what the live reports had not covered
    agent.post(f"/api/agent/jobs/{job['id']}/events", json={"events": [{"type": "usage", "data": spend(1.5)}]})
    agent.post(f"/api/agent/jobs/{job['id']}/finish", json={"ok": True, "result": "{}", "session_id": "s1"})
    assert svc.heat.recent(uids(app)["alice"]) == pytest.approx(1.5)

    boss.delete(f"/api/admin/guard/hot/{uids(app)['alice']}")
    assert boss.get("/api/admin/guard").json()["hot"] == []


def test_many_turns_at_once_is_hot_and_admins_are_never_watched(app):
    login(app, "boss").put("/api/admin/quota", json={"cap_5h_usd": 10})
    svc, uid = app.state.bridge.service, uids(app)
    assert svc.heat.check(uid["bob"], running=5, force=True) == "同时运行 5 个任务"
    assert svc.heat.check(uid["bob"], running=0, force=True) is None and svc.heat.is_hot(uid["bob"])  # stays a while
    assert svc.heat.check(uid["boss"], running=50, force=True) is None


class Plugin:
    def turn_settings(self, owner, thread, settings, message_id):
        return {**settings, "model": "plug-model", "subagent_model": "plug-sub"}

    def event_view(self, owner, message_id, type, data):
        return {**data, "model": "shown-model"} if type in ("init", "result") and isinstance(data, dict) else data

    def context_view(self, thread_id, ctx):
        return {**ctx, "model": "shown-model"}


def test_plugin_hooks_adjust_turns_and_what_pages_receive(app, agent, tmp_path):
    f = tmp_path / "plug.py"
    f.write_text("class P:\n    def __init__(self, svc):\n        self.svc = svc\n\ndef setup(svc):\n    return P(svc)\n")
    svc = app.state.bridge.service
    assert load_plugin(str(f), svc).svc is svc and load_plugin("", svc) is None

    svc.config.plugin = Plugin()
    boss, alice = login(app, "boss"), login(app, "alice")
    assert alice.post("/api/send", json={"text": "hi"}).status_code == 201
    job = next_job(agent)
    assert job["payload"]["settings"]["model"] == "plug-model" and job["payload"]["settings"]["subagent_model"] == "plug-sub"
    agent.post(f"/api/agent/jobs/{job['id']}/events", json={"deltas": ["ok"], "events": [{"type": "init", "data": {"model": "real-model"}}]})
    agent.post(f"/api/agent/jobs/{job['id']}/finish", json={"ok": True, "result": "{}", "session_id": "s1"})
    tid = alice.get("/api/threads").json()["items"][0]["id"]
    evs = [e for m in alice.get(f"/api/threads/{tid}").json()["messages"] for e in m.get("events", [])]
    assert [e["data"]["model"] for e in evs if e["type"] == "init"] == ["shown-model"]
    items = boss.get(f"/api/admin/users/{uids(app)['alice']}/threads/{tid}/messages").json()["items"]
    assert [e["data"]["model"] for m in items for e in m.get("events", []) if e["type"] == "init"] == ["real-model"]  # stored as is


def test_emergency_stop_holds_ordinary_users_but_not_the_admin(app, agent):
    boss, bob = login(app, "boss"), login(app, "bob")
    assert bob.post("/api/send", json={"text": "long"}).status_code == 201
    job = next_job(agent)
    r = boss.put("/api/admin/emergency", json={"on": True}).json()
    assert r["emergency"] is True and r["stopped"] == 1
    assert agent.post(f"/api/agent/jobs/{job['id']}/events", json={"deltas": ["…"]}).json()["cancel"] is True
    agent.post(f"/api/agent/jobs/{job['id']}/finish", json={"ok": False, "cancelled": True})
    tid = bob.get("/api/threads").json()["items"][0]["id"]
    msg = bob.get(f"/api/threads/{tid}").json()["messages"][-1]
    assert msg["status"] == "cancelled" and "⛔ 5 小时额度已满" in msg["content"]

    r = bob.post("/api/send", json={"text": "more"})
    assert r.status_code == 429 and r.json()["detail"].startswith("5 小时额度已满")
    assert bob.get("/api/me").json()["usage"]["blocked"].startswith("5 小时额度已满")
    assert boss.post("/api/send", json={"text": "admin carries on"}).status_code == 201

    assert boss.put("/api/admin/emergency", json={"on": False}).json()["emergency"] is False
    assert bob.post("/api/send", json={"text": "back", "new_thread": True}).status_code == 201


def test_guard_endpoints_are_admin_only(app):
    bob = login(app, "bob")
    assert bob.get("/api/admin/guard").status_code == 403
    assert bob.put("/api/admin/emergency", json={"on": True}).status_code == 403
