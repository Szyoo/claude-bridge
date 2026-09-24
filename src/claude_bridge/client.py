"""HTTP client the worker uses to talk to the agent router."""

from __future__ import annotations

from typing import Any

import requests


class BridgeClientError(RuntimeError):
    pass


class BridgeClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        http: Any | None = None,
        timeout: float = 20.0,
        agent_prefix: str = "/api/agent",
    ) -> None:
        if not base_url or not token:
            raise BridgeClientError("bridge url / agent token 未配置")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.prefix = agent_prefix
        # requests.Session or a Starlette TestClient — both expose .request(method, url, ...)
        self.http = http or requests.Session()

    def _call(self, method: str, path: str, *, json: Any = None, timeout: float | None = None) -> Any:
        url = f"{self.base_url}{self.prefix}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        # a TestClient has no per-request timeout (and warns about the kwarg)
        extra = {"timeout": timeout or self.timeout} if isinstance(self.http, requests.Session) else {}
        try:
            resp = self.http.request(method, url, headers=headers, json=json, **extra)
        except requests.RequestException as e:
            raise BridgeClientError(f"bridge 不可达 {path}: {e}") from e
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text[:200]
            raise BridgeClientError(f"bridge {method} {path} HTTP {resp.status_code}: {detail}")
        return resp.json() if resp.content else None

    def get_file(self, file_id: str) -> tuple[bytes, str]:
        """Raw bytes + mime of an uploaded image (for handing it to claude)."""
        url = f"{self.base_url}{self.prefix}/files/{file_id}"
        extra = {"timeout": self.timeout * 3} if isinstance(self.http, requests.Session) else {}
        try:
            resp = self.http.request("GET", url, headers={"Authorization": f"Bearer {self.token}"}, **extra)
        except requests.RequestException as e:
            raise BridgeClientError(f"图片取不到 {file_id}: {e}") from e
        if resp.status_code >= 400:
            raise BridgeClientError(f"图片取不到 {file_id}: HTTP {resp.status_code}")
        return resp.content, (resp.headers.get("content-type") or "image/png").split(";")[0].strip()

    def model_candidates(self) -> list[str]:
        return (self._call("GET", "/models") or {}).get("candidates") or []

    def report_models(self, report: dict[str, Any]) -> None:
        self._call("POST", "/models", json=report)

    def report_limits(self, report: dict[str, Any]) -> None:
        self._call("POST", "/limits", json=report)

    def next_job(self, worker: str, kinds: list[str], wait: int = 25) -> dict[str, Any] | None:
        body = {"worker": worker, "kinds": kinds, "wait": wait}
        return (self._call("POST", "/jobs/next", json=body, timeout=wait + 15) or {}).get("job")

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return (self._call("GET", f"/jobs/{job_id}") or {}).get("job")

    def post_events(
        self,
        job_id: int,
        *,
        status: str | None = None,
        deltas: list[str] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if status:
            body["status"] = status
        if deltas:
            body["deltas"] = deltas
        if events:
            body["events"] = events
        return self._call("POST", f"/jobs/{job_id}/events", json=body) or {"ok": True, "cancel": False}

    def finish_job(
        self,
        job_id: int,
        *,
        ok: bool,
        result: str | None = None,
        error: str | None = None,
        error_kind: str | None = None,
        cancelled: bool = False,
        session_id: str | None = None,
        reset_session: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        self._call(
            "POST",
            f"/jobs/{job_id}/finish",
            json={
                "ok": ok,
                "cancelled": cancelled,
                "result": result,
                "error": error,
                "error_kind": error_kind,
                "session_id": session_id,
                "reset_session": reset_session,
                "context": context,
            },
        )

    def create_chat(
        self,
        text: str,
        *,
        scope: str = "",
        key: str = "",
        new_thread: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = {"text": text, "scope": scope, "key": key, "new_thread": new_thread, "payload": payload or {}}
        return self._call("POST", "/chat", json=body)

    def status(self) -> dict[str, Any]:
        return self._call("GET", "/status")
