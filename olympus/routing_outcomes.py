"""Routing-outcome telemetry — SPEC-04 Phase A. A PASSIVE sensor.

For each run, record a row joining the routing decision (which specialist ran on
which model, in which role, on what kind of task) to its eventual outcome signal.
This is the data a learned router (Phase B) would train on — the link that does
not exist today. Phase A **changes nothing about how routing works**: it only
writes telemetry.

It deliberately mirrors `outcomes.py`: the same `store` backend, per-user
scoping (`memory.safe_id`), an append-only capped list, and best-effort writes
that never raise into the caller (a telemetry failure must never break a run).

`outcome_signal` precedence (highest wins), documented in docs/LEARNED_ROUTING.md:
    explicit user feedback  >  the verify/review verdict  >  an action outcome.

Phase B is GATED: `gate_status()` reports whether enough LABELED outcomes from
REAL adoption exist. Synthetic / self-generated rows are flagged and excluded, so
test traffic and replays can never satisfy the gate.
"""

from __future__ import annotations

import json
import threading
import time

from . import memory, store

_NS = "routing_outcomes"
_LOCK = threading.Lock()
# One row per dispatched specialist per run, so the cap is a little larger than
# outcomes.py's per-action ledger; still a hard rolling cap (no unbounded growth).
_MAX = 2000

# --- outcome_signal enum -----------------------------------------------------
POSITIVE = "positive"                 # 👍 feedback / review approved / action approved
NEGATIVE = "negative"                 # 👎 feedback / review retry / action rejected|undone
APPROVED_AFTER_EDIT = "approved_after_edit"   # action approved after an edit
PENDING = "pending"                   # no outcome signal yet
SIGNALS = (POSITIVE, NEGATIVE, APPROVED_AFTER_EDIT, PENDING)

# --- signal source tier (for precedence + provenance) ------------------------
SRC_FEEDBACK = "feedback"
SRC_REVIEW = "review"
SRC_ACTION = "action"
SRC_NONE = "none"
_TIER = {SRC_FEEDBACK: 3, SRC_REVIEW: 2, SRC_ACTION: 1, SRC_NONE: 0}

# --- Phase B data gate (see docs/LEARNED_ROUTING.md for the justification) ----
GATE_MIN_LABELED = 300                # labeled (non-pending) real outcomes
GATE_MIN_TASK_TYPES = 3               # spread across task-types
GATE_MIN_DISTINCT_USERS = 2           # from more than one real source

# --- coarse, documented task-type tag ----------------------------------------
# We reuse the pipeline's OWN routing (which specialist handled it) as the
# task-type, grouped into a small, stable set. This is a cheap tag, not feature
# engineering — Phase A captures signal, it does not model it.
_TASK_TYPE = {
    "hephaestus": "code",
    "argus": "research", "mnemosyne": "research",
    "plutus": "finance",
    "peitho": "marketing", "iris": "social",
    "chronos": "scheduling", "angelos": "inbox",
    "aegis": "security", "chiron": "coaching",
    "prometheus": "evolution", "metis": "learning",
    "hermes": "general",
}


def task_type(specialist: str) -> str:
    return _TASK_TYPE.get(specialist, "general")


def length_bucket(text: str) -> str:
    """Coarse input-length bucket (characters). Deliberately crude."""
    n = len(text or "")
    if n < 80:
        return "xs"
    if n < 400:
        return "s"
    if n < 1500:
        return "m"
    if n < 6000:
        return "l"
    return "xl"


def signal_from_verdict(verdict: str) -> str:
    """Map a review/verify verdict to an outcome signal."""
    return POSITIVE if str(verdict).lower() == "approve" else NEGATIVE


def signal_from_feedback(verdict: str) -> str:
    return POSITIVE if str(verdict).lower() in (
        "up", "good", "positive", "+1", "👍") else NEGATIVE


def signal_from_action(outcome: str) -> str:
    """Map an outcomes.py action outcome to a routing signal."""
    o = str(outcome).lower()
    if o == "approved":
        return POSITIVE
    if o == "approved_after_edit":
        return APPROVED_AFTER_EDIT
    return NEGATIVE                    # rejected / undone


def resolve_signal(*, feedback: str | None = None, review: str | None = None,
                   action: str | None = None) -> tuple[str, str]:
    """Pick the outcome signal by documented precedence:
    explicit feedback > verify/review verdict > action outcome. Returns
    (signal, source); (PENDING, SRC_NONE) when no signal is available."""
    if feedback:
        return feedback, SRC_FEEDBACK
    if review:
        return review, SRC_REVIEW
    if action:
        return action, SRC_ACTION
    return PENDING, SRC_NONE


# --- storage (mirrors outcomes.py) -------------------------------------------
def _load(user: str) -> list:
    blob = store.backend().get(_NS, memory.safe_id(user))
    if not blob:
        return []
    try:
        return json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return []


def _save(user: str, data: list) -> None:
    store.backend().put(_NS, memory.safe_id(user), json.dumps(data).encode())


