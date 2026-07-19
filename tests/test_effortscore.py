"""ADR 0005 Phase 3: the deterministic difficulty pre-scorer.

Proves: hard signals raise the tier, the per-specialist effort field is a
floor the scorer can raise but never lower below, a retry escalates to the
top tier, and — the acceptance criterion — escalation demonstrably changes
the call parameters on a single-model (single-key) pool where the teacher
path has no stronger member to swap in.
"""

import dataclasses

from olympus import actions, config, effortscore, memory, orchestrator, trace
from olympus.specialists import SPECIALISTS


# --- the pure scorer ------------------------------------------------------

def test_easy_task_stays_low():
    assert effortscore.score() == "low"
    assert effortscore.score(prompt_chars=100, tool_count=3) == "low"


def test_each_hard_signal_raises_one_tier():
    assert effortscore.score(needs_verification=True) == "medium"
    assert effortscore.score(
        prompt_chars=effortscore.LONG_PROMPT_CHARS + 1) == "medium"
    assert effortscore.score(tool_count=effortscore.MANY_TOOLS + 1) == "medium"
    assert effortscore.score(
        needs_verification=True,
        prompt_chars=effortscore.LONG_PROMPT_CHARS + 1) == "high"


def test_very_long_prompt_alone_reaches_high():
    # Kills the review's "routing effort is provably constant" finding: with
    # only prompt length as signal, the top tier must be reachable.
    assert effortscore.score(
        prompt_chars=effortscore.VERY_LONG_PROMPT_CHARS + 1) == "high"


def test_signals_clamp_at_high():
    assert effortscore.score(
        needs_verification=True,
        prompt_chars=10 ** 6,
        tool_count=10 ** 3) == "high"


def test_retry_index_forces_high():
    assert effortscore.score(retry_index=1) == "high"
    assert effortscore.score(retry_index=3, prompt_chars=1) == "high"


def test_risk_class_forces_high():
    assert effortscore.score(risk_class=actions.IRREVERSIBLE) == "high"
    assert effortscore.score(risk_class=actions.FINANCIAL_LEGAL) == "high"
    assert effortscore.score(risk_class=actions.NOTABLE) == "low"


def test_floor_semantics_never_lower():
    assert effortscore.at_least("high", "low") == "high"
    assert effortscore.at_least("low", "medium") == "medium"
    assert effortscore.at_least("medium", "medium") == "medium"
    assert effortscore.at_least("garbage", "low") == "low"   # unknown → low


def test_scorer_is_deterministic_and_pure():
    args = dict(risk_class=actions.TRIVIAL, prompt_chars=1234, tool_count=9,
                retry_index=0, needs_verification=True)
    assert all(effortscore.score(**args) == effortscore.score(**args)
               for _ in range(10))


# --- pipeline integration -------------------------------------------------

def _spy_hermes(monkeypatch, efforts, floor="low"):
    """Swap in a Hermes whose run_counted records the effort it was given.
    The frozen dataclass means we swap the SPECIALISTS entry wholesale."""
    spec = dataclasses.replace(SPECIALISTS["hermes"], effort=floor)

    def fake_run_counted(task, settings=None, effort=None):
        efforts.append(effort)
        return "output", 0

    object.__setattr__(spec, "run_counted", fake_run_counted)
    monkeypatch.setitem(SPECIALISTS, "hermes", spec)
    return spec


def _run_one(bot, task, **kw):
    memory.set_user("effort-test")
    tr = trace.Trace("ask", "effort-test")
    try:
        return bot._run_one("hermes", task, tr, **kw)
    finally:
        tr.flush()


def test_run_one_respects_specialist_floor_on_easy_tasks(monkeypatch):
    efforts = []
    _spy_hermes(monkeypatch, efforts, floor="medium")
    bot = orchestrator.Olympus(user="effort-test")
    _run_one(bot, "short task")
    assert efforts == ["medium"]          # floor holds; scorer said low


