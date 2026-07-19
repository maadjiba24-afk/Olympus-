"""Parked-item 6 — offline RL preference-data + reward-model SCAFFOLD.

Not a live training loop: it turns already-logged, human-labeled outcomes into
preference pairs, fits a transparent linear reward model OFFLINE (deterministic,
no ML dep, no model call), gates on real data (synthetic rows excluded), is
default-off for fit/export, signs its exports, and — critically — has NO path
that changes any decision. These tests pin all of that.
"""

import pytest

from olympus import (config, deltas, outcomes, rlscaffold, routing_outcomes,
                     store, witness)

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "parked6-seed")
    monkeypatch.delenv("OLYMPUS_RL_SCAFFOLD", raising=False)
    monkeypatch.setattr(deltas, "_locks", {})
    # A fresh store per test so logs don't bleed across tests.
    store.reset()
    yield
    store.reset()


def _routing_append(user, row):
    """Append one routing-outcome row straight to its store (mirrors the
    persistence in routing_outcomes.record_run)."""
    log = routing_outcomes._load(user)
    log.append(row)
    routing_outcomes._save(user, log)


def _seed_routing(user="u1", n_ctx=4, per_arm=8):
    """Seed real (non-synthetic) routing outcomes: in each context a good model
    gets POSITIVE and a bad model gets NEGATIVE — a clean, learnable signal."""
    for c in range(n_ctx):
        tt = f"task{c}"
        for i in range(per_arm):
            _routing_append(user, {
                "ts": 0, "run_id": f"{tt}-w{i}", "user": user,
                "specialist": "hephaestus", "model": "opus", "role": "primary",
                "task_type": tt, "length_bucket": "m",
                "outcome_signal": routing_outcomes.POSITIVE,
                "signal_source": "feedback", "synthetic": False})
            _routing_append(user, {
                "ts": 0, "run_id": f"{tt}-l{i}", "user": user,
                "specialist": "hermes", "model": "haiku", "role": "primary",
                "task_type": tt, "length_bucket": "m",
                "outcome_signal": routing_outcomes.NEGATIVE,
                "signal_source": "feedback", "synthetic": False})


# --- preference-pair collection ---------------------------------------------

def test_routing_pairs_are_matched_context_and_feature_differentiated():
    _seed_routing(n_ctx=3, per_arm=2)
    pairs = rlscaffold.collect_routing_pairs()
    assert pairs, "expected routing preference pairs"
    for p in pairs:
        assert p.source == "routing"
        # chosen is the winner's features, rejected the loser's — and they differ.
        assert p.chosen != p.rejected
        assert "specialist=hephaestus" in p.chosen
        assert "specialist=hermes" in p.rejected


def test_synthetic_and_pending_rows_are_excluded():
    _routing_append("s", {"specialist": "hephaestus", "model": "opus",
                          "role": "primary", "task_type": "t", "length_bucket": "m",
                          "outcome_signal": routing_outcomes.POSITIVE,
                          "synthetic": True})
    _routing_append("s", {"specialist": "hermes", "model": "haiku",
                          "role": "primary", "task_type": "t", "length_bucket": "m",
                          "outcome_signal": routing_outcomes.NEGATIVE,
                          "synthetic": True})
    _routing_append("s", {"specialist": "hephaestus", "model": "opus",
                          "role": "primary", "task_type": "t", "length_bucket": "m",
                          "outcome_signal": routing_outcomes.PENDING,
                          "synthetic": False})
    # All synthetic or pending → no learnable pairs.
    assert rlscaffold.collect_routing_pairs() == []


def test_action_outcomes_become_pairs_vs_baseline():
    outcomes.record("bob", "send_email", outcomes.APPROVED)
    outcomes.record("bob", "wire_money", outcomes.REJECTED)
    pairs = rlscaffold.collect_action_pairs(["bob"])
    # APPROVED → chosen has features, rejected is baseline (empty).
    assert any(p.chosen and not p.rejected for p in pairs)
    # REJECTED → baseline beats the action.
    assert any(p.rejected and not p.chosen for p in pairs)


# --- the offline reward model -----------------------------------------------

def test_reward_model_learns_to_rank_and_is_deterministic():
    _seed_routing(n_ctx=4, per_arm=6)
    pairs = rlscaffold.collect_pairs()
    m1 = rlscaffold.RewardModel(); s1 = m1.fit(pairs)
    m2 = rlscaffold.RewardModel(); s2 = m2.fit(pairs)
    # Deterministic: identical weights across independent fits.
    assert m1.to_dict() == m2.to_dict()
    # It actually learned the signal: the good specialist outscores the bad one.
    assert m1.score({"specialist=hephaestus": 1.0}) > \
        m1.score({"specialist=hermes": 1.0})
    assert s1["accuracy"] >= 0.9 and s1["fitted"]


def test_top_features_are_interpretable():
    _seed_routing(n_ctx=4, per_arm=6)
    m = rlscaffold.RewardModel(); m.fit(rlscaffold.collect_pairs())
    top = m.top_features()
    pos = dict(top["most_positive"])
    neg = dict(top["most_negative"])
    assert "specialist=hephaestus" in pos
    assert "specialist=hermes" in neg


