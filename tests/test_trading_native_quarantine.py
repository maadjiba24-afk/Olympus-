"""The native model's quarantine, proved property by property.

The matched evaluation returned **insufficient evidence**: on identical data,
costs, splits and metrics the native arm lost to a gradient-boosted tree, to a
linear fit and to persistence. Separately, blocker **B8** is open — the model's
error is almost entirely a constant offset of +0.025 against an MAE of 0.0256,
which means it has not learned the conditional mean it was trained for.

That result is not tuned away here and it is not softened. It is enforced. Each
test below takes one of the eight structural properties and tries to break it:

1. it is experimental
2. it is not the default forecaster
3. it is not automatically registered
4. it cannot participate in live trading
5. it cannot become production eligible
6. it cannot pass the promotion gate while B8 is unresolved
7. it cannot be selected by an autonomous agent
8. it is disabled by default

A ninth set of tests guards the guard: the blocker list must not be quietly
emptied, and `enabled()` must not become satisfiable by an environment variable
alone.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from olympus.trading.governance import Actor
from olympus.trading.modes import Mode
from olympus.trading.native import promotion as PR
from olympus.trading.native import quarantine as Q

REPO = Path(__file__).resolve().parents[1]
START = datetime(2028, 5, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. experimental
# ---------------------------------------------------------------------------

def test_the_model_is_experimental_and_nothing_can_set_it_otherwise():
    assert Q.experimental() is True
    assert Q.status()["experimental"] is True
    assert {b.id for b in Q.open_blockers()} >= {"B8"}

    # Computed from frozen data. A blocker cannot be marked resolved at
    # runtime — closing one is an edit to `BLOCKERS` in a commit a reviewer
    # reads, which is the point of putting it in source rather than in state.
    with pytest.raises((AttributeError, TypeError)):
        Q.BLOCKERS[0].resolved = True                   # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        Q.BLOCKERS.append(None)                         # type: ignore[attr-defined]
    assert isinstance(Q.BLOCKERS, tuple)
    assert Q.experimental() is True


def test_the_blocker_that_matters_is_stated_rather_than_softened():
    """B8 names the number. A blocker that says "some issues" is not a blocker."""
    b8 = Q.blocker("B8")
    assert b8.resolved is False
    assert "0.025636" in b8.detail and "0.025038" in b8.detail
    assert "constant offset" in b8.title
    assert any("without a post-hoc offset term" in item
               for item in b8.resolution_evidence), (
        "B8 must exclude the fix that would make the number go away without "
        "fixing the model")

    b9 = Q.blocker("B9")
    assert "loses to every simple matched baseline" in b9.title
    assert "INSUFFICIENT EVIDENCE" in b9.detail


# ---------------------------------------------------------------------------
# 2. not the default forecaster
# ---------------------------------------------------------------------------

def test_nothing_in_the_forecast_layer_names_the_native_model():
    """`forecast.py` is where a default would live, and it does not know it exists.

    An import-graph fact, not a naming convention: the forecasting layer cannot
    resolve a default to a model it has never heard of.
    """
    text = (REPO / "olympus" / "trading" / "forecast.py").read_text()
    for needle in ("NativeForecaster", "olympus-native", "native.forecaster"):
        assert needle not in text, (
            f"forecast.py mentions {needle!r}; a default that can name the "
            f"native model is a default that can become it")


def test_no_default_anywhere_resolves_to_the_native_model():
    """Every module-level `DEFAULT_*` in the trading package, checked."""
    offenders: list[str] = []
    for path in sorted((REPO / "olympus" / "trading").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, ValueError):
            continue
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = ([node.target] if isinstance(node, ast.AnnAssign)
                       else node.targets)
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if not any("DEFAULT" in n.upper() for n in names):
                continue
            rendered = ast.dump(node.value) if node.value else ""
            if any(mark in rendered for mark in ("olympus-native",
                                                 "NativeForecaster",
                                                 "olympus_native")):
                offenders.append(f"{path.relative_to(REPO)}: {names}")
    assert offenders == [], (
        "a module-level default resolves to the native model:\n  "
        + "\n  ".join(offenders))


def test_the_quarantine_says_so_as_data():
    assert Q.status()["is_default_forecaster"] is False


# ---------------------------------------------------------------------------
# 3. not automatically registered
# ---------------------------------------------------------------------------

_IMPORT_PROBE = """
import sys

