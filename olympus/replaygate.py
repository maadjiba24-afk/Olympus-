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


# Errors that mean "couldn't run the gate" (infrastructure/account), NOT "the
# reasoning diverged". These are skipped, never reported as a replay failure, so
# an empty wallet or a missing key can't masquerade as a regression.
_PROVIDER_UNAVAILABLE = (
    "credit balance", "plans & billing", "billing", "quota",
    "authentication", "x-api-key", "api key", "permission",
    "overloaded", "rate limit", "503", "529",
)


def _is_provider_unavailable(msg: str) -> bool:
    m = (msg or "").lower()
    return any(sig in m for sig in _PROVIDER_UNAVAILABLE)


def check_one(prompt: str, make_bot, report) -> dict:
    """Run one prompt live, then replay it. Returns a result record. `skipped`
    marks an infrastructure/account problem (no key, no credits, rate limit,
    budget cap) — distinct from a genuine replay failure."""
    rec: dict = {"prompt": prompt, "completed": False, "replayable": False,
                 "decisions": 0, "run_id": None, "detail": "", "skipped": False}
    bot = make_bot()
    try:
        reply = bot.ask(prompt)
    except Exception as err:
        if _is_provider_unavailable(str(err)):
            rec["skipped"] = True       # infra/account problem, not a real failure
            rec["detail"] = f"skipped — provider unavailable: {str(err)[:140]}"
        else:
            # An unexpected failure is a real FAIL — log it loudly, never let it
            # masquerade as a pass or a benign skip.
            from . import errors
            errors.capture("replaygate.ask", err, context=prompt[:120])
            rec["detail"] = f"ask() FAILED (unexpected error, logged): {err!r}"
        return rec

    if not reply or reply.startswith(("Configuration problem", "Daily budget")):
        rec["skipped"] = True            # our own config/budget guard, not a fail
        rec["detail"] = f"skipped — {reply[:120]}"
        return rec

    rec["completed"] = True
    rec["run_id"] = getattr(bot, "last_run_id", None)
    run = trace.load_run(rec["run_id"]) if rec["run_id"] else None
    rec["decisions"] = len(run.get("decisions", [])) if run else 0
    if rec["decisions"] == 0:
        rec["detail"] = "completed but recorded no decisions"
        return rec

    try:
        _original, _fresh, diffs = orchestrator.replay_run(rec["run_id"])
    except replaystore.ReplayDivergence as err:
        rec["detail"] = f"replay diverged: {err}"   # genuine divergence -> FAIL
        return rec
    except Exception as err:
        # An unexpected internal error is NOT a pass and NOT a benign skip —
        # log it loudly and fail, so a swallowed bug can't yield a false green.
        from . import errors
        errors.capture("replaygate.replay_run", err, context=f"run {rec['run_id']}")
        rec["detail"] = f"replay FAILED (unexpected error, logged): {err!r}"
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
        status = "SKIP" if rec.get("skipped") else ("PASS" if _ok(rec) else "FAIL")
        report(f"    completed={rec['completed']} decisions={rec['decisions']} "
               f"replayable={rec['replayable']} — {rec['detail']}")
        report(f"    {status}  (run {rec['run_id']})")
        results.append(rec)
    all_pass = len(results) >= 3 and all(_ok(r) for r in results)
    return all_pass, results


def genuine_failures(results: list[dict]) -> list[dict]:
    """Runs that actually broke the gate — not passes, not infra/account skips."""
    return [r for r in results if not _ok(r) and not r.get("skipped")]


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

    # Only infra/account skips (no credits, no key, rate limit) and no genuine
    # divergence → inconclusive. Don't escalate: that's not a regression.
    if not genuine_failures(results):
        return {"ran": False, "passed": None, "summary": summary,
                "skipped": "provider unavailable or budget — gate inconclusive",
                "results": results}

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
