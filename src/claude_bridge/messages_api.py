"""Pure helpers for the Messages-compatible endpoint (`/v1/messages`): request shapes, conversation fingerprints,
history rendering and the Anthropic error / event envelopes. The HTTP layer and the job plumbing live in api_mode.py.

The Messages API is stateless (every request carries the whole conversation) while the bridge runs `claude` sessions
that are resumed turn by turn. A conversation is matched to a session by fingerprint: after each response the bridge
remembers `fingerprint(request messages + the assistant content it returned)`; a later request whose messages minus
the last one hash to that value continues the same session, keeping the prompt cache. Anything else (an edited or
compacted history, a client that rewrites old turns) starts a fresh session with the history rendered as text.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
from typing import Any

# content block types that don't change what the conversation says (clients may drop or keep them)
_SKIP_FOR_FINGERPRINT = {"thinking", "redacted_thinking"}
HISTORY_MAX_CHARS = 400_000
TOOL_RESULT_MAX_CHARS = 20_000


def new_message_id() -> str:
    return "msg_cb" + secrets.token_hex(12)


def blocks(content: Any) -> list[dict[str, Any]]:
    """Message content in block form (a bare string is one text block)."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in blocks(system) if b.get("type") == "text")


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in blocks(content):
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "image":
            parts.append("[图片]")
    return "\n".join(parts)


def _norm_block(b: dict[str, Any]) -> dict[str, Any] | None:
    t = b.get("type")
    if t in _SKIP_FOR_FINGERPRINT:
        return None
    if t == "text":
        text = (b.get("text") or "").strip()
        return {"t": "text", "v": text} if text else None
    if t == "tool_use":
        return {"t": "tool_use", "id": b.get("id"), "name": b.get("name"), "input": b.get("input")}
    if t == "tool_result":
        return {"t": "tool_result", "id": b.get("tool_use_id"), "v": _result_text(b.get("content")).strip(),
                "err": bool(b.get("is_error"))}
    if t in ("image", "document"):
        src = b.get("source") or {}
        data = src.get("data") or src.get("url") or ""
        return {"t": t, "h": hashlib.sha256(str(data).encode()).hexdigest()}
    return {"t": t}


def fingerprint(messages: list[dict[str, Any]]) -> str:
    """Stable digest of what a conversation says, insensitive to thinking blocks, whitespace and string-vs-block form."""
    norm = []
    for m in messages:
        bl = [x for x in (_norm_block(b) for b in blocks(m.get("content"))) if x]
        if bl:
            norm.append({"r": m.get("role"), "c": bl})
    raw = json.dumps(norm, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def render_history(messages: list[dict[str, Any]]) -> str:
    """Earlier turns as text, for a request that can't continue an existing session. Newest turns win if it's long."""
    lines: list[str] = []
    for m in messages:
        who = "用户" if m.get("role") == "user" else "助手"
        for b in blocks(m.get("content")):
            t = b.get("type")
            if t == "text" and (b.get("text") or "").strip():
                lines.append(f"[{who}] {b['text'].strip()}")
            elif t == "tool_use":
                lines.append(f"[助手调用工具 {b.get('name')}] {json.dumps(b.get('input'), ensure_ascii=False)[:4000]}")
            elif t == "tool_result":
                text = _result_text(b.get("content"))
                if len(text) > TOOL_RESULT_MAX_CHARS:
                    text = text[:TOOL_RESULT_MAX_CHARS] + "\n…（已截断）"
                lines.append(f"[工具结果{'（出错）' if b.get('is_error') else ''}] {text}")
            elif t in ("image", "document"):
                lines.append(f"[{who}附带了{'图片' if t == 'image' else '文档'}，此处省略]")
    out = "\n\n".join(lines)
    if len(out) > HISTORY_MAX_CHARS:
        out = "…（更早的内容已省略）\n\n" + out[-HISTORY_MAX_CHARS:]
    return out


def tool_results(message: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool_result blocks of a user message, as {tool_use_id, content, is_error}."""
    if message.get("role") != "user":
        return []
    return [{"tool_use_id": b.get("tool_use_id"), "content": b.get("content"), "is_error": bool(b.get("is_error"))}
            for b in blocks(message.get("content")) if b.get("type") == "tool_result"]


def user_text_and_images(message: dict[str, Any]) -> tuple[str, list[tuple[bytes, str]]]:
    """A user message's text and its base64 images (bytes, media type); other block kinds are rendered as text."""
    texts, images = [], []
    for b in blocks(message.get("content")):
        t = b.get("type")
        if t == "text":
            texts.append(b.get("text") or "")
        elif t == "image":
            src = b.get("source") or {}
            if src.get("type") == "base64" and src.get("data"):
                try:
                    images.append((base64.b64decode(src["data"], validate=True), src.get("media_type") or "image/png"))
                except ValueError:
                    texts.append("[一张无法解码的图片]")
            elif src.get("type") == "url":
                texts.append(f"[图片链接] {src.get('url')}")
        elif t == "tool_result":
            texts.append(f"[工具结果] {_result_text(b.get('content'))}")
        elif t == "document":
            texts.append("[附带了一份文档，此处无法读取]")
    return "\n\n".join(x for x in texts if x), images


def estimate_tokens(body: dict[str, Any]) -> int:
    """A rough count for /v1/messages/count_tokens (about 3.5 characters a token; an image ~1,600)."""
    chars, images = len(system_text(body.get("system"))), 0
    for m in body.get("messages") or []:
        for b in blocks(m.get("content")):
            if b.get("type") == "image":
                images += 1
            else:
                chars += len(json.dumps(b, ensure_ascii=False))
    chars += len(json.dumps(body.get("tools") or [], ensure_ascii=False))
    return max(1, math.ceil(chars / 3.5) + images * 1600)


# ---------------- envelopes ----------------

ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error", 404: "not_found_error",
               409: "invalid_request_error", 413: "request_too_large", 429: "rate_limit_error", 500: "api_error",
               503: "overloaded_error", 529: "overloaded_error"}


def error_body(status: int, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": ERROR_TYPES.get(status, "api_error"), "message": message}}


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


def message_shell(msg_id: str, model: str, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                      **(usage or {})}}
