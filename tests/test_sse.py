from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest
import requests

from claude_bridge.sse import encode_frame, event_stream


def parse_frames(raw: bytes | str):
    """Split raw SSE text into (event, id, data); comments (pings) come back as ('ping', None, None)."""
    text = raw.decode() if isinstance(raw, bytes) else raw
    out = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        if block.startswith(":"):
            out.append(("ping", None, None))
            continue
        ev, fid, data = None, None, []
        for ln in block.split("\n"):
            if ln.startswith("event: "):
                ev = ln[7:]
            elif ln.startswith("id: "):
                fid = int(ln[4:])
            elif ln.startswith("data: "):
                data.append(ln[6:])
        out.append((ev, fid, json.loads("\n".join(data)) if data else None))
    return out


async def collect(gen, *, until="done", max_frames=50, timeout=5.0):
    frames = []
    try:
        async with asyncio.timeout(timeout):
            async for chunk in gen:
                frames.extend(parse_frames(chunk))
                if any(f[0] == until for f in frames) or len(frames) >= max_frames:
                    break
    finally:
        await gen.aclose()
    return frames


def test_encode_frame():
    assert encode_frame("x", {"a": "多行\n文本"}, id=7) == 'id: 7\nevent: x\ndata: {"a": "多行\\n文本"}\n\n'.encode()
    assert encode_frame("y", None) == b"event: y\ndata: null\n\n"


@pytest.mark.anyio
async def test_stream_snapshot_then_live_frames(bridge, web, agent):
    r = web.post("/api/send", json={"text": "问题", "scope": "stock"}).json()
    mid, jid = r["message_id"], r["job_id"]

    async def worker():
        await asyncio.sleep(0.05)
        post = agent.post
        await asyncio.to_thread(post, "/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0})
        await asyncio.to_thread(
            post,
            f"/api/agent/jobs/{jid}/events",
            json={
                "status": "streaming",
                "deltas": ["你好"],
                "events": [{"type": "tool_use", "data": {"id": "t1", "name": "Bash", "input": {}}}],
            },
        )
        await asyncio.to_thread(post, f"/api/agent/jobs/{jid}/events", json={"deltas": ["！"]})
        await asyncio.to_thread(post, f"/api/agent/jobs/{jid}/finish", json={"ok": True, "result": "{}", "session_id": "s1"})

    gen = event_stream(bridge.service, bridge.broker, "main", heartbeat=0.2)
    task = asyncio.create_task(worker())
    frames = await collect(gen)
    await task
    assert bridge.broker.subscribers("main") == 0

    assert frames[0][0] == "snapshot"
    snap = frames[0][2]
    assert snap["inflight"] == mid and [m["role"] for m in snap["messages"]] == ["user", "assistant"]
    assert snap["cursor"] == 0 and frames[0][1] == 0
    live = [f for f in frames[1:] if f[0] != "ping"]
    assert [f[0] for f in live] == ["status", "delta", "event", "delta", "status", "done"]
    assert live[0][2] == {"message_id": mid, "status": "streaming", "rev": 0}
    assert live[1][2] == {"message_id": mid, "text": "你好", "rev": 1}
    assert live[2][1] == live[2][2]["id"] and live[2][2]["type"] == "tool_use"
    assert live[3][2] == {"message_id": mid, "text": "！", "rev": 2}
    assert live[4][2]["status"] == "done"
    assert live[5][2]["job"]["status"] == "done" and live[5][2]["message_id"] == mid


@pytest.mark.anyio
async def test_stream_heartbeat_and_last_event_id_trims_snapshot(bridge, web, agent):
    r = web.post("/api/send", json={"text": "问题", "scope": "stock"}).json()
    mid, jid = r["message_id"], r["job_id"]
    agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0})
    agent.post(
        f"/api/agent/jobs/{jid}/events",
        json={"events": [{"type": "tool_use", "data": {"id": "a"}}, {"type": "tool_result", "data": {"tool_use_id": "a"}}]},
    )
    ev_ids = [e["id"] for e in bridge.store.events(mid)]

    frames = await collect(event_stream(bridge.service, bridge.broker, "main", heartbeat=0.1), until="ping", timeout=3)
    assert frames[0][0] == "snapshot" and frames[0][1] == ev_ids[-1]
    inflight_events = next(m for m in frames[0][2]["messages"] if m["id"] == mid)["events"]
    assert [e["id"] for e in inflight_events] == ev_ids
    assert frames[-1][0] == "ping"

    frames = await collect(
        event_stream(bridge.service, bridge.broker, "main", last_event_id=ev_ids[0], heartbeat=0.1), until="ping", timeout=3
    )
    inflight_events = next(m for m in frames[0][2]["messages"] if m["id"] == mid)["events"]
    assert [e["id"] for e in inflight_events] == ev_ids[1:]


