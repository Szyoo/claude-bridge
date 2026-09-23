"""压缩进度：排队 / 开始 / 结束都推 job 帧、快照里带进行中的任务、完成后会话里留一行、压缩过程中有心跳。"""

from __future__ import annotations

import json

from test_context import context_result
from test_worker import FakeClient, init_ev, make_worker


def ready_thread(web, agent):
    """A thread with a Claude session, so it can be compacted."""
    r = web.post("/api/send", json={"text": "hi", "scope": "stock"}).json()
    agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0})
    agent.post(f"/api/agent/jobs/{r['job_id']}/finish", json={"ok": True, "result": "{}", "session_id": "s1"})
    return r["thread"]


def record(bridge, monkeypatch):
    """Capture every frame the service publishes: [(thread, type, data)]."""
    seen: list[tuple[str, str, dict]] = []
    orig = bridge.service._pub

    def spy(thread, type, data, id=None):
        seen.append((thread, type, data))
        orig(thread, type, data, id=id)

    monkeypatch.setattr(bridge.service, "_pub", spy)
    return seen


def test_compaction_progress_frames_snapshot_and_note(web, agent, bridge, monkeypatch):
    tid = ready_thread(web, agent)
    seen = record(bridge, monkeypatch)

    r = web.post(f"/api/threads/{tid}/compact").json()
    assert r["job"]["status"] == "queued" and r["job"]["kind"] == "compact"
    # a second click while it is queued reports the same job instead of queueing another
    assert web.post(f"/api/threads/{tid}/compact").json()["job_id"] == r["job_id"]
    assert [j["status"] for j in web.get(f"/api/threads/{tid}").json()["jobs"]] == ["queued"]

    job = agent.post("/api/agent/jobs/next", json={"kinds": ["compact"], "wait": 0}).json()["job"]
    snap_jobs = web.get(f"/api/threads/{tid}").json()["jobs"]
    assert snap_jobs[0]["status"] == "running" and snap_jobs[0]["started_at"]   # a reload mid-compaction still sees it

    result = {"compact": {"trigger": "manual", "pre_tokens": 744000, "post_tokens": 31500}}
    agent.post(f"/api/agent/jobs/{job['id']}/finish", json={"ok": True, "result": json.dumps(result)})
    got = [(t, d) for th, t, d in seen if th == tid and t in ("job", "message")]
    assert [(t, d["role"] if t == "message" else d["status"]) for t, d in got] == [
        ("job", "queued"), ("job", "running"), ("message", "system"), ("job", "done")]
    assert "744k → 31.5k" in got[2][1]["content"]
    assert web.get(f"/api/threads/{tid}").json()["jobs"] == []
    # the note is a real message in the thread's history
    assert any(m["role"] == "system" and "已压缩会话历史" in m["content"] for m in web.get(f"/api/threads/{tid}/messages").json()["items"])


def test_stale_compaction_is_reported_to_the_page(web, agent, bridge, monkeypatch):
    tid = ready_thread(web, agent)
    web.post(f"/api/threads/{tid}/compact")
    job = agent.post("/api/agent/jobs/next", json={"kinds": ["compact"], "wait": 0}).json()["job"]
    seen = record(bridge, monkeypatch)
    bridge.store._x("UPDATE bridge_jobs SET heartbeat_at=datetime('now','-1 hour') WHERE id=?", (job["id"],))
    bridge.service.requeue_stale()
    got = [d for th, t, d in seen if th == tid and t == "job"]
    assert got and got[-1]["status"] == "failed" and got[-1]["error"] == "helper 心跳超时"


COMPACT_LINES = [
    init_ev(),
    {"__sleep__": 0.8},
    {"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "manual", "pre_tokens": 744000, "post_tokens": 31500}},
    context_result(),
]


def compact_job():
    return {"id": 9, "kind": "compact", "payload": {"thread": "t", "session_id": "s1", "settings": {}}}


def test_compact_heartbeats_while_running(tmp_path):
    client = FakeClient([compact_job()])
    worker, _ = make_worker(tmp_path, COMPACT_LINES, client=client, session_heartbeat=0.1)
    worker.run_once(wait=0)
    assert client.finished[0]["ok"] is True
    assert len(client.posts) >= 3  # empty event posts = heartbeats (each also asks "should I stop?")


def test_compact_stops_when_the_server_says_cancel(tmp_path):
    class Cancelling(FakeClient):
        def post_events(self, job_id, **kw):
            super().post_events(job_id, **kw)
            return {"ok": True, "cancel": True}

    client = Cancelling([compact_job()])
    worker, _ = make_worker(tmp_path, COMPACT_LINES, client=client, session_heartbeat=0.1)
    worker.run_once(wait=0)
    fin = client.finished[0]
    assert fin["ok"] is False and "取消" in fin["error"]
