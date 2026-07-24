"""Per-run USD spend ceiling (OLYMPUS_RUN_BUDGET_USD).

The daily budget is a floor-of-the-day guard checked at run entry; this is the
"no single question runs away" guard enforced mid-fan-out at
`orchestrator._run_one`, so one pathological council run can't drain the whole
daily budget. Tests make zero model calls.
"""

from __future__ import annotations

import pytest

from olympus import config, orchestrator, replaystore, usage
from olympus.orchestrator import SPECIALISTS
from olympus import trace as trace_mod


# --- config + usage units ----------------------------------------------------

def test_run_budget_default_off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_RUN_BUDGET_USD", raising=False)
    assert config.run_budget_usd() == 0.0
    assert usage.run_budget() == 0.0


def test_run_budget_env_parsing(monkeypatch):
    monkeypatch.setenv("OLYMPUS_RUN_BUDGET_USD", "0.50")
    assert usage.run_budget() == 0.50
    monkeypatch.setenv("OLYMPUS_RUN_BUDGET_USD", "garbage")
    assert usage.run_budget() == 0.0            # malformed → no cap, not a crash
    monkeypatch.setenv("OLYMPUS_RUN_BUDGET_USD", "-5")
    assert usage.run_budget() == 0.0            # clamped to >= 0


def test_run_over_budget_none_when_no_cap(monkeypatch):
    monkeypatch.delenv("OLYMPUS_RUN_BUDGET_USD", raising=False)
    monkeypatch.setattr(usage, "today_spend", lambda: 999.0)
    assert usage.run_over_budget(0.0) is None   # no cap → never over


def test_run_over_budget_under_and_over(monkeypatch):
    monkeypatch.setenv("OLYMPUS_RUN_BUDGET_USD", "1.00")
    # baseline 10.0, spent 10.4 → added 0.4 < 1.0 → under
    monkeypatch.setattr(usage, "today_spend", lambda: 10.4)
    assert usage.run_over_budget(10.0) is None
    # spent 11.5 → added 1.5 >= 1.0 → over by 0.5
    monkeypatch.setattr(usage, "today_spend", lambda: 11.5)
    over = usage.run_over_budget(10.0)
    assert over is not None and abs(over - 0.5) < 1e-9


# --- integration at _run_one -------------------------------------------------

def _stub_run_counted(monkeypatch):
    """Every specialist returns a deterministic output with zero model calls."""
    def fake(self, task, settings=None, effort="high"):
        return f"output[{self.key}]", 0
    monkeypatch.setattr(SPECIALISTS["plutus"].__class__, "run_counted", fake)


def test_run_one_stops_when_over_budget(monkeypatch):
    monkeypatch.setenv("OLYMPUS_RUN_BUDGET_USD", "1.00")
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    _stub_run_counted(monkeypatch)
    bot = orchestrator.Olympus(user="runbudget")
    tr = trace_mod.Trace("t")
    # Pre-seed the baseline low, then report spend far above it → over budget.
    tr.meta["budget_baseline"] = 0.0
    monkeypatch.setattr(usage, "today_spend", lambda: 5.0)

    out = bot._run_one("plutus", "go", tr)
    assert "Skipped" in out and "per-run spend ceiling" in out
    assert any(e.get("stage") == "run.budget_stopped" for e in tr.events)


def test_run_one_first_specialist_always_runs(monkeypatch):
    """With no baseline yet, _run_one snapshots it so the run's own delta is 0 —
    the first specialist is never starved even if the DAY is already expensive."""
    monkeypatch.setenv("OLYMPUS_RUN_BUDGET_USD", "1.00")
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    _stub_run_counted(monkeypatch)
    monkeypatch.setattr(usage, "today_spend", lambda: 500.0)   # pricey day
    bot = orchestrator.Olympus(user="runbudget")
    tr = trace_mod.Trace("t")
    out = bot._run_one("plutus", "go", tr)
    assert out == "output[plutus]"                             # ran normally
    assert tr.meta["budget_baseline"] == 500.0                # baseline captured


def test_run_one_no_cap_takes_no_baseline_snapshot(monkeypatch):
    """Default (no cap): the guard short-circuits on the cheap env read and never
    calls the disk-reading today_spend() — no budget_baseline is written."""
    monkeypatch.delenv("OLYMPUS_RUN_BUDGET_USD", raising=False)
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    _stub_run_counted(monkeypatch)

    def _boom():
        raise AssertionError("today_spend() must not run when no cap is set")
    monkeypatch.setattr(usage, "today_spend", _boom)
    bot = orchestrator.Olympus(user="runbudget")
    tr = trace_mod.Trace("t")
    assert bot._run_one("plutus", "go", tr) == "output[plutus]"
    assert "budget_baseline" not in tr.meta


def test_run_one_gated_off_during_replay(monkeypatch):
    """A replayed run must re-dispatch exactly what it recorded — a live-spend
    STOP would diverge it, so the ceiling is inert under OLYMPUS_REPLAY."""
    monkeypatch.setenv("OLYMPUS_RUN_BUDGET_USD", "1.00")
    monkeypatch.setenv("OLYMPUS_REPLAY", "some-run-id")
    _stub_run_counted(monkeypatch)
    monkeypatch.setattr(usage, "today_spend", lambda: 999.0)   # wildly over
    bot = orchestrator.Olympus(user="runbudget")
    tr = trace_mod.Trace("t")
    tr.meta["budget_baseline"] = 0.0
    out = bot._run_one("plutus", "go", tr)
    assert out == "output[plutus]"                             # ran, no skip
