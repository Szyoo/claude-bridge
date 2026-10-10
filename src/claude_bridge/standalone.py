"""`claude-bridge serve`: a complete little app — password login, the two routers, the reference widget."""

from __future__ import annotations

import html
import os
import re
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from claude_bridge.auth import COOKIE_NAME, PasswordAuth
from claude_bridge.server import create_bridge, static_dir
from claude_bridge.service import BridgeConfig
from claude_bridge.store import BridgeStore


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


SITE_ICONS = {
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}


def mount_site_icons(app: FastAPI) -> None:
    """Root-level favicon routes for the standalone site only (never auth-gated; embedding hosts keep their own icons)."""
    site = static_dir() / "site"
    for path, (name, media_type) in SITE_ICONS.items():
        def icon(name: str = name, media_type: str = media_type) -> FileResponse:
            return FileResponse(site / name, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})

        app.add_api_route(path, icon, methods=["GET"], include_in_schema=False)


# The pages' look comes from @szyyw/design, loaded from its CDN (https://design.szyyw.xyz/<tag>/, immutable per tag; no
# vendored copy): the templates have {{design_base}} / {{design_origin}} placeholders that render_page fills, and an import
# map "@szyyw/design/" → DESIGN_BASE so the static JS (corner-boot.js, bridge-pages.js) never names a version. The
# templates load tokens.css + components.css + corner-boot.js (mountChrome: 🌗 / appearance always, app switcher + account
# menu under portal SSO). Appearance is stored in the cb_theme / cb_palette / cb_scheme cookies (mountChrome cookiePrefix
# "cb_", @szyyw/design appearanceCookieNames) and rendered onto <html> here, so the first paint has the right scheme
# (DESIGN.md §2.1). The embedded widget never loads the design package.
APPEARANCE_COOKIE_PREFIX = "cb_"
# The one place the design package version lives (scripts/update-design.sh rewrites this line). Every design file of a
# page must come from the same version (the modules import each other relatively). Never /latest/.
DESIGN_VERSION = "v0.15.0"
DESIGN_CDN = "https://design.szyyw.xyz"


def design_base() -> str:
    """`DESIGN_BASE` env (e.g. a local CORS static server over a szyyw-design checkout, for offline work) or the CDN
    directory of DESIGN_VERSION. No trailing slash."""
    return (os.environ.get("DESIGN_BASE") or f"{DESIGN_CDN}/{DESIGN_VERSION}").rstrip("/")


def _origin(url: str) -> str:
    m = re.match(r"^[a-z][a-z0-9+.-]*://[^/]+", url, re.I)
    return m.group(0) if m else DESIGN_CDN


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
    """Put the appearance (and, under portal SSO, data-sso / data-portal for corner-boot.js) on `<html>`, and the design
    package location into the {{design_base}} / {{design_origin}} placeholders (links + import map)."""
    base = html.escape(design_base(), quote=True)
    page = page.replace("{{design_base}}", base).replace("{{design_origin}}", html.escape(_origin(base), quote=True))
    attrs = appearance_attrs(cookies)
    if portal is not None:
        attrs = f' data-sso="1" data-portal="{html.escape(portal, quote=True)}"' + attrs
    return page.replace('<html lang="zh-CN">', f'<html lang="zh-CN"{attrs}>', 1)


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
    client_tools_enabled: bool = False,
) -> FastAPI:
    store = BridgeStore(db_path)
    auth = PasswordAuth(password, secret, disabled=no_auth)
    if not auth.configured:
        raise RuntimeError("需要 CLAUDE_BRIDGE_PASSWORD（或 --no-auth）")
    cfg = config or BridgeConfig(
        new_thread_notice="新对话已开始，Claude 不再记得之前的内容。",
        files_dir=files_dir or Path(db_path).resolve().parent / "claude-bridge-files",
    )
    if client_tools_enabled:
        cfg.client_tools_enabled = True
    bridge = create_bridge(store=store, config=cfg, browser_auth=auth.dependency(), agent_token=agent_token)

    app = FastAPI(title="claude-bridge", docs_url=None, redoc_url=None)
    app.state.bridge = bridge
    app.state.auth = auth
    bridge.mount(app, browser_prefix="/api", agent_prefix="/api/agent", static_prefix="/static/bridge")
    pages = static_dir()
    mount_site_icons(app)

    def authed(request: Request) -> bool:
        return auth.verify_token(request.cookies.get(COOKIE_NAME))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not authed(request):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(render_page((pages / "index.html").read_text(encoding="utf-8"), request.cookies, None))

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, err: str = ""):
        if authed(request):
            return RedirectResponse("/", status_code=303)
        text = render_page((pages / "login.html").read_text(encoding="utf-8"), request.cookies, None)
        return HTMLResponse(text.replace("{{error}}", "口令不对" if err else ""))

    @app.post("/login")
    def login(request: Request, password: str = Form("")):
        ip = client_ip(request)
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
