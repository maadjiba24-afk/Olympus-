"""Deterministic regressions for the usage-contention telemetry evaluator.

The wall-clock verdicts on `usage.record` contention moved out of the required
pytest suite and into `scripts/usage_contention_telemetry.py`, which the
required suite does not execute. Its evaluator is pure, so it is pinned here
with SYNTHETIC rows: no threads, no ledger, no filesystem, no timing. If the
moved contract rotted, nothing else would notice.

Everything below is driven by injected numbers, so these tests are as
deterministic on a loaded shared runner as on an idle laptop — which is the
whole point of the relocation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import usage_contention_telemetry as uct  # noqa: E402


_LEVELS = (1, 6, 16)
_PER_THREAD = uct.CONTRACT["per_thread"]


def _num(value):
    """A finite, non-boolean number — the only thing worth deriving from."""
    import math
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _row(threads, over=None):
    """One structurally perfect, comfortably passing level measurement.

    Overrides arrive as a dict rather than `**kwargs` so a test can poison
    `threads` itself without colliding with this function's own parameter.

    COHERENT BY CONSTRUCTION. `max`, `wall_ms` and `calls_per_s` are DERIVED
    from whatever the test asked for, unless the test pinned them explicitly.
    The evaluator now enforces arithmetic identities — percentiles ordered,
    max within the wall time, throughput equal to n/wall — so a synthetic row
    that raised `p99` without raising `max` would trip a measurement-integrity
    violation instead of the performance bound it meant to test, and the test
    would pass for the wrong reason. Deriving keeps each case aimed at exactly
    one rule; a test that wants an INCONSISTENT row pins the field itself.
    """
    over = dict(over or {})
    expected = threads * _PER_THREAD
    row = {
        "threads": threads,
        "per_thread": _PER_THREAD,
        "n": expected,
        "expected_samples": expected,
        "ledger_calls": expected,
        "expected_calls": expected,
        "ledger_read_errors": [],
        "barrier_parties": threads,
        "workers_started": threads,
        "p50": 0.5 * threads,
        "p90": 1.0 * threads,
        "p99": 2.0 * threads,
        "mean": 0.6 * threads,
        "start_skew_ms": 1.0,
        "max": 4.0 * threads,
        "wall_ms": 16.0 * threads,
        "calls_per_s": 1000.0,
    }
    row.update(over)

    if "max" not in over and _num(row["p99"]) and _num(row["mean"]):
        row["max"] = max(row["p99"], row["mean"]) * 2.0
    if "wall_ms" not in over:
        if ("calls_per_s" in over and _num(row["calls_per_s"])
                and row["calls_per_s"] > 0 and _num(row["n"])):
            row["wall_ms"] = row["n"] / row["calls_per_s"] * 1000.0
        elif _num(row["max"]) and _num(row["start_skew_ms"]):
            row["wall_ms"] = max(row["max"], row["start_skew_ms"]) * 2.0
    if ("calls_per_s" not in over and _num(row["n"])
            and _num(row["wall_ms"]) and row["wall_ms"] > 0):
        row["calls_per_s"] = row["n"] / (row["wall_ms"] / 1000.0)
    return row


def _rep(over_by_threads=None):
    """One repetition: one row per level, with optional per-level overrides.

    Overrides are keyed by THREAD COUNT and passed positionally — `**` cannot
    expand integer keys.
    """
    over_by_threads = over_by_threads or {}
    return [_row(t, over_by_threads.get(t)) for t in _LEVELS]


def _sweep(bad_reps=0, over=None):
    """`repetitions` reps, the first `bad_reps` of which carry `over`."""
    return [_rep(over) if i < bad_reps else _rep()
            for i in range(uct.CONTRACT["repetitions"])]


# --- the preserved contract itself ------------------------------------------

def test_every_threshold_is_the_preserved_one():
    """The moved numbers are the original numbers. Pinned so they cannot drift."""
    assert uct.CONTRACT["repetitions"] == 5
    assert uct.CONTRACT["quorum"] == 3
    assert uct.CONTRACT["per_thread"] == 50
    assert uct.CONTRACT["max_start_skew_ms"] == 100.0
    assert uct.CONTRACT["p50_floor_ms"] == 2.0
    assert uct.CONTRACT["p50_serialisation_factor"] == 1.5
    assert uct.CONTRACT["p99_max_ms"] == 500.0
    assert uct.CONTRACT["min_calls_per_s"] == 100.0
    assert uct.CONTRACT["first_touch_max_ms"] == 2000.0


def test_median_bound_is_max_of_floor_and_serialisation_curve():
    # `approx` because 0.1*16*1.5 is 2.4000000000000004 in binary floating
    # point; the contract is the formula, not a decimal literal.
    assert uct.median_bound_ms(0.1, 16) == pytest.approx(2.4)   # 0.1*16*1.5
    assert uct.median_bound_ms(0.01, 6) == 2.0                  # floor wins
    assert uct.median_bound_ms(10.0, 16) == pytest.approx(240.0)


# --- quorum arithmetic -------------------------------------------------------

def test_a_clean_sweep_passes():
    verdict = uct.evaluate(_sweep(), _LEVELS)
    assert verdict["passed"] is True, verdict
    assert verdict["hard_violations"] == []
    assert verdict["clean_repetitions"] == [1, 2, 3, 4, 5]


def test_three_of_five_meets_the_quorum():
    verdict = uct.evaluate(_sweep(bad_reps=2, over={16: {"p99": 900.0}}), _LEVELS)
    assert verdict["clean_repetitions"] == [3, 4, 5]
    assert verdict["quorum_met"] is True
    assert verdict["passed"] is True, verdict


def test_two_of_five_fails_the_quorum():
    verdict = uct.evaluate(_sweep(bad_reps=3, over={16: {"p99": 900.0}}), _LEVELS)
    assert verdict["clean_repetitions"] == [4, 5]
    assert verdict["quorum_met"] is False
    assert verdict["passed"] is False, verdict


def test_the_reported_v0273_failure_shape_is_rejected():
    """Four of five repetitions with 865-1188 ms p99 must NOT pass."""
    reps = [_rep({16: {"p99": p99}}) if p99 else _rep()
            for p99 in (865.0, 1188.0, 1042.0, 903.0, None)]
    verdict = uct.evaluate(reps, _LEVELS)
    assert verdict["clean_repetitions"] == [5]
    assert verdict["passed"] is False


# --- strict thresholds: equality must FAIL -----------------------------------

@pytest.mark.parametrize("over, needle", [
    ({16: {"p99": 500.0}}, "p99"),                    # == 500 fails
    ({16: {"calls_per_s": 100.0}}, "throughput"),     # == 100 fails
    ({6: {"p99": 500.0}}, "p99"),
    ({6: {"calls_per_s": 100.0}}, "throughput"),
])
def test_threshold_equality_is_a_failure_not_a_pass(over, needle):
    failures = uct.repetition_performance_failures(_rep(over))
    assert failures and any(needle in f for f in failures), failures


def test_median_exactly_on_its_bound_fails():
    """p50 == max(2.0, single_p50 * threads * 1.5) must fail, not pass."""
    single_p50 = 0.5
    bound = uct.median_bound_ms(single_p50, 16)
    rep = _rep({1: {"p50": single_p50}, 16: {"p50": bound}})
    failures = uct.repetition_performance_failures(rep)
    assert any("median" in f for f in failures), failures
    # ...and a hair under the bound passes, so the boundary is where it says.
    ok = _rep({1: {"p50": single_p50}, 16: {"p50": bound - 1e-9}})
    assert uct.repetition_performance_failures(ok) == []


def test_first_touch_exactly_on_its_bound_fails():
    """1-thread max == 2000 ms must fail, and it carries NO quorum."""
    limit = uct.CONTRACT["first_touch_max_ms"]
    hard = uct.repetition_hard_violations(_rep({1: {"max": limit}}), _LEVELS)
    assert any("first-touch" in v for v in hard), hard

    # A single bad repetition sinks the whole sweep despite a clean quorum.
    reps = _sweep(bad_reps=1, over={1: {"max": limit}})
    verdict = uct.evaluate(reps, _LEVELS)
    assert verdict["quorum_met"] is True
    assert verdict["passed"] is False, "first touch must not be quorum-tolerant"


def test_start_skew_above_the_maximum_fails():
    limit = uct.CONTRACT["max_start_skew_ms"]
    failures = uct.repetition_performance_failures(
        _rep({16: {"start_skew_ms": limit + 0.001}}))
    assert any("start skew" in f for f in failures), failures
    # Exactly at the limit is preserved as passing, matching the moved rule.
    assert uct.repetition_performance_failures(
        _rep({16: {"start_skew_ms": limit}})) == []


# --- hard failures: no quorum tolerance --------------------------------------

@pytest.mark.parametrize("over, needle", [
    ({16: {"ledger_calls": 799}}, "LOSING calls"),
    ({16: {"ledger_read_errors": ["unreadable"]}}, "could not be read"),
    ({16: {"workers_started": 15}}, "cleared the start barrier"),
    ({16: {"barrier_parties": 15}}, "start barrier held"),
    ({16: {"n": 799}}, "samples"),
    ({16: {"expected_samples": 799}}, "samples"),
    ({16: {"expected_calls": 799}}, "expected"),
])
def test_accounting_and_worker_failures_are_hard(over, needle):
    """One bad repetition fails the sweep even with a clean 4/5 quorum."""
    hard = uct.repetition_hard_violations(_rep(over), _LEVELS)
    assert any(needle in v for v in hard), hard

    verdict = uct.evaluate(_sweep(bad_reps=1, over=over), _LEVELS)
    assert verdict["hard_violations"], verdict
    assert verdict["passed"] is False
    assert len(verdict["clean_repetitions"]) >= uct.CONTRACT["quorum"], (
        "the quorum was still met, which is exactly why this must be hard")


def test_missing_levels_fail():
    short = _rep()[:2]
    assert uct.repetition_hard_violations(short, _LEVELS)
    reordered = list(reversed(_rep()))
    assert uct.repetition_hard_violations(reordered, _LEVELS)


def test_wrong_repetition_count_fails():
    for count in (0, 4, 6):
        verdict = uct.evaluate([_rep() for _ in range(count)], _LEVELS)
        assert verdict["passed"] is False, count
        assert any("repetitions" in v for v in verdict["hard_violations"])


def test_a_structurally_unusable_repetition_cannot_count_toward_the_quorum():
    reps = _sweep(bad_reps=3, over={16: {"ledger_calls": 799}})
    verdict = uct.evaluate(reps, _LEVELS)
    assert verdict["clean_repetitions"] == [4, 5]
    assert verdict["quorum_met"] is False
    assert verdict["passed"] is False


# --- fail closed on unusable values ------------------------------------------

@pytest.mark.parametrize("field, value", [
    ("p99", float("nan")),
    ("p99", float("inf")),
    ("p50", float("-inf")),
    ("p50", float("nan")),
    ("calls_per_s", float("nan")),
    ("start_skew_ms", float("inf")),
    ("max", float("nan")),
    ("p99", None),
    ("p99", "500"),
    ("p99", True),
    ("calls_per_s", False),
])
def test_non_finite_or_boolean_measurements_fail_closed(field, value):
    hard = uct.repetition_hard_violations(_rep({16: {field: value}}), _LEVELS)
    assert hard, f"{field}={value!r} must be rejected, not silently compared"


@pytest.mark.parametrize("field, value", [
    ("ledger_calls", True),
    ("n", True),
    ("workers_started", -1),
    ("barrier_parties", 3.5),
    ("ledger_calls", None),
    ("expected_calls", "800"),
])
def test_non_integer_counts_fail_closed(field, value):
    hard = uct.repetition_hard_violations(_rep({16: {field: value}}), _LEVELS)
    assert hard, f"{field}={value!r} must be rejected"


@pytest.mark.parametrize("missing", [
    "p99", "p50", "calls_per_s", "start_skew_ms", "max",
    "ledger_calls", "n", "workers_started", "barrier_parties",
])
def test_missing_fields_fail_closed(missing):
    row = _row(16)
    del row[missing]
    rep = [_row(1), _row(6), row]
    hard = uct.repetition_hard_violations(rep, _LEVELS)
    assert any(missing in v for v in hard), hard


def test_malformed_rows_and_error_lists_fail_closed():
    assert uct.row_hard_violations("not a row", threads=16)
    assert uct.repetition_hard_violations("not a repetition", _LEVELS)
    assert uct.repetition_hard_violations(
        _rep({16: {"ledger_read_errors": "boom"}}), _LEVELS)


# --- the runner: red must still produce evidence -----------------------------

_LEAKY = ("TAVILY_API_KEY=sk_live_9f3a21 "
          "C:/Users/someone/AppData/Local/Temp/olymperf-abc/usage")


class _LoudFailure(Exception):
    """Carries a credential-shaped message, as a real exception might."""


class _FakeHarness:
    """Stands in for `perf_validation` at the `_load_benchmark` seam."""

    def __init__(self, *, bench_error=None, cleanup_error=None, reps=None,
                 levels=_LEVELS):
        self._bench_error = bench_error
        self._cleanup_error = cleanup_error
        self._reps = list(reps) if reps is not None else None
        self._levels = levels
        self.cleanup_calls = 0
        self.bench_calls = 0

    def contention_levels(self):
        return self._levels

    def bench_usage_contention(self, levels, per_thread):
        self.bench_calls += 1
        if self._bench_error is not None:
            raise self._bench_error
        if self._reps:
            return self._reps.pop(0)
        return _rep()

    def cleanup(self):
        self.cleanup_calls += 1
        if self._cleanup_error is not None:
            raise self._cleanup_error


def _run(monkeypatch, tmp_path, loader):
    monkeypatch.setattr(uct, "_load_benchmark", loader)
    out = tmp_path / "telemetry" / "usage-contention.json"
    code = uct.main(["--out", str(out)])
    return code, out


def test_a_clean_run_exits_zero_and_writes_a_passing_artifact(monkeypatch,
                                                              tmp_path):
    harness = _FakeHarness()
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "pass"
    assert data["failure_reasons"] == []
    assert data["clean_repetitions"] == [1, 2, 3, 4, 5]
    assert harness.bench_calls == uct.CONTRACT["repetitions"], (
        "each repetition must be measured exactly once, never retried")
    assert harness.cleanup_calls == 1


def test_a_quorum_breach_exits_nonzero_with_the_artifact(monkeypatch,
                                                         tmp_path):
    reps = _sweep(bad_reps=3, over={16: {"p99": 900.0}})
    harness = _FakeHarness(reps=reps)
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any("repetitions met every" in r for r in data["failure_reasons"])
    assert data["clean_repetitions"] == [4, 5]


def test_setup_failure_still_writes_a_red_artifact(monkeypatch, tmp_path):
    def exploding_loader():
        raise _LoudFailure(_LEAKY)

    code, out = _run(monkeypatch, tmp_path, exploding_loader)
    assert code == 1
    assert out.is_file(), "no artifact was written for a failed setup"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any("_LoudFailure" in r for r in data["failure_reasons"])
    assert data["repetitions"] == []


def test_benchmark_failure_still_writes_a_red_artifact(monkeypatch, tmp_path):
    harness = _FakeHarness(bench_error=_LoudFailure(_LEAKY))
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any("_LoudFailure" in r for r in data["failure_reasons"])
    assert harness.cleanup_calls == 1, "cleanup must run even after a fault"


def test_cleanup_failure_still_writes_a_red_artifact(monkeypatch, tmp_path):
    harness = _FakeHarness(cleanup_error=_LoudFailure(_LEAKY))
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any("cleanup" in r and "_LoudFailure" in r
               for r in data["failure_reasons"])
    # The measurements it DID take survive the cleanup fault.
    assert len(data["repetitions"]) == uct.CONTRACT["repetitions"]
    assert harness.cleanup_calls == 1, "cleanup must not be retried"


@pytest.mark.parametrize("loader_factory", [
    lambda: (lambda: (_ for _ in ()).throw(_LoudFailure(_LEAKY))),
    lambda: (lambda: _FakeHarness(bench_error=_LoudFailure(_LEAKY))),
    lambda: (lambda: _FakeHarness(cleanup_error=_LoudFailure(_LEAKY))),
])
def test_failure_artifacts_never_carry_the_exception_message(
        monkeypatch, tmp_path, loader_factory):
    """Only the exception TYPE reaches the artifact — never its message."""
    code, out = _run(monkeypatch, tmp_path, loader_factory())
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    for secret in ("TAVILY_API_KEY", "sk_live_9f3a21", "AppData",
                   "C:/Users/someone", "olymperf-abc"):
        assert secret not in blob, f"{secret!r} leaked into the artifact"
    assert "_LoudFailure" in blob, "the exception type must still be reported"


def test_published_rows_are_allowlisted(monkeypatch, tmp_path):
    """Only numeric measurements and fixed identifiers may be published."""
    dirty = _rep()
    dirty[2].update({
        "conversation_id": "perf-16t-deadbeef",
        "scratch_dir": "C:/Users/someone/AppData/Local/Temp/olymperf-abc",
        "env": {"TAVILY_API_KEY": "sk_live_9f3a21"},
        "ledger_read_errors": ["cannot read C:/Users/someone/secret.json"],
    })
    harness = _FakeHarness(reps=[dirty] + [_rep() for _ in range(4)])
    code, out = _run(monkeypatch, tmp_path, lambda: harness)

    blob = out.read_text(encoding="utf-8")
    for secret in ("conversation_id", "scratch_dir", "TAVILY_API_KEY",
                   "sk_live_9f3a21", "AppData", "secret.json", "deadbeef"):
        assert secret not in blob, f"{secret!r} leaked into the artifact"
    # A ledger read error is still REPORTED — as a count, and as a red verdict.
    assert code == 1
    data = json.loads(blob)
    assert data["repetitions"][0]["rows"][2]["ledger_read_error_count"] == 1


# --- no rejected value may reach ANY output channel --------------------------
#
# A key allowlist controls which NAMES are copied, not what is copied under
# them, and refusing to publish a bad value is worthless if a failure reason
# interpolates it. Both paths are checked here, for every allowlisted field, on
# every channel a CI run can read: the artifact, stdout, stderr, and the
# evaluator's own reason strings.

_SECRET = "sk_live_9f3a21_TAVILY_API_KEY"


class _Unserializable:
    """Not JSON-serializable, and its repr carries the secret."""

    def __repr__(self):
        return f"<Unserializable {_SECRET}>"


_LEAK_FIELDS = (
    # measurement fields, validated by _finite
    "p50", "p99", "p90", "max", "mean", "wall_ms", "calls_per_s",
    "start_skew_ms",
    # count fields, validated by _count
    "n", "expected_samples", "expected_calls", "ledger_calls",
    "workers_started", "barrier_parties", "threads", "per_thread",
)

_LEAK_VALUES = (
    _SECRET,
    ["nested", _SECRET],
    {"env": _SECRET},
    _Unserializable(),
    float("nan"),
    float("inf"),
    float("-inf"),
    True,
)


def _assert_no_leak(blob, captured, reasons):
    """No channel may carry the rejected VALUE.

    A bare type name is explicitly allowed — it is the safe diagnostic the
    design keeps — so this checks the value itself and, separately, that
    `repr()` was never interpolated (the repr carries the secret, so its
    distinctive opening bracket is the tell).
    """
    for channel, text in (("artifact", blob),
                          ("stdout", captured.out),
                          ("stderr", captured.err),
                          ("reasons", " ".join(reasons))):
        assert _SECRET not in text, f"{_SECRET!r} leaked into {channel}"
        assert "<Unserializable" not in text, f"a repr leaked into {channel}"


@pytest.mark.parametrize("field", _LEAK_FIELDS)
@pytest.mark.parametrize("value", _LEAK_VALUES,
                         ids=lambda v: type(v).__name__)
def test_a_rejected_value_never_reaches_any_channel(field, value, monkeypatch,
                                                    tmp_path, capsys):
    """Every allowlisted field x every rejected shape, on every channel."""
    poisoned = _rep({16: {field: value}})
    harness = _FakeHarness(reps=[poisoned] + [_rep() for _ in range(4)])

    # The evaluator's own reasons must not carry it either.
    reasons = uct.repetition_hard_violations(poisoned, _LEVELS)
    assert reasons, f"{field}={type(value).__name__} must be rejected"

    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 1, "a rejected measurement must be red"
    blob = out.read_text(encoding="utf-8")
    _assert_no_leak(blob, capsys.readouterr(), reasons)

    # ...and the artifact is still valid, parseable JSON.
    data = json.loads(blob)
    assert data["verdict"] == "fail"


@pytest.mark.parametrize("value", [_SECRET, ["boom"], {"a": 1},
                                   _Unserializable(), 7])
def test_malformed_ledger_read_errors_never_leak(value, monkeypatch, tmp_path,
                                                 capsys):
    poisoned = _rep({16: {"ledger_read_errors": value}})
    reasons = uct.repetition_hard_violations(poisoned, _LEVELS)
    harness = _FakeHarness(reps=[poisoned] + [_rep() for _ in range(4)])
    code, out = _run(monkeypatch, tmp_path, lambda: harness)

    assert code == 1
    blob = out.read_text(encoding="utf-8")
    _assert_no_leak(blob, capsys.readouterr(), reasons)
    # A list of messages is reduced to a count; anything else publishes None.
    published = json.loads(blob)["repetitions"][0]["rows"][2]
    assert published["ledger_read_error_count"] in (None, len(value)
                                                    if isinstance(value, list)
                                                    else None)


def test_published_values_are_type_validated_not_merely_key_filtered():
    """The allowlist is a floor, not the guarantee — types are re-checked."""
    rows = _rep({16: {"p50": _SECRET, "n": _SECRET, "threads": _SECRET,
                      "calls_per_s": float("nan"),
                      "ledger_calls": True}})
    published = uct._published_rows(rows)[2]
    for key in ("p50", "n", "threads", "calls_per_s", "ledger_calls"):
        assert published[key] is None, (key, published[key])
    blob = json.dumps(published)
    assert _SECRET not in blob


def test_the_artifact_is_always_valid_json_without_nan(monkeypatch, tmp_path):
    """`allow_nan=False`: a non-finite must never be emitted as bare NaN."""
    poisoned = _rep({16: {"p99": float("nan"), "p50": float("inf")}})
    harness = _FakeHarness(reps=[poisoned] + [_rep() for _ in range(4)])
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    for token in ("NaN", "Infinity", "-Infinity"):
        assert token not in blob, f"{token} is not valid JSON"
    json.loads(blob)      # parses under a strict reader


def test_artifact_and_console_are_the_same_string(monkeypatch, tmp_path,
                                                  capsys):
    """One serialization, so the two channels cannot disagree."""
    harness = _FakeHarness()
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 0
    blob = out.read_text(encoding="utf-8").rstrip("\n")
    assert blob in capsys.readouterr().out


# --- per_thread is enforced, not merely published ----------------------------

@pytest.mark.parametrize("value, why", [
    (49, "a smaller call count is a different measurement"),
    (51, "a larger call count is a different measurement"),
    (0, "no calls at all"),
    (True, "a bool masquerading as a count"),
    (-1, "a negative count"),
    ("50", "a string count"),
    (50.0, "a float count"),
    (None, "a missing value"),
])
def test_wrong_per_thread_is_a_hard_violation(value, why):
    hard = uct.repetition_hard_violations(_rep({16: {"per_thread": value}}),
                                          _LEVELS)
    assert hard, why


def test_missing_per_thread_is_a_hard_violation():
    row = _row(16)
    del row["per_thread"]
    hard = uct.repetition_hard_violations([_row(1), _row(6), row], _LEVELS)
    assert any("per_thread" in v for v in hard), hard


def test_per_thread_violations_carry_no_quorum_tolerance():
    """One repetition with the wrong call count sinks a clean 4/5 quorum."""
    verdict = uct.evaluate(_sweep(bad_reps=1, over={16: {"per_thread": 49}}),
                           _LEVELS)
    assert len(verdict["clean_repetitions"]) >= uct.CONTRACT["quorum"]
    assert verdict["hard_violations"], verdict
    assert verdict["passed"] is False


# --- measurement integrity: impossible readings are not fast readings --------
#
# v2 validated finiteness and stopped there. Two sweeps of physically
# impossible data therefore evaluated GREEN, because every value sat under its
# bound: an all-negative one (a negative p99 is comfortably "< 500 ms") and a
# positive but inverted one. Both are reproduced verbatim below and must now be
# red. These are integrity checks, not new performance thresholds — no
# preserved bound moved.

_V2_ALL_NEGATIVE = {"p50": -1.0, "p90": -2.0, "p99": -3.0, "max": -4.0,
                    "mean": -5.0, "wall_ms": -6.0, "start_skew_ms": -7.0,
                    "calls_per_s": 1000.0}
_V2_INVERTED = {"p50": 1.0, "p90": 0.8, "p99": 0.5, "max": 0.4, "mean": 0.3}


@pytest.mark.parametrize("poison, why", [
    (_V2_ALL_NEGATIVE, "the all-negative sweep that v2 passed"),
    (_V2_INVERTED, "the inverted-percentile sweep that v2 passed"),
])
def test_the_formerly_green_impossible_sweeps_are_now_red(poison, why):
    """The exact two datasets from the v2 reproduction."""
    reps = [_rep({16: dict(poison)}) for _ in range(uct.CONTRACT["repetitions"])]
    verdict = uct.evaluate(reps, _LEVELS)
    assert verdict["passed"] is False, why
    assert verdict["hard_violations"], why


@pytest.mark.parametrize("field", ["p50", "p90", "p99", "max", "mean",
                                   "wall_ms", "calls_per_s"])
@pytest.mark.parametrize("value", [-1.0, -0.000001, 0.0, 0])
def test_non_positive_timings_and_rates_fail_hard(field, value):
    """A duration or a rate at or below zero is impossible, not fast."""
    hard = uct.repetition_hard_violations(_rep({16: {field: value}}), _LEVELS)
    assert any(field in v and "valid measurement" in v for v in hard), hard


@pytest.mark.parametrize("value", [-1.0, -0.000001])
def test_negative_start_skew_fails_hard(value):
    hard = uct.repetition_hard_violations(
        _rep({16: {"start_skew_ms": value}}), _LEVELS)
    assert any("start_skew_ms" in v for v in hard), hard


def test_zero_start_skew_is_legitimate():
    """One worker cannot be skewed against anything, so 0.0 must be valid."""
    assert uct.repetition_hard_violations(
        _rep({1: {"start_skew_ms": 0.0}}), _LEVELS) == []


@pytest.mark.parametrize("over, needle", [
    ({"p50": 20.0, "p90": 16.0}, "p50"),                  # p50 > p90
    ({"p90": 40.0, "p99": 32.0}, "p90"),                  # p90 > p99
    ({"p99": 100.0, "max": 64.0}, "p99"),                 # p99 > max
    ({"mean": 100.0, "max": 64.0}, "mean"),               # mean > max
    ({"max": 5000.0, "wall_ms": 128.0}, "max"),           # max > wall
    ({"start_skew_ms": 500.0, "wall_ms": 128.0}, "start skew"),
])
def test_internally_inconsistent_measurements_fail_hard(over, needle):
    """Arithmetic identities the benchmark's own output must satisfy."""
    hard = uct.repetition_hard_violations(_rep({16: over}), _LEVELS)
    assert any(needle in v for v in hard), hard


