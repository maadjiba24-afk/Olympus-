"""Exact-owner context heat and recoverable, externally gated pins (M07).

Default-off; shadow records proposals without promotion. PROVISIONAL policy
cannot activate. Unavailable/unclaimed state is preserved and cannot qualify.
Telemetry alone cannot promote; owned completed verifier receipts bind exact
owner, source revision, run and non-replaying event identity. The existing
provisional policy remains unqualified for activation. Broader verifier and
benchmark qualification belongs to M15/M16; native process locking to M13.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import config, memory
from . import ctxheat_state as state
import copy
import contextlib

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
    return min(100.0, max(0.0, _float_env("OLYMPUS_CTXHEAT_HYSTERESIS", DEFAULT_HYSTERESIS)))


def halflife_days() -> float:
    """PROVISIONAL. Heat halves after this many days without use."""
    return min(36500.0, max(0.01, _float_env("OLYMPUS_CTXHEAT_HALFLIFE_DAYS",
                                DEFAULT_HALFLIFE_DAYS)))


def protect_n() -> int:
    """PROVISIONAL. Verifier-accepted uses that make an item eviction-protected
    against a never-useful challenger."""
    return max(1, _int_env("OLYMPUS_CTXHEAT_PROTECT_N", DEFAULT_PROTECT_N))


def min_verified() -> int:
    """PROVISIONAL. Verifier acceptances required before an item is pinnable at
    all — the hard half of the poisoning defence."""
    return max(1, _int_env("OLYMPUS_CTXHEAT_MIN_VERIFIED", DEFAULT_MIN_VERIFIED))


def max_swaps() -> int:
    """PROVISIONAL. Pin-set changes allowed in one proposal (cache stability)."""
    return min(1000, max(0, _int_env("OLYMPUS_CTXHEAT_MAX_SWAPS", DEFAULT_MAX_SWAPS)))


def max_pins() -> int:
    """PROVISIONAL. Hard cap on the number of pinned items."""
    return min(1000, max(0, _int_env("OLYMPUS_CTXHEAT_MAX_PINS", DEFAULT_MAX_PINS)))


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
    except (TypeError, ValueError, OverflowError):
        return None
    return val if math.isfinite(val) else None


def _float_env(name: str, default: float) -> float:
    val = _to_float(os.environ.get(name, "").strip())
    return default if val is None or abs(val) > 1000000 else val


def _int_env(name: str, default: int) -> int:
    val = _to_float(os.environ.get(name, "").strip())
    return default if val is None or abs(val) > 1000000 or not val.is_integer() else int(val)


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


def _clean_id(item_id) -> str | None:
    """An item id, or None if it is not a reference-shaped token.

    Rejecting (rather than sanitising) is the point: a sanitiser would happily
    turn a sentence of user text into a dashed slug and store it. An id must
    already look like what the gated stores emit — a uuid, a slug, a path-free
    token."""
    text = item_id if isinstance(item_id, str) else ""
    return text if _ID_CHARS.fullmatch(text) else None


def _clean_kind(kind) -> str | None:
    text = kind if isinstance(kind, str) else ""
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
    if _clean_id(entry.get("id")) is None or _clean_id(entry.get("id")) != entry.get("id"):
        return False
    if _clean_kind(entry.get("kind")) is None or _clean_kind(entry.get("kind")) != entry.get("kind"):
        return False
    if entry.get("provenance") not in PROVENANCES:
        return False
    for name in _COUNTER_FIELDS:
        val = entry.get(name)
        if not isinstance(val, int) or isinstance(val, bool) or not 0 <= val <= state.MAX_COUNT:
            return False
    for name in _FLOAT_FIELDS:
        val = entry.get(name)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return False
        if not 0 <= val <= state.MAX_TIME or not math.isfinite(val):
            return False
    if entry['first'] > entry['last']:
        return False
    if entry['avoided_cost'] > MAX_AVOIDED_COST_USD or entry['avoided_latency'] > MAX_AVOIDED_LATENCY_MS:
        return False
    return True



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
    half = halflife_days() if halflife is None else max(0.01, min(36500.0, _num(halflife, DEFAULT_HALFLIFE_DAYS)))
    stamp = time.time() if now is None else max(0.0, min(state.MAX_TIME, _num(now)))

    verifier_ok = min(state.MAX_COUNT, max(0.0, _num(entry.get("verifier_ok"))))
    reuse = min(state.MAX_COUNT, max(0.0, _num(entry.get("reuse"))))
    cost = min(max(0.0, _num(entry.get("avoided_cost"))), MAX_AVOIDED_COST_USD)
    latency_ms = min(max(0.0, _num(entry.get("avoided_latency"))),
                     MAX_AVOIDED_LATENCY_MS)
    hits = min(state.MAX_COUNT, max(0.0, _num(entry.get("hits"))))
    useful = min(state.MAX_COUNT, max(0.0, _num(entry.get("useful"))))
    corrections = min(state.MAX_COUNT, max(0.0, _num(entry.get("corrections"))))

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
                if val is not None and 0 < val <= 1000000:
                    return max(1, math.ceil(val))
    return int(DEFAULT_EST_TOKENS.get(entry.get("kind", ""), 200))


def explain(item_id: str, kind: str, *, user: str | None = None,
            now: float | None = None) -> dict:
    """Operator answer to "why is this item hot (or not)?" — the score, its
    components, pin eligibility and eviction protection. Never raises."""
    found = entry(item_id, kind, user=user)
    if not found:
        return {"found": False, "id": str(item_id), "kind": str(kind),
                "scope": scope_of(user), "score": 0.0}
    stamp = time.time() if now is None else max(0.0, min(state.MAX_TIME, _num(now)))
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


def _select(budget_tokens: int | None = None, *, user: str | None = None,
                 now: float | None = None, est_tokens: dict | None = None,
                 incumbents=None, _ledger=None) -> list[PinProposal]:
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
    stamp = time.time() if now is None else max(0.0, min(state.MAX_TIME, _num(now)))
    ledger = _ledger if _ledger is not None else _load(user)
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



def scope_of(user=None):
    return 'owner:' + state.owner(user)


def _scope_dir(user=None):
    return state.path(user)


def ledger_path(user=None):
    return state.path(user) / state.STATE


def pins_path(user=None):
    """Pins and heat share one authority; this is a diagnostic path only."""
    return ledger_path(user)


def shadow_log_path(user=None):
    return ledger_path(user)


def _lock_name(user=None):
    return 'ctxheat-' + state.digest(state.owner(user))


def _load(user=None):
    found = state.read(user)
    return {} if found is None else found['entries']


def entries(user=None):
    return _load(user) if enabled() else {}


def entry(item_id, kind, *, user=None):
    return entries(user).get(key(kind, item_id))


def stats():
    return dict(_STATS)


def _stamp(now=None):
    value = time.time() if now is None else now
    state.number(value)
    return float(value)


def memory_revision(user, row):
    """Revision of the actual prompt item, unaffected by touch/counters."""
    from . import usermem
    if type(row) is not dict or row.get('status') != usermem.ACTIVE:
        return None
    if (not _clean_id(row.get('id')) or row.get('type') not in usermem.TYPES
            or not isinstance(row.get('content'), str) or len(row['content']) > 600):
        raise state.StateError('source_unavailable')
    return state.digest({'version':1,'owner':state.owner(user),'namespace':'usermem.memories',
                         'id':row['id'],'type':row['type'],'content':row['content']})


def resolve_source(user, kind, item_id):
    """Only the existing recall memory consumer has a qualified adapter.

    Other kinds may accumulate non-authoritative telemetry, but cannot qualify
    until their actual producer/consumer receives its own reviewed adapter.
    This function reads the M02 exact-owner API; it never initializes a store.
    """
    if kind != 'memory':
        return None
    from . import usermem
    try:
        row = usermem.get_memory(state.owner(user), item_id)
        rev = memory_revision(user, row)
    except Exception as exc:
        raise state.StateError('source_unavailable') from exc
    if rev is None:
        return None
    return {'revision':rev,'est_tokens':len(row['content']) // 4 + 4}


@contextlib.contextmanager
def _source_guard(user):
    from . import usermem, owner_evidence
    try:
        with usermem._guard(state.owner(user)):
            yield
    except owner_evidence.OwnerEvidenceStateError as exc:
        raise state.StateError('source_unavailable') from exc


@contextlib.contextmanager
def _transaction(user, *, sources=True):
    # One lock order: the source snapshot first, then the heat authority.
    # No source changes occur; nested M02 reads use its held snapshot.
    with _source_guard(user) if sources else contextlib.nullcontext():
        with state.transaction(user) as held:
            yield held


def _record_entry(data, item_id, kind, prov, stamp, source):
    ekey = key(kind, item_id)
    rev = source['revision'] if source else None
    if ekey in data['entries'] and data['bindings'][ekey] != rev:
        if len(data['retired']) >= state.MAX_ENTRIES:
            raise state.StateError('capacity')
        data['retired'].append({'entry':data['entries'].pop(ekey),
                                'revision':data['bindings'].pop(ekey), 'serial':data['serial']})
    if ekey not in data['entries']:
        if len(data['entries']) >= _MAX_ENTRIES:
            raise state.StateError('capacity')
        prior = next((row['entry'] for row in reversed(data['retired'])
                      if row['revision'] == rev and row['entry']['id'] == item_id
                      and row['entry']['kind'] == kind), None)
        data['entries'][ekey] = copy.deepcopy(prior) if prior else _new_entry(item_id, kind, prov, stamp)
        data['bindings'][ekey] = rev
    return data['entries'][ekey]


def record(item_id, kind, *, retrieved=False, useful=None, verifier_accepted=None,
           reused=False, avoided_cost_usd=0.0, avoided_latency_ms=0,
           corrected=False, user=None, provenance='unknown', now=None,
           source_revision=None):
    if not enabled():
        return False
    if verifier_accepted is not None:
        _STATS['verifier_claims_refused'] += 1
    try:
        exact = state.owner(user)
        if (_clean_id(item_id) is None or _clean_kind(kind) is None or _clean_id(item_id) != item_id or _clean_kind(kind) != kind
                or type(retrieved) is not bool or type(reused) is not bool
                or type(corrected) is not bool or useful not in (None, True, False)
                or useful is not None and type(useful) is not bool):
            raise ValueError('invalid observation')
        state.number(avoided_cost_usd, MAX_AVOIDED_COST_USD)
        state.number(avoided_latency_ms, MAX_AVOIDED_LATENCY_MS)
        stamp = _stamp(now)
        source = resolve_source(exact, kind, item_id)
        with _transaction(exact, sources=kind == "memory") as (directory, data):
            source = resolve_source(exact, kind, item_id)
            if source_revision is not None:
                state.revision(source_revision)
                if not source or source['revision'] != source_revision:
                    raise ValueError('retrieved source changed before recording')
            current = _record_entry(data,item_id,kind,_clean_provenance(provenance),stamp,source)
            current['hits'] += int(retrieved)
            current['useful'] += int(useful is True)
            current['reuse'] += int(reused)
            current['corrections'] += int(corrected)
            current['avoided_cost'] = min(MAX_AVOIDED_COST_USD, round(current['avoided_cost'] + avoided_cost_usd, 6))
            current['avoided_latency'] = min(MAX_AVOIDED_LATENCY_MS, round(current['avoided_latency'] + avoided_latency_ms, 3))
            current['last'] = max(stamp,current['last'])
            current['first'] = min(stamp,current['first'])
            state.publish(directory,data)
        _STATS['recorded'] += 1
        return True
    except (state.StateError, ValueError, TypeError, OverflowError):
        _STATS['rejected'] += 1
        return False


def record_verifier_outcome(item_id, accepted, *, source, kind=None, user=None,
                            provenance='orchestrator', now=None, evidence=None):
    """Ingest an ALREADY COMPLETED owned verifier receipt; never run a verifier.

    Trusted in-process producers supply exact owner, source, run/event ID,
    content revision and strict verdict. Labels alone no longer count. The
    closed vocabulary is not a signature or M15/M16 verifier qualification.
    There is deliberately no recall producer of these external outcomes.
    """
    if not enabled():
        return False
    try:
        exact = state.owner(user)
        if type(accepted) is not bool or source not in TRUSTED_VERIFIER_SOURCES:
            raise ValueError('unqualified outcome')
        state.fields(evidence, 'owner item kind revision source run_id event_id accepted observed_at')
        if (evidence['owner'] != exact or evidence['item'] != item_id
                or evidence['source'] != source or evidence['accepted'] is not accepted
                or kind is not None and evidence['kind'] != kind):
            raise ValueError('wrong attribution')
        kind = evidence['kind']
        if not _clean_id(item_id) or not _clean_kind(kind) or _clean_id(item_id) != item_id or _clean_kind(kind) != kind:
            raise ValueError('invalid identity')
        state.token(evidence['run_id']); state.token(evidence['event_id'])
        state.revision(evidence['revision']); state.number(evidence['observed_at'])
        if now is not None and _stamp(now) != evidence['observed_at']:
            raise ValueError('outcome timestamp mismatch')
        def same_receipt(existing):
            return existing is not None and {
                k:v for k,v in existing.items() if k not in ('evidence_digest','recorded_serial')
            } == evidence
        prior = state.read(exact)
        if prior is not None and evidence['event_id'] in prior['events']:
            # Recovery of a completed receipt does not require the source to
            # still exist. Confirm persistence without granting fresh evidence.
            with state.transaction(exact) as (directory, data):
                return same_receipt(data['events'].get(evidence['event_id']))
        resolved = resolve_source(exact,kind,item_id)
        if not resolved or resolved['revision'] != evidence['revision']:
            raise ValueError('stale or unresolved source')
        with _transaction(exact) as (directory,data):
            current_source = resolve_source(exact,kind,item_id)
            if not current_source or current_source['revision'] != evidence['revision']:
                raise ValueError('source changed before commit')
            existing = data['events'].get(evidence['event_id'])
            if existing is not None:
                return same_receipt(existing)
            ekey = key(kind,item_id)
            current = data['entries'].get(ekey)
            if not current or data['bindings'][ekey] != evidence['revision'] or current['hits'] == 0:
                raise ValueError('no attributed retrieval')
            if len(data['events']) >= state.MAX_EVENTS:
                raise state.StateError('capacity')
            event = dict(evidence, recorded_serial=data['serial']+1)
            event['evidence_digest'] = state.digest(event)
            data['events'][evidence['event_id']] = event
            current['verifier_ok' if accepted else 'corrections'] += 1
            current['last'] = max(current['last'],evidence['observed_at'])
            state.publish(directory,data)
        return True
    except (state.StateError, ValueError, TypeError, OverflowError):
        _STATS['rejected'] += 1
        return False


def _registry_entry():
    """Read committed qualification without mutable retest fallback or logging.

    Broader qualification remains a M15/M16 dependency. An unreviewed retest ledger
    cannot activate M07; a current committed ACTIVE entry and calibrated code
    are both necessary. No reader writes errors or repairs on damage.
    """
    from .gallery_state import Directory, _decode, GalleryError
    from . import experiments
    directory = None
    try:
        directory = Directory(Path(__file__).absolute().parent)
        registry = _decode(directory.read('experiments.json', 1024 * 1024))
        state.fields(registry, 'version entries')
        if type(registry['version']) is not int or registry['version'] != 1:
            raise ValueError('unsupported registry')
        state.collection(registry['entries'], list, 1000)
        ids = set()
        found = None
        for item in registry['entries']:
            if type(item) is not dict or not isinstance(item.get('id'), str) or item['id'] in ids:
                raise ValueError('invalid registry identity')
            ids.add(item['id'])
            if item['id'] == 'ctxheat-provisional-constants':
                if set(item) != set(experiments.REQUIRED_FIELDS) or any(
                        not isinstance(value, str) or len(value) > 16384 for value in item.values()):
                    raise ValueError('invalid qualification')
                found = item
        return found
    except (OSError, ValueError, GalleryError) as exc:
        raise state.StateError('qualification_unavailable') from exc
    finally:
        if directory is not None:
            directory.close()


def promotion_qualified():
    import datetime
    registered = _registry_entry()
    if PROVISIONAL is not False or not registered or registered.get('status') != 'active':
        return False
    try:
        tested = datetime.date.fromisoformat(registered['last_tested'])
        review = datetime.date.fromisoformat(registered['next_review'])
        today = datetime.date.today()
        return tested <= today <= review and bool(registered['outcome'].strip())
    except (KeyError, TypeError, ValueError):
        return False


def _policy():
    return {'hysteresis':hysteresis(),'halflife':halflife_days(),'protect_n':protect_n(),
            'min_verified':min_verified(),'max_swaps':max_swaps(),'max_pins':max_pins(),
            'budget':pin_budget_tokens(),'mode':mode(),'qualified':promotion_qualified(),
            'registry':state.digest(_registry_entry())}


def _rollback_policy():
    """A nonqualifying receipt needs neither registry nor source availability."""
    return {'hysteresis':DEFAULT_HYSTERESIS,'halflife':DEFAULT_HALFLIFE_DAYS,
            'protect_n':DEFAULT_PROTECT_N,'min_verified':DEFAULT_MIN_VERIFIED,
            'max_swaps':DEFAULT_MAX_SWAPS,'max_pins':DEFAULT_MAX_PINS,
            'budget':DEFAULT_PIN_BUDGET_TOKENS,'mode':OFF,'qualified':False,
            'registry':state.digest(None)}


def _sources(data):
    with _source_guard(data['owner']):
        return _sources_unlocked(data)


def _sources_unlocked(data):
    sources, estimates = {}, {}
    for ekey, item in data['entries'].items():
        resolved = resolve_source(data['owner'],item['kind'],item['id'])
        if resolved is not None and data['bindings'][ekey] == resolved['revision']:
            state.revision(resolved['revision']); state.integer(resolved['est_tokens'],1,1000000)
            sources[ekey] = resolved['revision']
            estimates[ekey] = resolved['est_tokens']
    return sources, estimates


def _binding(data, stamp):
    sources, estimates = _sources(data)
    return {'owner':data['owner'],'nonce':data['nonce'],'serial':data['serial'],
            'at':stamp,'ledger':state.digest({'entries':data['entries'],'bindings':data['bindings']}),
            'pins':state.digest(data['pins']),'policy':_policy(),
            'sources':sources,'estimates':estimates}


def _validate_binding(binding, exact):
    state.fields(binding,'owner nonce serial at ledger pins policy sources estimates')
    if binding['owner'] != exact:
        raise ValueError('wrong proposal owner')
    state.token(binding['nonce']); state.integer(binding['serial']); state.number(binding['at'])
    state.revision(binding['ledger']); state.revision(binding['pins'])
    policy = binding['policy']
    state.fields(policy,'hysteresis halflife protect_n min_verified max_swaps max_pins budget mode qualified registry')
    state.number(policy['hysteresis'],100); state.number(policy['halflife'],36500)
    if policy['halflife'] < .01:
        raise ValueError('invalid half-life')
    for name in ('protect_n','min_verified'):
        state.integer(policy[name],1,1000000)
    state.integer(policy['max_swaps'],0,1000); state.integer(policy['max_pins'],0,1000)
    state.integer(policy['budget'],0,1000000); state.revision(policy['registry'])
    if policy['mode'] not in (OFF,SHADOW,ON) or type(policy['qualified']) is not bool:
        raise ValueError('invalid policy')
    state.collection(binding['sources'],dict,_MAX_ENTRIES)
    state.collection(binding['estimates'],dict,_MAX_ENTRIES)
    if set(binding['sources']) != set(binding['estimates']):
        raise ValueError('missing measured size')
    for ekey, rev in binding['sources'].items():
        knd, sep, iid = ekey.partition(':')
        if not sep or _clean_id(iid) != iid or _clean_kind(knd) != knd:
            raise ValueError('invalid source reference')
        state.revision(rev); state.integer(binding['estimates'][ekey],1,1000000)


def _validate_proposals(rows):
    state.collection(rows,list,2000)
    seen = set()
    for row in rows:
        state.fields(row,'id kind action score est_tokens verifier_ok reason')
        if (not _clean_id(row['id']) or not _clean_kind(row['kind']) or _clean_id(row['id']) != row['id'] or _clean_kind(row['kind']) != row['kind']
                or row['action'] not in (KEEP,ADD,DROP)
                or row['reason'] not in ('incumbent_retained','highest_value_density','displaced','stale_or_ineligible')):
            raise ValueError('invalid proposal')
        state.number(row['score'],1e12); state.integer(row['est_tokens'],0 if row['action']==DROP else 1,1000000)
        state.integer(row['verifier_ok'])
        ekey = key(row['kind'],row['id'])
        if ekey in seen:
            raise ValueError('duplicate proposal')
        seen.add(ekey)


@dataclass(frozen=True)
class ProposalSet:
    items: tuple
    binding: dict

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]


def propose_pins(budget_tokens=None, *, user=None, now=None, est_tokens=None, incumbents=None):
    if not enabled():
        return []
    data = state.read(user)
    if data is None:
        return []
    stamp = _stamp(now)
    binding = _binding(data,stamp)
    _validate_binding(binding,data['owner'])
    # Caller hints may increase estimates, never make a measured source cheaper.
    estimates = dict(binding['estimates'])
    if est_tokens is not None:
        state.collection(est_tokens,dict,_MAX_ENTRIES)
        for ekey in estimates:
            iid = ekey.partition(':')[2]
            hinted = est_tokens.get(ekey,est_tokens.get(iid,estimates[ekey]))
            state.integer(hinted,1,1000000)
            estimates[ekey] = max(estimates[ekey],hinted)
    # A non-default exploratory budget/incumbent list is a pure scenario, never
    # authority. Apply recomputes the authoritative policy before a gate runs.
    budget = pin_budget_tokens() if budget_tokens is None else budget_tokens
    state.integer(budget,0,1000000)
    ledger = {k:e for k,e in data['entries'].items() if k in binding['sources']}
    pins = data['pins'] if incumbents is None else incumbents
    items = _select(budget,user=data['owner'],now=stamp,est_tokens=estimates,incumbents=pins,_ledger=ledger)
    return ProposalSet(tuple(items),binding)


def _rows(proposals):
    if not isinstance(proposals,ProposalSet):
        raise ValueError('an attributed proposal set is required')
    for p in proposals:
        if type(p) is not PinProposal or type(p.est_tokens) is not int or type(p.verifier_ok) is not int or type(p.score) not in (int,float):
            raise ValueError('invalid proposal types')
    rows = [p.to_dict() for p in proposals]
    _validate_proposals(rows)
    return rows


def _matches(data, binding, expected_serial):
    if data['serial'] != expected_serial:
        return False
    fresh = _binding(data,binding['at'])
    fresh['serial'] = binding['serial']
    return fresh == binding


def _authoritative(data, binding):
    ledger = {k:e for k,e in data['entries'].items() if k in binding['sources']}
    return [p.to_dict() for p in _select(binding['policy']['budget'],user=data['owner'],
            now=binding['at'],est_tokens=binding['estimates'],incumbents=data['pins'],_ledger=ledger)]


@dataclass(frozen=True)
class ApplyResult:
    applied: bool
    reason: str
    scope: str = ''
    mode: str = OFF
    pins: list = field(default_factory=list)
    counterfactual: dict = field(default_factory=dict)
    gate_called: bool = False
    operation_id: str | None = None
    active: bool = False

    def to_dict(self):
        return dict(self.__dict__)


def _result(reason, user, operation_id=None, gate_called=False, applied=False, pins=None, active=False):
    try:
        scope = scope_of(user)
    except state.StateError:
        scope = ''
    return ApplyResult(applied,reason,scope,mode(),pins or [],{},gate_called,operation_id,active)


def _operation_pins(op):
    return [{'id':p['id'],'kind':p['kind'],'pinned_at':op['created'],
             'score':p['score'],'est_tokens':p['est_tokens'],'verifier_ok':p['verifier_ok'],
             'revision':op['binding']['sources'][key(p['kind'],p['id'])]}
            for p in op['proposals'] if p['action'] != DROP]


def _replayed(data, operation_id):
    op=data['operations'][operation_id]
    applied=op['status']=='applied'
    active=bool(data['gate'] and data['gate']['operation_id']==operation_id)
    return _result(op['status'],data['owner'],operation_id,applied=applied,
                   pins=_operation_pins(op) if applied else [],active=active)


def counterfactual(proposals, *, user=None, now=None):
    rows = [p.to_dict() for p in proposals or []]
    _validate_proposals(rows)
    keeps = [p for p in proposals or [] if p.action == KEEP]
    adds = [p for p in proposals or [] if p.action == ADD]
    drops = [p for p in proposals or [] if p.action == DROP]
    return {'ts':_stamp(now),'scope':scope_of(user),'mode':mode(),'provisional':provisional(),
            'pins':sorted(p.key for p in keeps+adds),'add':sorted(p.key for p in adds),
            'drop':sorted(p.key for p in drops),'keep':sorted(p.key for p in keeps),
            'tokens':sum(p.est_tokens for p in keeps+adds),'swaps':len(adds)}


def gate_pins(proposals, gate_fn=None, *, user=None, operation_id=None, now=None):
    if mode() == OFF:
        return _result('off',user,operation_id)
    called = False
    try:
        exact = state.owner(user)
        stamp = _stamp(now)
        rows = _rows(proposals)
        binding = copy.deepcopy(proposals.binding)
        _validate_binding(binding,exact)
        if mode() == SHADOW or gate_fn is None:
            reason = 'shadow' if mode() == SHADOW else 'no_benchmark_gate'
            with _transaction(exact) as (directory,data):
                if not _matches(data,binding,binding['serial']):
                    return _result('stale',exact,operation_id)
                state.shadow(data,reason,rows,stamp); state.publish(directory,data)
            return _result(reason,exact,operation_id)
        state.token(operation_id)
        if not promotion_qualified():
            return _result('promotion_unqualified',exact,operation_id)
        request = state.digest({'binding':binding,'proposals':rows,'kind':'gate'})
        with _transaction(exact) as (directory,data):
            previous = data['operations'].get(operation_id)
            if previous:
                if previous['request'] != request:
                    return _result('operation_conflict',exact,operation_id)
                return _replayed(data,operation_id)
            if not _matches(data,binding,binding['serial']) or rows != _authoritative(data,binding):
                return _result('stale_or_ineligible',exact,operation_id)
            if len(data['operations']) >= state.MAX_OPERATIONS:
                raise state.StateError('capacity')
            op = {'kind':'gate','request':request,'status':'evaluating','created':stamp,
                  'revision':data['serial']+1,'binding':binding,'proposals':rows,'result':'pending'}
            data['operations'][operation_id] = op
            state.publish(directory,data)
            expected_serial = data['serial']
        # An external gate is called once, only after its durable reservation.
        # Exceptions, interruption or an unknown reply never become a pass.
        before = {'owner':exact,'revision':binding['serial'],'pins_digest':binding['pins'],
                  'ledger_digest':binding['ledger'],'policy':copy.deepcopy(binding['policy'])}
        after = {'owner':exact,'request_digest':request,'sources':copy.deepcopy(binding['sources']),
                 'proposals':copy.deepcopy(rows)}
        called = True
        try:
            verdict = gate_fn(before,after)
            verdict_reason = 'benchmark_passed' if verdict is True else 'benchmark_failed'
        except Exception:
            verdict = False
            verdict_reason = 'gate_error'
        with _transaction(exact) as (directory,data):
            op = data['operations'][operation_id]
            if not _matches(data,binding,expected_serial):
                verdict = False; verdict_reason = 'stale'
            op['status'] = 'qualified' if verdict is True else 'refused'
            op['result'] = verdict_reason; op['revision'] = data['serial']+1
            state.shadow(data,'qualified' if verdict is True else 'refused',rows,stamp)
            state.publish(directory,data)
        return _result('qualified' if verdict is True else verdict_reason,exact,operation_id,called)
    except state.StateError as exc:
        return _result(exc.code,user,operation_id,called)
    except (ValueError,TypeError,OverflowError,AttributeError):
        return _result('invalid_proposal',user,operation_id,called)


def apply_gate(operation_id, *, user=None):
    if mode() != ON:
        return _result(mode(),user,operation_id)
    try:
        exact = state.owner(user); state.token(operation_id)
        if not promotion_qualified():
            return _result('promotion_unqualified',exact,operation_id)
        if state.read(exact) is None:
            return _result('missing_operation',exact,operation_id)
        with _transaction(exact) as (directory,data):
            op = data['operations'].get(operation_id)
            if not op or op['kind'] != 'gate':
                return _result('missing_operation',exact,operation_id)
            if op['status'] == 'applied':
                return _replayed(data,operation_id)
            if op['status'] != 'qualified':
                return _result(op['status'],exact,operation_id)
            binding = op['binding']
            if not _matches(data,binding,op['revision']) or op['proposals'] != _authoritative(data,binding):
                op['status']='refused'; op['result']='stale'; op['revision']=data['serial']+1
                state.publish(directory,data)
                return _result('stale',exact,operation_id)
            pins = [{'id':p['id'],'kind':p['kind'],'pinned_at':op['created'],
                     'score':p['score'],'est_tokens':p['est_tokens'],'verifier_ok':p['verifier_ok'],
                     'revision':binding['sources'][key(p['kind'],p['id'])]}
                    for p in op['proposals'] if p['action'] != DROP]
            op['status']='applied'; op['result']='applied'; op['revision']=data['serial']+1
            data['pins']=pins
            data['gate']={'operation_id':operation_id,'pins_digest':state.digest(pins)}
            state.shadow(data,'applied',op['proposals'],op['created'])
            state.publish(directory,data)
        return _result('applied',exact,operation_id,applied=True,pins=pins,active=True)
    except state.StateError as exc:
        return _result(exc.code,user,operation_id)
    except (ValueError,TypeError,OverflowError):
        return _result('invalid_operation',user,operation_id)


def apply_pins(proposals, gate_fn=None, *, user=None, reason='pin_selection', now=None, operation_id=None):
    result = gate_pins(proposals,gate_fn,user=user,operation_id=operation_id,now=now)
    if result.reason == 'qualified':
        applied = apply_gate(operation_id,user=user)
        return replace(applied,gate_called=result.gate_called)
    return result


def _read_pins(user=None):
    data = state.read(user)
    return [] if data is None else data['pins']


def applied_pins(user=None):
    if mode() != ON or not promotion_qualified():
        return []
    data = state.read(user)
    if not data or not data['gate']:
        return []
    op = data['operations'][data['gate']['operation_id']]
    if op['binding']['policy'] != _policy():
        return []
    for pin in data['pins']:
        source = resolve_source(data['owner'],pin['kind'],pin['id'])
        if not source or source['revision'] != pin['revision']:
            return []
    return data['pins']


def shadow_log(user=None, limit=50):
    state.integer(limit,0,state.MAX_SHADOW)
    data=state.read(user)
    return [] if data is None or limit==0 else data['shadow'][-limit:]


def rollback_pins(*, user=None, operation_id=None):
    """Publish an explicit empty-pin receipt, including while mode is off."""
    try:
        exact=state.owner(user); state.token(operation_id)
        if state.read(exact) is None:
            return _result('missing',exact,operation_id)
        with state.transaction(exact) as (directory,data):
            previous=data['operations'].get(operation_id)
            if previous:
                return _result('rolled_back' if previous['kind']=='rollback' else 'operation_conflict',exact,operation_id,
                               active=state.latest_pin_operation(data)==operation_id)
            if len(data['operations'])>=state.MAX_OPERATIONS:
                raise state.StateError('capacity')
            stamp=_stamp()
            # Rollback needs no source availability or promotion qualification.
            binding={'owner':exact,'nonce':data['nonce'],'serial':data['serial'],'at':stamp,
                     'ledger':state.digest({'entries':data['entries'],'bindings':data['bindings']}),
                     'pins':state.digest(data['pins']),'policy':_rollback_policy(),'sources':{},'estimates':{}}
            op={'kind':'rollback','request':state.digest({'binding':binding,'proposals':[],'kind':'rollback'}),
                'status':'rolled_back','created':stamp,'revision':data['serial']+1,
                'binding':binding,'proposals':[],'result':'rolled_back'}
            data['operations'][operation_id]=op; data['pins']=[]; data['gate']=None
            state.shadow(data,'rolled_back',[],stamp); state.publish(directory,data)
        return _result('rolled_back',exact,operation_id,active=True)
    except state.StateError as exc:
        return _result(exc.code,user,operation_id)
    except (ValueError,TypeError,OverflowError):
        return _result('invalid_operation',user,operation_id)


def clear_pins(user=None, *, operation_id=None):
    return rollback_pins(user=user,operation_id=operation_id).reason == 'rolled_back'


def status(user=None, *, operation_id=None):
    try:
        exact=state.owner(user); data=state.read(exact)
        out={'status':'missing' if data is None else 'available','owner':exact,'mode':mode(),
             'promotion_qualified':False,'qualification_status':'unavailable',
             'entries':len(data['entries']) if data else 0,
             'stored_pins':len(data['pins']) if data else 0,'serial':data['serial'] if data else None,
             'topology':'single-process' if os.name=='nt' else 'local-filesystem-flock',
             'directory_fsync':os.name!='nt'}
        if operation_id is not None:
            state.token(operation_id)
            op=data['operations'].get(operation_id) if data else None
            out['operation']={'id':operation_id,'status':op['status'],'result':op['result'],
                              'active':state.latest_pin_operation(data)==operation_id} if op else None
        try:
            out['promotion_qualified']=promotion_qualified()
            out['qualification_status']='qualified' if out['promotion_qualified'] else 'unqualified'
        except state.StateError as exc:
            out['qualification_status']=exc.code
        return out
    except state.StateError as exc:
        return {'status':exc.code,'mode':mode(),'promotion_qualified':False}


def liveness():
    data=state.read()
    if data is None:
        return {'hits':0,'misses':0,'note':'no attributed history'}
    # Self-reported reuse is never proof that a qualified pin avoided work.
    return {'hits':0,'misses':sum(e['hits'] for e in data['entries'].values()),
            'savings':None,'overhead':None,'net_benefit':None,
            'note':'owned activation evidence unavailable; mode='+mode()}
