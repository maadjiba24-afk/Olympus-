"""Phase 4 Stage D — performance & cost validation (CI regression guard).

These tests re-run the measurements in ``scripts/perf_validation.py`` at reduced
sample sizes and assert **deliberately loose** bounds. The bounds are sized to
catch a ~10x regression (someone puts an fsync in a loop, makes the classifier
ladder quadratic, turns a pure function into an I/O call), NOT to police
machine-to-machine variance. Every bound below is annotated with the value
actually measured on the reference run, so the headroom is visible.

They also assert the HONESTY properties of the report itself:

* every provider-dependent metric is in the UNMEASURABLE register with a reason
  and a way to measure it — it is never given a number;
* every service objective that needs real traffic is labelled a PROPOSAL.

Nothing here needs a provider, a key, or a network.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import perf_validation as pv  # noqa: E402

from olympus import config  # noqa: E402


def test_perf_validation_imports_with_platform_memory_backend():
    """Import collection must select a real backend on every supported OS."""
    assert pv.memory_backend() in {
        "windows-psapi",
        "unix-getrusage+/proc",
        "unix-getrusage+ps",
    }
    if sys.platform == "win32":
        assert pv.memory_backend() == "windows-psapi"


def test_rss_measurements_are_non_negative_integers():
    for sample in (pv.rss_kb(), pv.current_rss_kb()):
        assert isinstance(sample, int)
        assert sample >= 0


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires the Windows PSAPI process-memory backend",
)
def test_windows_memory_backend_is_a_live_process_measurement():
    """A fresh child process must report live PSAPI working-set growth."""
    probe = textwrap.dedent(
        """
        import json
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path.cwd() / "scripts"))
        import perf_validation as pv

        if pv.memory_backend() != "windows-psapi":
            raise AssertionError(f"unexpected backend: {pv.memory_backend()}")

        peak_before, current_before = pv._windows_process_memory_kb()
        blocks = []
        peak_after, current_after = peak_before, current_before

        for _ in range(8):
            block = bytearray(8 * 1024 * 1024)
            for offset in range(0, len(block), 4096):
                block[offset] = 1
            blocks.append(block)
            peak_after, current_after = pv._windows_process_memory_kb()
            if peak_after > peak_before and current_after > current_before:
                break

        print(json.dumps({
            "backend": pv.memory_backend(),
            "peak_before_kb": peak_before,
            "current_before_kb": current_before,
            "peak_after_kb": peak_after,
            "current_after_kb": current_after,
            "allocated_mib": len(blocks) * 8,
            "touch_checksum": sum(block[0] for block in blocks),
        }))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        "isolated Windows PSAPI probe failed:\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )

    sample = json.loads(completed.stdout)
    assert sample["backend"] == "windows-psapi"
    assert sample["peak_before_kb"] > 0
    assert sample["current_before_kb"] > 0
    assert sample["peak_before_kb"] >= sample["current_before_kb"]
    assert sample["peak_after_kb"] >= sample["current_after_kb"]
    assert sample["peak_after_kb"] > sample["peak_before_kb"], sample
    assert sample["current_after_kb"] > sample["current_before_kb"], sample
    assert 8 <= sample["allocated_mib"] <= 64
    assert sample["touch_checksum"] == sample["allocated_mib"] // 8


# --- 1. sealed session journal ---------------------------------------------

# --- 1a. session-journal append: DETERMINISTIC contracts --------------------
#
# WHAT MOVED, AND WHY. Two tests here used to assert absolute wall-clock bounds
# on `sessionlog.append_turn` (p50 < 60 / p99 < 200 for fsync=auto; p50 < 100 /
# p99 < 400 for fsync=always) inside the REQUIRED suite, which runs on
# uncontrolled GitHub-hosted runners. On 2026-08-08 that gate blocked a merge
# with p50 88.11 ms, on a commit whose failing test function and benchmark are
# byte-identical to a commit whose push job passed. The series also decayed
# (first decile 164.01 ms, last decile 33.90 ms) rather than rising with
# journal depth. The failure is consistent with uncontrolled runner variance
# and was not attributable to the Step 1D diff, but the precise external cause
# was not proven.
#
# The thresholds were not raised, scaled or deleted. They live unchanged in
# scripts/sessionlog_latency_telemetry.py, on a scheduled/manual workflow that
# goes red honestly without holding an unrelated PR shut. Two facts decided
# that placement: `append_turn` has NO production caller (the live per-turn
# path is `sessionlog.sync` — see the test below, which is untouched), and this
# repository has no qualified performance runner.
#
# What stays REQUIRED here is what a shared runner can actually decide:
# integrity and work accounting, neither of which depends on host speed.

def test_append_persists_every_record_with_a_verified_chain():
    """Layer A — integrity. Host-independent; no wall-clock anywhere.

    Real appends against a real journal: nothing about the work under test is
    mocked. A benchmark that returns quickly because it wrote less is caught
    here, which is what allows the timing contract to live elsewhere.
    """
    from olympus import sessionlog

    n = 40
    r = pv.bench_sessionlog_append(n=n, fsync="auto")
    assert r["n"] == n

    # The benchmark's own accounting agrees with the journal on disk.
    assert r["records_verified"] == n, r
    assert r["journal_status"] == "ok", r
    assert r["journal_bytes"] > 0, r

    # ...and re-reading independently confirms it, chain and all.
    records, status = sessionlog.read_verified(r["conversation_id"])
    assert status == "ok"
    assert len(records) == n
    assert [rec["seq"] for rec in records] == list(range(1, n + 1)), (
        "sequence numbers must be exactly 1..n, strictly monotonic, no gaps")

    prev = ""
    for rec in records:
        assert rec["prev"] == prev, f"chain break at seq {rec['seq']}"
        assert rec["sha"] == sessionlog._seal(rec), (
            f"record {rec['seq']} does not verify against its own seal")
        assert rec["kind"] == "turn"
        prev = rec["sha"]


def test_append_failure_cannot_be_reported_as_a_fast_success(monkeypatch):
    """Layer A — a failed append must void the measurement, never speed it up.

    The cheapest way to look fast is to stop doing the work. `_open_append` is
    the module's documented fault-injection seam; failing it makes
    `append_turn` return 0, and the benchmark must raise rather than summarise
    whatever samples it collected before the fault.
    """
    from olympus import sessionlog

    calls = {"n": 0}
    real_open = sessionlog._open_append

    def failing_open(path):
        calls["n"] += 1
        if calls["n"] > 5:
            raise OSError("simulated journal write fault")
        return real_open(path)

    monkeypatch.setattr(sessionlog, "_open_append", failing_open)

    with pytest.raises(RuntimeError, match="measurement void"):
        pv.bench_sessionlog_append(n=20, fsync="auto")
    assert calls["n"] > 5, "the fault never fired, so this asserted nothing"


def test_append_corruption_is_detected_not_silently_accepted(tmp_path,
                                                             monkeypatch):
    """Layer A — a mutated journal must not verify.

    Guards the integrity check above against being vacuous: if `read_verified`
    accepted anything, the record/chain assertions would prove nothing.
    """
    from olympus import config, memory, sessionlog

    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    memory.set_user("perf")
    cid = "corrupt-probe"
    with pv.env(OLYMPUS_SESSION_JOURNAL="on", OLYMPUS_SESSION_FSYNC="auto"):
        for _ in range(5):
            assert sessionlog.append_turn(cid, [{"role": "user",
                                                 "content": "x"}]) > 0
        records, status = sessionlog.read_verified(cid)
        assert status == "ok" and len(records) == 5

        # Flip a byte inside the FIRST record's payload — mid-file, so it
        # cannot be mistaken for a torn tail.
        path = sessionlog._journal_path(sessionlog._sid(cid))
        raw = path.read_bytes()
        first_nl = raw.index(b"\n")
        mutated = raw[:first_nl].replace(b'"content":"x"',
                                         b'"content":"y"', 1)
        assert mutated != raw[:first_nl], "the probe failed to mutate anything"
        path.write_bytes(mutated + raw[first_nl:])

        after, after_status = sessionlog.read_verified(cid)
    assert after_status != "ok" or len(after) < 5, (
        "a mid-file mutation was accepted as a verified journal")


