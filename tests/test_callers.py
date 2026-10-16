"""Caller tokens and /api/caller/*: one-off tasks from server-side programs, run as the bound account."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from claude_bridge import accounts as accounts_mod
from claude_bridge.accounts import Accounts
from claude_bridge.callers import Callers, error_kind, hash_token
from claude_bridge.cli import main
from claude_bridge.multiuser import create_multiuser_app
from claude_bridge.store import BridgeStore
from claude_bridge.worker import Worker, WorkerConfig

PW = "password-1"


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_mod, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture
def setup(tmp_path):
    db = tmp_path / "c.db"
    store = BridgeStore(db)
    acc = Accounts(store)
    boss = acc.create("boss", PW, role="admin")
    acc.create("alice", PW)
    callers = Callers(store)
    _, token = callers.add("app", owner=str(boss["id"]), scopes=["task"], max_inflight=2)
    _, other = callers.add("other", owner=str(boss["id"]), scopes=["task"])
    store.close()
    app = create_multiuser_app(db_path=db, agent_token="tok", tz="UTC", scopes=("task",))
    return app, token, other, boss


def client(app, token):
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def online(agent):
    agent.post("/api/agent/jobs/next", json={"kinds": ["none"], "wait": 0})


def claim(agent):
    return agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}).json()["job"]


def finish(agent, job_id, text='{"ok":true}', model="claude-haiku-5-5"):
    usage = {"input_tokens": 120, "output_tokens": 30, "model": model}
    agent.post(f"/api/agent/jobs/{job_id}/events", json={"deltas": [text], "events": [{"type": "usage", "data": usage}]})
    agent.post(f"/api/agent/jobs/{job_id}/finish", json={"ok": True, "result": json.dumps(usage), "session_id": "s1"})


def test_auth(setup):
    app, token, _, _ = setup
    assert TestClient(app).get("/api/caller/status").status_code == 401
    assert client(app, "tok").get("/api/caller/status").status_code == 401  # the worker token is not a caller token
    assert client(app, "cbc_wrong").get("/api/caller/status").status_code == 401
    assert client(app, token).get("/api/caller/status").status_code == 200
    # a caller token opens nothing else
    assert client(app, token).get("/api/threads").status_code == 401
    assert client(app, token).post("/api/agent/jobs/next", json={"wait": 0}).status_code == 401


def test_offline_scope_and_happy_path(setup):
    app, token, _, boss = setup
    c, agent = client(app, token), TestClient(app, headers={"Authorization": "Bearer tok"})
    body = {"text": "分类这些商家", "scope": "task", "options": {"model": "haiku", "max_turns": 1, "instructions": "只输出 JSON"}}
    r = c.post("/api/caller/tasks", json=body)
    assert r.status_code == 503 and r.json()["error_kind"] == "offline"
    online(agent)
    assert c.post("/api/caller/tasks", json={**body, "scope": ""}).json()["error_kind"] == "scope"
    r = c.post("/api/caller/tasks", json=body)
    assert r.status_code == 201, r.text
    tid = r.json()["task_id"]
    assert c.get(f"/api/caller/tasks/{tid}").json()["status"] == "queued"

    job = claim(agent)
    p = job["payload"]
    assert p["ephemeral"] is True and p["caller"] == "app" and p["owner"] == str(boss["id"]) and p["scope"] == "task"
    assert p["settings"]["model"] == "haiku" and p["settings"]["instructions"] == "只输出 JSON" and not p["session_id"]
    assert c.get(f"/api/caller/tasks/{tid}").json()["status"] == "running"
    finish(agent, job["id"])
    out = c.get(f"/api/caller/tasks/{tid}").json()
    assert out["status"] == "done" and out["text"] == '{"ok":true}' and out["model"] == "claude-haiku-5-5"
    assert out["usage"]["output_tokens"] == 30 and out["elapsed_ms"] >= 0

    # booked to the bound account; the task's thread is not in that account's chat list
    store = app.state.bridge.store
    owners = {r["owner"] for r in store._q("SELECT owner FROM bridge_usage")}
    assert owners == {str(boss["id"])}
    web = TestClient(app, follow_redirects=False)
    web.post("/login", data={"username": "boss", "password": PW})
    assert all(t["scope"] == "" for t in web.get("/api/threads").json()["items"])


def test_other_caller_cannot_see_task_and_busy_limit(setup):
    app, token, other, _ = setup
    c, agent = client(app, token), TestClient(app, headers={"Authorization": "Bearer tok"})
    online(agent)
    body = {"text": "x", "scope": "task"}
    ids = [c.post("/api/caller/tasks", json=body).json()["task_id"] for _ in range(2)]
    r = c.post("/api/caller/tasks", json=body)
    assert r.status_code == 429 and r.json()["error_kind"] == "busy"
    assert client(app, other).get(f"/api/caller/tasks/{ids[0]}").status_code == 404
    assert c.post(f"/api/caller/tasks/{ids[0]}/cancel").json()["status"] == "cancelled"
    out = c.get(f"/api/caller/tasks/{ids[0]}").json()
    assert out["status"] == "cancelled" and out["error_kind"] == "cancelled"
    assert c.post("/api/caller/tasks", json=body).status_code == 201  # a slot is free again


def test_timeout_and_failure_kinds(setup):
    app, token, _, _ = setup
    c, agent = client(app, token), TestClient(app, headers={"Authorization": "Bearer tok"})
    online(agent)
    tid = c.post("/api/caller/tasks", json={"text": "x", "scope": "task", "timeout": 5}).json()["task_id"]
    store = app.state.bridge.store
    store._x("UPDATE bridge_caller_tasks SET deadline=? WHERE id=?", (time.time() - 1, tid))
    out = c.get(f"/api/caller/tasks/{tid}").json()
    assert out["status"] == "cancelled" and out["error_kind"] == "timeout"

    tid = c.post("/api/caller/tasks", json={"text": "y", "scope": "task"}).json()["task_id"]
    job = claim(agent)
    agent.post(f"/api/agent/jobs/{job['id']}/finish",
               json={"ok": False, "error": "Claude 返回错误：Claude AI usage limit reached", "error_kind": "claude"})
    out = c.get(f"/api/caller/tasks/{tid}?wait=1").json()
    assert out["status"] == "failed" and out["error_kind"] == "quota"


def test_error_kind_mapping():
    assert error_kind("claude", "Claude 返回错误：rate limit") == "quota"
    assert error_kind("timeout", "Claude 超过 15 分钟未完成") == "timeout"
    assert error_kind("claude", "boom") == "claude"
    assert error_kind("stale", "helper 心跳超时") == "worker"
    assert error_kind(None, None) == "worker"


def test_disabled_owner_and_cleanup(setup):
    app, token, _, boss = setup
    c, agent = client(app, token), TestClient(app, headers={"Authorization": "Bearer tok"})
    online(agent)
    tid = c.post("/api/caller/tasks", json={"text": "x", "scope": "task"}).json()["task_id"]
    finish(agent, claim(agent)["id"])
    assert c.get(f"/api/caller/tasks/{tid}").json()["status"] == "done"
    store = app.state.bridge.store
    thread = store._one("SELECT thread FROM bridge_caller_tasks WHERE id=?", (tid,))["thread"]
    store._x("UPDATE bridge_caller_tasks SET finished=? WHERE id=?", (time.time() - 7200, tid))
    c.post("/api/caller/tasks", json={"text": "z", "scope": "task"})  # submitting sweeps expired tasks
    assert store.get_thread(thread) is None and c.get(f"/api/caller/tasks/{tid}").status_code == 404
    assert store._one("SELECT COUNT(*) AS n FROM bridge_usage")["n"] == 1  # usage outlives the thread

    store._x("UPDATE bridge_users SET disabled=1 WHERE id=?", (boss["id"],))
    r = c.post("/api/caller/tasks", json={"text": "x", "scope": "task"})
    assert r.status_code == 403 and r.json()["error_kind"] == "owner"


def test_callers_cli(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    assert main(["users", "--db", db, "add", "root", "--admin", "--password", "root-password"]) == 0
    capsys.readouterr()
    assert main(["callers", "--db", db, "add", "svc", "--user", "root", "--scope", "task", "--token-only"]) == 0
    token = capsys.readouterr().out.strip()
    assert token.startswith("cbc_") and "\n" not in token
    assert main(["callers", "--db", db, "add", "svc", "--scope", "task"]) == 1  # duplicate
    assert main(["callers", "--db", db, "add", "Bad Name", "--scope", "task"]) == 1
    assert main(["callers", "--db", db, "add", "ghost", "--user", "nobody", "--scope", "task"]) == 1
    capsys.readouterr()
    assert main(["callers", "--db", db, "list"]) == 0
    listing = capsys.readouterr().out
    assert "svc" in listing and token not in listing
    store = BridgeStore(db)
    row = store._one("SELECT * FROM bridge_callers WHERE name='svc'")
    assert row["token_hash"] == hash_token(token) and row["owner"] == "1"
    store.close()
    assert main(["callers", "--db", db, "rotate", "svc", "--token-only"]) == 0
    new = capsys.readouterr().out.strip()
    store = BridgeStore(db)
    callers = Callers(store)
    assert callers.verify(token) is None and callers.verify(new)["name"] == "svc"
    assert main(["callers", "--db", db, "disable", "svc"]) == 0
    assert callers.verify(new) is None
    store.close()


class _FakeClient:
    pass


def test_worker_ephemeral_flag():
    worker = Worker(_FakeClient(), WorkerConfig(claude_bin="claude"))  # type: ignore[arg-type]
    cmd, _ = worker.build_chat_command("hi", "sess", {}, "", ephemeral=True)
    assert "--no-session-persistence" in cmd and "--resume" not in cmd
    cmd, _ = worker.build_chat_command("hi", "sess", {}, "")
    assert "--no-session-persistence" not in cmd and cmd[cmd.index("--resume") + 1] == "sess"
