"""Job-scoped tool handoff. Tool execution belongs to the authenticated caller."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time

from claude_bridge.errors import BadRequest, ChatBusy, NotFound
from claude_bridge.schema_validation import SchemaValidationError, validate_schema


def validate_tools(tools: list[dict]) -> None:
    names = set()
    for tool in tools:
        if tool["name"] in names:
            raise BadRequest("duplicate client tool name")
        names.add(tool["name"])
        if tool["input_schema"].get("type") != "object":
            raise BadRequest("client tool input_schema must describe an object")
        if len(json.dumps(tool, ensure_ascii=False).encode()) > 100000:
            raise BadRequest("client tool declaration is too large")
    try:
        validate_schema({"schemas": [tool["input_schema"] for tool in tools]})
    except SchemaValidationError as exc:
        raise BadRequest(str(exc)) from exc


class ClientToolService:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    def job(self, job_id: int) -> dict:
        job = self.store.get_job(job_id)
        if not job or job["kind"] != "chat" or not job["payload"].get("client_tools"):
            raise NotFound("no client-tool job")
        if not self.store.get_thread(job["payload"]["thread"]):
            raise NotFound("thread was deleted")
        return job

    @staticmethod
    def active(job: dict) -> bool:
        return job["status"] == "running" and not job.get("cancel_requested")

    def open(self, job_id: int) -> dict:
        if not self.service.config.client_tools_enabled:
            raise BadRequest("client tools are disabled")
        job = self.job(job_id)
        if not self.active(job):
            raise ChatBusy("job is not running")
        token = secrets.token_urlsafe(32)
        with self.store.transaction():
            self.store._x(
                "UPDATE bridge_client_tools SET deadline=0 WHERE job_id=? AND result IS NULL", (job_id,)
            )
            self.store.set_meta(f"client-tool-token:{job_id}", hashlib.sha256(token.encode()).hexdigest())
        return {"token": token, "message_id": job["payload"]["message_id"]}

    def authorize(self, job_id: int, token: str) -> dict:
        if not self.service.config.client_tools_enabled:
            raise NotFound("client tools are disabled")
        digest = self.store.get_meta(f"client-tool-token:{job_id}")
        if not digest or not hmac.compare_digest(digest, hashlib.sha256(token.encode()).hexdigest()):
            raise NotFound("no client-tool session")
        return self.job(job_id)

    def definition(self, job_id: int, token: str) -> dict:
        job = self.authorize(job_id, token)
        if not self.active(job):
            raise ChatBusy("job is not running")
        return {
            "tools": job["payload"]["client_tools"],
            "environment": job["payload"].get("client_environment", {}),
        }

    def call(self, job_id: int, token: str, request: dict) -> dict:
        job = self.authorize(job_id, token)
        if not self.active(job):
            raise ChatBusy("job is not running")
        tool = next((t for t in job["payload"]["client_tools"] if t["name"] == request["name"]), None)
        if tool is None:
            raise BadRequest("unknown client tool")
        try:
            validate_schema({"schema": tool["input_schema"], "input": request["input"]})
        except SchemaValidationError as exc:
            raise BadRequest(str(exc)) from exc
        mid = job["payload"]["message_id"]
        timeout = request.pop("timeout")
        with self.store.transaction():
            job = self.authorize(job_id, token)
            if not self.active(job):
                raise ChatBusy("job is not running")
            existing = self.store.client_tool_call(job_id, request["call_id"])
            if existing:
                if existing["request"] != request:
                    raise ChatBusy("call id already has different arguments")
                return {"call_id": request["call_id"], "deadline": existing["deadline"]}
            deadline = time.time() + timeout
            self.store.add_client_tool_call(job_id, request["call_id"], mid, request, deadline)
            event = self.store.add_event(mid, "client_tool_call", {**request, "deadline": deadline})
        self.service._pub(job["payload"]["thread"], "event", event, event["id"])
        return {"call_id": request["call_id"], "deadline": deadline}

    def result(self, job_id: int, token: str, call_id: str) -> dict:
        job = self.authorize(job_id, token)
        call = self.store.client_tool_call(job_id, call_id)
        if not call:
            raise NotFound("no client tool call")
        if call["result"] is not None:
            return {"status": "completed", "result": call["result"]}
        status = "pending" if self.active(job) and call["deadline"] > time.time() else "cancelled"
        return {"status": status}

    def respond(self, message_id: int, call_id: str, body, owner: str) -> dict:
        msg = self.store.get_message(message_id, with_events=False)
        if not msg or msg["role"] != "assistant" or not msg.get("job_id"):
            raise NotFound("no client tool message")
        self.service.own_thread(msg["thread"], owner)
        job = self.job(msg["job_id"])
        content = [{"type": "text", "text": body.content}] if isinstance(body.content, str) else body.content
        result = {"content": content, "isError": body.is_error}
        if len(json.dumps(result, ensure_ascii=False).encode()) > 8000000:
            raise BadRequest("client tool result is too large")
        for block in content:
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                continue
            if block.get("type") == "image" and block.get("mimeType") in {
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
            }:
                try:
                    if isinstance(block.get("data"), str) and base64.b64decode(block["data"], validate=True):
                        continue
                except (ValueError, binascii.Error):
                    pass
            raise BadRequest("client tool result must contain MCP text or base64 raster image blocks")
        with self.store.transaction():
            call = self.store.client_tool_call(job["id"], call_id)
            if not call:
                raise NotFound("no client tool call")
            if call["result"] is not None:
                if call["result"] != result:
                    raise ChatBusy("tool result is immutable")
                return {"ok": True}
            job = self.job(job["id"])
            if not self.active(job) or call["deadline"] <= time.time():
                raise ChatBusy("tool call has expired or was cancelled")
            self.store.save_client_tool_result(job["id"], call_id, result)
        return {"ok": True}
