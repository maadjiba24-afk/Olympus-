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





# "no result supplied" and "supplied None on purpose" are different things, and
# `result=None` used to mean the first. That made the second untestable: a
# benchmark that silently returns `None` was exactly the false-green path the
# runner had to be hardened against, and no fake could express it.
_UNSET = object()


class _FakeHarness:
    def __init__(self, *, result=_UNSET, bench_error=None, cleanup_error=None):
        self._result = result
        self._bench_error = bench_error
        self._cleanup_error = cleanup_error
        self.cleanup_calls = 0

    def bench_sessionlog_sync(self, turns, cache):
        if self._bench_error is not None:
            raise self._bench_error
        return _result() if self._result is _UNSET else self._result

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

    def poisoned(result, contract=None):
        published = original(result, contract)
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


def test_the_benchmark_and_measurement_vocabularies_are_closed():
    assert set(sst.BENCHMARKS) == {"sessionlog.sync",
                                   "sessionlog.sync depth scaling"}
    assert set(sst.MEASUREMENTS) == {"latency", "depth"}


# =============================================================================
# The SECOND relocated contract: D1 depth scaling
# =============================================================================
#
# `test_sync_depth_scaling_is_sharply_reduced_but_not_eliminated` asserted
# three wall-clock bounds on the same production path and was left in required
# pytest by the first pass of this correction. Push run 31427036314 failed the
# Windows 3.12 leg on it — cached slope 18.320 us/turn, uncached 35.444,
# reduction 1.935x against a required 5x, paired speedup 1.638x against a
# required 2x — on a branch that changed no production code.
#
# The bounds are unchanged and the strict comparisons are unchanged; only the
# place a breach blocks has moved.

_DEPTHS = sst.DEPTH_CONTRACT["depths"]
_LO, _HI = min(_DEPTHS), max(_DEPTHS)
_SAMPLES = sst.DEPTH_CONTRACT["samples"]


