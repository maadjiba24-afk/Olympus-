"""Benchmark harness — how Olympus knows it's getting smarter, not just older.

Each benchmark item sends a fixed task to a specialist's *prompt* (no tools,
so the score isolates prompt quality) and an LLM judge scores the answer
against explicit criteria. Prometheus runs this before and after prompt
upgrades; if the score drops, he rolls the prompt back.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import agent, backend, config, memory, skills

JUDGE_SYSTEM = (
    "You are a strict evaluation judge. Score the assistant answer against "
    "the given criteria. Be harsh: 9-10 means every criterion is fully met "
    "with excellent execution; 5 means roughly half met; 1-2 means the answer "
    "misses the point. Judge only against the criteria, not your own taste."
)

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        # Structured outputs don't support numeric min/max on integers, so the
        # range is stated in the description and clamped after parsing.
        "score": {"type": "integer",
                  "description": "Quality score from 1 (poor) to 10 (excellent)"},
        "justification": {"type": "string"},
    },
    "required": ["score", "justification"],
    "additionalProperties": False,
}


def load_benchmarks() -> list[dict]:
    """Built-in benchmark items plus any Olympus auto-generated for new domains."""
    path = Path(__file__).resolve().parent / "benchmarks.json"
    items = json.loads(path.read_text(encoding="utf-8"))
    extra = config.MEMORY_DIR / "benchmarks_extra.json"
    if extra.exists():
        try:
            items = items + json.loads(extra.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return items


def ids_for(specialists) -> list[str]:
    keys = set(specialists)
    return [b["id"] for b in load_benchmarks() if b["specialist"] in keys]


# Specialists that answer user tasks (benchmarkable). Metis and Prometheus are
# internal — their quality is their effect on the system, not a single answer.
USER_FACING = ("plutus", "peitho", "hephaestus", "aegis", "iris", "chiron",
               "chronos", "argus", "mnemosyne", "angelos")


def coverage() -> dict[str, int]:
    """How many benchmark items each specialist has."""
    out = {s: 0 for s in USER_FACING}
    for b in load_benchmarks():
        out[b["specialist"]] = out.get(b["specialist"], 0) + 1
    return out


def ensure_coverage(min_items: int = 1,
                    settings: config.Settings | None = None) -> list[str]:
    """Generate benchmark items for any user-facing specialist below min_items.
    Returns the specialists for which an item was generated."""
    settings = settings or config.Settings.from_env()
    generated = []
    cov = coverage()
    for spec in USER_FACING:
        for _ in range(max(0, min_items - cov.get(spec, 0))):
            try:
                generate_item(spec, settings)
                generated.append(spec)
            except Exception:
                break
    return generated


def per_specialist_scores(settings: config.Settings | None = None,
                          only_specialists: list[str] | None = None
                          ) -> dict[str, float]:
    """Run the benchmark and return the average score per specialist.
    `only_specialists` restricts the run to those specialists' items — used by
    the gate's confirmation pass to cheaply re-score only flagged ones."""
    settings = settings or config.Settings.from_env()
    only = ids_for(only_specialists) if only_specialists else None
    result = run(settings, only=only)
    buckets: dict[str, list[int]] = {}
    bench_by_id = {b["id"]: b for b in load_benchmarks()}
    for item in result["items"]:
        spec = bench_by_id.get(item["id"], {}).get("specialist")
        if spec:
            buckets.setdefault(spec, []).append(item["score"])
    return {s: round(sum(v) / len(v), 2) for s, v in buckets.items()}


def add_item(item: dict) -> None:
    """Append an auto-generated benchmark item to the extensible eval set."""
    extra = config.MEMORY_DIR / "benchmarks_extra.json"
    extra.parent.mkdir(parents=True, exist_ok=True)
    items = []
    if extra.exists():
        try:
            items = json.loads(extra.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            items = []
    items = [i for i in items if i.get("id") != item.get("id")]
    items.append(item)
    extra.write_text(json.dumps(items, indent=2), encoding="utf-8")


_GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string",
                 "description": "A realistic, specific user request for this "
                 "specialist — concrete enough that quality is judgeable"},
        "criteria": {"type": "string",
                     "description": "Explicit, independently checkable success "
                     "criteria for a great answer"},
        "must_contain": {
            "type": "array", "items": {"type": "string"},
            "description": "0-3 short substrings a correct answer MUST include "
            "(e.g. a term/number the task objectively requires). Deterministic — "
            "leave empty if nothing is objectively required."},
        "must_not_contain": {
            "type": "array", "items": {"type": "string"},
            "description": "0-3 short substrings a correct answer must NOT include "
            "(e.g. a wrong/unsafe claim). Deterministic; leave empty if none."},
    },
    "required": ["task", "criteria"],
    "additionalProperties": False,
}


