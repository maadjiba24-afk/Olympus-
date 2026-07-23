"""Keyless-install UX gaps closed (FEATURE_AUDIT §2.1, §2.2).

- `olympus scores` is display-only: it reads the committed baseline and never
  runs the paid live benchmark (which, with no key, crashed with a raw SDK
  TypeError).
- `olympus tick` (the heartbeat) collapses an LLM job's no-key failure into one
  quiet line instead of a full traceback per job per tick.
"""

from __future__ import annotations

import pytest

from olympus import cli, evals, heartbeat


# --- heartbeat: no-key LLM jobs skip quietly, no traceback spew --------------

def test_job_error_skips_quietly_without_a_key():
    line = heartbeat._job_error("Argus", configured=False)
    assert line == "Argus: skipped (no provider key configured)"
    assert "Traceback" not in line


def test_job_error_keeps_traceback_when_configured():
    # With a key present, a genuine failure still surfaces its traceback.
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        line = heartbeat._job_error("Argus", configured=True)
    assert line.startswith("Argus failed:")
    assert "RuntimeError" in line


def test_tick_llm_job_logs_skip_not_traceback(monkeypatch):
    from olympus import firstrun, orchestrator
    monkeypatch.setattr(firstrun, "configured", lambda: False)
    # Force ONLY the opportunity scan due, so the test is deterministic.
    monkeypatch.setattr(heartbeat, "_due",
                        lambda state, key, interval, now: key == "opportunity_scan")

    def boom(*a, **k):
        raise RuntimeError("Could not resolve authentication method")

    monkeypatch.setattr(orchestrator, "opportunity_scan", boom)

    log = heartbeat.tick({}, now=1_000_000.0)
    joined = "\n".join(log)
    assert "Argus: skipped (no provider key configured)" in joined
    assert "Traceback" not in joined and "traceback" not in joined


# --- scores: display-only, never runs the benchmark --------------------------

def test_scores_is_display_only_and_never_runs_the_benchmark(monkeypatch, capsys):
    def forbidden(*a, **k):
        raise AssertionError("`scores` must not run the live benchmark")

    monkeypatch.setattr(evals, "per_specialist_scores", forbidden)
    monkeypatch.setattr(evals, "load_baseline",
                        lambda *a, **k: {"iris": 9.8, "chronos": 7.2})
    monkeypatch.setattr(evals, "load_baseline_meta",
                        lambda *a, **k: {"model": "m1", "date": "2026-07-21"})

    cli.main(["scores"])
    out = capsys.readouterr().out
    assert "iris: 9.8/10" in out and "chronos: 7.2/10" in out
    assert "m1" in out and "2026-07-21" in out          # provenance shown


def test_scores_without_a_baseline_points_to_eval(monkeypatch, capsys):
    monkeypatch.setattr(evals, "load_baseline", lambda *a, **k: {})

    def forbidden(*a, **k):
        raise AssertionError("`scores` must not run the live benchmark")

    monkeypatch.setattr(evals, "per_specialist_scores", forbidden)

    cli.main(["scores"])
    out = capsys.readouterr().out
    assert "olympus eval" in out
