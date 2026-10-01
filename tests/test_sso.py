"""serve --multi-user behind the szyyw.xyz portal gate (SZYYW_SSO=1): identity from X-User / X-Role only."""

from __future__ import annotations

import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from claude_bridge import accounts as accounts_mod
from claude_bridge.accounts import Accounts
from claude_bridge.cli import main
from claude_bridge.multiuser import create_multiuser_app
from claude_bridge.store import BridgeStore

PW = "password-1"
PORTAL = "https://portal.test"


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_mod, "PBKDF2_ITERATIONS", 1000)
    for name in ("SZYYW_SSO", "CLAUDE_BRIDGE_SSO", "SZYYW_SSO_AUTOCREATE", "PORTAL_ORIGIN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "sso.db"
    store = BridgeStore(path)
    acc = Accounts(store)
    acc.create("szyyw", PW, role="admin")  # id 1
    acc.create("user", PW)  # id 2
    acc.create("user1", PW)  # id 3
    acc.set_portal_user(2, "alice")  # an approved merge row: portal alice → bridge "user"
    acc.set_portal_user(3, "carol")  # user1 is already taken by portal carol
    store.close()
    return path


def make(db, monkeypatch, *, autocreate=False):
    monkeypatch.setenv("SZYYW_SSO", "1")
    monkeypatch.setenv("PORTAL_ORIGIN", PORTAL)
    if autocreate:
        monkeypatch.setenv("SZYYW_SSO_AUTOCREATE", "1")
    return create_multiuser_app(db_path=db, agent_token="tok", tz="UTC")


def gated(app, user, role="user") -> TestClient:
    return TestClient(app, follow_redirects=False, headers={"X-User": user, "X-Role": role,
                                                            "X-Forwarded-Proto": "https", "X-Forwarded-Host": "claude.szyyw.xyz"})


def rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return {r["username"]: dict(r) for r in conn.execute("SELECT * FROM bridge_users")}
    finally:
        conn.close()


def test_migration_adds_nullable_unique_portal_user(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(accounts_mod.SCHEMA)  # a v0.3.x file: no portal_user column
    conn.execute("INSERT INTO bridge_users(username, pw_hash, role) VALUES('old', 'x', 'admin')")
    conn.commit()
    conn.close()
    store = BridgeStore(path)
    acc = Accounts(store)
    assert acc.get(1)["portal_user"] is None and acc.get(1)["username"] == "old"
    acc.create("other", PW)
    acc.set_portal_user(1, "p")
    with pytest.raises(accounts_mod.BadRequest):
        acc.set_portal_user(2, "p")
    Accounts(store)  # idempotent
    store.close()


def test_mapped_user_owns_by_row_id(db, monkeypatch):
    app = make(db, monkeypatch)
    c = gated(app, "alice")
    me = c.get("/api/me").json()
    assert me["user"]["id"] == 2 and me["user"]["username"] == "user" and me["sso"]["portal_user"] == "alice"
    r = c.post("/api/send", json={"text": "hi"})
    assert r.status_code == 201
    store = app.state.bridge.store
    assert store.get_thread(r.json()["thread"])["owner"] == "2"
    assert c.get("/").status_code == 200 and c.get("/account").status_code == 200
    # the cookie is not identity under SSO: no headers → 401, even with a valid local session cookie
    acc = app.state.accounts
    bare = TestClient(app, follow_redirects=False)
    bare.cookies.set("bridge_session", acc.issue_token(acc.get(1)))
    assert bare.get("/api/me").status_code == 401


def test_unmapped_autocreate_off_is_403(db, monkeypatch):
    app = make(db, monkeypatch)
    c = gated(app, "dave")
    r = c.get("/api/me")
    assert r.status_code == 403 and "尚未在 claude-bridge 开通" in r.json()["detail"]
    page = c.get("/")
    assert page.status_code == 403 and "尚未在 claude-bridge 开通" in page.text
    assert "dave" not in rows(db)


def test_autocreate_on_creates_row_with_portal_user(db, monkeypatch):
    app = make(db, monkeypatch, autocreate=True)
    c = gated(app, "dave", role="admin")
    me = c.get("/api/me").json()["user"]
    row = rows(db)["dave"]
    assert me["id"] == row["id"] == 4 and row["portal_user"] == "dave" and row["role"] == "admin"
    assert row["limit_5h_pct"] is None and not accounts_mod.verify_password("", row["pw_hash"])
    # the auto-created row can't sign in locally once SSO is switched off again
    assert app.state.accounts.authenticate("dave", "!sso") is None
    assert gated(app, "dave").get("/api/me").json()["user"]["id"] == 4  # second visit: mapped, no new row


def test_same_username_with_null_portal_user_is_adopted(db, monkeypatch):
    app = make(db, monkeypatch)
    assert rows(db)["szyyw"]["portal_user"] is None
    me = gated(app, "szyyw", role="admin").get("/api/me").json()["user"]
    assert me["id"] == 1 and rows(db)["szyyw"]["portal_user"] == "szyyw"


def test_same_username_mapped_elsewhere_is_not_adopted(db, monkeypatch):
    # bridge "user1" is portal carol's; a portal account named "user1" must not get it
    app = make(db, monkeypatch)
    assert gated(app, "user1").get("/api/me").status_code == 403
    assert rows(db)["user1"]["portal_user"] == "carol"
    app2 = make(db, monkeypatch, autocreate=True)
    me = gated(app2, "user1").get("/api/me").json()["user"]
    assert me["id"] != 3 and me["username"] == "user1-2"
    assert rows(db)["user1-2"]["portal_user"] == "user1" and rows(db)["user1"]["portal_user"] == "carol"


def test_role_from_header_not_stored(db, monkeypatch):
    app = make(db, monkeypatch)
    # stored admin, X-Role user → no admin access
    c = gated(app, "szyyw", role="user")
    assert c.get("/api/admin/users").status_code == 403
    assert c.get("/admin").headers["location"] == "/"
    # stored user, X-Role admin → admin access; the stored role column is untouched
    c2 = gated(app, "alice", role="admin")
    assert c2.get("/api/admin/users").status_code == 200
    assert rows(db)["user"]["role"] == "user" and rows(db)["szyyw"]["role"] == "admin"


def test_login_redirects_to_portal_and_password_login_disabled(db, monkeypatch):
    app = make(db, monkeypatch)
    c = gated(app, "alice")
    r = c.get("/login")
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{PORTAL}/login"
    assert parse_qs(loc.query)["rd"] == ["https://claude.szyyw.xyz/"]
    assert c.post("/login", data={"username": "user", "password": PW}).status_code == 403
    assert c.post("/api/me/password", json={"current": PW, "new": "another-pw-1"}).status_code == 403
    out = c.post("/logout")
    assert out.status_code == 303 and out.headers["location"] == PORTAL
    # no gate identity on a page → portal login coming back to this URL
    bare = TestClient(app, follow_redirects=False, headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "claude.szyyw.xyz"})
    r = bare.get("/account?x=1")
    assert parse_qs(urlparse(r.headers["location"]).query)["rd"] == ["https://claude.szyyw.xyz/account?x=1"]


def test_agent_bearer_and_health_unchanged(db, monkeypatch):
    app = make(db, monkeypatch)
    agent = TestClient(app, headers={"Authorization": "Bearer tok"})
    assert agent.get("/api/agent/status").status_code == 200
    assert agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}).json() == {"job": None}
    assert TestClient(app).get("/api/agent/status").status_code == 401
    # X-User never authenticates the agent router (Caddy strips it anyway)
    assert TestClient(app, headers={"X-User": "szyyw", "X-Role": "admin"}).get("/api/agent/status").status_code == 401
    assert TestClient(app).get("/api/health").json() == {"ok": True}
    # a whole turn: browser via headers, worker via Bearer
    c = gated(app, "alice")
    sent = c.post("/api/send", json={"text": "hi"}).json()
    job = agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}).json()["job"]
    assert job and job["payload"]["thread"] == sent["thread"]


