"""Per-user sandboxes: each owner's space, the Seatbelt rules (exercised for real on macOS), session adoption."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claude_bridge import sandbox
from claude_bridge.worker import Worker, WorkerConfig

macos = pytest.mark.skipif(sys.platform != "darwin" or not sandbox.available(), reason="needs macOS sandbox-exec")


def test_tenant_space_layout_and_profile_outside_it(tmp_path):
    t = sandbox.tenant(tmp_path / "tenants", "3")
    assert t.work.is_dir() and t.home.is_dir() and t.tmp.is_dir() and t.config_dir.is_dir()
    assert t.profile.exists() and not t.profile.resolve().is_relative_to(t.root.resolve())
    assert oct(t.root.stat().st_mode & 0o777) == "0o700"
    assert t.cwd_for("") == t.work / "chat" and t.cwd_for("code:my app") == t.work / "code" / "my_app"
    assert t.cwd_for("api") == t.work / "api"
    with pytest.raises(ValueError):
        sandbox.tenant(tmp_path / "tenants", "")


def test_environment_is_rebuilt_from_an_allow_list(tmp_path):
    t = sandbox.tenant(tmp_path / "t", "3")
    env = t.env({"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "x", "CLAUDE_BRIDGE_AGENT_TOKEN": "secret",
                 "CLAUDE_BRIDGE_BACKENDS": "{}", "SSH_AUTH_SOCK": "/tmp/s", "HOME": "/Users/host", "AWS_SECRET_ACCESS_KEY": "k"})
    assert env["HOME"] == str(t.home) and env["CLAUDE_CONFIG_DIR"] == str(t.config_dir) and env["CLAUDE_CODE_TMPDIR"] == str(t.tmp)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in env  # claude itself needs it; the CLI keeps it from its tools
    for gone in ("CLAUDE_BRIDGE_AGENT_TOKEN", "CLAUDE_BRIDGE_BACKENDS", "SSH_AUTH_SOCK", "AWS_SECRET_ACCESS_KEY"):
        assert gone not in env
    assert t.wrap(["claude", "-p"])[:3] == [sandbox.SANDBOX_EXEC, "-f", str(t.profile)]


def test_a_session_from_before_sandboxing_is_adopted(tmp_path):
    host = tmp_path / "host-projects"
    (host / "-old-cwd").mkdir(parents=True)
    (host / "-old-cwd" / "S1.jsonl").write_text('{"x":1}\n')
    (host / "-old-cwd" / "S1" / "subagents").mkdir(parents=True)
    t = sandbox.tenant(tmp_path / "t", "3")
    cwd = t.cwd_for("api")
    assert sandbox.adopt_session(t, "S1", cwd, host)
    dest = t.projects / sandbox.project_dir_name(cwd)
    assert (dest / "S1.jsonl").read_text() == '{"x":1}\n' and (dest / "S1" / "subagents").is_dir()
    assert not sandbox.adopt_session(t, "S1", cwd, host)  # already there
    assert not sandbox.adopt_session(t, "nope", cwd, host)


def test_the_note_says_where_tools_run():
    t = sandbox.Tenant("3", Path("/srv/t/3"), Path("/srv/t/.profiles/3.sb"))
    note = sandbox.environment_note(t, t.cwd_for("api"), client_tools=True, client_environment={"os": "Windows 11"})
    assert "not on the user's computer" in note and "/srv/t/3/work/api" in note
    assert "mcp__bridge_client__" in note and "Windows 11" in note and "do not share files" in note
    assert "mcp__bridge_client__" not in sandbox.environment_note(t, t.cwd_for(""), client_tools=False)


def test_worker_confines_marked_jobs_only(tmp_path):
    w = Worker(client=None, config=WorkerConfig(cwd=str(tmp_path / "work")))  # type: ignore[arg-type]
    plain = w.config_for({"scope": "api", "owner": "1"})
    assert plain.tenant is None
    if not sandbox.available():
        with pytest.raises(RuntimeError):
            w.config_for({"scope": "api", "owner": "3", "sandbox": True})
        return
    cfg = w.config_for({"scope": "api", "owner": "3", "sandbox": True})
    assert cfg.tenant is not None and Path(cfg.cwd) == cfg.tenant.work / "api"
    assert cfg.tenant.root.parent == (tmp_path / "work" / ".tenants").resolve()
    assert cfg.local_usage_root == cfg.tenant.projects and cfg.permission_mode == "bypassPermissions"
    cmd, env = w.launch(cfg, ["claude", "-p", "hi"], {"PATH": "/usr/bin", "CLAUDE_BRIDGE_AGENT_TOKEN": "s"})
    assert cmd[0] == sandbox.SANDBOX_EXEC and "CLAUDE_BRIDGE_AGENT_TOKEN" not in env
    assert str(tmp_path / "work" / ".tenants") in w.bridge_roots()


@macos
def test_the_sandbox_holds(tmp_path):
    """Run a shell inside one tenant's sandbox and try to reach the host, another tenant and the network."""
    root = tmp_path / "tenants"
    me, other = sandbox.tenant(root, "8"), sandbox.tenant(root, "9")
    (other.work / "secret.txt").write_text("other tenant")
    outside = tmp_path / "host-file.txt"
    outside.write_text("host")
    (me.work / "mine.txt").write_text("mine")
    probe = me.work / "probe.sh"
    probe.write_text(f"""
chk() {{ if eval "$2" >/dev/null 2>&1; then echo "$1 yes"; else echo "$1 no"; fi; }}
chk own_read "cat {me.work}/mine.txt"
chk own_write "echo x > {me.work}/new.txt"
chk home_cache "mkdir -p $HOME/.cache/x"
chk python "python3 -c 1"
chk other_tenant "cat {other.work}/secret.txt"
chk host_file "cat {outside}"
chk host_home "ls {Path.home()}"
chk host_claude "ls {Path.home()}/.claude"
chk tmp "ls /private/tmp"
chk listen "python3 -c 'import socket;s=socket.socket();s.bind((\\"0.0.0.0\\",0));s.listen()'"
chk localhost "python3 -c 'import socket;socket.create_connection((\\"127.0.0.1\\",9),timeout=2)'"
chk signal_parent "kill -0 {__import__('os').getpid()}"
""")
    env = me.env({"PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
    out = subprocess.run(me.wrap(["/bin/bash", str(probe)]), cwd=me.work, env=env, capture_output=True, text=True, timeout=60,
                         start_new_session=True)  # as the worker starts claude
    got = dict(line.split() for line in out.stdout.splitlines() if line.strip())
    assert {k: got.get(k) for k in ("own_read", "own_write", "home_cache", "python")} == dict.fromkeys(
        ("own_read", "own_write", "home_cache", "python"), "yes"), out.stderr
    denied = ("other_tenant", "host_file", "host_home", "host_claude", "tmp", "listen", "localhost", "signal_parent")
    assert {k: got.get(k) for k in denied} == dict.fromkeys(denied, "no"), (got, out.stderr)
    assert json.loads(json.dumps(got))  # plain report


@macos
def test_parents_of_allowed_paths_can_be_resolved_but_not_listed(tmp_path):
    me = sandbox.tenant(tmp_path / "tenants", "8")
    docs = Path.home() / "Documents"
    probe = me.work / "p.sh"
    probe.write_text(f'test -d "{docs}" && echo stat_ok; ls "{docs}" >/dev/null 2>&1 && echo list_ok; '
                     f'{sys.executable} -I -c "import claude_bridge" && echo import_ok\n')
    out = subprocess.run(me.wrap(["/bin/bash", str(probe)]), cwd=me.work, env=me.env({"PATH": "/usr/bin:/bin"}),
                         capture_output=True, text=True, timeout=60, start_new_session=True)
    assert "list_ok" not in out.stdout and "import_ok" in out.stdout, (out.stdout, out.stderr)
