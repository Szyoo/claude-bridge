"""The machine-side worker: claims jobs, runs `claude -p`, streams results back, honours cancel.

Hosts customise behaviour through `WorkerConfig` (static knobs), `Hooks` (per-job callbacks) and
`handlers` (extra job kinds beyond the built-in `chat`).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_bridge.client import BridgeClient, BridgeClientError
from claude_bridge.stream_json import StreamState, describe_tool, iter_stream

log = logging.getLogger(__name__)

VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


@dataclass
class WorkerConfig:
    claude_bin: str = "claude"
    cwd: str | Path | None = None
    model: str = ""
    max_turns: int = 40
    allowed_tools: list[str] = field(default_factory=list)
    system_prompt: str = ""
    permission_mode: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    chat_timeout: float = 900.0
    include_partial: bool = True
    worker_name: str = field(default_factory=lambda: f"{platform.node()}:{os.getpid()}")
    kinds: list[str] | None = None
    poll_wait: int = 25
    flush_interval: float = 0.15
    flush_chars: int = 400
    cancel_poll_interval: float = 2.0
    argv_limit: int = 200_000
    tool_result_max_chars: int = 4000
    thinking_max_chars: int = 4000
    kill_grace: float = 3.0


class Hooks:
    """Override any of these; the defaults do nothing."""

    def build_context(self, *, thread_id: str, is_new_session: bool, payload: dict[str, Any]) -> str:
        return ""

    def env(self, payload: dict[str, Any]) -> dict[str, str]:
        return {}

    def system_prompt(self, payload: dict[str, Any]) -> str | None:
        return None

    def on_chat_finished(self, job: dict[str, Any], text: str, summary: dict[str, Any]) -> None:
        pass

    def on_job_error(self, job: dict[str, Any], exc: BaseException) -> None:
        pass

    def tick(self) -> None:
        pass


Handler = Callable[[dict[str, Any], "Worker"], Any]


class Worker:
    def __init__(
        self,
        client: BridgeClient,
        config: WorkerConfig | None = None,
        *,
        hooks: Hooks | None = None,
        handlers: dict[str, Handler] | None = None,
    ) -> None:
        self.client = client
        self.config = config or WorkerConfig()
        self.hooks = hooks or Hooks()
        self.handlers = dict(handlers or {})
        self.stopping = False
        self._current: ChatRunner | None = None

    @property
    def kinds(self) -> list[str]:
        return list(self.config.kinds) if self.config.kinds else ["chat", *self.handlers]

    # ---------------- loop ----------------

    def run_forever(self) -> None:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.request_stop())
        log.info("worker %s started (claude=%s cwd=%s kinds=%s)", self.config.worker_name, self.config.claude_bin, self.config.cwd, self.kinds)
        backoff = 2.0
        while not self.stopping:
            try:
                try:
                    self.hooks.tick()
                except Exception:
                    log.exception("hooks.tick failed")
                self.run_once()
                backoff = 2.0
            except BridgeClientError as e:
                log.warning("bridge unreachable, retry in %.0fs: %s", backoff, e)
                self._sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception:
                log.exception("job loop error")
                self._sleep(2)
        log.info("worker stopped")

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self.stopping and time.time() < end:
            time.sleep(0.2)

    def request_stop(self) -> None:
        self.stopping = True
        cur = self._current
        if cur is not None:
            cur.kill("shutdown")

    def run_once(self, wait: int | None = None) -> bool:
        job = self.client.next_job(self.config.worker_name, self.kinds, self.config.poll_wait if wait is None else wait)
        if not job:
            return False
        log.info("claimed job #%s %s", job["id"], job["kind"])
        try:
            kind = job["kind"]
            if kind == "chat":
                self.run_chat(job)
            elif kind in self.handlers:
                out = self.handlers[kind](job, self)
                result = out if isinstance(out, str) or out is None else json.dumps(out, ensure_ascii=False)
                self._safe_finish(job["id"], ok=True, result=result)
            else:
                self._safe_finish(job["id"], ok=False, error=f"未知任务类型 {kind}", error_kind="worker")
        except Exception as e:
            log.exception("job #%s failed", job["id"])
            try:
                self.hooks.on_job_error(job, e)
            except Exception:
                log.exception("hooks.on_job_error failed")
            self._safe_finish(job["id"], ok=False, error=f"{type(e).__name__}: {e}", error_kind="worker")
        return True

    def _safe_finish(self, job_id: int, **kw: Any) -> None:
        delay = 0.5
        for attempt in range(4):
            try:
                self.client.finish_job(job_id, **kw)
                return
            except BridgeClientError as e:
                if attempt == 3:
                    log.error("could not report job #%s result: %s", job_id, e)
                    return
                time.sleep(delay)
                delay *= 2

    # ---------------- chat ----------------

    def run_chat(self, job: dict[str, Any]) -> None:
        runner = ChatRunner(self, job)
        self._current = runner
        try:
            runner.run()
        finally:
            self._current = None

    def build_chat_command(
        self, prompt: str, session_id: str | None, settings: dict[str, Any], system_prompt: str
    ) -> tuple[list[str], bool]:
        cfg = self.config
        model = (settings.get("model") or cfg.model or "").strip()
        effort = (settings.get("effort") or "").strip().lower()
        max_turns = int(settings.get("max_turns") or cfg.max_turns)
        via_stdin = len(prompt.encode()) > cfg.argv_limit
        cmd = [cfg.claude_bin, "-p", *([] if via_stdin else [prompt]), "--output-format", "stream-json", "--verbose"]
        if cfg.include_partial:
            cmd.append("--include-partial-messages")
        cmd += ["--max-turns", str(max(1, min(max_turns, 100)))]
        if cfg.allowed_tools:
            cmd += ["--allowedTools", *cfg.allowed_tools]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        if model:
            cmd += ["--model", model]
        if effort in VALID_EFFORTS:
            cmd += ["--effort", effort]
        if cfg.permission_mode:
            cmd += ["--permission-mode", cfg.permission_mode]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd, via_stdin


class Emitter:
    """Coalesces deltas/events into batched `/events` posts; never drops text on transport errors."""

    def __init__(self, client: BridgeClient, job_id: int, cfg: WorkerConfig, on_cancel: Callable[[], None]) -> None:
        self.client = client
        self.job_id = job_id
        self.cfg = cfg
        self.on_cancel = on_cancel
        self._lock = threading.Lock()
        self._deltas: list[str] = []
        self._events: list[dict[str, Any]] = []
        self._status: str | None = None
        self._chars = 0
        self._urgent = False
        self.last_flush = time.time()
        self.retry_at = 0.0
        self._backoff = 0.5
        self.posts = 0

    def set_status(self, status: str) -> None:
        with self._lock:
            self._status = status
            self._urgent = True

    def add_delta(self, text: str) -> None:
        with self._lock:
            self._deltas.append(text)
            self._chars += len(text)
            big = self._chars >= self.cfg.flush_chars
        if big:
            self.flush()

    def add_event(self, type: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._events.append({"type": type, "data": data})
            self._urgent = True

    def add(self, emission: tuple[str, Any]) -> None:
        kind, payload = emission
        if kind == "delta":
            self.add_delta(payload)
        else:
            self.add_event(payload["type"], payload["data"])

    def pending(self) -> bool:
        with self._lock:
            return bool(self._deltas or self._events or self._status)

    def due(self, now: float) -> bool:
        if now < self.retry_at:
            return False
        with self._lock:
            if not (self._deltas or self._events or self._status):
                return False
            return self._urgent or now - self.last_flush >= self.cfg.flush_interval

    def flush(self) -> bool:
        with self._lock:
            if not (self._deltas or self._events or self._status):
                return True
            deltas, events, status = self._deltas, self._events, self._status
            self._deltas, self._events, self._status, self._chars, self._urgent = [], [], None, 0, False
        try:
            resp = self.client.post_events(self.job_id, status=status, deltas=deltas or None, events=events or None)
        except BridgeClientError as e:
            with self._lock:  # put everything back in front of whatever arrived meanwhile
                self._deltas = deltas + self._deltas
                self._events = events + self._events
                self._status = status or self._status
                self._chars += sum(len(d) for d in deltas)
            self.retry_at = time.time() + self._backoff
            self._backoff = min(self._backoff * 2, 8.0)
            log.warning("post_events failed, will retry: %s", e)
            return False
        self.posts += 1
        self._backoff = 0.5
        self.last_flush = time.time()
        if resp and resp.get("cancel"):
            self.on_cancel()
        return True

    def final_flush(self, attempts: int = 6) -> None:
        for _ in range(attempts):
            if self.flush():
                return
            time.sleep(max(0.0, self.retry_at - time.time()))


class ChatRunner:
    def __init__(self, worker: Worker, job: dict[str, Any]) -> None:
        self.worker = worker
        self.job = job
        self.cfg = worker.config
        self.proc: subprocess.Popen[str] | None = None
        self.kill_reason: str | None = None
        self._kill_lock = threading.Lock()
        self._done = threading.Event()
        self.stderr_tail: deque[str] = deque(maxlen=200)
        self.emitter = Emitter(worker.client, job["id"], self.cfg, on_cancel=lambda: self.kill("cancel"))
        self.state: StreamState | None = None

    # ---------------- lifecycle ----------------

    def run(self) -> None:
        p = self.job.get("payload") or {}
        text = p.get("text", "")
        settings = p.get("settings") or {}
        session_id = p.get("session_id") or None
        prompt = text
        if settings.get("auto_context", True):
            try:
                ctx = self.worker.hooks.build_context(
                    thread_id=p.get("thread", ""), is_new_session=not session_id, payload=p
                )
            except Exception:
                log.exception("hooks.build_context failed; continuing without context")
                ctx = ""
            if ctx:
                prompt = ctx + text
        system_prompt = self.worker.hooks.system_prompt(p) or self.cfg.system_prompt
        cmd, via_stdin = self.worker.build_chat_command(prompt, session_id, settings, system_prompt)
        env = {**os.environ, **self.cfg.extra_env, **self.worker.hooks.env(p)}
        self.state = StreamState(
            resumed=bool(session_id),
            thinking_max=self.cfg.thinking_max_chars,
            tool_result_max=self.cfg.tool_result_max_chars,
        )
        label = f"model={settings.get('model') or self.cfg.model or 'default'} effort={settings.get('effort') or 'default'}"
        self.emitter.set_status("streaming")
        self.emitter.add_event(
            "status",
            {"phase": "started", "text": f"$ claude -p … ({len(prompt)} chars, {label}{', with context' if prompt is not text else ''})"},
        )

        start = time.time()
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.cfg.cwd) if self.cfg.cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if via_stdin else subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        proc = self.proc
        if via_stdin and proc.stdin is not None:
            try:
                proc.stdin.write(prompt)
            finally:
                proc.stdin.close()

        threads = [
            threading.Thread(target=self._drain_stderr, daemon=True),
            threading.Thread(target=self._supervise, args=(start,), daemon=True),
        ]
        for t in threads:
            t.start()
        try:
            assert proc.stdout is not None
            for ev in iter_stream(proc.stdout):
                for emission in self.state.feed(ev):
                    self.emitter.add(emission)
                    if emission[0] == "event" and emission[1]["type"] == "tool_use":
                        log.info("job #%s tool: %s", self.job["id"], describe_tool(emission[1]["data"]))
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.kill("timeout")
                proc.wait(timeout=10)
        finally:
            self._done.set()
            if proc.poll() is None:
                self._terminate(proc)
        for t in threads:
            t.join(timeout=5)
        self.emitter.final_flush()
        self._finish(proc.returncode, settings)

    def _drain_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                self.stderr_tail.append(line.rstrip("\n"))
        except ValueError:
            pass

    def _supervise(self, start: float) -> None:
        last_poll = time.time()
        while not self._done.wait(0.1):
            now = time.time()
            if self.emitter.due(now):
                self.emitter.flush()
            if self.worker.stopping:
                self.kill("shutdown")
            if now - start > self.cfg.chat_timeout:
                self.kill("timeout")
            if now - last_poll >= self.cfg.cancel_poll_interval and self.kill_reason is None:
                last_poll = now
                try:
                    job = self.worker.client.get_job(self.job["id"])
                except BridgeClientError as e:
                    log.debug("cancel poll failed: %s", e)
                    continue
                if job is None or job.get("cancel_requested") or job.get("status") != "running":
                    self.kill("cancel")

    # ---------------- kill ----------------

    def kill(self, reason: str) -> None:
        with self._kill_lock:
            if self.kill_reason is not None:
                return
            self.kill_reason = reason
        proc = self.proc
        if proc is not None and proc.poll() is None:
            log.info("job #%s killing claude (%s)", self.job["id"], reason)
            self._terminate(proc)

    def _terminate(self, proc: subprocess.Popen[str]) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return

        def hard_kill() -> None:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

        threading.Timer(self.cfg.kill_grace, hard_kill).start()

    # ---------------- finish ----------------

    def _finish(self, returncode: int | None, settings: dict[str, Any]) -> None:
        st = self.state
        assert st is not None
        jid = self.job["id"]
        effort = settings.get("effort") or None
        summary = st.summary(effort=effort)
        stderr = "\n".join(self.stderr_tail).strip()[-400:]

        if self.kill_reason == "cancel":
            self.worker._safe_finish(
                jid, ok=False, cancelled=True, session_id=st.session_id, result=json.dumps(summary, ensure_ascii=False)
            )
            return
        if self.kill_reason == "timeout":
            self.worker._safe_finish(
                jid, ok=False, error=f"Claude 超过 {int(self.cfg.chat_timeout // 60)} 分钟未完成", error_kind="timeout",
                session_id=st.session_id,
            )
            return
        if self.kill_reason == "shutdown":
            self.worker._safe_finish(jid, ok=False, error="helper 重启 / 停止", error_kind="worker", session_id=st.session_id)
            return
        if st.result is None:
            self.worker._safe_finish(
                jid, ok=False, error=f"claude 未返回 result（exit {returncode}）：{stderr or '无 stderr'}", error_kind="claude"
            )
            return
        if st.result.get("is_error"):
            msg = str(st.result.get("result") or stderr or "未知错误")
            low = msg.lower()
            bad_session = bool(self.job["payload"].get("session_id")) and ("session" in low or "resume" in low)
            self.worker._safe_finish(
                jid, ok=False, error=f"Claude 返回错误：{msg}", error_kind="bad_session" if bad_session else "claude",
                reset_session=bad_session,
            )
            return

        self.emitter.add_event("usage", st.usage_data())
        self.emitter.final_flush()
        self.worker._safe_finish(jid, ok=True, result=json.dumps(summary, ensure_ascii=False), session_id=st.session_id)
        try:
            self.worker.hooks.on_chat_finished(self.job, st.text, summary)
        except Exception:
            log.exception("hooks.on_chat_finished failed")