@pytest.mark.parametrize("cps", [1.0, 6249.0, 6251.0, 12500.0])
def test_throughput_inconsistent_with_n_over_wall_fails_hard(cps):
    """`calls_per_s` must be the rate that was actually measured."""
    # wall_ms pinned, so calls_per_s is NOT derived and can disagree with it.
    hard = uct.repetition_hard_violations(
        _rep({16: {"wall_ms": 128.0, "calls_per_s": cps}}), _LEVELS)
    assert any("throughput" in v for v in hard), hard


def test_throughput_consistent_within_tolerance_passes():
    """Float round-trip noise must not be mistaken for an accounting fault."""
    implied = 800 / (128.0 / 1000.0)
    for nudge in (1.0, 1 + 1e-12, 1 - 1e-12):
        assert uct.repetition_hard_violations(
            _rep({16: {"wall_ms": 128.0, "calls_per_s": implied * nudge}}),
            _LEVELS) == [], nudge


def test_one_impossible_repetition_sinks_a_satisfied_quorum():
    """Integrity carries no quorum tolerance, even at 4 clean repetitions."""
    verdict = uct.evaluate(_sweep(bad_reps=1, over={16: dict(_V2_INVERTED)}),
                           _LEVELS)
    assert len(verdict["clean_repetitions"]) >= uct.CONTRACT["quorum"], (
        "the quorum was still met, which is exactly why this must be hard")
    assert verdict["hard_violations"], verdict
    assert verdict["passed"] is False


