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
# DIRECTORY on an append that CREATES the journal (the Q5 create-path barrier —
# which fires after every compaction, not once a day, since `compact()` unlinks
# the journal), and that is a real `os.fsync` on POSIX, indistinguishable from
# the exit flush unless the descriptor is classified. `CAN_FSYNC_DIR` is False
# on Windows, so this is the reverse of the defect above: a POSIX-only event the
# Windows leg cannot see.

# A FOURTH TRAP, and the one that made CI red on a green tree. Classifying by
# DESCRIPTOR KIND ("file" vs "dir") is not attribution. `record()` itself syncs
# the journal when the group-commit interval has elapsed — same file, same
# `os.fsync`, indistinguishable from the exit flush under a kind-only filter.
# The old probe tried to prevent that by stamping `_LAST_FSYNC[0] = time.time()`
# immediately before `record()`, reasoning that the interval "must NOT be able
# to fire". It can: the stamp is taken BEFORE an unbounded amount of work.
# `record()` then lazily imports `proclock`, waits on the cross-process
# `usage-ledger` flock, creates directories and estimates cost — and only then
# reaches `_should_sync_now()` inside `_append_usage`. Measured on the local
# Windows machine that wrote this, `record()` takes 0.57-0.95 s against a
# 1000 ms default interval; on a loaded CI runner it crosses 1 s routinely.
# When it does, the interval fires INSIDE `record()`, the journal is synced with
# the hook unregistered, and the negative control fails on production code that
# is behaving exactly as designed.
#
# So the probe attributes every sync two ways instead of classifying it:
#   * DESCRIPTOR IDENTITY — `st_dev:st_ino` from `os.fstat`, compared against
#     the journal's own stat. Populated on Windows as well as POSIX, so this
#     works on every leg. It answers WHICH FILE.
#   * CODE-OBJECT IDENTITY — whether `usage.flush.__code__` is the code object
#     of a live frame. It answers WHICH CALLABLE, exactly: a name+basename
#     heuristic would credit any other `flush` in any other `usage.py`.
# A journal sync from the interval group commit carries `in_flush: false`; the
# exit hook's carries `in_flush: true`.
#
# AND the interval is genuinely suppressed for the two non-forced controls, so
# each of the three proves a DISTINCT state rather than leaning on attribution
# to paper over a sync that should not have happened at all. Suppression needs
# the `_LAST_FSYNC` stamp as well as the long interval — see `_run_probe`.

_PROBE = r'''
import json, os, pathlib, stat, sys, time
sentinel = pathlib.Path(sys.argv[1])
memdir = pathlib.Path(sys.argv[2])
unregister = len(sys.argv) > 3 and sys.argv[3] == "unregister"
force_interval = len(sys.argv) > 4 and sys.argv[4] == "force-interval"

def _emit(payload):
    with open(sentinel, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")

# The exit hook's CODE OBJECT, bound after the import below. Initialized to None
# first so a sync during import is still recorded safely (as not-the-hook)
# rather than raising NameError inside a patched syscall.
_FLUSH_CODE = None

_real_fsync = os.fsync
def traced(fd):
    result = _real_fsync(fd)          # delegate FIRST — only success is recorded
    try:
        st = os.fstat(fd)
        kind = "dir" if stat.S_ISDIR(st.st_mode) else "file"
        ident = "%s:%s" % (st.st_dev, st.st_ino)
    except OSError:
        kind, ident = "unknown", ""
    # WHICH CALLABLE, BY CODE-OBJECT IDENTITY. Matching a frame's function NAME
    # and FILE BASENAME would credit any other `flush` defined in any other
    # `usage.py` on the stack; `f_code is usage.flush.__code__` is the exact
    # function and nothing else. Walk real frames — `traceback.extract_stack`
    # yields FrameSummary objects, which carry names, not code objects.
    in_flush = False
    frame = sys._getframe(1)
    while frame is not None:
        if _FLUSH_CODE is not None and frame.f_code is _FLUSH_CODE:
            in_flush = True
            break
        frame = frame.f_back
    _emit({"event": "fsync", "kind": kind, "ident": ident, "in_flush": in_flush})
    return result
os.fsync = traced

from olympus import config, memory, usage
config.MEMORY_DIR = memdir
memory.set_user("shared")
_FLUSH_CODE = usage.flush.__code__

if unregister:
    import atexit
    atexit.unregister(usage.flush)

# SUPPRESS THE INTERVAL FOR REAL. `_LAST_FSYNC` starts at 0.0, so
# `time.time() - 0.0` is ~1.79e9 seconds and clears ANY interval — an hour
# included. Setting a long interval alone therefore suppresses nothing: the
# first `record()` still group-commits. Stamping the clock here, with the
# subprocess timeout far below the configured hour, is what actually makes the
# interval unreachable. When `force_interval` is set the interval is zero
# (per-call) and the clock is deliberately left alone so `record()` DOES sync.
if not force_interval:
    usage._LAST_FSYNC[0] = time.time()

usage.record("claude-opus-5", 100, 50)

# Publish the journal's identity so the parent can attribute syncs to it by
# inode rather than by guessing from descriptor kind.
day = time.strftime("%Y-%m-%d")
journal = usage._journal_path(memdir / "usage" / ("%s.json" % day))
if journal.exists():
    st = journal.stat()
    _emit({"event": "journal", "ident": "%s:%s" % (st.st_dev, st.st_ino)})
'''


