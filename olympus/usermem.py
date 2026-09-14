"""Durable per-user memory — event log + typed memory projection.

The substrate beneath the profile card: an append-only event log (provenance,
the source of truth) and a typed memory projection the agent reads from. Every
memory is inspectable, user-editable, carries provenance and confidence, and
decays unless reinforced — so the agent's recollection stays honest and current.

Storage uses one validated exact-owner snapshot for events, memories and
candidates. Approval and supersession publish atomically. Normalized legacy
values remain unclaimed and preserved; explicit empty initialization is separate
from migration. File writers serialize on the state directory; Postgres writers
use a transaction-scoped database lock. Native Windows remains single-process
per state directory until M13. Operator/backend validation is required.
"""

from __future__ import annotations

import math
import time
import uuid

from . import memory, store, memory_evidence
from . import owner_evidence as oe

# the nine memory types from the architecture
TYPES = ("identity", "preference", "project", "behavioral", "task",
         "episodic", "procedural", "relationship", "safety")

# decay half-life (days) per type — how fast confidence fades without reuse
HALF_LIFE = {
    "identity": 3650, "preference": 365, "project": 365, "behavioral": 30,
    "task": 365, "episodic": 3650, "procedural": 365, "relationship": 730,
    "safety": 3650,
}

ACTIVE, SUPERSEDED, TOMBSTONED = "active", "superseded", "tombstoned"

_EVENTS, _MEMS, _CANDS = "usermem.events", "usermem.memories", "usermem.candidates"
_MAX_EVENTS = 2000          # cap raw provenance; typed memory is the durable view
_MAX_MEMORIES = 500         # per user; prune the weakest beyond this (cost/bloat)
_MAX_CANDIDATES = 100       # per user; bound the approval queue
_MAX_CONTENT = 600          # per memory; truncate to bound context + storage


def _validate_state(data):
    from . import memory_evidence as me
    oe.fields(data, (_EVENTS, _MEMS, _CANDS))
    for row in me.rows(data[_EVENTS], _MAX_EVENTS):
        me.timestamp(row["ts"])
        oe.text(row["kind"], 256)
        oe.text(row["source"], 256)
        if not isinstance(row["payload"], dict):
            raise ValueError("invalid event payload")
    for row in me.rows(data[_MEMS], _MAX_MEMORIES * 3):
        if row["type"] not in TYPES or row["status"] not in (ACTIVE, SUPERSEDED, TOMBSTONED):
            raise ValueError("invalid memory type or status")
        oe.text(row["content"], _MAX_CONTENT, empty=True)
        me.probability(row["confidence"])
        me.probability(row["importance"])
        oe.number(row["half_life_days"], minimum=1)
        me.timestamp(row["last_used_at"])
        if row.get("created_at") is not None:
            me.timestamp(row["created_at"])
        oe.integer(row["use_count"])
        me.strings(row["provenance"])
        if row.get("key") is not None:
            oe.text(row["key"], 1024, empty=True)
        oe.text(row["sensitivity"], 128)
        if row.get("superseded_by") is not None:
            oe.text(row["superseded_by"], 128)
            if row["superseded_by"] == row["id"]:
                raise ValueError("self supersession")
        if "embedding" in row:
            oe.records(row["embedding"], 16384)
            for number in row["embedding"]:
                oe.number(number, minimum=-1e100)
    for row in me.rows(data[_CANDS], _MAX_CANDIDATES):
        if row["type"] not in TYPES:
            raise ValueError("invalid candidate type")
        oe.text(row["content"], _MAX_CONTENT, empty=True)
        me.timestamp(row["created_at"])
        for field, default in (("confidence", .7), ("importance", .5)):
            me.probability(row.get(field, default))
        me.strings(row.get("provenance", []))
        if row.get("conflicts_with") is not None:
            oe.text(row["conflicts_with"], 128)


def _state(user):
    return memory_evidence.Snapshot(user, "usermem.state.v3",
                                    (_EVENTS, _MEMS, _CANDS), _validate_state)


def _load(ns: str, user: str) -> list:
    return _state(user).load(ns)


def _save(ns: str, user: str, data: list) -> None:
    _state(user).save(ns, data)


def _guard(user: str):
    return _state(user).transaction()


def owners():
    """Enumerate envelope principals, never normalized backend keys."""
    return sorted(oe.namespace_values("usermem.state.v3", lambda user: _state(user).document))


# --- event log -----------------------------------------------------------

def record_event(user: str, kind: str, payload: dict, source: str) -> str:
    with _guard(user):
        events = _load(_EVENTS, user)
        eid = uuid.uuid4().hex[:12]
        events.append({"id": eid, "ts": time.time(), "kind": kind,
                       "payload": payload, "source": source})
        _save(_EVENTS, user, events[-_MAX_EVENTS:])
    return eid


