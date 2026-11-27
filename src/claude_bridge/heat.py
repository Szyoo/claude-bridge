"""Spend-rate watch: which ordinary users are using the subscription fast right now.

A turn's cost is normally known only when it ends, and one agent turn with many parallel subagents can run for an
hour. The worker therefore reports each running turn's spend so far (read from its transcripts, subagents
included) about once a minute; together with what finished turns book, that gives every owner's spend over the
last few minutes. An owner whose recent spend passes a share of their 5-hour allowance, or who runs many turns at
once, is flagged ("hot") for a while and the admin is notified; `BridgeService.is_hot` exposes the flag.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from claude_bridge.store import BridgeStore

WINDOW = 600.0  # seconds of recent spend that count
COOL = 1800.0  # an owner stays hot this long after the last time they tripped a rule
SHARE = 0.2  # recent spend ≥ this share of the owner's 5h allowance → hot
RUNNING = 5  # this many turns running at once → hot
RECHECK = 15.0  # seconds between checks of one owner
NOTIFY_EVERY = 3600.0  # one notification per subject per hour
ACCOUNT_RISE = 20.0  # the account's 5h utilization rising this many points…
ACCOUNT_RISE_WINDOW = 900.0  # …within this many seconds → notify


class HeatWatch:
    def __init__(self, store: BridgeStore, *, allowance: Callable[[str], float | None] | None = None,
                 notify: Callable[[str, str], None] | None = None, name_of: Callable[[str], str] | None = None) -> None:
        self.store = store
        self.allowance = allowance  # owner → their 5h allowance in list-price USD (None: an admin, or not calibrated)
        self.notify = notify
        self.name_of = name_of or (lambda owner: owner)
        self.lock = threading.Lock()
        self.spend: dict[str, deque[tuple[float, float]]] = {}
        self.live: dict[int, float] = {}  # job → spend reported so far
        self.checked: dict[str, float] = {}
        self.notified: dict[str, float] = {}

    # ---------------- spend ----------------

    def add(self, owner: str, usd: float, at: float | None = None) -> None:
        if not owner or usd <= 0:
            return
        with self.lock:
            q = self.spend.setdefault(owner, deque())
            q.append((at or time.time(), usd))
            self._trim(q)

    def live_total(self, owner: str, job_id: int, total: float) -> None:
        """A running turn's spend so far (cumulative): book the part not seen yet."""
        with self.lock:
            prev = self.live.get(job_id, 0.0)
            self.live[job_id] = max(prev, total)
        self.add(owner, total - prev)

    def booked(self, owner: str, job_id: int | None, usd: float) -> None:
        """A finished turn's final cost: book what its live reports did not already cover."""
        with self.lock:
            prev = self.live.pop(job_id, 0.0) if job_id is not None else 0.0
        self.add(owner, usd - prev)

    def recent(self, owner: str) -> float:
        with self.lock:
            q = self.spend.get(owner)
            if not q:
                return 0.0
            self._trim(q)
            return sum(usd for _, usd in q)

    @staticmethod
    def _trim(q: deque[tuple[float, float]]) -> None:
        cutoff = time.time() - WINDOW
        while q and q[0][0] < cutoff:
            q.popleft()

    # ---------------- hot owners ----------------

    def _state(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.store.get_meta("quota_heat") or "{}")
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _save(self, state: dict[str, dict[str, Any]]) -> None:
        self.store.set_meta("quota_heat", json.dumps(state, ensure_ascii=False))

    def hot(self) -> dict[str, dict[str, Any]]:
        """Owners currently hot: {owner: {since, last, reason}} (cooled ones are dropped)."""
        state = self._state()
        now = time.time()
        live = {o: s for o, s in state.items() if now - float(s.get("last") or 0) < COOL}
        if len(live) != len(state):
            self._save(live)
        return live

    def is_hot(self, owner: str) -> bool:
        return owner in self.hot()

    def clear(self, owner: str) -> None:
        """The admin clears an owner's flag (it comes back if they trip a rule again)."""
        state = self._state()
        if state.pop(owner, None) is not None:
            self._save(state)
        with self.lock:
            self.spend.pop(owner, None)

    def check(self, owner: str, running: int, *, force: bool = False) -> str | None:
        """Re-evaluate an owner (at most every RECHECK s unless forced); returns why they are hot now, if they are."""
        if not owner or self.allowance is None:
            return None
        now = time.monotonic()
        if not force and now - self.checked.get(owner, 0.0) < RECHECK:
            return None
        self.checked[owner] = now
        allowance = self.allowance(owner)
        if allowance is None:  # an admin (or an unknown owner): never watched
            return None
        spent = self.recent(owner)
        reason = None
        if allowance > 0 and spent >= SHARE * allowance:
            reason = f"10 分钟用掉 5 小时额度的 {spent / allowance:.0%}（约 ${spent:.2f}）"
        elif running >= RUNNING:
            reason = f"同时运行 {running} 个任务"
        if reason is None:
            return None
        state = self._state()
        t = time.time()
        new = owner not in state or t - float(state[owner].get("last") or 0) >= COOL
        state[owner] = {"since": t if new else state[owner].get("since", t), "last": t, "reason": reason}
        self._save(state)
        if new:
            self.alert(f"hot:{owner}", f"高用量：{self.name_of(owner)}", f"{reason}。可在管理页查看或紧急停止。")
        return reason

    # ---------------- the account as a whole ----------------

    def account_rise(self, samples: list[dict[str, Any]], now_util: float) -> None:
        """The account's 5h utilization jumped: notify (whoever caused it, the admin included)."""
        cutoff = time.time() - ACCOUNT_RISE_WINDOW
        past = [s["utilization"] for s in samples if s["at"] >= cutoff]
        if not past:
            return
        rise = (now_util - min(past)) * 100
        if rise >= ACCOUNT_RISE:
            self.alert("account-rise", "额度消耗过快",
                       f"账户 5 小时用量 15 分钟内上涨 {rise:.0f} 个百分点，现在 {now_util * 100:.0f}%。")

    # ---------------- notifications ----------------

    def alert(self, key: str, title: str, body: str) -> None:
        if self.notify is None:
            return
        now = time.monotonic()
        with self.lock:
            if now - self.notified.get(key, -NOTIFY_EVERY) < NOTIFY_EVERY:
                return
            self.notified[key] = now
        threading.Thread(target=self._send, args=(title, body), daemon=True).start()

    def _send(self, title: str, body: str) -> None:
        try:
            self.notify(title, body)  # type: ignore[misc]
        except Exception:  # a notification must never break a turn
            pass
