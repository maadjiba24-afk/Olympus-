"""Per-user adaptive evolution — Olympus gets smarter at working with YOU.

Metis's daily cycle makes the whole council better for *everyone*. This is its
personal counterpart: the more a given person uses Olympus, the more it adapts
**to them**. Every few exchanges it distills that user's own history —
preferences, recurring goals, domain, communication style, and the corrections
they've made — into a compact, evolving *working model* that is injected into
every answer for that user (and nobody else: it is per-user and private). A
visible **growth level** lets the user feel the relationship deepening.

This is deliberately higher-order than `recall` (which stores atomic facts): the
working model is a short, synthesized "how to work well with this person" brief,
rewritten in place as evidence accumulates — so the system measurably tailors
itself the longer you use it.
"""

from __future__ import annotations

import json
import os
import threading
import time

from . import config, memory, usermem

# Serializes read-modify-write of the per-user companion state so concurrent
# turns (web + Telegram, two tabs) don't lose an exchange increment or let a
# background evolve() clobber it — which would also skip an EVOLVE_EVERY
# checkpoint and silently stop the per-user self-improvement.
_LOCK = threading.Lock()

# Re-distill the working model every N exchanges (per user). An "exchange" is
# one user↔Olympus turn — note_interaction() is called once per completed turn.
EVOLVE_EVERY = int(os.environ.get("OLYMPUS_EVOLVE_EVERY", "6"))
MODEL_MAX_CHARS = 1400        # keep the injected brief small

# Growth tiers by lifetime interaction count — what the user sees deepening.
_LEVELS = [
    (0, "new", "just getting started"),
    (3, "acquainted", "learning your basics"),
    (10, "familiar", "knows your preferences"),
    (30, "attuned", "anticipates your needs"),
    (100, "trusted companion", "deeply tailored to you"),
]


def _path(user: str):
    d = config.MEMORY_DIR / "companion"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{memory.safe_id(user)}.json"


def load(user: str) -> dict:
    p = _path(user)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"interactions": 0, "evolutions": 0, "model": "", "updated": 0.0}


def save(user: str, state: dict) -> None:
    _path(user).write_text(json.dumps(state, indent=2), encoding="utf-8")


def note_interaction(user: str, now: float | None = None) -> int:
    """Count one exchange (a completed user↔Olympus turn) for this user; return
    the new total. Atomic across concurrent turns."""
    with _LOCK:
        state = load(user)
        state["interactions"] = int(state.get("interactions", 0)) + 1
        save(user, state)
        return state["interactions"]


def due(count: int) -> bool:
    return EVOLVE_EVERY > 0 and count > 0 and count % EVOLVE_EVERY == 0


def growth_level(count: int) -> tuple[str, str]:
    name, desc = _LEVELS[0][1], _LEVELS[0][2]
    for threshold, n, d in _LEVELS:
        if count >= threshold:
            name, desc = n, d
    return name, desc


def model_block(user: str) -> str:
    """Compact working-model brief for injection into this user's context.
    Empty string when nothing has been learned yet."""
    model = (load(user).get("model") or "").strip()
    if not model:
        return ""
    return ("\n\n## What you've learned about working with this user "
            "(adapt to it; it is private to them)\n" + model)


_SYNTH_SYSTEM = (
    "You maintain a concise WORKING MODEL of how to collaborate well with one "
    "specific user, so an assistant can tailor its help to them. Capture only "
    "durable, useful patterns: their domain and goals, recurring tasks, "
    "communication/style preferences, expertise level, and concrete do's and "
    "don'ts learned from their feedback and corrections. Be specific and brief. "
    "Output the UPDATED model as short bullet points — no preamble. If the new "
    "evidence adds nothing, return the existing model unchanged."
)


def evolve(user: str, settings: config.Settings | None = None) -> str:
    """Re-distill this user's working model from their own recent history.
    Returns the updated model text. Best-effort: returns the prior model on any
    failure so a flaky call never wipes accumulated learning."""
    from . import backend
    settings = settings or config.Settings.from_env()
    memory.set_user(user)
    state = load(user)
    prior = state.get("model", "")

    mems = usermem.active_memories(user)
    facts = "\n".join(f"- ({m['type']}) {m['content']}" for m in mems[:30]) or "(none yet)"
    feedback = memory.recent("feedback", 8)
    corrections = memory.recent("corrections", 5)

    prompt = (
        f"## Current working model\n{prior or '(empty — first time)'}\n\n"
        f"## Durable facts known about this user\n{facts}\n\n"
        f"## Recent feedback they gave (👍/👎)\n{feedback or '(none)'}\n\n"
        f"## Recent corrections made to answers\n{corrections or '(none)'}\n\n"
        "Update the working model from this evidence. Keep it tight."
    )
    try:
        model = backend.complete_text(settings, _SYNTH_SYSTEM,
                                      [{"role": "user", "content": prompt}],
                                      effort="low").strip()
    except Exception:
        return prior
    if not model:
        return prior
    # Re-read under the lock and merge only the model fields, so a concurrent
    # note_interaction()'s exchange increment isn't clobbered by this (slow,
    # off-thread) evolution. The model call itself stays outside the lock.
    with _LOCK:
        state = load(user)
        state["model"] = model[:MODEL_MAX_CHARS]
        state["evolutions"] = int(state.get("evolutions", 0)) + 1
        state["updated"] = now_ts()
        save(user, state)
        return state["model"]


def maybe_evolve(user: str, count: int,
                 settings: config.Settings | None = None) -> bool:
    """Evolve the model if this interaction count is a checkpoint. Returns
    whether an evolution ran. Intended to be called in the background."""
    if not due(count):
        return False
    try:
        evolve(user, settings)
        return True
    except Exception:
        return False


def now_ts() -> float:
    return time.time()


def summary(user: str) -> str:
    """Human-readable growth view for `olympus growth`."""
    state = load(user)
    count = int(state.get("interactions", 0))
    name, desc = growth_level(count)
    lines = [f"Relationship: {name} — {desc}",
             f"  exchanges: {count}",
             f"  model updates: {state.get('evolutions', 0)}"]
    model = (state.get("model") or "").strip()
    if model:
        lines.append("\nWhat Olympus has learned about working with you:")
        lines.append(model)
    else:
        nxt = EVOLVE_EVERY - (count % EVOLVE_EVERY) if EVOLVE_EVERY else 0
        lines.append(f"\n(Keep chatting — Olympus adapts to you every "
                     f"{EVOLVE_EVERY} exchanges; next update in ~{nxt}.)")
    return "\n".join(lines)
