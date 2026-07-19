"""Best-first tree search: explores read-only/reversible steps, HALTS side-
effectful ones for approval, and never breaches its node/token/time/depth caps."""

import itertools

import pytest

from olympus import treesearch as ts


# --- a tiny grid problem: reach target value by +1 / +2 (read-only), with an
#     optional 'commit' step that is side-effectful ------------------------

class Climb:
    """State is an int. Safe steps add 1 or 2 (exploration). A 'commit' step is
    side-effectful. Goal is reaching `target`. evaluate = closeness to target."""

    def __init__(self, target, with_side_effect=False, branching=("+1", "+2")):
        self.target = target
        self.with_side_effect = with_side_effect
        self.branching = branching

    def expand(self, state):
        steps = []
        for b in self.branching:
            steps.append(ts.Step(action=b, args={"n": int(b[1:])},
                                  effect=ts.READ_ONLY, cost_tokens=10))
        if self.with_side_effect:
            steps.append(ts.Step(action="commit", effect=ts.SIDE_EFFECTFUL,
                                 cost_tokens=10))
        return steps

    def apply(self, state, step):
        if step.effect not in ts.SAFE:
            raise AssertionError("apply() must never be called for a side effect")
        return state + step.args["n"]

    def evaluate(self, state):
        return -abs(self.target - state)          # higher (closer to 0) is better

    def is_goal(self, state):
        return state == self.target


def test_finds_goal_via_safe_steps():
    r = ts.search(Climb(5), 0, ts.SearchCaps(max_nodes=100, max_depth=10))
    assert r.status == ts.GOAL
    assert r.best_state == 5
    assert sum(s.args["n"] for s in r.path) == 5      # a valid additive path


def test_apply_never_called_for_side_effect():
    # Climb.apply raises if ever handed a side-effect step; a clean run proves
    # the engine never applied one.
    r = ts.search(Climb(5, with_side_effect=True), 0,
                  ts.SearchCaps(max_nodes=200))
    assert r.status == ts.GOAL                     # still reaches goal via safe
    assert r.pending_approval                       # and withheld the commits
    assert all(p["effect"] == ts.SIDE_EFFECTFUL for p in r.pending_approval)


def test_side_effect_only_progress_halts_for_approval():
    class OnlyCommit:
        def expand(self, s):
            return [ts.Step(action="commit", effect=ts.SIDE_EFFECTFUL)]

        def apply(self, s, step):
            raise AssertionError("never")

        def evaluate(self, s):
            return 0.0

        def is_goal(self, s):
            return False
    r = ts.search(OnlyCommit(), "start", ts.SearchCaps(max_nodes=10))
    assert r.status == ts.HALTED
    assert r.needs_approval and len(r.pending_approval) >= 1


# --- caps -----------------------------------------------------------------

def test_node_cap_stops_runaway_search():
    # infinite branching, unreachable goal → must stop at the node cap
    r = ts.search(Climb(10**9, branching=("+1", "+2")), 0,
                  ts.SearchCaps(max_nodes=25, max_depth=10_000,
                                max_seconds=1000))
    assert r.status == ts.NODE_CAP
    assert r.stats["nodes"] <= 25


def test_token_cap_stops_search():
    r = ts.search(Climb(10**9), 0,
                  ts.SearchCaps(max_nodes=10_000, max_tokens=100,
                                max_seconds=1000, max_depth=10_000))
    assert r.status == ts.TOKEN_CAP
    assert r.stats["tokens"] <= 100 + 100        # bounded (last expansion batch)


def test_time_cap_stops_search():
    ticks = itertools.count(0, 5)                 # each clock() call advances 5s
    r = ts.search(Climb(10**9), 0,
                  ts.SearchCaps(max_nodes=10_000, max_seconds=12,
                                max_depth=10_000),
                  clock=lambda: next(ticks))
    assert r.status == ts.TIME_CAP


def test_depth_cap_bounds_path_length():
    # goal unreachable within depth; safe steps only → depth-capped, no goal
    r = ts.search(Climb(10**9, branching=("+1",)), 0,
                  ts.SearchCaps(max_nodes=10_000, max_depth=5,
                                max_seconds=1000))
    assert r.status in (ts.DEPTH_CAP, ts.EXHAUSTED, ts.NODE_CAP)
    assert r.stats["depth"] <= 5


