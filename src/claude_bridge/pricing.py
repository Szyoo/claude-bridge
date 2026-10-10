"""Token counts → API-list-price USD, the common unit for the usage ledger and the limit calibration.

The CLI's `total_cost_usd` can't be booked per turn: on a resumed session it is often the running total of
the whole session, so every later turn would re-book everything before it. Token counts in `usage` are per
invocation, so each turn is priced here from those instead.

Subscription and seat limits aren't billed in dollars, but list prices weight models and token kinds
(output vs input vs cache) roughly the way the limits do; the absolute scale is calibrated against the
utilization the server reports (see `calibration.py`).
"""

from __future__ import annotations

import re
from typing import Any

# USD per million tokens: (input, output, cache read). Cache writes are 1.25× input (5-minute TTL) / 2× (1 hour).
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-mythos-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-opus-4-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-sonnet-4-5": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
# a model id this table doesn't know yet is priced as the newest of its family
FAMILY_DEFAULT = {"fable": "claude-fable-5-1", "mythos": "claude-mythos-5-1", "opus": "claude-opus-5-5",
                  "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
DEFAULT_MODEL = "claude-sonnet-5-5"
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0

_SUFFIX_RE = re.compile(r"(-\d{8}|\[1m\])$")


def price_key(model: str | None) -> str:
    """Normalize a model id (date suffix, `[1m]`, provider prefix) to a PRICES key."""
    m = (model or "").strip().lower()
    m = _SUFFIX_RE.sub("", m)
    m = m.rsplit(".", 1)[-1] if m.startswith(("anthropic.", "us.anthropic.", "eu.anthropic.")) else m
    if m in PRICES:
        return m
    for fam, key in FAMILY_DEFAULT.items():
        if fam in m:
            return key
    return DEFAULT_MODEL


def _int(v: Any) -> int:
    return int(v) if isinstance(v, int | float) and v > 0 else 0


def token_cost(
    model: str | None,
    *,
    input_tokens: Any = 0,
    output_tokens: Any = 0,
    cache_read: Any = 0,
    cache_write: Any = 0,
    cache_write_1h: Any = 0,
) -> float:
    """USD at list price. `cache_write` is the total written; `cache_write_1h` the part of it with the 1-hour TTL."""
    pin, pout, pread = PRICES[price_key(model)]
    write, write_1h = _int(cache_write), min(_int(cache_write_1h), _int(cache_write))
    return (
        _int(input_tokens) * pin
        + _int(output_tokens) * pout
        + _int(cache_read) * pread
        + (write - write_1h) * pin * CACHE_WRITE_5M
        + write_1h * pin * CACHE_WRITE_1H
    ) / 1_000_000


def usage_cost(model: str | None, usage: dict[str, Any]) -> float:
    """Price an Anthropic `usage` object (API response, CLI result, or a transcript line)."""
    cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    return token_cost(
        model,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read=usage.get("cache_read_input_tokens"),
        cache_write=usage.get("cache_creation_input_tokens"),
        cache_write_1h=cc.get("ephemeral_1h_input_tokens"),
    )


def has_tokens(usage: dict[str, Any]) -> bool:
    return any(isinstance(usage.get(k), int | float) for k in
               ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
