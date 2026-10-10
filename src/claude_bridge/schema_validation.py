"""Validate untrusted schemas outside the service process, with a bounded budget."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

MAX_BYTES = 6_000_000
MAX_SECONDS = 2.0
_slots = threading.BoundedSemaphore(4)


class SchemaValidationError(ValueError):
    pass


def validate_schema(payload: dict) -> None:
    # Bound traversal before serializing; no recursive Python walk on caller data.
    pending = [(payload, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > 50_000 or depth > 48:
            raise SchemaValidationError("schema validation input exceeds structural limits")
        if isinstance(value, dict):
            pending.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            pending.extend((v, depth + 1) for v in value)
    try:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError) as exc:
        raise SchemaValidationError("invalid schema validation input") from exc
    if len(data) > MAX_BYTES:
        raise SchemaValidationError("schema validation input is too large")
    if not _slots.acquire(blocking=False):
        raise SchemaValidationError("schema validation capacity is exhausted; retry later")
    try:
        # -I plus an absolute, package-owned script avoids workspace module shadowing.
        # Do not pass service credentials/proxy variables into the validator process.
        proc = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--child"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}},
            timeout=MAX_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # subprocess.run kills and reaps the child before raising on timeout.
        raise SchemaValidationError("schema validation timed out or exceeded limits") from exc
    finally:
        _slots.release()
    if proc.returncode != 0 or proc.stdout != b"ok":
        raise SchemaValidationError("invalid schema, arguments, or validation budget exceeded")


def _child() -> None:
    if os.name == "posix":
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
    # Imports, regex matching, reference expansion and recursion all share the budget.
    from jsonschema import Draft202012Validator
    from referencing import Registry

    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        return
    payload = json.loads(raw)
    if "schemas" in payload:
        for schema in payload["schemas"]:
            Draft202012Validator.check_schema(schema)
    else:
        Draft202012Validator(payload["schema"], registry=Registry()).validate(payload["input"])
    sys.stdout.buffer.write(b"ok")


if __name__ == "__main__":
    try:
        _child()
    except Exception:
        # No schema, arguments, traceback or credentials on the output channel.
        sys.exit(1)
