"""Writes must reach the file even after something leaves the shared connection's transaction open."""

import sqlite3

import pytest

from claude_bridge.store import BridgeStore


def _other(path):
    return sqlite3.connect(path)


def test_returning_cursor_does_not_hold_the_transaction(tmp_path):
    db = tmp_path / "b.db"
    store = BridgeStore(db)
    store.create_thread("t1", owner="1")
    mid = store.add_message("t1", "user", "hi")["id"]
    kept = store._x("INSERT INTO bridge_events(message_id, seq, type, data, created_at) VALUES(?,1,'x','{}','now') RETURNING *", (mid,))
    assert kept.fetchone() is not None  # caller keeps the result object alive …
    assert not store.conn.in_transaction  # … but the statement is finished and committed
    assert _other(db).execute("SELECT COUNT(*) FROM bridge_events WHERE type='x'").fetchone()[0] == 1


def test_a_leaked_transaction_is_committed_by_the_next_statement(tmp_path):
    db = tmp_path / "b.db"
    store = BridgeStore(db)
    store.conn.execute("BEGIN")  # something left it open
    store.set_meta("k", "v1")
    store.set_meta("k2", "v2")  # next statement notices and commits what piled up
    assert not store.conn.in_transaction
    assert dict(_other(db).execute("SELECT key, value FROM bridge_meta WHERE key IN ('k','k2')").fetchall()) == {"k": "v1", "k2": "v2"}


def test_transaction_rolls_back_and_reopens_autocommit_on_error(tmp_path):
    store = BridgeStore(tmp_path / "b.db")
    with pytest.raises(RuntimeError), store.transaction():
        store.set_meta("k", "v")
        raise RuntimeError
    assert not store.conn.in_transaction and store.get_meta("k") is None
    with store.transaction(), store.transaction():  # nesting still commits once, at the outermost
        store.set_meta("k", "v")
    assert not store.conn.in_transaction
    assert _other(tmp_path / "b.db").execute("SELECT value FROM bridge_meta WHERE key='k'").fetchone()[0] == "v"
