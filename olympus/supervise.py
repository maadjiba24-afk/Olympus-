"""Supervised-cycle harness — makes the 10-clean-cycle graduation rule
EXECUTABLE (`olympus sleeptime-supervise`).

Runs exactly ONE reflection cycle in supervision posture and grades it:

  * memory consolidation runs with auto-apply HARD-OFF — `refine_user` is
    always called with `auto_apply=False`, a literal in this module. Nothing is
    committed, superseded, or rewritten; every proposal is evidence only.
  * the failure-trace miner runs in REPORT-ONLY mode — no proposer, no prompt
    gate, no prompt writes.
  * a per-cycle EVIDENCE REPORT is emitted: the mined failure signals, every
    proposed delta with its Aletheia verdict + confidence annotation, and the
    exact would-have-applied diff (`sleeptime.render_diff`).
  * the cycle is graded CLEAN or DIRTY with quoted reasons, the official
    graduation streak is advanced/reset accordingly (`sleeptime._record_cycle`),
    and a witness-signed scoreboard entry is appended on the delta substrate
    (`deltas.record_snapshot`) — tamper-evident, and head-anchored externally
    when anchoring is on.

Architecturally incapable of escalation — enforced by tests, not convention:
this module NEVER writes to the process environment or any config, so it
cannot enable OLYMPUS_SLEEPTIME, OLYMPUS_SLEEPTIME_AUTOAPPLY, or block-mode.
It grades cycles; only a human flips switches.
"""

from __future__ import annotations

from . import config, deltas, memory, security, sleeptime, store, usermem

SCOREBOARD = "sleeptime:scoreboard"
_MAX_SAMPLE = 200


def _grade(summary_by_user: dict, mined_count: int, errors: list[str]
           ) -> tuple[str, list[str]]:
    """CLEAN unless anything in the cycle needed a human: an Aletheia rejection,
    an unverified or below-floor-confidence proposal, or a harness error. Every
    DIRTY reason is quoted so the operator sees exactly what went wrong."""
    reasons: list[str] = []
    floor = config.SLEEPTIME_CONFIDENCE_MIN
    for user, s in summary_by_user.items():
        if s.get("error"):
            reasons.append(f"user {user!r}: cycle error — {s['error']}")
            continue
        for p in s.get("cycle_proposals", []):
            if not p.get("verified"):
                claims = "; ".join(str(c)[:_MAX_SAMPLE]
                                   for c in (p.get("unsupported") or []))
                reasons.append(
                    f"user {user!r}: Aletheia REJECTED proposal {p.get('id')}"
                    + (f" — unsupported claims: {claims}" if claims else ""))
            elif floor > 0 and float(p.get("confidence", 1.0)) < floor:
                reasons.append(
                    f"user {user!r}: proposal {p.get('id')} confidence "
                    f"{p.get('confidence')} below the {floor} floor "
                    "(block-mode would have refused it)")
    reasons.extend(errors)
    return ("CLEAN" if not reasons else "DIRTY"), reasons