def record_run(user: str, run_id: str, message: str, specialists: list[str], *,
               models: dict[str, str], roles: dict[str, str],
               review_verdict: str | None = None, synthetic: bool = False) -> int:
    """Append one telemetry row per dispatched specialist for this run.

    `models[key]`/`roles[key]` are the model/role the routing ACTUALLY used
    (read from the deterministic selection, never re-decided). `review_verdict`
    is the pipeline's verify/review verdict ('approve'|'retry'), the review-tier
    signal; an explicit feedback signal can upgrade it later via `apply_feedback`.
    Best-effort: never raises into the caller. Returns the number of rows added.
    """
    try:
        review_sig = signal_from_verdict(review_verdict) if review_verdict else None
        signal, source = resolve_signal(review=review_sig)
        tt = None
        rows_new = []
        now = time.time()
        for key in specialists:
            rows_new.append({
                "ts": now,
                "run_id": run_id,
                "user": memory.safe_id(user),
                "specialist": key,
                "model": models.get(key, ""),
                "role": roles.get(key, ""),
                "task_type": task_type(key),
                "length_bucket": length_bucket(message),
                "outcome_signal": signal,
                "signal_source": source,
                "synthetic": bool(synthetic),
            })
        if not rows_new:
            return 0
        with _LOCK:
            log = _load(user)
            log.extend(rows_new)
            _save(user, log[-_MAX:])
        return len(rows_new)
    except Exception:
        return 0


def apply_feedback(user: str, run_id: str, verdict: str) -> int:
    """A later EXPLICIT feedback signal (👍/👎) upgrades the outcome signal of
    this run's rows — feedback is the top precedence tier. Best-effort; returns
    the number of rows updated."""
    if not run_id:
        return 0
    try:
        sig = signal_from_feedback(verdict)
        updated = 0
        with _LOCK:
            log = _load(user)
            for row in log:
                if row.get("run_id") == run_id:
                    row["outcome_signal"] = sig
                    row["signal_source"] = SRC_FEEDBACK
                    updated += 1
            if updated:
                _save(user, log)
        return updated
    except Exception:
        return 0


def events(user: str) -> list:
    """This user's routing-outcome rows (per-user scoped, like outcomes.py)."""
    return _load(user)


# --- aggregation + the Phase B gate (operator/gate view, across sources) -----
def _all_rows() -> list:
    """Every stored row across users — used only by the operator gate view."""
    out: list = []
    try:
        for key in store.backend().keys(_NS):
            blob = store.backend().get(_NS, key)
            if not blob:
                continue
            try:
                out.extend(json.loads(blob))
            except (ValueError, json.JSONDecodeError):
                continue
    except Exception:
        pass
    return out


def _labeled(rows: list) -> list:
    """Rows that count toward the gate: a real (non-synthetic) outcome that has
    an actual signal (not pending)."""
    return [r for r in rows
            if not r.get("synthetic") and r.get("outcome_signal") != PENDING]


def stats(rows: list | None = None) -> dict:
    rows = _all_rows() if rows is None else rows
    labeled = _labeled(rows)
    by_type: dict[str, int] = {}
    by_model: dict[str, int] = {}
    signals: dict[str, int] = {s: 0 for s in SIGNALS}
    users = set()
    for r in labeled:
        by_type[r.get("task_type", "?")] = by_type.get(r.get("task_type", "?"), 0) + 1
        mk = f"{r.get('specialist', '?')}/{r.get('model', '?')}"
        by_model[mk] = by_model.get(mk, 0) + 1
        users.add(r.get("user", "?"))
        s = r.get("outcome_signal", PENDING)
        signals[s] = signals.get(s, 0) + 1
    return {
        "total_rows": len(rows),
        "synthetic_rows": sum(1 for r in rows if r.get("synthetic")),
        "pending_rows": sum(1 for r in rows if r.get("outcome_signal") == PENDING),
        "labeled": len(labeled),
        "task_types": by_type,
        "by_specialist_model": by_model,
        "signals": signals,
        "distinct_users": len(users),
    }


def gate_status(rows: list | None = None) -> dict:
    """Whether the SPEC-04 Phase B data threshold is met from REAL adoption.
    THIS IS THE GATE CHECK. Synthetic/self-only rows never satisfy it."""
    s = stats(rows)
    reasons = []
    if s["labeled"] < GATE_MIN_LABELED:
        reasons.append(f"need ≥{GATE_MIN_LABELED} labeled real outcomes "
                       f"(have {s['labeled']})")
    if len(s["task_types"]) < GATE_MIN_TASK_TYPES:
        reasons.append(f"need ≥{GATE_MIN_TASK_TYPES} task-types "
                       f"(have {len(s['task_types'])})")
    if s["distinct_users"] < GATE_MIN_DISTINCT_USERS:
        reasons.append(f"need ≥{GATE_MIN_DISTINCT_USERS} distinct real sources "
                       f"(have {s['distinct_users']})")
    return {
        "met": not reasons,
        "reasons": reasons,
        "labeled": s["labeled"],
        "task_types": len(s["task_types"]),
        "distinct_users": s["distinct_users"],
        "thresholds": {"labeled": GATE_MIN_LABELED,
                       "task_types": GATE_MIN_TASK_TYPES,
                       "distinct_users": GATE_MIN_DISTINCT_USERS},
        "stats": s,
    }
