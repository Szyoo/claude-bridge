"""Deployment plugins: a Python file named by `CLAUDE_BRIDGE_PLUGIN`, loaded once at start-up.

The file defines `setup(service) -> object`; the returned object may implement any of these hooks (all optional):

- `turn_settings(owner, thread, settings, message_id) -> dict`: the settings a new turn runs with
  (model, effort, subagent_model, compact_window, instructions, …), given the ones it would run with.
- `event_view(owner, message_id, type, data) -> data`: an event of a message as it is sent to its owner's pages
  and stored-history reads (`type` "result" is the finished job's summary). The stored event is unchanged.
- `context_view(thread_id, context) -> dict`: a thread's `/context` report as its pages receive it.

Hooks run in request threads and must be quick; an exception in one fails that request.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def load_plugin(path: str, service: Any) -> Any:
    """The object a plugin file's `setup(service)` returns; None when no path is given."""
    path = (path or "").strip()
    if not path:
        return None
    file = Path(path).expanduser()
    spec = importlib.util.spec_from_file_location(f"claude_bridge_plugin_{file.stem}", file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load plugin {file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = module.setup(service)
    log.info("plugin loaded: %s", file)
    return plugin
