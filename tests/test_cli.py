import json

import pytest

from claude_bridge import cli
from conftest import make_fake_claude


def test_worker_args_env_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_BRIDGE_URL", "http://env")
    monkeypatch.setenv("CLAUDE_BRIDGE_AGENT_TOKEN", "envtok")
    monkeypatch.setenv("CLAUDE_BRIDGE_ALLOWED_TOOLS", "Read, Glob ,Bash(git log *)")
    monkeypatch.setenv("CLAUDE_BRIDGE_MAX_TURNS", "12")
    monkeypatch.setenv("CLAUDE_BRIDGE_CWD", str(tmp_path))
    spf = tmp_path / "sp.txt"
    spf.write_text("系统提示", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_BRIDGE_SYSTEM_PROMPT_FILE", str(spf))

    args = cli.build_parser().parse_args(["worker", "--model", "sonnet", "--no-partial"])
    w = cli.worker_from_args(args)
    assert w.client.base_url == "http://env" and w.client.token == "envtok"
    c = w.config
    assert c.allowed_tools == ["Read", "Glob", "Bash(git log *)"] and c.max_turns == 12 and c.model == "sonnet"
    assert c.system_prompt == "系统提示" and c.include_partial is False and str(c.cwd) == str(tmp_path)
    assert c.permission_mode is None

    args = cli.build_parser().parse_args(["worker", "--url", "http://flag", "--token", "t", "--permission-mode", "plan", "--timeout", "60"])
    w = cli.worker_from_args(args)
    assert w.client.base_url == "http://flag" and w.config.permission_mode == "plan" and w.config.chat_timeout == 60


def test_worker_requires_url_and_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_BRIDGE_URL", raising=False)
    monkeypatch.delenv("CLAUDE_BRIDGE_AGENT_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        cli.worker_from_args(cli.build_parser().parse_args(["worker"]))


def test_status_with_fake_claude_and_unreachable_server(tmp_path, capsys):
    fake = make_fake_claude(tmp_path, [{"type": "system", "subtype": "version", "v": "2.1.266"}])
    rc = cli.main(["status", "--url", "http://127.0.0.1:1", "--token", "x", "--claude-bin", fake])
    out = capsys.readouterr().out
    assert rc == 1 and "❌ server" in out and "claude --version" in out and "claude auth status" in out
    assert json.loads((tmp_path / "args.json").read_text()) == ["auth", "status"]

    rc = cli.main(["status", "--claude-bin", fake])
    assert rc == 0 and "skipped" in capsys.readouterr().out
    assert cli.main(["status", "--claude-bin", str(tmp_path / "nope")]) == 1


def test_parser_defaults(monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_PORT", "9000")
    a = cli.build_parser().parse_args(["serve"])
    assert a.port == 9000 and a.host == "127.0.0.1" and a.db == "claude-bridge.db" and a.no_auth is False
