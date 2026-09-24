"""The machine-side worker: claims jobs, runs `claude -p`, streams results back, honours cancel.

Hosts customise behaviour through `WorkerConfig` (static knobs), `Hooks` (per-job callbacks) and
`handlers` (extra job kinds beyond the built-in `chat`).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from claude_bridge._version import __version__
from claude_bridge.client import BridgeClient, BridgeClientError
from claude_bridge.stream_json import (
    StreamState,
    describe_tool,
    iter_stream,
    parse_context_report,
    parse_usage_report,
)

log = logging.getLogger(__name__)

VALID_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
SESSION_KINDS = ("context", "compact")  # built-in jobs that act on an existing Claude session
PROJECT_KIND = "project"  # create (git init / git clone) or delete a Code-mode project directory
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass
class WorkerProfile:
    """Per-scope overrides (e.g. a "chat" scope without tools, a "code" scope with a sandbox). None = inherit.

    `cwd` may contain `{owner}` (the thread owner's id, "shared" when there is none) so every user gets their own
    directory, and `{project}` for scopes of the form "<profile>:<project>" (Code mode: "code:myrepo"); it is
    created on demand. Session jobs (/context, /compact) use the same cwd as the chat, because
    claude keeps sessions per project directory and `--resume` only finds them there.
    """

    cwd: str | None = None
    tools: list[str] | None = None            # --tools: the built-in tools that exist at all ([] = none)
    allowed_tools: list[str] | None = None    # --allowedTools: run without asking
    disallowed_tools: list[str] | None = None
    permission_mode: str | None = None
    settings: dict[str, Any] | str | None = None  # --settings (JSON object or a file path), e.g. a sandbox block
    system_prompt: str | None = None          # replaces WorkerConfig.system_prompt for this scope
    add_dirs: list[str] | None = None
    strict_mcp: bool | None = None            # --strict-mcp-config with no servers: ignore the machine's MCP config

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorkerProfile:
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown profile keys: {sorted(unknown)}")
        return cls(**d)


def load_profiles(path: str | Path) -> dict[str, WorkerProfile]:
    """JSON `{scope: {profile keys}}`; the scope "" is the default chat."""
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    return {str(k): WorkerProfile.from_dict(v) for k, v in raw.items() if not str(k).startswith("_")}


def _owner_dir(owner: Any) -> str:
    o = re.sub(r"[^A-Za-z0-9_-]", "", str(owner or ""))
    return f"u{o}" if o else "shared"


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
    # report which models this machine's `claude` supports (zero-cost local `/model` probes)
    session_heartbeat: float = 15.0  # /compact can take minutes; keep the server from declaring the job dead
    model_probe: bool = True
    model_probe_interval: float = 6 * 3600
    version_check_interval: float = 600
    # account-wide 5h / weekly utilization via the zero-cost `claude -p "/usage"` (0 = off); feeds the server's quotas
    usage_probe_interval: float = 600
    tool_result_max_chars: int = 4000
    thinking_max_chars: int = 4000
    kill_grace: float = 3.0
    # scope → overrides; jobs carry their thread's scope and owner. Built-in knobs below are the fallback.
    profiles: dict[str, WorkerProfile] = field(default_factory=dict)
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    settings: dict[str, Any] | str | None = None
    add_dirs: list[str] = field(default_factory=list)
    strict_mcp: bool = False
    project_timeout: float = 900.0  # git clone of a big repository


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
        self._probe_lock = threading.Lock()
        self._probe_thread: threading.Thread | None = None
        self._next_probe = 0.0          # monotonic time of the next model probe
        self._next_version_check = 0.0
        self._probed_version: str | None = None
        self._next_usage_probe = 0.0

    @property
    def kinds(self) -> list[str]:
        if self.config.kinds:
            return list(self.config.kinds)
        has_projects = any("{project}" in (p.cwd or "") for p in self.config.profiles.values())
        return ["chat", *SESSION_KINDS, *([PROJECT_KIND] if has_projects else []), *self.handlers]

    # ---------------- loop ----------------

    def run_forever(self) -> None:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.request_stop())
        if self.config.cwd:
            Path(self.config.cwd).expanduser().mkdir(parents=True, exist_ok=True)
        log.info("worker %s started (claude=%s cwd=%s kinds=%s)", self.config.worker_name, self.config.claude_bin, self.config.cwd, self.kinds)
        backoff = 2.0
        while not self.stopping:
            try:
                self.maybe_probe_models()
                self.maybe_probe_usage()
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
            elif kind in SESSION_KINDS:
                self.run_session_job(job)
            elif kind == PROJECT_KIND:
                self.run_project_job(job)
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

    # ---------------- per-scope profiles ----------------

    def config_for(self, payload: dict[str, Any] | None) -> WorkerConfig:
        """The worker config with the job's scope profile applied (cwd resolved per owner and created)."""
        payload = payload or {}
        scope = str(payload.get("scope") or "")
        base, _, project = scope.partition(":")
        prof = self.config.profiles.get(scope) or (self.config.profiles.get(base) if project else None)
        if prof is None:
            return self.config
        over = {f.name: getattr(prof, f.name) for f in fields(prof) if getattr(prof, f.name) is not None}
        if "cwd" in over:
            cwd = self.profile_dir(str(over["cwd"]), payload.get("owner"), project)
            cwd.mkdir(parents=True, exist_ok=True)
            over["cwd"] = cwd
        return replace(self.config, **over)

    @staticmethod
    def profile_dir(template: str, owner: Any, project: str = "") -> Path:
        if "{project}" in template and not PROJECT_NAME_RE.match(project or ""):
            raise ValueError(f"bad or missing project name {project!r}")
        return Path(os.path.expanduser(template.replace("{owner}", _owner_dir(owner)).replace("{project}", project or "")))

    # ---------------- Code-mode projects ----------------

    def _project_path(self, payload: dict[str, Any]) -> Path:
        base, _, project = str(payload.get("scope") or "").partition(":")
        prof = self.config.profiles.get(base)
        if not prof or not prof.cwd or "{project}" not in prof.cwd or project != payload.get("name"):
            raise RuntimeError("worker 没有配置项目目录（profile 的 cwd 里需要 {project}）")
        path = self.profile_dir(prof.cwd, payload.get("owner"), project)
        parent = path.parent.resolve()
        if path.is_symlink() or path.resolve().parent != parent or path.name != project:
            raise RuntimeError("项目路径不安全")
        return path

    def _run_git(self, cmd: list[str], job_id: int, cwd: Path | None = None) -> str:
        """Run git with heartbeats (a clone can outlast the stale-job timeout); never prompts for credentials."""
        env = {**self._session_env(), "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "SSH_ASKPASS": "true",
               "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"}
        proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, start_new_session=True)
        out: list[str] = []
        reader = threading.Thread(target=lambda: out.append(proc.stdout.read() if proc.stdout else ""), daemon=True)
        reader.start()
        deadline = time.monotonic() + self.config.project_timeout
        while True:
            try:
                proc.wait(timeout=max(0.1, min(self.config.session_heartbeat, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                cancel = time.monotonic() >= deadline
                try:
                    cancel = cancel or bool((self.client.post_events(job_id) or {}).get("cancel"))
                except BridgeClientError as e:
                    log.warning("project heartbeat failed: %s", e)
                if cancel:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
                    proc.wait(timeout=10)
                    raise RuntimeError("超时或已取消") from None
        reader.join(timeout=5)
        text = (out[0] if out else "").strip()
        if proc.returncode != 0:
            raise RuntimeError(text[-400:] or f"{cmd[1]} 失败（exit {proc.returncode}）")
        return text

    def run_project_job(self, job: dict[str, Any]) -> None:
        p = job.get("payload") or {}
        path: Path | None = None
        created = False
        try:
            path = self._project_path(p)
            if p.get("action") == "delete":
                if path.exists():
                    shutil.rmtree(path)
                self._safe_finish(job["id"], ok=True, result=json.dumps({"path": str(path)}))
                return
            if path.exists() and any(path.iterdir()):
                raise RuntimeError("目录已存在且不是空的")
            created = not path.exists()
            path.parent.mkdir(parents=True, exist_ok=True)
            url = str(p.get("clone_url") or "")
            if url:
                self._run_git(["git", "clone", "--", url, str(path)], job["id"])
            else:
                path.mkdir(exist_ok=True)
                self._run_git(["git", "init", "-b", "main"], job["id"], cwd=path)
            branch = self._run_git(["git", "rev-parse", "--abbrev-ref", "HEAD"], job["id"], cwd=path) if url else "main"
            info = {"path": str(path), "branch": branch, "cloned": bool(url)}
            self._safe_finish(job["id"], ok=True, result=json.dumps(info, ensure_ascii=False))
        except Exception as e:
            log.warning("project job #%s failed: %s", job["id"], e)
            if created and path is not None and path.exists():  # leave no half-made directory behind
                shutil.rmtree(path, ignore_errors=True)
            self._safe_finish(job["id"], ok=False, error=str(e)[:500], error_kind="worker")

    # ---------------- session commands (/context, /compact) ----------------

    def _session_env(self) -> dict[str, str]:
        return {**os.environ, **self.config.extra_env}

    # ---------------- models the local CLI supports ----------------

    def cli_version(self) -> str:
        try:
            out = subprocess.run([self.config.claude_bin, "--version"], capture_output=True, text=True, timeout=20,
                                 stdin=subprocess.DEVNULL, env=self._session_env())
            return out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def _slash(self, command: str, model: str | None = None) -> str | None:
        """Run a local slash command in print mode (no model call, nothing saved) and return its text."""
        cmd = [self.config.claude_bin, "-p", command, "--output-format", "json", "--no-session-persistence"]
        if model:
            cmd += ["--model", model]
        try:
            proc = subprocess.run(cmd, cwd=str(self.config.cwd) if self.config.cwd else None, env=self._session_env(),
                                  capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("claude %s failed: %s", command, e)
            return None
        env = next((ev for ev in reversed(list(iter_stream(iter(proc.stdout.splitlines())))) if ev.get("type") == "result"), None)
        return str(env.get("result") or "") if env else None

    def probe_models(self, candidates: list[str]) -> dict[str, Any]:
        """Aliases this CLI knows (each follows its newest model) + which pinned `candidates` it recognises."""
        text = self._slash("/model", self.config.model or None)
        if text is None:
            raise RuntimeError("claude /model 没有输出")
        avail = re.search(r"Available:\s*(.+)", text)
        aliases = []
        for a in (avail.group(1).split(",") if avail else []):
            a = a.strip().rstrip(".")
            if a and not a.startswith("or ") and " " not in a and a not in PROBE_SKIP_ALIASES:
                aliases.append(a)
        with ThreadPoolExecutor(max_workers=4) as pool:
            alias_names = list(pool.map(lambda a: model_display_name(self._slash("/model", a)), aliases))
            pinned_names = list(pool.map(lambda i: model_display_name(self._slash(f"/model {i}")), candidates))
        seen: set[str] = set()
        out_aliases = []
        for a, name in zip(aliases, alias_names, strict=True):
            if name and name not in seen:  # "best" and "fable" are both Fable 5.1: keep the first
                seen.add(name)
                out_aliases.append({"id": a, "name": name})
        pinned = [{"id": i, "name": n} for i, n in zip(candidates, pinned_names, strict=True) if n]
        return {"cli_version": self.cli_version(), "bridge_version": __version__, "default_name": model_display_name(text) or "",
                "aliases": out_aliases, "pinned": pinned}

    def _probe_and_report(self) -> None:
        try:
            report = self.probe_models(self.client.model_candidates())
            self.client.report_models(report)
            self._probed_version = report["cli_version"]
            self._next_probe = time.monotonic() + self.config.model_probe_interval
            log.info("models reported: %d aliases, %d pinned (%s)", len(report["aliases"]), len(report["pinned"]), report["cli_version"])
        except Exception as e:
            self._next_probe = time.monotonic() + self.config.version_check_interval  # retry later, not every loop
            log.warning("model probe failed: %s", e)

    def maybe_probe_models(self, *, block: bool = False) -> None:
        """Probe in the background on start, every `model_probe_interval`, and when `claude --version` changes."""
        if not self.config.model_probe:
            return
        with self._probe_lock:
            if self._probe_thread and self._probe_thread.is_alive():
                return
            now = time.monotonic()
            if now >= self._next_version_check and self._probed_version is not None:
                self._next_version_check = now + self.config.version_check_interval
                if self.cli_version() != self._probed_version:
                    log.info("claude CLI version changed; re-probing models")
                    self._next_probe = 0.0
            if now < self._next_probe:
                return
            self._next_probe = now + self.config.model_probe_interval  # claimed; _probe_and_report sets the real value
            self._probe_thread = threading.Thread(target=self._probe_and_report, name="model-probe", daemon=True)
            self._probe_thread.start()
        if block:
            self._probe_thread.join()

    # ---------------- subscription usage (`/usage`) ----------------

    def probe_usage(self) -> dict[str, float] | None:
        text = self._slash("/usage")
        return parse_usage_report(text) if text else None

    def maybe_probe_usage(self) -> None:
        """Every `usage_probe_interval`: local command, no tokens, ~1 s. Servers without /limits just log a warning."""
        interval = self.config.usage_probe_interval
        now = time.monotonic()
        if not interval or now < self._next_usage_probe:
            return
        self._next_usage_probe = now + interval
        try:
            report = self.probe_usage()
            if report:
                self.client.report_limits(report)
        except BridgeClientError as e:
            log.warning("usage report failed: %s", e)
        except Exception:
            log.exception("usage probe failed")

    def context_report(
        self, session_id: str, settings: dict[str, Any] | None = None, cfg: WorkerConfig | None = None
    ) -> dict[str, Any] | None:
        """`claude -p "/context" --resume` is computed locally: no API call, no cost, ~1 s."""
        cfg = cfg or self.config
        model = ((settings or {}).get("model") or cfg.model or "").strip()
        cmd = [cfg.claude_bin, "-p", "/context", "--output-format", "json", "--resume", session_id]
        if model:
            cmd += ["--model", model]
        try:
            proc = subprocess.run(
                cmd, cwd=str(cfg.cwd) if cfg.cwd else None, env=self._session_env(), capture_output=True, text=True,
                timeout=60, stdin=subprocess.DEVNULL,
            )
            envelope = next((ev for ev in reversed(list(iter_stream(iter(proc.stdout.splitlines())))) if ev.get("type") == "result"), None)
            if envelope is None:
                log.warning("/context returned no result (exit %s): %s", proc.returncode, proc.stderr.strip()[-200:])
                return None
            if envelope.get("is_error"):
                log.warning("/context failed: %s", str(envelope.get("result"))[:200])
                return None
            report = parse_context_report(str(envelope.get("result") or ""))
            return report if report.get("used") is not None else None
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("/context unavailable: %s", e)
            return None

    def compact_session(
        self, session_id: str, settings: dict[str, Any] | None = None, job_id: int | None = None,
        cfg: WorkerConfig | None = None,
    ) -> dict[str, Any]:
        """`claude -p "/compact" --resume`: asks the model to summarise the history; returns compact_metadata.
        Long histories take minutes, so while it runs we heartbeat the job (and stop if the server asks us to)."""
        cfg = cfg or self.config
        model = ((settings or {}).get("model") or cfg.model or "").strip()
        cmd = [cfg.claude_bin, "-p", "/compact", "--output-format", "stream-json", "--verbose", "--resume", session_id]
        if model:
            cmd += ["--model", model]
        proc = subprocess.Popen(
            cmd, cwd=str(cfg.cwd) if cfg.cwd else None, env=self._session_env(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        out: dict[str, str] = {}
        readers = [threading.Thread(target=lambda k, f: out.__setitem__(k, f.read()), args=(k, f), daemon=True)
                   for k, f in (("stdout", proc.stdout), ("stderr", proc.stderr))]
        for t in readers:
            t.start()
        deadline = time.monotonic() + cfg.chat_timeout

        def stop(reason: str) -> None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=10)
            raise RuntimeError(reason)

        while True:
            try:
                proc.wait(timeout=max(0.1, min(cfg.session_heartbeat, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    stop(f"压缩超时（{cfg.chat_timeout:.0f} 秒）")
                if job_id is not None:
                    try:
                        if (self.client.post_events(job_id) or {}).get("cancel"):
                            stop("压缩已取消")
                    except BridgeClientError as e:
                        log.warning("compact heartbeat failed: %s", e)
        for t in readers:
            t.join(timeout=5)
        meta: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        for ev in iter_stream(iter(out.get("stdout", "").splitlines())):
            if ev.get("type") == "system" and ev.get("subtype") == "compact_boundary":
                m = ev.get("compact_metadata") or {}
                meta = {k: m.get(k) for k in ("trigger", "pre_tokens", "post_tokens", "cumulative_dropped_tokens", "duration_ms")}
            elif ev.get("type") == "result":
                result = ev
        if result is None:
            raise RuntimeError(f"claude 未返回 result（exit {proc.returncode}）：{out.get('stderr', '').strip()[-300:]}")
        if result.get("is_error"):
            raise RuntimeError(f"压缩失败：{result.get('result')}")
        if meta is None:
            raise RuntimeError("claude 没有报告压缩结果（compact_boundary）")
        meta["cost_usd"] = result.get("total_cost_usd")  # summarising calls the model; the server books it
        return meta

    def run_session_job(self, job: dict[str, Any]) -> None:
        p = job.get("payload") or {}
        sid = p.get("session_id")
        settings = p.get("settings") or {}
        if not sid:
            self._safe_finish(job["id"], ok=False, error="这个对话还没有 Claude 会话", error_kind="worker")
            return
        out: dict[str, Any] = {}
        cfg = self.config_for(p)
        if job["kind"] == "compact":
            out["compact"] = self.compact_session(sid, settings, job_id=job["id"], cfg=cfg)
        report = self.context_report(sid, settings, cfg)
        if job["kind"] == "context" and report is None:
            self._safe_finish(job["id"], ok=False, error="取不到上下文报告（claude /context 失败）", error_kind="claude")
            return
        out["context"] = report
        self._safe_finish(job["id"], ok=True, result=json.dumps(out, ensure_ascii=False), context=report)

    def build_chat_command(
        self, prompt: str, session_id: str | None, settings: dict[str, Any], system_prompt: str,
        *, input_json: bool = False, cfg: WorkerConfig | None = None,
    ) -> tuple[list[str], bool]:
        """`input_json`: the user turn is written to stdin as one stream-json message (used when it carries images)."""
        cfg = cfg or self.config
        model = (settings.get("model") or cfg.model or "").strip()
        effort = (settings.get("effort") or "").strip().lower()
        max_turns = int(settings.get("max_turns") or cfg.max_turns)
        via_stdin = input_json or len(prompt.encode()) > cfg.argv_limit
        cmd = [cfg.claude_bin, "-p", *([] if via_stdin else [prompt])]
        if input_json:
            cmd += ["--input-format", "stream-json"]
        cmd += ["--output-format", "stream-json", "--verbose"]
        if cfg.include_partial:
            cmd.append("--include-partial-messages")
        cmd += ["--max-turns", str(max(1, min(max_turns, 100)))]
        if cfg.tools is not None:  # [] → `--tools ""`: no built-in tools at all
            cmd += ["--tools", ",".join(cfg.tools)]
        if cfg.allowed_tools:
            cmd += ["--allowedTools", *cfg.allowed_tools]
        if cfg.disallowed_tools:
            cmd += ["--disallowedTools", *cfg.disallowed_tools]
        if cfg.settings:
            cmd += ["--settings", cfg.settings if isinstance(cfg.settings, str) else json.dumps(cfg.settings)]
        if cfg.add_dirs:
            cmd += ["--add-dir", *[os.path.expanduser(d) for d in cfg.add_dirs]]
        if cfg.strict_mcp:
            cmd += ["--strict-mcp-config"]
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


PROBE_SKIP_ALIASES = {"default", "opusplan"}  # "default" is our "" choice; opusplan only matters in plan mode


def model_display_name(text: str | None) -> str | None:
    """'Current model: <backticked name>' or 'Set model to <backticked name>' → the name; '… not found' → None."""
    m = re.search(r"(?:Current model|Set model to)[^`]*`([^`]+)`", text or "")
    return m.group(1).strip() if m else None


def stream_json_user_message(text: str, images: list[dict[str, str]]) -> str:
    """One `--input-format stream-json` line: images first (Claude reads image-then-text best), then the text.
    Several images get a short label each ("图 1：" or the name the browser gave, e.g. a long-screenshot part)."""
    content: list[dict[str, Any]] = []
    for i, im in enumerate(images, 1):
        if len(images) > 1:
            content.append({"type": "text", "text": f"{im.get('name') or f'图 {i}'}："})
        content.append({"type": "image", "source": {"type": "base64", "media_type": im["mime"], "data": im["data"]}})
    if text:
        content.append({"type": "text", "text": text})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False) + "\n"


class ChatRunner:
    def __init__(self, worker: Worker, job: dict[str, Any]) -> None:
        self.worker = worker
        self.job = job
        self.cfg = worker.config_for(job.get("payload"))
        self.proc: subprocess.Popen[str] | None = None
        self.kill_reason: str | None = None
        self._kill_lock = threading.Lock()
        self._done = threading.Event()
        self.stderr_tail: deque[str] = deque(maxlen=200)
        self.emitter = Emitter(worker.client, job["id"], self.cfg, on_cancel=lambda: self.kill("cancel"))
        self.state: StreamState | None = None

    # ---------------- lifecycle ----------------

    def _load_images(self, files: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Fetch the message's uploads from the server; a failure fails the job (the message shows the error)."""
        out = []
        for f in files:
            data, mime = self.worker.client.get_file(f["id"])
            out.append({"mime": f.get("mime") or mime, "name": f.get("name") or "", "data": base64.b64encode(data).decode()})
        if out:
            log.info("job #%s: %d image(s), %d KB", self.job["id"], len(out), sum(len(i["data"]) for i in out) * 3 // 4 // 1024)
        return out

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
        images = self._load_images(p.get("files") or [])
        cmd, via_stdin = self.worker.build_chat_command(
            prompt, session_id, settings, system_prompt, input_json=bool(images), cfg=self.cfg
        )
        stdin_data = stream_json_user_message(prompt, images) if images else prompt
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
            {"phase": "started", "text": f"$ claude -p … ({len(prompt)} chars{f', +{len(images)} 张图' if images else ''}, {label}{', with context' if prompt is not text else ''})"},
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
                proc.stdin.write(stdin_data)
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
        # refresh the session's context breakdown (local, free) so the UI shows the post-turn composition
        report = self.worker.context_report(st.session_id, settings, cfg=self.cfg) if st.session_id else None
        summary["context"] = report
        self.worker._safe_finish(
            jid, ok=True, result=json.dumps(summary, ensure_ascii=False), session_id=st.session_id, context=report
        )
        try:
            self.worker.hooks.on_chat_finished(self.job, st.text, summary)
        except Exception:
            log.exception("hooks.on_chat_finished failed")
