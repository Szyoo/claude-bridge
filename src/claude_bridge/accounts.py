"""Multi-user accounts for `claude-bridge serve --multi-user`: users, password login, long-lived sessions, quotas.

Quotas are shares of the subscription the worker's `claude` is logged into. The CLI only reports the
account-wide 5h / weekly utilization in whole percent, far too coarse to split per turn, so each user's
use is booked in USD-equivalent (`total_cost_usd`, exact per turn) and turned into a percentage with the
admin's "100% of the window ≈ $X". Separately, a guard stops ordinary users once the whole account's
utilization reaches a threshold, so the admin always keeps some for themself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from claude_bridge.errors import BadRequest, NotFound, QuotaExceeded
from claude_bridge.principal import Principal
from claude_bridge.service import LIMIT_WINDOWS, BridgeService
from claude_bridge.store import BridgeStore

ROLES = ("admin", "user")
WINDOW_LABELS = {"five_hour": "5 小时", "seven_day": "本周"}
SHARE_TEXT = {"five_hour": "你的 5 小时份额", "seven_day": "你的本周份额"}
ACCOUNT_TEXT = {"five_hour": "整个账户的 5 小时额度", "seven_day": "整个账户的本周额度"}
PBKDF2_ITERATIONS = 390_000
USERNAME_RE = re.compile(r"^[\w.@-]{2,32}$")
MIN_PASSWORD = 8
LAST_SEEN_EVERY = 300  # seconds between last_seen_at writes for one user
MIN_ESTIMATE_UTILIZATION = 0.05  # below this the whole-percent utilization is too coarse to estimate from

SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name  TEXT NOT NULL DEFAULT '',
  pw_hash       TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
  disabled      INTEGER NOT NULL DEFAULT 0,
  limit_5h_pct  REAL,
  limit_7d_pct  REAL,
  note          TEXT NOT NULL DEFAULT '',
  token_version INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen_at  TEXT
);
"""

DEFAULT_QUOTA: dict[str, Any] = {"cap_5h_usd": None, "cap_7d_usd": None, "guard_5h_pct": None, "guard_7d_pct": None}
LIMIT_FIELD = {"five_hour": "limit_5h_pct", "seven_day": "limit_7d_pct"}
CAP_FIELD = {"five_hour": "cap_5h_usd", "seven_day": "cap_7d_usd"}
GUARD_FIELD = {"five_hour": "guard_5h_pct", "seven_day": "guard_7d_pct"}


# ---------------- passwords ----------------


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, *, iterations: int | None = None) -> str:
    iterations = iterations or PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, _unb64(digest))


def generate_password() -> str:
    """12 characters, no look-alikes — something the admin can read out or paste into a chat."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


_dummy_hash: list[str] = []  # built on first failed lookup, not at import (the worker imports this module too)


def _dummy() -> str:
    if not _dummy_hash:
        _dummy_hash.append(hash_password("x" * 16))
    return _dummy_hash[0]


def check_new_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise BadRequest(f"密码至少 {MIN_PASSWORD} 位")
    if len(password) > 200:
        raise BadRequest("密码太长")


def clean_username(name: str) -> str:
    name = (name or "").strip()
    if not USERNAME_RE.match(name):
        raise BadRequest("用户名 2–32 位：字母、数字、汉字、. _ - @")
    return name


def _pct(value: Any, label: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise BadRequest(f"{label} 需要是数字") from e
    if not 0 <= v <= 100:
        raise BadRequest(f"{label} 需在 0–100 之间")
    return v


def _usd(value: Any, label: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise BadRequest(f"{label} 需要是数字") from e
    if v <= 0:
        raise BadRequest(f"{label} 需大于 0")
    return v


def public_user(row: dict[str, Any], *, admin_view: bool = False) -> dict[str, Any]:
    """What a page may see of an account; the admin's note is only for the admin page."""
    keys = ["id", "username", "display_name", "role", "limit_5h_pct", "limit_7d_pct", "created_at", "last_seen_at"]
    if admin_view:
        keys.append("note")
    return {k: row.get(k) for k in keys} | {"disabled": bool(row.get("disabled"))}


