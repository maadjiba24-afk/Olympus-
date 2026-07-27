"""Routing substitution within measured quality bands (Wave-2 C3, `routesub`).

What these prove (Wave-2 acceptance gates A4, A5, A11):

  A4  Every precondition individually blocks a substitution. Each test below
      satisfies EVERY other precondition and breaks exactly one, then asserts
      that this one is the ONLY failing check — so no precondition is
      accidentally load-bearing for another.
  A5  **W2-I3.1 Aletheia is never substituted below its measured verification
      floor** — including the case where the cheaper candidate is otherwise
      fully qualified, and including with the relaxable doctrine protection
      explicitly turned off.
  A11 **W2-I3.2 no silent degradation** — a substitution that lowers the
      measured quality lower bound always carries a disclosure.

Plus the mode contract (off = fully inert, shadow = records but never
substitutes, on = substitutes), the always-recorded row schema, outcome
completion, and the agreement telemetry math.
"""

from __future__ import annotations

import json
import time

import pytest

from olympus import config, modelgrade, routesub

# --- pool members ----------------------------------------------------------
# Prices come from config.price_per_mtok: opus 30.0, haiku 1.5, gemini 3.0,
# deepseek 0.5 — so haiku/deepseek are genuinely CHEAPER candidates.
OPUS = config.Settings(provider="anthropic", model="claude-opus-4-8",
                       api_key="k")
HAIKU = config.Settings(provider="anthropic", model="claude-haiku-4",
                        api_key="k")
GEMINI = config.Settings(provider="openai", model="gemini-2.5-pro", api_key="k")
DEEPSEEK = config.Settings(provider="openai", model="deepseek-chat",
                           api_key="k")

OPUS_ID = "anthropic/claude-opus-4-8"
HAIKU_ID = "anthropic/claude-haiku-4"

CELL = modelgrade.cell_of("code", "en", 2000)
CELL_B = modelgrade.cell_of("finance", "en", 2000)
CELL_BIG = modelgrade.cell_of("code", "en", 200_000)
CELL_TOOLS = modelgrade.cell_of("code", "en", 2000, needs_tools=True)
CELL_STRUCT = modelgrade.cell_of("code", "en", 2000, needs_structured=True)
VERIFY_CELL = modelgrade.cell_of("verify", "en", 2000)


@pytest.fixture(autouse=True)
def _clean_warmth():
    routesub.clear_warmth()
    yield
    routesub.clear_warmth()


@pytest.fixture
def env(monkeypatch):
    """A configuration in which a substitution is legal — every precondition
    satisfied. Each precondition test breaks exactly one thing on top of it.

    The band and confidence thresholds are pinned explicitly rather than left at
    their PROVISIONAL defaults so a later calibration of those constants cannot
    silently turn these tests into vacuous ones."""
    monkeypatch.setenv("OLYMPUS_MODELGRADE", "1")
    monkeypatch.setenv("OLYMPUS_ROUTESUB", "on")
    monkeypatch.setenv("OLYMPUS_ROUTESUB_BAND", "0.25")
    monkeypatch.setenv("OLYMPUS_ROUTESUB_CONFIDENCE", "0.5")
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    monkeypatch.delenv("OLYMPUS_SOVEREIGN", raising=False)
    yield


def seed(member, cell, *, success=1.0, n=40, dimension="quality", ts=None):
    """Record measured evidence for one member x cell (the only way a member
    can become QUALIFIED — a manual claim never promotes, W2-I1.1)."""
    extra = {"ts": ts} if ts is not None else {}
    assert modelgrade.observe(provider=member.provider, model=member.model,
                              cell=cell, dimension=dimension, success=success,
                              n=n, source="test", **extra)


def seed_pair(cell=CELL, *, preferred=OPUS, candidate=HAIKU,
              pref_success=1.0, cand_success=1.0, cand_ts=None):
    seed(preferred, cell, success=pref_success)
    seed(candidate, cell, success=cand_success, ts=cand_ts)


def decide(cell=CELL, *, members=(OPUS, HAIKU), specialist="hephaestus",
           preferred=OPUS, **kwargs):
    return routesub.evaluate(members=list(members), specialist=specialist,
                             preferred=preferred, cell=cell, **kwargs)


