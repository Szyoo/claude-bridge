"""`claude-bridge serve --multi-user`: accounts with username + password, per-user threads / settings, admin page.

Sessions are long-lived on purpose (the audience is a few people the admin knows): a signed cookie good for
`session_days`, renewed whenever a page is opened, invalidated by a password change, a reset or a disable.

Portal SSO (`SZYYW_SSO=1`, alias `CLAUDE_BRIDGE_SSO=1`): the szyyw.xyz Caddy gate authenticates browsers and
injects `X-User` / `X-Role` / `X-Portal-Sub`; identity comes from those headers only and the session cookie is
ignored. Safe only behind that gate with no published port. `X-Portal-Sub` (the portal's stable id; `X-User` is
the renameable display name) → a `bridge_users` row via `Accounts.resolve_portal`; no sub = signed out;
`X-Role` decides admin access per request (the stored `role` column is not touched). Login / logout go to
`PORTAL_ORIGIN`; the local password login is off. `/api/agent/*` (Bearer) and `/api/health` are unchanged.
"""

from __future__ import annotations

import html
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from claude_bridge.accounts import Accounts, generate_password, public_user, verify_password
from claude_bridge.auth import COOKIE_NAME, LoginLimiter
from claude_bridge.errors import BridgeError
from claude_bridge.ipgeo import IpGeo
from claude_bridge.principal import Principal
from claude_bridge.server import create_bridge, static_dir
from claude_bridge.service import PROJECT_SCOPE_PREFIX, BridgeConfig
from claude_bridge.standalone import client_ip, mount_site_icons
from claude_bridge.store import BridgeStore

log = logging.getLogger(__name__)

NOT_PROVISIONED = "此账号尚未在 claude-bridge 开通，请联系管理员"
SSO_NO_PASSWORD = "门户登录模式下密码由门户管理"
_TRUE = ("1", "true", "yes", "on")


def _flag(*names: str) -> bool:
    return any(os.environ.get(n, "").strip().lower() in _TRUE for n in names)


# The pages' look comes from @szyyw/design (vendored under static/vendor/szyyw-design by scripts/update-design.sh): the
# templates load tokens.css + components.css + corner-boot.js (mountChrome: 🌗 / appearance always, app switcher + account
# menu under portal SSO). Appearance is stored in the cb_theme / cb_palette / cb_scheme cookies (mountChrome cookiePrefix
# "cb_", @szyyw/design appearanceCookieNames) and rendered onto <html> here, so the first paint has the right scheme
# (DESIGN.md §2.1). Standalone (single password) mode and the embedded widget never load the design package.
APPEARANCE_COOKIE_PREFIX = "cb_"
_SCHEMES = ("auto", "dark", "light")
_APPEARANCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def appearance_attrs(cookies: dict[str, str]) -> str:
    """`<html>` attributes from the appearance cookies, like @szyyw/design appearance-data's
    readAppearanceFromCookies + appearanceAttrs: defaults nebula / default palette (no data-palette) / dark.
    Theme / palette ids are only shape-checked (the option list lives in the package; an unknown id has no CSS block
    and the client resets it)."""
    def get(key: str) -> str:
        return unquote(cookies.get(f"{APPEARANCE_COOKIE_PREFIX}{key}", "")).strip()

    theme = get("theme") if _APPEARANCE_ID.match(get("theme")) else "nebula"
    palette = get("palette") if _APPEARANCE_ID.match(get("palette")) else "default"
    scheme = get("scheme") if get("scheme") in _SCHEMES else "dark"
    attrs = f' data-theme="{theme}"'
    if palette != "default":
        attrs += f' data-palette="{palette}"'
    return attrs + f' data-scheme="{scheme}"'


def render_page(page: str, cookies: dict[str, str], portal: str | None) -> str:
    """Put the appearance (and, under portal SSO, data-sso / data-portal for corner-boot.js) on `<html>`."""
    attrs = appearance_attrs(cookies)
    if portal is not None:
        attrs = f' data-sso="1" data-portal="{html.escape(portal, quote=True)}"' + attrs
    return page.replace('<html lang="zh-CN">', f'<html lang="zh-CN"{attrs}>', 1)


def sso_from_env() -> bool:
    """`SZYYW_SSO` (the platform-wide name, same as szyyw_auth.sso_enabled) or `CLAUDE_BRIDGE_SSO`."""
    return _flag("SZYYW_SSO", "CLAUDE_BRIDGE_SSO")


