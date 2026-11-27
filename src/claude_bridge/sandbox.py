"""Per-user sandboxes for the worker's `claude` runs (macOS Seatbelt, `sandbox-exec`).

A job the server marks `sandbox` (an ordinary user's turn) runs with its own space under the worker's tenant
root: a work directory (the cwd, kept across conversations), a HOME, a TMPDIR and its own Claude config dir, so
its sessions and transcripts live apart from the host's and from other users'. The Seatbelt profile then makes
everything else on the host invisible: the host user's home directory (documents, keys, app data, ~/.claude),
/tmp and /var/folders, other users' spaces, the keychain, local network services (localhost, unix sockets) and
listening sockets; signals reach only the run's own processes. Outbound internet, the system toolchain
(/usr, /opt/homebrew, …) and the claude binary stay available. The environment is rebuilt from an allow-list.

Profiles are written outside every tenant's space (a run cannot change its own rules).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import claude_bridge

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# variables a sandboxed run keeps from the worker's environment (credentials for claude itself are handled by
# the CLI, which does not pass them to the tools' processes)
ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "USER", "LOGNAME", "SHELL",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
ENV_KEEP_PREFIX = ("CLAUDE_CODE_", "ANTHROPIC_", "DISABLE_", "MAX_THINKING_TOKENS", "BASH_")


def available() -> bool:
    return os.path.exists(SANDBOX_EXEC)


def owner_key(owner: Any) -> str:
    key = re.sub(r"[^A-Za-z0-9_-]", "_", str(owner or ""))
    if not key:
        raise ValueError("a sandboxed job needs an owner")
    return key


def project_dir_name(cwd: Path | str) -> str:
    """How the CLI names a cwd's folder under projects/."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


@dataclass(frozen=True)
class Tenant:
    owner: str
    root: Path  # the tenant's whole space
    profile: Path  # its Seatbelt profile (outside `root`)

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def config_dir(self) -> Path:
        return self.home / ".claude"

    @property
    def projects(self) -> Path:
        return self.config_dir / "projects"

    def cwd_for(self, scope: str) -> Path:
        """The work folder of a scope: chat → work/chat, code:app → work/code/app, api → work/api."""
        base, _, project = (scope or "chat").partition(":")
        parts = [re.sub(r"[^A-Za-z0-9._-]", "_", p) for p in (base or "chat", project) if p]
        return self.work.joinpath(*parts)

    # ---------------- running ----------------

    def wrap(self, cmd: list[str]) -> list[str]:
        return [SANDBOX_EXEC, "-f", str(self.profile), *cmd]

    def env(self, base: dict[str, str]) -> dict[str, str]:
        env = {k: v for k, v in base.items() if k in ENV_KEEP or k.startswith(ENV_KEEP_PREFIX)}
        env.update(HOME=str(self.home), TMPDIR=str(self.tmp) + "/", CLAUDE_CODE_TMPDIR=str(self.tmp),
                   CLAUDE_CONFIG_DIR=str(self.config_dir), PWD=str(self.work))
        return env


def tenant(root: Path | str, owner: Any, local_ports: list[int] | None = None) -> Tenant:
    """The owner's space under `root` (created on first use, private to the host user) with a fresh profile."""
    root = Path(root).expanduser().resolve()
    t = Tenant(owner_key(owner), root / owner_key(owner), root / ".profiles" / f"{owner_key(owner)}.sb")
    for d in (t.work, t.home, t.tmp, t.config_dir, t.profile.parent):
        d.mkdir(parents=True, exist_ok=True)
    for d in (root, t.root, t.profile.parent):
        d.chmod(0o700)
    text = profile_text(t.root, local_ports=local_ports)
    if not t.profile.exists() or t.profile.read_text() != text:
        t.profile.write_text(text)
    return t