def run_supervised_cycle(settings=None, *, generator=None, verifier=None) -> dict:
    """One supervised reflection cycle. Returns the full evidence report:
    {grade, reasons, users, mined_failures, streak, scoreboard_hash}.

    Apply is HARD-OFF: `auto_apply=False` below is a literal, not a config
    read — this harness has no code path that commits a rewrite or writes a
    prompt, whatever the environment says."""
    memory.set_user("shared")
    errors: list[str] = []

    # -- evidence: mined failure traces (report-only; no proposer, no gate) --
    mined = {"count": 0, "by_agent": {}, "samples": []}
    try:
        from . import reflect
        signals = reflect.mine_failures()
        mined["count"] = len(signals)
        for s in signals:
            key = s.agent or "(run-level)"
            mined["by_agent"][key] = mined["by_agent"].get(key, 0) + 1
        # Samples are UNTRUSTED trace text — defang before they enter a report.
        mined["samples"] = [
            f"[{s.kind}] {s.agent or 'run'}@{s.run_id[:8]}: "
            + security.sanitize_for_memory(s.detail)[:_MAX_SAMPLE]
            for s in signals[:10]]
    except Exception as err:
        errors.append(f"failure-trace mining failed: {err}")

    # -- one supervised consolidation pass per user (apply hard-off) --------
    summary_by_user: dict[str, dict] = {}
    try:
        users = list(store.backend().keys(usermem._MEMS))
    except Exception as err:
        users = []
        errors.append(f"user enumeration failed: {err}")
    for uid in users:
        before = len(sleeptime.proposals(uid))
        s = sleeptime.refine_user(uid, settings=settings, generator=generator,
                                  verifier=verifier,
                                  auto_apply=False)   # HARD-OFF — never a config read
        cycle_props = sleeptime.proposals(uid)[before:]
        s["cycle_proposals"] = [
            {"id": p.get("id"), "verified": bool(p.get("verified")),
             "confidence": p.get("confidence", 1.0),
             "unsupported": p.get("unsupported") or [],
             "applied": bool(p.get("applied")),
             "diff": sleeptime.render_diff(p)}
            for p in cycle_props]
        summary_by_user[uid] = s

    grade, reasons = _grade(summary_by_user, mined["count"], errors)

    # -- advance/reset the OFFICIAL graduation streak ------------------------
    proposed = sum(s.get("proposed", 0) for s in summary_by_user.values())
    st = sleeptime._record_cycle(grade == "CLEAN", proposed, committed=0)

    # -- witness-signed scoreboard entry (tamper-evident, head-anchored) -----
    entry_state = {
        "grade": grade, "reasons": reasons,
        "users": len(summary_by_user), "proposed": proposed,
        "rejected": sum(s.get("rejected", 0) for s in summary_by_user.values()),
        "mined_failures": mined["count"],
        "clean_cycles_after": st["clean_cycles"],
        "graduation": config.SLEEPTIME_GRADUATION,
    }
    snap = deltas.record_snapshot(
        SCOREBOARD, kind="supervision", state=entry_state,
        provenance=deltas.Provenance(source="supervise", trust="operator",
                                     detail=f"supervised cycle graded {grade}"))

    return {"grade": grade, "reasons": reasons, "users": summary_by_user,
            "mined_failures": mined, "streak": st["clean_cycles"],
            "graduation": config.SLEEPTIME_GRADUATION,
            "scoreboard_hash": snap["snapshot_hash"]}


def scoreboard() -> list[dict]:
    """Every graded cycle, oldest first (verify with deltas.verify_history)."""
    return deltas.snapshots(SCOREBOARD)


def render_report(report: dict) -> str:
    """The operator-facing evidence report for one cycle."""
    lines = [f"# Supervised reflection cycle — {report['grade']}",
             f"streak: {report['streak']}/{report['graduation']} clean "
             f"(scoreboard {report['scoreboard_hash'][:12]})", ""]
    if report["reasons"]:
        lines.append("## Why DIRTY")
        lines += [f"- {r}" for r in report["reasons"]]
        lines.append("")
    m = report["mined_failures"]
    lines.append(f"## Mined failure traces: {m['count']}")
    lines += [f"- {agent}: {n}" for agent, n in sorted(m["by_agent"].items())]
    lines += [f"  {s}" for s in m["samples"]]
    lines.append("")
    for user, s in report["users"].items():
        props = s.get("cycle_proposals", [])
        lines.append(f"## {user}: {s.get('groups', 0)} group(s), "
                     f"{len(props)} proposal(s), 0 committed (supervised)")
        for p in props:
            tag = "verified" if p["verified"] else "REJECTED"
            lines.append(f"### proposal {p['id']} [{tag}, "
                         f"confidence {p['confidence']}]")
            if p["unsupported"]:
                lines.append(f"unsupported: {p['unsupported']}")
            lines.append("Would-have-applied diff:")
            lines.append(p["diff"])
    return "\n".join(lines)