def _scan_field(scan, key, where, out) -> int | None:
    """One non-negative integer out of a scan record, or a violation."""
    if not isinstance(scan, dict) or key not in scan:
        out.append(f"{where} has no {key!r}")
        return None
    value = scan[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        out.append(f"{where} has a non-integer or negative {key}: {value!r}")
        return None
    return value


def append_work_violations(*, n, append_scans, verification_scans,
                           size_before_append, writes, journal_bytes,
                           fsyncs, opens) -> list[str]:
    """Judge one work measurement. Pure, so it can be driven synthetically.

    COHERENT BY CONSTRUCTION. Scan COUNT and BYTES READ are derived here from
    `append_scans` itself; they are not accepted as separate arguments. An
    earlier revision took `append_scan_count`, `append_bytes_read` and the scan
    list independently, so a synthetic case could claim "zero scans" while
    handing over twelve scan records — contradictory data that proved nothing
    about a coherent zero-scan implementation. There is now no way to state a
    count that disagrees with the records behind it.

    CEILINGS ONLY on work quantities: every scan/byte-read rule is an upper
    bound, so an implementation that reads LESS — a future cache, a metadata
    shortcut, an incremental tail read — passes unchanged. Nothing here forces
    a rescan, and the O(n^2) shape is deliberately NOT pinned.

    EXACT only where correctness demands it: durability (one fsync per append),
    one sealed record written per append, and journal bytes equal to the bytes
    actually presented to the write seam.

    Structural inputs FAIL CLOSED: a malformed scan record, a negative size, or
    a `size_before_append` of the wrong length is a violation, never a silently
    skipped check.
    """
    out: list[str] = []

    # --- structural validation: unusable input is a failure ----------------
    if not isinstance(size_before_append, list) or \
            len(size_before_append) != n:
        out.append(
            f"size_before_append has {len(size_before_append)} entries, "
            f"expected {n} — one journal size per append")
    for i, size in enumerate(size_before_append):
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            out.append(f"size_before_append[{i}] is not a non-negative int: "
                       f"{size!r}")

    append_read: list[int] = []
    for i, scan in enumerate(append_scans):
        size = _scan_field(scan, "journal_size_at_scan_start",
                           f"append scan {i}", out)
        read = _scan_field(scan, "actual_bytes_read", f"append scan {i}", out)
        if size is None or read is None:
            continue
        append_read.append(read)
        if read > size:
            out.append(
                f"append scan {i} read {read} bytes from a journal holding "
                f"{size} — a scan cannot read more than the file contains")
    for i, scan in enumerate(verification_scans):
        size = _scan_field(scan, "journal_size_at_scan_start",
                           f"verification scan {i}", out)
        read = _scan_field(scan, "actual_bytes_read",
                           f"verification scan {i}", out)
        if size is not None and read is not None and read > size:
            out.append(
                f"verification scan {i} read {read} bytes from a journal "
                f"holding {size} — a scan cannot read more than the file "
                f"contains")

    # --- durability and write shape: exact ---------------------------------
    if fsyncs != n:
        out.append(f"fsyncs {fsyncs} != {n}; fsync='auto' still fsyncs once "
                   f"per append, and one append must leave one durable record")
    if len(writes) != n:
        out.append(f"{len(writes)} writes for {n} appends; the contract is one "
                   f"sealed record per append")
    if journal_bytes != sum(writes):
        out.append(f"journal is {journal_bytes} bytes but {sum(writes)} were "
                   f"written to the seam")
    if writes and min(writes) <= 0:
        out.append("a zero-length record was written")

    # --- work ceilings, DERIVED from the scan records: never a floor -------
    append_scan_count = len(append_scans)
    append_bytes_read = sum(append_read)
    if append_scan_count > n:
        out.append(f"{append_scan_count} append-phase scans for {n} appends; "
                   f"more than one per append is a second pass")
    budget = sum(s for s in size_before_append
                 if isinstance(s, int) and not isinstance(s, bool) and s >= 0)
    if append_bytes_read > budget:
        out.append(f"append phase read {append_bytes_read} bytes from the "
                   f"journal, above the {budget}-byte full-rescan ceiling")
    if not 0 < opens <= n:
        out.append(f"{opens} journal opens for {n} appends; more than one per "
                   f"append is duplicated work")

    # --- the benchmark's own integrity read must have happened -------------
    if len(verification_scans) < 1:
        out.append("the benchmark performed no final verification scan, so "
                   "its record count and status are unsubstantiated")
    return out


def test_append_does_a_bounded_and_exactly_durable_amount_of_work(monkeypatch):
    """Layer B — work accounting in operations and ACTUAL bytes, not time.

    This replaces the wall-clock gate as the REQUIRED signal. It catches an
    extra scan, a duplicated open, a lost fsync, a second pass over the
    journal, or a changed write shape — deterministically, on any host at any
    speed — while permitting an implementation that does strictly less work.

    ACTUAL BYTES READ, NOT FILE SIZE. `_scan` reads the journal with
    `pathlib.Path.read_bytes()` (olympus/sessionlog.py:158). An earlier
    revision recorded `stat().st_size` at scan entry and called that "bytes
    read", which it is not: a file's size is not evidence that anything read
    it. The read seam itself is now wrapped, delegating to the real
    `Path.read_bytes` and counting `len()` of what it actually returns.
    Accounting is restricted to the exact journal under test — the `_scan`
    wrapper registers that one path, and reads of any other path are ignored,
    so unrelated filesystem activity cannot leak into the totals. `stat()` is
    still recorded, but only as `journal_size_at_scan_start`, and only as a
    CEILING; the two are never conflated.

    Byte expectations come from the ACTUAL canonically encoded records handed
    to the write seam, never from an assumed constant record size: `seq` widens
    from 1 to 2 digits and `ts` is a float whose repr length varies, so any
    fixed-size equation would be wrong.

    Only narrow seams are used: `_open_append` (documented as a fault-injection
    seam), `_scan`, `read_verified`, `os.fsync`, and the single `read_bytes`
    method — each restored by monkeypatch. No filesystem class is replaced
    wholesale and no production file is modified.
    """
    from pathlib import Path

    from olympus import sessionlog

    n = 12
    writes: list[int] = []          # bytes handed to the write seam, in order
    journal_reads: list[int] = []   # bytes RETURNED by reads of THIS journal
    per_scan: list[dict] = []
    targets: set = set()            # the exact journal path(s) under test
    opens = {"n": 0}
    fsyncs = {"n": 0}
    verification_at = {"index": None}

    real_open = sessionlog._open_append
    real_scan = sessionlog._scan
    real_read_verified = sessionlog.read_verified
    real_fsync = sessionlog.os.fsync
    real_read_bytes = Path.read_bytes

    class _CountingHandle:
        """Wraps the real file object; records exact bytes written."""

        def __init__(self, handle):
            self._handle = handle

        def write(self, data):
            writes.append(len(data))
            return self._handle.write(data)

        def __getattr__(self, name):
            return getattr(self._handle, name)

    def counting_open(path):
        opens["n"] += 1
        return _CountingHandle(real_open(path))

    def counting_read_bytes(self):
        """Delegates to the real read; counts only the journal under test."""
        data = real_read_bytes(self)
        if self in targets:
            journal_reads.append(len(data))
        return data

    def counting_scan(sid):
        path = sessionlog._journal_path(sid)
        targets.add(path)
        size_before = path.stat().st_size if path.exists() else 0
        first_read = len(journal_reads)
        try:
            return real_scan(sid)
        finally:
            per_scan.append({
                "journal_size_at_scan_start": size_before,
                "actual_bytes_read": sum(journal_reads[first_read:]),
            })

    def recording_read_verified(conversation_id):
        # Marks the boundary between the append phase and the benchmark's own
        # final integrity read, so the latter can never contaminate the
        # append-phase ceilings. Recorded by position, not by assuming how many
        # scans the append phase performed.
        if verification_at["index"] is None:
            verification_at["index"] = len(per_scan)
        return real_read_verified(conversation_id)

    def counting_fsync(fd):
        fsyncs["n"] += 1
        return real_fsync(fd)

    monkeypatch.setattr(sessionlog, "_open_append", counting_open)
    monkeypatch.setattr(sessionlog, "_scan", counting_scan)
    monkeypatch.setattr(sessionlog, "read_verified", recording_read_verified)
    monkeypatch.setattr(sessionlog.os, "fsync", counting_fsync)
    # One METHOD, not the class, and filtered to the journal under test.
    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    r = pv.bench_sessionlog_append(n=n, fsync="auto")

    assert r["records_verified"] == n and r["journal_status"] == "ok"
    assert verification_at["index"] is not None, (
        "the benchmark never called read_verified, so the append phase and "
        "the verification phase could not be separated")

    boundary = verification_at["index"]
    append_scans = per_scan[:boundary]
    verify_scans = per_scan[boundary:]

    # Journal size immediately before append i, derived from the bytes actually
    # written — never from a fixed record size, and never from stat().
    size_before_append = [sum(writes[:i]) for i in range(len(writes))]

    violations = append_work_violations(
        n=n,
        append_scans=append_scans,
        verification_scans=verify_scans,
        size_before_append=size_before_append,
        writes=writes,
        journal_bytes=r["journal_bytes"],
        fsyncs=fsyncs["n"],
        opens=opens["n"],
    )
    assert violations == [], violations

    # The seams really fired — otherwise the ceilings above are vacuous.
    assert journal_reads, "no read of the journal under test was observed"
    assert len(writes) == n and min(writes) > 0
    # `journal_bytes == sum(writes)` above is the guarantee that matters: the
    # expected byte total comes from the bytes ACTUALLY handed to the write
    # seam, never from an assumed constant record size. No assertion here
    # claims anything about how much the lengths vary.


_SYNTH_N = 12
_SYNTH_WRITES = [100] * _SYNTH_N
_SYNTH_SIZES = [sum(_SYNTH_WRITES[:i]) for i in range(_SYNTH_N)]


def _scan(size, read=None):
    """One coherent scan record: a journal of `size` from which `read` came."""
    return {"journal_size_at_scan_start": size,
            "actual_bytes_read": size if read is None else read}


def _synthetic(**over):
    """A coherent baseline measurement of the CURRENT full-rescan shape.

    Scan count and bytes read are never passed in — the helper derives them
    from the scan lists — so an override cannot state a count that disagrees
    with the records supplied.
    """
    base = dict(
        n=_SYNTH_N,
        append_scans=[_scan(size) for size in _SYNTH_SIZES],
        verification_scans=[_scan(sum(_SYNTH_WRITES))],
        size_before_append=list(_SYNTH_SIZES),
        writes=list(_SYNTH_WRITES),
        journal_bytes=sum(_SYNTH_WRITES),
        fsyncs=_SYNTH_N,
        opens=_SYNTH_N,
    )
    base.update(over)
    return base


@pytest.mark.parametrize("over, expect_violation, why", [
    ({}, False, "the current full-rescan shape passes"),

    # LESS work must always be allowed — the whole point of ceilings. These are
    # COHERENT: the scan list itself is empty or short, so the derived count
    # and byte total follow from it and cannot contradict it.
    ({"append_scans": []}, False,
     "a future cache doing NO append-phase scans must pass"),
    ({"append_scans": [_scan(1100, 100)]}, False,
     "a single incremental tail read must pass"),
    ({"append_scans": [_scan(size, 0) for size in _SYNTH_SIZES]}, False,
     "scans that read nothing must pass"),

    # MORE work must fail.
    ({"append_scans": [_scan(size) for size in _SYNTH_SIZES]
                      + [_scan(1200)]}, True,
     "a 13th append-phase scan for 12 appends"),
    ({"append_scans": [_scan(10 ** 9, 10 ** 9)]}, True,
     "one scan reading past the whole-run rescan budget"),
    ({"append_scans": [_scan(100, 101)]}, True,
     "a scan reading more than its journal holds"),
    ({"verification_scans": []}, True, "no final verification scan"),
    ({"fsyncs": 11}, True, "a lost fsync is lost durability"),
    ({"fsyncs": 13}, True, "a duplicated fsync"),
    ({"opens": 13}, True, "a duplicated open"),
    ({"opens": 0}, True, "no open at all"),
    ({"journal_bytes": 1}, True, "journal bytes != bytes written"),

    # Structural inputs fail closed rather than skipping a check.
    ({"size_before_append": _SYNTH_SIZES[:5]}, True,
     "one journal size per append is required"),
    ({"size_before_append": [-1] + _SYNTH_SIZES[1:]}, True,
     "a negative journal size"),
    ({"append_scans": [{"actual_bytes_read": 0}]}, True,
     "a scan record missing its journal size"),
    ({"append_scans": [_scan(100, -1)]}, True, "a negative byte count"),
    ({"append_scans": [_scan(100, True)]}, True,
     "a bool masquerading as a byte count"),
    ({"append_scans": ["not a scan record"]}, True, "a malformed scan record"),
])
def test_work_ceilings_allow_less_work_and_reject_more(over, expect_violation,
                                                       why):
    """Synthetic proof that the contract is a ceiling, not a fingerprint.

    Driven with injected records, so it needs no filesystem and no timing. The
    zero-scan, single-read and read-nothing cases are the ones that matter:
    they prove a future optimization reading less of the journal is ACCEPTED
    rather than failed, which the removed exact-equality assertion would have
    blocked. Each is stated by supplying the actual scan records, so the claim
    and the evidence cannot disagree.
    """
    violations = append_work_violations(**_synthetic(**over))
    assert bool(violations) is expect_violation, (why, violations)


def test_zero_append_scan_case_really_supplies_no_scan_records():
    """Guards the guard: the zero-scan case must be coherent, not asserted.

    An earlier revision claimed zero scans while passing twelve scan records,
    so it proved nothing. Derivation from the list is what makes the claim
    real, and this pins that the helper takes no independent count or byte
    argument that could reintroduce the contradiction.
    """
    import inspect

    params = set(inspect.signature(append_work_violations).parameters)
    assert "append_scan_count" not in params
    assert "append_bytes_read" not in params
    assert "per_scan" not in params
    assert {"append_scans", "verification_scans"} <= params

    case = _synthetic(append_scans=[])
    assert case["append_scans"] == []
    assert append_work_violations(**case) == []


# --- 1a-evaluator: the relocated append contracts must fail CLOSED ----------
#
# The preserved 60/200 (fsync=auto) and 100/400 (fsync=always) contracts now
# live in the telemetry runner, which the required suite does not execute. Its
# evaluator, publisher and runner are therefore pinned here deterministically,
# with injected results and injected failures — no wall-clock, no filesystem
# benchmark — so the moved contract cannot rot unnoticed. Two properties carry
# the design and are hammered accordingly:
#
#   * the evaluator and the runner FAIL CLOSED. An impossible measurement is
#     not a fast one, and an unexpected `None` from any stage is a failure,
#     never a pass. Only the real `cleanup()`'s None return is normal.
#   * the artifact LEAKS NOTHING. It is published to a CI run, so a rejected
#     value, an exception message, a repr, a path or a caller-controlled type
#     name must never reach it — and stdout is exactly the artifact bytes.

def _slt():
    import sessionlog_latency_telemetry as slt
    return slt


_TELEMETRY_SECRET = "sk_live_9f3a21_TAVILY_API_KEY"

_LEAKY = ("TAVILY_API_KEY=sk_live_9f3a21 "
          "C:/Users/someone/AppData/Local/Temp/olymperf-abc/journal")

# Python ints are arbitrary precision, so `math.isfinite(10**400)` raises
# rather than returning False. A validator that crashes is not fail-closed.
_HUGE_INT = 10 ** 400

_APPEND_N = 120

_APPEND_MEASUREMENT_FIELDS = ("p50", "p90", "p99", "max", "mean",
                              "first_decile_mean", "last_decile_mean",
                              "growth_ratio", "slope_ms_per_record",
                              "projected_ms_at_10k", "projected_ms_at_50k")
_APPEND_COUNT_FIELDS = ("n", "records_verified", "journal_bytes")


class _TelemetryFailure(Exception):
    """Carries a credential-and-path-shaped message, as a real one might."""


class _UnserializablePoison:
    def __repr__(self):
        return f"<Unserializable {_TELEMETRY_SECRET}>"


class _SecretInt(int):
    """Passes `isinstance(x, int)`; overrides everything that could print it,
    and lies about every comparison."""

    def __format__(self, spec):
        return _TELEMETRY_SECRET

    def __repr__(self):
        return _TELEMETRY_SECRET

    def __str__(self):
        return _TELEMETRY_SECRET

    def __lt__(self, other):
        return True

    def __le__(self, other):
        return True

    def __gt__(self, other):
        return False

    def __ge__(self, other):
        return False


class _SecretFloat(float):
    def __format__(self, spec):
        return _TELEMETRY_SECRET

    def __repr__(self):
        return _TELEMETRY_SECRET

    def __str__(self):
        return _TELEMETRY_SECRET

    def __lt__(self, other):
        return True

    def __le__(self, other):
        return True

    def __gt__(self, other):
        return False

    def __ge__(self, other):
        return False


class _SecretStr(str):
    """Answers `== anything` truthfully-looking while carrying the canary."""

    def __new__(cls):
        return super().__new__(cls, _TELEMETRY_SECRET)

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    def __hash__(self):
        return hash("ok")

    def __repr__(self):
        return _TELEMETRY_SECRET

    def __str__(self):
        return _TELEMETRY_SECRET


def _auto_contract():
    slt = _slt()
    return next(c for c in slt.CONTRACTS if c["fsync"] == "auto")


def _always_contract():
    slt = _slt()
    return next(c for c in slt.CONTRACTS if c["fsync"] == "always")


def _sample(**over):
    """One structurally perfect measurement inside the auto thresholds.

    Derived fields are computed with the benchmark's own formulas
    (perf_validation.bench_sessionlog_append), so the baseline satisfies every
    identity; a test that wants an inconsistency pins the field itself.
    """
    n = _APPEND_N
    head, tail = 2.0, 1.0               # a decaying series, like the CI run
    k = max(5, n // 10)
    span = max(1.0, float(n) - k)
    slope = (tail - head) / span
    growth = max(0.0, slope)
    base = {"n": n, "fsync": "auto",
            "p50": 5.0, "p90": 8.0, "p99": 13.0, "max": 20.0, "mean": 6.0,
            "first_decile_mean": head, "last_decile_mean": tail,
            "growth_ratio": tail / head,
            "slope_ms_per_record": slope,
            "projected_ms_at_10k": head + growth * 10000,
            "projected_ms_at_50k": head + growth * 50000,
            "records_verified": n, "journal_status": "ok",
            "journal_bytes": 74640,
            "conversation_id": "perf-auto-deadbeef"}
    base.update(over)
    return base


def test_telemetry_contract_thresholds_are_the_preserved_ones():
    """The moved numbers are the original numbers. Pinned so they cannot drift."""
    slt = _slt()
    by_mode = {c["fsync"]: c for c in slt.CONTRACTS}
    assert by_mode["auto"]["n"] == 120
    assert by_mode["auto"]["p50_max_ms"] == 60.0
    assert by_mode["auto"]["p99_max_ms"] == 200.0
    assert by_mode["always"]["n"] == 120
    assert by_mode["always"]["p50_max_ms"] == 100.0
    assert by_mode["always"]["p99_max_ms"] == 400.0


def test_telemetry_evaluator_passes_a_result_inside_both_thresholds():
    slt = _slt()
    assert slt.evaluate(_sample(), _auto_contract()) == []
    assert slt.evaluate(_sample(fsync="always"), _always_contract()) == []


@pytest.mark.parametrize("over, needle", [
    ({"p50": 60.0, "p90": 60.0, "p99": 60.0, "max": 60.0}, "p50"),
    # the exact 2026-08-08 CI values
    ({"p50": 88.1122, "p90": 90.0, "p99": 95.0, "max": 99.0}, "p50"),
    ({"p99": 200.0, "max": 200.0}, "p99"),
    ({"p99": 459.9876, "max": 460.0}, "p99"),
])
def test_telemetry_evaluator_fails_a_breach(over, needle):
    reasons = _slt().evaluate(_sample(**over), _auto_contract())
    assert reasons and any(needle in r for r in reasons), reasons


def test_the_always_contract_enforces_its_own_thresholds():
    """100/400, not 60/200 — a mode mix-up must not borrow the wrong bound."""
    slt = _slt()
    inside = _sample(fsync="always", p50=88.0, p90=90.0, p99=95.0, max=99.0)
    assert slt.evaluate(inside, _always_contract()) == []
    breach = _sample(fsync="always", p50=100.0, p90=150.0, p99=400.0,
                     max=400.0)
    reasons = slt.evaluate(breach, _always_contract())
    assert any("p50" in r for r in reasons), reasons
    assert any("p99" in r for r in reasons), reasons


def test_a_breach_is_not_hidden_by_another_passing_statistic():
    """A p99 breach must fail even when p50 is comfortably inside its bound.

    The evaluator collects both thresholds instead of short-circuiting, so one
    healthy statistic can never mask a broken one.
    """
    slt = _slt()
    reasons = slt.evaluate(_sample(p99=1000.0, max=1000.0), _auto_contract())
    assert any("p99" in r for r in reasons)
    both = slt.evaluate(_sample(p50=1000.0, p90=1000.0, p99=1000.0,
                                max=1000.0), _auto_contract())
    assert sum(1 for r in both if "p50" in r or "p99" in r) >= 2, both


def test_thresholds_are_applied_only_after_structural_validation():
    """A structurally broken result reports its defect, not a bound."""
    reasons = _slt().evaluate(_sample(p50=float("nan")), _auto_contract())
    assert all("ms >=" not in r for r in reasons), reasons
    assert any("p50" in r and "withheld" in r for r in reasons), reasons


@pytest.mark.parametrize("over, why", [
    ({"p50": float("nan")}, "NaN p50 (nan < 60 is False by accident)"),
    ({"p99": float("nan")}, "NaN p99"),
    ({"p50": float("inf")}, "infinite p50"),
    ({"p99": float("-inf")}, "negative-infinite p99"),
    ({"n": 119}, "wrong sample count"),
    ({"n": 121}, "sample count above the contract"),
    ({"n": "120"}, "non-int sample count"),
    ({"p50": 0.0}, "zero p50 — no real work measured"),
    ({"records_verified": 119}, "a partial journal"),
    ({"records_verified": None}, "missing integrity accounting"),
    ({"journal_status": "torn_tail_truncated"}, "a damaged journal"),
    ({"journal_bytes": 0}, "an empty journal"),
    ({"fsync": "always"}, "the wrong fsync mode for the contract"),
])
def test_telemetry_evaluator_fails_closed_on_unusable_measurements(over, why):
    """An unusable measurement is a failure, never a pass."""
    assert _slt().evaluate(_sample(**over), _auto_contract()), why


@pytest.mark.parametrize("missing", sorted(
    _APPEND_MEASUREMENT_FIELDS + _APPEND_COUNT_FIELDS
    + ("fsync", "journal_status")))
def test_telemetry_evaluator_fails_on_incomplete_results(missing):
    sample = _sample()
    del sample[missing]
    reasons = _slt().evaluate(sample, _auto_contract())
    assert any(missing in r for r in reasons), reasons


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", _APPEND_MEASUREMENT_FIELDS)
def test_non_finite_measurements_fail_closed(field, value):
    assert _slt().evaluate(_sample(**{field: value}), _auto_contract()), (
        field, value)


@pytest.mark.parametrize("field", [f for f in _APPEND_MEASUREMENT_FIELDS
                                   if f != "slope_ms_per_record"])
@pytest.mark.parametrize("value", [0.0, 0, -1.0, -0.000001])
def test_non_positive_durations_fail_closed(field, value):
    """A latency at or below zero is impossible, not fast — `-1 < 200` is True."""
    assert _slt().evaluate(_sample(**{field: value}), _auto_contract()), (
        field, value)


def test_a_negative_slope_is_legitimate():
    """A decaying series is exactly the shape worth reporting, not rejecting."""
    base = _sample()
    assert base["slope_ms_per_record"] < 0.0
    assert _slt().evaluate(base, _auto_contract()) == []


@pytest.mark.parametrize("field", _APPEND_COUNT_FIELDS)
@pytest.mark.parametrize("value", [True, False, "120", 120.0, -1, None])
def test_boolean_and_non_integer_counts_fail_closed(field, value):
    assert _slt().evaluate(_sample(**{field: value}), _auto_contract()), (
        field, value)


@pytest.mark.parametrize("value", ["absent", "torn_tail_truncated",
                                   "quarantined", "oversize", "OK", "", None])
def test_a_journal_that_is_not_ok_fails_closed(value):
    assert _slt().evaluate(_sample(journal_status=value), _auto_contract()), \
        value


@pytest.mark.parametrize("value", ["auto ", "AUTO", "always", "", None, True,
                                   1])
def test_a_wrong_fsync_mode_fails_closed(value):
    assert _slt().evaluate(_sample(fsync=value), _auto_contract()), value


def test_the_result_must_be_a_mapping():
    for bad in (None, [], "result", 7, object()):
        assert _slt().evaluate(bad, _auto_contract())


# --- internal consistency ----------------------------------------------------

@pytest.mark.parametrize("over, needle", [
    ({"p50": 9.0, "p90": 8.5}, "p50"),                 # p50 > p90
    ({"p90": 14.0, "p99": 13.5}, "p90"),               # p90 > p99
    ({"p99": 21.0}, "p99"),                            # p99 > max
    ({"mean": 25.0}, "mean"),                          # mean > max
    ({"first_decile_mean": 99.0}, "first_decile_mean"),
])
def test_inverted_percentiles_fail_closed(over, needle):
    reasons = _slt().evaluate(_sample(**over), _auto_contract())
    assert reasons and any(needle in r for r in reasons), reasons


def test_the_whole_inverted_set_is_rejected():
    """Every value under its bound, yet the measurement is impossible."""
    assert _slt().evaluate(_sample(p50=1.0, p90=0.8, p99=0.5, max=0.4,
                                   mean=0.3), _auto_contract())


@pytest.mark.parametrize("over, needle", [
    ({"growth_ratio": 0.123}, "growth_ratio"),
    ({"slope_ms_per_record": 5.0}, "slope"),
    ({"projected_ms_at_10k": 12.5}, "projected_ms_at_10k"),
    ({"projected_ms_at_50k": 12.5}, "projected_ms_at_50k"),
])
def test_derived_identities_are_recomputed(over, needle):
    reasons = _slt().evaluate(_sample(**over), _auto_contract())
    assert reasons and any(needle in r for r in reasons), reasons


def test_derived_identities_tolerate_only_float_noise():
    slt = _slt()
    base = _sample()
    for nudge in (1.0, 1 + 1e-13, 1 - 1e-13):
        assert slt.evaluate(
            _sample(growth_ratio=base["growth_ratio"] * nudge),
            _auto_contract()) == [], nudge
    assert slt.evaluate(_sample(growth_ratio=base["growth_ratio"] * 1.001),
                        _auto_contract())


def test_a_growing_series_projects_upward_and_still_passes():
    """The documented O(n^2) append shape must not be rejected by the
    identities — a rising series with clamped projections is the healthy
    case, not an anomaly."""
    n = _APPEND_N
    head, tail = 1.0, 3.0
    k = max(5, n // 10)
    span = max(1.0, float(n) - k)
    slope = (tail - head) / span
    row = _sample(first_decile_mean=head, last_decile_mean=tail,
                  growth_ratio=tail / head, slope_ms_per_record=slope,
                  projected_ms_at_10k=head + slope * 10000,
                  projected_ms_at_50k=head + slope * 50000)
    assert _slt().evaluate(row, _auto_contract()) == []


# --- numeric subclasses are not safe numbers ---------------------------------

@pytest.mark.parametrize("field", _APPEND_COUNT_FIELDS)
def test_int_subclasses_are_rejected(field):
    """`isinstance` is not enough — a subclass lies about every comparison."""
    reasons = _slt().evaluate(_sample(**{field: _SecretInt(120)}),
                              _auto_contract())
    assert reasons, f"{field} accepted an int subclass"
    assert _TELEMETRY_SECRET not in " ".join(reasons)


@pytest.mark.parametrize("field", _APPEND_MEASUREMENT_FIELDS)
def test_float_subclasses_are_rejected(field):
    reasons = _slt().evaluate(_sample(**{field: _SecretFloat(1.0)}),
                              _auto_contract())
    assert reasons, f"{field} accepted a float subclass"
    assert _TELEMETRY_SECRET not in " ".join(reasons)


def test_a_comparison_lying_subclass_cannot_satisfy_a_threshold():
    """`__lt__` returning True would otherwise walk straight past both bounds."""
    liar = _SecretFloat(9999.0)
    assert liar < 1.0 and liar < 60.0        # it really does lie
    reasons = _slt().evaluate(_sample(p50=liar, p99=liar), _auto_contract())
    assert reasons
    assert _TELEMETRY_SECRET not in " ".join(reasons)


def test_a_status_subclass_impersonating_ok_is_rejected():
    slt = _slt()
    impostor = _SecretStr()
    assert impostor == "ok", "the impostor does not actually lie; test is void"
    reasons = slt.evaluate(_sample(journal_status=impostor), _auto_contract())
    assert any("journal_status" in r for r in reasons), reasons
    assert _TELEMETRY_SECRET not in " ".join(reasons)
    assert slt._published(_sample(journal_status=impostor),
                          _auto_contract()) is None


def test_an_fsync_subclass_impersonating_the_mode_is_rejected():
    impostor = _SecretStr()
    assert impostor == "auto", "the impostor does not actually lie"
    reasons = _slt().evaluate(_sample(fsync=impostor), _auto_contract())
    assert any("fsync" in r for r in reasons), reasons
    assert _TELEMETRY_SECRET not in " ".join(reasons)


# --- an oversized int must be rejected, not raise ----------------------------

def test_the_validators_never_raise_on_an_oversized_int():
    """The primitives themselves must return False, not explode."""
    slt = _slt()
    assert slt._finite(_HUGE_INT) is False
    assert slt._finite(-_HUGE_INT) is False
    assert slt._positive(_HUGE_INT) is False
    assert slt._count(_HUGE_INT) is False
    assert slt._count(120) is True and slt._count(0) is True
    assert slt._count(sys.maxsize) is True
    assert slt._count(sys.maxsize + 1) is False
    assert slt._count(-1) is False


@pytest.mark.parametrize("field",
                         _APPEND_COUNT_FIELDS + _APPEND_MEASUREMENT_FIELDS)
def test_an_oversized_int_is_rejected_and_never_rendered(field):
    slt = _slt()
    reasons = slt.evaluate(_sample(**{field: _HUGE_INT}), _auto_contract())
    assert reasons, f"{field} accepted 10**400"
    assert "0000000000" not in " ".join(reasons)
    assert slt._published(_sample(**{field: _HUGE_INT}),
                          _auto_contract()) is None


# --- publication is type-validated, not merely key-filtered ------------------

def test_a_structurally_invalid_result_publishes_nothing():
    """Not a partial measurement — no measurement.

    A field-by-field sanitized view of an invalid row reads as a measurement
    with gaps, when in fact nothing about it can be trusted.
    """
    assert _slt()._published(
        _sample(p50=_TELEMETRY_SECRET, n=_TELEMETRY_SECRET,
                journal_bytes=True, slope_ms_per_record=float("nan")),
        _auto_contract()) is None


@pytest.mark.parametrize("over, why", [
    ({"p50": _TELEMETRY_SECRET}, "a secret-shaped measurement"),
    ({"n": _TELEMETRY_SECRET}, "a secret-shaped count"),
    ({"journal_bytes": True}, "a boolean count"),
    ({"slope_ms_per_record": float("nan")}, "NaN"),
    ({"p99": float("inf")}, "infinity"),
    ({"p50": _SecretFloat(1.0)}, "a float subclass"),
    ({"n": _SecretInt(120)}, "an int subclass"),
    ({"journal_status": "absent"}, "a journal that is not ok"),
    ({"fsync": "always"}, "the wrong fsync mode"),
    ({"n": _HUGE_INT}, "an unrepresentable count"),
    ({"n": 121}, "a sample-count mismatch"),
    ({"records_verified": 119}, "a record-count mismatch"),
    ({"journal_bytes": 0}, "an empty journal"),
    ({"p50": 21.0}, "p50 above max"),
    ({"projected_ms_at_10k": 12.5}, "an inconsistent projection"),
    ({"growth_ratio": 0.123}, "an inconsistent growth ratio"),
])
def test_published_returns_none_for_every_structurally_invalid_case(over, why):
    assert _slt()._published(_sample(**over), _auto_contract()) is None, why


def test_a_valid_measurement_is_published_type_normalised():
    slt = _slt()
    published = slt._published(_sample(), _auto_contract())
    assert published is not None
    assert type(published["p50"]) is float and published["p50"] == 5.0
    assert type(published["records_verified"]) is int
    assert published["records_verified"] == _APPEND_N
    assert published["fsync"] == "auto"
    assert published["journal_status_ok"] is True
    assert _TELEMETRY_SECRET not in json.dumps(published)


def test_the_conversation_id_and_status_string_are_never_published():
    published = _slt()._published(_sample(), _auto_contract())
    assert "conversation_id" not in published
    assert "journal_status" not in published        # only the boolean
    assert "perf-auto-deadbeef" not in json.dumps(published)


def test_extra_result_keys_are_never_published():
    """The published artifact carries numbers, not environment or paths."""
    dirty = _sample(secret_env="TAVILY_API_KEY=abc",
                    path="C:/Users/someone/AppData/Local/Temp/x",
                    payload={"messages": ["..."]})
    published = _slt()._published(dirty, _auto_contract())
    assert published is not None
    blob = json.dumps(published)
    for needle in ("secret_env", "TAVILY_API_KEY", "path", "AppData",
                   "payload", "messages"):
        assert needle not in blob, needle


# --- exactly one measurement per contract, or the run is unverified ----------

def test_measurement_completeness_requires_exactly_one_entry_per_contract():
    slt = _slt()
    good = [{"fsync": "auto"}, {"fsync": "always"}]
    assert slt.measurement_completeness_violations(good) == []
    for bad in ([],                                        # nothing measured
                [{"fsync": "auto"}],                       # always missing
                [{"fsync": "always"}],                     # auto missing
                [{"fsync": "always"}, {"fsync": "auto"}],  # out of order
                [{"fsync": "auto"}, {"fsync": "auto"}],    # duplicated
                [{"fsync": "auto"}, {"fsync": "always"},
                 {"fsync": "always"}],                     # a third run
                [{"fsync": "auto"}, {}],                   # no mode at all
                [{"fsync": "auto"}, "not an entry"],       # malformed entry
                "not a list"):
        assert slt.measurement_completeness_violations(bad), bad


def test_measurement_completeness_rejects_an_impersonating_mode():
    impostor = _SecretStr()
    assert impostor == "always", "the impostor does not actually lie"
    violations = _slt().measurement_completeness_violations(
        [{"fsync": "auto"}, {"fsync": impostor}])
    assert violations
    assert _TELEMETRY_SECRET not in " ".join(violations)


def test_the_runner_really_enforces_measurement_completeness(monkeypatch,
                                                             tmp_path):
    """main() must call the completeness check on what it publishes."""
    slt = _slt()
    sentinel = "completeness sentinel violation"
    monkeypatch.setattr(slt, "measurement_completeness_violations",
                        lambda measurements: [sentinel])
    code, out = _run_telemetry(monkeypatch, tmp_path,
                               lambda: _FakeAppendHarness())
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert sentinel in data["failure_reasons"]


# --- the telemetry runner must go red WITH evidence, never red in silence ---
#
# The workflow uploads the artifact with `if: always()`, which is worthless if
# an operational failure kills the process before the file exists. These drive
# `main()` end to end against a temporary path, with the benchmark harness
# replaced at a named seam — so no real timing measurement runs.

class _FakeAppendHarness:
    """Stands in for `perf_validation` at the `_load_benchmark` seam.

    `results` maps an fsync mode to what `bench_sessionlog_append` returns for
    it; a mode absent from the map returns a healthy row for that mode. Every
    call is recorded, so the exact one-call-per-contract behaviour is asserted
    rather than assumed. The real `cleanup()` returns None, and so does this
    one — that None is a normal success and must never read as a failure.
    """

    def __init__(self, *, results=None, bench_error=None, bench_error_on=None,
                 bench_returns_none_on=None, cleanup_error=None):
        self._results = results or {}
        self._bench_error = bench_error
        self._bench_error_on = bench_error_on
        self._bench_returns_none_on = bench_returns_none_on
        self._cleanup_error = cleanup_error
        self.calls = []
        self.cleanup_calls = 0

    def bench_sessionlog_append(self, n, fsync):
        self.calls.append((n, fsync))
        if self._bench_error is not None and \
                self._bench_error_on in (None, fsync):
            raise self._bench_error
        if self._bench_returns_none_on in ("*", fsync):
            return None
        if fsync in self._results:
            return self._results[fsync]
        return _sample(fsync=fsync)

    def cleanup(self):
        self.cleanup_calls += 1
        if self._cleanup_error is not None:
            raise self._cleanup_error
        return None


def _run_telemetry(monkeypatch, tmp_path, loader):
    slt = _slt()
    monkeypatch.setattr(slt, "_load_benchmark", loader)
    out = tmp_path / "telemetry" / "result.json"
    code = slt.main(["--out", str(out)])
    return code, out


def test_a_healthy_run_exits_zero_and_cleanup_none_is_not_a_failure(
        monkeypatch, tmp_path):
    """The green path — including the real cleanup()'s legitimate None."""
    harness = _FakeAppendHarness()
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "pass"
    assert data["failure_reasons"] == [] and data["failed_stages"] == []
    assert [m["fsync"] for m in data["measurements"]] == ["auto", "always"]
    for entry in data["measurements"]:
        assert entry["passed"] is True
        assert entry["violation_count"] == 0
        assert entry["measurement"]["records_verified"] == _APPEND_N
        assert entry["measurement"]["fsync"] == entry["fsync"]
    assert harness.cleanup_calls == 1


def test_each_contract_is_measured_exactly_once_and_never_retried(monkeypatch,
                                                                  tmp_path):
    slt = _slt()
    expected_calls = [(c["n"], c["fsync"]) for c in slt.CONTRACTS]

    harness = _FakeAppendHarness()
    code, _out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 0
    assert harness.calls == expected_calls

    # A breach is NOT retried either: still one call per contract.
    breach = _FakeAppendHarness(
        results={"auto": _sample(p99=459.9876, max=460.0)})
    code, _out = _run_telemetry(monkeypatch, tmp_path, lambda: breach)
    assert code == 1
    assert breach.calls == expected_calls


def test_a_benchmark_fault_in_one_contract_does_not_retry_or_skip_the_other(
        monkeypatch, tmp_path):
    harness = _FakeAppendHarness(
        bench_error=_TelemetryFailure(_LEAKY), bench_error_on="auto")
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    assert harness.calls == [(120, "auto"), (120, "always")], (
        "a fault must neither retry the failed contract nor skip the other")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any(s["stage"] == "benchmark" and s.get("fsync") == "auto"
               for s in data["failed_stages"]), data["failed_stages"]
    by_mode = {m["fsync"]: m for m in data["measurements"]}
    assert by_mode["auto"]["measurement"] is None
    assert by_mode["auto"]["passed"] is False
    assert by_mode["always"]["passed"] is True
    assert by_mode["always"]["measurement"] is not None
    assert harness.cleanup_calls == 1


@pytest.mark.parametrize("stage, loader_factory", [
    ("setup", lambda: (lambda: (_ for _ in ()).throw(
        _TelemetryFailure(_LEAKY)))),
    ("benchmark", lambda: (lambda: _FakeAppendHarness(
        bench_error=_TelemetryFailure(_LEAKY)))),
    ("cleanup", lambda: (lambda: _FakeAppendHarness(
        cleanup_error=_TelemetryFailure(_LEAKY)))),
])
def test_a_raising_stage_writes_a_red_artifact_with_a_closed_stage_name(
        stage, loader_factory, monkeypatch, tmp_path):
    code, out = _run_telemetry(monkeypatch, tmp_path, loader_factory())
    assert code == 1
    assert out.is_file(), f"no artifact was written for a failed {stage}"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any(s["stage"] == stage for s in data["failed_stages"]), data
    slt = _slt()
    assert all(s["stage"] in slt.STAGES for s in data["failed_stages"])
    assert all(isinstance(s["error_count"], int)
               for s in data["failed_stages"])


def test_a_raising_evaluator_is_a_red_evaluation_stage(monkeypatch, tmp_path):
    slt = _slt()

    def exploding_evaluate(result, contract):
        raise _TelemetryFailure(_LEAKY)

    monkeypatch.setattr(slt, "evaluate", exploding_evaluate)
    harness = _FakeAppendHarness()
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any(s["stage"] == "evaluation" for s in data["failed_stages"])
    assert harness.cleanup_calls == 1, "cleanup must still run"


def test_a_raising_publisher_is_a_red_publication_stage(monkeypatch, tmp_path,
                                                        capsys):
    """`_published` walks caller data and can raise; that must be contained.

    A hostile `__eq__`/`__float__` can raise from inside publication, after
    evaluation has already succeeded. Unprotected, that would escape as a
    traceback carrying an exception message no validator ever inspected.
    """
    slt = _slt()
    boom = type(f"PubErr_{_TELEMETRY_SECRET}", (Exception,), {})

    def exploding_publish(result, contract):
        raise boom(_TELEMETRY_SECRET)

    monkeypatch.setattr(slt, "_published", exploding_publish)
    harness = _FakeAppendHarness()
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert any(s["stage"] == "publication" for s in data["failed_stages"])
    assert all(m["measurement"] is None for m in data["measurements"])
    assert harness.cleanup_calls == 1, "cleanup must still run"
    captured = capsys.readouterr()
    for channel, text in (("artifact", blob), ("stdout", captured.out),
                          ("stderr", captured.err)):
        assert _TELEMETRY_SECRET not in text, f"leak into {channel}"
        assert "PubErr_" not in text, f"class name leaked into {channel}"


@pytest.mark.parametrize("stage, loader_factory", [
    ("setup", lambda: (lambda: None)),
    ("benchmark", lambda: (lambda: _FakeAppendHarness(
        bench_returns_none_on="*"))),
])
def test_an_unexpected_none_return_fails_closed(stage, loader_factory,
                                                monkeypatch, tmp_path):
    """A stage that silently returns None produced nothing — never a pass."""
    code, out = _run_telemetry(monkeypatch, tmp_path, loader_factory())
    assert code == 1, f"a None return from {stage} exited green"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any(s["stage"] == stage for s in data["failed_stages"]), data


def test_an_evaluator_returning_none_fails_closed(monkeypatch, tmp_path):
    slt = _slt()
    monkeypatch.setattr(slt, "evaluate", lambda result, contract: None)
    harness = _FakeAppendHarness()
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert any(s["stage"] == "evaluation" for s in data["failed_stages"])
    assert harness.cleanup_calls == 1


def test_a_healthy_result_that_publishes_none_is_a_publication_failure(
        monkeypatch, tmp_path):
    """If evaluation reports healthy, `None` from the publisher is a runner
    defect — it must never read as a pass with no numbers."""
    slt = _slt()
    monkeypatch.setattr(slt, "_published", lambda result, contract: None)
    harness = _FakeAppendHarness()
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any(s["stage"] == "publication" for s in data["failed_stages"])
    assert all(m["passed"] is False for m in data["measurements"])


def test_a_threshold_breach_still_publishes_the_whole_measurement(
        monkeypatch, tmp_path, capsys):
    """A valid measurement that exceeded a limit is exactly what to publish."""
    harness = _FakeAppendHarness(
        results={"auto": _sample(p50=88.1122, p90=90.0, p99=95.0, max=99.0)})
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert data["failed_stages"] == [], "a breach is not a crash"
    by_mode = {m["fsync"]: m for m in data["measurements"]}
    auto = by_mode["auto"]
    assert auto["passed"] is False
    assert auto["measurement"] is not None, "operators must see the numbers"
    # The published numbers are the AUTHENTIC source numbers — the same
    # values the evaluator judged, rebuilt from the source result.
    assert auto["measurement"]["p50"] == pytest.approx(88.1122)
    assert auto["measurement"]["p99"] == pytest.approx(95.0)
    assert auto["measurement"]["records_verified"] == _APPEND_N
    # Only the mode and the violation count are published as reasons — the
    # breach itself is read from the published measurement.
    assert auto["violation_count"] == 1
    assert any("fsync=auto" in r and "1 violation" in r
               for r in data["failure_reasons"])
    assert by_mode["always"]["passed"] is True
    captured = capsys.readouterr()
    assert captured.out == blob
    assert captured.err == ""


@pytest.mark.parametrize("over, why", [
    ({"journal_bytes": 0}, "an empty journal"),
    ({"p50": 1.0, "p90": 0.8, "p99": 0.5, "max": 0.4, "mean": 0.3},
     "an inverted but positive percentile set"),
    ({"projected_ms_at_10k": 12.5}, "an inconsistent projection"),
    ({"fsync": "always"}, "a result from the wrong fsync mode"),
    ({"records_verified": 119}, "a partial run"),
])
def test_a_structurally_invalid_result_publishes_measurement_null(
        over, why, monkeypatch, tmp_path, capsys):
    harness = _FakeAppendHarness(results={"auto": _sample(**over)})
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1, why
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert data["failed_stages"] == [], "a rejected row is not a crash"
    by_mode = {m["fsync"]: m for m in data["measurements"]}
    assert by_mode["auto"]["measurement"] is None, why
    assert by_mode["auto"]["passed"] is False
    captured = capsys.readouterr()
    assert captured.out == blob, "stdout must remain exactly the artifact"
    assert captured.err == ""


def test_a_benchmark_returning_a_non_mapping_is_red_not_a_crash(monkeypatch,
                                                                tmp_path):
    harness = _FakeAppendHarness(results={"auto": ["not", "a", "mapping"]})
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["failed_stages"] == [], "a rejected row is not a crash"
    by_mode = {m["fsync"]: m for m in data["measurements"]}
    assert by_mode["auto"]["measurement"] is None
    assert by_mode["auto"]["passed"] is False
    assert by_mode["auto"]["violation_count"] == 1
    assert any("fsync=auto" in r for r in data["failure_reasons"])


def test_a_serialization_failure_still_writes_a_minimal_red_artifact(
        monkeypatch, tmp_path, capsys):
    """`allow_nan=False` plus an unserialisable payload must not lose the run.

    The poison is injected DOWNSTREAM of the publication schema gate — at the
    `_validated_published` seam — because that gate now rejects a poisoned
    publisher return before it can reach the payload. This exercises the
    serializer backstop itself: the last line of defence when a runner defect
    puts something unserialisable into the payload anyway.
    """
    slt = _slt()
    original = slt._validated_published

    def poisoned(published, result, contract):
        clean = original(published, result, contract)
        clean["poison"] = _UnserializablePoison()
        return clean

    monkeypatch.setattr(slt, "_validated_published", poisoned)
    code, out = _run_telemetry(monkeypatch, tmp_path,
                               lambda: _FakeAppendHarness())
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert data["measurements_withheld"] is True
    assert any(s["stage"] == "serialization" for s in data["failed_stages"])
    assert _TELEMETRY_SECRET not in blob and "Unserializable" not in blob
    captured = capsys.readouterr()
    assert captured.out == blob
    assert captured.err == ""


def test_the_artifact_is_valid_json_without_nan(monkeypatch, tmp_path):
    harness = _FakeAppendHarness(
        results={"auto": _sample(p50=float("nan"), mean=float("inf"))})
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    for token in ("NaN", "Infinity", "-Infinity"):
        assert token not in blob, f"{token} is not valid JSON"
    json.loads(blob)


@pytest.mark.parametrize("loader_factory, expected_code", [
    (lambda: (lambda: _FakeAppendHarness()), 0),
    (lambda: (lambda: _FakeAppendHarness(
        results={"auto": _sample(p99=459.9876, max=460.0)})), 1),
    (lambda: (lambda: _FakeAppendHarness(
        cleanup_error=_TelemetryFailure(_LEAKY))), 1),
])
def test_stdout_is_exactly_the_artifact_and_stderr_is_empty(
        loader_factory, expected_code, monkeypatch, tmp_path, capsys):
    """Byte-for-byte equality, on green AND on handled-failure runs.

    Substring containment was too weak: it permitted the extra `verdict:` and
    `FAIL:` lines the runner used to print, one of which echoed the
    caller-supplied `--out` pathname.
    """
    code, out = _run_telemetry(monkeypatch, tmp_path, loader_factory())
    assert code == expected_code
    captured = capsys.readouterr()
    artifact = out.read_text(encoding="utf-8")
    assert captured.out == artifact, "stdout is not exactly the artifact bytes"
    assert artifact.endswith("}\n") and not artifact.endswith("}\n\n")
    assert captured.err == "", "a handled failure must not write to stderr"


def test_the_output_pathname_never_appears_on_any_channel(monkeypatch,
                                                          tmp_path, capsys):
    """`--out` is caller-supplied; echoing it publishes whatever it contains."""
    slt = _slt()
    out = tmp_path / f"dir_{_TELEMETRY_SECRET}" / f"{_TELEMETRY_SECRET}.json"
    monkeypatch.setattr(slt, "_load_benchmark", lambda: _FakeAppendHarness())
    code = slt.main(["--out", str(out)])

    assert code == 0
    assert out.is_file()
    captured = capsys.readouterr()
    assert _TELEMETRY_SECRET not in captured.out, "the pathname leaked"
    assert captured.err == ""
    assert _TELEMETRY_SECRET not in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("loader_factory", [
    lambda: (lambda: (_ for _ in ()).throw(_TelemetryFailure(_LEAKY))),
    lambda: (lambda: _FakeAppendHarness(
        bench_error=_TelemetryFailure(_LEAKY))),
    lambda: (lambda: _FakeAppendHarness(
        cleanup_error=_TelemetryFailure(_LEAKY))),
])
def test_failure_artifacts_carry_no_exception_type_message_or_path(
        loader_factory, monkeypatch, tmp_path, capsys):
    """Neither the exception TYPE nor its message reaches any channel.

    A real exception message can carry a credential or a scratch path, and
    `type(name, (), {})` makes even `__name__` caller-controlled — so a
    failure publishes a closed stage name and nothing derived from the
    exception at all.
    """
    code, out = _run_telemetry(monkeypatch, tmp_path, loader_factory())
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for channel, text in (("artifact", blob), ("stdout", captured.out),
                          ("stderr", captured.err)):
        for secret in ("TAVILY_API_KEY", "sk_live_9f3a21", "AppData",
                       "C:/Users/someone", "olymperf-abc"):
            assert secret not in text, f"{secret!r} leaked into {channel}"
        assert "_TelemetryFailure" not in text, (
            f"the exception type leaked into {channel}")
    json.loads(blob)


def test_a_dynamic_class_name_carrying_the_secret_never_leaks(monkeypatch,
                                                              tmp_path,
                                                              capsys):
    poison_cls = type(f"Cls_{_TELEMETRY_SECRET}", (Exception,), {})
    harness = _FakeAppendHarness(bench_error=poison_cls(_TELEMETRY_SECRET))
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for channel, text in (("artifact", blob), ("stdout", captured.out),
                          ("stderr", captured.err)):
        assert _TELEMETRY_SECRET not in text, f"leak into {channel}"
        assert "Cls_" not in text, f"the class name leaked into {channel}"
    json.loads(blob)


def test_failure_stages_are_a_closed_vocabulary():
    """Only fixed stage names and contract modes may ever be published."""
    slt = _slt()
    assert set(slt.STAGES) == {"setup", "benchmark", "evaluation",
                               "publication", "cleanup", "serialization"}
    for stage in slt.STAGES:
        assert stage.isascii() and stage.islower()
    assert slt.FSYNC_MODES == ("auto", "always")


def test_cleanup_failure_keeps_the_already_collected_evidence(monkeypatch,
                                                              tmp_path):
    """Cleanup is protected separately, so measurements survive its fault."""
    harness = _FakeAppendHarness(cleanup_error=_TelemetryFailure(_LEAKY))
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any(s["stage"] == "cleanup" for s in data["failed_stages"])
    assert len(data["measurements"]) == len(_slt().CONTRACTS)
    for entry in data["measurements"]:
        assert entry["passed"] is True
        assert entry["measurement"] is not None, (
            "measurements taken before the cleanup fault must survive it")
    assert harness.cleanup_calls == 1, "cleanup must not be retried"


# --- evaluator output is data, never trusted code ----------------------------
#
# `evaluate` is a module global a fault or a patch can replace. Its return
# value therefore gets the same treatment as a benchmark result: only an
# exact built-in list is a verdict, only its LENGTH is used, and the published
# reason is a closed constant carrying the contract mode and the count — never
# the evaluator's own strings.

@pytest.mark.parametrize("evil, why", [
    (False, "False is not a violations list (iterating it would crash)"),
    (True, "True is not a violations list"),
    (object(), "an arbitrary object"),
    (_TELEMETRY_SECRET, "a secret-shaped string"),
    ((), "a tuple is not the exact list type"),
    ({}, "a mapping is not a violations list"),
])
def test_a_non_list_evaluator_return_fails_closed_without_crashing(
        evil, why, monkeypatch, tmp_path, capsys):
    slt = _slt()
    monkeypatch.setattr(slt, "evaluate", lambda result, contract: evil)
    harness = _FakeAppendHarness()
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1, why
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail", "a broken evaluator must never pass"
    assert any(s["stage"] == "evaluation" for s in data["failed_stages"])
    assert all(m["measurement"] is None for m in data["measurements"])
    assert all(m["passed"] is False for m in data["measurements"])
    assert harness.cleanup_calls == 1, "cleanup must still run"
    captured = capsys.readouterr()
    for text in (blob, captured.out, captured.err):
        assert _TELEMETRY_SECRET not in text
    assert captured.out == blob and captured.err == ""


@pytest.mark.parametrize("evil_list, why", [
    ([_TELEMETRY_SECRET], "a fabricated secret-shaped violation"),
    ([object(), object()], "fabricated object violations"),
    ([{"env": _TELEMETRY_SECRET}], "a fabricated mapping violation"),
])
def test_a_fake_violation_for_a_healthy_result_is_an_evaluation_failure(
        evil_list, why, monkeypatch, tmp_path, capsys):
    """The canonical judges find nothing; an evaluator claiming otherwise is
    a count disagreement — one closed evaluation-stage failure — and the
    list's contents never reach any channel."""
    slt = _slt()
    monkeypatch.setattr(slt, "evaluate",
                        lambda result, contract: list(evil_list))
    harness = _FakeAppendHarness()
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1, why
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert any(s["stage"] == "evaluation" for s in data["failed_stages"])
    by_mode = {m["fsync"]: m for m in data["measurements"]}
    assert by_mode["auto"]["violation_count"] is None
    assert by_mode["auto"]["passed"] is False
    assert by_mode["auto"]["measurement"] is None
    assert harness.cleanup_calls == 1, "cleanup must still run"
    captured = capsys.readouterr()
    for text in (blob, captured.out, captured.err):
        assert _TELEMETRY_SECRET not in text
    assert captured.out == blob and captured.err == ""


def test_an_evaluator_returning_empty_for_a_breach_cannot_pass(monkeypatch,
                                                               tmp_path,
                                                               capsys):
    """The v2 false-green: `[]` for a breaching result suppressed the breach.

    The canonical judges find one threshold violation; an evaluator claiming
    zero is a count disagreement, so the run goes red on the evaluation stage
    of exactly the contract whose breach was being hidden.
    """
    slt = _slt()
    monkeypatch.setattr(slt, "evaluate", lambda result, contract: [])
    harness = _FakeAppendHarness(
        results={"auto": _sample(p99=459.9876, max=460.0)})
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1, "a lying evaluator produced a green run on a breach"
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert any(s["stage"] == "evaluation" and s.get("fsync") == "auto"
               for s in data["failed_stages"]), data["failed_stages"]
    by_mode = {m["fsync"]: m for m in data["measurements"]}
    assert by_mode["auto"]["passed"] is False
    assert by_mode["auto"]["measurement"] is None
    # The always contract really is healthy, so `[]` agrees there.
    assert by_mode["always"]["passed"] is True
    captured = capsys.readouterr()
    assert captured.out == blob and captured.err == ""


def test_an_agreeing_but_hostile_violation_list_is_counted_never_published(
        monkeypatch, tmp_path, capsys):
    """When the evaluator's COUNT agrees with the canonical judges, only that
    count is used — the strings still never reach any channel."""
    slt = _slt()
    real_evaluate = slt.evaluate

    def hostile(result, contract):
        return [_TELEMETRY_SECRET] * len(real_evaluate(result, contract))

    monkeypatch.setattr(slt, "evaluate", hostile)
    harness = _FakeAppendHarness(
        results={"auto": _sample(p99=459.9876, max=460.0)})
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert data["failed_stages"] == [], "an agreed count is not a crash"
    by_mode = {m["fsync"]: m for m in data["measurements"]}
    assert by_mode["auto"]["violation_count"] == 1
    assert by_mode["auto"]["passed"] is False
    assert by_mode["always"]["passed"] is True
    assert any("fsync=auto" in r and "1 violation" in r
               for r in data["failure_reasons"])
    captured = capsys.readouterr()
    for text in (blob, captured.out, captured.err):
        assert _TELEMETRY_SECRET not in text
    assert captured.out == blob and captured.err == ""


# --- publication output is schema-validated, never trusted -------------------

@pytest.mark.parametrize("evil, why", [
    ({}, "an empty mapping"),
    ([], "a list"),
    (["p50", 5.0], "a list of fragments"),
    (object(), "an arbitrary object"),
    (_TELEMETRY_SECRET, "a serializable secret string"),
])
def test_a_malformed_publisher_return_is_a_publication_failure(
        evil, why, monkeypatch, tmp_path, capsys):
    """A serializable-but-wrong publisher return must never enter a passing
    artifact — it is one publication-stage failure and measurement: null."""
    slt = _slt()
    monkeypatch.setattr(slt, "_published", lambda result, contract: evil)
    harness = _FakeAppendHarness()
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1, why
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail", "a malformed publication must never pass"
    assert any(s["stage"] == "publication" for s in data["failed_stages"])
    assert all(m["measurement"] is None for m in data["measurements"])
    assert all(m["passed"] is False for m in data["measurements"])
    captured = capsys.readouterr()
    for text in (blob, captured.out, captured.err):
        assert _TELEMETRY_SECRET not in text
    assert captured.out == blob and captured.err == ""


def test_a_publisher_forging_every_latency_value_is_a_publication_failure(
        monkeypatch, tmp_path, capsys):
    """The v3 false-green: a SCHEMA-VALID forgery. A publisher that replaces
    every latency statistic with 999.0 used to exit 0 with verdict pass and
    p50 999.0 against a 60.0 threshold; publication now requires every
    published value to equal the source result's, so the forgery is one
    publication-stage failure with measurement: null."""
    slt = _slt()
    real_published = slt._published

    def forging(result, contract):
        published = real_published(result, contract)
        for key in ("p50", "p90", "p99", "max", "mean"):
            published[key] = 999.0
        return published

    monkeypatch.setattr(slt, "_published", forging)
    harness = _FakeAppendHarness()
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1, "a forged publication produced a green run"
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert any(s["stage"] == "publication" for s in data["failed_stages"])
    assert all(m["measurement"] is None for m in data["measurements"])
    assert all(m["passed"] is False for m in data["measurements"])
    assert "999.0" not in blob, "a forged number reached the artifact"
    captured = capsys.readouterr()
    assert captured.out == blob and captured.err == ""


@pytest.mark.parametrize("key, forged", [
    ("journal_bytes", 1),
    ("n", 240),
    ("records_verified", 119),
    ("slope_ms_per_record", 0.0),
    ("projected_ms_at_50k", 1.5),
])
def test_a_publisher_forging_a_single_field_is_a_publication_failure(
        key, forged, monkeypatch, tmp_path, capsys):
    """One validly typed field that disagrees with the source is enough."""
    slt = _slt()
    real_published = slt._published

    def forging(result, contract):
        published = real_published(result, contract)
        published[key] = forged
        return published

    monkeypatch.setattr(slt, "_published", forging)
    harness = _FakeAppendHarness()
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1, (key, forged)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any(s["stage"] == "publication" for s in data["failed_stages"])
    assert all(m["measurement"] is None for m in data["measurements"])
    captured = capsys.readouterr()
    assert captured.err == ""


def test_an_unchanged_publisher_still_succeeds(monkeypatch, tmp_path):
    """The source-equality gate must not reject honest publication."""
    slt = _slt()
    real_published = slt._published
    monkeypatch.setattr(
        slt, "_published",
        lambda result, contract: real_published(result, contract))
    harness = _FakeAppendHarness()
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "pass"
    for entry in data["measurements"]:
        assert entry["passed"] is True
        assert entry["measurement"] is not None
        assert entry["measurement"]["records_verified"] == _APPEND_N


def test_published_schema_is_exact_source_checked_and_rebuilt():
    slt = _slt()
    contract = _auto_contract()
    result = _sample()
    good = slt._published(result, contract)
    clean = slt._validated_published(good, result, contract)
    assert clean == good
    assert clean is not good, "the measurement must be rebuilt from source"

    # Schema violations.
    missing = dict(good)
    del missing["p50"]
    assert slt._validated_published(missing, result, contract) is None
    assert slt._validated_published(dict(good, poison=1.0), result,
                                    contract) is None
    assert slt._validated_published(dict(good, p50="5.0"), result,
                                    contract) is None
    assert slt._validated_published(dict(good, p50=5), result,
                                    contract) is None
    assert slt._validated_published(dict(good, n=120.0), result,
                                    contract) is None
    assert slt._validated_published(dict(good, n=True), result,
                                    contract) is None
    assert slt._validated_published(dict(good, journal_status_ok=1), result,
                                    contract) is None
    assert slt._validated_published(dict(good, fsync="always"), result,
                                    contract) is None
    assert slt._validated_published(
        dict(good, slope_ms_per_record=float("nan")), result,
        contract) is None

    class _DictSubclass(dict):
        pass

    assert slt._validated_published(_DictSubclass(good), result,
                                    contract) is None

    # Source disagreement: perfect schema and types, fabricated values.
    assert slt._validated_published(dict(good, p50=999.0), result,
                                    contract) is None
    assert slt._validated_published(dict(good, journal_bytes=1), result,
                                    contract) is None
    assert slt._validated_published(dict(good, slope_ms_per_record=0.0),
                                    result, contract) is None

    # A structurally invalid source can never publish, whatever is claimed.
    bad_source = _sample(records_verified=119)
    assert slt._validated_published(good, bad_source, contract) is None


def test_a_hostile_key_impersonating_a_schema_key_is_rejected():
    """A str-subclass key can hash and compare like 'p50' while its actual
    text is a secret; json would serialize the actual text."""
    slt = _slt()
    contract = _auto_contract()
    good = slt._published(_sample(), contract)

    class _KeyImpostor(str):
        def __new__(cls):
            return super().__new__(cls, _TELEMETRY_SECRET)

        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return False

        def __hash__(self):
            return hash("p50")

    bad = {(_KeyImpostor() if key == "p50" else key): value
           for key, value in good.items()}
    assert slt._validated_published(bad, _sample(), contract) is None


# --- the contract table cannot vouch for itself ------------------------------

def test_the_real_contract_table_matches_the_immutable_specification():
    slt = _slt()
    assert slt.contract_configuration_violations() == []
    assert slt.EXPECTED_CONTRACT_SPEC == (("auto", 120, 60.0, 200.0),
                                          ("always", 120, 100.0, 400.0))
    assert slt.EXPECTED_MODES == ("auto", "always")


def _spec_contract(fsync, n, p50, p99):
    return {"benchmark": "sessionlog.append_turn", "fsync": fsync, "n": n,
            "p50_max_ms": p50, "p99_max_ms": p99}


@pytest.mark.parametrize("broken, why", [
    ((), "an empty contract table"),
    ((_spec_contract("auto", 120, 60.0, 200.0),), "a missing contract"),
    ((_spec_contract("auto", 120, 60.0, 200.0),) * 2, "a duplicated contract"),
    ((_spec_contract("always", 120, 100.0, 400.0),
      _spec_contract("auto", 120, 60.0, 200.0)), "a reordered table"),
    ((_spec_contract("auto", 120, 61.0, 200.0),
      _spec_contract("always", 120, 100.0, 400.0)), "a softened p50 bound"),
    ((_spec_contract("auto", 120, 60.0, 200.0),
      _spec_contract("always", 100, 100.0, 400.0)), "an altered sample count"),
    ((_spec_contract("auto", 120, 60.0, 200.0),
      _spec_contract("always", 120, 100.0, _TELEMETRY_SECRET)),
     "a secret-shaped threshold"),
    (("not a contract",
      _spec_contract("always", 120, 100.0, 400.0)), "a malformed entry"),
    ("not a sequence", "a non-sequence table"),
])
def test_a_mutated_contract_table_fails_closed_without_executing(
        broken, why, monkeypatch, tmp_path, capsys):
    slt = _slt()
    monkeypatch.setattr(slt, "CONTRACTS", broken)
    harness = _FakeAppendHarness()
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1, why
    assert harness.calls == [], (
        "a broken contract table must refuse to execute the benchmark")
    assert harness.cleanup_calls == 0
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail", why
    assert data["measurements"] == []
    captured = capsys.readouterr()
    for text in (blob, captured.out, captured.err):
        assert _TELEMETRY_SECRET not in text
    assert captured.out == blob and captured.err == ""


def test_completeness_is_independent_of_contract_table_mutation(monkeypatch):
    """Emptying CONTRACTS must not shrink what completeness demands."""
    slt = _slt()
    monkeypatch.setattr(slt, "CONTRACTS", ())
    assert slt.measurement_completeness_violations([]) != []
    assert slt.measurement_completeness_violations(
        [{"fsync": "auto"}, {"fsync": "always"}]) == []


# --- serialization catches every exception type ------------------------------

def test_a_custom_serialization_exception_still_writes_the_fallback(
        monkeypatch, tmp_path, capsys):
    """The backstop must catch EVERY exception, not just TypeError/ValueError,
    and publish nothing derived from it — neither its type nor its message."""
    slt = _slt()
    boom = type(f"Ser_{_TELEMETRY_SECRET}", (Exception,), {})

    class _ClassLookupBomb:
        """json's failure path reads `o.__class__`; make that raise a custom
        exception carrying the canary, so a TypeError-only handler dies."""

        @property
        def __class__(self):
            raise boom(_TELEMETRY_SECRET)

    original = slt._validated_published

    def poisoned(published, result, contract):
        clean = original(published, result, contract)
        clean["poison"] = _ClassLookupBomb()
        return clean

    monkeypatch.setattr(slt, "_validated_published", poisoned)
    code, out = _run_telemetry(monkeypatch, tmp_path,
                               lambda: _FakeAppendHarness())
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert data["measurements_withheld"] is True
    assert any(s["stage"] == "serialization" for s in data["failed_stages"])
    captured = capsys.readouterr()
    for text in (blob, captured.out, captured.err):
        assert _TELEMETRY_SECRET not in text
        assert "Ser_" not in text
    assert captured.out == blob and captured.err == ""


# --- nothing sensitive may reach any channel ---------------------------------

# Every field is poisoned only with shapes that are genuinely WRONG for it.
# Booleans are invalid for every append field (there are no flag fields in
# this result), so `True` needs no special-casing here.
_APPEND_LEAK_FIELDS = (_APPEND_MEASUREMENT_FIELDS + _APPEND_COUNT_FIELDS
                       + ("journal_status", "fsync"))

# (shape-name, value) — the NAME is what pytest prints. A pytest node id lands
# in CI logs and JUnit XML, so a raw secret-shaped value must never become one.
_APPEND_LEAK_SHAPES = (
    ("secret_str", _TELEMETRY_SECRET),
    ("secret_in_list", [_TELEMETRY_SECRET]),
    ("secret_in_dict", {"env": _TELEMETRY_SECRET}),
    ("secret_repr_object", _UnserializablePoison()),
    ("nan", float("nan")),
    ("inf", float("inf")),
    ("bool", True),
    ("secret_int_subclass", _SecretInt(120)),
    ("secret_float_subclass", _SecretFloat(1.0)),
    ("secret_str_subclass", _SecretStr()),
    ("oversized_int", 10 ** 400),
)

_APPEND_LEAK_CASES = [(f"{field}-{shape}", field, value)
                      for field in _APPEND_LEAK_FIELDS
                      for shape, value in _APPEND_LEAK_SHAPES]


@pytest.mark.parametrize(
    "field, value",
    [(field, value) for _id, field, value in _APPEND_LEAK_CASES],
    ids=[_id for _id, _f, _v in _APPEND_LEAK_CASES])
def test_a_rejected_value_never_reaches_any_channel(field, value, monkeypatch,
                                                    tmp_path, capsys):
    """Every field x every rejected shape, on every channel a CI run reads."""
    slt = _slt()
    reasons = slt.evaluate(_sample(**{field: value}), _auto_contract())
    assert reasons, f"{field} must reject this shape"

    harness = _FakeAppendHarness(results={"auto": _sample(**{field: value})})
    code, out = _run_telemetry(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for channel, text in (("artifact", blob), ("stdout", captured.out),
                          ("stderr", captured.err),
                          ("reasons", " ".join(reasons))):
        assert _TELEMETRY_SECRET not in text, (
            f"the secret leaked into {channel}")
        assert "<Unserializable" not in text, f"a repr leaked into {channel}"
    json.loads(blob)


def test_projected_append_cost_is_never_negative():
    """Reporting must not publish impossible numbers.

    When the early samples happen to be slower than the late ones the fitted
    slope is negative; the un-clamped projection published -60073 ms at 50k
    records on a real CI run. The raw slope stays visible so the inversion is
    still diagnosable.
    """
    r = pv.bench_sessionlog_append(n=12, fsync="auto")
    assert r["projected_ms_at_10k"] >= 0.0, r
    assert r["projected_ms_at_50k"] >= 0.0, r
    assert r["projected_ms_at_50k"] >= r["projected_ms_at_10k"], r
    assert "slope_ms_per_record" in r


# --- 1b. sessionlog.sync: the LIVE per-turn path, DETERMINISTIC contracts ----
#
# WHAT MOVED, AND WHY. This test used to assert `p50 < 60 ms` and
# `p99 < 250 ms` inside the REQUIRED suite, which runs on uncontrolled
# GitHub-hosted runners. On 2026-08-09 run 31377790256 it failed the Windows
# 3.12 leg at p99 284.429 ms, on a commit whose failing test function
# (sha256 86dcc0bdce5e69df) and benchmark (0c348439ff53962b) are byte-identical
# to its base, whose diff touched no file under olympus/, and whose local
# Windows full suite passed. Linux 3.10-3.13 passed the same commit. The series
# DECAYED (first decile 74.406 ms -> last decile 9.273 ms) where the cached path
# should be roughly flat in depth. Consistent with uncontrolled runner variance
# or an early-run transient; the precise external cause is NOT proven.
#
# The thresholds were not raised, scaled or deleted. They live unchanged in
# scripts/sessionlog_sync_telemetry.py on a scheduled/manual workflow.
#
# `sync` IS production, so deleting a timing bound without replacing it would
# be indefensible. What is required here now is strictly stronger than a clock
# and independent of host speed: routing, one record per extending turn, dense
# sequences, a verified hash chain, replayed-history equality, and exact
# scan/fsync/byte accounting including the cached-vs-uncached D1 property.

def test_save_conversation_routes_the_turn_to_sessionlog_sync(monkeypatch,
                                                              tmp_path):
    """The routing claim, asserted instead of merely documented.

    The old docstring said "`memory.save_conversation` calls `sessionlog.sync`,
    not `append_turn`" and nothing verified it. If that routing ever changed,
    every guarantee below would be measuring a path production does not use.
    """
    from olympus import config, memory, sessionlog

    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    memory.set_user("perf")
    seen = {}
    real_sync = sessionlog.sync

    def spy(conversation_id, history):
        seen["cid"] = conversation_id
        seen["history"] = [dict(m) for m in history]
        seen["calls"] = seen.get("calls", 0) + 1
        return real_sync(conversation_id, history)

    monkeypatch.setattr(sessionlog, "sync", spy)
    monkeypatch.setattr(sessionlog, "append_turn", _refuse_append_turn)

    history = [{"role": "user", "content": "one"},
               {"role": "assistant", "content": "two"}]
    with pv.env(OLYMPUS_SESSION_JOURNAL="on", OLYMPUS_SESSION_FSYNC="auto"):
        memory.save_conversation("route-probe", history)

    assert seen.get("calls") == 1, "save_conversation did not reach sync once"
    assert seen["cid"] == "route-probe", seen["cid"]
    assert seen["history"] == history, "sync received a different history"


def _refuse_append_turn(*_a, **_k):
    raise AssertionError(
        "memory.save_conversation used append_turn; the live per-turn path is "
        "sessionlog.sync and every contract below assumes it")


def test_sync_journals_every_turn_with_a_verified_chain():
    """Integrity of the live path. Host-independent; no wall-clock anywhere.

    Real syncs against a real journal — nothing about the work under test is
    mocked. `sync` captures its own exceptions and returns 0 by design, so a
    journal that silently stopped being written would produce excellent
    timings; this is what makes that impossible to report as success.
    """
    from olympus import sessionlog

    turns = 60
    r = pv.bench_sessionlog_sync(turns=turns)

    # The benchmark's own accounting.
    assert r["turns"] == turns and r["cache"] is True
    assert r["n"] == turns, r
    assert r["records_verified"] == turns, r
    assert r["journal_status"] == "ok", r
    assert r["journal_bytes"] > 0, r
    assert r["seqs_dense"] is True, r
    assert r["chain_verified"] is True, r
    assert r["replayed_history_matches"] is True, r

    # ...and an independent re-read confirms it, chain and all.
    records, status = sessionlog.read_verified(r["conversation_id"])
    assert status == "ok"
    assert len(records) == turns
    assert [rec["seq"] for rec in records] == list(range(1, turns + 1)), (
        "sequence numbers must be exactly 1..turns, dense, no gaps")
    prev = ""
    for rec in records:
        assert rec["kind"] == "turn", rec["kind"]
        assert rec["prev"] == prev, f"chain break at seq {rec['seq']}"
        assert rec["sha"] == sessionlog._seal(rec), (
            f"record {rec['seq']} does not verify against its own seal")
        prev = rec["sha"]


def test_a_swallowed_sync_failure_voids_the_measurement(monkeypatch):
    """`sync` returns 0 on failure instead of raising — that must not be fast.

    `_open_append` is the module's documented fault-injection seam. With it
    failing, `sync` captures the error and returns 0, so the benchmark sees a
    turn that journaled nothing. It must raise rather than summarise the
    samples it collected: the cheapest way to look fast is to stop working.
    """
    from olympus import sessionlog

    calls = {"n": 0}
    real_open = sessionlog._open_append

    def failing_open(path):
        calls["n"] += 1
        if calls["n"] > 5:
            raise OSError("simulated journal write fault")
        return real_open(path)

    monkeypatch.setattr(sessionlog, "_open_append", failing_open)

    with pytest.raises(RuntimeError, match="measurement void"):
        pv.bench_sessionlog_sync(turns=20)
    assert calls["n"] > 5, "the fault never fired, so this asserted nothing"


def test_an_unexpected_sync_return_is_reported_without_echoing_it(monkeypatch):
    """The void message must be constant — the returned value is caller-shaped.

    A patched or faulty `sync` can return anything, including an object whose
    `__str__`/`__repr__` carries a credential. Interpolating it would put that
    into a traceback and from there into a CI log.
    """
    from olympus import sessionlog

    secret = "sk_live_9f3a21_TAVILY_API_KEY"

    class _SecretSeq:
        def __eq__(self, other):
            return False

        def __repr__(self):
            return f"<seq {secret}>"

        def __str__(self):
            return secret

    monkeypatch.setattr(sessionlog, "sync", lambda cid, hist: _SecretSeq())

    with pytest.raises(RuntimeError) as caught:
        pv.bench_sessionlog_sync(turns=5)

    rendered = f"{caught.value!r} {caught.value}"
    assert "measurement void" in rendered
    assert secret not in rendered, "the returned value leaked into the error"
    assert "_SecretSeq" not in rendered, "the type name leaked into the error"


def test_sync_corruption_is_detected_not_silently_accepted(tmp_path,
                                                           monkeypatch):
    """A mutated journal must not verify.

    Guards the chain assertions above against being vacuous: if
    `read_verified` accepted anything, they would prove nothing.
    """
    from olympus import config, memory, sessionlog

    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    memory.set_user("perf")
    cid = "sync-corrupt-probe"
    history = []
    with pv.env(OLYMPUS_SESSION_JOURNAL="on", OLYMPUS_SESSION_FSYNC="auto"):
        for i in range(5):
            history.append({"role": "user", "content": f"q{i}"})
            assert sessionlog.sync(cid, history) == i + 1
        records, status = sessionlog.read_verified(cid)
        assert status == "ok" and len(records) == 5

        # Flip a byte inside the FIRST record — mid-file, so it cannot be
        # excused as a torn tail.
        path = sessionlog._journal_path(sessionlog._sid(cid))
        raw = path.read_bytes()
        first_nl = raw.index(b"\n")
        mutated = raw[:first_nl].replace(b'"content":"q0"',
                                         b'"content":"q9"', 1)
        assert mutated != raw[:first_nl], "the probe failed to mutate anything"
        path.write_bytes(mutated + raw[first_nl:])

        after, after_status = sessionlog.read_verified(cid)
    assert after_status != "ok" or len(after) < 5, (
        "a mid-file mutation was accepted as a verified journal")


def _sync_work(turns, *, cache, monkeypatch):
    """Run one isolated arm and return its operation/byte accounting.

    Only the module's own narrow seams are used — `_scan`, `_open_append`
    (documented as a test seam) and `os.fsync`. `Path.read_bytes` is counted
    only for the journal under test, so unrelated filesystem activity is never
    attributed to the benchmark. No production file is modified.
    """
    from pathlib import Path as _Path

    from olympus import sessionlog

    scans: list[dict] = []
    reads: list[int] = []
    writes: list[int] = []
    targets: set = set()
    fsyncs = {"n": 0}
    opens = {"n": 0}

    verification_at = {"index": None}

    real_scan = sessionlog._scan
    real_open = sessionlog._open_append
    real_fsync = sessionlog.os.fsync
    real_read_bytes = _Path.read_bytes
    real_read_verified = sessionlog.read_verified

    def marking_read_verified(cid):
        # The benchmark's own integrity read is NOT part of the sync path. Mark
        # where it starts so its scan is never charged to the 60 syncs.
        verification_at["index"] = len(scans)
        return real_read_verified(cid)

    def counting_read_bytes(self):
        data = real_read_bytes(self)
        if self in targets:
            reads.append(len(data))
        return data

    def counting_scan(sid):
        path = sessionlog._journal_path(sid)
        targets.add(path)
        size = path.stat().st_size if path.exists() else 0
        before = len(reads)
        try:
            return real_scan(sid)
        finally:
            scans.append({"journal_size_at_scan_start": size,
                          "actual_bytes_read": sum(reads[before:])})

    class _CountingHandle:
        def __init__(self, handle):
            self._handle = handle

        def write(self, data):
            writes.append(len(data))
            return self._handle.write(data)

        def __getattr__(self, name):
            return getattr(self._handle, name)

    def counting_open(path):
        opens["n"] += 1
        return _CountingHandle(real_open(path))

    def counting_fsync(fd):
        fsyncs["n"] += 1
        return real_fsync(fd)

    monkeypatch.setattr(sessionlog, "_scan", counting_scan)
    monkeypatch.setattr(sessionlog, "_open_append", counting_open)
    monkeypatch.setattr(sessionlog.os, "fsync", counting_fsync)
    monkeypatch.setattr(_Path, "read_bytes", counting_read_bytes)
    monkeypatch.setattr(sessionlog, "read_verified", marking_read_verified)

    result = pv.bench_sessionlog_sync(turns=turns, cache=cache)

    monkeypatch.undo()
    boundary = verification_at["index"]
    assert boundary is not None, (
        "the benchmark never performed its integrity read, so its record "
        "count and status are unsubstantiated")
    return {"result": result,
            "sync_scans": scans[:boundary],
            "verification_scans": scans[boundary:],
            "writes": writes, "fsyncs": fsyncs["n"], "opens": opens["n"]}


def test_sync_does_a_bounded_and_exactly_durable_amount_of_work(monkeypatch):
    """Work accounting in operations and bytes, not time.

    This is what replaces the wall-clock gate as the REQUIRED signal, and it is
    strictly stronger against the regressions a clock was meant to catch: a lost
    cache, an extra scan, a lost fsync or a second pass over the journal each
    change a count here, deterministically, on any host at any speed.

    It also expresses the D1 property — the reason the cache exists — WITHOUT a
    stopwatch: the cached arm must not rescan the journal on every turn, and
    the uncached arm must.
    """
    turns = 60

    cached = _sync_work(turns, cache=True, monkeypatch=monkeypatch)
    uncached = _sync_work(turns, cache=False, monkeypatch=monkeypatch)

    for arm, label in ((cached, "cached"), (uncached, "uncached")):
        r = arm["result"]
        assert r["records_verified"] == turns, (label, r)
        assert r["journal_status"] == "ok", (label, r)
        assert r["seqs_dense"] and r["chain_verified"], (label, r)
        assert r["replayed_history_matches"], (label, r)

        # Durability is EXACT: one extending sync must leave one durable
        # record. `fsync='auto'` still fsyncs once per append
        # (sessionlog._append_records: `if not always: os.fsync(...)`).
        assert arm["fsyncs"] == turns, (
            f"{label}: expected exactly {turns} fsyncs, got {arm['fsyncs']} — "
            f"durability per turn is a correctness property")
        # One sealed line per turn, one open per turn.
        assert len(arm["writes"]) == turns, (label, len(arm["writes"]))
        assert arm["opens"] == turns, (label, arm["opens"])
        # Bytes written must equal the journal on disk — derived from the bytes
        # actually presented to the write seam, never an assumed record size.
        assert r["journal_bytes"] == sum(arm["writes"]), (
            f"{label}: journal is {r['journal_bytes']} bytes but "
            f"{sum(arm['writes'])} were written")
        # A scan can never read more than the file held at the time.
        for i, scan in enumerate(arm["sync_scans"] + arm["verification_scans"]):
            assert (scan["actual_bytes_read"]
                    <= scan["journal_size_at_scan_start"]), (label, i, scan)
        # The integrity read really happened.
        assert len(arm["verification_scans"]) >= 1, label

    # --- the D1 property, counted rather than timed -------------------------
    # Counted over the SYNC phase only: the benchmark's own `read_verified`
    # scan is not part of the path under test and is excluded above.
    #
    # The cached arm's one scan is the cold first turn — there is no cache
    # entry to hit before the first write, which `sessionlog.sync` documents
    # ("On any miss — first turn of a process, ... — it falls through"). Turns
    # 2..60 must all hit.
    cached_scans = len(cached["sync_scans"])
    uncached_scans = len(uncached["sync_scans"])
    assert cached_scans <= 1, (
        f"the cached arm performed {cached_scans} full journal scans across "
        f"{turns} syncs; the cache exists so per-turn cost stops scaling with "
        f"depth, so only the unavoidable cold first turn may scan")
    assert uncached_scans == turns, (
        f"the uncached arm performed {uncached_scans} scans, expected exactly "
        f"{turns} — it must reproduce the pre-D1 rescan-per-turn behaviour, or "
        f"this A/B proves nothing")
    assert cached_scans < uncached_scans, (
        f"cached scans ({cached_scans}) must be strictly fewer than uncached "
        f"({uncached_scans})")
    assert (sum(s["actual_bytes_read"] for s in cached["sync_scans"])
            < sum(s["actual_bytes_read"] for s in uncached["sync_scans"])), (
        "the cached arm must also read strictly fewer journal bytes")


# --- 1c. D1 depth scaling: the wall-clock arm lives in telemetry ------------
#
# `test_sync_depth_scaling_is_sharply_reduced_but_not_eliminated` USED to live
# here and asserted three wall-clock bounds on `sessionlog.sync`:
#
#     off_slope > 5.0 us/turn
#     on_slope  < off_slope / 5.0
#     paired speedup at depth 900 > 2.0
#
# It is the same class of gate as the p50/p99 bounds already relocated: a
# shared-runner timing verdict on the live per-turn path. Its own docstring
# conceded that a scheduler stall can move the median. Push CI run 31427036314
# duly failed the Windows 3.12 leg on it — cached slope 18.320 us/turn,
# uncached 35.444, reduction 1.935x against a required 5x, and a paired
# speedup of 1.638x against a required 2x — on a branch that changed no
# production code. Leaving it behind made the earlier correction incomplete.
#
# All three bounds are preserved EXACTLY, with their original strict
# comparisons, in scripts/sessionlog_sync_telemetry.py (`DEPTH_CONTRACT`), run
# by the scheduled/manual workflow at depths=(100, 900) and samples=12.
#
# What stays REQUIRED is the deterministic half, immediately below: the paired,
# order-balanced STRUCTURE of the A/B — that the two arms are separate
# conversations, interleaved, order-balanced, and that the cache bypass is
# applied per-arm and restored. That is host-independent and is what stops the
# measurement silently becoming a sequential, order-biased comparison again.

def test_sync_depth_scaling_measures_the_two_arms_paired_and_order_balanced():
    """The A/B must not drift back to sequential, order-biased phases.

    This is the anti-regression for the methodology fix above, and it is
    deliberately STRUCTURAL rather than timing-based: it observes the actual
    `sessionlog.sync` calls the benchmark makes and the cache regime in force
    at each one. No sleeps, no wall-clock thresholds, nothing that can flake on
    a loaded runner — the properties asserted are true or false regardless of
    how fast the machine is.

    What it pins, and what each would catch:

      * the two arms are DIFFERENT conversations — one shared session would
        make the arms contend on one journal and one lock;
      * within the timed window each arm is sampled equally and no arm ever
        runs more than twice in a row — a sequential implementation produces
        one run of `samples` per arm, which fails this immediately;
      * both ON-first and OFF-first orders actually occur, balanced to within
        one — a fixed order hands the second-position warm-cache advantage to
        the same arm every time;
      * the cache bypass is applied to the OFF timed calls ONLY, and the real
        `_cache_get` is in force for every other call, including after the
        benchmark returns.
    """
    from olympus import sessionlog

    real_cache_get = sessionlog._cache_get
    real_sync = sessionlog.sync
    depth, samples = 4, 4
    calls: list[tuple[str, bool]] = []

    def spy(conversation_id, history):
        # Record the cache regime AT THE MOMENT OF THE CALL — that is the thing
        # the benchmark is supposed to be toggling per sample.
        calls.append((conversation_id,
                      sessionlog._cache_get is not real_cache_get))
        return real_sync(conversation_id, history)

    sessionlog.sync = spy
    try:
        r = pv.bench_sync_depth_scaling(depths=(depth, depth + 2),
                                        samples=samples)
    finally:
        sessionlog.sync = real_sync

    assert sessionlog._cache_get is real_cache_get, (
        "the benchmark left the cache seam patched — a later test would run "
        "against a disabled sessionlog cache")
    assert r["paired"] is True, r

    for d in (depth, depth + 2):
        arms = {"on": f"depth{d}-on", "off": f"depth{d}-off"}
        assert arms["on"] != arms["off"]
        mine = [c for c in calls if c[0] in arms.values()]
        # setup is 2 x depth syncs, measurement is 2 x samples; the timed
        # window is therefore exactly the tail.
        assert len(mine) == 2 * d + 2 * samples, (d, len(mine))
        window = mine[-2 * samples:]

        seq = ["on" if cid == arms["on"] else "off" for cid, _ in window]
        assert seq.count("on") == samples, (d, seq)
        assert seq.count("off") == samples, (d, seq)

        # No arm may run more than twice consecutively. Sequential phases give
        # a run of `samples`; a fixed ON-then-OFF order gives runs of 2 but
        # would fail the order-count check below.
        longest, run = 1, 1
        for prev, cur in zip(seq, seq[1:]):
            run = run + 1 if cur == prev else 1
            longest = max(longest, run)
        assert longest <= 2, (
            f"depth {d}: the arms are measured in blocks of {longest}, not "
            f"interleaved — this is the sequential, order-biased A/B the "
            f"paired design replaced: {seq}")

        # Order alternates: the first element of each pair must not always be
        # the same arm.
        firsts = seq[0::2]
        assert firsts.count("on") > 0 and firsts.count("off") > 0, (
            f"depth {d}: only {set(firsts)} ever ran first, so one arm always "
            f"pays first-position cost: {seq}")
        assert abs(firsts.count("on") - firsts.count("off")) <= 1, (
            f"depth {d}: measurement order is unbalanced: {seq}")

        # The cache bypass is per-sample and per-arm: every OFF timed call ran
        # with `_cache_get` replaced, every ON timed call did not, and no setup
        # call did either.
        for cid, bypassed in window:
            assert bypassed == (cid == arms["off"]), (d, cid, bypassed)
        assert not any(bypassed for _cid, bypassed in mine[:-2 * samples]), (
            f"depth {d}: setup syncs ran with the cache bypassed; setup is "
            f"untimed and must not be part of the A/B")

    # The reported metadata must agree with what actually happened.
    orders = r["order_counts_at_max_depth"]
    assert orders["on_first"] + orders["off_first"] == samples, orders
    assert orders["on_first"] > 0 and orders["off_first"] > 0, orders
    assert abs(orders["on_first"] - orders["off_first"]) <= 1, orders
    assert r["paired_samples_at_max_depth"] == samples, r
    deep = r["per_depth"][depth + 2]
    assert deep["on_samples"] == deep["off_samples"] == samples, deep


def test_journal_recovery_scales_linearly_and_stays_bounded():
    """Reference: 17 us/record; 90.8 ms at 5000 records."""
    rows = pv.bench_journal_recovery(sizes=(100, 500), repeats=3)
    small, large = rows[0], rows[1]
    assert small["records"] == 100 and large["records"] == 500
    # a full verified scan is inherently linear — a deeper journal costs more
    assert large["p50_ms"] > small["p50_ms"]
    for row in rows:
        assert row["us_per_record"] < 400.0, f"recovery regressed: {row}"
    assert large["p50_ms"] < 2000.0, f"recovery at 500 records too slow: {large}"


# --- 2. observability non-interference cost ---------------------------------
#
# WHAT CHANGED, AND WHY. CI run 31544926530 failed this contract at
# abs_overhead_ms 35.877 with 16.335 ms of reported noise and a "metrics"
# attribution of -84.879 ms inside 125.967 ms of noise — numbers no stable
# overhead could produce. The benchmark's `_interleaved` helper alternated
# arms run-for-run but executed every pair in a FIXED A->B order, so
# chronological runner drift landed systematically on the second position and
# was reported as instrumentation cost; the test then read six pairs' raw
# median as conclusive even though the 25 ms boundary sat inside the
# measurement's own noise interval.
#
# The 25.0 ms limit is UNCHANGED. What changed is the evidence: pairs are now
# counterbalanced (A->B, B->A, ...) over three independent batches, and the
# verdict is TRI-STATE on the uncertainty band [max(0, delta - noise),
# max(0, delta + noise)]:
#
#   * BREACH — the lower edge reaches 25.0 ms: the evidence supports the
#     limit even after subtracting its own noise;
#   * PASS — only when the UPPER edge is strictly below 25.0 ms: the whole
#     band clears the limit;
#   * INCONCLUSIVE, and FAILING — everything in between, including exact
#     boundary equality, plus any measurement whose own noise reaches
#     25.0 ms. A band that crosses the threshold proves nothing in either
#     direction, and a result that proves nothing is never treated as
#     evidence of health.
#
# Counterbalancing removes systematic first/second-position bias and robust
# pairing resists isolated stalls, but hosted-runner noise is still possible;
# the lower edge is a conservative breach test, not proof that the raw
# overhead is below 25 ms.

_OBS_PAIRS = 18
_OBS_BATCHES = 3
_OBS_LIMIT_MS = 25.0        # the preserved contract limit — not softened
_OBS_COMPONENTS = {"usage", "ctx", "otel", "metrics", "guard"}


def _obs_finite(value) -> bool:
    """A finite built-in number: bools and non-numerics are not."""
    return (type(value) in (int, float)
            and math.isfinite(float(value)))


def observability_gate_violations(r, *, pairs=_OBS_PAIRS,
                                  batches=_OBS_BATCHES,
                                  limit_ms=_OBS_LIMIT_MS) -> list[str]:
    """Tri-state, uncertainty-aware verdict on one observability measurement.
    Pure, so the deterministic regressions below can drive it synthetically.

    Fails when the structure is incomplete or unbalanced, a count is wrong,
    a value is non-finite or impossibly negative, the noise reaches the
    limit (inconclusive), the lower edge of the uncertainty band reaches the
    limit (breach), or the band crosses the limit at all (inconclusive — a
    result that proves nothing is failed, never passed). A result passes
    ONLY when its entire band [lower edge, upper edge] sits strictly below
    the limit. Raw overhead, noise, both edges, and the threshold appear in
    every verdict message.
    """
    out: list[str] = []
    if not isinstance(r, dict):
        return ["result is not a mapping"]

    for key in ("off_p50_ms", "on_p50_ms", "abs_overhead_ms", "noise_ms",
                "overhead_pct", "overhead_lower_bound_ms",
                "overhead_upper_bound_ms", "position_effect_ms"):
        if not _obs_finite(r.get(key)):
            out.append(f"{key} is not a finite built-in number")
    for key in ("iters", "paired_samples", "batches",
                "counterbalanced_blocks"):
        if type(r.get(key)) is not int or r.get(key) <= 0:
            out.append(f"{key} is not a positive built-in int")
    if type(r.get("significant")) is not bool:
        out.append("significant is not a bool")
    if r.get("order_balanced") is not True:
        out.append("order_balanced is not True")

    counts = r.get("order_counts")
    if not isinstance(counts, dict) or set(counts) != {"a_first", "b_first"} \
            or any(type(counts[k]) is not int or counts[k] < 0
                   for k in ("a_first", "b_first")):
        out.append("order_counts is malformed")
    rows = r.get("batch_summaries")
    if not isinstance(rows, list) or len(rows) != batches:
        out.append(f"batch_summaries does not hold exactly {batches} batches")
        rows = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            out.append(f"batch {i} is not a mapping")
            continue
        for key in ("pairs", "a_first", "b_first", "counterbalanced_blocks"):
            if type(row.get(key)) is not int or row.get(key) < 0:
                out.append(f"batch {i} {key} is not a valid count")
        for key in ("delta_ms", "noise_ms", "position_effect_ms"):
            if not _obs_finite(row.get(key)):
                out.append(f"batch {i} {key} is not finite")
    if out:
        return out              # the cross-checks below assume valid scalars

    if r["paired_samples"] != pairs or r["iters"] != pairs:
        out.append(f"expected {pairs} measured pairs, got paired_samples="
                   f"{r['paired_samples']} iters={r['iters']}")
    if r["batches"] != batches:
        out.append(f"expected {batches} batches, got {r['batches']}")
    if r["counterbalanced_blocks"] != pairs // 2:
        out.append(f"counterbalanced_blocks does not equal pairs // 2 "
                   f"({pairs // 2})")
    if sum(row["counterbalanced_blocks"] for row in rows) \
            != r["counterbalanced_blocks"]:
        out.append("batch block counts do not sum to the total block count")
    a_first, b_first = counts["a_first"], counts["b_first"]
    if a_first + b_first != pairs:
        out.append("order counts do not sum to the pair count")
    if a_first != b_first:
        out.append(f"execution order is unbalanced: {a_first} A-first vs "
                   f"{b_first} B-first")
    if sum(row["pairs"] for row in rows) != pairs:
        out.append("batch pair counts do not sum to the total")
    for i, row in enumerate(rows):
        if row["pairs"] < 2:
            out.append(f"batch {i} holds no measured pairs — an empty batch "
                       f"is a batch that never ran")
        if row["pairs"] % 2:
            out.append(f"batch {i} holds an odd number of pairs, which "
                       f"cannot balance A-first against B-first")
        if row["a_first"] + row["b_first"] != row["pairs"]:
            out.append(f"batch {i} order counts do not sum to its pairs")
        if row["a_first"] != row["b_first"]:
            out.append(f"batch {i} is order-unbalanced")
        if row["counterbalanced_blocks"] != row["pairs"] // 2:
            out.append(f"batch {i} block count does not equal its own "
                       f"pairs // 2")
        if row["noise_ms"] < 0.0:
            out.append(f"batch {i} reports negative noise, which no real "
                       f"measurement produces")
    if not r["off_p50_ms"] > 0.0 or not r["on_p50_ms"] > 0.0:
        out.append("arm medians are not positive — nothing was measured")

    if r["noise_ms"] < 0.0:
        out.append("noise_ms is negative, which no real measurement produces")
    if r["overhead_lower_bound_ms"] < 0.0 \
            or r["overhead_upper_bound_ms"] < 0.0:
        out.append("a band edge is negative, which max(0, ...) cannot "
                   "produce")
    # Both band edges must be DERIVED from the raw evidence, not asserted.
    expected_lower = max(0.0, r["abs_overhead_ms"] - r["noise_ms"])
    if abs(r["overhead_lower_bound_ms"] - expected_lower) > 1e-9:
        out.append("overhead_lower_bound_ms does not equal "
                   "max(0, abs_overhead_ms - noise_ms)")
    expected_upper = max(0.0, r["abs_overhead_ms"] + r["noise_ms"])
    if abs(r["overhead_upper_bound_ms"] - expected_upper) > 1e-9:
        out.append("overhead_upper_bound_ms does not equal "
                   "max(0, abs_overhead_ms + noise_ms)")
    # Derived verdict fields must AGREE with the evidence they claim to
    # summarise — a well-typed but contradictory flag is forged evidence.
    if r["significant"] is not (abs(r["abs_overhead_ms"]) > r["noise_ms"]):
        out.append("significant contradicts its own delta and noise")
    if r["off_p50_ms"] > 0.0:
        expected_pct = r["abs_overhead_ms"] / r["off_p50_ms"] * 100.0
        if not math.isclose(r["overhead_pct"], expected_pct,
                            rel_tol=1e-9, abs_tol=1e-9):
            out.append("overhead_pct does not equal "
                       "abs_overhead_ms / off_p50_ms * 100")

    comps = r.get("components_ms")
    if not isinstance(comps, dict) or set(comps) != _OBS_COMPONENTS:
        out.append("components_ms does not cover every component")
    else:
        for name, comp in sorted(comps.items()):
            if not isinstance(comp, dict):
                out.append(f"component {name} is not a mapping")
                continue
            numeric_ok = all(_obs_finite(comp.get(key))
                             for key in ("ms", "noise_ms", "lower_bound_ms",
                                         "upper_bound_ms",
                                         "position_effect_ms"))
            if not numeric_ok:
                out.append(f"component {name} carries a non-finite value")
            else:
                if comp["noise_ms"] < 0.0:
                    out.append(f"component {name} reports negative noise")
                if comp["lower_bound_ms"] < 0.0 \
                        or comp["upper_bound_ms"] < 0.0:
                    out.append(f"component {name} carries a negative band "
                               f"edge")
                if abs(comp["lower_bound_ms"]
                       - max(0.0, comp["ms"] - comp["noise_ms"])) > 1e-9:
                    out.append(f"component {name} lower bound does not match "
                               f"its own raw evidence")
                if abs(comp["upper_bound_ms"]
                       - max(0.0, comp["ms"] + comp["noise_ms"])) > 1e-9:
                    out.append(f"component {name} upper bound does not match "
                               f"its own raw evidence")
            if type(comp.get("resolved")) is not bool:
                out.append(f"component {name} resolved is not a bool")
            elif numeric_ok and comp["resolved"] is not (
                    abs(comp["ms"]) > comp["noise_ms"]):
                out.append(f"component {name} resolved contradicts its own "
                           f"delta and noise")
            # Exact built-in ints: 18.0 compares equal to 18 and must still
            # be rejected — a float count is not a count.
            if type(comp.get("paired_samples")) is not int \
                    or comp["paired_samples"] != pairs:
                out.append(f"component {name} did not measure exactly "
                           f"{pairs} pairs as a built-in int")
            if type(comp.get("batches")) is not int \
                    or comp["batches"] != batches:
                out.append(f"component {name} did not use exactly {batches} "
                           f"batches as a built-in int")
            if type(comp.get("counterbalanced_blocks")) is not int \
                    or comp["counterbalanced_blocks"] != pairs // 2:
                out.append(f"component {name} block count is not exactly "
                           f"pairs // 2 as a built-in int")
            if comp.get("order_balanced") is not True:
                out.append(f"component {name} is order-unbalanced")
            ccounts = comp.get("order_counts")
            if not isinstance(ccounts, dict) \
                    or set(ccounts) != {"a_first", "b_first"} \
                    or any(type(ccounts.get(k)) is not int
                           for k in ("a_first", "b_first")) \
                    or ccounts["a_first"] + ccounts["b_first"] != pairs \
                    or ccounts["a_first"] != ccounts["b_first"]:
                out.append(f"component {name} order accounting is malformed "
                           f"or unbalanced")

    # --- the preserved 25 ms contract: a TRI-STATE verdict on the band -----
    # BREACH when the lower edge reaches the limit; PASS only when the upper
    # edge is strictly below it; everything in between — including exact
    # boundary equality — is INCONCLUSIVE and FAILS, because a band that
    # crosses the threshold proves nothing in either direction.
    raw = r["abs_overhead_ms"]
    noise = r["noise_ms"]
    lower = r["overhead_lower_bound_ms"]
    upper = r["overhead_upper_bound_ms"]
    if lower >= limit_ms:
        out.append(
            f"BREACH: overhead lower edge {lower:.3f} ms >= {limit_ms} ms — "
            f"the evidence supports at least the limit even after "
            f"subtracting its own noise (raw abs_overhead_ms={raw:.3f} ms, "
            f"noise_ms={noise:.3f} ms, upper edge {upper:.3f} ms)")
    elif noise >= limit_ms:
        out.append(
            f"INCONCLUSIVE: measurement noise {noise:.3f} ms reaches the "
            f"{limit_ms} ms limit, so this run can prove nothing either way "
            f"(raw abs_overhead_ms={raw:.3f} ms, lower edge {lower:.3f} ms, "
            f"upper edge {upper:.3f} ms)")
    elif not upper < limit_ms:
        out.append(
            f"INCONCLUSIVE: the uncertainty band crosses the {limit_ms} ms "
            f"limit — raw abs_overhead_ms={raw:.3f} ms, noise_ms="
            f"{noise:.3f} ms, lower edge {lower:.3f} ms, upper edge "
            f"{upper:.3f} ms — this run proves neither a pass nor a breach, "
            f"and a result that proves nothing is failed, not trusted")
    return out


def _format_obs_evidence(r: dict, limit_ms: float) -> str:
    """The measurement, as a block for the CI log.

    Printed on PASS as well as failure (W1-1b). This contract was green on
    `main` for seven consecutive runs while its margin eroded invisibly, and
    W1-1's fix reported "passed" without saying by how much. A perf contract
    that shows its numbers only once it is already breached cannot show drift
    approaching the limit — and it cannot corroborate a claim that some
    component was deliberately left un-fsynced.
    """
    head = max(0.0, r["abs_overhead_ms"] - r["noise_ms"])
    tail = max(0.0, r["abs_overhead_ms"] + r["noise_ms"])
    lines = [
        "",
        "observability overhead (limit %.1f ms)" % limit_ms,
        "  off_p50_ms       %9.3f" % r["off_p50_ms"],
        "  on_p50_ms        %9.3f" % r["on_p50_ms"],
        "  abs_overhead_ms  %9.3f" % r["abs_overhead_ms"],
        "  noise_ms         %9.3f" % r["noise_ms"],
        "  band             [%.3f, %.3f]  headroom %.3f ms" % (
            head, tail, limit_ms - tail),
    ]
    comps = r.get("components_ms") or {}
    if comps:
        lines.append("  components_ms")
        for name, c in sorted(comps.items(),
                              key=lambda kv: -(kv[1].get("ms", 0.0)
                                               if isinstance(kv[1], dict)
                                               else kv[1])):
            if isinstance(c, dict):
                lines.append(
                    "    %-9s %8.3f  +/- %6.3f  %s" % (
                        name, c.get("ms", 0.0), c.get("noise_ms", 0.0),
                        "resolved" if c.get("resolved") else "NOT resolved"))
            else:
                lines.append("    %-9s %8.3f" % (name, c))
    return "\n".join(lines)


def test_observability_overhead_absolute_cost_bounded(capsys):
    """Reference: +2.10 ms/run absolute on a fake pipeline (+100.9% of a
    denominator containing ~0 ms of provider time). The assertion is on the
    ABSOLUTE cost, which is the portable number.

    The verdict is the tri-state gate above: 18 counterbalanced pairs over 3
    independent batches, judged against the unchanged 25.0 ms limit. It
    passes only when the entire uncertainty band sits strictly below the
    limit; a band that reaches or crosses it is inconclusive and fails, and
    a lower edge at or above it is a breach. The failure message keeps the
    raw overhead, noise, and both band edges visible.

    The same numbers are printed on PASS — see `_format_obs_evidence`.
    """
    r = pv.bench_observability_overhead(iters=_OBS_PAIRS,
                                        batches=_OBS_BATCHES)
    evidence = _format_obs_evidence(r, _OBS_LIMIT_MS)
    # `capsys.disabled()` so it reaches the CI log under a plain `pytest -q`,
    # which otherwise shows captured stdout only for FAILING tests.
    with capsys.disabled():
        print(evidence)
    violations = observability_gate_violations(r)
    assert violations == [], (
        f"observability wall-clock contract violated: {violations}\n"
        f"raw abs_overhead_ms={r['abs_overhead_ms']} "
        f"noise_ms={r['noise_ms']} "
        f"overhead_lower_bound_ms={r['overhead_lower_bound_ms']} "
        f"overhead_upper_bound_ms={r['overhead_upper_bound_ms']}\n"
        f"{evidence}\n"
        f"full result: {r}")


# --- 2a. deterministic regressions for the counterbalanced primitive ---------
#
# Driven by fake runners over a fake clock, so nothing here depends on actual
# wall-clock scheduling. The fakes price each MEASURED run through a
# `cost_ms(pair_index, position)` callable — position 0 is the run executed
# first within its pair — which is exactly the knob needed to prove that
# position bias no longer becomes arm bias.

class _FakeClock:
    """Deterministic monotonic clock; advances only when told."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeObsArm:
    """One logical arm for `counterbalanced_ab`: records every call.

    The first run() after each make() is the batch warmup (the primitive
    warms immediately after constructing fresh runners). Measured runs
    append (arm, pair_index, position) to the shared `trace`; every run
    appends (arm, kind) to the shared `chronology`.
    """

    def __init__(self, name, clock, trace, chronology, cost_ms,
                 raise_on_call=None):
        self.name = name
        self.clock = clock
        self.trace = trace
        self.chronology = chronology
        self.cost_ms = cost_ms
        self.raise_on_call = raise_on_call
        self.run_calls = 0
        self.warmups = 0
        self.enters = 0
        self.leaves = 0
        self._fresh = False

    def make(self):
        self._fresh = True

        def enter():
            self.enters += 1

        def run():
            self.run_calls += 1
            if self.raise_on_call is not None \
                    and self.run_calls == self.raise_on_call:
                raise RuntimeError("simulated arm fault")
            if self._fresh:
                self._fresh = False
                self.warmups += 1
                self.chronology.append((self.name, "warm"))
                self.clock.advance(0.001)
                return
            position = len(self.trace) % 2
            pair_index = len(self.trace) // 2
            self.trace.append((self.name, pair_index, position))
            self.chronology.append((self.name, "measured"))
            self.clock.advance(self.cost_ms(pair_index, position) / 1000.0)

        def leave():
            self.leaves += 1

        return enter, run, leave


def _fake_counterbalanced(cost_a, cost_b, *, pairs=_OBS_PAIRS,
                          batches=_OBS_BATCHES):
    clock = _FakeClock()
    trace: list = []
    chronology: list = []
    a = _FakeObsArm("A", clock, trace, chronology, cost_a)
    b = _FakeObsArm("B", clock, trace, chronology, cost_b)
    result = pv.counterbalanced_ab(a.make, b.make, pairs=pairs,
                                   batches=batches, tag="fake-obs",
                                   clock=clock)
    return result, a, b, trace, chronology


def test_counterbalanced_orders_are_exactly_balanced_at_18_pairs():
    """18 pairs / 3 batches: exactly 9 A-first and 9 B-first, 3/3 per batch."""
    result, _a, _b, trace, _chron = _fake_counterbalanced(
        lambda pair, pos: 1.0, lambda pair, pos: 1.0)

    assert result["paired_samples"] == 18
    assert result["batches"] == 3
    assert result["order_counts"] == {"a_first": 9, "b_first": 9}
    assert result["order_balanced"] is True
    assert [row["pairs"] for row in result["batch_summaries"]] == [6, 6, 6]
    for row in result["batch_summaries"]:
        assert row["a_first"] == 3 and row["b_first"] == 3

    # Ground truth from the execution trace, not from the metadata.
    a_first_pairs = {p for arm, p, pos in trace if arm == "A" and pos == 0}
    b_first_pairs = {p for arm, p, pos in trace if arm == "B" and pos == 0}
    assert a_first_pairs == {0, 2, 4, 6, 8, 10, 12, 14, 16}
    assert b_first_pairs == {1, 3, 5, 7, 9, 11, 13, 15, 17}


def test_each_arm_runs_exactly_18_measured_times_excluding_warmups():
    _result, a, b, trace, chronology = _fake_counterbalanced(
        lambda pair, pos: 1.0, lambda pair, pos: 1.0)

    for arm in (a, b):
        assert arm.warmups == 3, "one warmup per batch per arm"
        assert arm.run_calls - arm.warmups == 18, (
            "each arm must contribute exactly one measured run per pair")
        assert arm.enters == arm.leaves == arm.run_calls, (
            "every run must be bracketed by its own enter/leave")
    assert len(trace) == 36

    # The warmup STARTING order alternates between batches: each batch is
    # 2 warmups + 12 measured runs = 14 chronology entries.
    assert chronology[0] == ("A", "warm") and chronology[1] == ("B", "warm")
    assert chronology[14] == ("B", "warm") and chronology[15] == ("A", "warm")
    assert chronology[28] == ("A", "warm") and chronology[29] == ("B", "warm")


def test_pairs_stay_matched_when_execution_order_reverses():
    """Samples are stored by LOGICAL arm and every delta is B minus A.

    Arm costs vary per pair (so a positional mix-up could not cancel out),
    with B always exactly 5 ms above A. If B-first pairs were paired by
    execution position instead of by logical arm, their deltas would come
    out as -5; the paired median and every batch median must be exactly +5.
    """
    result, _a, _b, _trace, _chron = _fake_counterbalanced(
        lambda pair, pos: 1.0 + pair, lambda pair, pos: 6.0 + pair)

    assert result["paired_delta_ms"] == pytest.approx(5.0, abs=1e-9)
    assert result["noise_ms"] == pytest.approx(0.0, abs=1e-6)
    for row in result["batch_summaries"]:
        assert row["delta_ms"] == pytest.approx(5.0, abs=1e-9)
    assert result["lower_bound_ms"] == pytest.approx(5.0, abs=1e-6)


def _positional_cost(base):
    """Cost model: `base` ms plus a +30 ms penalty on the SECOND run of
    each executed pair — the CI failure mode, deterministically."""
    return lambda pair, pos: base + (30.0 if pos == 1 else 0.0)


def _executed_pair_deltas(trace, cost_a, cost_b, pairs):
    """Recompute every raw pair delta (B - A) from the EXECUTED positions in
    the trace, so a test's claim about the raw deltas is grounded in what
    actually ran rather than in the model that generated it."""
    deltas = []
    for pair in range(pairs):
        pos_a = next(pos for arm, p, pos in trace
                     if arm == "A" and p == pair)
        pos_b = next(pos for arm, p, pos in trace
                     if arm == "B" and p == pair)
        deltas.append(cost_b(pair, pos_b) - cost_a(pair, pos_a))
    return deltas


def test_second_position_slowdown_is_not_reported_as_b_overhead():
    """The CI failure mode, reproduced deterministically — and now PASSING.

    Both arms cost the same; whichever run executes SECOND in its pair pays
    +30 ms — pure position bias, zero real overhead. Under the old fixed
    A->B order B was always second, so this measured as a rock-steady
    +30 ms B overhead with near-zero noise: a proven false breach.

    Counterbalancing exists to ESTIMATE AND REMOVE exactly this controlled
    effect. The raw pair deltas alternate +30, -30; each two-pair block
    averages one of each, so every block treatment effect is 0 and the ±30
    swing resolves into `position_effect_ms = 30` — measured, cancelled,
    and published, not laundered into noise. The result is delta 0 with
    ZERO residual noise, and the gate PASSES: a stable additive order
    effect is not uncertainty about the overhead.
    """
    cost = _positional_cost(1.0)
    result, _a, _b, trace, _chron = _fake_counterbalanced(cost, cost)

    raw = _executed_pair_deltas(trace, cost, cost, 18)
    assert raw == [30.0 if p % 2 == 0 else -30.0 for p in range(18)], (
        "the raw pair deltas must alternate +30, -30")

    assert result["counterbalanced_blocks"] == 9
    assert result["paired_delta_ms"] == pytest.approx(0.0, abs=1e-9)
    assert result["noise_ms"] == pytest.approx(0.0, abs=1e-9)
    assert result["position_effect_ms"] == pytest.approx(30.0, abs=1e-9)
    assert result["lower_bound_ms"] == pytest.approx(0.0, abs=1e-9)
    assert result["upper_bound_ms"] == pytest.approx(0.0, abs=1e-9)
    assert result["order_counts"] == {"a_first": 9, "b_first": 9}
    for row in result["batch_summaries"]:
        assert row["position_effect_ms"] == pytest.approx(30.0, abs=1e-9)
        assert row["delta_ms"] == pytest.approx(0.0, abs=1e-9)

    violations = observability_gate_violations(_obs_result(
        abs_overhead_ms=result["paired_delta_ms"],
        noise_ms=result["noise_ms"],
        overhead_lower_bound_ms=result["lower_bound_ms"],
        overhead_upper_bound_ms=result["upper_bound_ms"],
        position_effect_ms=result["position_effect_ms"]))
    assert violations == [], (
        "a measured-and-cancelled order effect with zero residual noise "
        "is a legitimate pass, not a failure: " + repr(violations))


def test_a_real_overhead_riding_a_position_effect_is_not_hidden():
    """Genuine +40 ms B overhead PLUS the same +30 ms second-position bias.

    The raw pair deltas alternate +70 (A-first) and +10 (B-first). Every
    two-pair block averages one of each: block treatment effects are all
    exactly +40, the position effect resolves to +30, and the residual
    noise is ZERO — so the lower edge is 40 and the gate declares a proven
    BREACH. This must never pass, and it must not soften into
    INCONCLUSIVE: the order effect was controlled and cancelled, so it
    cannot launder a real regression into uncertainty.
    """
    cost_a = _positional_cost(1.0)
    cost_b = _positional_cost(41.0)
    result, _a, _b, trace, _chron = _fake_counterbalanced(cost_a, cost_b)

    raw = _executed_pair_deltas(trace, cost_a, cost_b, 18)
    assert raw == [70.0 if p % 2 == 0 else 10.0 for p in range(18)], (
        "the raw pair deltas must alternate +70, +10")

    assert result["counterbalanced_blocks"] == 9
    assert result["paired_delta_ms"] == pytest.approx(40.0, abs=1e-9)
    assert result["noise_ms"] == pytest.approx(0.0, abs=1e-9)
    assert result["position_effect_ms"] == pytest.approx(30.0, abs=1e-9)
    assert result["lower_bound_ms"] == pytest.approx(40.0, abs=1e-9)

    violations = observability_gate_violations(_obs_result(
        abs_overhead_ms=result["paired_delta_ms"],
        noise_ms=result["noise_ms"],
        overhead_lower_bound_ms=result["lower_bound_ms"],
        overhead_upper_bound_ms=result["upper_bound_ms"],
        position_effect_ms=result["position_effect_ms"]))
    assert any("BREACH" in v for v in violations), violations
    assert all("INCONCLUSIVE" not in v for v in violations), (
        "a cancelled order effect must not soften a real breach into "
        "inconclusive: " + repr(violations))


def test_a_block_local_position_drift_does_not_inflate_noise():
    """The position penalty CHANGES between blocks but is stable inside
    each block: pen(k) = 5 + 7k for block k, on top of a genuine +10 ms B
    overhead. Every block still cancels its own penalty exactly, so the
    treatment estimate stays 10 ms with ZERO residual noise; the drifting
    controlled effect lands in the position metadata (median 33, per-batch
    medians 12/33/54), not in the uncertainty band. The gate passes."""
    def pen(pair):
        return 5.0 + 7.0 * (pair // 2)

    def cost_a(pair, pos):
        return 1.0 + (pen(pair) if pos == 1 else 0.0)

    def cost_b(pair, pos):
        return 11.0 + (pen(pair) if pos == 1 else 0.0)

    result, _a, _b, _trace, _chron = _fake_counterbalanced(cost_a, cost_b)

    assert result["paired_delta_ms"] == pytest.approx(10.0, abs=1e-9), (
        "the treatment estimate must remain the known true overhead")
    assert result["noise_ms"] == pytest.approx(0.0, abs=1e-9), (
        "a controlled, locally-paired position effect is not residual noise")
    assert result["position_effect_ms"] == pytest.approx(33.0, abs=1e-9)
    assert [row["position_effect_ms"] for row in result["batch_summaries"]] \
        == [pytest.approx(v, abs=1e-9) for v in (12.0, 33.0, 54.0)]

    violations = observability_gate_violations(_obs_result(
        abs_overhead_ms=result["paired_delta_ms"],
        noise_ms=result["noise_ms"],
        overhead_lower_bound_ms=result["lower_bound_ms"],
        overhead_upper_bound_ms=result["upper_bound_ms"],
        position_effect_ms=result["position_effect_ms"]))
    assert violations == [], violations


def test_one_extreme_outlier_cannot_fake_a_breach():
    """A single 1000 ms stall in one B run poisons ONE two-pair block (its
    treatment estimate jumps to 500 ms), but eight of nine blocks still say
    zero — the block median stays at zero and cannot be dragged across the
    25 ms threshold by one stall."""
    result, _a, _b, _trace, _chron = _fake_counterbalanced(
        lambda pair, pos: 1.0,
        lambda pair, pos: 1.0 + (1000.0 if pair == 7 else 0.0))

    assert result["counterbalanced_blocks"] == 9
    assert result["paired_delta_ms"] == pytest.approx(0.0, abs=1e-6)
    assert result["lower_bound_ms"] < 1e-6
    assert observability_gate_violations(_obs_result(
        abs_overhead_ms=result["paired_delta_ms"],
        noise_ms=result["noise_ms"],
        overhead_lower_bound_ms=result["lower_bound_ms"],
        overhead_upper_bound_ms=result["upper_bound_ms"],
        position_effect_ms=result["position_effect_ms"])) == []


def test_a_raw_estimate_above_the_limit_inside_its_noise_fails_inconclusive():
    """GENUINE stochastic variation in the treatment itself stays noise.

    The per-pair deltas ramp 22..39 ms, so the block treatment effects ramp
    22.5, 24.5, ... 38.5 — real run-to-run variation in the estimate, not a
    controlled order effect (the position effect is a constant -0.5 ms and
    is published, not subtracted from the noise). The MAD of the block
    treatments is 4.0, noise ~5.930 ms, band ~[24.570, 36.430] ms: it
    CROSSES the unchanged 25.0 ms threshold, proving neither a breach nor
    health. It cannot be declared a proven breach — and it must NOT pass
    either: it fails as INCONCLUSIVE, with the raw overhead, noise, both
    edges, and the threshold in the message.
    """
    result, _a, _b, _trace, _chron = _fake_counterbalanced(
        lambda pair, pos: 1.0,
        lambda pair, pos: 23.0 + pair)          # paired deltas 22..39 ms

    assert result["paired_delta_ms"] == pytest.approx(30.5, abs=1e-6)
    assert result["noise_ms"] == pytest.approx(1.4826 * 4.0, abs=1e-6), (
        "the MAD must reflect variation of the treatment estimates")
    assert result["position_effect_ms"] == pytest.approx(-0.5, abs=1e-9)
    assert result["lower_bound_ms"] < _OBS_LIMIT_MS \
        < result["upper_bound_ms"]

    r = _obs_result(abs_overhead_ms=result["paired_delta_ms"],
                    noise_ms=result["noise_ms"],
                    overhead_lower_bound_ms=result["lower_bound_ms"],
                    overhead_upper_bound_ms=result["upper_bound_ms"],
                    position_effect_ms=result["position_effect_ms"])
    violations = observability_gate_violations(r)
    assert any("INCONCLUSIVE" in v for v in violations), violations
    assert all("BREACH" not in v for v in violations), (
        "a band crossing the limit is not a proven breach")
    message = next(v for v in violations if "INCONCLUSIVE" in v)
    for needle in ("30.500", "5.930", "24.570", "36.430", "25.0"):
        assert needle in message, (needle, message)


def test_noise_at_or_above_the_limit_is_inconclusive_not_passing():
    """A measurement that cannot resolve 25 ms proves nothing — fail.

    Block treatments ramp 4..132 ms: median 68, MAD 32, noise ~47.4 ms with
    a lower edge ~20.6 ms — below the limit, so the noise safeguard (not
    the breach rule) is what fails this run.
    """
    result, _a, _b, _trace, _chron = _fake_counterbalanced(
        lambda pair, pos: 1.0,
        lambda pair, pos: 1.0 + 8.0 * pair)     # paired deltas 0..136 ms

    assert result["noise_ms"] >= _OBS_LIMIT_MS
    assert result["lower_bound_ms"] < _OBS_LIMIT_MS
    violations = observability_gate_violations(_obs_result(
        abs_overhead_ms=result["paired_delta_ms"],
        noise_ms=result["noise_ms"],
        overhead_lower_bound_ms=result["lower_bound_ms"],
        overhead_upper_bound_ms=result["upper_bound_ms"]))
    assert any("INCONCLUSIVE" in v for v in violations), violations

    # Exactly on the limit is still inconclusive.
    exactly = observability_gate_violations(_obs_result(
        abs_overhead_ms=10.0, noise_ms=25.0, overhead_lower_bound_ms=0.0,
        overhead_upper_bound_ms=35.0))
    assert any("INCONCLUSIVE" in v for v in exactly), exactly


def test_non_divisible_pairs_distribute_as_complete_two_pair_units():
    """20 pairs / 3 batches is 8+6+6 — never 7+7+6.

    An odd-sized batch cannot hold equal A-first and B-first counts, so the
    remainder over the even base moves in whole two-pair units and every
    batch stays INDEPENDENTLY balanced: 4/4, 3/3, 3/3. The requested total
    and batch count are preserved exactly.
    """
    result, a, b, _trace, _chron = _fake_counterbalanced(
        lambda pair, pos: 1.0, lambda pair, pos: 1.0, pairs=20, batches=3)

    assert [row["pairs"] for row in result["batch_summaries"]] == [8, 6, 6]
    assert [(row["a_first"], row["b_first"])
            for row in result["batch_summaries"]] == [(4, 4), (3, 3), (3, 3)]
    assert result["paired_samples"] == 20
    assert result["batches"] == 3
    assert result["order_counts"] == {"a_first": 10, "b_first": 10}
    assert result["order_balanced"] is True
    for arm in (a, b):
        assert arm.run_calls - arm.warmups == 20


@pytest.mark.parametrize("pairs, batches, sizes", [
    (18, 3, [6, 6, 6]),
    (20, 3, [8, 6, 6]),
    (20, 4, [6, 6, 4, 4]),
    (10, 3, [4, 4, 2]),
    (14, 4, [4, 4, 4, 2]),
    (12, 5, [4, 2, 2, 2, 2]),
    (8, 3, [4, 2, 2]),
    (24, 4, [6, 6, 6, 6]),
    (6, 3, [2, 2, 2]),
    (4, 1, [4]),
    (2, 1, [2]),
])
def test_valid_distributions_are_even_balanced_and_complete(pairs, batches,
                                                            sizes):
    """Every accepted batch size is even, every batch is exactly balanced,
    and every requested pair is measured exactly once per arm."""
    result, a, b, trace, _chron = _fake_counterbalanced(
        lambda pair, pos: 1.0, lambda pair, pos: 1.0,
        pairs=pairs, batches=batches)

    assert [row["pairs"] for row in result["batch_summaries"]] == sizes
    assert sum(sizes) == pairs
    for row in result["batch_summaries"]:
        assert row["pairs"] % 2 == 0
        assert row["a_first"] == row["b_first"] == row["pairs"] // 2
    assert result["paired_samples"] == pairs
    assert result["order_counts"] == {"a_first": pairs // 2,
                                      "b_first": pairs // 2}
    assert result["order_balanced"] is True
    # No sample dropped, none duplicated: every pair index appears exactly
    # once per arm in the measured trace.
    for arm_name in ("A", "B"):
        seen = [p for arm, p, _pos in trace if arm == arm_name]
        assert sorted(seen) == list(range(pairs))
    for arm in (a, b):
        assert arm.run_calls - arm.warmups == pairs
        assert arm.warmups == batches


@pytest.mark.parametrize("pairs, batches", [
    (0, 3), (-1, 3), (True, 3), ("18", 3), (18.0, 3),
    (18, 0), (18, -1), (18, True), (18, "3"),
    (2, 3),                  # pairs < 2 * batches
    (6, 4),                  # pairs < 2 * batches
    (3, 2),                  # odd total can never balance
    (7, 3),                  # odd total can never balance
])
def test_malformed_sample_counts_are_rejected(pairs, batches):
    """Counts must be positive built-in ints (bools rejected), even in
    total, and large enough for one A-first and one B-first pair per batch.

    Any even total meeting `pairs >= 2 * batches` IS distributable into
    nonempty even batches by two-pair units — 20/4 and 10/3 included — so
    only genuinely impossible combinations are rejected."""
    with pytest.raises(ValueError):
        pv.counterbalanced_ab(lambda: None, lambda: None,
                              pairs=pairs, batches=batches, tag="bad")


def test_a_raising_enter_still_runs_leave_and_restores_state():
    """`enter()` can raise AFTER partially modifying state; `leave()` must
    still run so the partial state is undone, the original exception must
    propagate, and no partial measurement may be returned."""
    sentinel = {"installed": False}
    leaves = {"n": 0}

    def make_bad_runner():
        def enter():
            sentinel["installed"] = True        # partial state, then fail
            raise RuntimeError("simulated enter fault")

        def run():
            raise AssertionError(
                "run() must never execute after a failed enter()")

        def leave():
            leaves["n"] += 1
            sentinel["installed"] = False

        return enter, run, leave

    clock = _FakeClock()
    trace: list = []
    chronology: list = []
    good = _FakeObsArm("A", clock, trace, chronology, lambda pair, pos: 1.0)

    with pytest.raises(RuntimeError, match="simulated enter fault"):
        pv.counterbalanced_ab(good.make, make_bad_runner, pairs=6, batches=3,
                              tag="enter-fault", clock=clock)
    assert leaves["n"] == 1, "leave() must run when enter() raises"
    assert sentinel["installed"] is False, (
        "the partially installed enter() state was not undone")
    assert trace == [], "no measurement may survive a failed enter()"


def test_a_cleanup_base_exception_cannot_mask_a_failed_enter():
    """`leave()` raising KeyboardInterrupt during fault cleanup must not
    replace the original RuntimeError from `enter()`.

    `except Exception` around the secondary cleanup would let a
    BaseException — KeyboardInterrupt, SystemExit — escape and MASK the
    fault that actually broke the run. The outward exception must remain
    the original, the sentinel state must still be restored, and no partial
    result may be returned.
    """
    sentinel = {"installed": False}
    leaves = {"n": 0}

    def make_bad_runner():
        def enter():
            sentinel["installed"] = True        # partial state, then fail
            raise RuntimeError("simulated enter fault")

        def run():
            raise AssertionError(
                "run() must never execute after a failed enter()")

        def leave():
            leaves["n"] += 1
            sentinel["installed"] = False
            raise KeyboardInterrupt             # hostile cleanup

        return enter, run, leave

    clock = _FakeClock()
    trace: list = []
    chronology: list = []
    good = _FakeObsArm("A", clock, trace, chronology, lambda pair, pos: 1.0)

    with pytest.raises(RuntimeError, match="simulated enter fault"):
        pv.counterbalanced_ab(good.make, make_bad_runner, pairs=6, batches=3,
                              tag="enter-mask", clock=clock)
    assert leaves["n"] == 1, "leave() must still run when enter() raises"
    assert sentinel["installed"] is False, (
        "the partially installed enter() state was not undone")
    assert trace == [], "no measurement may survive a failed enter()"


def test_a_cleanup_base_exception_cannot_mask_a_measured_run_fault():
    """The measured path has the same guarantee as the warmup path: a
    cleanup KeyboardInterrupt raised while a measured `run()` fault is
    active must not replace it."""
    state = {"installed": False, "faulted": False}
    calls = {"run": 0, "leave": 0}

    def make_bad_runner():
        def enter():
            state["installed"] = True

        def run():
            calls["run"] += 1
            if calls["run"] > 1:        # the warmup succeeds; a MEASURED
                state["faulted"] = True  # run raises mid-batch
                raise RuntimeError("simulated measured fault")

        def leave():
            calls["leave"] += 1
            state["installed"] = False
            if state["faulted"]:
                state["faulted"] = False
                raise KeyboardInterrupt         # hostile cleanup mid-fault

        return enter, run, leave

    clock = _FakeClock()
    trace: list = []
    chronology: list = []
    good = _FakeObsArm("A", clock, trace, chronology, lambda pair, pos: 1.0)

    with pytest.raises(RuntimeError, match="simulated measured fault"):
        pv.counterbalanced_ab(good.make, make_bad_runner, pairs=6, batches=3,
                              tag="run-mask", clock=clock)
    assert calls["run"] == 2, "the fault must fire on the first measured run"
    assert calls["leave"] == 2, "leave() must run for warmup AND the fault"
    assert state["installed"] is False, "patched state was left installed"


def test_a_raising_arm_still_balances_enter_and_leave():
    """A fault mid-measurement must not leave an arm entered."""
    clock = _FakeClock()
    trace: list = []
    chronology: list = []
    a = _FakeObsArm("A", clock, trace, chronology,
                    lambda pair, pos: 1.0, raise_on_call=10)
    b = _FakeObsArm("B", clock, trace, chronology, lambda pair, pos: 1.0)

    with pytest.raises(RuntimeError, match="simulated arm fault"):
        pv.counterbalanced_ab(a.make, b.make, pairs=18, batches=3,
                              tag="fake-raise", clock=clock)
    assert a.enters == a.leaves > 0
    assert b.enters == b.leaves > 0


def test_a_raising_arm_restores_patches_and_environment(monkeypatch):
    """The REAL runners: a raising pipeline must leave no env var and no
    monkeypatch installed, and the fault must propagate (measurement void,
    never a silent partial result)."""
    from olympus import llm, otel, streamguard, usage

    real_record = usage.record
    real_observe = llm._observe_ctx
    real_guard = streamguard.check_usage_evidence
    real_client = llm.client
    real_post = otel._post
    env_keys = ("OLYMPUS_OTLP_ENDPOINT", "OLYMPUS_ADMISSION",
                "OLYMPUS_STREAMGUARD", "OLYMPUS_CTX_BUDGET")
    env_before = {key: os.environ.get(key) for key in env_keys}

    def exploding(*_a, **_k):
        raise RuntimeError("simulated pipeline fault")

    monkeypatch.setattr(pv, "provider_pipeline", exploding)
    with pytest.raises(RuntimeError, match="simulated pipeline fault"):
        pv.bench_observability_overhead(iters=6, batches=3)

    assert usage.record is real_record
    assert llm._observe_ctx is real_observe
    assert streamguard.check_usage_evidence is real_guard
    assert llm.client is real_client
    assert otel._post is real_post
    assert {key: os.environ.get(key) for key in env_keys} == env_before


def test_a_patch_restore_fault_still_restores_the_environment(monkeypatch):
    """`_obs_runner.leave()` runs `pat.__exit__()` THEN `ctx.__exit__()`.

    If restoring the monkeypatches raises, the environment restore must not
    be skipped: `leave()` needs `try: pat.__exit__() finally: ctx.__exit__()`.
    This drives the REAL `_obs_runner` composition through
    `counterbalanced_ab`: the pipeline raises a RuntimeError, and the real
    `patched` context manager performs its genuine restoration and then
    raises KeyboardInterrupt from `__exit__`. The outward exception must
    remain the ORIGINAL RuntimeError, every patched Olympus attribute must
    be back, and every OLYMPUS_* variable must hold its pre-benchmark
    sentinel — a skipped `ctx.__exit__()` leaves them popped.
    """
    from olympus import llm, otel, streamguard, usage

    real_record = usage.record
    real_observe = llm._observe_ctx
    real_guard = streamguard.check_usage_evidence
    real_client = llm.client
    real_post = otel._post

    sentinels = {"OLYMPUS_OTLP_ENDPOINT": "sentinel-otlp",
                 "OLYMPUS_ADMISSION": "sentinel-admission",
                 "OLYMPUS_STREAMGUARD": "sentinel-streamguard",
                 "OLYMPUS_CTX_BUDGET": "sentinel-ctx-budget"}
    for key, value in sentinels.items():
        monkeypatch.setenv(key, value)

    real_patched = pv.patched

    class _ExplodingRestore(real_patched):
        """The real restoration, then a hostile BaseException."""

        def __exit__(self, *args):
            real_patched.__exit__(self, *args)
            raise KeyboardInterrupt

    def exploding_pipeline(*_a, **_k):
        raise RuntimeError("simulated pipeline fault")

    monkeypatch.setattr(pv, "patched", _ExplodingRestore)
    monkeypatch.setattr(pv, "provider_pipeline", exploding_pipeline)

    result = None
    with pytest.raises(RuntimeError, match="simulated pipeline fault"):
        result = pv.counterbalanced_ab(
            lambda: pv._obs_runner(**pv._ALL_OFF),
            lambda: pv._obs_runner(**pv._ALL_ON),
            pairs=6, batches=3, tag="leave-compose")

    assert result is None, "no partial benchmark result may survive"
    assert usage.record is real_record
    assert llm._observe_ctx is real_observe
    assert streamguard.check_usage_evidence is real_guard
    assert llm.client is real_client
    assert otel._post is real_post
    assert {key: os.environ.get(key) for key in sentinels} == sentinels, (
        "a raising patch restore skipped the environment restore")


# --- 2b. the gate itself, driven synthetically -------------------------------

def _obs_component(**over):
    base = {"ms": 0.5, "noise_ms": 0.1, "resolved": True,
            "lower_bound_ms": 0.4, "upper_bound_ms": 0.6,
            "paired_samples": _OBS_PAIRS,
            "counterbalanced_blocks": _OBS_PAIRS // 2,
            "position_effect_ms": 0.0,
            "batches": _OBS_BATCHES,
            "order_counts": {"a_first": 9, "b_first": 9},
            "order_balanced": True}
    base.update(over)
    return base


def _obs_result(**over):
    """A complete, balanced, comfortably passing measurement."""
    base = {
        "off_p50_ms": 1.0, "on_p50_ms": 11.0,
        "abs_overhead_ms": 10.0, "noise_ms": 0.5,
        "significant": True, "overhead_pct": 1000.0,
        "iters": _OBS_PAIRS, "paired_samples": _OBS_PAIRS,
        "counterbalanced_blocks": _OBS_PAIRS // 2,
        "position_effect_ms": 0.0,
        "batches": _OBS_BATCHES,
        "order_counts": {"a_first": 9, "b_first": 9},
        "order_balanced": True,
        "batch_summaries": [
            {"pairs": 6, "counterbalanced_blocks": 3,
             "a_first": 3, "b_first": 3,
             "delta_ms": 10.0, "noise_ms": 0.5,
             "position_effect_ms": 0.0} for _ in range(_OBS_BATCHES)],
        "overhead_lower_bound_ms": 9.5,
        "overhead_upper_bound_ms": 10.5,
        "components_ms": {name: _obs_component()
                          for name in sorted(_OBS_COMPONENTS)},
    }
    base.update(over)
    # Dependent verdict fields stay consistent with whatever raw evidence a
    # test injected — unless the test poisons them EXPLICITLY, which is how
    # the forged-evidence cases below are expressed.
    if "significant" not in over and _obs_finite(base["abs_overhead_ms"]) \
            and _obs_finite(base["noise_ms"]):
        base["significant"] = abs(base["abs_overhead_ms"]) > base["noise_ms"]
    if "overhead_pct" not in over and _obs_finite(base["abs_overhead_ms"]) \
            and _obs_finite(base["off_p50_ms"]) and base["off_p50_ms"] > 0.0:
        base["overhead_pct"] = (base["abs_overhead_ms"]
                                / base["off_p50_ms"] * 100.0)
    return base


def test_the_observability_gate_accepts_a_complete_balanced_measurement():
    assert observability_gate_violations(_obs_result()) == []


def test_a_proven_breach_message_carries_the_raw_evidence():
    """Lower edge 40 >= 25 fails, and the message keeps every raw number:
    raw overhead, noise, lower edge, upper edge, and the threshold."""
    r = _obs_result(abs_overhead_ms=40.0, noise_ms=0.0,
                    overhead_lower_bound_ms=40.0,
                    overhead_upper_bound_ms=40.0)
    violations = observability_gate_violations(r)
    breach = [v for v in violations if "BREACH" in v]
    assert breach, violations
    assert "40.000" in breach[0] and "25.0" in breach[0]
    assert "noise_ms=0.000" in breach[0]
    assert "upper edge 40.000" in breach[0]


@pytest.mark.parametrize("raw, noise, verdict", [
    (20.0, 4.0, "pass"),           # band [16, 24] entirely below the limit
    (20.0, 5.0, "inconclusive"),   # upper edge exactly on the boundary
    (30.0, 5.93, "inconclusive"),  # the band crosses 25: not a pass
    (40.0, 5.0, "breach"),         # lower edge 35 >= 25
    (10.0, 0.0, "pass"),           # stable genuine overhead below the limit
    (40.0, 0.0, "breach"),         # stable genuine overhead above the limit
])
def test_the_tri_state_threshold_policy(raw, noise, verdict):
    """BREACH on the lower edge, PASS only strictly under the limit with the
    whole band, INCONCLUSIVE-and-failing for everything between."""
    r = _obs_result(abs_overhead_ms=raw, noise_ms=noise,
                    overhead_lower_bound_ms=max(0.0, raw - noise),
                    overhead_upper_bound_ms=max(0.0, raw + noise))
    violations = observability_gate_violations(r)
    if verdict == "pass":
        assert violations == [], violations
    elif verdict == "breach":
        assert any("BREACH" in v for v in violations), violations
        assert all("INCONCLUSIVE" not in v for v in violations), violations
    else:
        assert any("INCONCLUSIVE" in v for v in violations), violations
        assert all("BREACH" not in v for v in violations), violations


@pytest.mark.parametrize("mutate, why", [
    (lambda r: r.update(abs_overhead_ms=float("nan")), "a NaN overhead"),
    (lambda r: r.update(noise_ms=float("inf")), "infinite noise"),
    (lambda r: r.update(overhead_lower_bound_ms=float("-inf")),
     "a non-finite lower bound"),
    (lambda r: r.update(abs_overhead_ms=True), "a boolean overhead"),
    (lambda r: r.update(paired_samples=17), "a lost pair"),
    (lambda r: r.update(paired_samples=True), "a boolean pair count"),
    (lambda r: r.update(iters=6), "an iters/pairs mismatch"),
    (lambda r: r.update(batches=2), "a missing batch"),
    (lambda r: r.update(order_counts={"a_first": 10, "b_first": 8}),
     "an unbalanced execution order"),
    (lambda r: r.update(order_counts={"a_first": 9}),
     "a missing order count"),
    (lambda r: r.update(order_counts={"a_first": 9.0, "b_first": 9.0}),
     "non-integer order counts"),
    (lambda r: r.update(order_balanced=False), "order_balanced not True"),
    (lambda r: r.pop("batch_summaries"), "missing batch summaries"),
    (lambda r: r.update(batch_summaries=r["batch_summaries"][:2]),
     "a dropped batch summary"),
    (lambda r: r["batch_summaries"][0].update(pairs=5),
     "batch pairs that do not sum to the total"),
    (lambda r: r["batch_summaries"][0].update(a_first=5, b_first=1),
     "an order-unbalanced batch"),
    (lambda r: r["batch_summaries"][0].update(pairs=7, a_first=4, b_first=3),
     "an odd-sized batch, which cannot balance"),
    (lambda r: r.update(batch_summaries=[
        {"pairs": 0, "counterbalanced_blocks": 0, "a_first": 0, "b_first": 0,
         "delta_ms": 10.0, "noise_ms": 0.5, "position_effect_ms": 0.0},
        {"pairs": 8, "counterbalanced_blocks": 4, "a_first": 4, "b_first": 4,
         "delta_ms": 10.0, "noise_ms": 0.5, "position_effect_ms": 0.0},
        {"pairs": 10, "counterbalanced_blocks": 5, "a_first": 5, "b_first": 5,
         "delta_ms": 10.0, "noise_ms": 0.5, "position_effect_ms": 0.0}]),
     "a zero-sized batch hidden inside a correct total"),
    (lambda r: r.update(counterbalanced_blocks=9.0),
     "a float total block count that compares equal to 9"),
    (lambda r: r.update(counterbalanced_blocks=8),
     "a forged total block count"),
    (lambda r: r.pop("counterbalanced_blocks"),
     "a missing total block count"),
    (lambda r: r["batch_summaries"][0].update(counterbalanced_blocks=2),
     "a batch block count that is not its own pairs // 2"),
    (lambda r: r["batch_summaries"][1].update(counterbalanced_blocks=3.0),
     "a float batch block count"),
    (lambda r: r["batch_summaries"][2].update(counterbalanced_blocks=-3),
     "a negative batch block count"),
    (lambda r: r["components_ms"]["usage"].update(counterbalanced_blocks=9.0),
     "a float component block count that compares equal to 9"),
    (lambda r: r["components_ms"]["ctx"].update(counterbalanced_blocks=8),
     "a forged component block count"),
    (lambda r: r.update(position_effect_ms=float("nan")),
     "a NaN total position effect"),
    (lambda r: r["batch_summaries"][0].pop("position_effect_ms"),
     "a missing batch position effect"),
    (lambda r: r["components_ms"]["metrics"].update(
        position_effect_ms=float("inf")),
     "an infinite component position effect"),
    (lambda r: r["batch_summaries"][0].update(noise_ms=-1.0),
     "a negative batch noise"),
    (lambda r: r.update(noise_ms=-5.0, overhead_lower_bound_ms=15.0,
                        overhead_upper_bound_ms=5.0),
     "negative noise with forged bounds that satisfy both identities"),
    (lambda r: r.update(overhead_upper_bound_ms=99.0),
     "an upper bound that does not match the raw evidence"),
    (lambda r: r["components_ms"]["usage"].update(paired_samples=18.0),
     "a component pair count that is a float, not a built-in int"),
    (lambda r: r["components_ms"]["ctx"].update(batches=3.0),
     "a component batch count that is a float, not a built-in int"),
    (lambda r: r["components_ms"]["otel"].update(
        noise_ms=-0.1, lower_bound_ms=0.6, upper_bound_ms=0.4),
     "a component with negative noise and forged matching bounds"),
    (lambda r: r.update(significant=False),
     "a well-typed significant flag contradicting delta > noise"),
    (lambda r: r["components_ms"]["guard"].update(resolved=False),
     "a well-typed component resolved flag contradicting its evidence"),
    (lambda r: r.update(overhead_pct=1.0),
     "a finite but forged overhead percentage"),
    (lambda r: r["batch_summaries"][0].update(noise_ms=float("nan")),
     "a NaN batch noise"),
    (lambda r: r.update(overhead_lower_bound_ms=0.0),
     "a lower bound that does not match the raw evidence"),
    (lambda r: r.update(significant="yes"), "a non-bool significance"),
    (lambda r: r.update(off_p50_ms=0.0), "a zero baseline median"),
    (lambda r: r.pop("components_ms"), "missing components"),
    (lambda r: r["components_ms"].pop("usage"), "a missing component"),
    (lambda r: r["components_ms"]["ctx"].update(paired_samples=6),
     "a component measured with fewer pairs"),
    (lambda r: r["components_ms"]["otel"].update(order_balanced=False),
     "an order-unbalanced component"),
    (lambda r: r["components_ms"]["metrics"].update(ms=float("nan")),
     "a NaN component attribution"),
    (lambda r: r["components_ms"]["guard"].update(
        order_counts={"a_first": 18, "b_first": 0}),
     "a component that never alternated"),
    (lambda r: r["components_ms"]["usage"].update(lower_bound_ms=99.0),
     "a component lower bound that does not match its raw evidence"),
])
def test_observability_gate_fails_closed_on_malformed_measurements(mutate,
                                                                   why):
    r = _obs_result()
    mutate(r)
    assert observability_gate_violations(r), why


# --- 2c. the gate over the REAL bench with deterministic fake runners --------

def _fake_bench(monkeypatch, cost_ms_by_flags, *, iters=_OBS_PAIRS,
                batches=_OBS_BATCHES, tags=None):
    """Run the REAL `bench_observability_overhead` end to end over fake
    runners and a fake clock: `cost_ms_by_flags(flags)` prices one pipeline
    run for one instrumentation configuration. Nothing sleeps and nothing
    depends on the host's scheduler."""
    clock = _FakeClock()
    real = pv.counterbalanced_ab

    def with_fake_clock(make_a, make_b, **kw):
        if tags is not None:
            tags.append(kw.get("tag"))
        kw["clock"] = clock
        return real(make_a, make_b, **kw)

    def fake_runner(**flags):
        cost = cost_ms_by_flags(flags)

        def run():
            clock.advance(cost / 1000.0)

        return (lambda: None), run, (lambda: None)

    monkeypatch.setattr(pv, "counterbalanced_ab", with_fake_clock)
    monkeypatch.setattr(pv, "_obs_runner", fake_runner)
    return pv.bench_observability_overhead(iters=iters, batches=batches)


def test_a_stable_genuine_overhead_below_the_limit_passes(monkeypatch):
    """OFF 1 ms, ON 11 ms, zero noise: a real 10 ms overhead passes."""
    r = _fake_bench(monkeypatch,
                    lambda flags: 11.0 if all(flags.values()) else 1.0)
    assert observability_gate_violations(r) == []
    assert r["abs_overhead_ms"] == pytest.approx(10.0, abs=1e-6)
    assert r["overhead_lower_bound_ms"] == pytest.approx(10.0, abs=1e-6)
    assert r["overhead_upper_bound_ms"] == pytest.approx(10.0, abs=1e-6)
    assert r["overhead_upper_bound_ms"] < _OBS_LIMIT_MS, (
        "the whole uncertainty band must clear the limit for a pass")
    assert r["order_counts"] == {"a_first": 9, "b_first": 9}


def test_a_stable_genuine_overhead_above_the_limit_fails(monkeypatch):
    """OFF 1 ms, ON 41 ms, zero noise: a real 40 ms overhead is a breach —
    counterbalancing must never absolve a genuine regression."""
    r = _fake_bench(monkeypatch,
                    lambda flags: 41.0 if all(flags.values()) else 1.0)
    violations = observability_gate_violations(r)
    assert any("BREACH" in v for v in violations), violations
    assert r["abs_overhead_ms"] == pytest.approx(40.0, abs=1e-6)
    assert r["overhead_lower_bound_ms"] >= _OBS_LIMIT_MS


def test_every_component_is_counterbalanced_with_complete_accounting(
        monkeypatch):
    """One counterbalanced measurement per component, after the total, each
    publishing its full order/noise/lower-bound accounting."""
    tags: list = []
    r = _fake_bench(monkeypatch,
                    lambda flags: 3.5 if all(flags.values())
                    else (1.0 if not any(flags.values()) else 3.0),
                    tags=tags)

    assert tags == ["obs-ab", "obs-no-usage", "obs-no-ctx", "obs-no-otel",
                    "obs-no-metrics", "obs-no-guard"]
    assert set(r["components_ms"]) == _OBS_COMPONENTS
    for name, comp in r["components_ms"].items():
        assert comp["paired_samples"] == _OBS_PAIRS, name
        assert comp["counterbalanced_blocks"] == _OBS_PAIRS // 2, name
        assert comp["batches"] == _OBS_BATCHES, name
        assert comp["order_counts"] == {"a_first": 9, "b_first": 9}, name
        assert comp["order_balanced"] is True, name
        assert comp["ms"] == pytest.approx(0.5, abs=1e-6), name
        assert comp["noise_ms"] == pytest.approx(0.0, abs=1e-6), name
        assert comp["lower_bound_ms"] == pytest.approx(0.5, abs=1e-6), name
        assert comp["upper_bound_ms"] == pytest.approx(0.5, abs=1e-6), name
        assert comp["position_effect_ms"] == pytest.approx(0.0,
                                                           abs=1e-6), name
        assert isinstance(comp["resolved"], bool), name
    assert observability_gate_violations(r) == []


# --- 3. context budgeting ----------------------------------------------------

def test_ctxbudget_call_costs_bounded():
    """Reference: estimate_tokens 13.3 us/call, plan 15.0 us/call."""
    r = pv.bench_ctxbudget(calls=300)
    assert r["estimate_tokens_us"] < 300.0, f"estimate_tokens regressed: {r}"
    assert r["plan_us"] < 300.0, f"plan regressed: {r}"
    # the live seam: flag ON delegates to the calibrated estimator, so it is
    # expected to cost more than legacy chars//4 — but not 10x more.
    assert r["seam_on_us"] < 300.0, f"orchestrator ctx seam regressed: {r}"


# --- 4. degenerate-stream defence -------------------------------------------

def test_streamguard_per_delta_cost_bounded_and_off_is_free():
    """Reference: 21.9 us/delta ON, 0.03 us/delta OFF (NullMonitor)."""
    r = pv.bench_streamguard(deltas=400, repeats=3)
    assert r["off_per_delta_us"] < r["on_per_delta_us"], \
        f"the NullMonitor must be cheaper than the real monitor: {r}"
    assert r["off_per_delta_us"] < 5.0, f"flag-off streamguard regressed: {r}"
    assert r["on_per_delta_us"] < 250.0, f"streamguard regressed: {r}"


# --- 5. progress watchdog ----------------------------------------------------

def test_watchdog_call_costs_bounded_and_off_short_circuits():
    """Reference: beat 2.1 us; check 1.9 us (off) vs 27.6 us (observe)."""
    r = pv.bench_watchdog_calls(calls=800)
    assert r["off_check_us"] < r["observe_check_us"], \
        f"mode=off must short-circuit classification: {r}"
    assert r["off_beat_us"] < 60.0, f"lease.beat regressed: {r}"
    assert r["observe_beat_us"] < 60.0, f"lease.beat regressed: {r}"
    assert r["observe_check_us"] < 300.0, f"lease.check regressed: {r}"


# --- 6. admission ------------------------------------------------------------

def test_admission_overhead_bounded_uncontended_and_contended():
    """Reference: slot acquire+release 0.060 ms off / 0.128 ms on
    (uncontended); 4.7 ms off / 8.5 ms on with 8 threads contending."""
    r = pv.bench_admission(calls=40, threads=8, per_thread=5)
    assert r["off_uncontended_p50_ms"] < 5.0, f"slot primitive regressed: {r}"
    assert r["on_uncontended_p50_ms"] < 10.0, f"admission policy regressed: {r}"
    assert r["on_contended_p50_ms"] < 200.0, f"contended admission regressed: {r}"


def test_concurrency_limit_matches_configured_capacity():
    """Reference: 6 concurrent slots with admission OFF (== MAX_CONCURRENT_CALLS,
    and there is no refusal path — it blocks); 5 with admission ON, the sixth
    refused with `no_capacity` because 1 slot is reserved for `critical`."""
    r = pv.bench_concurrency_limit()
    assert r["admission_off_max_concurrent"] == config.MAX_CONCURRENT_CALLS
    assert r["admission_off_behaviour"].startswith("blocks")
    assert 1 <= r["admission_on_max_concurrent"] <= config.MAX_CONCURRENT_CALLS
    assert r["admission_on_max_concurrent"] <= r["admission_off_max_concurrent"]
    assert r["admission_on_refusal_reason"] not in ("", "none-within-probe"), \
        f"admission must REFUSE (never silently downgrade) at capacity: {r}"


# --- 7. replay ---------------------------------------------------------------

def test_replay_is_faster_than_record_and_touches_no_provider():
    """Reference: record 4.68 ms -> replay 0.43 ms (11.0x faster), 0 calls."""
    r = pv.bench_replay(iters=4)
    assert r["provider_calls_during_replay"] == 0, \
        f"replay must make ZERO provider calls: {r}"
    assert r["speedup_x"] > 1.0, \
        f"replay must be FASTER than record, not slower: {r}"


# --- 8. tool-call ladder -----------------------------------------------------

def test_toolcall_ladder_per_call_cost():
    """Wave 1 published 0.016 ms/call against a 0.5 ms target; re-measured
    here at 0.0147 ms/call over the same corpus."""
    r = pv.bench_ladder(passes=8)
    assert r["corpus_cases"] >= 20
    assert r["per_call_ms"] < 0.5, f"ladder overhead regressed: {r}"


# --- 9. memory & storage -----------------------------------------------------

def test_memory_growth_bounded():
    """Reference: 3.2 MiB peak-RSS delta for 3x1000 records."""
    r = pv.bench_memory_growth(n=200)
    for key in (
        "journal_peak_kb", "journal_cur_kb", "heat_peak_kb", "heat_cur_kb",
        "usage_peak_kb", "usage_cur_kb", "total_peak_kb", "total_cur_kb",
    ):
        assert isinstance(r[key], int)
    assert r["total_peak_kb"] >= 0
    assert r["total_peak_kb"] < 200_000, f"RSS growth regressed: {r}"
    assert r["disk_bytes"] > 0


def test_storage_per_turn_bounded_and_extrapolated():
    """Reference: 4.4 KiB/turn -> ~43 MiB at 10k turns."""
    r = pv.bench_storage_per_turn(turns=6)
    assert r["per_turn_bytes"] > 0
    assert r["per_turn_bytes"] < 200 * 1024, f"storage per turn regressed: {r}"
    assert r["extrapolated_10k_bytes"] == pytest.approx(
        r["per_turn_bytes"] * 10000)


# --- 10. HONESTY properties of the report itself ----------------------------

_MUST_BE_UNMEASURABLE = [
    "request latency", "time to first token", "council completion latency",
    "tool latency", "verifier", "queue time", "model-routing savings",
    "prompt-cache savings", "cost per successful task", "recovery cost",
]


def test_every_provider_dependent_metric_is_declared_unmeasurable():
    """The register is the anti-fabrication guard: a metric that needs a real
    provider must appear here rather than as a number in the measured table."""
    blob = " ".join(name.lower() for name, _why, _how in pv.UNMEASURABLE)
    for needle in _MUST_BE_UNMEASURABLE:
        assert needle in blob, f"{needle!r} missing from the UNMEASURABLE register"


def test_every_unmeasurable_entry_states_why_and_how():
    for name, why, how in pv.UNMEASURABLE:
        assert name and why and how, f"incomplete UNMEASURABLE entry: {name}"
        assert len(why) > 30, f"UNMEASURABLE reason too thin: {name}"
        assert "Phase 5" in how or "shadow" in how, \
            f"UNMEASURABLE entry must name what would measure it: {name}"


def test_provider_dependent_objectives_are_labelled_proposals():
    """No objective that needs real traffic may read as measured."""
    needs_traffic = ("Request latency", "Time to first token", "Availability",
                     "Verified-answer rate", "Admission refusal rate",
                     "Cost per successful task")
    seen = set()
    for name, _target, basis in pv.DRAFT_SLOS:
        for key in needs_traffic:
            if name.startswith(key):
                seen.add(key)
                assert ("CANNOT BE VALIDATED UNTIL SHADOW TRAFFIC" in basis
                        or "PROPOSAL WITHHELD" in basis), \
                    f"{name} must be labelled a proposal, got: {basis}"
    assert seen == set(needs_traffic), f"missing objectives: {set(needs_traffic) - seen}"


def test_every_error_budget_is_a_proposal():
    assert pv.ERROR_BUDGETS
    for name, budget, basis in pv.ERROR_BUDGETS:
        assert name and budget
        assert "PROPOSAL" in basis, \
            f"error budget {name} must be labelled a proposal, got: {basis}"


def test_offline_measured_objectives_are_marked_measured_not_proposed():
    """The converse guard: the handful of objectives that DO have an offline
    basis must say so, so a reader can tell the two classes apart."""
    measured = [b for _n, _t, b in pv.DRAFT_SLOS if b.startswith("MEASURED")]
    assert len(measured) >= 4


# --- 11. usage-ledger accounting & latency under concurrency ----------------

_CONTENTION_REPS = 5           # independent repetitions of the whole sweep
_CONTENTION_PER_THREAD = 50    # calls issued by each worker at each level

# WHAT MOVED OUT OF THIS FILE, AND WHY.
#
# The wall-clock VERDICTS on this sweep — start-skew maximum, the contended p50
# bound, p99 < 500 ms, throughput > 100 calls/s, the fresh-scratch first-touch
# bound, and the 3-of-5 quorum that governed them — used to be asserted here, in
# the REQUIRED suite, on uncontrolled GitHub-hosted runners. On the v0.27.3
# release branch that gate failed the Linux Python 3.11 leg with p99 outliers of
# 865-1188 ms across four of five repetitions.
#
# PROVEN on that run: every deterministic guarantee held. Persisted accounting
# was exact, every sample was collected, every worker started, the barrier
# tripped, p50 stayed healthy, throughput stayed above 100 calls/s. The release
# diff touched only version metadata and CHANGELOG.md, and the other Linux
# versions and Windows passed at the same commit.
#
# INFERRED, NOT PROVEN: that the outliers were uncontrolled hosted-runner
# contention. The evidence is consistent with it; the precise external cause was
# never established, and nothing here claims otherwise.
#
# The verdicts now live in scripts/usage_contention_telemetry.py, run by
# .github/workflows/usage-contention-performance.yml on the same platform matrix
# that previously enforced them, with every threshold and the quorum unchanged.
# The MEASUREMENTS did not move: `bench_usage_contention` still reports every
# timing field, this file still runs all five repetitions at all three levels,
# and every deterministic guarantee below is still required, 5/5, with no quorum
# tolerance — because a lost ledger row is a spend-cap defect regardless of how
# the machine was behaving that second.


def _contention_dump(reps: list[list[dict]]) -> str:
    """Every measurement from every repetition, verbatim, for failure output.

    A concurrency bound that fails must print the whole matrix. Printing the
    one row that tripped tells a reader nothing about whether the run was noisy
    or the ledger lock genuinely regressed — which is the only question worth
    asking when this test goes red. `max` on the 1-thread row is the
    fresh-scratch-tree first touch; it dwarfs the other numbers when the process
    happened to be cold and is unremarkable when it was not, so its size is
    diagnostic context rather than a signal on its own.
    """
    lines: list[str] = []
    for i, rows in enumerate(reps, start=1):
        single = rows[0]
        lines.append(f"  repetition {i}/{len(reps)}:")
        for row in rows:
            bound = max(2.0, single["p50"] * row["threads"] * 1.5)
            lines.append(
                f"    threads={row['threads']:>3}"
                f"  samples={row['n']}/{row['expected_samples']}"
                f"  ledger={row['ledger_calls']}/{row['expected_calls']}"
                f"  p50={row['p50']:8.3f}ms (bound {bound:8.3f})"
                f"  p90={row['p90']:8.3f}ms"
                f"  p99={row['p99']:9.3f}ms"
                f"  max={row['max']:9.3f}ms"
                f"{'  <- cold first touch' if row['threads'] == 1 else ''}"
                f"  mean={row['mean']:8.3f}ms"
                f"  wall={row['wall_ms']:9.1f}ms"
                f"  throughput={row['calls_per_s']:8.1f}/s"
                f"  started={row['workers_started']}/{row['barrier_parties']}"
                f"  start_skew={row['start_skew_ms']:.3f}ms"
                f"  ledger_errors={row['ledger_read_errors'] or 'none'}")
    return "\n".join(lines)


def test_usage_ledger_accounting_under_concurrency_is_exact():
    """Stage-D F1, re-characterised. `usage.record` takes a cross-process lock
    around a read-modify-write of the day ledger after EVERY provider call, so
    the council's parallel fan-out serialises there. This is ACCEPTED, not
    fixed — batching would make spend non-durable between flushes and the spend
    cap is a safety property, not a metric.

    CORRECTION to what this test used to claim. It described the measured shape
    as "p50 flat at ~0.20 ms" with only the tail and the ceiling moving. The
    median is NOT flat and never was: serialising N callers behind one lock
    makes the median caller wait out roughly N-1 other holders, so the median
    rises about linearly in the thread count (measured 0.63 -> 3.47 -> 9.28 ms
    at 1 / 6 / 16 threads). The bound asserted below — single-thread p50 x
    threads x 1.5 — has always encoded exactly that linear growth, so the
    docstring contradicted the assertion underneath it.

    WHAT THIS TEST OWNS. The DETERMINISTIC contention contract: that the sweep
    ran the work it claims, at every level, in every repetition, and that the
    ledger accounts for every call. Measured at 1 thread, at the cap the process
    enforces (`config.MAX_CONCURRENT_CALLS`), and at the 16-concurrent-call
    operating bound F1 publishes, over five independent repetitions of 50 calls
    per worker — none of which changed when the wall-clock verdicts moved out.

    Accounting is the load-bearing guarantee. `usage.record` swallows a
    ledger-lock timeout by design (losing one row beats re-billing the user), so
    contention could in principle buy its latency by dropping spend — silently
    breaking the budget cap this whole design exists to keep correct. Every call
    is counted out of the PERSISTED ledger, never inferred from the timings, and
    every count here is exact with no quorum and no tolerance.

    WHAT THIS TEST NO LONGER OWNS. The wall-clock verdicts — start skew, the
    contended median bound, p99, throughput, the fresh-scratch first touch, and
    the 3-of-5 quorum — are enforced unchanged by
    scripts/usage_contention_telemetry.py on a scheduled workflow. See the
    module comment above for the failure that motivated the split and for what
    is proven versus inferred about it. The timing FIELDS are still measured and
    still returned by `bench_usage_contention`; this test simply no longer
    renders a verdict on them from a shared runner it does not control.
    """
    levels = pv.contention_levels()
    assert 1 in levels, f"need an uncontended baseline: {levels}"
    assert config.MAX_CONCURRENT_CALLS in levels, (
        f"the enforced cap must be one of the measured levels: {levels}")
    assert max(levels) >= 16, (
        f"the published 16-concurrent-call operating bound must be measured: "
        f"{levels}")
    # The published operating policy itself, enforced rather than assumed: F1
    # commits to council fan-out staying at or below 16 concurrent provider
    # calls per host, and every bound below is characterised at that ceiling.
    # A config change that raised the cap past it would leave the documented
    # bound unmeasured, so it fails here instead of passing quietly.
    assert config.MAX_CONCURRENT_CALLS <= pv.CONTENTION_OPERATING_BOUND, (
        f"MAX_CONCURRENT_CALLS={config.MAX_CONCURRENT_CALLS} exceeds the "
        f"published operating bound of {pv.CONTENTION_OPERATING_BOUND} "
        f"concurrent provider calls per host (finding F1) — either the cap or "
        f"the published bound has to change, deliberately")

    # Five INDEPENDENT repetitions. Each level within each repetition gets its
    # own scratch tree, so no repetition inherits another's ledger state.
    # Process-level warmth is whatever it happens to be — under the full suite
    # every repetition here is warm, which is exactly why the process-restart
    # guarantee lives in its own subprocess test rather than in repetition 1.
    reps = [pv.bench_usage_contention(levels=levels,
                                      per_thread=_CONTENTION_PER_THREAD)
            for _ in range(_CONTENTION_REPS)]
    dump = "\n" + _contention_dump(reps)

    for i, rows in enumerate(reps, start=1):
        assert [r["threads"] for r in rows] == list(levels), (
            f"repetition {i} did not measure the requested levels {levels}:"
            f"{dump}")

    # --- accounting correctness: 5/5, no quorum, no tolerance ---------------
    # A dropped ledger row is a spend-cap defect, not a slow run, so noise is
    # not an excuse for one: every repetition at every level must account for
    # every call.
    for i, rows in enumerate(reps, start=1):
        for row in rows:
            threads = row["threads"]
            expected = threads * _CONTENTION_PER_THREAD
            assert row["ledger_read_errors"] == [], (
                f"repetition {i}, {threads} threads: the persisted ledger "
                f"could not be read — {row['ledger_read_errors']}:{dump}")
            assert row["expected_calls"] == expected, (
                f"repetition {i}, {threads} threads: benchmark expected the "
                f"wrong call count:{dump}")
            assert row["ledger_calls"] == expected, (
                f"repetition {i}, {threads} threads: the persisted ledger "
                f"recorded {row['ledger_calls']} calls in __all__ but "
                f"{expected} were issued — accounting is LOSING calls under "
                f"contention, which silently under-counts spend against the "
                f"budget cap:{dump}")
            assert row["n"] == row["expected_samples"] == expected, (
                f"repetition {i}, {threads} threads: timed "
                f"{row['n']} samples, expected {expected} "
                f"({threads} threads x {_CONTENTION_PER_THREAD} calls) — the "
                f"latency percentiles below describe less work than they "
                f"claim:{dump}")
            # Contention is only real if every worker was actually in flight.
            assert row["barrier_parties"] == threads, (
                f"repetition {i}: start barrier held {row['barrier_parties']} "
                f"parties for {threads} workers:{dump}")
            assert row["workers_started"] == threads, (
                f"repetition {i}, {threads} threads: only "
                f"{row['workers_started']} workers cleared the start barrier, "
                f"so the level measured less contention than it reports:{dump}")

    # --- the 1-thread baseline must still BE the baseline --------------------
    # Structural, not a timing verdict: the contended rows are judged against
    # row 0 by the telemetry evaluator, so its identity has to hold here.
    for i, rows in enumerate(reps, start=1):
        assert rows[0]["threads"] == 1, (
            f"repetition {i}: the first row must be the uncontended "
            f"baseline:{dump}")

    # NO wall-clock verdict is rendered here. Start skew, the contended median
    # bound, p99, throughput, the fresh-scratch first touch and the 3-of-5
    # quorum are enforced — unchanged — by
    # scripts/usage_contention_telemetry.py on the scheduled workflow. The
    # timing fields above were still measured and are still printed in `dump`
    # on any failure, so a red run here remains fully diagnosable.


class _SimulatedLedgerFault(Exception):
    """Deliberately NOT a RuntimeError, so `pytest.raises(RuntimeError)` below
    can only match the benchmark's own VOID signal — never the injected fault
    escaping the worker unchanged."""


def test_worker_exception_voids_the_contention_measurement():
    """A failing worker must destroy the measurement, never speed it up.

    This is the load-bearing property of every bound in
    `test_usage_ledger_tail_under_concurrency`: the cheapest way to look fast
    under contention is to not do the work. If a worker could raise part-way
    through and have the benchmark quietly summarise whatever samples the
    survivors produced, the p50/p99/throughput rows would improve — fewer
    threads contending, fewer calls timed — and every guarantee above would
    read as PASSING on a run that measured almost nothing.

    Both shapes are covered, because they fail in different places: a fault in
    the FIRST level (before any level has produced a row) and a fault in a
    CONTENDED level (after an earlier level has already succeeded, where a
    partial result is most tempting to keep).
    """
    from olympus import usage

    # --- fault in the uncontended level -------------------------------------
    def always_raises(*_args, **_kwargs):
        raise _SimulatedLedgerFault("simulated ledger fault")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(usage, "record", always_raises)
        with pytest.raises(RuntimeError) as caught:
            pv.bench_usage_contention(levels=(1, 4), per_thread=5)
    message = str(caught.value)
    assert "VOID" in message, (
        f"a worker exception must be reported as a VOID measurement, not as a "
        f"slow one: {message}")
    assert "_SimulatedLedgerFault" in message, (
        f"the failure must name what actually went wrong: {message}")

    # --- fault in the contended level, after a level has already passed ------
    seen = {"calls": 0}
    guard = threading.Lock()
    real_record = usage.record

    def raises_once_contended(*args, **kwargs):
        with guard:
            seen["calls"] += 1
            n = seen["calls"]
        if n > 5:                      # the 1-thread level's 5 calls succeed
            raise _SimulatedLedgerFault("simulated ledger fault under load")
        return real_record(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(usage, "record", raises_once_contended)
        with pytest.raises(RuntimeError) as caught:
            pv.bench_usage_contention(levels=(1, 4), per_thread=5)
    message = str(caught.value)
    assert "VOID" in message and "4-thread" in message, (
        f"a contended level whose workers failed must be voided by that level, "
        f"not salvaged from the level that passed: {message}")
    assert seen["calls"] > 5, (
        "the contended level never ran, so this asserted nothing")


# --- 12. process cold start: the authoritative first-call guarantee ---------

# Runs in a FRESH interpreter, which is the only place the claim can honestly be
# made. In-process the first `usage.record` of a pytest session has long since
# happened by the time this file runs, so the contention benchmark's 1-thread
# row can only speak for a fresh scratch tree, never for a process restart.
#
# The child sockets itself shut before importing anything from `olympus`, so
# "no provider call, no network" is enforced rather than asserted in prose: any
# attempt to open a connection raises inside the child and the parent fails with
# the child's stderr attached. MEMORY_DIR arrives via OLYMPUS_MEMORY_DIR, so the
# fresh temp tree is in force from the first import of `config` — nothing is
# monkeypatched after the fact.
_COLD_START_PROBE = textwrap.dedent(
    """
    import json
    import socket
    import sys
    import time
    from pathlib import Path

    def _no_network(*_a, **_k):
        raise AssertionError(
            "cold-start probe attempted a network connection; it must touch "
            "no provider and no socket")

    # Block CONNECTING, not the socket type itself. Replacing `socket.socket`
    # outright breaks `ssl.SSLSocket`, which subclasses it -- and `ssl` is
    # imported lazily on this very path (proclock -> errors -> telegram ->
    # urllib.request -> http.client -> ssl), so a stub that broke it would
    # abort the measurement rather than guard it. Patching the connect
    # entry points leaves every import working and still makes an outbound
    # call impossible.
    socket.create_connection = _no_network
    socket.socket.connect = _no_network
    socket.socket.connect_ex = _no_network

    sys.path.insert(0, sys.argv[1])

    from olympus import config, memory

    memory.set_user("coldstart-probe")

    from olympus import usage

    # Exactly one call, and the clock covers that call and nothing else.
    t0 = time.perf_counter()
    usage.record("claude-opus-4-8", 100, 50)
    first_call_ms = (time.perf_counter() - t0) * 1000.0

    ledger_dir = config.MEMORY_DIR / "usage"
    files = sorted(p.name for p in ledger_dir.glob("*.json"))
    calls = 0
    for name in files:
        blob = json.loads((ledger_dir / name).read_text(encoding="utf-8"))
        calls += int(blob.get("__all__", {}).get("calls", 0) or 0)

    print(json.dumps({
        "executable": sys.executable,
        "memory_dir": str(config.MEMORY_DIR),
        "record_calls_issued": 1,
        "first_call_ms": first_call_ms,
        "ledger_calls": calls,
        "ledger_files": files,
    }))
    """
)


# Owned by the fresh-interpreter test below, and by nothing else. The contention
# sweep used to apply this same ceiling to its in-process 1-thread row; that
# in-suite use was removed with the other wall-clock verdicts, but the bound
# still governs the subprocess measurement, where a genuinely cold first
# `usage.record` is the thing being timed and the claim is actually true.
_FIRST_TOUCH_MAX_MS = 2000.0


def test_first_usage_record_after_process_start_is_bounded():
    """The authoritative process-cold-start / restart guarantee.

    `usage.record` runs after every provider call, and the FIRST one in a
    process pays costs no later call does: importing the module graph is already
    done by then, but the ledger directory does not exist, the proclock lock
    tree does not exist, and on Windows the one-time "fcntl unavailable" capture
    has not fired. An operator restarting the service pays that on their first
    reply — so it needs a bound, and the bound needs to be measured somewhere
    the claim is actually true.

    In-process it is not true. By the time any test in this file runs, a full
    pytest session has already imported and exercised `usage.record` and
    `proclock` many times over, so the contention benchmark's 1-thread row
    measures a fresh SCRATCH TREE, not a fresh PROCESS. This test spawns a real
    interpreter (`sys.executable`) with a fresh `OLYMPUS_MEMORY_DIR`, has it
    make exactly one `usage.record` call, and times only that call.

    It also proves the call is accounted for: a first write that is fast because
    it silently failed is worse than a slow one, so the child reads its own
    persisted ledger back and reports `__all__.calls`, which must be exactly 1.

    No network, no provider, no credentials, no extra dependency — before
    importing `olympus` the child points `socket.create_connection`,
    `socket.socket.connect` and `socket.socket.connect_ex` at a raising network
    guard, so a provider call would abort it rather than pass unnoticed. The
    `socket.socket` CLASS is deliberately left intact: `ssl.SSLSocket`
    subclasses it, and `ssl` is imported lazily on this very path (proclock ->
    errors -> telegram -> urllib.request -> http.client -> ssl), so replacing
    the type would abort the measurement instead of guarding it. Blocking the
    connect entry points leaves every import working and still makes an
    outbound call impossible.
    """
    memory_dir = tempfile.mkdtemp(prefix="olymp-coldstart-")
    try:
        # mkdtemp guarantees a new empty directory; assert the ledger really is
        # absent so "1 call persisted" cannot be satisfied by a pre-existing one.
        assert not (Path(memory_dir) / "usage").exists(), (
            f"the probe's MEMORY_DIR must start empty: {memory_dir}")

        child_env = dict(os.environ)
        child_env["OLYMPUS_MEMORY_DIR"] = memory_dir
        completed = subprocess.run(
            [sys.executable, "-c", _COLD_START_PROBE, str(_REPO)],
            cwd=_REPO,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert completed.returncode == 0, (
            f"fresh-interpreter cold-start probe failed "
            f"(exit {completed.returncode}):\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}")

        payloads = [line for line in completed.stdout.splitlines()
                    if line.strip().startswith("{")]
        assert payloads, (
            "the cold-start probe printed no JSON payload:\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}")
        try:
            sample = json.loads(payloads[-1])
        except json.JSONDecodeError as err:
            raise AssertionError(
                f"the cold-start probe's output is not JSON ({err}):\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}") from err

        # It really was a fresh interpreter, against a fresh MEMORY_DIR.
        assert sample["executable"] == sys.executable, (
            f"the probe ran under {sample['executable']!r}, not this test's "
            f"interpreter {sys.executable!r}:\n{sample}")
        assert Path(sample["memory_dir"]) == Path(memory_dir), (
            f"the probe used MEMORY_DIR {sample['memory_dir']!r} instead of the "
            f"fresh temp tree {memory_dir!r}:\n{sample}")

        # Exactly one call, and it is in the ledger.
        assert sample["record_calls_issued"] == 1, sample
        assert sample["ledger_files"] and len(sample["ledger_files"]) == 1, (
            f"one usage.record call must persist exactly one day ledger:\n"
            f"{sample}")
        assert sample["ledger_calls"] == 1, (
            f"the first usage.record of a fresh process persisted "
            f"{sample['ledger_calls']} calls in __all__, expected exactly 1 — "
            f"a first write that is fast because it was dropped is worse than a "
            f"slow one:\n{sample}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")

        # And it is bounded.
        assert sample["first_call_ms"] > 0.0, (
            f"the probe reported a non-positive first-call latency:\n{sample}")
        assert sample["first_call_ms"] < _FIRST_TOUCH_MAX_MS, (
            f"the FIRST usage.record after a process restart took "
            f"{sample['first_call_ms']:.3f} ms, at or above the "
            f"{_FIRST_TOUCH_MAX_MS:.0f} ms bound — every operator pays this on "
            f"their first reply after a deploy:\n{sample}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
    finally:
        shutil.rmtree(memory_dir, ignore_errors=True)


# --- 13. the published report curve may not shrink --------------------------

def test_full_report_concurrency_curve_is_never_reduced():
    """`run_all` publishes a CURVE; the focused gate checks three points.

    These are different jobs and the narrower one must not quietly replace the
    wider one. Hardening the gate down to 1 / cap / 16 was correct — a gate
    answers "does the guarantee still hold" — but if the report had been
    narrowed with it, the scaling shape the F1 finding is argued from would
    have silently disappeared from the harness output. This pins the historical
    levels at the source so that cannot happen by omission.
    """
    assert pv.REPORT_LEVELS == (1, 2, 4, 8, 16), (
        f"the historical reporting curve was changed at the source: "
        f"{pv.REPORT_LEVELS}")

    levels = pv.report_levels()
    for historical in (1, 2, 4, 8, 16):
        assert historical in levels, (
            f"the full report dropped the {historical}-thread level, so its "
            f"concurrency curve no longer covers the published shape: {levels}")
    assert config.MAX_CONCURRENT_CALLS in levels, (
        f"the cap this process enforces ({config.MAX_CONCURRENT_CALLS}) must be "
        f"one of the reported levels: {levels}")
    assert list(levels) == sorted(set(levels)), (
        f"report levels must be sorted and unique: {levels}")

    # The gate is allowed to be narrower — but only a subset, never a different
    # set of points than the report is drawn from.
    assert set(pv.contention_levels()) <= set(levels), (
        f"the focused gate measures levels the full report does not: "
        f"gate {pv.contention_levels()} vs report {levels}")
