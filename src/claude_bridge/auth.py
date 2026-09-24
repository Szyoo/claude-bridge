"""Auth helpers: bearer token for the agent router, HMAC-cookie password login for standalone serve."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections.abc import Callable
from hashlib import sha256

from fastapi import HTTPException, Request

COOKIE_NAME = "bridge_session"
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 60


def bearer_auth(token: str | Callable[[], str | None]) -> Callable[[Request], None]:
    def dependency(request: Request) -> None:
        expected = token() if callable(token) else token
        if not expected:
            raise HTTPException(status_code=503, detail="服务端未配置 agent 令牌")
        given = request.headers.get("authorization", "")
        if not given.startswith("Bearer ") or not hmac.compare_digest(given[7:], expected):
            raise HTTPException(status_code=401, detail="agent 令牌无效")

    return dependency


class LoginLimiter:
    """At most `attempts` failed logins per key (IP, username) within `window` seconds."""

    def __init__(self, attempts: int = _MAX_ATTEMPTS, window: float = _WINDOW_SECONDS) -> None:
        self.attempts = attempts
        self.window = window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_limited(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if hits:
                self._hits[key] = hits
            else:
                self._hits.pop(key, None)
            return len(hits) >= self.attempts

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._hits.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class PasswordAuth:
    """Single-user password → signed session cookie `<exp>.<hmac>`; per-IP login rate limit."""

    def __init__(self, password: str, secret: str = "", *, disabled: bool = False, session_days: int = 30) -> None:
        self.password = password or ""
        self.disabled = disabled
        self.session_seconds = session_days * 86400
        self._key = (secret or secrets.token_hex(32)).encode()
        self._limiter = LoginLimiter()

    @property
    def configured(self) -> bool:
        return self.disabled or bool(self.password)

    def check_password(self, given: str) -> bool:
        return bool(self.password) and hmac.compare_digest(given.encode(), self.password.encode())

    def issue_token(self) -> str:
        exp = str(int(time.time()) + self.session_seconds)
        return f"{exp}.{self._sign(exp)}"

    def verify_token(self, token: str | None) -> bool:
        if self.disabled:
            return True
        if not token or "." not in token:
            return False
        exp, sig = token.rsplit(".", 1)
        if not exp.isdigit() or int(exp) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(exp))

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), sha256).hexdigest()

    def is_limited(self, ip: str) -> bool:
        return self._limiter.is_limited(ip)

    def record_failure(self, ip: str) -> None:
        self._limiter.record_failure(ip)

    def clear_failures(self, ip: str) -> None:
        self._limiter.clear(ip)

    def dependency(self) -> Callable[[Request], None]:
        def dep(request: Request) -> None:
            if not self.verify_token(request.cookies.get(COOKIE_NAME)):
                raise HTTPException(status_code=401, detail="未登录")

        return dep
