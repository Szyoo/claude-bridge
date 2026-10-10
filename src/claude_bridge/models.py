from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------- browser ----------------


class TurnOptions(BaseModel):
    """CLI-supported controls scoped to a single queued chat, never saved globally."""
    model_config = ConfigDict(extra='forbid')

    model: str | None = Field(None, max_length=100)
    effort: str | None = Field(None, max_length=20)
    max_turns: int | None = Field(None, ge=1, le=100)
    auto_context: bool | None = None
    max_output_tokens: int | None = Field(None, ge=1, le=128000)
    thinking: bool | None = None
    instructions: str | None = Field(None, max_length=16000)


class ClientTool(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
    description: str = Field('', max_length=20000)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {'type':'object'})


class ClientToolCallIn(BaseModel):
    call_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    name: str = Field(min_length=1, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)
    timeout: int = Field(300, ge=1, le=900)


class ClientToolResultIn(BaseModel):
    content: str | list[dict[str, Any]]
    is_error: bool = False


class SendIn(BaseModel):
    text: str = ""  # may be empty when images are attached; the service enforces "text or files"
    scope: str = ""
    key: str = ""
    new_thread: bool = False
    files: list[str] = Field(default_factory=list, max_length=50)
    options: TurnOptions | None = None
    client_tools: list[ClientTool] = Field(default_factory=list, max_length=50)
    client_environment: dict[str, str] = Field(default_factory=dict, max_length=16)


class ThreadMessageIn(BaseModel):
    text: str = ""
    files: list[str] = Field(default_factory=list, max_length=50)
    options: TurnOptions | None = None
    client_tools: list[ClientTool] = Field(default_factory=list, max_length=50)
    client_environment: dict[str, str] = Field(default_factory=dict, max_length=16)


class ThreadIn(BaseModel):
    scope: str = ""
    key: str = ""
    title: str = Field("", max_length=80)


class ThreadSelectIn(BaseModel):
    scope: str = ""


class ThreadPatchIn(BaseModel):
    title: str | None = Field(None, max_length=80)
    pinned: bool | None = None


class ProjectIn(BaseModel):
    name: str = Field("", max_length=64)  # empty with a clone_url = the repository's name
    clone_url: str = Field("", max_length=500)


class SettingsIn(BaseModel):
    model: str | None = None
    effort: str | None = None
    max_turns: int | None = Field(None, ge=1, le=100)
    auto_context: bool | None = None


# ---------------- agent ----------------


class JobNextIn(BaseModel):
    worker: str = "helper"
    kinds: list[str] = Field(default_factory=lambda: ["chat"])
    wait: int = Field(20, ge=0, le=30)


class EventIn(BaseModel):
    type: str = Field(min_length=1, max_length=32)
    data: dict[str, Any] = Field(default_factory=dict)


class JobEventsIn(BaseModel):
    status: str | None = None
    deltas: list[str] | None = None
    events: list[EventIn] | None = None
    # legacy worker body (before structured events): free text append + trace line
    append: str | None = None
    trace_append: str | None = None
    content: str | None = None


class JobFinishIn(BaseModel):
    ok: bool
    cancelled: bool = False
    result: str | None = None
    error: str | None = None
    error_kind: str | None = None
    session_id: str | None = None
    reset_session: bool = False
    context: dict[str, Any] | None = None  # parsed `claude /context` report for the thread's session


class ModelEntry(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field("", max_length=100)  # the CLI's display name, e.g. "Opus 5.5"


class AgentModelsIn(BaseModel):
    """What the worker's local `claude` supports (probed with zero-cost `/model` calls)."""

    cli_version: str = Field("", max_length=100)
    bridge_version: str = Field("", max_length=40)  # the worker's claude-bridge; differs from the server's until it restarts
    default_name: str = Field("", max_length=100)
    aliases: list[ModelEntry] = Field(default_factory=list, max_length=50)
    pinned: list[ModelEntry] = Field(default_factory=list, max_length=100)


class AgentLimitsIn(BaseModel):
    """Whole percentages from `claude -p "/usage"`; None when the CLI did not show that window."""

    five_hour_pct: float | None = Field(None, ge=0, le=1000)
    seven_day_pct: float | None = Field(None, ge=0, le=1000)


class AgentChatIn(BaseModel):
    text: str = Field(min_length=1)
    scope: str = ""
    key: str = ""
    new_thread: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)
