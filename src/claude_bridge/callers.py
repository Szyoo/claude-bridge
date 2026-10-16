"""Caller tokens and `/api/caller/*`: one-off tasks submitted by server-side programs (not browsers, not the worker).

A caller is a named Bearer token bound to an owner (a `bridge_users` id in multi-user mode, "" otherwise) and to
the scopes it may use. Its tasks run as that owner — quota checks and the usage ledger are the owner's — but in
threads of their own: every task is a fresh, ephemeral thread (no `--resume`, no session kept on the worker), so
nothing accumulates and nothing shows up in the owner's chat list. Finished tasks are deleted after
`retention_seconds`; their usage rows stay.

Only the token's SHA-256 is stored; the token itself is shown once by `claude-bridge callers add|rotate`.

Routes (prefix chosen by the host, `/api/caller` in the bundled apps):

    POST /tasks               {text, scope, options, timeout, require_online} → {task_id, status, agent_online}
    GET  /tasks/{id}?wait=N   long-poll up to N s → {status, text, error, error_kind, usage, model, elapsed_ms}
    POST /tasks/{id}/cancel
    GET  /models · GET /status
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from claude_bridge.errors import BridgeError
from claude_bridge.models import TurnOptions
from claude_bridge.principal import Principal
from claude_bridge.service import BridgeService
from claude_bridge.store import BridgeStore

TOKEN_PREFIX = "cbc_"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TASK_STATUSES = ("queued", "running", "done", "failed", "cancelled")
# why a task did not produce text; callers branch on these, never on the message wording
ERROR_KINDS = ("offline", "timeout", "quota", "busy", "cancelled", "claude", "worker")
QUOTA_HINTS = ("usage limit", "rate limit", "limit reached", "rate_limit", "overloaded")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_callers (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,
  token_hash   TEXT NOT NULL UNIQUE,
  owner        TEXT NOT NULL DEFAULT '',
  scopes       TEXT NOT NULL DEFAULT '[]',
  max_inflight INTEGER NOT NULL DEFAULT 2,
  disabled     INTEGER NOT NULL DEFAULT 0,
  note         TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS bridge_caller_tasks (
  id          TEXT PRIMARY KEY,
  caller_id   INTEGER NOT NULL,
  thread      TEXT NOT NULL,
  message_id  INTEGER NOT NULL,
  job_id      INTEGER NOT NULL,
  created     REAL NOT NULL,
  deadline    REAL NOT NULL,
  timed_out   INTEGER NOT NULL DEFAULT 0,
  finished    REAL
);
CREATE INDEX IF NOT EXISTS idx_bridge_caller_tasks_caller ON bridge_caller_tasks(caller_id, finished);
"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def error_kind(raw_kind: str | None, message: str | None) -> str:
    """The worker's error kind / message → one of ERROR_KINDS."""
    low = (message or "").lower()
    if any(h in low for h in QUOTA_HINTS):
        return "quota"
    if raw_kind == "timeout":
        return "timeout"
    if raw_kind in ("claude", "bad_session"):
        return "claude"
    return "worker"


