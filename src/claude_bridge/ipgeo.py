"""IP → 归属地，给管理页（用户列表的最近访问、访问记录）显示「日本 东京都 东京 · ARTERIA Networks」。

公网地址查 ip-api.com batch（zh-CN，免 key），结果按 IP 永久缓存在 bridge_ipgeo 表；查不出的
（status=fail）也记一行空结果，不再重查。内网 / tailnet / 保留地址不出网，直接给标签。网络失败
不缓存，下次打开页面再试。只有管理员接口会调用这里。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any

from claude_bridge.store import BridgeStore

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_ipgeo (
  ip         TEXT PRIMARY KEY,
  country    TEXT NOT NULL DEFAULT '',
  region     TEXT NOT NULL DEFAULT '',
  city       TEXT NOT NULL DEFAULT '',
  isp        TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
API = "http://ip-api.com/batch?lang=zh-CN&fields=status,query,country,regionName,city,isp"
BATCH = 100  # ip-api's batch limit
TAILNET = ipaddress.ip_network("100.64.0.0/10")
TAILNET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

Fetch = Callable[[list[str]], list[dict[str, Any]]]


def _fetch_ip_api(ips: list[str]) -> list[dict[str, Any]]:
    req = urllib.request.Request(API, data=json.dumps(ips).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as r:  # noqa: S310 — fixed http URL
        return json.loads(r.read())


def _label(ip: str) -> dict[str, str] | None:
    """{label} for addresses that never leave the box; None for public ones (and garbage → handled by caller)."""
    addr = ipaddress.ip_address(ip)
    if addr in TAILNET or addr in TAILNET6:
        return {"label": "Tailscale 内网"}
    if not addr.is_global:
        return {"label": "内网"}
    return None


class IpGeo:
    def __init__(self, store: BridgeStore, *, fetch: Fetch | None = None) -> None:
        self.store = store
        self.fetch = fetch or _fetch_ip_api
        with store.lock:
            store.conn.executescript(SCHEMA)

    def lookup(self, ips: Iterable[str]) -> dict[str, dict[str, str] | None]:
        """ip → {country, region, city, isp} | {label} | None (unknown / not looked up yet)."""
        out: dict[str, dict[str, str] | None] = {}
        public: list[str] = []
        for ip in dict.fromkeys(i for i in ips if i):
            try:
                label = _label(ip)
            except ValueError:
                out[ip] = None
                continue
            if label:
                out[ip] = label
            else:
                public.append(ip)
        if not public:
            return out
        cached = self._cached(public)
        missing = [ip for ip in public if ip not in cached]
        for i in range(0, len(missing), BATCH):
            cached.update(self._query(missing[i : i + BATCH]))
        for ip in public:
            g = cached.get(ip)
            out[ip] = g if g and any(g.values()) else None
        return out

    def _cached(self, ips: list[str]) -> dict[str, dict[str, str]]:
        marks = ",".join("?" * len(ips))
        rows = self.store._q(f"SELECT ip, country, region, city, isp FROM bridge_ipgeo WHERE ip IN ({marks})", tuple(ips))
        return {r.pop("ip"): r for r in rows}

    def _query(self, ips: list[str]) -> dict[str, dict[str, str]]:
        try:
            res = self.fetch(ips)
        except Exception as e:  # network down, rate limited, … — try again next time
            log.info("ip-api lookup failed: %s", e)
            return {}
        got: dict[str, dict[str, str]] = {}
        for g in res if isinstance(res, list) else []:
            ip = g.get("query")
            if not ip:
                continue
            ok = g.get("status") == "success"
            row = {k: (str(g.get(src) or "")[:100] if ok else "")
                   for k, src in (("country", "country"), ("region", "regionName"), ("city", "city"), ("isp", "isp"))}
            self.store._x("INSERT OR REPLACE INTO bridge_ipgeo(ip, country, region, city, isp) VALUES(?,?,?,?,?)",
                          (ip, row["country"], row["region"], row["city"], row["isp"]))
            got[ip] = row
        return got
