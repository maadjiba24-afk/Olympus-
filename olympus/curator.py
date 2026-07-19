"""Skill-library curation — the retirement half of self-improvement.

The benchmark gate (orchestrator.gate_skills) governs skills on the way IN;
nothing has ever retired them. Left alone, a self-building library only
grows: stale how-tos linger, near-duplicates accumulate, and every one of
them costs prompt budget in each specialist's system prompt forever.

On a cadence (OLYMPUS_CURATION_EVERY, default 7 days) the curator:

1. **Grades** the library with a scoped, tool-free model call — each skill
   is judged keep / prune / consolidate, conservatively (pruning needs a
   stated reason: obsolete, duplicative, or trivially generic).
2. **Prunes with the same regression gate that admits skills** — a prune
   candidate is hidden and the affected benchmark re-run; it is archived
   (recoverably, to skill_backups/) ONLY if the score does not drop, and
   kept otherwise. The gate that proves a skill helps is the gate that
   proves removing it doesn't hurt.
3. **Recommends consolidations to Metis** by saving them as lessons — the
   daily learning cycle (which owns skill synthesis) merges them with full
   context rather than the curator duplicating that role.

A per-run report is saved to memory/reports.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from . import config, memory, skills

MAX_PRUNE_PER_RUN = 3        # blast-radius cap: curation is gradual
MIN_LIBRARY = 4              # below this there is nothing worth curating

GRADE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "verdict": {"type": "string",
                                "enum": ["keep", "prune", "consolidate"]},
                    "target": {"type": "string",
                               "description": "for consolidate: the skill to "
                                              "merge this one into"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "verdict", "reason"],
            },
        },
    },
    "required": ["verdicts"],
}


def curation_every() -> int:
    try:
        return int(os.environ.get("OLYMPUS_CURATION_EVERY", str(7 * 86400)))
    except ValueError:
        return 7 * 86400


def _grade(library: list[dict],
           judge_fn: Callable[..., dict] | None = None) -> list[dict]:
    """One scoped model call grading the whole library. Tool-free by
    construction (a pure completion) — the reviewer can't reach shell/web."""
    if judge_fn is None:
        from . import backend
        pool = config.ModelPool.from_env()

        def judge_fn(system, messages, schema):
            return backend.complete_json(pool.for_role("reasoning"), system,
                                         messages, schema, effort="medium")
    catalog = "\n".join(
        f"- name: {s['name']} | specialist: {s['specialist'] or 'all'} | "
        f"updated: {s['updated'] or 'unknown'} | {s['description'][:150]}"
        for s in library)
    system = (
        "You curate an AI agent's self-built skill library. Grade each skill "
        "keep / prune / consolidate. Be CONSERVATIVE: prune only what is "
        "clearly obsolete (references dead dates/tools), duplicative of "
        "another listed skill, or so generic it adds nothing an LLM doesn't "
        "already know. Mark near-duplicates as consolidate with a target. "
        "When in doubt: keep.")
    messages = [{"role": "user", "content":
                 f"The skill library ({len(library)} skills):\n{catalog}\n\n"
                 "Grade every skill."}]
    verdicts = judge_fn(system, messages, GRADE_SCHEMA).get("verdicts", [])
    known = {s["name"] for s in library}
    return [v for v in verdicts if v.get("name") in known]


def _default_eval(settings: config.Settings):
    from . import evals

    def eval_fn(bench_ids: list[str] | None) -> float:
        return evals.run(settings, only=bench_ids)["avg"]
    return eval_fn


def curate(settings: config.Settings | None = None,
           judge_fn: Callable[..., dict] | None = None,
           eval_fn: Callable[[list[str] | None], float] | None = None) -> str:
    """One curation pass. Returns the human-readable report (also saved)."""
    settings = settings or config.Settings.from_env()
    library = [s for s in skills.list_all() if not s["provisional"]]
    if len(library) < MIN_LIBRARY:
        return (f"Curator: library has {len(library)} proven skills "
                f"(< {MIN_LIBRARY}) — nothing to curate yet.")

    try:
        verdicts = _grade(library, judge_fn=judge_fn)
    except Exception as err:
        return f"Curator: grading failed ({err}); library untouched."

    # Self-tuned caution: if prunes keep getting reverted by the benchmark
    # gate (a sign the grader is over-eager), the reviewer lowers this cap.
    prune_cap = MAX_PRUNE_PER_RUN
    try:                                # the tuner only NARROWS within the cap
        from . import evolve
        prune_cap = min(prune_cap, int(evolve.current("curator", "prune_per_run")))
    except Exception:
        pass
    prunes = [v for v in verdicts if v["verdict"] == "prune"][:max(1, prune_cap)]
    consolidations = [v for v in verdicts if v["verdict"] == "consolidate"]
    by_name = {s["name"]: s for s in library}

    pruned, kept = [], []
    if prunes:
        from . import evals
        eval_fn = eval_fn or _default_eval(settings)
        for v in prunes:
            sp = by_name[v["name"]]["specialist"]
            scoped = bool(sp) and sp != skills.GLOBAL_SPECIALIST
            bench_ids = evals.ids_for([sp]) if scoped else None
            if scoped and not bench_ids:
                kept.append(f"{v['name']} (no benchmark coverage to gate the "
                            "prune — kept)")
                continue
            # Regression-gate the REMOVAL: archive only if hiding the skill
            # does not lower the affected score.
            try:
                before = eval_fn(bench_ids)            # skill visible
                skills.set_hidden(v["name"], True)
                after = eval_fn(bench_ids)             # skill hidden
                skills.set_hidden(v["name"], False)
            except Exception as err:
                skills.set_hidden(v["name"], False)    # never leave it hidden
                kept.append(f"{v['name']} (gate failed: {err})")
                continue
            if after >= before:
                pruned.append(f"{v['name']} [{before}→{after}]: "
                              + skills.archive(v["name"], v["reason"]))
            else:
                kept.append(f"{v['name']} [{before}→{after}] — pruning would "
                            "regress the benchmark; kept")

    if consolidations:
        memory.save(
            "lessons", "Curator: consolidation recommendations",
            "The skill curator flagged near-duplicate skills. On the next "
            "learning cycle, merge each pair into ONE skill (update the "
            "target, then the duplicate will be pruned once redundant):\n"
            + "\n".join(f"- merge '{v['name']}' into '{v.get('target') or '?'}'"
                        f" — {v['reason']}" for v in consolidations))

    parts = [f"graded {len(library)}"]
    if pruned:
        parts.append(f"pruned {len(pruned)}: " + "; ".join(pruned))
    if kept:
        parts.append(f"kept after gate {len(kept)}: " + "; ".join(kept))
    if consolidations:
        parts.append(f"{len(consolidations)} consolidation(s) queued for Metis")
    report = "Curator — " + " · ".join(parts)
    memory.save("reports", "skill curation", report)
    # Telemetry: a prune the gate reverted ('kept … regress') is a degraded
    # run (the grader wanted to remove a skill that actually helps); this
    # tightens prune_per_run over time.
    try:
        from . import evolve
        reverted = any("regress" in k for k in kept)
        evolve.record("curator", evolve.DEGRADED if reverted else evolve.OK)
    except Exception:
        pass
    return report
