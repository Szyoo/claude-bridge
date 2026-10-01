"""serve --multi-user behind the szyyw.xyz portal gate (SZYYW_SSO=1): identity from X-Portal-Sub / X-User / X-Role only."""

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
    acc.set_portal_sub(2, "sub-alice")  # an approved merge row: portal alice → bridge "user"
    acc.set_portal_sub(3, "sub-carol")  # user1 is already taken by portal carol
    store.close()
    return path


def make(db, monkeypatch, *, autocreate=False):
    monkeypatch.setenv("SZYYW_SSO", "1")
    monkeypatch.setenv("PORTAL_ORIGIN", PORTAL)
    if autocreate:
        monkeypatch.setenv("SZYYW_SSO_AUTOCREATE", "1")
    return create_multiuser_app(db_path=db, agent_token="tok", tz="UTC")


def gated(app, user, role="user", sub="") -> TestClient:
    """A request through the gate; portal sub defaults to "sub-<user>", `sub=None` leaves the header out."""
    headers = {"X-User": user, "X-Role": role, "X-Forwarded-Proto": "https", "X-Forwarded-Host": "claude.szyyw.xyz"}
    if sub is not None:
        headers["X-Portal-Sub"] = sub or f"sub-{user}"
    return TestClient(app, follow_redirects=False, headers=headers)


def rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return {r["username"]: dict(r) for r in conn.execute("SELECT * FROM bridge_users")}
    finally:
        conn.close()