def test_fit_on_no_data_is_safe():
    m = rlscaffold.RewardModel()
    s = m.fit([])
    assert s["pairs"] == 0 and s["fitted"] is False and m.weights == {}


# --- the data gate ----------------------------------------------------------

def test_gate_not_met_on_thin_data():
    _seed_routing(n_ctx=1, per_arm=1)      # 1 pair, 1 context
    g = rlscaffold.gate_status()
    assert not g["met"] and g["reasons"]


def test_gate_met_on_sufficient_real_data():
    _seed_routing(n_ctx=4, per_arm=6)      # 4 contexts, 36 pairs
    g = rlscaffold.gate_status()
    assert g["met"], g["reasons"]
    assert g["contexts"] >= rlscaffold.GATE_MIN_CONTEXTS
    assert g["pairs"] >= rlscaffold.GATE_MIN_PAIRS


# --- gating: fit/export require the operator opt-in --------------------------

def test_disabled_by_default_for_fit_and_export():
    assert not rlscaffold.enabled()
    _seed_routing(n_ctx=4, per_arm=6)
    with pytest.raises(rlscaffold.RLScaffoldError):
        rlscaffold.collect_report(fit=True)
    with pytest.raises(rlscaffold.RLScaffoldError):
        rlscaffold.export()


def test_readonly_report_works_without_optin():
    _seed_routing(n_ctx=4, per_arm=6)
    # No OLYMPUS_RL_SCAFFOLD — a read-only report (no fit) is still allowed.
    report = rlscaffold.collect_report(fit=False)
    assert report["model"] is None
    assert report["pairs"] >= rlscaffold.GATE_MIN_PAIRS
    assert report["by_source"].get("routing")


# --- signed export ----------------------------------------------------------

@_signing
def test_export_is_signed_and_content_safe(monkeypatch):
    monkeypatch.setenv("OLYMPUS_RL_SCAFFOLD", "1")
    _seed_routing(n_ctx=4, per_arm=6)
    report = rlscaffold.export(fit=True)
    assert report["snapshot_hash"]
    assert deltas.verify_history(rlscaffold.RL_DATASET)["ok"]
    # The signed record is categorical + numeric ONLY: pair/source counts,
    # context tags, scoreboard counts, and reward weights keyed by categorical
    # decision features — no message body or other free-text field exists.
    state = rlscaffold.history()[-1]["state"]
    assert set(state) == {"pairs", "by_source", "contexts", "gate",
                          "scoreboard_health", "model"}
    assert state["model"]["fitted"]
    # Feature keys are decision categories (specialist/model/role), never text.
    for feat in state["model"]["weights"]:
        assert feat.split("=", 1)[0] in ("specialist", "model", "role", "action")


def test_export_below_gate_reports_unfitted(monkeypatch):
    monkeypatch.setenv("OLYMPUS_RL_SCAFFOLD", "1")
    _seed_routing(n_ctx=1, per_arm=1)      # below gate
    report = rlscaffold.collect_report(fit=True)
    assert report["model"]["fitted"] is False
    assert report["model"]["gate_reasons"]


# --- the load-bearing safety property: no autonomous behavior change ---------

def test_no_decision_module_imports_the_scaffold():
    """The scaffold must be advisory-only: no routing/decision module may import
    it, so a fitted reward can never feed back into behavior autonomously."""
    import inspect
    from olympus import orchestrator, learned_routing, sleeptime
    for mod in (orchestrator, learned_routing, sleeptime):
        src = inspect.getsource(mod)
        assert "rlscaffold" not in src, (
            f"{mod.__name__} must not import the RL scaffold — it is advisory "
            "only; a reward must never autonomously change behavior")


def test_ONLY_the_cli_imports_the_scaffold_packagewide():
    """H5 hardening: the load-bearing 'no autonomous behavior change' guarantee,
    checked EXHAUSTIVELY rather than against a hand-picked few. `rlscaffold` may
    be referenced ONLY by the operator CLI entry point (and itself) — any other
    module importing it would be a potential write-back path into behavior, so a
    future accidental wire-up fails this test."""
    import pathlib
    import olympus
    pkg = pathlib.Path(olympus.__file__).resolve().parent
    allowed = {"rlscaffold.py", "cli.py"}
    offenders = []
    for p in pkg.rglob("*.py"):
        if p.name in allowed:
            continue
        src = p.read_text(encoding="utf-8")
        if "rlscaffold" in src:
            offenders.append(p.name)
    assert not offenders, (
        f"only the CLI may reference the RL scaffold; found references in "
        f"{offenders} — the scaffold must never be wired into a decision path")


def test_scaffold_never_writes_env_or_config():
    """Like the supervision harness, it must be architecturally incapable of
    flipping a switch: no os.environ writes, no config mutation in the source."""
    import inspect
    src = inspect.getsource(rlscaffold)
    assert "os.environ[" not in src and "environ.pop" not in src
    assert "setenv" not in src
