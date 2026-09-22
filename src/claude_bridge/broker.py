"""In-process fan-out of SSE frames, one queue per subscriber, keyed by thread id.

Publishers run in worker threads (sync FastAPI routes); subscribers are async generators on the event loop.
Single-process only: with several uvicorn workers a publish would only reach subscribers in its own process.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

Frame = dict[str, Any]


class Broker:
    def __init__(self, maxsize: int = 2000) -> None:
        self.maxsize = maxsize
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self, thread_id: str) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=self.maxsize)
        with self._lock:
            self._loop = loop
            self._subs.setdefault(thread_id, set()).add(q)
        return q

    def unsubscribe(self, thread_id: str, q: asyncio.Queue) -> None:
        with self._lock:
            subs = self._subs.get(thread_id)
            if subs:
                subs.discard(q)
                if not subs:
                    del self._subs[thread_id]

    def subscribers(self, thread_id: str) -> int:
        with self._lock:
            return len(self._subs.get(thread_id, ()))

    def publish(self, thread_id: str, frame: Frame) -> None:
        with self._lock:
            loop = self._loop
            targets = list(self._subs.get(thread_id, ()))
        if not targets or loop is None or loop.is_closed():
            return

        def _deliver() -> None:
            for q in targets:
                try:
                    q.put_nowait(frame)
                except asyncio.QueueFull:
                    # Slow consumer: drop its backlog and end the stream; the client reconnects and resyncs.
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    q.put_nowait(None)

        try:
            loop.call_soon_threadsafe(_deliver)
        except RuntimeError:
            pass  # loop shut down between the check and the call
