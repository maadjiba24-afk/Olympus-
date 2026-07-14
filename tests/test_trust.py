"""Earned per-domain autonomy: trust is a pure function of the immutable audit
log — earned slowly over a clean run, snapped back to zero by any surprise, and
never able to auto-run an irreversible action or lift a capped conversation.
"""

import uuid

import pytest

from olympus import actions, capprofile, config, memory, prefs, trust


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.delenv("OLYMPUS_EARNED_AUTONOMY", raising=False)


def _run(user, domain, status, ts, type_name="browser_operate",
         risk=actions.NOTABLE, executed_at=None):
    """Write one governed operator action into the audit trail directly."""
    a = actions.Action(
        id=uuid.uuid4().hex[:12], user=memory.safe_id(user), type=type_name,
        title="op", payload={"domain": domain}, risk_class=risk,
        reversible=(risk in (actions.TRIVIAL, actions.NOTABLE)),
        status=status, created_at=ts, executed_at=executed_at)
    actions._save(a)
    return a


# --- the streak: derived, not stored -------------------------------------

def test_streak_counts_clean_runs_and_resets_on_surprise():
    u = "u1"
    for i in range(4):
        _run(u, "shop.com", actions.EXECUTED, ts=100 + i)
    assert trust.streak(u, "shop.com") == 4
    # a single surprise (failed run — how a checkpoint/drift/decline surfaces)
    # snaps the whole streak back to zero…
    _run(u, "shop.com", actions.FAILED, ts=200)
    assert trust.streak(u, "shop.com") == 0
    # …and it has to be rebuilt from scratch.
    _run(u, "shop.com", actions.EXECUTED, ts=300)
    assert trust.streak(u, "shop.com") == 1


@pytest.mark.parametrize("bad", [actions.FAILED, actions.REJECTED, actions.UNDONE])
def test_every_bad_outcome_snaps_back(bad):
    u = "u2"
    for i in range(6):
        _run(u, "site.com", actions.EXECUTED, ts=100 + i)
    _run(u, "site.com", bad, ts=200)
    assert trust.streak(u, "site.com") == 0


def test_streak_is_per_domain():
    u = "u3"
    for i in range(6):
        _run(u, "a.com", actions.EXECUTED, ts=100 + i)
    _run(u, "b.com", actions.FAILED, ts=150)
    assert trust.streak(u, "a.com") == 6 and trust.streak(u, "b.com") == 0


# --- tiers: earned slowly ------------------------------------------------

def test_tier_thresholds():
    u = "u4"
    assert trust.tier(u, "x.com") == trust.PROBATION
    for i in range(5):
        _run(u, "x.com", actions.EXECUTED, ts=100 + i)
    assert trust.tier(u, "x.com") == trust.TRUSTED
    for i in range(15):
        _run(u, "x.com", actions.EXECUTED, ts=200 + i)
    assert trust.tier(u, "x.com") == trust.ESTABLISHED


# --- the gate modifier ---------------------------------------------------

def test_off_by_default_never_boosts():
    u = "u5"
    for i in range(25):
        _run(u, "proven.com", actions.EXECUTED, ts=100 + i)
    # earned autonomy is opt-in: an established site still gets only the base level
    assert not trust.enabled(u)
    assert trust.effective_autonomy(u, "proven.com") == actions.autonomy_level(u)


def test_enabled_boosts_reversible_on_a_proven_site():
    u = "u6"
    trust.set_enabled(u, True)
    base = actions.autonomy_level(u)                       # L1 default
    for i in range(20):
        _run(u, "proven.com", actions.EXECUTED, ts=100 + i)
    eff = trust.effective_autonomy(u, "proven.com")
    assert eff == actions.L4_STANDING and eff > base       # notable now auto-runs
    # a fresh site with no history stays at base
    assert trust.effective_autonomy(u, "fresh.com") == base


def test_boost_never_exceeds_capability_profile_cap():
    u = "u7"
    trust.set_enabled(u, True)
    capprofile.assign(u, "reader")                         # autonomy capped at L1
    for i in range(20):
        _run(u, "proven.com", actions.EXECUTED, ts=100 + i)
    # an established streak cannot lift a capped conversation past its ceiling
    assert trust.effective_autonomy(u, "proven.com") <= capprofile.autonomy_cap(u)
    assert trust.effective_autonomy(u, "proven.com") == actions.L1_PREPARE


