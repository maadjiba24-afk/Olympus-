"""Routing-outcome telemetry — SPEC-04 Phase A. A PASSIVE sensor.

For each run, record a row joining the routing decision (which specialist ran on
which model, in which role, on what kind of task) to its eventual outcome signal.
This is the data a learned router (Phase B) would train on — the link that does
not exist today. Phase A **changes nothing about how routing works**: it only
writes telemetry.

It uses versioned exact-owner envelopes on the configured store backend.
Updates are serialized on one state directory. Invalid evidence is preserved;
consumers report recording failures without relabeling the completed run.
Legacy normalized records never count toward a gate.

`outcome_signal` precedence (highest wins), documented in docs/LEARNED_ROUTING.md:
    explicit user feedback  >  the verify/review verdict.
(An action-outcome tier was originally envisioned as a third source but is not
wired — actions aren't linked to a run here — so it is not part of precedence.)

Phase B is GATED: `gate_status()` reports whether enough LABELED outcomes from
REAL adoption exist. Synthetic / self-generated rows are flagged and excluded, so
test traffic and replays can never satisfy the gate.
"""

from __future__ import annotations

import math
import time

from . import memory, owner_evidence as evidence

_LEGACY_NS = "routing_outcomes"
_NS = "routing_outcomes.v2"
_MAX_OWNERS = 1000
_MAX_AGGREGATE_ROWS = 100000
# One row per dispatched specialist per run, so the cap is a little larger than
# outcomes.py's per-action ledger; still a hard rolling cap (no unbounded growth).
_MAX = 2000

# --- outcome_signal enum -----------------------------------------------------
POSITIVE = "positive"                 # 👍 feedback / review approved
NEGATIVE = "negative"                 # 👎 feedback / review retry
# A half-win value the learned selector weights at 0.5 (see learned_routing).
# No emit path produces it today; retained as a supported, tested signal value.
APPROVED_AFTER_EDIT = "approved_after_edit"
PENDING = "pending"                   # no outcome signal yet
SIGNALS = (POSITIVE, NEGATIVE, APPROVED_AFTER_EDIT, PENDING)
LABELED_SIGNALS = (POSITIVE, NEGATIVE, APPROVED_AFTER_EDIT)

# --- signal source tier (for precedence + provenance) ------------------------
# Precedence: explicit feedback > verify/review verdict. An action-outcome tier
# was originally envisioned as a third, lowest source, but nothing feeds it
# (actions aren't linked to a run_id in this store), so it is intentionally NOT
# part of the wired precedence — the code and docs reflect only what is emitted.
SRC_FEEDBACK = "feedback"
SRC_REVIEW = "review"
SRC_NONE = "none"
LABELED_SOURCES = (SRC_FEEDBACK, SRC_REVIEW)

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
    """Map only supported review/verify verdicts to an outcome signal."""
    value = str(verdict).strip().lower()
    if value == "approve":
        return POSITIVE
    if value == "retry":
        return NEGATIVE
    return PENDING


def signal_from_feedback(verdict: str) -> str:
    value = str(verdict).strip().lower()
    if value in ("up", "good", "positive", "+1", "👍"):
        return POSITIVE
    if value in ("down", "bad", "negative", "-1", "👎"):
        return NEGATIVE
    return PENDING


def resolve_signal(*, feedback: str | None = None,
                   review: str | None = None) -> tuple[str, str]:
    """Pick the outcome signal by precedence: explicit feedback > verify/review
    verdict. Returns (signal, source); (PENDING, SRC_NONE) when neither source
    has a signal yet."""
    if feedback is not None:
        if feedback in LABELED_SIGNALS:
            return feedback, SRC_FEEDBACK
        return PENDING, SRC_NONE
    if review is not None:
        if review in LABELED_SIGNALS:
            return review, SRC_REVIEW
        return PENDING, SRC_NONE
    return PENDING, SRC_NONE


# --- storage (mirrors outcomes.py) -------------------------------------------
def _validate(data, owner):
    evidence.records(data, _MAX)
    identities = set()
    for row in data:
        evidence.fields(row, ("ts", "run_id", "user", "specialist", "model", "role",
                              "task_type", "length_bucket", "outcome_signal",
                              "signal_source", "synthetic"))
        if not _valid_row(row) or row["user"] != evidence.exact(owner):
            raise ValueError("invalid routing evidence or attribution")
        identity = row["run_id"], row["specialist"]
        if identity in identities:
            raise ValueError("duplicate routing observation")
        identities.add(identity)


def _evidence(user):
    return evidence.JsonStore(user, "routing outcomes", lambda data: _validate(data, user),
                              max_bytes=8 * 1024 * 1024, namespace=_NS)


def evidence_status(user):
    return _evidence(user).status()


def _load(user: str) -> list:
    return _evidence(user).load()


def _save(user: str, data: list) -> None:
    _evidence(user).save(data)
    from . import learned_routing
    learned_routing.clear_cache()