def assert_only_blocker(dec, name):
    """The strongest form of "this precondition blocks independently": the
    decision refused, `blocked_by` names this precondition, and every OTHER
    precondition passed."""
    assert not dec.substituted, dec.reason
    assert not dec.counterfactual, dec.reason
    assert dec.blocked_by == name, (dec.blocked_by, dec.checks)
    failed = sorted(k for k, v in dec.checks.items() if not v["ok"])
    assert failed == [name], failed
    assert dec.selected is OPUS or dec.selected is None


# --- mode contract ---------------------------------------------------------

def test_default_mode_is_off_and_module_is_importable():
    assert routesub.mode() == routesub.OFF
    assert not routesub.enabled()
    assert routesub.PRECONDITIONS  # the documented precondition set


def test_off_mode_is_fully_inert(monkeypatch):
    """Off => no computation, no evidence read, no files at all."""
    monkeypatch.setenv("OLYMPUS_MODELGRADE", "1")
    monkeypatch.delenv("OLYMPUS_ROUTESUB", raising=False)
    seed_pair()
    dec = decide()
    assert dec.mode == routesub.OFF
    assert not dec.substituted and not dec.counterfactual
    assert dec.checks == {}                      # nothing was even evaluated
    assert dec.selected is OPUS
    assert not routesub.ledger_path().exists()
    assert not (config.MEMORY_DIR / "routesub").exists()
    assert routesub.decisions() == []
    assert routesub.agreement_stats()["decisions"] == 0
    assert not (config.MEMORY_DIR / "routesub").exists()


def test_replay_forces_off(monkeypatch, env):
    monkeypatch.setenv("OLYMPUS_REPLAY", "1")
    assert routesub.mode() == routesub.OFF
    seed_pair()
    assert not decide().substituted


