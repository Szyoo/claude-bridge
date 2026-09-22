"""claude-bridge: web chat → job queue → local `claude -p` worker."""

from __future__ import annotations

__version__ = "0.1.0"

from claude_bridge.auth import PasswordAuth, bearer_auth  # noqa: E402
from claude_bridge.broker import Broker  # noqa: E402
from claude_bridge.errors import BadRequest, BridgeError, ChatBusy, NotFound  # noqa: E402
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
    "bearer_auth",
    "create_bridge",
    "static_dir",
    "__version__",
]
