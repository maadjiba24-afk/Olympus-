"""The model registry, and the three refusals that make it worth having.

An approval nobody's name is on, an approval of a moving revision, and an
unapproved model reaching a live venue are all failures that only show up long
after the fact — in an audit trail that cannot explain a trade. Each of them is
a hard refusal here, and each has a test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Mode
from olympus.trading.errors import ConfigurationError, ModeNotPermitted
from olympus.trading.registry import (APPROVED, CANDIDATE, RETIRED,
                                      ModelRecord, ModelRegistry)

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
REV = "9f2c1b7d4e5a6b8c9d0e1f2a3b4c5d6e7f809a1b"


class FakeAudit:
    def __init__(self):
        self.events = []

    def record(self, event_type, payload=None, *, actor="system", subject="",
               correlation_id=""):
        self.events.append((str(event_type), dict(payload or {}), actor, subject))


def registry(audit=None, clock=None):
    return ModelRegistry(clock=clock or FixedClock(T0), audit=audit)


def register(reg, model_id="kronos-small", *, revision=REV, **kw):
    return reg.register(model_id, kind=kw.pop("kind", "forecaster"),
                        version=kw.pop("version", "1.0"),
                        repo_id=kw.pop("repo_id", "NeoQuasar/Kronos-small"),
                        revision=revision, sha256=kw.pop("sha256", "a" * 64),
                        registered_by=kw.pop("registered_by", "alice"), **kw)


# --- registration -----------------------------------------------------------

def test_a_new_model_is_a_candidate_whatever_its_metrics_say():
    """Registration never confers permission."""
    reg = registry()
    record = register(reg, metrics={"sharpe": 9.9, "mape": 0.001})
    assert record.status == CANDIDATE
    assert record.registered_at == T0
    assert record.registered_by == "alice"
    assert reg.get("kronos-small").status == CANDIDATE


def test_re_registering_an_id_is_refused():
    """Changing the identity under an approval would launder an unreviewed
    checkpoint into live use."""
    reg = registry()
    register(reg)
    with pytest.raises(ConfigurationError):
        register(reg, revision="b" * 40)


def test_registration_requires_an_id_and_a_kind():
    reg = registry()
    with pytest.raises(ConfigurationError):
        reg.register("", kind="forecaster")
    with pytest.raises(ConfigurationError):
        reg.register("x", kind="")


def test_records_round_trip_through_the_store():
    reg = registry()
    original = register(reg, notes="first import", pin={"revision": REV})
    again = ModelRecord.from_dict(original.to_dict())
    assert again == original
    # ...and through the persistent store, in a fresh registry object.
    assert registry().get("kronos-small") == original


# --- pinning ----------------------------------------------------------------

@pytest.mark.parametrize("revision", ["", "main", "master", "HEAD", "latest"])
def test_a_moving_revision_counts_as_unpinned(revision):
    record = ModelRecord(model_id="m", kind="forecaster", revision=revision)
    assert record.is_pinned is False


def test_a_pin_that_contradicts_the_record_counts_as_unpinned():
    """A stale pin looks like diligence while loading something else."""
    record = ModelRecord(model_id="m", kind="forecaster", revision=REV,
                         sha256="a" * 64, pin={"revision": "b" * 40})
    assert record.is_pinned is False
    mismatched_hash = ModelRecord(model_id="m", kind="forecaster",
                                  revision=REV, sha256="a" * 64,
                                  pin={"sha256": "c" * 64})
    assert mismatched_hash.is_pinned is False


def test_a_consistent_pin_is_pinned():
    record = ModelRecord(model_id="m", kind="forecaster", revision=REV,
                         sha256="a" * 64,
                         pin={"revision": REV, "sha256": "a" * 64})
    assert record.is_pinned is True


# --- approval ---------------------------------------------------------------

def test_approval_requires_a_named_operator():
    reg = registry()
    register(reg)
    for operator in ("", "   ", None):
        with pytest.raises(ConfigurationError):
            reg.approve("kronos-small", operator=operator)
    assert reg.get("kronos-small").status == CANDIDATE


def test_an_unpinned_model_can_never_be_approved():
    """A moving revision makes every forecast unreproducible and the recorded
    model_version untrue."""
    reg = registry()
    register(reg, model_id="drifting", revision="main")
    with pytest.raises(ConfigurationError) as exc:
        reg.approve("drifting", operator="alice")
    assert "pinned" in str(exc.value)
    assert reg.get("drifting").status == CANDIDATE

    register(reg, model_id="unversioned", revision="")
    with pytest.raises(ConfigurationError):
        reg.approve("unversioned", operator="alice")


def test_approval_records_the_operator_and_writes_an_audit_event():
    audit = FakeAudit()
    reg = registry(audit=audit)
    register(reg)
    record = reg.approve("kronos-small", operator="alice",
                         notes="passed OOS evaluation")
    assert record.status == APPROVED
    assert record.approved_by == "alice"
    assert record.approved_at == T0
    kinds = [(e[0], e[1]["action"], e[2]) for e in audit.events]
    assert ("MODEL_CHANGED", "approve", "alice") in kinds


def test_approving_an_unknown_model_raises():
    with pytest.raises(ConfigurationError):
        registry().approve("ghost", operator="alice")


def test_a_retired_model_cannot_be_approved_again():
    reg = registry()
    register(reg)
    reg.retire("kronos-small", operator="alice", reason="superseded")
    assert reg.get("kronos-small").status == RETIRED
    with pytest.raises(ConfigurationError):
        reg.approve("kronos-small", operator="alice")


def test_retiring_an_approved_model_takes_it_out_of_live_immediately():
    reg = registry()
    register(reg)
    reg.approve("kronos-small", operator="alice")
    reg.retire("kronos-small", operator="alice", reason="drift")
    with pytest.raises(ModeNotPermitted):
        reg.assert_usable("kronos-small", Mode.LIVE_BOUNDED)


# --- queries ----------------------------------------------------------------

def test_listing_filters_by_status_and_kind():
    reg = registry()
    register(reg, model_id="a", kind="forecaster")
    register(reg, model_id="b", kind="forecaster")
    register(reg, model_id="c", kind="tokenizer")
    reg.approve("a", operator="alice")
    assert [r.model_id for r in reg.list()] == ["a", "b", "c"]
    assert [r.model_id for r in reg.list(status=APPROVED)] == ["a"]
    assert [r.model_id for r in reg.list(kind="tokenizer")] == ["c"]
    with pytest.raises(ConfigurationError):
        reg.list(status="probationary")


def test_approved_returns_the_most_recent_approval_deterministically():
    clock = FixedClock(T0)
    reg = registry(clock=clock)
    register(reg, model_id="old")
    register(reg, model_id="new")
    reg.approve("old", operator="alice")
    clock.advance(timedelta(days=1))
    reg.approve("new", operator="alice")
    assert reg.approved("forecaster").model_id == "new"
    assert reg.approved("tokenizer") is None
    # Repeated calls must agree — two processes must load the same checkpoint.
    assert reg.approved("forecaster") == reg.approved("forecaster")


# --- the live gate ----------------------------------------------------------

@pytest.mark.parametrize("mode", [Mode.LIVE_RESTRICTED, Mode.LIVE_BOUNDED])
def test_live_modes_refuse_an_unapproved_model(mode):
    reg = registry()
    register(reg)
    with pytest.raises(ModeNotPermitted):
        reg.assert_usable("kronos-small", mode)
    reg.approve("kronos-small", operator="alice")
    assert reg.assert_usable("kronos-small", mode).status == APPROVED


def test_live_modes_refuse_a_model_that_was_never_registered():
    with pytest.raises(ModeNotPermitted):
        registry().assert_usable("mystery-model", Mode.LIVE_BOUNDED)


@pytest.mark.parametrize("mode", [Mode.BACKTEST, Mode.PAPER, Mode.SHADOW])
def test_non_live_modes_permit_unapproved_and_unknown_models(mode):
    """Backtest, paper and shadow are where a candidate earns approval; refusing
    to run it there would make approval unreachable."""
    reg = registry()
    register(reg)
    assert reg.assert_usable("kronos-small", mode).status == CANDIDATE
    assert reg.assert_usable("never-registered", mode) is None


def test_an_approved_but_unpinned_record_is_still_refused_in_live():
    """Belt and braces: a record edited out of band must not smuggle an
    unpinned model into live trading."""
    reg = registry()
    register(reg)
    reg.approve("kronos-small", operator="alice")
    tampered = reg.get("kronos-small").evolve(revision="main")
    reg._write(tampered)                                 # simulate out-of-band edit
    with pytest.raises(ModeNotPermitted):
        reg.assert_usable("kronos-small", Mode.LIVE_BOUNDED)


def test_assert_all_usable_reports_every_blocker_not_just_the_first():
    reg = registry()
    register(reg, model_id="ok")
    register(reg, model_id="candidate")
    reg.approve("ok", operator="alice")
    with pytest.raises(ModeNotPermitted) as exc:
        reg.assert_all_usable(["ok", "candidate", "ghost"], Mode.LIVE_BOUNDED)
    blocked = exc.value.details["blocked"]
    assert len(blocked) == 2
    assert any(b.startswith("candidate") for b in blocked)
    assert any(b.startswith("ghost") for b in blocked)


def test_the_modes_gate_reads_this_registry():
    """`modes.ModelApprovedGate` asks for `.get(model_id).status == 'approved'`;
    this pins that contract so a rename here fails loudly rather than quietly
    opening the live gate."""
    from olympus.trading.modes import ModelApprovedGate
    reg = registry()
    register(reg)
    ctx = {"model_registry": reg, "active_models": ["kronos-small"]}
    assert not ModelApprovedGate().check(ctx).passed
    reg.approve("kronos-small", operator="alice")
    assert ModelApprovedGate().check(ctx).passed