def test_disabled_row_is_refused(db, monkeypatch):
    store = BridgeStore(db)
    Accounts(store).update(2, {"disabled": True})
    store.close()
    app = make(db, monkeypatch)
    assert gated(app, "alice").get("/api/me").status_code == 403


def test_admin_portal_user_endpoint(db, monkeypatch):
    app = make(db, monkeypatch)
    admin = gated(app, "szyyw", role="admin")
    items = admin.get("/api/admin/users").json()
    assert {u["username"]: u["portal_user"] for u in items["items"]}["user"] == "alice" and items["sso"]["portal"] == PORTAL
    r = admin.post("/api/admin/users/3/portal-user", json={"portal_user": "alice"})
    assert r.status_code == 400  # alice already maps to id 2
    assert admin.post("/api/admin/users/3/portal-user", json={"portal_user": None}).json()["user"]["portal_user"] is None
    assert admin.post("/api/admin/users/3/portal-user", json={"portal_user": "bob"}).json()["user"]["portal_user"] == "bob"
    assert gated(app, "bob").get("/api/me").json()["user"]["id"] == 3
    assert gated(app, "alice").post("/api/admin/users/3/portal-user", json={"portal_user": "x"}).status_code == 403


def test_sso_off_ignores_headers(db):
    app = create_multiuser_app(db_path=db, agent_token="tok", tz="UTC")
    c = TestClient(app, follow_redirects=False, headers={"X-User": "alice", "X-Role": "admin"})
    assert c.get("/api/me").status_code == 401
    assert c.get("/").headers["location"] == "/login"
    assert c.get("/login").status_code == 200
    r = c.post("/login", data={"username": "user", "password": PW})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "sso" not in c.get("/api/me").json()


def test_claude_bridge_sso_alias(db, monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_SSO", "1")
    app = create_multiuser_app(db_path=db, agent_token="tok", tz="UTC", portal_origin=PORTAL)
    assert gated(app, "alice").get("/api/me").json()["user"]["id"] == 2


def test_cli_map_unmap(db, capsys):
    assert main(["users", "--db", str(db), "map", "szyyw", "szyyw-portal"]) == 0
    assert "— → szyyw-portal" in capsys.readouterr().out
    assert main(["users", "--db", str(db), "map", "user1", "szyyw-portal"]) == 1  # taken
    assert "已对应到 szyyw" in capsys.readouterr().err
    assert main(["users", "--db", str(db), "list"]) == 0
    assert "门户=szyyw-portal" in capsys.readouterr().out
    assert main(["users", "--db", str(db), "unmap", "szyyw"]) == 0
    assert rows(db)["szyyw"]["portal_user"] is None
    assert main(["users", "--db", str(db), "map", "nobody", "x"]) == 1
