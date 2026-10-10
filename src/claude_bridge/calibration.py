"""How much use (list-price USD) 100% of a 5h / weekly window is, from utilization samples vs. machine-wide use.

The server reports utilization in whole percent and doesn't say how it weighs tokens. For each sample
(utilization u, window start s, time t) the worker machine's use in [s, t) is known from its transcripts,
so `use / u` is one estimate of the window's capacity. Use the account makes elsewhere (claude.ai, other
devices) isn't in the transcripts and pulls single estimates low, and the 1% rounding scatters them; pooling
every (window, percent level) seen recently and taking an upper quantile damps both.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from typing import Any

MIN_UTILIZATION = 0.03  # below this the 1% rounding dominates
MIN_GROUPS = 4  # distinct (window, percent) levels needed before an estimate is shown
QUANTILE = 0.75
ROUNDING_MID = 0.005  # a reported 0.06 means somewhere in [0.06, 0.07)


def _quantile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def estimate_capacity(samples: list[dict[str, Any]], series: list[tuple[float, float]], span: float) -> dict[str, Any] | None:
    """samples: [{at, utilization (0..1), resets_at}]; series: [(bucket epoch, usd)] sorted, all sources.

    Returns {cap_usd, groups, low, high} or None when there isn't enough to go on.
    """
    if not series:
        return None
    times = [t for t, _ in series]
    prefix = [0.0]
    for _, c in series:
        prefix.append(prefix[-1] + c)
    covered_from = times[0]

    def use(start: float, end: float) -> float:
        return prefix[bisect.bisect_left(times, end)] - prefix[bisect.bisect_left(times, start)]

    groups: dict[tuple[float, float], list[float]] = defaultdict(list)
    for s in samples:
        u, resets, at = s.get("utilization"), s.get("resets_at"), s.get("at")
        if not isinstance(u, int | float) or not isinstance(resets, int | float) or not isinstance(at, int | float):
            continue
        start = resets - span
        if u < MIN_UTILIZATION or at < start or start < covered_from:
            continue
        spent = use(start, at)
        if spent > 0:
            groups[(resets, round(u, 4))].append(spent)
    ratios = [sum(v) / len(v) / (u + ROUNDING_MID) for (_, u), v in groups.items()]
    if len(ratios) < MIN_GROUPS:
        return None
    return {"cap_usd": _quantile(ratios, QUANTILE), "groups": len(ratios),
            "low": _quantile(ratios, 0.25), "high": _quantile(ratios, 0.9)}