def test_run_one_raises_effort_on_hard_signals(monkeypatch):
    efforts = []
    _spy_hermes(monkeypatch, efforts, floor="low")
    bot = orchestrator.Olympus(user="effort-test")
    _run_one(bot, "x" * (effortscore.LONG_PROMPT_CHARS + 1),
             needs_verification=True)
    assert efforts == ["high"]            # long + verification → raised


def test_run_one_never_lowers_below_floor(monkeypatch):
    efforts = []
    _spy_hermes(monkeypatch, efforts, floor="high")
    bot = orchestrator.Olympus(user="effort-test")
    _run_one(bot, "tiny")
    assert efforts == ["high"]            # scorer low, floor high wins


def test_retry_dispatch_escalates_effort(monkeypatch):
    """_dispatch(retry_index=1) must run the rework at the top tier even for
    a cheap-floored specialist with a trivial task."""
    efforts = []
    _spy_hermes(monkeypatch, efforts, floor="low")
    bot = orchestrator.Olympus(user="effort-test")
    memory.set_user("effort-test")
    tr = trace.Trace("ask", "effort-test")
    try:
        bot._dispatch([{"specialist": "hermes", "task": "redo it"}], tr,
                      retry_index=1)
    finally:
        tr.flush()
    assert efforts == ["high"]


def test_risky_action_loadout_forces_high(monkeypatch):
    """A specialist holding an irreversible action tool always thinks at the
    top tier — the risk_class input is genuinely wired from the action
    registry, not a dead parameter."""
    efforts = []
    spec = dataclasses.replace(SPECIALISTS["hermes"], effort="low",
                               extra_tools=("send_email",))

    def fake_run_counted(task, settings=None, effort=None):
        efforts.append(effort)
        return "output", 0

    object.__setattr__(spec, "run_counted", fake_run_counted)
    monkeypatch.setitem(SPECIALISTS, "hermes", spec)
    bot = orchestrator.Olympus(user="effort-test")
    _run_one(bot, "trivial task")          # tiny prompt, no other signals
    assert efforts == ["high"]             # risk alone forces the top tier


def test_production_floors_make_the_dial_real():
    """The adversarial review proved an all-'high' roster makes the scorer a
    production no-op. At least one shipped specialist must sit below 'high'
    (cheap path exists), and the deep ones must still floor at 'high'."""
    floors = {k: s.effort for k, s in SPECIALISTS.items()}
    assert any(e != "high" for e in floors.values()), floors
    assert floors["iris"] == "medium"
    assert floors["mnemosyne"] == "medium"
    assert floors["chiron"] == "medium"
    assert floors["hephaestus"] == "high"    # deep work keeps its floor
    assert floors["aegis"] == "high"


def test_single_pool_rework_escalates_same_model_higher_effort(monkeypatch):
    """THE acceptance case: on a 1-member pool teacher_for is None (no
    stronger model exists) — the Athena-ordered rework must still escalate,
    demonstrably changing the call params: same Settings, effort=high."""
    from olympus import teacher
    assert teacher.teacher_for(
        config.ModelPool(members=(config.Settings(
            provider="anthropic", model="claude-opus-4-8"),)),
        "hermes") is None                 # precondition: no teacher available

    efforts = []
    calls = {"settings": []}
    spec = dataclasses.replace(SPECIALISTS["hermes"], effort="low")

    def fake_run_counted(task, settings=None, effort=None):
        efforts.append(effort)
        calls["settings"].append(settings)
        return "reworked", 0

    object.__setattr__(spec, "run_counted", fake_run_counted)
    monkeypatch.setitem(SPECIALISTS, "hermes", spec)

    bot = orchestrator.Olympus(
        settings=config.Settings(provider="anthropic",
                                 model="claude-opus-4-8", api_key="k"))
    assert len(bot.pool.members) == 1     # genuinely single-key
    memory.set_user("effort-test")
    tr = trace.Trace("ask", "effort-test")
    try:
        # the exact call the Athena rework block makes when teacher_for
        # returned None for every retry key
        bot._dispatch([{"specialist": "hermes", "task": "redo"}], tr,
                      overrides={}, retry_index=1)
    finally:
        tr.flush()
    assert efforts == ["high"]            # same model, more compute
    assert calls["settings"][0] is not None


