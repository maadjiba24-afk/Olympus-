"""Phase 0 gate harness — does the code graph earn its place?

Runs repo-navigation tasks through the full Olympus pipeline twice — graph OFF
(today's behavior: the agent greps / reads source files one by one) and graph ON
— and compares the metrics the gate is decided on:

  - read_source_file calls : the file-by-file grind the graph is meant to remove
  - aggregate tokens       : in+out across the whole task (from the usage ledger)
  - model calls            : a proxy for iterations-to-done
  - hallucination flags    : Aletheia's [unverified] / [low confidence] markers

The gate passes if ANY of: read_source_file calls down >=50%, hallucination
flags down, model calls down, or aggregate tokens down >=25%. This module only
MEASURES — it prints the table and which arms cleared threshold; a human reads
it and decides go / no-go.

It runs real agent turns, so it needs a working model and is NOT part of the
unit suite. tests/test_codegraph_gate.py exercises the measurement plumbing with
a stub brain instead. Run for real against Olympus's own repo:

    OLYMPUS_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-... python -m olympus.codegraph_gate --root .
"""

from __future__ import annotations

import argparse
import contextlib
import json
import time
from pathlib import Path

from . import codegraph, codegraph_ast, config, tools

# Questions that require navigating the EXISTING codebase (not writing new
# isolated functions — code_eval already covers that). These are where a code
# graph should help and a grep loop should hurt.
DEFAULT_TASKS = [
    "In the Olympus codebase, what calls resolve_handler, and where is it defined?",
    "How does a user message reach Aletheia's verification step? Trace the path.",
    "If I change add_edge in relgraph, what else would break?",
    "Which modules import store, and what is store responsible for?",
    "Is there any dead code: a function defined but never called anywhere?",
]

_GATE = {"read_source_file_drop": 0.50, "tokens_drop": 0.25}


def _usage_totals() -> tuple[int, int]:
    """(model calls, total tokens) recorded across the instance today."""
    day = time.strftime("%Y-%m-%d")
    path = config.MEMORY_DIR / "usage" / f"{day}.json"
    if not path.exists():
        return (0, 0)
    try:
        allrow = json.loads(path.read_text(encoding="utf-8")).get("__all__", {})
    except (json.JSONDecodeError, OSError):
        return (0, 0)
    return (int(allrow.get("calls", 0)),
            int(allrow.get("in", 0)) + int(allrow.get("out", 0)))


def _count_flags(text: str) -> int:
    t = text or ""
    return t.count("[unverified]") + t.count("[low confidence")


@contextlib.contextmanager
def _instrument_read_source():
    """Count read_source_file dispatches for the duration of one task."""
    counter = {"n": 0}
    original = tools.HANDLERS.get("read_source_file")

    def _wrapped(*a, **k):
        counter["n"] += 1
        return original(*a, **k) if original else ""

    tools.HANDLERS["read_source_file"] = _wrapped
    try:
        yield counter
    finally:
        if original is not None:
            tools.HANDLERS["read_source_file"] = original


def _default_ask(task: str) -> str:
    from . import orchestrator        # local import: heavy peer
    return orchestrator.Olympus(user="gate").ask(task)


def run_task(task: str, *, graph_on: bool, ask=None) -> dict:
    """Run one task with the graph on or off; return its metrics. `ask` is
    injectable (a callable(task)->str) so the plumbing can be tested without a
    model."""
    codegraph.set_enabled(graph_on)     # off -> no context block, tools hidden
    ask = ask or _default_ask
    with _instrument_read_source() as counter:
        before = _usage_totals()
        answer = ask(task)
        after = _usage_totals()
    return {"read_source_file": counter["n"],
            "tokens": after[1] - before[1],
            "calls": after[0] - before[0],
            "flags": _count_flags(answer)}


def _pct_drop(off: float, on: float) -> float:
    return (off - on) / off if off else 0.0


def run_gate(root: str = ".", tasks: list[str] | None = None, ask=None) -> dict:
    """Build the graph, run every task off-vs-on, print the comparison, and
    return the aggregate result + whether each gate arm cleared threshold."""
    tasks = tasks or DEFAULT_TASKS
    build_stats = codegraph_ast.build("self", root=root)

    metrics = ("read_source_file", "tokens", "calls", "flags")
    agg = {m: {"off": 0, "on": 0} for m in metrics}
    rows = []
    for task in tasks:
        off = run_task(task, graph_on=False, ask=ask)
        on = run_task(task, graph_on=True, ask=ask)
        rows.append((task, off, on))
        for m in metrics:
            agg[m]["off"] += off[m]
            agg[m]["on"] += on[m]
    codegraph.set_enabled(True)         # restore default after the A/B run

    arms = {
        "read_source_file -50%":
            _pct_drop(agg["read_source_file"]["off"],
                      agg["read_source_file"]["on"]) >= _GATE["read_source_file_drop"],
        "tokens -25%":
            _pct_drop(agg["tokens"]["off"], agg["tokens"]["on"]) >= _GATE["tokens_drop"],
        "fewer model calls":
            agg["calls"]["on"] < agg["calls"]["off"],
        "fewer hallucination flags":
            agg["flags"]["on"] < agg["flags"]["off"],
    }
    passed = any(arms.values())

    _print_report(build_stats, agg, arms, passed)
    return {"build": build_stats, "aggregate": agg, "arms": arms,
            "passed": passed, "rows": rows}


def _print_report(build_stats, agg, arms, passed) -> None:
    print("\n=== Phase 0 gate — code graph off vs on ===")
    print(f"graph: {build_stats}\n")
    print(f"{'metric':<20}{'OFF':>10}{'ON':>10}{'delta':>12}")
    for m, label in (("read_source_file", "read_source_file"),
                     ("tokens", "tokens"), ("calls", "model calls"),
                     ("flags", "halluc. flags")):
        off, on = agg[m]["off"], agg[m]["on"]
        delta = f"{(_pct_drop(off, on) * 100):+.0f}%" if off else "n/a"
        print(f"{label:<20}{off:>10}{on:>10}{delta:>12}")
    print("\ngate arms (any one passing = proceed to Phase 1):")
    for name, ok in arms.items():
        print(f"  [{'PASS' if ok else '  - '}] {name}")
    print(f"\nVERDICT: {'PROCEED to Phase 1' if passed else 'STOP — write GATE-FAILED.md'}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 0 code-graph gate")
    ap.add_argument("--root", default=".", help="repo root to graph")
    ap.add_argument("--tasks-file", help="JSON list of task strings (optional)")
    args = ap.parse_args()
    tasks = None
    if args.tasks_file:
        tasks = json.loads(Path(args.tasks_file).read_text(encoding="utf-8"))
    run_gate(root=args.root, tasks=tasks)


if __name__ == "__main__":
    main()
