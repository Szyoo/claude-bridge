"""The Messages-compatible endpoint: `POST /v1/messages`, `POST /v1/messages/count_tokens`, `GET /v1/models`.

Clients that speak the Anthropic Messages API (desktop apps in third-party mode, editors, scripts) point their base URL
here and use a caller token (`cbc_…`, scope "api") as their API key. Each request runs as a bridge turn of the token's
owner (their quota, their usage ledger) on the worker's `claude`, with:

- the request's `system` replacing the CLI's own system prompt, and only the request's `tools` available: they run in
  the client (bridge client tools), never on the worker. The worker profile for scope "api" should allow no built-ins;
- the conversation mapped to a resumable session by fingerprint (messages_api.fingerprint), so the prompt cache
  survives across requests; a history the bridge doesn't recognise starts a fresh session with the history as text;
- one bridge turn spanning several API responses: a response ends with `stop_reason: "tool_use"` while claude waits for
  the tool; the client's next request (carrying the `tool_result`s) is fed back into that same waiting process and
  the rest of the turn becomes that request's response. Results are matched to claude's calls by the model's own
  tool_use ids, so it doesn't matter whether claude issues parallel calls at once or one after another.

Tiny requests (`max_tokens` ≤ 1 without tools — connectivity probes) are answered here without the model; small
tool-less ones (titles, summaries) run as one-off turns on a small model.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from claude_bridge import messages_api as mx
from claude_bridge.callers import Callers
from claude_bridge.client_tools import ClientToolService
from claude_bridge.errors import BridgeError
from claude_bridge.models import ClientTool, ClientToolResultIn, TurnOptions
from claude_bridge.pricing import FAMILY_DEFAULT, PRICES, price_key
from claude_bridge.principal import Principal
from claude_bridge.service import BridgeService
from claude_bridge.sse import SSE_HEADERS

log = logging.getLogger(__name__)

SCOPE = "api"
MCP_PREFIX = "mcp__bridge_client__"
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
TOOL_NAME_MAX = 44  # claude sees mcp__bridge_client__<name>; tool names are capped at 64 characters
PROBE_MAX_TOKENS = 1  # max_tokens at or below this, no tools: answered without the model
SMALL_MAX_TOKENS = 1024  # no tools and at most this: a one-off turn on SMALL_MODEL
SMALL_MODEL = "haiku"
KEEPALIVE_SECONDS = 8.0
TOOL_WAIT_SECONDS = 3600.0  # a turn whose client never sends the tool results back is cancelled after this
TURN_TIMEOUT_SECONDS = 4 * 3600.0
CONVO_SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_api_convos (
  owner      TEXT NOT NULL,
  fp         TEXT NOT NULL,
  thread     TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (owner, fp)
);
CREATE INDEX IF NOT EXISTS idx_bridge_api_convos_thread ON bridge_api_convos(thread);
"""


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _fail(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=mx.error_body(status, message))


def safe_tool_name(name: str) -> str:
    """A client tool's name as registered with claude (MCP-safe, short enough); stable, so sessions stay valid."""
    if TOOL_NAME_RE.match(name) and len(name) <= TOOL_NAME_MAX:
        return name
    head = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:30].lstrip("-") or "t"
    return f"{head}_{hashlib.sha1(name.encode()).hexdigest()[:12]}"


def cli_model(requested: str | None) -> str:
    """The model id to run: a known id as is, otherwise the newest of its family (or the CLI default when none)."""
    m = (requested or "").strip().lower()
    return price_key(m) if m and any(f in m for f in FAMILY_DEFAULT) else ""


def classify_error(message: str) -> int:
    low = (message or "").lower()
    if any(h in low for h in ("session limit", "usage limit", "rate limit", "limit reached", "rate_limit", "额度")):
        return 429
    if "overloaded" in low:
        return 529
    return 500


