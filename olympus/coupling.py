"""Predictability report — WAVE1_IMPLEMENTATION_SPEC §C7 (PR 8, first half).

Prefetch is BANNED until predictability is proven (synthesis ruling R10 /
Colibri's LOOKA-first discipline). This module is the measurement that could
one day justify it: an offline-only analyzer over the traces Olympus already
records. It evaluates four predictors of "which specialist runs next":

  P1  marginal frequency        — predict the overall most-frequent successors
  P2  previous-specialist       — predict from a prev→next conditional table
  P3  task-keyword              — predict a step's specialist from its plan
                                  task text (simple bag-of-words votes)
  P4  plan-adherence            — predict the executed SET from the plan
                                  decision (recall/precision of the plan)

For each predictor: recall@1/@2, precision, wasted-work rate, estimated
avoided latency (when per-specialist span timings exist in events — real
traces today do not carry them, so the report says so instead of inventing a
number), Wilson 95% CIs, a per-task_type split (labels joined from
`routing_outcomes` rows), and sample sizes.

Invariants:
  I-P1 — READ-ONLY over MEMORY_DIR: this module performs ZERO writes anywhere
         (the CLI handles the optional --out file; stdout is the report path).
  I-P2 — sample sizes always stated; the report ABSTAINS below n=50 runs
         overall ("insufficient_data", not numbers) and per-cell below n=20.

Acceptance floors (domain 08): a predictor qualifies for *future* prefetch
consideration only when recall@2 ≥ 0.6 with CI half-width ≤ 0.1 on n ≥ 200.
No prefetch code, no activation path, no network — nothing here executes
anything.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from . import config

__all__ = ["predictability_report", "render_report", "wilson_interval"]

# --- thresholds (I-P2 + floors) ----------------------------------------------
MIN_RUNS = 50            # abstain below this many usable runs overall
MIN_CELL = 20            # per-task_type cells abstain below this
FLOOR_RECALL2 = 0.6      # floors: recall@2 threshold
FLOOR_HALF_WIDTH = 0.1   # floors: CI half-width ceiling
FLOOR_N = 200            # floors: minimum prediction points
_FLOOR_RULE = (f"recall@2 >= {FLOOR_RECALL2} with Wilson-CI half-width <= "
               f"{FLOOR_HALF_WIDTH} on n >= {FLOOR_N}")

_WORD = re.compile(r"[a-z][a-z0-9]{2,}")


# --- Wilson 95% interval (reuse calibration's when importable) ----------------
try:                                            # pragma: no cover - trivial
    from .calibration import wilson_interval    # noqa: F401
except Exception:                               # pragma: no cover - fallback
    def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
        if n <= 0:
            return (0.0, 1.0)
        p = k / n
        d = 1 + z * z / n
        centre = p + z * z / (2 * n)
        margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        return (max(0.0, (centre - margin) / d), min(1.0, (centre + margin) / d))


# --- trace ingestion (read-only; mirrors trace.load_run's tolerance) ----------

def _trace_files(days: int) -> list:
    base = config.MEMORY_DIR / "traces"
    if not base.exists():
        return []
    cutoff = date.today() - timedelta(days=max(1, int(days)))
    out = []
    for path in sorted(base.glob("*.jsonl")):
        stem = path.stem
        try:
            d = date(int(stem[:4]), int(stem[4:6]), int(stem[6:8]))
        except (ValueError, IndexError):
            out.append(path)            # overflow-*.jsonl etc. — include
            continue
        if d >= cutoff:
            out.append(path)
    return out


def _load_runs(days: int) -> list[dict]:
    runs: list[dict] = []
    for path in _trace_files(days):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue                # torn/corrupt lines are skipped, never fixed
            if isinstance(rec, dict) and (rec.get("events")
                                          or rec.get("decisions")):
                runs.append(rec)
    return runs


def _plan_steps(rec: dict) -> list[dict]:
    """The 'plan' decision's rationale: Athena's step list
    [{"id","specialist","task","depends_on"},...] — verbatim from the trace."""
    for d in rec.get("decisions") or []:
        if (isinstance(d, dict) and d.get("decision_type") == "plan"
                and isinstance(d.get("rationale"), list)):
            return [s for s in d["rationale"]
                    if isinstance(s, dict) and s.get("specialist")]
    return []


def _executed_sequence(rec: dict) -> list[str]:
    """The run's executed specialist sequence: dag.level order (step ids mapped
    through the plan) when present, else the dispatch event's list order."""
    by_id = {str(s.get("id")): str(s.get("specialist"))
             for s in _plan_steps(rec)}
    seq: list[str] = []
    for ev in rec.get("events") or []:
        if (isinstance(ev, dict) and ev.get("stage") == "dag.level"
                and isinstance(ev.get("steps"), list)):
            for sid in ev["steps"]:
                sp = by_id.get(str(sid))
                if sp:
                    seq.append(sp)
    if seq:
        return seq
    for ev in rec.get("events") or []:
        if (isinstance(ev, dict) and ev.get("stage") == "dispatch"
                and isinstance(ev.get("specialists"), list)):
            return [s for s in ev["specialists"] if isinstance(s, str)]
    return []