class Callers:
    """The callers table (works on the bridge's own SQLite store; also used by the CLI)."""

    def __init__(self, store: BridgeStore) -> None:
        self.store = store
        with store.lock:
            store.conn.executescript(SCHEMA)

    @staticmethod
    def _row(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row = dict(row)
        row.pop("token_hash", None)
        row["scopes"] = json.loads(row.get("scopes") or "[]")
        row["disabled"] = bool(row["disabled"])
        return row

    def list(self) -> list[dict[str, Any]]:
        return [r for r in (self._row(x) for x in self.store._q("SELECT * FROM bridge_callers ORDER BY id")) if r]

    def get(self, name: str) -> dict[str, Any] | None:
        return self._row(self.store._one("SELECT * FROM bridge_callers WHERE name=?", (name,)))

    def add(self, name: str, *, owner: str, scopes: list[str], max_inflight: int = 2, note: str = "") -> tuple[dict[str, Any], str]:
        if not NAME_RE.match(name):
            raise BridgeError("名称只能用小写字母、数字、- 和 _（最多 32 个字符）")
        if self.get(name):
            raise BridgeError(f"已有调用方 {name}", status=409)
        if not scopes:
            raise BridgeError("至少要给一个 scope")
        if not 1 <= max_inflight <= 20:
            raise BridgeError("max_inflight 需在 1..20")
        token = new_token()
        self.store._x(
            "INSERT INTO bridge_callers(name, token_hash, owner, scopes, max_inflight, note) VALUES(?,?,?,?,?,?)",
            (name, hash_token(token), owner, json.dumps(scopes), max_inflight, note),
        )
        row = self.get(name)
        assert row is not None
        return row, token

    def rotate(self, name: str) -> str:
        token = new_token()
        if not self.store._x("UPDATE bridge_callers SET token_hash=? WHERE name=?", (hash_token(token), name)).rowcount:
            raise BridgeError(f"没有调用方 {name}", status=404)
        return token

    def set_disabled(self, name: str, disabled: bool) -> None:
        if not self.store._x("UPDATE bridge_callers SET disabled=? WHERE name=?", (int(disabled), name)).rowcount:
            raise BridgeError(f"没有调用方 {name}", status=404)

    def verify(self, token: str) -> dict[str, Any] | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        digest = hash_token(token)
        row = self.store._one("SELECT * FROM bridge_callers WHERE token_hash=?", (digest,))
        if not row or not hmac.compare_digest(row["token_hash"], digest) or row["disabled"]:
            return None
        self.store._x("UPDATE bridge_callers SET last_used_at=datetime('now') WHERE id=?", (row["id"],))
        return self._row(row)

    # ---------------- tasks ----------------

    def add_task(self, task_id: str, caller_id: int, thread: str, message_id: int, job_id: int, deadline: float) -> None:
        self.store._x(
            "INSERT INTO bridge_caller_tasks(id, caller_id, thread, message_id, job_id, created, deadline) VALUES(?,?,?,?,?,?,?)",
            (task_id, caller_id, thread, message_id, job_id, time.time(), deadline),
        )

    def task(self, task_id: str, caller_id: int) -> dict[str, Any] | None:
        return self.store._one("SELECT * FROM bridge_caller_tasks WHERE id=? AND caller_id=?", (task_id, caller_id))

    def mark(self, task_id: str, *, timed_out: bool = False, finished: bool = False) -> None:
        if timed_out:
            self.store._x("UPDATE bridge_caller_tasks SET timed_out=1 WHERE id=?", (task_id,))
        if finished:
            self.store._x("UPDATE bridge_caller_tasks SET finished=COALESCE(finished, ?) WHERE id=?", (time.time(), task_id))

    def inflight(self, caller_id: int) -> int:
        row = self.store._one(
            "SELECT COUNT(*) AS n FROM bridge_caller_tasks t JOIN bridge_jobs j ON j.id=t.job_id "
            "WHERE t.caller_id=? AND j.status IN ('queued','running')",
            (caller_id,),
        )
        return int(row["n"]) if row else 0

    def expired(self, before: float) -> list[dict[str, Any]]:
        """Tasks whose job ended before `before` (or that were created before it and never ended)."""
        return self.store._q(
            "SELECT t.* FROM bridge_caller_tasks t LEFT JOIN bridge_jobs j ON j.id=t.job_id "
            "WHERE (t.finished IS NOT NULL AND t.finished < ?) "
            "OR ((j.id IS NULL OR j.status NOT IN ('queued','running')) AND t.created < ?)",
            (before, before),
        )

    def drop_task(self, task_id: str) -> None:
        self.store._x("DELETE FROM bridge_caller_tasks WHERE id=?", (task_id,))


# ---------------- HTTP ----------------


class CallerTaskIn(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    scope: str = Field(max_length=64)
    options: TurnOptions | None = None
    timeout: int = Field(120, ge=5, le=900)  # from submission: queued time counts
    require_online: bool = True  # refuse at once (503, offline) instead of queueing for an absent worker


class CallerTaskOut(BaseModel):
    task_id: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    text: str | None = None
    error: str | None = None
    error_kind: str | None = None
    usage: dict[str, Any] | None = None
    model: str | None = None
    elapsed_ms: int | None = None
    agent_online: bool | None = None


@dataclass
class CallerConfig:
    retention_seconds: int = 3600
    poll_interval: float = 0.4
    max_wait: int = 25


def _fail(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": message, "error_kind": kind})


def create_caller_router(
    service: BridgeService,
    callers: Callers,
    principal_for: Callable[[dict[str, Any]], Principal | None],
    config: CallerConfig | None = None,
) -> APIRouter:
    """`principal_for(caller_row)` → who the caller's tasks run as; None = the owner is gone or disabled (403)."""
    cfg = config or CallerConfig()
    store = service.store

    def caller(request: Request) -> dict[str, Any]:
        auth = request.headers.get("authorization", "")
        row = callers.verify(auth[7:].strip()) if auth.startswith("Bearer ") else None
        if not row:
            raise HTTPException(status_code=401, detail="调用方令牌无效")
        return row

    router = APIRouter(dependencies=[Depends(caller)])

    def cleanup() -> None:
        for t in callers.expired(time.time() - cfg.retention_seconds):
            th = store.get_thread(t["thread"])
            if th:
                try:
                    service.delete_thread(t["thread"], owner=th.get("owner") or "")
                except BridgeError:
                    pass
            callers.drop_task(t["id"])

    def state(t: dict[str, Any], owner: str) -> CallerTaskOut:
        job = store.get_job(t["job_id"]) or {}
        msg = store.get_message(t["message_id"], with_events=True) or {}
        now = time.time()
        jstatus = job.get("status") or "failed"
        mstatus = msg.get("status") or "error"
        if jstatus in ("queued", "running") and now > t["deadline"]:
            callers.mark(t["id"], timed_out=True)
            try:
                service.cancel(t["message_id"], owner)
            except BridgeError:
                pass
            job = store.get_job(t["job_id"]) or job
            msg = store.get_message(t["message_id"], with_events=True) or msg
            jstatus, mstatus = job.get("status") or "failed", msg.get("status") or "error"
            t = {**t, "timed_out": 1}
        out = CallerTaskOut(task_id=t["id"], status="queued")
        if mstatus == "done" and jstatus == "done":
            out.status = "done"
            out.text = msg.get("content") or ""
        elif mstatus in ("pending", "streaming") and jstatus in ("queued", "running"):
            out.status = "queued" if jstatus == "queued" else "running"
            return out
        elif jstatus == "cancelled" or mstatus == "cancelled":
            out.status = "cancelled"
            out.error_kind = "timeout" if t["timed_out"] else "cancelled"
            out.error = "超时，已取消" if t["timed_out"] else "已取消"
        else:
            out.status = "failed"
            errs = [e for e in msg.get("events") or [] if e.get("type") == "error"]
            last = (errs[-1].get("data") or {}) if errs else {}
            out.error = job.get("error") or last.get("message") or "执行失败"
            out.error_kind = error_kind(last.get("kind"), out.error)
        callers.mark(t["id"], finished=True)
        try:
            res = json.loads(job.get("result") or "null")
        except json.JSONDecodeError:
            res = None
        if isinstance(res, dict):
            out.model = res.get("model")
            out.usage = {k: res.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens", "duration_ms", "num_turns")}
        out.elapsed_ms = int((min(now, (t.get("finished") or now)) - t["created"]) * 1000)
        return out

    @router.post("/tasks", status_code=201)
    def submit(body: CallerTaskIn, c: dict[str, Any] = Depends(caller)):
        cleanup()
        if body.scope not in c["scopes"]:
            return _fail(403, "scope", f"这个调用方不能用 scope {body.scope!r}")
        principal = principal_for(c)
        if principal is None:
            return _fail(403, "owner", "调用方绑定的账户不存在或已停用")
        service.requeue_stale()
        online = service.agent_online()
        if body.require_online and not online:
            return _fail(503, "offline", "worker 不在线")
        if callers.inflight(c["id"]) >= c["max_inflight"]:
            return _fail(429, "busy", f"同时进行的任务已达上限（{c['max_inflight']}）")
        task_id = secrets.token_hex(8)
        try:
            r = service.start_chat(
                body.text, scope=body.scope, key=f"caller:{c['name']}:{task_id}", principal=principal,
                options=body.options, extra_payload={"ephemeral": True, "caller": c["name"]},
            )
        except BridgeError as e:
            kind = "quota" if e.status == 429 else "busy" if e.status == 409 else "invalid"
            return _fail(e.status, kind, e.detail)
        callers.add_task(task_id, c["id"], r["thread"], r["message_id"], r["job_id"], time.time() + body.timeout)
        return CallerTaskOut(task_id=task_id, status="queued", agent_online=r.get("agent_online"))

    def own(task_id: str, c: dict[str, Any]) -> dict[str, Any]:
        t = callers.task(task_id, c["id"])
        if not t:
            raise HTTPException(status_code=404, detail="没有这个任务")
        return t

    @router.get("/tasks/{task_id}")
    def poll(task_id: str, wait: int = Query(0, ge=0), c: dict[str, Any] = Depends(caller)):
        t = own(task_id, c)
        owner = (store.get_thread(t["thread"]) or {}).get("owner") or ""
        end = time.time() + min(wait, cfg.max_wait)
        while True:
            service.requeue_stale()
            out = state(callers.task(task_id, c["id"]) or t, owner)
            if out.status not in ("queued", "running") or time.time() >= end:
                return out
            time.sleep(cfg.poll_interval)

    @router.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str, c: dict[str, Any] = Depends(caller)):
        t = own(task_id, c)
        owner = (store.get_thread(t["thread"]) or {}).get("owner") or ""
        try:
            return service.cancel(t["message_id"], owner)
        except BridgeError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from e

    @router.get("/models")
    def models():
        return {"models": service.model_choices(), "info": service.models_info(), "agent": service.agent_status()}

    @router.get("/status")
    def status():
        return service.status()

    return router