def record_run(user: str, run_id: str, message: str, specialists: list[str], *,
               models: dict[str, str], roles: dict[str, str],
               review_verdict: str | None = None, synthetic: bool = False) -> int:
    """Record attributable outcomes; refusal is surfaced by the run consumer.

    Duplicate retries do not create new qualifying observations. Replay does
    not read or mutate evidence. Synthetic labels stay excluded from gates.
    """
    from . import replaystore
    if replaystore.replaying():
        return 0
    if type(synthetic) is not bool:
        raise ValueError("synthetic must be a boolean")
    signal, source = resolve_signal(review=(signal_from_verdict(review_verdict)
                                           if review_verdict is not None else None))
    owner = evidence.exact(user)
    now = time.time()
    rows_new = [{"ts": now, "run_id": run_id, "user": owner, "specialist": key,
                 "model": models.get(key, ""), "role": roles.get(key, ""),
                 "task_type": task_type(key), "length_bucket": length_bucket(message),
                 "outcome_signal": signal, "signal_source": source,
                 "synthetic": synthetic} for key in dict.fromkeys(specialists)]
    _validate(rows_new, owner)
    if not rows_new:
        return 0
    with _evidence(owner).guard():
        log = _load(owner)
        recorded = {(r["run_id"], r["specialist"]): r for r in log}
        added = []
        for row in rows_new:
            prior = recorded.get((row["run_id"], row["specialist"]))
            if prior is not None:
                immutable = ("user", "model", "role", "task_type", "length_bucket", "synthetic")
                if any(prior[k] != row[k] for k in immutable):
                    raise evidence.OwnerEvidenceStateError("routing outcomes", "conflicting retry")
                continue
            added.append(row)
        if added:
            _save(owner, (log + added)[-_MAX:])
    return len(added)


def apply_feedback(user: str, run_id: str, verdict: str) -> int:
    """Update only this exact owner's validated rows; never hide write failure."""
    from . import replaystore
    if replaystore.replaying() or not run_id:
        return 0
    sig = signal_from_feedback(verdict)
    if sig == PENDING:
        return 0
    updated = 0
    with _evidence(user).guard():
        log = _load(user)
        for row in log:
            if row["run_id"] == run_id:
                row["outcome_signal"] = sig
                row["signal_source"] = SRC_FEEDBACK
                updated += 1
        if updated:
            _save(user, log)
    return updated


def events(user: str) -> list:
    """This user's routing-outcome rows (per-user scoped, like outcomes.py)."""
    return _load(user)


# --- aggregation + the Phase B gate (operator/gate view, across sources) -----
def _all_rows() -> list:
    """Every qualified row, or an explicit refusal; never partial evidence."""
    values = evidence.namespace_values(_NS, _evidence, max_owners=_MAX_OWNERS,
                                       max_rows=_MAX_AGGREGATE_ROWS)
    return [row for rows in values.values() for row in rows]


def _valid_row(row) -> bool:
    """Whether a row matches the schema emitted by :func:`record_run`.

    Stored telemetry is evidence, not configuration. Unknown enum values,
    incomplete rows, and inconsistent derived fields cannot feed a gate, selector,
    or offline preference dataset. Stored blobs must validate in their entirety;
    explicit in-memory analytical inputs retain invalid-row counts.
    """
    if not isinstance(row, dict) or not isinstance(row.get("synthetic"), bool):
        return False
    ts = row.get("ts")
    if (isinstance(ts, bool) or not isinstance(ts, (int, float))
            or not math.isfinite(ts)):
        return False
    required = ("run_id", "user", "specialist", "model", "role",
                "task_type", "length_bucket")
    if any(not isinstance(row.get(k), str) or not row[k].strip()
           for k in required):
        return False
    if row["user"] != memory.canonical_owner(row["user"]):
        return False
    if ts < 0 or any(len(row[k]) > (8192 if k == "user" else 512) for k in required):
        return False
    if row["task_type"] != task_type(row["specialist"]):
        return False
    if row["length_bucket"] not in ("xs", "s", "m", "l", "xl"):
        return False
    signal = row.get("outcome_signal")
    source = row.get("signal_source")
    if signal == PENDING:
        return source == SRC_NONE
    return signal in LABELED_SIGNALS and source in LABELED_SOURCES


def _labeled(rows: list) -> list:
    """Rows that count toward the gate: a real (non-synthetic) outcome that has
    an actual signal (not pending)."""
    return [r for r in rows
            if _valid_row(r) and r["synthetic"] is False
            and r["outcome_signal"] in LABELED_SIGNALS]


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
        "invalid_rows": sum(1 for r in rows if not _valid_row(r)),
        "synthetic_rows": sum(1 for r in rows
                              if _valid_row(r) and r["synthetic"] is True),
        "pending_rows": sum(1 for r in rows
                            if _valid_row(r) and r["outcome_signal"] == PENDING),
        "labeled": len(labeled),
        "task_types": by_type,
        "by_specialist_model": by_model,
        "signals": signals,
        "distinct_users": len(users),
    }


def _gate_status(rows: list | None = None) -> dict:
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


def gate_status(rows: list | None = None) -> dict:
    try:
        result = _gate_status(rows)
        result["evidence_state"] = "valid"
        return result
    except evidence.OwnerEvidenceStateError as err:
        return {"met": False, "evidence_state": "unavailable", "reasons": [str(err)],
                "labeled": None, "task_types": None, "distinct_users": None,
                "thresholds": {"labeled": GATE_MIN_LABELED, "task_types": GATE_MIN_TASK_TYPES,
                               "distinct_users": GATE_MIN_DISTINCT_USERS}, "stats": None}