def _specialist_secs(rec: dict) -> dict[str, float]:
    """Per-specialist span seconds, when events carry them (events with both a
    'specialist' and a numeric 'secs' field). Today's pipeline records only
    stage-level spans, so this is usually empty — the report then omits the
    avoided-latency estimate with a note instead of fabricating one."""
    out: dict[str, float] = {}
    for ev in rec.get("events") or []:
        if (isinstance(ev, dict) and isinstance(ev.get("specialist"), str)
                and isinstance(ev.get("secs"), (int, float))):
            out[ev["specialist"]] = out.get(ev["specialist"], 0.0) + float(ev["secs"])
    return out


def _run_task_types() -> dict[str, str]:
    """run_id → task_type, joined from routing_outcomes rows (majority label
    when a run's rows disagree). Best-effort; {} when the store is empty."""
    try:
        from . import routing_outcomes, store
        if isinstance(store.backend(), store.FileStore):
            # I-P1 guard: FileStore lazily mkdirs its namespace dir on any
            # read — skip entirely when nothing was ever written, so this
            # module provably performs zero writes under MEMORY_DIR.
            if not (config.MEMORY_DIR / "store" / "routing_outcomes").exists():
                return {}
        rows = routing_outcomes._all_rows()
    except Exception:
        return {}
    votes: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if isinstance(r, dict) and r.get("run_id") and r.get("task_type"):
            votes[str(r["run_id"])][str(r["task_type"])] += 1
    return {rid: c.most_common(1)[0][0] for rid, c in votes.items()}


# --- metric assembly -----------------------------------------------------------

def _metrics(hits1: int, hits2: int, n: int) -> dict:
    r1 = hits1 / n if n else 0.0
    r2 = hits2 / n if n else 0.0
    lo1, hi1 = wilson_interval(hits1, n)
    lo2, hi2 = wilson_interval(hits2, n)
    return {
        "n": n,
        "recall_at_1": round(r1, 4),
        "recall_at_2": round(r2, 4),
        "ci_at_1": [round(lo1, 4), round(hi1, 4)],
        "ci_at_2": [round(lo2, 4), round(hi2, 4)],
        "ci_half_width_at_2": round((hi2 - lo2) / 2, 4),
        # precision = top-1 accuracy (single-label prediction: the one acted-on
        # prediction is either right or wasted).
        "precision": round(r1, 4),
        # wasted-work: share of the 2n predicted-@2 slots that were never used.
        "wasted_work_rate": round((2 * n - hits2) / (2 * n), 4) if n else 0.0,
    }


def _eval_topk(points: list[tuple[Any, str, str, float | None]],
               predict: Any) -> dict:
    """Evaluate a top-2 predictor over prediction points
    (context, actual, task_type, secs_of_actual). `predict(context)` returns
    an ordered candidate list; only the first two are scored."""
    hits1 = hits2 = 0
    avoided = 0.0
    have_secs = False
    per_type: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # h1,h2,n
    for ctx, actual, ttype, secs in points:
        top = list(predict(ctx))[:2]
        h1 = bool(top) and top[0] == actual
        h2 = actual in top
        hits1 += h1
        hits2 += h2
        cell = per_type[ttype]
        cell[0] += h1
        cell[1] += h2
        cell[2] += 1
        if secs is not None:
            have_secs = True
            if h2:
                avoided += secs
    n = len(points)
    out = _metrics(hits1, hits2, n)
    if have_secs:
        out["avoided_latency_secs"] = round(avoided, 2)
    else:
        out["avoided_latency_secs"] = None
        out["latency_note"] = ("no per-specialist span timings in traces — "
                               "avoided-latency estimate omitted")
    by_type: dict[str, Any] = {}
    for ttype, (h1, h2, cn) in sorted(per_type.items()):
        by_type[ttype] = (_metrics(h1, h2, cn) if cn >= MIN_CELL
                          else {"n": cn, "status": "insufficient_data"})
    out["by_task_type"] = by_type
    return out


