"""W2-2: the append-only usage ledger — journal, bound, and exit flush.

The ledger is the budget guard's only durable state, so every test here is
written to be able to FAIL. Three earlier tests in this program passed while
proving nothing, and one of them was this file's subject (see
`test_atexit_hook_actually_fsyncs`).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from olympus import config, memory, usage

_REPO = Path(__file__).resolve().parent.parent


def _journal(day: str | None = None) -> Path:
    day = day or time.strftime("%Y-%m-%d")
    return config.MEMORY_DIR / "usage" / f"{day}.jsonl"


# --- the exit flush (Item 3) ----------------------------------------------
#
# THE TRAP THIS AVOIDS. The first attempt asserted that the record was in the
# FILE after a clean exit. That is true whether or not fsync ever runs, because
# write() + close() reaches the OS page cache — a clean exit never loses data,
# only power loss does. It would have passed with flush() deleted.
#
# A second trap: `atexit.register(flush)` captured the FUNCTION OBJECT at
# import time, so monkeypatching `usage.flush` does not change what atexit
# calls. Instrumenting `os.fsync` sidesteps both — it observes the syscall
# itself, from underneath.

_PROBE = r'''
import os, pathlib, sys, time
sentinel = pathlib.Path(sys.argv[1])
memdir = pathlib.Path(sys.argv[2])
unregister = len(sys.argv) > 3 and sys.argv[3] == "unregister"

_real_fsync = os.fsync
def traced(fd):
    with open(sentinel, "a", encoding="utf-8") as fh:
        fh.write("fsync\n")
    return _real_fsync(fd)
os.fsync = traced

from olympus import config, memory, usage
config.MEMORY_DIR = memdir
memory.set_user("shared")

if unregister:
    import atexit
    atexit.unregister(usage.flush)

# The interval must NOT be able to fire, so any fsync the sentinel records is
# the EXIT-TIME one and nothing else.
usage._LAST_FSYNC[0] = time.time()
usage.record("claude-opus-5", 100, 50)
'''


def _run_probe(tmp_path: Path, *, unregister: bool) -> list[str]:
    sentinel = tmp_path / ("unreg.txt" if unregister else "reg.txt")
    memdir = tmp_path / ("mem-unreg" if unregister else "mem-reg")
    memdir.mkdir()
    args = [sys.executable, "-c", _PROBE, str(sentinel), str(memdir)]
    if unregister:
        args.append("unregister")
    subprocess.run(args, cwd=str(_REPO), check=True, capture_output=True,
                   timeout=120)
    if not sentinel.exists():
        return []
    return [ln for ln in sentinel.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def test_atexit_hook_actually_fsyncs(tmp_path):
    """A clean exit inside the interval still syncs the record.

    Short-lived processes are the gap group commit creates: a CLI run that
    records a model call and exits inside the interval would lose it with no
    crash at all — a clean exit that drops spend.
    """
    synced = _run_probe(tmp_path, unregister=False)
    assert synced, (
        "no fsync was observed at exit — the atexit hook did not run, or ran "
        "without syncing. A clean exit inside the interval therefore leaves "
        "the record unflushed.")


def test_atexit_probe_can_fail(tmp_path):
    """THE MUTATION, BUILT IN. The same probe with the hook unregistered must
    observe NO fsync.

    Without this the test above cannot be distinguished from one that passes
    because something else happened to sync. It is the difference between
    'we saw a flush' and 'the hook caused the flush'.
    """
    synced = _run_probe(tmp_path, unregister=True)
    assert synced == [], (
        f"fsync was observed with the atexit hook UNREGISTERED ({synced}) — "
        f"the sibling test is therefore not evidence that the hook works, "
        f"because something else is syncing.")


# --- the bound (Item 4.3) --------------------------------------------------

def test_journal_is_bounded_without_any_heartbeat_tick(monkeypatch, tmp_path):
    """Size-threshold auto-compaction, with no maintenance job involved.

    W2-2 put an O(journal) read on the per-model-call budget guard
    (`today_spend`). The daily compaction alone does not bound it — a busy day
    would grow the journal all day. This is the trigger that does.
    """
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    memory.set_user("shared")
    monkeypatch.setenv("OLYMPUS_USAGE_COMPACT_BYTES", "8192")

    for _ in range(400):
        usage.record("claude-opus-4-8", 1200, 300)

    size = _journal().stat().st_size if _journal().exists() else 0
    assert size < 8192, (
        f"journal is {size} B against an 8192 B threshold — auto-compaction "
        f"never fired, so today_spend() grows unbounded with the day")

    ceiling_ms = 2.5
    start = time.perf_counter()
    for _ in range(20):
        usage.today_spend()
    per_call_ms = (time.perf_counter() - start) / 20 * 1000
    assert per_call_ms < ceiling_ms, (
        f"today_spend() cost {per_call_ms:.3f} ms, over the {ceiling_ms} ms "
        f"ceiling the threshold was derived against — it is on the "
        f"per-model-call budget-guard path")


def test_total_is_unchanged_across_auto_compaction(monkeypatch, tmp_path):
    """Compaction moves where the numbers live, never what they are — the same
    property the manual `compact()` test asserts, on the automatic path."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    memory.set_user("shared")
    monkeypatch.setenv("OLYMPUS_USAGE_COMPACT_BYTES", "4096")

    total = 0.0
    for _ in range(200):
        usage.record("claude-opus-4-8", 1200, 300)
        total = usage.today_spend()
    assert (tmp_path / "usage" / f"{time.strftime('%Y-%m-%d')}.json").exists(), \
        "no snapshot was ever published, so nothing was auto-compacted"
    assert usage.today_spend() == pytest.approx(total), (
        "the running total changed across an auto-compaction")


