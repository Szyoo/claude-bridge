"""Parse `claude -p --output-format stream-json --verbose [--include-partial-messages]` into bridge emissions.

Observed shape (Claude Code 2.1.x): with partial messages on, each content block streams as
`stream_event` deltas, then the CLI emits an `assistant` event holding just that one finished block,
then `content_block_stop`. Without partials only the `assistant` events arrive. `StreamState` turns
both shapes into the same sequence of text deltas and structured events without double-emitting text.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections.abc import Iterator
from typing import Any

log = logging.getLogger(__name__)

Emission = tuple[str, Any]  # ("delta", str) | ("event", {"type": str, "data": dict})

_TOKEN_RE = re.compile(r"([\d.]+)\s*([kKmM]?)")
_TOKENS_LINE_RE = re.compile(r"\*\*Tokens:\*\*\s*([\d.]+[kKmM]?)\s*/\s*([\d.]+[kKmM]?)\s*\((\d+)%\)")
_MODEL_LINE_RE = re.compile(r"\*\*Model:\*\*\s*(\S+)")


def parse_token_count(text: str) -> int | None:
    """'42.1k' → 42100, '1M' → 1000000, '< 20' → 20, '~80' → 80."""
    m = _TOKEN_RE.search(text or "")
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "k":
        n *= 1_000
    elif unit == "m":
        n *= 1_000_000
    return int(round(n))


def parse_context_report(md: str) -> dict[str, Any]:
    """Parse the Markdown that `claude -p "/context"` returns into {model, used, window, pct, categories}."""
    out: dict[str, Any] = {"model": None, "used": None, "window": None, "pct": None, "categories": [], "autocompact_pct": None}
    m = _MODEL_LINE_RE.search(md or "")
    if m:
        out["model"] = m.group(1)
    m = _TOKENS_LINE_RE.search(md or "")
    if m:
        out["used"], out["window"], out["pct"] = parse_token_count(m.group(1)), parse_token_count(m.group(2)), int(m.group(3))
    section = ""
    if "### Estimated usage by category" in (md or ""):
        section = md.split("### Estimated usage by category", 1)[1].split("###", 1)[0]
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() == "category":
            continue
        pct_txt = cells[2].rstrip("%").strip()
        try:
            pct = float(pct_txt)
        except ValueError:
            pct = None
        out["categories"].append({"name": cells[0], "tokens": parse_token_count(cells[1]), "pct": pct,
                                  "deferred": "deferred" in cells[0].lower()})
    buf = next((c for c in out["categories"] if c["name"].lower().startswith("autocompact")), None)
    if buf and buf["tokens"] and out["window"]:
        out["autocompact_pct"] = round(100 - buf["tokens"] / out["window"] * 100)
    return out


def iter_stream(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    for raw in lines:
        raw = raw.strip()
        if not raw or not raw.startswith("{"):
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            log.debug("ignoring non-JSON line: %s", raw[:120])


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if limit and len(text) > limit:
        return text[:limit] + "\n…", True
    return text, False


def describe_tool(block: dict[str, Any]) -> str:
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    if name == "Bash":
        cmd = str(inp.get("command", ""))
        try:
            cmd = shlex.join(shlex.split(cmd))
        except ValueError:
            pass
        return f"Bash: {cmd[:160]}"
    if name in ("Read", "Glob", "Grep", "Edit", "Write"):
        target = inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
        return f"{name}: {str(target)[:120]}"
    return str(name)


def tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(str(c.get("text", "")))
                elif c.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(c, ensure_ascii=False)[:200])
            else:
                parts.append(str(c))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


class StreamState:
    def __init__(self, *, resumed: bool = False, thinking_max: int = 4000, tool_result_max: int = 4000) -> None:
        self.resumed = resumed
        self.thinking_max = thinking_max
        self.tool_result_max = tool_result_max
        self.session_id: str | None = None
        self.init: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None
        self.rate_limit: dict[str, Any] | None = None
        self.model: str | None = None
        self.context_tokens = 0
        self.output_tokens = 0
        self.stop_reason: str | None = None
        self.text_total = 0
        self.chunks: list[str] = []
        self.compactions: list[dict[str, Any]] = []
        self._cur_msg: str | None = None
        self._open: dict[int, dict[str, Any]] = {}
        self._tools_seen: set[str] = set()

    @property
    def text(self) -> str:
        return "".join(self.chunks)

    # ---------------- feed ----------------

    def feed(self, ev: dict[str, Any]) -> list[Emission]:
        kind = ev.get("type")
        if kind == "system":
            return self._system(ev)
        if kind == "stream_event":
            return self._stream_event(ev.get("event") or {})
        if kind == "assistant":
            return self._assistant(ev)
        if kind == "user":
            return self._user(ev)
        if kind == "rate_limit_event":
            return self._rate_limit(ev)
        if kind == "result":
            self.result = ev
            self.session_id = ev.get("session_id") or self.session_id
            return []
        return []

    def _system(self, ev: dict[str, Any]) -> list[Emission]:
        if ev.get("subtype") == "compact_boundary":
            meta = ev.get("compact_metadata") or {}
            data = {k: meta.get(k) for k in ("trigger", "pre_tokens", "post_tokens", "cumulative_dropped_tokens", "duration_ms")}
            self.compactions.append(data)
            return [("event", {"type": "compact", "data": data})]
        if ev.get("subtype") != "init":
            return []
        self.init = ev
        self.session_id = ev.get("session_id") or self.session_id
        self.model = ev.get("model") or self.model
        data = {
            "session_id": self.session_id,
            "model": ev.get("model"),
            "tools": ev.get("tools") or [],
            "permission_mode": ev.get("permissionMode"),
            "cli_version": ev.get("claude_code_version"),
            "cwd": ev.get("cwd"),
            "resumed": self.resumed,
        }
        return [("event", {"type": "init", "data": data})]

    def _stream_event(self, e: dict[str, Any]) -> list[Emission]:
        t = e.get("type")
        out: list[Emission] = []
        if t == "message_start":
            m = e.get("message") or {}
            self._cur_msg = m.get("id")
            self.model = m.get("model") or self.model
            u = m.get("usage") or {}
            self.context_tokens = (
                int(u.get("input_tokens") or 0)
                + int(u.get("cache_creation_input_tokens") or 0)
                + int(u.get("cache_read_input_tokens") or 0)
            )
            self._open = {}
        elif t == "content_block_start":
            idx = int(e.get("index", 0))
            cb = e.get("content_block") or {}
            self._open[idx] = {
                "type": cb.get("type"),
                "streamed": "",
                "reconciled": False,
                "thinking": cb.get("thinking") or "",
                "id": cb.get("id"),
                "name": cb.get("name"),
            }
            if cb.get("type") == "thinking":
                out.append(("event", {"type": "status", "data": {"phase": "thinking"}}))
        elif t == "content_block_delta":
            idx = int(e.get("index", 0))
            d = e.get("delta") or {}
            b = self._open.setdefault(
                idx, {"type": None, "streamed": "", "reconciled": False, "thinking": "", "id": None, "name": None}
            )
            if d.get("type") == "text_delta":
                text = d.get("text") or ""
                b["type"] = b["type"] or "text"
                if text:
                    b["streamed"] += text
                    out.append(self._delta(text))
            elif d.get("type") == "thinking_delta":
                b["type"] = b["type"] or "thinking"
                b["thinking"] += d.get("thinking") or ""
        elif t == "content_block_stop":
            idx = int(e.get("index", 0))
            b = self._open.pop(idx, None)
            if b:
                if b["type"] == "text" and (b["streamed"] or b["reconciled"]):
                    out.append(self._delta("\n\n"))
                elif b["type"] == "thinking":
                    out.extend(self._thinking_event(b["thinking"]))
        elif t == "message_delta":
            u = e.get("usage") or {}
            if u.get("output_tokens") is not None:
                self.output_tokens = int(u["output_tokens"])
            sr = (e.get("delta") or {}).get("stop_reason")
            if sr:
                self.stop_reason = sr
        return out

    def _assistant(self, ev: dict[str, Any]) -> list[Emission]:
        out: list[Emission] = []
        msg = ev.get("message") or {}
        self.model = msg.get("model") or self.model
        for block in msg.get("content") or []:
            bt = block.get("type")
            if bt == "text":
                text = block.get("text") or ""
                open_b = self._last_open("text")
                if open_b is None:
                    if text:
                        out.append(self._delta(text + "\n\n"))
                    continue
                streamed = open_b["streamed"]
                if text.startswith(streamed):
                    rest = text[len(streamed) :]
                    if rest:
                        out.append(self._delta(rest))
                    open_b["streamed"] = text
                elif not streamed:
                    out.append(self._delta(text))
                    open_b["streamed"] = text
                else:
                    log.warning("assistant text does not extend streamed prefix; keeping streamed version")
                open_b["reconciled"] = True
            elif bt == "tool_use":
                tid = block.get("id") or f"anon-{len(self._tools_seen)}"
                if tid in self._tools_seen:
                    continue
                self._tools_seen.add(tid)
                data = {"id": tid, "name": block.get("name"), "input": block.get("input") or {}, "at": self.text_total}
                out.append(("event", {"type": "tool_use", "data": data}))
            elif bt == "thinking":
                open_b = self._last_open("thinking")
                if open_b is not None:
                    open_b["thinking"] = open_b["thinking"] or (block.get("thinking") or "")
                    open_b["reconciled"] = True
                else:
                    out.extend(self._thinking_event(block.get("thinking") or ""))
        return out

    def _user(self, ev: dict[str, Any]) -> list[Emission]:
        out: list[Emission] = []
        content = (ev.get("message") or {}).get("content") or []
        if isinstance(content, str):
            return out
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            text, truncated = truncate(tool_result_text(block.get("content")), self.tool_result_max)
            data = {
                "tool_use_id": block.get("tool_use_id"),
                "content": text,
                "is_error": bool(block.get("is_error")),
                "truncated": truncated,
            }
            out.append(("event", {"type": "tool_result", "data": data}))
        return out

    def _rate_limit(self, ev: dict[str, Any]) -> list[Emission]:
        info = ev.get("rate_limit_info") or {}
        windows = info.get("unifiedWindows") or {}

        def win(name: str) -> dict[str, Any] | None:
            w = windows.get(name)
            if not isinstance(w, dict):
                return None
            return {"utilization": w.get("utilization"), "resets_at": w.get("resetsAt")}

        data = {
            "status": info.get("status"),
            "type": info.get("rateLimitType"),
            "resets_at": info.get("resetsAt"),
            "five_hour": win("five_hour"),
            "seven_day": win("seven_day"),
        }
        self.rate_limit = data
        return [("event", {"type": "rate_limit", "data": data})]

    # ---------------- helpers ----------------

    def _delta(self, text: str) -> Emission:
        self.text_total += len(text)
        self.chunks.append(text)
        return ("delta", text)

    def _last_open(self, btype: str) -> dict[str, Any] | None:
        for idx in sorted(self._open, reverse=True):
            b = self._open[idx]
            if b["type"] == btype and not b["reconciled"]:
                return b
        return None

    def _thinking_event(self, text: str) -> list[Emission]:
        if not text.strip():
            return []
        t, truncated = truncate(text, self.thinking_max)
        # `at` = how much answer text had streamed when this block closed; the browser interleaves it at that point
        return [("event", {"type": "thinking", "data": {"text": t, "truncated": truncated, "at": self.text_total}})]

    # ---------------- end of run ----------------

    def usage_data(self) -> dict[str, Any]:
        r = self.result or {}
        u = r.get("usage") or {}
        return {
            "input_tokens": u.get("input_tokens"),
            "output_tokens": u.get("output_tokens"),
            "cache_read_input_tokens": u.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
            "context_tokens": self.context_tokens,
            "total_cost_usd": r.get("total_cost_usd"),
            "num_turns": r.get("num_turns"),
            "duration_ms": r.get("duration_ms"),
            "duration_api_ms": r.get("duration_api_ms"),
            "model": self.model,
            "stop_reason": r.get("stop_reason") or self.stop_reason,
        }

    def summary(self, *, effort: str | None = None) -> dict[str, Any]:
        return {"session_id": self.session_id, "effort": effort, "rate_limit": self.rate_limit, **self.usage_data()}
