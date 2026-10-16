"""Browser one-off sends (`ephemeral: true`): fresh hidden thread, worker keeps no session, deleted when idle."""

from claude_bridge.store import EPHEMERAL_KEY_PREFIX


def _claim(agent):
    return agent.post("/api/agent/jobs/next", json={"worker": "w", "kinds": ["chat"], "wait": 0}).json()["job"]


def test_ephemeral_send_is_a_fresh_hidden_thread(web, agent, bridge):
    r1 = web.post("/api/send", json={"text": "翻译这段", "scope": "stock", "key": "same", "ephemeral": True}).json()
    r2 = web.post("/api/send", json={"text": "翻译这段", "scope": "stock", "key": "same", "ephemeral": True}).json()
    # same key, still two separate threads: the key is ignored, nothing is resumed
    assert r1["thread"] != r2["thread"] and "main" not in (r1["thread"], r2["thread"])
    th = bridge.store.get_thread(r1["thread"])
    assert th["scope"] == "stock" and th["key"].startswith(EPHEMERAL_KEY_PREFIX)
    # not in the thread list, and the scope's current thread did not change
    listing = web.get("/api/threads?scope=stock").json()
    assert listing["current"] == "main"
    assert r1["thread"] not in {t["id"] for t in listing["items"]}
    # the worker sees the flag (→ --no-session-persistence) and no session to resume
    job = _claim(agent)
    assert job["payload"]["ephemeral"] is True and job["payload"]["session_id"] is None
    # the client can still read and delete it
    assert web.get(f"/api/threads/{r1['thread']}/messages").status_code == 200
    assert web.delete(f"/api/threads/{r1['thread']}?scope=stock").status_code == 200
    assert bridge.store.get_thread(r1["thread"]) is None


def test_ephemeral_rejected_for_existing_thread(bridge):
    from claude_bridge.errors import BadRequest

    try:
        bridge.service.start_chat("x", thread_id="main", ephemeral=True)
    except BadRequest:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected BadRequest")


def test_plain_send_unchanged(web, agent):
    r = web.post("/api/send", json={"text": "你好", "scope": "stock"}).json()
    assert r["thread"] == "main"
    assert "ephemeral" not in _claim(agent)["payload"]


def test_idle_ephemeral_threads_are_purged(web, agent, bridge):
    r = web.post("/api/send", json={"text": "一次性", "scope": "stock", "ephemeral": True}).json()
    tid = r["thread"]
    job = _claim(agent)
    # still answering: never purged, however old
    bridge.store.conn.execute("UPDATE bridge_threads SET updated_at=datetime('now','-2 hours') WHERE id=?", (tid,))
    assert bridge.service.purge_ephemeral() == 0
    agent.post(f"/api/agent/jobs/{job['id']}/finish", json={"ok": True, "result": "{}"})
    # finished just now: kept for the idle window
    assert bridge.service.purge_ephemeral() == 0
    bridge.store.conn.execute("UPDATE bridge_threads SET updated_at=datetime('now','-2 hours') WHERE id=?", (tid,))
    bridge.store.conn.execute("UPDATE bridge_messages SET updated_at=datetime('now','-2 hours') WHERE thread=?", (tid,))
    # ordinary threads are never touched, however idle
    plain = bridge.service.new_thread("stock", select=False)
    bridge.store.conn.execute("UPDATE bridge_threads SET updated_at=datetime('now','-2 hours') WHERE id=?", (plain,))
    assert bridge.service.purge_ephemeral() == 1
    assert bridge.store.get_thread(tid) is None and bridge.store.get_thread(plain) is not None
    # the next send also sweeps
    r2 = web.post("/api/send", json={"text": "再来", "scope": "stock", "ephemeral": True}).json()
    assert bridge.store.get_thread(r2["thread"]) is not None