def generate_item(specialist: str, settings: config.Settings | None = None) -> str:
    """Create and save a new benchmark item for a specialist's domain.

    This is how a newly-strengthened or newly-covered domain earns its own
    objective eval, so future skill/prompt changes there can be measured."""
    from . import specialists as specs
    if specialist not in specs.SPECIALISTS:
        return f"Unknown specialist '{specialist}'."
    settings = settings or config.Settings.from_env()
    spec = specs.SPECIALISTS[specialist]
    judge = _judge_settings(settings)
    gen = backend.complete_json(
        judge,
        "You design evaluation cases for an AI specialist. Produce ONE "
        "realistic user task and explicit, checkable success criteria.",
        [{"role": "user", "content":
            f"Specialist: {spec.name} — {spec.title}\n{spec.description}\n\n"
            "Write a new benchmark case that probes whether this specialist "
            "gives an excellent, concrete, honest answer. Avoid duplicating "
            "obvious cases; pick a realistic but non-trivial scenario."}],
        _GEN_SCHEMA, effort="medium")
    item = {
        "id": f"{specialist}-gen-{int(time.time())}",
        "specialist": specialist,
        "task": gen["task"],
        "criteria": gen["criteria"],
    }
    # Attach any objective assertions the generator produced, so the item scores
    # partly judge-independently from the start. Empty lists → no `checks` key.
    checks = {}
    if gen.get("must_contain"):
        checks["contains"] = [s for s in gen["must_contain"] if s]
    if gen.get("must_not_contain"):
        checks["not_contains"] = [s for s in gen["must_not_contain"] if s]
    if any(checks.values()):
        item["checks"] = {k: v for k, v in checks.items() if v}
    add_item(item)
    return f"Generated benchmark item {item['id']} for {spec.name}."


def _judge_settings(settings: config.Settings) -> config.Settings:
    """A separate judge model so the scorer can't be gamed by the tuned model.
    Only swaps models on the Anthropic backend; other providers judge in-model.
    """
    if settings.provider == "anthropic" and config.JUDGE_MODEL \
            and config.JUDGE_MODEL != settings.model:
        return config.Settings(provider="anthropic", model=config.JUDGE_MODEL,
                               api_key=settings.api_key,
                               base_url=settings.base_url)
    return settings


def objective_score(answer: str, checks: dict | None) -> tuple[float, list[str]]:
    """Score an answer against DETERMINISTIC assertions — no LLM judge involved.

    `checks` may carry any of: `contains` / `not_contains` / `regex` (str or list
    of str), and `min_chars` / `max_chars` (int). Each individual assertion is one
    check. Returns (pass_rate in [0,1], list of human-readable failures). No
    checks (or a malformed block) → (1.0, []): a strict no-op, so an item without
    assertions scores exactly as the judge alone gave it.

    This is what makes the self-improvement gate partly JUDGE-INDEPENDENT: a skill
    or prompt that breaks a measurable property (drops a required caveat, starts
    emitting a forbidden phrase, blows a length bound) fails the gate regardless
    of what the LLM judge thought."""
    if not isinstance(checks, dict):
        return 1.0, []
    ans = answer or ""
    low = ans.lower()

    def _as_list(v):
        if v is None:
            return []
        return list(v) if isinstance(v, (list, tuple)) else [v]

    total, passed, failures = 0, 0, []
    for needle in _as_list(checks.get("contains")):
        total += 1
        if str(needle).lower() in low:
            passed += 1
        else:
            failures.append(f"missing required text: {str(needle)[:60]!r}")
    for needle in _as_list(checks.get("not_contains")):
        total += 1
        if str(needle).lower() not in low:
            passed += 1
        else:
            failures.append(f"contains forbidden text: {str(needle)[:60]!r}")
    for pat in _as_list(checks.get("regex")):
        total += 1
        try:
            ok = re.search(str(pat), ans, re.I | re.S) is not None
        except re.error:
            ok = False                       # a broken pattern fails closed
        if ok:
            passed += 1
        else:
            failures.append(f"regex did not match: {str(pat)[:60]!r}")
    for bound, cmp, label in (("min_chars", lambda n: len(ans) >= n, "too short"),
                              ("max_chars", lambda n: len(ans) <= n, "too long")):
        if checks.get(bound) is not None:
            total += 1
            try:
                ok = cmp(int(checks[bound]))
            except (TypeError, ValueError):
                ok = True                    # malformed bound → ignore, not fail
                total -= 1
            else:
                if ok:
                    passed += 1
                else:
                    failures.append(f"{label} ({len(ans)} chars vs {bound}="
                                    f"{checks[bound]})")
    if total == 0:
        return 1.0, []
    return passed / total, failures


