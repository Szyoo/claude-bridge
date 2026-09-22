import pytest
from fastapi.testclient import TestClient

from claude_bridge.client import BridgeClient, BridgeClientError


def test_client_requires_config():
    with pytest.raises(BridgeClientError):
        BridgeClient("", "tok")
    with pytest.raises(BridgeClientError):
        BridgeClient("http://x", "")


def test_client_roundtrip_over_testclient(app, bridge, web):
    http = TestClient(app)
    c = BridgeClient("http://testserver", "tok", http=http)
    assert c.status()["online"] is False
    assert c.next_job("w", ["chat"], wait=0) is None

    r = c.create_chat("请解读", scope="review", key="2026-09-22", new_thread=True, payload={"notify": True})
    job = c.next_job("w", ["chat"], wait=0)
    assert job["id"] == r["job_id"] and job["payload"]["notify"] is True
    assert c.get_job(job["id"])["status"] == "running"
    assert c.status()["online"] is True and c.status()["running"] == 1

    resp = c.post_events(job["id"], status="streaming", deltas=["a", "b"], events=[{"type": "tool_use", "data": {"id": "t"}}])
    assert resp == {"ok": True, "cancel": False}
    assert c.post_events(job["id"]) == {"ok": True, "cancel": False}  # empty batch is fine
    web.post(f"/api/messages/{r['message_id']}/cancel")
    assert c.post_events(job["id"], deltas=["c"])["cancel"] is True
    c.finish_job(job["id"], ok=False, cancelled=True, session_id="s")
    m = bridge.store.get_message(r["message_id"])
    assert m["status"] == "cancelled" and m["content"] == "abc" and bridge.store.get_thread(r["thread"])["session_id"] == "s"

    bad = BridgeClient("http://testserver", "nope", http=http)
    with pytest.raises(BridgeClientError, match="401"):
        bad.status()
    with pytest.raises(BridgeClientError, match="404"):
        c.get_job(999)
