"""D8 — live-trace online eval: pure rule-based scoring of recent runs, bounded
+ deterministic sampling, opt-in, regression signal to telemetry."""

import json

from olympus import liveeval


def _run(rid="r1", *, total_secs=1.0, decisions=None):
    return {"id": rid, "total_secs": total_secs,
            "decisions": decisions if decisions is not None else []}


def _dec(dtype="plan", status="ok", error="", cost=0.0):
    return {"decision_type": dtype, "cost": cost,
            "outcome": {"status": status, "error": error}}


# --- individual scorers ---------------------------------------------------

def test_no_violation_scorer():
    good = _run(decisions=[_dec(status="ok")])
    bad = _run(decisions=[_dec("contract", status="violation")])
    assert liveeval.score_no_violation(good)[1] is True
    assert liveeval.score_no_violation(bad)[1] is False


def test_no_errored_decision_scorer():
    bad = _run(decisions=[_dec(error="boom")])
    assert liveeval.score_no_errored_decision(bad)[1] is False
    assert liveeval.score_no_errored_decision(_run())[1] is True


def test_verified_scorer_is_vacuous_without_review():
    assert liveeval.score_verified(_run())[1] is True          # direct answer n/a
    ok = _run(decisions=[_dec("review", status="ok")])
    bad = _run(decisions=[_dec("review", status="fail")])
    assert liveeval.score_verified(ok)[1] is True
    assert liveeval.score_verified(bad)[1] is False


def test_latency_and_cost_bounds(monkeypatch):
    monkeypatch.setenv("OLYMPUS_LIVE_EVAL_MAX_SECS", "10")
    monkeypatch.setenv("OLYMPUS_LIVE_EVAL_MAX_COST", "0.50")
    assert liveeval.score_within_latency(_run(total_secs=5))[1] is True
    assert liveeval.score_within_latency(_run(total_secs=99))[1] is False
    cheap = _run(decisions=[_dec(cost=0.1)])
    dear = _run(decisions=[_dec(cost=5.0)])
    assert liveeval.score_within_cost(cheap)[1] is True
    assert liveeval.score_within_cost(dear)[1] is False


# --- score_run ------------------------------------------------------------

def test_score_run_aggregates():
    clean = _run("ok1", decisions=[_dec("review", status="ok")])
    dirty = _run("bad1", decisions=[_dec("contract", status="violation"),
                                    _dec(error="x")])
    assert liveeval.score_run(clean).passed is True
    rs = liveeval.score_run(dirty)
    assert rs.passed is False and len(rs.fail_reasons) >= 2


def test_bad_scorer_is_skipped_not_fatal():
    def boom(run):
        raise RuntimeError("scorer bug")
    rs = liveeval.score_run(_run(), scorers=[boom, liveeval.score_no_violation])
    assert rs.passed is True         # the good scorer still counts; boom skipped


# --- evaluate: sampling + aggregation -------------------------------------

def test_evaluate_pass_rate():
    runs = [_run(f"g{i}", decisions=[_dec(status="ok")]) for i in range(8)]
    runs += [_run("b1", decisions=[_dec("contract", status="violation")])]
    rep = liveeval.evaluate(runs, sample=20)
    assert rep.sampled == 9 and rep.passed == 8 and rep.failed == 1
    assert 0.88 < rep.pass_rate < 0.9 or round(rep.pass_rate, 2) == 0.89


def test_sampling_is_bounded_and_recent():
    runs = [_run(f"r{i}") for i in range(200)]
    rep = liveeval.evaluate(runs, sample=999)     # request more than the cap
    assert rep.sampled <= liveeval._MAX_SAMPLE


def test_sampling_takes_most_recent():
    runs = [_run("old", decisions=[_dec("contract", status="violation")])]
    runs += [_run(f"new{i}", decisions=[_dec(status="ok")]) for i in range(5)]
    rep = liveeval.evaluate(runs, sample=5)       # drops the oldest (the bad one)
    assert rep.failed == 0 and rep.sampled == 5


def test_evaluate_empty_is_perfect():
    rep = liveeval.evaluate([], sample=10)
    assert rep.sampled == 0 and rep.pass_rate == 1.0


def test_evaluate_ignores_non_dict_runs():
    rep = liveeval.evaluate([None, "x", _run()], sample=10)
    assert rep.sampled == 1


# --- reader ---------------------------------------------------------------

def test_recent_runs_reads_trace_jsonl(tmp_path, monkeypatch):
    from olympus import config
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    d = tmp_path / "traces"
    d.mkdir()
    (d / "20260101.jsonl").write_text(
        json.dumps(_run("a")) + "\n"
        + "{corrupt\n"                                   # skipped
        + json.dumps(_run("b")) + "\n",
        encoding="utf-8")
    runs = liveeval.recent_runs(10)
    assert [r["id"] for r in runs] == ["a", "b"]


def test_recent_runs_no_dir(tmp_path, monkeypatch):
    from olympus import config
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "nope")
    assert liveeval.recent_runs() == []


# --- run(): gating + telemetry --------------------------------------------

def test_run_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OLYMPUS_LIVE_EVAL", raising=False)
    assert liveeval.run() == []
    assert liveeval.report()["enabled"] is False


def test_run_enabled_reports_regressions(monkeypatch):
    monkeypatch.setenv("OLYMPUS_LIVE_EVAL", "1")
    monkeypatch.setattr(liveeval, "recent_runs", lambda sample=20: [
        _run("g", decisions=[_dec(status="ok")]),
        _run("b", decisions=[_dec("contract", status="violation")])])
    lines = liveeval.run()
    assert lines and "regression" in lines[0]
    rep = liveeval.report()
    assert rep["enabled"] is True and rep["failed"] == 1
