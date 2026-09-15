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

import contextlib
import hashlib
import json
import math
import os
import threading
import time

from . import atomicio, config, memory, proclock, usermem

# Serializes read-modify-write of the per-user companion state so concurrent
# turns (web + Telegram, two tabs) don't lose an exchange increment or let a
# background evolve() clobber it — which would also skip an EVOLVE_EVERY
# checkpoint and silently stop the per-user self-improvement.
_LOCK = threading.Lock()

# Re-distill the working model every N exchanges (per user). An "exchange" is
# one user↔Olympus turn — note_interaction() is called once per completed turn.
EVOLVE_EVERY = int(os.environ.get("OLYMPUS_EVOLVE_EVERY", "6"))
MODEL_MAX_CHARS = 1400        # keep the injected brief small
_MAX_STATE_BYTES = 64 * 1024  # > worst-case JSON escaping of MODEL_MAX_CHARS
_STATE_KEYS = frozenset({"interactions", "evolutions", "model", "updated"})
_QUARANTINE_DIGEST_HEX = 16

# Growth tiers by lifetime interaction count — what the user sees deepening.
_LEVELS = [
    (0, "new", "just getting started"),
    (3, "acquainted", "learning your basics"),
    (10, "familiar", "knows your preferences"),
    (30, "attuned", "anticipates your needs"),
    (100, "trusted companion", "deeply tailored to you"),
]


class CompanionStateError(RuntimeError):
    """The exact owner's adaptive working-model evidence is unavailable.

    Missing state is valid first use. Existing bytes that cannot be read and
    validated are different: treating them as empty would both erase evidence
    on the next write and silently change the private model injected into every
    answer.
    """

    def __init__(self, user: str, reason: str) -> None:
        self.user = memory.canonical_owner(user)
        self.reason = str(reason)
        self.repair_command = (
            "olympus growth --evidence --repair --owner <exact-owner>"
        )
        super().__init__(
            "Companion-model evidence is unavailable "
            f"({self.reason}). Private-model reads and writes are refused; "
            "preserve the stored bytes for operator inspection."
        )


def _default_state() -> dict:
    return {"interactions": 0, "evolutions": 0, "model": "", "updated": 0.0}


def _dir():
    d = config.MEMORY_DIR / "companion"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(user: str):
    """Collision-resistant path for one exact owner."""
    return _dir() / f"{memory.storage_key(user)}.json"


def _legacy_path(user: str):
    """The pre-P2T lossy path, preserved but never implicitly claimed."""
    return _dir() / f"{memory.safe_id(memory.canonical_owner(user))}.json"


def _duplicate_rejecting_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _validate_state(user: str, data: object) -> dict:
    if not isinstance(data, dict):
        raise CompanionStateError(user, "root is not an object")
    if set(data) != _STATE_KEYS:
        raise CompanionStateError(user, "state keys do not match the schema")

    for field in ("interactions", "evolutions"):
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CompanionStateError(
                user, f"{field} is not a non-negative integer")

    model = data["model"]
    if not isinstance(model, str) or len(model) > MODEL_MAX_CHARS:
        raise CompanionStateError(user, "model is not a bounded string")

    updated = data["updated"]
    try:
        updated_value = float(updated)
    except (OverflowError, TypeError, ValueError) as err:
        raise CompanionStateError(
            user, "updated is not a finite timestamp") from err
    if (isinstance(updated, bool) or not isinstance(updated, (int, float))
            or not math.isfinite(updated_value) or updated_value < 0):
        raise CompanionStateError(user, "updated is not a finite timestamp")

    return {
        "interactions": data["interactions"],
        "evolutions": data["evolutions"],
        "model": model,
        "updated": updated_value,
    }


