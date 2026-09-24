from __future__ import annotations

import json
import threading
import time

import pytest

from claude_bridge.client import BridgeClientError
from claude_bridge.stream_json import StreamState, describe_tool, tool_result_text
from claude_bridge.worker import Hooks, Worker, WorkerConfig
from conftest import make_fake_claude

# ---------------- canned stream-json (shapes copied from a real claude 2.1.266 run) ----------------

SID = "sess-1"


def init_ev(**kw):
    return {"type": "system", "subtype": "init", "session_id": SID, "model": "claude-haiku-4-5", "tools": ["Bash", "Read"],
            "permissionMode": "default", "claude_code_version": "2.1.266", "cwd": "/x", **kw}


def msg_start(mid="m1", inp=10, cc=100, cr=0):
    return {"type": "stream_event", "event": {"type": "message_start", "message": {
        "id": mid, "model": "claude-haiku-4-5", "usage": {"input_tokens": inp, "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}}}}


def cb_start(idx, block):
    return {"type": "stream_event", "event": {"type": "content_block_start", "index": idx, "content_block": block}}


def cb_delta(idx, delta):
    return {"type": "stream_event", "event": {"type": "content_block_delta", "index": idx, "delta": delta}}


def cb_stop(idx):
    return {"type": "stream_event", "event": {"type": "content_block_stop", "index": idx}}


def msg_delta(out=41, stop="end_turn"):
    return {"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": out}}}


def assistant(mid, *blocks):
    return {"type": "assistant", "message": {"id": mid, "model": "claude-haiku-4-5", "role": "assistant", "content": list(blocks)}, "session_id": SID}


def user_tool_result(tid, content, is_error=False):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": is_error}]}}


RATE_LIMIT = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1790072400, "rateLimitType": "five_hour",
              "unifiedWindows": {"five_hour": {"utilization": 0.15, "resetsAt": 1790072400}, "seven_day": {"utilization": 0.02, "resetsAt": 1790618400}}}}


