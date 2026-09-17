"""Sleep-time memory refinement — Letta-style idle-time consolidation.

During idle time (heartbeat cadence) Olympus reviews a user's typed memory and
proposes *refinements*: consolidating near-duplicate memories, tightening a
verbose one. This shifts memory upkeep off the user-facing critical path so
future recall is cleaner, without a live turn paying for it.

Every safety property the loop must hold:

  * Reversible + versioned. A rewrite never destroys the source memories: they
    are `supersede()`d (kept as history) and an append-only SNAPSHOT of their
    pre-state is written, so `revert()` restores them exactly.
  * Aletheia-gated. A proposed rewrite is VERIFIED before it can commit — it may
    assert nothing its source memories do not support. An unverified rewrite is
    never committed and breaks the "clean cycle" streak.
  * Provenance/trust preserving. A consolidated memory inherits the UNION of its
    sources' provenance and the STRONGEST sensitivity — a rewrite can never
    launder a high-sensitivity fact into a normal one, nor drop provenance.
  * Governed. Every commit is gated by the `memory.rewrite` behavioral contract
    (behavioral_contracts.py), which re-checks the three properties above.
  * Off by default, and earns autonomy. `config.sleeptime_enabled()` is False by
    default. Even enabled, the loop runs SUPERVISED — it proposes reversible
    diffs and commits NOTHING — until it has logged `SLEEPTIME_GRADUATION` clean
    cycles; auto-apply additionally requires `config.sleeptime_autoapply()`. Any
    Aletheia rejection resets the streak.

The selection + merge logic is pure and deterministic; the two model-backed
steps (generate the rewrite, verify it) are pluggable so tests drive them with
no network, mirroring ace.py / transcript.py.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from copy import deepcopy
from functools import wraps
from typing import Any, Callable

from . import config, memory, store, usermem, sleeptime_cycles

_WORD = re.compile(r"[a-z0-9]+")
_STATE_NS = "sleeptime"
_SNAP_NS = "sleeptime.snapshots"
_PROP_NS = "sleeptime.proposals"
_QUAR_NS = "sleeptime.quarantine"       # rewrites rejected by Aletheia block-mode

# bounds (hard caps)
_MIN_GROUP_OVERLAP = 0.34       # Jaccard ≥ this ⇒ same-topic, consolidatable
_MAX_GROUP = 5                  # never merge more than this many at once
_MAX_MEMS_SCANNED = 400         # cap the per-user scan
_MAX_PROPOSALS_PER_RUN = 20     # bound work per cycle
_MAX_SNAPSHOTS = 500            # append-only history cap per user


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(str(text).lower()) if len(w) > 2)


def _overlap(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# --- selection (pure) -----------------------------------------------------

def consolidation_groups(mems: list[dict]) -> list[list[dict]]:
    """Group ACTIVE memories of the same type that overlap enough to be a single
    consolidated memory. Deterministic: greedy over a stable ordering. A memory
    joins at most one group; singletons are dropped (nothing to consolidate)."""
    active = [m for m in mems if m.get("status") == usermem.ACTIVE][:_MAX_MEMS_SCANNED]
    # stable order: type, then creation time, then id
    active.sort(key=lambda m: (m.get("type", ""), m.get("created_at", 0),
                               m.get("id", "")))
    used: set[str] = set()
    groups: list[list[dict]] = []
    for i, m in enumerate(active):
        if m["id"] in used:
            continue
        grp = [m]
        mt = _tokens(m.get("content", ""))
        for n in active[i + 1:]:
            if n["id"] in used or n.get("type") != m.get("type"):
                continue
            if _overlap(mt, _tokens(n.get("content", ""))) >= _MIN_GROUP_OVERLAP:
                grp.append(n)
                if len(grp) >= _MAX_GROUP:
                    break
        if len(grp) > 1:
            for g in grp:
                used.add(g["id"])
            groups.append(grp)
    return groups


def _rank_sensitivity(mems: list[dict]) -> str:
    return "high" if any(m.get("sensitivity") == "high" for m in mems) else "normal"


def _merged_provenance(mems: list[dict]) -> list[str]:
    out: list[str] = []
    for m in mems:
        for p in (m.get("provenance") or []):
            if p not in out:
                out.append(p)
    return out


# --- model-backed steps (pluggable) --------------------------------------

GenerateFn = Callable[[list[dict]], str]
VerifyFn = Callable[[list[dict], str], dict]

_GEN_SYSTEM = (
    "You consolidate a small set of overlapping memories about a user into ONE "
    "clear, self-contained memory. Preserve every fact; add nothing new; do not "
    "speculate. If they conflict, keep the most specific and note the others are "
    "uncertain. Return only the consolidated memory text, a few sentences at most."
)

_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        # A numeric self-assessed confidence in [0,1] — what feeds Aletheia
        # block-mode: a graduated loop refuses to auto-commit a below-floor
        # rewrite. Without this, the guardrail would be inert in production.
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["supported", "confidence"],
    "additionalProperties": False,
}

_VERIFY_SYSTEM = (
    "You are Aletheia, a strict verification gate. You are given SOURCE memories "
    "and a proposed CONSOLIDATED memory. Decide whether the consolidation "
    "asserts anything NOT supported by the sources. Return supported=false and "
    "list the unsupported claims if it introduces, exaggerates, or alters any "
    "fact; supported=true only if every claim traces to the sources. Also return "
    "confidence in [0,1]: how sure you are the consolidation is faithful and "
    "safe to commit unattended (use a LOW value when the sources are ambiguous, "
    "conflicting, or thin — an honest low score is better than a false high one)."
)


def default_generator(settings) -> GenerateFn:
    from . import backend
    s = settings or config.Settings.from_env()

    def generate(sources: list[dict]) -> str:
        from . import deltas
        body = "\n".join(f"- {m.get('content', '')}" for m in sources)
        return backend.complete_text(
            s, _GEN_SYSTEM, [{"role": "user", "content": deltas.enveloped(body[:4000], source="consolidation-sources")}],
            effort="low").strip()[:usermem._MAX_CONTENT]

    return generate


def default_verifier(settings) -> VerifyFn:
    from . import backend
    s = settings or config.Settings.from_env()

    def verify(sources: list[dict], rewrite: str) -> dict:
        from . import deltas
        body = ("SOURCE memories:\n"
                + "\n".join(f"- {m.get('content', '')}" for m in sources)
                + f"\n\nCONSOLIDATED memory:\n{rewrite}")
        out = backend.complete_json(
            s, _VERIFY_SYSTEM, [{"role": "user", "content": deltas.enveloped(body[:6000], source="consolidation-verification")}],
            _VERIFY_SCHEMA, effort="low")
        return out if isinstance(out, dict) else {"supported": False}

    return verify


# --- proposal / snapshot / state -----------------------------------------

@dataclass
class Proposal:
    id: str
    user: str
    type: str
    source_ids: list[str]
    source_contents: list[str]
    rewrite: str
    provenance: list[str]
    sensitivity: str
    verified: bool
    unsupported: list[str] = field(default_factory=list)
    applied: bool = False
    created_at: float = 0.0
    source_records: list[dict] = field(default_factory=list)
    confidence: float = 0.0             # Missing confidence is not affirmative evidence.

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "id", "user", "type", "source_ids", "source_contents", "rewrite",
            "provenance", "sensitivity", "verified", "unsupported", "applied",
            "created_at", "confidence", "source_records")}


def state() -> dict:
    from . import sleeptime_cycles
    return sleeptime_cycles.state()


def graduated() -> bool:
    """True once the loop has logged enough clean supervised cycles to be
    trusted with auto-apply (still gated by config.sleeptime_autoapply)."""
    from . import witness
    return (state()["clean_cycles"] >= config.SLEEPTIME_GRADUATION
            and not witness.is_default_seed())


def block_mode_active() -> bool:
    """Whether Aletheia BLOCK-MODE is live: it activates only once the loop has
    GRADUATED (10 clean supervised cycles) and a confidence floor is configured.
    Before that it is inert — Aletheia annotates but never rejects a rewrite."""
    return graduated() and config.SLEEPTIME_CONFIDENCE_MIN > 0.0


def _record_cycle(clean: bool, proposed: int, committed: int, **evidence) -> dict:
    from . import sleeptime_cycles
    return sleeptime_cycles.record(clean, proposed, committed, **evidence)["state"]["counters"]


def proposals(user: str) -> list[dict]:
    from . import sleeptime_evidence as se
    return se.load(user, "proposals")


def _add_proposal(p: Proposal) -> None:
    from . import sleeptime_evidence as se
    se.add(p.user, p.to_dict())


def quarantined(user: str) -> list[dict]:
    from . import sleeptime_evidence as se
    return [{**row["proposal"], "reason": row["reason"]}
            for row in se.load(user, "quarantine")]


def snapshots(user: str) -> list[dict]:
    from . import sleeptime_evidence as se
    return se.load(user, "snapshots")


def _commit(user: str, sources: list[dict], p: Proposal) -> str | None:
    """One owner snapshot commits proposal, undo evidence and memory changes."""
    from . import behavioral_contracts as abc, sleeptime_evidence as se
    if p.user != memory.canonical_owner(user):
        raise ValueError("proposal belongs to another owner")
    proposal = p.to_dict()
    if not proposal["source_records"]:
        proposal["source_records"] = deepcopy(sources)
    if proposal["source_records"] != sources:
        raise ValueError("proposal source binding differs")

    def check(live):
        try:
            abc.enforce("memory.rewrite", {
                "source_provenance": _merged_provenance(live),
                "new_provenance": p.provenance,
                "source_sensitivities": [m["sensitivity"] for m in live],
                "new_sensitivity": p.sensitivity,
                "source_type": p.type, "new_type": p.type,
                "verify_supported": p.verified,
                "unsupported_claims": p.unsupported,
                "block_mode_active": block_mode_active(),
                "rewrite_confidence": p.confidence,
                "confidence_threshold": config.SLEEPTIME_CONFIDENCE_MIN,
            })
        except abc.ContractViolation as err:
            return str(err)
        return None

    result = se.commit(user, proposal, check)
    se.flush_quarantine(user)
    return result


def approve(user: str, proposal_id: str) -> str | None:
    from . import sleeptime_evidence as se
    with se.transaction(user) as data:
        row = next((p for p in data["proposals"] if p["id"] == proposal_id), None)
        if row is None:
            return None
        # Do not hold a second copied consolidation collection across commit.
        proposal = Proposal(**deepcopy(row))
    return _commit(user, proposal.source_records, proposal)


def _owner_context(fn):
    @wraps(fn)
    def wrapped(user, *args, **kwargs):
        if not isinstance(user, str) or not user.strip():
            return {"error": "invalid user", "clean": False}
        with memory.user_context(user):
            return fn(user, *args, **kwargs)
    return wrapped


@_owner_context
def refine_user(user: str, *, settings=None,
                generator: GenerateFn | None = None,
                verifier: VerifyFn | None = None,
                auto_apply: bool | None = None) -> dict:
    """One refinement cycle for one user. Returns a summary. Never raises into
    the caller. `auto_apply` overrides the config gate (tests set it True)."""
    summary = {"groups": 0, "proposed": 0, "committed": 0,
               "rejected": 0, "clean": True, "proposal_ids": []}

    def fail(reason: str) -> None:
        """Make missing/malformed cycle evidence non-graduating."""
        summary["clean"] = False
        summary.setdefault("error", reason)

    if not isinstance(user, str) or not user.strip():
        fail("invalid user")
        return summary
    try:
        gen = generator or default_generator(settings)
        ver = verifier or default_verifier(settings)
        if auto_apply is None:
            auto_apply = graduated() and config.sleeptime_autoapply()
    except Exception as err:
        from . import errors
        errors.capture("sleeptime.setup", err)
        fail("refinement setup failed")
        return summary
    try:
        proposals(user)  # Refuse damaged/ambiguous history before provider work.
        mems = usermem.active_memories(user)
    except Exception as err:
        from . import errors
        errors.capture("sleeptime.load", err)
        fail("load failed")
        return summary

    groups = consolidation_groups(mems)[:_MAX_PROPOSALS_PER_RUN]
    summary["groups"] = len(groups)
    for grp in groups:
        try:
            rewrite = gen(grp)
        except Exception as err:
            from . import errors
            errors.capture("sleeptime.generate", err)
            fail("generation failed")
            continue
        if not isinstance(rewrite, str) or not rewrite.strip():
            fail("generation produced no usable rewrite")
            continue
        from . import security
        rewrite = security.sanitize_for_memory(rewrite.strip())[:usermem._MAX_CONTENT]
        try:
            verdict = ver(grp, rewrite)
        except Exception as err:
            from . import errors
            errors.capture("sleeptime.verify", err)
            fail("verification failed")
            continue
        if not isinstance(verdict, dict):
            fail("verification verdict is not an object")
            continue

        # Aletheia approval is load-bearing evidence: only the Boolean value
        # True authorises a proposal.  Truthy strings/numbers must not turn a
        # malformed model response into an unattended memory rewrite.
        raw_supported = verdict.get("supported")
        supported = raw_supported is True
        if not isinstance(raw_supported, bool):
            fail("verification verdict has no Boolean supported decision")

        raw_unsupported = verdict.get("unsupported_claims", [])
        if (not isinstance(raw_unsupported, list)
                or not all(isinstance(claim, str)
                           for claim in raw_unsupported)):
            fail("verification verdict has invalid unsupported claims")
            raw_unsupported = []
            supported = False
        elif raw_unsupported and supported:
            fail("verification verdict contradicts its unsupported claims")
            supported = False

        # No inferred confidence: missing evidence cannot authorize a rewrite.
        if "confidence" not in verdict:
            fail("verification verdict is missing confidence")
            confidence = 0.0
            supported = False
        else:
            try:
                raw_confidence = verdict["confidence"]
                if (not isinstance(raw_confidence, (int, float))
                        or isinstance(raw_confidence, bool)):
                    raise TypeError("confidence is not numeric")
                confidence = float(raw_confidence)
                if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                    raise ValueError("confidence outside [0,1]")
            except (TypeError, ValueError):
                fail("verification verdict has invalid confidence")
                confidence = 0.0
                supported = False
        # Poisoned-feedback defense: a rewrite is model output over (possibly
        # untrusted-derived) memory — defang any injection-shaped imperative
        # BEFORE it can become a stored memory, exactly as the write path does
        # (recall._run_extractor). Aletheia is the semantic gate; this is the
        # lexical one, and both run before anything is committed.
        from . import security
        clean_rewrite = security.sanitize_for_memory(
            rewrite.strip())[:usermem._MAX_CONTENT]
        p = Proposal(
            id=uuid.uuid4().hex[:12], user=user, type=grp[0].get("type", "project"),
            source_ids=[m["id"] for m in grp],
            source_contents=[m.get("content", "") for m in grp],
            source_records=deepcopy(grp),
            rewrite=clean_rewrite,
            provenance=_merged_provenance(grp),
            sensitivity=_rank_sensitivity(grp),
            verified=supported,
            unsupported=list(raw_unsupported),
            created_at=time.time(), confidence=confidence)
        try:
            _add_proposal(p)  # Persist the reference before attempting application.
            summary["proposal_ids"].append(p.id)
            if not supported:
                summary["rejected"] += 1
                summary["clean"] = False
                continue
            summary["proposed"] += 1
            if auto_apply:
                new_id = _commit(user, grp, p)
                if new_id:
                    summary["committed"] += 1
                else:
                    fail("memory rewrite contract rejected")
        except Exception as err:
            from . import errors
            errors.capture("sleeptime.publish", err)
            fail("proposal/application evidence unavailable; inspect persisted proposal " + p.id)
    return summary


def render_diff(proposal: dict) -> str:
    """A proposal as a unified diff — the sources it would supersede on the left,
    the consolidated rewrite on the right. This is the operator-facing form of a
    destructive change: reviewed as a diff, applied only through the gates."""
    import difflib
    if not isinstance(proposal, dict):
        return ""
    before = [str(s) for s in (proposal.get("source_contents") or [])]
    after = [str(proposal.get("rewrite", ""))]
    lines = difflib.unified_diff(
        before, after,
        fromfile=f"memories/{','.join(proposal.get('source_ids') or [])}",
        tofile=f"proposal/{proposal.get('id', '?')}", lineterm="")
    header = (f"# proposal {proposal.get('id', '?')} "
              f"[{'verified' if proposal.get('verified') else 'UNVERIFIED'}"
              f"{', applied' if proposal.get('applied') else ''}]")
    return "\n".join([header, *lines])


def revert(user: str, snapshot_id: str) -> bool:
    """Restore exact pre-rewrite rows atomically; reject stale intervening edits."""
    from . import sleeptime_evidence
    return sleeptime_evidence.revert(user, snapshot_id)


@sleeptime_cycles.serialized
def run_result(settings=None) -> dict:
    """Typed cycle outcome; unavailable/dirty evidence is never exit success."""
    if not config.sleeptime_enabled():
        return {"status": "disabled", "lines": []}
    try:
        state()  # Refuse unavailable qualification evidence before model work.
        users = usermem.owners()
    except Exception as err:
        from . import errors
        errors.capture("sleeptime.enumerate", err)
        return {"status": "unavailable", "lines": ["Sleep-time evidence unavailable: " + str(err)]}
    total = {"proposed": 0, "committed": 0, "rejected": 0}
    refs, problems = [], []
    clean = True
    for uid in users:
        try:
            s = refine_user(uid, settings=settings)
        except Exception as err:
            # `refine_user` promises containment, but the graduation boundary
            # must remain safe even if a future refactor violates that promise.
            from . import errors
            errors.capture("sleeptime.refine", err, context=str(uid))
            s = {"error": "refinement failed", "clean": False}
        if not isinstance(s, dict):
            s = {"error": "invalid refinement summary", "clean": False}
        for k in ("proposed", "committed", "rejected"):
            value = s.get(k, 0)
            if (not isinstance(value, int) or isinstance(value, bool)
                    or value < 0):
                clean = False
                continue
            total[k] += value
        # Graduation needs an affirmative Boolean clean verdict.  Errors,
        # missing fields, and merely truthy values all reset the streak.
        cycle_clean = s.get("clean") is True and not s.get("error")
        clean = clean and cycle_clean
        identifiers = s.get("proposal_ids")
        if not isinstance(identifiers, list) or not all(isinstance(v, str) for v in identifiers):
            clean = False
            problems.append("missing proposal identities for " + uid)
        else:
            refs.extend((uid, identifier) for identifier in identifiers)
        if s.get("error"):
            problems.append(str(s["error"]))
    cycle_id = uuid.uuid4().hex
    try:
        st = _record_cycle(clean, total["proposed"], total["committed"],
                           proposal_refs=refs, cycle_id=cycle_id, reasons=problems)
    except Exception as err:
        return {"status": "unavailable", "cycle_id": cycle_id, "lines": [
            f"Sleep-time cycle {cycle_id} recording unconfirmed: {err}. Preserve evidence and retry this cycle ID."]}
    clean = clean and st["clean_cycles"] > 0
    from . import evolve
    evolve.record("sleeptime", evolve.OK if clean else evolve.DEGRADED,
                  f"proposed={total['proposed']} committed={total['committed']} "
                  f"rejected={total['rejected']} clean_cycles={st['clean_cycles']}")
    # Structured memory-rewrite metrics (Phase 3 evolution governance): the
    # cycle's behaviour lands in the queryable evolution log, not only a string.
    evolve.log_event("sleeptime", "cycle", {
        "proposed": total["proposed"], "committed": total["committed"],
        "rejected": total["rejected"], "clean": clean,
        "clean_cycles": st["clean_cycles"], "graduated": graduated(),
        "autoapply": config.sleeptime_autoapply()})
    if not (total["proposed"] or total["committed"] or total["rejected"]):
        return {"status": "dirty", "cycle_id": cycle_id, "lines": [
            "Sleep-time evidence unavailable: " + "; ".join(problems)] if problems else []}
    mode = "auto-apply" if (graduated() and config.sleeptime_autoapply()) \
        else f"supervised ({st['clean_cycles']}/{config.SLEEPTIME_GRADUATION} clean)"
    return {"status": "clean" if clean else "dirty", "cycle_id": cycle_id, "lines": [
        f"Sleep-time memory [{mode}]: proposed {total['proposed']}, "
        f"committed {total['committed']}, rejected {total['rejected']}."]}


def run(settings=None) -> list[str]:
    """Heartbeat compatibility: retain visible failure lines and quiet no-work."""
    return run_result(settings)["lines"]