def test_env_switch_enables_globally(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EARNED_AUTONOMY", "1")
    assert trust.enabled("anyone") is True


# --- the hard gate: irreversible never auto-runs, however trusted --------

def _register(name, risk):
    # Distinct throwaway type names so we never clobber the real operate
    # ActionTypes in the shared global registry (test-order independence).
    actions.register(actions.ActionType(
        name=name, risk_class=risk, scope="browser.operate",
        preview=lambda p: "", execute=lambda p: {}))


def test_irreversible_is_never_auto_even_at_max_level():
    u = "u8"
    prefs.set(memory.safe_id(u), "scopes", ["browser.operate"])
    _register("browser_operate_trust_irrev", actions.IRREVERSIBLE)
    a = actions.prepare(u, "browser_operate_trust_irrev", {"domain": "bank.com"})
    # even handed the top autonomy level, an irreversible action holds for approval
    assert actions.can_auto_execute(a, level=actions.L4_STANDING) is False


def test_reversible_auto_runs_with_earned_level_but_respects_cap():
    u = "u9"
    prefs.set(memory.safe_id(u), "scopes", ["browser.operate"])
    _register("browser_operate_trust_rev", actions.NOTABLE)
    a = actions.prepare(u, "browser_operate_trust_rev", {"domain": "shop.com"})
    assert actions.can_auto_execute(a, level=actions.L4_STANDING) is True
    assert actions.can_auto_execute(a, level=actions.L1_PREPARE) is False
    # a caller-supplied level is still clamped by the capability-profile cap
    capprofile.assign(u, "reader")
    assert actions.can_auto_execute(a, level=actions.L4_STANDING) is False


# --- runaway guards: cooling-off + daily ceiling -------------------------

def test_cooling_off_suppresses_boost_after_a_surprise():
    u = "uc"
    trust.set_enabled(u, True)
    # a surprise, then a burst of clean runs that numerically rebuilds trust
    _run(u, "site.com", actions.FAILED, ts=1000)
    for i in range(20):
        _run(u, "site.com", actions.EXECUTED, ts=1001 + i)
    assert trust.tier(u, "site.com") == trust.ESTABLISHED
    # within the cooling-off window the boost is withheld despite the streak…
    soon = 1000 + trust._COOLDOWN_SECS - 5
    assert trust.effective_autonomy(u, "site.com", now=soon) == actions.autonomy_level(u)
    assert trust.cooling_off(u, "site.com", now=soon) > 0
    # …and returns once the site has settled
    later = 1000 + trust._COOLDOWN_SECS + 5
    assert trust.effective_autonomy(u, "site.com", now=later) == actions.L4_STANDING
    assert trust.cooling_off(u, "site.com", now=later) == 0.0


def test_daily_ceiling_falls_back_to_asking():
    u = "ud"
    trust.set_enabled(u, True)
    day = 10 * 86400
    # exactly at the ceiling: every run executed "today"
    for i in range(trust._DAILY_AUTO_CEILING):
        _run(u, "site.com", actions.EXECUTED, ts=day + i, executed_at=day + i)
    at = day + trust._DAILY_AUTO_CEILING
    assert trust.auto_runs_today(u, "site.com", now=at) == trust._DAILY_AUTO_CEILING
    # past the ceiling, earned autonomy withholds the boost (holds for approval)
    assert trust.effective_autonomy(u, "site.com", now=at) == actions.autonomy_level(u)
    # a NEW day resets the count, so the proven site auto-runs again
    next_day = day + 86400
    assert trust.auto_runs_today(u, "site.com", now=next_day) == 0
    assert trust.effective_autonomy(u, "site.com", now=next_day) == actions.L4_STANDING


# --- absorption: operator degradation self-tightens the envelope ---------

def test_operator_degradation_tightens_trust_knobs_and_can_demote():
    from olympus import evolve
    u = "ut"
    trust.set_enabled(u, True)
    # A site earns established trust at the default bar (20 clean runs).
    for i in range(20):
        _run(u, "proven.com", actions.EXECUTED, ts=100 + i)
    assert trust.tier(u, "proven.com") == trust.ESTABLISHED
    assert trust.effective_autonomy(u, "proven.com") == actions.L4_STANDING
    # The operator starts failing across sites → feature evolution tightens the
    # earned-autonomy knobs (higher bar, longer cooldown, lower ceiling).
    for _ in range(10):
        evolve.record("operator", evolve.FAIL, "drift-no-heal")
    evolve.review()
    assert trust.establish_after() > 20
    assert trust.cooldown_secs() > 3600
    assert trust.daily_ceiling() < 25
    # The raised bar demotes the once-established site until it earns more —
    # a failing actuator narrows its own freedom, no human in the loop. The
    # effective level drops below L4, so a notable operator template that used to
    # auto-run now falls back to approval.
    assert trust.tier(u, "proven.com") == trust.TRUSTED
    assert trust.effective_autonomy(u, "proven.com") < actions.L4_STANDING
    # A human widens it back; the site is established again.
    evolve.reset("operator")
    assert trust.tier(u, "proven.com") == trust.ESTABLISHED


# --- surfaces ------------------------------------------------------------

def test_report_and_earned_hint():
    u = "u10"
    trust.set_enabled(u, True)
    for i in range(6):
        _run(u, "trusted.com", actions.EXECUTED, ts=100 + i)
    _run(u, "flaky.com", actions.EXECUTED, ts=90)
    _run(u, "flaky.com", actions.FAILED, ts=95)
    rep = trust.report(u)
    assert "trusted.com" in rep and "trusted" in rep and "ON" in rep
    hint = trust.earned_hint(u)
    assert "trusted.com" in hint and "flaky.com" not in hint     # only earned sites
    # off → no hint, report says so
    trust.set_enabled(u, False)
    assert trust.earned_hint(u) == ""
    assert "off" in trust.report(u)
