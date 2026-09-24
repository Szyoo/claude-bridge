"""serve --multi-user: accounts, isolation between users, long-lived sessions, quotas, the admin API, the users CLI."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from claude_bridge import accounts as accounts_mod
from claude_bridge.accounts import Accounts, hash_password, verify_password
from claude_bridge.cli import main
from claude_bridge.multiuser import create_multiuser_app
from claude_bridge.store import BridgeStore
from claude_bridge.stream_json import parse_usage_report

PW = "password-1"


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_mod, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture
def app(tmp_path):
    db = tmp_path / "m.db"
    store = BridgeStore(db)
    acc = Accounts(store)
    acc.create("boss", PW, role="admin")
    acc.create("alice", PW, display_name="Alice")
    acc.create("bob", PW)
    store.close()
    return create_multiuser_app(db_path=db, agent_token="tok", tz="UTC")


def login(app, username, password=PW) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"username": username, "password": password})
    assert r.status_code == 303 and r.headers["location"] == "/", r.headers.get("location")
    return c


@pytest.fixture
def agent(app):
    return TestClient(app, headers={"Authorization": "Bearer tok"})


def finish_turn(agent, job_id, cost, rate_limit=None):
    events = [{"type": "usage", "data": {"total_cost_usd": cost, "input_tokens": 10, "output_tokens": 5, "model": "m"}}]
    if rate_limit:
        events.insert(0, {"type": "rate_limit", "data": rate_limit})
    agent.post(f"/api/agent/jobs/{job_id}/events", json={"deltas": ["ok"], "events": events})
    agent.post(f"/api/agent/jobs/{job_id}/finish", json={"ok": True, "result": "{}", "session_id": f"s{job_id}"})


def run_turn(web, agent, cost, text="hi", rate_limit=None):
    r = web.post("/api/send", json={"text": text})
    assert r.status_code == 201, r.text
    job = agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}).json()["job"]
    finish_turn(agent, job["id"], cost, rate_limit)
    return r.json()


# ---------------- passwords / login ----------------


def test_password_hash_roundtrip():
    h = hash_password("secret-pw")
    assert h.startswith("pbkdf2_sha256$") and verify_password("secret-pw", h) and not verify_password("nope", h)
    assert not verify_password("x", "garbage")


def test_login_logout_and_pages(app):
    c = TestClient(app, follow_redirects=False)
    assert c.get("/").headers["location"] == "/login"
    assert c.get("/api/threads").status_code == 401 and c.get("/api/me").status_code == 401
    r = c.post("/login", data={"username": "alice", "password": "wrong"})
    assert r.headers["location"] == "/login?err=1&u=alice"
    page = c.get("/login?err=1&u=alice").text
    assert "用户名或密码不对" in page and 'value="alice"' in page

    c = login(app, "ALICE")  # usernames are case-insensitive
    assert c.get("/").status_code == 200 and "bridge-widget" in c.get("/").text
    assert c.get("/account").status_code == 200
    assert c.get("/admin").headers["location"] == "/"  # not an admin
    assert c.get("/api/admin/users").status_code == 403
    me = c.get("/api/me").json()
    assert me["user"]["username"] == "alice" and me["user"]["display_name"] == "Alice" and "pw_hash" not in me["user"]
    assert c.post("/logout").headers["location"] == "/login" and c.get("/api/me").status_code == 401

    boss = login(app, "boss")
    assert boss.get("/admin").status_code == 200


def test_login_rate_limit_per_username(app):
    c = TestClient(app, follow_redirects=False)
    for _ in range(5):
        c.post("/login", data={"username": "bob", "password": "wrong"}, headers={"x-forwarded-for": f"10.0.0.{_}"})
    r = c.post("/login", data={"username": "bob", "password": PW}, headers={"x-forwarded-for": "10.9.9.9"})
    assert r.headers["location"].startswith("/login?err=limited")


def test_session_is_long_lived_and_renewed(app):
    acc: Accounts = app.state.accounts
    assert acc.session_seconds == 365 * 86400
    c = login(app, "alice")
    token = c.cookies.get("bridge_session")
    assert acc.token_age(token) < 5
    # a day-old cookie gets reissued when a page is opened
    user = acc.by_name("alice")
    old = acc.issue_token(user)
    uid, ver, exp, _ = old.split(".")
    payload = f"{uid}.{ver}.{int(exp) - 2 * 86400}"
    stale = TestClient(app, follow_redirects=False, cookies={"bridge_session": f"{payload}.{acc._sign(payload)}"})
    r = stale.get("/")
    fresh = r.headers.get("set-cookie", "").split("bridge_session=", 1)[1].split(";", 1)[0]
    assert r.status_code == 200 and acc.token_age(fresh) < 5
    assert "set-cookie" not in c.get("/").headers  # a fresh cookie is left alone


def test_password_change_signs_out_other_devices(app):
    phone, laptop = login(app, "alice"), login(app, "alice")
    assert phone.post("/api/me/password", json={"current": "wrong", "new": "new-password"}).status_code == 400
    assert phone.post("/api/me/password", json={"current": PW, "new": "short"}).status_code == 400
    assert phone.post("/api/me/password", json={"current": PW, "new": "new-password"}).status_code == 200
    assert phone.get("/api/me").status_code == 200  # this device got a fresh cookie
    assert laptop.get("/api/me").status_code == 401
    login(app, "alice", "new-password")


def test_rename_self(app):
    c = login(app, "alice")
    assert c.patch("/api/me", json={"username": "bob"}).status_code == 400  # taken
    assert c.patch("/api/me", json={"username": "a"}).status_code == 400  # too short
    r = c.patch("/api/me", json={"username": "alice2", "display_name": "爱丽丝"})
    assert r.status_code == 200 and r.json()["user"]["username"] == "alice2"
    assert c.get("/api/me").status_code == 200  # the session follows the id, not the name
    login(app, "alice2")


# ---------------- isolation ----------------


def test_threads_settings_and_files_are_per_user(app, agent):
    alice, bob = login(app, "alice"), login(app, "bob")
    a = run_turn(alice, agent, 0.01, text="alice secret")
    a_thread = a["thread"]
    b_current = bob.get("/api/threads").json()["current"]
    assert b_current != a_thread
    assert [t["id"] for t in bob.get("/api/threads").json()["items"]] == [b_current]
    assert a_thread in [t["id"] for t in alice.get("/api/threads").json()["items"]]

    for method, path in [("GET", f"/api/threads/{a_thread}"), ("GET", f"/api/threads/{a_thread}/messages"),
                         ("GET", f"/api/threads/{a_thread}/stream"), ("POST", f"/api/threads/{a_thread}/select"),
                         ("PATCH", f"/api/threads/{a_thread}"), ("DELETE", f"/api/threads/{a_thread}"),
                         ("POST", f"/api/threads/{a_thread}/messages"), ("POST", f"/api/threads/{a_thread}/compact"),
                         ("POST", f"/api/messages/{a['message_id']}/cancel")]:
        body = {"text": "x"} if "messages" in path and method == "POST" else ({"title": "x"} if method == "PATCH" else None)
        r = bob.request(method, path, json=body)
        assert r.status_code == 404, (method, path, r.status_code)

    assert bob.get(f"/api/jobs/{a['job_id']}").status_code == 404
    assert bob.get("/api/jobs").json()["items"] == []
    assert [j["id"] for j in alice.get("/api/jobs").json()["items"]] == [a["job_id"]]

    bob.put("/api/settings", json={"effort": "high"})
    assert bob.get("/api/settings").json()["chat"]["effort"] == "high"
    assert alice.get("/api/settings").json()["chat"]["effort"] == ""

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    fid = alice.post("/api/files", content=png).json()["id"]
    assert alice.get(f"/api/files/{fid}").status_code == 200
    assert bob.get(f"/api/files/{fid}").status_code == 404
    assert bob.post("/api/send", json={"text": "steal", "files": [fid]}).status_code == 400
    assert agent.get(f"/api/agent/files/{fid}").status_code == 200  # the worker reads any upload


def test_first_admin_adopts_single_password_history(tmp_path):
    from claude_bridge.standalone import create_standalone_app

    db = tmp_path / "old.db"
    old = TestClient(create_standalone_app(db_path=db, password="pw", secret="s", agent_token="tok"))
    old.post("/login", data={"password": "pw"})
    old.put("/api/settings", json={"effort": "max"})
    tid = old.post("/api/threads", json={"title": "旧对话"}).json()["thread"]

    store = BridgeStore(db)
    acc = Accounts(store)
    acc.create("me", PW, role="admin")
    acc.create("friend", PW)
    store.close()
    app = create_multiuser_app(db_path=db, agent_token="tok")
    me, friend = login(app, "me"), login(app, "friend")
    assert tid in [t["id"] for t in me.get("/api/threads").json()["items"]]
    assert me.get("/api/threads").json()["current"] == tid
    assert me.get("/api/settings").json()["chat"]["effort"] == "max"
    assert friend.get(f"/api/threads/{tid}").status_code == 404


# ---------------- quota ----------------


def rl(five, seven, resets_in=3600):
    now = int(time.time())
    return {"five_hour": {"utilization": five, "resets_at": now + resets_in},
            "seven_day": {"utilization": seven, "resets_at": now + 5 * 86400}}


def test_usage_ledger_share_limit_and_guard(app, agent):
    boss, alice, bob = login(app, "boss"), login(app, "alice"), login(app, "bob")
    uid = {u["username"]: u["id"] for u in boss.get("/api/admin/users").json()["items"]}
    boss.patch(f"/api/admin/users/{uid['alice']}", json={"limit_5h_pct": 10, "limit_7d_pct": 50})

    run_turn(alice, agent, 0.5, rate_limit=rl(0.05, 0.10))
    run_turn(alice, agent, 0.6)
    # no "100% ≈ $X" yet: the share can't be computed, so it isn't enforced
    assert alice.post("/api/send", json={"text": "more"}).status_code == 201
    job = agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}).json()["job"]
    finish_turn(agent, job["id"], 0.0)

    data = boss.get("/api/admin/users").json()
    five = data["account"]["five_hour"]
    assert five["utilization_pct"] == 5.0 and five["bridge_cost_usd"] == pytest.approx(1.1)
    assert five["estimate_cap_usd"] == pytest.approx(22.0)  # 1.1 / 0.05

    boss.put("/api/admin/quota", json={"cap_5h_usd": 10, "cap_7d_usd": 100})
    me = alice.get("/api/me").json()["usage"]
    assert me["five_hour"]["used_pct"] == pytest.approx(11.0) and me["five_hour"]["limit_pct"] == 10
    assert me["seven_day"]["used_pct"] == pytest.approx(1.1)
    assert "你的 5 小时份额已用完（11% / 10%）" in me["blocked"]
    r = alice.post("/api/send", json={"text": "blocked"})
    assert r.status_code == 429 and "份额已用完" in r.json()["detail"]
    assert bob.post("/api/send", json={"text": "bob is fine"}).status_code == 201  # no limit for bob
    job = agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}).json()["job"]
    finish_turn(agent, job["id"], 0.01, rate_limit=rl(0.85, 0.30))

    # the account guard: at 85% of the 5h window everyone but admins stops
    boss.put("/api/admin/quota", json={"guard_5h_pct": 80})
    r = bob.post("/api/send", json={"text": "guarded"})
    assert r.status_code == 429 and "整个账户的 5 小时额度已用 85%" in r.json()["detail"]
    assert boss.post("/api/send", json={"text": "admin never limited"}).status_code == 201

    # deleting threads does not give the quota back
    for t in alice.get("/api/threads").json()["items"]:
        alice.delete(f"/api/threads/{t['id']}")
    assert alice.get("/api/me").json()["usage"]["five_hour"]["used_pct"] == pytest.approx(11.0)


def test_window_rollover_and_usage_probe(app, agent):
    boss, bob = login(app, "boss"), login(app, "bob")
    boss.put("/api/admin/quota", json={"guard_5h_pct": 50})
    run_turn(bob, agent, 0.1, rate_limit=rl(0.9, 0.2, resets_in=-10))  # already reset by the time we look
    assert boss.get("/api/admin/users").json()["account"]["five_hour"]["utilization_pct"] == 0.0
    assert bob.post("/api/send", json={"text": "ok again"}).status_code == 201

    # the worker's zero-cost /usage report (the admin used the account outside the bridge)
    r = agent.post("/api/agent/limits", json={"five_hour_pct": 60, "seven_day_pct": 31})
    assert r.status_code == 200 and r.json()["five_hour"]["utilization"] == 0.6
    acc = boss.get("/api/admin/users").json()["account"]
    assert acc["five_hour"]["utilization_pct"] == 60.0 and acc["five_hour"]["source"] == "probe"
    assert bob.post("/api/threads", json={}).status_code == 201
    assert bob.post("/api/send", json={"text": "guarded", "new_thread": True}).status_code == 429


def test_compact_cost_is_booked(app, agent):
    alice = login(app, "alice")
    t = run_turn(alice, agent, 0.2)
    assert alice.post(f"/api/threads/{t['thread']}/compact").status_code == 202
    job = agent.post("/api/agent/jobs/next", json={"kinds": ["compact"], "wait": 0}).json()["job"]
    result = {"compact": {"pre_tokens": 1000, "post_tokens": 100, "cost_usd": 0.3}}
    agent.post(f"/api/agent/jobs/{job['id']}/finish", json={"ok": True, "result": json.dumps(result)})
    assert alice.get("/api/me").json()["usage"]["five_hour"]["cost_usd"] == pytest.approx(0.5)


def test_parse_usage_report():
    text = ("You are currently using your subscription to power your Claude Code usage\n\n"
            "Current session: 12% used · resets Sep 24 at 12:39pm (Asia/Tokyo)\n"
            "Current week (all models): 30% used · resets Sep 29 at 2:59am (Asia/Tokyo)\n"
            "Current week (Fable): 39% used · resets Sep 29 at 2:59am (Asia/Tokyo)\n")
    assert parse_usage_report(text) == {"five_hour_pct": 12.0, "seven_day_pct": 30.0}
    assert parse_usage_report("You are using an API key") is None


# ---------------- admin ----------------


def test_admin_user_management(app):
    boss = login(app, "boss")
    data = boss.get("/api/admin/users").json()
    me = data["me"]
    r = boss.post("/api/admin/users", json={"username": "carol", "display_name": "Carol", "limit_5h_pct": 20})
    assert r.status_code == 201 and len(r.json()["password"]) == 12
    carol_id, carol_pw = r.json()["user"]["id"], r.json()["password"]
    assert boss.post("/api/admin/users", json={"username": "Carol"}).status_code == 400  # taken (case-insensitive)
    assert boss.post("/api/admin/users", json={"username": "dave", "password": "given-password"}).json()["password"] is None
    carol = login(app, "carol", carol_pw)

    assert boss.patch(f"/api/admin/users/{carol_id}", json={"limit_5h_pct": 150}).status_code == 400
    assert boss.patch(f"/api/admin/users/{carol_id}", json={"limit_5h_pct": None, "note": "同事"}).json()["user"]["limit_5h_pct"] is None
    assert "note" not in carol.get("/api/me").json()["user"]  # the admin's note stays on the admin page

    # disable → signed out now, can't log back in; enable → can
    boss.patch(f"/api/admin/users/{carol_id}", json={"disabled": True})
    assert carol.get("/api/me").status_code == 401
    r = TestClient(app, follow_redirects=False).post("/login", data={"username": "carol", "password": carol_pw})
    assert r.headers["location"].startswith("/login?err=disabled")
    boss.patch(f"/api/admin/users/{carol_id}", json={"disabled": False})
    carol = login(app, "carol", carol_pw)

    new_pw = boss.post(f"/api/admin/users/{carol_id}/password", json={}).json()["password"]
    assert carol.get("/api/me").status_code == 401
    carol = login(app, "carol", new_pw)
    carol.post("/api/threads", json={"title": "x"})

    # the last admin can't lock themselves out
    assert boss.patch(f"/api/admin/users/{me}", json={"role": "user"}).status_code == 400
    assert boss.patch(f"/api/admin/users/{me}", json={"disabled": True}).status_code == 400
    assert boss.delete(f"/api/admin/users/{me}").status_code == 400

    assert boss.delete(f"/api/admin/users/{carol_id}").status_code == 200
    assert carol.get("/api/me").status_code == 401
    store: BridgeStore = app.state.bridge.store
    assert store.threads(owner=str(carol_id)) == []
    assert "carol" not in [u["username"] for u in boss.get("/api/admin/users").json()["items"]]


def test_quota_config_validation(app):
    boss = login(app, "boss")
    assert boss.put("/api/admin/quota", json={"cap_5h_usd": -1}).status_code == 400
    assert boss.put("/api/admin/quota", json={"guard_7d_pct": 101}).status_code == 400
    q = boss.put("/api/admin/quota", json={"cap_5h_usd": 12.5, "guard_7d_pct": 90}).json()["quota"]
    assert q == {"cap_5h_usd": 12.5, "cap_7d_usd": None, "guard_5h_pct": None, "guard_7d_pct": 90}


# ---------------- CLI ----------------


def test_users_cli(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "c.db")
    assert main(["users", "--db", db, "add", "root", "--admin", "--password", "root-password"]) == 0
    assert main(["users", "--db", db, "add", "guest"]) == 0  # stdin is not a tty → a generated password is printed
    out = capsys.readouterr().out
    assert "已创建 root（admin）" in out and "初始密码：" in out
    assert main(["users", "--db", db, "add", "guest", "--password", "whatever-pw"]) == 1
    assert main(["users", "--db", db, "passwd", "guest", "--password", "guest-password"]) == 0
    assert main(["users", "--db", db, "list"]) == 0
    listing = capsys.readouterr().out
    assert "root" in listing and "guest" in listing
    assert main(["users", "--db", db, "passwd", "nobody", "--password", "x" * 10]) == 1

    app = create_multiuser_app(db_path=db, agent_token="tok")
    login(app, "guest", "guest-password")


def test_serve_multi_user_needs_an_admin(tmp_path, capsys, monkeypatch):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    db = str(tmp_path / "s.db")
    assert main(["serve", "--db", db, "--multi-user"]) == 1
    assert "users --db" in capsys.readouterr().err
    main(["users", "--db", db, "add", "root", "--admin", "--password", "root-password"])
    assert main(["serve", "--db", db, "--multi-user"]) == 0


def test_worker_probes_usage_on_its_schedule(tmp_path):
    import stat
    import sys

    from claude_bridge.worker import Worker, WorkerConfig
    from test_worker import FakeClient

    script = tmp_path / "claude"
    script.write_text(f"""#!{sys.executable}
