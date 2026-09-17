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

from functools import wraps
import uuid
from . import config, deltas, memory, security, sleeptime, sleeptime_cycles, usermem

SCOREBOARD = sleeptime_cycles.TARGET
_MAX_SAMPLE = 200


def _grade(summary_by_user: dict, mined_count: int, errors: list[str]
           ) -> tuple[str, list[str]]:
    """CLEAN unless anything in the cycle needed a human: an Aletheia rejection,
    an unverified or below-floor-confidence proposal, or a harness error. Every
    DIRTY reason is quoted so the operator sees exactly what went wrong."""
    # DIRTY reasons are signed into the scoreboard and externally anchored, and
    # some of their text is UNTRUSTED (proposal claims + cycle errors derive
    # from mined failure traces / memory content). Defang it before it enters
    # the record — the same discipline the mined `samples` already follow, so
    # an injection payload can't ride into the signed/anchored evidence.
    def _clean(text) -> str:
        return security.sanitize_for_memory(str(text))[:_MAX_SAMPLE]

    reasons: list[str] = []
    floor = config.SLEEPTIME_CONFIDENCE_MIN
    for user, s in summary_by_user.items():
        if s.get("error"):
            reasons.append(f"user {user!r}: cycle error — {_clean(s['error'])}")
            continue
        if s.get("clean") is not True:
            reasons.append(f"user {user!r}: no affirmative clean decision")
        if len(s.get("cycle_proposals", [])) != s.get("proposed", 0) + s.get("rejected", 0):
            reasons.append(f"user {user!r}: incomplete proposal evidence")
        for p in s.get("cycle_proposals", []):
            if not p.get("verified"):
                claims = "; ".join(_clean(c) for c in (p.get("unsupported") or []))
                reasons.append(
                    f"user {user!r}: Aletheia REJECTED proposal {p.get('id')}"
                    + (f" — unsupported claims: {claims}" if claims else ""))
            elif floor > 0 and float(p.get("confidence", 0.0)) < floor:
                reasons.append(
                    f"user {user!r}: proposal {p.get('id')} confidence "
                    f"{p.get('confidence')} below the {floor} floor "
                    "(block-mode would have refused it)")
    reasons.extend(_clean(e) for e in errors)
    if not any(s.get("cycle_proposals") for s in summary_by_user.values()):
        reasons.append("no persisted proposals; empty work does not qualify")
    return ("CLEAN" if not reasons else "DIRTY"), reasons


def _shared_context(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with memory.user_context("shared"):
            return fn(*args, **kwargs)
    return wrapped


@_shared_context
@sleeptime_cycles.serialized
def run_supervised_cycle(settings=None, *, generator=None, verifier=None, cycle_id=None) -> dict:
    """One supervised reflection cycle. Returns the full evidence report:
    {grade, reasons, users, mined_failures, streak, scoreboard_hash}.

    Apply is HARD-OFF: `auto_apply=False` below is a literal, not a config
    read — this harness has no code path that commits a rewrite or writes a
    prompt, whatever the environment says."""
    cycle_id = cycle_id or uuid.uuid4().hex
    prior = sleeptime_cycles.replay(cycle_id)
    if prior is not None:
        return _report(prior)
    # Fail before any model work when authoritative evidence is unavailable.
    sleeptime.state()
    errors: list[str] = []

    # -- evidence: mined failure traces (report-only; no proposer, no gate) --
    mined = {"count": 0, "by_agent": {}, "samples": []}
    try:
        from . import reflect
        signals = reflect.mine_failures(strict=True)
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
        users = usermem.owners()
    except Exception as err:
        users = []
        errors.append(f"user enumeration failed: {err}")
    for uid in users:
        try:
            s = sleeptime.refine_user(uid, settings=settings, generator=generator,
                                      verifier=verifier,
                                      auto_apply=False)   # HARD-OFF — never a config read
            if not isinstance(s, dict):
                raise ValueError("invalid refinement summary")
            by_id = {p["id"]: p for p in sleeptime.proposals(uid)}
            identifiers = s.get("proposal_ids")
            if not isinstance(identifiers, list) or len(set(identifiers)) != len(identifiers):
                raise ValueError("missing or duplicate proposal identities")
            cycle_props = [by_id[identifier] for identifier in identifiers]
            s["cycle_proposals"] = [
                {"id": p["id"], "verified": p["verified"],
                 "confidence": p["confidence"], "unsupported": p["unsupported"],
                 "applied": p["applied"], "diff": sleeptime.render_diff(p)} for p in cycle_props]
            summary_by_user[uid] = s
        except Exception as err:
            summary_by_user[uid] = {"error": str(err), "clean": False, "proposal_ids": []}

    grade, reasons = _grade(summary_by_user, mined["count"], errors)

    # The signed scoreboard and the official streak are ONE publication.
    proposed = sum(s.get("proposed", 0) for s in summary_by_user.values())
    refs = [(uid, identifier) for uid, s in summary_by_user.items()
            for identifier in s.get("proposal_ids", [])]
    try:
        snap = sleeptime_cycles.record(grade == "CLEAN", proposed, 0,
            proposal_refs=refs, cycle_id=cycle_id, reasons=reasons,
            report={"users": summary_by_user, "mined_failures": mined})
    except Exception as err:
        # This identifies a retryable cycle. It does not invent a failed/zero
        # counter when local publication may already have succeeded.
        raise deltas.DeltaError(f"cycle {cycle_id} recording unconfirmed; retry this cycle ID: {err}") from err
    return _report(snap)


def _report(snap):
    data = snap["state"]
    return {"cycle_id": data["cycle_id"], "grade": data["grade"], "reasons": data["reasons"],
            **data["report"], "streak": data["counters"]["clean_cycles"],
            "graduation": config.SLEEPTIME_GRADUATION,
            "scoreboard_hash": snap["snapshot_hash"]}


def scoreboard() -> list[dict]:
    """Every graded cycle, oldest first (verify with deltas.verify_history)."""
    return sleeptime_cycles.records()


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
