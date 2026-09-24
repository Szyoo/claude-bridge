"""Business logic shared by the routers and by hosts embedding the bridge directly.

Everything here is synchronous; the SSE route calls `snapshot` through `run_in_threadpool`.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_bridge._version import REPO, __version__
from claude_bridge.broker import Broker
from claude_bridge.errors import BadRequest, ChatBusy, NotFound
from claude_bridge.models import AgentChatIn, JobEventsIn, JobFinishIn
from claude_bridge.principal import ANONYMOUS, Principal
from claude_bridge.store import INFLIGHT, BridgeStore, auto_title, public_file, utc_text

log = logging.getLogger(__name__)

DEFAULT_SETTINGS: dict[str, Any] = {"model": "", "effort": "", "max_turns": 40, "auto_context": True}
DEFAULT_EFFORTS = ["", "low", "medium", "high", "xhigh", "max"]
SESSION_JOB_KINDS = ("context", "compact")  # jobs that act on a thread's Claude session; the page shows their progress
# the subscription's rolling windows as the CLI reports them (rate_limit_event / `/usage`)
LIMIT_WINDOWS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}


def job_frame(job: dict[str, Any]) -> dict[str, Any]:
    return {k: job.get(k) for k in ("id", "kind", "status", "result", "error", "created_at", "started_at", "finished_at")}


def _fmt_tokens(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    # same shape as the page's fmtTokens: 744k / 31.5k / 1.2M
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.0f}k" if n >= 100_000 else f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _default_thread(scope: str) -> str:
    return "main" if not scope else f"main-{scope}"


def latest_label(name: str) -> str:
    """Label for an alias that follows the CLI's newest model: "Opus 5.5 (1M context)" → "最新 Opus（5.5 · 1M）"."""
    m = re.match(r"^(\S+)\s*(.*?)\s*(?:\((\S+) context\))?$", (name or "").strip())
    if not m:
        return f"最新 {name}"
    family, version, ctx = m.groups()
    detail = " · ".join(x for x in (version, ctx) if x)
    return f"最新 {family}（{detail}）" if detail else f"最新 {family}"


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """(mime, extension) from the file header; the client's Content-Type is not trusted. Raster only — no SVG."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


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
    # image uploads: None disables them. 7 MB raw ≈ the API's 10 MB-per-image limit once base64-encoded.
    files_dir: Path | str | None = None
    max_file_bytes: int = 7 * 1024 * 1024
    max_files_per_message: int = 10
    orphan_seconds: int = 86400  # uploads never sent are removed after this long
    # called before a chat / compact is queued; raise QuotaExceeded (or any BridgeError) to refuse it
    check_quota: Callable[[Principal], None] | None = None


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

    @staticmethod
    def meta_key(owner: str, key: str) -> str:
        """Per-owner meta keys (current thread, settings); the shared namespace keeps the historical names."""
        return f"u{owner}:{key}" if owner else key

    def own_thread(self, thread_id: str, owner: str = "") -> dict[str, Any]:
        """The thread if it belongs to `owner`; someone else's thread is indistinguishable from a missing one."""
        th = self.store.get_thread(thread_id)
        if not th or (th.get("owner") or "") != owner:
            raise NotFound("没有这个对话")
        return th

    def current_thread(self, scope: str = "", owner: str = "") -> str:
        self._check_scope(scope)
        key = self.meta_key(owner, f"current_thread:{scope}")
        cur = self.store.get_meta(key)
        if cur:
            th = self.store.get_thread(cur)
            if th and (th.get("owner") or "") == owner:
                return cur
        tid = self.config.default_thread(scope) + (f"-u{owner}" if owner else "")
        if not self.store.get_thread(tid):
            self.store.create_thread(tid, scope=scope, owner=owner)
        self.store.set_meta(key, tid)
        return tid

    def new_thread(self, scope: str = "", key: str = "", title: str = "", *, select: bool = True, owner: str = "") -> str:
        self._check_scope(scope)
        tid = uuid.uuid4().hex[:12]
        self.store.create_thread(tid, scope=scope, key=key, title=title, owner=owner)
        if self.config.new_thread_notice:
            self.store.add_message(tid, "system", self.config.new_thread_notice)
        if select:
            self.store.set_meta(self.meta_key(owner, f"current_thread:{scope}"), tid)
        return tid

    def find_thread(self, scope: str, key: str, owner: str = "") -> str | None:
        return self.store.find_thread(scope, key, owner)

    def select_thread(self, scope: str, thread_id: str, owner: str = "") -> None:
        self._check_scope(scope)
        self.own_thread(thread_id, owner)
        self.store.set_meta(self.meta_key(owner, f"current_thread:{scope}"), thread_id)

    def patch_thread(
        self, thread_id: str, *, title: str | None = None, pinned: bool | None = None, owner: str = ""
    ) -> None:
        th = self.own_thread(thread_id, owner)
        self.store.update_thread(thread_id, title=title, pinned=pinned)
        th = self.store.get_thread(thread_id) or th
        self._pub(thread_id, "thread", {"id": thread_id, "title": th["title"], "pinned": th["pinned"]})

    def delete_thread(self, thread_id: str, scope: str | None = None, owner: str = "") -> dict[str, Any]:
        th = self.own_thread(thread_id, owner)
        sc = th["scope"] if scope is None else scope
        n = self.store.delete_thread(thread_id)
        self._unlink(self.store.delete_thread_files(thread_id))
        key = self.meta_key(owner, f"current_thread:{sc}")
        current = self.store.get_meta(key)
        if current == thread_id:
            rest = self.store.threads(sc, owner)
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

    def request_session_job(self, thread_id: str, kind: str, principal: Principal = ANONYMOUS) -> dict[str, Any]:
        """Queue a `/context` refresh or a `/compact` on the thread's Claude session; the worker does the rest."""
        th = self.own_thread(thread_id, principal.owner)
        if not th.get("session_id"):
            raise BadRequest("这个对话还没有 Claude 会话，先发一条消息")
        if kind == "compact" and self.store.inflight(thread_id):
            raise ChatBusy("回答中不能压缩")
        existing = next((j for j in self.store.active_jobs(thread_id, (kind,))), None)
        if existing:  # a second click while one is queued / running just reports the same job
            return {"job_id": existing["id"], "thread": thread_id, "agent_online": self.agent_online(), "job": job_frame(existing)}
        if kind == "compact" and self.config.check_quota:  # /context is local and free; /compact asks the model
            self.config.check_quota(principal)
        payload = {"thread": thread_id, "session_id": th["session_id"], "settings": self.settings(principal.owner),
                   "scope": th.get("scope") or "", "owner": principal.owner}
        jid = self.store.enqueue_job(kind, payload)
        job = self.store.get_job(jid) or {"id": jid, "kind": kind, "status": "queued"}
        self._pub(thread_id, "job", job_frame(job))
        return {"job_id": jid, "thread": thread_id, "agent_online": self.agent_online(), "job": job_frame(job)}

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
        files: list[str] | None = None,
        principal: Principal = ANONYMOUS,
    ) -> dict[str, Any]:
        owner = principal.owner
        text = (text or "").strip()
        file_ids = list(dict.fromkeys(files or []))
        if not text and not file_ids:
            raise BadRequest("消息不能为空")
        if len(text) > self.config.max_text_len:
            raise BadRequest(f"消息 {len(text)} 字，超过 {self.config.max_text_len} 字上限")
        self._check_files(file_ids, owner)
        if thread_id:
            self.own_thread(thread_id, owner)
        if self.config.check_quota:
            self.config.check_quota(principal)
        if thread_id:
            tid = thread_id
        elif key:
            self._check_scope(scope)
            tid = self.store.find_thread(scope, key, owner) or self.new_thread(scope, key, select=False, owner=owner)
        elif new_thread:
            tid = self.new_thread(scope, owner=owner)
        else:
            tid = self.current_thread(scope, owner)

        self.requeue_stale()
        if self.store.inflight(tid):
            raise ChatBusy("上一条还在回答中")

        titled = False
        with self.store.transaction():
            th = self.store.get_thread(tid) or {}
            title = auto_title(text) or "图片"
            if not th.get("title"):
                self.store.update_thread(tid, title=title)
                titled = True
            user_msg = self.store.add_message(tid, "user", text)
            attached = self.store.attach_files(user_msg["id"], tid, file_ids) if file_ids else []
            if len(attached) != len(file_ids):  # raced with another send of the same upload
                raise BadRequest("图片已经发送过，请重新添加")
            user_msg["files"] = [public_file(f) for f in attached]
            asst_msg = self.store.add_message(tid, "assistant", "", status="pending")
            payload: dict[str, Any] = {
                "thread": tid,
                "message_id": asst_msg["id"],
                "text": text,
                "settings": self.settings(owner),
                "session_id": th.get("session_id"),
                "scope": th.get("scope") or "",
                "owner": owner,
                **({"files": user_msg["files"]} if attached else {}),
                **(extra_payload or {}),
            }
            jid = self.store.enqueue_job("chat", payload)
            self.store.set_message_job(asst_msg["id"], jid)
            asst_msg["job_id"] = jid
            self.store.touch_thread(tid)

        self._pub(tid, "message", user_msg)
        self._pub(tid, "message", asst_msg)
        if titled:
            self._pub(tid, "thread", {"id": tid, "title": title, "pinned": bool(th.get("pinned"))})
        if self.config.on_chat_started:
            self.config.on_chat_started(payload)
        return {"message_id": asst_msg["id"], "job_id": jid, "thread": tid, "agent_online": self.agent_online()}

    # ---------------- files ----------------

    @property
    def uploads_enabled(self) -> bool:
        return self.config.files_dir is not None

    def _files_dir(self) -> Path:
        if self.config.files_dir is None:
            raise NotFound("未开启图片上传")
        return Path(self.config.files_dir)

    def _unlink(self, names: list[str]) -> None:
        if not names or self.config.files_dir is None:
            return
        d = Path(self.config.files_dir)
        for name in names:
            try:
                (d / name).unlink(missing_ok=True)
            except OSError:
                log.warning("could not delete %s", d / name)

    def save_upload(self, data: bytes, name: str = "", owner: str = "") -> dict[str, Any]:
        d = self._files_dir()
        if not data:
            raise BadRequest("空文件")
        if len(data) > self.config.max_file_bytes:
            raise BadRequest(f"图片 {len(data) / 1048576:.1f} MB，超过 {self.config.max_file_bytes / 1048576:.0f} MB 上限", status=413)
        kind = sniff_image(data)
        if not kind:
            raise BadRequest("只支持 PNG / JPEG / GIF / WebP 图片", status=415)
        mime, ext = kind
        self._unlink(self.store.purge_orphans(self.config.orphan_seconds))
        fid = secrets.token_urlsafe(12)
        fname = f"{fid}.{ext}"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{fname}.part"
        tmp.write_bytes(data)
        tmp.replace(d / fname)
        row = self.store.add_file(fid, name=(name or "")[:80], mime=mime, size=len(data), fname=fname, owner=owner)
        return public_file(row)

    def file_for_download(self, file_id: str, owner: str | None = None) -> tuple[Path, str]:
        """`owner=None` (the worker) may fetch any upload; a browser only its own."""
        d = self._files_dir()
        row = self.store.get_file(file_id)
        if row and owner is not None and (row.get("owner") or "") != owner:
            row = None
        path = d / row["fname"] if row else None
        if not row or not path.is_file():
            raise NotFound("图片不存在")
        return path, row["mime"]

    def _check_files(self, ids: list[str], owner: str = "") -> None:
        if not ids:
            return
        self._files_dir()
        if len(ids) > self.config.max_files_per_message:
            raise BadRequest(f"一条消息最多 {self.config.max_files_per_message} 张图")
        for fid in ids:
            row = self.store.get_file(fid)
            if not row or (row.get("owner") or "") != owner:
                raise BadRequest("图片不存在或已过期，请重新添加")
            if row["message_id"] is not None:
                raise BadRequest("图片已经发送过，请重新添加")

    def cancel(self, message_id: int, owner: str = "") -> dict[str, Any]:
        msg = self.store.get_message(message_id, with_events=False)
        th = self.store.get_thread(msg["thread"]) if msg else None
        if not msg or not th or (th.get("owner") or "") != owner:
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
            "jobs": [job_frame(j) for j in self.store.active_jobs(thread_id, SESSION_JOB_KINDS)],
        }

    # ---------------- settings / status ----------------

    def settings(self, owner: str = "") -> dict[str, Any]:
        try:
            saved = json.loads(self.store.get_meta(self.meta_key(owner, "settings")) or "{}")
        except json.JSONDecodeError:
            saved = {}
        defaults = self.config.default_settings
        cur = {**defaults, **{k: v for k, v in saved.items() if k in defaults}}
        model = cur.get("model") or ""
        cur["model"] = self.config.model_aliases.get(model, model)
        return cur

    def save_settings(self, patch: dict[str, Any], owner: str = "") -> dict[str, Any]:
        cur = self.settings(owner)
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
        self.store.set_meta(self.meta_key(owner, "settings"), json.dumps(cur, ensure_ascii=False))
        return self.settings(owner)

    # ---------------- models (reported by the worker's CLI) ----------------

    def model_candidates(self) -> list[str]:
        """Pinned model ids the worker should validate against its local CLI."""
        return [m["id"] for m in self.config.model_choices if m.get("id")]

    def save_agent_models(self, report: dict[str, Any]) -> dict[str, Any]:
        def entries(key: str) -> list[dict[str, str]]:
            return [{"id": str(e["id"]), "name": str(e.get("name") or e["id"])} for e in report.get(key) or [] if e.get("id")]

        data = {"cli_version": report.get("cli_version") or "", "bridge_version": report.get("bridge_version") or "",
                "default_name": report.get("default_name") or "",
                "aliases": entries("aliases"), "pinned": entries("pinned"),
                "probed_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}
        self.store.set_meta("agent_models", json.dumps(data, ensure_ascii=False))
        return data

    def agent_models(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.store.get_meta("agent_models") or "null")
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) and (data.get("aliases") or data.get("pinned")) else None

    def model_choices(self) -> list[dict[str, str]]:
        """The worker's report when there is one (aliases that follow the CLI's newest + pinned ids it recognised),
        else the host's static list."""
        rep = self.agent_models()
        if not rep:
            return [dict(m) for m in self.config.model_choices]
        default = rep.get("default_name")
        out = [{"id": "", "label": f"默认（helper：{default}）" if default else "默认（helper 配置）"}]
        out += [{"id": a["id"], "label": latest_label(a["name"]), "group": "跟随 CLI 最新"} for a in rep["aliases"]]
        out += [{"id": p["id"], "label": p["name"], "group": "固定版本"} for p in rep["pinned"]]
        return out

    def versions(self) -> dict[str, Any]:
        """claude-bridge on the server, and on the worker as of its last model report (None until it reports)."""
        try:
            rep = json.loads(self.store.get_meta("agent_models") or "null") or {}
        except json.JSONDecodeError:
            rep = {}
        return {"version": __version__, "repo": REPO, "helper_version": rep.get("bridge_version") or None}

    def models_info(self) -> dict[str, Any]:
        rep = self.agent_models()
        if not rep:
            return {"source": "static"}
        return {"source": "helper", "cli_version": rep.get("cli_version"), "probed_at": rep.get("probed_at")}

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
            "bridge": self.versions(),
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
            if job and job["kind"] in SESSION_JOB_KINDS and job["payload"].get("thread"):
                self._pub(job["payload"]["thread"], "job", job_frame(job))  # queued → running
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
        owner = ((self.store.get_thread(thread) or {}).get("owner") or "")

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
                if etype == "usage":
                    self._record_usage(owner, data, thread=thread, message_id=mid, job_id=job_id, kind=job["kind"])
                elif etype == "rate_limit":
                    self.note_rate_limit(data)
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
            if job["kind"] in SESSION_JOB_KINDS:
                if job["kind"] == "compact" and status == "done":
                    self._note_compaction(thread, body.result)
                if job["kind"] == "compact":
                    self._record_compact_usage(thread, job_id, body.result)
                self._pub(thread, "job", job_frame(job))

    def _note_compaction(self, thread: str, result: str | None) -> None:
        """Leave a visible line in the conversation: the history Claude sees is now a summary."""
        try:
            meta = (json.loads(result or "{}") or {}).get("compact") or {}
        except json.JSONDecodeError:
            meta = {}
        size = f"：{_fmt_tokens(meta.get('pre_tokens'))} → {_fmt_tokens(meta.get('post_tokens'))}" if meta.get("pre_tokens") else ""
        msg = self.store.add_message(thread, "system", f"已压缩会话历史{size}。之后 Claude 看到的是摘要，更早的细节可能记不清。")
        self._pub(thread, "message", msg)

    # ---------------- usage ledger / account limits ----------------

    def _record_usage(self, owner: str, data: dict[str, Any], **kw: Any) -> None:
        cost = data.get("total_cost_usd")
        if not isinstance(cost, int | float):
            return
        self.store.add_usage(
            owner, cost_usd=float(cost), input_tokens=data.get("input_tokens"), output_tokens=data.get("output_tokens"),
            model=data.get("model"), **kw,
        )

    def _record_compact_usage(self, thread: str, job_id: int, result: str | None) -> None:
        try:
            cost = ((json.loads(result or "{}") or {}).get("compact") or {}).get("cost_usd")
        except (json.JSONDecodeError, AttributeError):
            return
        if isinstance(cost, int | float) and cost > 0:
            owner = (self.store.get_thread(thread) or {}).get("owner") or ""
            self.store.add_usage(owner, cost_usd=float(cost), thread=thread, job_id=job_id, kind="compact")

    def _limits_raw(self) -> dict[str, Any]:
        try:
            data = json.loads(self.store.get_meta("account_limits") or "{}")
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}

    def note_rate_limit(self, data: dict[str, Any]) -> None:
        """Remember the subscription's 5h / 7d utilization from a turn's rate_limit event (0..1, account-wide)."""
        cur = self._limits_raw()
        now = time.time()
        for name in LIMIT_WINDOWS:
            w = data.get(name)
            if isinstance(w, dict) and isinstance(w.get("utilization"), int | float):
                cur[name] = {"utilization": float(w["utilization"]), "resets_at": w.get("resets_at"), "at": now, "source": "turn"}
        self.store.set_meta("account_limits", json.dumps(cur))

    def save_usage_probe(self, report: dict[str, Any]) -> dict[str, Any]:
        """The worker's periodic zero-cost `claude /usage` (whole percent; also counts use outside the bridge)."""
        cur = self._limits_raw()
        now = time.time()
        for name in LIMIT_WINDOWS:
            pct = report.get(f"{name}_pct")
            if not isinstance(pct, int | float):
                continue
            prev = cur.get(name) or {}
            resets = report.get(f"{name}_resets_at") or prev.get("resets_at")
            if isinstance(resets, int | float) and resets < now:
                resets = None
            cur[name] = {"utilization": float(pct) / 100, "resets_at": resets, "at": now, "source": "probe"}
        self.store.set_meta("account_limits", json.dumps(cur))
        return self.account_limits()

    def account_limits(self) -> dict[str, Any]:
        """{five_hour, seven_day}: utilization 0..1 (0 once the window has reset), resets_at, window_start (epoch)."""
        raw = self._limits_raw()
        now = time.time()
        out: dict[str, Any] = {}
        for name, span in LIMIT_WINDOWS.items():
            w = dict(raw.get(name) or {})
            resets = w.get("resets_at")
            live = isinstance(resets, int | float) and resets > now
            if isinstance(resets, int | float) and not live:
                w["utilization"] = 0.0  # the window rolled over since we last heard
            w["window_start"] = (resets - span) if live else now - span
            w["known"] = "utilization" in w
            out[name] = w
        return out

    def usage_in_window(self, name: str, owner: str | None = None) -> dict[str, dict[str, Any]]:
        """Ledger totals by owner since the start of the account's current 5h / 7d window."""
        start = self.account_limits()[name]["window_start"]
        return self.store.usage_totals(utc_text(start), owner)

    def requeue_stale(self) -> int:
        n = 0
        for job in self.store.stale_running(self.config.stale_seconds):
            self.store.finish_job(job["id"], "failed", None, "helper 心跳超时")
            if job["kind"] in SESSION_JOB_KINDS and job["payload"].get("thread"):
                self._pub(job["payload"]["thread"], "job", job_frame(self.store.get_job(job["id"]) or job))
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
