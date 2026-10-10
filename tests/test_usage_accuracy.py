"""Usage accounting and calibration: token pricing, ledger re-pricing, transcript scanner, capacity estimate, API, worker."""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from claude_bridge import accounts as accounts_mod
from claude_bridge.accounts import Accounts
from claude_bridge.broker import Broker
from claude_bridge.calibration import MIN_GROUPS, MIN_UTILIZATION, estimate_capacity
from claude_bridge.client import BridgeClientError
from claude_bridge.local_usage import BUCKET_SECONDS, TranscriptScanner, bucket_text
from claude_bridge.multiuser import create_multiuser_app
from claude_bridge.pricing import DEFAULT_MODEL, has_tokens, price_key, token_cost, usage_cost
from claude_bridge.service import BridgeService
from claude_bridge.store import BridgeStore
from claude_bridge.worker import Worker, WorkerConfig, WorkerProfile
from test_worker import FakeClient

M = 1_000_000

# ---------------- pricing ----------------


@pytest.mark.parametrize(
    ("model", "inp", "out", "read", "w5", "w1"),
    [
        ("claude-sonnet-5-5", 2.0, 10.0, 0.20, 2.5, 4.0),
        ("claude-opus-5-5", 4.0, 20.0, 0.20, 5.0, 8.0),
        ("claude-haiku-5-5", 0.10, 0.50, 0.01, 0.125, 0.20),
    ],
)
def test_token_cost_per_kind(model, inp, out, read, w5, w1):
    assert token_cost(model, input_tokens=M) == pytest.approx(inp)
    assert token_cost(model, output_tokens=M) == pytest.approx(out)
    assert token_cost(model, cache_read=M) == pytest.approx(read)
    assert token_cost(model, cache_write=M) == pytest.approx(w5)  # 5-minute TTL: 1.25x input
    assert token_cost(model, cache_write=M, cache_write_1h=M) == pytest.approx(w1)  # 1-hour TTL: 2x input
    assert token_cost(model, cache_write=M, cache_write_1h=M // 4) == pytest.approx(w5 * 0.75 + w1 * 0.25)
    # the 1h part can't exceed the total written
    assert token_cost(model, cache_write=1000, cache_write_1h=5000) == pytest.approx(1000 * w1 / M)


def test_usage_cost_reads_cache_creation_breakdown():
    usage = {"input_tokens": 1000, "output_tokens": 2000, "cache_read_input_tokens": 3000,
             "cache_creation_input_tokens": 4000,
             "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 3000}}
    expect = (1000 * 2 + 2000 * 10 + 3000 * 0.2 + 1000 * 2 * 1.25 + 3000 * 2 * 2.0) / M
    assert usage_cost("claude-sonnet-5-5", usage) == pytest.approx(expect)
    plain = {k: v for k, v in usage.items() if k != "cache_creation"}
    assert usage_cost("claude-sonnet-5-5", plain) == pytest.approx((1000 * 2 + 2000 * 10 + 3000 * 0.2 + 4000 * 2 * 1.25) / M)


@pytest.mark.parametrize(
    ("model", "key"),
    [
        ("claude-sonnet-5-5", "claude-sonnet-5-5"),
        ("claude-opus-4-8-20260115", "claude-opus-4-8"),
        ("claude-sonnet-4-6[1m]", "claude-sonnet-4-6"),
        ("CLAUDE-OPUS-5-5", "claude-opus-5-5"),
        ("anthropic.claude-haiku-4-5", "claude-haiku-4-5"),
        ("us.anthropic.claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
        ("claude-opus-9-9", "claude-opus-5-5"),  # unknown version: newest of the family
        ("claude-fable-7", "claude-fable-5-1"),
        ("mystery-model", DEFAULT_MODEL),
        ("", DEFAULT_MODEL),
        (None, DEFAULT_MODEL),
    ],
)
def test_price_key(model, key):
    assert price_key(model) == key


def test_negative_and_none_tokens_count_as_zero():
    assert token_cost("claude-sonnet-5-5", input_tokens=-5, output_tokens=None, cache_read="x", cache_write=-1) == 0.0
    assert token_cost("claude-sonnet-5-5", input_tokens=-5, output_tokens=M) == pytest.approx(10.0)
    assert usage_cost("claude-sonnet-5-5", {"input_tokens": None, "output_tokens": -1}) == 0.0


def test_has_tokens():
    assert has_tokens({"output_tokens": 0})
    assert not has_tokens({"total_cost_usd": 3.0, "model": "x"})
    assert not has_tokens({})


# ---------------- ledger: from tokens, never total_cost_usd ----------------


def make_service(tmp_path, name="s.db") -> tuple[BridgeStore, BridgeService]:
    store = BridgeStore(tmp_path / name)
    return store, BridgeService(store, Broker())


def ledger(store):
    return store._q("SELECT * FROM bridge_usage ORDER BY id")


def test_resumed_session_total_cost_is_ignored(tmp_path):
    # live bug: on a resumed session the CLI's total_cost_usd is the whole session's running total
    store, svc = make_service(tmp_path)
    data = {"total_cost_usd": 16.15, "model": "claude-opus-5-5", "input_tokens": 0, "output_tokens": 142,
            "cache_read_input_tokens": 322_710, "cache_creation_input_tokens": 29_739}
    svc._record_usage("7", data, kind="chat")
    (row,) = ledger(store)
    expect = (322_710 * 0.20 + 29_739 * 4.0 * 1.25 + 142 * 20.0) / M
    assert row["cost_usd"] == pytest.approx(expect) and 0.1 < row["cost_usd"] < 0.3
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]) == (0, 142, 322_710, 29_739)
    assert row["model"] == "claude-opus-5-5" and row["owner"] == "7"


def test_usage_without_tokens_is_not_booked(tmp_path):
    store, svc = make_service(tmp_path)
    svc._record_usage("", {"total_cost_usd": 5.0, "model": "claude-sonnet-5-5"}, kind="chat")
    assert ledger(store) == []


def test_compact_cost_from_usage_or_pre_post_tokens(tmp_path):
    _, svc = make_service(tmp_path)
    cost, usage = svc._compact_cost({"model": "claude-sonnet-5-5", "usage": {"output_tokens": 30_000}, "cost_usd": 9.9})
    assert cost == pytest.approx(0.3) and usage == {"output_tokens": 30_000}
    cost, usage = svc._compact_cost({"model": "claude-sonnet-5-5", "pre_tokens": 100_000, "post_tokens": 5_000})
    assert cost == pytest.approx(100_000 * 0.2 / M + 5_000 * 10 / M) and usage == {}


# ---------------- reprice_ledger ----------------


def seed_old_ledger(store):
    store.create_thread("t1", owner="3")
    msg = store.add_message("t1", "assistant", "hi")
    usage = {"total_cost_usd": 15.96, "model": "claude-sonnet-5-5", "input_tokens": 10, "output_tokens": 1000,
             "cache_read_input_tokens": 200_000, "cache_creation_input_tokens": 20_000}
    store.add_event(msg["id"], "usage", usage)
    store.add_usage("3", cost_usd=15.96, thread="t1", message_id=msg["id"], kind="chat", model="claude-sonnet-5-5")
    job = store.enqueue_job("compact", {"thread": "t1"})
    store.finish_job(job, "done", json.dumps({"compact": {"pre_tokens": 80_000, "post_tokens": 4_000, "model": "claude-sonnet-5-5"}}))
    store.add_usage("3", cost_usd=7.5, thread="t1", job_id=job, kind="compact", model="claude-sonnet-5-5")
    return usage


def test_reprice_ledger_rewrites_old_rows_once(tmp_path):
    store = BridgeStore(tmp_path / "r.db")
    usage = seed_old_ledger(store)
    svc = BridgeService(store, Broker())

    chat, compact = ledger(store)
    assert chat["cost_usd"] == pytest.approx(usage_cost("claude-sonnet-5-5", usage))
    assert chat["cost_usd"] < 1
    assert (chat["input_tokens"], chat["output_tokens"], chat["cache_read_tokens"], chat["cache_write_tokens"]) == (10, 1000, 200_000, 20_000)
    assert compact["cost_usd"] == pytest.approx(80_000 * 0.2 / M + 4_000 * 10 / M)  # reads the context, writes the summary
    assert store.get_meta("usage_repriced") == "1"

    # a second service on the same database leaves the ledger alone
    store.set_usage_cost(chat["id"], 123.0)
    assert svc.reprice_ledger() == 0
    BridgeService(store, Broker())
    assert ledger(store)[0]["cost_usd"] == 123.0


def test_reprice_survives_broken_event_and_missing_job(tmp_path):
    store = BridgeStore(tmp_path / "r2.db")
    store.create_thread("t1", owner="3")
    msg = store.add_message("t1", "assistant", "hi")
    store._x("INSERT INTO bridge_events(message_id, seq, type, data, created_at) VALUES(?,1,'usage','not json','2026-01-01 00:00:00')", (msg["id"],))
    store.add_usage("3", cost_usd=2.0, thread="t1", message_id=msg["id"])
    store.add_usage("3", cost_usd=3.0, thread="t1", job_id=999, kind="compact")
    BridgeService(store, Broker())  # must not raise
    assert store.get_meta("usage_repriced") == "1"


# ---------------- utilization samples ----------------


def test_backfill_util_samples_from_rate_limit_events_once(tmp_path):
    store = BridgeStore(tmp_path / "b.db")
    store.create_thread("t1")
    msg = store.add_message("t1", "assistant", "hi")
    r1, r2 = time.time() + 3600, time.time() + 5 * 86400
    store.add_event(msg["id"], "rate_limit", {"five_hour": {"utilization": 0.05, "resets_at": r1}, "seven_day": {"utilization": 0.01, "resets_at": r2}})
    store.add_event(msg["id"], "rate_limit", {"five_hour": {"utilization": 0.08, "resets_at": r1}, "seven_day": {"utilization": 0.02, "resets_at": r2}})
    svc = BridgeService(store, Broker())

    five = store.util_samples("five_hour", 0)
    seven = store.util_samples("seven_day", 0)
    assert [s["utilization"] for s in five] == [0.05, 0.08] and [s["utilization"] for s in seven] == [0.01, 0.02]
    assert all(s["resets_at"] == r1 and s["source"] == "turn" for s in five)
    assert store.get_meta("util_samples_backfilled") == "1"

    store.add_event(msg["id"], "rate_limit", {"five_hour": {"utilization": 0.5, "resets_at": r1}})
    assert svc.backfill_util_samples() == 0
    BridgeService(store, Broker())
    assert len(store.util_samples("five_hour", 0)) == 2


def test_sample_dedupes_unchanged_readings(tmp_path):
    store, svc = make_service(tmp_path)

    def put(u, at, resets=5000.0):
        svc._sample("five_hour", {"utilization": u, "resets_at": resets, "at": at, "source": "probe"})

    t = 1_000_000.0
    put(0.10, t)
    put(0.10, t + 60)  # same value, 1 minute later: skipped
    put(0.10, t + 899)  # still inside 15 minutes
    assert len(store.util_samples("five_hour", 0)) == 1
    put(0.11, t + 900)  # changed: stored at once
    put(0.11, t + 960)
    assert [s["utilization"] for s in store.util_samples("five_hour", 0)] == [0.10, 0.11]
    put(0.11, t + 900 + 900)  # unchanged but 15 minutes since the last stored one
    assert len(store.util_samples("five_hour", 0)) == 3
    put(0.11, t + 1800 + 10, resets=9000.0)  # a new window with the same reading counts as a change
    assert len(store.util_samples("five_hour", 0)) == 4
    put(0.5, t + 1800 + 10)
    assert len(store.util_samples("seven_day", 0)) == 0  # windows are separate


# ---------------- TranscriptScanner ----------------


def tline(mid, ts, out=10, *, cwd="/x", model="claude-sonnet-5-5", type="assistant", inp=0, cr=0, cw=0, usage=True):
    msg: dict = {"id": mid, "model": model, "role": "assistant", "content": []}
    if usage:
        msg["usage"] = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1000):03d}Z"
    return json.dumps({"type": type, "timestamp": iso, "cwd": cwd, "message": msg, "uuid": f"u-{mid}"})


