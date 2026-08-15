"""Context & skill heat — measured USEFULNESS, not frequency (Wave-2 C2).

Nothing in Olympus learns which context items (typed memories, wiki pages,
skills) actually *earn* their tokens. Retrieval is scored lexically per turn and
forgotten; a page that was retrieved a hundred times and never helped costs the
same as one that carried the answer. Colibri's expert-heat principle translated
to context placement — with the one change that matters: **heat is scored from
measured usefulness, not from raw frequency.** Retrieval is the weakest signal
in the formula; a VERIFIER-ACCEPTED use outweighs a hundred retrievals.

Invariants (WAVE2 spec W2-C2):
  W2-I2.1  CONTENT-MINIMISED. An entry stores exactly `_ENTRY_FIELDS`:
           `{id, kind, hits, useful, verifier_ok, reuse, avoided_cost,
           avoided_latency, corrections, last, first, provenance}` — ids,
           counters, timestamps and a label from a CLOSED vocabulary. There is
           no API that accepts item text: `record()` takes no content parameter
           (passing one is a `TypeError`), `kind`/`provenance` must come from
           the fixed vocabularies, and an `item_id` outside `_ID_CHARS` (so:
           anything that looks like prose) is REFUSED. A ledger that somehow
           grows an extra field is quarantined on read, never half-parsed —
           text cannot enter the prompt through this door.
  W2-I2.2  ISOLATION. `MEMORY_DIR/users/<safe_id(user)>/context_heat.json` per
           user, `MEMORY_DIR/context_heat.json` for the global/shared scope.
           A global item never inherits per-user heat and vice versa; a hostile
           user id is sanitised through `memory.safe_id` and the resolved path
           is re-checked to be inside `MEMORY_DIR` before any write.
  W2-I2.3  PIN POLICY. Proposals are bounded by a token budget and a max pin
           count, incumbents carry a HYSTERESIS margin (no ping-pong on
           near-ties), heat DECAYS (halves per half-life, applied lazily at
           read time from timestamps — no write storm), and a genuinely hot
           item (>= `protect_n()` verifier-accepted uses) cannot be displaced
           by a never-useful challenger.
  W2-I2.4  BENCHMARK GATE. `apply_pins()` writes a pin set only after an
           injected `gate_fn(before, after) -> bool` returns True — the same
           discipline as `orchestrator.gate_prompt` (a pin IS prompt content,
           so there is no unmeasured pin-set write path). A missing gate, a
           failing gate, or a gate that raises all REFUSE.
  W2-I2.5  ROLLBACK. `OLYMPUS_CTXHEAT=off` (the default) is fully inert — no
           file is written, no proposal is computed. `shadow` records heat and
           computes proposals but applies nothing: `apply_pins()` only logs the
           counterfactual. Placement stays static in both.

Poisoning resistance. The promotion signal is EXTERNAL: only
`record_verifier_outcome(item_id, accepted, source=...)` can move the
`verifier_ok` counter, and only for a `source` in `TRUSTED_VERIFIER_SOURCES`.
The recording path cannot self-grant it — `record(verifier_accepted=True)` is
accepted for signature compatibility and DELIBERATELY IGNORED (counted in
`stats()["verifier_claims_refused"]`). Self-reported `useful=True` contributes a
saturating term capped at `SELF_REPORT_CAP` (below the weight of a SINGLE
verifier acceptance) and pin eligibility additionally requires
`verifier_ok >= min_verified()`, so an item that farms its own usefulness can
never reach the pinned prefix. Claimed savings are bounds-validated.

Failure. A missing ledger means static placement (empty proposals). A corrupt
or tampered ledger is QUARANTINED aside (reject-never-repair) and the store
starts fresh and empty. Nothing here raises into a caller.

------------------------------------------------------------------------------
PROVISIONAL CONSTANTS — NOT CALIBRATED. (Wave-1 audit directive: no inherited
constants.) Every value in `PROVISIONAL_CONSTANTS` below is a placeholder taken
from the design note, NOT derived from Olympus swap/pin telemetry — the
telemetry does not exist yet, which is precisely why this ships in shadow mode.
They are env-overridable so calibration can sweep them, and `apply_pins()`
refuses to apply a pin set unless an explicit benchmark gate has passed, which
is the only thing that makes an uncalibrated constant safe to run.
Calibration owner: `CALIBRATION_OWNER` (see docs/absorption/02-memory-hierarchy
rubrics 2-3 and docs/absorption/WAVE2_IMPLEMENTATION_SPEC.md W2-C2).
------------------------------------------------------------------------------

Scope note: this PR builds the LIBRARY. It is deliberately NOT wired into
`recall`/`wiki`/`skills`/`orchestrator` — nothing imports it, nothing consumes a
pin set (that wiring is a later PR, gated on calibration). It ships inert.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, memory, proclock

# --- modes ----------------------------------------------------------------
OFF, SHADOW, ON = "off", "shadow", "on"
MODES = (OFF, SHADOW, ON)

_ON_WORDS = frozenset({"on", "1", "true", "yes", "enforce", "apply"})
_SHADOW_WORDS = frozenset({"shadow", "observe", "dry-run", "dryrun", "propose"})

# --- closed vocabularies (W2-I2.1: no free text can enter an entry) --------

#: The context item kinds heat is tracked for. Each maps to an existing gated
#: store — `usermem` rows, `wiki` pages, `skills` items, `facts`. An unknown
#: kind is REFUSED (fail closed) rather than stored as a free-text label.
KINDS = ("memory", "wiki", "skill", "fact", "doc", "lesson")

#: Where the observation came from. Also closed — `provenance` is a LABEL, not
#: a description. Anything unrecognised is coerced to `"unknown"`.
PROVENANCES = ("recall", "wiki", "skills", "usermem", "facts", "orchestrator",
               "heartbeat", "eval", "unknown")

#: The ONLY sources allowed to move `verifier_ok`. Deliberately a module
#: constant with no env override: widening the trust boundary from the
#: environment would hand the poisoning defence to the attacker's config.
TRUSTED_VERIFIER_SOURCES = frozenset({
    "aletheia",            # orchestrator stage 3 structured verdict
    "aletheia.consensus",  # multi-verifier consensus path
    "synth_check",         # composed-answer faithfulness check
    "direct_verify",       # bounded-latency DIRECT-reply check
    "evals",               # offline benchmark outcome
    "liveeval",            # live-run scoring
    "modelgate",           # drift/qualification gate
})

#: Exactly the fields one ledger entry may contain — the content-minimisation
#: contract. Enforced on write AND on read (a ledger with any other field is
#: quarantined, never half-parsed).
_ENTRY_FIELDS = ("id", "kind", "hits", "useful", "verifier_ok", "reuse",
                 "avoided_cost", "avoided_latency", "corrections", "last",
                 "first", "provenance")

_COUNTER_FIELDS = ("hits", "useful", "verifier_ok", "reuse", "corrections")
_FLOAT_FIELDS = ("avoided_cost", "avoided_latency", "last", "first")

#: An id is a REFERENCE (a uuid, a slug), never prose. Anything outside this
#: character class — a space, punctuation, a sentence — is refused outright.
_ID_CHARS = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

SCHEMA_VERSION = 1
_LEDGER_NAME = "context_heat.json"
_PINS_NAME = "pins.json"
_SHADOW_NAME = "ctxheat_shadow.jsonl"
_MAX_ENTRIES = 2000            # per scope; the weakest are pruned beyond this
#: Numeric floor, not a policy constant: heat this faint is indistinguishable
#: from zero after decay, and an item that faint has no business in the prompt.
MIN_PIN_SCORE = 1e-6
_MAX_SHADOW_ROWS = 500         # bound the counterfactual log

# --- scoring weights ------------------------------------------------------
# All PROVISIONAL (see the module banner). The ORDERING, however, is the
# capability's whole point and is test-enforced, not tuned: one verifier
# acceptance must outweigh an unbounded pile of retrievals and an unbounded
# pile of self-reported usefulness.

W_VERIFIER = 3.0        # per externally verified useful use  (the real signal)
W_REUSE = 0.5           # per cross-turn reuse of the same item
W_COST = 2.0            # per USD of measured avoided spend
W_LATENCY = 0.2         # per second of measured avoided latency
W_HITS = 0.05           # per log1p(retrieval) — frequency, saturating, weakest
SELF_REPORT_CAP = 0.25  # TOTAL contribution of self-reported usefulness, ever
CORRECTION_PENALTY = 1.0  # trust = 1 / (1 + penalty * corrections)

# Bounds validation (poisoning defence): a claimed saving beyond these is
# refused rather than clamped, so a hostile caller cannot inflate heat.
MAX_AVOIDED_COST_USD = 100.0
MAX_AVOIDED_LATENCY_MS = 86_400_000        # 24h

# --- PROVISIONAL policy constants ----------------------------------------
DEFAULT_HYSTERESIS = 0.25       # challenger must beat incumbent by 25%
DEFAULT_HALFLIFE_DAYS = 30.0    # heat halves every 30 days since last use
DEFAULT_PROTECT_N = 3           # verifier-accepted uses that protect from eviction
DEFAULT_MIN_VERIFIED = 1        # verifier acceptances required to be pinnable
DEFAULT_MAX_SWAPS = 2           # pin-set churn allowed per proposal
DEFAULT_MAX_PINS = 16           # hard cap on pin-set size
DEFAULT_PIN_BUDGET_TOKENS = 1500  # T0 pins are a slice of the system prompt

#: Fallback token cost per kind, used only when the caller supplies no
#: `est_tokens` map. Also PROVISIONAL — real sizes come from the gated stores
#: at wiring time.
DEFAULT_EST_TOKENS = {"memory": 40, "wiki": 600, "skill": 300, "fact": 40,
                      "doc": 400, "lesson": 120}

CALIBRATION_OWNER = "olympus-context-economics (Wave-2 C2)"

#: The audit surface: every constant that is a placeholder, with its knob.
PROVISIONAL_CONSTANTS: dict[str, dict] = {
    "hysteresis": {"value": DEFAULT_HYSTERESIS,
                   "env": "OLYMPUS_CTXHEAT_HYSTERESIS",
                   "why": "25%+4 was tuned to Colibri disk economics; ours must "
                          "be tuned to prompt-cache economics"},
    "halflife_days": {"value": DEFAULT_HALFLIFE_DAYS,
                      "env": "OLYMPUS_CTXHEAT_HALFLIFE_DAYS",
                      "why": "no measured decay curve for context usefulness yet"},
    "protect_n": {"value": DEFAULT_PROTECT_N,
                  "env": "OLYMPUS_CTXHEAT_PROTECT_N",
                  "why": "eviction-protection threshold not derived from swap "
                         "telemetry"},
    "min_verified": {"value": DEFAULT_MIN_VERIFIED,
                     "env": "OLYMPUS_CTXHEAT_MIN_VERIFIED",
                     "why": "pin-eligibility floor not derived from telemetry"},
    "max_swaps": {"value": DEFAULT_MAX_SWAPS,
                  "env": "OLYMPUS_CTXHEAT_MAX_SWAPS",
                  "why": "churn budget not derived from cache-hit measurements"},
    "max_pins": {"value": DEFAULT_MAX_PINS,
                 "env": "OLYMPUS_CTXHEAT_MAX_PINS", "why": "placeholder cap"},
    "pin_budget_tokens": {"value": DEFAULT_PIN_BUDGET_TOKENS,
                          "env": "OLYMPUS_PIN_BUDGET_TOKENS",
                          "why": "T0 slice size not derived from measurement"},
    "weights": {"value": {"verifier": W_VERIFIER, "reuse": W_REUSE,
                          "cost": W_COST, "latency": W_LATENCY,
                          "hits": W_HITS, "self_report_cap": SELF_REPORT_CAP,
                          "correction_penalty": CORRECTION_PENALTY},
                "env": "(not overridable — calibrate in code with evidence)",
                "why": "relative weights are a design claim, not a measurement; "
                       "only their ORDERING is test-enforced"},
    "est_tokens": {"value": dict(DEFAULT_EST_TOKENS),
                   "env": "(caller supplies est_tokens)",
                   "why": "per-kind size fallbacks are guesses"},
}

#: True while the constants above are uncalibrated. It is what makes
#: `apply_pins` demand a passing benchmark gate before any write.
PROVISIONAL = True

_STATS: dict[str, int] = {"recorded": 0, "rejected": 0,
                          "verifier_claims_refused": 0, "quarantined": 0}


# --- flags + knobs (config.py idioms: zero-arg, live-read, default off) ----

def mode() -> str:
    """`OLYMPUS_CTXHEAT` = `off` (default) | `shadow` | `on`.

    `off`   — fully inert: nothing recorded, nothing proposed, nothing written.
    `shadow`— heat is recorded and pin sets are PROPOSED; `apply_pins` applies
              nothing and only logs the counterfactual.
    `on`    — additionally allows application, through the benchmark gate.
    An unrecognised value is `off` (fail closed)."""
    raw = os.environ.get("OLYMPUS_CTXHEAT", "").strip().lower()
    if raw in _ON_WORDS:
        return ON
    if raw in _SHADOW_WORDS:
        return SHADOW
    return OFF


def enabled() -> bool:
    """True in `shadow` or `on` — i.e. whether heat is recorded at all."""
    return mode() != OFF


def hysteresis() -> float:
    """PROVISIONAL. Fractional margin a challenger must beat an incumbent by."""
    return max(0.0, _float_env("OLYMPUS_CTXHEAT_HYSTERESIS", DEFAULT_HYSTERESIS))


def halflife_days() -> float:
    """PROVISIONAL. Heat halves after this many days without use."""
    return max(0.01, _float_env("OLYMPUS_CTXHEAT_HALFLIFE_DAYS",
                                DEFAULT_HALFLIFE_DAYS))


def protect_n() -> int:
    """PROVISIONAL. Verifier-accepted uses that make an item eviction-protected
    against a never-useful challenger."""
    return max(1, _int_env("OLYMPUS_CTXHEAT_PROTECT_N", DEFAULT_PROTECT_N))


def min_verified() -> int:
    """PROVISIONAL. Verifier acceptances required before an item is pinnable at
    all — the hard half of the poisoning defence."""
    return max(0, _int_env("OLYMPUS_CTXHEAT_MIN_VERIFIED", DEFAULT_MIN_VERIFIED))


def max_swaps() -> int:
    """PROVISIONAL. Pin-set changes allowed in one proposal (cache stability)."""
    return max(0, _int_env("OLYMPUS_CTXHEAT_MAX_SWAPS", DEFAULT_MAX_SWAPS))


def max_pins() -> int:
    """PROVISIONAL. Hard cap on the number of pinned items."""
    return max(0, _int_env("OLYMPUS_CTXHEAT_MAX_PINS", DEFAULT_MAX_PINS))


def pin_budget_tokens() -> int:
    """PROVISIONAL. Default token budget for the pinned T0 slice."""
    return max(0, _int_env("OLYMPUS_PIN_BUDGET_TOKENS",
                           DEFAULT_PIN_BUDGET_TOKENS))


def provisional() -> bool:
    """Whether the policy constants are still uncalibrated (they are)."""
    return bool(PROVISIONAL)


def _to_float(raw):
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def _float_env(name: str, default: float) -> float:
    val = _to_float(os.environ.get(name, "").strip())
    return default if val is None else val


def _int_env(name: str, default: int) -> int:
    val = _to_float(os.environ.get(name, "").strip())
    return default if val is None else int(val)


def _num(value, default: float = 0.0) -> float:
    val = _to_float(value)
    return default if val is None else val


def _capture(where: str, exc: BaseException, context: str = "") -> None:
    from . import errors                     # lazy: keep the import graph thin
    try:
        errors.capture(where, exc, context=context)
    except Exception:                        # noqa: BLE001 - never raise
        pass


# --- identity + storage (W2-I2.2 isolation) -------------------------------

def key(kind: str, item_id: str) -> str:
    """The ledger key. Kind-qualified so a wiki slug and a memory id that
    happen to collide are still two different items."""
    return f"{kind}:{item_id}"


def scope_of(user: str | None) -> str:
    """`global` or `user:<safe_id>` — the isolation boundary, as a label."""
    if user is None or str(user).strip() == "" or str(user) == "shared":
        return "global"
    return f"user:{memory.safe_id(user)}"


def _scope_dir(user: str | None) -> Path | None:
    """The directory holding this scope's ledger, or None if it would escape.

    Hostile ids are sanitised by `memory.safe_id` (which cannot emit `/`, `.`
    or an empty string); the resolved path is then re-checked to be inside
    MEMORY_DIR — belt and braces, because this is the one place a caller-
    supplied string touches the filesystem."""
    base = Path(config.MEMORY_DIR)
    if user is None or str(user).strip() == "" or str(user) == "shared":
        target = base
    else:
        target = base / "users" / memory.safe_id(user)
    try:
        root = base.resolve()
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            _capture("ctxheat.scope_escape",
                     ValueError("resolved path outside MEMORY_DIR"),
                     context=str(target))
            return None
    except OSError as err:                   # pragma: no cover - fs edge
        _capture("ctxheat.scope_resolve", err, context=str(target))
        return None
    return target


def ledger_path(user: str | None = None) -> Path | None:
    d = _scope_dir(user)
    return None if d is None else d / _LEDGER_NAME


def pins_path(user: str | None = None) -> Path | None:
    d = _scope_dir(user)
    return None if d is None else d / _PINS_NAME


def shadow_log_path(user: str | None = None) -> Path | None:
    d = _scope_dir(user)
    return None if d is None else d / _SHADOW_NAME


def _lock_name(user: str | None) -> str:
    return f"ctxheat-{scope_of(user)}"


def _atomic_write_json(path: Path, obj) -> None:
    """tmp + `os.replace` publish (ADR 0005) — a reader never sees a torn
    ledger, and a crash leaves the previous good file intact.

    DELIBERATELY NOT FSYNCED (W1-1b). Reached from `record()` -> `_apply()`,
    a locked read-modify-write, and `recall._heat_record` calls that once per
    RETRIEVED MEMORY on a path `orchestrator` runs every turn
    (`recall.context_block`, orchestrator.py:531/1195/2307) — so this writes
    more often per turn than `usage.record` does, where W1-1's per-call fsync
    broke the observability contract. `OLYMPUS_CTXHEAT` defaults to `off`,
    which means a default install would not have caught the regression and an
    operator enabling heat would have absorbed it silently.

    No knob: heat is telemetry by construction ("never a retrieval
    dependency"), so a power cut costs pin-proposal quality, not correctness or
    a safety control. Atomicity is unchanged."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    from . import atomicio
    atomicio.publish(tmp, path, json.dumps(obj, indent=1, sort_keys=True),
                     fsync=False)


# --- entry validation (W2-I2.1 content minimisation) ----------------------

def _clean_id(item_id) -> str | None:
    """An item id, or None if it is not a reference-shaped token.

    Rejecting (rather than sanitising) is the point: a sanitiser would happily
    turn a sentence of user text into a dashed slug and store it. An id must
    already look like what the gated stores emit — a uuid, a slug, a path-free
    token."""
    text = str(item_id if item_id is not None else "").strip()
    return text if _ID_CHARS.match(text) else None


def _clean_kind(kind) -> str | None:
    text = str(kind if kind is not None else "").strip().lower()
    return text if text in KINDS else None


def _clean_provenance(provenance) -> str:
    text = str(provenance if provenance is not None else "").strip().lower()
    return text if text in PROVENANCES else "unknown"


def _new_entry(item_id: str, kind: str, provenance: str, now: float) -> dict:
    """A zeroed entry containing EXACTLY `_ENTRY_FIELDS`."""
    return {"id": item_id, "kind": kind, "hits": 0, "useful": 0,
            "verifier_ok": 0, "reuse": 0, "avoided_cost": 0.0,
            "avoided_latency": 0.0, "corrections": 0, "last": now,
            "first": now, "provenance": provenance}


def _entry_ok(entry) -> bool:
    """Whether a loaded entry honours the content-minimisation contract.

    Anything else — an extra field, a wrong type, a non-reference id, a kind or
    provenance outside the closed vocabularies — means the file was tampered
    with or written by something that is not this module. It is not repaired."""
    if not isinstance(entry, dict):
        return False
    if set(entry) != set(_ENTRY_FIELDS):
        return False
    if _clean_id(entry.get("id")) != entry.get("id"):
        return False
    if _clean_kind(entry.get("kind")) != entry.get("kind"):
        return False
    if entry.get("provenance") not in PROVENANCES:
        return False
    for name in _COUNTER_FIELDS:
        val = entry.get(name)
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            return False
    for name in _FLOAT_FIELDS:
        val = entry.get(name)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return False
        if not math.isfinite(float(val)) or float(val) < 0:
            return False
    return True


# --- ledger I/O (corrupt => quarantine, never repair, never raise) --------

def _quarantine(path: Path, reason: str) -> Path | None:
    dest = path.with_name(f"{path.stem}.corrupt.{time.time():.0f}{path.suffix}")
    try:
        if path.exists():
            os.replace(path, dest)
    except OSError as err:                   # pragma: no cover - fs edge
        _capture("ctxheat.quarantine", err, context=str(path))
        return None
    _STATS["quarantined"] += 1
    _capture("ctxheat.ledger_corrupt", ValueError(reason), context=str(dest))
    return dest


def _load(user: str | None = None) -> dict:
    """Every entry for a scope, keyed by `kind:id`. `{}` when absent/corrupt."""
    path = ledger_path(user)
    if path is None or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as err:
        _quarantine(path, f"unreadable ledger: {type(err).__name__}")
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        _quarantine(path, "missing or unsupported schema_version")
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        _quarantine(path, "entries is not an object")
        return {}
    out: dict[str, dict] = {}
    for ekey, entry in entries.items():
        if not _entry_ok(entry):
            _quarantine(path, f"entry {ekey!r} violates the entry contract")
            return {}
        if ekey != key(entry["kind"], entry["id"]):
            _quarantine(path, f"entry key {ekey!r} does not match its id/kind")
            return {}
        out[ekey] = dict(entry)
    return out


def _prune(entries: dict, now: float, keep: str | None = None) -> dict:
    """Bound the ledger; the coldest (lowest scored) entries go first.

    `keep` is the entry being written by this call — a brand-new item starts
    cold by construction, and dropping it here would mean an item could never
    accumulate heat once the ledger is full."""
    if len(entries) <= _MAX_ENTRIES:
        return entries
    ranked = sorted(entries.items(),
                    key=lambda kv: (kv[0] != keep, -score(kv[1], now=now),
                                    kv[0]))
    return dict(ranked[:_MAX_ENTRIES])


def _save(entries: dict, user: str | None, now: float,
          keep: str | None = None) -> bool:
    path = ledger_path(user)
    if path is None:
        return False
    try:
        _atomic_write_json(path, {"schema_version": SCHEMA_VERSION,
                                  "scope": scope_of(user),
                                  "updated": now,
                                  "entries": _prune(entries, now, keep)})
    except (OSError, ValueError) as err:
        _capture("ctxheat.save", err, context=str(path))
        return False
    return True


def entries(user: str | None = None) -> dict:
    """Public read of a scope's entries (a copy). `{}` when the flag is off."""
    if not enabled():
        return {}
    return _load(user)


def entry(item_id: str, kind: str, *, user: str | None = None) -> dict | None:
    """One entry, or None. Read-only; never creates anything."""
    return entries(user).get(key(kind, str(item_id)))


def stats() -> dict:
    """Process-local counters: recorded / rejected / refused verifier claims /
    quarantined ledgers. Deliberately not persisted — a poisoning attempt must
    not be able to write anything at all."""
    return dict(_STATS)


# --- recording ------------------------------------------------------------

def record(item_id, kind, *, retrieved: bool = False, useful=None,
           verifier_accepted=None, reused: bool = False,
           avoided_cost_usd: float = 0.0, avoided_latency_ms: float = 0,
           corrected: bool = False, user: str | None = None,
           provenance: str = "unknown", now: float | None = None) -> bool:
    """Record one observation about a context item. Returns True if persisted.

    There is NO content parameter and there never will be (W2-I2.1) — this
    function's signature is the content-minimisation contract: passing
    `content=`/`text=`/`snippet=` is a `TypeError`, not a silently dropped
    field.

    `verifier_accepted` is accepted for signature compatibility and IGNORED:
    the recording path cannot grant itself the promotion signal. Use
    `record_verifier_outcome()`, which demands a trusted external source. A
    claim made here is counted in `stats()["verifier_claims_refused"]`.

    `useful=True` is a SELF-REPORT: it is stored, but its total contribution to
    the score saturates at `SELF_REPORT_CAP` (less than one verifier
    acceptance) and it never satisfies pin eligibility.

    Bounds-validated: an unknown `kind`, a prose-shaped `item_id`, a negative or
    absurd saving is REFUSED (counted in `stats()["rejected"]`), never raised
    and never written. Inert when `OLYMPUS_CTXHEAT` is off."""
    if not enabled():
        return False
    if verifier_accepted is not None:
        _STATS["verifier_claims_refused"] += 1
    clean_id = _clean_id(item_id)
    clean_kind = _clean_kind(kind)
    cost = _to_float(avoided_cost_usd)
    latency = _to_float(avoided_latency_ms)
    if (clean_id is None or clean_kind is None
            or cost is None or not (0.0 <= cost <= MAX_AVOIDED_COST_USD)
            or latency is None
            or not (0.0 <= latency <= MAX_AVOIDED_LATENCY_MS)):
        _STATS["rejected"] += 1
        return False

    stamp = time.time() if now is None else _num(now, time.time())
    prov = _clean_provenance(provenance)

    def mutate(current: dict) -> None:
        if retrieved:
            current["hits"] += 1
        if useful is True:
            current["useful"] += 1
        if reused:
            current["reuse"] += 1
        if corrected:
            current["corrections"] += 1
        current["avoided_cost"] = round(current["avoided_cost"] + cost, 6)
        current["avoided_latency"] = round(
            current["avoided_latency"] + latency, 3)

    return _apply(clean_id, clean_kind, prov, user, stamp, mutate)


def record_verifier_outcome(item_id, accepted: bool, *, source: str,
                            kind: str | None = None, user: str | None = None,
                            provenance: str = "orchestrator",
                            now: float | None = None) -> bool:
    """The ONLY way `verifier_ok` moves — an EXTERNAL usefulness signal.

    `source` must name a trusted verifier (`TRUSTED_VERIFIER_SOURCES`): the
    path that injected an item cannot grant it acceptance, and no env var can
    widen the set. `accepted=False` is recorded as a CORRECTION (measured
    negative evidence lowers heat rather than being discarded).

    `kind` may be omitted only when exactly one existing entry carries this id;
    otherwise the outcome is refused rather than attributed to a guess."""
    if not enabled():
        return False
    label = str(source or "").strip().lower()
    clean_id = _clean_id(item_id)
    if not label or label not in TRUSTED_VERIFIER_SOURCES or clean_id is None:
        _STATS["rejected"] += 1
        return False

    clean_kind = _clean_kind(kind) if kind is not None else None
    if kind is not None and clean_kind is None:
        _STATS["rejected"] += 1
        return False
    if clean_kind is None:
        matches = [e for e in _load(user).values() if e["id"] == clean_id]
        if len(matches) != 1:
            _STATS["rejected"] += 1
            return False
        clean_kind = matches[0]["kind"]

    stamp = time.time() if now is None else _num(now, time.time())

    def mutate(current: dict) -> None:
        if accepted:
            current["verifier_ok"] += 1
        else:
            current["corrections"] += 1

    return _apply(clean_id, clean_kind, _clean_provenance(provenance), user,
                  stamp, mutate)


def _apply(item_id: str, kind: str, provenance: str, user: str | None,
           now: float, mutate) -> bool:
    """Read-modify-write one entry under the scope lock. Never raises."""
    ekey = key(kind, item_id)
    try:
        with proclock.lock(_lock_name(user), timeout=10.0):
            current = _load(user)
            current.setdefault(ekey, _new_entry(item_id, kind, provenance, now))
            mutate(current[ekey])
            current[ekey]["last"] = max(now, _num(current[ekey].get("last")))
            current[ekey]["first"] = min(now, _num(current[ekey].get("first"),
                                                   now))
            if not _entry_ok(current[ekey]):     # defensive: never write junk
                _STATS["rejected"] += 1
                return False
            ok = _save(current, user, now, keep=ekey)
    except (OSError, TimeoutError, ValueError) as err:
        _capture("ctxheat.record", err, context=ekey)
        return False
    if ok:
        _STATS["recorded"] += 1
    return ok


# --- scoring (the core requirement: usefulness != frequency) --------------

def score(entry: dict, *, now: float | None = None,
          halflife: float | None = None) -> float:
    """Heat for one entry — a PURE function of the entry, `now`, and the
    half-life (env-read only when not passed).

        measured    = W_VERIFIER x verifier_ok
                    + W_REUSE    x reuse
                    + W_COST     x avoided_cost_usd
                    + W_LATENCY  x avoided_latency_seconds
                    + W_HITS     x log1p(hits)            <- frequency, saturating
        self_report = SELF_REPORT_CAP x (1 - 2^-useful)   <- capped, unpromotable
        trust       = 1 / (1 + CORRECTION_PENALTY x corrections)
        decay       = 0.5 ^ (days_since_last / halflife)
        score       = (measured + self_report) x trust x decay

    The shape encodes the capability: retrieval enters through a logarithm at
    the smallest weight, so an item retrieved a hundred times and never useful
    scores BELOW one retrieved five times and accepted by the verifier five
    times; self-reported usefulness is bounded below the weight of a SINGLE
    verifier acceptance; corrections erode heat; and heat halves with age so
    the score is a leaky integrator, not a lifetime count."""
    if not isinstance(entry, dict):
        return 0.0
    half = halflife_days() if halflife is None else max(0.01, float(halflife))
    stamp = time.time() if now is None else float(now)

    verifier_ok = max(0.0, _num(entry.get("verifier_ok")))
    reuse = max(0.0, _num(entry.get("reuse")))
    cost = min(max(0.0, _num(entry.get("avoided_cost"))), MAX_AVOIDED_COST_USD)
    latency_ms = min(max(0.0, _num(entry.get("avoided_latency"))),
                     MAX_AVOIDED_LATENCY_MS)
    hits = max(0.0, _num(entry.get("hits")))
    useful = max(0.0, _num(entry.get("useful")))
    corrections = max(0.0, _num(entry.get("corrections")))

    measured = (W_VERIFIER * verifier_ok
                + W_REUSE * reuse
                + W_COST * cost
                + W_LATENCY * (latency_ms / 1000.0)
                + W_HITS * math.log1p(hits))
    self_report = SELF_REPORT_CAP * (1.0 - 0.5 ** min(useful, 64.0))
    trust = 1.0 / (1.0 + CORRECTION_PENALTY * corrections)

    age_days = max(0.0, (stamp - _num(entry.get("last"), stamp)) / 86400.0)
    decay = 0.5 ** (age_days / half)
    return max(0.0, (measured + self_report) * trust * decay)


def est_tokens_for(entry: dict, est_tokens: dict | None = None) -> int:
    """Token cost of an item: the caller's map (by `kind:id` or bare id) first,
    then the PROVISIONAL per-kind fallback. Never zero (it is a divisor)."""
    if est_tokens:
        ekey = key(entry.get("kind", ""), entry.get("id", ""))
        for candidate in (ekey, entry.get("id")):
            if candidate in est_tokens:
                val = _to_float(est_tokens[candidate])
                if val is not None and val > 0:
                    return int(val)
    return int(DEFAULT_EST_TOKENS.get(entry.get("kind", ""), 200))


def explain(item_id: str, kind: str, *, user: str | None = None,
            now: float | None = None) -> dict:
    """Operator answer to "why is this item hot (or not)?" — the score, its
    components, pin eligibility and eviction protection. Never raises."""
    found = entry(item_id, kind, user=user)
    if not found:
        return {"found": False, "id": str(item_id), "kind": str(kind),
                "scope": scope_of(user), "score": 0.0}
    stamp = time.time() if now is None else float(now)
    verifier_ok = int(_num(found.get("verifier_ok")))
    return {
        "found": True, "id": found["id"], "kind": found["kind"],
        "scope": scope_of(user), "score": round(score(found, now=stamp), 6),
        "components": {
            "verifier": round(W_VERIFIER * verifier_ok, 6),
            "reuse": round(W_REUSE * _num(found.get("reuse")), 6),
            "cost": round(W_COST * _num(found.get("avoided_cost")), 6),
            "latency": round(W_LATENCY * _num(found.get("avoided_latency"))
                             / 1000.0, 6),
            "hits": round(W_HITS * math.log1p(_num(found.get("hits"))), 6),
            "self_report_capped": round(
                SELF_REPORT_CAP * (1.0 - 0.5 ** min(_num(found.get("useful")),
                                                    64.0)), 6),
        },
        "pin_eligible": verifier_ok >= min_verified(),
        "eviction_protected": verifier_ok >= protect_n(),
        "provisional": provisional(),
    }


# --- pin proposals (W2-I2.3) ---------------------------------------------

KEEP, ADD, DROP = "keep", "add", "drop"


@dataclass(frozen=True)
class PinProposal:
    """One proposed pin-set decision. Content-minimised like the ledger: ids,
    counters, a score, a reason label — never item text."""

    item_id: str
    kind: str
    action: str                    # keep | add | drop
    score: float = 0.0
    est_tokens: int = 0
    verifier_ok: int = 0
    reason: str = ""

    @property
    def pinned(self) -> bool:
        """Whether this proposal is part of the RESULTING pin set."""
        return self.action in (KEEP, ADD)

    @property
    def key(self) -> str:
        return key(self.kind, self.item_id)

    def to_dict(self) -> dict:
        return {"id": self.item_id, "kind": self.kind, "action": self.action,
                "score": round(float(self.score), 6),
                "est_tokens": int(self.est_tokens),
                "verifier_ok": int(self.verifier_ok), "reason": self.reason}


def pinned_ids(proposals) -> list[str]:
    """The `kind:id` keys of the resulting pin set (keeps + adds)."""
    return [p.key for p in proposals or [] if p.pinned]


def _incumbent_keys(incumbents, user: str | None) -> list[str]:
    """Normalise the current pin set: explicit argument, else the applied
    `pins.json` for this scope."""
    if incumbents is None:
        incumbents = [p for p in _read_pins(user)]
    out: list[str] = []
    for item in incumbents or []:
        if isinstance(item, PinProposal):
            out.append(item.key)
        elif isinstance(item, dict):
            iid, knd = _clean_id(item.get("id")), _clean_kind(item.get("kind"))
            if iid and knd:
                out.append(key(knd, iid))
        else:
            text = str(item)
            if ":" in text:
                knd, _, iid = text.partition(":")
                if _clean_kind(knd) and _clean_id(iid):
                    out.append(text)
    return out


@dataclass
class _Cand:
    ekey: str
    entry: dict
    score: float
    est: int
    incumbent: bool

    @property
    def density(self) -> float:
        return self.score / max(1, self.est)

    @property
    def verifier_ok(self) -> int:
        return int(_num(self.entry.get("verifier_ok")))

    @property
    def protected(self) -> bool:
        return self.verifier_ok >= protect_n()


def propose_pins(budget_tokens: int | None = None, *, user: str | None = None,
                 now: float | None = None, est_tokens: dict | None = None,
                 incumbents=None) -> list[PinProposal]:
    """Propose the pin set for a scope. NEVER applies anything.

    Selection is by **value density** (score per token, the fix for Colibri's
    uniform-blob assumption), greedily filling `budget_tokens` under
    `max_pins()`, with three protections that make placement STABLE rather than
    merely optimal:

    * **eligibility** — an item needs `min_verified()` verifier acceptances to
      be pinnable at all (poisoning defence: self-reported usefulness never
      promotes);
    * **hysteresis** — an incumbent's density is scaled by `1 + hysteresis()`,
      so a challenger must beat it by that margin to displace it (no ping-pong
      on near-ties, and prompt-cache prefixes stay stable);
    * **eviction protection** — an incumbent with `protect_n()` verifier-
      accepted uses is restored if it was displaced by a never-useful
      challenger;
    * **churn cap** — at most `max_swaps()` additions per proposal.

    Returns `keep`/`add`/`drop` proposals (the pin set is the non-`drop` ones).
    Empty list when the flag is off."""
    if not enabled():
        return []
    budget = pin_budget_tokens() if budget_tokens is None else max(
        0, int(_num(budget_tokens)))
    stamp = time.time() if now is None else float(now)
    ledger = _load(user)
    incumbent_keys = _incumbent_keys(incumbents, user)
    incumbent_set = set(incumbent_keys)

    margin = 1.0 + hysteresis()
    floor = min_verified()
    cands: list[_Cand] = []
    for ekey, item in ledger.items():
        if int(_num(item.get("verifier_ok"))) < floor:
            continue                      # not measurably useful => not pinnable
        cands.append(_Cand(ekey=ekey, entry=item,
                           score=score(item, now=stamp),
                           est=est_tokens_for(item, est_tokens),
                           incumbent=ekey in incumbent_set))

    def rank(cand: _Cand) -> tuple:
        eff = cand.density * (margin if cand.incumbent else 1.0)
        return (-eff, -cand.score, cand.ekey)

    ranked = sorted(cands, key=rank)
    limit = max_pins()
    chosen: list[_Cand] = []
    used = 0
    for cand in ranked:
        if len(chosen) >= limit:
            break
        if cand.score <= MIN_PIN_SCORE:
            continue                      # fully decayed heat earns no residency
        if used + cand.est > budget:
            continue                      # try a smaller item (best-fit greedy)
        chosen.append(cand)
        used += cand.est

    chosen, used = _protect_incumbents(chosen, cands, used, budget, limit)
    if incumbent_set:            # a cold start is a first FILL, not a swap
        chosen, used = _cap_swaps(chosen, cands, used, budget, limit)

    chosen_keys = {c.ekey for c in chosen}
    proposals = [
        PinProposal(item_id=c.entry["id"], kind=c.entry["kind"],
                    action=KEEP if c.incumbent else ADD,
                    score=round(c.score, 6), est_tokens=c.est,
                    verifier_ok=c.verifier_ok,
                    reason="incumbent_retained" if c.incumbent
                    else "highest_value_density")
        for c in chosen]
    by_key = {c.ekey: c for c in cands}
    for ekey in incumbent_keys:
        if ekey in chosen_keys:
            continue
        old = by_key.get(ekey)
        knd, _, iid = ekey.partition(":")
        proposals.append(PinProposal(
            item_id=old.entry["id"] if old else iid,
            kind=old.entry["kind"] if old else knd,
            action=DROP,
            score=round(old.score, 6) if old else 0.0,
            est_tokens=old.est if old else 0,
            verifier_ok=old.verifier_ok if old else 0,
            reason="displaced" if old else "stale_or_ineligible"))
    return proposals


def _protect_incumbents(chosen: list, cands: list, used: int, budget: int,
                        limit: int):
    """Restore an eviction-protected incumbent that a NEVER-USEFUL challenger
    pushed out, freeing room by removing the weakest such challenger."""
    chosen_keys = {c.ekey for c in chosen}
    dropped = [c for c in cands
               if c.incumbent and c.protected and c.ekey not in chosen_keys]
    for inc in sorted(dropped, key=lambda c: -c.score):
        weak = sorted([c for c in chosen
                       if not c.incumbent and c.verifier_ok == 0],
                      key=lambda c: c.density)
        if not weak:
            continue                      # only real contenders displaced it
        while weak and (used + inc.est > budget or len(chosen) >= limit):
            victim = weak.pop(0)
            chosen.remove(victim)
            used -= victim.est
        if used + inc.est <= budget and len(chosen) < limit:
            chosen.append(inc)
            used += inc.est
    return chosen, used


def _cap_swaps(chosen: list, cands: list, used: int, budget: int,
               limit: int):
    """Bound pin-set churn to `max_swaps()` additions, restoring the strongest
    displaced incumbents into the room that frees up (cache stability).

    Only meaningful against an EXISTING pin set: with no incumbents there is
    nothing to swap, and the first proposal is a fill bounded by the token
    budget and `max_pins()` instead."""
    cap = max_swaps()
    adds = sorted([c for c in chosen if not c.incumbent],
                  key=lambda c: c.density)
    while len(adds) > cap:
        victim = adds.pop(0)
        chosen.remove(victim)
        used -= victim.est
    chosen_keys = {c.ekey for c in chosen}
    restorable = sorted([c for c in cands
                         if c.incumbent and c.ekey not in chosen_keys],
                        key=lambda c: -c.score)
    for inc in restorable:
        if len(chosen) >= limit or used + inc.est > budget:
            continue
        chosen.append(inc)
        used += inc.est
    return chosen, used


# --- applied pin state + the benchmark gate (W2-I2.4) --------------------

def _read_pins(user: str | None = None) -> list[dict]:
    """The raw applied pin set on disk (ids/kinds only). `[]` when absent or
    malformed — a damaged pin file means static placement, never an error."""
    path = pins_path(user)
    if path is None or not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as err:
        _capture("ctxheat.read_pins", err, context=str(path))
        return []
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return []
    out = []
    for item in raw.get("pins", []) or []:
        if not isinstance(item, dict):
            continue
        iid, knd = _clean_id(item.get("id")), _clean_kind(item.get("kind"))
        if iid and knd:
            out.append({"id": iid, "kind": knd,
                        "pinned_at": _num(item.get("pinned_at")),
                        "est_tokens": int(_num(item.get("est_tokens"))),
                        "score": _num(item.get("score"))})
    return out


def applied_pins(user: str | None = None) -> list[dict]:
    """The pin set a consumer may act on — EMPTY unless the mode is `on`
    (W2-I2.5: with the flag off or in shadow, placement is static even if a pin
    file exists from an earlier experiment)."""
    if mode() != ON:
        return []
    return _read_pins(user)


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of an application attempt — always explicit, never silent."""

    applied: bool
    reason: str
    scope: str = "global"
    mode: str = OFF
    pins: list = field(default_factory=list)
    counterfactual: dict = field(default_factory=dict)
    gate_called: bool = False

    def to_dict(self) -> dict:
        return {"applied": self.applied, "reason": self.reason,
                "scope": self.scope, "mode": self.mode, "pins": list(self.pins),
                "counterfactual": dict(self.counterfactual),
                "gate_called": self.gate_called}


def counterfactual(proposals, *, user: str | None = None,
                   now: float | None = None) -> dict:
    """What WOULD change if this proposal were applied — ids and counts only."""
    stamp = time.time() if now is None else float(now)
    keeps = [p for p in proposals or [] if p.action == KEEP]
    adds = [p for p in proposals or [] if p.action == ADD]
    drops = [p for p in proposals or [] if p.action == DROP]
    return {
        "ts": stamp, "scope": scope_of(user), "mode": mode(),
        "provisional": provisional(),
        "pins": sorted(p.key for p in keeps + adds),
        "add": sorted(p.key for p in adds),
        "drop": sorted(p.key for p in drops),
        "keep": sorted(p.key for p in keeps),
        "tokens": sum(int(p.est_tokens) for p in keeps + adds),
        "swaps": len(adds),
    }


def _log_counterfactual(record_row: dict, user: str | None) -> None:
    """Append the shadow row (bounded). Best-effort; never raises."""
    path = shadow_log_path(user)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_row, sort_keys=True) + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_SHADOW_ROWS:
            path.write_text("\n".join(lines[-_MAX_SHADOW_ROWS:]) + "\n",
                            encoding="utf-8")
    except (OSError, ValueError) as err:
        _capture("ctxheat.shadow_log", err, context=str(path))


def shadow_log(user: str | None = None, limit: int = 50) -> list[dict]:
    """The recorded counterfactuals for a scope, oldest-first."""
    path = shadow_log_path(user)
    if path is None or not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except (ValueError, json.JSONDecodeError):
                continue
    except OSError as err:                   # pragma: no cover - fs edge
        _capture("ctxheat.shadow_read", err, context=str(path))
        return []
    return out[-limit:]


def apply_pins(proposals, *, gate_fn=None, user: str | None = None,
               now: float | None = None, reason: str = "") -> ApplyResult:
    """Apply a proposed pin set — ONLY behind a passing benchmark gate.

    A pin is prompt content, so this mirrors `orchestrator.gate_prompt`: the
    caller injects `gate_fn(before, after) -> bool` (in practice a `liveeval` /
    `evals` before-after run over the two pin sets) and a write happens only if
    it returns True. There is no unmeasured pin-set write path — a missing
    gate, a falsey gate, or a gate that raises all REFUSE. While the policy
    constants are PROVISIONAL this is the only thing that makes them safe.

    Modes: `off` refuses inertly (nothing computed, nothing written); `shadow`
    refuses but LOGS the counterfactual (this is the shipping mode); `on`
    evaluates the gate and, on success, publishes `pins.json`."""
    current_mode = mode()
    scope = scope_of(user)
    stamp = time.time() if now is None else float(now)
    proposals = list(proposals or [])

    if current_mode == OFF:
        return ApplyResult(False, "mode_off", scope, current_mode)

    cf = counterfactual(proposals, user=user, now=stamp)
    cf["reason"] = str(reason or "")[:120]

    if current_mode == SHADOW:
        cf["applied"] = False
        cf["outcome"] = "shadow_counterfactual_only"
        _log_counterfactual(cf, user)
        return ApplyResult(False, "shadow", scope, current_mode,
                           pins=cf["pins"], counterfactual=cf)

    if gate_fn is None or not callable(gate_fn):
        cf["applied"] = False
        cf["outcome"] = "refused_no_gate"
        _log_counterfactual(cf, user)
        return ApplyResult(False, "no_benchmark_gate", scope, current_mode,
                           counterfactual=cf)

    before = {"pins": sorted(key(p["kind"], p["id"]) for p in _read_pins(user)),
              "tokens": sum(int(p.get("est_tokens", 0))
                            for p in _read_pins(user)),
              "scope": scope}
    after = {"pins": list(cf["pins"]), "tokens": cf["tokens"], "scope": scope}

    try:
        passed = bool(gate_fn(before, after))
    except Exception as err:                 # noqa: BLE001 - a gate that raises
        _capture("ctxheat.gate", err, context=scope)   # is a FAILED gate
        cf["applied"] = False
        cf["outcome"] = "refused_gate_error"
        _log_counterfactual(cf, user)
        return ApplyResult(False, "benchmark_gate_error", scope, current_mode,
                           counterfactual=cf, gate_called=True)

    if not passed:
        cf["applied"] = False
        cf["outcome"] = "refused_gate_failed"
        _log_counterfactual(cf, user)
        return ApplyResult(False, "benchmark_gate_failed", scope, current_mode,
                           counterfactual=cf, gate_called=True)

    pins = [{"id": p.item_id, "kind": p.kind, "pinned_at": stamp,
             "est_tokens": int(p.est_tokens), "score": round(float(p.score), 6),
             "verifier_ok": int(p.verifier_ok)}
            for p in proposals if p.pinned]
    path = pins_path(user)
    if path is None:
        return ApplyResult(False, "scope_unavailable", scope, current_mode,
                           counterfactual=cf, gate_called=True)
    try:
        with proclock.lock(_lock_name(user), timeout=10.0):
            _atomic_write_json(path, {
                "schema_version": SCHEMA_VERSION, "scope": scope,
                "applied_at": stamp, "gate": "passed",
                "reason": str(reason or "")[:120],
                "provisional": provisional(), "pins": pins})
    except (OSError, TimeoutError, ValueError) as err:
        _capture("ctxheat.apply", err, context=scope)
        return ApplyResult(False, "write_failed", scope, current_mode,
                           counterfactual=cf, gate_called=True)

    cf["applied"] = True
    cf["outcome"] = "applied_after_gate"
    _log_counterfactual(cf, user)
    return ApplyResult(True, "applied", scope, current_mode,
                       pins=[key(p["kind"], p["id"]) for p in pins],
                       counterfactual=cf, gate_called=True)


def clear_pins(user: str | None = None) -> bool:
    """Remove the applied pin set (the one-step rollback to static placement).
    Always allowed — unpinning needs no benchmark, it IS the safe direction."""
    path = pins_path(user)
    if path is None or not path.exists():
        return False
    try:
        with proclock.lock(_lock_name(user), timeout=10.0):
            path.unlink()
    except (OSError, TimeoutError) as err:
        _capture("ctxheat.clear_pins", err, context=str(path))
        return False
    return True
