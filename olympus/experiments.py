"""Experiments & quarantine registry — Wave-2 capability C4 (synthesis R2).

One registry holds every experimental capability, failed optimization,
quarantined route, rejected algorithm, security incident, negative benchmark
result, scheduled retest and consciously accepted debt. Ruling R2 folded the
proposed `quarantine.py` in here: a quarantine is just an entry whose status
says so, carrying the same eulogy (`evidence`, `reason`) and the same scheduled
retest (`next_review`) as every other entry.

Why this exists (synthesis I6): *negative results and quarantines are artifacts,
not folklore*. A feature that was tried and dropped, or shipped behind a flag
whose benefit was never measured, must leave a durable, greppable record with
the numbers attached — otherwise the next author re-tries it, or worse, leaves
it enabled forever because nobody remembers to re-check.

Two invariants, both test-enforced:

- **W2-I4.1 — no experimental feature bypasses the registry.** `EXPERIMENTAL_FLAGS`
  declares every experimental env flag and the entry that justifies it, and
  `check_registry()` fails if a declared flag has no entry, if an entry is
  missing a required field, if a status is not in `STATUSES`, or if a date does
  not parse. It also enumerates the `OLYMPUS_*EXPERIMENTAL*` namespace (ruling
  R2) in the package source and in the live environment, so a new experimental
  flag cannot appear without an entry. This mirrors `capabilities.check_repo()`:
  a pure `check()` over data, plus a thin on-disk wrapper.
- **W2-I4.2 — no feature stays enabled after its evidence expires.** `active(id)`
  returns False once `next_review` is in the past, until a retest extends it.
  Features call `active()` before enabling themselves, so stale evidence
  *auto-disables* rather than quietly persisting.

Storage (spec §6):

- ``olympus/experiments.json`` — the COMMITTED registry. Data, not code:
  changing an entry is a reviewable diff, which is the point.
- ``MEMORY_DIR/experiments/state.jsonl`` — the append-only retest ledger
  (`record_test`). A retest recorded here with a later `next_review` extends the
  effective review window, so "re-tested" genuinely re-enables a feature without
  a commit; the committed registry stays the human-reviewed record of intent.

Failure policy: this module never raises. A corrupt or missing registry is
reported via `errors.capture` and read as an EMPTY registry — which makes
`active()` return False for everything, i.e. the fail-closed direction (an
unreadable registry disables experiments, never enables them).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
from pathlib import Path

from . import config, proclock

SCHEMA_VERSION = 1

# --- status vocabulary --------------------------------------------------------
# proposed      hypothesis registered; not built, or not permitted to run yet
# active        permitted to run in production (behind its flag)
# shadow        permitted to run in record-only mode (changes nothing)
# quarantined   was tried, disabled by evidence, retest scheduled
# rejected      tried and abandoned; the entry is the eulogy
# accepted_debt a known limitation consciously carried, with its residual named
# incident      a security or reliability incident and its containment
PROPOSED = "proposed"
ACTIVE = "active"
SHADOW = "shadow"
QUARANTINED = "quarantined"
REJECTED = "rejected"
ACCEPTED_DEBT = "accepted_debt"
INCIDENT = "incident"

STATUSES = (PROPOSED, ACTIVE, SHADOW, QUARANTINED, REJECTED, ACCEPTED_DEBT,
            INCIDENT)

# Only these two statuses may run. Everything else — including accepted_debt —
# answers False from `active()`.
ENABLED_STATUSES = (ACTIVE, SHADOW)

# Every field is required to be PRESENT on every entry (spec §4).
REQUIRED_FIELDS = (
    "id", "owner", "hypothesis", "status", "evidence", "reason",
    "activation_condition", "deactivation_trigger", "cost", "security_impact",
    "created", "last_tested", "next_review", "outcome",
)

# ...and required to be non-empty, except these two: an experiment that has
# never been tested has no honest date to put in `last_tested`, and one that is
# still open has no `outcome`. Writing a placeholder date would be a small lie,
# so emptiness is the honest encoding and is allowed HERE ONLY.
MAY_BE_EMPTY = ("last_tested", "outcome")

DATE_FIELDS = ("created", "last_tested", "next_review")

# The declared experimental-flag surface: flag -> the entry that justifies it.
# W2-I4.1 — a flag here without an entry is a CI failure. Flags for Wave-2
# modules that have not landed yet are declared deliberately: the registry entry
# exists BEFORE the feature, which is the whole point of the invariant.
EXPERIMENTAL_FLAGS: dict[str, str] = {
    # Wave-1, shipped and flag-gated
    "OLYMPUS_CTX_BUDGET": "ctxbudget-calibrated-estimator",
    "OLYMPUS_TOOL_VALIDATE": "toolcall-validate-off-legacy",
    "OLYMPUS_TOOL_SALVAGE": "toolcall-salvage",
    # adaptive selection surfaces (safety proven, benefit unmeasured)
    "OLYMPUS_LEARNED_ROUTING": "learned-routing",
    "OLYMPUS_BANDIT_ROUTING": "bandit-routing",
    "OLYMPUS_SEMANTIC_ROUTING": "semantic-routing",
    "OLYMPUS_SEMANTIC_SKILLS": "semantic-skill-scoping",
    # Wave-2 capabilities — entries first, modules in their own PRs
    "OLYMPUS_MODELGRADE": "modelgrade-ladder",
    "OLYMPUS_CTXHEAT": "ctxheat-provisional-constants",
    "OLYMPUS_ROUTESUB": "routesub-substitution-bands",
    "OLYMPUS_WATCHDOG": "watchdog-progress-leases",
}

# Ruling R2's reserved namespace: any OLYMPUS_..EXPERIMENTAL.. env name is an
# experimental flag by construction and must be declared above.
_NAMESPACE_RE = re.compile(r"OLYMPUS_[A-Z0-9_]*EXPERIMENTAL[A-Z0-9_]*")


def _report(where: str, exc: BaseException, context: str = "") -> None:
    """Best-effort error capture — this module must never raise at a caller."""
    try:
        from . import errors
        errors.capture(where, exc, context=context)
    except Exception:                                   # pragma: no cover
        pass


# --- the committed registry ---------------------------------------------------

def registry_path() -> Path:
    """The committed registry file (data, shipped with the package)."""
    return Path(__file__).resolve().parent / "experiments.json"


def _empty_registry() -> dict:
    return {"version": SCHEMA_VERSION, "entries": []}


def load() -> dict:
    """The committed registry as ``{"version": int, "entries": [ ... ]}``.

    Never raises: a missing, unreadable, corrupt or wrongly-shaped registry is
    captured to `errors` and read as EMPTY — which disables every experiment
    (fail-closed), rather than enabling anything on bad data."""
    path = registry_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _report("experiments.load", exc, context=f"registry unreadable: {path}")
        return _empty_registry()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        _report("experiments.load", exc, context=f"corrupt registry: {path}")
        return _empty_registry()
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        _report("experiments.load",
                ValueError("registry must be an object with an 'entries' list"),
                context=f"bad registry shape: {path}")
        return _empty_registry()
    entries = [e for e in data["entries"] if isinstance(e, dict)]
    if len(entries) != len(data["entries"]):
        _report("experiments.load",
                ValueError("registry contains non-object entries (dropped)"),
                context=f"{len(data['entries']) - len(entries)} dropped")
    version = data.get("version")
    return {"version": version if isinstance(version, int) else SCHEMA_VERSION,
            "entries": entries}


def all_entries() -> list[dict]:
    """Every registry entry, in committed order."""
    return list(load()["entries"])


def entry(entry_id: str) -> dict | None:
    """One entry by id, or None if it is not registered."""
    for e in all_entries():
        if e.get("id") == entry_id:
            return e
    return None


def by_status(status: str) -> list[dict]:
    """Every entry with exactly this status (see `STATUSES`)."""
    return [e for e in all_entries() if e.get("status") == status]


def known_flags() -> dict[str, str]:
    """The declared experimental-flag -> entry-id map (a copy; callers may not
    mutate the registry's flag surface at runtime)."""
    return dict(EXPERIMENTAL_FLAGS)


# --- dates --------------------------------------------------------------------

def _as_date(value) -> _dt.date | None:
    """An ISO date (or date/datetime) as a date, else None. Never raises."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return _dt.date.fromisoformat(text[:10])
    except (ValueError, TypeError):
        return None


def _today(now=None) -> _dt.date:
    parsed = _as_date(now)
    return parsed if parsed is not None else _dt.date.today()


# --- the retest ledger (MEMORY_DIR/experiments/state.jsonl) -------------------

def state_path() -> Path:
    """The append-only retest ledger. Not created by this call."""
    return config.MEMORY_DIR / "experiments" / "state.jsonl"


def state_records(entry_id: str | None = None) -> list[dict]:
    """Recorded retests, oldest-first (all, or for one entry). Never raises;
    unparseable lines are skipped rather than failing the read."""
    path = state_path()
    try:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        if entry_id is None or rec.get("id") == entry_id:
            out.append(rec)
    return out


def _recorded_reviews() -> dict[str, _dt.date]:
    """The latest recorded `next_review` per entry id, from the retest ledger."""
    latest: dict[str, _dt.date] = {}
    for rec in state_records():
        rid = rec.get("id")
        when = _as_date(rec.get("next_review"))
        if not isinstance(rid, str) or when is None:
            continue
        if rid not in latest or when > latest[rid]:
            latest[rid] = when
    return latest


def next_review(entry_id: str, *, _entry: dict | None = None,
                _recorded: dict | None = None) -> _dt.date | None:
    """The EFFECTIVE review date: the committed `next_review`, extended by any
    later date recorded through `record_test`. None when neither parses (which
    `active()` treats as expired — evidence that cannot prove its own freshness
    does not count)."""
    e = _entry if _entry is not None else entry(entry_id)
    if e is None:
        return None
    best = _as_date(e.get("next_review"))
    recorded = _recorded_reviews() if _recorded is None else _recorded
    extended = recorded.get(entry_id)
    if extended is not None and (best is None or extended > best):
        best = extended
    return best


def record_test(entry_id: str, *, outcome: str, evidence: str,
                next_review=None) -> dict:
    """Append a retest result to the ledger — the retest sink (spec §7).

    `next_review` (an ISO date or date object) extends the entry's effective
    review window, so re-testing an expired experiment re-enables it per
    W2-I4.2. An unparseable date is simply not recorded as an extension.

    Never raises: a broken lock, an unwritable MEMORY_DIR or a full disk is
    captured to `errors` and the record is still returned to the caller."""
    rec: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "id": str(entry_id)[:200],
        "outcome": str(outcome)[:1000],
        "evidence": str(evidence)[:4000],
    }
    when = _as_date(next_review)
    if when is not None:
        rec["next_review"] = when.isoformat()
    try:
        with proclock.lock("experiments"):
            path = state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except Exception as exc:
        _report("experiments.record_test", exc, context=rec["id"])
    return rec


# --- the two questions features actually ask ---------------------------------

def active(entry_id: str, *, now=None) -> bool:
    """**The function a feature calls before enabling itself.**

    True only when the entry exists, its status permits running
    (`ENABLED_STATUSES`), and its effective `next_review` is not in the past
    (W2-I4.2). Everything else — unregistered id, `proposed`, `quarantined`,
    `rejected`, `accepted_debt`, `incident`, expired evidence, an unreadable
    registry — is False."""
    e = entry(entry_id)
    if e is None:
        return False
    if e.get("status") not in ENABLED_STATUSES:
        return False
    review = next_review(entry_id, _entry=e)
    if review is None:
        return False
    return review >= _today(now)


def expired(*, now=None) -> list[str]:
    """Ids whose effective `next_review` has passed (or does not parse) — the
    retest worklist. Independent of status, so a stale `accepted_debt` entry
    also surfaces for re-examination."""
    today = _today(now)
    recorded = _recorded_reviews()
    out: list[str] = []
    for e in all_entries():
        eid = e.get("id")
        if not isinstance(eid, str) or not eid:
            continue
        review = next_review(eid, _entry=e, _recorded=recorded)
        if review is None or review < today:
            out.append(eid)
    return out


# --- the CI gate (W2-I4.1) ----------------------------------------------------

def namespace_flags() -> set[str]:
    """Every `OLYMPUS_*EXPERIMENTAL*` name referenced in the package source or
    present in the live environment (ruling R2's reserved namespace). This
    module is skipped — it names the namespace in order to police it."""
    found: set[str] = set()
    here = Path(__file__).resolve()
    try:
        paths = sorted(here.parent.glob("*.py"))
    except OSError:                                     # pragma: no cover
        paths = []
    for path in paths:
        if path.name == here.name:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:                                 # pragma: no cover
            continue
        found.update(_NAMESPACE_RE.findall(text))
    found.update(n for n in os.environ if _NAMESPACE_RE.fullmatch(n))
    return found


def check(registry: dict | None = None, flags: dict | None = None,
          *, namespace: set | None = None) -> list[str]:
    """Pure registry check — the shape of `capabilities.check()`.

    Returns human-readable problems; an empty list means the registry is
    complete and internally consistent. Verifies: every declared experimental
    flag has an entry (W2-I4.1); every entry carries all `REQUIRED_FIELDS`,
    non-empty except `MAY_BE_EMPTY`; every status is in `STATUSES`; every date
    parses as ISO and `next_review` is not before `created`; ids are unique;
    and no `OLYMPUS_*EXPERIMENTAL*` flag exists without a declaration."""
    reg = load() if registry is None else registry
    flag_map = EXPERIMENTAL_FLAGS if flags is None else flags
    problems: list[str] = []

    entries = reg.get("entries") if isinstance(reg, dict) else None
    if not isinstance(entries, list):
        return ["experiments.json is missing, unreadable, or has no 'entries' "
                "list — the registry is committed data and must be present."]
    if isinstance(reg, dict) and reg.get("version") != SCHEMA_VERSION:
        problems.append(
            f"registry version is {reg.get('version')!r}, expected "
            f"{SCHEMA_VERSION}.")

    seen: set[str] = set()
    for index, e in enumerate(entries):
        if not isinstance(e, dict):
            problems.append(f"entry #{index} is not an object.")
            continue
        raw_id = e.get("id")
        eid = raw_id if isinstance(raw_id, str) and raw_id.strip() \
            else f"#{index}"

        for field in REQUIRED_FIELDS:
            if field not in e:
                problems.append(
                    f"experiment '{eid}' is missing the required field "
                    f"'{field}' — every entry must carry all "
                    f"{len(REQUIRED_FIELDS)} fields.")
                continue
            value = e[field]
            if not isinstance(value, str):
                problems.append(
                    f"experiment '{eid}' field '{field}' must be a string, got "
                    f"{type(value).__name__}.")
            elif not value.strip() and field not in MAY_BE_EMPTY:
                problems.append(
                    f"experiment '{eid}' has an empty '{field}' — an entry "
                    f"without it is folklore, not a record.")

        if eid in seen:
            problems.append(f"duplicate experiment id '{eid}'.")
        seen.add(eid)

        status = e.get("status")
        if isinstance(status, str) and status.strip() and status not in STATUSES:
            problems.append(
                f"experiment '{eid}' has invalid status '{status}' — allowed: "
                f"{', '.join(STATUSES)}.")

        for field in DATE_FIELDS:
            raw = e.get(field)
            if isinstance(raw, str) and raw.strip() and _as_date(raw) is None:
                problems.append(
                    f"experiment '{eid}' has an unparseable {field} "
                    f"'{raw}' (expected ISO YYYY-MM-DD).")
        created, review = _as_date(e.get("created")), _as_date(e.get("next_review"))
        if created is not None and review is not None and review < created:
            problems.append(
                f"experiment '{eid}' has next_review {review} before created "
                f"{created}.")

    for flag in sorted(flag_map):
        target = flag_map[flag]
        if target not in seen:
            problems.append(
                f"experimental flag {flag} maps to '{target}', which has no "
                f"entry in experiments.json — W2-I4.1: no experimental feature "
                f"bypasses the registry.")

    discovered = namespace_flags() if namespace is None else set(namespace)
    for name in sorted(discovered):
        if name not in flag_map:
            problems.append(
                f"{name} is in the reserved OLYMPUS_*EXPERIMENTAL* namespace "
                f"but is not declared in experiments.EXPERIMENTAL_FLAGS "
                f"(W2-I4.1).")
    return problems


def check_registry() -> list[str]:
    """Run `check` against the committed registry and the declared flags —
    the CI gate (mirrors `capabilities.check_repo()`)."""
    return check()
