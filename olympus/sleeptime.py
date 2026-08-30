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
from typing import Any, Callable

from . import config, memory, store, usermem

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
        body = "\n".join(f"- {m.get('content', '')}" for m in sources)
        return backend.complete_text(
            s, _GEN_SYSTEM, [{"role": "user", "content": body[:4000]}],
            effort="low").strip()[:usermem._MAX_CONTENT]

    return generate


def default_verifier(settings) -> VerifyFn:
    from . import backend
    s = settings or config.Settings.from_env()

    def verify(sources: list[dict], rewrite: str) -> dict:
        body = ("SOURCE memories:\n"
                + "\n".join(f"- {m.get('content', '')}" for m in sources)
                + f"\n\nCONSOLIDATED memory:\n{rewrite}")
        out = backend.complete_json(
            s, _VERIFY_SYSTEM, [{"role": "user", "content": body[:6000]}],
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
    confidence: float = 1.0             # Aletheia's numeric confidence in the rewrite

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "id", "user", "type", "source_ids", "source_contents", "rewrite",
            "provenance", "sensitivity", "verified", "unsupported", "applied",
            "created_at", "confidence")}


def _load(ns: str, key: str) -> Any:
    blob = store.backend().get(ns, key)
    if not blob:
        return None
    try:
        return json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return None


def _save(ns: str, key: str, data: Any) -> None:
    store.backend().put(ns, key, json.dumps(data).encode("utf-8"))


def state() -> dict:
    st = _load(_STATE_NS, "state") or {}
    return {"clean_cycles": int(st.get("clean_cycles", 0)),
            "runs": int(st.get("runs", 0)),
            "last_run": float(st.get("last_run", 0.0)),
            "committed": int(st.get("committed", 0)),
            "proposed": int(st.get("proposed", 0))}


def graduated() -> bool:
    """True once the loop has logged enough clean supervised cycles to be
    trusted with auto-apply (still gated by config.sleeptime_autoapply)."""
    return state()["clean_cycles"] >= config.SLEEPTIME_GRADUATION


def block_mode_active() -> bool:
    """Whether Aletheia BLOCK-MODE is live: it activates only once the loop has
    GRADUATED (10 clean supervised cycles) and a confidence floor is configured.
    Before that it is inert — Aletheia annotates but never rejects a rewrite."""
    return graduated() and config.SLEEPTIME_CONFIDENCE_MIN > 0.0


def _record_cycle(clean: bool, proposed: int, committed: int) -> dict:
    st = state()
    st["runs"] += 1
    st["last_run"] = time.time()
    st["proposed"] += proposed
    st["committed"] += committed
    # A clean cycle advances the streak; any Aletheia rejection resets it — the
    # loop must re-earn trust from zero after a bad rewrite.
    st["clean_cycles"] = st["clean_cycles"] + 1 if clean else 0
    _save(_STATE_NS, "state", st)
    return st


def proposals(user: str) -> list[dict]:
    return _load(_PROP_NS, memory.safe_id(user)) or []


def _add_proposal(p: Proposal) -> None:
    key = memory.safe_id(p.user)
    cur = _load(_PROP_NS, key) or []
    cur.append(p.to_dict())
    _save(_PROP_NS, key, cur[-_MAX_PROPOSALS_PER_RUN * 5:])


def _quarantine(user: str, p: Proposal, reason: str) -> None:
    """Hold a rewrite the memory.rewrite contract refused (e.g. Aletheia
    block-mode's confidence floor) for operator review — persisted in the
    quarantine namespace AND as a witness-signed delta-substrate record, so the
    near-miss is tamper-evident. Best-effort: never breaks the commit path."""
    try:
        key = memory.safe_id(user)
        entry = {**p.to_dict(), "reason": reason}
        cur = _load(_QUAR_NS, key) or []
        cur.append(entry)
        _save(_QUAR_NS, key, cur[-_MAX_SNAPSHOTS:])
        from . import deltas
        deltas.record_snapshot(
            f"quarantine:{key}", kind="quarantine",
            state={"proposal": p.to_dict(), "reason": reason},
            provenance=deltas.Provenance(
                source="sleeptime", trust="user",
                detail=f"block-mode rejected rewrite {p.id} "
                       f"(sensitivity={p.sensitivity})"))
    except Exception as err:
        from . import errors
        errors.capture("sleeptime.quarantine", err)


def quarantined(user: str) -> list[dict]:
    """Rewrites rejected by the memory.rewrite contract, held for review."""
    return _load(_QUAR_NS, memory.safe_id(user)) or []


def _snapshot(user: str, sources: list[dict], rewrite_id: str | None) -> str:
    """Append an immutable pre-rewrite snapshot; returns its id (for revert)."""
    key = memory.safe_id(user)
    snaps = _load(_SNAP_NS, key) or []
    sid = uuid.uuid4().hex[:12]
    snaps.append({
        "id": sid, "ts": time.time(), "rewrite_id": rewrite_id,
        "sources": [{"id": m["id"], "content": m.get("content", ""),
                     "status": m.get("status"),
                     "sensitivity": m.get("sensitivity"),
                     "type": m.get("type"),
                     "provenance": list(m.get("provenance") or [])}
                    for m in sources],
    })
    _save(_SNAP_NS, key, snaps[-_MAX_SNAPSHOTS:])
    return sid


