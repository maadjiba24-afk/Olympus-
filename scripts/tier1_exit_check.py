#!/usr/bin/env python3
"""Tier-1 exit gate — prove Olympus is ready for Tier 2 of the moat plan.

The plan gates Tier 2 on:

  > build only after Tier 1 is merged AND `olympus ask "<real multi-step task>"`
  > completes unattended, producing a replayable log, on 3 consecutive distinct
  > prompts.

This runs exactly that check. For each prompt it:
  1. runs the full pipeline live (`bot.ask`) — recording the decision log and
     freezing every LLM response (Task 1's replay store);
  2. re-executes the recorded run against those frozen responses
     (`orchestrator.replay_run`) and asserts the decision path replays with
     **zero divergence and zero new API calls**.

A prompt passes only if it completed unattended (a real answer, no exception,
at least one recorded decision) AND replayed byte-identically. The gate passes
only if every prompt passes.

The record pass makes real model calls, so a provider key must be configured
(`olympus setup`). The replay pass makes none. Run it where the key lives — e.g.
the deployed instance:

    python scripts/tier1_exit_check.py
    python scripts/tier1_exit_check.py "prompt one" "prompt two" "prompt three"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from olympus import config, orchestrator, replaystore, trace  # noqa: E402

# Three distinct, genuinely multi-step tasks that exercise different specialists
# and the full route -> plan -> dispatch -> review path.
DEFAULT_PROMPTS = [
    "Research the current state of small modular nuclear reactors and give me a "
    "five-bullet investment brief, ending with the two biggest risks.",
    "Write a Python function that parses ISO-8601 durations into seconds, then "
    "review it for edge cases and list the unit tests it still needs.",
    "Draft a go-to-market plan for a B2B SaaS scheduling tool: positioning, "
    "three acquisition channels, and a 30-day launch checklist.",
]


def check_one(prompt: str, make_bot, report) -> dict:
    """Run one prompt live, then replay it. Returns a result record."""
    rec: dict = {"prompt": prompt, "completed": False, "replayable": False,
                 "decisions": 0, "run_id": None, "detail": ""}
    bot = make_bot()
    try:
        reply = bot.ask(prompt)
    except Exception as err:                       # unattended means: no human
        rec["detail"] = f"ask() raised: {err!r}"
        return rec
    rec["completed"] = bool(reply) and not reply.startswith(
        ("Configuration problem", "Daily budget"))
    rec["run_id"] = getattr(bot, "last_run_id", None)
    if not rec["completed"]:
        rec["detail"] = f"did not complete: {reply[:120]}"
        return rec

    run = trace.load_run(rec["run_id"]) if rec["run_id"] else None
    rec["decisions"] = len(run.get("decisions", [])) if run else 0
    if rec["decisions"] == 0:
        rec["detail"] = "completed but recorded no decisions"
        return rec

    try:
        _original, _fresh, diffs = orchestrator.replay_run(rec["run_id"])
    except replaystore.ReplayDivergence as err:
        rec["detail"] = f"replay diverged: {err}"
        return rec
    except Exception as err:
        rec["detail"] = f"replay failed: {err!r}"
        return rec

    rec["replayable"] = (diffs == [])
    rec["detail"] = ("replayed byte-identically" if rec["replayable"]
                     else f"{len(diffs)} divergent decision(s)")
    return rec


def run_exit_check(prompts=None, *, make_bot=None, report=print) -> tuple[bool, list[dict]]:
    prompts = prompts or DEFAULT_PROMPTS
    make_bot = make_bot or (
        lambda: orchestrator.Olympus(pool=config.ModelPool.from_env()))
    results = []
    for i, prompt in enumerate(prompts, 1):
        report(f"\n[{i}/{len(prompts)}] {prompt[:70]}...")
        rec = check_one(prompt, make_bot, report)
        ok = rec["completed"] and rec["replayable"] and rec["decisions"] > 0
        report(f"    completed={rec['completed']} decisions={rec['decisions']} "
               f"replayable={rec['replayable']} — {rec['detail']}")
        report(f"    {'PASS' if ok else 'FAIL'}  (run {rec['run_id']})")
        results.append(rec)
    all_pass = len(results) >= 3 and all(
        r["completed"] and r["replayable"] and r["decisions"] > 0 for r in results)
    return all_pass, results


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    from olympus import firstrun
    firstrun.load_env_file()
    prompts = argv or None
    all_pass, results = run_exit_check(prompts)
    passed = sum(1 for r in results
                 if r["completed"] and r["replayable"] and r["decisions"] > 0)
    print(f"\n{'='*60}")
    print(f"Tier-1 exit gate: {passed}/{len(results)} prompts passed.")
    if all_pass:
        print("✓ GATE MET — runs complete unattended and replay byte-identically. "
              "Cleared for Tier 2.")
        return 0
    print("✗ GATE NOT MET — see the failing prompt(s) above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
