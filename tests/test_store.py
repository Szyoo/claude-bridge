import sqlite3
import threading

import pytest

from claude_bridge.store import BridgeStore, auto_title


@pytest.fixture
def store(tmp_path):
    s = BridgeStore(tmp_path / "b.db")
    yield s
    s.close()


def test_requires_exactly_one_of_path_or_conn(tmp_path):
    with pytest.raises(ValueError):
        BridgeStore()
    with pytest.raises(ValueError):
        BridgeStore(tmp_path / "x.db", conn=sqlite3.connect(":memory:"))


def test_shared_connection_does_not_touch_row_factory(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "host.db"), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE host(x)")
    lock = threading.RLock()
    s = BridgeStore(conn=conn, lock=lock)
    s2 = BridgeStore(conn=conn, lock=lock)  # idempotent schema
    assert conn.row_factory is sqlite3.Row
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='bridge_messages'").fetchone() is not None
    s.close()  # must not close a connection it doesn't own
    conn.execute("SELECT 1")
    s2.close()


def test_threads_crud_and_listing(store):
    store.create_thread("t1", scope="stock")
    store.create_thread("t2", scope="review", key="2026-09-22", title="复盘")
    assert store.find_thread("review", "2026-09-22") == "t2"
    assert store.find_thread("review", "2026-01-01") is None

    store.add_message("t1", "user", "请帮我看看 600519 今天怎么样\n第二行")
    store.add_message("t1", "assistant", "看起来不错", status="done")
    items = store.threads("stock")
    assert [t["id"] for t in items] == ["t1"]
    assert items[0]["n"] == 2 and items[0]["preview"] == "看起来不错"
    assert items[0]["title"] == "请帮我看看 600519 今天怎么样"  # first line of first user message
    assert store.threads("review")[0]["title"] == "复盘"
    assert len(store.threads()) == 2

    store.update_thread("t1", title="改名", pinned=True, session_id="sess-1")
    t = store.get_thread("t1")
    assert t["title"] == "改名" and t["pinned"] is True and t["session_id"] == "sess-1"
    store.update_thread("t1", session_id=None)
    assert store.get_thread("t1")["session_id"] is None
    assert store.threads()[0]["id"] == "t1"  # pinned first

    n = store.delete_thread("t1")
    assert n == 2 and store.get_thread("t1") is None and store.messages("t1") == []


def test_messages_events_rev(store):
    store.create_thread("t")
    u = store.add_message("t", "user", "hi")
    a = store.add_message("t", "assistant", "", status="pending")
    assert store.inflight("t")["id"] == a["id"]
    assert store.append_content(a["id"], "你") == 1
    assert store.append_content(a["id"], "好") == 2
    e1 = store.add_event(a["id"], "tool_use", {"id": "tu1", "name": "Bash", "input": {"command": "ls"}})
    e2 = store.add_event(a["id"], "tool_result", {"tool_use_id": "tu1", "content": "a b"})
    assert (e1["seq"], e2["seq"]) == (1, 2) and e2["id"] > e1["id"]
    assert store.last_event_id() == e2["id"]
    assert store.events(a["id"], after_id=e1["id"]) == [e2]

    msgs = store.messages("t")
    assert [m["id"] for m in msgs] == [u["id"], a["id"]]
    assert msgs[1]["content"] == "你好" and msgs[1]["rev"] == 2
    assert [e["type"] for e in msgs[1]["events"]] == ["tool_use", "tool_result"]
    assert msgs[1]["events"][0]["data"]["input"] == {"command": "ls"}
    assert store.messages("t", after_id=u["id"])[0]["id"] == a["id"]
    assert store.messages("t", with_events=False)[1].get("events") is None

    rev = store.set_status(a["id"], "error", append="\n\n> ❌ boom")
    assert rev == 3
    m = store.get_message(a["id"])
    assert m["status"] == "error" and m["content"].endswith("boom") and store.inflight("t") is None


def test_jobs_claim_fifo_kinds_cancel_and_stale(store):
    j1 = store.enqueue_job("chat", {"thread": "t", "text": "a"})
    j2 = store.enqueue_job("review", {"day": "2026-09-22"})
    j3 = store.enqueue_job("chat", {"thread": "t", "text": "b"})
    assert store.claim_job("w", ["review"])["id"] == j2
    got = store.claim_job("w", ["chat"])
    assert got["id"] == j1 and got["status"] == "running" and got["heartbeat_at"] and got["worker"] == "w"
    assert got["payload"]["text"] == "a" and got["cancel_requested"] is False

    # queued → cancelled directly; running → cancel_requested; terminal → noop
    assert store.request_cancel(j3) == "cancelled"
    assert store.get_job(j3)["status"] == "cancelled" and store.claim_job("w", ["chat"]) is None
    assert store.request_cancel(j1) == "cancelling"
    assert store.get_job(j1)["cancel_requested"] is True
    store.finish_job(j1, "done", result="{}")
    assert store.request_cancel(j1) == "noop"
    with pytest.raises(ValueError):
        store.finish_job(j2, "queued")

    # stale detection is heartbeat based
    store.conn.execute("UPDATE bridge_jobs SET heartbeat_at=datetime('now','-10 minutes') WHERE id=?", (j2,))
    assert [j["id"] for j in store.stale_running(120)] == [j2]
    store.touch_job(j2)
    assert store.stale_running(120) == []
    assert store.count_jobs("running") == 1
    assert [j["id"] for j in store.recent_jobs(2)] == [j3, j2]


def test_message_by_job_and_meta(store):
    store.create_thread("t")
    jid = store.enqueue_job("chat", {})
    a = store.add_message("t", "assistant", "", status="pending", job_id=jid)
    assert store.message_by_job(jid)["id"] == a["id"]
    assert store.message_by_job(999) is None
    assert store.get_meta("k") is None
    store.set_meta("k", "v")
    assert store.get_meta("k") == "v"
    store.set_meta("k", None)
    assert store.get_meta("k") is None


def test_wait_for_job_wakes_on_enqueue(store):
    woke = threading.Event()

    def waiter():
        store.wait_for_job(5.0)
        woke.set()

    th = threading.Thread(target=waiter)
    th.start()
    import time

    time.sleep(0.05)
    store.enqueue_job("chat", {})
    assert woke.wait(1.0)
    th.join()


def test_transaction_rolls_back(store):
    store.create_thread("t")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.add_message("t", "user", "x")
            raise RuntimeError("boom")
    assert store.messages("t") == []
    assert not store.conn.in_transaction


def test_auto_title():
    assert auto_title("  \n请分析一下贵州茅台。\n更多") == "请分析一下贵州茅台"
    assert auto_title("a" * 40) == "a" * 30 + "…"
    assert auto_title("") == ""