# --- aggregation and the torn tail (Items 4.1 / 4.2) ----------------------

def test_todays_spend_includes_uncompacted_appends(monkeypatch, tmp_path):
    """The budget guard must see spend that has not been compacted yet.

    Reading only the snapshot UNDER-REPORTS, and under-reporting disables the
    cap — the direction that costs money.
    """
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    memory.set_user("shared")
    monkeypatch.setenv("OLYMPUS_USAGE_COMPACT_BYTES", "10000000")  # never

    for _ in range(5):
        usage.record("claude-opus-5", 100, 50)
    snapshot = tmp_path / "usage" / f"{time.strftime('%Y-%m-%d')}.json"
    assert not snapshot.exists(), "precondition: nothing compacted yet"
    assert usage.today_spend() > 0.0, (
        "today_spend() reported 0 while 5 records sat in the journal — the "
        "budget guard is blind to un-compacted spend")


def test_torn_tail_is_discarded_and_prior_records_survive(monkeypatch,
                                                          tmp_path):
    """A crash can only damage the FINAL record. Everything before it stands.

    This is what the append buys over the old whole-file rewrite, where an
    interrupted publish could return an empty file and reset the day to zero.
    """
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    memory.set_user("shared")
    monkeypatch.setenv("OLYMPUS_USAGE_COMPACT_BYTES", "10000000")

    for _ in range(4):
        usage.record("claude-opus-5", 100, 50)
    intact = usage.today_spend()
    per_record = intact / 4

    raw = _journal().read_bytes()
    _journal().write_bytes(raw[:-15])          # truncate mid-record
    after = usage.today_spend()

    assert after == pytest.approx(intact - per_record, rel=1e-6), (
        f"a torn final record cost more than itself: {intact} -> {after}. "
        f"Prior records must survive; only the tail may be lost.")
    assert after > 0.0, (
        "a torn tail zeroed the day's spend — that is the whole-file-rewrite "
        "failure mode the append was meant to remove")


def test_mid_file_corruption_is_not_forgiven(monkeypatch, tmp_path):
    """Only the LAST line is forgiven. Corruption anywhere else is real damage
    and must not be silently skipped."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    memory.set_user("shared")
    monkeypatch.setenv("OLYMPUS_USAGE_COMPACT_BYTES", "10000000")

    for _ in range(3):
        usage.record("claude-opus-5", 100, 50)
    lines = _journal().read_bytes().split(b"\n")
    lines[0] = b'{"broken'                     # damage a NON-final record
    _journal().write_bytes(b"\n".join(lines))
    with pytest.raises(ValueError):
        usage._replay_journal(config.MEMORY_DIR / "usage" /
                              f"{time.strftime('%Y-%m-%d')}.json")


def test_per_user_and_per_model_rows_survive_the_journal(monkeypatch,
                                                         tmp_path):
    """Aggregation shape is unchanged: the journal stores raw events and
    `ledger()` rebuilds the same rows the snapshot always had."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_USAGE_COMPACT_BYTES", "10000000")

    memory.set_user("alice")
    usage.record("claude-opus-5", 100, 50)
    memory.set_user("bob")
    usage.record("claude-haiku-4-5", 200, 20)

    led = usage.ledger()
    assert led["__all__"]["calls"] == 2
    assert led["user:alice"]["calls"] == 1
    assert led["user:bob"]["calls"] == 1
    assert led["model:claude-opus-5"]["calls"] == 1
    assert led["model:claude-haiku-4-5"]["in"] == 200


def test_migration_seam_old_snapshot_plus_new_appends(monkeypatch, tmp_path):
    """An instance upgraded MID-DAY keeps its morning and adds its afternoon.

    The pre-W2-2 day file is already in snapshot shape, so it is the base and
    the journal layers on top — no special case. Getting this wrong
    UNDER-reports, which is the direction that costs money.
    """
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    memory.set_user("shared")
    monkeypatch.setenv("OLYMPUS_USAGE_COMPACT_BYTES", "10000000")

    day = time.strftime("%Y-%m-%d")
    snap = tmp_path / "usage" / f"{day}.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps(
        {"__all__": {"calls": 2, "in": 10, "out": 5, "cost": 0.5}}),
        encoding="utf-8")

    usage.record("claude-opus-5", 100, 50)
    led = usage.ledger()
    assert led["__all__"]["cost"] > 0.5, "the morning's spend was lost"
    assert led["__all__"]["calls"] == 3, "the old row was not carried forward"
