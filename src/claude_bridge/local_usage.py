"""Token use of every Claude Code session on the worker's machine, from its transcripts.

The account's 5h / weekly utilization counts everything on the subscription or seat, not just the bridge.
Claude Code writes each API response's `usage` into `~/.claude/projects/**/*.jsonl`, so the worker can add
up the machine's whole use (the bridge's own sessions and everything else run there) in one-minute
buckets per model and report them; the server lines these up with the utilization samples to calibrate how much
use 1% of each window is. Only aggregates leave the machine: token counts and list-price USD per bucket.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_bridge.pricing import price_key, usage_cost

log = logging.getLogger(__name__)

BUCKET_SECONDS = 60
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
ROW_FIELDS = {"input_tokens": "input_tokens", "output_tokens": "output_tokens",
              "cache_read_input_tokens": "cache_read_tokens", "cache_creation_input_tokens": "cache_write_tokens"}


def bucket_text(ts: float) -> str:
    return datetime.fromtimestamp(ts - ts % BUCKET_SECONDS, UTC).strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(v: Any) -> float | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class _Seen:
    key: tuple[str, str, str]
    ts: float
    tokens: tuple[int, ...]
    cost: float


@dataclass
class TranscriptScanner:
    """Incremental: remembers how far each file was read. A fresh scanner (worker restart) re-reads the last
    `horizon_days` and re-sends absolute bucket totals, which the server upserts, so restarts are harmless."""

    bridge_roots: list[str] = field(default_factory=list)  # cwd prefixes of the bridge's own sessions
    root: Path = field(default_factory=lambda: Path.home() / ".claude" / "projects")
    horizon_days: float = 8.0  # the weekly window plus a day
    _offsets: dict[str, tuple[int, int]] = field(default_factory=dict)  # path → (inode, bytes consumed)
    _seen: dict[str, _Seen] = field(default_factory=dict)  # message id → what was booked for it
    _buckets: dict[tuple[str, str, str], dict[str, float]] = field(default_factory=dict)  # (minute, source, model)
    _dirty: set[tuple[str, str, str]] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.bridge_roots = [os.path.realpath(os.path.expanduser(r)).rstrip("/") + "/" for r in self.bridge_roots if r]

    def source_of(self, cwd: Any) -> str:
        c = (os.path.realpath(cwd) if isinstance(cwd, str) and cwd else "").rstrip("/") + "/"
        return "bridge" if any(c.startswith(r) for r in self.bridge_roots) else "local"

    # ---------------- reading ----------------

    def scan(self, now: float | None = None) -> list[dict[str, Any]]:
        """Read what's new and return the buckets that changed (absolute totals)."""
        now = time.time() if now is None else now
        cutoff = now - self.horizon_days * 86400
        if self.root.is_dir():
            for path in self.root.rglob("*.jsonl"):
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    continue
                self._read(path, st, cutoff)
        self._prune(cutoff)
        return self.take_dirty()

    def _read(self, path: Path, st: os.stat_result, cutoff: float) -> None:
        key = str(path)
        inode, done = self._offsets.get(key, (st.st_ino, 0))
        if inode != st.st_ino or st.st_size < done:  # replaced or truncated: start over
            done = 0
        if st.st_size == done:
            self._offsets[key] = (st.st_ino, done)
            return
        try:
            with path.open("rb") as f:
                f.seek(done)
                data = f.read(st.st_size - done)
        except OSError as e:
            log.debug("cannot read %s: %s", path, e)
            return
        end = data.rfind(b"\n")
        if end < 0:  # a line still being written
            return
        for raw in data[: end + 1].splitlines():
            self._line(raw, cutoff)
        self._offsets[key] = (st.st_ino, done + end + 1)

    def _line(self, raw: bytes, cutoff: float) -> None:
        if b'"usage"' not in raw:
            return
        try:
            ev = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(ev, dict) or ev.get("type") != "assistant":
            return
        msg = ev.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
            return
        model = msg.get("model")
        if not isinstance(model, str) or model.startswith("<"):  # "<synthetic>": local, no API call
            return
        ts = _parse_ts(ev.get("timestamp"))
        mid = msg.get("id") or ev.get("requestId") or ev.get("uuid")
        if ts is None or ts < cutoff or not isinstance(mid, str):
            return
        usage = msg["usage"]
        tokens = tuple(int(usage.get(k) or 0) for k in TOKEN_FIELDS)
        cost = usage_cost(model, usage)
        prev = self._seen.get(mid)
        if prev is None:
            bkey = (bucket_text(ts), self.source_of(ev.get("cwd")), price_key(model))
            self._add(bkey, tokens, cost, 1)
            self._seen[mid] = _Seen(bkey, ts, tokens, cost)
        elif tokens != prev.tokens and sum(tokens) > sum(prev.tokens):
            # the same response written again with final counts (streamed blocks): book only the growth
            self._add(prev.key, tuple(a - b for a, b in zip(tokens, prev.tokens, strict=True)), cost - prev.cost, 0)
            prev.tokens, prev.cost = tokens, cost

    def _add(self, key: tuple[str, str, str], tokens: tuple[int, ...], cost: float, requests: int) -> None:
        b = self._buckets.setdefault(key, {"cost_usd": 0.0, "requests": 0, **{v: 0 for v in ROW_FIELDS.values()}})
        b["cost_usd"] += cost
        b["requests"] += requests
        for name, n in zip(TOKEN_FIELDS, tokens, strict=True):
            b[ROW_FIELDS[name]] += n
        self._dirty.add(key)

    def _prune(self, cutoff: float) -> None:
        old = bucket_text(cutoff)
        for k in [k for k in self._buckets if k[0] < old]:
            self._buckets.pop(k)
            self._dirty.discard(k)
        for mid in [m for m, s in self._seen.items() if s.ts < cutoff]:
            self._seen.pop(mid)
        self._offsets = {p: v for p, v in self._offsets.items() if os.path.exists(p)}

    # ---------------- reporting ----------------

    def take_dirty(self) -> list[dict[str, Any]]:
        rows = [{"bucket": k[0], "source": k[1], "model": k[2], **{f: (round(v, 6) if f == "cost_usd" else int(v)) for f, v in self._buckets[k].items()}}
                for k in sorted(self._dirty) if k in self._buckets]
        self._dirty.clear()
        return rows

    def requeue(self, rows: list[dict[str, Any]]) -> None:
        """Report failed: send these buckets (their latest totals) next time."""
        self._dirty.update((r["bucket"], r["source"], r["model"]) for r in rows)
