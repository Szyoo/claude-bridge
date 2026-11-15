from copy import deepcopy

from claude_bridge.stream_json import StreamState


def test_native_stream_preserves_thinking_signature_and_parameter_fragments():
    state = StreamState(emit_content_blocks=True, thinking_max=3)
    original = [
        {"type": "message_start", "message": {"id": "native-1", "model": "sonnet"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "完整思考内容"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "opaque-"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "signature"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "tool-1", "name": "Echo", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"value":"'},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '你好"}'},
        },
        {"type": "content_block_stop", "index": 1},
    ]
    emissions = []
    for event in original:
        emissions += state.feed({"type": "stream_event", "event": deepcopy(event)})
    raw = [data["data"] for kind, data in emissions if kind == "event" and data["type"] == "content_stream"]
    assert [row["event"] for row in raw] == original
    assert all(row["native_message_id"] == "native-1" for row in raw)
    preview = [data["data"] for kind, data in emissions if kind == "event" and data["type"] == "thinking"][0]
    assert preview["truncated"] is True
    full = {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "完整思考内容", "signature": "opaque-signature"},
            {"type": "redacted_thinking", "data": "opaque-redacted"},
        ],
    }
    out = state.feed({"type": "assistant", "message": full})
    message = next(
        data["data"]["message"] for kind, data in out if kind == "event" and data["type"] == "content_message"
    )
    assert message == full
    full["content"][0]["signature"] = "changed after emission"
    assert message["content"][0]["signature"] == "opaque-signature"


def test_legacy_preview_is_unchanged_and_no_signature_is_fabricated():
    st = StreamState()
    assert st.feed(
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "short"}]}}
    ) == [("event", {"type": "thinking", "data": {"text": "short", "truncated": False, "at": 0}})]
    native = StreamState(emit_content_blocks=True)
    out = native.feed(
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "short"}]}}
    )
    payload = next(
        data["data"]["message"] for kind, data in out if kind == "event" and data["type"] == "content_message"
    )
    assert "signature" not in payload["content"][0]


def test_raw_text_contains_no_legacy_display_separators():
    st = StreamState(emit_content_blocks=True)
    out = []
    for e in [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "answer"}},
        {"type": "content_block_stop", "index": 0},
    ]:
        out += st.feed({"type": "stream_event", "event": e})
    assert st.text == "answer\n\n"
    assert [
        d["data"]["event"]["delta"]["text"]
        for k, d in out
        if k == "event"
        and d["type"] == "content_stream"
        and d["data"]["event"]["type"] == "content_block_delta"
    ] == ["answer"]


def test_page_reads_only_thinking_streams_and_collapses_finished_ones(tmp_path):
    from claude_bridge.store import BridgeStore

    store = BridgeStore(tmp_path / "b.db")
    th = store.create_thread("t1", owner="1")
    msg = store.add_message(th["id"], "assistant", "", status="streaming")
    ev = lambda e: store.add_event(msg["id"], "content_stream", {"native_message_id": "m1", "at": 0, "event": e})
    ev({"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 9}}})
    ev({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
    for part in ("想", "一下"):
        ev({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": part}})
    ev({"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "secret"}})
    ev({"type": "content_block_stop", "index": 0})
    ev({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
    for part in ("答", "案"):
        ev({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": part}})
    ev({"type": "content_block_stop", "index": 1})

    live = store.messages(th["id"])[0]["events"]
    assert [e["data"]["event"]["type"] for e in live] == [
        "content_block_start", "content_block_delta", "content_block_delta", "content_block_delta",
        "content_block_stop", "content_block_stop"]

    store.set_status(msg["id"], "done")
    done = [e["data"]["event"] for e in store.messages(th["id"])[0]["events"]]
    assert done[0] == {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "想一下"}}
    assert done[1]["delta"] == {"type": "signature_delta", "signature": "1"}
    assert [e["type"] for e in done[2:]] == ["content_block_stop"]
    assert "secret" not in str(done)


def test_chat_scopes_relay_only_thinking_stream_rows():
    from claude_bridge.stream_json import StreamState

    st = StreamState(emit_content_blocks=True, full_stream=False)
    feed = lambda e: [x for x in st.feed({"type": "stream_event", "event": e}) if x[0] == "event" and x[1]["type"] == "content_stream"]
    assert not feed({"type": "message_start", "message": {"id": "m1"}})
    assert feed({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
    assert feed({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "x"}})
    assert feed({"type": "content_block_stop", "index": 0})
    assert not feed({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
    assert not feed({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "y"}})
    assert not feed({"type": "content_block_stop", "index": 1})
    assert "y" in st.text
