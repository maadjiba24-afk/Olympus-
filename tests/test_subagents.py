"""Scriptable subagents: isolation, parallel fan-out, failure containment."""

from olympus import subagents
from olympus.specialists import SPECIALISTS


def _fake_run(monkeypatch, fn):
    monkeypatch.setattr(SPECIALISTS["plutus"].__class__, "run", fn, raising=True)


def test_spawn_unknown_specialist_is_handled():
    out = subagents.spawn("nobody", "do a thing")
    assert "unknown specialist" in out


def test_spawn_returns_specialist_output(monkeypatch):
    monkeypatch.setattr(type(SPECIALISTS["plutus"]), "run",
                        lambda self, task, settings=None: f"PLUTUS:{task}")
    assert subagents.spawn("plutus", "price it") == "PLUTUS:price it"


def test_spawn_contains_failures(monkeypatch):
    def boom(self, task, settings=None):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(type(SPECIALISTS["plutus"]), "run", boom)
    out = subagents.spawn("plutus", "x")
    assert "failed" in out and "kaboom" in out


def test_spawn_many_runs_all_and_keeps_order(monkeypatch):
    monkeypatch.setattr(type(SPECIALISTS["plutus"]), "run",
                        lambda self, task, settings=None: f"{self.key}:{task}")
    tasks = [subagents.SubTask("plutus", "a"),
             subagents.SubTask("peitho", "b"),
             subagents.SubTask("aegis", "c")]
    results = subagents.spawn_many(tasks, max_workers=3)
    assert [r[0] for r in results] == ["plutus", "peitho", "aegis"]
    assert results[0][1] == "plutus:a"


def test_spawn_tool_wraps_result(monkeypatch):
    monkeypatch.setattr(type(SPECIALISTS["argus"]), "run",
                        lambda self, task, settings=None: "scanned")
    out = subagents.spawn_tool("argus", "scan")
    assert out.startswith("### argus subagent result")
    assert "scanned" in out
