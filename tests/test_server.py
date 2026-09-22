import json

from fastapi.testclient import TestClient


def _claim(agent, kinds=("chat",)):
    return agent.post("/api/agent/jobs/next", json={"worker": "w", "kinds": list(kinds), "wait": 0}).json()["job"]


def test_send_stream_events_finish_roundtrip(web, agent, bridge):
    t0 = web.get("/api/threads?scope=stock").json()
    assert t0["current"] == "main" and t0["items"][0]["id"] == "main"

    r = web.post("/api/send", json={"text": "  我有几只股票？ ", "scope": "stock"})
    assert r.status_code == 201
    mid, jid, th = r.json()["message_id"], r.json()["job_id"], r.json()["thread"]
    assert th == "main" and r.json()["agent_online"] is False
    assert web.post("/api/send", json={"text": "再来", "scope": "stock"}).status_code == 409
    msgs = web.get(f"/api/threads/{th}/messages").json()
    assert [m["role"] for m in msgs["items"]] == ["user", "assistant"]
    assert msgs["items"][0]["content"] == "我有几只股票？" and msgs["inflight"] == mid
    assert web.get("/api/threads?scope=stock").json()["items"][0]["title"] == "我有几只股票"

    job = _claim(agent)
    assert job["id"] == jid and job["payload"]["message_id"] == mid and job["payload"]["session_id"] is None
    assert job["payload"]["settings"]["max_turns"] == 40
    assert web.get("/api/status").json()["online"] is True

    r = agent.post(
        f"/api/agent/jobs/{jid}/events",
        json={
            "status": "streaming",
            "deltas": ["你持有 ", "**1** 只。"],
            "events": [{"type": "tool_use", "data": {"id": "tu1", "name": "Bash", "input": {"command": "ashare positions"}}}],
        },
    )
    assert r.json() == {"ok": True, "cancel": False}
    # legacy body from the pre-bridge helper
    agent.post(f"/api/agent/jobs/{jid}/events", json={"append": "\n\n补充。", "trace_append": "🔧 Bash: x\n"})
    m = web.get(f"/api/threads/{th}/messages?after={mid - 1}").json()["items"][0]
    assert m["status"] == "streaming" and m["content"] == "你持有 **1** 只。\n\n补充。" and m["rev"] == 2
    assert [e["type"] for e in m["events"]] == ["tool_use", "status"]
    assert m["events"][1]["data"] == {"phase": "legacy_trace", "text": "🔧 Bash: x\n"}

    agent.post(f"/api/agent/jobs/{jid}/finish", json={"ok": True, "result": "{}", "session_id": "sess-1"})
    m = web.get(f"/api/threads/{th}/messages?after={mid - 1}").json()["items"][0]
    assert m["status"] == "done"
    assert web.get(f"/api/threads/{th}").json()["thread"]["session_id"] == "sess-1"
    assert web.get(f"/api/threads/{th}/messages?after={mid}").json() == {"items": [], "inflight": None}

    # second turn carries the session id; a bad-session failure resets it
    jid2 = web.post("/api/send", json={"text": "再问", "scope": "stock"}).json()["job_id"]
    assert _claim(agent)["payload"]["session_id"] == "sess-1"
    agent.post(
        f"/api/agent/jobs/{jid2}/finish",
        json={"ok": False, "error": "Claude 返回错误：No conversation found with session", "reset_session": True, "error_kind": "bad_session"},
    )
    last = web.get(f"/api/threads/{th}/messages").json()["items"][-1]
    assert last["status"] == "error" and "No conversation found" in last["content"]
    assert last["events"][-1]["type"] == "error" and last["events"][-1]["data"]["kind"] == "bad_session"
    assert web.get(f"/api/threads/{th}").json()["thread"]["session_id"] is None
    # error text already in content is not appended twice
    jid3 = web.post("/api/send", json={"text": "三", "scope": "stock"}).json()["job_id"]
    _claim(agent)
    agent.post(f"/api/agent/jobs/{jid3}/events", json={"deltas": ["出错了：磁盘满"]})
    agent.post(f"/api/agent/jobs/{jid3}/finish", json={"ok": False, "error": "RuntimeError：磁盘满"})
    last = web.get(f"/api/threads/{th}/messages").json()["items"][-1]
    assert last["content"] == "出错了：磁盘满"

    # token check
    assert TestClient(web.app, headers={"Authorization": "Bearer nope"}).post(
        "/api/agent/jobs/next", json={"wait": 0}
    ).status_code == 401


