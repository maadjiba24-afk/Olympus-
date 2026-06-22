"""The replay gate — prove runs complete unattended and replay byte-identically.

This is both the Tier-1 exit check (run on demand via `scripts/tier1_exit_check`)
and a self-correcting tripwire: the heartbeat runs `self_check()` on a cadence,
and if a live run no longer replays byte-identically it escalates loudly (memory
report, Telegram, and a GitHub issue) so a newly-introduced nondeterminism
source is caught within a cycle instead of silently rotting the audit trail.

For each prompt it runs the full pipeline live (recording the decision log and
freezing every LLM response + tool result), then re-executes the recorded run
against those frozen responses and asserts the decision path replays with zero
divergence and zero new API calls. See docs/DECISION_LOG.md ("The replay
invariant") for what must be frozen.
"""

from __future__ import annotations

from . import config, orchestrator, replaystore, trace, usage

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


def _gate_bot() -> "orchestrator.Olympus":
    """Build the bot the gate runs on. For Anthropic, pin the cheaper
    `config.GATE_MODEL` (the determinism check doesn't need the main model);
    for other providers, just use the configured pool."""
    base = config.Settings.from_env()
    if base.provider == "anthropic" and config.GATE_MODEL:
        s = config.Settings(provider="anthropic", model=config.GATE_MODEL,
                            api_key=base.api_key, base_url=base.base_url)
        return orchestrator.Olympus(pool=config.ModelPool.of(s))
    return orchestrator.Olympus(pool=config.ModelPool.from_env())


def _ok(rec: dict) -> bool:
    return rec["completed"] and rec["replayable"] and rec["decisions"] > 0


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
    """Run the gate over `prompts`. Returns (all_pass, results)."""
    prompts = prompts or DEFAULT_PROMPTS
    make_bot = make_bot or _gate_bot
    results = []
    for i, prompt in enumerate(prompts, 1):
        report(f"\n[{i}/{len(prompts)}] {prompt[:70]}...")
        rec = check_one(prompt, make_bot, report)
        report(f"    completed={rec['completed']} decisions={rec['decisions']} "
               f"replayable={rec['replayable']} — {rec['detail']}")
        report(f"    {'PASS' if _ok(rec) else 'FAIL'}  (run {rec['run_id']})")
        results.append(rec)
    all_pass = len(results) >= 3 and all(_ok(r) for r in results)
    return all_pass, results


def _failure_body(results: list[dict]) -> str:
    lines = [f"- {'PASS' if _ok(r) else 'FAIL'} [{r['run_id']}] "
             f"{r['prompt'][:80]} — {r['detail']}" for r in results]
    return (
        "Olympus's re-executable decision log diverged on a live run: a "
        "recorded run no longer replays byte-identically. This means a non-LLM "
        "input to a decision is not being frozen — see docs/DECISION_LOG.md, "
        "'The replay invariant'. Every tool result and any mutable state "
        "injected into a prompt must be frozen (replaystore.put_tool / "
        "frozen_context).\n\nPer-prompt results:\n" + "\n".join(lines))


def self_check(prompts=None, *, make_bot=None, report=None) -> dict:
    """Run the gate and, on failure, escalate (memory, Telegram, GitHub issue).
    Returns a summary dict. Used by the heartbeat as an automatic tripwire."""
    from . import memory
    rep = report or (lambda _msg: None)
    try:
        usage.check_budget()
    except usage.BudgetExceeded as err:
        return {"ran": False, "passed": None, "skipped": str(err)}

    all_pass, results = run_exit_check(prompts, make_bot=make_bot, report=rep)
    passed = sum(1 for r in results if _ok(r))
    summary = (f"Replay self-check: {passed}/{len(results)} live runs replayed "
               "byte-identically.")

    if all_pass:
        memory.save("reports", "Replay self-check passed", summary)
        return {"ran": True, "passed": True, "summary": summary, "results": results}

    body = summary + "\n\n" + _failure_body(results)
    memory.save("corrections", "Replay self-check FAILED", body)
    try:
        from . import telegram
        telegram.notify("⚠️ Olympus replay self-check FAILED\n\n" + body)
    except Exception:
        pass
    issue_url = None
    try:
        from . import github
        if github.configured():
            issue_url = github.create_issue(
                "Replay self-check failed: decision log no longer byte-identical",
                body)
    except Exception:
        pass
    return {"ran": True, "passed": False, "summary": summary,
            "results": results, "issue": issue_url}
