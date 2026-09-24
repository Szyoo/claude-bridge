"""/context 报告解析、回答后自动刷新构成、context / compact 任务、服务端存取与推送。"""

from __future__ import annotations

import json

from claude_bridge.stream_json import StreamState, parse_context_report, parse_token_count
from test_worker import HAPPY, FakeClient, chat_job, init_ev, make_worker

REPORT = """## Context Usage

**Model:** claude-opus-5
**Tokens:** 770.2k / 1M (77%)

### Estimated usage by category

| Category | Tokens | Percentage |
|----------|--------|------------|
| System prompt | 11.2k | 1.1% |
| System tools | 23.8k | 2.4% |
| MCP tools | 15.1k | 1.5% |
| MCP tools (deferred) | 129.3k | — |
| Skills | 7.7k | 0.8% |
| Memory files | 5.9k | 0.6% |
| Messages | 706.6k | 70.7% |
| Autocompact buffer | 33k | 3.3% |
| Free space | 196.8k | 19.7% |

### Skills

| Skill | Source | Tokens |
|-------|--------|--------|
| dataviz | Built-in | ~360 |
"""


def test_parse_token_count():
    assert parse_token_count("42.1k") == 42100 and parse_token_count("1M") == 1_000_000
    assert parse_token_count("460") == 460 and parse_token_count("< 20") == 20 and parse_token_count("~80") == 80
    assert parse_token_count("") is None


def test_parse_context_report():
    r = parse_context_report(REPORT)
    assert r["model"] == "claude-opus-5" and r["used"] == 770200 and r["window"] == 1_000_000 and r["pct"] == 77
    names = [c["name"] for c in r["categories"]]
    assert names[0] == "System prompt" and "Free space" in names and len(names) == 9
    msg = next(c for c in r["categories"] if c["name"] == "Messages")
    assert msg["tokens"] == 706600 and msg["pct"] == 70.7 and msg["deferred"] is False
    deferred = next(c for c in r["categories"] if "deferred" in c["name"])
    assert deferred["deferred"] is True and deferred["pct"] is None and deferred["tokens"] == 129300
    assert r["autocompact_pct"] == 97
    assert parse_context_report("nothing here")["used"] is None


def test_compact_boundary_becomes_event():
    st = StreamState()
    out = st.feed({"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto", "pre_tokens": 900000, "post_tokens": 12000, "duration_ms": 9000}})
    assert out[0][1]["type"] == "compact" and out[0][1]["data"]["pre_tokens"] == 900000 and st.compactions[0]["trigger"] == "auto"
    assert st.feed({"type": "system", "subtype": "status", "status": "compacting"}) == []


def context_result(md: str = REPORT) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "result": md, "num_turns": 0, "total_cost_usd": 0}


def test_chat_finish_carries_context_report(tmp_path, monkeypatch):
    worker, client = make_worker(tmp_path, HAPPY)
    calls = []

    def fake_report(session_id, settings=None, cfg=None):
        calls.append((session_id, settings.get("model")))
        return parse_context_report(REPORT)

    monkeypatch.setattr(worker, "context_report", fake_report)
    worker.run_chat(chat_job(model="opus"))
    fin = client.finished[0]
    assert fin["ok"] is True and fin["context"]["used"] == 770200 and json.loads(fin["result"])["context"]["pct"] == 77
    assert calls == [("sess-1", "opus")]


def test_context_report_runs_slash_context(tmp_path):
    worker, _ = make_worker(tmp_path, [context_result()])
    r = worker.context_report("sess-9", {"model": "sonnet"})
    assert r["used"] == 770200
    args = json.loads((tmp_path / "args.json").read_text())
    assert args[:3] == ["-p", "/context", "--output-format"] and args[args.index("--resume") + 1] == "sess-9"
    assert args[args.index("--model") + 1] == "sonnet" and "--include-partial-messages" not in args
    # a broken binary just yields None instead of failing the chat
    worker2, _ = make_worker(tmp_path, [{"type": "result", "is_error": True, "result": "boom"}])
    assert worker2.context_report("sess-9") is None


