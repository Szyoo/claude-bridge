"""FastAPI routers: one for the browser, one for the worker (agent)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from claude_bridge.auth import bearer_auth
from claude_bridge.broker import Broker
from claude_bridge.errors import BridgeError
from claude_bridge.models import (
    AgentChatIn,
    JobEventsIn,
    JobFinishIn,
    JobNextIn,
    SendIn,
    SettingsIn,
    ThreadIn,
    ThreadMessageIn,
    ThreadPatchIn,
    ThreadSelectIn,
)
from claude_bridge.service import BridgeConfig, BridgeService
from claude_bridge.sse import SSE_HEADERS, event_stream
from claude_bridge.store import BridgeStore

# uploads are immutable (a new upload gets a new id); nosniff so a browser never reinterprets the bytes
FILE_HEADERS = {"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"}


def static_dir() -> Path:
    return Path(__file__).parent / "static"


def _svc(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    try:
        return fn(*args, **kw)
    except BridgeError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


@dataclass
class Bridge:
    store: BridgeStore
    config: BridgeConfig
    broker: Broker
    service: BridgeService
    browser_router: APIRouter
    agent_router: APIRouter

    def mount(
        self,
        app: FastAPI,
        *,
        browser_prefix: str = "/api/bridge",
        agent_prefix: str = "/api/agent",
        static_prefix: str | None = "/static/bridge",
    ) -> None:
        app.include_router(self.browser_router, prefix=browser_prefix)
        app.include_router(self.agent_router, prefix=agent_prefix)
        if static_prefix:
            app.mount(static_prefix, StaticFiles(directory=str(static_dir())), name="bridge-static")


def create_bridge(
    *,
    store: BridgeStore,
    config: BridgeConfig | None = None,
    browser_auth: Callable[..., Any] | None = None,
    agent_auth: Callable[..., Any] | None = None,
    agent_token: str | Callable[[], str | None] | None = None,
) -> Bridge:
    config = config or BridgeConfig()
    broker = Broker()
    service = BridgeService(store, broker, config)
    if agent_auth is None:
        agent_auth = bearer_auth(agent_token or "")

    browser = APIRouter(dependencies=[Depends(browser_auth)] if browser_auth else [])
    agent = APIRouter(dependencies=[Depends(agent_auth)])

    # ---------------- browser ----------------

    @browser.get("/threads")
    def list_threads(scope: str = Query("")):
        current = _svc(service.current_thread, scope)
        return {"items": store.threads(scope), "current": current, "scope": scope}

    @browser.post("/threads", status_code=201)
    def create_thread(body: ThreadIn | None = None):
        body = body or ThreadIn()
        return {"thread": _svc(service.new_thread, body.scope, body.key, body.title)}

    @browser.get("/threads/find")
    def find_thread(scope: str = Query(""), key: str = Query(...)):
        return {"thread": service.find_thread(scope, key)}

    @browser.get("/threads/{thread_id}")
    def get_thread(thread_id: str):
        snap = _svc(service.snapshot, thread_id)
        return {k: snap[k] for k in ("thread", "messages", "inflight", "agent")}

    @browser.post("/threads/{thread_id}/select")
    def select_thread(thread_id: str, body: ThreadSelectIn | None = None):
        _svc(service.select_thread, (body.scope if body else ""), thread_id)
        return {"thread": thread_id}

    @browser.patch("/threads/{thread_id}")
    def patch_thread(thread_id: str, body: ThreadPatchIn):
        _svc(service.patch_thread, thread_id, title=body.title, pinned=body.pinned)
        return {"ok": True}

    @browser.delete("/threads/{thread_id}")
    def delete_thread(thread_id: str, scope: str | None = Query(None)):
        return _svc(service.delete_thread, thread_id, scope)

    @browser.get("/threads/{thread_id}/messages")
    def list_messages(
        thread_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        events: int = Query(1),
    ):
        if not store.get_thread(thread_id):
            raise HTTPException(status_code=404, detail="没有这个对话")
        items = store.messages(thread_id, after_id=after, limit=limit, with_events=bool(events), tail=after == 0)
        inflight = store.inflight(thread_id)
        return {"items": items, "inflight": inflight["id"] if inflight else None}

    @browser.post("/threads/{thread_id}/messages", status_code=201)
    def send_to_thread(thread_id: str, body: ThreadMessageIn):
        return _svc(service.start_chat, body.text, thread_id=thread_id, files=body.files)

    @browser.post("/send", status_code=201)
    def send(body: SendIn):
        return _svc(
            service.start_chat, body.text, scope=body.scope, key=body.key, new_thread=body.new_thread, files=body.files
        )

    # image upload: the request body is the image itself (no multipart dependency); ?name= is an optional label
    @browser.post("/files", status_code=201)
    async def upload_file(request: Request, name: str = Query("", max_length=80)):
        if not service.uploads_enabled:
            raise HTTPException(status_code=404, detail="未开启图片上传")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > config.max_file_bytes:
                raise HTTPException(status_code=413, detail=f"图片超过 {config.max_file_bytes / 1048576:.0f} MB 上限")
        return await run_in_threadpool(_svc, service.save_upload, bytes(data), name)

    def _file_response(file_id: str) -> FileResponse:
        path, mime = _svc(service.file_for_download, file_id)
        return FileResponse(path, media_type=mime, headers=FILE_HEADERS)

    @browser.get("/files/{file_id}")
    def get_file(file_id: str):
        return _file_response(file_id)

    @browser.get("/threads/{thread_id}/stream")
    async def stream(
        thread_id: str, request: Request, after: int = Query(0, ge=0), last_event_id: int = Query(0, ge=0)
    ):
        if not await run_in_threadpool(store.get_thread, thread_id):
            raise HTTPException(status_code=404, detail="没有这个对话")
        # browsers send the header on auto-reconnect; the query form is for hand-made reconnects
        raw = request.headers.get("last-event-id", "")
        last_event_id = int(raw) if raw.isdigit() else last_event_id
        gen = event_stream(
            service, broker, thread_id, after=after, last_event_id=last_event_id, heartbeat=config.heartbeat_seconds
        )
        return StreamingResponse(gen, media_type="text/event-stream", headers=SSE_HEADERS)

    @browser.post("/messages/{message_id}/cancel")
    def cancel(message_id: int):
        return _svc(service.cancel, message_id)

    @browser.post("/threads/{thread_id}/context", status_code=202)
    def refresh_context(thread_id: str):
        return _svc(service.request_session_job, thread_id, "context")

    @browser.post("/threads/{thread_id}/compact", status_code=202)
    def compact_thread(thread_id: str):
        return _svc(service.request_session_job, thread_id, "compact")

    @browser.get("/settings")
    def get_settings():
        return {
            "chat": service.settings(),
            "models": config.model_choices,
            "efforts": config.effort_choices,
            "agent": service.agent_status(),
            "uploads": {"enabled": service.uploads_enabled, "max_bytes": config.max_file_bytes,
                        "max_files": config.max_files_per_message},
        }

    @browser.put("/settings")
    def put_settings(body: SettingsIn):
        return {"chat": _svc(service.save_settings, body.model_dump(exclude_unset=True))}

    @browser.get("/jobs")
    def list_jobs(limit: int = Query(20, ge=1, le=200)):
        service.requeue_stale()
        return {"items": store.recent_jobs(limit), "agent": service.agent_status()}

    @browser.get("/jobs/{job_id}")
    def get_job(job_id: int):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="没有这个任务")
        return job

    @browser.get("/status")
    def status():
        return service.status()

    # ---------------- agent ----------------

    @agent.post("/jobs/next")
    def a_next_job(body: JobNextIn):
        return {"job": service.next_job(body.worker, body.kinds, body.wait)}

    @agent.get("/jobs/{job_id}")
    def a_get_job(job_id: int):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="没有这个任务")
        if job["status"] == "running":
            store.touch_job(job_id)
        return {"job": job}

    @agent.post("/jobs/{job_id}/events")
    def a_job_events(job_id: int, body: JobEventsIn):
        return _svc(service.apply_events, job_id, body)

    @agent.post("/jobs/{job_id}/finish")
    def a_job_finish(job_id: int, body: JobFinishIn):
        _svc(service.finish, job_id, body)
        return {"ok": True}

    @agent.post("/chat", status_code=201)
    def a_chat(body: AgentChatIn):
        r = _svc(service.create_chat_from_agent, body)
        return {k: r[k] for k in ("message_id", "job_id", "thread")}

    @agent.get("/status")
    def a_status():
        return service.status()

    @agent.get("/files/{file_id}")
    def a_get_file(file_id: str):
        return _file_response(file_id)

    return Bridge(
        store=store, config=config, broker=broker, service=service, browser_router=browser, agent_router=agent
    )
