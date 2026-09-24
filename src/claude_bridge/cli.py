"""`claude-bridge serve | worker | status` — env vars `CLAUDE_BRIDGE_*`, flags override."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from claude_bridge.client import BridgeClient, BridgeClientError
from claude_bridge.worker import Worker, WorkerConfig


def env(name: str, default: str = "") -> str:
    return os.environ.get(f"CLAUDE_BRIDGE_{name}", default)


def load_env_file(path: str) -> None:
    """KEY=VALUE lines (# comments, optional quotes) into os.environ; variables already set win."""
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _pop_env_file(argv: list[str]) -> list[str]:
    """`--env-file PATH` anywhere on the line is applied before the parser reads its env defaults."""
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--env-file" and i + 1 < len(argv):
            load_env_file(argv[i + 1])
            i += 2
            continue
        if a.startswith("--env-file="):
            load_env_file(a.split("=", 1)[1])
        else:
            out.append(a)
        i += 1
    return out


def _split_tools(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-bridge", description="web chat → local claude -p bridge",
                                epilog="--env-file PATH (anywhere): read CLAUDE_BRIDGE_* from a KEY=VALUE file first")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the standalone server (login + chat page + API)")
    s.add_argument("--host", default=env("HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(env("PORT", "8770")))
    s.add_argument("--db", default=env("DB", "claude-bridge.db"))
    s.add_argument("--files", default=env("FILES", ""), help="where uploaded images are stored (default: next to the db)")
    s.add_argument("--no-auth", action="store_true", help="disable the password login (local debugging)")
    s.add_argument("--multi-user", action="store_true", default=env("MULTI_USER") == "1",
                   help="username + password accounts, per-user history, quotas, /admin (create users with `claude-bridge users`)")
    s.add_argument("--tz", default=env("TZ"), help="timezone for reset times in quota messages, e.g. Asia/Tokyo (default: system)")

    w = sub.add_parser("worker", help="run the machine-side worker that executes claude -p")
    w.add_argument("--once", action="store_true", help="handle at most one job, then exit")
    w.add_argument("--url", default=env("URL"))
    w.add_argument("--token", default=env("AGENT_TOKEN"))
    w.add_argument("--cwd", default=env("CWD"))
    w.add_argument("--model", default=env("MODEL"))
    w.add_argument("--claude-bin", default=env("CLAUDE_BIN", "claude"))
    w.add_argument("--max-turns", type=int, default=int(env("MAX_TURNS", "40")))
    w.add_argument("--allowed-tools", default=env("ALLOWED_TOOLS"), help='comma list, e.g. "Read,Glob,Bash(git log *)"')
    w.add_argument("--system-prompt", default=env("SYSTEM_PROMPT"))
    w.add_argument("--system-prompt-file", default=env("SYSTEM_PROMPT_FILE"))
    w.add_argument("--permission-mode", default=env("PERMISSION_MODE") or None)
    w.add_argument("--timeout", type=float, default=float(env("CHAT_TIMEOUT", "900")))
    w.add_argument("--no-partial", action="store_true", help="do not pass --include-partial-messages")
    w.add_argument("--worker-name", default=env("WORKER_NAME"))
    w.add_argument("-v", "--verbose", action="store_true")

    u = sub.add_parser("users", help="manage accounts for serve --multi-user (works on the database directly)")
    u.add_argument("--db", default=env("DB", "claude-bridge.db"))
    us = u.add_subparsers(dest="users_cmd", required=True)
    ua = us.add_parser("add", help="create an account")
    ua.add_argument("username")
    ua.add_argument("--admin", action="store_true")
    ua.add_argument("--display-name", default="")
    ua.add_argument("--password", help="default: prompt (or generate when stdin is not a terminal)")
    us.add_parser("list", help="list accounts")
    up = us.add_parser("passwd", help="set a new password (signs that user out everywhere)")
    up.add_argument("username")
    up.add_argument("--password", help="default: prompt (or generate when stdin is not a terminal)")
    ue = us.add_parser("enable", help="re-enable a disabled account")
    ue.add_argument("username")

    st = sub.add_parser("status", help="check the server and the local claude login")
    st.add_argument("--url", default=env("URL"))
    st.add_argument("--token", default=env("AGENT_TOKEN"))
    st.add_argument("--claude-bin", default=env("CLAUDE_BIN", "claude"))
    return p


def worker_from_args(args: argparse.Namespace) -> Worker:
    if not args.url or not args.token:
        raise SystemExit("需要 --url/--token（或 CLAUDE_BRIDGE_URL / CLAUDE_BRIDGE_AGENT_TOKEN）")
    system_prompt = args.system_prompt
    if args.system_prompt_file:
        system_prompt = Path(args.system_prompt_file).read_text(encoding="utf-8")
    cfg = WorkerConfig(
        claude_bin=args.claude_bin,
        cwd=args.cwd or None,
        model=args.model,
        max_turns=args.max_turns,
        allowed_tools=_split_tools(args.allowed_tools),
        system_prompt=system_prompt,
        permission_mode=args.permission_mode,
        chat_timeout=args.timeout,
        include_partial=not args.no_partial,
    )
    if args.worker_name:
        cfg.worker_name = args.worker_name
    return Worker(BridgeClient(args.url, args.token), cfg)


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("需要 pip install 'claude-bridge[serve]'", file=sys.stderr)
        return 1
    from claude_bridge.standalone import create_standalone_app

    local = args.host in ("127.0.0.1", "localhost", "::1")
    secure_env = env("COOKIE_SECURE")
    cookie_secure = (secure_env == "1") if secure_env else not local
    if args.multi_user:
        from claude_bridge.accounts import Accounts
        from claude_bridge.multiuser import create_multiuser_app
        from claude_bridge.store import BridgeStore

        store = BridgeStore(args.db)
        n = Accounts(store, secret=env("SECRET")).count(role="admin", active=True)
        store.close()
        if not n:
            print(f"还没有管理员账户，先运行：claude-bridge users --db {args.db} add <用户名> --admin", file=sys.stderr)
            return 1
        app = create_multiuser_app(
            db_path=args.db, agent_token=env("AGENT_TOKEN"), secret=env("SECRET"), cookie_secure=cookie_secure,
            files_dir=args.files or None, tz=args.tz,
        )
        print(f"claude-bridge serving on http://{args.host}:{args.port}  db={args.db}  [multi-user]")
        uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True)
        return 0
    app = create_standalone_app(
        db_path=args.db,
        password=env("PASSWORD"),
        secret=env("SECRET"),
        agent_token=env("AGENT_TOKEN"),
        no_auth=args.no_auth,
        cookie_secure=cookie_secure,
        files_dir=args.files or None,
    )
    print(f"claude-bridge serving on http://{args.host}:{args.port}  db={args.db}{'  [no auth]' if args.no_auth else ''}")
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True)
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = worker_from_args(args)
    if args.once:
        handled = worker.run_once(wait=5)
        print("handled one job" if handled else "no queued job")
        return 0
    worker.run_forever()
    return 0


def _ask_password(given: str | None) -> tuple[str, bool]:
    """(password, generated): the flag, else a prompt on a terminal, else a generated one to print."""
    from claude_bridge.accounts import generate_password

    if given:
        return given, False
    if sys.stdin.isatty():
        first = getpass.getpass("新密码（留空则自动生成）：")
        if not first:
            return generate_password(), True
        if getpass.getpass("再输一次：") != first:
            raise SystemExit("两次输入不一致")
        return first, False
    return generate_password(), True


def cmd_users(args: argparse.Namespace) -> int:
    from claude_bridge.accounts import Accounts
    from claude_bridge.errors import BridgeError
    from claude_bridge.store import BridgeStore

    store = BridgeStore(args.db)
    accounts = Accounts(store, secret=env("SECRET"))
    try:
        if args.users_cmd == "list":
            for u in accounts.users():
                flags = " [停用]" if u["disabled"] else ""
                print(f"{u['id']:>4}  {u['username']:<20} {u['role']:<6} {u['display_name'] or '':<16} "
                      f"最近 {u['last_seen_at'] or '—'}{flags}")
            return 0
        if args.users_cmd == "add":
            password, generated = _ask_password(args.password)
            first = accounts.count() == 0
            user = accounts.create(args.username, password, role="admin" if args.admin else "user",
                                   display_name=args.display_name)
            print(f"已创建 {user['username']}（{user['role']}）" + ("，原来的对话已归到这个账户" if first and args.admin else ""))
            if generated:
                print(f"初始密码：{password}")
            return 0
        user = accounts.by_name(args.username)
        if not user:
            print(f"没有用户 {args.username}", file=sys.stderr)
            return 1
        if args.users_cmd == "passwd":
            password, generated = _ask_password(args.password)
            accounts.set_password(user["id"], password)
            print(f"已重设 {user['username']} 的密码，其它设备上的登录已失效" + (f"\n新密码：{password}" if generated else ""))
        elif args.users_cmd == "enable":
            accounts.update(user["id"], {"disabled": False})
            print(f"已启用 {user['username']}")
        return 0
    except BridgeError as e:
        print(e.detail, file=sys.stderr)
        return 1
    finally:
        store.close()


def cmd_status(args: argparse.Namespace) -> int:
    rc = 0
    if args.url and args.token:
        try:
            st = BridgeClient(args.url, args.token).status()
            print(f"✅ server {args.url}: online={st.get('online')} worker={st.get('worker')} queued={st.get('queued')} running={st.get('running')}")
        except BridgeClientError as e:
            print(f"❌ server: {e}")
            rc = 1
    else:
        print("ℹ️  server check skipped (no url/token)")
    exe = shutil.which(args.claude_bin)
    if not exe:
        print(f"❌ claude binary not found: {args.claude_bin}")
        return 1
    for sub in (["--version"], ["auth", "status"]):
        try:
            r = subprocess.run([exe, *sub], capture_output=True, text=True, timeout=30)
            print(f"claude {' '.join(sub)}: {(r.stdout or r.stderr).strip()[:400]}")
        except (subprocess.SubprocessError, OSError) as e:
            print(f"❌ claude {' '.join(sub)}: {e}")
            rc = 1
    return rc


def main(argv: list[str] | None = None) -> int:
    argv = _pop_env_file(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(argv)
    return {"serve": cmd_serve, "worker": cmd_worker, "users": cmd_users, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
