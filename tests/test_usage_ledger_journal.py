"""W2-2: the append-only usage ledger — journal, bound, and exit flush.

The ledger is the budget guard's only durable state, so every test here is
written to be able to FAIL. Three earlier tests in this program passed while
proving nothing, and one of them was this file's subject (see
`test_atexit_hook_actually_fsyncs`).
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from olympus import config, errors, memory, usage

_REPO = Path(__file__).resolve().parent.parent


def _journal(day: str | None = None) -> Path:
    day = day or time.strftime("%Y-%m-%d")
    return config.MEMORY_DIR / "usage" / f"{day}.jsonl"


def _raise_ebadf(fd):
    """What Windows actually does when `fsync` gets a read-only descriptor:
    FlushFileBuffers refuses without GENERIC_WRITE and the CRT surfaces it as
    EBADF. Injected rather than reproduced, so the failure path is exercised on
    every platform and not only the one that can produce it naturally."""
    raise OSError(errno.EBADF, "Bad file descriptor")


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
#
# A THIRD TRAP, and the one that shipped. The probe used to write its sentinel
# line BEFORE delegating to the real `os.fsync`, so it recorded the ATTEMPT and
# not the OUTCOME. `flush()` opened the journal `O_RDONLY`; on Windows
# `os.fsync` is `FlushFileBuffers`, which needs `GENERIC_WRITE`, so the sync
# RAISED every time — and the sentinel already had its line. The test passed on
# a platform where the hook did nothing. Delegating first makes the line
# reachable only on success, so the sentinel now separates three states instead
# of two: synced (a line), attempted-and-failed (empty), never fired (empty).
# The `atexit.unregister` sibling still covers the third.
#
# The KIND is recorded with each line because `_append_usage` fsyncs the parent
# DIRECTORY on the day's first append (the Q5 create-path barrier), and that is
# a real `os.fsync` on POSIX — indistinguishable from the exit flush unless the
# descriptor is classified. `CAN_FSYNC_DIR` is False on Windows, so this is the
# reverse of the defect above: a POSIX-only event the Windows leg cannot see.

_PROBE = r'''
import os, pathlib, stat, sys, time
sentinel = pathlib.Path(sys.argv[1])
memdir = pathlib.Path(sys.argv[2])
unregister = len(sys.argv) > 3 and sys.argv[3] == "unregister"

_real_fsync = os.fsync
def traced(fd):
    result = _real_fsync(fd)          # delegate FIRST
    try:
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
    except OSError:
        kind = "unknown"
    with open(sentinel, "a", encoding="utf-8") as fh:
        fh.write("fsync %s\n" % kind)  # only reached if it SUCCEEDED
    return result
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
    """Every fsync the probe process COMPLETED, as "fsync <kind>" lines."""
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


def _file_syncs(lines: list[str]) -> list[str]:
    """Only the syncs of the JOURNAL itself.

    The create-path parent-directory sync is a different guarantee (the day's
    first append making its directory entry durable) and fires on POSIX only,
    so counting it here would make this test's verdict platform-dependent in
    both directions: noise on Linux, invisible on Windows.
    """
    return [ln for ln in lines if ln.endswith(" file")]


def test_atexit_hook_actually_fsyncs(tmp_path):
    """A clean exit inside the interval still syncs the record.

    Short-lived processes are the gap group commit creates: a CLI run that
    records a model call and exits inside the interval would lose it with no
    crash at all — a clean exit that drops spend.

    The sentinel line is written AFTER the real `os.fsync` returns, so this
    asserts the sync SUCCEEDED, not that it was attempted. That distinction is
    the whole test: the previous version recorded the attempt, and passed on
    Windows while `flush()` opened the journal read-only and the sync raised
    every single time.
    """
    synced = _file_syncs(_run_probe(tmp_path, unregister=False))
    assert synced, (
        "no COMPLETED fsync of the journal was observed at exit — the atexit "
        "hook did not run, ran without syncing, or attempted a sync that "
        "FAILED. A clean exit inside the interval therefore leaves the record "
        "unflushed.")


def test_atexit_probe_can_fail(tmp_path):
    """THE MUTATION, BUILT IN. The same probe with the hook unregistered must
    observe NO fsync of the journal.

    Without this the test above cannot be distinguished from one that passes
    because something else happened to sync. It is the difference between
    'we saw a flush' and 'the hook caused the flush'.
    """
    lines = _run_probe(tmp_path, unregister=True)
    synced = _file_syncs(lines)
    assert synced == [], (
        f"fsync of the journal was observed with the atexit hook UNREGISTERED "
        f"({lines}) — the sibling test is therefore not evidence that the hook "
        f"works, because something else is syncing.")


def test_flush_opens_the_journal_with_write_access(monkeypatch, tmp_path):
    """`flush()` must open for WRITING, on every platform.

    The subprocess probe above can only catch this ON WINDOWS: POSIX lets
    `fsync` succeed on a read-only descriptor, so a regression to `O_RDONLY`
    would leave the probe green on three of the four test legs and red on the
    one that runs last. This asserts the flags directly, so the defect fails
    everywhere in milliseconds rather than only on windows-py3.12.

    O_APPEND is part of the contract too, not decoration: it is what makes a
    stray write through this descriptor unable to land anywhere but the end.
    """
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    memory.set_user("shared")
    usage.record("claude-opus-5", 100, 50)

    seen: list[int] = []
    real_open = os.open

    def spy(path, flags, *a, **kw):
        if str(path).endswith(".jsonl"):
            seen.append(flags)
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", spy)
    assert usage.flush() is True, "flush() did not report success"
    assert seen, "flush() never opened the journal"
    flags = seen[-1]
    assert flags & os.O_WRONLY, (
        f"flush() opened the journal without write access (flags={flags:#o}) — "
        f"os.fsync is FlushFileBuffers on Windows and needs GENERIC_WRITE, so "
        f"the exit flush is inert there")
    assert flags & os.O_APPEND, (
        f"flush() opened the journal without O_APPEND (flags={flags:#o}) — a "
        f"stray write through this descriptor could land mid-journal")


def test_flush_reports_a_failure_instead_of_swallowing_it(monkeypatch,
                                                           tmp_path):
    """NEVER RAISES, NEVER SILENT.

    `except Exception: return False` is right for atexit safety and it is also
    exactly what hid the O_RDONLY defect for a whole commit — every Windows
    exit flush failed and nothing anywhere said so. A failure must reach
    `errors.capture`; the return value stays False and nothing propagates.
    """
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(usage, "_FLUSH_FAILURE_WARNED", False)
    memory.set_user("shared")
    usage.record("claude-opus-5", 100, 50)

    captured: list[tuple] = []
    monkeypatch.setattr(errors, "capture",
                        lambda where, exc, **kw: captured.append((where, exc)))
    monkeypatch.setattr(os, "fsync", _raise_ebadf)

    assert usage.flush() is False, "a failed flush must still report False"
    assert captured, (
        "flush() swallowed a failed sync silently — that is the defect that "
        "let an inert exit flush ship as a proven one")
    assert captured[0][0] == "usage.flush"


def test_flush_reports_a_failure_only_once(monkeypatch, tmp_path):
    """`flush()` runs from the SIGTERM handler as well as from atexit, and a
    failing disk fails for every caller. One report, not a flood at shutdown."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(usage, "_FLUSH_FAILURE_WARNED", False)
    memory.set_user("shared")
    usage.record("claude-opus-5", 100, 50)

    captured: list[tuple] = []
    monkeypatch.setattr(errors, "capture",
                        lambda where, exc, **kw: captured.append((where, exc)))
    monkeypatch.setattr(os, "fsync", _raise_ebadf)

    for _ in range(5):
        usage.flush()
    assert len(captured) == 1, (
        f"5 failing flushes produced {len(captured)} reports — an exit-path "
        f"report must be once per process, not per call")


def test_missing_journal_is_not_reported_as_a_failure(monkeypatch, tmp_path):
    """A peer compaction unlinks the journal. That is a normal outcome of the
    design, not an error, and reporting it would train the operator to ignore
    the key that carries the real one."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(usage, "_FLUSH_FAILURE_WARNED", False)
    memory.set_user("shared")

    captured: list[tuple] = []
    monkeypatch.setattr(errors, "capture",
                        lambda where, exc, **kw: captured.append((where, exc)))
    assert usage.flush() is False
    assert captured == [], (
        f"a missing journal was reported as a flush failure: {captured}")


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