def test_caps_clamp_nonpositive_to_safe_minimums():
    c = ts.SearchCaps(max_nodes=0, max_tokens=-5, max_seconds=0, max_depth=-1)
    assert c.max_nodes >= 1 and c.max_tokens >= 1
    assert c.max_seconds > 0 and c.max_depth >= 1


def test_caps_from_env(monkeypatch):
    monkeypatch.setenv("OLYMPUS_TREESEARCH_MAX_NODES", "7")
    monkeypatch.setenv("OLYMPUS_TREESEARCH_MAX_SECONDS", "2.5")
    c = ts.SearchCaps.from_env()
    assert c.max_nodes == 7 and c.max_seconds == 2.5


# --- robustness -----------------------------------------------------------

def test_bad_problem_api_raises():
    with pytest.raises(TypeError):
        ts.search(object(), 0)


def test_expand_error_is_survived():
    class Flaky:
        def __init__(self):
            self.n = 0

        def expand(self, s):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("boom")
            return [ts.Step(action="+1", args={"n": 1}, effect=ts.READ_ONLY)]

        def apply(self, s, step):
            return s + 1

        def evaluate(self, s):
            return -abs(3 - s)

        def is_goal(self, s):
            return s == 3
    # first expand throws (branch skipped); search must not crash
    r = ts.search(Flaky(), 0, ts.SearchCaps(max_nodes=50))
    assert r.status in (ts.GOAL, ts.EXHAUSTED, ts.NODE_CAP)


def test_evaluate_error_yields_worst_score():
    class BadEval:
        def expand(self, s):
            return []

        def apply(self, s, step):
            return s

        def evaluate(self, s):
            raise RuntimeError("nope")

        def is_goal(self, s):
            return False
    r = ts.search(BadEval(), 0, ts.SearchCaps(max_nodes=5))
    assert r.best_score == float("-inf")           # captured, not raised


def test_immediate_goal():
    class Done:
        def expand(self, s): return []
        def apply(self, s, step): return s
        def evaluate(self, s): return 0.0
        def is_goal(self, s): return True
    r = ts.search(Done(), "x")
    assert r.status == ts.GOAL and r.path == []


# --- browser classifier ---------------------------------------------------

def test_browser_classification():
    assert ts.classify_browser(ts.Step("read_ax")) == ts.READ_ONLY
    assert ts.classify_browser(ts.Step("navigate")) == ts.REVERSIBLE
    assert ts.classify_browser(ts.Step("click")) == ts.SIDE_EFFECTFUL
    assert ts.classify_browser(ts.Step("type")) == ts.SIDE_EFFECTFUL
    # unknown verb ⇒ fail closed
    assert ts.classify_browser(ts.Step("frobnicate")) == ts.SIDE_EFFECTFUL


def test_browser_step_helper_fills_effect():
    assert ts.browser_step("observe").effect == ts.READ_ONLY
    assert ts.browser_step("click", selector="#buy").effect == ts.SIDE_EFFECTFUL


def test_classify_override_fails_closed_on_error():
    def bad_classify(step):
        raise ValueError("classifier bug")
    # a classifier that errors must be treated as side-effectful (withheld)
    r = ts.search(Climb(5, with_side_effect=True), 0,
                  ts.SearchCaps(max_nodes=50), classify=bad_classify)
    # every generated step (even the +1/+2) is now treated unsafe → all withheld
    assert r.status in (ts.HALTED, ts.EXHAUSTED)
    assert r.pending_approval


# --- approval-gate handoff ------------------------------------------------

def test_to_approvals_prepares_but_does_not_execute(monkeypatch):
    from olympus import actions
    monkeypatch.setattr(actions, "_REGISTRY", {})
    ran = {"n": 0}
    actions.register(actions.ActionType(
        name="ts_click", risk_class=actions.IRREVERSIBLE, scope="web",
        preview=lambda p: f"click {p.get('selector')}",
        execute=lambda p: ran.__setitem__("n", ran["n"] + 1)))
    r = ts.search(Climb(5, with_side_effect=True), 0, ts.SearchCaps(max_nodes=80))

    def mapper(entry):
        return {"type": "ts_click", "payload": {"selector": "#buy"},
                "title": "buy"}
    prepared = ts.to_approvals(r, "u", mapper)
    assert prepared and all(a.status == actions.PREPARED for a in prepared)
    assert ran["n"] == 0                            # prepared, never executed
