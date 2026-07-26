"""Routing substitution within measured quality bands (Wave-2 Capability C3).

Colibri's `CACHE_ROUTE` translated: the only mechanism allowed to move work off
the quality-first pick for **cost or warmth**, and then only *inside a measured
band* — with per-decision agreement telemetry so the loss is permanently
visible. The three-part contract is unchanged from max-rank: **protect the head,
substitute only inside a band, telemeter the divergence.** What differs is that
Olympus's band is derived from the same measured evidence store the selector
already trusts (`modelgrade`, W2-C1), not from a hand-set rank.

Invariants (WAVE2 spec W2-C3):

  Every precondition below must hold to substitute, and each one blocks
  INDEPENDENTLY (there is no "mostly qualified"):

    candidate_available  a member that is genuinely cheaper or warmer exists
    modelgrade           the measured qualification store is importable AND on
    preferred_qualified  the incumbent is QUALIFIED for this cell
    candidate_qualified  the challenger is QUALIFIED for this cell
    quality_floor        the challenger's measured quality LOWER BOUND stays at
                         or above the cell's minimum quality floor
    quality_band         the challenger is within `OLYMPUS_ROUTESUB_BAND` of the
                         incumbent's lower bound (statistically interchangeable)
    verifier_floor       W2-I3.1 — on verification work the substituted route's
                         measured verification score stays at or above the
                         verifier floor
    verifier_protected   doctrine: the verify role is the head of the routing
                         and is not substituted at all (relaxable, the floor
                         above is NOT)
    security_class       the request's data classification permits substitution,
                         and substitution never crosses a locality boundary
    context_window       the challenger's context window fits the request
    tools                measured tool support when the request needs tools
    structured           measured structured-output support when required
    freshness            the evidence behind both cards is within TTL
    confidence           candidate confidence >= `OLYMPUS_ROUTESUB_CONFIDENCE`

  **W2-I3.1 Aletheia is NEVER substituted below its measured verification
  floor.** Enforced as an EXPLICIT EARLY GUARD inside the per-candidate
  assessment (the first thing checked once cards are in hand, before any
  cost/warmth reasoning can express a preference) AND re-asserted immediately
  before a substituting `Decision` is returned. Neither path is reachable by a
  flag: `OLYMPUS_ROUTESUB_PROTECT_VERIFIER=0` relaxes the *doctrine* block on
  the verify role, never the floor.

  **W2-I3.2 no silent quality degradation.** A substitution whose measured
  quality lower bound is below the incumbent's sets
  `Decision.user_visible_degradation` and MUST carry a non-empty
  `Decision.disclosure` (`requires_disclosure()`); refusal-over-silent-
  degradation is the Wave-2 universal rule R6.

Always recorded, per decision, to `MEMORY_DIR/routesub/decisions.jsonl`
(append-only, `proclock`-guarded, never raising into the caller): the original
preferred route, the substituted route, the reason, the estimated savings, the
actual cost, the latency, the verifier outcome, the disagreement flag, fallback
activation and user-visible degradation. `record_outcome()` appends the
completion half of a row; readers merge the two by `decision_id`.
`agreement_stats()` is the CACHE_ROUTE agreement-telemetry analog — substitution
rate, disagreement rate, estimated-vs-actual savings, per-cell breakdown — but
computed against OUTCOMES rather than a distributional divergence, which is the
whole point: lossy routing must stay visible and continuously re-priced.

Mode. `OLYMPUS_ROUTESUB=off|shadow|on`, **default off**.
  off     fully inert — no computation, no imports, no files, no decision.
  shadow  the counterfactual is computed and RECORDED, and the returned
          decision is ALWAYS "no substitution", so routing is unchanged.
  on      the substitution is returned.
Forced `off` under `OLYMPUS_REPLAY`, exactly like `learned_routing` and
`bandit_routing`: a replayed run reproduces its recorded decisions rather than
re-deciding them against a ledger that has since grown.

PROVISIONAL constants (Wave-2 universal rule: no uncalibrated inherited
constants ship enabled). `DEFAULT_BAND`, `DEFAULT_VERIFIER_FLOOR`,
`DEFAULT_CONFIDENCE` and `DEFAULT_EST_TOKENS` are not yet derived from measured
substitution telemetry; they ship in SHADOW mode only and are calibrated by the
routing owner from this module's own `agreement_stats()` output (the estimated-
vs-actual savings and disagreement-rate columns are exactly the calibration
input). Until then, `on` is an operator's explicit choice.

Scope note: this PR builds the DECISION LIBRARY. Nothing in the tree calls it —
`choose()` exists as the `learned_routing`/`bandit_routing` seam adapter for the
later wiring PR, and ships inert.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config, proclock

# --- modes ----------------------------------------------------------------
OFF = "off"
SHADOW = "shadow"
ON = "on"
MODES = (OFF, SHADOW, ON)

# --- PROVISIONAL defaults (see the module docstring) ----------------------
DEFAULT_BAND = 0.05           # measured-equivalence half-width (04-routing R3)
DEFAULT_VERIFIER_FLOOR = 0.90  # strictly above the general qualification floor
DEFAULT_CONFIDENCE = 0.70
DEFAULT_QUALITY_FLOOR = 0.70   # only used when modelgrade cannot be consulted
DEFAULT_TTL_DAYS = 30.0
DEFAULT_EST_TOKENS = 2000      # billed-token estimate when the caller gives none

#: Data classes that may be substituted at all. `restricted` never is.
DEFAULT_CLASSES = ("public", "internal")

#: The head of the routing. `aletheia` is the verifier; it is not a member of
#: `specialists.SPECIALISTS` (it is the orchestrator's verify stage), so it is
#: named here as well as detected through `config.specialist_role`.
VERIFIER_SPECIALISTS = ("aletheia",)
VERIFIER_ROLES = ("verify", "verifier", "verification")
VERIFIER_TASK_CLASSES = ("verify", "verifier", "verification")

#: Verifier verdicts that count as a disagreement with the preferred route's
#: expectation (the substituted route did NOT produce an accepted answer).
NEGATIVE_VERDICTS = ("reject", "rejected", "fail", "failed", "retry",
                     "negative", "disagree", "rework", "error")

#: Canonical precondition order. `blocked_by` names the FIRST failing entry;
#: every entry is evaluated so `checks` is always complete.
PRECONDITIONS = (
    "candidate_available",
    "modelgrade",
    "preferred_qualified",
    "candidate_qualified",
    "quality_floor",
    "quality_band",
    "verifier_floor",
    "verifier_protected",
    "security_class",
    "context_window",
    "tools",
    "structured",
    "freshness",
    "confidence",
)

#: Every field a decision row carries, always (W2-C3 §5). The outcome half is
#: present from the start (null/false) so a row is never structurally missing a
#: column an auditor expects.
ROW_FIELDS = (
    "kind", "decision_id", "ts", "mode", "specialist", "cell", "security_class",
    "preferred_route", "candidate_route", "substituted_route", "substituted",
    "counterfactual", "reason", "blocked_by", "checks", "confidence",
    "preferred_quality_lo", "candidate_quality_lo", "verification_score",
    "estimated_preferred_cost_usd", "estimated_candidate_cost_usd",
    "estimated_savings_usd", "actual_cost_usd", "latency_ms",
    "verifier_outcome", "disagreement", "fallback",
    "user_visible_degradation", "disclosure", "outcome_recorded",
)

_STATE_DIR = "routesub"
_LEDGER = "decisions.jsonl"
_LOCK = "routesub"

# Warmth ledger (the Olympus analog of Colibri's "resident expert"): the last
# time a member actually served a call in THIS process. Deliberately in-process
# and unpersisted — warmth is a property of the live connection/prompt cache,
# not a durable fact, and a stale on-disk value would be worse than none.
_WARMTH: dict[str, float] = {}


# --- flag + thresholds (config.py idioms: zero-arg, live-read, default off) --

def mode() -> str:
    """`off` | `shadow` | `on` (default `off`; unknown values fail safe to off).

    Forced `off` during replay, like both existing selectors — a replayed run
    must reproduce its RECORDED decision, not re-decide it."""
    if os.environ.get("OLYMPUS_REPLAY"):
        return OFF
    raw = os.environ.get("OLYMPUS_ROUTESUB", "").strip().lower()
    if raw == SHADOW:
        return SHADOW
    if raw in ("1", "on", "true", "yes", "enable", "enabled"):
        return ON
    return OFF


def enabled() -> bool:
    """Whether the module does anything at all (shadow counts — it records)."""
    return mode() != OFF


def band() -> float:
    """Measured-equivalence half-width: how far BELOW the incumbent's quality
    lower bound a challenger may sit and still count as interchangeable."""
    return max(0.0, _float_env("OLYMPUS_ROUTESUB_BAND", DEFAULT_BAND))


def verifier_floor() -> float:
    """W2-I3.1's hard floor: the measured verification lower bound a substituted
    route must keep on verification work. Deliberately ABOVE the general
    qualification floor — a merely `qualified` model is not a verifier."""
    val = _float_env("OLYMPUS_ROUTESUB_VERIFIER_FLOOR", DEFAULT_VERIFIER_FLOOR)
    return val if 0.0 <= val <= 1.0 else DEFAULT_VERIFIER_FLOOR


def confidence_threshold() -> float:
    """Minimum candidate confidence for a substitution."""
    val = _float_env("OLYMPUS_ROUTESUB_CONFIDENCE", DEFAULT_CONFIDENCE)
    return val if 0.0 <= val <= 1.0 else DEFAULT_CONFIDENCE


def protect_verifier() -> bool:
    """Doctrine block: the verify role is the head of the routing and is not
    substituted at all (04-routing R3 "the head is sacred"). ON by default;
    `OLYMPUS_ROUTESUB_PROTECT_VERIFIER=0` relaxes THIS rule only — the W2-I3.1
    floor below it is not relaxable by any flag."""
    raw = os.environ.get("OLYMPUS_ROUTESUB_PROTECT_VERIFIER", "").strip().lower()
    return raw not in ("0", "off", "false", "no")


def quality_floor(cell=None) -> float:
    """The cell's minimum quality floor for substitution.

    Defaults to `modelgrade`'s own promotion floor for the cell (one ladder,
    synthesis R1); `OLYMPUS_ROUTESUB_QUALITY_FLOOR` raises it for substitution
    specifically without touching qualification."""
    explicit = _float_env("OLYMPUS_ROUTESUB_QUALITY_FLOOR", float("nan"))
    if math.isfinite(explicit) and 0.0 <= explicit <= 1.0:
        return explicit
    mg = _modelgrade()
    if mg is not None:
        try:
            return float(mg.floor_for(cell))
        except Exception:                            # noqa: BLE001 - never raise
            pass
    return DEFAULT_QUALITY_FLOOR


def ttl_days() -> float:
    """Evidence freshness TTL for a substitution decision (defaults to
    `modelgrade`'s TTL — the same evidence, the same expiry)."""
    explicit = _float_env("OLYMPUS_ROUTESUB_TTL_DAYS", float("nan"))
    if math.isfinite(explicit) and explicit > 0:
        return explicit
    mg = _modelgrade()
    if mg is not None:
        try:
            return float(mg.ttl_days())
        except Exception:                            # noqa: BLE001
            pass
    return DEFAULT_TTL_DAYS


def allowed_classes() -> tuple[str, ...]:
    """Data classes a substitution may run under (`OLYMPUS_ROUTESUB_CLASSES`).

    `restricted` is absent by default and adding it does NOT bypass the locality
    rule below it: a local-only class still refuses a non-local candidate."""
    raw = os.environ.get("OLYMPUS_ROUTESUB_CLASSES", "").strip().lower()
    if not raw:
        return DEFAULT_CLASSES
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts or DEFAULT_CLASSES


def est_tokens_default() -> int:
    """Billed-token estimate used for savings when the caller gives none."""
    val = _float_env("OLYMPUS_ROUTESUB_EST_TOKENS", float(DEFAULT_EST_TOKENS))
    return int(val) if math.isfinite(val) and val > 0 else DEFAULT_EST_TOKENS


def _to_float(raw):
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def _float_env(name: str, default: float) -> float:
    val = _to_float(os.environ.get(name, "").strip())
    return default if val is None else val


def _capture(where: str, exc: BaseException, context: str = "") -> None:
    from . import errors                              # lazy: keep imports thin
    try:
        errors.capture(where, exc, context=context)
    except Exception:                                 # noqa: BLE001
        pass


# --- the measured-evidence ladder (lazy, fail-safe) -----------------------

def _import_modelgrade():
    """Import the qualification store. Separated so it can be made to fail in a
    test, and so this module imports cleanly in a tree where `modelgrade` does
    not exist yet."""
    return importlib.import_module("olympus.modelgrade")


def _modelgrade():
    """The qualification store, or None.

    None means FAIL-SAFE: no evidence ladder, therefore no substitution. A
    missing module, an import error and a disabled store are all the same
    answer — routing keeps the quality-first pick."""
    try:
        mg = _import_modelgrade()
        return mg if mg.enabled() else None
    except Exception:                                 # noqa: BLE001 - never raise
        return None


# --- member identity, price, warmth ---------------------------------------

def member_id(member) -> str:
    """`provider/model` — byte-identical to `modelgrade.member_of`, so a card
    lookup needs no translation."""
    provider = getattr(member, "provider", "") or ""
    model = getattr(member, "model", "") or ""
    return f"{provider}/{model}"


def price(member) -> float:
    """Rough $/Mtok for a member (live pricing when the runtime has it)."""
    try:
        return float(config.price_per_mtok(getattr(member, "model", "")))
    except Exception:                                 # noqa: BLE001
        return 0.0


def mark_warm(member, *, now: float | None = None) -> None:
    """Record that `member` just served a call — the residency signal.

    The later wiring PR calls this from the backend's call-completion path; it
    is a no-op for routing until then."""
    _WARMTH[member_id(member)] = time.time() if now is None else float(now)


def warmth(member) -> float:
    """Last-served timestamp for a member (0.0 = cold)."""
    return _WARMTH.get(member_id(member), 0.0)


def clear_warmth() -> None:
    """Forget the residency map (tests, and process-lifecycle boundaries)."""
    _WARMTH.clear()


def _is_local(member) -> bool:
    try:
        return bool(config.member_is_local(member))
    except Exception:                                 # noqa: BLE001
        return False


# --- cells ----------------------------------------------------------------

def _cell_key(mg, cell) -> str:
    if mg is not None:
        try:
            return str(mg.cell_key(cell))
        except Exception:                             # noqa: BLE001
            pass
    return str(cell if cell is not None else "any")


def cell_for(specialist, *, language: str = "any", context_tokens: int = 0,
             needs_tools: bool = False, needs_structured: bool = False):
    """Build the `modelgrade` task cell for a specialist's work.

    The task class comes from the EXISTING coarse tag (`routing_outcomes.
    task_type`) so substitution evidence lands in the same buckets the routing
    ledger already uses; verification work is tagged `verify` regardless, since
    that is the cell W2-I3.1's floor is measured on."""
    task = "general"
    try:
        from . import routing_outcomes
        task = routing_outcomes.task_type(str(specialist or ""))
    except Exception:                                 # noqa: BLE001
        pass
    if _is_verifier_name(specialist):
        task = "verify"
    mg = _modelgrade()
    if mg is None:
        return (f"{task}|{language}|any|"
                f"{'tools' if needs_tools else 'notools'}|"
                f"{'struct' if needs_structured else 'nostruct'}")
    return mg.cell_of(task, language, context_tokens,
                      needs_tools=needs_tools,
                      needs_structured=needs_structured)


def _is_verifier_name(specialist) -> bool:
    key = str(specialist or "").strip().lower()
    if not key:
        return False
    if key in VERIFIER_SPECIALISTS:
        return True
    try:
        return str(config.specialist_role(key)).strip().lower() in VERIFIER_ROLES
    except Exception:                                 # noqa: BLE001
        return False


def is_verification(specialist, cell_key: str = "") -> bool:
    """Whether this decision is about VERIFICATION work — by specialist identity
    (Aletheia / any `verify`-role specialist) or by the cell's task class, so a
    renamed caller cannot route around the floor."""
    if _is_verifier_name(specialist):
        return True
    task = str(cell_key or "").split("|", 1)[0].strip().lower()
    return task in VERIFIER_TASK_CLASSES


def verification_score(card) -> float:
    """The measured verification score of a card.

    On a `verify` cell the graded `quality` dimension IS the verification
    success rate (its observations come from verifier/eval outcomes, never a
    self-report), so its recency-weighted Wilson LOWER bound is the honest,
    small-sample-conservative reading of "how well does this member verify".
    No evidence => 0.0, which the floor then blocks."""
    return _dim(card, "quality").get("lo", 0.0)


def _dim(card, name: str) -> dict:
    try:
        val = (card.dimensions or {}).get(name) or {}
        return val if isinstance(val, dict) else {}
    except Exception:                                 # noqa: BLE001
        return {}


# --- the decision ---------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    """One substitution decision — the whole audit trail of a single choice.

    `substituted` is what ACTUALLY happened (always False in shadow mode);
    `counterfactual` is "shadow mode would have substituted". `selected` is the
    member the caller should use, and is the preferred member unless
    `substituted` is True."""

    decision_id: str
    ts: float
    mode: str
    specialist: str
    cell: str
    security_class: str
    preferred_route: str
    candidate_route: str
    substituted: bool
    counterfactual: bool
    reason: str
    blocked_by: str
    checks: dict = field(default_factory=dict)
    confidence: float = 0.0
    preferred_quality_lo: float = 0.0
    candidate_quality_lo: float = 0.0
    verification_score: float = 0.0
    estimated_preferred_cost_usd: float = 0.0
    estimated_candidate_cost_usd: float = 0.0
    estimated_savings_usd: float = 0.0
    actual_cost_usd: float | None = None
    latency_ms: int | None = None
    verifier_outcome: str = ""
    disagreement: bool = False
    fallback: bool = False
    user_visible_degradation: bool = False
    disclosure: str = ""
    outcome_recorded: bool = False
    recorded: bool = False
    selected: object = None

    @property
    def substituted_route(self) -> str:
        """The route that actually replaced the preferred one ("" when none)."""
        return self.candidate_route if self.substituted else ""

    def requires_disclosure(self) -> bool:
        """W2-I3.2: does this decision have to be disclosed to the user?

        True whenever the evaluated substitution could lower the user-visible
        answer quality. A True here is ALWAYS accompanied by a non-empty
        `disclosure` — silent degradation is the thing the invariant forbids."""
        return bool(self.user_visible_degradation)

    def to_row(self) -> dict:
        """The always-recorded row (W2-C3 §5). Every `ROW_FIELDS` key present."""
        return {
            "kind": "decision",
            "decision_id": self.decision_id,
            "ts": self.ts,
            "mode": self.mode,
            "specialist": self.specialist,
            "cell": self.cell,
            "security_class": self.security_class,
            "preferred_route": self.preferred_route,
            "candidate_route": self.candidate_route,
            "substituted_route": self.substituted_route,
            "substituted": self.substituted,
            "counterfactual": self.counterfactual,
            "reason": self.reason,
            "blocked_by": self.blocked_by,
            "checks": self.checks,
            "confidence": self.confidence,
            "preferred_quality_lo": self.preferred_quality_lo,
            "candidate_quality_lo": self.candidate_quality_lo,
            "verification_score": self.verification_score,
            "estimated_preferred_cost_usd": self.estimated_preferred_cost_usd,
            "estimated_candidate_cost_usd": self.estimated_candidate_cost_usd,
            "estimated_savings_usd": self.estimated_savings_usd,
            "actual_cost_usd": self.actual_cost_usd,
            "latency_ms": self.latency_ms,
            "verifier_outcome": self.verifier_outcome,
            "disagreement": self.disagreement,
            "fallback": self.fallback,
            "user_visible_degradation": self.user_visible_degradation,
            "disclosure": self.disclosure,
            "outcome_recorded": self.outcome_recorded,
        }


# --- storage --------------------------------------------------------------

def _dir(create: bool = False) -> Path:
    d = config.MEMORY_DIR / _STATE_DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def ledger_path(create: bool = False) -> Path:
    """`MEMORY_DIR/routesub/decisions.jsonl`. Never created by a read."""
    return _dir(create) / _LEDGER


def _append(row: dict) -> bool:
    """Append one row under the cross-process lock. Never raises: telemetry
    must not be able to break a routing decision."""
    try:
        line = json.dumps(row, sort_keys=True, default=str)
        with proclock.lock(_LOCK, timeout=10.0):
            with ledger_path(create=True).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return True
    except Exception as err:                          # noqa: BLE001 - never raise
        _capture("routesub.append", err,
                 context=str(row.get("decision_id", "")))
        return False


def _read_rows() -> list[dict]:
    """Every raw ledger row. A torn/unparseable line is SKIPPED, never repaired
    and never raised — a damaged telemetry line must not cost the whole view."""
    path = ledger_path()
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        _capture("routesub.read", err, context=str(path))
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(row, dict) and row.get("decision_id"):
            rows.append(row)
    return rows


def decisions(days: float | None = None, *, now: float | None = None) -> list[dict]:
    """Recorded decisions, oldest-first, with their outcome half MERGED in.

    The ledger is append-only (a decision row, then later an outcome row under
    the same `decision_id`); merging happens on READ so nothing is ever
    rewritten in place."""
    now = time.time() if now is None else now
    merged: dict[str, dict] = {}
    for row in _read_rows():
        did = str(row["decision_id"])
        if row.get("kind") == "outcome":
            base = merged.get(did)
            if base is None:
                continue                    # an outcome without its decision
            for key, val in row.items():
                if key not in ("kind", "decision_id"):
                    base[key] = val
            base["outcome_recorded"] = True
        elif did not in merged:
            merged[did] = dict(row)
    out = list(merged.values())
    if days is not None:
        cutoff = now - float(days) * 86400.0
        out = [r for r in out if _to_float(r.get("ts")) is None
               or float(r["ts"]) >= cutoff]
    out.sort(key=lambda r: _to_float(r.get("ts")) or 0.0)
    return out


def decision(decision_id: str) -> dict | None:
    """One merged decision row by id, or None."""
    did = str(decision_id or "")
    for row in decisions():
        if row.get("decision_id") == did:
            return row
    return None


# --- evaluation -----------------------------------------------------------

def _inert(specialist, preferred, current_mode: str, reason: str) -> Decision:
    """A decision that changes nothing and records nothing."""
    return Decision(
        decision_id="", ts=time.time(), mode=current_mode,
        specialist=str(specialist or ""), cell="", security_class="",
        preferred_route=member_id(preferred), candidate_route="",
        substituted=False, counterfactual=False, reason=reason, blocked_by="",
        checks={}, selected=preferred)


def _candidates(members, preferred) -> list:
    """Members that are genuinely CHEAPER or WARMER than the preferred pick,
    best first (cheapest, then warmest, then model name for determinism).

    A member that is neither cheaper nor warmer has no reason to displace the
    quality-first pick, so it is not a candidate at all."""
    pref_id = member_id(preferred)
    pref_price = price(preferred)
    pref_warm = warmth(preferred)
    out = []
    for m in members or ():
        if member_id(m) == pref_id:
            continue
        cheaper = price(m) < pref_price - 1e-12
        warmer = warmth(m) > pref_warm
        if cheaper or warmer:
            out.append(m)
    out.sort(key=lambda m: (price(m), -warmth(m), member_id(m)))
    return out


def _assess(mg, *, preferred, candidate, specialist, cell, ckey, security_class,
            needs_tools, needs_structured, context_tokens, now) -> dict:
    """Evaluate EVERY precondition for one candidate. Pure w.r.t. the ledger.

    All checks are evaluated (never short-circuited) so `checks` is a complete,
    auditable picture of why a substitution did or did not happen; `ok` is the
    conjunction, so each precondition blocks independently."""
    checks: dict[str, dict] = {}

    def put(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": bool(ok), "detail": str(detail)[:240]}

    put("candidate_available", True,
        f"{member_id(candidate)} is cheaper/warmer than {member_id(preferred)}")

    if mg is None:
        put("modelgrade", False,
            "measured qualification store unavailable or disabled — fail-safe: "
            "no substitution without evidence")
        for name in PRECONDITIONS:
            if name not in checks:
                put(name, False, "not evaluated (no qualification store)")
        return {"checks": checks, "ok": False, "candidate": candidate,
                "preferred_lo": 0.0, "candidate_lo": 0.0, "verification": 0.0,
                "confidence": 0.0}

    put("modelgrade", True, "measured qualification store available")

    pref_id, cand_id = member_id(preferred), member_id(candidate)
    pref_card = mg.card(pref_id, cell, now=now)
    cand_card = mg.card(cand_id, cell, now=now)
    pref_lo = float(_dim(pref_card, "quality").get("lo", 0.0))
    cand_lo = float(_dim(cand_card, "quality").get("lo", 0.0))
    conf = float(getattr(cand_card, "confidence", 0.0) or 0.0)
    verify_score = verification_score(cand_card)

    # --- W2-I3.1 HARD GUARD, first, before any cost/warmth reasoning ------
    # On verification work the substituted route's measured verification score
    # must stay at or above the verifier floor. No flag relaxes this; the
    # `verifier_protected` doctrine check below it is the relaxable one.
    verifying = is_verification(specialist, ckey)
    if verifying:
        put("verifier_floor", verify_score >= verifier_floor(),
            f"verification score {verify_score:.3f} vs floor "
            f"{verifier_floor():.3f} for {cand_id} (W2-I3.1)")
        put("verifier_protected", not protect_verifier(),
            "verify is the head of the routing and is not substituted "
            "(OLYMPUS_ROUTESUB_PROTECT_VERIFIER)" if protect_verifier()
            else "verifier protection relaxed by operator; the W2-I3.1 floor "
                 "still applies")
    else:
        put("verifier_floor", True, "not verification work")
        put("verifier_protected", True, "not verification work")

    # --- qualification (one ladder, synthesis R1) -------------------------
    put("preferred_qualified", bool(mg.qualified(pref_id, cell, now=now)),
        f"{pref_id} status={mg.status(pref_id, cell, now=now)}")
    put("candidate_qualified", bool(mg.qualified(cand_id, cell, now=now)),
        f"{cand_id} status={mg.status(cand_id, cell, now=now)}")

    # --- the measured band ------------------------------------------------
    floor = quality_floor(cell)
    put("quality_floor", cand_lo >= floor,
        f"candidate quality lower bound {cand_lo:.3f} vs cell floor "
        f"{floor:.3f}")
    put("quality_band", (pref_lo - cand_lo) <= band(),
        f"candidate is {max(0.0, pref_lo - cand_lo):.3f} below the incumbent "
        f"({pref_lo:.3f}); band {band():.3f}")

    # --- security classification -----------------------------------------
    put("security_class", *_security(preferred, candidate, security_class))

    # --- capability requirements -----------------------------------------
    window = _context_window(candidate)
    need = max(0, int(context_tokens or 0))
    put("context_window", window >= need,
        f"candidate window {window} tokens vs required {need}")
    put("tools", _capability_ok(cand_card, "tools", needs_tools, floor),
        _capability_detail(cand_card, "tools", needs_tools, floor))
    put("structured",
        _capability_ok(cand_card, "structured", needs_structured, floor),
        _capability_detail(cand_card, "structured", needs_structured, floor))

    # --- evidence freshness ----------------------------------------------
    ttl = ttl_days()
    pref_fresh = getattr(pref_card, "freshness_days", None)
    cand_fresh = getattr(cand_card, "freshness_days", None)
    fresh_ok = (pref_fresh is not None and cand_fresh is not None
                and pref_fresh <= ttl and cand_fresh <= ttl)
    put("freshness", fresh_ok,
        f"evidence age preferred={pref_fresh}d candidate={cand_fresh}d vs TTL "
        f"{ttl:g}d")

    # --- confidence -------------------------------------------------------
    put("confidence", conf >= confidence_threshold(),
        f"candidate confidence {conf:.3f} vs threshold "
        f"{confidence_threshold():.3f}")

    return {"checks": checks, "ok": all(c["ok"] for c in checks.values()),
            "candidate": candidate, "preferred_lo": pref_lo,
            "candidate_lo": cand_lo, "verification": verify_score,
            "confidence": conf}


def _security(preferred, candidate, security_class) -> tuple[bool, str]:
    """Does the request's data classification permit this substitution?

    Two independent rules, both fail-closed: the class must be substitutable at
    all (`restricted` never is), and substitution must never cross a locality
    boundary — a local-only class, or a local incumbent, can never be replaced
    by a member that egresses further (04-routing R3 §6: substitution must not
    be able to resurrect a remote model)."""
    try:
        cls = (config.normalize_data_class(security_class)
               or config.default_data_class())
    except Exception:                                 # noqa: BLE001
        cls = str(security_class or "").strip().lower() or "public"
    if cls not in allowed_classes():
        return False, (f"data class '{cls}' is not substitutable "
                       f"(allowed: {', '.join(allowed_classes())})")
    cand_local = _is_local(candidate)
    try:
        local_only = bool(config.data_class_local_only(cls))
    except Exception:                                 # noqa: BLE001
        local_only = False
    if local_only and not cand_local:
        return False, (f"data class '{cls}' is local-only and "
                       f"{member_id(candidate)} is not a local member")
    if _is_local(preferred) and not cand_local:
        return False, ("substitution would move work from a local member to "
                       f"{member_id(candidate)}, which is not local")
    return True, f"data class '{cls}' permits substitution"


def _context_window(member) -> int:
    try:
        return int(config.context_window(getattr(member, "model", "")))
    except Exception:                                 # noqa: BLE001
        return 0


def _capability_ok(card, dim: str, required: bool, floor: float) -> bool:
    """A required capability must be MEASURED on the candidate, not assumed."""
    if not required:
        return True
    d = _dim(card, dim)
    return bool(d.get("n", 0)) and float(d.get("lo", 0.0)) >= floor


def _capability_detail(card, dim: str, required: bool, floor: float) -> str:
    if not required:
        return f"{dim} not required by this request"
    d = _dim(card, dim)
    return (f"measured {dim} support n={d.get('n', 0)} lo="
            f"{float(d.get('lo', 0.0)):.3f} vs floor {floor:.3f}")


def evaluate(*, members, specialist, preferred, cell, security_class: str = "",
             needs_tools: bool = False, needs_structured: bool = False,
             context_tokens: int = 0, now: float | None = None) -> Decision:
    """May a cheaper/warmer qualified member replace `preferred` for this cell?

    Returns a `Decision` — always, never raising. In `off` mode the answer is
    inert (no computation, no file, no evidence read). In `shadow` mode the full
    counterfactual is computed and RECORDED but `substituted` is always False,
    so routing is byte-identically unchanged. In `on` mode a fully-qualified
    substitution is returned in `Decision.selected`.

    Every precondition in `PRECONDITIONS` must hold; each blocks independently,
    and `Decision.checks` reports all of them whether they passed or not."""
    current = mode()
    if current == OFF:
        return _inert(specialist, preferred, OFF,
                      "OLYMPUS_ROUTESUB is off — no substitution considered")
    try:
        return _evaluate(current, members=members, specialist=specialist,
                         preferred=preferred, cell=cell,
                         security_class=security_class, needs_tools=needs_tools,
                         needs_structured=needs_structured,
                         context_tokens=context_tokens, now=now)
    except Exception as err:                          # noqa: BLE001 - never raise
        _capture("routesub.evaluate", err, context=str(specialist))
        return _inert(specialist, preferred, current,
                      f"evaluation failed, keeping the preferred route: {err}")


def _evaluate(current: str, *, members, specialist, preferred, cell,
              security_class, needs_tools, needs_structured, context_tokens,
              now) -> Decision:
    now = time.time() if now is None else now
    mg = _modelgrade()
    ckey = _cell_key(mg, cell)
    pref_id = member_id(preferred)

    cands = _candidates(members, preferred)
    if not cands:
        checks = {"candidate_available": {
            "ok": False,
            "detail": "no member in the pool is cheaper or warmer than "
                      f"{pref_id}"}}
        for name in PRECONDITIONS:
            checks.setdefault(name, {"ok": False,
                                     "detail": "not evaluated (no candidate)"})
        best = {"checks": checks, "ok": False, "candidate": None,
                "preferred_lo": 0.0, "candidate_lo": 0.0, "verification": 0.0,
                "confidence": 0.0}
    else:
        best = None
        first = None
        for cand in cands:
            assessed = _assess(mg, preferred=preferred, candidate=cand,
                               specialist=specialist, cell=cell, ckey=ckey,
                               security_class=security_class,
                               needs_tools=needs_tools,
                               needs_structured=needs_structured,
                               context_tokens=context_tokens, now=now)
            first = first if first is not None else assessed
            if assessed["ok"]:
                best = assessed
                break
        best = best or first

    candidate = best["candidate"]
    cand_id = member_id(candidate) if candidate is not None else ""
    qualifies = bool(best["ok"])
    blocked_by = next((n for n in PRECONDITIONS
                       if not best["checks"].get(n, {"ok": True})["ok"]), "")

    # --- W2-I3.1 re-assertion (belt and braces) --------------------------
    # The guard inside `_assess` already blocks this; re-checking here means no
    # future refactor of the candidate loop can produce a verifier substitution
    # below the floor without tripping over this line as well.
    if qualifies and is_verification(specialist, ckey) \
            and best["verification"] < verifier_floor():
        qualifies = False
        blocked_by = "verifier_floor"
        best["checks"]["verifier_floor"] = {
            "ok": False,
            "detail": (f"W2-I3.1: verification score {best['verification']:.3f} "
                       f"< floor {verifier_floor():.3f} — a verifier is never "
                       "substituted below its measured floor")}

    substituted = qualifies and current == ON
    counterfactual = qualifies and current == SHADOW

    est_tokens = int(context_tokens) if context_tokens else est_tokens_default()
    pref_cost = price(preferred) * est_tokens / 1_000_000.0
    cand_cost = (price(candidate) * est_tokens / 1_000_000.0
                 if candidate is not None else pref_cost)
    savings = max(0.0, pref_cost - cand_cost) if qualifies else 0.0

    # --- W2-I3.2: no silent degradation ----------------------------------
    degrades = bool(qualifies and best["preferred_lo"] - best["candidate_lo"]
                    > 1e-9)
    disclosure = ""
    if degrades:
        disclosure = (
            f"Answered by {cand_id} instead of {pref_id} to reduce cost. "
            f"Measured quality lower bound {best['candidate_lo']:.3f} vs "
            f"{best['preferred_lo']:.3f} for this kind of task (within the "
            f"{band():.3f} measured-equivalence band).")

    if qualifies:
        reason = (f"{cand_id} is within the measured band of {pref_id} for "
                  f"{ckey} and is cheaper/warmer"
                  + ("" if current == ON else
                     " (shadow: recorded, routing unchanged)"))
    elif blocked_by:
        reason = (f"no substitution: {blocked_by} — "
                  f"{best['checks'].get(blocked_by, {}).get('detail', '')}")
    else:                                             # pragma: no cover - guard
        reason = "no substitution"

    dec = Decision(
        decision_id=uuid.uuid4().hex,
        ts=now,
        mode=current,
        specialist=str(specialist or ""),
        cell=ckey,
        security_class=str(security_class or ""),
        preferred_route=pref_id,
        candidate_route=cand_id,
        substituted=substituted,
        counterfactual=counterfactual,
        reason=reason,
        blocked_by=blocked_by,
        checks=best["checks"],
        confidence=round(float(best["confidence"]), 6),
        preferred_quality_lo=round(float(best["preferred_lo"]), 6),
        candidate_quality_lo=round(float(best["candidate_lo"]), 6),
        verification_score=round(float(best["verification"]), 6),
        estimated_preferred_cost_usd=round(pref_cost, 8),
        estimated_candidate_cost_usd=round(cand_cost, 8),
        estimated_savings_usd=round(savings, 8),
        user_visible_degradation=degrades,
        disclosure=disclosure,
        selected=(candidate if substituted else preferred),
    )
    recorded = _append(dec.to_row())
    return Decision(**{**dec.__dict__, "recorded": recorded})


def choose(members, specialist: str, heuristic_pick, *, cell=None, **kwargs):
    """`learned_routing.choose` / `bandit_routing.choose` seam adapter.

    Same contract: returns a pool member to use, or None to defer to the caller's
    pick; never raises. NOTHING IN THE TREE CALLS THIS YET — wiring substitution
    into `ModelPool.for_specialist` (precedence
    `pin > (bandit | learned) > substitution > heuristic`) is a later PR, and
    this module ships inert."""
    try:
        if not enabled():
            return None
        if cell is None:
            cell = cell_for(specialist,
                            context_tokens=kwargs.get("context_tokens", 0),
                            needs_tools=kwargs.get("needs_tools", False),
                            needs_structured=kwargs.get("needs_structured",
                                                        False))
        dec = evaluate(members=members, specialist=specialist,
                       preferred=heuristic_pick, cell=cell, **kwargs)
        return dec.selected if dec.substituted else None
    except Exception:                                 # noqa: BLE001
        return None                     # telemetry must never break routing


# --- outcomes -------------------------------------------------------------

def _is_negative(verdict) -> bool:
    return str(verdict or "").strip().lower() in NEGATIVE_VERDICTS


def record_outcome(decision_id: str, *, actual_cost, latency_ms,
                   verifier_outcome, disagreement: bool = False,
                   fallback: bool = False) -> bool:
    """Complete a recorded decision with what actually happened.

    Appends the outcome half of the row (`decisions()` merges the two by id):
    the ACTUAL cost, the latency, the verifier's verdict, whether that verdict
    disagreed with the preferred route's expectation, and whether a fallback
    had to be activated. `disagreement` is the caller's explicit signal OR'd
    with a negative verifier verdict, so a rejected substituted answer can never
    be recorded as agreement. Never raises; returns whether it was persisted."""
    try:
        if mode() == OFF:
            return False
        did = str(decision_id or "").strip()
        if not did:
            return False
        cost = _to_float(actual_cost)
        lat = _to_float(latency_ms)
        return _append({
            "kind": "outcome",
            "decision_id": did,
            "outcome_ts": time.time(),
            "actual_cost_usd": None if cost is None else round(cost, 8),
            "latency_ms": None if lat is None else int(lat),
            "verifier_outcome": str(verifier_outcome or "")[:60],
            "disagreement": bool(disagreement) or _is_negative(verifier_outcome),
            "fallback": bool(fallback),
            "outcome_recorded": True,
        })
    except Exception as err:                          # noqa: BLE001 - never raise
        _capture("routesub.record_outcome", err, context=str(decision_id))
        return False


# --- agreement telemetry (the CACHE_ROUTE analog) -------------------------

def _rate(num: int, den: int) -> float:
    return round(num / den, 6) if den else 0.0


def _blank_cell_stats() -> dict:
    return {"decisions": 0, "substitutions": 0, "counterfactuals": 0,
            "completed": 0, "disagreements": 0, "fallbacks": 0,
            "degradations": 0, "estimated_savings_usd": 0.0,
            "actual_savings_usd": 0.0}


def agreement_stats(days: float = 7, *, now: float | None = None) -> dict:
    """Agreement telemetry over the last `days` — how lossy is the routing?

    Colibri prints swap% / top-K overlap / KL against the model's own routing
    distribution; that is outcome-blind. This computes the same shape against
    OUTCOMES: how often work was substituted, how often the verifier then
    DISAGREED with what the preferred route was expected to produce, and what
    the substitutions actually saved versus what they were estimated to save —
    per cell, so a cell where cheap substitution is quietly failing is visible
    rather than averaged away.

    Savings definition: `estimated` is `price(preferred) - price(candidate)`
    over the estimated billed tokens at decision time; `actual` is the same
    preferred-route estimate minus the cost the run ACTUALLY incurred, so the
    delta between them prices the estimate itself."""
    rows = decisions(days=days, now=now)
    subs = [r for r in rows if r.get("substituted")]
    shadow = [r for r in rows if r.get("counterfactual")]
    completed = [r for r in subs if r.get("outcome_recorded")]
    disagreements = [r for r in completed if r.get("disagreement")]

    def num(row, key) -> float:
        val = _to_float(row.get(key))
        return 0.0 if val is None else val

    estimated = sum(num(r, "estimated_savings_usd") for r in subs)
    estimated_completed = sum(num(r, "estimated_savings_usd") for r in completed)
    actual = sum(num(r, "estimated_preferred_cost_usd") - num(r, "actual_cost_usd")
                 for r in completed)
    shadow_savings = sum(num(r, "estimated_savings_usd") for r in shadow)

    by_cell: dict[str, dict] = {}
    for row in rows:
        cell = str(row.get("cell", ""))
        stats = by_cell.setdefault(cell, _blank_cell_stats())
        stats["decisions"] += 1
        if row.get("substituted"):
            stats["substitutions"] += 1
            stats["estimated_savings_usd"] += num(row, "estimated_savings_usd")
            if row.get("outcome_recorded"):
                stats["completed"] += 1
                stats["actual_savings_usd"] += (
                    num(row, "estimated_preferred_cost_usd")
                    - num(row, "actual_cost_usd"))
                if row.get("disagreement"):
                    stats["disagreements"] += 1
        if row.get("counterfactual"):
            stats["counterfactuals"] += 1
        if row.get("fallback"):
            stats["fallbacks"] += 1
        if row.get("user_visible_degradation"):
            stats["degradations"] += 1
    for stats in by_cell.values():
        stats["substitution_rate"] = _rate(stats["substitutions"],
                                           stats["decisions"])
        stats["disagreement_rate"] = _rate(stats["disagreements"],
                                           stats["completed"])
        stats["estimated_savings_usd"] = round(stats["estimated_savings_usd"], 8)
        stats["actual_savings_usd"] = round(stats["actual_savings_usd"], 8)

    verdicts: dict[str, int] = {}
    for row in completed:
        verdicts[str(row.get("verifier_outcome", ""))] = verdicts.get(
            str(row.get("verifier_outcome", "")), 0) + 1

    return {
        "mode": mode(),
        "days": days,
        "decisions": len(rows),
        "substitutions": len(subs),
        "substitution_rate": _rate(len(subs), len(rows)),
        "counterfactuals": len(shadow),
        "counterfactual_rate": _rate(len(shadow), len(rows)),
        "counterfactual_savings_usd": round(shadow_savings, 8),
        "completed": len(completed),
        "disagreements": len(disagreements),
        "disagreement_rate": _rate(len(disagreements), len(completed)),
        "fallbacks": sum(1 for r in rows if r.get("fallback")),
        "degradations": sum(1 for r in rows
                            if r.get("user_visible_degradation")),
        "savings": {
            "estimated_usd": round(estimated, 8),
            "estimated_on_completed_usd": round(estimated_completed, 8),
            "actual_usd": round(actual, 8),
            "delta_usd": round(actual - estimated_completed, 8),
        },
        "verifier_outcomes": verdicts,
        "by_cell": by_cell,
        "blocked_by": _blocked_histogram(rows),
    }


def _blocked_histogram(rows: list[dict]) -> dict:
    """Which precondition refused the most substitutions — the operator's first
    question when substitution never engages."""
    out: dict[str, int] = {}
    for row in rows:
        name = str(row.get("blocked_by", ""))
        if name:
            out[name] = out.get(name, 0) + 1
    return out


def status() -> dict:
    """Operator/auditor view, in the shape `learned_routing.status()` uses."""
    return {
        "mode": mode(),
        "enabled": enabled(),
        "active": mode() == ON,
        "thresholds": {
            "band": band(),
            "quality_floor": quality_floor(None),
            "verifier_floor": verifier_floor(),
            "protect_verifier": protect_verifier(),
            "confidence": confidence_threshold(),
            "ttl_days": ttl_days(),
            "classes": list(allowed_classes()),
        },
        "modelgrade": _modelgrade() is not None,
        "warm_members": sorted(_WARMTH),
        "agreement": agreement_stats() if enabled() else {},
    }