def predictability_report(days: int = 30) -> dict:
    """The C7 report. Read-only over MEMORY_DIR (I-P1); abstains below n=50
    runs (I-P2). Output schema: {"v":1,"n_runs",...,"floors":{...}}."""
    runs = _load_runs(days)
    ttypes = _run_task_types()
    usable: list[tuple[dict, list[str]]] = []
    for rec in runs:
        seq = _executed_sequence(rec)
        if seq:
            usable.append((rec, seq))
    n_runs = len(usable)
    report: dict[str, Any] = {"v": 1, "n_runs": n_runs, "days": int(days)}
    if n_runs < MIN_RUNS:
        report["status"] = "insufficient_data"
        report["note"] = (f"need >= {MIN_RUNS} runs with an executed specialist "
                          f"sequence (have {n_runs}) — abstaining (I-P2)")
        report["by_predictor"] = {}
        report["floors"] = {"pass": False, "rule": _FLOOR_RULE,
                            "qualifying": [], "note": "insufficient data"}
        return report
    report["status"] = "ok"

    # -- prediction points: every within-run transition (prev → next) ---------
    marginal_next: Counter = Counter()      # successor-position frequencies
    conditional: dict[str, Counter] = defaultdict(Counter)
    transitions: list[tuple[str, str, str, float | None]] = []
    for rec, seq in usable:
        ttype = ttypes.get(str(rec.get("id")), "unlabeled")
        secs_map = _specialist_secs(rec)
        for prev, nxt in zip(seq, seq[1:]):
            marginal_next[nxt] += 1
            conditional[prev][nxt] += 1
            transitions.append((prev, nxt, ttype, secs_map.get(nxt)))

    marginal_top = [s for s, _ in marginal_next.most_common(2)]

    def _p1(_ctx: str) -> list[str]:
        return marginal_top

    def _p2(prev: str) -> list[str]:
        ranked = [s for s, _ in conditional[prev].most_common(2)]
        for s in marginal_top:              # back-fill unseen contexts
            if len(ranked) >= 2:
                break
            if s not in ranked:
                ranked.append(s)
        return ranked

    p1_points = [((), nxt, tt, secs) for _, nxt, tt, secs in transitions]
    p2_points = [(prev, nxt, tt, secs) for prev, nxt, tt, secs in transitions]

    # -- P3: task-keyword votes over planned steps ----------------------------
    kw_votes: dict[str, Counter] = defaultdict(Counter)
    step_points: list[tuple[frozenset, str, str, float | None]] = []
    for rec, seq in usable:
        ttype = ttypes.get(str(rec.get("id")), "unlabeled")
        secs_map = _specialist_secs(rec)
        executed = set(seq)
        for step in _plan_steps(rec):
            spec = str(step.get("specialist"))
            words = frozenset(_WORD.findall(str(step.get("task", "")).lower()))
            if not words or spec not in executed:
                continue
            for w in words:
                kw_votes[w][spec] += 1
            step_points.append((words, spec, ttype, secs_map.get(spec)))

    def _p3(words: frozenset) -> list[str]:
        score: Counter = Counter()
        for w in words:
            score.update(kw_votes.get(w, {}))
        if not score:
            return marginal_top
        return [s for s, _ in score.most_common(2)]

    # -- P4: plan-adherence (predicted executed set = planned set) ------------
    inter_sum = plan_sum = exec_sum = 0
    p4_runs = 0
    p4_by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for rec, seq in usable:
        planned = {str(s.get("specialist")) for s in _plan_steps(rec)}
        if not planned:
            continue
        executed = set(seq)
        inter = len(planned & executed)
        inter_sum += inter
        plan_sum += len(planned)
        exec_sum += len(executed)
        p4_runs += 1
        cell = p4_by_type[ttypes.get(str(rec.get("id")), "unlabeled")]
        cell[0] += inter
        cell[1] += len(planned)
        cell[2] += len(executed)
        cell[3] += 1

    def _p4_metrics(inter: int, planned: int, executed: int, nruns: int) -> dict:
        rec_ = inter / executed if executed else 0.0
        prec = inter / planned if planned else 0.0
        lo, hi = wilson_interval(inter, executed)
        return {
            "n": nruns,
            "n_predictions": executed,
            # set-prediction: recall@k is the set recall (k has no rank meaning)
            "recall_at_1": round(rec_, 4),
            "recall_at_2": round(rec_, 4),
            "ci_at_2": [round(lo, 4), round(hi, 4)],
            "ci_half_width_at_2": round((hi - lo) / 2, 4),
            "precision": round(prec, 4),
            "wasted_work_rate": round(1 - prec, 4),
        }

    p4 = _p4_metrics(inter_sum, plan_sum, exec_sum, p4_runs)
    p4["avoided_latency_secs"] = None
    p4["latency_note"] = ("set-level predictor — avoided latency requires "
                          "per-specialist span timings (not recorded)")
    p4["by_task_type"] = {
        tt: (_p4_metrics(i, p, e, nr) if nr >= MIN_CELL
             else {"n": nr, "status": "insufficient_data"})
        for tt, (i, p, e, nr) in sorted(p4_by_type.items())}

    by_predictor = {
        "P1_marginal": _eval_topk(p1_points, _p1),
        "P2_prev_specialist": _eval_topk(p2_points, _p2),
        "P3_task_keyword": _eval_topk(step_points, _p3),
        "P4_plan_adherence": p4,
    }
    report["by_predictor"] = by_predictor

    # -- floors ----------------------------------------------------------------
    qualifying = []
    for name, m in by_predictor.items():
        n = m.get("n_predictions", m.get("n", 0)) if name == "P4_plan_adherence" \
            else m.get("n", 0)
        if (n >= FLOOR_N and m.get("recall_at_2", 0.0) >= FLOOR_RECALL2
                and m.get("ci_half_width_at_2", 1.0) <= FLOOR_HALF_WIDTH):
            qualifying.append(name)
    report["floors"] = {"pass": bool(qualifying), "rule": _FLOOR_RULE,
                        "qualifying": qualifying}
    report["notes"] = [
        "read-only analysis (I-P1): no state was written",
        "in-sample evaluation over recorded traces — a measurement report, "
        "not a trained model",
        "prefetch remains disabled: this module contains no activation path",
    ]
    return report


