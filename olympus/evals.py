"""Benchmark harness — how Olympus knows it's getting smarter, not just older.

Each benchmark item sends a fixed task to a specialist's *prompt* (no tools,
so the score isolates prompt quality) and an LLM judge scores the answer
against explicit criteria. Prometheus runs this before and after prompt
upgrades; if the score drops, he rolls the prompt back.
"""

from __future__ import annotations

import json
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
               "chronos", "argus", "mnemosyne")


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


def per_specialist_scores(settings: config.Settings | None = None) -> dict[str, float]:
    """Run the benchmark and return the average score per specialist."""
    settings = settings or config.Settings.from_env()
    result = run(settings)
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


def run(settings: config.Settings | None = None,
        only: list[str] | None = None) -> dict:
    """Run the benchmark; returns {avg, items: [{id, score, justification}]}."""
    settings = settings or config.Settings.from_env()
    judge = _judge_settings(settings)
    items = []
    for bench in load_benchmarks():
        if only and bench["id"] not in only:
            continue
        system = (agent.load_prompt(bench["specialist"])
                  + "\n\n## Skill library\n" + skills.index()
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
        items.append({"id": bench["id"],
                      "score": max(1, min(10, int(verdict["score"]))),
                      "justification": verdict["justification"]})
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


def latest_score() -> float | None:
    """Most recent saved benchmark average, if any."""
    files = sorted((config.MEMORY_DIR / "evals").glob("*.md"), reverse=True) \
        if (config.MEMORY_DIR / "evals").exists() else []
    for path in files:
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        if "avg" in first:
            try:
                return float(first.split("avg", 1)[1].split()[0])
            except (ValueError, IndexError):
                continue
    return None
