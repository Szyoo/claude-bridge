"""Worker backends: another provider behind an Anthropic-compatible endpoint (e.g. DeepSeek) for chosen admins.

The worker holds the provider's key (`--backends`); the server only learns each backend's label, models, allowed
accounts and API prices. A backend's threads live in scopes "<scope>@<name>", so the page keeps them apart from
Claude's (its own thread list, model menu and settings). Its turns are booked at the provider's API rates in
`bridge_backend_usage` — never in the Claude ledger, so they touch neither quotas nor the window calibration.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any

from claude_bridge.store import BridgeStore, utc_text

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_backend_usage (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  owner              TEXT NOT NULL DEFAULT '',
  backend            TEXT NOT NULL,
  model              TEXT,
  thread             TEXT,
  message_id         INTEGER,
  job_id             INTEGER,
  kind               TEXT NOT NULL DEFAULT 'chat',
  input_tokens       INTEGER NOT NULL DEFAULT 0,
  output_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  peak               INTEGER NOT NULL DEFAULT 0,
  cost               REAL NOT NULL DEFAULT 0,      -- in `currency` (the backend's pricing currency)
  currency           TEXT NOT NULL DEFAULT 'USD',
  created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bridge_backend_usage ON bridge_backend_usage(owner, backend, created_at);
"""

PERIODS = (("day", "24 小时", 86400), ("week", "7 天", 7 * 86400), ("month", "30 天", 30 * 86400))


def split_backend(scope: str) -> tuple[str, str]:
    """"code:app@deepseek" → ("code:app", "deepseek"); a scope without "@" runs on Claude itself."""
    base, sep, backend = (scope or "").rpartition("@")
    return (base, backend) if sep else (scope or "", "")


def _int(v: Any) -> int:
    return int(v) if isinstance(v, int | float) and v > 0 else 0


def is_peak(at: datetime, peak: dict[str, Any] | None) -> bool:
    """`peak` = {"utc_hours": [[1, 4], [6, 10]], "weekdays_only": true, "multiplier": 2}: inside one of the
    [start, end) UTC hour ranges (Monday–Friday only with weekdays_only). Public holidays aren't known here,
    so a holiday's peak hours are booked at the peak rate — the estimate errs high, never low."""
    if not peak:
        return False
    at = at.astimezone(UTC)
    if peak.get("weekdays_only") and at.weekday() >= 5:
        return False
    h = at.hour + at.minute / 60
    return any(float(a) <= h < float(b) for a, b in peak.get("utc_hours") or [] if a is not None and b is not None)


def price_for(pricing: dict[str, dict[str, float]], model: str | None) -> dict[str, float] | None:
    """The model's prices: exact name (without "[1m]"), else a key contained in the name, else "default"."""
    m = re.sub(r"\[1m\]$", "", (model or "").strip().lower())
    if m in pricing:
        return pricing[m]
    for key in sorted(pricing, key=len, reverse=True):
        if key != "default" and key.lower() in m:
            return pricing[key]
    return pricing.get("default")


def currency_of(info: dict[str, Any] | None) -> str:
    """The backend's pricing currency ("CNY" for DeepSeek's yuan price list); USD when it doesn't say."""
    return str((info or {}).get("currency") or "USD").upper()


def turn_cost(info: dict[str, Any], model: str | None, usage: dict[str, Any], at: datetime | None,
              peak: bool | None = None) -> tuple[float, bool]:
    """The cost of one `usage` (Anthropic shape) at the provider's API rates, in its pricing currency, and whether it
    fell in peak hours (`peak` given = already known, e.g. when re-pricing a booked row).

    DeepSeek's Anthropic endpoint reports cache hits in `cache_read_input_tokens` and leaves them out of
    `input_tokens`; it has no cache writes (always 0), so any are priced as ordinary input."""
    price = price_for(info.get("pricing") or {}, model) or {}
    peak = is_peak(at, info.get("peak")) if peak is None else peak
    mult = float((info.get("peak") or {}).get("multiplier") or 1) if peak else 1.0
    cost = (
        (_int(usage.get("input_tokens")) + _int(usage.get("cache_creation_input_tokens"))) * price.get("input", 0)
        + _int(usage.get("cache_read_input_tokens")) * price.get("cache_read", price.get("input", 0))
        + _int(usage.get("output_tokens")) * price.get("output", 0)
    ) * mult / 1_000_000
    return cost, peak