def profile_text(space: Path, home: Path | None = None, extra_read: list[Path] | None = None,
                 local_ports: list[int] | None = None) -> str:
    """The Seatbelt rules for one tenant space (later rules win). `local_ports`: localhost ports it may reach
    (the bridge server itself, when the worker talks to one on this machine)."""
    home = (home or Path.home()).resolve()
    space = space.resolve()
    pkg = Path(claude_bridge.__file__).resolve().parent
    reads = [Path(os.path.realpath(os.path.expanduser("~/.local/share/claude"))),
             Path(os.path.realpath(sys.prefix)), Path(os.path.realpath(sys.base_prefix)), pkg.parent,
             *(extra_read or [])]
    claude_link = Path(os.path.expanduser("~/.local/bin/claude"))
    # resolving paths (realpath) walks every parent: those may be stat'ed, never listed or read
    ancestors = sorted({str(a) for p in (space, *reads, claude_link) for a in Path(p).parents if str(a) != "/"} |
                       {os.path.expanduser("~/.local/bin"), str(Path(sys.prefix) / "bin")})

    def q(p: Path | str) -> str:
        return json.dumps(str(p))

    lines = [
        "(version 1)",
        "(allow default)",
        ";; the host user's home, temp folders and mounted volumes",
        f"(deny file-read* file-write* (subpath {q(home)}))",
        '(deny file-read* file-write* (subpath "/private/tmp") (subpath "/private/var/folders") (subpath "/Volumes"))',
        ";; resolving the path to the tenant's space (names only)",
        "(allow file-read-metadata " + " ".join(f"(literal {q(p)})" for p in ancestors) + ")",
        ";; what running claude needs",
        "(allow file-read* " + " ".join(f"(subpath {q(p)})" for p in reads) + f" (literal {q(claude_link)}))",
        ";; the tenant's own space",
        f"(allow file-read* file-write* (subpath {q(space)}))",
        ";; no listening sockets, no local services",
        "(deny network-bind)",
        "(deny network-inbound)",
        '(deny network-outbound (remote ip "localhost:*"))',
        *[f'(allow network-outbound (remote ip "localhost:{int(port)}"))' for port in (local_ports or [])],
        "(deny network-outbound (remote unix-socket))",
        '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',
        ";; signals to its own processes only",
        "(deny signal)",
        "(allow signal (target self) (target pgrp) (target children))",
        ";; keychain",
        '(deny mach-lookup (global-name "com.apple.SecurityServer") (global-name "com.apple.securityd.xpc"))',
    ]
    return "\n".join(lines) + "\n"


def adopt_session(t: Tenant, session_id: str, cwd: Path, host_projects: Path) -> bool:
    """A session that ran before sandboxing (in the host's ~/.claude): copy its transcript into the tenant's
    config under the new cwd so `--resume` finds it. False when it is already there or nowhere to be found."""
    dest = t.projects / project_dir_name(cwd)
    if (dest / f"{session_id}.jsonl").exists():
        return False
    found = next(iter(host_projects.glob(f"*/{session_id}.jsonl")), None) if host_projects.is_dir() else None
    if found is None:
        found = next(iter(t.projects.glob(f"*/{session_id}.jsonl")), None)  # another scope folder of the same tenant
        if found is None or found.parent == dest:
            return False
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(found, dest / found.name)
    sub = found.parent / session_id
    if sub.is_dir() and not (dest / session_id).exists():
        shutil.copytree(sub, dest / session_id)
    return True


def environment_note(t: Tenant, cwd: Path, *, client_tools: bool, client_environment: dict[str, Any] | None = None) -> str:
    """Tells the model where its tools run, so it does not act as if it were on the user's computer."""
    lines = [
        "# Execution environment (read before using tools)",
        "- Your built-in tools (Bash, Read, Write, Edit, Glob, Grep, WebFetch, subagents) run on a shared macOS (arm64) "
        "server, inside an isolated per-user sandbox — not on the user's computer.",
        f"- Working directory: {cwd}  (HOME={t.home}, TMPDIR={t.tmp}). Files here persist across this user's conversations.",
        "- Only this space is readable and writable. Everything else on the server (other home directories, /tmp, "
        "system services, localhost ports, the local network, the keychain) is blocked by design: do not probe it "
        "or try to work around it, and do not start servers — listening sockets are not allowed.",
        "- Available: python3, node/npm, git, curl and outbound internet access. Install packages into this space "
        "(e.g. a venv or a local node_modules), not system-wide.",
    ]
    if client_tools:
        where = ""
        if client_environment:
            where = " (" + ", ".join(f"{k}: {v}" for k, v in list(client_environment.items())[:6]) + ")"
        lines += [
            f"- Tools named mcp__bridge_client__* run on the user's own computer{where}. That machine and this "
            "server do not share files or paths: a path from one side does not exist on the other.",
            "- To move a file between them, read it with one side's tool and write it with the other side's tool "
            "(text content); for the user's own files, folders, apps and OS-specific commands, use the "
            "mcp__bridge_client__* tools.",
        ]
    return "\n".join(lines)
