"""`claude-bridge serve --multi-user`: accounts with username + password, per-user threads / settings, admin page.

Sessions are long-lived on purpose (the audience is a few people the admin knows): a signed cookie good for
`session_days`, renewed whenever a page is opened, invalidated by a password change, a reset or a disable.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from claude_bridge.accounts import Accounts, generate_password, public_user, verify_password
from claude_bridge.auth import COOKIE_NAME, LoginLimiter
from claude_bridge.errors import BridgeError
from claude_bridge.principal import Principal
from claude_bridge.server import create_bridge, static_dir
from claude_bridge.service import BridgeConfig
from claude_bridge.standalone import client_ip
from claude_bridge.store import BridgeStore

# the page's two modes; the worker maps each scope to a profile (tools, permissions, per-user directory)
SCOPES = ("", "code")  # "" = Chat, "code" = Code
RENEW_AFTER = 86400  # a page load reissues the cookie once it is a day old: active users never see the login again

LOGIN_ERRORS = {
    "1": "用户名或密码不对",
    "disabled": "这个账户已停用，请联系管理员",
    "limited": "尝试太多次，一分钟后再试",
}


class MeIn(BaseModel):
    username: str | None = Field(None, max_length=32)
    display_name: str | None = Field(None, max_length=40)


class PasswordIn(BaseModel):
    current: str = Field("", max_length=200)
    new: str = Field(min_length=1, max_length=200)


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    display_name: str = Field("", max_length=40)
    role: str = "user"
    password: str = Field("", max_length=200)  # empty = generate one and show it once
    limit_5h_pct: float | None = None
    limit_7d_pct: float | None = None
    note: str = Field("", max_length=200)


class UserPatchIn(BaseModel):
    username: str | None = Field(None, max_length=32)
    display_name: str | None = Field(None, max_length=40)
    role: str | None = None
    disabled: bool | None = None
    limit_5h_pct: float | None = None
    limit_7d_pct: float | None = None
    note: str | None = Field(None, max_length=200)


class ResetIn(BaseModel):
    password: str = Field("", max_length=200)


class QuotaIn(BaseModel):
    cap_5h_usd: float | None = None
    cap_7d_usd: float | None = None
    guard_5h_pct: float | None = None
    guard_7d_pct: float | None = None


def _svc(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    try:
        return fn(*args, **kw)
    except BridgeError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


def create_multiuser_app(
    *,
    db_path: str | Path,
    agent_token: str = "",
    secret: str = "",
    cookie_secure: bool = False,
    config: BridgeConfig | None = None,
    files_dir: str | Path | None = None,
    session_days: int = 365,
    tz: str = "",
) -> FastAPI:
    store = BridgeStore(db_path)
    accounts = Accounts(store, secret=secret, session_days=session_days, tz=tz)
    cfg = config or BridgeConfig(
        scopes=SCOPES,
        new_thread_notice="新对话已开始，Claude 不再记得之前的内容。",
        files_dir=files_dir or Path(db_path).resolve().parent / "claude-bridge-files",
    )
    cfg.check_quota = accounts.check_quota

    def signed_in(request: Request) -> dict[str, Any] | None:
        return accounts.verify_token(request.cookies.get(COOKIE_NAME))

    def browser_auth(request: Request) -> Principal:
        user = signed_in(request)
        if not user:
            raise HTTPException(status_code=401, detail="未登录")
        return accounts.principal(user)

    bridge = create_bridge(store=store, config=cfg, browser_auth=browser_auth, agent_token=agent_token)
    accounts.service = bridge.service

    app = FastAPI(title="claude-bridge", docs_url=None, redoc_url=None)
    app.state.bridge = bridge
    app.state.accounts = accounts
    bridge.mount(app, browser_prefix="/api", agent_prefix="/api/agent", static_prefix="/static/bridge")
    pages = static_dir()
    limiter = LoginLimiter()

    def set_session(resp: Response, user: dict[str, Any]) -> None:
        resp.set_cookie(
            COOKIE_NAME, accounts.issue_token(user), max_age=accounts.session_seconds, httponly=True,
            secure=cookie_secure, samesite="lax", path="/",
        )

    def page(request: Request, name: str, *, admin: bool = False) -> Response:
        user = signed_in(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        if admin and user["role"] != "admin":
            return RedirectResponse("/", status_code=303)
        resp = HTMLResponse((pages / name).read_text(encoding="utf-8"))
        age = accounts.token_age(request.cookies.get(COOKIE_NAME))
        if age is not None and age > RENEW_AFTER:
            set_session(resp, user)
        return resp

    def api_user(request: Request) -> dict[str, Any]:
        user = signed_in(request)
        if not user:
            raise HTTPException(status_code=401, detail="未登录")
        return user

    def api_admin(user: dict[str, Any] = Depends(api_user)) -> dict[str, Any]:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return user

    # ---------------- pages ----------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return page(request, "app.html")

    @app.get("/account", response_class=HTMLResponse)
    def account_page(request: Request):
        return page(request, "account.html")

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request):
        return page(request, "admin.html", admin=True)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, err: str = "", u: str = ""):
        if signed_in(request):
            return RedirectResponse("/", status_code=303)
        text = (pages / "users-login.html").read_text(encoding="utf-8")
        text = text.replace("{{error}}", html.escape(LOGIN_ERRORS.get(err, "") if err else ""))
        return HTMLResponse(text.replace("{{username}}", html.escape(u[:32], quote=True)))

    @app.post("/login")
    def login(request: Request, username: str = Form(""), password: str = Form("")):
        ip, name_key = client_ip(request), f"user:{username.strip().lower()}"
        back = f"&u={quote(username.strip()[:32])}" if username.strip() else ""
        if limiter.is_limited(ip) or limiter.is_limited(name_key):
            return RedirectResponse(f"/login?err=limited{back}", status_code=303)
        user = accounts.authenticate(username, password)
        if not user:
            limiter.record_failure(ip)
            limiter.record_failure(name_key)
            return RedirectResponse(f"/login?err=1{back}", status_code=303)
        if user["disabled"]:
            return RedirectResponse(f"/login?err=disabled{back}", status_code=303)
        limiter.clear(ip)
        limiter.clear(name_key)
        resp = RedirectResponse("/", status_code=303)
        set_session(resp, user)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # ---------------- self-service ----------------

    @app.get("/api/me")
    def me(user: dict[str, Any] = Depends(api_user)):
        return {"user": public_user(user), "usage": accounts.usage_for(user)}

    @app.patch("/api/me")
    def patch_me(body: MeIn, user: dict[str, Any] = Depends(api_user)):
        updated = _svc(accounts.update, user["id"], body.model_dump(exclude_unset=True))
        return {"user": public_user(updated)}

    @app.post("/api/me/password")
    def change_password(body: PasswordIn, user: dict[str, Any] = Depends(api_user)):
        if not verify_password(body.current, user["pw_hash"]):
            raise HTTPException(status_code=400, detail="当前密码不对")
        updated = _svc(accounts.set_password, user["id"], body.new)
        resp = JSONResponse({"ok": True})
        set_session(resp, updated)  # other devices are signed out; this one stays in
        return resp

    # ---------------- admin ----------------

    def user_row(u: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        return {**public_user(u, admin_view=True), "usage": accounts.usage_for(u, account)}

    @app.get("/api/admin/users")
    def admin_users(admin: dict[str, Any] = Depends(api_admin)):
        account = accounts.account_status()
        return {"items": [user_row(u, account) for u in accounts.users()], "account": account,
                "quota": accounts.quota_config(), "me": admin["id"]}

    @app.post("/api/admin/users", status_code=201)
    def admin_create(body: UserIn, admin: dict[str, Any] = Depends(api_admin)):
        password = body.password or generate_password()
        user = _svc(accounts.create, body.username, password, role=body.role, display_name=body.display_name,
                    limit_5h_pct=body.limit_5h_pct, limit_7d_pct=body.limit_7d_pct, note=body.note)
        return {"user": user_row(user, accounts.account_status()), "password": None if body.password else password}

    @app.patch("/api/admin/users/{uid}")
    def admin_patch(uid: int, body: UserPatchIn, admin: dict[str, Any] = Depends(api_admin)):
        user = _svc(accounts.update, uid, body.model_dump(exclude_unset=True), acting=admin)
        return {"user": user_row(user, accounts.account_status())}

    @app.post("/api/admin/users/{uid}/password")
    def admin_reset(uid: int, body: ResetIn | None = None, admin: dict[str, Any] = Depends(api_admin)):
        given = body.password if body else ""
        password = given or generate_password()
        _svc(accounts.set_password, uid, password)
        return {"password": None if given else password}

    @app.delete("/api/admin/users/{uid}")
    def admin_delete(uid: int, admin: dict[str, Any] = Depends(api_admin)):
        names = _svc(accounts.delete, uid, acting=admin)
        bridge.service._unlink(names)
        return {"ok": True}

    @app.put("/api/admin/quota")
    def admin_quota(body: QuotaIn, admin: dict[str, Any] = Depends(api_admin)):
        quota = _svc(accounts.save_quota_config, body.model_dump(exclude_unset=True))
        return {"quota": quota, "account": accounts.account_status()}

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        if exc.status_code == 401 and not request.url.path.startswith("/api"):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    return app
