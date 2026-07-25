"""Calibration Record — controlled production-trial harness.

**Read-only over the record.** This module computes the trial CHECKPOINT REPORTS
(A/100, B/250, C/500) and captures the trial CONFIGURATION. It records nothing
into Olympus's decision path — it only reads `calibration`'s durable record,
counters, and integrity, then folds them into the metrics the trial protocol
specifies. It is not a queue, scheduler, or event bus.

The trial itself runs over LIVE usage: an operator enables collection
(`OLYMPUS_CALIBRATION=1`) on one instance and real runs accumulate. These
functions turn that accumulating record into the checkpoint reports and the
bias analysis (selection/survivorship) the protocol requires — they cannot and
do not manufacture runs.
"""

from __future__ import annotations

import datetime
import json
import os
import platform
from pathlib import Path

from . import calibration, config, witness


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat()


def _commit() -> str:
    """Best-effort current commit (read from .git, no subprocess needed)."""
    try:
        head = Path(".git/HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            return Path(".git", ref).read_text(encoding="utf-8").strip()[:12]
        return head[:12]
    except OSError:
        return "unknown"


def _configured_models() -> list[str]:
    """Enabled provider/model configurations, from the pool — never secrets."""
    try:
        pool = config.ModelPool.from_env()
        out, seen = [], set()
        for m in pool.members:
            k = calibration.model_key(m.provider, m.model)
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out
    except Exception:
        return []


def trial_config(owner: str = "") -> dict:
    """The trial manifest — everything needed to interpret the record later.
    Reversible-by-config: the trial is turned off with OLYMPUS_CALIBRATION=0, no
    code rollback. No secrets are captured (models, not keys)."""
    st = calibration.status()
    topology = ("heartbeat+web (multi-process)"
                if os.environ.get("OLYMPUS_HEARTBEAT") else "single-process")
    return {
        "trial": "calibration-record/1",
        "activated_at": _now_iso(),
        "commit": _commit(),
        "schema": st["schema"],
        "taxonomy_version": st["taxonomy_version"],
        "providers_models": _configured_models(),
        "collection_dir": str(Path(config.MEMORY_DIR)),
        "record_path": st["path"],
        "retention_days": st["retention_days"],
        "export_allowed": st["export_allowed"],
        "signing_available": witness.available(),
        "process_topology": topology,
        "platform": platform.system(),
        "reversible_via": "OLYMPUS_CALIBRATION=0 (no code rollback)",
        "trial_owner": owner or os.environ.get("OLYMPUS_TRIAL_OWNER", "unset"),
    }


# --- checkpoint gates (pause-for-investigation conditions) -------------------

def integrity_gate() -> dict:
    """The always-on safety gate. The trial must PAUSE if any of these trip.
    Returns {ok, pause_reasons}."""
    v = calibration.verify()
    cnt = calibration.counters()
    pause = []
    if calibration.V_CORRUPTED_CHAIN in v["states"]:
        pause.append("middle-chain corruption detected")
    if not v["ok"]:
        pause.append("chain does not verify")
    if cnt["failure_rate"] > 0.01:
        pause.append(f"recording failure rate {cnt['failure_rate']:.2%} exceeds 1%")
    # A silent-loss check: persisted must equal the count the chain verified.
    if v["entries"] != cnt["persisted_records"]:
        pause.append("persisted count disagrees with verified entries "
                     "(possible silent loss)")
    return {"ok": not pause, "pause_reasons": pause}


def _by(field: str, kind: str = calibration.OBSERVATION) -> dict:
    """Count records by a body field (provider/model/domain/etc.)."""
    out: dict[str, int] = {}
    for e in calibration.entries(kind):
        v = str((e.get("body") or {}).get(field, "") or "—")
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _by_evidence_level() -> dict:
    counts = {calibration.EVIDENCE_LEVELS[k]: 0 for k in calibration.EVIDENCE_LEVELS}
    counts[calibration.EVIDENCE_LEVELS[calibration.EV_COMPLETION]] = \
        len(calibration.entries(calibration.OBSERVATION))
    for e in calibration.entries(calibration.FEEDBACK):
        lvl = (e.get("body") or {}).get("evidence_level")
        name = calibration.EVIDENCE_LEVELS.get(lvl)
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def checkpoint_a() -> dict:
    """Checkpoint A (100 runs): INSTRUMENTATION QUALITY only — no model ranking."""
    h = calibration.health()
    cnt = calibration.counters()
    v = calibration.verify()
    obs = calibration.entries(calibration.OBSERVATION)
    routed = [e for e in obs
              if (e.get("body") or {}).get("domain_source") == calibration.SRC_SPECIALIST]
    classified = [e for e in routed
                  if (e.get("body") or {}).get("domain") not in
                  (calibration.UNCLASSIFIED,)]
    return {
        "checkpoint": "A", "target_runs": 100, "generated_at": _now_iso(),
        "run_attempts_session": cnt["session"]["attempted"],
        "persisted_observations": len(obs),
        "feedback_events": len(calibration.entries(calibration.FEEDBACK)),
        "recording_failures": cnt["durable_failures"],
        "recording_failure_rate": cnt["failure_rate"],
        "lock_timeouts": cnt["session"]["dropped_timeout"],
        "chain_integrity": {"ok": v["ok"], "states": v["states"]},
        "incomplete_or_corrupt": {
            "incomplete_trailing_write": v["incomplete_trailing_write"],
            "corrupted": calibration.V_CORRUPTED_CHAIN in v["states"]},
        "signed": v["signed"], "unsigned": v["unsigned"],
        "domain_classification_coverage_routed": (
            round(len(classified) / len(routed), 4) if routed else None),
        "unclassified_rate": h["unclassified_rate"],
        "feedback_coverage": h["feedback_coverage"],
        "records_by_provider_model": _by("model_key"),
        "records_by_domain": _by("domain"),
        "records_by_evidence_level": _by_evidence_level(),
        "recording_overhead": calibration.overhead_percentiles(),
        "gate": integrity_gate(),
        "note": ("Instrumentation quality only. No model comparison at A unless "
                 "the record's own policy independently finds comparable evidence."),
    }


def _bias_scan() -> dict:
    """Selection/survivorship bias inputs: are drops/unclassified/missing-feedback
    associated with particular specialists/domains/providers? Reads what the
    record and the durable failure log actually contain — no inference."""
    obs = calibration.entries(calibration.OBSERVATION)
    unclassified = [e for e in obs
                    if (e.get("body") or {}).get("domain") == calibration.UNCLASSIFIED]
    fb_runs = {str((e.get("body") or {}).get("ref_run_id", ""))
               for e in calibration.entries(calibration.FEEDBACK)}
    no_feedback = [e for e in obs
                   if str((e.get("body") or {}).get("run_id", "")) not in fb_runs]

    def _dist(rows, field):
        out: dict[str, int] = {}
        for e in rows:
            k = str((e.get("body") or {}).get(field, "") or "—")
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    fails = calibration.record_failures()
    fail_by_hour: dict[str, int] = {}
    for f in fails:
        hr = str(f.get("at", ""))[:13]
        fail_by_hour[hr] = fail_by_hour.get(hr, 0) + 1
    return {
        "unclassified_by_provider": _dist(unclassified, "model_key"),
        "no_feedback_by_domain": _dist(no_feedback, "domain"),
        "no_feedback_by_specialist": _dist(no_feedback, "specialist"),
        "failures_total": len(fails),
        "failures_by_hour": fail_by_hour,
        "caveat": ("Association is descriptive, not causal. A concentration here "
                   "flags where selection bias could live; it does not prove it."),
    }


def checkpoint_b() -> dict:
    """Checkpoint B (250 runs): EVIDENCE QUALITY + bias scan. Signals kept
    separate — never one score."""
    rep = calibration.report()
    cells = rep["cells"]
    comparable = [c for c in cells if not c["insufficient_evidence"]]
    mixed = [c for c in cells if c.get("mixed_config")]
    useful = len(comparable) / len(cells) if cells else 0.0
    # approval↔verified correlation, per run, only where both exist.
    fb = calibration.entries(calibration.FEEDBACK)
    by_run: dict[str, dict] = {}
    for e in fb:
        b = e.get("body", {})
        by_run.setdefault(str(b.get("ref_run_id", "")), {})[
            b.get("outcome")] = b.get("evidence_level")
    both = [(calibration.APPROVED in v or calibration.APPROVED_AFTER_EDIT in v,
             calibration.VERIFIED in v)
            for v in by_run.values()
            if (calibration.APPROVED in v or calibration.APPROVED_AFTER_EDIT in v
                or calibration.REJECTED in v) and calibration.VERIFIED in v]
    agree = sum(1 for a, ve in both if a == ve)
    return {
        "checkpoint": "B", "target_runs": 250, "generated_at": _now_iso(),
        "rates_note": "completion, approval, rejection, edit, retry, verified are "
                      "SEPARATE — never collapsed",
        "cells": cells,
        "unclassified_domain_rate": rep["unclassified_domain_rate"],
        "missing_feedback_rate": rep["missing_feedback_rate"],
        "comparable_cells": len(comparable),
        "total_cells": len(cells),
        "pct_records_useful_for_comparison": round(useful, 4),
        "model_version_fragmentation": {
            "cells_with_mixed_config": len(mixed),
            "mixed_model_keys": sorted({c["model_key"] for c in mixed})},
        "approval_vs_verified": {
            "paired_runs": len(both), "agreement": agree,
            "note": ("too few paired runs to correlate" if len(both) < 5
                     else f"{agree}/{len(both)} agree")},
        "bias_scan": _bias_scan(),
        "gate": integrity_gate(),
    }


def checkpoint_c() -> dict:
    """Checkpoint C (500 runs): the STRATEGIC HYPOTHESIS. Per-cell evidence with
    uncertainty, plus the ranking verdict (which must still refuse when evidence
    is thin/incomparable/mixed/cross-domain/overlapping)."""
    rep = calibration.report()
    freshness = rep["freshness"]
    per_domain = {}
    for c in rep["cells"]:
        per_domain.setdefault(c["domain"], []).append(c)
    rankings = {d: calibration.rank_models(domain=d) for d in per_domain}
    return {
        "checkpoint": "C", "target_runs": 500, "generated_at": _now_iso(),
        "cells": rep["cells"],
        "comparison_summary": rep["comparison_summary"],
        "evidence_time_span": {"newest_at": freshness["newest_at"],
                               "oldest_at": (calibration._read_raw()[0].get("at")
                                             if calibration._read_raw() else None)},
        "rankings_by_domain": rankings,
        "any_ranking_produced": any(r.get("ranked") for r in rankings.values()),
        "gate": integrity_gate(),
        "note": ("A produced ranking is necessary but NOT sufficient for a moat "
                 "claim: the distinction must be operationally useful and the "
                 "advantage must compound over time."),
    }


def full_report(owner: str = "") -> dict:
    """Everything a checkpoint run needs, in one machine-readable object."""
    return {
        "config": trial_config(owner),
        "checkpoint_a": checkpoint_a(),
        "checkpoint_b": checkpoint_b(),
        "checkpoint_c": checkpoint_c(),
    }


def render(owner: str = "") -> str:
    """Human-facing checkpoint summary (safe: no prompt/output text)."""
    if not calibration.enabled():
        return ("Calibration collection is OFF — the trial is not running. "
                "Enable with OLYMPUS_CALIBRATION=1 on exactly one instance.")
    a = checkpoint_a()
    n = a["persisted_observations"]
    lines = [f"Calibration trial — {n} observation(s) recorded",
             f"  recording failure rate : {a['recording_failure_rate']:.3%} "
             f"({a['recording_failures']} durable)",
             f"  chain                  : "
             f"{'VALID' if a['chain_integrity']['ok'] else 'PROBLEM'} "
             f"{a['chain_integrity']['states']}",
             f"  domain coverage (routed): "
             f"{a['domain_classification_coverage_routed']}",
             f"  feedback coverage      : {a['feedback_coverage']:.0%}",
             f"  overhead median/p95 ms : {a['recording_overhead']['median_ms']}"
             f" / {a['recording_overhead']['p95_ms']}"]
    g = a["gate"]
    if not g["ok"]:
        lines.append("  ⏸ PAUSE FOR INVESTIGATION: " + "; ".join(g["pause_reasons"]))
    which = ("C/500" if n >= 500 else "B/250" if n >= 250
             else "A/100" if n >= 100 else "pre-A")
    lines.append(f"  checkpoint reached     : {which} "
                 f"({'ready' if n >= 100 else f'{n}/100 to first checkpoint'})")
    return "\n".join(lines)
