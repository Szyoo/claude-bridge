"""Controlled stdio MCP server: publishes calls, waits for caller-owned results.

Only a job-scoped token is available in this process. It never executes tool inputs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from typing import Any

import requests


class ToolRelay:
    def __init__(self, url: str, prefix: str, job_id: int, token: str, *, timeout: int = 300, http=None):
        self.url = f"{url.rstrip('/')}{prefix}/{job_id}"
        self.token = token
        self.timeout = timeout
        self.http = http or requests.Session()

    def request(self, method: str, path: str, body=None):
        response = self.http.request(
            method, self.url + path, json=body, headers={"Authorization": "Bearer " + self.token}, timeout=10
        )
        if response.status_code >= 400:
            raise RuntimeError("client tool session is unavailable")
        return response.json()

    def dispatch(self, method: str, params: dict, request_id: Any):
        if method == "initialize":
            version = params.get("protocolVersion", "2024-11-05")
            return {
                "protocolVersion": version
                if version in {"2024-11-05", "2025-03-26", "2025-06-18"}
                else "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "claude-bridge-client", "version": "1.0"},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            definition = self.request("GET", "/definition")
            return {
                "tools": [
                    {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "inputSchema": t["input_schema"],
                    }
                    for t in definition["tools"]
                ]
            }
        if method == "tools/call":
            call_id = hmac.new(
                self.token.encode(), json.dumps(request_id).encode(), hashlib.sha256
            ).hexdigest()
            self.request(
                "POST",
                "/calls",
                {
                    "call_id": call_id,
                    "name": params["name"],
                    "input": params.get("arguments", {}),
                    "timeout": self.timeout,
                },
            )
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                state = self.request("GET", "/calls/" + call_id)
                if state["status"] == "completed":
                    return state["result"]
                if state["status"] != "pending":
                    return {
                        "content": [{"type": "text", "text": "Client tool call was cancelled or expired."}],
                        "isError": True,
                    }
                time.sleep(0.25)
            return {"content": [{"type": "text", "text": "Client tool response timed out."}], "isError": True}
        raise ValueError("unsupported MCP method")


def serve(relay: ToolRelay, input_stream, output_stream) -> None:
    for line in input_stream:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            output_stream.write(
                json.dumps(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
                )
                + "\n"
            )
            output_stream.flush()
            continue
        if not isinstance(request, dict) or "id" not in request:
            continue  # notifications have no response
        try:
            result = relay.dispatch(request.get("method", ""), request.get("params") or {}, request["id"])
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except Exception:
            # Never put URLs, credentials or request parameters in protocol errors.
            response = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32603, "message": "Client tool request failed"},
            }
        output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
        output_stream.flush()


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    relay = ToolRelay(
        os.environ["CB_CLIENT_URL"],
        os.environ["CB_CLIENT_PREFIX"],
        int(os.environ["CB_CLIENT_JOB"]),
        os.environ["CB_CLIENT_TOKEN"],
        timeout=int(os.environ.get("CB_CLIENT_TIMEOUT", "300")),
    )
    serve(relay, sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