def test_cancel_queued_and_running(web, agent):
    r = web.post("/api/send", json={"text": "慢问题", "scope": "stock"}).json()
    mid, jid = r["message_id"], r["job_id"]
    # queued → cancelled immediately, thread unlocked
    assert web.post(f"/api/messages/{mid}/cancel").json() == {"status": "cancelled"}
    assert web.get(f"/api/jobs/{jid}").json()["status"] == "cancelled"
    assert web.get("/api/threads/main/messages").json()["items"][-1]["status"] == "cancelled"
    assert web.post(f"/api/messages/{mid}/cancel").json() == {"status": "noop"}
    assert agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}).json()["job"] is None

    # running → cancel_requested, worker sees it on its next events post, finishes as cancelled
    r = web.post("/api/send", json={"text": "另一个", "scope": "stock"}).json()
    mid, jid = r["message_id"], r["job_id"]
    assert agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}).json()["job"]["id"] == jid
    assert web.post(f"/api/messages/{mid}/cancel").json() == {"status": "cancelling"}
    assert agent.get(f"/api/agent/jobs/{jid}").json()["job"]["cancel_requested"] is True
    r = agent.post(f"/api/agent/jobs/{jid}/events", json={"deltas": ["一半"]})
    assert r.json()["cancel"] is True
    agent.post(f"/api/agent/jobs/{jid}/finish", json={"ok": False, "cancelled": True, "session_id": "s"})
    m = web.get("/api/threads/main/messages").json()["items"][-1]
    assert m["status"] == "cancelled" and m["content"] == "一半"
    assert [e["type"] for e in m["events"]] == ["status"] and m["events"][0]["data"]["phase"] == "cancel_requested"
    assert web.get(f"/api/jobs/{jid}").json()["status"] == "cancelled"
    assert web.post("/api/send", json={"text": "解锁了", "scope": "stock"}).status_code == 201
    assert web.post("/api/messages/99999/cancel").status_code == 404


def test_stale_job_fails_and_unlocks_message(web, agent, bridge):
    r = web.post("/api/send", json={"text": "x", "scope": "stock"}).json()
    jid = r["job_id"]
    agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0})
    bridge.store.conn.execute("UPDATE bridge_jobs SET heartbeat_at=datetime('now','-1 hour') WHERE id=?", (jid,))
    jobs = web.get("/api/jobs").json()["items"]
    assert jobs[0]["status"] == "failed" and "心跳" in jobs[0]["error"]
    m = web.get("/api/threads/main/messages").json()["items"][-1]
    assert m["status"] == "error" and "心跳" in m["content"] and m["events"][-1]["data"]["kind"] == "stale"
    assert web.post("/api/send", json={"text": "y", "scope": "stock"}).status_code == 201
    # a later finish from the worker is still the final word
    agent.post(f"/api/agent/jobs/{jid}/finish", json={"ok": True, "result": "{}"})
    assert web.get(f"/api/jobs/{jid}").json()["status"] == "done"