def run(settings: config.Settings | None = None,
        only: list[str] | None = None) -> dict:
    """Run the benchmark; returns {avg, items: [{id, score, justification}]}."""
    settings = settings or config.Settings.from_env()
    judge = _judge_settings(settings)
    items = []
    for bench in load_benchmarks():
        if only and bench["id"] not in only:
            continue
        # Scope the index to this specialist — exactly what it sees in
        # production — so the gate measures a skill against the specialist that
        # actually uses it.
        system = (agent.load_prompt(bench["specialist"])
                  + "\n\n## Skill library\n" + skills.index(bench["specialist"])
                  + "\n\nNote: tools are unavailable in this evaluation — "
                    "answer directly from expertise.")
        answer = backend.complete_text(
            settings, system,
            [{"role": "user", "content": bench["task"]}], effort="medium",
        )
        verdict = backend.complete_json(
            judge, JUDGE_SYSTEM,
            [{"role": "user", "content":
                f"## Task given to the assistant\n{bench['task']}\n\n"
                f"## Criteria\n{bench['criteria']}\n\n"
                f"## Assistant answer\n{answer}"}],
            JUDGE_SCHEMA, effort="medium",
        )
        judge_score = max(1, min(10, int(verdict["score"])))
        # Objective assertions (if the item has any) proportionally cap the judge
        # score — deterministic, judge-independent. No `checks` → obj=1.0, so the
        # score is unchanged (backward compatible with every existing item).
        obj, obj_failures = objective_score(answer, bench.get("checks"))
        score = max(1, min(10, round(judge_score * obj)))
        item = {"id": bench["id"], "score": score,
                "justification": verdict["justification"]}
        if bench.get("checks"):
            item["objective"] = {"pass_rate": round(obj, 3),
                                 "judge_score": judge_score,
                                 "failures": obj_failures}
        items.append(item)
    avg = round(sum(i["score"] for i in items) / max(len(items), 1), 2)
    return {"avg": avg, "items": items}


def run_and_save(settings: config.Settings | None = None) -> str:
    """Run, persist to memory/evals, return a readable summary."""
    result = run(settings)
    lines = [f"Benchmark average: {result['avg']}/10  "
             f"({time.strftime('%Y-%m-%d %H:%M')})", ""]
    for item in result["items"]:
        lines.append(f"- {item['id']}: {item['score']}/10 — "
                     f"{item['justification'][:200]}")
    summary = "\n".join(lines)
    memory.save("evals", f"benchmark avg {result['avg']}", summary)
    return summary


# --- answer-quality regression gate (M5 / SECURITY_RESIDUALS §6) -----------
# The passing unit suite proves the guardrails; it does NOT score answer
# quality. This gate does: it runs the benchmark, compares per-specialist
# averages against a committed baseline, and FAILS when any specialist
# regresses by more than a tolerance. The comparison core below is PURE
# (no I/O, no model calls) so it is unit-tested without a key; the live run
# that produces `scores` needs a real ANTHROPIC_API_KEY (CI secret).

BASELINE_PATH = Path(__file__).resolve().parent / "quality_baseline.json"
DEFAULT_TOLERANCE = 1.0        # a specialist may drop up to 1.0/10 vs baseline


def load_baseline(path: Path | None = None) -> dict[str, float]:
    """Committed per-specialist baseline scores, or {} if none exists yet."""
    path = path or BASELINE_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    scores = raw.get("scores", raw) if isinstance(raw, dict) else {}
    return {str(k): float(v) for k, v in scores.items()
            if isinstance(v, (int, float))}


