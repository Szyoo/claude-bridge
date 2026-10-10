from __future__ import annotations

import subprocess
import threading

import pytest

from claude_bridge import schema_validation as validation


def test_validator_rejects_recursive_ref_within_budget():
    with pytest.raises(validation.SchemaValidationError):
        validation.validate_schema({"schema": {"$ref": "#"}, "input": {}})
    validation.validate_schema({"schema": {"type": "object"}, "input": {}})


def test_structural_limits_and_busy_capacity_fail_closed(monkeypatch):
    deep = {}
    for _ in range(50):
        deep = {"child": deep}
    with pytest.raises(validation.SchemaValidationError, match="structural limits"):
        validation.validate_schema(deep)
    monkeypatch.setattr(validation, "_slots", threading.BoundedSemaphore(0))
    with pytest.raises(validation.SchemaValidationError, match="capacity"):
        validation.validate_schema({"schema": {}, "input": {}})


def test_validator_launch_is_isolated_and_does_not_inherit_secrets(monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_AGENT_TOKEN", "private-control-plane")
    monkeypatch.setenv("PRIVATE_SECRET", "private-service-secret")
    monkeypatch.setenv("HTTPS_PROXY", "https://user:password@example.invalid")
    launches = []

    def run(command, **kwargs):
        launches.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"ok")

    monkeypatch.setattr(validation.subprocess, "run", run)
    validation.validate_schema({"schema": {}, "input": {}})
    command, kwargs = launches[0]
    assert command[1] == "-I" and command[-1] == "--child"
    assert kwargs["timeout"] == validation.MAX_SECONDS
    assert "private-" not in str(kwargs["env"])
    assert "HTTPS_PROXY" not in kwargs["env"]