class BackendLedger:
    """The backends' own usage rows, priced when booked."""

    def __init__(self, store: BridgeStore) -> None:
        self.store = store
        with store.lock:
            store.conn.executescript(SCHEMA)
            cols = {r[1] for r in store.conn.execute("PRAGMA table_info(bridge_backend_usage)")}
            if "cost_usd" in cols:  # before v0.24: every backend was priced in USD
                store.conn.execute("ALTER TABLE bridge_backend_usage RENAME COLUMN cost_usd TO cost")
            if "currency" not in cols:
                store.conn.execute("ALTER TABLE bridge_backend_usage ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'")
            if "api_ms" not in cols:  # the turn's model time (duration_api_ms): output speed of local models
                store.conn.execute("ALTER TABLE bridge_backend_usage ADD COLUMN api_ms INTEGER NOT NULL DEFAULT 0")
            store.conn.commit()

    def reprice(self, backend: str, info: dict[str, Any]) -> int:
        """Rows booked in another currency than the backend's current one, re-priced from their tokens and peak flag
        (exact: both were stored). Runs when the worker reports its backends; returns how many rows changed."""
        cur = currency_of(info)
        rows = self.store._q("SELECT * FROM bridge_backend_usage WHERE backend=? AND currency!=?", (backend, cur))
        for r in rows:
            usage = {"input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                     "cache_read_input_tokens": r["cache_read_tokens"], "cache_creation_input_tokens": r["cache_write_tokens"]}
            cost, _ = turn_cost(info, r["model"], usage, None, peak=bool(r["peak"]))
            self.store._x("UPDATE bridge_backend_usage SET cost=?, currency=? WHERE id=?", (cost, cur, r["id"]))
        return len(rows)

    def add(self, owner: str, backend: str, info: dict[str, Any], data: dict[str, Any], *, thread: str | None = None,
            message_id: int | None = None, job_id: int | None = None, kind: str = "chat") -> None:
        now = datetime.now(UTC)
        parts = [m for m in data.get("by_model") or [] if isinstance(m, dict)] or [data]
        api_ms = _int(data.get("duration_api_ms"))  # one per turn: booked on its first row
        for part in parts:
            tokens = {k: _int(part.get(k)) for k in
                      ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
            if not any(tokens.values()):
                continue
            model = part.get("model") or data.get("model")
            cost, peak = turn_cost(info, model, tokens, now)
            self.store._x(
                "INSERT INTO bridge_backend_usage(owner, backend, model, thread, message_id, job_id, kind, input_tokens, "
                "output_tokens, cache_read_tokens, cache_write_tokens, peak, cost, currency, api_ms, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner, backend, model, thread, message_id, job_id, kind, tokens["input_tokens"], tokens["output_tokens"],
                 tokens["cache_read_input_tokens"], tokens["cache_creation_input_tokens"], 1 if peak else 0, cost,
                 currency_of(info), api_ms, utc_text(now.timestamp())),
            )
            api_ms = 0

    def rows(self, owner: str, backend: str, since: str) -> list[dict[str, Any]]:
        """The owner's booked rows on `backend` since a UTC text time, oldest first (one per turn and model)."""
        return self.store._q(
            "SELECT created_at, model, job_id, peak, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost, api_ms "
            "FROM bridge_backend_usage WHERE owner=? AND backend=? AND created_at >= ? ORDER BY id",
            (owner, backend, since),
        )

    def lifetime(self, owner: str, backend: str) -> float:
        r = self.store._one("SELECT COALESCE(SUM(cost), 0) AS c FROM bridge_backend_usage WHERE owner=? AND backend=?",
                            (owner, backend)) or {}
        return round(float(r.get("c") or 0), 6)

    def totals(self, owner: str, backend: str, now: float | None = None) -> dict[str, dict[str, Any]]:
        now = time.time() if now is None else now
        out = {}
        for key, label, seconds in PERIODS:
            r = self.store._one(
                "SELECT COUNT(DISTINCT COALESCE(job_id, id)) AS turns, COALESCE(SUM(cost), 0) AS cost, "
                "COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o, "
                "COALESCE(SUM(cache_read_tokens), 0) AS r, COALESCE(SUM(api_ms), 0) AS ms, "
                "COALESCE(SUM(CASE WHEN api_ms > 0 THEN output_tokens END), 0) AS timed FROM bridge_backend_usage "
                "WHERE owner=? AND backend=? AND created_at >= ?",
                (owner, backend, utc_text(now - seconds)),
            ) or {}
            out[key] = {"label": label, "turns": int(r.get("turns") or 0), "cost": round(float(r.get("cost") or 0), 6),
                        "input_tokens": int(r.get("i") or 0), "output_tokens": int(r.get("o") or 0),
                        "cache_read_tokens": int(r.get("r") or 0),
                        # output tokens per second of model time, over the turns that report it (None: none did)
                        "tok_s": round(int(r.get("timed") or 0) / (int(r["ms"]) / 1000), 1) if r.get("ms") else None}
        return out
