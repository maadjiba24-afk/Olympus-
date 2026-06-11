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
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "justification": {"type": "string"},
    },
    "required": ["score", "justification"],
    "additionalProperties": False,
}


def load_benchmarks() -> list[dict]:
    path = Path(__file__).resolve().parent / "benchmarks.json"
    return json.loads(path.read_text(encoding="utf-8"))


def run(settings: config.Settings | None = None,
        only: list[str] | None = None) -> dict:
    """Run the benchmark; returns {avg, items: [{id, score, justification}]}."""
    settings = settings or config.Settings.from_env()
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
            settings, JUDGE_SYSTEM,
            [{"role": "user", "content":
                f"## Task given to the assistant\n{bench['task']}\n\n"
                f"## Criteria\n{bench['criteria']}\n\n"
                f"## Assistant answer\n{answer}"}],
            JUDGE_SCHEMA, effort="medium",
        )
        items.append({"id": bench["id"], "score": int(verdict["score"]),
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
