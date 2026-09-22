"""`claude-bridge serve | worker | status` — env vars `CLAUDE_BRIDGE_*`, flags override."""

from __future__ import annotations

import argparse
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


def _split_tools(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-bridge", description="web chat → local claude -p bridge")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the standalone server (login + chat page + API)")
    s.add_argument("--host", default=env("HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(env("PORT", "8770")))
    s.add_argument("--db", default=env("DB", "claude-bridge.db"))
    s.add_argument("--no-auth", action="store_true", help="disable the password login (local debugging)")

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
    app = create_standalone_app(
        db_path=args.db,
        password=env("PASSWORD"),
        secret=env("SECRET"),
        agent_token=env("AGENT_TOKEN"),
        no_auth=args.no_auth,
        cookie_secure=cookie_secure,
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
    args = build_parser().parse_args(argv)
    return {"serve": cmd_serve, "worker": cmd_worker, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
