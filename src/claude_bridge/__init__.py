"""claude-bridge: web chat → job queue → local `claude -p` worker."""

from __future__ import annotations

from claude_bridge._version import REPO, __version__  # noqa: E402
from claude_bridge.auth import PasswordAuth, bearer_auth  # noqa: E402
from claude_bridge.broker import Broker  # noqa: E402
from claude_bridge.errors import BadRequest, BridgeError, ChatBusy, NotFound, QuotaExceeded  # noqa: E402
from claude_bridge.principal import Principal  # noqa: E402
from claude_bridge.server import Bridge, create_bridge, static_dir  # noqa: E402
from claude_bridge.service import BridgeConfig, BridgeService  # noqa: E402
from claude_bridge.store import BridgeStore  # noqa: E402

__all__ = [
    "BadRequest",
    "Bridge",
    "BridgeConfig",
    "BridgeError",
    "BridgeService",
    "BridgeStore",
    "Broker",
    "ChatBusy",
    "NotFound",
    "PasswordAuth",
    "Principal",
    "QuotaExceeded",
    "REPO",
    "bearer_auth",
    "create_bridge",
    "static_dir",
    "__version__",
]
