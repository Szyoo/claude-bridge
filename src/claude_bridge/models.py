from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---------------- browser ----------------


class SendIn(BaseModel):
    text: str = Field(min_length=1)
    scope: str = ""
    key: str = ""
    new_thread: bool = False


class ThreadMessageIn(BaseModel):
    text: str = Field(min_length=1)


class ThreadIn(BaseModel):
    scope: str = ""
    key: str = ""
    title: str = Field("", max_length=80)


class ThreadSelectIn(BaseModel):
    scope: str = ""


class ThreadPatchIn(BaseModel):
    title: str | None = Field(None, max_length=80)
    pinned: bool | None = None


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
    # legacy helper body (pre-bridge ashare): free text append + trace line
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


class AgentChatIn(BaseModel):
    text: str = Field(min_length=1)
    scope: str = ""
    key: str = ""
    new_thread: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)