def load_baseline_meta(path: Path | None = None) -> dict:
    """The baseline's `_provenance` block (model, endpoint, date, ...), or {}.

    Scores are model-dependent, so the gate only ENFORCES against a baseline
    produced by the same model it is currently evaluating with — comparing a
    Claude run to a Kimi baseline would gate apples against oranges. The
    provenance records which model scored the baseline."""
    path = path or BASELINE_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    meta = raw.get("_provenance") if isinstance(raw, dict) else None
    return meta if isinstance(meta, dict) else {}


def regression_check(scores: dict[str, float], baseline: dict[str, float],
                     tolerance: float = DEFAULT_TOLERANCE) -> dict:
    """PURE regression comparison — the heart of the CI gate.

    Returns {"ok": bool, "regressions": [{specialist, baseline, score, drop}],
    "missing": [specialists in baseline with no fresh score], "checked": int}.
    A specialist FAILS the gate when its fresh score is more than `tolerance`
    below its baseline. Specialists absent from the baseline are new coverage,
    never a regression. A baseline specialist with no fresh score is reported
    as `missing` (the eval didn't cover it) and fails the gate — a silently
    dropped benchmark must not pass as green."""
    tol = max(0.0, float(tolerance))
    regressions, missing = [], []
    for spec, base in baseline.items():
        if spec not in scores:
            missing.append(spec)
            continue
        drop = round(float(base) - float(scores[spec]), 2)
        if drop > tol:
            regressions.append({"specialist": spec, "baseline": round(float(base), 2),
                                "score": round(float(scores[spec]), 2), "drop": drop})
    return {"ok": not regressions and not missing,
            "regressions": sorted(regressions, key=lambda r: -r["drop"]),
            "missing": sorted(missing), "checked": len(baseline)}


def confirm_regressions(first: dict, retry_scores: dict[str, float],
                        baseline: dict[str, float],
                        tolerance: float = DEFAULT_TOLERANCE) -> dict:
    """PURE second-opinion filter for the gate's confirmation pass.

    Single-run per-specialist averages carry judge noise of well over a point,
    so a first-pass regression may be variance, not a real drop. The gate
    re-scores ONLY the flagged specialists in an independent eval; a specialist
    stays failed only if the retry ALSO shows a drop beyond tolerance. Noise
    rarely strikes the same specialist twice; a real regression reproduces.

    Fail-closed edges: a flagged specialist absent from `retry_scores` stays
    failed (reported under `missing`), and first-pass `missing` coverage can
    never be cleared by a retry."""
    flagged = {r["specialist"] for r in first.get("regressions", [])}
    sub_base = {s: baseline[s] for s in flagged if s in baseline}
    second = regression_check(retry_scores, sub_base, tolerance)
    missing = sorted(set(first.get("missing", [])) | set(second["missing"]))
    return {"ok": second["ok"] and not missing,
            "regressions": second["regressions"],
            "missing": missing,
            "checked": first.get("checked", len(baseline))}


def format_gate_report(scores: dict[str, float], result: dict,
                       tolerance: float = DEFAULT_TOLERANCE) -> str:
    """Human-readable gate outcome for CI logs."""
    lines = [f"Answer-quality gate (tolerance {tolerance:.2f}/10):"]
    if not scores:
        lines.append("  no scores produced (benchmark did not run).")
    for spec in sorted(scores):
        lines.append(f"  {spec}: {scores[spec]}/10")
    for r in result.get("regressions", []):
        lines.append(f"  ✗ REGRESSION {r['specialist']}: "
                     f"{r['score']}/10 vs baseline {r['baseline']}/10 "
                     f"(−{r['drop']})")
    for spec in result.get("missing", []):
        lines.append(f"  ✗ MISSING {spec}: in baseline but not scored this run")
    lines.append("PASS" if result.get("ok") else "FAIL")
    return "\n".join(lines)


def latest_score() -> float | None:
    """Most recent saved benchmark average, if any."""
    files = sorted((config.MEMORY_DIR / "evals").glob("*.md"), reverse=True) \
        if (config.MEMORY_DIR / "evals").exists() else []
    for path in files:
        first = memory.note_title(
            path.read_text(encoding="utf-8", errors="replace"))
        if "avg" in first:
            try:
                return float(first.split("avg", 1)[1].split()[0])
            except (ValueError, IndexError):
                continue
    return None
