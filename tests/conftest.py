from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from claude_bridge.server import BridgeConfig, create_bridge
from claude_bridge.store import BridgeStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def bridge(tmp_path):
    deleted: list[str] = []
    cfg = BridgeConfig(
        scopes=("stock", "fund", "review"),
        default_thread=lambda s: "main" if s in ("", "stock") else f"main-{s}",
        new_thread_notice="新对话已开始",
        heartbeat_seconds=0.2,
        on_thread_deleted=deleted.append,
        model_choices=[{"id": "", "label": "默认"}, {"id": "sonnet", "label": "Sonnet"}],
        model_aliases={"claude-sonnet-4-5": "sonnet"},
        files_dir=tmp_path / "files",
    )
    b = create_bridge(store=BridgeStore(tmp_path / "b.db"), config=cfg, agent_token="tok")
    b.deleted = deleted  # type: ignore[attr-defined]
    return b


@pytest.fixture
def app(bridge):
    a = FastAPI()
    bridge.mount(a, browser_prefix="/api", agent_prefix="/api/agent", static_prefix=None)
    return a


@pytest.fixture
def web(app):
    return TestClient(app)


@pytest.fixture
def agent(app):
    return TestClient(app, headers={"Authorization": "Bearer tok"})


def make_fake_claude(tmp_path: Path, lines: list[dict], *, exit_code: int = 0, sleep_after: float = 0, name: str = "claude") -> str:
    """Executable stand-in for `claude`: records argv / stdin / an env probe, prints canned stream-json lines.

    A pseudo-line ``{"__sleep__": seconds}`` pauses instead of printing; ``{"__stderr__": text}`` writes to stderr.
    """
    payload = json.dumps(lines, ensure_ascii=False)
    script = tmp_path / name
    script.write_text(
        f"""#!{sys.executable}
import json, os, sys, time
argv = json.dumps(sys.argv[1:], ensure_ascii=False)
if not os.path.exists({str(tmp_path / 'args.json')!r}):
    open({str(tmp_path / 'args.json')!r}, 'w').write(argv)   # first invocation (the chat command)
open({str(tmp_path / 'args.jsonl')!r}, 'a').write(argv + '\\n')  # every invocation, incl. the /context follow-up
if not os.path.exists({str(tmp_path / 'env.txt')!r}):
    open({str(tmp_path / 'env.txt')!r}, 'w').write(os.environ.get('BRIDGE_TEST_ENV', ''))
if not sys.stdin.isatty():
    try:
        data = sys.stdin.read()
    except Exception:
        data = ''
    if data:
        open({str(tmp_path / 'stdin.txt')!r}, 'w').write(data)
for line in json.loads({payload!r}):
    if '__sleep__' in line:
        time.sleep(line['__sleep__']); continue
    if '__stderr__' in line:
        print(line['__stderr__'], file=sys.stderr, flush=True); continue
    print(json.dumps(line, ensure_ascii=False), flush=True)
if {sleep_after!r}:
    time.sleep({sleep_after!r})
sys.exit({exit_code})
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)