def test_migration_adds_nullable_unique_portal_sub(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(accounts_mod.SCHEMA)  # a v0.3.x file: no portal_sub column
    conn.execute("INSERT INTO bridge_users(username, pw_hash, role) VALUES('old', 'x', 'admin')")
    conn.commit()
    conn.close()
    store = BridgeStore(path)
    acc = Accounts(store)
    assert acc.get(1)["portal_sub"] is None and acc.get(1)["username"] == "old"
    acc.create("other", PW)
    acc.set_portal_sub(1, "p")
    with pytest.raises(accounts_mod.BadRequest):
        acc.set_portal_sub(2, "p")
    with pytest.raises(accounts_mod.BadRequest):
        acc.set_portal_sub(2, "has space")
    Accounts(store)  # idempotent
    store.close()


def test_mapped_user_owns_by_row_id(db, monkeypatch):
    app = make(db, monkeypatch)
    c = gated(app, "alice")
    me = c.get("/api/me").json()
    assert me["user"]["id"] == 2 and me["user"]["username"] == "user" and me["sso"]["portal_user"] == "alice"
    assert me["sso"]["portal_sub"] == "sub-alice"
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


def test_autocreate_on_creates_row_with_portal_sub(db, monkeypatch):
    app = make(db, monkeypatch, autocreate=True)
    c = gated(app, "dave", role="admin")
    me = c.get("/api/me").json()["user"]
    row = rows(db)["dave"]
    assert me["id"] == row["id"] == 4 and row["portal_sub"] == "sub-dave" and row["role"] == "admin"
    assert row["limit_5h_pct"] is None and not accounts_mod.verify_password("", row["pw_hash"])
    # the auto-created row can't sign in locally once SSO is switched off again
    assert app.state.accounts.authenticate("dave", "!sso") is None
    assert gated(app, "dave").get("/api/me").json()["user"]["id"] == 4  # second visit: mapped, no new row
    # renamed in the portal, same sub → same row, still no new one; the local username is not renamed
    assert gated(app, "david", sub="sub-dave").get("/api/me").json()["user"]["id"] == 4
    assert "david" not in rows(db) and rows(db)["dave"]["portal_sub"] == "sub-dave"


def test_same_username_with_null_portal_sub_is_adopted(db, monkeypatch):
    app = make(db, monkeypatch)
    assert rows(db)["szyyw"]["portal_sub"] is None
    me = gated(app, "szyyw", role="admin", sub="3f2a-uuid").get("/api/me").json()["user"]
    assert me["id"] == 1 and rows(db)["szyyw"]["portal_sub"] == "3f2a-uuid"  # filled with the sub, not the name
    # after adoption the sub is what counts: a later portal rename keeps the row
    me2 = gated(app, "boss", role="admin", sub="3f2a-uuid").get("/api/me").json()
    assert me2["user"]["id"] == 1 and me2["user"]["username"] == "szyyw" and me2["sso"]["portal_user"] == "boss"


def test_same_username_different_sub_is_not_adopted(db, monkeypatch):
    # bridge "user1" is portal carol's (sub-carol); a portal account named "user1" with another sub must not get it
    app = make(db, monkeypatch)
    assert gated(app, "user1", sub="sub-other").get("/api/me").status_code == 403
    assert rows(db)["user1"]["portal_sub"] == "sub-carol"
    app2 = make(db, monkeypatch, autocreate=True)
    me = gated(app2, "user1", sub="sub-other").get("/api/me").json()["user"]
    assert me["id"] != 3 and me["username"] == "user1-2"
    assert rows(db)["user1-2"]["portal_sub"] == "sub-other" and rows(db)["user1"]["portal_sub"] == "sub-carol"


def test_sub_match_wins_over_username_match(db, monkeypatch):
    # portal alice renamed herself to "szyyw" — the name of an unmapped bridge row; her sub still picks her row
    app = make(db, monkeypatch)
    me = gated(app, "szyyw", sub="sub-alice").get("/api/me").json()
    assert me["user"]["id"] == 2 and me["sso"]["portal_user"] == "szyyw"
    assert rows(db)["szyyw"]["portal_sub"] is None  # not adopted
    # and the display in the app is the current portal name; the stored username stays
    assert rows(db)["user"]["username"] == "user"


def test_missing_sub_is_signed_out(db, monkeypatch):
    app = make(db, monkeypatch, autocreate=True)
    c = gated(app, "szyyw", role="admin", sub=None)  # X-User but no X-Portal-Sub: never a username-only match
    assert c.get("/api/me").status_code == 401
    r = c.get("/")
    assert r.status_code == 302 and r.headers["location"].startswith(f"{PORTAL}/login?rd=")
    assert rows(db)["szyyw"]["portal_sub"] is None and len(rows(db)) == 3  # nothing adopted, nothing created
    assert gated(app, "alice", sub=None).get("/api/me").status_code == 401
    assert gated(app, "alice", sub="  ").get("/api/me").status_code == 401


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
    assert TestClient(app, headers={"X-User": "szyyw", "X-Role": "admin", "X-Portal-Sub": "sub-szyyw"}).get("/api/agent/status").status_code == 401
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


def test_admin_portal_sub_endpoint(db, monkeypatch):
    app = make(db, monkeypatch)
    admin = gated(app, "szyyw", role="admin")
    items = admin.get("/api/admin/users").json()
    assert {u["username"]: u["portal_sub"] for u in items["items"]}["user"] == "sub-alice" and items["sso"]["portal"] == PORTAL
    assert items["sso"]["me_portal_user"] == "szyyw"
    r = admin.post("/api/admin/users/3/portal-user", json={"portal_sub": "sub-alice"})
    assert r.status_code == 400  # sub-alice already maps to id 2
    assert admin.post("/api/admin/users/3/portal-user", json={"portal_sub": None}).json()["user"]["portal_sub"] is None
    assert admin.post("/api/admin/users/3/portal-user", json={"portal_sub": "sub-bob"}).json()["user"]["portal_sub"] == "sub-bob"
    assert gated(app, "bob").get("/api/me").json()["user"]["id"] == 3
    assert gated(app, "alice").post("/api/admin/users/3/portal-user", json={"portal_sub": "x"}).status_code == 403


def test_sso_off_ignores_headers(db):
    app = create_multiuser_app(db_path=db, agent_token="tok", tz="UTC")
    c = TestClient(app, follow_redirects=False, headers={"X-User": "alice", "X-Role": "admin", "X-Portal-Sub": "sub-alice"})
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
    sub = "3f2a9c1e-0000-4000-8000-000000000001"
    assert main(["users", "--db", str(db), "map", "szyyw", sub]) == 0
    assert f"— → {sub}" in capsys.readouterr().out
    assert main(["users", "--db", str(db), "map", "user1", sub]) == 1  # taken
    assert "已对应到 szyyw" in capsys.readouterr().err
    assert main(["users", "--db", str(db), "list"]) == 0
    assert f"门户ID={sub}" in capsys.readouterr().out
    assert main(["users", "--db", str(db), "unmap", "szyyw"]) == 0
    assert rows(db)["szyyw"]["portal_sub"] is None
    assert main(["users", "--db", str(db), "map", "nobody", "x"]) == 1


def test_sso_pages_load_corner_tools(db, monkeypatch):
    c = gated(make(db, monkeypatch), "alice", role="admin")
    for path in ("/", "/account", "/admin"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert f'<html lang="zh-CN" data-sso="1" data-portal="{PORTAL}" data-scheme="auto">' in r.text
        assert "/static/bridge/vendor/szyyw-design/tokens.css" in r.text
        assert "/static/bridge/corner-boot.js" in r.text
    for f in ("corner-boot.js", "vendor/szyyw-design/switcher.js", "vendor/szyyw-design/components.css"):
        assert c.get(f"/static/bridge/{f}").status_code == 200, f


def test_sso_off_pages_have_no_corner_tools(db):
    app = create_multiuser_app(db_path=db, agent_token="tok", tz="UTC")
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"username": "szyyw", "password": PW})
    for path in ("/", "/account", "/admin", "/login"):
        r = c.get(path)
        assert r.status_code in (200, 303), path
        assert "data-sso" not in r.text and "szyyw-design" not in r.text and "corner-boot" not in r.text