@pytest.mark.anyio
async def test_stream_after_cursor_tail_and_overflow(bridge):
    bridge.store.create_thread("main", scope="stock")
    ids = [bridge.store.add_message("main", "user", f"m{i}")["id"] for i in range(5)]
    bridge.config.messages_limit = 2
    frames = await collect(event_stream(bridge.service, bridge.broker, "main", heartbeat=0.1), until="ping", timeout=3)
    assert [m["id"] for m in frames[0][2]["messages"]] == ids[-2:]  # newest N when after=0
    frames = await collect(
        event_stream(bridge.service, bridge.broker, "main", after=ids[1], heartbeat=0.1), until="ping", timeout=3
    )
    assert [m["id"] for m in frames[0][2]["messages"]] == ids[2:4]  # oldest N after the cursor

    # a slow subscriber whose queue overflows gets a sentinel and the stream ends cleanly
    bridge.broker.maxsize = 2
    gen = event_stream(bridge.service, bridge.broker, "main", heartbeat=5)
    first = await gen.__anext__()
    assert parse_frames(first)[0][0] == "snapshot"
    for i in range(5):
        bridge.broker.publish("main", {"type": "thread", "data": {"i": i}})
    await asyncio.sleep(0.05)
    rest = []
    async with asyncio.timeout(2):
        async for chunk in gen:
            rest.append(chunk)
    assert rest == [] or all(parse_frames(c)[0][0] == "thread" for c in rest)
    assert bridge.broker.subscribers("main") == 0


# ---------------- real HTTP round trip through the route ----------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(app):
    uvicorn = pytest.importorskip("uvicorn")
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(timeout=5)


def test_stream_over_http(live_server):
    base = live_server
    r = requests.post(f"{base}/api/send", json={"text": "hi", "scope": "stock"}, timeout=5).json()
    mid, jid = r["message_id"], r["job_id"]
    hdr = {"Authorization": "Bearer tok"}
    resp = requests.get(f"{base}/api/threads/main/stream", stream=True, timeout=10)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache, no-transform" and resp.headers["x-accel-buffering"] == "no"

    def worker():
        time.sleep(0.2)
        requests.post(f"{base}/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}, headers=hdr, timeout=5)
        requests.post(f"{base}/api/agent/jobs/{jid}/events", json={"deltas": ["a"]}, headers=hdr, timeout=5)
        requests.post(f"{base}/api/agent/jobs/{jid}/finish", json={"ok": True}, headers=hdr, timeout=5)

    threading.Thread(target=worker, daemon=True).start()
    frames = []
    buf = b""
    for chunk in resp.iter_content(chunk_size=None):
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            frames.extend(parse_frames(block + b"\n\n"))
        if any(f[0] == "done" for f in frames):
            break
    resp.close()
    kinds = [f[0] for f in frames if f[0] != "ping"]
    assert kinds[0] == "snapshot" and kinds[-1] == "done" and "delta" in kinds
    assert frames[0][2]["inflight"] == mid
    assert requests.get(f"{base}/api/threads/nope/stream", timeout=5).status_code == 404