class NotProvisioned(Exception):
    """A gate identity with no bridge_users row (and autocreate off), or a disabled one."""

    def __init__(self, detail: str = NOT_PROVISIONED) -> None:
        super().__init__(detail)
        self.detail = detail


def public_base(request: Request) -> str:
    """scheme://host the browser used: X-Forwarded-Proto / -Host from Caddy, else the request itself."""
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if proto and host:
        return f"{proto}://{host}"
    return str(request.base_url).rstrip("/")

# the page's two modes: "" = Chat; "code:<project>" = Code in one of the user's project directories.
# The worker maps each scope to a profile (tools, permissions, per-user / per-project directory).
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


class PortalSubIn(BaseModel):
    portal_sub: str | None = Field(None, max_length=64)


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
    sso: bool | None = None,
    sso_autocreate: bool | None = None,
    portal_origin: str | None = None,
) -> FastAPI:
    """`sso` / `sso_autocreate` / `portal_origin` default to `SZYYW_SSO` (or `CLAUDE_BRIDGE_SSO`) /
    `SZYYW_SSO_AUTOCREATE` / `PORTAL_ORIGIN` (default https://szyyw.xyz)."""
    if sso is None:
        sso = sso_from_env()
    if sso_autocreate is None:
        sso_autocreate = _flag("SZYYW_SSO_AUTOCREATE")
    portal = (portal_origin or os.environ.get("PORTAL_ORIGIN") or "https://szyyw.xyz").rstrip("/")
    if sso:
        from szyyw_auth import identity_from_headers, login_url
    store = BridgeStore(db_path)
    accounts = Accounts(store, secret=secret, session_days=session_days, tz=tz)
    ipgeo = IpGeo(store)  # admin pages: where an IP is (app.state.ipgeo so tests can swap the fetcher)
    def scope_ok(scope: str, owner: str) -> bool:
        if scope == "":
            return True
        return scope.startswith(PROJECT_SCOPE_PREFIX) and bridge.service.project_ready(owner, scope[len(PROJECT_SCOPE_PREFIX):])

    cfg = config or BridgeConfig(
        scopes=scope_ok,
        projects=True,
        new_thread_notice="新对话已开始，Claude 不再记得之前的内容。",
        files_dir=files_dir or Path(db_path).resolve().parent / "claude-bridge-files",
    )
    cfg.check_quota = accounts.check_quota

    def sso_user(request: Request) -> dict[str, Any] | None:
        """The effective user for a gated request: the mapped row with `role` = X-Role (in memory only)."""
        cached = request.scope.get("bridge_sso_user", False)
        if cached is not False:
            return cached
        ident = identity_from_headers(request.headers.get)
        # the stable id, read directly: szyyw_auth falls back to X-User when it's missing, but a username-only
        # match is exactly what keying on the sub avoids — no X-Portal-Sub = not through the gate = signed out
        sub = (request.headers.get("X-Portal-Sub") or "").strip()
        user = None
        if ident is not None and sub:
            row, how = accounts.resolve_portal(sub, ident.user, ident.role, autocreate=sso_autocreate)
            if how == "adopted":
                log.warning("SSO: portal user %r (sub %s) adopted bridge_users id=%s (same username, portal_sub was empty)",
                            ident.user, sub, row["id"] if row else "?")
            elif how == "created":
                log.warning("SSO: portal user %r (sub %s) auto-created bridge_users id=%s username=%r",
                            ident.user, sub, row["id"] if row else "?", row["username"] if row else "?")
            if row is None:
                log.warning("SSO: portal user %r (sub %s) has no bridge_users row (autocreate %s) → 403",
                            ident.user, sub, "on" if sso_autocreate else "off")
                raise NotProvisioned()
            if row["disabled"]:
                raise NotProvisioned("这个账户已停用，请联系管理员")
            accounts.touch_seen(row["id"])
            # role and the portal's current display name are per request, in memory only — the stored row
            # (username is the data key) is never renamed to follow X-User
            user = {**row, "role": ident.role, "portal_user": ident.user}
        request.scope["bridge_sso_user"] = user
        return user

    def signed_in(request: Request) -> dict[str, Any] | None:
        user = sso_user(request) if sso else accounts.verify_token(request.cookies.get(COOKIE_NAME))
        if user and not request.scope.get("bridge_access_logged"):  # the access log (admin only), once per request
            request.scope["bridge_access_logged"] = True
            accounts.record_access(user, "visit", client_ip(request), request.headers.get("user-agent", ""))
        return user

    def portal_login(request: Request, path: str | None = None) -> RedirectResponse:
        """Unauthenticated browser under SSO: the portal's login, coming back to `path` (default: this URL)."""
        if path is None:
            path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(login_url(portal, public_base(request) + path), status_code=302)

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
    app.state.ipgeo = ipgeo
    bridge.mount(app, browser_prefix="/api", agent_prefix="/api/agent", static_prefix="/static/bridge")
    pages = static_dir()
    mount_site_icons(app)
    limiter = LoginLimiter()

    def set_session(resp: Response, user: dict[str, Any]) -> None:
        resp.set_cookie(
            COOKIE_NAME, accounts.issue_token(user), max_age=accounts.session_seconds, httponly=True,
            secure=cookie_secure, samesite="lax", path="/",
        )

    def page(request: Request, name: str, *, admin: bool = False) -> Response:
        user = signed_in(request)
        if not user:
            return portal_login(request) if sso else RedirectResponse("/login", status_code=303)
        if admin and user["role"] != "admin":
            return RedirectResponse("/", status_code=303)
        text = render_page((pages / name).read_text(encoding="utf-8"), request.cookies, portal if sso else None)
        if sso:
            return HTMLResponse(text)
        resp = HTMLResponse(text)
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
        if sso:  # back to the app's root, not /login itself (that would bounce between the two)
            return portal_login(request, "/")
        if signed_in(request):
            return RedirectResponse("/", status_code=303)
        text = render_page((pages / "users-login.html").read_text(encoding="utf-8"), request.cookies, None)
        text = text.replace("{{error}}", html.escape(LOGIN_ERRORS.get(err, "") if err else ""))
        return HTMLResponse(text.replace("{{username}}", html.escape(u[:32], quote=True)))

    @app.post("/login")
    def login(request: Request, username: str = Form(""), password: str = Form("")):
        if sso:
            raise HTTPException(status_code=403, detail="门户登录模式下本地密码登录已关闭")
        ip, name_key = client_ip(request), f"user:{username.strip().lower()}"
        back = f"&u={quote(username.strip()[:32])}" if username.strip() else ""
        if limiter.is_limited(ip) or limiter.is_limited(name_key):
            return RedirectResponse(f"/login?err=limited{back}", status_code=303)
        user = accounts.authenticate(username, password)
        if not user:
            limiter.record_failure(ip)
            limiter.record_failure(name_key)
            accounts.record_access(accounts.by_name(username), "login_failed", ip, request.headers.get("user-agent", ""),
                                   username=username.strip())
            return RedirectResponse(f"/login?err=1{back}", status_code=303)
        if user["disabled"]:
            return RedirectResponse(f"/login?err=disabled{back}", status_code=303)
        limiter.clear(ip)
        limiter.clear(name_key)
        accounts.record_access(user, "login", ip, request.headers.get("user-agent", ""))
        resp = RedirectResponse("/", status_code=303)
        set_session(resp, user)
        return resp

    @app.post("/logout")
    def logout():
        if sso:  # the local cookie is unused under SSO but cleared anyway; the real sign-out is at the portal
            resp = RedirectResponse(portal, status_code=303)
            resp.delete_cookie(COOKIE_NAME, path="/")
            return resp
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # ---------------- self-service ----------------

    @app.get("/api/me")
    def me(user: dict[str, Any] = Depends(api_user)):
        out = {"user": public_user(user), "usage": accounts.usage_for(user)}
        if sso:
            out["sso"] = {"portal": portal, "portal_user": user.get("portal_user"), "portal_sub": user.get("portal_sub")}
        return out

    @app.patch("/api/me")
    def patch_me(body: MeIn, user: dict[str, Any] = Depends(api_user)):
        updated = _svc(accounts.update, user["id"], body.model_dump(exclude_unset=True))
        return {"user": public_user(updated)}

    @app.post("/api/me/password")
    def change_password(body: PasswordIn, user: dict[str, Any] = Depends(api_user)):
        if sso:
            raise HTTPException(status_code=403, detail=SSO_NO_PASSWORD)
        if not verify_password(body.current, user["pw_hash"]):
            raise HTTPException(status_code=400, detail="当前密码不对")
        updated = _svc(accounts.set_password, user["id"], body.new)
        resp = JSONResponse({"ok": True})
        set_session(resp, updated)  # other devices are signed out; this one stays in
        return resp

    # ---------------- admin ----------------

    def user_row(u: dict[str, Any], account: dict[str, Any], last: dict[int, dict[str, Any]] | None = None,
                 geo: dict[str, Any] | None = None) -> dict[str, Any]:
        a = (last if last is not None else accounts.last_access()).get(u["id"])
        if a and geo is None:
            geo = ipgeo.lookup([a["ip"]])
        return {**public_user(u, admin_view=True), "usage": accounts.usage_for(u, account),
                "last_access": {**{k: a[k] for k in ("ip", "user_agent", "last_at", "kind")}, "geo": (geo or {}).get(a["ip"])}
                if a else None}

    # ---------------- admin: a user's access log and conversations (read only) ----------------
    # Nothing here writes: no current-thread change, no updated_at touch, no last_seen — the user can't tell.

    def target_user(uid: int) -> dict[str, Any]:
        u = accounts.get(uid)
        if not u:
            raise HTTPException(status_code=404, detail="没有这个用户")
        return u

    @app.get("/admin/users/{uid}", response_class=HTMLResponse)
    def admin_user_page(request: Request, uid: int):
        return page(request, "admin-user.html", admin=True)

    @app.get("/api/admin/users/{uid}/access")
    def admin_user_access(uid: int, limit: int = 200, admin: dict[str, Any] = Depends(api_admin)):
        target_user(uid)
        items = accounts.access_log(uid, max(1, min(limit, 1000)))
        geo = ipgeo.lookup(a["ip"] for a in items)
        return {"items": [{**a, "geo": geo.get(a["ip"])} for a in items]}

    @app.get("/api/admin/users/{uid}/threads")
    def admin_user_threads(uid: int, admin: dict[str, Any] = Depends(api_admin)):
        u = target_user(uid)
        return {"user": public_user(u, admin_view=True), "items": store.threads(None, str(uid)),
                "projects": store.projects(str(uid))}

    @app.get("/api/admin/users/{uid}/threads/{thread_id}/messages")
    def admin_user_messages(uid: int, thread_id: str, limit: int = 500, admin: dict[str, Any] = Depends(api_admin)):
        target_user(uid)
        th = store.get_thread(thread_id)
        if not th or th.get("owner") != str(uid):
            raise HTTPException(status_code=404, detail="没有这个对话")
        return {"thread": th, "items": store.messages(thread_id, limit=max(1, min(limit, 2000)), tail=True)}

    @app.get("/api/admin/files/{file_id}")
    def admin_file(file_id: str, admin: dict[str, Any] = Depends(api_admin)):
        path, mime = _svc(bridge.service.file_for_download, file_id, None)
        return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/admin/users")
    def admin_users(admin: dict[str, Any] = Depends(api_admin)):
        account = accounts.account_status()
        last = accounts.last_access()
        geo = ipgeo.lookup(a["ip"] for a in last.values())
        out = {"items": [user_row(u, account, last, geo) for u in accounts.users()], "account": account,
               "quota": accounts.quota_config(), "me": admin["id"]}
        if sso:
            out["sso"] = {"portal": portal, "autocreate": sso_autocreate, "me_portal_user": admin.get("portal_user")}
        return out

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

    # portal SSO mapping: which portal account (its stable id, X-Portal-Sub) acts as this row. Works with SSO
    # off too, so the mapping can be prepared before the gate is switched on. Body {"portal_sub": "<id>"} or
    # null to clear.
    @app.post("/api/admin/users/{uid}/portal-user")
    def admin_portal_user(uid: int, body: PortalSubIn, admin: dict[str, Any] = Depends(api_admin)):
        user = _svc(accounts.set_portal_sub, uid, body.portal_sub)
        log.warning("portal_sub of bridge_users id=%s set to %r by %s", uid, user["portal_sub"], admin["username"])
        return {"user": user_row(user, accounts.account_status())}

    @app.put("/api/admin/quota")
    def admin_quota(body: QuotaIn, admin: dict[str, Any] = Depends(api_admin)):
        quota = _svc(accounts.save_quota_config, body.model_dump(exclude_unset=True))
        return {"quota": quota, "account": accounts.account_status()}

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        if exc.status_code == 401 and not request.url.path.startswith("/api"):
            return portal_login(request) if sso else RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(NotProvisioned)
    async def not_provisioned(request: Request, exc: NotProvisioned):
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": exc.detail}, status_code=403)
        body = (f'<!doctype html><meta charset="utf-8"><title>claude-bridge</title>'
                f'<p style="font:16px system-ui;margin:3em auto;max-width:32em">{html.escape(exc.detail)}</p>'
                f'<p style="font:14px system-ui;margin:0 auto;max-width:32em"><a href="{html.escape(portal, quote=True)}">回到门户</a></p>')
        return HTMLResponse(body, status_code=403)

    return app