class Accounts:
    def __init__(self, store: BridgeStore, *, secret: str = "", session_days: int = 365, tz: str = "") -> None:
        self.store = store
        self.session_seconds = session_days * 86400
        self.tz = ZoneInfo(tz) if tz else None
        self.service: BridgeService | None = None  # set by the app once the bridge exists (quota needs its windows)
        self._seen: dict[int, float] = {}
        with store.lock:
            store.conn.executescript(SCHEMA)
        if not secret:  # persisted, so sessions survive restarts without configuring a secret
            secret = store.get_meta("accounts_secret") or ""
            if not secret:
                secret = secrets.token_hex(32)
                store.set_meta("accounts_secret", secret)
        self._key = secret.encode()

    # ---------------- users ----------------

    def users(self) -> list[dict[str, Any]]:
        return self.store._q("SELECT * FROM bridge_users ORDER BY role, id")

    def get(self, uid: int) -> dict[str, Any] | None:
        return self.store._one("SELECT * FROM bridge_users WHERE id=?", (uid,))

    def by_name(self, username: str) -> dict[str, Any] | None:
        return self.store._one("SELECT * FROM bridge_users WHERE username=?", ((username or "").strip(),))

    def count(self, *, role: str | None = None, active: bool = False) -> int:
        where, params = ["1=1"], []
        if role:
            where.append("role=?")
            params.append(role)
        if active:
            where.append("disabled=0")
        row = self.store._one(f"SELECT COUNT(*) AS n FROM bridge_users WHERE {' AND '.join(where)}", tuple(params))
        return int(row["n"]) if row else 0

    def create(
        self,
        username: str,
        password: str,
        *,
        role: str = "user",
        display_name: str = "",
        limit_5h_pct: float | None = None,
        limit_7d_pct: float | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        username = clean_username(username)
        check_new_password(password)
        if role not in ROLES:
            raise BadRequest("角色只能是 admin / user")
        if self.by_name(username):
            raise BadRequest("用户名已被占用")
        first = self.count() == 0
        cur = self.store._x(
            "INSERT INTO bridge_users(username, display_name, pw_hash, role, limit_5h_pct, limit_7d_pct, note) "
            "VALUES(?,?,?,?,?,?,?) RETURNING *",
            (username, (display_name or "").strip()[:40], hash_password(password), role,
             _pct(limit_5h_pct, "5 小时上限"), _pct(limit_7d_pct, "每周上限"), (note or "")[:200]),
        )
        user = dict(cur.fetchone())
        if first and role == "admin":
            self._adopt_shared(str(user["id"]))
        return user

    def _adopt_shared(self, owner: str) -> None:
        """The first admin inherits whatever the single-password install had (threads, uploads, settings)."""
        self.store.reassign_owner("", owner)
        rows = self.store._q("SELECT key, value FROM bridge_meta WHERE key='settings' OR key LIKE 'current_thread:%'")
        for r in rows:
            self.store.set_meta(BridgeService.meta_key(owner, r["key"]), r["value"])

    def update(self, uid: int, patch: dict[str, Any], *, acting: dict[str, Any] | None = None) -> dict[str, Any]:
        """Admin edits (and self-service when `patch` only has username / display_name). `acting` = the admin."""
        user = self.get(uid)
        if not user:
            raise NotFound("没有这个用户")
        sets: dict[str, Any] = {}
        if "username" in patch and patch["username"] is not None:
            name = clean_username(patch["username"])
            other = self.by_name(name)
            if other and other["id"] != uid:
                raise BadRequest("用户名已被占用")
            sets["username"] = name
        if "display_name" in patch and patch["display_name"] is not None:
            sets["display_name"] = str(patch["display_name"]).strip()[:40]
        if "note" in patch and patch["note"] is not None:
            sets["note"] = str(patch["note"])[:200]
        for key, label in (("limit_5h_pct", "5 小时上限"), ("limit_7d_pct", "每周上限")):
            if key in patch:
                sets[key] = _pct(patch[key], label)
        demoting = "role" in patch and patch["role"] is not None and patch["role"] != user["role"]
        disabling = "disabled" in patch and patch["disabled"] is not None and bool(patch["disabled"]) != bool(user["disabled"])
        if demoting:
            if patch["role"] not in ROLES:
                raise BadRequest("角色只能是 admin / user")
            sets["role"] = patch["role"]
        if disabling:
            sets["disabled"] = 1 if patch["disabled"] else 0
        is_self = acting is not None and acting["id"] == uid
        if is_self and ((demoting and patch["role"] != "admin") or (disabling and patch["disabled"])):
            raise BadRequest("不能停用或降级自己")
        if user["role"] == "admin" and not user["disabled"] and (
            (demoting and patch["role"] != "admin") or (disabling and patch["disabled"])
        ) and self.count(role="admin", active=True) <= 1:
            raise BadRequest("至少要保留一个可用的管理员")
        if not sets:
            return user
        bump = ", token_version=token_version+1" if disabling and patch["disabled"] else ""
        cols = ", ".join(f"{k}=?" for k in sets)
        self.store._x(f"UPDATE bridge_users SET {cols}{bump} WHERE id=?", (*sets.values(), uid))
        return self.get(uid) or user

    def set_password(self, uid: int, password: str) -> dict[str, Any]:
        """Also signs out every session of that user (the caller reissues the current one when it's self-service)."""
        check_new_password(password)
        self.store._x("UPDATE bridge_users SET pw_hash=?, token_version=token_version+1 WHERE id=?", (hash_password(password), uid))
        user = self.get(uid)
        if not user:
            raise NotFound("没有这个用户")
        return user

    def delete(self, uid: int, *, acting: dict[str, Any] | None = None) -> list[str]:
        user = self.get(uid)
        if not user:
            raise NotFound("没有这个用户")
        if acting is not None and acting["id"] == uid:
            raise BadRequest("不能删除自己")
        if user["role"] == "admin" and not user["disabled"] and self.count(role="admin", active=True) <= 1:
            raise BadRequest("至少要保留一个可用的管理员")
        names = self.store.delete_owner(str(uid))
        self.store._x("DELETE FROM bridge_users WHERE id=?", (uid,))
        return names

    # ---------------- login / sessions ----------------

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        """The user when the password matches (disabled users included — the caller says why they can't get in)."""
        user = self.by_name(username)
        if not user:
            verify_password(password, _dummy())  # same cost as a real check: no username probing by timing
            return None
        return user if verify_password(password, user["pw_hash"]) else None

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def issue_token(self, user: dict[str, Any]) -> str:
        payload = f"{user['id']}.{user['token_version']}.{int(time.time()) + self.session_seconds}"
        return f"{payload}.{self._sign(payload)}"

    def token_age(self, token: str | None) -> float | None:
        """Seconds since the token was issued (for sliding renewal), None if malformed."""
        try:
            exp = int((token or "").split(".")[2])
        except (IndexError, ValueError):
            return None
        return time.time() - (exp - self.session_seconds)

    def verify_token(self, token: str | None) -> dict[str, Any] | None:
        parts = (token or "").split(".")
        if len(parts) != 4 or not all(p.isdigit() for p in parts[:3]):
            return None
        uid, ver, exp, sig = int(parts[0]), int(parts[1]), int(parts[2]), parts[3]
        if exp < time.time() or not hmac.compare_digest(sig, self._sign(".".join(parts[:3]))):
            return None
        user = self.get(uid)
        if not user or user["disabled"] or user["token_version"] != ver:
            return None
        now = time.time()
        if now - self._seen.get(uid, 0) > LAST_SEEN_EVERY:
            self._seen[uid] = now
            self.store._x("UPDATE bridge_users SET last_seen_at=datetime('now') WHERE id=?", (uid,))
        return user

    @staticmethod
    def principal(user: dict[str, Any]) -> Principal:
        return Principal(owner=str(user["id"]), admin=user["role"] == "admin", name=user["username"])

    # ---------------- quota ----------------

    def quota_config(self) -> dict[str, Any]:
        try:
            saved = json.loads(self.store.get_meta("quota") or "{}")
        except json.JSONDecodeError:
            saved = {}
        return {**DEFAULT_QUOTA, **{k: v for k, v in saved.items() if k in DEFAULT_QUOTA}}

    def save_quota_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        cur = self.quota_config()
        for name in LIMIT_WINDOWS:
            label = WINDOW_LABELS[name]
            if CAP_FIELD[name] in patch:
                cur[CAP_FIELD[name]] = _usd(patch[CAP_FIELD[name]], f"{label}换算")
            if GUARD_FIELD[name] in patch:
                cur[GUARD_FIELD[name]] = _pct(patch[GUARD_FIELD[name]], f"{label}保护线")
        self.store.set_meta("quota", json.dumps(cur))
        return cur

    def _svc(self) -> BridgeService:
        if self.service is None:
            raise RuntimeError("Accounts.service is not set")
        return self.service

    def fmt_time(self, ts: float | None) -> str:
        if not ts:
            return ""
        return datetime.fromtimestamp(ts, self.tz).strftime("%m-%d %H:%M")

    def account_status(self) -> dict[str, Any]:
        """The subscription as a whole: utilization, reset times, what the bridge spent, and a capacity estimate."""
        svc = self._svc()
        limits = svc.account_limits()
        cfg = self.quota_config()
        out: dict[str, Any] = {}
        for name in LIMIT_WINDOWS:
            w = limits[name]
            spent = sum(v["cost_usd"] for v in svc.usage_in_window(name).values())
            util = w.get("utilization")
            # util covers the bridge plus anything else on this account, so spent/util can only undershoot 100%'s worth
            estimate = spent / util if util and util >= MIN_ESTIMATE_UTILIZATION and spent > 0 else None
            out[name] = {
                "utilization_pct": round(util * 100, 1) if isinstance(util, int | float) else None,
                "resets_at": w.get("resets_at"), "window_start": w["window_start"], "observed_at": w.get("at"),
                "source": w.get("source"), "bridge_cost_usd": round(spent, 4),
                "estimate_cap_usd": round(estimate, 2) if estimate else None,
                "cap_usd": cfg[CAP_FIELD[name]], "guard_pct": cfg[GUARD_FIELD[name]],
            }
        return out

    def usage_for(self, user: dict[str, Any], account: dict[str, Any] | None = None) -> dict[str, Any]:
        """One user's standing in each window, plus why they're blocked right now (None = free to send)."""
        svc = self._svc()
        account = account or self.account_status()
        owner = str(user["id"])
        admin = user["role"] == "admin"
        out: dict[str, Any] = {"blocked": None}
        for name in LIMIT_WINDOWS:
            acc = account[name]
            cost = svc.usage_in_window(name, owner).get(owner, {"cost_usd": 0.0, "turns": 0})
            cap = acc["cap_usd"]
            pct = cost["cost_usd"] / cap * 100 if cap else None
            limit = None if admin else user.get(LIMIT_FIELD[name])
            out[name] = {"cost_usd": round(cost["cost_usd"], 4), "turns": cost["turns"],
                         "used_pct": round(pct, 1) if pct is not None else None, "limit_pct": limit,
                         "resets_at": acc["resets_at"], "window_start": acc["window_start"]}
            if admin or out["blocked"]:
                continue
            when = self.fmt_time(acc["resets_at"])
            again = f"，{when} 重置" if when else ""
            guard, util = acc["guard_pct"], acc["utilization_pct"]
            if guard is not None and util is not None and util >= guard:
                out["blocked"] = f"{ACCOUNT_TEXT[name]}已用 {util:.0f}%，暂停普通用户使用{again}"
            elif limit is not None and pct is not None and pct >= limit:
                out["blocked"] = f"{SHARE_TEXT[name]}已用完（{pct:.0f}% / {limit:g}%）{again}"
            elif limit is not None and limit <= 0:
                out["blocked"] = f"管理员没有给你分配{WINDOW_LABELS[name]}额度"
        return out

    def check_quota(self, principal: Principal) -> None:
        """`BridgeConfig.check_quota`: admins are never limited; users by their share and the account guard."""
        if principal.admin or not principal.owner:
            return
        user = self.get(int(principal.owner))
        if not user or user["disabled"]:
            raise QuotaExceeded("账户已停用", status=403)
        reason = self.usage_for(user)["blocked"]
        if reason:
            raise QuotaExceeded(reason)
