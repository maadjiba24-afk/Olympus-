"""Deterministic regressions for the `sessionlog.sync` latency telemetry.

The absolute p50/p99 bounds for the LIVE per-turn journal path moved out of the
required suite into `scripts/sessionlog_sync_telemetry.py`, which the required
suite does not execute. Its evaluator is therefore pinned here, driven entirely
by synthetic rows and injected failures — no wall-clock, no filesystem
benchmark, no network — so the relocated contract cannot rot unnoticed.

Two properties carry the design and are hammered accordingly:

  * the evaluator FAILS CLOSED. An impossible measurement is not a fast one;
    it is a broken one, and every bound would otherwise be satisfied by it.
  * the artifact LEAKS NOTHING. It is published to a CI run, so a rejected
    value, an exception message, a repr, a path or a caller-controlled type
    name must never reach it.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import sessionlog_sync_telemetry as sst  # noqa: E402

_TURNS = sst.CONTRACT["turns"]
_SECRET = "sk_live_9f3a21_TAVILY_API_KEY"

# Python ints are arbitrary precision, so `math.isfinite(10**400)` raises
# rather than returning False. A validator that crashes is not fail-closed.
_HUGE = 10 ** 400

_MEASUREMENT_FIELDS = ("p50", "p90", "p99", "max", "mean",
                       "first_decile_mean", "last_decile_mean",
                       "growth_ratio", "slope_ms_per_turn",
                       "projected_ms_at_1k", "projected_ms_at_10k")


# --- hostile canaries --------------------------------------------------------
#
# Defined at module scope, ahead of every use, because a `parametrize` argument
# list is evaluated at import time. Each one passes an `isinstance` check while
# overriding whatever a naive validator or formatter would rely on.

class _LoudFailure(Exception):
    """Carries a credential-shaped message, as a real exception might."""


class _Unserializable:
    def __repr__(self):
        return f"<Unserializable {_SECRET}>"


class _SecretInt(int):
    """Passes `isinstance(x, int)`; overrides everything that could print it."""

    def __format__(self, spec):
        return _SECRET

    def __repr__(self):
        return _SECRET

    def __str__(self):
        return _SECRET

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
        return _SECRET

    def __repr__(self):
        return _SECRET

    def __str__(self):
        return _SECRET

    def __lt__(self, other):
        return True

    def __le__(self, other):
        return True

    def __gt__(self, other):
        return False

    def __ge__(self, other):
        return False


class _SecretStatus(str):
    """Answers `== "ok"` truthfully-looking while carrying the canary."""

    def __new__(cls):
        return super().__new__(cls, _SECRET)

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    def __hash__(self):
        return hash("ok")

    def __repr__(self):
        return _SECRET

    def __str__(self):
        return _SECRET


def _result(**over):
    """One structurally perfect, comfortably passing measurement.

    Derived fields are computed with the benchmark's own formulas so the
    baseline satisfies every identity; a test that wants an inconsistency pins
    the field itself.
    """
    head, tail = 2.0, 1.0
    k = max(5, _TURNS // 10)
    span = max(1.0, float(_TURNS) - k)
    slope = (tail - head) / span
    growth = max(0.0, slope)
    row = {
        "n": _TURNS, "turns": _TURNS, "cache": True,
        "records_verified": _TURNS, "journal_status": "ok",
        "journal_bytes": 40000,
        "seqs_dense": True, "chain_verified": True,
        "replayed_history_matches": True,
        "p50": 1.5, "p90": 2.5, "p99": 3.5, "max": 4.0, "mean": 1.8,
        "first_decile_mean": head, "last_decile_mean": tail,
        "growth_ratio": tail / head,
        "slope_ms_per_turn": slope,
        "projected_ms_at_1k": head + growth * 1000,
        "projected_ms_at_10k": head + growth * 10000,
        "conversation_id": "sync-deadbeef",
    }
    row.update(over)
    return row


# --- the preserved contract --------------------------------------------------

def test_the_thresholds_are_the_preserved_ones():
    """The moved numbers are the original numbers. Pinned so they cannot drift."""
    assert sst.CONTRACT["turns"] == 60
    assert sst.CONTRACT["cache"] is True
    assert sst.CONTRACT["p50_max_ms"] == 60.0
    assert sst.CONTRACT["p99_max_ms"] == 250.0
    assert sst.CONTRACT["benchmark"] == "sessionlog.sync"


def test_a_healthy_measurement_passes():
    assert sst.evaluate(_result()) == []


# --- thresholds: strict, and never masked by each other ----------------------

@pytest.mark.parametrize("over, needle", [
    ({"p50": 60.0, "p90": 60.0, "p99": 60.0, "max": 60.0}, "p50"),
    ({"p50": 88.0, "p90": 90.0, "p99": 95.0, "max": 99.0}, "p50"),
    ({"p99": 250.0, "max": 250.0}, "p99"),
    ({"p99": 284.429, "max": 300.0}, "p99"),   # the exact 2026-08-09 CI value
])
def test_threshold_equality_and_breach_fail(over, needle):
    reasons = sst.evaluate(_result(**over))
    assert reasons and any(needle in r for r in reasons), reasons


def test_a_p99_breach_is_not_hidden_by_a_healthy_p50():
    reasons = sst.evaluate(_result(p99=999.0, max=1000.0))
    assert any("p99" in r for r in reasons)
    both = sst.evaluate(_result(p50=100.0, p90=200.0, p99=999.0, max=1000.0))
    assert sum(1 for r in both if "p50" in r or "p99" in r) >= 2, both


def test_thresholds_are_applied_only_after_structural_validation():
    """A structurally broken result reports its defect, not a bound."""
    reasons = sst.evaluate(_result(p50=float("nan")))
    assert all("ms >=" not in r for r in reasons), reasons
    assert any("p50" in r and "withheld" in r for r in reasons), reasons


# --- fail closed on unusable measurements ------------------------------------

@pytest.mark.parametrize("field", [
    "n", "turns", "records_verified", "journal_bytes", "cache",
    "journal_status", "seqs_dense", "chain_verified",
    "replayed_history_matches", "p50", "p90", "p99", "max", "mean",
    "first_decile_mean", "last_decile_mean", "growth_ratio",
    "slope_ms_per_turn", "projected_ms_at_1k", "projected_ms_at_10k",
])
def test_a_missing_field_fails_closed(field):
    row = _result()
    del row[field]
    reasons = sst.evaluate(row)
    assert any(field in r for r in reasons), reasons


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["p50", "p90", "p99", "max", "mean",
                                   "first_decile_mean", "last_decile_mean",
                                   "growth_ratio", "slope_ms_per_turn",
                                   "projected_ms_at_1k",
                                   "projected_ms_at_10k"])
def test_non_finite_measurements_fail_closed(field, value):
    assert sst.evaluate(_result(**{field: value})), (field, value)


@pytest.mark.parametrize("field", ["p50", "p90", "p99", "max", "mean",
                                   "first_decile_mean", "last_decile_mean",
                                   "growth_ratio", "projected_ms_at_1k",
                                   "projected_ms_at_10k"])
@pytest.mark.parametrize("value", [0.0, 0, -1.0, -0.000001])
def test_non_positive_durations_fail_closed(field, value):
    """A latency at or below zero is impossible, not fast — `-1 < 250` is True."""
    assert sst.evaluate(_result(**{field: value})), (field, value)


def test_a_negative_slope_is_legitimate():
    """A decaying series is exactly the shape worth reporting, not rejecting."""
    assert sst.evaluate(_result()) == []
    assert _result()["slope_ms_per_turn"] < 0.0


@pytest.mark.parametrize("field", ["n", "turns", "records_verified",
                                   "journal_bytes"])
@pytest.mark.parametrize("value", [True, False, "60", 60.0, -1, None])
def test_boolean_and_non_integer_counts_fail_closed(field, value):
    assert sst.evaluate(_result(**{field: value})), (field, value)


@pytest.mark.parametrize("field", ["cache", "seqs_dense", "chain_verified",
                                   "replayed_history_matches"])
@pytest.mark.parametrize("value", [False, 1, "True", None, "yes"])
def test_flags_must_be_exactly_true(field, value):
    assert sst.evaluate(_result(**{field: value})), (field, value)


@pytest.mark.parametrize("value", ["absent", "torn_tail_truncated",
                                   "quarantined", "oversize", "OK", "", None])
def test_a_journal_that_is_not_ok_fails_closed(value):
    assert sst.evaluate(_result(journal_status=value)), value


@pytest.mark.parametrize("over, why", [
    ({"n": 59}, "sample count below the turn count"),
    ({"n": 61}, "sample count above the turn count"),
    ({"turns": 59}, "a different turn count is a different measurement"),
    ({"records_verified": 59}, "a partial journal"),
    ({"records_verified": 0}, "nothing journaled at all"),
    ({"journal_bytes": 0}, "an empty journal"),
])
def test_partial_and_miscounted_work_fails_closed(over, why):
    assert sst.evaluate(_result(**over)), why


def test_the_result_must_be_a_mapping():
    for bad in (None, [], "result", 7, object()):
        assert sst.evaluate(bad)


# --- internal consistency ----------------------------------------------------

@pytest.mark.parametrize("over, needle", [
    ({"p50": 3.0, "p90": 2.5}, "p50"),                 # p50 > p90
    ({"p90": 4.0, "p99": 3.5}, "p90"),                 # p90 > p99
    ({"p99": 5.0, "max": 4.0}, "p99"),                 # p99 > max
    ({"mean": 5.0, "max": 4.0}, "mean"),               # mean > max
    ({"first_decile_mean": 99.0}, "growth_ratio"),     # identities break
])
def test_inverted_percentiles_fail_closed(over, needle):
    reasons = sst.evaluate(_result(**over))
    assert reasons and any(needle in r for r in reasons), reasons


def test_the_whole_inverted_set_is_rejected():
    """Every value under its bound, yet the measurement is impossible."""
    assert sst.evaluate(_result(p50=1.0, p90=0.8, p99=0.5, max=0.4, mean=0.3))


@pytest.mark.parametrize("over, needle", [
    ({"growth_ratio": 0.123}, "growth_ratio"),
    ({"slope_ms_per_turn": 5.0}, "slope"),
    ({"projected_ms_at_1k": 12.5}, "projected_ms_at_1k"),
    ({"projected_ms_at_10k": 12.5}, "projected_ms_at_10k"),
])
def test_derived_identities_are_recomputed(over, needle):
    reasons = sst.evaluate(_result(**over))
    assert reasons and any(needle in r for r in reasons), reasons


def test_derived_identities_tolerate_only_float_noise():
    base = _result()
    for nudge in (1.0, 1 + 1e-13, 1 - 1e-13):
        assert sst.evaluate(
            _result(growth_ratio=base["growth_ratio"] * nudge)) == [], nudge
    assert sst.evaluate(_result(growth_ratio=base["growth_ratio"] * 1.001))


def test_a_growing_series_projects_upward_and_still_passes():
    """The healthy pre-D1-fix shape must not be rejected by the identities."""
    head, tail = 1.0, 3.0
    k = max(5, _TURNS // 10)
    span = max(1.0, float(_TURNS) - k)
    slope = (tail - head) / span
    assert sst.evaluate(_result(
        first_decile_mean=head, last_decile_mean=tail,
        growth_ratio=tail / head, slope_ms_per_turn=slope,
        projected_ms_at_1k=head + slope * 1000,
        projected_ms_at_10k=head + slope * 10000,
        p50=1.5, p90=2.5, p99=3.5, max=4.0, mean=1.8)) == []
    assert math.isclose(head + slope * 10000, 1.0 + (2.0 / 54) * 10000)


# --- publication is type-validated, not merely key-filtered ------------------

def test_a_structurally_invalid_result_publishes_nothing():
    """Not a partial measurement — no measurement.

    A field-by-field sanitized view of an invalid row reads as a measurement
    with gaps, when in fact nothing about it can be trusted.
    """
    assert sst._published(_result(p50=_SECRET, n=_SECRET,
                                  journal_bytes=True,
                                  slope_ms_per_turn=float("nan"))) is None


@pytest.mark.parametrize("over, why", [
    ({"p50": _SECRET}, "a secret-shaped measurement"),
    ({"n": _SECRET}, "a secret-shaped count"),
    ({"journal_bytes": True}, "a boolean count"),
    ({"slope_ms_per_turn": float("nan")}, "NaN"),
    ({"p99": float("inf")}, "infinity"),
    ({"p50": _SecretFloat(1.0)}, "a float subclass"),
    ({"n": _SecretInt(60)}, "an int subclass"),
    ({"journal_status": _SecretStatus()}, "a str subclass impersonating ok"),
    ({"n": _HUGE}, "an unrepresentable count"),
    ({"n": 1234567890123456789}, "a plausible-looking but wrong count"),
    ({"turns": 61}, "a turn-count mismatch"),
    ({"records_verified": 59}, "a record-count mismatch"),
    ({"journal_bytes": 0}, "an empty journal"),
    ({"cache": False}, "the uncached arm"),
    ({"chain_verified": False}, "an unverified chain"),
    ({"seqs_dense": False}, "a gapped sequence"),
    ({"replayed_history_matches": False}, "a history mismatch"),
    ({"p50": 1.0, "p90": 0.8, "p99": 0.5, "max": 0.4, "mean": 0.3},
     "an inverted but positive percentile set"),
    ({"projected_ms_at_1k": 12.5}, "an inconsistent positive projection"),
    ({"growth_ratio": 0.123}, "an inconsistent growth ratio"),
])
def test_published_returns_none_for_every_structurally_invalid_case(over, why):
    assert sst._published(_result(**over)) is None, why


def test_published_values_are_type_validated_within_a_valid_result():
    """The allowlist is still only a floor for results that DO publish."""
    published = sst._published(_result())
    assert published is not None
    assert type(published["p50"]) is float
    assert type(published["records_verified"]) is int
    assert _SECRET not in json.dumps(published)


def test_the_conversation_id_is_never_published():
    published = sst._published(_result())
    assert "conversation_id" not in published
    assert "sync-deadbeef" not in json.dumps(published)


def test_a_valid_measurement_is_published_unchanged():
    published = sst._published(_result())
    assert published["p50"] == pytest.approx(1.5)
    assert published["records_verified"] == _TURNS
    assert published["journal_status_ok"] is True


# --- the runner: red WITH evidence, never red in silence ---------------------





class _FakeHarness:
    def __init__(self, *, result=None, bench_error=None, cleanup_error=None):
        self._result = result
        self._bench_error = bench_error
        self._cleanup_error = cleanup_error
        self.cleanup_calls = 0

    def bench_sessionlog_sync(self, turns, cache):
        if self._bench_error is not None:
            raise self._bench_error
        return self._result if self._result is not None else _result()

    def cleanup(self):
        self.cleanup_calls += 1
        if self._cleanup_error is not None:
            raise self._cleanup_error


def _run(monkeypatch, tmp_path, loader):
    monkeypatch.setattr(sst, "_load_benchmark", loader)
    out = tmp_path / "nested" / "sync-telemetry.json"
    code = sst.main(["--out", str(out)])
    return code, out


def test_a_healthy_run_exits_zero_and_writes_a_passing_artifact(monkeypatch,
                                                                tmp_path):
    harness = _FakeHarness()
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "pass"
    assert data["failure_reasons"] == [] and data["failed_stages"] == []
    assert data["measurement"]["records_verified"] == _TURNS
    assert harness.cleanup_calls == 1


@pytest.mark.parametrize("stage, loader_factory", [
    ("setup", lambda: (lambda: (_ for _ in ()).throw(_LoudFailure(_SECRET)))),
    ("benchmark", lambda: (lambda: _FakeHarness(
        bench_error=_LoudFailure(_SECRET)))),
    ("cleanup", lambda: (lambda: _FakeHarness(
        cleanup_error=_LoudFailure(_SECRET)))),
])
def test_operational_failures_still_write_a_red_artifact(stage,
                                                         loader_factory,
                                                         monkeypatch,
                                                         tmp_path):
    code, out = _run(monkeypatch, tmp_path, loader_factory())
    assert code == 1
    assert out.is_file(), f"no artifact was written for a failed {stage}"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any(s["stage"] == stage for s in data["failed_stages"]), data
    assert all(s["stage"] in sst.STAGES for s in data["failed_stages"])
    assert all(isinstance(s["error_count"], int)
               for s in data["failed_stages"])


def test_a_threshold_breach_writes_the_artifact_before_exiting(monkeypatch,
                                                               tmp_path):
    harness = _FakeHarness(result=_result(p99=284.429, max=300.0))
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any("p99" in r for r in data["failure_reasons"])
    # The measurement is still published — a red run is when it matters most.
    assert data["measurement"]["p99"] == pytest.approx(284.429)


def test_a_serialization_failure_still_writes_a_minimal_red_artifact(
        monkeypatch, tmp_path):
    """`allow_nan=False` plus an unserialisable payload must not lose the run."""
    original = sst._published

    def poisoned(result):
        published = original(result)
        published["poison"] = _Unserializable()
        return published

    monkeypatch.setattr(sst, "_published", poisoned)
    code, out = _run(monkeypatch, tmp_path, lambda: _FakeHarness())
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert data["measurement_withheld"] is True
    assert any(s["stage"] == "serialization" for s in data["failed_stages"])
    assert _SECRET not in blob and "Unserializable" not in blob


def test_the_artifact_is_valid_json_without_nan(monkeypatch, tmp_path):
    harness = _FakeHarness(result=_result(p50=float("nan"),
                                          mean=float("inf")))
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    for token in ("NaN", "Infinity", "-Infinity"):
        assert token not in blob, f"{token} is not valid JSON"
    json.loads(blob)


@pytest.mark.parametrize("loader_factory, expected_code", [
    (lambda: (lambda: _FakeHarness()), 0),
    (lambda: (lambda: _FakeHarness(result=_result(p99=284.429, max=300.0))), 1),
    (lambda: (lambda: _FakeHarness(cleanup_error=_LoudFailure(_SECRET))), 1),
])
def test_stdout_is_exactly_the_artifact_and_stderr_is_empty(
        loader_factory, expected_code, monkeypatch, tmp_path, capsys):
    """Byte-for-byte equality, on green AND on handled-failure runs.

    Substring containment was too weak: it permitted the extra `verdict:` and
    `FAIL:` lines the runner used to print, one of which echoed the
    caller-supplied `--out` pathname.
    """
    code, out = _run(monkeypatch, tmp_path, loader_factory())
    assert code == expected_code
    captured = capsys.readouterr()
    artifact = out.read_text(encoding="utf-8")
    assert captured.out == artifact, "stdout is not exactly the artifact bytes"
    assert artifact.endswith("}\n") and not artifact.endswith("}\n\n")
    assert captured.err == "", "a handled failure must not write to stderr"


def test_the_output_pathname_never_appears_on_any_channel(monkeypatch,
                                                          tmp_path, capsys):
    """`--out` is caller-supplied; echoing it publishes whatever it contains."""
    out = tmp_path / f"dir_{_SECRET}" / f"{_SECRET}.json"
    monkeypatch.setattr(sst, "_load_benchmark", lambda: _FakeHarness())
    code = sst.main(["--out", str(out)])

    assert code == 0
    assert out.is_file()
    captured = capsys.readouterr()
    assert _SECRET not in captured.out, "the pathname leaked into stdout"
    assert _SECRET not in captured.err, "the pathname leaked into stderr"
    assert captured.err == ""
    assert _SECRET not in out.read_text(encoding="utf-8")


def test_a_publication_failure_still_writes_a_red_artifact(monkeypatch,
                                                           tmp_path, capsys):
    """`_published` walks caller data and can raise; that must be contained.

    A hostile `__float__`/`__eq__` can raise from inside publication, after
    evaluation has already succeeded. Unprotected, that would escape as a
    traceback carrying an exception message no validator ever inspected.
    """
    boom = type(f"PubErr_{_SECRET}", (Exception,), {})

    def exploding_publish(_result):
        raise boom(_SECRET)

    monkeypatch.setattr(sst, "_published", exploding_publish)
    harness = _FakeHarness()
    code, out = _run(monkeypatch, tmp_path, lambda: harness)

    assert code == 1
    assert out.is_file(), "no artifact was written for a failed publication"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any(s["stage"] == "publication" for s in data["failed_stages"]), data
    assert data["measurement"] is None
    assert harness.cleanup_calls == 1, "cleanup must still run"

    blob = out.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for channel, text in (("artifact", blob), ("stdout", captured.out),
                          ("stderr", captured.err)):
        assert _SECRET not in text, f"the secret leaked into {channel}"
        assert "PubErr_" not in text, f"the class name leaked into {channel}"


# --- numeric subclasses are not safe numbers ---------------------------------





@pytest.mark.parametrize("field", ["n", "turns", "records_verified",
                                   "journal_bytes"])
def test_int_subclasses_are_rejected(field):
    """`isinstance` is not enough — a subclass lies about every comparison."""
    reasons = sst.evaluate(_result(**{field: _SecretInt(60)}))
    assert reasons, f"{field} accepted an int subclass"
    assert _SECRET not in " ".join(reasons)


@pytest.mark.parametrize("field", ["p50", "p90", "p99", "max", "mean",
                                   "first_decile_mean", "last_decile_mean",
                                   "growth_ratio", "slope_ms_per_turn",
                                   "projected_ms_at_1k",
                                   "projected_ms_at_10k"])
def test_float_subclasses_are_rejected(field):
    reasons = sst.evaluate(_result(**{field: _SecretFloat(1.0)}))
    assert reasons, f"{field} accepted a float subclass"
    assert _SECRET not in " ".join(reasons)


def test_a_comparison_lying_subclass_cannot_satisfy_a_threshold():
    """`__lt__` returning True would otherwise walk straight past both bounds."""
    liar = _SecretFloat(9999.0)
    assert liar < 1.0 and liar < 60.0        # it really does lie
    reasons = sst.evaluate(_result(p50=liar, p99=liar))
    assert reasons
    assert _SECRET not in " ".join(reasons)


# --- an oversized int must be rejected, not raise ----------------------------
#
# Python ints are arbitrary precision, so `math.isfinite(10**400)` raises
# OverflowError. A validator that crashes is not fail-closed: it is an
# unhandled traceback that reaches the CI log carrying a value nothing
# sanitized.

def test_the_validators_never_raise_on_an_oversized_int():
    """The primitives themselves must return False, not explode."""
    assert sst._finite(_HUGE) is False
    assert sst._finite(-_HUGE) is False
    assert sst._positive(_HUGE) is False
    # A count of unrepresentable magnitude is not a measurement either. Found
    # by the leak matrix: `10**400` is an exact non-negative int, so the
    # type-and-sign test alone let it through to publication.
    assert sst._count(_HUGE) is False
    assert sst._count(60) is True and sst._count(0) is True


@pytest.mark.parametrize("field", ["n", "turns", "records_verified",
                                   "journal_bytes"])
def test_an_oversized_count_is_rejected_and_publishes_nothing(field):
    reasons = sst.evaluate(_result(**{field: _HUGE}))
    assert reasons, f"{field} accepted 10**400"
    assert "0000000000" not in " ".join(reasons)
    assert sst._published(_result(**{field: _HUGE})) is None


@pytest.mark.parametrize("field", _MEASUREMENT_FIELDS)
def test_an_oversized_int_is_a_structural_violation(field):
    reasons = sst.evaluate(_result(**{field: _HUGE}))
    assert reasons, f"{field} accepted 10**400"
    assert any(field in r and "withheld" in r for r in reasons), reasons
    # and no 400-digit number was rendered into the reason text
    assert "0000000000" not in " ".join(reasons)


@pytest.mark.parametrize("field", _MEASUREMENT_FIELDS)
def test_an_oversized_int_publishes_no_measurement(field):
    assert sst._published(_result(**{field: _HUGE})) is None


def test_counts_are_bounded_by_the_platform_integer_range():
    """`sys.maxsize` is the principled ceiling, not an invented one.

    Every count here is a container length or a filesystem size; neither can
    exceed the process's addressable integer range.
    """
    assert sst._count(sys.maxsize) is True
    assert sst._count(sys.maxsize + 1) is False
    assert sst._count(-1) is False
    assert sst._count(0) is True and sst._count(60) is True


@pytest.mark.parametrize("field", ["n", "turns", "records_verified"])
def test_a_plausible_but_wrong_count_is_rejected_and_never_rendered(
        field, monkeypatch, tmp_path, capsys):
    """In range, so it passes `_count` — and then fails on the contract."""
    wrong = 1234567890123456789
    assert wrong < sys.maxsize

    reasons = sst.evaluate(_result(**{field: wrong}))
    assert reasons, f"{field}={wrong} was accepted"
    assert str(wrong) not in " ".join(reasons)
    assert sst._published(_result(**{field: wrong})) is None

    harness = _FakeHarness(result=_result(**{field: wrong}))
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert json.loads(blob)["measurement"] is None
    for channel, text in (("artifact", blob), ("stdout", captured.out),
                          ("stderr", captured.err)):
        assert str(wrong) not in text, f"the count leaked into {channel}"
    assert captured.err == ""


@pytest.mark.parametrize("over, why", [
    ({"journal_bytes": 0}, "an empty journal"),
    ({"p50": 1.0, "p90": 0.8, "p99": 0.5, "max": 0.4, "mean": 0.3},
     "an inverted but positive percentile set"),
    ({"projected_ms_at_1k": 12.5}, "an inconsistent positive projection"),
])
def test_a_structurally_invalid_run_publishes_measurement_null(
        over, why, monkeypatch, tmp_path, capsys):
    harness = _FakeHarness(result=_result(**over))
    code, out = _run(monkeypatch, tmp_path, lambda: harness)

    assert code == 1, why
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["measurement"] is None, (why, data["measurement"])
    assert data["verdict"] == "fail"
    assert data["failed_stages"] == [], "a rejected row is not a crash"
    captured = capsys.readouterr()
    assert captured.out == blob, "stdout must remain exactly the artifact"
    assert captured.err == ""


def test_a_threshold_breach_still_publishes_the_whole_measurement(
        monkeypatch, tmp_path, capsys):
    """A valid measurement that exceeded a limit is exactly what to publish."""
    harness = _FakeHarness(result=_result(p99=284.429, max=300.0))
    code, out = _run(monkeypatch, tmp_path, lambda: harness)

    assert code == 1
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    measurement = data["measurement"]
    assert measurement is not None, "an operator must be able to read p99"
    assert measurement["p99"] == pytest.approx(284.429)
    assert measurement["p50"] == pytest.approx(1.5)
    assert measurement["records_verified"] == _TURNS
    assert measurement["journal_status_ok"] is True
    assert any("p99" in r for r in data["failure_reasons"])
    captured = capsys.readouterr()
    assert captured.out == blob
    assert captured.err == ""


def test_an_oversized_int_run_is_red_with_a_clean_artifact(monkeypatch,
                                                           tmp_path, capsys):
    harness = _FakeHarness(result=_result(p99=_HUGE, mean=_HUGE))
    code, out = _run(monkeypatch, tmp_path, lambda: harness)

    assert code == 1
    assert out.is_file()
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert data["measurement"] is None, (
        "an oversized value makes the whole result unusable, so nothing about "
        "it may be published as a measurement")
    assert harness.cleanup_calls == 1, "cleanup must still run"
    # No stage failed — this is a rejected measurement, not a crash.
    assert data["failed_stages"] == [], data["failed_stages"]
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "0000000000" not in blob, "the raw oversized value was published"


# --- a str subclass must not be able to impersonate "ok" ---------------------



def test_the_journal_status_helper_demands_an_exact_builtin_string():
    assert sst._journal_ok("ok") is True
    assert sst._journal_ok(_SecretStatus()) is False
    for bad in ("OK", "", "absent", None, 1, True, ["ok"], object()):
        assert sst._journal_ok(bad) is False, bad


def test_a_status_subclass_impersonating_ok_is_rejected():
    impostor = _SecretStatus()
    assert impostor == "ok", "the impostor does not actually lie; test is void"

    reasons = sst.evaluate(_result(journal_status=impostor))
    assert reasons, "a str subclass impersonated a verified journal"
    assert any("journal_status" in r for r in reasons), reasons
    assert _SECRET not in " ".join(reasons)

    assert sst._published(_result(journal_status=impostor)) is None


def test_a_status_subclass_run_is_red_and_leaks_nothing(monkeypatch, tmp_path,
                                                        capsys):
    harness = _FakeHarness(result=_result(journal_status=_SecretStatus()))
    code, out = _run(monkeypatch, tmp_path, lambda: harness)

    assert code == 1
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert data["measurement"] is None
    captured = capsys.readouterr()
    for channel, text in (("artifact", blob), ("stdout", captured.out),
                          ("stderr", captured.err)):
        assert _SECRET not in text, f"the canary leaked into {channel}"
        assert "_SecretStatus" not in text, f"the class name leaked into {channel}"
    assert captured.err == ""


def test_a_subclass_never_reaches_any_channel(monkeypatch, tmp_path, capsys):
    harness = _FakeHarness(result=_result(p99=_SecretFloat(1.0),
                                          n=_SecretInt(60)))
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for channel, text in (("artifact", blob), ("stdout", captured.out),
                          ("stderr", captured.err)):
        assert _SECRET not in text, f"the secret leaked into {channel}"
    json.loads(blob)


# --- nothing sensitive may reach any channel ---------------------------------

# Fields where a boolean is INVALID (counts and measurements) versus the four
# flags where `True` is the only correct value. Pairing `True` with a flag
# would assert that a valid result must be rejected, which is false — the leak
# matrix must poison each field with shapes that are actually wrong for it.
_FLAG_LEAK_FIELDS = ("cache", "seqs_dense", "chain_verified",
                     "replayed_history_matches")
_VALUE_LEAK_FIELDS = ("p50", "p90", "p99", "max", "mean", "first_decile_mean",
                      "last_decile_mean", "growth_ratio", "slope_ms_per_turn",
                      "projected_ms_at_1k", "projected_ms_at_10k",
                      "n", "turns", "records_verified", "journal_bytes",
                      "journal_status")
_LEAK_FIELDS = _VALUE_LEAK_FIELDS + _FLAG_LEAK_FIELDS

# (shape-name, value) — the NAME is what pytest prints. A pytest node id lands
# in CI logs and JUnit XML, so a raw secret-shaped value must never become one;
# an earlier revision derived ids from the values themselves and put the canary
# straight into the test id.
_LEAK_SHAPES = (
    ("secret_str", _SECRET),
    ("secret_in_list", [_SECRET]),
    ("secret_in_dict", {"env": _SECRET}),
    ("secret_repr_object", _Unserializable()),
    ("nan", float("nan")),
    ("inf", float("inf")),
    ("secret_int_subclass", _SecretInt(60)),
    ("secret_float_subclass", _SecretFloat(1.0)),
    ("secret_str_subclass", _SecretStatus()),
    ("oversized_int", 10 ** 400),
)


def _leak_cases():
    """(id, field, value) triples where the value is genuinely wrong."""
    cases = [(f"{field}-{shape}", field, value)
             for field in _LEAK_FIELDS for shape, value in _LEAK_SHAPES]
    # A boolean is a rejected shape only where a number is expected.
    cases += [(f"{field}-bool", field, True) for field in _VALUE_LEAK_FIELDS]
    return cases


_LEAK_CASES = _leak_cases()


@pytest.mark.parametrize(
    "field, value",
    [(field, value) for _id, field, value in _LEAK_CASES],
    ids=[_id for _id, _f, _v in _LEAK_CASES])
def test_a_rejected_value_never_reaches_any_channel(field, value, monkeypatch,
                                                    tmp_path, capsys):
    """Every field x every rejected shape, on every channel a CI run reads."""
    reasons = sst.evaluate(_result(**{field: value}))
    assert reasons, f"{field}={type(value).__name__} must be rejected"

    harness = _FakeHarness(result=_result(**{field: value}))
    code, out = _run(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for channel, text in (("artifact", blob), ("stdout", captured.out),
                          ("stderr", captured.err),
                          ("reasons", " ".join(reasons))):
        assert _SECRET not in text, f"the secret leaked into {channel}"
        assert "<Unserializable" not in text, f"a repr leaked into {channel}"
    json.loads(blob)


def test_a_dynamic_class_name_carrying_the_secret_never_leaks(monkeypatch,
                                                              tmp_path,
                                                              capsys):
    """`type(name, (), {})` makes `__name__` caller-controlled.

    That is why a rejected value contributes nothing but its field name, and
    why an operational failure publishes a CLOSED stage name rather than an
    exception type.
    """
    poison_cls = type(f"Cls_{_SECRET}", (), {})
    reasons = sst.evaluate(_result(p99=poison_cls()))
    assert reasons

    for loader in (lambda: _FakeHarness(result=_result(p99=poison_cls())),
                   lambda: _FakeHarness(bench_error=poison_cls
                                        and _LoudFailure(_SECRET))):
        code, out = _run(monkeypatch, tmp_path, loader)
        assert code == 1
        blob = out.read_text(encoding="utf-8")
        captured = capsys.readouterr()
        for channel, text in (("artifact", blob), ("stdout", captured.out),
                              ("stderr", captured.err),
                              ("reasons", " ".join(reasons))):
            assert _SECRET not in text, f"the secret leaked into {channel}"
            assert "Cls_" not in text, f"the class name leaked into {channel}"
        json.loads(blob)


def test_failure_stages_are_a_closed_vocabulary():
    """Only fixed stage names may ever be published."""
    assert set(sst.STAGES) == {"setup", "benchmark", "evaluation",
                               "publication", "cleanup", "serialization"}
    for stage in sst.STAGES:
        assert stage.isascii() and stage.islower()
