"""Unified measured model qualification (Wave-2 Capability C1).

Routing evidence is scattered today — a hand-typed `config._CAPABILITIES` table,
`routing_outcomes` rows, `evals` baselines, `modelgate` drift results,
`calibration` observations — and no store can answer the only question routing
actually needs: **is this member qualified for THIS KIND of task?** A model can
be selected today on a human-written claim alone.

This module is that store: one append-only evidence ledger keyed by
**member x task cell**, plus a derived, rebuildable card per cell.

Invariants (WAVE2 spec W2-C1):
  W2-I1.1  No manual claim promotes. A `config._CAPABILITIES` entry may seed a
           PRIOR — which caps the state at `OBSERVING` — and can NEVER yield
           `QUALIFIED`. Promotion requires MEASURED rows: n >= MIN_N, a
           recency-weighted Wilson lower bound at or above the cell's floor,
           freshness within TTL, and no `modelgate` freeze/quarantine.
  W2-I1.2  Every evidence row (and therefore every card) carries the FULL key:
           provider, model, revision, commit, tmpl_ver, policy_ver,
           eval_set_ver, cell, date.
  W2-I1.3  Stale evidence LOSES CONFIDENCE rather than silently persisting:
           past TTL the recency weighting shrinks the effective sample (the
           interval widens) and the card falls back to `OBSERVING`; past 2xTTL
           of silence it is `RETIRED`.
  W2-I1.4  Read-only w.r.t. other stores. It READS `modelgate` (freeze markers,
           `tmpl_ver`/`eval_set_ver` fingerprints), `calibration.wilson_interval`
           and `config._CAPABILITIES`; it writes only under
           `MEMORY_DIR/modelgrade/`.

Contradictory evidence is PRESERVED, never overwritten. `evidence.jsonl` is
append-only; when rows under one full key disagree beyond `CONFLICT_DELTA` a
`conflict` record is emitted into the derived `cards.json` naming the resolution
rule actually applied (`recency_weighted_wilson`) — explicit, auditable, and
reversible (delete the derived file, rebuild, nothing is lost).

Threats. Evidence poisoning: every observation is bounds-validated (finite,
in-range, known dimension) and discarded-and-counted otherwise, the ledger is
append-only and fully keyed, and scores are expected to come from verifier/eval
outcomes — never from a model's self-report (`observe` has no path for a member
to grade itself; `source` is recorded on every row).

Failure. No evidence => `UNTESTED` (never a default `QUALIFIED`). Corrupt
`cards.json` => rebuilt from evidence. Corrupt `evidence.jsonl` =>
quarantined aside as `evidence.corrupt.<ts>.jsonl` (reject-never-repair) and the
store falls back to `UNTESTED`. Nothing here raises into a caller.

Rollback. `OLYMPUS_MODELGRADE` is OFF by default: `observe()` no-ops, every
status is `UNTESTED`, nothing is written — Wave-1 behaviour exactly.

Scope note: this PR builds the STORE. Wiring it into routing substitution is
W2-PR8 (`routesub`); `doctor`/`cli` surfaces are later PRs.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import calibration, config, modelgate, proclock

# --- states ---------------------------------------------------------------
UNTESTED = "untested"          # no measured evidence (the only safe default)
OBSERVING = "observing"        # evidence accumulating, or prior-seeded, or stale
QUALIFIED = "qualified"        # measured promotion rule satisfied
RESTRICTED = "restricted"      # promotion dimension ok, another dimension failed
FROZEN = "frozen"              # modelgate freeze/block marker present
QUARANTINED = "quarantined"    # modelgate quarantine marker present
RETIRED = "retired"            # silent for more than 2x TTL

STATES = (UNTESTED, OBSERVING, QUALIFIED, RESTRICTED, FROZEN, QUARANTINED,
          RETIRED)

# --- graded dimensions ----------------------------------------------------
DIMENSIONS = ("quality", "cost", "latency", "reliability", "structured",
              "tools", "security")

# The dimension the promotion rule reads. The others gate DOWN (a measured
# deficit on any of them restricts a member) but never gate up.
PROMOTION_DIMENSION = "quality"

# --- defaults (all env-overridable, all documented in the accessors) -------
DEFAULT_MIN_N = 20
DEFAULT_FLOOR = 0.7
DEFAULT_TTL_DAYS = 30

# Conflict detection: two disjoint observation sets under ONE full key whose
# rates differ by more than this, each with at least CONFLICT_MIN_N rows.
CONFLICT_DELTA = 0.4
CONFLICT_MIN_N = 3
CONFLICT_RESOLUTION = "recency_weighted_wilson"

# Bounds validation (poisoning defence — anything outside is discarded).
MAX_COST_USD = 1000.0
MAX_LATENCY_MS = 86_400_000        # 24h
MAX_ROW_N = 10_000                 # one row may aggregate at most this many trials

_STATE_DIR = "modelgrade"
_EVIDENCE = "evidence.jsonl"
_CARDS = "cards.json"
_LOCK = "modelgrade"

# Process-local counter of discarded (bounds-rejected) observations. Deliberately
# NOT persisted into the evidence ledger — a poisoning attempt must not be able
# to write anything at all.
_STATS: dict[str, int] = {"discarded": 0}


# --- flag + thresholds (config.py idioms: zero-arg, live-read, default off) --

def enabled() -> bool:
    """Whether the qualification store is active at all (default OFF).

    Off => `observe()` no-ops, every status is `UNTESTED`, nothing is written."""
    return os.environ.get("OLYMPUS_MODELGRADE", "").strip().lower() in (
        "1", "on", "true", "yes")


def min_n() -> int:
    """Minimum MEASURED observations before promotion is even considered."""
    return max(1, _int_env("OLYMPUS_GRADE_MIN_N", DEFAULT_MIN_N))


def ttl_days() -> float:
    """Freshness TTL: past this the card decays to `OBSERVING`; 2x => `RETIRED`."""
    return max(0.5, _float_env("OLYMPUS_GRADE_TTL_DAYS", DEFAULT_TTL_DAYS))


def floor_for(cell) -> float:
    """The Wilson-lower-bound floor a cell must clear to qualify.

    `OLYMPUS_GRADE_FLOOR_<TASK_CLASS>` (upper-cased, non-alphanumerics as `_`)
    overrides the global `OLYMPUS_GRADE_FLOOR` for one task class."""
    tc = cell_of(cell).task_class if cell is not None else "any"
    safe = "".join(ch if ch.isalnum() else "_" for ch in tc).upper()
    per_cell = os.environ.get(f"OLYMPUS_GRADE_FLOOR_{safe}", "").strip()
    if per_cell:
        val = _to_float(per_cell)
        if val is not None and 0.0 <= val <= 1.0:
            return val
    val = _float_env("OLYMPUS_GRADE_FLOOR", DEFAULT_FLOOR)
    return val if 0.0 <= val <= 1.0 else DEFAULT_FLOOR


def halflife_days() -> float:
    """Recency half-life for the weighted Wilson interval (defaults to the TTL).

    A row exactly one TTL old counts half as much as a fresh one — this is the
    mechanism behind W2-I1.3 (confidence decays, evidence is not deleted)."""
    return max(0.5, _float_env("OLYMPUS_GRADE_HALFLIFE_DAYS", ttl_days()))


def _to_float(raw: str):
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


# --- the task cell --------------------------------------------------------

_CONTEXT_BANDS = ((4_000, "short"), (32_000, "medium"), (128_000, "long"))


def context_band(tokens) -> str:
    """Bucket an approximate input size into a coarse, stable band name."""
    val = _to_float(str(tokens))
    if val is None:
        return _token(tokens, "any")
    for limit, name in _CONTEXT_BANDS:
        if val < limit:
            return name
    return "huge"


def _token(value, default: str = "any") -> str:
    """Normalise one cell component: lower-cased, `|` free, bounded, non-empty."""
    text = str(value if value is not None else "").strip().lower()
    text = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in text)
    text = text.strip("-")
    return text[:32] or default


@dataclass(frozen=True)
class TaskCell:
    """The unit of qualification: a member is qualified FOR A CELL, never
    globally. Frozen + normalised so the key string is deterministic."""

    task_class: str = "any"
    language: str = "any"
    context_band: str = "any"
    needs_tools: bool = False
    needs_structured: bool = False

    @property
    def key(self) -> str:
        """Short deterministic key, e.g. `coding|en|short|tools|struct`."""
        return "|".join((
            self.task_class, self.language, self.context_band,
            "tools" if self.needs_tools else "notools",
            "struct" if self.needs_structured else "nostruct",
        ))

    def __str__(self) -> str:                       # pragma: no cover - trivial
        return self.key


def cell_of(task_class="any", language="any", context="any", *,
            needs_tools: bool = False, needs_structured: bool = False) -> TaskCell:
    """Build a normalised `TaskCell`.

    Accepts a `TaskCell` or an existing cell KEY as the first argument and
    returns it unchanged/parsed, so every public entry point can take either
    form (`cell_of(CELL_ANY) is CELL_ANY`). `context` may be a band name or an
    approximate token count (bucketed by `context_band`)."""
    if isinstance(task_class, TaskCell):
        return task_class
    if isinstance(task_class, str) and task_class.count("|") == 4:
        return cell_from_key(task_class)
    return TaskCell(
        task_class=_token(task_class),
        language=_token(language),
        context_band=_token(context_band(context)),
        needs_tools=bool(needs_tools),
        needs_structured=bool(needs_structured),
    )


def cell_from_key(key: str) -> TaskCell:
    """Parse a cell key back into a `TaskCell` (round-trips `TaskCell.key`)."""
    parts = str(key).split("|")
    while len(parts) < 5:                            # tolerate a truncated key
        parts.append("any")
    return TaskCell(
        task_class=_token(parts[0]), language=_token(parts[1]),
        context_band=_token(parts[2]),
        needs_tools=parts[3] == "tools", needs_structured=parts[4] == "struct",
    )


def cell_key(cell) -> str:
    """The deterministic key string for any accepted cell form."""
    return cell_of(cell).key


#: Coarse evidence that is not tied to a specific cell (`any|any|any|...`).
CELL_ANY = TaskCell()


def member_of(provider: str, model: str) -> str:
    """The member identity — the SAME `provider/model` string `modelgate`'s
    freeze markers are keyed by, so a freeze is reflected without translation."""
    return f"{provider or ''}/{model or ''}"


# --- the full evidence key (W2-I1.2) --------------------------------------

_KEY_FIELDS = ("provider", "model", "revision", "commit", "tmpl_ver",
               "policy_ver", "eval_set_ver", "cell")

_FINGERPRINTS: dict[str, str] = {}


def _fingerprint(name: str, fn) -> str:
    """Memoised per-process prompt/corpus fingerprint (reused from modelgate).

    These hash files on disk; observations can be frequent, so each is computed
    once per process. A failure degrades to `""` rather than losing the row."""
    if name not in _FINGERPRINTS:
        try:
            _FINGERPRINTS[name] = str(fn())
        except Exception:                            # noqa: BLE001 - never raise
            _FINGERPRINTS[name] = ""
    return _FINGERPRINTS[name]


def policy_ver() -> str:
    """Routing/qualification policy version (`OLYMPUS_POLICY_VERSION`, default 1)."""
    return os.environ.get("OLYMPUS_POLICY_VERSION", "").strip() or "1"


def commit() -> str:
    """The Olympus build the observation was taken on (same env as modelgate)."""
    return (os.environ.get("OLYMPUS_COMMIT")
            or os.environ.get("COMMIT") or "")


def full_key(row: dict) -> str:
    """The comparison key of one evidence row — everything a score depends on."""
    return "|".join(str(row.get(f, "")) for f in _KEY_FIELDS)


# --- storage --------------------------------------------------------------

def _dir() -> Path:
    d = config.MEMORY_DIR / _STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def evidence_path() -> Path:
    return _dir() / _EVIDENCE


def cards_path() -> Path:
    return _dir() / _CARDS


def _capture(where: str, exc: BaseException, context: str = "") -> None:
    from . import errors                              # lazy: keep imports thin
    try:
        errors.capture(where, exc, context=context)
    except Exception:                                 # noqa: BLE001
        pass


def _quarantine_evidence(reason: str) -> Path | None:
    """Reject-never-repair: move a corrupt ledger aside, keeping every byte.

    The store then reads as EMPTY (=> `UNTESTED`), which is the safe direction:
    a damaged ledger must never be silently half-parsed into a promotion."""
    path = evidence_path()
    dest = path.with_name(f"evidence.corrupt.{time.time():.0f}.jsonl")
    try:
        if path.exists():
            os.replace(path, dest)
    except OSError as err:                            # pragma: no cover - fs edge
        _capture("modelgrade.quarantine", err, context=str(path))
        return None
    _capture("modelgrade.evidence_corrupt", ValueError(reason), context=str(dest))
    return dest


def _load_evidence() -> list[dict] | None:
    """Every evidence row, oldest-first — or None when the ledger is UNUSABLE.

    A single unparseable/malformed line condemns the WHOLE file: partial
    parsing would be repair-by-omission, and a half-read ledger must never be
    able to promote anything. The file is moved aside intact (nothing is lost,
    an operator can inspect it) and None propagates as `UNTESTED` — the safe
    direction — rather than as an empty-but-healthy store."""
    path = evidence_path()
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        _capture("modelgrade.read_evidence", err, context=str(path))
        return None
    rows: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            with proclock.lock(_LOCK):
                _quarantine_evidence(f"unparseable JSON at line {lineno}")
            return None
        if not isinstance(row, dict) or "ts" not in row or "member" not in row:
            with proclock.lock(_LOCK):
                _quarantine_evidence(f"malformed row at line {lineno}")
            return None
        rows.append(row)
    rows.sort(key=lambda r: _num(r.get("ts"), 0.0))
    return rows


def _num(value, default: float = 0.0) -> float:
    val = _to_float(str(value))
    return default if val is None else val


# --- observation (append-only, bounds-validated) --------------------------

def observe(*, provider: str, model: str, revision: str = "",
            cell=CELL_ANY, dimension: str = PROMOTION_DIMENSION,
            success, cost_usd: float = 0.0, latency_ms: int = 0,
            source: str = "", **extras) -> bool:
    """Record ONE measured observation. Returns True if it was persisted.

    `success` is a bool or a 0..1 float (partial credit); it must come from a
    VERIFIER/EVAL outcome, never from the graded model's own report. `extras`
    are folded into the row under `extra` (plus `n`, the number of trials this
    row aggregates, default 1) so callers can attach provenance without
    schema churn.

    Bounds-validated (poisoning defence): a non-finite score, an out-of-range
    rate, a negative/absurd `n`, an absurd cost/latency, an unknown dimension or
    a missing member is DISCARDED AND COUNTED (`discarded()`), never raised and
    never written. Inert when `OLYMPUS_MODELGRADE` is off."""
    if not enabled():
        return False
    try:
        row = _validate(provider=provider, model=model, revision=revision,
                        cell=cell, dimension=dimension, success=success,
                        cost_usd=cost_usd, latency_ms=latency_ms,
                        source=source, extras=extras)
    except Exception as err:                          # noqa: BLE001 - never raise
        _STATS["discarded"] += 1
        _capture("modelgrade.observe", err, context=f"{provider}/{model}")
        return False
    if row is None:
        _STATS["discarded"] += 1
        return False
    try:
        line = json.dumps(row, sort_keys=True)
        with proclock.lock(_LOCK):
            with evidence_path().open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except (OSError, TimeoutError, ValueError) as err:
        _capture("modelgrade.append", err, context=row.get("member", ""))
        return False
    return True


def _validate(*, provider, model, revision, cell, dimension, success,
              cost_usd, latency_ms, source, extras) -> dict | None:
    """Bounds-check one observation and build its fully-keyed row, or None."""
    provider = str(provider or "").strip()
    model = str(model or "").strip()
    if not provider or not model:
        return None
    if dimension not in DIMENSIONS:
        return None

    if isinstance(success, bool):
        rate = 1.0 if success else 0.0
    else:
        rate = _to_float(str(success))
        if rate is None or not (0.0 <= rate <= 1.0):
            return None

    cost = _to_float(str(cost_usd))
    if cost is None or cost < 0.0 or cost > MAX_COST_USD:
        return None
    lat = _to_float(str(latency_ms))
    if lat is None or lat < 0.0 or lat > MAX_LATENCY_MS:
        return None

    n = extras.pop("n", 1)
    n_val = _to_float(str(n))
    if n_val is None or n_val < 1 or n_val > MAX_ROW_N or n_val != int(n_val):
        return None

    ts = extras.pop("ts", None)
    ts_val = _num(ts, time.time()) if ts is not None else time.time()
    if not math.isfinite(ts_val) or ts_val <= 0:
        return None

    cell_obj = cell_of(cell)
    return {
        # --- the full key (W2-I1.2) ---
        "provider": provider,
        "model": model,
        "revision": str(revision or ""),
        "commit": commit(),
        "tmpl_ver": _fingerprint("tmpl_ver", modelgate.tmpl_ver),
        "policy_ver": policy_ver(),
        "eval_set_ver": _fingerprint("eval_set_ver", modelgate.eval_set_ver),
        "cell": cell_obj.key,
        "date": time.strftime("%Y-%m-%d", time.gmtime(ts_val)),
        # --- the observation ---
        "member": member_of(provider, model),
        "dimension": dimension,
        "success": round(rate, 6),
        "n": int(n_val),
        "cost_usd": round(cost, 6),
        "latency_ms": int(lat),
        "source": str(source or "")[:120],
        "ts": ts_val,
        "extra": _safe_extras(extras),
    }


def _safe_extras(extras: dict) -> dict:
    """JSON-safe, bounded provenance blob (never raw user content)."""
    out: dict = {}
    for key, val in list(extras.items())[:16]:
        if isinstance(val, bool) or isinstance(val, int) or isinstance(val, float):
            if isinstance(val, float) and not math.isfinite(val):
                continue
            out[str(key)[:40]] = val
        else:
            out[str(key)[:40]] = str(val)[:200]
    return out


def discarded() -> int:
    """How many observations this process rejected on bounds validation."""
    return _STATS["discarded"]


# --- derivation -----------------------------------------------------------

@dataclass(frozen=True)
class GradeCard:
    """The derived, per-(member, cell) answer. Rebuildable from evidence."""

    member: str
    cell: str
    status: str
    confidence: float
    reason: str
    dimensions: dict = field(default_factory=dict)
    key: dict = field(default_factory=dict)
    prior: dict = field(default_factory=dict)
    conflicts: list = field(default_factory=list)
    evidence_rows: int = 0
    freshness_days: float | None = None
    generated: float = 0.0

    def to_dict(self) -> dict:
        return {
            "member": self.member, "cell": self.cell, "status": self.status,
            "confidence": self.confidence, "reason": self.reason,
            "dimensions": self.dimensions, "key": self.key, "prior": self.prior,
            "conflicts": self.conflicts, "evidence_rows": self.evidence_rows,
            "freshness_days": self.freshness_days,
        }


def _weighted(rows: list[dict], now: float, halflife: float) -> dict:
    """Recency-weighted Wilson summary of one (member, cell, dimension) group.

    Weight is `0.5 ** (age_days / halflife)`: old rows shrink the EFFECTIVE
    sample, which widens the Wilson interval and lowers the bound — evidence is
    never deleted or overwritten, it just stops carrying the same confidence
    (W2-I1.3)."""
    raw = 0
    k = 0.0
    eff = 0.0
    newest = None
    for row in rows:
        ts = _num(row.get("ts"), 0.0)
        cnt = int(_num(row.get("n"), 1)) or 1
        age_days = max(0.0, (now - ts) / 86400.0)
        weight = 0.5 ** (age_days / halflife) if halflife > 0 else 1.0
        raw += cnt
        k += weight * cnt * _num(row.get("success"), 0.0)
        eff += weight * cnt
        newest = ts if newest is None else max(newest, ts)
    lo, hi = calibration.wilson_interval(k, eff) if eff > 0 else (0.0, 1.0)
    rate = (k / eff) if eff > 0 else 0.0
    fresh = None if newest is None else round(max(0.0, (now - newest) / 86400.0), 4)
    return {"n": raw, "effective_n": round(eff, 4), "rate": round(rate, 6),
            "lo": round(lo, 6), "hi": round(hi, 6), "freshness_days": fresh}


_PRIOR_ROLE = {
    "coding": "coding", "code": "coding",
    "reasoning": "reasoning", "planning": "reasoning", "research": "reasoning",
    "verify": "verify", "verification": "verify",
}


def prior_for(member: str, cell) -> dict:
    """The hand-written `config._CAPABILITIES` claim for this member, if any.

    This is a PRIOR ONLY (W2-I1.1): it can move an unmeasured member from
    `UNTESTED` to `OBSERVING` — signalling "worth measuring" — and can never
    contribute to promotion. It is reported separately from `dimensions` so an
    operator can always see that it is a claim, not a measurement."""
    model = (member or "").split("/", 1)[-1].lower()
    if not model:
        return {}
    for name, caps in config._CAPABILITIES.items():
        if name in model:
            role = _PRIOR_ROLE.get(cell_of(cell).task_class, "general")
            return {"source": "config._CAPABILITIES", "entry": name,
                    "role": role,
                    "claim": float(caps.get(role, caps.get("general", 5))),
                    "measured": False}
    return {}


def _freeze_state(member: str) -> tuple[str, str]:
    """Reflect a `modelgate` marker as (state, reason) — read-only (W2-I1.4)."""
    try:
        marker = modelgate.frozen_members().get(member)
    except Exception as err:                          # noqa: BLE001
        _capture("modelgrade.frozen_members", err, context=member)
        return "", ""
    if not isinstance(marker, dict):
        return "", ""
    severity = str(marker.get("severity", ""))
    state = QUARANTINED if severity == modelgate.QUARANTINE else FROZEN
    return state, (f"modelgate {severity or 'freeze'}: "
                   f"{marker.get('reason', 'marker present')}")


def _derive(member: str, cell, rows: list[dict], now: float) -> GradeCard:
    """The whole decision, in one auditable place. PURE given `rows` and `now`."""
    ckey = cell_key(cell)
    thresholds_n, floor, ttl = min_n(), floor_for(cell), ttl_days()
    halflife = halflife_days()

    by_dim: dict[str, list[dict]] = {}
    for row in rows:
        by_dim.setdefault(str(row.get("dimension", "")), []).append(row)
    dims = {d: _weighted(by_dim.get(d, []), now, halflife) for d in DIMENSIONS}

    newest = max((_num(r.get("ts"), 0.0) for r in rows), default=None)
    freshness = None if newest is None else round(max(0.0, (now - newest) / 86400.0), 4)
    key_block = _key_block(rows)
    prior = prior_for(member, cell)
    promo = dims[PROMOTION_DIMENSION]

    def card(status: str, reason: str, confidence: float) -> GradeCard:
        return GradeCard(
            member=member, cell=ckey, status=status,
            confidence=round(max(0.0, min(1.0, confidence)), 6), reason=reason,
            dimensions=dims, key=key_block, prior=prior,
            conflicts=_conflicts(member, ckey, rows), evidence_rows=len(rows),
            freshness_days=freshness, generated=now)

    # 1. A modelgate marker outranks everything measured here.
    frozen_state, frozen_reason = _freeze_state(member)
    if frozen_state:
        return card(frozen_state, frozen_reason, 0.0)

    # 2. No measured rows: a manual claim seeds OBSERVING at most (W2-I1.1).
    if not rows:
        if prior:
            return card(OBSERVING,
                        f"prior only ({prior['source']} claims "
                        f"{prior['claim']}/10 for {prior['role']}) — a manual "
                        "claim never promotes; no measured evidence yet", 0.0)
        return card(UNTESTED, "no evidence for this member/cell", 0.0)

    # 3. Staleness (W2-I1.3).
    if freshness is not None and freshness > 2 * ttl:
        return card(RETIRED,
                    f"no evidence for {freshness:.1f}d (> 2x TTL {ttl:.0f}d)", 0.0)
    conf = _confidence(promo["lo"], freshness, ttl)
    if freshness is not None and freshness > ttl:
        return card(OBSERVING,
                    f"evidence is {freshness:.1f}d old (> TTL {ttl:.0f}d) — "
                    "confidence decayed", conf)

    # 4. The measured promotion rule.
    if promo["n"] < thresholds_n:
        return card(OBSERVING,
                    f"insufficient evidence: n={promo['n']} < MIN_N="
                    f"{thresholds_n}", conf)
    if promo["lo"] < floor:
        return card(OBSERVING,
                    f"wilson lower bound {promo['lo']:.3f} < floor {floor:.3f} "
                    f"(n={promo['n']}, rate={promo['rate']:.3f})", conf)

    # 5. A measured deficit on any OTHER dimension restricts rather than blocks.
    for dim in DIMENSIONS:
        if dim == PROMOTION_DIMENSION:
            continue
        d = dims[dim]
        if d["n"] >= thresholds_n and d["lo"] < floor:
            return card(RESTRICTED,
                        f"quality qualified but {dim} lower bound "
                        f"{d['lo']:.3f} < floor {floor:.3f} (n={d['n']})", conf)

    return card(QUALIFIED,
                f"measured: n={promo['n']}, wilson lo={promo['lo']:.3f} >= floor "
                f"{floor:.3f}, {freshness:.1f}d old (TTL {ttl:.0f}d)", conf)


def _confidence(lo: float, freshness, ttl: float) -> float:
    """Overall confidence: the promotion bound, linearly decayed past the TTL
    so it reaches 0 at 2x TTL (stale evidence loses confidence, W2-I1.3)."""
    conf = float(lo)
    if freshness is not None and ttl > 0 and freshness > ttl:
        conf *= max(0.0, 1.0 - (freshness - ttl) / ttl)
    return conf


def _key_block(rows: list[dict]) -> dict:
    """The full key carried by this card (W2-I1.2), plus every key variant seen.

    Evidence under DIFFERENT keys is not silently merged into one claim: the
    newest row's key is authoritative for the card, and `variants` lists every
    distinct key the card was derived from, so a mixed-key card is visible."""
    if not rows:
        return {}
    newest = rows[-1]
    block = {f: newest.get(f, "") for f in _KEY_FIELDS}
    block["date"] = newest.get("date", "")
    variants = sorted({full_key(r) for r in rows})
    block["variants"] = variants
    block["mixed_key"] = len(variants) > 1
    return block


# --- conflicts (preserved, never overwritten; resolution named) -----------

def _conflicts(member: str, ckey: str, rows: list[dict]) -> list[dict]:
    """Disagreements between observation sets under ONE full key.

    Nothing is dropped or rewritten: both sets stay in `evidence.jsonl` and the
    card's numbers come from the recency-weighted Wilson interval over ALL rows.
    The conflict record NAMES that resolution rule so the operator can see which
    tie-break produced the state (auditable, and reversible by deleting the
    derived file)."""
    out: list[dict] = []
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((full_key(row), str(row.get("dimension", ""))),
                          []).append(row)
    for (key, dim), grp in sorted(groups.items()):
        out.extend(_source_conflicts(member, ckey, key, dim, grp))
        out.extend(_temporal_conflicts(member, ckey, key, dim, grp))
    return out


def _summary(label: str, rows: list[dict]) -> dict:
    n = sum(int(_num(r.get("n"), 1)) or 1 for r in rows)
    k = sum((int(_num(r.get("n"), 1)) or 1) * _num(r.get("success"), 0.0)
            for r in rows)
    return {"label": label, "n": n, "rate": round(k / n, 6) if n else 0.0}


def _record(member, ckey, key, dim, kind, a, b) -> dict:
    return {
        "member": member, "cell": ckey, "dimension": dim, "key": key,
        "kind": kind, "a": a, "b": b,
        "delta": round(abs(a["rate"] - b["rate"]), 6),
        "threshold": CONFLICT_DELTA,
        "resolution": CONFLICT_RESOLUTION,
        "resolution_note": (
            "both observation sets are retained in evidence.jsonl (append-only, "
            "nothing overwritten); the card's rate and interval are the "
            f"recency-weighted Wilson bound over ALL rows (half-life "
            f"{halflife_days():.0f}d), so the more recent set dominates without "
            "the older one being deleted"),
    }


def _source_conflicts(member, ckey, key, dim, grp) -> list[dict]:
    """Two named sources disagreeing about the same key (e.g. eval vs prod)."""
    by_src: dict[str, list[dict]] = {}
    for row in grp:
        by_src.setdefault(str(row.get("source", "")), []).append(row)
    if len(by_src) < 2:
        return []
    sums = [_summary(f"source:{s or 'unattributed'}", rs)
            for s, rs in sorted(by_src.items())]
    sums = [s for s in sums if s["n"] >= CONFLICT_MIN_N]
    out = []
    for i in range(len(sums)):
        for j in range(i + 1, len(sums)):
            if abs(sums[i]["rate"] - sums[j]["rate"]) > CONFLICT_DELTA:
                out.append(_record(member, ckey, key, dim,
                                   "source_disagreement", sums[i], sums[j]))
    return out


def _temporal_conflicts(member, ckey, key, dim, grp) -> list[dict]:
    """The same source flipping its verdict over time under one unchanged key."""
    ordered = sorted(grp, key=lambda r: _num(r.get("ts"), 0.0))
    mid = len(ordered) // 2
    older, newer = ordered[:mid], ordered[mid:]
    a = _summary("older_half", older)
    b = _summary("newer_half", newer)
    if a["n"] < CONFLICT_MIN_N or b["n"] < CONFLICT_MIN_N:
        return []
    if abs(a["rate"] - b["rate"]) <= CONFLICT_DELTA:
        return []
    return [_record(member, ckey, key, dim, "temporal_disagreement", a, b)]


# --- public read API ------------------------------------------------------

#: Reason attached to every read that hit a quarantined/unreadable ledger.
CORRUPT_REASON = ("evidence ledger unreadable and quarantined aside "
                  "(reject-never-repair) — falling back to untested")


def _rows_for(member: str, cell, rows: list[dict] | None = None):
    """Rows for one member x cell, or None when the ledger is unusable."""
    ckey = cell_key(cell)
    rows = _load_evidence() if rows is None else rows
    if rows is None:
        return None
    return [r for r in rows
            if str(r.get("member", "")) == member and str(r.get("cell", "")) == ckey]


def card(member: str, cell=CELL_ANY, *, now: float | None = None) -> GradeCard:
    """The derived grade card for one member x cell.

    Always computed from `evidence.jsonl` (the derived `cards.json` is a cache
    for operators/consumers, never the source of truth), so a deleted or corrupt
    card file can never change an answer."""
    now = time.time() if now is None else now
    ckey = cell_key(cell)
    if not enabled():
        return GradeCard(member=member, cell=ckey, status=UNTESTED,
                         confidence=0.0,
                         reason="OLYMPUS_MODELGRADE is off — store is inert",
                         dimensions={d: _weighted([], now, 1.0)
                                     for d in DIMENSIONS}, generated=now)
    try:
        rows = _rows_for(member, cell)
        if rows is None:                              # corrupt ledger => untested
            return GradeCard(member=member, cell=ckey, status=UNTESTED,
                             confidence=0.0, reason=CORRUPT_REASON,
                             dimensions={d: _weighted([], now, 1.0)
                                         for d in DIMENSIONS}, generated=now)
        return _derive(member, cell, rows, now)
    except Exception as err:                          # noqa: BLE001 - never raise
        _capture("modelgrade.card", err, context=f"{member} {ckey}")
        return GradeCard(member=member, cell=ckey, status=UNTESTED,
                         confidence=0.0, reason=f"derivation failed: {err}",
                         dimensions={d: _weighted([], now, 1.0)
                                     for d in DIMENSIONS}, generated=now)


def status(member: str, cell=CELL_ANY, *, now: float | None = None) -> str:
    """The state of one member x cell. `UNTESTED` whenever the flag is off."""
    return card(member, cell, now=now).status


def qualified(member: str, cell=CELL_ANY, *, now: float | None = None) -> bool:
    """THE routing question. True only on measured promotion (W2-I1.1)."""
    return status(member, cell, now=now) == QUALIFIED


def explain(member: str, cell=CELL_ANY, *, now: float | None = None) -> dict:
    """Why this state — the operator-facing answer.

    Names the BLOCKING reason (or `""` when qualified) alongside the counts,
    bounds, freshness, thresholds, the manual prior (marked as a claim), any
    recorded conflicts, and the full key the card was derived from."""
    c = card(member, cell, now=now)
    return {
        "member": member,
        "cell": c.cell,
        "enabled": enabled(),
        "status": c.status,
        "qualified": c.status == QUALIFIED,
        "blocking": "" if c.status == QUALIFIED else c.reason,
        "reason": c.reason,
        "confidence": c.confidence,
        "evidence_rows": c.evidence_rows,
        "freshness_days": c.freshness_days,
        "dimensions": c.dimensions,
        "thresholds": {"min_n": min_n(), "floor": floor_for(cell),
                       "ttl_days": ttl_days(),
                       "halflife_days": halflife_days(),
                       "promotion_dimension": PROMOTION_DIMENSION},
        "prior": c.prior,
        "key": c.key,
        "conflicts": c.conflicts,
        "discarded_observations": discarded(),
    }


def members(rows: list[dict] | None = None) -> list[tuple[str, str]]:
    """Every (member, cell) pair the ledger has evidence for."""
    rows = _load_evidence() if rows is None else rows
    return sorted({(str(r.get("member", "")), str(r.get("cell", "")))
                   for r in (rows or [])})


# --- the derived artifact -------------------------------------------------

CARDS_VERSION = 1


def rebuild(*, now: float | None = None) -> dict:
    """Derive `cards.json` from `evidence.jsonl` and persist it atomically.

    Deleting `cards.json` loses NOTHING — this reproduces it byte-for-byte from
    the append-only ledger for the same `now`. Inert (and non-writing) when the
    flag is off."""
    now = time.time() if now is None else now
    if not enabled():
        return {"version": CARDS_VERSION, "generated": now, "cards": {},
                "conflicts": [], "counts": {"evidence_rows": 0, "cards": 0}}
    rows = _load_evidence()
    degraded = rows is None
    rows = rows or []
    cards: dict[str, dict] = {}
    conflicts: list[dict] = []
    for member, ckey in members(rows):
        c = _derive(member, ckey, _rows_for(member, ckey, rows), now)
        cards.setdefault(member, {})[ckey] = c.to_dict()
        conflicts.extend(c.conflicts)
    data = {
        "version": CARDS_VERSION,
        "generated": now,
        "cards": cards,
        "conflicts": conflicts,
        "degraded": degraded,          # ledger was quarantined: everything untested
        "counts": {"evidence_rows": len(rows), "cards": len(cards),
                   "conflicts": len(conflicts)},
    }
    try:
        path = cards_path()
        with proclock.lock(_LOCK):
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, path)
    except (OSError, TimeoutError, ValueError) as err:
        _capture("modelgrade.rebuild", err, context=str(cards_path()))
    return data


def cards(*, now: float | None = None) -> dict:
    """Read the derived view, rebuilding it when missing, stale-schema or CORRUPT.

    A damaged `cards.json` is never an error and never a lost answer: it is a
    cache of a derivation the ledger can always reproduce."""
    if not enabled():
        return {"version": CARDS_VERSION, "cards": {}, "conflicts": [],
                "counts": {"evidence_rows": 0, "cards": 0}}
    path = cards_path()
    if not path.exists():
        return rebuild(now=now)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError) as err:
        _capture("modelgrade.cards_corrupt", err, context=str(path))
        return rebuild(now=now)
    if not isinstance(data, dict) or data.get("version") != CARDS_VERSION \
            or not isinstance(data.get("cards"), dict):
        _capture("modelgrade.cards_corrupt",
                 ValueError("unexpected cards.json shape"), context=str(path))
        return rebuild(now=now)
    return data


def format_report(*, now: float | None = None) -> str:
    """Human-readable qualification summary (for a later doctor/CLI surface)."""
    if not enabled():
        return "Model qualification: OFF (OLYMPUS_MODELGRADE)"
    data = cards(now=now)
    lines = [f"Model qualification: {data['counts']['cards']} member(s), "
             f"{data['counts']['evidence_rows']} evidence row(s)"]
    for member in sorted(data["cards"]):
        for ckey, c in sorted(data["cards"][member].items()):
            promo = c["dimensions"].get(PROMOTION_DIMENSION, {})
            lines.append(f"  {member} [{ckey}]: {c['status'].upper()} "
                         f"(n={promo.get('n', 0)}, lo={promo.get('lo', 0):.3f})")
            if c["status"] != QUALIFIED:
                lines.append(f"      why: {c['reason']}")
    if data["conflicts"]:
        lines.append(f"  conflicts: {len(data['conflicts'])} "
                     f"(resolution: {CONFLICT_RESOLUTION})")
    return "\n".join(lines)
