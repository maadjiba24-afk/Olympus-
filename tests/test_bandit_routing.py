"""UCB1 bandit routing — exploration, exploitation, determinism, replay-safety."""

import math

import pytest

from olympus import bandit_routing, config, learned_routing, routing_outcomes, store

OPUS = config.Settings(provider="anthropic", model="claude-opus-4-8")
HAIKU = config.Settings(provider="anthropic", model="claude-haiku-4-5")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    store.reset()
    learned_routing.clear_cache()
    monkeypatch.setenv("OLYMPUS_BANDIT_ROUTING", "1")
    yield
    store.reset()


def _seed(specialist, model, verdict, n):
    """Record n outcomes for (specialist, model) with the given review verdict."""
    for i in range(n):
        routing_outcomes.record_run(
            "shared", f"run-{specialist}-{model}-{verdict}-{i}", "a task",
            [specialist], models={specialist: model},
            roles={specialist: "reasoning"}, review_verdict=verdict)


# --- gating --------------------------------------------------------------

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OLYMPUS_BANDIT_ROUTING", raising=False)
    assert bandit_routing.enabled() is False


def test_forced_off_during_replay(monkeypatch):
    monkeypatch.setenv("OLYMPUS_REPLAY", "1")
    assert bandit_routing.enabled() is False


def test_single_member_defers():
    assert bandit_routing.choose([OPUS], "chiron", OPUS) is None


# --- warmup --------------------------------------------------------------

def test_warmup_defers_to_heuristic():
    _seed("chiron", "claude-opus-4-8", "approve", 3)   # < MIN_WARMUP
    assert bandit_routing.choose([OPUS, HAIKU], "chiron", OPUS) is None


# --- exploration: an untried arm is picked first -------------------------

def test_explores_untried_arm():
    # Opus is well-sampled; Haiku has never been tried → UCB +inf → explore it.
    _seed("chiron", "claude-opus-4-8", "approve", 12)
    pick = bandit_routing.choose([OPUS, HAIKU], "chiron", OPUS)
    assert pick is HAIKU


# --- exploitation: the empirically best arm wins once both are known -----

def test_exploits_best_when_both_sampled():
    _seed("chiron", "claude-opus-4-8", "approve", 20)   # mean reward 1.0
    _seed("chiron", "claude-haiku-4-5", "retry", 20)    # mean reward 0.0
    pick = bandit_routing.choose([OPUS, HAIKU], "chiron", HAIKU)
    assert pick is OPUS


def test_deterministic_pick():
    _seed("chiron", "claude-opus-4-8", "approve", 15)
    _seed("chiron", "claude-haiku-4-5", "approve", 15)
    a = bandit_routing.choose([OPUS, HAIKU], "chiron", OPUS)
    b = bandit_routing.choose([OPUS, HAIKU], "chiron", OPUS)
    assert a is b


# --- ucb math ------------------------------------------------------------

def test_ucb_score_unpulled_is_inf():
    assert bandit_routing.ucb_score(0.0, 0, 10) == float("inf")


def test_ucb_score_bonus_shrinks_with_n():
    # same mean, more pulls → smaller exploration bonus
    hi = bandit_routing.ucb_score(5.0, 10, 100)
    lo = bandit_routing.ucb_score(50.0, 100, 100)
    assert math.isclose(hi - 0.5, _bonus(10, 100), rel_tol=1e-6)
    assert _bonus(10, 100) > _bonus(100, 100)


def _bonus(n, total):
    return bandit_routing._C * math.sqrt(math.log(total) / n)


# --- robustness ----------------------------------------------------------

def test_never_raises_on_ledger_error(monkeypatch):
    monkeypatch.setattr(routing_outcomes, "_all_rows",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert bandit_routing.choose([OPUS, HAIKU], "chiron", OPUS) is None


# --- config seam: bandit takes precedence over learned when enabled ------

def test_config_seam_uses_bandit_when_enabled(monkeypatch):
    _seed("chiron", "claude-opus-4-8", "approve", 12)   # haiku untried
    pool = config.ModelPool.of(OPUS, HAIKU)
    # bandit explores the untried haiku, overriding the heuristic's opus lean
    assert pool.for_specialist("chiron") is HAIKU


def test_config_seam_falls_back_to_learned_when_bandit_off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_BANDIT_ROUTING", raising=False)
    monkeypatch.setenv("OLYMPUS_LEARNED_ROUTING", "1")
    pool = config.ModelPool.of(OPUS, HAIKU)
    # no data + learned selector gates closed → heuristic pick, never crashes
    assert pool.for_specialist("chiron") in (OPUS, HAIKU)


# --- status --------------------------------------------------------------

def test_status_reports_arms():
    _seed("chiron", "claude-opus-4-8", "approve", 10)
    st = bandit_routing.status()
    assert st["flag_enabled"] is True and st["active"] is True
    assert any(a["specialist"] == "chiron" for a in st["arms"])


# --- hardening: UCB horizon uses CURRENT arms only (M-bandit-3) ----------

def test_warmup_ignores_removed_model_pulls():
    # 40 pulls on a model no longer in the pool + only 4 on the current arms →
    # the current-arm total (4) is below MIN_WARMUP, so the bandit abstains
    # instead of engaging on inflated stale history.
    _seed("chiron", "gpt-legacy", "approve", 40)        # removed from pool
    _seed("chiron", "claude-opus-4-8", "approve", 4)    # current
    assert bandit_routing.choose([OPUS, HAIKU], "chiron", OPUS) is None


def test_horizon_excludes_removed_model():
    # Enough current-arm data to engage; the removed model's 100 pulls must not
    # distort the pick. Opus (all-positive) should still be chosen over the
    # untried Haiku only via exploration — here we assert a stable, valid pick.
    _seed("chiron", "gpt-legacy", "approve", 100)       # removed
    _seed("chiron", "claude-opus-4-8", "approve", 10)   # current, good
    pick = bandit_routing.choose([OPUS, HAIKU], "chiron", OPUS)
    assert pick in (OPUS, HAIKU)                         # valid in-pool member
    assert pick is HAIKU                                 # untried arm explored first


# --- self-evolution: exploration constant self-tunes (moat) --------------

from olympus import evolve   # noqa: E402


def test_ucb_score_uses_self_tuned_exploration():
    store.reset()
    assert bandit_routing._exploration() == 1.414        # the registered default
    evolve._set_param("bandit", "exploration", 0.5)
    assert bandit_routing._exploration() == 0.5          # picks up the tuned value
    store.reset()


def test_choose_respects_tuned_exploration():
    # At the exploration floor, the higher-MEAN arm wins (small bonus); a
    # lightly-sampled but weaker arm no longer gets explored into the pick.
    store.reset()
    evolve._set_param("bandit", "exploration", 0.3)
    _seed("chiron", "claude-opus-4-8", "approve", 30)    # mean reward 1.0
    _seed("chiron", "claude-haiku-4-5", "approve", 4)
    _seed("chiron", "claude-haiku-4-5", "retry", 8)      # mean reward ~0.33
    pick = bandit_routing.choose([OPUS, HAIKU], "chiron", HAIKU)
    assert pick is OPUS                                  # exploitation wins
    store.reset()


def test_evolution_reduces_exploration_on_degradation():
    store.reset()
    before = bandit_routing._exploration()
    for _ in range(evolve._MIN_SAMPLES + 2):
        evolve.record("bandit", evolve.DEGRADED, "retry")
    evolve.review()
    after = bandit_routing._exploration()
    assert after < before                                # explores less over time
    assert after >= 0.3                                  # clamped to the floor
    store.reset()
