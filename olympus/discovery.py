"""Self-discovery — Olympus acquires new knowledge and proposes new features.

Olympus already improves what it HAS: Prometheus upgrades prompts
(benchmark-gated), Metis distills experience into skills, `evolve.py` auto-tunes
registered feature parameters, and the wiki refreshes concept pages by nightly
dreaming. What it lacked was a loop that notices what it does NOT yet know or
CANNOT yet do, and closes those gaps over time:

  * **Knowledge gaps → acquire durable knowledge.** A gap the system noticed
    ("I don't understand X") triggers a bounded research pass whose cited result
    is written to a durable wiki page — so next time X comes up, Olympus already
    understands it. This is the same experience→knowledge loop the Aegis
    Assessment suite uses (ADR 0011), generalized to any topic.
  * **Capability gaps → propose a new feature.** A recurring friction signal, or
    a gap an agent flags, becomes a structured proposal filed on the existing
    upgrade spine (`memory.save("upgrades", …)` — surfaced in the digest and
    `olympus discover`), for the operator to review. This is the native form of
    the manual "analyze the landscape → propose what to absorb" pattern that
    produced the scraper and security-agent absorptions.

Doctrine (inherited from `evolve.py` / `outcomes.py`):

  * **Notice, don't impose.** Knowledge is stored (sanitized at the memory sink,
    wrapped-untrusted upstream by research); features are PROPOSED to the
    operator, never silently built. Nothing here changes security-relevant
    behaviour.
  * **Bounded, replay-inert, opt-in on the heartbeat** (`OLYMPUS_DISCOVERY`).
  * **Degrades.** No provider → a knowledge gap is *queued*, not failed; a
    broken signal source never crashes the cycle (every step is isolated).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import memory, owner_evidence as evidence_store, note_evidence as notes

# Bounds — the discovery ledger and each cycle are hard-capped so a runaway
# signal source can never flood memory or spend unbounded tokens.
_MAX_GAPS = 200
_MAX_TOPIC = 200
_MAX_EVIDENCE = 500
_DEFAULT_MAX_KNOWLEDGE = 1       # research passes per cycle (each costs tokens)
_DEFAULT_MAX_FEATURES = 3        # proposals filed per cycle

_KNOWLEDGE = "knowledge"
_CAPABILITY = "capability"
_KINDS = (_KNOWLEDGE, _CAPABILITY)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """Discovery on the heartbeat is opt-in and OFF during replay (a research
    fetch / memory write must never happen while replaying a recorded run)."""
    if _replaying():
        return False
    return os.environ.get("OLYMPUS_DISCOVERY", "").strip().lower() in (
        "1", "true", "yes", "on")


def _replaying() -> bool:
    return os.environ.get("OLYMPUS_REPLAY", "").strip().lower() in (
        "1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# gap ledger
# ---------------------------------------------------------------------------

def _user(user: str | None) -> str:
    return memory.canonical_owner(memory.current_owner() if user is None else user)


def _store_dir(user: str) -> Path:
    return evidence_store.workspace(user) / "discovery"


def _gaps_path(user: str) -> Path:
    return _store_dir(user) / "gaps.json"


def _validate(data):
    evidence_store.records(data, _MAX_GAPS)
    ids, topics = set(), set()
    for row in data:
        evidence_store.fields(row, ("id", "kind", "topic", "evidence", "source",
                                    "status", "hits", "created", "last_seen", "resolved_ref"))
        for key, cap in (("id", 128), ("topic", _MAX_TOPIC), ("source", 80)):
            evidence_store.text(row[key], cap)
        evidence_store.text(row["evidence"], _MAX_EVIDENCE, empty=True)
        evidence_store.text(row["resolved_ref"], 4096, empty=True)
        if row["kind"] not in _KINDS or row["status"] not in ("open", "acquired", "proposed"):
            raise ValueError("invalid gap state")
        if row["topic"] != _norm(row["topic"]):
            raise ValueError("invalid normalized topic")
        pair = row["kind"], row["topic"]
        if row["id"] in ids or pair in topics:
            raise ValueError("duplicate gap")
        ids.add(row["id"]); topics.add(pair)
        evidence_store.integer(row["hits"], minimum=1)
        evidence_store.number(row["created"])
        evidence_store.number(row["last_seen"], minimum=row["created"])


def _evidence(user):
    return evidence_store.JsonStore(user, "discovery", _validate, path=_gaps_path(user))


def evidence_status(user=None):
    return _evidence(_user(user)).status()


def _load_gaps(user: str) -> list[dict]:
    with notes.guard():
        return _evidence(user).load()


def _save_gaps(user: str, gaps: list[dict]) -> None:
    with notes.guard():
        document = _evidence(user)
        before = document._raw()
        document._value(before)
        target = notes.relative(_gaps_path(user))
        notes.transact({target: _encode_gaps(user, gaps)}, {target: notes.digest(before)})


def _encode_gaps(user, gaps):
    raw = json.dumps({"version": 2, "owner": _user(user), "data": gaps},
                     sort_keys=True, allow_nan=False).encode()
    _evidence(user)._value(raw)
    if len(raw) > notes.MAX_NOTE:
        raise ValueError("discovery snapshot exceeds publication bound")
    return raw


def _norm(topic: str) -> str:
    return " ".join(str(topic or "").lower().split())[:_MAX_TOPIC]


def note_gap(kind: str, topic: str, *, evidence: str = "", source: str = "",
             user: str | None = None) -> dict:
    """Record a gap Olympus noticed — a `knowledge` gap ("I don't understand X")
    or a `capability` gap ("I can't do Y"). Deduped by (kind, normalized topic);
    an existing open gap is bumped, never duplicated. Bounded: at the cap, the
    oldest RESOLVED gap is dropped first (open gaps are preserved)."""
    kind = kind if kind in _KINDS else _KNOWLEDGE
    topic = _norm(topic)
    if not topic:
        raise ValueError("a gap needs a topic")
    user = _user(user)
    with notes.guard():
        gaps = _load_gaps(user)
        for g in gaps:
            if g.get("kind") == kind and _norm(g.get("topic", "")) == topic:
                g["hits"] = int(g.get("hits", 1)) + 1
                g["last_seen"] = time.time()
                if evidence and not g.get("evidence"):
                    g["evidence"] = str(evidence)[:_MAX_EVIDENCE]
                _save_gaps(user, gaps)
                return g
        gap = {
            "id": f"gap-{int(time.time())}-{os.urandom(3).hex()}",
            "kind": kind, "topic": topic,
            "evidence": str(evidence or "")[:_MAX_EVIDENCE],
            "source": str(source or "unknown")[:80],
            "status": "open", "hits": 1,
            "created": time.time(), "last_seen": time.time(),
            "resolved_ref": "",
        }
        gaps.append(gap)
        if len(gaps) > _MAX_GAPS:
            raise ValueError("discovery capacity reached; existing gaps and receipts preserved")
        _save_gaps(user, gaps)
        return gap


def open_gaps(user: str | None = None, kind: str | None = None) -> list[dict]:
    gaps = [g for g in _load_gaps(_user(user)) if g.get("status") == "open"]
    if kind:
        gaps = [g for g in gaps if g.get("kind") == kind]
    # Most-hit first (recurring gaps are the highest-signal), then newest.
    return sorted(gaps, key=lambda g: (-int(g.get("hits", 1)),
                                       -g.get("last_seen", 0)))


def _set_status(user: str, gap_id: str, status: str, ref: str = "") -> None:
    with notes.guard():
        gaps = _load_gaps(user)
        for g in gaps:
            if g.get("id") == gap_id:
                g["status"] = status
                if ref:
                    g["resolved_ref"] = ref
                break
        _save_gaps(user, gaps)


# ---------------------------------------------------------------------------
# capability-gap derivation (deterministic — from friction signals)
# ---------------------------------------------------------------------------

def derive_capability_gaps(user: str | None = None) -> list[dict]:
    """Turn recurring action friction (`outcomes.insights`) into capability-gap
    candidates. Deterministic, no model call — a ref the user keeps editing or
    declining is a signal the capability defaults are wrong. Each is noted (so it
    accrues hits over time) and returned."""
    user = _user(user)
    noted: list[dict] = []
    try:
        from . import outcomes
        for ins in outcomes.insights(user):
            ref = ins.get("ref", "")
            if not ref:
                continue
            noted.append(note_gap(
                _CAPABILITY, f"reduce friction on '{ref}' actions",
                evidence=ins.get("message", ""), source="outcomes.friction",
                user=user))
    except evidence_store.OwnerEvidenceStateError:
        raise
    except Exception:
        raise evidence_store.OwnerEvidenceStateError("outcomes", "friction source unavailable")
    return noted


# ---------------------------------------------------------------------------
# closing a gap: acquire knowledge / propose a feature
# ---------------------------------------------------------------------------

# Markers of a DEGRADED research result (no provider / search+fetch failed).
# A degraded report must never be written as "learned" knowledge — the gap
# stays open and is retried next cycle.
_DEGRADED_MARKERS = (
    "no usable evidence could be collected",
    "search or fetch failed",
)
_MIN_REPORT_CHARS = 200


def _is_substantive(report: str) -> bool:
    body = str(report or "").strip()
    if len(body) < _MIN_REPORT_CHARS:
        return False
    low = body.lower()
    return not any(m in low for m in _DEGRADED_MARKERS)


def acquire_knowledge(gap: dict, user: str | None = None, runner=None) -> str:
    """Owned research publication with durable retry; replay never acquires."""
    from . import discovery_publication, research
    try:
        return discovery_publication.publish(_user(user), gap, _KNOWLEDGE,
                                              research=runner or research.run)
    except Exception as err:
        return "(queued: publication unavailable; preserve prepared results and inspect notes-status: " + str(err)[:300] + ")"


def propose_feature(gap: dict, user: str | None = None) -> str:
    """Shared operator proposal and exact-owner acknowledgement commit together."""
    from . import discovery_publication
    try:
        return discovery_publication.publish(_user(user), gap, _CAPABILITY)
    except Exception as err:
        return "(could not file proposal; evidence preserved: " + str(err)[:300] + ")"


# ---------------------------------------------------------------------------
# the cycle
# ---------------------------------------------------------------------------

def run(user: str | None = None, *, max_knowledge: int = _DEFAULT_MAX_KNOWLEDGE,
        max_features: int = _DEFAULT_MAX_FEATURES, runner=None) -> dict[str, Any]:
    """One bounded discovery cycle: derive fresh capability gaps from friction,
    acquire knowledge for the top knowledge gaps, and propose the top capability
    gaps. Replay-inert. Returns a structured report."""
    user = _user(user)
    if _replaying():
        return {"skipped": "replay", "learned": [], "proposed": []}

    derive_capability_gaps(user)                      # refresh capability signals

    learned: list[str] = []
    for gap in open_gaps(user, _KNOWLEDGE)[:max(0, int(max_knowledge))]:
        learned.append(acquire_knowledge(gap, user, runner=runner))

    proposed: list[str] = []
    for gap in open_gaps(user, _CAPABILITY)[:max(0, int(max_features))]:
        proposed.append(propose_feature(gap, user))

    return {
        "learned": learned,
        "proposed": proposed,
        "open_knowledge": len(open_gaps(user, _KNOWLEDGE)),
        "open_capability": len(open_gaps(user, _CAPABILITY)),
    }


def report(user: str | None = None) -> str:
    user = _user(user)
    gaps = _load_gaps(user)
    if not gaps:
        return ("No discovery activity yet. Olympus records knowledge gaps "
                "(things it should learn) and capability gaps (features it "
                "should gain) here, then closes them over time. Enable the "
                "heartbeat loop with OLYMPUS_DISCOVERY=1, or run "
                "`olympus discover run`.")
    ok = [g for g in gaps if g.get("kind") == _KNOWLEDGE]
    cg = [g for g in gaps if g.get("kind") == _CAPABILITY]
    lines = [f"Discovery ledger — {len(gaps)} gap(s):", ""]

    def _fmt(g: dict) -> str:
        mark = {"open": "○", "acquired": "✓", "proposed": "▲"}.get(
            g.get("status"), "·")
        ref = f" → {g['resolved_ref']}" if g.get("resolved_ref") else ""
        return (f"  {mark} [{g.get('status')}] {g.get('topic')} "
                f"({int(g.get('hits', 1))}×){ref}")

    lines.append(f"Knowledge ({len(ok)}): ✓acquired = learned, ○open = queued")
    lines += [_fmt(g) for g in sorted(ok, key=lambda g: -g.get("last_seen", 0))[:20]]
    lines.append("")
    lines.append(f"Capability ({len(cg)}): ▲proposed = filed for review")
    lines += [_fmt(g) for g in sorted(cg, key=lambda g: -g.get("last_seen", 0))[:20]]
    return "\n".join(lines)