def snapshots(user: str) -> list[dict]:
    return _load(_SNAP_NS, memory.safe_id(user)) or []


# --- the refinement cycle -------------------------------------------------

def _commit(user: str, sources: list[dict], p: Proposal) -> str | None:
    """Apply a verified rewrite under the memory.rewrite contract: snapshot →
    add the consolidated memory → supersede the sources. Returns the new memory
    id, or None if the contract blocked the commit."""
    from . import behavioral_contracts as abc
    ctx = {
        "source_provenance": _merged_provenance(sources),
        "new_provenance": p.provenance,
        "source_sensitivities": [m.get("sensitivity", "normal") for m in sources],
        "new_sensitivity": p.sensitivity,
        "source_type": p.type, "new_type": p.type,
        "verify_supported": p.verified,
        "unsupported_claims": p.unsupported,
        # Aletheia block-mode: reject a below-confidence rewrite once graduated.
        "block_mode_active": block_mode_active(),
        "rewrite_confidence": p.confidence,
        "confidence_threshold": config.SLEEPTIME_CONFIDENCE_MIN,
    }
    try:
        abc.enforce("memory.rewrite", ctx)
    except abc.ContractViolation as violation:
        # A blocked rewrite is not silently dropped: quarantine it (signed,
        # inspectable) so an operator can review the near-miss. The existing
        # memories are left untouched — consolidation is the only rewriter.
        _quarantine(user, p, str(violation))
        return None
    sid = _snapshot(user, sources, None)
    new = usermem.add_memory(
        user, type=p.type, content=p.rewrite,
        confidence=max((m.get("confidence", 0.5) for m in sources), default=0.5),
        importance=max((m.get("importance", 0.5) for m in sources), default=0.5),
        sensitivity=p.sensitivity, provenance=p.provenance)
    for m in sources:
        usermem.supersede(user, m["id"], new)
    # link the snapshot to the memory it produced, so revert can undo the commit
    _relink_snapshot(user, sid, new["id"])
    return new["id"]


def _relink_snapshot(user: str, sid: str, rewrite_id: str) -> None:
    key = memory.safe_id(user)
    snaps = _load(_SNAP_NS, key) or []
    for s in snaps:
        if s.get("id") == sid:
            s["rewrite_id"] = rewrite_id
            break
    _save(_SNAP_NS, key, snaps)


def refine_user(user: str, *, settings=None,
                generator: GenerateFn | None = None,
                verifier: VerifyFn | None = None,
                auto_apply: bool | None = None) -> dict:
    """One refinement cycle for one user. Returns a summary. Never raises into
    the caller. `auto_apply` overrides the config gate (tests set it True)."""
    summary = {"groups": 0, "proposed": 0, "committed": 0,
               "rejected": 0, "clean": True}

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

        # Aletheia's numeric confidence in the rewrite — from the verdict if it
        # gives one, else derived from the support verdict (a supported rewrite
        # with no explicit score is treated as confident).
        if "confidence" not in verdict:
            confidence = 1.0 if supported else 0.0
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
            rewrite=clean_rewrite,
            provenance=_merged_provenance(grp),
            sensitivity=_rank_sensitivity(grp),
            verified=supported,
            unsupported=list(raw_unsupported),
            created_at=time.time(), confidence=confidence)
        if not supported:
            summary["rejected"] += 1
            summary["clean"] = False        # Aletheia rejection breaks the streak
            _add_proposal(p)                 # kept for the operator to inspect
            continue
        if auto_apply:
            new_id = _commit(user, grp, p)
            if new_id:
                p.applied = True
                summary["committed"] += 1
            else:
                fail("memory rewrite contract rejected")
        _add_proposal(p)
        summary["proposed"] += 1
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
    """Undo a committed rewrite: reactivate the source memories from the snapshot
    and tombstone the memory the rewrite produced. Returns False if the snapshot
    is unknown."""
    for s in snapshots(user):
        if s.get("id") != snapshot_id:
            continue
        for src in s.get("sources", []):
            usermem._mutate(user, src["id"], lambda m: (
                m.__setitem__("status", usermem.ACTIVE),
                m.__setitem__("superseded_by", None)))
        rid = s.get("rewrite_id")
        if rid:
            usermem.tombstone(user, rid)
        return True
    return False


def run(settings=None) -> list[str]:
    """The heartbeat entry: refine every user with memory. No-op unless enabled.
    Returns human log lines (empty when nothing happened — a nightly job must not
    spam the heartbeat)."""
    if not config.sleeptime_enabled():
        return []
    try:
        users = store.backend().keys(usermem._MEMS)
    except Exception as err:
        from . import errors
        errors.capture("sleeptime.enumerate", err)
        return []
    total = {"proposed": 0, "committed": 0, "rejected": 0}
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
    st = _record_cycle(clean, total["proposed"], total["committed"])
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
        return []
    mode = "auto-apply" if (graduated() and config.sleeptime_autoapply()) \
        else f"supervised ({st['clean_cycles']}/{config.SLEEPTIME_GRADUATION} clean)"
    return [f"Sleep-time memory [{mode}]: proposed {total['proposed']}, "
            f"committed {total['committed']}, rejected {total['rejected']}."]