def result_ev(is_error=False, text="ok", **kw):
    return {"type": "result", "subtype": "error" if is_error else "success", "is_error": is_error, "result": text, "session_id": SID,
            "duration_ms": 1500, "duration_api_ms": 1249, "num_turns": 1, "total_cost_usd": 0.079, "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 39416, "cache_read_input_tokens": 0, "output_tokens": 41}, **kw}


def feed_all(events, **kw):
    st = StreamState(**kw)
    out = []
    for ev in events:
        out.extend(st.feed(ev))
    return st, out


def deltas(out):
    return [p for k, p in out if k == "delta"]


def events(out, type=None):
    evs = [p for k, p in out if k == "event"]
    return [e for e in evs if type is None or e["type"] == type]


# ---------------- StreamState ----------------


def test_stream_state_partial_then_assistant_dedupes():
    seq = [
        init_ev(), msg_start(inp=10, cc=39416),
        cb_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
        cb_delta(0, {"type": "thinking_delta", "thinking": ""}), cb_delta(0, {"type": "signature_delta", "signature": "x"}),
        assistant("m1", {"type": "thinking", "thinking": "", "signature": "x"}), cb_stop(0),
        cb_start(1, {"type": "text", "text": ""}),
        cb_delta(1, {"type": "text_delta", "text": "hello"}), cb_delta(1, {"type": "text_delta", "text": " bridge"}),
        assistant("m1", {"type": "text", "text": "hello bridge"}), cb_stop(1),
        msg_delta(), RATE_LIMIT, result_ev(),
    ]
    st, out = feed_all(seq)
    assert deltas(out) == ["hello", " bridge", "\n\n"]
    assert [e["type"] for e in events(out)] == ["init", "status", "rate_limit"]
    assert events(out, "init")[0]["data"]["resumed"] is False and events(out, "init")[0]["data"]["model"] == "claude-haiku-4-5"
    assert events(out, "status")[0]["data"] == {"phase": "thinking"}
    rl = events(out, "rate_limit")[0]["data"]
    assert rl["five_hour"] == {"utilization": 0.15, "resets_at": 1790072400} and rl["status"] == "allowed"
    assert st.text == "hello bridge\n\n" and st.context_tokens == 39426 and st.output_tokens == 41
    u = st.usage_data()
    assert u["context_tokens"] == 39426 and u["total_cost_usd"] == 0.079 and u["stop_reason"] == "end_turn"
    s = st.summary(effort="high")
    assert s["session_id"] == SID and s["effort"] == "high" and s["rate_limit"]["seven_day"]["utilization"] == 0.02


def test_stream_state_without_partials():
    seq = [
        init_ev(),
        assistant("m1", {"type": "text", "text": "先看看"}),
        assistant("m1", {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la"}}),
        user_tool_result("t1", [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}, {"type": "image"}]),
        assistant("m2", {"type": "text", "text": "有两个文件"}),
        result_ev(),
    ]
    st, out = feed_all(seq, resumed=True)
    assert deltas(out) == ["先看看\n\n", "有两个文件\n\n"]
    tu = events(out, "tool_use")[0]["data"]
    assert tu == {"id": "t1", "name": "Bash", "input": {"command": "ls -la"}, "at": len("先看看\n\n")}
    tr = events(out, "tool_result")[0]["data"]
    assert tr == {"tool_use_id": "t1", "content": "a\nb\n[image]", "is_error": False, "truncated": False}
    assert events(out, "init")[0]["data"]["resumed"] is True
    assert describe_tool(tu) == "Bash: ls -la"


def test_stream_state_tool_flow_with_partials_and_context_reset():
    seq = [
        init_ev(), msg_start("m1", inp=10, cc=6792, cr=32743),
        cb_start(0, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
        cb_delta(0, {"type": "input_json_delta", "partial_json": '{"command": "ls'}),
        cb_delta(0, {"type": "input_json_delta", "partial_json": '"}'}),
        assistant("m1", {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}),
        assistant("m1", {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}),  # duplicate
        cb_stop(0), msg_delta(out=5, stop="tool_use"),
        user_tool_result("t1", "x" * 50, is_error=True),
        msg_start("m2", inp=8, cc=189, cr=39535),
        cb_start(0, {"type": "text", "text": ""}), cb_delta(0, {"type": "text_delta", "text": "done"}),
        assistant("m2", {"type": "text", "text": "done"}), cb_stop(0), msg_delta(out=9), result_ev(),
    ]
    st, out = feed_all(seq, tool_result_max=10)
    assert len(events(out, "tool_use")) == 1
    tr = events(out, "tool_result")[0]["data"]
    assert tr["content"] == "x" * 10 + "\n…" and tr["truncated"] is True and tr["is_error"] is True
    assert deltas(out) == ["done", "\n\n"]
    assert st.context_tokens == 8 + 189 + 39535  # last message_start wins
    assert st.stop_reason == "end_turn"


def test_stream_state_thinking_and_prefix_extension():
    seq = [
        cb_start(0, {"type": "thinking", "thinking": ""}),
        cb_delta(0, {"type": "thinking_delta", "thinking": "想一想"}), cb_delta(0, {"type": "thinking_delta", "thinking": "再想想"}),
        assistant("m1", {"type": "thinking", "thinking": "想一想再想想"}), cb_stop(0),
        cb_start(1, {"type": "text", "text": ""}), cb_delta(1, {"type": "text_delta", "text": "hel"}),
        assistant("m1", {"type": "text", "text": "hello"}), cb_stop(1),
        # a text block that never streamed but gets its assistant event while another block is open
        cb_start(2, {"type": "text", "text": ""}), assistant("m1", {"type": "text", "text": "tail"}), cb_stop(2),
    ]
    st, out = feed_all(seq, thinking_max=4)
    th = events(out, "thinking")
    assert len(th) == 1 and th[0]["data"] == {"text": "想一想再\n…", "truncated": True, "at": 0}
    assert deltas(out) == ["hel", "lo", "\n\n", "tail", "\n\n"]
    assert st.text == "hello\n\ntail\n\n"


def test_tool_result_text_shapes():
    assert tool_result_text("plain") == "plain"
    assert tool_result_text(None) == ""
    assert tool_result_text([{"type": "text", "text": "a"}, "raw", {"type": "weird", "k": 1}]) == 'a\nraw\n{"type": "weird", "k": 1}'


# ---------------- worker end to end with a fake claude ----------------


class FakeClient:
    def __init__(self, jobs=None):
        self.jobs = list(jobs or [])
        self.posts: list[dict] = []
        self.finished: list[dict] = []
        self.created: list[dict] = []
        self.cancel_on_text: str | None = None  # return cancel:true once the streamed text contains this
        self.job_state = {"status": "running", "cancel_requested": False}
        self.get_job_calls = 0
        self.fail_posts = 0

    def next_job(self, worker, kinds, wait=25):
        return self.jobs.pop(0) if self.jobs else None

    def get_job(self, job_id):
        self.get_job_calls += 1
        return {"id": job_id, "kind": "chat", **self.job_state}

    def post_events(self, job_id, *, status=None, deltas=None, events=None):
        if self.fail_posts:
            self.fail_posts -= 1
            raise BridgeClientError("down")
        self.posts.append({"status": status, "deltas": deltas or [], "events": events or []})
        return {"ok": True, "cancel": bool(self.cancel_on_text) and self.cancel_on_text in self.text}

    def finish_job(self, job_id, **kw):
        self.finished.append({"id": job_id, **kw})

    def create_chat(self, text, **kw):
        self.created.append({"text": text, **kw})
        return {"thread": "t"}

    # helpers
    @property
    def text(self):
        return "".join(d for p in self.posts for d in p["deltas"])

    def events(self, type=None):
        return [e for p in self.posts for e in p["events"] if type is None or e["type"] == type]


class RecordingHooks(Hooks):
    def __init__(self):
        self.ctx_calls = []
        self.finished = []
        self.errors = []
        self.ticks = 0

    def build_context(self, *, thread_id, is_new_session, payload):
        self.ctx_calls.append((thread_id, is_new_session))
        return "【背景】持仓 1 只\n\n【用户消息】\n" if is_new_session else ""

    def env(self, payload):
        return {"BRIDGE_TEST_ENV": f"thread={payload.get('thread')}"}

    def on_chat_finished(self, job, text, summary):
        self.finished.append((job["id"], text, summary))

    def on_job_error(self, job, exc):
        self.errors.append((job["id"], str(exc)))

    def tick(self):
        self.ticks += 1


def make_worker(tmp_path, lines, client=None, hooks=None, handlers=None, exit_code=0, sleep_after=0, **cfg):
    client = client or FakeClient()
    opts = {"flush_interval": 0.05, "cancel_poll_interval": 0.1, "kill_grace": 0.5, **cfg}
    config = WorkerConfig(
        claude_bin=make_fake_claude(tmp_path, lines, exit_code=exit_code, sleep_after=sleep_after), cwd=tmp_path, **opts
    )
    return Worker(client, config, hooks=hooks, handlers=handlers), client


def chat_job(jid=7, text="我有几只股票", session_id=None, **settings):
    return {"id": jid, "kind": "chat", "payload": {"thread": "main", "message_id": 3, "text": text, "session_id": session_id,
                                                  "settings": {"model": "", "effort": "", "max_turns": 40, "auto_context": True, **settings}}}


HAPPY = [
    init_ev(), msg_start(),
    cb_start(0, {"type": "text", "text": ""}), cb_delta(0, {"type": "text_delta", "text": "你好"}), cb_delta(0, {"type": "text_delta", "text": "世界"}),
    assistant("m1", {"type": "text", "text": "你好世界"}), cb_stop(0), msg_delta(), RATE_LIMIT, result_ev(),
]


def test_run_chat_streams_context_env_and_finishes(tmp_path):
    hooks = RecordingHooks()
    worker, client = make_worker(tmp_path, HAPPY, hooks=hooks)
    worker.run_chat(chat_job())

    assert client.posts[0]["status"] == "streaming"
    assert client.posts[0]["events"][0]["type"] == "status" and client.posts[0]["events"][0]["data"]["phase"] == "started"
    assert client.text == "你好世界\n\n"
    types = [e["type"] for e in client.events()]
    assert types[:2] == ["status", "init"] and types[-1] == "usage" and "rate_limit" in types
    usage = client.events("usage")[0]["data"]
    assert usage["context_tokens"] == 110 and usage["output_tokens"] == 41

    fin = client.finished[0]
    assert fin["ok"] is True and fin["session_id"] == SID
    summary = json.loads(fin["result"])
    assert summary["session_id"] == SID and summary["context_tokens"] == 110 and summary["rate_limit"]["five_hour"]["utilization"] == 0.15
    assert hooks.ctx_calls == [("main", True)] and hooks.finished[0][1] == "你好世界\n\n"

    args = json.loads((tmp_path / "args.json").read_text())
    assert args[0] == "-p" and args[1].startswith("【背景】") and args[1].endswith("【用户消息】\n我有几只股票")
    assert "--include-partial-messages" in args and "--resume" not in args
    assert (tmp_path / "env.txt").read_text() == "thread=main"


def test_run_chat_resume_and_settings_flags(tmp_path):
    worker, client = make_worker(tmp_path, HAPPY, allowed_tools=["Read", "Bash(git log *)"], system_prompt="SP", permission_mode="plan", model="opus")
    worker.run_chat(chat_job(session_id="old", model="sonnet", effort="HIGH", max_turns=500, auto_context=False))
    args = json.loads((tmp_path / "args.json").read_text())
    assert args[:2] == ["-p", "我有几只股票"]  # no context on resume / auto_context off
    assert args[-2:] == ["--resume", "old"]
    i = args.index("--allowedTools")
    assert args[i + 1 : i + 3] == ["Read", "Bash(git log *)"]
    assert args[args.index("--append-system-prompt") + 1] == "SP"
    assert args[args.index("--model") + 1] == "sonnet" and args[args.index("--effort") + 1] == "high"
    assert args[args.index("--max-turns") + 1] == "100" and args[args.index("--permission-mode") + 1] == "plan"
    assert client.events("init")[0]["data"]["resumed"] is True


def test_long_prompt_goes_to_stdin(tmp_path):
    worker, client = make_worker(tmp_path, HAPPY, argv_limit=50)
    worker.run_chat(chat_job(text="长" * 100, auto_context=False))
    args = json.loads((tmp_path / "args.json").read_text())
    assert args[:3] == ["-p", "--output-format", "stream-json"]
    assert (tmp_path / "stdin.txt").read_text() == "长" * 100
    assert client.finished[0]["ok"] is True


def test_cancel_via_events_response(tmp_path):
    lines = [init_ev(), msg_start(), cb_start(0, {"type": "text", "text": ""}), cb_delta(0, {"type": "text_delta", "text": "一"}),
             {"__sleep__": 0.4}, cb_delta(0, {"type": "text_delta", "text": "二"}), {"__sleep__": 30}]
    client = FakeClient()
    client.cancel_on_text = "二"
    worker, _ = make_worker(tmp_path, lines, client=client, cancel_poll_interval=60)
    t0 = time.time()
    worker.run_chat(chat_job())
    assert time.time() - t0 < 5
    fin = client.finished[0]
    assert fin["ok"] is False and fin["cancelled"] is True and fin["session_id"] == SID
    assert client.text == "一二"


def test_cancel_via_poller_during_silence(tmp_path):
    lines = [init_ev(), {"__sleep__": 30}]
    client = FakeClient()
    client.job_state["cancel_requested"] = True
    worker, _ = make_worker(tmp_path, lines, client=client)
    t0 = time.time()
    worker.run_chat(chat_job())
    assert time.time() - t0 < 5 and client.get_job_calls >= 1
    assert client.finished[0]["cancelled"] is True


def test_timeout_kills_silent_claude(tmp_path):
    worker, client = make_worker(tmp_path, [init_ev(), {"__sleep__": 30}], chat_timeout=0.5)
    t0 = time.time()
    worker.run_chat(chat_job())
    assert time.time() - t0 < 5
    fin = client.finished[0]
    assert fin["ok"] is False and fin.get("cancelled", False) is False and fin["error_kind"] == "timeout" and "未完成" in fin["error"]


def test_request_stop_mid_chat(tmp_path):
    worker, client = make_worker(tmp_path, [init_ev(), {"__sleep__": 30}])
    th = threading.Thread(target=worker.run_chat, args=(chat_job(),))
    th.start()
    time.sleep(0.4)
    worker.request_stop()
    th.join(timeout=5)
    assert not th.is_alive() and worker.stopping
    assert client.finished[0]["error_kind"] == "worker" and "重启" in client.finished[0]["error"]


def test_bad_session_resets_and_claude_error_kind(tmp_path):
    worker, client = make_worker(tmp_path, [init_ev(), result_ev(is_error=True, text="No conversation found with session ID old")])
    worker.run_chat(chat_job(session_id="old"))
    fin = client.finished[0]
    assert fin["ok"] is False and fin["reset_session"] is True and fin["error_kind"] == "bad_session"
    assert "No conversation found" in fin["error"]

    worker, client = make_worker(tmp_path, [init_ev(), result_ev(is_error=True, text="Rate limited")])
    worker.run_chat(chat_job())
    assert client.finished[0]["reset_session"] is False and client.finished[0]["error_kind"] == "claude"


def test_no_result_reports_exit_code_and_stderr(tmp_path):
    worker, client = make_worker(tmp_path, [init_ev(), {"__stderr__": "Not logged in · run claude login"}], exit_code=3)
    worker.run_chat(chat_job())
    fin = client.finished[0]
    assert fin["ok"] is False and "exit 3" in fin["error"] and "Not logged in" in fin["error"] and fin["error_kind"] == "claude"


def test_emitter_retries_and_never_drops_text(tmp_path):
    client = FakeClient()
    client.fail_posts = 2
    worker, _ = make_worker(tmp_path, HAPPY, client=client)
    worker.run_chat(chat_job())
    assert client.text == "你好世界\n\n" and client.finished[0]["ok"] is True
    assert client.posts[0]["status"] == "streaming"  # status survived the failed attempts


def test_run_once_dispatch_handlers_unknown_and_errors(tmp_path):
    hooks = RecordingHooks()
    jobs = [
        {"id": 1, "kind": "review", "payload": {"day": "2026-09-22"}},
        {"id": 2, "kind": "boom", "payload": {}},
        {"id": 3, "kind": "nope", "payload": {}},
        {"id": 4, "kind": "text", "payload": {}},
    ]
    client = FakeClient(jobs)

    def review(job, worker):
        return {"day": job["payload"]["day"], "chars": 12}

    def boom(job, worker):
        raise RuntimeError("bad day")

    worker, _ = make_worker(tmp_path, HAPPY, client=client, hooks=hooks, handlers={"review": review, "boom": boom, "text": lambda j, w: "plain"})
    assert worker.kinds == ["chat", "context", "compact", "review", "boom", "text"]
    for _ in range(4):
        assert worker.run_once(wait=0) is True
    assert worker.run_once(wait=0) is False
    f = {x["id"]: x for x in client.finished}
    assert f[1]["ok"] is True and json.loads(f[1]["result"]) == {"day": "2026-09-22", "chars": 12}
    assert f[2]["ok"] is False and "RuntimeError: bad day" in f[2]["error"] and hooks.errors == [(2, "bad day")]
    assert f[3]["ok"] is False and "未知任务类型" in f[3]["error"]
    assert f[4]["ok"] is True and f[4]["result"] == "plain"


def test_run_once_chat_with_broken_binary_reports_failure(tmp_path):
    client = FakeClient([chat_job()])
    worker = Worker(client, WorkerConfig(claude_bin=str(tmp_path / "missing"), cwd=tmp_path))
    assert worker.run_once(wait=0) is True
    assert client.finished[0]["ok"] is False and "FileNotFoundError" in client.finished[0]["error"]


def test_build_chat_command_defaults(tmp_path):
    worker = Worker(FakeClient(), WorkerConfig(claude_bin="claude", include_partial=False))
    cmd, via_stdin = worker.build_chat_command("hi", None, {}, "")
    assert cmd == ["claude", "-p", "hi", "--output-format", "stream-json", "--verbose", "--max-turns", "40"] and via_stdin is False
    cmd, _ = worker.build_chat_command("hi", None, {"effort": "turbo", "max_turns": 0}, "")
    assert "--effort" not in cmd and cmd[cmd.index("--max-turns") + 1] == "40"  # 0 → config default


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_hooks_default_noops(bad):
    h = Hooks()
    assert h.build_context(thread_id="t", is_new_session=True, payload={}) == ""
    assert h.env({}) == {} and h.system_prompt({}) is None
    h.tick()


# ---------------- per-scope profiles ----------------


def test_profiles_pick_tools_permissions_and_per_owner_cwd(tmp_path):
    import json as _json

    from claude_bridge.worker import ChatRunner, WorkerProfile, load_profiles

    pf = tmp_path / "profiles.json"
    pf.write_text(_json.dumps({
        "_comment": "ignored",
        "": {"cwd": str(tmp_path / "chat" / "{owner}"), "tools": ["WebSearch", "WebFetch"],
             "allowed_tools": ["WebSearch", "WebFetch"], "strict_mcp": True},
        "code": {"cwd": str(tmp_path / "code" / "{owner}"), "permission_mode": "bypassPermissions"},
    }))
    profiles = load_profiles(pf)
    assert set(profiles) == {"", "code"} and profiles["code"].permission_mode == "bypassPermissions"
    with pytest.raises(ValueError):
        WorkerProfile.from_dict({"nope": 1})

    worker, _ = make_worker(tmp_path, HAPPY, profiles=profiles, allowed_tools=["Read"])
    chat = worker.config_for({"scope": "", "owner": "3"})
    assert chat.cwd == tmp_path / "chat" / "u3" and chat.cwd.is_dir()
    cmd, _ = worker.build_chat_command("hi", None, {}, "", cfg=chat)
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch" and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--allowedTools") + 1: cmd.index("--allowedTools") + 3] == ["WebSearch", "WebFetch"]
    assert "--permission-mode" not in cmd

    code = worker.config_for({"scope": "code", "owner": "../../etc"})  # the owner never escapes the base dir
    assert code.cwd == tmp_path / "code" / "uetc"
    cmd, _ = worker.build_chat_command("hi", None, {}, "", cfg=code)
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions" and "--tools" not in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == "Read"  # inherited from the base config

    assert worker.config_for({"scope": "other"}) is worker.config  # no profile → base config
    assert worker.config_for({"scope": "code"}).cwd == tmp_path / "code" / "shared"

    job = chat_job()
    job["payload"].update(scope="code", owner="5")
    assert ChatRunner(worker, job).cfg.cwd == tmp_path / "code" / "u5"

    empty = worker.build_chat_command("hi", None, {}, "", cfg=worker.config_for({"scope": ""}))[0]
    worker.config.profiles[""].tools = []
    none = worker.build_chat_command("hi", None, {}, "", cfg=worker.config_for({"scope": ""}))[0]
    assert "--tools" in empty and none[none.index("--tools") + 1] == ""


def test_session_jobs_resume_in_the_profile_cwd(tmp_path):
    from claude_bridge.worker import WorkerProfile

    worker, client = make_worker(tmp_path, HAPPY, profiles={"code": WorkerProfile(cwd=str(tmp_path / "code" / "{owner}"))})
    seen = {}

    def fake_report(session_id, settings=None, cfg=None):
        seen["cwd"] = cfg.cwd
        return {"used": 1, "window": 10, "pct": 10, "categories": []}

    worker.context_report = fake_report
    worker.run_session_job({"id": 9, "kind": "context", "payload": {"thread": "t", "session_id": "s", "scope": "code", "owner": "2"}})
    assert seen["cwd"] == tmp_path / "code" / "u2" and client.finished[-1]["ok"] is True
