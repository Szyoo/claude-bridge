"""Self-service API keys: /api/me/api-keys, the admin view, and that a key authenticates /v1/messages."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from claude_bridge import accounts as accounts_mod
from claude_bridge.accounts import Accounts
from claude_bridge.multiuser import create_multiuser_app
from claude_bridge.store import BridgeStore

PW = "password-1"
PROBE = {"model": "claude-sonnet-5-5", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_mod, "PBKDF2_ITERATIONS", 1000)


def build(tmp_path, tools=True):
    db = tmp_path / "k.db"
    store = BridgeStore(db)
    acc = Accounts(store)
    acc.create("boss", PW, role="admin")
    acc.create("alice", PW)
    acc.create("bob", PW)
    store.close()
    return create_multiuser_app(db_path=db, agent_token="tok", tz="UTC", client_tools_enabled=tools)


def login(app, username) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    assert c.post("/login", data={"username": username, "password": PW}).status_code == 303
    return c


@pytest.fixture
def app(tmp_path):
    return build(tmp_path)


def probe(app, key):
    return TestClient(app).post("/v1/messages", json=PROBE, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})


def test_create_lists_without_plaintext_and_authenticates(app):
    alice = login(app, "alice")
    r = alice.post("/api/me/api-keys", json={"note": "cursor"})
    assert r.status_code == 200
    key = r.json()["key"]
    assert key.startswith("cbc_")
    listing = alice.get("/api/me/api-keys").json()
    assert key not in str(listing)
    assert listing["max_keys"] == 5 and listing["base_url"].startswith("http")
    assert [i["note"] for i in listing["items"]] == ["cursor"]
    p = probe(app, key)
    assert p.status_code == 200 and p.json()["stop_reason"] == "max_tokens"


def test_revoke_then_401(app):
    alice = login(app, "alice")
    r = alice.post("/api/me/api-keys", json={}).json()
    assert alice.delete(f"/api/me/api-keys/{r['item']['name']}").json() == {"ok": True}
    assert probe(app, r["key"]).status_code == 401
    assert alice.get("/api/me/api-keys").json()["items"][0]["disabled"] is True


def test_cannot_revoke_someone_elses(app):
    name = login(app, "alice").post("/api/me/api-keys", json={}).json()["item"]["name"]
    bob = login(app, "bob")
    assert bob.delete(f"/api/me/api-keys/{name}").status_code == 404
    assert bob.get("/api/me/api-keys").json()["items"] == []


def test_sixth_active_key_is_rejected(app):
    alice = login(app, "alice")
    names = [alice.post("/api/me/api-keys", json={}).json()["item"]["name"] for _ in range(5)]
    assert alice.post("/api/me/api-keys", json={}).status_code == 400
    alice.delete(f"/api/me/api-keys/{names[0]}")
    assert alice.post("/api/me/api-keys", json={}).status_code == 200


def test_disabled_without_client_tools(tmp_path):
    alice = login(build(tmp_path, tools=False), "alice")
    assert alice.get("/api/me/api-keys").status_code == 404
    assert alice.post("/api/me/api-keys", json={}).status_code in (404, 405)


def test_admin_lists_and_revokes_any_users_keys(app):
    alice = login(app, "alice")
    made = alice.post("/api/me/api-keys", json={"note": "x"}).json()
    uid = int(made["item"]["name"].split("-")[1][1:])
    boss = login(app, "boss")
    assert [i["name"] for i in boss.get(f"/api/admin/users/{uid}/api-keys").json()["items"]] == [made["item"]["name"]]
    assert boss.delete(f"/api/admin/users/{uid}/api-keys/{made['item']['name']}").json() == {"ok": True}
    assert probe(app, made["key"]).status_code == 401
    assert alice.get(f"/api/admin/users/{uid}/api-keys").status_code == 403
