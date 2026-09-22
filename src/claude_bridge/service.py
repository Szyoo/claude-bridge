"""Business logic shared by the routers and by hosts embedding the bridge directly.

Everything here is synchronous; the SSE route calls `snapshot` through `run_in_threadpool`.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from claude_bridge.broker import Broker
from claude_bridge.errors import BadRequest, ChatBusy, NotFound
from claude_bridge.models import AgentChatIn, JobEventsIn, JobFinishIn
from claude_bridge.store import INFLIGHT, BridgeStore, auto_title

DEFAULT_SETTINGS: dict[str, Any] = {"model": "", "effort": "", "max_turns": 40, "auto_context": True}
DEFAULT_EFFORTS = ["", "low", "medium", "high", "xhigh", "max"]


def _default_thread(scope: str) -> str:
    return "main" if not scope else f"main-{scope}"


@dataclass
class BridgeConfig:
    scopes: tuple[str, ...] | None = None  # None = any scope string is accepted
    default_thread: Callable[[str], str] = _default_thread
    default_settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    model_choices: list[dict[str, str]] = field(default_factory=lambda: [{"id": "", "label": "默认"}])
    model_aliases: dict[str, str] = field(default_factory=dict)
    effort_choices: list[str] = field(default_factory=lambda: list(DEFAULT_EFFORTS))
    max_text_len: int = 100_000
    new_thread_notice: str = ""
    online_seconds: int = 90
    stale_seconds: int = 120
    heartbeat_seconds: float = 15.0
    messages_limit: int = 200
    on_thread_deleted: Callable[[str], None] | None = None
    on_chat_started: Callable[[dict[str, Any]], None] | None = None


class BridgeService:
    def __init__(self, store: BridgeStore, broker: Broker, config: BridgeConfig | None = None) -> None:
        self.store = store
        self.broker = broker
        self.config = config or BridgeConfig()

    # ---------------- publish ----------------

    def _pub(self, thread: str, type: str, data: dict[str, Any], id: int | None = None) -> None:
        self.broker.publish(thread, {"type": type, "data": data, "id": id})

    def _pub_status(self, msg_id: int, thread: str, status: str, rev: int) -> None:
        self._pub(thread, "status", {"message_id": msg_id, "status": status, "rev": rev})

    def _pub_done(self, msg_id: int, thread: str, status: str, job: dict[str, Any] | None) -> None:
        j = None
        if job:
            j = {k: job.get(k) for k in ("id", "status", "result", "error")}
        self._pub(thread, "done", {"message_id": msg_id, "status": status, "job": j})

    # ---------------- threads ----------------

    def _check_scope(self, scope: str) -> None:
        if self.config.scopes is not None and scope not in self.config.scopes:
            raise BadRequest(f"未知 scope {scope!r}")

    def current_thread(self, scope: str = "") -> str:
        self._check_scope(scope)
        key = f"current_thread:{scope}"
        cur = self.store.get_meta(key)
        if cur and self.store.get_thread(cur):
            return cur
        tid = self.config.default_thread(scope)
        if not self.store.get_thread(tid):
            self.store.create_thread(tid, scope=scope)
        self.store.set_meta(key, tid)
        return tid

    def new_thread(self, scope: str = "", key: str = "", title: str = "", *, select: bool = True) -> str:
        self._check_scope(scope)
        tid = uuid.uuid4().hex[:12]
        self.store.create_thread(tid, scope=scope, key=key, title=title)
        if self.config.new_thread_notice:
            self.store.add_message(tid, "system", self.config.new_thread_notice)
        if select:
            self.store.set_meta(f"current_thread:{scope}", tid)
        return tid

    def find_thread(self, scope: str, key: str) -> str | None:
        return self.store.find_thread(scope, key)

    def select_thread(self, scope: str, thread_id: str) -> None:
        self._check_scope(scope)
        if not self.store.get_thread(thread_id):
            raise NotFound("没有这个对话")
        self.store.set_meta(f"current_thread:{scope}", thread_id)

    def patch_thread(self, thread_id: str, *, title: str | None = None, pinned: bool | None = None) -> None:
        th = self.store.get_thread(thread_id)
        if not th:
            raise NotFound("没有这个对话")
        self.store.update_thread(thread_id, title=title, pinned=pinned)
        th = self.store.get_thread(thread_id) or th
        self._pub(thread_id, "thread", {"id": thread_id, "title": th["title"], "pinned": th["pinned"]})

    def delete_thread(self, thread_id: str, scope: str | None = None) -> dict[str, Any]:
        th = self.store.get_thread(thread_id)
        if not th:
            raise NotFound("没有这个对话")
        sc = th["scope"] if scope is None else scope
        n = self.store.delete_thread(thread_id)
        key = f"current_thread:{sc}"
        current = self.store.get_meta(key)
        if current == thread_id:
            rest = self.store.threads(sc)
            current = rest[0]["id"] if rest else None
            self.store.set_meta(key, current)
        self.store.set_meta(f"context:{thread_id}", None)
        if self.config.on_thread_deleted:
            self.config.on_thread_deleted(thread_id)
        return {"deleted": n, "current": current}

    # ---------------- session context (/context, /compact) ----------------

    def thread_context(self, thread_id: str) -> dict[str, Any] | None:
        raw = self.store.get_meta(f"context:{thread_id}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _save_context(self, thread_id: str, report: dict[str, Any]) -> None:
        data = {**report, "at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}
        self.store.set_meta(f"context:{thread_id}", json.dumps(data, ensure_ascii=False))
        self._pub(thread_id, "context", {"thread": thread_id, "context": data})

    def request_session_job(self, thread_id: str, kind: str) -> dict[str, Any]:
        """Queue a `/context` refresh or a `/compact` on the thread's Claude session; the worker does the rest."""
        th = self.store.get_thread(thread_id)
        if not th:
            raise NotFound("没有这个对话")
        if not th.get("session_id"):
            raise BadRequest("这个对话还没有 Claude 会话，先发一条消息")
        if kind == "compact" and self.store.inflight(thread_id):
            raise ChatBusy("回答中不能压缩")
        payload = {"thread": thread_id, "session_id": th["session_id"], "settings": self.settings()}
        jid = self.store.enqueue_job(kind, payload)
        return {"job_id": jid, "thread": thread_id, "agent_online": self.agent_online()}

    # ---------------- chat ----------------

    def start_chat(
        self,
        text: str,
        *,
        thread_id: str | None = None,
        scope: str = "",
        key: str = "",
        new_thread: bool = False,
        extra_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise BadRequest("消息不能为空")
        if len(text) > self.config.max_text_len:
            raise BadRequest(f"消息 {len(text)} 字，超过 {self.config.max_text_len} 字上限")
        if thread_id:
            if not self.store.get_thread(thread_id):
                raise NotFound("没有这个对话")
            tid = thread_id
        elif key:
            self._check_scope(scope)
            tid = self.store.find_thread(scope, key) or self.new_thread(scope, key, select=False)
        elif new_thread:
            tid = self.new_thread(scope)
        else:
            tid = self.current_thread(scope)

        self.requeue_stale()
        if self.store.inflight(tid):
            raise ChatBusy("上一条还在回答中")

        titled = False
        with self.store.transaction():
            th = self.store.get_thread(tid) or {}
            if not th.get("title"):
                self.store.update_thread(tid, title=auto_title(text))
                titled = True
            user_msg = self.store.add_message(tid, "user", text)
            asst_msg = self.store.add_message(tid, "assistant", "", status="pending")
            payload: dict[str, Any] = {
                "thread": tid,
                "message_id": asst_msg["id"],
                "text": text,
                "settings": self.settings(),
                "session_id": th.get("session_id"),
                **(extra_payload or {}),
            }
            jid = self.store.enqueue_job("chat", payload)
            self.store.set_message_job(asst_msg["id"], jid)
            asst_msg["job_id"] = jid
            self.store.touch_thread(tid)

        self._pub(tid, "message", user_msg)
        self._pub(tid, "message", asst_msg)
        if titled:
            self._pub(tid, "thread", {"id": tid, "title": auto_title(text), "pinned": bool(th.get("pinned"))})
        if self.config.on_chat_started:
            self.config.on_chat_started(payload)
        return {"message_id": asst_msg["id"], "job_id": jid, "thread": tid, "agent_online": self.agent_online()}

    def cancel(self, message_id: int) -> dict[str, Any]:
        msg = self.store.get_message(message_id, with_events=False)
        if not msg:
            raise NotFound("没有这条消息")
        if msg["status"] not in INFLIGHT:
            return {"status": "noop"}
        thread = msg["thread"]
        job = self.store.get_job(msg["job_id"]) if msg.get("job_id") else None
        outcome = self.store.request_cancel(job["id"]) if job else "cancelled"
        if outcome == "cancelled":
            job = self.store.get_job(job["id"]) if job else None
            rev = self.store.set_status(message_id, "cancelled")
            self._pub_status(message_id, thread, "cancelled", rev)
            self._pub_done(message_id, thread, "cancelled", job)
            return {"status": "cancelled"}
        if outcome == "cancelling":
            ev = self.store.add_event(message_id, "status", {"phase": "cancel_requested"})
            self._pub(thread, "event", ev, id=ev["id"])
            return {"status": "cancelling"}
        # job already terminal but the message never got finished: repair it
        rev = self.store.set_status(message_id, "error", append="\n\n> ❌ 任务已结束但回答未收尾")
        self._pub_status(message_id, thread, "error", rev)
        self._pub_done(message_id, thread, "error", job)
        return {"status": "noop"}

    def snapshot(self, thread_id: str, after: int = 0, last_event_id: int = 0) -> dict[str, Any]:
        th = self.store.get_thread(thread_id)
        if not th:
            raise NotFound("没有这个对话")
        th["context"] = self.thread_context(thread_id)
        cursor = self.store.last_event_id()
        msgs = self.store.messages(thread_id, after_id=after, limit=self.config.messages_limit, tail=after == 0)
        inflight = self.store.inflight(thread_id)
        if inflight and last_event_id:
            for m in msgs:
                if m["id"] == inflight["id"]:
                    m["events"] = [e for e in m["events"] if e["id"] > last_event_id]
        return {
            "thread": th,
            "messages": msgs,
            "inflight": inflight["id"] if inflight else None,
            "agent": self.agent_status(),
            "cursor": cursor,
        }

    # ---------------- settings / status ----------------

    def settings(self) -> dict[str, Any]:
        try:
            saved = json.loads(self.store.get_meta("settings") or "{}")
        except json.JSONDecodeError:
            saved = {}
        defaults = self.config.default_settings
        cur = {**defaults, **{k: v for k, v in saved.items() if k in defaults}}
        model = cur.get("model") or ""
        cur["model"] = self.config.model_aliases.get(model, model)
        return cur

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        cur = self.settings()
        if "model" in patch and patch["model"] is not None:
            cur["model"] = str(patch["model"]).strip()
        if "effort" in patch and patch["effort"] is not None:
            eff = str(patch["effort"]).strip().lower()
            if eff and eff not in self.config.effort_choices:
                raise BadRequest(f"effort 只能是 {[e for e in self.config.effort_choices if e]}")
            cur["effort"] = eff
        if "max_turns" in patch and patch["max_turns"] is not None:
            n = int(patch["max_turns"])
            if not 1 <= n <= 100:
                raise BadRequest("max_turns 需在 1..100")
            cur["max_turns"] = n
        if "auto_context" in patch and patch["auto_context"] is not None:
            cur["auto_context"] = bool(patch["auto_context"])
        self.store.set_meta("settings", json.dumps(cur, ensure_ascii=False))
        return self.settings()

    def agent_online(self) -> bool:
        raw = self.store.get_meta("agent_heartbeat")
        try:
            return bool(raw) and time.time() - float(raw) < self.config.online_seconds
        except ValueError:
            return False

    def agent_status(self) -> dict[str, Any]:
        return {
            "online": self.agent_online(),
            "worker": self.store.get_meta("agent_worker"),
            "heartbeat": self.store.get_meta("agent_heartbeat"),
        }

    def status(self) -> dict[str, Any]:
        self.requeue_stale()
        return {
            **self.agent_status(),
            "queued": self.store.count_jobs("queued"),
            "running": self.store.count_jobs("running"),
        }

    # ---------------- agent side ----------------

    def next_job(self, worker: str, kinds: list[str], wait: int) -> dict[str, Any] | None:
        deadline = time.time() + max(0, wait)
        self.requeue_stale()
        while True:
            self.store.set_meta("agent_heartbeat", repr(time.time()))
            self.store.set_meta("agent_worker", worker)
            job = self.store.claim_job(worker, kinds)
            remaining = deadline - time.time()
            if job or remaining <= 0:
                return job
            self.store.wait_for_job(min(1.0, remaining))

    def apply_events(self, job_id: int, body: JobEventsIn) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise NotFound("没有这个任务")
        self.store.touch_job(job_id)
        stop = job["status"] != "running" or job["cancel_requested"]
        msg = self.store.message_by_job(job_id)
        if not msg:
            return {"ok": True, "cancel": stop}
        mid, thread = msg["id"], msg["thread"]

        deltas = list(body.deltas or [])
        events = [(e.type, e.data) for e in (body.events or [])]
        if body.append:
            deltas.append(body.append)
        if body.trace_append:
            events.append(("status", {"phase": "legacy_trace", "text": body.trace_append}))

        frames: list[tuple[str, dict[str, Any], int | None]] = []
        with self.store.transaction():
            if body.status in INFLIGHT and msg["status"] in INFLIGHT and body.status != msg["status"]:
                rev = self.store.set_status(mid, body.status)
                frames.append(("status", {"message_id": mid, "status": body.status, "rev": rev}, None))
            if body.content is not None:  # legacy full replace — nobody streams this way any more
                self.store._x(
                    "UPDATE bridge_messages SET content=?, rev=rev+1 WHERE id=?", (body.content, mid)
                )
                m2 = self.store.get_message(mid, with_events=False) or msg
                frames.append(("message", m2, None))
            elif deltas:
                text = "".join(deltas)
                rev = self.store.append_content(mid, text)
                frames.append(("delta", {"message_id": mid, "text": text, "rev": rev}, None))
            for etype, data in events:
                ev = self.store.add_event(mid, etype, data)
                frames.append(("event", ev, ev["id"]))
        for ftype, data, fid in frames:
            self._pub(thread, ftype, data, id=fid)
        return {"ok": True, "cancel": stop}

    def finish(self, job_id: int, body: JobFinishIn) -> None:
        job = self.store.get_job(job_id)
        if not job:
            raise NotFound("没有这个任务")
        status = "cancelled" if body.cancelled else ("done" if body.ok else "failed")
        frames: list[tuple[str, dict[str, Any], int | None]] = []
        msg_status = None
        with self.store.transaction():
            self.store.finish_job(job_id, status, body.result, body.error)
            job = self.store.get_job(job_id) or job
            msg = self.store.message_by_job(job_id)
            thread = job["payload"].get("thread") or (msg["thread"] if msg else None)
            if msg and msg["status"] in INFLIGHT:
                mid = msg["id"]
                if status == "cancelled":
                    msg_status = "cancelled"
                    rev = self.store.set_status(mid, "cancelled")
                elif status == "done":
                    msg_status = "done"
                    rev = self.store.set_status(mid, "done")
                else:
                    msg_status = "error"
                    err = body.error or "执行失败"
                    already = err.split("：")[-1][:60] in (msg.get("content") or "")
                    rev = self.store.set_status(mid, "error", append=None if already else f"\n\n> ❌ {err}")
                    ev = self.store.add_event(mid, "error", {"message": err, "kind": body.error_kind or "worker"})
                    frames.append(("event", ev, ev["id"]))
                frames.insert(0, ("status", {"message_id": mid, "status": msg_status, "rev": rev}, None))
            if thread:
                if body.reset_session:
                    self.store.update_thread(thread, session_id=None)
                elif body.session_id:
                    self.store.update_thread(thread, session_id=body.session_id)
                self.store.touch_thread(thread)
        if thread:
            for ftype, data, fid in frames:
                self._pub(thread, ftype, data, id=fid)
            if msg and msg_status:
                self._pub_done(msg["id"], thread, msg_status, job)
            if body.context and isinstance(body.context, dict) and body.context.get("used") is not None:
                self._save_context(thread, body.context)
            if job["kind"] in ("context", "compact"):
                self._pub(thread, "job", {k: job.get(k) for k in ("id", "kind", "status", "result", "error")})

    def requeue_stale(self) -> int:
        n = 0
        for job in self.store.stale_running(self.config.stale_seconds):
            self.store.finish_job(job["id"], "failed", None, "helper 心跳超时")
            n += 1
        for msg in self.store.orphan_inflight():
            job = self.store.get_job(msg["job_id"])
            reason = (job or {}).get("error") or "任务已结束但回答未收尾"
            rev = self.store.set_status(msg["id"], "error", append=f"\n\n> ❌ {reason}")
            ev = self.store.add_event(msg["id"], "error", {"message": reason, "kind": "stale"})
            self._pub_status(msg["id"], msg["thread"], "error", rev)
            self._pub(msg["thread"], "event", ev, id=ev["id"])
            self._pub_done(msg["id"], msg["thread"], "error", job)
            n += 1
        return n

    def create_chat_from_agent(self, body: AgentChatIn) -> dict[str, Any]:
        return self.start_chat(
            body.text, scope=body.scope, key=body.key, new_thread=body.new_thread, extra_payload=body.payload
        )
