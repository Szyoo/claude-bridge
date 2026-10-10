from __future__ import annotations

import io
import json
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from claude_bridge.client_tool_mcp import ToolRelay, serve
from claude_bridge.principal import Principal
from claude_bridge.server import BridgeConfig, create_bridge
from claude_bridge.store import BridgeStore

TOOL = {
    "name": "Echo",
    "description": "Executed by the caller, not the worker.",
    "input_schema": {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
}


@pytest.fixture
def runtime(tmp_path):
    def auth(request: Request):
        return Principal(owner=request.headers.get("x-user", "alice"))

    b = create_bridge(
        store=BridgeStore(tmp_path / "test.db"),
        config=BridgeConfig(client_tools_enabled=True),
        browser_auth=auth,
        agent_token="control-plane-secret",
    )
    app = FastAPI()
    b.mount(app, browser_prefix="/api", agent_prefix="/api/agent")
    with TestClient(app) as web:
        yield b, web


def start(runtime, tools=None):
    b, web = runtime
    r = web.post(
        "/api/send",
        json={
            "text": "Use Echo",
            "new_thread": True,
            "client_tools": tools or [TOOL],
            "client_environment": {"platform": "win32", "working_directory": "F:/project"},
        },
    )
    assert r.status_code == 201, r.text
    sent = r.json()
    job = b.store.claim_job("test-worker", ["chat"])
    assert job["id"] == sent["job_id"]
    scoped = web.post(
        f"/api/agent/jobs/{job['id']}/client-tool-session",
        headers={"Authorization": "Bearer control-plane-secret"},
        json={},
    )
    assert scoped.status_code == 200, scoped.text
    return sent, job, scoped.json()["token"]


def call(web, job, token, **override):
    payload = {"call_id": "call-1", "name": "Echo", "input": {"value": "hello"}, "timeout": 300, **override}
    return web.post(
        f"/api/agent/client-tools/{job['id']}/calls",
        json=payload,
        headers={"Authorization": "Bearer " + token},
    )


def test_handoff_preserves_scope_and_results_are_immutable(runtime):
    b, web = runtime
    sent, job, token = start(runtime)
    assert token not in b.store.get_meta(f"client-tool-token:{job['id']}")
    assert call(web, job, token).status_code == 200
    assert call(web, job, token).status_code == 200
    pending = web.get(f"/api/threads/{sent['thread']}/client-tools").json()["items"]
    assert len(pending) == 1 and pending[0]["input"] == {"value": "hello"}
    assert call(web, job, token, input={"value": "changed"}).status_code == 409
    path = f"/api/messages/{sent['message_id']}/client-tools/call-1/result"
    assert web.post(path, headers={"x-user": "bob"}, json={"content": "stolen"}).status_code == 404
    assert web.post(path, json={"content": "hello from caller"}).status_code == 200
    assert web.post(path, json={"content": "hello from caller"}).status_code == 200
    assert web.post(path, json={"content": "different"}).status_code == 409
    polled = web.get(
        f"/api/agent/client-tools/{job['id']}/calls/call-1", headers={"Authorization": "Bearer " + token}
    ).json()
    assert polled == {
        "status": "completed",
        "result": {"content": [{"type": "text", "text": "hello from caller"}], "isError": False},
    }


def test_scoped_token_cannot_access_other_jobs_or_control_plane(runtime):
    _, web = runtime
    _, job, token = start(runtime)
    headers = {"Authorization": "Bearer " + token}
    assert web.get(f"/api/agent/jobs/{job['id']}", headers=headers).status_code == 401
    assert web.get(f"/api/agent/client-tools/{job['id']}/definition", headers=headers).status_code == 200
    _, other, _ = start(runtime)
    assert web.get(f"/api/agent/client-tools/{other['id']}/definition", headers=headers).status_code == 404


def test_undeclared_invalid_arguments_and_remote_references_are_refused(runtime):
    _, web = runtime
    _, job, token = start(runtime)
    assert call(web, job, token, name="Bash").status_code == 400
    assert call(web, job, token, input={"value": 123}).status_code == 400
    assert call(web, job, token, input={"value": "x", "extra": True}).status_code == 400
    remote = {
        **TOOL,
        "input_schema": {"type": "object", "$ref": "https://must-not-be-fetched.invalid/schema"},
    }
    _, other, key = start(runtime, [remote])
    assert call(web, other, key).status_code == 400


def test_cancel_expiry_and_scope_rotation_invalidate_pending_calls(runtime):
    b, web = runtime
    sent, job, token = start(runtime)
    assert call(web, job, token).status_code == 200
    rotated = web.post(
        f"/api/agent/jobs/{job['id']}/client-tool-session",
        headers={"Authorization": "Bearer control-plane-secret"},
        json={},
    ).json()["token"]
    assert (
        web.get(
            f"/api/agent/client-tools/{job['id']}/definition", headers={"Authorization": "Bearer " + token}
        ).status_code
        == 404
    )
    assert web.get(f"/api/threads/{sent['thread']}/client-tools").json()["items"] == []
    assert (
        web.post(
            f"/api/messages/{sent['message_id']}/client-tools/call-1/result", json={"content": "late"}
        ).status_code
        == 409
    )
    assert call(web, job, rotated, call_id="call-2").status_code == 200
    b.store._x("UPDATE bridge_client_tools SET deadline=? WHERE job_id=?", (time.time() - 1, job["id"]))
    assert (
        web.post(
            f"/api/messages/{sent['message_id']}/client-tools/call-2/result", json={"content": "late"}
        ).status_code
        == 409
    )
    assert call(web, job, rotated, call_id="call-3").status_code == 200
    assert web.post(f"/api/messages/{sent['message_id']}/cancel").status_code == 200
    assert (
        web.get(
            f"/api/agent/client-tools/{job['id']}/calls/call-3",
            headers={"Authorization": "Bearer " + rotated},
        ).json()["status"]
        == "cancelled"
    )
    assert (
        web.post(
            f"/api/messages/{sent['message_id']}/client-tools/call-3/result", json={"content": "late"}
        ).status_code
        == 409
    )


def test_disabled_hosts_reject_before_creating_a_job(runtime):
    b, web = runtime
    b.config.client_tools_enabled = False
    r = web.post("/api/send", json={"text": "test", "client_tools": [TOOL]})
    assert r.status_code == 400
    assert b.store.count_jobs("queued") == 0


def test_deleted_threads_remove_tool_requests(runtime):
    b, web = runtime
    sent, job, token = start(runtime)
    call(web, job, token)
    assert web.delete(f"/api/threads/{sent['thread']}").status_code == 200
    assert b.store.client_tool_call(job["id"], "call-1") is None


def test_per_turn_controls_do_not_change_saved_settings(runtime):
    b, web = runtime
    saved = b.service.save_settings({"model": "haiku", "effort": "low"}, owner="alice")
    r = web.post(
        "/api/send",
        json={
            "text": "hello",
            "new_thread": True,
            "options": {
                "model": "sonnet",
                "effort": "high",
                "max_turns": 2,
                "auto_context": False,
                "max_output_tokens": 2048,
                "thinking": False,
                "instructions": "Concise answers.",
            },
        },
    )
    assert r.status_code == 201, r.text
    settings = b.store.get_job(r.json()["job_id"])["payload"]["settings"]
    assert settings["model"] == "sonnet" and settings["effort"] == "high"
    assert settings["instructions"] == "Concise answers." and settings["thinking"] is False
    assert b.service.settings("alice") == saved
    assert web.post("/api/send", json={"text": "test", "options": {"temperature": 0}}).status_code == 422
    assert (
        web.post("/api/send", json={"text": "test", "options": {"effort": "unsupported"}}).status_code == 400
    )


def test_stdio_protocol_does_not_reply_to_notifications_or_leak_errors():
    class Relay:
        def dispatch(self, method, params, request_id):
            if method == "ping":
                return {"text": "你好"}
            raise RuntimeError("private-secret-credential")

    source = io.StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "unknown"}),
            ]
        )
        + "\n"
    )
    out = io.StringIO()
    serve(Relay(), source, out)
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(rows) == 2 and rows[0]["result"] == {"text": "你好"}
    assert "private-secret" not in out.getvalue() and rows[1]["error"]["code"] == -32603


def test_mcp_relay_polls_existing_call_and_does_not_execute_inputs():
    class Relay(ToolRelay):
        requests = []

        def request(self, method, path, body=None):
            self.requests.append((method, path, body))
            if method == "POST":
                return {"call_id": body["call_id"]}
            return {
                "status": "completed",
                "result": {"content": [{"type": "text", "text": "local result"}], "isError": False},
            }

    relay = Relay("http://example.invalid", "/api/agent/client-tools", 1, "scoped-secret")
    result = relay.dispatch(
        "tools/call", {"name": "Echo", "arguments": {"value": "do not run this as a command"}}, 10
    )
    assert result["content"][0]["text"] == "local result"
    assert (
        len(relay.requests) == 2 and relay.requests[0][2]["input"]["value"] == "do not run this as a command"
    )
