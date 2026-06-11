"""Tests for systematic per-specialist training (coverage, scoring, weakest-first)."""

from olympus import evals, orchestrator
from olympus.specialists import SPECIALISTS


def test_every_user_facing_specialist_has_coverage():
    cov = evals.coverage()
    for spec in evals.USER_FACING:
        assert cov.get(spec, 0) >= 1, f"{spec} has no benchmark item"


def test_internal_specialists_excluded_from_user_facing():
    assert "metis" not in evals.USER_FACING
    assert "prometheus" not in evals.USER_FACING


def test_ensure_coverage_generates_for_gaps(monkeypatch):
    # pretend chiron has no items
    monkeypatch.setattr(evals, "coverage",
                        lambda: {s: (0 if s == "chiron" else 1)
                                 for s in evals.USER_FACING})
    gen = []
    monkeypatch.setattr(evals, "generate_item",
                        lambda spec, settings=None: gen.append(spec))
    out = evals.ensure_coverage(min_items=1)
    assert out == ["chiron"] and gen == ["chiron"]


def test_per_specialist_scores_buckets_by_specialist(monkeypatch):
    monkeypatch.setattr(evals, "run", lambda settings, only=None: {"avg": 0,
        "items": [
            {"id": "plutus-budget", "score": 6},
            {"id": "hephaestus-review", "score": 9},
            {"id": "hephaestus-go", "score": 7},
        ]})
    scores = evals.per_specialist_scores()
    assert scores["plutus"] == 6.0
    assert scores["hephaestus"] == 8.0          # (9+7)/2


def test_train_focuses_on_weakest(monkeypatch):
    monkeypatch.setattr(evals, "ensure_coverage", lambda **k: [])
    monkeypatch.setattr(evals, "per_specialist_scores",
                        lambda settings=None: {"plutus": 9.0, "iris": 4.0,
                                               "argus": 5.0, "chiron": 8.0})
    captured = {}

    def fake_run(self, task, settings=None, effort="high"):
        captured["task"] = task
        return "trained"

    monkeypatch.setattr(SPECIALISTS["prometheus"].__class__, "run", fake_run)
    out = orchestrator.train_specialists(focus=2)
    # weakest two (iris=4, argus=5) must be the focus
    assert "iris" in captured["task"] and "argus" in captured["task"]
    assert "Focused on: iris, argus" in out
    # the scoreboard is in the report
    assert "iris: 4.0/10" in out


def test_train_handles_no_scores(monkeypatch):
    monkeypatch.setattr(evals, "ensure_coverage", lambda **k: [])
    monkeypatch.setattr(evals, "per_specialist_scores", lambda settings=None: {})
    assert "could not score" in orchestrator.train_specialists()