def test_threads_scopes_select_patch_delete(web, bridge):
    assert web.get("/api/threads?scope=fund").json()["current"] == "main-fund"
    assert web.get("/api/threads?scope=bogus").status_code == 400
    assert web.get("/api/threads?scope=stock").json()["current"] == "main"  # default thread created lazily
    t = web.post("/api/threads", json={"scope": "stock"}).json()["thread"]
    assert web.get("/api/threads?scope=stock").json()["current"] == t
    items = web.get("/api/threads?scope=stock").json()["items"]
    assert {i["id"] for i in items} == {"main", t}
    assert web.get(f"/api/threads/{t}/messages").json()["items"][0]["role"] == "system"

    assert web.post("/api/threads/main/select", json={"scope": "stock"}).json()["thread"] == "main"
    assert web.post("/api/threads/nope/select", json={"scope": "stock"}).status_code == 404
    assert web.get("/api/threads?scope=stock").json()["current"] == "main"

    web.patch(f"/api/threads/{t}", json={"title": "标题", "pinned": True})
    th = web.get(f"/api/threads/{t}").json()["thread"]
    assert th["title"] == "标题" and th["pinned"] is True
    assert web.get("/api/threads?scope=stock").json()["items"][0]["id"] == t  # pinned first

    # review-style keyed threads
    r = web.post("/api/send", json={"text": "解读", "scope": "review", "key": "2026-09-22"}).json()
    assert web.get("/api/threads/find?scope=review&key=2026-09-22").json()["thread"] == r["thread"]
    r2 = web.post("/api/send", json={"text": "再解读", "scope": "review", "key": "2026-09-22"})
    assert r2.status_code == 409  # same keyed thread, still in flight
    assert web.get("/api/threads?scope=stock").json()["current"] == "main"  # keyed threads never take "current"

    d = web.delete("/api/threads/main?scope=stock").json()
    assert d["current"] == t and bridge.deleted == ["main"]
    assert web.get("/api/threads?scope=stock").json()["current"] == t
    assert web.delete("/api/threads/main").status_code == 404


def test_settings_validation_and_aliases(web, bridge):
    s = web.get("/api/settings").json()
    assert s["chat"] == {"model": "", "effort": "", "max_turns": 40, "auto_context": True}
    assert s["models"][1]["id"] == "sonnet" and "" in s["efforts"]
    assert web.put("/api/settings", json={"effort": "turbo"}).status_code == 400
    assert web.put("/api/settings", json={"max_turns": 0}).status_code == 422
    r = web.put("/api/settings", json={"model": "claude-sonnet-4-5", "effort": "HIGH", "auto_context": False}).json()
    assert r["chat"]["model"] == "sonnet" and r["chat"]["effort"] == "high" and r["chat"]["auto_context"] is False
    bridge.store.set_meta("settings", json.dumps({"model": "x", "bogus": 1}))
    assert "bogus" not in web.get("/api/settings").json()["chat"]


def test_agent_initiated_chat_and_payload_passthrough(web, agent):
    r = agent.post(
        "/api/agent/chat",
        json={"text": "请解读今天", "scope": "review", "key": "2026-09-22", "new_thread": True, "payload": {"notify": True}},
    )
    assert r.status_code == 201
    job = agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}).json()["job"]
    assert job["payload"]["notify"] is True and job["payload"]["thread"] == r.json()["thread"]
    assert web.get("/api/threads/find?scope=review&key=2026-09-22").json()["thread"] == r.json()["thread"]
    st = agent.get("/api/agent/status").json()
    assert st["running"] == 1 and st["queued"] == 0


def test_text_limits(web, bridge):
    bridge.config.max_text_len = 10
    r = web.post("/api/send", json={"text": "x" * 11, "scope": "stock"})
    assert r.status_code == 400 and "上限" in r.json()["detail"]
    assert web.post("/api/send", json={"text": "   ", "scope": "stock"}).status_code == 400
    assert web.post("/api/send", json={"text": "", "scope": "stock"}).status_code == 422
    assert web.post("/api/threads/nope/messages", json={"text": "hi"}).status_code == 404


def test_non_chat_jobs_are_generic(agent, bridge):
    jid = bridge.store.enqueue_job("review", {"day": "2026-09-22"})
    job = agent.post("/api/agent/jobs/next", json={"kinds": ["review", "chat"], "wait": 0}).json()["job"]
    assert job["id"] == jid
    assert agent.post(f"/api/agent/jobs/{jid}/events", json={"deltas": ["ignored"]}).json()["cancel"] is False
    agent.post(f"/api/agent/jobs/{jid}/finish", json={"ok": True, "result": json.dumps({"chars": 12})})
    assert bridge.store.get_job(jid)["status"] == "done"
    assert agent.post("/api/agent/jobs/99/events", json={}).status_code == 404