import json, sys
open({str(tmp_path / 'calls.txt')!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')
text = "Current session: 7% used · resets 1pm\\nCurrent week (all models): 41% used · resets Sep 29"
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": text, "local_command": "usage"}}))
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    class Client(FakeClient):
        def __init__(self):
            super().__init__()
            self.limits = []

        def report_limits(self, report):
            self.limits.append(report)

    client = Client()
    w = Worker(client, WorkerConfig(claude_bin=str(script), cwd=tmp_path, usage_probe_interval=600))
    w.maybe_probe_usage()
    w.maybe_probe_usage()  # not due again yet
    assert client.limits == [{"five_hour_pct": 7.0, "seven_day_pct": 41.0}]
    assert (tmp_path / "calls.txt").read_text().count("/usage --output-format json --no-session-persistence") == 1
    Worker(client, WorkerConfig(claude_bin=str(script), usage_probe_interval=0)).maybe_probe_usage()
    assert len(client.limits) == 1


def next_job(agent, kind):
    return agent.post("/api/agent/jobs/next", json={"kinds": [kind], "wait": 0}).json()["job"]


def test_code_projects_lifecycle_and_scopes(app, agent):
    alice, bob = login(app, "alice"), login(app, "bob")
    uid = str(alice.get("/api/me").json()["user"]["id"])
    assert alice.get("/api/projects").json()["items"] == []
    for bad in ({"clone_url": "file:///Users/szyyw/secret"}, {"clone_url": "/etc"}, {"name": ".hidden"}, {"name": "a b"}, {}):
        assert alice.post("/api/projects", json=bad).status_code == 400, bad

    r = alice.post("/api/projects", json={"clone_url": "https://github.com/Szyoo/claude-bridge.git"})
    assert r.status_code == 202 and r.json()["project"]["name"] == "claude-bridge" and r.json()["project"]["status"] == "creating"
    assert alice.post("/api/projects", json={"name": "claude-bridge"}).status_code == 400  # taken
    job = next_job(agent, "project")
    assert job["payload"] == {"action": "create", "owner": uid, "name": "claude-bridge",
                              "clone_url": "https://github.com/Szyoo/claude-bridge.git", "scope": "code:claude-bridge"}
    # not ready yet: its scope is refused
    assert alice.post("/api/send", json={"text": "x", "scope": "code:claude-bridge"}).status_code == 400
    agent.post(f"/api/agent/jobs/{job['id']}/finish", json={"ok": True, "result": json.dumps({"branch": "main"})})
    proj = alice.get("/api/projects").json()["items"][0]
    assert proj["status"] == "ready" and proj["info"]["branch"] == "main" and proj["threads"] == 0

    # a project's conversations are their own scope; Chat is separate; other users can't use the project
    chat = alice.post("/api/send", json={"text": "chat"}).json()
    code = alice.post("/api/send", json={"text": "code", "scope": "code:claude-bridge"}).json()
    assert chat["thread"] != code["thread"]
    assert [t["id"] for t in alice.get("/api/threads?scope=code:claude-bridge").json()["items"]] == [code["thread"]]
    assert bob.post("/api/send", json={"text": "x", "scope": "code:claude-bridge"}).status_code == 400
    assert bob.get("/api/projects").json()["items"] == []
    assert alice.post("/api/send", json={"text": "x", "scope": "code"}).status_code == 400
    jobs = [next_job(agent, "chat") for _ in range(2)]
    assert [(j["payload"]["scope"], j["payload"]["owner"]) for j in jobs] == [("", uid), ("code:claude-bridge", uid)]
    for j in jobs:
        agent.post(f"/api/agent/jobs/{j['id']}/finish", json={"ok": True, "result": "{}", "session_id": "s"})

    # a failed create can be retried under the same name
    alice.post("/api/projects", json={"name": "scratch"})
    j = next_job(agent, "project")
    agent.post(f"/api/agent/jobs/{j['id']}/finish", json={"ok": False, "error": "boom"})
    assert {p["name"]: p["status"] for p in alice.get("/api/projects").json()["items"]}["scratch"] == "failed"
    assert alice.post("/api/projects", json={"name": "scratch"}).status_code == 202

    # delete: conversations go at once, the row after the worker removed the directory
    r = alice.delete("/api/projects/claude-bridge").json()
    assert r["deleted"] is False and r["project"]["status"] == "deleting"
    assert alice.get(f"/api/threads/{code['thread']}").status_code == 404
    next_job(agent, "project")  # the retried "scratch" create
    d = next_job(agent, "project")
    assert d["payload"]["action"] == "delete"
    agent.post(f"/api/agent/jobs/{d['id']}/finish", json={"ok": True, "result": "{}"})
    assert "claude-bridge" not in [p["name"] for p in alice.get("/api/projects").json()["items"]]
    assert bob.delete("/api/projects/scratch").status_code == 404
