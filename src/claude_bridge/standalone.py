"""`claude-bridge serve`: a complete little app — password login, the two routers, the reference widget."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from claude_bridge.auth import COOKIE_NAME, PasswordAuth
from claude_bridge.server import create_bridge, static_dir
from claude_bridge.service import BridgeConfig
from claude_bridge.store import BridgeStore


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def create_standalone_app(
    *,
    db_path: str | Path,
    password: str = "",
    secret: str = "",
    agent_token: str = "",
    no_auth: bool = False,
    cookie_secure: bool = False,
    config: BridgeConfig | None = None,
    files_dir: str | Path | None = None,
) -> FastAPI:
    store = BridgeStore(db_path)
    auth = PasswordAuth(password, secret, disabled=no_auth)
    if not auth.configured:
        raise RuntimeError("需要 CLAUDE_BRIDGE_PASSWORD（或 --no-auth）")
    cfg = config or BridgeConfig(
        new_thread_notice="新对话已开始，Claude 不再记得之前的内容。",
        files_dir=files_dir or Path(db_path).resolve().parent / "claude-bridge-files",
    )
    bridge = create_bridge(store=store, config=cfg, browser_auth=auth.dependency(), agent_token=agent_token)

    app = FastAPI(title="claude-bridge", docs_url=None, redoc_url=None)
    app.state.bridge = bridge
    app.state.auth = auth
    bridge.mount(app, browser_prefix="/api", agent_prefix="/api/agent", static_prefix="/static/bridge")
    pages = static_dir()

    def authed(request: Request) -> bool:
        return auth.verify_token(request.cookies.get(COOKIE_NAME))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not authed(request):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse((pages / "index.html").read_text(encoding="utf-8"))

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, err: str = ""):
        if authed(request):
            return RedirectResponse("/", status_code=303)
        html = (pages / "login.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("{{error}}", "口令不对" if err else ""))

    @app.post("/login")
    def login(request: Request, password: str = Form("")):
        ip = _client_ip(request)
        if auth.is_limited(ip):
            raise HTTPException(status_code=429, detail="尝试太多，一分钟后再试")
        if not auth.check_password(password):
            auth.record_failure(ip)
            return RedirectResponse("/login?err=1", status_code=303)
        auth.clear_failures(ip)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE_NAME, auth.issue_token(), max_age=auth.session_seconds, httponly=True, secure=cookie_secure,
            samesite="lax", path="/",
        )
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        if exc.status_code == 401 and not request.url.path.startswith("/api"):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    return app