def events(user: str) -> list:
    return _load(_EVENTS, user)


# --- typed memory projection --------------------------------------------

def add_memory(user: str, *, type: str, content: str, confidence: float,
               key: str | None = None, importance: float = 0.5,
               sensitivity: str = "normal", provenance: list | None = None) -> dict:
    if type not in TYPES:
        raise ValueError(f"unknown memory type: {type}")
    now = time.time()
    mem = {
        "id": uuid.uuid4().hex[:12], "type": type, "key": key,
        "content": str(content)[:_MAX_CONTENT], "confidence": round(float(confidence), 3),
        "importance": round(float(importance), 3), "sensitivity": sensitivity,
        "half_life_days": HALF_LIFE.get(type, 365), "status": ACTIVE,
        "superseded_by": None, "provenance": provenance or [],
        "created_at": now, "last_used_at": now, "use_count": 0,
    }
    with _guard(user):
        mems = _load(_MEMS, user)
        mems.append(mem)
        _prune(mems)
        _save(_MEMS, user, mems)
    return mem


def _prune(mems: list) -> None:
    """Keep the store bounded: drop the weakest ACTIVE memories (lowest decayed
    confidence) once past the cap. Non-active rows are kept as history but don't
    count toward the cap and are trimmed first if the list grows huge."""
    active = [m for m in mems if m["status"] == ACTIVE]
    if len(active) <= _MAX_MEMORIES:
        # still trim total bloat from superseded/tombstoned tails
        if len(mems) > _MAX_MEMORIES * 3:
            keep_ids = {m["id"] for m in active}
            inactive = [m for m in mems if m["id"] not in keep_ids]
            drop = set(id(m) for m in inactive[:len(mems) - _MAX_MEMORIES * 3])
            mems[:] = [m for m in mems if id(m) not in drop]
        return
    now = time.time()
    weakest = sorted(active, key=lambda m: effective_confidence(m, now))
    drop_ids = {m["id"] for m in weakest[:len(active) - _MAX_MEMORIES]}
    mems[:] = [m for m in mems if m["id"] not in drop_ids]


def all_memories(user: str) -> list:
    return _load(_MEMS, user)


def active_memories(user: str) -> list:
    return [m for m in _load(_MEMS, user) if m["status"] == ACTIVE]


def get_memory(user: str, mem_id: str) -> dict | None:
    return next((m for m in _load(_MEMS, user) if m["id"] == mem_id), None)


def _mutate(user: str, mem_id: str, fn) -> dict | None:
    with _guard(user):
        mems = _load(_MEMS, user)
        for m in mems:
            if m["id"] == mem_id:
                fn(m)
                _save(_MEMS, user, mems)
                return m
    return None


def reinforce(user: str, mem_id: str, bump: float = 0.05) -> dict | None:
    """Corroborated again — raise confidence toward 1 and refresh recency."""
    def _f(m):
        m["confidence"] = round(min(1.0, m["confidence"] + bump), 3)
        m["last_used_at"] = time.time()
        m["use_count"] += 1
    return _mutate(user, mem_id, _f)


def touch(user: str, mem_id: str) -> None:
    """Mark a memory as used at retrieval time (recency, not confidence)."""
    _mutate(user, mem_id, lambda m: m.__setitem__("last_used_at", time.time()))


def update_content(user: str, mem_id: str, content: str) -> dict | None:
    return _mutate(user, mem_id, lambda m: m.__setitem__("content", content))


def set_embedding(user: str, mem_id: str, vector: list) -> dict | None:
    """Attach a semantic embedding to a memory (used by hybrid retrieval)."""
    return _mutate(user, mem_id, lambda m: m.__setitem__("embedding", vector))


def supersede(user: str, old_id: str, new_mem: dict) -> dict:
    """Supersede only with the owner's already-persisted replacement row."""
    with _guard(user):
        old = get_memory(user, old_id)
        new = get_memory(user, new_mem["id"])
        if old is None or new != new_mem or old_id == new_mem["id"]:
            raise ValueError("supersession needs distinct owner-bound memories")
        if old["status"] == SUPERSEDED and old["superseded_by"] == new["id"]:
            return new
        if old["status"] != ACTIVE or new["status"] != ACTIVE:
            raise ValueError("supersession needs active memories")
        _mutate(user, old_id, lambda m: m.update(
            status=SUPERSEDED, superseded_by=new["id"]))
        return new