def _decode_state(user: str, raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8")
    except (AttributeError, UnicodeDecodeError) as err:
        raise CompanionStateError(user, "state is not valid UTF-8") from err
    try:
        data = json.loads(text, object_pairs_hook=_duplicate_rejecting_object)
    except (json.JSONDecodeError, ValueError) as err:
        raise CompanionStateError(user, "state is malformed JSON") from err
    return _validate_state(user, data)


def _read_exact(user: str) -> bytes | None:
    path = _path(user)
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as err:
        raise CompanionStateError(
            user, f"state read failed: {type(err).__name__}") from err
    if len(raw) > _MAX_STATE_BYTES:
        raise CompanionStateError(user, "state exceeds the size bound")
    return raw


def _load_exact(user: str) -> dict:
    raw = _read_exact(user)
    return _default_state() if raw is None else _decode_state(user, raw)


def _state_for_write(user: str, state: object) -> dict:
    if not isinstance(state, dict):
        raise ValueError("companion state must be an object")
    unknown = set(state) - _STATE_KEYS
    if unknown:
        raise ValueError("unknown companion state keys: " + ", ".join(sorted(unknown)))
    candidate = _default_state()
    candidate.update(state)
    try:
        return _validate_state(user, candidate)
    except CompanionStateError as err:
        raise ValueError(f"invalid companion state: {err.reason}") from err


@contextlib.contextmanager
def _guard(user: str):
    """Serialize one exact owner's whole-document update.

    The full digest fits under ``proclock``'s 80-character lock-name bound, so
    distinct owners cannot collapse onto one truncated lock filename.
    """
    exact = memory.canonical_owner(user)
    digest = hashlib.sha256(exact.encode("utf-8")).hexdigest()
    with _LOCK, proclock.lock(f"companion-{digest}"):
        yield exact


def _save_locked(user: str, state: dict) -> None:
    path = _path(user)
    tmp = path.with_name(f".companion-{os.getpid()}-{threading.get_ident()}.tmp")
    atomicio.publish(
        tmp,
        path,
        json.dumps(state, indent=2, sort_keys=True),
    )


def load(user: str) -> dict:
    """Return validated exact-owner state; only a missing file means empty."""
    return _load_exact(memory.canonical_owner(user))


def save(user: str, state: dict) -> None:
    """Publish validated state without overwriting unreadable evidence."""
    exact = memory.canonical_owner(user)
    candidate = _state_for_write(exact, state)
    with _guard(exact):
        _load_exact(exact)  # malformed current bytes are never replaced here
        _save_locked(exact, candidate)


def note_interaction(user: str, now: float | None = None) -> int:
    """Count one exchange (a completed user↔Olympus turn) for this user; return
    the new total. Atomic across threads and processes supported by proclock."""
    with _guard(user) as exact:
        state = _load_exact(exact)
        state["interactions"] = int(state.get("interactions", 0)) + 1
        _save_locked(exact, state)
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
    Returns the updated model text. Provider failure returns the prior model;
    unavailable stored evidence raises so it is never treated as first use."""
    from . import backend
    settings = settings or config.Settings.from_env()
    with memory.user_context(user):
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
        with _guard(user) as exact:
            state = _load_exact(exact)
            state["model"] = model[:MODEL_MAX_CHARS]
            state["evolutions"] = int(state.get("evolutions", 0)) + 1
            state["updated"] = now_ts()
            _save_locked(exact, state)
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
    except Exception as err:
        from . import errors
        errors.capture("companion.maybe_evolve", err)
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


def state_status(user: str) -> dict:
    """Return non-sensitive operator evidence about one exact-owner store."""
    exact = memory.canonical_owner(user)
    path = _path(exact)
    legacy = _legacy_path(exact)
    legacy_present = legacy != path and legacy.is_file()
    try:
        state = load(exact)
    except CompanionStateError as err:
        return {
            "owner": exact,
            "state": "unavailable",
            "reason": err.reason,
            "model_present": None,
            "legacy_quarantined": legacy_present,
            "legacy_file": legacy.name if legacy_present else None,
            "repair_command": err.repair_command,
        }
    return {
        "owner": exact,
        "state": "valid" if path.is_file() else "missing",
        "reason": None,
        "model_present": bool(state["model"].strip()),
        "legacy_quarantined": legacy_present,
        "legacy_file": legacy.name if legacy_present else None,
        "repair_command": None,
    }


def repair(user: str) -> dict:
    """Explicitly preserve corrupt exact-owner bytes, then reset state.

    Missing and valid stores are not rewritten. Ambiguous pre-P2T ``safe_id``
    files are never touched: attributing those bytes to an owner is a separate
    operator decision.
    """
    exact = memory.canonical_owner(user)
    with _guard(exact):
        raw = _read_exact(exact)
        if raw is None:
            result = state_status(exact)
            result["repaired"] = False
            return result
        try:
            _decode_state(exact, raw)
        except CompanionStateError:
            pass
        else:
            result = state_status(exact)
            result["repaired"] = False
            return result

        digest = hashlib.sha256(raw).hexdigest()
        path = _path(exact)
        quarantine = path.with_name(
            f"companion.corrupt.{digest[:_QUARANTINE_DIGEST_HEX]}.json")
        try:
            existing = quarantine.read_bytes()
        except FileNotFoundError:
            tmp_q = path.with_name(f".companion-quarantine-{os.getpid()}.tmp")
            atomicio.publish(tmp_q, quarantine, raw)
        except OSError as err:
            raise CompanionStateError(
                exact, f"quarantine read failed: {type(err).__name__}") from err
        else:
            if existing != raw:
                raise CompanionStateError(
                    exact, "content-addressed quarantine collision")

        _save_locked(exact, _default_state())
        result = state_status(exact)
        result.update({
            "repaired": True,
            "quarantined_sha256": digest,
            "quarantine_file": quarantine.name,
        })
        return result