# --- hardening addendum: budget cap + totality -----------------------------

def test_budget_low_caps_the_raise_at_the_floor(monkeypatch):
    """'Thinks harder' never defeats the spend guard: with low budget
    headroom, hard signals no longer raise above the specialist floor, and
    the denial is traced."""
    from olympus import trace, usage
    monkeypatch.setattr(usage, "budget_headroom_low", lambda: True)
    efforts = []
    _spy_hermes(monkeypatch, efforts, floor="low")
    bot = orchestrator.Olympus(user="effort-test")
    memory.set_user("effort-test")
    tr = trace.Trace("ask", "effort-test")
    try:
        bot._run_one("hermes",
                     "x" * (effortscore.VERY_LONG_PROMPT_CHARS + 1), tr,
                     needs_verification=True)
    finally:
        tr.flush()
    assert efforts == ["low"]                    # capped at the floor
    assert any(e.get("event") == "effort.budget_capped" or
               "effort.budget_capped" in str(e) for e in tr.events)


def test_budget_low_never_blocks_the_work_itself(monkeypatch):
    """A rework still RUNS under a low budget — only the raise is denied."""
    from olympus import trace, usage
    monkeypatch.setattr(usage, "budget_headroom_low", lambda: True)
    efforts = []
    _spy_hermes(monkeypatch, efforts, floor="low")
    bot = orchestrator.Olympus(user="effort-test")
    memory.set_user("effort-test")
    tr = trace.Trace("ask", "effort-test")
    try:
        bot._dispatch([{"specialist": "hermes", "task": "redo"}], tr,
                      retry_index=1)
    finally:
        tr.flush()
    assert efforts == ["low"]                    # ran — at the floor


def test_budget_fine_still_raises(monkeypatch):
    from olympus import trace, usage
    monkeypatch.setattr(usage, "budget_headroom_low", lambda: False)
    efforts = []
    _spy_hermes(monkeypatch, efforts, floor="low")
    bot = orchestrator.Olympus(user="effort-test")
    memory.set_user("effort-test")
    tr = trace.Trace("ask", "effort-test")
    try:
        bot._run_one("hermes",
                     "x" * (effortscore.VERY_LONG_PROMPT_CHARS + 1), tr,
                     needs_verification=True)
    finally:
        tr.flush()
    assert efforts == ["high"]


def test_budget_headroom_thresholds(monkeypatch):
    from olympus import usage
    monkeypatch.setattr(usage, "daily_budget", lambda: 10.0)
    monkeypatch.setattr(usage, "today_spend", lambda: 9.5)
    assert usage.budget_headroom_low() is True   # < 10% left
    monkeypatch.setattr(usage, "today_spend", lambda: 5.0)
    assert usage.budget_headroom_low() is False
    monkeypatch.setattr(usage, "daily_budget", lambda: 0.0)
    assert usage.budget_headroom_low() is False  # no budget set → no cap


def test_scorer_is_total_and_deterministic_under_junk():
    """Property test: randomized garbage inputs (seeded — reproducible)
    never raise, always yield a valid tier, and are deterministic."""
    import random
    rng = random.Random(20260719)
    junk_pool = [None, -1, -10**9, 10**9, 0, 3.7, float("nan"),
                 float("inf"), -float("inf"), "", "hello", "12", b"bytes",
                 [], {}, (1, 2), True, False, object()]
    for _ in range(300):
        kwargs = dict(
            risk_class=rng.choice(junk_pool),
            prompt_chars=rng.choice(junk_pool),
            tool_count=rng.choice(junk_pool),
            retry_index=rng.choice(junk_pool),
            needs_verification=rng.choice(junk_pool),
        )
        first = effortscore.score(**kwargs)
        assert first in effortscore.TIERS
        assert effortscore.score(**kwargs) == first    # deterministic
        assert effortscore.at_least(rng.choice(junk_pool),
                                    first) in effortscore.TIERS