def write_lines(path: Path, *lines: str, newline_at_end: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + ("\n" if newline_at_end else "")
    with path.open("ab") as f:
        f.write(text.encode())


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    bridge = tmp_path / "bridge-work"
    other = tmp_path / "elsewhere"
    (bridge / "sub").mkdir(parents=True)
    other.mkdir()
    base = (int(time.time()) - 3600) // 60 * 60  # an aligned minute an hour ago
    return {"root": root, "bridge": str(bridge), "sub": str(bridge / "sub"), "other": str(other), "base": float(base)}


def scanner(tree, **kw):
    return TranscriptScanner(bridge_roots=[tree["bridge"]], root=tree["root"], **kw)


def by_key(rows):
    return {(r["bucket"], r["source"]): r for r in rows}


def test_scanner_sums_nested_files_by_bucket_and_source(tree):
    b = tree["base"]
    write_lines(tree["root"] / "proj-a" / "s1.jsonl",
                tline("m1", b + 5, out=100, inp=10, cr=1000, cw=200, cwd=tree["sub"]),
                tline("m2", b + 30, out=50, cwd=tree["sub"]),
                tline("m3", b + 70, out=7, cwd=tree["other"]))
    write_lines(tree["root"] / "proj-b" / "sub" / "agent.jsonl", tline("m4", b + 10, out=20, cwd=tree["other"]))
    rows = by_key(scanner(tree).scan())

    br = rows[(bucket_text(b), "bridge")]
    assert br["requests"] == 2 and br["output_tokens"] == 150 and br["input_tokens"] == 10
    assert br["cache_read_tokens"] == 1000 and br["cache_write_tokens"] == 200
    assert br["cost_usd"] == pytest.approx((10 * 2 + 150 * 10 + 1000 * 0.2 + 200 * 2.5) / M)
    assert rows[(bucket_text(b), "local")]["output_tokens"] == 20  # nested file, other cwd
    assert rows[(bucket_text(b + 60), "local")]["requests"] == 1
    assert len(rows) == 3
    assert set(next(iter(rows.values()))) == {"bucket", "source", "model", "cost_usd", "input_tokens", "output_tokens",
                                              "cache_read_tokens", "cache_write_tokens", "requests"}


def test_scanner_splits_models_within_a_minute(tree):
    b = tree["base"]
    write_lines(tree["root"] / "p" / "m.jsonl",
                tline("m-1", b + 1, 100, model="claude-opus-5-5-20260901"),
                tline("m-2", b + 2, 100, model="claude-sonnet-5-5"),
                tline("m-3", b + 3, 100, model="claude-sonnet-5-5[1m]"))
    rows = {(r["model"]): r for r in scanner(tree).scan()}
    assert set(rows) == {"claude-opus-5-5", "claude-sonnet-5-5"}  # normalized ids, one row each
    assert rows["claude-sonnet-5-5"]["requests"] == 2 and rows["claude-opus-5-5"]["output_tokens"] == 100


def test_scanner_bridge_root_is_a_directory_prefix(tree, tmp_path):
    sibling = tmp_path / "bridge-work2"
    sibling.mkdir()
    write_lines(tree["root"] / "p" / "s.jsonl",
                tline("a", tree["base"], cwd=tree["bridge"]),  # the root itself
                tline("b", tree["base"], cwd=str(sibling)),  # shares the string prefix only
                tline("c", tree["base"], cwd=""))
    rows = by_key(scanner(tree).scan())
    assert rows[(bucket_text(tree["base"]), "bridge")]["requests"] == 1
    assert rows[(bucket_text(tree["base"]), "local")]["requests"] == 2


def test_scanner_bridge_root_is_resolved_through_symlinks(tree, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tree["bridge"])
    write_lines(tree["root"] / "p" / "s.jsonl", tline("a", tree["base"], cwd=str(link / "sub")))
    sc = TranscriptScanner(bridge_roots=[str(link)], root=tree["root"])
    assert sc.scan()[0]["source"] == "bridge"
    sc2 = TranscriptScanner(bridge_roots=[tree["bridge"]], root=tree["root"])
    assert sc2.scan()[0]["source"] == "bridge"


def test_scanner_same_message_id_booked_once_with_larger_tokens(tree):
    b = tree["base"]
    write_lines(tree["root"] / "p" / "s.jsonl", tline("m1", b, out=10), tline("m1", b, out=50), tline("m1", b, out=30))
    (row,) = scanner(tree).scan()
    assert row["requests"] == 1 and row["output_tokens"] == 50
    assert row["cost_usd"] == pytest.approx(50 * 10 / M)


def test_scanner_same_message_id_across_scans_books_only_growth(tree):
    b = tree["base"]
    path = tree["root"] / "p" / "s.jsonl"
    write_lines(path, tline("m1", b, out=10))
    sc = scanner(tree)
    assert sc.scan()[0]["output_tokens"] == 10
    write_lines(path, tline("m1", b, out=40))
    (row,) = sc.scan()
    assert row["requests"] == 1 and row["output_tokens"] == 40 and row["cost_usd"] == pytest.approx(40 * 10 / M)


def test_scanner_ignores_synthetic_user_and_usageless_lines(tree):
    b = tree["base"]
    write_lines(tree["root"] / "p" / "s.jsonl",
                tline("s1", b, model="<synthetic>"),
                tline("u1", b, type="user"),
                tline("n1", b, usage=False),
                "not json {\"usage\": 1",
                json.dumps({"type": "assistant", "message": {"id": "x", "model": "claude-sonnet-5-5", "usage": {"output_tokens": 5}}}),  # no timestamp
                tline("ok", b, out=3))
    (row,) = scanner(tree).scan()
    assert row["requests"] == 1 and row["output_tokens"] == 3


def test_scanner_ignores_lines_older_than_horizon(tree):
    now = time.time()
    write_lines(tree["root"] / "p" / "s.jsonl",
                tline("old", now - 9 * 86400, out=99),
                tline("new", now - 7 * 86400, out=1))
    rows = scanner(tree).scan(now=now)
    assert [r["output_tokens"] for r in rows] == [1]
    assert [r["output_tokens"] for r in scanner(tree, horizon_days=1).scan(now=now)] == []


def test_scanner_unfinished_last_line_waits_for_newline(tree):
    b = tree["base"]
    path = tree["root"] / "p" / "s.jsonl"
    write_lines(path, tline("m1", b, out=1), tline("m2", b + 120, out=2), newline_at_end=False)
    sc = scanner(tree)
    rows = sc.scan()
    assert [r["output_tokens"] for r in rows] == [1]  # m2 has no newline yet
    assert sc.scan() == []
    with path.open("ab") as f:
        f.write(b"\n")
    rows = sc.scan()
    assert [(r["bucket"], r["output_tokens"]) for r in rows] == [(bucket_text(b + 120), 2)]


def test_scanner_second_scan_is_empty_and_only_changed_buckets_return(tree):
    b = tree["base"]
    path = tree["root"] / "p" / "s.jsonl"
    write_lines(path, tline("m1", b, out=1), tline("m2", b + 120, out=2))
    sc = scanner(tree)
    assert len(sc.scan()) == 2
    assert sc.scan() == []
    write_lines(path, tline("m3", b + 240, out=3))
    rows = sc.scan()
    assert [(r["bucket"], r["output_tokens"]) for r in rows] == [(bucket_text(b + 240), 3)]
    write_lines(path, tline("m4", b + 120, out=5))  # an old bucket grows: its absolute total comes back
    (row,) = sc.scan()
    assert row["bucket"] == bucket_text(b + 120) and row["output_tokens"] == 7 and row["requests"] == 2


def test_scanner_requeue_returns_the_buckets_again(tree):
    b = tree["base"]
    write_lines(tree["root"] / "p" / "s.jsonl", tline("m1", b, out=1), tline("m2", b + 120, out=2))
    sc = scanner(tree)
    rows = sc.scan()
    assert sc.take_dirty() == []
    sc.requeue(rows)
    assert sc.take_dirty() == rows
    assert sc.take_dirty() == []
    sc.requeue(rows[:1])
    write_lines(tree["root"] / "p" / "s.jsonl", tline("m3", b + 240, out=3))
    assert [r["bucket"] for r in sc.scan()] == [rows[0]["bucket"], bucket_text(b + 240)]


def test_scanner_truncated_or_replaced_file_rereads_without_double_counting(tree):
    b = tree["base"]
    path = tree["root"] / "p" / "s.jsonl"
    l1, l2, l3 = tline("m1", b, out=1), tline("m2", b + 120, out=2), tline("m3", b + 240, out=3)
    write_lines(path, l1, l2)
    sc = scanner(tree)
    sc.scan()

    path.write_text(l1 + "\n")  # truncated to something shorter: re-read from the start, m1 already known
    assert sc.scan() == []

    new = path.with_suffix(".tmp")  # replaced by a different file (new inode) that is longer
    new.write_text("\n".join([l1, l2, l3]) + "\n")
    os.replace(new, path)
    rows = sc.scan()
    assert [(r["bucket"], r["output_tokens"], r["requests"]) for r in rows] == [(bucket_text(b + 240), 3, 1)]
    assert sc.scan() == []


def test_scanner_fresh_instance_reproduces_absolute_totals(tree):
    b = tree["base"]
    write_lines(tree["root"] / "p" / "s.jsonl", tline("m1", b, out=1), tline("m1", b, out=4), tline("m2", b + 10, out=2))
    a = scanner(tree).scan()
    assert a == scanner(tree).scan() and a[0]["output_tokens"] == 6


def test_scanner_missing_root_is_empty(tmp_path):
    assert TranscriptScanner(root=tmp_path / "nope").scan() == []


def test_scanner_bucket_text_is_utc_minute():
    assert bucket_text(1_800_000_059) == bucket_text(1_800_000_040 - 1_800_000_040 % BUCKET_SECONDS)
    assert bucket_text(0) == "1970-01-01 00:00:00"


# ---------------- calibration ----------------

T0 = 1_800_000_000 // 60 * 60
SPAN = 5 * 3600


def minute_series(minutes: int, usd: float = 0.05, start: float = T0) -> list[tuple[float, float]]:
    return [(start + i * 60, usd) for i in range(minutes)]


def real_samples(windows: int, *, cap: float = 100.0, external: float = 1.0, every: int = 17, start: float = T0):
    """Each window uses 0.05 USD/min; the server reports floor(share of cap) in whole percent."""
    out = []
    for w in range(windows):
        ws = start + w * SPAN
        for m in range(every, SPAN // 60, every):
            cum = 0.05 * m * external
            out.append({"at": ws + m * 60, "utilization": math.floor(cum / cap * 100 + 1e-9) / 100, "resets_at": ws + SPAN})
    return out


def test_estimate_capacity_recovers_true_cap():
    est = estimate_capacity(real_samples(3), minute_series(3 * 300), SPAN)
    assert est is not None
    assert 85 <= est["cap_usd"] <= 115
    assert est["low"] <= est["cap_usd"] <= est["high"] and est["groups"] >= MIN_GROUPS


def test_estimate_capacity_with_external_use_is_lower_but_present():
    est = estimate_capacity(real_samples(3, external=1.5), minute_series(3 * 300), SPAN)
    assert est is not None
    assert 50 < est["cap_usd"] < 90  # the machine only sees two thirds of the use


def sample_at(m: int, u: float, ws: float = T0):
    return {"at": ws + m * 60, "utilization": u, "resets_at": ws + SPAN}


def test_estimate_capacity_needs_enough_groups():
    series = minute_series(300)
    three = [sample_at(61, 0.03), sample_at(81, 0.04), sample_at(101, 0.05), sample_at(102, 0.05)]  # 3 distinct levels
    assert estimate_capacity(three, series, SPAN) is None
    four = [*three, sample_at(121, 0.06)]
    est = estimate_capacity(four, series, SPAN)
    assert est and est["groups"] == 4
    assert estimate_capacity([], series, SPAN) is None
    assert estimate_capacity(four, [], SPAN) is None


def test_estimate_capacity_ignores_low_utilization():
    series = minute_series(300)
    good = [sample_at(61, 0.03), sample_at(81, 0.04), sample_at(101, 0.05)]
    low = [sample_at(10, 0.01), sample_at(20, MIN_UTILIZATION - 0.001), sample_at(30, 0.0)]
    assert estimate_capacity([*good, *low], series, SPAN) is None  # still 3 groups
    est = estimate_capacity([*good, *low, sample_at(121, 0.06)], series, SPAN)
    assert est and est["groups"] == 4


def test_estimate_capacity_ignores_windows_before_the_series_and_bad_rows():
    series = minute_series(300)
    good = [sample_at(61, 0.03), sample_at(81, 0.04), sample_at(101, 0.05), sample_at(121, 0.06)]
    early = [sample_at(100, 0.20, ws=T0 - 60), sample_at(100, 0.30, ws=T0 - SPAN)]  # window starts before the series does
    junk = [{"at": T0 + 6000, "utilization": None, "resets_at": T0 + SPAN}, {"at": "x", "utilization": 0.5, "resets_at": T0 + SPAN},
            {"at": T0 - 1000, "utilization": 0.5, "resets_at": T0 + SPAN}]  # the last: sampled before its window began
    est = estimate_capacity([*good, *early, *junk], series, SPAN)
    assert est and est["groups"] == 4
    assert estimate_capacity([*good[:3], *early, *junk], series, SPAN) is None


def test_estimate_capacity_pools_repeated_readings_of_one_level():
    series = minute_series(300)
    samples = [sample_at(61, 0.03), sample_at(62, 0.03), sample_at(63, 0.03), sample_at(81, 0.04), sample_at(101, 0.05), sample_at(121, 0.06)]
    assert estimate_capacity(samples, series, SPAN)["groups"] == 4


# ---------------- API: local usage, calibration, shares ----------------


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_mod, "PBKDF2_ITERATIONS", 1000)


PW = "password-1"


@pytest.fixture
def app(tmp_path):
    db = tmp_path / "m.db"
    store = BridgeStore(db)
    acc = Accounts(store)
    acc.create("boss", PW, role="admin")
    acc.create("alice", PW, display_name="Alice")
    acc.create("bob", PW)
    store.close()
    return create_multiuser_app(db_path=db, agent_token="tok", tz="UTC")


@pytest.fixture
def agent(app):
    return TestClient(app, headers={"Authorization": "Bearer tok"})


def login(app, username) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    assert c.post("/login", data={"username": username, "password": PW}).status_code == 303
    return c


def run_turn(web, agent, cost, rate_limit=None):
    assert web.post("/api/send", json={"text": "hi"}).status_code == 201
    job = agent.post("/api/agent/jobs/next", json={"kinds": ["chat"], "wait": 0}).json()["job"]
    usage = {"total_cost_usd": 999.0, "input_tokens": 0, "output_tokens": round(cost * 100_000), "model": "claude-sonnet-5-5"}
    events = [{"type": "usage", "data": usage}]
    if rate_limit:
        events.insert(0, {"type": "rate_limit", "data": rate_limit})
    agent.post(f"/api/agent/jobs/{job['id']}/events", json={"deltas": ["ok"], "events": events})
    agent.post(f"/api/agent/jobs/{job['id']}/finish", json={"ok": True, "result": "{}", "session_id": "s"})


def bucket(minutes_ago: int, source="local", cost=0.5, **kw):
    ts = time.time() - minutes_ago * 60
    return {"bucket": bucket_text(ts), "source": source, "cost_usd": cost, "input_tokens": 1, "output_tokens": 2,
            "cache_read_tokens": 3, "cache_write_tokens": 4, "requests": 5, **kw}


def five_hour(app) -> dict:
    return login(app, "boss").get("/api/admin/users").json()["account"]["five_hour"]


def test_local_usage_endpoint_validates_and_overwrites(app, agent):
    good = bucket(10)
    for bad in ({**good, "bucket": "2026-10-11 12:34:56"}, {**good, "bucket": "yesterday"}, {**good, "source": "cloud"},
                {**good, "cost_usd": -1}, {**good, "requests": -3}):
        assert agent.post("/api/agent/local-usage", json={"buckets": [bad]}).status_code == 422
    assert agent.post("/api/agent/local-usage", json={"buckets": [{"bucket": good["bucket"]}]}).status_code == 422
    assert agent.post("/api/agent/local-usage", json={}).status_code == 422
    assert five_hour(app)["machine_local_usd"] == 0

    r = agent.post("/api/agent/local-usage", json={"buckets": [good, bucket(10, "bridge", 0.25)]})
    assert r.status_code == 200 and r.json() == {"ok": True, "buckets": 2}
    acc = five_hour(app)
    assert acc["machine_local_usd"] == pytest.approx(0.5) and acc["machine_bridge_usd"] == pytest.approx(0.25)

    agent.post("/api/agent/local-usage", json={"buckets": [{**good, "cost_usd": 0.5}]})  # same totals again: no growth
    assert five_hour(app)["machine_local_usd"] == pytest.approx(0.5)
    agent.post("/api/agent/local-usage", json={"buckets": [{**good, "cost_usd": 0.8}]})  # absolute: replaces
    assert five_hour(app)["machine_local_usd"] == pytest.approx(0.8)

    assert TestClient(app).post("/api/agent/local-usage", json={"buckets": []}).status_code in (401, 403)


def build_calibrated_window(app, agent) -> tuple[float, float]:
    """A current 5h window with 230 minutes of machine use (0.04 local + 0.01 bridge per minute) and readings at several levels."""
    svc = app.state.bridge.service
    resets = (int(time.time()) // 60) * 60 + 3600
    start = resets - SPAN
    rows = [{"bucket": bucket_text(start + i * 60), "source": src, "cost_usd": c, "requests": 1}
            for i in range(230) for src, c in (("local", 0.04), ("bridge", 0.01))]
    assert agent.post("/api/agent/local-usage", json={"buckets": rows}).status_code == 200
    for m in (61, 81, 101, 121, 141, 161, 181):  # whole-percent floor of (0.05 * m) / 100 USD
        svc.store.add_util_sample("five_hour", math.floor(0.05 * m) / 100, resets, "turn", start + m * 60)
    svc._capacity_cache.clear()
    return resets, start


def test_auto_calibration_and_manual_override(app, agent):
    resets, _ = build_calibrated_window(app, agent)
    alice, boss = login(app, "alice"), login(app, "boss")
    util = 0.30
    run_turn(alice, agent, 2.0, rate_limit={"five_hour": {"utilization": util, "resets_at": resets},
                                            "seven_day": {"utilization": 0.1, "resets_at": resets + 4 * 86400}})
    app.state.bridge.service._capacity_cache.clear()

    acc = boss.get("/api/admin/users").json()["account"]["five_hour"]
    assert acc["cap_source"] == "auto" and acc["cap_usd"] is None
    assert acc["estimate_cap_usd"] is not None and 80 <= acc["estimate_cap_usd"] <= 120
    assert acc["effective_cap_usd"] == acc["estimate_cap_usd"]
    assert acc["estimate_groups"] >= 4 and acc["estimate_range_usd"][0] <= acc["estimate_cap_usd"] <= acc["estimate_range_usd"][1]
    cap = acc["effective_cap_usd"]
    assert acc["machine_local_usd"] == pytest.approx(9.2, abs=0.01) and acc["machine_bridge_usd"] == pytest.approx(2.3, abs=0.01)
    assert acc["bridge_cost_usd"] == pytest.approx(2.0)
    assert acc["utilization_pct"] == 30.0
    assert acc["bridge_pct"] == pytest.approx(2.0 / cap * 100, abs=0.06)
    assert acc["local_pct"] == pytest.approx(9.2 / cap * 100, abs=0.06)
    assert acc["external_pct"] == pytest.approx(30 - acc["bridge_pct"] - acc["local_pct"], abs=0.11)

    me = alice.get("/api/me").json()["usage"]["five_hour"]
    assert me["cost_usd"] == pytest.approx(2.0) and me["used_pct"] == pytest.approx(2.0 / cap * 100, abs=0.06)

    # the admin's own number wins over the estimate
    r = boss.put("/api/admin/quota", json={"cap_5h_usd": 100})
    assert r.status_code == 200
    acc = boss.get("/api/admin/users").json()["account"]["five_hour"]
    assert acc["cap_source"] == "manual" and acc["cap_usd"] == 100 and acc["effective_cap_usd"] == 100
    assert acc["estimate_cap_usd"] is not None  # still shown for comparison
    assert acc["bridge_pct"] == pytest.approx(2.0) and acc["local_pct"] == pytest.approx(9.2, abs=0.01)
    assert acc["external_pct"] == pytest.approx(30 - 2.0 - 9.2, abs=0.11)
    assert alice.get("/api/me").json()["usage"]["five_hour"]["used_pct"] == pytest.approx(2.0)

    # a tiny cap: the shares exceed the reported utilization, external never goes negative
    boss.put("/api/admin/quota", json={"cap_5h_usd": 1})
    acc = boss.get("/api/admin/users").json()["account"]["five_hour"]
    assert acc["bridge_pct"] == pytest.approx(200.0) and acc["external_pct"] == 0.0

    # clearing it falls back to the estimate
    boss.put("/api/admin/quota", json={"cap_5h_usd": None})
    acc = boss.get("/api/admin/users").json()["account"]["five_hour"]
    assert acc["cap_source"] == "auto" and acc["effective_cap_usd"] == acc["estimate_cap_usd"]


def test_no_cap_means_no_shares(app, agent):
    alice = login(app, "alice")
    run_turn(alice, agent, 1.0, rate_limit={"five_hour": {"utilization": 0.1, "resets_at": time.time() + 3600}})
    acc = five_hour(app)
    assert acc["cap_source"] is None and acc["effective_cap_usd"] is None
    assert acc["bridge_pct"] is None and acc["local_pct"] is None and acc["external_pct"] is None
    assert alice.get("/api/me").json()["usage"]["five_hour"]["used_pct"] is None


def test_capacity_is_cached_until_new_local_usage(app, agent):
    svc = app.state.bridge.service
    assert svc.capacity("five_hour") is None
    resets, start = build_calibrated_window(app, agent)
    assert svc.capacity("five_hour") is not None
    svc.store._x("DELETE FROM bridge_util_samples")
    assert svc.capacity("five_hour") is not None  # cached for CAPACITY_TTL
    agent.post("/api/agent/local-usage", json={"buckets": [bucket(1)]})  # clears the cache
    assert svc.capacity("five_hour") is None


# ---------------- worker ----------------


class UsageClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.reported: list[list[dict]] = []
        self.fail = 0

    def report_local_usage(self, buckets):
        if self.fail:
            self.fail -= 1
            raise BridgeClientError("down")
        self.reported.append(buckets)


def make_worker(tree, **kw) -> tuple[Worker, UsageClient]:
    client = UsageClient()
    cfg = WorkerConfig(local_usage_scan=True, local_usage_root=tree["root"], cwd=tree["bridge"], **kw)
    return Worker(client, cfg), client


def scan_now(worker: Worker) -> None:
    worker._next_scan = 0.0
    worker.maybe_scan_local_usage(block=True)


def test_worker_reports_buckets(tree):
    b = tree["base"]
    write_lines(tree["root"] / "p" / "s.jsonl", tline("m1", b, out=10, cwd=tree["sub"]), tline("m2", b, out=5, cwd=tree["other"]))
    worker, client = make_worker(tree)
    scan_now(worker)
    assert len(client.reported) == 1
    assert {(r["source"], r["output_tokens"]) for r in client.reported[0]} == {("bridge", 10), ("local", 5)}
    scan_now(worker)  # nothing new: nothing sent
    assert len(client.reported) == 1
    write_lines(tree["root"] / "p" / "s.jsonl", tline("m3", b + 60, out=1, cwd=tree["other"]))
    scan_now(worker)
    assert [r["bucket"] for r in client.reported[1]] == [bucket_text(b + 60)]


def test_worker_resends_after_failed_report(tree):
    b = tree["base"]
    write_lines(tree["root"] / "p" / "s.jsonl", tline("m1", b, out=10), tline("m2", b + 60, out=20))
    worker, client = make_worker(tree)
    client.fail = 1
    scan_now(worker)  # BridgeClientError is logged, not raised
    assert client.reported == []
    scan_now(worker)
    assert len(client.reported) == 1 and sorted(r["output_tokens"] for r in client.reported[0]) == [10, 20]


def test_worker_interval_throttles_scans(tree):
    write_lines(tree["root"] / "p" / "s.jsonl", tline("m1", tree["base"], out=10))
    worker, client = make_worker(tree)
    worker.maybe_scan_local_usage(block=True)
    write_lines(tree["root"] / "p" / "s.jsonl", tline("m2", tree["base"] + 60, out=20))
    worker.maybe_scan_local_usage(block=True)  # within local_usage_interval
    assert len(client.reported) == 1


def test_worker_scan_off_by_default(tree):
    write_lines(tree["root"] / "p" / "s.jsonl", tline("m1", tree["base"], out=10))
    client = UsageClient()
    worker = Worker(client, WorkerConfig(local_usage_root=tree["root"], cwd=tree["bridge"]))
    assert worker.config.local_usage_scan is False
    scan_now(worker)
    assert client.reported == [] and worker._scanner is None


def test_worker_bridge_roots(tmp_path):
    cfg = WorkerConfig(cwd=tmp_path / "w", profiles={
        "": WorkerProfile(cwd=str(tmp_path / "chat" / "{owner}")),
        "code": WorkerProfile(cwd=str(tmp_path / "code" / "{owner}" / "{project}")),
        "plain": WorkerProfile(cwd=str(tmp_path / "plain")),
        "inherit": WorkerProfile(),
    })
    roots = Worker(UsageClient(), cfg).bridge_roots()
    assert roots == [str(tmp_path / "w"), f"{tmp_path}/chat/", f"{tmp_path}/code/", str(tmp_path / "plain")]
    assert Worker(UsageClient(), WorkerConfig()).bridge_roots() == []


def test_users_see_their_own_share_apart_from_everyone_else(app, agent):
    resets, _ = build_calibrated_window(app, agent)
    alice, bob, boss = login(app, "alice"), login(app, "bob"), login(app, "boss")
    week = {"utilization": 0.1, "resets_at": resets + 4 * 86400}
    run_turn(alice, agent, 2.0, rate_limit={"five_hour": {"utilization": 0.30, "resets_at": resets}, "seven_day": week})
    run_turn(bob, agent, 1.0)
    boss.put("/api/admin/quota", json={"cap_5h_usd": 100, "cap_7d_usd": 200})

    me = alice.get("/api/me").json()["usage"]["five_hour"]
    assert me["used_pct"] == pytest.approx(2.0) and me["others_pct"] == pytest.approx(1.0)
    assert me["account_pct"] == 30.0 and me["outside_pct"] == pytest.approx(27.0)  # the admin's own clients etc.

    # each conversation's share of the weekly window, in the thread list
    items = alice.get("/api/threads").json()["items"]
    assert [t["quota_pct"] for t in items] == [pytest.approx(1.0)]  # $2 of a $200 week
    assert bob.get("/api/threads").json()["items"][0]["quota_pct"] == pytest.approx(0.5)


def test_thread_quota_pct_is_none_until_calibrated(app, agent):
    alice = login(app, "alice")
    run_turn(alice, agent, 1.0)
    assert [t["quota_pct"] for t in alice.get("/api/threads").json()["items"]] == [None]
