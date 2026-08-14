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
    if user:
        return user
    try:
        from . import memory
        return memory.current_user()
    except Exception:
        return "shared"


def _store_dir(user: str) -> Path:
    from . import config, memory
    d = config.MEMORY_DIR / "discovery" / memory.safe_id(user)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gaps_path(user: str) -> Path:
    return _store_dir(user) / "gaps.json"


def _load_gaps(user: str) -> list[dict]:
    try:
        data = json.loads(_gaps_path(user).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_gaps(user: str, gaps: list[dict]) -> None:
    path = _gaps_path(user)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(gaps, indent=2), encoding="utf-8")
    tmp.replace(path)


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
        # Drop oldest resolved first; if all open, drop the oldest open.
        gaps.sort(key=lambda g: (g.get("status") == "open", g.get("created", 0)))
        gaps = gaps[len(gaps) - _MAX_GAPS:]
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
    except Exception:
        pass
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
    """Research a knowledge gap and write the cited result to a durable wiki
    page, marking the gap acquired. Best-effort: with no provider, a degraded
    result, or any failure the gap stays OPEN and a queued note is returned —
    never a raise, and never a degraded page written as if it were knowledge."""
    user = _user(user)
    topic = gap.get("topic", "")
    if not topic:
        return "(skipped: empty topic)"
    try:
        from . import research, wiki
        report = (runner or research.run)(
            f"Give a clear, current, well-sourced explanation of: {topic}")
        if not _is_substantive(report):
            return f"(queued: no substantive result yet for '{topic}')"
        slug = wiki.upsert(user, f"{topic}".strip().capitalize(), str(report),
                           sources="discovery: research", durable=False)
        _set_status(user, gap.get("id", ""), "acquired", ref=slug)
        return f"learned '{topic}' → wiki page '{slug}'"
    except Exception as err:
        return f"(queued: could not research '{topic}' now — {str(err)[:120]})"


def propose_feature(gap: dict, user: str | None = None) -> str:
    """File a capability gap as a structured proposal on the upgrade spine
    (`memory.save('upgrades', …)` — surfaced in the digest and `olympus
    discover`), for the operator to review. Never auto-builds anything."""
    user = _user(user)
    topic = gap.get("topic", "")
    if not topic:
        return "(skipped: empty topic)"
    title = f"Discovery: {topic}"[:120]
    details = (
        f"Olympus's self-discovery loop flagged a capability gap.\n\n"
        f"- **Gap:** {topic}\n"
        f"- **Evidence:** {gap.get('evidence', '(none)')}\n"
        f"- **Seen:** {int(gap.get('hits', 1))} time(s)\n"
        f"- **Source:** {gap.get('source', 'unknown')}\n\n"
        "This is a PROPOSAL for the operator to review — nothing has been "
        "built or changed. If it's worth doing, promote it via the normal "
        "upgrade path (`propose_upgrade` files it upstream).")
    try:
        from . import memory
        prev = memory.current_user()
        try:
            memory.set_user(user)
            path = memory.save("upgrades", title, details)
        finally:
            memory.set_user(prev)
        _set_status(user, gap.get("id", ""), "proposed", ref=str(path))
        return f"proposed feature '{topic}' → {path}"
    except Exception as err:
        return f"(could not file proposal for '{topic}': {str(err)[:120]})"


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