def test_invalid_measurements_are_not_published_as_numbers():
    """A negative duration must publish as None, never as a legitimate value."""
    published = uct._published_rows(_rep({16: dict(_V2_ALL_NEGATIVE)}))[2]
    for key in ("p50", "p90", "p99", "max", "mean", "wall_ms",
                "start_skew_ms"):
        assert published[key] is None, (key, published[key])
    # ...while a valid reading is still published unchanged.
    assert uct._published_rows(_rep())[2]["p50"] == pytest.approx(8.0)


def test_a_dynamic_class_name_carrying_the_secret_never_leaks(monkeypatch,
                                                              tmp_path,
                                                              capsys):
    """`type(name, (), {})` makes `__name__` caller-controlled.

    That is why a rejected row value contributes nothing but its field name —
    reporting `type(value).__name__` would have published this class name.
    """
    poison_cls = type(f"Cls_{_SECRET}", (), {})
    poisoned = _rep({16: {"p99": poison_cls()}})
    reasons = uct.repetition_hard_violations(poisoned, _LEVELS)
    assert reasons

    harness = _FakeHarness(reps=[poisoned] + [_rep() for _ in range(4)])
    code, out = _run(monkeypatch, tmp_path, lambda: harness)

    assert code == 1
    blob = out.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for channel, text in (("artifact", blob), ("stdout", captured.out),
                          ("stderr", captured.err),
                          ("reasons", " ".join(reasons))):
        assert _SECRET not in text, f"the secret leaked into {channel}"
        assert "Cls_" not in text, f"the class name leaked into {channel}"
    json.loads(blob)          # still valid JSON
    assert json.loads(blob)["verdict"] == "fail"


def test_partial_work_is_never_reported_as_fast_work(monkeypatch, tmp_path):
    """A sweep that dropped calls must be red however good its timings look."""
    fast_but_wrong = _rep({16: {"ledger_calls": 700, "p50": 0.001,
                                "p90": 0.0015, "p99": 0.002,
                                "calls_per_s": 99999.0}})
    harness = _FakeHarness(
        reps=[fast_but_wrong] + [_rep() for _ in range(4)])
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert any("LOSING calls" in r for r in data["failure_reasons"])