touched = []
import olympus.trading.registry as registry

original = registry.ModelRegistry.register


def watched(self, *args, **kwargs):
    touched.append(args[0] if args else kwargs.get("model_id"))
    return original(self, *args, **kwargs)


registry.ModelRegistry.register = watched

import olympus.trading.native as native
import olympus.trading.native.forecaster
import olympus.trading.native.quarantine

# Touch the lazy attributes too, so a registration hidden behind one of them is
# not missed by an import that never resolved it.
for name in sorted(getattr(native, "_LAZY", {})):
    getattr(native, name)

print("REGISTERED", touched)
"""


def test_importing_the_native_package_registers_nothing():
    """Not "we do not call register" — nothing calls it, watched from outside."""
    proc = subprocess.run([sys.executable, "-c", _IMPORT_PROBE],
                          capture_output=True, text=True, cwd=str(REPO),
                          timeout=600)
    assert proc.returncode == 0, proc.stderr
    line = [l for l in proc.stdout.splitlines() if l.startswith("REGISTERED")]
    assert line, proc.stdout
    assert line[-1] == "REGISTERED []", (
        f"importing the native package registered a model: {line[-1]}")


def test_the_quarantine_reports_that_it_is_not_registered():
    assert Q.status()["automatically_registered"] is False


# ---------------------------------------------------------------------------
# 4. cannot participate in live trading
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("purpose", [Q.Purpose.LIVE, Q.Purpose.PAPER,
                                     Q.Purpose.SHADOW, "live", "paper"])
def test_a_non_research_purpose_is_refused_at_construction(purpose):
    """Refused before an instance exists to be handed anywhere."""
    with pytest.raises(Q.QuarantineViolation) as raised:
        Q.assert_purpose(purpose)
    assert "B8" in raised.value.details["open_blockers"]
    assert set(raised.value.details["permitted"]) == {
        "research", "backtest", "evaluation"}


@pytest.mark.parametrize("purpose", [Q.Purpose.RESEARCH, Q.Purpose.BACKTEST,
                                     Q.Purpose.EVALUATION])
def test_research_purposes_are_permitted(purpose):
    """The quarantine is not a ban. Research is how a blocker gets closed."""
    assert Q.assert_purpose(purpose) is purpose


@pytest.mark.parametrize("mode", [Mode.LIVE_RESTRICTED, Mode.LIVE_BOUNDED])
def test_a_live_mode_is_refused_independently_of_the_purpose(mode):
    """The second check. Two that can each fail alone, not one `if`."""
    with pytest.raises(Q.QuarantineViolation):
        Q.assert_not_live(mode)


@pytest.mark.parametrize("mode", [Mode.BACKTEST, Mode.PAPER, Mode.SHADOW])
def test_a_non_live_mode_passes_the_live_check(mode):
    Q.assert_not_live(mode)


def test_the_forecaster_refuses_a_live_purpose_and_carries_the_quarantine():
    """End to end, through the object the rest of Olympus would actually hold."""
    from olympus.trading.native import forecaster as F

    with pytest.raises(Q.QuarantineViolation):
        F.NativeForecaster.__init__(object.__new__(F.NativeForecaster),
                                    checkpoint=None, purpose=Q.Purpose.LIVE)

    built = _fitted_forecaster()
    assert built.experimental is True
    assert built.production_eligible is False
    assert built.purpose is Q.Purpose.RESEARCH
    identity = built._identity()                         # noqa: SLF001
    assert identity["experimental"] is True
    assert identity["production_eligible"] is False
    assert "B8" in identity["open_blockers"]
    assert "quarantined" in identity["quarantine"]
    with pytest.raises(Q.QuarantineViolation):
        built.assert_usable(Mode.LIVE_BOUNDED)
    built.assert_usable(Mode.BACKTEST)


def _fitted_forecaster():
    """A real forecaster, built the way the existing suite builds one.

    Reuses `tests/test_trading_native.py`'s fixture rather than reimplementing
    the training call: a second copy of it would drift, and this test is about
    the quarantine rather than about training.
    """
    from olympus.trading.native import forecaster as F
    import test_trading_native as base                  # noqa: PLC0415

    return F.NativeForecaster(base.train_one().checkpoint)


# ---------------------------------------------------------------------------
# 5. cannot become production eligible
# ---------------------------------------------------------------------------

def test_production_eligibility_is_computed_from_the_blockers():
    assert Q.production_eligible() is False
    assert Q.status()["production_eligible"] is False


def test_registry_approval_does_not_make_it_production_eligible(tmp_path):
    """Two independent gates, and paperwork only satisfies one of them.

    A model can be registered and approved — that is what the registry is for.
    The quarantine is a separate refusal about the *model*, not about its
    record, so approving it changes nothing here.
    """
    from olympus.trading.clock import FixedClock
    from olympus.trading.registry import ModelRegistry

    registry = ModelRegistry(clock=FixedClock(START))
    registry.register("olympus-native", kind="forecaster", version="0.1.0",
                      revision="a" * 40)
    assert registry.get("olympus-native") is not None
    assert Q.production_eligible() is False, (
        "registering the model changed the quarantine")
    assert Q.is_quarantined("olympus-native")


@pytest.mark.parametrize("identifier", [
    "olympus-native", "olympus-native/statistical-abc123",
    "OLYMPUS-NATIVE", "native-0.1.0", "SIM:olympus-native",
    "olympus_native_v2",
])
def test_every_shape_of_the_native_name_is_recognised(identifier):
    """A quarantine with a documented way round it is not a quarantine."""
    assert Q.is_quarantined(identifier) is True


@pytest.mark.parametrize("identifier", [
    "persistence", "kronos-base", "incumbent-forecaster", "", "drift",
])
def test_other_models_are_not_caught_by_the_quarantine(identifier):
    assert Q.is_quarantined(identifier) is False


# ---------------------------------------------------------------------------
# 6. cannot pass the promotion gate
# ---------------------------------------------------------------------------

def _passing_checks():
    return {stage: (lambda s=stage: (True, {"stage": s.value, "ok": 1.0}))
            for stage in PR.AUTONOMOUS_STAGES}


def _reviewed(tmp_path, operator, challenger_id="native-challenger"):
    from olympus.trading.native import durable as DU

    ledger = PR.GateLedger(audit_log=DU.AuditLog(tmp_path / "audit.jsonl"),
                           state=DU.StateStore(tmp_path / "state.json"))
    ledger.enter(challenger_id=challenger_id, proposal_id="p1",
                 created_at=START)
    PR.run_autonomous_stages(ledger, challenger_id, checks=_passing_checks(),
                             at=START)
    ledger.advance(challenger_id, PR.StageResult(
        stage=PR.GateStage.HUMAN_REVIEW, passed=True, at=START,
        evidence={"reviewer": "alice"}, actor="alice"), actor=operator)
    return ledger


def test_the_native_model_cannot_be_promoted_while_b8_is_open(tmp_path):
    """Even with a real operator, a real token and complete evidence."""
    operator = Actor.operator("alice", token="tok")
    ledger = _reviewed(tmp_path, operator)
    restriction = PR.default_restriction(instruments=("SIM:BTCUSDT",),
                                         max_notional=100.0, at=START)
    evidence = {
        "model_id": "olympus-native",
        "model_version": "olympus-native/statistical-abc123",
        "evaluation_report": "docs/OLYMPUS_VS_KRONOS.md",
        "reviewed_by": "alice",
        "review_notes": "looks fine to me",
        "rollback_plan": "revert",
    }
    with pytest.raises(Q.QuarantineViolation) as raised:
        ledger.promote("native-challenger", actor=operator, at=START,
                       restriction=restriction, evidence=evidence)
    assert "B8" in raised.value.details["open_blockers"]
    assert ledger.durable_state["native-challenger"]["outcome"] != "promoted"


def test_renaming_the_model_in_the_evidence_does_not_get_it_through(tmp_path):
    """The check reads every string in the evidence, not one named field."""
    operator = Actor.operator("alice", token="tok")
    ledger = _reviewed(tmp_path, operator)
    restriction = PR.default_restriction(instruments=("SIM:BTCUSDT",),
                                         max_notional=100.0, at=START)
    evidence = {
        "model_id": "candidate-7",
        "model_version": "2.0.0",
        "evaluation_report": "the olympus-native checkpoint, renamed",
        "reviewed_by": "alice",
        "review_notes": "no concerns",
        "rollback_plan": "revert",
    }
    with pytest.raises(Q.QuarantineViolation):
        ledger.promote("native-challenger", actor=operator, at=START,
                       restriction=restriction, evidence=evidence)


def test_the_challenger_id_itself_is_checked(tmp_path):
    """A challenger called `olympus-native-...` is the same model."""
    operator = Actor.operator("alice", token="tok")
    ledger = _reviewed(tmp_path, operator, challenger_id="olympus-native-c1")
    with pytest.raises(Q.QuarantineViolation):
        ledger.promote("olympus-native-c1", actor=operator, at=START,
                       restriction=PR.default_restriction(
                           instruments=("SIM:BTCUSDT",), max_notional=1.0,
                           at=START),
                       evidence={"model_id": "candidate-7",
                                 "model_version": "2.0.0",
                                 "evaluation_report": "r",
                                 "reviewed_by": "alice",
                                 "review_notes": "n",
                                 "rollback_plan": "revert"})


def test_a_different_model_still_promotes(tmp_path):
    """The quarantine is specific. It does not break the gate for everything."""
    operator = Actor.operator("alice", token="tok")
    ledger = _reviewed(tmp_path, operator, challenger_id="incumbent-c1")
    run = ledger.promote("incumbent-c1", actor=operator, at=START,
                         restriction=PR.default_restriction(
                             instruments=("SIM:BTCUSDT",), max_notional=100.0,
                             at=START),
                         evidence={"model_id": "incumbent-forecaster",
                                   "model_version": "2.4.0",
                                   "evaluation_report": "docs/report.md",
                                   "reviewed_by": "alice",
                                   "review_notes": "beats persistence",
                                   "rollback_plan": "revert"})
    assert run.outcome is PR.GateOutcome.PROMOTED


# ---------------------------------------------------------------------------
# 7. not selectable by an autonomous agent
# ---------------------------------------------------------------------------

def test_an_autonomous_actor_cannot_select_the_native_model():
    assert Q.autonomously_selectable() is False
    with pytest.raises(Q.QuarantineViolation):
        Q.assert_autonomous_selection_refused(Actor.autonomous("engine"))
    with pytest.raises(Q.QuarantineViolation):
        Q.assert_autonomous_selection_refused(None)
    # A named operator may, and that is the whole permitted route.
    Q.assert_autonomous_selection_refused(Actor.operator("alice", token="tok"))


def test_selectability_does_not_depend_on_the_blockers():
    """Closing every blocker still does not make selection autonomous.

    Moving a model into use is a permission expansion, and permission expansion
    is human-only everywhere else in Olympus. A constant here says the same
    thing rather than deriving it from a list that could empty.
    """
    # Read from the file by name rather than through `inspect.getsource`,
    # which resolves by line number through a cache and hands back a
    # neighbouring function when the file has been edited since import.
    tree = ast.parse(Path(Q.__file__).read_text(encoding="utf-8"))
    body = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name == "autonomously_selectable")
    source = ast.unparse(body)
    assert "return False" in source
    assert "open_blockers" not in source


# ---------------------------------------------------------------------------
# 8. disabled by default
# ---------------------------------------------------------------------------

def test_the_model_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv(Q.ENABLE_FLAG, raising=False)
    assert Q.enabled() is False
    assert Q.enable_intent() is False
    assert Q.status()["enabled"] is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_setting_the_flag_expresses_intent_and_changes_nothing(
        value, monkeypatch):
    """The distinction between a flag and a gate, as a test.

    `OLYMPUS_NATIVE_MODEL_ENABLED=1` is somebody saying they would like the
    model enabled. The blockers still say no, so it is not enabled — which is
    what stops the quarantine being one environment variable deep.
    """
    monkeypatch.setenv(Q.ENABLE_FLAG, value)
    assert Q.enable_intent() is True
    assert Q.enabled() is False, (
        "the environment variable alone lifted the quarantine")
    status = Q.status()
    assert status["enable_intent"] is True
    assert status["enabled"] is False


def test_enabling_needs_both_the_flag_and_no_blockers(monkeypatch):
    """The conjunction, checked by closing the blockers under the test."""
    monkeypatch.setenv(Q.ENABLE_FLAG, "1")
    resolved = tuple(Q.Blocker(id=b.id, title=b.title, detail=b.detail,
                               resolution_evidence=b.resolution_evidence,
                               resolved=True) for b in Q.BLOCKERS)
    monkeypatch.setattr(Q, "BLOCKERS", resolved)
    assert Q.open_blockers() == ()
    assert Q.enabled() is True
    assert Q.experimental() is False
    assert Q.production_eligible() is True
    # And still not autonomously selectable, because that never depended on
    # the blockers.
    assert Q.autonomously_selectable() is False

    monkeypatch.delenv(Q.ENABLE_FLAG)
    assert Q.enabled() is False, "with no flag, resolved blockers alone enabled it"


# ---------------------------------------------------------------------------
# guarding the guard
# ---------------------------------------------------------------------------

def test_every_blocker_states_what_would_close_it():
    """A blocker with no exit is a complaint, and gets ignored."""
    from olympus.trading.errors import TradingError

    for entry in Q.BLOCKERS:
        assert entry.resolution_evidence, entry.id
        assert entry.detail.strip()
        assert len(entry.detail) > 80, (
            f"{entry.id}: the detail is too short to be a reason someone "
            f"could act on")
    with pytest.raises(TradingError):
        Q.Blocker(id="B0", title="t", detail="d", resolution_evidence=())


def test_the_quarantine_is_not_tuned_away_by_editing_the_number():
    """B8 cites the measurement, and the measurement is in the record.

    If somebody improves the model, this test fails and the fix is to rerun the
    evaluation and update both — not to edit the number here so the two agree
    again.
    """
    status_doc = REPO / "docs" / "OLYMPUS_NATIVE_MODEL_STATUS.md"
    audit_doc = REPO / "docs" / "OLYMPUS_NATIVE_FINAL_AUDIT.md"
    assert status_doc.is_file() and audit_doc.is_file()
    combined = status_doc.read_text() + audit_doc.read_text()
    assert "0.025636" in combined or "0.0256" in combined, (
        "the location-bias measurement is not in the documented record; the "
        "quarantine cites a number nobody can check")


def test_the_status_report_has_every_property_the_docstring_claims():
    """Eight properties, and the report says all eight."""
    status = Q.status()
    for name in ("experimental", "is_default_forecaster",
                 "automatically_registered", "usable_in_live_trading",
                 "production_eligible", "promotable",
                 "autonomously_selectable", "enabled"):
        assert name in status, name
    assert status["usable_in_live_trading"] is False
    assert status["promotable"] is False
    assert status["summary"].startswith("quarantined:")
    assert "B8" in status["summary"]
