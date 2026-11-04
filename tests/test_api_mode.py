"""The Messages-compatible endpoint, end to end: an API client on one side, a simulated worker on the other."""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from claude_bridge import messages_api as mx
from claude_bridge.api_mode import create_api_router, safe_tool_name
from claude_bridge.callers import Callers
from claude_bridge.principal import Principal
from claude_bridge.server import BridgeConfig, create_bridge
from claude_bridge.store import BridgeStore

AGENT = {"Authorization": "Bearer agent-secret"}
BASH = {"name": "Bash", "description": "Run a shell command on the user's machine.",
        "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}


@pytest.fixture
def env(tmp_path):
    store = BridgeStore(tmp_path / "api.db")
    b = create_bridge(store=store, config=BridgeConfig(client_tools_enabled=True, files_dir=tmp_path / "files"),
                      agent_token="agent-secret")
    callers = Callers(store)
    _, key = callers.add("api-alice", owner="alice", scopes=["api"], max_inflight=3)
    _, other = callers.add("tasks-only", owner="alice", scopes=["chat"])
    b.api_router, mgr = create_api_router(b.service, callers, b.tools, lambda row: Principal(owner=row["owner"]))
    app = FastAPI()
    b.mount(app, browser_prefix="/api", agent_prefix="/api/agent")
    with TestClient(app) as api, TestClient(app) as agent:
        agent.post("/api/agent/jobs/next", json={"worker": "w", "kinds": ["chat"], "wait": 0}, headers=AGENT)  # online
        yield {"b": b, "api": api, "agent": agent, "key": key, "other": other, "mgr": mgr}


def hdr(key):
    return {"x-api-key": key, "anthropic-version": "2023-06-01"}


def cs(ev, mid="m1"):
    return {"type": "content_stream", "data": {"native_message_id": mid, "event": ev, "at": 0}}


def native_turn(*blocks, stop="end_turn", mid="m1", usage=None):
    """Native stream events for one model message: blocks are ("text", str) or ("tool", id, name, input)."""
    evs = [cs({"type": "message_start", "message": {"id": mid, "usage": usage or {"input_tokens": 10, "cache_read_input_tokens": 500}}}, mid)]
    for i, b in enumerate(blocks):
        if b[0] == "text":
            evs += [cs({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}, mid),
                    cs({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b[1]}}, mid),
                    cs({"type": "content_block_stop", "index": i}, mid)]
        else:
            _, tid, name, inp = b
            evs += [cs({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": tid, "name": name, "input": {}}}, mid),
                    cs({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(inp)}}, mid),
                    cs({"type": "content_block_stop", "index": i}, mid)]
    evs += [cs({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 42}}, mid),
            cs({"type": "message_stop"}, mid)]
    return evs


