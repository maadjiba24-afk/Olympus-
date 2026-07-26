"""C7 — predictability report (WAVE1_IMPLEMENTATION_SPEC §C7).

Synthetic trace corpora with KNOWN conditional structure prove the predictor
math; the fs-audit test proves I-P1 (read-only over MEMORY_DIR); abstention
proves I-P2; the absence test proves prefetch stays disabled (A11).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from olympus import config, coupling


# --- synthetic trace corpus -----------------------------------------------------

def _mk_run(run_id: str, seq: list[str], *, secs: float | None = None,
            tasks: list[str] | None = None, with_plan: bool = True) -> dict:
    steps = [{"id": f"s{i}", "specialist": sp,
              "task": (tasks[i] if tasks else f"do {sp} things"),
              "depends_on": ([f"s{i - 1}"] if i else [])}
             for i, sp in enumerate(seq)]
    events: list[dict] = [{"t": 0.1, "stage": "dispatch",
                           "specialists": list(seq), "dag": True}]
    for i, sp in enumerate(seq):
        events.append({"t": 0.2, "stage": "dag.level", "n": i + 1,
                       "steps": [f"s{i}"]})
        if secs is not None:
            events.append({"t": 0.3, "stage": "specialist.end",
                           "specialist": sp, "secs": secs})
    decisions = [{
        "schema_version": 1, "record_id": f"r-{run_id}", "run_id": run_id,
        "decision_type": "plan",
        "agent": {"name": "athena", "role": "supervisor"},
        "rationale": steps,
        "outcome": {"status": "ok", "error": ""},
    }] if with_plan else []
    return {"id": run_id, "kind": "chat", "user": "shared",
            "total_secs": 1.0, "meta": {}, "events": events,
            "decisions": decisions}


def _write_runs(runs: list[dict]) -> None:
    base = config.MEMORY_DIR / "traces"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{time.strftime('%Y%m%d')}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for r in runs:
            f.write(json.dumps(r) + "\n")


def _conditional_corpus(n: int = 300, follow_rate: float = 0.8,
                        secs: float | None = None) -> list[dict]:
    """B follows A in `follow_rate` of runs (deterministic pattern), else C —
    the known conditional structure the P2 assertions check against."""
    period = 10
    b_slots = round(follow_rate * period)
    runs = []
    for i in range(n):
        nxt = "hephaestus" if (i % period) < b_slots else "plutus"
        runs.append(_mk_run(f"run{i:04d}", ["argus", nxt], secs=secs,
                            tasks=["research market trends",
                                   ("write python code" if nxt == "hephaestus"
                                    else "model finance numbers")]))
    return runs


# --- 1. predictor math on known structure -----------------------------------------

def test_p2_recall_matches_known_conditional_structure():
    _write_runs(_conditional_corpus(n=300, follow_rate=0.8))
    rep = coupling.predictability_report(days=30)
    assert rep["v"] == 1 and rep["status"] == "ok"
    assert rep["n_runs"] == 300
    p2 = rep["by_predictor"]["P2_prev_specialist"]
    assert p2["n"] == 300                       # one transition per run
    assert p2["recall_at_1"] == pytest.approx(0.8, abs=0.05)
    assert p2["recall_at_2"] == pytest.approx(1.0)
    lo, hi = p2["ci_at_2"]
    assert 0.0 <= lo <= p2["recall_at_2"] <= hi <= 1.0
    # precision = top-1 accuracy; wasted work = unused predicted-@2 slots.
    assert p2["precision"] == p2["recall_at_1"]
    assert p2["wasted_work_rate"] == pytest.approx(0.5, abs=0.01)


def test_p1_marginal_and_p3_keywords_and_p4_adherence():
    _write_runs(_conditional_corpus(n=300, follow_rate=0.8))
    rep = coupling.predictability_report(days=30)
    p1 = rep["by_predictor"]["P1_marginal"]
    # Marginal over successor positions: hephaestus 80% / plutus 20%.
    assert p1["recall_at_1"] == pytest.approx(0.8, abs=0.05)
    assert p1["recall_at_2"] == pytest.approx(1.0)
    p3 = rep["by_predictor"]["P3_task_keyword"]
    # Task words are perfectly separable in this corpus → near-perfect recall.
    assert p3["n"] == 600                       # every planned step executed
    assert p3["recall_at_1"] > 0.95
    p4 = rep["by_predictor"]["P4_plan_adherence"]
    # Executed set == planned set in every synthetic run.
    assert p4["recall_at_2"] == pytest.approx(1.0)
    assert p4["precision"] == pytest.approx(1.0)
    assert p4["wasted_work_rate"] == pytest.approx(0.0)


def test_avoided_latency_from_span_secs_and_note_when_absent():
    _write_runs(_conditional_corpus(n=300, follow_rate=0.8, secs=2.0))
    rep = coupling.predictability_report(days=30)
    p2 = rep["by_predictor"]["P2_prev_specialist"]
    # All 300 next-steps are top-2 hits at 2.0s each.
    assert p2["avoided_latency_secs"] == pytest.approx(600.0)


def test_latency_note_when_no_span_timings():
    _write_runs(_conditional_corpus(n=300, follow_rate=0.8))
    rep = coupling.predictability_report(days=30)
    p2 = rep["by_predictor"]["P2_prev_specialist"]
    assert p2["avoided_latency_secs"] is None
    assert "no per-specialist span timings" in p2["latency_note"]


# --- 2. abstention (I-P2) ----------------------------------------------------------

def test_abstains_below_min_runs():
    _write_runs(_conditional_corpus(n=10))
    rep = coupling.predictability_report(days=30)
    assert rep["status"] == "insufficient_data"
    assert rep["n_runs"] == 10
    assert rep["by_predictor"] == {}
    assert rep["floors"]["pass"] is False
    assert "abstaining" in rep["note"]


def test_no_traces_dir_is_insufficient_data_not_error():
    rep = coupling.predictability_report(days=30)
    assert rep["status"] == "insufficient_data" and rep["n_runs"] == 0


def test_per_task_type_cells_abstain_below_20():
    _write_runs(_conditional_corpus(n=300))
    from olympus import routing_outcomes as ro
    # Label only a handful of runs → their cells stay below the n=20 floor.
    for i in range(3):
        ro.record_run("shared", f"run{i:04d}", "msg", ["hephaestus"],
                      models={"hephaestus": "m"}, roles={"hephaestus": "code"},
                      review_verdict="approve")
    rep = coupling.predictability_report(days=30)
    by_type = rep["by_predictor"]["P2_prev_specialist"]["by_task_type"]
    assert by_type["code"] == {"n": 3, "status": "insufficient_data"}
    assert by_type["unlabeled"]["n"] == 297    # the big cell reports numbers


# --- 3. floors -----------------------------------------------------------------------

def test_floors_pass_on_strong_predictor():
    _write_runs(_conditional_corpus(n=300, follow_rate=0.8))
    rep = coupling.predictability_report(days=30)
    floors = rep["floors"]
    assert floors["pass"] is True
    assert "P2_prev_specialist" in floors["qualifying"]
    assert "recall@2 >= 0.6" in floors["rule"]


def test_floors_fail_below_n200_even_with_high_recall():
    _write_runs(_conditional_corpus(n=60, follow_rate=0.8))   # ok but n<200
    rep = coupling.predictability_report(days=30)
    assert rep["status"] == "ok"
    p2 = rep["by_predictor"]["P2_prev_specialist"]
    assert p2["recall_at_2"] == pytest.approx(1.0)
    assert rep["floors"]["pass"] is False
    assert rep["floors"]["qualifying"] == []


def test_floors_fail_on_unpredictable_corpus():
    # Successor drawn round-robin over five specialists → recall@2 ≈ 0.4.
    # Plan-free runs (dispatch-event fallback), so the trivially-perfect
    # plan-adherence predictor has no sample to qualify on either.
    period = 5
    pool = ["hephaestus", "plutus", "peitho", "chronos", "aegis"]
    runs = [_mk_run(f"u{i:04d}", ["argus", pool[i % period]], with_plan=False)
            for i in range(300)]
    _write_runs(runs)
    rep = coupling.predictability_report(days=30)
    p2 = rep["by_predictor"]["P2_prev_specialist"]
    assert p2["recall_at_2"] == pytest.approx(0.4, abs=0.05)
    assert rep["floors"]["pass"] is False


# --- 4. read-only guarantee (I-P1) ----------------------------------------------------

def _fs_snapshot(root: Path) -> dict:
    snap = {}
    for p in sorted(root.rglob("*")):
        st = p.stat()
        snap[str(p)] = (st.st_mtime_ns, st.st_size if p.is_file() else -1)
    return snap


def test_report_is_read_only_over_memory_dir():
    _write_runs(_conditional_corpus(n=300))
    from olympus import routing_outcomes as ro
    ro.record_run("shared", "run0000", "msg", ["argus"],
                  models={"argus": "m"}, roles={"argus": "research"},
                  review_verdict="approve")
    before = _fs_snapshot(config.MEMORY_DIR)
    rep = coupling.predictability_report(days=30)
    assert rep["status"] == "ok"
    after = _fs_snapshot(config.MEMORY_DIR)
    assert before == after, "predictability_report wrote into MEMORY_DIR"


def test_no_prefetch_consumer_exists():
    """A11: no OLYMPUS_PREFETCH consumer anywhere in the package source."""
    pkg = Path(coupling.__file__).parent
    offenders = [p.name for p in pkg.glob("*.py")
                 if "OLYMPUS_PREFETCH" in p.read_text(encoding="utf-8")]
    assert offenders == []


# --- 5. CLI flag on the existing routing-stats command ---------------------------------

def test_cli_routing_stats_predictability_smoke(capsys):
    from olympus import cli
    _write_runs(_conditional_corpus(n=300, follow_rate=0.8))
    rc = cli.main(["routing-stats", "--predictability"])
    out = capsys.readouterr().out
    assert (rc or 0) == 0
    assert "Predictability report" in out
    assert "P2_prev_specialist" in out
    assert "FLOORS" in out


def test_cli_predictability_out_writes_json(tmp_path, capsys):
    from olympus import cli
    _write_runs(_conditional_corpus(n=300, follow_rate=0.8))
    dest = tmp_path / "report.json"
    rc = cli.main(["routing-stats", "--predictability", "--days", "7",
                   "--out", str(dest)])
    assert (rc or 0) == 0
    rep = json.loads(dest.read_text())
    assert rep["v"] == 1 and rep["n_runs"] == 300 and rep["days"] == 7
    assert "floors" in rep


def test_cli_routing_stats_without_flag_unchanged(capsys):
    from olympus import cli
    rc = cli.main(["routing-stats"])
    out = capsys.readouterr().out
    assert (rc or 0) == 0
    assert "Routing-outcome telemetry" in out    # the existing view intact