def test_unknown_mode_value_fails_safe_to_off(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ROUTESUB", "yolo")
    assert routesub.mode() == routesub.OFF


def test_fully_satisfied_case_substitutes_in_on_mode(env):
    seed_pair()
    dec = decide(context_tokens=2000)
    assert dec.substituted, dec.reason
    assert dec.blocked_by == ""
    assert all(c["ok"] for c in dec.checks.values())
    assert dec.preferred_route == OPUS_ID
    assert dec.candidate_route == HAIKU_ID
    assert dec.substituted_route == HAIKU_ID
    assert dec.selected is HAIKU
    # opus $30/Mtok vs haiku $1.5/Mtok over 2000 estimated tokens.
    assert dec.estimated_savings_usd == pytest.approx(0.057)
    assert dec.recorded
    rows = routesub.decisions()
    assert len(rows) == 1 and rows[0]["substituted"] is True


def test_shadow_mode_records_the_counterfactual_but_never_substitutes(
        monkeypatch, env):
    monkeypatch.setenv("OLYMPUS_ROUTESUB", "shadow")
    seed_pair()
    dec = decide(context_tokens=2000)

    # routing is unchanged...
    assert dec.substituted is False
    assert dec.substituted_route == ""
    assert dec.selected is OPUS
    # ...but the counterfactual was computed AND written.
    assert dec.counterfactual is True
    assert dec.blocked_by == ""
    rows = routesub.decisions()
    assert len(rows) == 1
    assert rows[0]["counterfactual"] is True
    assert rows[0]["substituted"] is False
    assert rows[0]["candidate_route"] == HAIKU_ID
    assert rows[0]["substituted_route"] == ""
    stats = routesub.agreement_stats()
    assert stats["counterfactuals"] == 1 and stats["substitutions"] == 0
    assert stats["counterfactual_savings_usd"] == pytest.approx(0.057)


# --- A4: every precondition individually blocks ----------------------------

def test_precondition_candidate_available(env):
    """No cheaper/warmer member => nothing to substitute (and no other check is
    even attempted, since there is no candidate to evaluate)."""
    seed_pair()
    dec = decide(members=(OPUS,))
    assert not dec.substituted
    assert dec.blocked_by == "candidate_available"
    assert dec.candidate_route == ""


def test_precondition_modelgrade_disabled_is_fail_safe(monkeypatch, env):
    """The evidence ladder disabled => NO substitution (never a default yes)."""
    monkeypatch.setenv("OLYMPUS_MODELGRADE", "1")
    seed_pair()
    monkeypatch.setenv("OLYMPUS_MODELGRADE", "0")
    dec = decide()
    assert not dec.substituted and not dec.counterfactual
    assert dec.blocked_by == "modelgrade"
    assert dec.selected is OPUS


def test_precondition_modelgrade_import_failure_is_fail_safe(monkeypatch, env):
    """The module missing entirely (this ships before/without modelgrade) =>
    the lazy import raises, is swallowed, and routing keeps its pick."""
    seed_pair()

    def boom():
        raise ImportError("no module named olympus.modelgrade")

    monkeypatch.setattr(routesub, "_import_modelgrade", boom)
    dec = decide()
    assert not dec.substituted and not dec.counterfactual
    assert dec.blocked_by == "modelgrade"
    assert dec.checks["modelgrade"]["ok"] is False
    assert dec.selected is OPUS
    # the fail-safe decision is still recorded — a refusal is evidence too
    assert routesub.decisions()[0]["blocked_by"] == "modelgrade"


def _patch_unqualified(monkeypatch, member_id):
    real = modelgrade.qualified

    def fake(member, cell=modelgrade.CELL_ANY, *, now=None):
        if member == member_id:
            return False
        return real(member, cell, now=now)

    monkeypatch.setattr(modelgrade, "qualified", fake)


def test_precondition_preferred_qualified(monkeypatch, env):
    seed_pair()
    _patch_unqualified(monkeypatch, OPUS_ID)
    assert_only_blocker(decide(), "preferred_qualified")


def test_precondition_candidate_qualified(monkeypatch, env):
    seed_pair()
    _patch_unqualified(monkeypatch, HAIKU_ID)
    assert_only_blocker(decide(), "candidate_qualified")


def test_precondition_preferred_qualified_on_real_evidence(env):
    """The same block, reached the honest way: too few measured rows on the
    incumbent, so the ladder has nothing to substitute *within*."""
    seed(OPUS, CELL, n=5)
    seed(HAIKU, CELL)
    dec = decide()
    assert not dec.substituted
    assert dec.blocked_by == "preferred_qualified"


def test_precondition_quality_floor(monkeypatch, env):
    """The candidate's measured lower bound must stay at or above the cell's
    minimum quality floor, even when it is otherwise qualified."""
    seed_pair()
    monkeypatch.setenv("OLYMPUS_ROUTESUB_QUALITY_FLOOR", "0.99")
    assert modelgrade.qualified(HAIKU_ID, CELL)      # qualified, still refused
    assert_only_blocker(decide(), "quality_floor")


def test_precondition_quality_band(monkeypatch, env):
    """A measurably worse candidate outside the equivalence band is refused
    even though it clears the floor and is qualified."""
    seed_pair(cand_success=0.9)                      # lo ~0.769 vs ~0.912
    monkeypatch.setenv("OLYMPUS_ROUTESUB_BAND", "0.05")
    assert modelgrade.qualified(HAIKU_ID, CELL)
    assert_only_blocker(decide(), "quality_band")


def test_precondition_security_class(env):
    """A restricted request is never substituted."""
    seed_pair()
    assert_only_blocker(decide(security_class="restricted"), "security_class")


def test_precondition_security_never_leaves_a_local_member(monkeypatch, env):
    """Substitution can never move work from a local member to a remote one
    (04-routing R3 §6 — it must not be able to resurrect a remote model)."""
    seed_pair()
    monkeypatch.setattr(config, "member_is_local",
                        lambda s: s.model == OPUS.model)
    assert_only_blocker(decide(), "security_class")


def test_precondition_context_window(env):
    """The candidate must actually fit the request."""
    seed(GEMINI, CELL_BIG)
    seed(DEEPSEEK, CELL_BIG)
    dec = decide(CELL_BIG, members=(GEMINI, DEEPSEEK), preferred=GEMINI,
                 context_tokens=200_000)
    assert not dec.substituted
    assert dec.blocked_by == "context_window"
    failed = sorted(k for k, v in dec.checks.items() if not v["ok"])
    assert failed == ["context_window"], failed


def test_precondition_tools(env):
    """Tool support must be MEASURED on the candidate, not assumed."""
    seed_pair(CELL_TOOLS)
    assert_only_blocker(decide(CELL_TOOLS, needs_tools=True), "tools")


def test_precondition_tools_satisfied_by_measured_evidence(env):
    seed_pair(CELL_TOOLS)
    seed(OPUS, CELL_TOOLS, dimension="tools")
    seed(HAIKU, CELL_TOOLS, dimension="tools")
    assert decide(CELL_TOOLS, needs_tools=True).substituted


def test_precondition_structured(env):
    seed_pair(CELL_STRUCT)
    assert_only_blocker(decide(CELL_STRUCT, needs_structured=True),
                        "structured")


def test_precondition_freshness(monkeypatch, env):
    """Evidence older than the substitution TTL cannot authorise a swap, even
    while it is still inside modelgrade's own (longer) qualification TTL."""
    seed_pair(cand_ts=time.time() - 5 * 86400)
    monkeypatch.setenv("OLYMPUS_ROUTESUB_TTL_DAYS", "1")
    assert modelgrade.qualified(HAIKU_ID, CELL)
    assert_only_blocker(decide(), "freshness")


def test_precondition_confidence(monkeypatch, env):
    seed_pair()
    monkeypatch.setenv("OLYMPUS_ROUTESUB_CONFIDENCE", "0.99")
    assert_only_blocker(decide(), "confidence")


# --- A5 / W2-I3.1: the Aletheia floor --------------------------------------

def test_aletheia_never_substituted_below_the_verification_floor(
        monkeypatch, env):
    """THE hard rule. The cheaper candidate is OTHERWISE FULLY QUALIFIED —
    modelgrade says `qualified`, it clears the cell floor, the band, security,
    context, freshness and confidence — and the doctrine-level protection of
    the verify role is explicitly relaxed. It is still refused, because its
    measured verification score is below the verifier floor."""
    monkeypatch.setenv("OLYMPUS_ROUTESUB_PROTECT_VERIFIER", "0")
    seed_pair(VERIFY_CELL, pref_success=0.9, cand_success=0.9)  # lo ~0.769

    assert modelgrade.qualified(HAIKU_ID, VERIFY_CELL)   # otherwise qualified
    dec = decide(VERIFY_CELL, specialist="aletheia")
    assert_only_blocker(dec, "verifier_floor")
    assert dec.verification_score == pytest.approx(0.7695, abs=1e-3)
    assert dec.verification_score < routesub.verifier_floor()
    assert dec.selected is OPUS


def test_aletheia_below_floor_is_not_even_a_shadow_counterfactual(
        monkeypatch, env):
    monkeypatch.setenv("OLYMPUS_ROUTESUB", "shadow")
    monkeypatch.setenv("OLYMPUS_ROUTESUB_PROTECT_VERIFIER", "0")
    seed_pair(VERIFY_CELL, pref_success=0.9, cand_success=0.9)
    dec = decide(VERIFY_CELL, specialist="aletheia")
    assert not dec.substituted and not dec.counterfactual
    assert routesub.decisions()[0]["blocked_by"] == "verifier_floor"


def test_verify_role_is_protected_by_default_even_above_the_floor(env):
    """Doctrine on top of the hard rule: with the default configuration the
    head of the routing is not substituted at all, floor or no floor."""
    seed_pair(VERIFY_CELL)                     # lo ~0.912, above the 0.90 floor
    dec = decide(VERIFY_CELL, specialist="aletheia")
    assert not dec.substituted
    assert dec.checks["verifier_floor"]["ok"] is True
    assert dec.blocked_by == "verifier_protected"


def test_verifier_detection_survives_a_renamed_caller(monkeypatch, env):
    """The floor keys off the WORK, not the caller's name: a verify cell is
    verification even if the specialist is called something else."""
    monkeypatch.setenv("OLYMPUS_ROUTESUB_PROTECT_VERIFIER", "0")
    seed_pair(VERIFY_CELL, pref_success=0.9, cand_success=0.9)
    dec = decide(VERIFY_CELL, specialist="not-aletheia")
    assert dec.blocked_by == "verifier_floor"
    assert routesub.is_verification("not-aletheia", VERIFY_CELL.key)


def test_aletheia_floor_holds_when_the_candidate_is_cheaper_and_warmer(
        monkeypatch, env):
    monkeypatch.setenv("OLYMPUS_ROUTESUB_PROTECT_VERIFIER", "0")
    seed_pair(VERIFY_CELL, pref_success=0.9, cand_success=0.9)
    routesub.mark_warm(HAIKU)
    dec = decide(VERIFY_CELL, specialist="aletheia")
    assert not dec.substituted
    assert dec.blocked_by == "verifier_floor"


# --- A11 / W2-I3.2: no silent degradation ----------------------------------

def test_degradation_requires_a_disclosure(env):
    seed_pair(cand_success=0.975)               # lo ~0.871 vs ~0.912
    dec = decide()
    assert dec.substituted
    assert dec.user_visible_degradation is True
    assert dec.requires_disclosure() is True
    assert dec.disclosure                       # non-empty, names both routes
    assert HAIKU_ID in dec.disclosure and OPUS_ID in dec.disclosure
    assert routesub.decisions()[0]["disclosure"] == dec.disclosure


def test_equal_measured_quality_is_not_a_degradation(env):
    seed_pair()
    dec = decide()
    assert dec.substituted
    assert dec.user_visible_degradation is False
    assert dec.requires_disclosure() is False
    assert dec.disclosure == ""


def test_a_blocked_decision_never_claims_degradation(env):
    seed_pair(cand_success=0.975)
    dec = decide(security_class="restricted")
    assert not dec.substituted
    assert dec.user_visible_degradation is False
    assert dec.disclosure == ""


def test_every_recorded_degradation_carries_a_disclosure(env):
    seed_pair(CELL, cand_success=0.975)
    seed_pair(CELL_B)
    decide(CELL)
    decide(CELL_B)
    decide(CELL, security_class="restricted")
    rows = routesub.decisions()
    assert len(rows) == 3
    for row in rows:
        if row["user_visible_degradation"]:
            assert row["disclosure"], row


# --- the always-recorded row ----------------------------------------------

def test_every_decision_row_carries_all_required_fields(env):
    seed_pair(CELL, cand_success=0.975)
    decide(CELL, context_tokens=2000)                 # a substitution
    decide(CELL, security_class="restricted")         # a refusal
    decide(CELL, members=(OPUS,))                     # no candidate at all
    lines = routesub.ledger_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        row = json.loads(line)
        missing = [f for f in routesub.ROW_FIELDS if f not in row]
        assert not missing, missing
        # the W2-C3 §5 list, explicitly
        assert row["preferred_route"] == OPUS_ID
        assert "substituted_route" in row and "reason" in row
        assert "estimated_savings_usd" in row and "actual_cost_usd" in row
        assert "latency_ms" in row and "verifier_outcome" in row
        assert "disagreement" in row and "fallback" in row
        assert "user_visible_degradation" in row
        assert set(routesub.PRECONDITIONS) >= set(row["checks"])


def test_record_outcome_completes_a_row(env):
    seed_pair()
    dec = decide(context_tokens=2000)
    assert dec.substituted
    assert routesub.record_outcome(dec.decision_id, actual_cost=0.0031,
                                   latency_ms=1234, verifier_outcome="approve")
    row = routesub.decision(dec.decision_id)
    assert row["outcome_recorded"] is True
    assert row["actual_cost_usd"] == pytest.approx(0.0031)
    assert row["latency_ms"] == 1234
    assert row["verifier_outcome"] == "approve"
    assert row["disagreement"] is False
    assert row["fallback"] is False
    # the decision half is untouched — the ledger is append-only
    assert row["substituted"] is True
    assert row["estimated_savings_usd"] == pytest.approx(0.057)
    assert len(routesub.decisions()) == 1


def test_record_outcome_treats_a_negative_verdict_as_disagreement(env):
    seed_pair()
    dec = decide()
    routesub.record_outcome(dec.decision_id, actual_cost=0.004, latency_ms=10,
                            verifier_outcome="reject")
    assert routesub.decision(dec.decision_id)["disagreement"] is True


def test_record_outcome_records_a_fallback_activation(env):
    seed_pair()
    dec = decide()
    routesub.record_outcome(dec.decision_id, actual_cost=0.01, latency_ms=99,
                            verifier_outcome="approve", fallback=True)
    row = routesub.decision(dec.decision_id)
    assert row["fallback"] is True


def test_record_outcome_never_raises_on_junk(env):
    assert routesub.record_outcome("", actual_cost=None, latency_ms=None,
                                   verifier_outcome=None) is False
    assert routesub.record_outcome("nope", actual_cost="x", latency_ms="y",
                                   verifier_outcome=None) is True
    # an outcome with no decision half is simply not surfaced
    assert routesub.decision("nope") is None


# --- agreement telemetry ---------------------------------------------------

def test_agreement_stats_math(env):
    seed_pair(CELL)
    seed_pair(CELL_B)
    d1 = decide(CELL, context_tokens=2000)                    # substituted
    d2 = decide(CELL, security_class="restricted")            # refused
    d3 = decide(CELL_B, context_tokens=2000)                  # substituted
    assert d1.substituted and d3.substituted and not d2.substituted

    routesub.record_outcome(d1.decision_id, actual_cost=0.001, latency_ms=900,
                            verifier_outcome="approve")
    routesub.record_outcome(d3.decision_id, actual_cost=0.004, latency_ms=1500,
                            verifier_outcome="reject")

    stats = routesub.agreement_stats(days=7)
    assert stats["mode"] == "on"
    assert stats["decisions"] == 3
    assert stats["substitutions"] == 2
    assert stats["substitution_rate"] == pytest.approx(2 / 3, abs=1e-5)
    assert stats["completed"] == 2
    assert stats["disagreements"] == 1
    assert stats["disagreement_rate"] == pytest.approx(0.5)

    # savings: estimated at decision time vs what actually happened
    assert stats["savings"]["estimated_usd"] == pytest.approx(0.114)
    assert stats["savings"]["actual_usd"] == pytest.approx(
        (0.06 - 0.001) + (0.06 - 0.004))
    assert stats["savings"]["delta_usd"] == pytest.approx(0.001)

    # per-cell split
    a = stats["by_cell"][CELL.key]
    b = stats["by_cell"][CELL_B.key]
    assert a["decisions"] == 2 and a["substitutions"] == 1
    assert a["substitution_rate"] == pytest.approx(0.5)
    assert a["disagreements"] == 0 and a["disagreement_rate"] == 0.0
    assert b["decisions"] == 1 and b["substitutions"] == 1
    assert b["substitution_rate"] == pytest.approx(1.0)
    assert b["disagreements"] == 1 and b["disagreement_rate"] == pytest.approx(1.0)
    assert b["actual_savings_usd"] == pytest.approx(0.056)

    assert stats["verifier_outcomes"] == {"approve": 1, "reject": 1}
    assert stats["blocked_by"] == {"security_class": 1}


def test_agreement_stats_windows_by_days(env):
    seed_pair()
    dec = decide()
    assert routesub.agreement_stats(days=7)["decisions"] == 1
    stale = routesub.agreement_stats(days=7, now=dec.ts + 30 * 86400)
    assert stale["decisions"] == 0
    assert stale["substitution_rate"] == 0.0


def test_agreement_stats_is_empty_without_a_ledger(env):
    stats = routesub.agreement_stats()
    assert stats["decisions"] == 0 and stats["substitution_rate"] == 0.0
    assert stats["by_cell"] == {}


# --- candidate ordering, adapter, robustness ------------------------------

def test_warmth_makes_an_equal_price_member_a_candidate(env):
    """Warmth is the second substitution reason (Colibri residency); with no
    warmth signal an equally-priced member is not a candidate at all."""
    twin = config.Settings(provider="anthropic", model="claude-opus-4-8-eu",
                           api_key="k")
    seed(OPUS, CELL)
    seed(twin, CELL)
    assert decide(members=(OPUS, twin)).blocked_by == "candidate_available"
    routesub.mark_warm(twin)
    dec = decide(members=(OPUS, twin))
    assert dec.substituted and dec.candidate_route == "anthropic/claude-opus-4-8-eu"


def test_choose_adapter_defers_when_blocked_and_substitutes_when_legal(env):
    seed_pair()
    assert routesub.choose([OPUS, HAIKU], "hephaestus", OPUS,
                           cell=CELL) is HAIKU
    assert routesub.choose([OPUS], "hephaestus", OPUS, cell=CELL) is None


def test_choose_is_inert_when_off(monkeypatch, env):
    monkeypatch.setenv("OLYMPUS_ROUTESUB", "off")
    seed_pair()
    assert routesub.choose([OPUS, HAIKU], "hephaestus", OPUS, cell=CELL) is None


def test_evaluate_never_raises_and_keeps_the_preferred_route(env):
    """Any internal failure degrades to "no substitution", never an exception
    into the routing path."""
    class Boom:
        provider = "anthropic"

        @property
        def model(self):
            raise RuntimeError("hostile member")

    dec = routesub.evaluate(members=[OPUS, Boom()], specialist="hephaestus",
                            preferred=OPUS, cell=CELL)
    assert not dec.substituted
    assert dec.selected is OPUS


def test_a_torn_ledger_line_is_skipped_not_repaired(env):
    seed_pair()
    decide()
    with routesub.ledger_path().open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    assert len(routesub.decisions()) == 1
    assert routesub.agreement_stats()["decisions"] == 1


def test_status_reports_the_active_thresholds(env):
    seed_pair()
    st = routesub.status()
    assert st["mode"] == "on" and st["active"] is True
    assert st["modelgrade"] is True
    assert st["thresholds"]["verifier_floor"] == pytest.approx(0.9)
    assert st["thresholds"]["band"] == pytest.approx(0.25)