def replace_memory(user, old_id, **fields):
    """Publish the new memory and old-memory supersession in one snapshot."""
    with _guard(user):
        old = get_memory(user, old_id)
        if old is None or old["status"] != ACTIVE:
            raise ValueError("replacement source is no longer active")
        new = add_memory(user, **fields)
        return supersede(user, old_id, new)


def approve_candidate(user, cand_id):
    """Consume a candidate only in the same publication as its accepted memory."""
    with _guard(user):
        candidate = next((c for c in candidates(user) if c["id"] == cand_id), None)
        if candidate is None:
            return None
        fields = {name: candidate[name] for name in (
            "type", "content", "confidence", "key", "importance", "sensitivity", "provenance"
        ) if name in candidate}
        fields.setdefault("confidence", .7)
        if candidate.get("conflicts_with"):
            result = replace_memory(user, candidate["conflicts_with"], **fields)
        else:
            result = add_memory(user, **fields)
        if get_memory(user, result["id"]) is None:
            raise ValueError("approved memory would be pruned; candidate preserved")
        pop_candidate(user, cand_id)
        return result


def tombstone(user: str, mem_id: str) -> bool:
    """Forget: hide from retrieval, keep a record that it existed."""
    return _mutate(user, mem_id,
                   lambda m: m.__setitem__("status", TOMBSTONED)) is not None


def effective_confidence(mem: dict, now: float | None = None) -> float:
    """Confidence after time decay since last use (reinforcement resets age)."""
    now = now or time.time()
    age_days = max(0.0, (now - mem.get("last_used_at", mem.get("created_at"))) / 86400)
    half = max(1, mem.get("half_life_days", 365))
    return mem["confidence"] * (0.5 ** (age_days / half))


# --- candidate queue (writes awaiting user approval) ---------------------

def add_candidate(user: str, cand: dict) -> dict:
    cand = dict(cand)
    cand["id"] = uuid.uuid4().hex[:12]
    cand["created_at"] = time.time()
    with _guard(user):
        cands = _load(_CANDS, user)
        cands.append(cand)
        _save(_CANDS, user, cands[-_MAX_CANDIDATES:])   # bound the queue
    return cand


def candidates(user: str) -> list:
    return _load(_CANDS, user)


def pop_candidate(user: str, cand_id: str) -> dict | None:
    with _guard(user):
        cands = _load(_CANDS, user)
        for i, c in enumerate(cands):
            if c["id"] == cand_id:
                cands.pop(i)
                _save(_CANDS, user, cands)
                return c
    return None


def render_card(user: str) -> str:
    """One markdown page of everything Olympus believes about this user —
    WITH receipts: every fact carries its live (decayed) confidence, type, and
    age. The transparency of a plain memory file, without giving up governance:
    this is a projection of the gated store, not an editable source of truth
    (edits go through `olympus memory approve/forget`, which is auditable)."""
    import time as _time
    from . import companion, profile
    lines = [f"# What Olympus knows — {user}", ""]
    prof = (profile.card(user) or "").strip()
    if prof:
        lines += ["## Profile (you told me this)", prof, ""]
    mems = active_memories(user)
    if mems:
        lines.append("## Learned facts (gated, confidence-decayed)")
        now = _time.time()
        for m in sorted(mems, key=lambda m: -effective_confidence(m, now)):
            eff = effective_confidence(m, now)
            created = m.get("created_at")
            # Missing or damaged age evidence is not a newly created memory.
            # Recency affects confidence; it must not replace creation age.
            age = "age unavailable"
            if isinstance(created, (int, float)) and not isinstance(created, bool):
                try:
                    created = float(created)
                except OverflowError:
                    created = math.inf
                if math.isfinite(created):
                    age = f"{int(max(0.0, now - created) / 86400)}d old"
            lines.append(f"- {m['content']}")
            lines.append(f"  `{m.get('type', '?')} · conf {eff:.2f} · "
                         f"{age} · id {m['id']}`")
        lines.append("")
    held = candidates(user)
    if held:
        lines.append("## Held for your review (never auto-committed)")
        for c in held:
            lines.append(f"- {c.get('content', '')[:120]}  "
                         f"`reason: {c.get('reason', '?')} · id {c.get('id', '?')}`")
        lines.append("")
    try:
        comp = (companion.summary(user) or "").strip()
        if comp and "no adaptation" not in comp.lower():
            lines += ["## How I've adapted to you", comp, ""]
    except companion.CompanionStateError as err:
        lines += ["## How I've adapted to you", str(err), ""]
    except Exception:
        pass
    if len(lines) <= 2:
        lines.append("_Nothing learned yet — memory builds as we work._")
    lines += ["---",
              "_Manage: `olympus memory approve|reject|forget <id>` · "
              "this page is a projection of the gated store, not a file to edit._"]
    return "\n".join(lines)