def mcp_content(content: Any) -> list[dict[str, Any]]:
    """A Messages tool_result's content → MCP content blocks for the waiting claude."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for b in mx.blocks(content):
        if b.get("type") == "text":
            out.append({"type": "text", "text": b.get("text") or ""})
        elif b.get("type") == "image" and (b.get("source") or {}).get("type") == "base64":
            out.append({"type": "image", "data": b["source"].get("data") or "",
                        "mimeType": b["source"].get("media_type") or "image/png"})
        else:
            out.append({"type": "text", "text": json.dumps(b, ensure_ascii=False)})
    return out or [{"type": "text", "text": ""}]


class Turn:
    """One bridge turn (one job) served as one or more API responses."""

    def __init__(self, mgr: ApiManager, *, owner: str, caller_id: int, thread: str, model: str, names: dict[str, str],
                 queue: asyncio.Queue) -> None:
        self.mgr, self.owner, self.caller_id, self.thread, self.model = mgr, owner, caller_id, thread, model
        self.names = names  # safe name → the client's name
        self.queue = queue
        self.message_id: int | None = None
        self.job_id: int | None = None
        self.pending: dict[str, dict[str, Any]] = {}  # toolu id → {safe, name, input, call_id, result}
        self.unmatched: list[dict[str, Any]] = []  # client_tool_call events not yet tied to a tool_use block
        self.done = False
        self.streamed_any = False  # content_stream seen at all (else fall back to the plain text track)
        self.waiting_since: float | None = None
        self.lock = asyncio.Lock()  # one response at a time
        self.created = time.time()

    # ---------------- tool results ----------------

    def _match_calls(self) -> list[tuple[str, dict[str, Any]]]:
        """Tie claude's client_tool_call events to tool_use blocks (same tool, same input, first come)."""
        ready = []
        for call in list(self.unmatched):
            for tid, p in self.pending.items():
                if p.get("call_id") is None and p["safe"] == call.get("name") and p["input"] == call.get("input"):
                    p["call_id"] = call["call_id"]
                    self.unmatched.remove(call)
                    if p.get("result") is not None:
                        ready.append((tid, p))
                    break
        return ready

    async def _send_ready(self, ready: list[tuple[str, dict[str, Any]]]) -> None:
        for _tid, p in ready:
            if p.get("sent") or p.get("call_id") is None or p.get("result") is None:
                continue
            p["sent"] = True
            body = ClientToolResultIn(content=p["result"]["content"], is_error=p["result"]["is_error"])
            try:
                await run_in_threadpool(self.mgr.tools.respond, self.message_id, p["call_id"], body, self.owner)
            except BridgeError as e:
                log.warning("api turn job #%s: tool result %s refused: %s", self.job_id, p["call_id"], e)

    async def deliver(self, results: list[dict[str, Any]]) -> None:
        for r in results:
            p = self.pending.get(r["tool_use_id"])
            if p is not None and p.get("result") is None:
                p["result"] = {"content": mcp_content(r["content"]), "is_error": r["is_error"]}
        self.waiting_since = None
        await self._send_ready([(tid, p) for tid, p in self.pending.items()])

    # ---------------- one response ----------------

    async def events(self, msg_id: str) -> AsyncIterator[tuple[str, dict[str, Any]] | None]:
        """The Messages stream events of the next response; None items are keep-alive ticks."""
        index: dict[int, int | None] = {}  # native block index → ours (None: dropped)
        tool_index: dict[int, str] = {}  # native index of a client tool_use block → its toolu id
        partial: dict[str, str] = {}
        nxt = 0
        started = False
        stop_reason, usage, out_usage = None, {}, {}
        seg_tools = 0

        def start(native_usage: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
            return "message_start", {"type": "message_start", "message": mx.message_shell(msg_id, self.model, native_usage)}

        def finish(reason: str | None) -> list[tuple[str, dict[str, Any]]]:
            return [("message_delta", {"type": "message_delta", "delta": {"stop_reason": reason or "end_turn", "stop_sequence": None},
                                       "usage": {"output_tokens": out_usage.get("output_tokens", 0)}}),
                    ("message_stop", {"type": "message_stop"})]

        text_fallback: list[str] = []
        deadline = time.time() + TURN_TIMEOUT_SECONDS
        while True:
            try:
                frame = await asyncio.wait_for(self.queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                if time.time() > deadline:
                    raise ApiError(500, "这一轮运行太久，已放弃等待") from None
                yield None
                continue
            if frame is None:  # the broker dropped a slow subscriber
                raise ApiError(500, "内部事件流中断")
            ftype, data = frame.get("type"), frame.get("data") or {}
            if data.get("message_id") not in (None, self.message_id):
                continue
            if ftype == "delta":
                text_fallback.append(data.get("text") or "")
                continue
            if ftype == "done":
                self.done = True
                status = data.get("status")
                job = data.get("job") or {}
                if status == "done":
                    if not self.streamed_any:  # a worker without content blocks: the plain text as one block
                        if not started:
                            yield start()
                        text = "".join(text_fallback)
                        yield "content_block_start", {"type": "content_block_start", "index": nxt, "content_block": {"type": "text", "text": ""}}
                        if text:
                            yield "content_block_delta", {"type": "content_block_delta", "index": nxt, "delta": {"type": "text_delta", "text": text}}
                        yield "content_block_stop", {"type": "content_block_stop", "index": nxt}
                        for e in finish("end_turn"):
                            yield e
                        return
                    if started:
                        for e in finish(stop_reason):
                            yield e
                    return
                err = job.get("error") or ("已取消" if status == "cancelled" else "执行失败")
                raise ApiError(classify_error(err), err)
            if ftype != "event":
                continue
            etype, edata = data.get("type"), data.get("data") or {}
            if etype == "client_tool_call":
                self.unmatched.append(edata)
                await self._send_ready(self._match_calls())
                continue
            if etype == "error" and not self.done:
                continue  # the done frame that follows carries the job's error
            if etype != "content_stream":
                continue
            self.streamed_any = True
            ev = edata.get("event") or {}
            kind = ev.get("type")
            if kind == "message_start":
                u = (ev.get("message") or {}).get("usage") or {}
                usage = {k: u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
                if not started:
                    started = True
                    yield start(usage)
                index.clear()
                continue
            if not started:
                started = True
                yield start(usage)
            if kind == "content_block_start":
                block = dict(ev.get("content_block") or {})
                ni = ev.get("index", 0)
                if block.get("type") in ("tool_use", "server_tool_use"):
                    native = block.get("name") or ""
                    if not native.startswith(MCP_PREFIX):  # a worker-side tool slipped through: not the client's
                        index[ni] = None
                        continue
                    safe = native[len(MCP_PREFIX):]
                    block = {"type": "tool_use", "id": block.get("id"), "name": self.names.get(safe, safe), "input": {}}
                    tool_index[ni] = block["id"]
                    self.pending[block["id"]] = {"safe": safe, "name": block["name"], "input": None, "call_id": None, "result": None}
                    partial[block["id"]] = ""
                    seg_tools += 1
                index[ni] = nxt
                nxt += 1
                yield "content_block_start", {"type": "content_block_start", "index": index[ni], "content_block": block}
            elif kind == "content_block_delta":
                ni = ev.get("index", 0)
                if index.get(ni) is None:
                    continue
                delta = ev.get("delta") or {}
                if delta.get("type") == "input_json_delta" and ni in tool_index:
                    partial[tool_index[ni]] += delta.get("partial_json") or ""
                yield "content_block_delta", {"type": "content_block_delta", "index": index[ni], "delta": delta}
            elif kind == "content_block_stop":
                ni = ev.get("index", 0)
                if index.get(ni) is None:
                    continue
                if ni in tool_index:
                    tid = tool_index.pop(ni)
                    try:
                        self.pending[tid]["input"] = json.loads(partial.pop(tid) or "{}")
                    except json.JSONDecodeError:
                        self.pending[tid]["input"] = {}
                    await self._send_ready(self._match_calls())
                yield "content_block_stop", {"type": "content_block_stop", "index": index[ni]}
            elif kind == "message_delta":
                stop_reason = (ev.get("delta") or {}).get("stop_reason") or stop_reason
                out_usage = ev.get("usage") or out_usage
            elif kind == "message_stop":
                if stop_reason == "tool_use" and seg_tools:
                    # claude now waits for the client to run the tools; this response ends here
                    self.waiting_since = time.time()
                    for e in finish("tool_use"):
                        yield e
                    return
                if stop_reason and stop_reason != "tool_use":
                    for e in finish(stop_reason):
                        yield e
                    return


class ApiManager:
    def __init__(self, service: BridgeService, tools: ClientToolService) -> None:
        self.service = service
        self.store = service.store
        self.tools = tools
        self.turns: dict[str, Turn] = {}  # toolu id → the turn waiting on it
        self.live: dict[int, Turn] = {}  # job id → turn
        with self.store.lock:
            self.store.conn.executescript(CONVO_SCHEMA)

    # ---------------- conversations ----------------

    def thread_for(self, owner: str, fp: str) -> str | None:
        row = self.store._one("SELECT thread FROM bridge_api_convos WHERE owner=? AND fp=?", (owner, fp))
        if not row:
            return None
        th = self.store.get_thread(row["thread"])
        return row["thread"] if th and (th.get("owner") or "") == owner else None

    async def settle(self, thread: str) -> bool:
        """Before continuing a conversation: a last turn that waits on tool results the client abandoned is cancelled;
        one that is merely finishing (its answer streamed, the worker still reporting back) is waited for.
        True when the thread is free and has a session to resume."""
        live = [t for t in self.live.values() if t.thread == thread and not t.done]
        if any(t.waiting_since for t in live):
            await self.cancel_waiting(thread)
        for _ in range(240):
            if not self.store.inflight(thread):
                break
            await asyncio.sleep(0.25)
        th = self.store.get_thread(thread)
        return bool(th and th.get("session_id")) and not self.store.inflight(thread)

    def remember(self, owner: str, fp: str, thread: str) -> None:
        self.store._x("INSERT INTO bridge_api_convos(owner, fp, thread, updated_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(owner, fp) DO UPDATE SET thread=excluded.thread, updated_at=excluded.updated_at",
                      (owner, fp, thread, time.time()))

    def reap(self) -> None:
        """Forget turns whose job has ended. A turn's last response ends at the model's message_stop, before the
        worker reports the job done, so nobody is reading its stream any more when that happens."""
        for jid, t in list(self.live.items()):
            job = self.store.get_job(jid)
            if t.done or not job or job["status"] not in ("queued", "running"):
                t.done = True
                self._forget(jid)

    def inflight_for(self, caller_id: int) -> int:
        self.reap()
        return sum(1 for t in self.live.values() if t.caller_id == caller_id)

    async def watchdog(self) -> None:
        """Cancel turns whose client never came back with the tool results; forget finished ones."""
        while True:
            await asyncio.sleep(30)
            now = time.time()
            self.reap()
            for jid, t in list(self.live.items()):
                if t.waiting_since and now - t.waiting_since > TOOL_WAIT_SECONDS and t.message_id:
                    log.info("api turn job #%s: no tool results for %.0f s, cancelling", jid, now - t.waiting_since)
                    with contextlib.suppress(BridgeError):
                        await run_in_threadpool(self.service.cancel, t.message_id, t.owner)
                    t.waiting_since = None

    def _forget(self, jid: int) -> None:
        t = self.live.pop(jid, None)
        if t is None:
            return
        for tid in [k for k, v in self.turns.items() if v is t]:
            self.turns.pop(tid, None)
        self.service.broker.unsubscribe(t.thread, t.queue)

    async def cancel_waiting(self, thread: str) -> None:
        """A new request continues a conversation whose last turn still waits for tool results: stop that turn."""
        for jid, t in list(self.live.items()):
            if t.thread == thread and not t.done and t.message_id:
                with contextlib.suppress(BridgeError):
                    await run_in_threadpool(self.service.cancel, t.message_id, t.owner)
                for _ in range(40):
                    if not self.store.inflight(thread):
                        break
                    await asyncio.sleep(0.25)
                self._forget(jid)


def create_api_router(
    service: BridgeService,
    callers: Callers,
    tools: ClientToolService,
    principal_for: Callable[[dict[str, Any]], Principal | None],
) -> tuple[APIRouter, ApiManager]:
    mgr = ApiManager(service, tools)
    router = APIRouter()
    state: dict[str, Any] = {"watchdog": None}

    def authorize(request: Request) -> tuple[dict[str, Any], Principal]:
        """x-api-key (what SDKs send) or Authorization: Bearer; a caller token with the "api" scope."""
        auth = request.headers.get("authorization", "")
        key = request.headers.get("x-api-key") or (auth[7:].strip() if auth.lower().startswith("bearer ") else "")
        row = callers.verify(key) if key else None
        if not row or SCOPE not in row["scopes"]:
            raise ApiError(401, "API 密钥无效或已停用")
        principal = principal_for(row)
        if principal is None:
            raise ApiError(403, "这个密钥所属的账户不可用")
        return row, principal

    def ensure_watchdog() -> None:
        if state["watchdog"] is None or state["watchdog"].done():
            state["watchdog"] = asyncio.get_running_loop().create_task(mgr.watchdog())

    @router.get("/models")
    def models(request: Request):
        try:
            authorize(request)
        except ApiError as e:
            return _fail(e.status, e.message)
        info = service.agent_models() or {}
        seen, data = set(), []
        for m in [*(info.get("pinned") or []), *(info.get("aliases") or [])]:
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                data.append({"type": "model", "id": mid, "display_name": m.get("name") or mid, "created_at": "2026-01-01T00:00:00Z"})
        for mid in PRICES:
            if mid not in seen:
                data.append({"type": "model", "id": mid, "display_name": mid, "created_at": "2026-01-01T00:00:00Z"})
        return {"data": data, "has_more": False, "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None}

    @router.post("/messages/count_tokens")
    async def count_tokens(request: Request):
        try:
            authorize(request)
        except ApiError as e:
            return _fail(e.status, e.message)
        try:
            body = await request.json()
        except ValueError:
            return _fail(400, "请求体不是 JSON")
        return {"input_tokens": mx.estimate_tokens(body if isinstance(body, dict) else {})}

    @router.post("/messages")
    async def messages(request: Request):
        try:
            row, principal = authorize(request)
        except ApiError as e:
            return _fail(e.status, e.message)
        ensure_watchdog()
        try:
            body = await request.json()
        except ValueError:
            return _fail(400, "请求体不是 JSON")
        try:
            return await handle(body if isinstance(body, dict) else {}, row, principal)
        except ApiError as e:
            return _fail(e.status, e.message)
        except BridgeError as e:
            return _fail(e.status if e.status in mx.ERROR_TYPES else 400, str(e))

    async def handle(body: dict[str, Any], row: dict[str, Any], principal: Principal) -> Any:
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs or not isinstance(msgs[-1], dict) or msgs[-1].get("role") != "user":
            raise ApiError(400, "messages 必须是非空数组，且最后一条是 user")
        max_tokens = body.get("max_tokens")
        if not isinstance(max_tokens, int) or max_tokens < 1:
            raise ApiError(400, "max_tokens 必须是正整数")
        stream = bool(body.get("stream"))
        model_echo = str(body.get("model") or "claude")
        req_tools = [t for t in body.get("tools") or [] if isinstance(t, dict) and t.get("input_schema") is not None]
        if (body.get("tool_choice") or {}).get("type") == "none":
            req_tools = []
        owner = principal.owner

        # 1. a probe: answer without the model
        if max_tokens <= PROBE_MAX_TOKENS and not req_tools:
            return local_reply(body, model_echo, stream)

        # 2. the client is sending back tool results for a turn that is waiting on them
        results = mx.tool_results(msgs[-1])
        turn = next((mgr.turns[r["tool_use_id"]] for r in results if r["tool_use_id"] in mgr.turns), None)
        if turn is not None and turn.owner == owner and not turn.done:
            await turn.deliver(results)
            return await respond(turn, msgs, stream)

        if mgr.inflight_for(row["id"]) >= int(row.get("max_inflight") or 2):
            raise ApiError(429, f"这个密钥同时进行的请求已达上限（{row.get('max_inflight')}）")
        if not service.agent_online():
            raise ApiError(529, "helper 离线，暂时无法处理请求")

        # 3. a new turn: continue the session this history belongs to, or start one
        names: dict[str, str] = {}
        ctools: list[ClientTool] = []
        for t in req_tools:
            orig = str(t.get("name") or "")
            safe = safe_tool_name(orig)
            if not orig or safe in names:
                continue
            names[safe] = orig
            ctools.append(ClientTool(name=safe, description=str(t.get("description") or orig)[:20000],
                                     input_schema=t["input_schema"]))
        small = not req_tools and max_tokens <= SMALL_MAX_TOKENS
        prev, last = msgs[:-1], msgs[-1]
        thread = None if small or not prev else mgr.thread_for(owner, mx.fingerprint(prev))
        if thread and not await mgr.settle(thread):
            thread = None
        text, images = mx.user_text_and_images(last)
        if not thread and prev:
            text = f"<此前的对话>\n{mx.render_history(prev)}\n</此前的对话>\n\n{text}"
        if not text.strip() and not images:
            text = "（继续）"
        files = []
        for data, _mime in images[:10]:
            files.append((await run_in_threadpool(service.save_upload, data, "image", owner))["id"])
        thinking = (body.get("thinking") or {}).get("type")
        effort = ((body.get("output_config") or {}).get("effort") or "").lower() or None
        options = TurnOptions(
            model=SMALL_MODEL if small else (cli_model(body.get("model")) or None),
            effort=effort if effort in ("low", "medium", "high", "xhigh", "max") else None,
            max_turns=100, auto_context=False, max_output_tokens=min(max_tokens, 128000),
            thinking=None if thinking is None else thinking != "disabled",
            system=mx.system_text(body.get("system")) or None,
        )
        if not thread and not small:
            thread = await run_in_threadpool(service.new_thread, SCOPE, "api:" + secrets.token_hex(8),
                                             (text.splitlines() or [""])[0][:40], select=False, owner=owner)
        queue = service.broker.subscribe(thread) if thread else None
        try:
            started = await run_in_threadpool(
                lambda: service.start_chat(text, thread_id=thread, scope=SCOPE, files=files or None, principal=principal,
                                           options=options, client_tools=ctools or None, ephemeral=small))
        except BaseException:
            if queue is not None:
                service.broker.unsubscribe(thread, queue)
            raise
        if queue is None:  # ephemeral: its thread is known only now
            thread = started["thread"]
            queue = service.broker.subscribe(thread)
        turn = Turn(mgr, owner=owner, caller_id=row["id"], thread=thread, model=model_echo, names=names, queue=queue)
        turn.message_id, turn.job_id = started["message_id"], started["job_id"]
        mgr.live[turn.job_id] = turn
        return await respond(turn, msgs, stream, remember=not small)

    async def respond(turn: Turn, msgs: list[dict[str, Any]], stream: bool, remember: bool = True) -> Any:
        msg_id = mx.new_message_id()

        def after(content: list[dict[str, Any]]) -> None:
            for tid in turn.pending:
                mgr.turns[tid] = turn
            if remember:
                mgr.remember(turn.owner, mx.fingerprint([*msgs, {"role": "assistant", "content": content}]), turn.thread)
            if turn.done:
                mgr._forget(turn.job_id)

        if stream:
            async def gen() -> AsyncIterator[bytes]:
                content: list[dict[str, Any]] = []
                async with turn.lock:
                    try:
                        async for item in turn.events(msg_id):
                            if item is None:
                                yield b": keep-alive\n\n"
                                continue
                            _collect(content, *item)
                            yield mx.sse(*item)
                    except ApiError as e:
                        yield mx.sse("error", mx.error_body(e.status, e.message))
                        turn.done = True
                    finally:  # also when the client hangs up: the next request must still find this turn
                        after(content)
            return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)

        message = mx.message_shell(msg_id, turn.model)
        content: list[dict[str, Any]] = []
        async with turn.lock:
            try:
                async for item in turn.events(msg_id):
                    if item is None:
                        continue
                    name, data = item
                    _collect(content, name, data)
                    if name == "message_start":
                        message["usage"].update(data["message"]["usage"])
                    elif name == "message_delta":
                        message["stop_reason"] = data["delta"]["stop_reason"]
                        message["usage"]["output_tokens"] = data["usage"].get("output_tokens", 0)
            except ApiError:
                turn.done = True
                after(content)
                raise
        after(content)
        message["content"] = content
        return message

    return router, mgr


def _collect(content: list[dict[str, Any]], name: str, data: dict[str, Any]) -> None:
    """Rebuild the response's content blocks from its stream (for non-streaming replies and fingerprints)."""
    if name == "content_block_start":
        block = dict(data["content_block"])
        if block.get("type") == "tool_use":
            block["_json"] = ""
        content.append(block)
    elif name == "content_block_delta" and content:
        b, d = content[-1], data["delta"]
        t = d.get("type")
        if t == "text_delta":
            b["text"] = (b.get("text") or "") + d.get("text", "")
        elif t == "thinking_delta":
            b["thinking"] = (b.get("thinking") or "") + d.get("thinking", "")
        elif t == "signature_delta":
            b["signature"] = d.get("signature")
        elif t == "input_json_delta":
            b["_json"] = b.get("_json", "") + d.get("partial_json", "")
    elif name == "content_block_stop" and content:
        b = content[-1]
        if "_json" in b:
            raw = b.pop("_json")
            try:
                b["input"] = json.loads(raw) if raw else b.get("input") or {}
            except json.JSONDecodeError:
                b["input"] = {}


def local_reply(body: dict[str, Any], model: str, stream: bool) -> Any:
    """A connectivity probe (max_tokens ≤ 1, no tools): an empty answer that hit its limit, no model involved."""
    msg_id = mx.new_message_id()
    usage = {"input_tokens": mx.estimate_tokens(body), "output_tokens": 0}
    if not stream:
        m = mx.message_shell(msg_id, model, usage)
        m["content"] = [{"type": "text", "text": ""}]
        m["stop_reason"] = "max_tokens"
        return m
    events = [
        ("message_start", {"type": "message_start", "message": mx.message_shell(msg_id, model, usage)}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "max_tokens", "stop_sequence": None}, "usage": {"output_tokens": 0}}),
        ("message_stop", {"type": "message_stop"}),
    ]

    async def gen() -> AsyncIterator[bytes]:
        for e in events:
            yield mx.sse(*e)
    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)
