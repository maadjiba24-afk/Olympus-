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


# --- richer objective check types --------------------------------------------

def test_numeric_within_and_outside_tolerance():
    assert evals.objective_score("the payment is $1,432.86/mo",
                                 {"numeric": {"value": 1432.86, "tolerance": 0.5}})[0] == 1.0
    assert evals.objective_score("about 1500",
                                 {"numeric": {"value": 1432.86, "tolerance": 0.5}})[0] == 0.0
    # malformed numeric block fails that check (no crash)
    assert evals.objective_score("x", {"numeric": {"value": "NaNish"}})[0] == 0.0


def test_parses_date():
    for good in ("your appointment is 2026-03-14", "let's meet on 3/14/2026",
                 "how about Mar 14", "the 14 Mar deadline"):
        assert evals.objective_score(good, {"parses_date": True})[0] == 1.0
    assert evals.objective_score("no date at all here", {"parses_date": True})[0] == 0.0


def test_json_valid():
    assert evals.objective_score('```json\n{"a": 1}\n```', {"json_valid": True})[0] == 1.0
    assert evals.objective_score('result: [1, 2, 3] done', {"json_valid": True})[0] == 1.0
    assert evals.objective_score("not json {oops", {"json_valid": True})[0] == 0.0


def test_word_bounds_and_code_block():
    assert evals.objective_score("one two three four five", {"min_words": 3})[0] == 1.0
    assert evals.objective_score("too short", {"min_words": 5})[0] == 0.0
    assert evals.objective_score("```py\nx=1\n```", {"code_block": True})[0] == 1.0
    assert evals.objective_score("no code here", {"code_block": True})[0] == 0.0


# --- refusal detection (robust, start-anchored) ------------------------------

def test_looks_like_refusal():
    assert evals.looks_like_refusal("I can't help with that.") is True
    assert evals.looks_like_refusal("   ") is True                 # empty non-answer
    assert evals.looks_like_refusal("As an AI language model, I ...") is True
    # a substantive answer that merely QUOTES a refusal later is NOT a refusal
    body = ("Here's how the API behaves: on an unauthorized call it returns the "
            "string 'I cannot assist'. You should handle that by ...")
    assert evals.looks_like_refusal(body) is False


def test_no_refusal_check_type():
    assert evals.objective_score("Sure — here is the plan: step 1 ...",
                                 {"no_refusal": True})[0] == 1.0
    assert evals.objective_score("I'm unable to help with that.",
                                 {"no_refusal": True})[0] == 0.0


# --- universal quality floor in run() ----------------------------------------

def test_run_quality_floor_caps_refusal(monkeypatch):
    _mock_backend(monkeypatch, "I can't help with that request.", judge_score=9)
    _one_item(monkeypatch, None)                    # no per-item checks at all
    r = evals.run(only=["plutus-obj-test"])
    it = r["items"][0]
    assert it["score"] == 1                          # floored, judge ignored
    assert it["objective"]["floor_failed"] is True


def test_run_floor_kill_switch(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EVAL_FLOOR", "off")
    _mock_backend(monkeypatch, "I can't help.", judge_score=8)
    _one_item(monkeypatch, None)
    r = evals.run(only=["plutus-obj-test"])
    assert r["items"][0]["score"] == 8              # floor disabled → judge stands


def test_run_floor_leaves_good_answers_untouched(monkeypatch):
    _mock_backend(monkeypatch, "Here is a thorough, correct answer.", judge_score=7)
    _one_item(monkeypatch, None)
    r = evals.run(only=["plutus-obj-test"])
    assert r["items"][0]["score"] == 7              # not a refusal → unchanged
    assert "objective" not in r["items"][0]


# --- built-in corpus: per-domain objective coverage --------------------------

def test_builtin_benchmarks_have_per_domain_objective_coverage():
    # Every user-facing specialist domain has at least one objectively-graded
    # item, so the gate is partly judge-independent for every domain.
    items = evals.load_benchmarks()
    with_checks = {i["specialist"] for i in items if i.get("checks")}
    missing = [sp for sp in evals.USER_FACING if sp not in with_checks]
    assert not missing, f"domains without objective checks: {missing}"


def test_builtin_benchmark_checks_are_valid():
    import re as _re
    known = {"contains", "not_contains", "regex", "min_chars", "max_chars",
             "min_words", "max_words", "numeric", "parses_date", "json_valid",
             "code_block", "no_refusal"}
    for i in evals.load_benchmarks():
        ch = i.get("checks")
        if not ch:
            continue
        assert set(ch) <= known, (i["id"], set(ch) - known)   # only known keys
        for pat in (ch.get("regex") or []):
            _re.compile(pat)                                    # every regex compiles


def test_numeric_ignores_malformed_number_tokens():
    # A malformed token (version string) alongside the correct number must still
    # pass — the check parses each token defensively.
    ans = "in version 1.2.3 the monthly payment is 1,432.86 dollars"
    assert evals.objective_score(ans, {"numeric": {"value": 1432.86, "tolerance": 0.5}})[0] == 1.0
