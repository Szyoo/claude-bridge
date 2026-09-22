import pytest
from fastapi.testclient import TestClient

from claude_bridge.standalone import create_standalone_app


def test_password_login_flow(tmp_path):
    app = create_standalone_app(db_path=tmp_path / "s.db", password="pw", secret="s", agent_token="tok")
    c = TestClient(app, follow_redirects=False)
    assert c.get("/").status_code == 303 and c.get("/").headers["location"] == "/login"
    assert c.get("/api/status").status_code == 401
    assert c.get("/login").status_code == 200 and "password" in c.get("/login").text

    r = c.post("/login", data={"password": "wrong"})
    assert r.status_code == 303 and r.headers["location"] == "/login?err=1"
    assert "口令不对" in c.get("/login?err=1").text
    r = c.post("/login", data={"password": "pw"})
    assert r.status_code == 303 and r.headers["location"] == "/" and "bridge_session" in r.headers["set-cookie"]
    assert c.get("/").status_code == 200 and "bridge-widget" in c.get("/").text
    assert c.get("/api/status").status_code == 200
    assert c.get("/api/threads?scope=").json()["current"] == "main"
    assert c.get("/static/bridge/bridge-client.js").status_code == 200
    assert c.get("/api/health").json() == {"ok": True}

    # agent side uses the bearer token, not the cookie
    a = TestClient(app, headers={"Authorization": "Bearer tok"})
    assert a.get("/api/agent/status").status_code == 200
    assert TestClient(app).get("/api/agent/status").status_code == 401

    r = c.post("/logout")
    assert r.status_code == 303 and c.get("/").status_code == 303


def test_login_rate_limit(tmp_path):
    app = create_standalone_app(db_path=tmp_path / "s.db", password="pw", secret="s", agent_token="tok")
    c = TestClient(app, follow_redirects=False)
    for _ in range(5):
        c.post("/login", data={"password": "wrong"})
    assert c.post("/login", data={"password": "pw"}).status_code == 429


def test_no_auth_mode_and_missing_password(tmp_path):
    app = create_standalone_app(db_path=tmp_path / "s.db", no_auth=True, agent_token="tok")
    c = TestClient(app)
    assert c.get("/").status_code == 200 and c.get("/api/status").status_code == 200
    with pytest.raises(RuntimeError):
        create_standalone_app(db_path=tmp_path / "x.db", agent_token="tok")