def test_session_jobs_context_and_compact(tmp_path):
    client = FakeClient([
        {"id": 1, "kind": "context", "payload": {"thread": "t", "session_id": "s1", "settings": {}}},
        {"id": 2, "kind": "context", "payload": {"thread": "t", "session_id": None, "settings": {}}},
    ])
    worker, _ = make_worker(tmp_path, [context_result()], client=client)
    assert worker.kinds[:3] == ["chat", "context", "compact"]
    worker.run_once(wait=0)
    worker.run_once(wait=0)
    ok, bad = client.finished
    assert ok["ok"] is True and ok["context"]["pct"] == 77 and json.loads(ok["result"])["context"]["window"] == 1_000_000
    assert bad["ok"] is False and "没有 Claude 会话" in bad["error"]

    # compact: the fake claude prints a compact_boundary then a result; the follow-up /context reuses the same script
    lines = [
        init_ev(),
        {"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "manual", "pre_tokens": 42955, "post_tokens": 3940, "cumulative_dropped_tokens": 39015, "duration_ms": 15968}},
        context_result(),
    ]
    client = FakeClient([{"id": 3, "kind": "compact", "payload": {"thread": "t", "session_id": "s1", "settings": {}}}])
    worker, _ = make_worker(tmp_path, lines, client=client)
    worker.run_once(wait=0)
    fin = client.finished[0]
    res = json.loads(fin["result"])
    assert fin["ok"] is True and res["compact"]["pre_tokens"] == 42955 and res["compact"]["post_tokens"] == 3940
    assert fin["context"]["used"] == 770200
    calls = [json.loads(ln) for ln in (tmp_path / "args.jsonl").read_text().splitlines()]
    assert calls[-2][1] == "/compact" and calls[-1][1] == "/context"  # compact, then the context refresh


def test_server_stores_context_and_queues_session_jobs(web, agent, bridge):
    r = web.post("/api/send", json={"text": "hi", "scope": "stock"}).json()
    mid, jid = r["message_id"], r["job_id"]
    # no session yet → cannot refresh / compact
    assert web.post("/api/threads/main/context").status_code == 400
    agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0})
    report = parse_context_report(REPORT)
    agent.post(f"/api/agent/jobs/{jid}/finish", json={"ok": True, "result": "{}", "session_id": "s1", "context": report})
    th = web.get("/api/threads/main").json()["thread"]
    assert th["session_id"] == "s1" and th["context"]["used"] == 770200 and th["context"]["at"]
    assert th["context"]["categories"][6]["name"] == "Messages"

    # refresh + compact queue worker jobs carrying the session id
    r = web.post("/api/threads/main/context")
    assert r.status_code == 202
    job = agent.post("/api/agent/jobs/next", json={"kinds": ["context", "compact"], "wait": 0}).json()["job"]
    assert job["id"] == r.json()["job_id"] and job["kind"] == "context" and job["payload"]["session_id"] == "s1"
    agent.post(f"/api/agent/jobs/{job['id']}/finish", json={"ok": True, "result": "{}", "context": {**report, "used": 1000}})
    assert web.get("/api/threads/main").json()["thread"]["context"]["used"] == 1000

    assert web.post("/api/threads/main/compact").status_code == 202
    web.post("/api/send", json={"text": "again", "scope": "stock"})
    assert web.post("/api/threads/main/compact").status_code == 409  # answering → no compaction
    assert web.post("/api/threads/nope/compact").status_code == 404

    # deleting the thread drops its stored context
    bridge.store.set_status(mid, "done")
    for m in bridge.store.messages("main"):
        if m["status"] in ("pending", "streaming"):
            bridge.store.set_status(m["id"], "cancelled")
    web.delete("/api/threads/main?scope=stock")
    assert bridge.store.get_meta("context:main") is None
