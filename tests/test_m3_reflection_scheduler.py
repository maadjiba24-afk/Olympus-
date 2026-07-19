"""Milestone 3.4 — sleep-time reflection scheduler (Plane 3, final).

One idle-time trigger runs BOTH reflection targets — Target A (prompt pipeline,
`reflect.reflect`) and Target B (memory consolidation, `sleeptime.run`) — under
budget caps and a Plane-1 behavioral contract (`reflection.cycle`), which HOLDS
the cycle when the daily budget is reached.
"""

import pytest

from olympus import (behavioral_contracts as abc, config, reflect, reflection,
                     sleeptime, usage)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# --- both targets fire under one cycle ------------------------------------

def test_cycle_runs_both_targets(monkeypatch):
    calls = []
    monkeypatch.setattr(reflection, "_target_memory",
                        lambda s: (calls.append("memory") or ["consolidated 2"]))
    monkeypatch.setattr(reflection, "_target_prompts",
                        lambda s: (calls.append("prompts") or "improved zeus"))
    monkeypatch.setattr(reflection, "_TARGETS",
                        (("memory", reflection._target_memory),
                         ("prompts", reflection._target_prompts)))
    monkeypatch.setattr(usage, "check_budget", lambda: None)   # inside budget

    out = reflection.sleep_cycle()
    assert out["status"] == "ran"
    assert calls == ["memory", "prompts"]                      # BOTH ran, in order
    names = {t["target"]: t["status"] for t in out["targets"]}
    assert names == {"memory": "ran", "prompts": "ran"}
    assert any("consolidated" in ln for ln in out["log"])
    assert any("improved zeus" in ln for ln in out["log"])


# --- the Plane-1 contract holds the cycle over budget ---------------------

def test_over_budget_holds_the_whole_cycle(monkeypatch):
    ran = []
    monkeypatch.setattr(reflection, "_target_memory", lambda s: ran.append("m"))
    monkeypatch.setattr(reflection, "_target_prompts", lambda s: ran.append("p"))

    def over(*a, **k):
        raise usage.BudgetExceeded("daily budget of $5.00 reached")
    monkeypatch.setattr(usage, "check_budget", over)

    out = reflection.sleep_cycle()
    assert out["status"] == "held" and not ran            # neither target ran
    assert "budget" in out["reason"].lower()


def test_reflection_contract_holds_when_budget_flag_false():
    # The Plane-1 contract itself: recovery=hold, blocking, deferred not run.
    v = abc.check("reflection.cycle", {"reflection_budget_ok": False})
    assert v.blocking and v.recovery == abc.HOLD and v.contract == "reflection_cycle"
    # In budget → the cycle is permitted.
    assert abc.check("reflection.cycle", {"reflection_budget_ok": True}).ok


# --- budget caps bound the cycle ------------------------------------------

def test_time_budget_stops_before_the_second_target(monkeypatch):
    clock = _Clock()
    ran = []

    def slow_memory(s):
        clock.t += 100.0                    # this target eats the whole budget
        ran.append("memory")
        return ["ok"]

    monkeypatch.setattr(reflection, "_target_memory", slow_memory)
    monkeypatch.setattr(reflection, "_target_prompts",
                        lambda s: ran.append("prompts"))
    monkeypatch.setattr(reflection, "_TARGETS",
                        (("memory", reflection._target_memory),
                         ("prompts", reflection._target_prompts)))
    monkeypatch.setattr(usage, "check_budget", lambda: None)

    out = reflection.sleep_cycle(budget=reflection.ReflectionBudget(max_seconds=50.0),
                                 clock=clock)
    assert ran == ["memory"]                              # prompts skipped: time cap
    statuses = {t["target"]: t["status"] for t in out["targets"]}
    assert statuses["prompts"] == "skipped:time-budget"


def test_max_targets_zero_runs_nothing(monkeypatch):
    ran = []
    monkeypatch.setattr(reflection, "_target_memory", lambda s: ran.append("m"))
    monkeypatch.setattr(reflection, "_target_prompts", lambda s: ran.append("p"))
    monkeypatch.setattr(usage, "check_budget", lambda: None)
    out = reflection.sleep_cycle(budget=reflection.ReflectionBudget(max_targets=0))
    assert out["status"] == "ran" and not ran and out["targets"] == []


def test_spend_budget_rechecked_per_target(monkeypatch):
    # Budget is fine for the contract gate and the first target, then crosses the
    # line — the second target is skipped mid-cycle.
    ran = []
    monkeypatch.setattr(reflection, "_target_memory",
                        lambda s: (ran.append("m") or ["ok"]))
    monkeypatch.setattr(reflection, "_target_prompts", lambda s: ran.append("p"))
    monkeypatch.setattr(reflection, "_TARGETS",
                        (("memory", reflection._target_memory),
                         ("prompts", reflection._target_prompts)))
    calls = {"n": 0}

    def budget(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 3:                 # gate ok, memory ok, then exceeded
            raise usage.BudgetExceeded("reached")
    monkeypatch.setattr(usage, "check_budget", budget)

    out = reflection.sleep_cycle()
    assert ran == ["m"]
    statuses = {t["target"]: t["status"] for t in out["targets"]}
    assert statuses["prompts"] == "skipped:spend-budget"


# --- robustness ------------------------------------------------------------

def test_failing_target_is_captured_not_raised(monkeypatch):
    monkeypatch.setattr(reflection, "_target_memory",
                        lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(reflection, "_target_prompts", lambda s: "ok")
    monkeypatch.setattr(reflection, "_TARGETS",
                        (("memory", reflection._target_memory),
                         ("prompts", reflection._target_prompts)))
    monkeypatch.setattr(usage, "check_budget", lambda: None)
    out = reflection.sleep_cycle()          # must not raise
    statuses = {t["target"]: t["status"] for t in out["targets"]}
    assert statuses["memory"] == "error" and statuses["prompts"] == "ran"


def test_respect_daily_budget_false_bypasses_check(monkeypatch):
    def over(*a, **k):
        raise usage.BudgetExceeded("reached")
    monkeypatch.setattr(usage, "check_budget", over)
    monkeypatch.setattr(reflection, "_target_memory", lambda s: ["ok"])
    monkeypatch.setattr(reflection, "_target_prompts", lambda s: "ok")
    monkeypatch.setattr(reflection, "_TARGETS",
                        (("memory", reflection._target_memory),
                         ("prompts", reflection._target_prompts)))
    out = reflection.sleep_cycle(
        budget=reflection.ReflectionBudget(respect_daily_budget=False))
    assert out["status"] == "ran"           # budget ignored → cycle proceeds


def test_budget_clamps_insane_values():
    b = reflection.ReflectionBudget(max_seconds=999999.0, max_targets=100)
    assert b.max_seconds == 3600.0 and b.max_targets == 8
    b2 = reflection.ReflectionBudget(max_seconds=-5.0, max_targets=-1)
    assert b2.max_seconds == 0.001 and b2.max_targets == 0


def test_real_targets_are_the_two_plane3_pipelines():
    # Wiring sanity: the scheduler dispatches to the actual Target A / B entries.
    import inspect
    assert "reflect.reflect" in inspect.getsource(reflection._target_prompts)
    assert "sleeptime.run" in inspect.getsource(reflection._target_memory)
