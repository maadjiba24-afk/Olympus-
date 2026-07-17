"""Live-trace online evaluation — sampled quality scoring of recent runs.

Olympus records every run as a signed decision log (`trace.py`). Live-eval reads
a bounded SAMPLE of recent runs and scores each with cheap, rule-based scorers
(was it verified? any contract violation? any errored decision? within latency /
cost bounds?), producing a pass-rate and a regression signal that lands in the
feature-evolution telemetry and the structured evolution log — so a quality
regression surfaces on its own instead of after a complaint.

The scoring core is PURE: a `Scorer` is `(run) -> (name, passed, detail)` over a
run dict, and `score_run` / `evaluate` are deterministic given their inputs (no
clock, no rng — sampling is a fixed stride). An optional LLM-judge scorer is
pluggable and off unless supplied. The whole thing is opt-in (`OLYMPUS_LIVE_EVAL`,
off by default) and bounded (sample size cap), layered on the existing trace
substrate — it reads runs, it never changes them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_MAX_SAMPLE = 50
_DEFAULT_SAMPLE = 20
_MAX_FILES = 7               # scan at most a week of daily trace files


def enabled() -> bool:
    return os.environ.get("OLYMPUS_LIVE_EVAL", "").strip().lower() in (
        "1", "on", "true", "yes")


def _latency_bound() -> float:
    try:
        return float(os.environ.get("OLYMPUS_LIVE_EVAL_MAX_SECS", "120"))
    except ValueError:
        return 120.0


def _cost_bound() -> float:
    try:
        return float(os.environ.get("OLYMPUS_LIVE_EVAL_MAX_COST", "1.0"))
    except ValueError:
        return 1.0


# --- rule-based scorers (pure) --------------------------------------------
# Each returns (name, passed, detail). A scorer only judges a run it applies to;
# an inapplicable dimension passes vacuously (True) rather than penalizing.

Scorer = Callable[[dict], "tuple[str, bool, str]"]


def _decisions(run: dict) -> list:
    d = run.get("decisions")
    return d if isinstance(d, list) else []


def score_no_violation(run: dict):
    bad = [d for d in _decisions(run)
           if (d.get("outcome") or {}).get("status") == "violation"]
    return ("no_contract_violation", not bad,
            f"{len(bad)} violation decision(s)" if bad else "")


def score_no_errored_decision(run: dict):
    bad = [d for d in _decisions(run)
           if (d.get("outcome") or {}).get("error")]
    return ("no_errored_decision", not bad,
            f"{len(bad)} errored decision(s)" if bad else "")


def score_verified(run: dict):
    """A delegate run should carry a review/verify decision that did not fail.
    Non-delegate runs (direct answers) pass vacuously."""
    decs = _decisions(run)
    reviews = [d for d in decs if d.get("decision_type") in ("review", "verify")]
    if not reviews:
        return ("verified", True, "")          # not a verified pipeline → n/a
    ok = all((d.get("outcome") or {}).get("status") == "ok" for d in reviews)
    return ("verified", ok, "" if ok else "a review decision did not pass")


def score_within_latency(run: dict):
    secs = float(run.get("total_secs", 0) or 0)
    bound = _latency_bound()
    return ("within_latency", secs <= bound,
            f"{secs:.1f}s > {bound:.0f}s" if secs > bound else "")


def score_within_cost(run: dict):
    cost = sum(float(d.get("cost", 0) or 0) for d in _decisions(run))
    bound = _cost_bound()
    return ("within_cost", cost <= bound,
            f"${cost:.4f} > ${bound:.2f}" if cost > bound else "")


_DEFAULT_SCORERS: list[Scorer] = [
    score_no_violation, score_no_errored_decision, score_verified,
    score_within_latency, score_within_cost,
]


# --- scoring (pure) -------------------------------------------------------

@dataclass
class RunScore:
    run_id: str
    passed: bool
    scores: list           # [(name, passed, detail), ...]
    fail_reasons: list = field(default_factory=list)


def score_run(run: dict, scorers: list | None = None) -> RunScore:
    """Score one run. Pure: a scorer that raises is treated as a non-fatal skip
    (it neither passes nor fails the run) so one bad scorer can't corrupt the
    signal."""
    scs = scorers if scorers is not None else _DEFAULT_SCORERS
    results, reasons, ok = [], [], True
    for scorer in scs:
        try:
            name, passed, detail = scorer(run)
        except Exception:
            continue
        results.append((name, bool(passed), detail))
        if not passed:
            ok = False
            reasons.append(f"{name}: {detail}" if detail else name)
    return RunScore(run_id=str(run.get("id", "")), passed=ok, scores=results,
                    fail_reasons=reasons)


@dataclass
class EvalReport:
    sampled: int
    passed: int
    failed: int
    pass_rate: float
    failures: list = field(default_factory=list)   # [(run_id, [reasons]), ...]

    def as_dict(self) -> dict:
        return {"sampled": self.sampled, "passed": self.passed,
                "failed": self.failed, "pass_rate": round(self.pass_rate, 3),
                "failures": self.failures[:10]}


def _sample(runs: list, sample: int) -> list:
    """Deterministic sample: the most recent `sample` runs (input is oldest→
    newest, so take the tail). Bounded by `_MAX_SAMPLE`."""
    n = max(1, min(int(sample), _MAX_SAMPLE))
    return runs[-n:]


def evaluate(runs: list, *, sample: int = _DEFAULT_SAMPLE,
             scorers: list | None = None) -> EvalReport:
    """Score a sample of runs and aggregate. Pure + deterministic."""
    chosen = _sample([r for r in (runs or []) if isinstance(r, dict)], sample)
    passed = 0
    failures = []
    for run in chosen:
        rs = score_run(run, scorers)
        if rs.passed:
            passed += 1
        else:
            failures.append((rs.run_id, rs.fail_reasons))
    n = len(chosen)
    return EvalReport(sampled=n, passed=passed, failed=n - passed,
                      pass_rate=(passed / n if n else 1.0), failures=failures)


# --- reader (thin, best-effort I/O over the trace substrate) --------------

def recent_runs(limit: int = _DEFAULT_SAMPLE) -> list:
    """Read the most recent recorded runs from the daily trace jsonl files.
    Oldest→newest, best-effort (a corrupt line is skipped). Bounded."""
    from . import config
    base = Path(config.MEMORY_DIR) / "traces"
    if not base.is_dir():
        return []
    cap = max(1, min(int(limit), _MAX_SAMPLE))
    files = sorted(base.glob("*.jsonl"))[-_MAX_FILES:]
    runs: list = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(obj, dict):
                runs.append(obj)
    return runs[-cap:]


# --- run entry (telemetry + surfacing) ------------------------------------

def _tuned_sample() -> int:
    """Self-tuned sample size (widened within [10,50] by the evolve reviewer when
    the signal is noisy). Falls back to the default on read error."""
    try:
        from . import evolve
        return int(evolve.current("liveeval", "sample_size"))
    except (KeyError, ValueError, TypeError, ImportError, OSError):
        return _DEFAULT_SAMPLE


def run(sample: int | None = None) -> list[str]:
    """Heartbeat entry: sample recent runs, score them, record the regression
    signal to evolve telemetry + the structured log. No-op unless enabled;
    returns human log lines (empty when nothing to report)."""
    if not enabled():
        return []
    sample = _tuned_sample() if sample is None else sample
    try:
        report = evaluate(recent_runs(sample), sample=sample)
    except Exception as err:
        from . import errors
        errors.capture("liveeval.run", err)
        return []
    if report.sampled == 0:
        return []
    from . import evolve
    ok = report.pass_rate >= 0.9
    evolve.record("liveeval", evolve.OK if ok else evolve.DEGRADED,
                  f"pass_rate={report.pass_rate:.2f} "
                  f"({report.passed}/{report.sampled})")
    evolve.log_event("liveeval", "eval", {
        "sampled": report.sampled, "passed": report.passed,
        "failed": report.failed, "pass_rate": round(report.pass_rate, 3)})
    if report.failed == 0:
        return []
    return [f"Live-eval: {report.passed}/{report.sampled} runs passed "
            f"({report.pass_rate:.0%}); {report.failed} regression(s)."]


def report(sample: int = _DEFAULT_SAMPLE) -> dict:
    """Admin-panel view: the current sampled eval, or a disabled marker."""
    if not enabled():
        return {"enabled": False}
    try:
        rep = evaluate(recent_runs(sample), sample=sample)
    except Exception:
        return {"enabled": True, "error": "eval failed"}
    return {"enabled": True, **rep.as_dict()}
