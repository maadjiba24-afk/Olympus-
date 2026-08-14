"""Durable per-user memory — event log + typed memory projection.

The substrate beneath the profile card: an append-only event log (provenance,
the source of truth) and a typed memory projection the agent reads from. Every
memory is inspectable, user-editable, carries provenance and confidence, and
decays unless reinforced — so the agent's recollection stays honest and current.

Storage rides the existing `store` backend (local files by default, Postgres
when OLYMPUS_DATABASE_URL is set). To stay backend-agnostic over a plain kv
interface, each user's events / memories / candidates are single JSON documents,
and every load-modify-save runs under `_guard()` — a thread lock AND a
`proclock` flock, so the web server and the heartbeat mutating the same user
concurrently cannot lose each other's writes on the default file backend.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid

from . import memory, store

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

_LOCK = threading.Lock()
_EVENTS, _MEMS, _CANDS = "usermem.events", "usermem.memories", "usermem.candidates"
_MAX_EVENTS = 2000          # cap raw provenance; typed memory is the durable view
_MAX_MEMORIES = 500         # per user; prune the weakest beyond this (cost/bloat)
_MAX_CANDIDATES = 100       # per user; bound the approval queue
_MAX_CONTENT = 600          # per memory; truncate to bound context + storage


def _load(ns: str, user: str) -> list:
    blob = store.backend().get(ns, memory.safe_id(user))
    if not blob:
        return []
    try:
        return json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return []


def _save(ns: str, user: str, data: list) -> None:
    store.backend().put(ns, memory.safe_id(user),
                        json.dumps(data).encode("utf-8"))


@contextlib.contextmanager
def _guard(user: str):
    """Serialize one user's load-modify-save across threads AND processes.

    Every mutation here is a whole-document RMW over a `store` key, and a blind
    `put` is defined as replace-whole-value (ADR 0005) — so two concurrent
    writers do not merge, the loser's entire memory list is discarded. A
    `threading.Lock` alone only ordered writers *inside one interpreter*, while
    the shipped topology runs the web server and the heartbeat as separate
    processes against the same MEMORY_DIR: the heartbeat's goal cycle extracts
    memories for the same user the web process is talking to. That is a silent
    lost update of durable user data, which is why the module docstring's
    "multi-process deployments should use the DB" was never an adequate answer —
    the file backend is the default.

    `prefs.set` already established the fix: hold `proclock` (an flock, so it is
    honored across processes) around the read+write, not just a thread lock."""
    from . import proclock
    with _LOCK, proclock.lock(f"usermem-{memory.safe_id(user)}"):
        yield


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
    """Replace a memory with a newer one, keeping the old as history."""
    _mutate(user, old_id, lambda m: (m.__setitem__("status", SUPERSEDED),
                                     m.__setitem__("superseded_by", new_mem["id"])))
    return new_mem


def tombstone(user: str, mem_id: str) -> bool:
    """Forget: hide from retrieval, keep a record that it existed."""
    return _mutate(user, mem_id,
                   lambda m: m.__setitem__("status", TOMBSTONED)) is not None


def effective_confidence(mem: dict, now: float | None = None) -> float:
    """Confidence after time decay since last use (reinforcement resets age)."""
    now = now or time.time()
    age_days = max(0.0, (now - mem.get("last_used_at", mem["created_at"])) / 86400)
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
            age_d = int((now - float(m.get("created", now))) / 86400)
            lines.append(f"- {m['content']}")
            lines.append(f"  `{m.get('type', '?')} · conf {eff:.2f} · "
                         f"{age_d}d old · id {m['id']}`")
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
    except Exception:
        pass
    if len(lines) <= 2:
        lines.append("_Nothing learned yet — memory builds as we work._")
    lines += ["---",
              "_Manage: `olympus memory approve|reject|forget <id>` · "
              "this page is a projection of the gated store, not a file to edit._"]
    return "\n".join(lines)
