"""SQLite persistence for threads / messages / events / jobs.

Works on its own connection (``BridgeStore(path)``) or on a connection the host already holds
(``BridgeStore(conn=..., lock=...)``). In the shared case nothing about the connection is changed:
no PRAGMAs, no ``row_factory`` — every query sets ``sqlite3.Row`` on its own cursor.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MESSAGE_STATUSES = ("pending", "streaming", "done", "error", "cancelled")
INFLIGHT = ("pending", "streaming")
JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_threads (
  id         TEXT PRIMARY KEY,
  scope      TEXT NOT NULL DEFAULT '',
  key        TEXT NOT NULL DEFAULT '',
  title      TEXT NOT NULL DEFAULT '',
  pinned     INTEGER NOT NULL DEFAULT 0,
  session_id TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bridge_threads_scope ON bridge_threads(scope, key);

CREATE TABLE IF NOT EXISTS bridge_messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  thread     TEXT NOT NULL,
  role       TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
  content    TEXT NOT NULL DEFAULT '',
  status     TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('pending','streaming','done','error','cancelled')),
  rev        INTEGER NOT NULL DEFAULT 0,
  job_id     INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bridge_messages_thread ON bridge_messages(thread, id);
CREATE INDEX IF NOT EXISTS idx_bridge_messages_job ON bridge_messages(job_id);

CREATE TABLE IF NOT EXISTS bridge_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER NOT NULL,
  seq        INTEGER NOT NULL,
  type       TEXT NOT NULL,
  data       TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bridge_events_msg ON bridge_events(message_id, seq);

CREATE TABLE IF NOT EXISTS bridge_jobs (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  kind             TEXT NOT NULL,
  payload          TEXT NOT NULL DEFAULT '{}',
  status           TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','cancelled')),
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  result           TEXT,
  error            TEXT,
  worker           TEXT,
  created_at       TEXT NOT NULL DEFAULT (datetime('now')),
  started_at       TEXT,
  heartbeat_at     TEXT,
  finished_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_bridge_jobs_status ON bridge_jobs(status, id);

CREATE TABLE IF NOT EXISTS bridge_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# (table, column, DDL) — applied when the column is missing; same idiom as ashare's Store.
MIGRATIONS: list[tuple[str, str, str]] = []


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def auto_title(text: str, limit: int = 30) -> str:
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    line = line.strip("，。：:；;,.!?！？ ")
    return line[:limit] + ("…" if len(line) > limit else "")


def _job(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    try:
        row["payload"] = json.loads(row.get("payload") or "{}")
    except json.JSONDecodeError:
        row["payload"] = {}
    row["cancel_requested"] = bool(row.get("cancel_requested"))
    return row


def _event(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    try:
        row["data"] = json.loads(row.get("data") or "{}")
    except json.JSONDecodeError:
        row["data"] = {}
    return row


class BridgeStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        conn: sqlite3.Connection | None = None,
        lock: threading.RLock | None = None,
    ) -> None:
        if (path is None) == (conn is None):
            raise ValueError("pass exactly one of path / conn")
        self._owns_conn = conn is None
        if conn is None:
            p = str(path)
            if p != ":memory:":
                Path(p).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA busy_timeout=5000")
            if p != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
        self.conn = conn
        self.lock = lock or threading.RLock()
        self._job_cond = threading.Condition()
        with self.lock:
            self.conn.executescript(SCHEMA)
            for table, col, ddl in MIGRATIONS:
                cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]
                if col not in cols:
                    self.conn.execute(ddl)

    def close(self) -> None:
        if self._owns_conn:
            with self.lock:
                self.conn.close()

    # ---------------- plumbing ----------------

    def _q(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            cur = self.conn.cursor()
            cur.row_factory = sqlite3.Row
            return [dict(r) for r in cur.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._q(sql, params)
        return rows[0] if rows else None

    def _x(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.cursor()
            cur.row_factory = sqlite3.Row
            return cur.execute(sql, params)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group statements atomically; safe to nest inside a host transaction."""
        with self.lock:
            if self.conn.in_transaction:
                yield
                return
            self.conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    # ---------------- meta ----------------

    def get_meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM bridge_meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self._x("DELETE FROM bridge_meta WHERE key=?", (key,))
            return
        self._x(
            "INSERT INTO bridge_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---------------- threads ----------------

    def create_thread(self, thread_id: str, *, scope: str = "", key: str = "", title: str = "") -> dict[str, Any]:
        now = _now()
        self._x(
            "INSERT INTO bridge_threads(id, scope, key, title, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (thread_id, scope, key, title, now, now),
        )
        return self.get_thread(thread_id) or {}

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM bridge_threads WHERE id=?", (thread_id,))
        return self._thread(row) if row else None

    def find_thread(self, scope: str, key: str) -> str | None:
        row = self._one(
            "SELECT id FROM bridge_threads WHERE scope=? AND key=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (scope, key),
        )
        return row["id"] if row else None

    def threads(self, scope: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE t.scope=?", (scope,)) if scope is not None else ("", ())
        rows = self._q(
            f"""
            SELECT t.*,
              (SELECT COUNT(*) FROM bridge_messages m WHERE m.thread=t.id) AS n,
              (SELECT substr(m.content, 1, 80) FROM bridge_messages m
                 WHERE m.thread=t.id AND m.role IN ('user','assistant') ORDER BY m.id DESC LIMIT 1) AS preview,
              (SELECT m.content FROM bridge_messages m WHERE m.thread=t.id AND m.role='user' ORDER BY m.id LIMIT 1) AS first_user
            FROM bridge_threads t {where}
            ORDER BY t.pinned DESC, t.updated_at DESC, t.created_at DESC
            """,
            params,
        )
        out = []
        for r in rows:
            first_user = r.pop("first_user", None)
            t = self._thread(r)
            if not t["title"] and first_user:
                t["title"] = auto_title(first_user)
            t["preview"] = t.get("preview") or ""
            out.append(t)
        return out

    def update_thread(
        self,
        thread_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        session_id: str | None | type[Ellipsis] = Ellipsis,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if title is not None:
            sets.append("title=?")
            params.append(title)
        if pinned is not None:
            sets.append("pinned=?")
            params.append(1 if pinned else 0)
        if session_id is not Ellipsis:
            sets.append("session_id=?")
            params.append(session_id)
        if not sets:
            return
        params.append(thread_id)
        self._x(f"UPDATE bridge_threads SET {', '.join(sets)} WHERE id=?", tuple(params))

    def touch_thread(self, thread_id: str) -> None:
        self._x("UPDATE bridge_threads SET updated_at=? WHERE id=?", (_now(), thread_id))

    def delete_thread(self, thread_id: str) -> int:
        with self.transaction():
            n = self._x("SELECT COUNT(*) AS n FROM bridge_messages WHERE thread=?", (thread_id,)).fetchone()["n"]
            self._x(
                "DELETE FROM bridge_events WHERE message_id IN (SELECT id FROM bridge_messages WHERE thread=?)",
                (thread_id,),
            )
            self._x("DELETE FROM bridge_messages WHERE thread=?", (thread_id,))
            self._x("DELETE FROM bridge_threads WHERE id=?", (thread_id,))
        return int(n)

    @staticmethod
    def _thread(row: dict[str, Any]) -> dict[str, Any]:
        row = dict(row)
        row["pinned"] = bool(row.get("pinned"))
        return row

    # ---------------- messages ----------------

    def add_message(
        self, thread: str, role: str, content: str = "", status: str = "done", job_id: int | None = None
    ) -> dict[str, Any]:
        now = _now()
        cur = self._x(
            "INSERT INTO bridge_messages(thread, role, content, status, job_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?) RETURNING *",
            (thread, role, content, status, job_id, now, now),
        )
        row = dict(cur.fetchone())
        row["events"] = []
        return row

    def get_message(self, message_id: int, *, with_events: bool = True) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM bridge_messages WHERE id=?", (message_id,))
        if not row:
            return None
        if with_events:
            row["events"] = self.events(message_id)
        return row

    def messages(
        self, thread: str, after_id: int = 0, limit: int = 200, *, with_events: bool = True, tail: bool = False
    ) -> list[dict[str, Any]]:
        """`tail=True` returns the newest `limit` rows (still ascending) instead of the oldest."""
        if tail:
            sql = (
                "SELECT * FROM (SELECT * FROM bridge_messages WHERE thread=? AND id>? ORDER BY id DESC LIMIT ?) "
                "ORDER BY id"
            )
        else:
            sql = "SELECT * FROM bridge_messages WHERE thread=? AND id>? ORDER BY id LIMIT ?"
        rows = self._q(sql, (thread, after_id, limit))
        if with_events and rows:
            ids = [r["id"] for r in rows]
            evs = self._q(
                f"SELECT * FROM bridge_events WHERE message_id IN ({','.join('?' * len(ids))}) ORDER BY id",
                tuple(ids),
            )
            by_msg: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
            for e in evs:
                by_msg[e["message_id"]].append(_event(e))
            for r in rows:
                r["events"] = by_msg[r["id"]]
        return rows

    def inflight(self, thread: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM bridge_messages WHERE thread=? AND status IN ('pending','streaming') ORDER BY id DESC LIMIT 1",
            (thread,),
        )

    def append_content(self, message_id: int, text: str) -> int:
        cur = self._x(
            "UPDATE bridge_messages SET content=content||?, rev=rev+1, updated_at=? WHERE id=? RETURNING rev",
            (text, _now(), message_id),
        )
        row = cur.fetchone()
        return int(row["rev"]) if row else 0

    def set_status(self, message_id: int, status: str, *, append: str | None = None) -> int:
        """rev only moves when content changes, so clients can treat it as a pure content cursor."""
        if append:
            sql = "UPDATE bridge_messages SET status=?, content=content||?, rev=rev+1, updated_at=? WHERE id=? RETURNING rev"
            params: tuple = (status, append, _now(), message_id)
        else:
            sql = "UPDATE bridge_messages SET status=?, updated_at=? WHERE id=? RETURNING rev"
            params = (status, _now(), message_id)
        row = self._x(sql, params).fetchone()
        return int(row["rev"]) if row else 0

    def message_by_job(self, job_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM bridge_messages WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,))

    def set_message_job(self, message_id: int, job_id: int) -> None:
        self._x("UPDATE bridge_messages SET job_id=? WHERE id=?", (job_id, message_id))

    def orphan_inflight(self) -> list[dict[str, Any]]:
        """In-flight messages whose job is already terminal (or missing) — nothing will ever finish them."""
        return self._q(
            "SELECT m.* FROM bridge_messages m LEFT JOIN bridge_jobs j ON j.id = m.job_id "
            "WHERE m.status IN ('pending','streaming') AND m.job_id IS NOT NULL "
            "AND (j.id IS NULL OR j.status IN ('done','failed','cancelled'))"
        )

    # ---------------- events ----------------

    def add_event(self, message_id: int, type: str, data: dict[str, Any]) -> dict[str, Any]:
        cur = self._x(
            "INSERT INTO bridge_events(message_id, seq, type, data, created_at) VALUES(?, "
            "(SELECT COALESCE(MAX(seq), 0) + 1 FROM bridge_events WHERE message_id=?), ?, ?, ?) RETURNING *",
            (message_id, message_id, type, json.dumps(data, ensure_ascii=False), _now()),
        )
        return _event(dict(cur.fetchone()))

    def events(self, message_id: int, after_id: int = 0) -> list[dict[str, Any]]:
        return [
            _event(r)
            for r in self._q(
                "SELECT * FROM bridge_events WHERE message_id=? AND id>? ORDER BY id", (message_id, after_id)
            )
        ]

    def last_event_id(self) -> int:
        row = self._one("SELECT COALESCE(MAX(id), 0) AS m FROM bridge_events")
        return int(row["m"]) if row else 0

    # ---------------- jobs ----------------

    def enqueue_job(self, kind: str, payload: dict[str, Any]) -> int:
        cur = self._x(
            "INSERT INTO bridge_jobs(kind, payload, created_at) VALUES(?, ?, ?)",
            (kind, json.dumps(payload, ensure_ascii=False), _now()),
        )
        with self._job_cond:
            self._job_cond.notify_all()
        return int(cur.lastrowid)

    def wait_for_job(self, timeout: float) -> None:
        """Block until enqueue_job is called or timeout passes (missed notifies only cost one timeout)."""
        with self._job_cond:
            self._job_cond.wait(timeout)

    def claim_job(self, worker: str, kinds: list[str] | None = None) -> dict[str, Any] | None:
        where = "status='queued'"
        params: tuple = ()
        if kinds:
            where += f" AND kind IN ({','.join('?' * len(kinds))})"
            params = tuple(kinds)
        now = _now()
        cur = self._x(
            "UPDATE bridge_jobs SET status='running', worker=?, started_at=?, heartbeat_at=? "
            f"WHERE id = (SELECT id FROM bridge_jobs WHERE {where} ORDER BY id LIMIT 1) RETURNING *",
            (worker, now, now, *params),
        )
        row = cur.fetchone()
        return _job(dict(row)) if row else None

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM bridge_jobs WHERE id=?", (job_id,))
        return _job(row) if row else None

    def touch_job(self, job_id: int) -> None:
        self._x("UPDATE bridge_jobs SET heartbeat_at=? WHERE id=? AND status='running'", (_now(), job_id))

    def request_cancel(self, job_id: int) -> str:
        with self.lock:
            if self._x(
                "UPDATE bridge_jobs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
                (_now(), job_id),
            ).rowcount:
                return "cancelled"
            if self._x(
                "UPDATE bridge_jobs SET cancel_requested=1 WHERE id=? AND status='running'", (job_id,)
            ).rowcount:
                return "cancelling"
        return "noop"

    def finish_job(self, job_id: int, status: str, result: str | None = None, error: str | None = None) -> None:
        if status not in ("done", "failed", "cancelled"):
            raise ValueError(f"bad terminal status {status!r}")
        self._x(
            "UPDATE bridge_jobs SET status=?, result=?, error=?, finished_at=? WHERE id=?",
            (status, result, error, _now(), job_id),
        )

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [_job(r) for r in self._q("SELECT * FROM bridge_jobs ORDER BY id DESC LIMIT ?", (limit,))]

    def count_jobs(self, status: str) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM bridge_jobs WHERE status=?", (status,))
        return int(row["n"]) if row else 0

    def stale_running(self, seconds: int) -> list[dict[str, Any]]:
        return [
            _job(r)
            for r in self._q(
                "SELECT * FROM bridge_jobs WHERE status='running' "
                "AND COALESCE(heartbeat_at, started_at) < datetime('now', ?)",
                (f"-{int(seconds)} seconds",),
            )
        ]