class Worker:
    """Plays the worker + the bridge_client MCP server for one job, on its own thread."""

    def __init__(self, agent):
        self.agent = agent
        self.job = None
        self.errors: list[BaseException] = []

    def claim(self, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            job = self.agent.post("/api/agent/jobs/next", json={"worker": "w", "kinds": ["chat"], "wait": 0}, headers=AGENT).json()["job"]
            if job:
                self.job = job
                return job
            time.sleep(0.05)
        raise AssertionError("no job")

    def events(self, events):
        r = self.agent.post(f"/api/agent/jobs/{self.job['id']}/events", json={"events": events}, headers=AGENT)
        assert r.status_code == 200, r.text

    def open_tools(self):
        self.token = self.agent.post(f"/api/agent/jobs/{self.job['id']}/client-tool-session", headers=AGENT).json()["token"]

    def call(self, call_id, name, inp, wait=5.0):
        """What claude's MCP call does: register the call, then wait for the client's result."""
        h = {"Authorization": f"Bearer {self.token}"}
        base = f"/api/agent/client-tools/{self.job['id']}/calls"
        r = self.agent.post(base, json={"call_id": call_id, "name": name, "input": inp, "timeout": 60}, headers=h)
        assert r.status_code == 200, r.text
        end = time.time() + wait
        while time.time() < end:
            out = self.agent.get(f"{base}/{call_id}", headers=h).json()
            if out["status"] != "pending":
                return out
            time.sleep(0.05)
        raise AssertionError("no tool result")

    def finish(self, session_id="sess-1", ok=True, **kw):
        self.agent.post(f"/api/agent/jobs/{self.job['id']}/finish", json={"ok": ok, "result": "{}", "session_id": session_id, **kw}, headers=AGENT)

    def run(self, fn):
        def wrapped():
            try:
                fn(self)
            except BaseException as e:  # surfaced by the test
                self.errors.append(e)
        t = threading.Thread(target=wrapped, daemon=True)
        t.start()
        return t


def sse_events(resp):
    out, name = [], None
    for line in resp.iter_lines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            out.append((name, json.loads(line[6:])))
    return out


def test_tool_round_trip_then_the_session_is_resumed(env):
    api, w = env["api"], Worker(env["agent"])
    first = {"model": "claude-opus-5-5", "max_tokens": 4096, "stream": True, "system": "你是测试助手。",
             "tools": [BASH], "messages": [{"role": "user", "content": "看看目录里有什么"}]}

    def turn1(w):
        job = w.claim()
        s = job["payload"]["settings"]
        assert s["system"] == "你是测试助手。" and s["model"] == "claude-opus-5-5" and s["max_turns"] == 100
        assert [t["name"] for t in job["payload"]["client_tools"]] == ["Bash"]
        w.open_tools()
        w.events(native_turn(("text", "我来看看。"), ("tool", "toolu_1", "mcp__bridge_client__Bash", {"command": "ls"}), stop="tool_use"))
        res = w.call("call-1", "Bash", {"command": "ls"})
        assert res["status"] == "completed" and res["result"]["content"] == [{"type": "text", "text": "a.txt\nb.txt"}]
        w.events(native_turn(("text", "目录里有 a.txt 和 b.txt。"), mid="m2"))
        w.finish()

    t = w.run(turn1)
    with api.stream("POST", "/v1/messages", json=first, headers=hdr(env["key"])) as r:
        assert r.status_code == 200
        evs = sse_events(r)
    names = [n for n, _ in evs]
    assert names[0] == "message_start" and names[-2:] == ["message_delta", "message_stop"]
    assert evs[0][1]["message"]["usage"]["cache_read_input_tokens"] == 500
    starts = [d["content_block"] for n, d in evs if n == "content_block_start"]
    assert starts == [{"type": "text", "text": ""}, {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]
    assert evs[-2][1]["delta"]["stop_reason"] == "tool_use"

    # the client ran the tool and sends the result back (non-streaming this time)
    history = [*first["messages"],
               {"role": "assistant", "content": [{"type": "text", "text": "我来看看。"},
                                                 {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt\nb.txt"}]}]
    r = api.post("/v1/messages", json={**first, "stream": False, "messages": history}, headers=hdr(env["key"]))
    t.join(5)
    assert not w.errors, w.errors
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["type"] == "message" and m["stop_reason"] == "end_turn" and m["usage"]["output_tokens"] == 42
    assert m["content"] == [{"type": "text", "text": "目录里有 a.txt 和 b.txt。"}]

    # the next user message continues the same claude session (prompt cache kept), with just the new text
    w2 = Worker(env["agent"])
    seen = {}

    def turn2(w):
        job = w.claim()
        seen.update(job["payload"])
        w.events(native_turn(("text", "不客气。")))
        w.finish()

    t = w2.run(turn2)
    nxt = [*history, {"role": "assistant", "content": m["content"]}, {"role": "user", "content": "谢谢"}]
    r = api.post("/v1/messages", json={**first, "stream": False, "messages": nxt}, headers=hdr(env["key"]))
    t.join(5)
    assert not w2.errors, w2.errors
    assert r.json()["content"][0]["text"] == "不客气。"
    assert seen["session_id"] == "sess-1" and seen["text"] == "谢谢"


def test_unknown_history_starts_fresh_with_the_history_as_text(env):
    w, seen = Worker(env["agent"]), {}

    def turn(w):
        seen.update(w.claim()["payload"])
        w.events(native_turn(("text", "好的")))
        w.finish()

    t = w.run(turn)
    msgs = [{"role": "user", "content": "我叫小王"}, {"role": "assistant", "content": "你好小王"}, {"role": "user", "content": "我叫什么？"}]
    r = env["api"].post("/v1/messages", json={"model": "claude-sonnet-5-5", "max_tokens": 2048, "messages": msgs, "tools": [BASH]},
                        headers=hdr(env["key"]))
    t.join(5)
    assert r.status_code == 200 and not w.errors
    assert seen["session_id"] is None and "我叫小王" in seen["text"] and "你好小王" in seen["text"] and seen["text"].endswith("我叫什么？")


def test_probe_is_answered_without_the_model(env):
    r = env["api"].post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
                        headers=hdr(env["key"]))
    assert r.status_code == 200 and r.json()["stop_reason"] == "max_tokens"
    assert env["b"].store.count_jobs("queued") == 0


def test_small_tool_less_request_runs_one_off_on_a_small_model(env):
    w, seen = Worker(env["agent"]), {}

    def turn(w):
        seen.update(w.claim()["payload"])
        w.events(native_turn(("text", "目录查看")))
        w.finish()

    t = w.run(turn)
    r = env["api"].post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 200, "system": "Write a short title.",
                                              "messages": [{"role": "user", "content": "看看目录里有什么"}]}, headers=hdr(env["key"]))
    t.join(5)
    assert r.status_code == 200 and r.json()["content"][0]["text"] == "目录查看"
    assert seen["ephemeral"] is True and seen["settings"]["model"] == "haiku"


def test_auth_and_errors(env):
    api, msg = env["api"], {"model": "x", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
    r = api.post("/v1/messages", json=msg)
    assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
    assert api.post("/v1/messages", json=msg, headers=hdr(env["other"])).status_code == 401  # no "api" scope
    probe = {**msg, "max_tokens": 1}
    assert api.post("/v1/messages", json=probe, headers={"Authorization": f"Bearer {env['key']}"}).status_code == 200
    bad = api.post("/v1/messages", json={**msg, "messages": []}, headers=hdr(env["key"]))
    assert bad.status_code == 400 and bad.json()["error"]["type"] == "invalid_request_error"
    assert api.post("/v1/messages/count_tokens", json=msg, headers=hdr(env["key"])).json()["input_tokens"] > 0
    assert any(m["id"] == "claude-opus-5-5" for m in api.get("/v1/models", headers=hdr(env["key"])).json()["data"])


def test_failed_turn_maps_to_an_api_error(env):
    w = Worker(env["agent"])

    def turn(w):
        w.claim()
        w.finish(ok=False, error="Claude 返回错误：You've hit your session limit · resets 7pm", error_kind="claude")

    t = w.run(turn)
    with env["api"].stream("POST", "/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 4096, "stream": True, "tools": [BASH],
                                                         "messages": [{"role": "user", "content": "hi"}]}, headers=hdr(env["key"])) as r:
        evs = sse_events(r)
    t.join(5)
    assert evs[-1][0] == "error" and evs[-1][1]["error"]["type"] == "rate_limit_error"


def test_tool_names_and_fingerprints():
    assert safe_tool_name("Bash") == "Bash"
    long = "mcp__ccd_session_mgmt__export_transcript_and_more"
    s = safe_tool_name(long)
    assert s != long and len(s) <= 44 and s == safe_tool_name(long)
    a = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": [{"type": "thinking", "thinking": "…"}, {"type": "text", "text": "hey "}]}]
    b = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}, {"role": "assistant", "content": "hey"}]
    assert mx.fingerprint(a) == mx.fingerprint(b)  # thinking, whitespace and string-vs-block form don't matter
    assert mx.fingerprint(a) != mx.fingerprint([*b, {"role": "user", "content": "more"}])


def test_finished_turns_do_not_count_against_the_key_limit(env):
    # max_inflight is 3: a fourth turn in a row must still go through once the earlier ones are done
    for i in range(4):
        w = Worker(env["agent"])

        def turn(w, i=i):
            w.claim()
            w.events(native_turn(("text", f"第 {i} 次")))
            w.finish(session_id=f"s{i}")

        t = w.run(turn)
        r = env["api"].post("/v1/messages", json={"model": "claude-sonnet-5-5", "max_tokens": 2048, "tools": [BASH],
                                                  "messages": [{"role": "user", "content": f"问题 {i}"}]}, headers=hdr(env["key"]))
        t.join(5)
        assert r.status_code == 200, (i, r.text)
        for _ in range(50):  # the worker's done report lands just after the response
            if not env["mgr"].inflight_for(1) and not [x for x in env["mgr"].live.values()]:
                break
            time.sleep(0.05)
    assert env["mgr"].live == {}
