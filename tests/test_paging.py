import json

from claude_bridge.store import BridgeStore


def _stream(store, mid, nid, kind, **ev):
    store.add_event(mid, "content_stream", {"native_message_id": nid, "event": {"type": kind, **ev}})


def test_compact_message_stream_keeps_what_the_page_renders(tmp_path):
    s = BridgeStore(tmp_path / "t.db")
    s.create_thread("t")
    mid = s.add_message("t", "assistant", "答案")["id"]
    _stream(s, mid, "n1", "content_block_start", index=0, content_block={"type": "thinking", "thinking": ""})
    for ch in "想一想" * 30:
        _stream(s, mid, "n1", "content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": ch})
    _stream(s, mid, "n1", "content_block_delta", index=0, delta={"type": "signature_delta", "signature": "abc"})
    _stream(s, mid, "n1", "content_block_stop", index=0)
    _stream(s, mid, "n1", "content_block_start", index=1, content_block={"type": "text", "text": ""})
    for ch in "答案" * 20:
        _stream(s, mid, "n1", "content_block_delta", index=1, delta={"type": "text_delta", "text": ch})
    s.add_event(mid, "tool_use", {"name": "Bash"})
    before = s.messages("t")[0]["events"]
    dropped = s.compact_message_stream(mid)
    assert dropped > 100
    after = s.messages("t")[0]["events"]
    strip = lambda evs: [(e["type"], json.dumps(e["data"], sort_keys=True)) for e in evs]
    assert strip(after) == strip(before)
    assert s.compact_message_stream(mid) == 0  # idempotent


def test_messages_before_pages_upward(tmp_path):
    s = BridgeStore(tmp_path / "t.db")
    s.create_thread("t")
    ids = [s.add_message("t", "user", f"m{i}")["id"] for i in range(7)]
    page = s.messages("t", limit=3, tail=True)
    assert [m["id"] for m in page] == ids[-3:] and s.has_older("t", page[0]["id"])
    page = s.messages("t", limit=3, tail=True, before_id=ids[-3])
    assert [m["id"] for m in page] == ids[1:4]
    page = s.messages("t", limit=3, tail=True, before_id=ids[1])
    assert [m["id"] for m in page] == ids[:1] and not s.has_older("t", ids[0])
