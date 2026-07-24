"""Objective, judge-independent assertions on eval items (evals.objective_score).

The self-improvement gate scores answers with an LLM judge. These deterministic
assertions make it partly JUDGE-INDEPENDENT: a skill/prompt that breaks a
measurable property (drops a required term, emits a forbidden one, blows a length
bound) fails the gate regardless of what the judge thought. Items WITHOUT checks
are a strict no-op (backward compatible).
"""

from __future__ import annotations

from olympus import evals


# --- pure scorer -------------------------------------------------------------

def test_no_checks_is_a_noop():
    assert evals.objective_score("anything", None) == (1.0, [])
    assert evals.objective_score("anything", {}) == (1.0, [])
    assert evals.objective_score("x", {"contains": []}) == (1.0, [])


def test_contains_pass_and_fail():
    rate, fails = evals.objective_score("The APR is 5% annually",
                                        {"contains": ["apr", "5%"]})
    assert rate == 1.0 and fails == []
    rate, fails = evals.objective_score("no rate here", {"contains": ["APR"]})
    assert rate == 0.0 and len(fails) == 1


def test_not_contains():
    rate, _ = evals.objective_score("safe answer",
                                    {"not_contains": ["guaranteed returns"]})
    assert rate == 1.0
    rate, fails = evals.objective_score("guaranteed returns!!",
                                        {"not_contains": ["guaranteed returns"]})
    assert rate == 0.0 and "forbidden" in fails[0]


def test_regex_and_bad_pattern_fails_closed():
    rate, _ = evals.objective_score("call 2026-01-02", {"regex": [r"\d{4}-\d{2}-\d{2}"]})
    assert rate == 1.0
    rate, _ = evals.objective_score("no date", {"regex": [r"\d{4}-\d{2}-\d{2}"]})
    assert rate == 0.0
    rate, fails = evals.objective_score("x", {"regex": ["("]})   # invalid regex
    assert rate == 0.0 and fails                                  # fails closed


def test_length_bounds():
    assert evals.objective_score("short", {"min_chars": 100})[0] == 0.0
    assert evals.objective_score("x" * 50, {"max_chars": 10})[0] == 0.0
    assert evals.objective_score("x" * 50, {"min_chars": 10, "max_chars": 100})[0] == 1.0


def test_partial_pass_rate():
    rate, fails = evals.objective_score(
        "has apr but not the number", {"contains": ["apr", "7.25%"]})
    assert rate == 0.5 and len(fails) == 1


def test_malformed_bound_ignored_not_failed():
    rate, _ = evals.objective_score("x", {"min_chars": "not-an-int"})
    assert rate == 1.0                            # malformed bound is ignored


# --- integration into run(): objective caps the judge score ------------------

def _mock_backend(monkeypatch, answer: str, judge_score: int = 9):
    from olympus import backend
    monkeypatch.setattr(backend, "complete_text", lambda *a, **k: answer)
    monkeypatch.setattr(backend, "complete_json",
                        lambda *a, **k: {"score": judge_score,
                                         "justification": "ok"})


def _one_item(monkeypatch, checks):
    from olympus import evals as ev
    item = {"id": "plutus-obj-test", "specialist": "plutus",
            "task": "t", "criteria": "c"}
    if checks is not None:
        item["checks"] = checks
    monkeypatch.setattr(ev, "load_benchmarks", lambda: [item])


def test_run_objective_failure_caps_score(monkeypatch):
    # Judge says 9, but the answer misses a required term → objective 0 → score 1.
    _mock_backend(monkeypatch, "an answer with no required term", judge_score=9)
    _one_item(monkeypatch, {"contains": ["MANDATORY-TERM"]})
    r = evals.run(only=["plutus-obj-test"])
    it = r["items"][0]
    assert it["score"] == 1                        # capped judge-independently
    assert it["objective"]["pass_rate"] == 0.0
    assert it["objective"]["judge_score"] == 9


def test_run_objective_pass_keeps_judge_score(monkeypatch):
    _mock_backend(monkeypatch, "contains MANDATORY-TERM here", judge_score=8)
    _one_item(monkeypatch, {"contains": ["MANDATORY-TERM"]})
    r = evals.run(only=["plutus-obj-test"])
    assert r["items"][0]["score"] == 8            # judge stands when objective ok


def test_run_no_checks_is_unchanged(monkeypatch):
    # An item with no checks scores exactly the judge score — backward compatible.
    _mock_backend(monkeypatch, "whatever", judge_score=7)
    _one_item(monkeypatch, None)
    r = evals.run(only=["plutus-obj-test"])
    assert r["items"][0]["score"] == 7
    assert "objective" not in r["items"][0]