def _depth_row(depth, over=None):
    """One coherent per-depth row.

    Overrides arrive as a dict rather than `**kwargs` so a test can poison
    `depth` itself without colliding with this function's own parameter.
    """
    on = 1.0 + depth * 0.001
    off = 1.0 + depth * 0.040
    row = {
        "depth": depth,
        "on_p50_ms": on,
        "off_p50_ms": off,
        "on_samples": _SAMPLES,
        "off_samples": _SAMPLES,
        "paired_samples": _SAMPLES,
        "order_counts": {"on_first": _SAMPLES // 2,
                         "off_first": _SAMPLES - _SAMPLES // 2},
        "paired_speedup": off / on,
        "paired_ratio_min": off / on * 0.9,
        "paired_ratio_max": off / on * 1.1,
    }
    row.update(over or {})
    return row


def _depth_result(*, rows=None, **over):
    """A structurally perfect, comfortably passing depth measurement.

    Derived fields are computed with the benchmark's own formulas so the
    baseline satisfies every identity; a test that wants an inconsistency pins
    the field itself.
    """
    per_depth = rows if rows is not None else {d: _depth_row(d)
                                               for d in (_LO, _HI)}
    span = max(1, _HI - _LO)
    out = {"depths": list(_DEPTHS), "samples": _SAMPLES, "paired": True,
           "per_depth": per_depth}
    for arm in ("on", "off"):
        out[f"{arm}_ms_at_{_LO}"] = per_depth[_LO][f"{arm}_p50_ms"]
        out[f"{arm}_ms_at_{_HI}"] = per_depth[_HI][f"{arm}_p50_ms"]
        out[f"{arm}_us_per_turn_of_depth"] = (
            (out[f"{arm}_ms_at_{_HI}"] - out[f"{arm}_ms_at_{_LO}"])
            / span * 1000.0)
    on_slope = out["on_us_per_turn_of_depth"]
    off_slope = out["off_us_per_turn_of_depth"]
    out["slope_reduction_x"] = (off_slope / on_slope) if on_slope > 0 else None
    deep = per_depth[_HI]
    # A test may pin a row value to something impossible (0.0, a subclass, a
    # string) to exercise a rejection; the helper must still build a dict
    # rather than raising, or the case under test never reaches the evaluator.
    on_deep = deep["on_p50_ms"]
    out["speedup_at_max_depth"] = (
        deep["off_p50_ms"] / on_deep
        if type(on_deep) in (int, float) and on_deep else 0.0)
    out["paired_speedup_at_max_depth"] = deep["paired_speedup"]
    out["paired_samples_at_max_depth"] = deep["paired_samples"]
    out["order_counts_at_max_depth"] = deep["order_counts"]
    out.update(over)
    return out


def _set_paired_speedup(result, value):
    """Set the deepest row's paired speedup, keeping its ratio range coherent.

    The median must lie inside the range the per-pair ratios spanned. A test
    that lowers the median to exercise the 2.0 bound must move the range with
    it, or it builds an arithmetically impossible row and trips the coherence
    check instead of the threshold it meant to test.
    """
    deep = result["per_depth"][_HI]
    deep["paired_speedup"] = value
    deep["paired_ratio_min"] = value * 0.9
    deep["paired_ratio_max"] = value * 1.1
    result["paired_speedup_at_max_depth"] = value
    return result


def test_the_depth_parameters_and_thresholds_are_the_preserved_ones():
    c = sst.DEPTH_CONTRACT
    assert c["depths"] == (100, 900)
    assert c["samples"] == 12
    assert c["min_uncached_slope_us_per_turn"] == 5.0
    assert c["slope_reduction_factor"] == 5.0
    assert c["min_paired_speedup_at_max_depth"] == 2.0
    assert c["benchmark"] == "sessionlog.sync depth scaling"


def test_a_healthy_depth_measurement_passes():
    assert sst.evaluate_depth(_depth_result()) == []


# --- the three preserved bounds, with their original strict comparisons ------

def test_the_reported_ci_failure_shape_is_rejected():
    """The exact numbers from push run 31427036314."""
    result = _depth_result(on_us_per_turn_of_depth=18.32037499994499,
                           off_us_per_turn_of_depth=35.444375000110995,
                           slope_reduction_x=1.9346970244996309)
    # Recompute the medians so the slope identity still holds.
    span = max(1, _HI - _LO)
    for arm, slope in (("on", 18.32037499994499),
                       ("off", 35.444375000110995)):
        result[f"{arm}_ms_at_{_HI}"] = (result[f"{arm}_ms_at_{_LO}"]
                                        + slope * span / 1000.0)
        result["per_depth"][_HI][f"{arm}_p50_ms"] = result[f"{arm}_ms_at_{_HI}"]
    deep = result["per_depth"][_HI]
    deep["paired_speedup"] = 1.6382275114582876
    deep["paired_ratio_min"] = 1.5
    deep["paired_ratio_max"] = 1.8
    result["paired_speedup_at_max_depth"] = 1.6382275114582876
    result["speedup_at_max_depth"] = deep["off_p50_ms"] / deep["on_p50_ms"]

    reasons = sst.evaluate_depth(result)
    assert any("cached slope" in r for r in reasons), reasons
    assert any("paired speedup" in r for r in reasons), reasons


@pytest.mark.parametrize("off_slope, why", [
    (5.0, "exactly on the floor must fail, as `> 5.0` did"),
    (4.999999, "below the floor"),
    (0.0, "a flat pre-fix arm reproduces nothing"),
])
def test_the_uncached_slope_floor_is_strict(off_slope, why):
    span = max(1, _HI - _LO)
    result = _depth_result()
    result["off_ms_at_900"] = (result[f"off_ms_at_{_LO}"]
                               + off_slope * span / 1000.0)
    result["per_depth"][_HI]["off_p50_ms"] = result["off_ms_at_900"]
    result["off_us_per_turn_of_depth"] = off_slope
    result["speedup_at_max_depth"] = (result["per_depth"][_HI]["off_p50_ms"]
                                      / result["per_depth"][_HI]["on_p50_ms"])
    result["slope_reduction_x"] = (
        off_slope / result["on_us_per_turn_of_depth"]
        if result["on_us_per_turn_of_depth"] > 0 else None)
    reasons = sst.evaluate_depth(result)
    assert any("uncached slope" in r for r in reasons), (why, reasons)


def test_the_slope_reduction_factor_is_strict():
    """`on_slope < off_slope / 5.0` — equality must fail."""
    result = _depth_result()
    off_slope = result["off_us_per_turn_of_depth"]
    exactly = off_slope / sst.DEPTH_CONTRACT["slope_reduction_factor"]
    span = max(1, _HI - _LO)
    result["on_ms_at_900"] = (result[f"on_ms_at_{_LO}"]
                              + exactly * span / 1000.0)
    result["per_depth"][_HI]["on_p50_ms"] = result["on_ms_at_900"]
    result["on_us_per_turn_of_depth"] = exactly
    result["slope_reduction_x"] = off_slope / exactly
    deep = result["per_depth"][_HI]
    result["speedup_at_max_depth"] = deep["off_p50_ms"] / deep["on_p50_ms"]
    deep["paired_speedup"] = result["paired_speedup_at_max_depth"]
    reasons = sst.evaluate_depth(result)
    assert any("cached slope" in r for r in reasons), reasons


@pytest.mark.parametrize("speedup, why", [
    (2.0, "exactly on the bound must fail, as `> 2.0` did"),
    (1.9999999, "below the bound"),
    (1.6382275114582876, "the exact value from run 31427036314"),
])
def test_the_paired_speedup_bound_is_strict(speedup, why):
    result = _set_paired_speedup(_depth_result(), speedup)
    reasons = sst.evaluate_depth(result)
    assert any("paired speedup" in r for r in reasons), (why, reasons)


def test_all_three_depth_breaches_are_collected_together():
    """One breach must never mask another."""
    span = max(1, _HI - _LO)
    result = _depth_result()
    # off slope exactly on its floor, on slope not 5x gentler, speedup at bound
    result["off_us_per_turn_of_depth"] = 5.0
    result["on_us_per_turn_of_depth"] = 5.0
    for arm in ("on", "off"):
        result[f"{arm}_ms_at_{_HI}"] = (result[f"{arm}_ms_at_{_LO}"]
                                        + 5.0 * span / 1000.0)
        result["per_depth"][_HI][f"{arm}_p50_ms"] = result[f"{arm}_ms_at_{_HI}"]
    result["slope_reduction_x"] = 1.0
    deep = result["per_depth"][_HI]
    result["speedup_at_max_depth"] = deep["off_p50_ms"] / deep["on_p50_ms"]
    _set_paired_speedup(result, 2.0)

    reasons = sst.evaluate_depth(result)
    assert any("uncached slope" in r for r in reasons), reasons
    assert any("cached slope" in r for r in reasons), reasons
    assert any("paired speedup" in r for r in reasons), reasons
    assert len(reasons) >= 3, reasons


# --- structural validation runs first and fails closed -----------------------

def test_depth_thresholds_are_applied_only_after_structural_validation():
    reasons = sst.evaluate_depth(_depth_result(paired=False))
    assert any("paired" in r for r in reasons), reasons
    assert all("slope" not in r and "speedup" not in r for r in reasons), reasons


@pytest.mark.parametrize("over, why", [
    ({"paired": False}, "an unpaired A/B"),
    ({"paired": 1}, "a truthy non-True paired flag"),
    ({"depths": [100, 800]}, "the wrong depths"),
    ({"depths": [900, 100]}, "depths out of order"),
    ({"depths": _SECRET}, "a secret-shaped depths value"),
    ({"samples": 11}, "the wrong sample count"),
    ({"samples": True}, "a boolean sample count"),
    ({"samples": _HUGE}, "an unrepresentable sample count"),
    ({"paired_samples_at_max_depth": 0}, "no paired observations"),
    ({"on_us_per_turn_of_depth": float("nan")}, "a NaN slope"),
    ({"off_us_per_turn_of_depth": float("inf")}, "an infinite slope"),
    ({"paired_speedup_at_max_depth": _SecretFloat(9.0)}, "a float subclass"),
    ({"on_ms_at_100": 0.0}, "a non-positive median"),
    ({"on_ms_at_100": -1.0}, "a negative median"),
    ({"per_depth": "not a mapping"}, "a malformed per_depth"),
    ({"slope_reduction_x": 1.0}, "an inconsistent reduction"),
    ({"speedup_at_max_depth": 99.0}, "an inconsistent unpaired speedup"),
    ({"paired_speedup_at_max_depth": 99.0}, "an inconsistent paired speedup"),
    ({"on_us_per_turn_of_depth": 999.0}, "a slope that is not the fitted one"),
])
def test_depth_structural_invalidity_fails_closed(over, why):
    assert sst.evaluate_depth(_depth_result(**over)), why


@pytest.mark.parametrize("over, why", [
    ({"on_samples": 11}, "an arm sample-count mismatch"),
    ({"paired_samples": 11}, "a paired sample-count mismatch"),
    ({"order_counts": {"on_first": 12, "off_first": 0}}, "one ordering only"),
    ({"order_counts": {"on_first": 0, "off_first": 12}}, "the other ordering"),
    ({"order_counts": {"on_first": 10, "off_first": 2}}, "unbalanced ordering"),
    ({"order_counts": {"on_first": 6, "off_first": 5}}, "counts not summing"),
    ({"order_counts": {"on_first": True, "off_first": 6}}, "a boolean count"),
    ({"order_counts": "not a mapping"}, "a malformed order_counts"),
    ({"on_p50_ms": 0.0}, "a non-positive arm median"),
    ({"paired_speedup": -1.0}, "a negative paired speedup"),
])
def test_depth_per_row_incoherence_fails_closed(over, why):
    rows = {_LO: _depth_row(_LO), _HI: _depth_row(_HI, over)}
    assert sst.evaluate_depth(_depth_result(rows=rows)), why


def test_a_balanced_odd_sample_count_is_accepted():
    """`|on_first - off_first| <= 1` — an odd count cannot split evenly.

    A genuine 11-sample case against a matching contract. An earlier revision
    named itself "odd" while using the contract's 12 samples and a 6/6 split,
    which is even and exercises none of the tolerance this rule exists for.
    """
    samples = 11
    assert samples % 2 == 1, "the point of this test is an odd count"
    contract = dict(sst.DEPTH_CONTRACT, samples=samples)

    rows = {d: _depth_row(d, {"on_samples": samples, "off_samples": samples,
                              "paired_samples": samples,
                              "order_counts": {"on_first": 5,
                                               "off_first": 6}})
            for d in (_LO, _HI)}
    result = _depth_result(rows=rows, samples=samples)
    assert result["order_counts_at_max_depth"] == {"on_first": 5,
                                                   "off_first": 6}
    assert sst.evaluate_depth(result, contract) == []

    # ...and a two-apart split at the same odd count is still rejected.
    unbalanced = {d: _depth_row(d, {"on_samples": samples, "off_samples": samples,
                                    "paired_samples": samples,
                                    "order_counts": {"on_first": 4,
                                                     "off_first": 7}})
                  for d in (_LO, _HI)}
    assert sst.evaluate_depth(_depth_result(rows=unbalanced, samples=samples),
                              contract)


# --- the paired-ratio identity ------------------------------------------------
#
# `paired_speedup` is the MEDIAN of the per-pair ratios, so it must lie inside
# the range those ratios spanned. A median outside its own min/max is
# arithmetically impossible, and without this it would still be compared
# against the 2.0 bound as though it were a real statistic.

@pytest.mark.parametrize("over, why", [
    ({"paired_ratio_min": 25.0}, "paired_ratio_min above paired_speedup"),
    ({"paired_ratio_max": 0.5}, "paired_speedup above paired_ratio_max"),
    ({"paired_ratio_min": 8.0, "paired_ratio_max": 2.0},
     "paired_ratio_min above paired_ratio_max"),
    ({"paired_ratio_min": 5.0, "paired_ratio_max": 6.0,
      "paired_speedup": 1.5}, "a median below a coherent range"),
    ({"paired_ratio_min": 1.0, "paired_ratio_max": 1.2,
      "paired_speedup": 9.0}, "a median above a coherent range"),
])
def test_contradictory_paired_ratios_fail_closed(over, why):
    rows = {_LO: _depth_row(_LO), _HI: _depth_row(_HI, over)}
    result = _depth_result(rows=rows)
    reasons = sst.evaluate_depth(result)
    assert reasons, why
    assert any("paired_ratio" in r or "paired_speedup" in r
               for r in reasons), reasons
    # every value involved is positive and finite — this is a coherence
    # failure, not a domain failure
    for key in ("paired_ratio_min", "paired_ratio_max", "paired_speedup"):
        assert rows[_HI][key] > 0
    assert sst._published_depth(result) is None, why


def test_the_paired_ratio_identity_accepts_the_boundary():
    """min == median == max is coherent and must pass."""
    rows = {_LO: _depth_row(_LO),
            _HI: _depth_row(_HI, {"paired_ratio_min": 4.0, "paired_speedup": 4.0,
                             "paired_ratio_max": 4.0})}
    result = _depth_result(rows=rows)
    result["paired_speedup_at_max_depth"] = 4.0
    assert sst.evaluate_depth(result) == []


# --- per-row depth identity ---------------------------------------------------

@pytest.mark.parametrize("depth_value, why", [
    (_LO, "a row filed under the wrong key"),
    (901, "a depth that matches neither key"),
    (True, "a boolean depth"),
    ("900", "a string depth"),
    (900.0, "a float depth"),
    (_SECRET, "a secret-shaped depth"),
    (_SecretInt(900), "an int subclass that compares equal"),
    (_HUGE, "an unrepresentable depth"),
    (None, "a missing depth"),
])
def test_a_row_depth_that_disagrees_with_its_key_fails_closed(depth_value, why):
    rows = {_LO: _depth_row(_LO), _HI: _depth_row(_HI, {"depth": depth_value})}
    result = _depth_result(rows=rows)
    reasons = sst.evaluate_depth(result)
    assert reasons, why
    assert any("depth" in r for r in reasons), reasons
    assert _SECRET not in " ".join(reasons)
    assert sst._published_depth(result) is None, why


def test_a_missing_row_depth_fails_closed():
    row = _depth_row(_HI)
    del row["depth"]
    result = _depth_result(rows={_LO: _depth_row(_LO), _HI: row})
    assert sst.evaluate_depth(result)
    assert sst._published_depth(result) is None


# --- order_counts_at_max_depth ------------------------------------------------

@pytest.mark.parametrize("value, why", [
    (None, "a missing summary"),
    ("not a mapping", "a string summary"),
    (["on_first", "off_first"], "a list summary"),
    ({}, "an empty summary"),
    ({"on_first": 6}, "a summary missing off_first"),
    ({"on_first": 6, "off_first": True}, "a boolean count"),
    ({"on_first": 6, "off_first": -1}, "a negative count"),
    ({"on_first": 6, "off_first": _SECRET}, "a secret-shaped count"),
    ({"on_first": 6, "off_first": _HUGE}, "an unrepresentable count"),
    ({"on_first": 5, "off_first": 7}, "counts that are not the deepest row's"),
    ({"on_first": 6, "off_first": 6, "extra": 1}, "an unexpected key"),
])
def test_a_bad_max_depth_order_summary_fails_closed(value, why):
    result = _depth_result(order_counts_at_max_depth=value)
    reasons = sst.evaluate_depth(result)
    assert reasons, why
    assert any("order_counts_at_max_depth" in r for r in reasons), reasons
    assert _SECRET not in " ".join(reasons)
    assert sst._published_depth(result) is None, why


def test_the_max_depth_order_summary_must_equal_the_deepest_row():
    """A balanced summary must not vouch for an unbalanced measurement."""
    rows = {_LO: _depth_row(_LO),
            _HI: _depth_row(_HI, {"order_counts": {"on_first": 11,
                                              "off_first": 1}})}
    result = _depth_result(rows=rows)
    result["order_counts_at_max_depth"] = {"on_first": 6, "off_first": 6}
    reasons = sst.evaluate_depth(result)
    assert any("order_counts_at_max_depth" in r for r in reasons), reasons
    assert sst._published_depth(result) is None


# --- the newly validated fields are published in normalised form --------------

def test_the_row_depth_and_max_depth_order_counts_are_published():
    published = sst._published_depth(_depth_result())
    assert published is not None
    assert published["per_depth"]["100"]["depth"] == 100
    assert published["per_depth"]["900"]["depth"] == 900
    assert type(published["per_depth"]["900"]["depth"]) is int
    assert published["order_counts_at_max_depth"] == {"on_first": 6,
                                                      "off_first": 6}
    for value in published["order_counts_at_max_depth"].values():
        assert type(value) is int
    json.dumps(published)          # provably serialisable


@pytest.mark.parametrize("over, why", [
    ({"rows": {_LO: _depth_row(_LO),
               _HI: _depth_row(_HI, {"depth": _SECRET})}}, "a bad row depth"),
    ({"rows": {_LO: _depth_row(_LO),
               _HI: _depth_row(_HI, {"paired_ratio_min": 25.0})}},
     "a contradictory ratio range"),
    ({"order_counts_at_max_depth": {"on_first": 6, "off_first": _SECRET}},
     "a bad order summary"),
])
def test_the_new_rejections_never_leak_through_any_channel(over, why,
                                                           monkeypatch,
                                                           tmp_path, capsys):
    result = _depth_result(**over)
    reasons = sst.evaluate_depth(result)
    assert reasons, why

    harness = _FakeDepthHarness(result=result)
    code, out = _run_depth(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for channel, text in (("artifact", blob), ("stdout", captured.out),
                          ("stderr", captured.err),
                          ("reasons", " ".join(reasons)),
                          ("reasons repr", repr(reasons))):
        assert _SECRET not in text, f"the secret leaked into {channel}"
        assert "<Unserializable" not in text, f"a repr leaked into {channel}"
    assert json.loads(blob)["measurement"] is None
    assert captured.err == ""


# --- publication --------------------------------------------------------------

@pytest.mark.parametrize("over", [
    {"paired": False}, {"samples": 11}, {"depths": [100, 800]},
    {"on_us_per_turn_of_depth": float("nan")}, {"on_ms_at_100": -1.0},
    {"paired_samples_at_max_depth": 0}, {"slope_reduction_x": 1.0},
])
def test_a_structurally_invalid_depth_result_publishes_nothing(over):
    assert sst._published_depth(_depth_result(**over)) is None


def test_a_depth_threshold_breach_publishes_the_whole_measurement():
    """A valid measurement that missed a bound is exactly what to publish."""
    result = _set_paired_speedup(_depth_result(), 1.6382275114582876)

    assert sst.evaluate_depth(result), "this must be a breach, not a pass"
    published = sst._published_depth(result)
    assert published is not None, "an operator must be able to read the slopes"
    assert published["depths"] == [100, 900]
    assert published["samples"] == 12
    assert published["paired"] is True
    assert published["paired_speedup_at_max_depth"] == pytest.approx(
        1.6382275114582876)
    assert set(published["per_depth"]) == {"100", "900"}
    assert published["per_depth"]["900"]["order_counts"]["on_first"] == 6
    assert type(published["on_us_per_turn_of_depth"]) is float
    assert _SECRET not in json.dumps(published)


# --- the runner, driven end to end -------------------------------------------

class _FakeDepthHarness:
    def __init__(self, *, result=_UNSET, bench_error=None, cleanup_error=None):
        self._result = result
        self._bench_error = bench_error
        self._cleanup_error = cleanup_error
        self.calls = 0
        self.cleanup_calls = 0

    def bench_sync_depth_scaling(self, depths, samples):
        self.calls += 1
        assert tuple(depths) == _DEPTHS and samples == _SAMPLES
        if self._bench_error is not None:
            raise self._bench_error
        return _depth_result() if self._result is _UNSET else self._result

    def bench_sessionlog_sync(self, turns, cache):     # pragma: no cover
        raise AssertionError("the depth measurement must not run the latency "
                             "benchmark")

    def cleanup(self):
        self.cleanup_calls += 1
        if self._cleanup_error is not None:
            raise self._cleanup_error


def _run_depth(monkeypatch, tmp_path, loader):
    monkeypatch.setattr(sst, "_load_benchmark", loader)
    out = tmp_path / "nested" / "sync-depth.json"
    code = sst.main(["--measure", "depth", "--out", str(out)])
    return code, out


def test_a_healthy_depth_run_exits_zero_and_measures_once(monkeypatch,
                                                          tmp_path, capsys):
    harness = _FakeDepthHarness()
    code, out = _run_depth(monkeypatch, tmp_path, lambda: harness)

    assert code == 0
    assert harness.calls == 1, "the benchmark must run exactly once"
    assert harness.cleanup_calls == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "pass"
    assert data["benchmark"] == "sessionlog.sync depth scaling"
    assert data["measurement_kind"] == "depth"
    assert data["contract"]["depths"] == [100, 900]
    assert data["measurement"]["samples"] == 12
    captured = capsys.readouterr()
    assert captured.out == out.read_text(encoding="utf-8")
    assert captured.err == ""


def test_a_depth_breach_is_red_and_publishes_its_measurement(monkeypatch,
                                                             tmp_path, capsys):
    result = _set_paired_speedup(_depth_result(), 1.6382275114582876)
    code, out = _run_depth(monkeypatch, tmp_path,
                           lambda: _FakeDepthHarness(result=result))

    assert code == 1
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["verdict"] == "fail"
    assert data["measurement"] is not None
    assert any("paired speedup" in r for r in data["failure_reasons"])
    captured = capsys.readouterr()
    assert captured.out == blob and captured.err == ""


def test_a_structurally_invalid_depth_run_publishes_measurement_null(
        monkeypatch, tmp_path, capsys):
    code, out = _run_depth(
        monkeypatch, tmp_path,
        lambda: _FakeDepthHarness(result=_depth_result(paired=False)))

    assert code == 1
    blob = out.read_text(encoding="utf-8")
    data = json.loads(blob)
    assert data["measurement"] is None
    assert data["failed_stages"] == [], "a rejected row is not a crash"
    captured = capsys.readouterr()
    assert captured.out == blob and captured.err == ""


@pytest.mark.parametrize("stage, loader_factory", [
    ("setup", lambda: (lambda: (_ for _ in ()).throw(_LoudFailure(_SECRET)))),
    ("benchmark", lambda: (lambda: _FakeDepthHarness(
        bench_error=_LoudFailure(_SECRET)))),
    ("cleanup", lambda: (lambda: _FakeDepthHarness(
        cleanup_error=_LoudFailure(_SECRET)))),
])
def test_depth_operational_failures_still_write_a_red_artifact(
        stage, loader_factory, monkeypatch, tmp_path, capsys):
    code, out = _run_depth(monkeypatch, tmp_path, loader_factory())
    assert code == 1
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any(s["stage"] == stage for s in data["failed_stages"]), data
    captured = capsys.readouterr()
    for text in (out.read_text(encoding="utf-8"), captured.out, captured.err):
        assert _SECRET not in text
    assert captured.err == ""


def test_a_depth_publication_failure_is_contained(monkeypatch, tmp_path,
                                                  capsys):
    boom = type(f"DepthPubErr_{_SECRET}", (Exception,), {})

    def exploding(_result, _contract=None):
        raise boom(_SECRET)

    monkeypatch.setattr(sst, "_published_depth", exploding)
    harness = _FakeDepthHarness()
    code, out = _run_depth(monkeypatch, tmp_path, lambda: harness)

    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "fail"
    assert any(s["stage"] == "publication" for s in data["failed_stages"])
    assert data["measurement"] is None
    assert harness.cleanup_calls == 1
    captured = capsys.readouterr()
    for text in (out.read_text(encoding="utf-8"), captured.out, captured.err):
        assert _SECRET not in text and "DepthPubErr_" not in text


_DEPTH_LEAK_FIELDS = ("samples", "paired_samples_at_max_depth",
                      "on_us_per_turn_of_depth", "off_us_per_turn_of_depth",
                      "speedup_at_max_depth", "paired_speedup_at_max_depth",
                      "on_ms_at_100", "off_ms_at_900", "slope_reduction_x",
                      "depths", "paired", "per_depth")

_DEPTH_LEAK_SHAPES = (
    ("secret_str", _SECRET),
    ("secret_in_list", [_SECRET]),
    ("secret_repr_object", _Unserializable()),
    ("nan", float("nan")),
    ("secret_float_subclass", _SecretFloat(1.0)),
    ("secret_int_subclass", _SecretInt(12)),
    ("oversized_int", _HUGE),
)

_DEPTH_LEAK_CASES = [(f"{field}-{shape}", field, value)
                     for field in _DEPTH_LEAK_FIELDS
                     for shape, value in _DEPTH_LEAK_SHAPES]


@pytest.mark.parametrize(
    "field, value",
    [(field, value) for _id, field, value in _DEPTH_LEAK_CASES],
    ids=[_id for _id, _f, _v in _DEPTH_LEAK_CASES])
def test_a_rejected_depth_value_never_reaches_any_channel(field, value,
                                                          monkeypatch,
                                                          tmp_path, capsys):
    reasons = sst.evaluate_depth(_depth_result(**{field: value}))
    assert reasons, f"{field} accepted a rejected shape"

    harness = _FakeDepthHarness(result=_depth_result(**{field: value}))
    code, out = _run_depth(monkeypatch, tmp_path, lambda: harness)
    assert code == 1
    blob = out.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for channel, text in (("artifact", blob), ("stdout", captured.out),
                          ("stderr", captured.err),
                          ("reasons", " ".join(reasons))):
        assert _SECRET not in text, f"the secret leaked into {channel}"
        assert "<Unserializable" not in text, f"a repr leaked into {channel}"
        assert "0000000000" not in text, f"an oversized int reached {channel}"
    assert json.loads(blob)["measurement"] is None
    assert captured.err == ""


def test_the_two_measurements_are_independent(monkeypatch, tmp_path):
    """A depth run must not execute the latency benchmark, or vice versa.

    `_FakeDepthHarness.bench_sessionlog_sync` raises, so a selector that ran
    the wrong contract would fail loudly rather than silently measuring the
    wrong thing.
    """
    harness = _FakeDepthHarness()
    code, _out = _run_depth(monkeypatch, tmp_path, lambda: harness)
    assert code == 0 and harness.calls == 1

    latency = _FakeHarness()
    monkeypatch.setattr(sst, "_load_benchmark", lambda: latency)
    out = tmp_path / "latency.json"
    assert sst.main(["--measure", "latency", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["measurement_kind"] \
        == "latency"


# =============================================================================
# A STAGE THAT RETURNS None IS A FAILED STAGE
# =============================================================================
#
# The runner used `if value is not None:` as its success guard, so a stage that
# returned `None` WITHOUT raising recorded nothing, added no reason, and let
# the run exit 0 with `verdict: pass` and `measurement: null`. That is a false
# green on setup, benchmark, evaluation and publication alike — a telemetry job
# reporting success while having measured nothing at all.
#
# Every case below drives `main()` end to end for BOTH selectors and proves the
# run is red, the stage is named exactly once, and nothing leaks.

def _assert_missing_stage(code, out, capsys, stage, *, kind):
    blob = out.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    data = json.loads(blob)

    assert code == 1, f"{kind}/{stage}: a stage that produced nothing is red"
    assert data["verdict"] == "fail", data
    assert data["measurement"] is None, data["measurement"]
    assert data["measurement_kind"] == kind

    named = [s for s in data["failed_stages"] if s["stage"] == stage]
    assert len(named) == 1, (
        f"{kind}: stage {stage!r} must appear exactly once, got "
        f"{data['failed_stages']}")
    assert all(s["stage"] in sst.STAGES for s in data["failed_stages"])
    assert data["failure_reasons"], "a red run must say why"

    assert captured.out == blob, "stdout must equal the artifact byte for byte"
    assert captured.err == "", "a handled failure must not write to stderr"
    for text in (blob, captured.out, captured.err):
        assert _SECRET not in text
        assert "<Unserializable" not in text
        assert "Traceback" not in text
    return data


def _none_returning_loader():
    """A loader that hands back nothing at all, without raising."""
    return None


@pytest.mark.parametrize("kind", ["latency", "depth"])
def test_a_loader_returning_none_is_a_setup_failure(kind, monkeypatch,
                                                    tmp_path, capsys):
    monkeypatch.setattr(sst, "_load_benchmark", _none_returning_loader)
    out = tmp_path / "nested" / f"{kind}.json"
    code = sst.main(["--measure", kind, "--out", str(out)])
    _assert_missing_stage(code, out, capsys, "setup", kind=kind)


@pytest.mark.parametrize("kind, harness_factory", [
    ("latency", lambda: _FakeHarness(result=None)),
    ("depth", lambda: _FakeDepthHarness(result=None)),
])
def test_a_benchmark_returning_none_is_a_benchmark_failure(
        kind, harness_factory, monkeypatch, tmp_path, capsys):
    harness = harness_factory()
    monkeypatch.setattr(sst, "_load_benchmark", lambda: harness)
    out = tmp_path / "nested" / f"{kind}.json"
    code = sst.main(["--measure", kind, "--out", str(out)])
    _assert_missing_stage(code, out, capsys, "benchmark", kind=kind)
    # Cleanup still runs — a benchmark that produced nothing must not also
    # leave the scratch tree behind.
    assert harness.cleanup_calls == 1


@pytest.mark.parametrize("kind, evaluator, harness_factory", [
    ("latency", "evaluate", lambda: _FakeHarness()),
    ("depth", "evaluate_depth", lambda: _FakeDepthHarness()),
])
def test_an_evaluator_returning_none_is_an_evaluation_failure(
        kind, evaluator, harness_factory, monkeypatch, tmp_path, capsys):
    harness = harness_factory()
    monkeypatch.setattr(sst, "_load_benchmark", lambda: harness)
    monkeypatch.setattr(sst, evaluator, lambda *_a, **_k: None)
    out = tmp_path / "nested" / f"{kind}.json"
    code = sst.main(["--measure", kind, "--out", str(out)])
    _assert_missing_stage(code, out, capsys, "evaluation", kind=kind)
    assert harness.cleanup_calls == 1


@pytest.mark.parametrize("kind, publisher, harness_factory", [
    ("latency", "_published", lambda: _FakeHarness()),
    ("depth", "_published_depth", lambda: _FakeDepthHarness()),
])
def test_a_publisher_returning_none_for_a_healthy_result_is_a_failure(
        kind, publisher, harness_factory, monkeypatch, tmp_path, capsys):
    """`None` is correct for an INVALID result and for nothing else.

    The harness supplies a result the evaluator finds no fault with, so the
    publisher must produce numbers. `None` there means the publisher and the
    evaluator disagree about validity — a defect in the runner — and it must
    not read as a pass with no measurement.
    """
    harness = harness_factory()
    monkeypatch.setattr(sst, "_load_benchmark", lambda: harness)
    monkeypatch.setattr(sst, publisher, lambda *_a, **_k: None)
    out = tmp_path / "nested" / f"{kind}.json"
    code = sst.main(["--measure", kind, "--out", str(out)])
    _assert_missing_stage(code, out, capsys, "publication", kind=kind)
    assert harness.cleanup_calls == 1


@pytest.mark.parametrize("kind, over", [
    ("latency", {"journal_bytes": 0}),
    ("depth", {"paired": False}),
])
def test_a_publisher_returning_none_for_an_invalid_result_is_not_a_failure(
        kind, over, monkeypatch, tmp_path):
    """The converse: `None` for a structurally invalid result is correct.

    Without this the new publication guard would fire on every rejected row and
    turn one honest reason into two, misattributing a measurement defect to the
    runner.
    """
    if kind == "depth":
        harness = _FakeDepthHarness(result=_depth_result(**over))
    else:
        harness = _FakeHarness(result=_result(**over))
    monkeypatch.setattr(sst, "_load_benchmark", lambda: harness)
    out = tmp_path / "nested" / f"{kind}.json"
    code = sst.main(["--measure", kind, "--out", str(out)])

    data = json.loads(out.read_text(encoding="utf-8"))
    assert code == 1 and data["measurement"] is None
    assert not any(s["stage"] == "publication"
                   for s in data["failed_stages"]), data["failed_stages"]
    assert data["failed_stages"] == [], "a rejected row is not a runner defect"


@pytest.mark.parametrize("kind, harness_factory", [
    ("latency", lambda: _FakeHarness(bench_error=_LoudFailure(_SECRET))),
    ("depth", lambda: _FakeDepthHarness(bench_error=_LoudFailure(_SECRET))),
])
def test_a_raising_stage_is_recorded_exactly_once(kind, harness_factory,
                                                  monkeypatch, tmp_path,
                                                  capsys):
    """A stage that raised must not ALSO be counted as a `None` return."""
    monkeypatch.setattr(sst, "_load_benchmark", harness_factory)
    out = tmp_path / "nested" / f"{kind}.json"
    code = sst.main(["--measure", kind, "--out", str(out)])
    data = _assert_missing_stage(code, out, capsys, "benchmark", kind=kind)
    assert len(data["failed_stages"]) == 1, data["failed_stages"]


@pytest.mark.parametrize("kind", ["latency", "depth"])
def test_a_healthy_run_records_no_stage_at_all(kind, monkeypatch, tmp_path):
    """The guard must not fire on the success path.

    `evaluate` returns `[]` — falsy but not None — and that distinction is the
    whole basis of the fix, so a clean run must still be green.
    """
    harness = (_FakeDepthHarness() if kind == "depth" else _FakeHarness())
    monkeypatch.setattr(sst, "_load_benchmark", lambda: harness)
    out = tmp_path / "nested" / f"{kind}.json"
    assert sst.main(["--measure", kind, "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "pass"
    assert data["failed_stages"] == []
    assert data["failure_reasons"] == []
    assert data["measurement"] is not None
    assert harness.cleanup_calls == 1
