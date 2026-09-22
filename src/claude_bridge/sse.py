from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from starlette.concurrency import run_in_threadpool

from claude_bridge.broker import Broker
from claude_bridge.service import BridgeService

PING = b": ping\n\n"
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def encode_frame(event: str, data: Any, id: int | None = None) -> bytes:
    lines = []
    if id is not None:
        lines.append(f"id: {id}")
    lines.append(f"event: {event}")
    payload = json.dumps(data, ensure_ascii=False, default=str)
    lines.extend(f"data: {ln}" for ln in payload.split("\n"))
    return ("\n".join(lines) + "\n\n").encode()


async def event_stream(
    service: BridgeService,
    broker: Broker,
    thread_id: str,
    *,
    after: int = 0,
    last_event_id: int = 0,
    heartbeat: float = 15.0,
) -> AsyncIterator[bytes]:
    # Subscribe before taking the snapshot so nothing published in between is lost;
    # duplicates are harmless (deltas carry rev, events carry id).
    q = broker.subscribe(thread_id)
    try:
        snap = await run_in_threadpool(service.snapshot, thread_id, after, last_event_id)
        yield encode_frame("snapshot", snap, id=snap["cursor"])
        while True:
            try:
                frame = await asyncio.wait_for(q.get(), timeout=heartbeat)
            except TimeoutError:
                yield PING
                continue
            if frame is None:
                break
            yield encode_frame(frame["type"], frame["data"], id=frame.get("id"))
    finally:
        broker.unsubscribe(thread_id, q)
