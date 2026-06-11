"""Tests for the DAG planner/executor (serial + parallel specialist steps)."""

from olympus import memory, orchestrator, trace
from olympus.specialists import SPECIALISTS


def _bot(monkeypatch, recorder):
    """An Olympus whose specialists just echo their task (no model calls)."""
    bot = orchestrator.Olympus(user="dag")

    def fake_run(self, task, settings=None, effort="high"):
        recorder.append((self.key, task))
        return f"output[{self.key}]"

    monkeypatch.setattr(SPECIALISTS["plutus"].__class__, "run", fake_run)
    return bot


def test_independent_steps_all_run(monkeypatch):
    rec = []
    bot = _bot(monkeypatch, rec)
    steps = [
        {"id": "a", "specialist": "plutus", "task": "task A", "depends_on": []},
        {"id": "b", "specialist": "peitho", "task": "task B", "depends_on": []},
    ]
    out = dict(bot._dispatch_dag(steps, trace.Trace("t")))
    assert out["plutus"].startswith("output[")
    assert out["peitho"].startswith("output[")


def test_dependent_step_receives_upstream_output(monkeypatch):
    rec = []
    bot = _bot(monkeypatch, rec)
    # both steps use plutus so we can read the recorder; b depends on a
    steps = [
        {"id": "a", "specialist": "plutus", "task": "decide pricing",
         "depends_on": []},
        {"id": "b", "specialist": "plutus", "task": "write copy",
         "depends_on": ["a"]},
    ]
    bot._dispatch_dag(steps, trace.Trace("t"))
    # the dependent task must have received step a's output injected
    dep_task = [task for key, task in rec if "write copy" in task][0]
    assert "Inputs from prior steps" in dep_task
    assert "output[plutus]" in dep_task
    # and the independent one did NOT get inputs
    indep_task = [task for key, task in rec if "decide pricing" in task][0]
    assert "Inputs from prior steps" not in indep_task


def test_execution_order_respects_dependencies(monkeypatch):
    rec = []
    bot = _bot(monkeypatch, rec)
    steps = [
        {"id": "a", "specialist": "plutus", "task": "first", "depends_on": []},
        {"id": "b", "specialist": "plutus", "task": "second", "depends_on": ["a"]},
        {"id": "c", "specialist": "plutus", "task": "third", "depends_on": ["b"]},
    ]
    bot._dispatch_dag(steps, trace.Trace("t"))
    order = [t.split("\n")[0] for _, t in rec]
    assert order.index("first") < order.index("second") < order.index("third")


def test_cycle_does_not_deadlock(monkeypatch):
    rec = []
    bot = _bot(monkeypatch, rec)
    steps = [
        {"id": "a", "specialist": "plutus", "task": "A", "depends_on": ["b"]},
        {"id": "b", "specialist": "plutus", "task": "B", "depends_on": ["a"]},
    ]
    out = bot._dispatch_dag(steps, trace.Trace("t"))  # must terminate
    assert len(out) == 2


def test_plan_normalizes_and_drops_bad_deps(monkeypatch):
    bot = orchestrator.Olympus(user="dag")
    # stub the planner LLM call to return a messy graph
    monkeypatch.setattr(orchestrator.backend, "complete_json",
                        lambda *a, **k: {"steps": [
                            {"id": "x", "specialist": "plutus", "task": "t1",
                             "depends_on": ["ghost", "x"]},  # bad + self ref
                            {"id": "y", "specialist": "notreal", "task": "t2",
                             "depends_on": []},               # invalid specialist
                            {"id": "z", "specialist": "peitho", "task": "t3",
                             "depends_on": ["x"]},
                        ]})
    plan = bot._plan("brief", ["plutus"])
    ids = {s["id"] for s in plan}
    assert ids == {"x", "z"}                       # invalid specialist dropped
    x = next(s for s in plan if s["id"] == "x")
    assert x["depends_on"] == []                   # ghost + self-ref removed
    z = next(s for s in plan if s["id"] == "z")
    assert z["depends_on"] == ["x"]                # valid dep kept


def test_plan_falls_back_on_planner_failure(monkeypatch):
    bot = orchestrator.Olympus(user="dag")
    monkeypatch.setattr(orchestrator.backend, "complete_json",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    plan = bot._plan("the brief", ["peitho"])
    assert len(plan) == 1 and plan[0]["specialist"] == "peitho"
    assert plan[0]["depends_on"] == []