def _run_probe(tmp_path: Path, *, unregister: bool,
               force_interval: bool = False) -> list[dict]:
    """Every fsync the probe process COMPLETED, as attribution records.

    The group-commit interval is suppressed by BOTH halves, because either
    alone is insufficient: the environment sets an hour, and the child stamps
    `_LAST_FSYNC` just before `record()`. A long interval on its own does
    nothing — `_LAST_FSYNC` starts at 0.0, so the elapsed time is ~1.79e9
    seconds and clears an hour trivially. `force_interval=True` sets zero
    (per-call) and skips the stamp, so `record()` deterministically syncs.

    The two flags occupy fixed argv slots (`-` when off) so `force_interval`
    is meaningful on its own rather than only alongside `unregister`.
    """
    tag = ("unreg" if unregister else "reg") + ("-forced" if force_interval else "")
    sentinel = tmp_path / f"{tag}.jsonl"
    memdir = tmp_path / f"mem-{tag}"
    memdir.mkdir()
    args = [sys.executable, "-c", _PROBE, str(sentinel), str(memdir),
            "unregister" if unregister else "-",
            "force-interval" if force_interval else "-"]
    env = dict(os.environ)
    env["OLYMPUS_USAGE_FSYNC"] = "interval"          # pin the mode too
    env["OLYMPUS_USAGE_FSYNC_INTERVAL_MS"] = "0" if force_interval else "3600000"
    subprocess.run(args, cwd=str(_REPO), check=True, capture_output=True,
                   timeout=120, env=env)
    if not sentinel.exists():
        return []
    return [json.loads(ln) for ln in
            sentinel.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _journal_ident(records: list[dict]) -> str:
    for rec in records:
        if rec.get("event") == "journal":
            return rec["ident"]
    return ""


def _journal_syncs(records: list[dict]) -> list[dict]:
    """Completed syncs OF THE JOURNAL FILE, whoever made them.

    Identified by descriptor identity, not by kind. The create-path
    parent-DIRECTORY sync is a different guarantee and fires on POSIX only, so
    counting it would make the verdict platform-dependent in both directions:
    noise on Linux, invisible on Windows. Matching the journal's own inode
    excludes it without needing a platform branch.
    """
    ident = _journal_ident(records)
    if not ident:
        return []
    return [r for r in records
            if r.get("event") == "fsync" and r.get("ident") == ident]


def _hook_journal_syncs(records: list[dict]) -> list[dict]:
    """Journal syncs performed BY THE ATEXIT HOOK specifically."""
    return [r for r in _journal_syncs(records) if r.get("in_flush")]


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
    records = _run_probe(tmp_path, unregister=False)
    assert _journal_ident(records), f"probe never created the journal: {records}"
    synced = _hook_journal_syncs(records)
    assert synced, (
        f"no COMPLETED fsync of the journal BY `usage.flush` was observed at "
        f"exit ({records}) — the atexit hook did not run, ran without syncing, "
        f"or attempted a sync that FAILED. A clean exit inside the interval "
        f"therefore leaves the record unflushed.")


def test_atexit_probe_can_fail(tmp_path):
    """THE MUTATION, BUILT IN. The same probe with the hook unregistered must
    observe no journal sync ATTRIBUTED TO THE HOOK.

    Without this the sibling above cannot be distinguished from one that passes
    because something else happened to sync. It is the difference between 'we
    saw a flush' and 'the hook caused the flush'.

    The interval is genuinely suppressed here — a long interval AND the
    `_LAST_FSYNC` stamp — so with the hook gone the journal must not be synced
    AT ALL. That is a strictly stronger statement than "no sync was attributed
    to the hook", and it is checkable only because the suppression is real:
    an earlier revision set the hour but not the stamp, so `record()` still
    group-committed and this control could only ever assert attribution.

    Attribution still matters, and
    `test_interval_sync_is_not_credited_to_the_atexit_hook` is where it is
    proved — there the sync is deliberately allowed to happen.
    """
    records = _run_probe(tmp_path, unregister=True)
    assert _journal_ident(records), f"probe never created the journal: {records}"
    assert _journal_syncs(records) == [], (
        f"the journal was synced with the atexit hook UNREGISTERED and the "
        f"group-commit interval suppressed ({records}) — either the suppression "
        f"is not working or something else syncs the journal, and in both cases "
        f"the sibling test is not evidence that the hook did it.")
    assert _hook_journal_syncs(records) == []


def test_interval_sync_is_not_credited_to_the_atexit_hook(tmp_path):
    """THE DISCRIMINATION PROOF, and a regression test for a real CI failure.

    `record()` syncs the journal itself when the group-commit interval has
    elapsed. That is correct production behaviour, and on a loaded runner it
    happens INSIDE `record()` — `record()` takes 0.57-0.95 s locally against a
    1000 ms default, and the old control stamped the interval clock before all
    of that work. A kind-only filter counted that sync as evidence the exit hook
    had run and failed the negative control on a healthy tree.

    Here the interval is forced to fire with the hook UNREGISTERED. The journal
    IS synced — so this is not the "nothing happened" case — and the attribution
    must still credit it to `record`, never to `flush`.
    """
    records = _run_probe(tmp_path, unregister=True, force_interval=True)
    assert _journal_ident(records), f"probe never created the journal: {records}"

    journal_syncs = _journal_syncs(records)
    assert journal_syncs, (
        f"the forced interval did not sync the journal, so this test is not "
        f"exercising the interleaving it exists for: {records}")
    assert all(not r["in_flush"] for r in journal_syncs), (
        f"a sync made by `record`/`_append_usage` was attributed to the atexit "
        f"hook ({journal_syncs}) — the attribution cannot tell the two apart, "
        f"which is exactly the defect that made CI red.")
    assert _hook_journal_syncs(records) == []


def test_flush_opens_the_journal_with_write_access(monkeypatch, tmp_path):
    """`flush()` must open for WRITING, on every platform.

    The subprocess probe above can only catch this ON WINDOWS: POSIX lets
    `fsync` succeed on a read-only descriptor, so a regression to `O_RDONLY`
    would leave the probe green on three of the four test legs and red on the
    one that runs last. This asserts the flags directly, so the defect fails
    everywhere in milliseconds rather than only on windows-py3.12.

    THE ACCESS MODE, NOT `O_WRONLY` SPECIFICALLY. This asserted
    `flags & os.O_WRONLY`, which is wrong in the same family as everything else
    this change corrected — a test pinning a MECHANISM where the GUARANTEE is
    what matters. `os.O_RDWR` is 2 and `os.O_RDWR & os.O_WRONLY == 0`, so a
    maintainer switching to `O_RDWR` — which grants write access and makes
    `fsync` work perfectly well — would have got a red test for a correct fix.
    The property is "not read-only".

    The low two bits are the access mode on both POSIX (`O_ACCMODE` == 3) and
    the Windows CRT (`_O_RDONLY`/`_O_WRONLY`/`_O_RDWR` == 0/1/2); `os.O_ACCMODE`
    itself is POSIX-only, so the mask is written out.

    O_APPEND is asserted SEPARATELY and strictly, because unlike the access mode
    it is a deliberate choice rather than the syscall's minimum requirement: it
    is what makes a stray write through this descriptor unable to land anywhere
    but the end.
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
    assert (flags & 3) in (os.O_WRONLY, os.O_RDWR), (
        f"flush() opened the journal read-only (flags={flags:#o}, access mode "
        f"{flags & 3}) — os.fsync is FlushFileBuffers on Windows and needs "
        f"GENERIC_WRITE, so the exit flush is inert there. O_WRONLY and O_RDWR "
        f"both satisfy this; only O_RDONLY does not.")
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