def render_report(report: dict) -> str:
    """A compact fixed-width table of the report (pure; no I/O)."""
    lines = ["Predictability report (C7 — offline, read-only; prefetch stays "
             "disabled)"]
    lines.append(f"  runs analysed    : {report.get('n_runs', 0)} "
                 f"(last {report.get('days', '?')} days)")
    if report.get("status") == "insufficient_data":
        lines.append(f"  verdict          : INSUFFICIENT DATA — "
                     f"{report.get('note', '')}")
        return "\n".join(lines)
    lines.append(f"  {'predictor':22s} {'n':>6s} {'r@1':>6s} {'r@2':>6s} "
                 f"{'±CI2':>6s} {'prec':>6s} {'waste':>6s}")
    for name, m in (report.get("by_predictor") or {}).items():
        lines.append(
            f"  {name:22s} {m.get('n', 0):>6d} "
            f"{m.get('recall_at_1', 0.0):>6.2f} "
            f"{m.get('recall_at_2', 0.0):>6.2f} "
            f"{m.get('ci_half_width_at_2', 0.0):>6.2f} "
            f"{m.get('precision', 0.0):>6.2f} "
            f"{m.get('wasted_work_rate', 0.0):>6.2f}")
        note = m.get("latency_note")
        if m.get("avoided_latency_secs") is not None:
            lines.append(f"  {'':22s} avoided latency ≈ "
                         f"{m['avoided_latency_secs']}s")
        elif note:
            lines.append(f"  {'':22s} ({note})")
    floors = report.get("floors") or {}
    lines.append("")
    if floors.get("pass"):
        lines.append("  FLOORS           : ✅ PASS — "
                     + ", ".join(floors.get("qualifying", []))
                     + f" meet [{floors.get('rule', '')}]")
    else:
        lines.append(f"  FLOORS           : ⛔ NOT MET [{floors.get('rule', '')}]"
                     " — prefetch consideration stays closed")
    return "\n".join(lines)
