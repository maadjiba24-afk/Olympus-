"""The Calibration Record — provider-neutral, customer-side reliability evidence.

**Observation only.** This module RECORDS and REPORTS. It writes no decision:
nothing here changes routing, permissions, trust tiers, prompts, or agent
behaviour, and a test pins that no decision module imports it. That mirrors the
contract `outcomes.py` already keeps ("suggested to the user, not imposed") and
the same no-write-back discipline the parked offline-RL preference scaffold
keeps. Automation, if it ever comes, is a separate decision.

Why it exists: measured reliability per provider/model/domain is the one asset
that compounds, because *time cannot be backfilled* (see docs/MOAT_ANALYSIS.md).
This is the falsifiable prototype of that hypothesis.

Storage is a single append-only, hash-chained JSONL at
`MEMORY_DIR/calibration.jsonl`:

  * **append-only** — feedback about an earlier run is a NEW entry referencing
    it; history is never rewritten (deletion is a tombstone, also appended);
  * **content-addressed + chained** — `entry_hash = sha256(canonical(core))` and
    `prev` is the previous entry's hash, so any edit, reorder, or deletion of an
    earlier entry breaks every entry after it (tamper-evident WITHOUT crypto);
  * **signed when possible** — under a domain-separated witness subkey, exactly
    as `ledger` does. Unlike `attest` (which must fail closed rather than mint an
    unsigned proof), an unavailable crypto backend here degrades to an UNSIGNED
    entry: dropping observations would bias the dataset, which is the one failure
    a calibration record cannot tolerate. `verify()` reports the split honestly.

Off by default (`OLYMPUS_CALIBRATION`); disabled means zero writes.
Never stores prompt or response text — only hashes and metadata.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path

from . import config, witness

SCHEMA = "olympus-calibration/1"
SCHEMA_FAMILY = "olympus-calibration"
_SUPPORTED_MAJOR = 1              # newest schema major this build can read
LABEL = "calibration/v1"          # witness subkey (domain separation)

_LOCK = threading.Lock()

# Entry kinds.
OBSERVATION = "observation"
FEEDBACK = "feedback"
COMPARISON = "comparison"
TOMBSTONE = "tombstone"
_KINDS = (OBSERVATION, FEEDBACK, COMPARISON, TOMBSTONE)

# Outcome vocabulary — the first four are reused VERBATIM from outcomes.py so the
# two stores speak the same language; the rest cover the full feedback surface.
APPROVED = "approved"
APPROVED_AFTER_EDIT = "approved_after_edit"   # kept for back-compat with Phase 1
EDITED = "edited"                  # user changed the output before accepting
REJECTED = "rejected"
UNDONE = "undone"
RETRIED = "retried"
OVERRIDDEN = "overridden"
ABANDONED = "abandoned"            # user walked away without accepting/rejecting
PREFERENCE = "preference"          # an explicit stated preference
VERIFIED = "verified"              # an externally verified downstream outcome
OUTCOMES = (APPROVED, APPROVED_AFTER_EDIT, EDITED, REJECTED, UNDONE, RETRIED,
            OVERRIDDEN, ABANDONED, PREFERENCE, VERIFIED)

# --- evidence hierarchy (documented; NEVER collapsed into one "success") ------
# Four strictly-separated levels of evidence about a run. Higher is stronger, but
# they are DIFFERENT QUESTIONS, not points on one axis: completion asks "did it
# run", satisfaction asks "did the user accept it", verified asks "was it actually
# correct". Analytics reports each separately; nothing blends them.
EV_COMPLETION = 1                  # execution completed (result field)
EV_IMPLICIT = 2                    # implicit behavioural signal (edit/retry/abandon)
EV_EXPLICIT = 3                    # explicit user feedback (approve/reject/preference)
EV_VERIFIED = 4                    # externally verified downstream outcome
EVIDENCE_LEVELS = {EV_COMPLETION: "completion", EV_IMPLICIT: "implicit",
                   EV_EXPLICIT: "explicit", EV_VERIFIED: "verified"}

# Which evidence level each outcome carries.
_OUTCOME_EVIDENCE = {
    EDITED: EV_IMPLICIT, RETRIED: EV_IMPLICIT, UNDONE: EV_IMPLICIT,
    ABANDONED: EV_IMPLICIT, OVERRIDDEN: EV_IMPLICIT,
    APPROVED: EV_EXPLICIT, APPROVED_AFTER_EDIT: EV_EXPLICIT,
    REJECTED: EV_EXPLICIT, PREFERENCE: EV_EXPLICIT,
    VERIFIED: EV_VERIFIED,
}

_RESULTS = ("ok", "error", "refused", "timeout")

# --- domain taxonomy (controlled + versioned) ---------------------------------
# A SMALL, EXPLICIT taxonomy — never free-form. The specialist→domain map mirrors
# `routing_outcomes._TASK_TYPE` (the pipeline's own routing tag, which already
# reflects the real Olympus workloads) so classification is DETERMINISTIC from
# structured metadata and never needs a model call or prompt-text inspection.
# Bump DOMAIN_TAXONOMY_VERSION on any change to the sets below; every classified
# observation records the version it was tagged under.
DOMAIN_TAXONOMY_VERSION = "1"
UNCLASSIFIED = "unclassified"      # no structured metadata to classify from
OTHER = "other"                    # classified, but outside the known set
_DOMAIN_BY_SPECIALIST = {
    "hephaestus": "code",
    "argus": "research", "mnemosyne": "research",
    "plutus": "finance",
    "peitho": "marketing", "iris": "social",
    "chronos": "scheduling", "angelos": "inbox",
    "aegis": "security", "chiron": "coaching",
    "prometheus": "evolution", "metis": "learning",
    "hermes": "general", "zeus": "general",
}
# A coarse tool→domain fallback for tool-led runs with no specialist. Kept tiny
# and deterministic; unknown tools fall through to `other`, never a guess.
_DOMAIN_BY_TOOL = {
    "write_file": "code", "read_file": "code", "run_python": "code",
    "web_search": "research", "web_fetch": "research",
    "send_email": "inbox", "assess": "security",
}
DOMAINS = frozenset(_DOMAIN_BY_SPECIALIST.values()) | frozenset(
    _DOMAIN_BY_TOOL.values()) | {UNCLASSIFIED, OTHER}

# Classification source (provenance of the domain label).
SRC_EXPLICIT = "explicit"          # a caller passed the domain in
SRC_SPECIALIST = "specialist"      # derived from the dispatched specialist
SRC_TOOL = "tool"                  # derived from the tool used
SRC_NONE = "none"                  # nothing to classify from

# Don't infer anything from a tiny history — same bar as outcomes._MIN_SAMPLES.
_MIN_SAMPLES = 5
_Z = 1.96                          # 95% Wilson interval


class CalibrationError(ValueError):
    """A malformed entry, or a chain that does not verify."""


# --- configuration ---------------------------------------------------------

def enabled() -> bool:
    """OFF by default. Disabled means zero writes and zero behaviour change."""
    return os.environ.get("OLYMPUS_CALIBRATION", "").strip().lower() in (
        "1", "true", "yes", "on")


def retention_days() -> int:
    """0 (default) keeps everything; >0 makes `prune()` tombstone older entries."""
    try:
        return max(0, int(os.environ.get("OLYMPUS_CALIBRATION_RETENTION_DAYS", "0")))
    except ValueError:
        return 0


def export_allowed() -> bool:
    """Export is a separate, explicit permission (default ON only when collection
    is on). An operator can forbid export while still collecting."""
    v = os.environ.get("OLYMPUS_CALIBRATION_EXPORT", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def lock_timeout() -> float:
    """Bounded wait for the cross-process append lock. On expiry an observation
    is dropped with a VISIBLE error (never silently), because blocking a run on a
    wedged peer is worse than losing one telemetry row."""
    try:
        return max(0.1, float(os.environ.get("OLYMPUS_CALIBRATION_LOCK_TIMEOUT", "5")))
    except ValueError:
        return 5.0


def status() -> dict:
    """Visible collection status — for `olympus calibration status` and health()."""
    return {"enabled": enabled(), "export_allowed": export_allowed(),
            "retention_days": retention_days(), "path": str(path()),
            "schema": SCHEMA, "taxonomy_version": DOMAIN_TAXONOMY_VERSION}


def path() -> Path:
    return Path(config.MEMORY_DIR) / "calibration.jsonl"


# --- domain classification (deterministic; never touches prompt text) ---------

def classify_domain(*, specialist: str = "", tool: str = "",
                    explicit: str = "") -> dict:
    """Classify a run's domain from STRUCTURED METADATA ONLY — never from prompt
    or output text, so no sensitive attribute is ever inferred. Precedence:
    explicit caller override > dispatched specialist > tool. Deterministic
    whenever any structured signal is present. Returns
    {domain, confidence, source, taxonomy_version}."""
    v = DOMAIN_TAXONOMY_VERSION
    if explicit:
        d = explicit.strip().lower()
        # An explicit label is honoured but still constrained to the taxonomy:
        # an unknown explicit domain is recorded as `other`, not invented.
        return {"domain": d if d in DOMAINS else OTHER, "confidence": 1.0,
                "source": SRC_EXPLICIT, "taxonomy_version": v}
    if specialist:
        s = specialist.strip().lower()
        if s in _DOMAIN_BY_SPECIALIST:
            return {"domain": _DOMAIN_BY_SPECIALIST[s], "confidence": 1.0,
                    "source": SRC_SPECIALIST, "taxonomy_version": v}
        return {"domain": OTHER, "confidence": 0.5, "source": SRC_SPECIALIST,
                "taxonomy_version": v}
    if tool:
        t = tool.strip().lower()
        if t in _DOMAIN_BY_TOOL:
            return {"domain": _DOMAIN_BY_TOOL[t], "confidence": 0.9,
                    "source": SRC_TOOL, "taxonomy_version": v}
        return {"domain": OTHER, "confidence": 0.4, "source": SRC_TOOL,
                "taxonomy_version": v}
    return {"domain": UNCLASSIFIED, "confidence": 0.0, "source": SRC_NONE,
            "taxonomy_version": v}


# --- redaction -------------------------------------------------------------

def text_ref(text: str | None) -> str:
    """A REFERENCE to text, never the text. Empty input yields "" so a missing
    field stays missing rather than hashing to a constant."""
    if not text:
        return ""
    return "sha256:" + hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]


def config_id(provider: str = "", model: str = "", base_url: str = "",
              effort: str = "") -> str:
    """Stable identity for a model CONFIGURATION (not just its name), so the same
    model at a different endpoint/effort is not silently pooled."""
    core = {"provider": provider or "", "model": model or "",
            "base_url": base_url or "", "effort": effort or ""}
    return hashlib.sha256(witness.canonical_json(core)).hexdigest()[:16]


def model_key(provider: str, model: str) -> str:
    """`provider/model` — the identity format compare.model_label() already uses.
    Provider and model stay EXPLICIT; they are never collapsed into one score."""
    return f"{provider}/{model}" if model else (provider or "unknown")


# --- chain primitives (mirrors ledger.py) ----------------------------------

def _core(seq: int, prev: str | None, kind: str, at: str, body: dict,
          event_key: str) -> dict:
    return {"schema": SCHEMA, "seq": int(seq), "prev": prev, "kind": kind,
            "at": at, "event_key": event_key, "body": body}


def _entry_hash(core: dict) -> str:
    return "sha256:" + hashlib.sha256(witness.canonical_json(core)).hexdigest()


def _seal(seq: int, prev: str | None, kind: str, at: str, body: dict,
          event_key: str) -> dict:
    """Mint a content-addressed, chained, (optionally) signed entry."""
    core = _core(seq, prev, kind, at, body, event_key)
    h = _entry_hash(core)
    entry = dict(core)
    entry["entry_hash"] = h
    try:
        entry["publicKey"] = witness.sub_public_key_hex(LABEL)
        entry["signature"] = witness.sign_with(LABEL, h.encode("utf-8"))
    except Exception:
        # Crypto unavailable (witness.WitnessError) or any signing failure:
        # record UNSIGNED rather than lose the observation. verify() reports it
        # as unsigned — honest, not silent.
        entry["publicKey"] = ""
        entry["signature"] = ""
    return entry


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat()


def _event_key(kind: str, parts: dict) -> str:
    """Idempotency key: the same logical event always hashes the same, so
    duplicate ingestion is a no-op rather than a double count."""
    core = {"kind": kind, **{k: parts[k] for k in sorted(parts)}}
    return "sha256:" + hashlib.sha256(
        witness.canonical_json(core)).hexdigest()[:24]


# --- reading ---------------------------------------------------------------

def _read_raw() -> list[dict]:
    """All well-formed entries, oldest first. Malformed lines are SKIPPED (a
    hand-edited file must not crash the reader) — same tolerance as
    attest.list_attestations."""
    p = path()
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        schema = str(rec.get("schema", ""))
        # Accept any version of OUR family; ignore foreign/unknown-major schemas
        # so historical entries stay readable across upgrades.
        if not schema.startswith(SCHEMA_FAMILY + "/"):
            continue
        out.append(rec)
    return out


def migrate_entry(entry: dict) -> dict:
    """Upgrade an older entry to the current shape IN MEMORY (never in place, so
    the on-disk chain and its hashes remain valid)."""
    e = dict(entry)
    if str(e.get("schema", "")) == SCHEMA:
        return e
    e.setdefault("body", {})
    e.setdefault("kind", OBSERVATION)
    e.setdefault("event_key", "")
    e["_migrated_from"] = e.get("schema", "")
    e["schema"] = SCHEMA
    return e


def entries(kind: str | None = None, *, include_tombstoned: bool = False
            ) -> list[dict]:
    """Entries oldest-first, migrated in memory. Tombstoned entries are hidden
    by default (the data is treated as deleted) while remaining on the chain."""
    raw = [migrate_entry(e) for e in _read_raw()]
    dead = {str((e.get("body") or {}).get("ref_event_key", ""))
            for e in raw if e.get("kind") == TOMBSTONE}
    out = []
    for e in raw:
        if not include_tombstoned and e.get("event_key") in dead \
                and e.get("kind") != TOMBSTONE:
            continue
        if kind and e.get("kind") != kind:
            continue
        out.append(e)
    return out


def _head() -> tuple[int, str | None, set[str]]:
    """(next_seq, prev_hash, seen_event_keys) — one pass, used by every append."""
    raw = _read_raw()
    if not raw:
        return 0, None, set()
    last = raw[-1]
    keys = {str(e.get("event_key", "")) for e in raw if e.get("event_key")}
    return int(last.get("seq", len(raw) - 1)) + 1, last.get("entry_hash"), keys


# --- appending -------------------------------------------------------------

def _append(kind: str, body: dict, event_key: str, at: str | None = None
            ) -> dict | None:
    """Append one entry, safely across THREADS AND PROCESSES.

    The read-modify-write (find the tail hash + seen keys, then append) runs
    under `proclock` (the repo's existing fcntl cross-process lock — ADR 0005),
    so two Olympus processes sharing MEMORY_DIR cannot interleave lines or fork
    the chain. The write itself is a single `write()` of one `\\n`-terminated
    line, which is atomic for small records on local POSIX filesystems (see
    docs/CALIBRATION_RECORD.md §6 for the NFS caveat).

    Returns the entry, or None when collection is disabled, the event is a
    duplicate, or the lock could not be acquired within the bounded timeout. A
    lock timeout is reported VISIBLY (never a silent drop) — losing one telemetry
    row under a wedged peer beats blocking a real run. Never raises into the
    caller."""
    if kind not in _KINDS:
        raise CalibrationError(f"unknown entry kind {kind!r}")
    if not enabled():
        return None
    from . import proclock
    try:
        with proclock.lock("calibration", timeout=lock_timeout()):
            seq, prev, seen = _head()
            if event_key and event_key in seen:
                return None                    # idempotent: already ingested
            entry = _seal(seq, prev, kind, at or _now_iso(), body, event_key)
            p = path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())          # durable before the lock releases
            return entry
    except TimeoutError as err:
        try:
            from . import errors
            errors.capture("calibration", err, context="append lock timeout")
        except Exception:
            pass
        return None                            # visible above, not silent here
    except OSError:
        return None


def _clean(body: dict) -> dict:
    """Drop None/empty-string values so a partial record is SPARSE, not corrupt.
    Zero and False are meaningful and kept."""
    return {k: v for k, v in body.items() if v is not None and v != ""}


def record_observation(run_id: str, *, domain: str = "", provider: str = "",
                       model: str = "", base_url: str = "", effort: str = "",
                       specialist: str = "", tool: str = "",
                       latency_ms: float | None = None,
                       tokens_in: int | None = None,
                       tokens_out: int | None = None,
                       cost_usd: float | None = None,
                       result: str = "", task: str | None = None,
                       trace_id: str = "", compare_id: str = "",
                       at: str | None = None) -> dict | None:
    """Record ONE governed model call/run. Every field but `run_id` is optional —
    missing evidence is simply absent. `task` is HASHED, never stored."""
    if not run_id:
        raise CalibrationError("run_id is required")
    if result and result not in _RESULTS:
        raise CalibrationError(f"unknown result {result!r}")
    # Deterministic domain classification from structured metadata only. An
    # explicit `domain` is honoured as a caller override; otherwise it is derived
    # from the specialist, then the tool — never from the prompt text.
    dc = classify_domain(specialist=specialist, tool=tool, explicit=domain)
    body = _clean({
        "run_id": str(run_id),
        "domain": dc["domain"], "domain_source": dc["source"],
        "domain_confidence": dc["confidence"],
        "taxonomy_version": dc["taxonomy_version"],
        "provider": provider, "model": model,
        "model_key": model_key(provider, model) if provider else "",
        "config_id": config_id(provider, model, base_url, effort) if provider else "",
        "specialist": specialist, "tool": tool,
        "latency_ms": latency_ms, "tokens_in": tokens_in,
        "tokens_out": tokens_out, "cost_usd": cost_usd,
        "result": result, "evidence_level": EV_COMPLETION,
        "task_hash": text_ref(task),
        "provenance": _clean({"trace_id": trace_id, "compare_id": compare_id}) or None,
    })
    return _append(OBSERVATION, body,
                   _event_key(OBSERVATION, {"run_id": str(run_id),
                                            "tool": tool, "specialist": specialist}),
                   at=at)


def record_feedback(run_id: str, outcome: str, *, note: str | None = None,
                    at: str | None = None, seq_hint: str = "") -> dict | None:
    """Record a signal about an earlier run as a NEW entry — the observation is
    never rewritten, and a run may accrue MANY feedback events over time. Each
    carries its evidence LEVEL (implicit / explicit / verified), so analytics can
    keep completion, satisfaction, and verified quality strictly separate. An
    `edit` is an implicit signal, NOT a failure; an `approval` is an explicit
    signal, NOT proof of correctness. `note` is hashed, never stored."""
    if not run_id:
        raise CalibrationError("run_id is required")
    if outcome not in OUTCOMES:
        raise CalibrationError(f"unknown outcome {outcome!r}")
    level = _OUTCOME_EVIDENCE.get(outcome, EV_IMPLICIT)
    body = _clean({"ref_run_id": str(run_id), "outcome": outcome,
                   "evidence_level": level,
                   "evidence": EVIDENCE_LEVELS.get(level, ""),
                   "note_hash": text_ref(note)})
    return _append(FEEDBACK, body,
                   _event_key(FEEDBACK, {"run_id": str(run_id),
                                         "outcome": outcome, "n": seq_hint}),
                   at=at)


def record_comparison(compare_id: str, *, chosen_model: str = "",
                      models: tuple | list = (), run_ids: tuple | list = (),
                      blind: bool = True, at: str | None = None) -> dict | None:
    """Link a blind comparison (compare.py's `cid`) to its runs and its winner.
    `compare.py` prunes to 50 stored comparisons, so this is the DURABLE record."""
    if not compare_id:
        raise CalibrationError("compare_id is required")
    body = _clean({"compare_id": str(compare_id), "chosen_model": chosen_model,
                   "models": sorted(str(m) for m in models) or None,
                   "run_ids": [str(r) for r in run_ids] or None,
                   "blind": bool(blind)})
    return _append(COMPARISON, body,
                   _event_key(COMPARISON, {"compare_id": str(compare_id)}), at=at)


def tombstone(event_key: str, reason: str = "", at: str | None = None
              ) -> dict | None:
    """Redact an entry by APPENDING a tombstone. The chain is never rewritten, so
    tamper-evidence survives deletion."""
    if not event_key:
        raise CalibrationError("event_key is required")
    return _append(TOMBSTONE, _clean({"ref_event_key": str(event_key),
                                      "reason": reason[:200]}),
                   _event_key(TOMBSTONE, {"ref": str(event_key)}), at=at)


def prune(now: float | None = None) -> int:
    """Tombstone entries older than the retention window. Returns how many."""
    days = retention_days()
    if not days or not enabled():
        return 0
    import datetime
    now = now if now is not None else time.time()
    cutoff = now - days * 86400
    n = 0
    for e in entries():
        if e.get("kind") == TOMBSTONE:
            continue
        try:
            ts = datetime.datetime.fromisoformat(str(e.get("at", ""))).timestamp()
        except (ValueError, TypeError):
            continue
        if ts < cutoff and tombstone(str(e.get("event_key", "")), "retention"):
            n += 1
    return n


# --- verification ----------------------------------------------------------

# Integrity states the verifier distinguishes (never collapsed into one bool).
V_VALID = "valid"                          # signed and chained
V_UNSIGNED_VALID = "unsigned_valid"        # structurally valid, no crypto sig
V_CORRUPTED_CHAIN = "corrupted_chain"      # a middle entry altered/removed
V_INCOMPLETE_TAIL = "incomplete_trailing_write"   # last line truncated (recoverable)
V_UNSUPPORTED_SCHEMA = "unsupported_schema"       # newer major we can't read
V_MISSING_EVIDENCE = "missing_referenced_evidence"  # feedback → absent run


def _physical_lines() -> list[str]:
    p = path()
    if not p.exists():
        return []
    try:
        return [ln for ln in p.read_text(encoding="utf-8").split("\n")]
    except OSError:
        return []


def verify() -> dict:
    """Verify the chain and CATEGORIZE its integrity. Distinguishes, without ever
    silently repairing a middle-of-chain fault:

      valid · unsigned_valid · corrupted_chain · incomplete_trailing_write ·
      unsupported_schema · missing_referenced_evidence

    A truncated LAST line (a write interrupted by a crash) is reported as a
    recoverable `incomplete_trailing_write`; a malformed or hash-broken MIDDLE
    entry is `corrupted_chain` and is never auto-healed. Signature status is
    reported honestly (signed vs unsigned) rather than failing closed."""
    lines = _physical_lines()
    # Trailing empty string from a final newline is normal; a trailing NON-empty
    # line that won't parse is an interrupted write.
    states: set[str] = set()
    incomplete_tail = False
    if lines:
        # drop the single trailing "" produced by a terminating newline
        body_lines = lines[:-1] if lines and lines[-1] == "" else lines
        for idx, ln in enumerate(body_lines):
            if not ln.strip():
                continue
            try:
                json.loads(ln)
            except (json.JSONDecodeError, TypeError):
                if idx == len(body_lines) - 1:
                    incomplete_tail = True          # recoverable trailing partial
                    states.add(V_INCOMPLETE_TAIL)
                else:
                    states.add(V_CORRUPTED_CHAIN)     # mid-file garbage line

    raw = _read_raw()
    problems: list[str] = []
    signed = unsigned = 0
    prev_hash: str | None = None
    try:
        expected_pub = witness.sub_public_key_hex(LABEL)
    except Exception:
        expected_pub = ""
    observed_runs: set[str] = set()
    for i, e in enumerate(raw):
        schema = str(e.get("schema", ""))
        major = schema.split("/")[-1] if "/" in schema else ""
        if major and major.isdigit() and int(major) > _SUPPORTED_MAJOR:
            states.add(V_UNSUPPORTED_SCHEMA)
            problems.append(f"entry {i}: schema {schema} newer than supported "
                            f"major {_SUPPORTED_MAJOR}")
        core = _core(e.get("seq", i), e.get("prev"), e.get("kind", ""),
                     e.get("at", ""), e.get("body", {}),
                     e.get("event_key", ""))
        if _entry_hash(core) != e.get("entry_hash"):
            problems.append(f"entry {i} (seq {e.get('seq')}): content hash "
                            "mismatch — entry was altered")
            states.add(V_CORRUPTED_CHAIN)
        if e.get("prev") != prev_hash:
            problems.append(f"entry {i} (seq {e.get('seq')}): broken chain — "
                            "an earlier entry was edited, reordered, or removed")
            states.add(V_CORRUPTED_CHAIN)
        prev_hash = e.get("entry_hash")
        if e.get("kind") == OBSERVATION:
            observed_runs.add(str((e.get("body") or {}).get("run_id", "")))
        sig, pub = str(e.get("signature", "")), str(e.get("publicKey", ""))
        if not sig:
            unsigned += 1
        else:
            ok = bool(expected_pub) and pub.lower() == expected_pub.lower() and \
                witness.verify_signature(
                    expected_pub, str(e.get("entry_hash", "")).encode("utf-8"), sig)
            if ok:
                signed += 1
            else:
                problems.append(f"entry {i} (seq {e.get('seq')}): signature invalid")
                states.add(V_CORRUPTED_CHAIN)

    # Referenced-evidence integrity: feedback pointing at a run we never observed.
    dangling = []
    for e in raw:
        if e.get("kind") == FEEDBACK:
            ref = str((e.get("body") or {}).get("ref_run_id", ""))
            if ref and ref not in observed_runs:
                dangling.append(ref)
    if dangling:
        states.add(V_MISSING_EVIDENCE)

    # The chain is "ok" for tamper purposes iff nothing corrupt was found. A
    # recoverable trailing partial, unsigned entries, and dangling feedback are
    # reported but do NOT by themselves mean tampering.
    chain_ok = V_CORRUPTED_CHAIN not in states
    if not states:
        states.add(V_VALID if (signed and not unsigned) else V_UNSIGNED_VALID)
    elif chain_ok and unsigned and not signed:
        states.add(V_UNSIGNED_VALID)
    return {"ok": chain_ok, "entries": len(raw), "signed": signed,
            "unsigned": unsigned, "problems": problems,
            "states": sorted(states),
            "incomplete_trailing_write": incomplete_tail,
            "missing_referenced_runs": sorted(set(dangling))}


# --- export / import -------------------------------------------------------

_EXPORT_HEADER = {"schema": SCHEMA, "kind": "_export_header"}


def export_jsonl(dest: str | Path) -> int:
    """Export the record as documented JSONL (one entry per line, first line a
    header). Explicit and operator-invoked — never automatic, never networked.
    Refuses when export permission is withdrawn (`OLYMPUS_CALIBRATION_EXPORT=0`)."""
    if not export_allowed():
        raise CalibrationError(
            "export is disabled (OLYMPUS_CALIBRATION_EXPORT=0)")
    raw = _read_raw()
    p = Path(dest)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({**_EXPORT_HEADER, "exported_at": _now_iso(),
                             "count": len(raw)}, sort_keys=True) + "\n")
        for e in raw:
            fh.write(json.dumps(e, sort_keys=True) + "\n")
    return len(raw)


def import_jsonl(src: str | Path) -> dict:
    """Read an exported file back (round-trip). Returns {count, entries,
    verified} — verification recomputes hashes over the IMPORTED data, so a
    tampered export is detected without trusting the exporter."""
    p = Path(src)
    if not p.exists():
        return {"count": 0, "entries": [], "verified": False,
                "problems": ["file not found"]}
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(rec, dict) or rec.get("kind") == "_export_header":
            continue
        if not str(rec.get("schema", "")).startswith(SCHEMA_FAMILY + "/"):
            continue
        out.append(rec)
    problems = []
    prev_hash = None
    for i, e in enumerate(out):
        core = _core(e.get("seq", i), e.get("prev"), e.get("kind", ""),
                     e.get("at", ""), e.get("body", {}), e.get("event_key", ""))
        if _entry_hash(core) != e.get("entry_hash"):
            problems.append(f"entry {i}: content hash mismatch")
        if e.get("prev") != prev_hash:
            problems.append(f"entry {i}: broken chain")
        prev_hash = e.get("entry_hash")
    return {"count": len(out), "entries": out, "verified": not problems,
            "problems": problems}


# --- analytics (inferred metrics — computed, never stored) -----------------

def wilson_interval(k: int, n: int, z: float = _Z) -> tuple[float, float]:
    """Wilson score interval — deterministic, stdlib-only (no numpy/scipy, so the
    three-dependency footprint is unchanged). Returns (low, high)."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (centre - margin) / d), min(1.0, (centre + margin) / d))


def _freshness(raw: list[dict], now: float | None = None) -> dict:
    import datetime
    if not raw:
        return {"newest_at": None, "age_seconds": None}
    newest = raw[-1].get("at", "")
    try:
        ts = datetime.datetime.fromisoformat(str(newest)).timestamp()
        age = max(0.0, (now if now is not None else time.time()) - ts)
    except (ValueError, TypeError):
        age = None
    return {"newest_at": newest, "age_seconds": age}


# Fields whose absence counts toward the observation-level missing-evidence rate.
_EVIDENCE_FIELDS = ("provider", "model", "domain", "latency_ms", "tokens_in",
                    "tokens_out", "cost_usd", "result")


def report(now: float | None = None) -> dict:
    """Inferred metrics — computed here, never stored. Keeps COMPLETION,
    SATISFACTION, and VERIFIED QUALITY strictly separate; there is deliberately
    no single 'success' number. Below `_MIN_SAMPLES` a cell reports
    `insufficient_evidence` and omits its rates."""
    obs = entries(OBSERVATION)
    fb = entries(FEEDBACK)
    cmp_ = entries(COMPARISON)

    # Feedback joined to its run. Track BOTH the outcome and its evidence level so
    # analytics never blends an implicit edit with an explicit approval.
    fb_by_run: dict[str, list[dict]] = {}
    for e in fb:
        b = e.get("body", {})
        fb_by_run.setdefault(str(b.get("ref_run_id", "")), []).append(b)

    cells: dict[tuple, dict] = {}
    missing_total = 0
    unclassified_obs = 0
    runs_with_feedback = 0
    for e in obs:
        b = e.get("body", {})
        run = str(b.get("run_id", ""))
        if b.get("domain", UNCLASSIFIED) == UNCLASSIFIED:
            unclassified_obs += 1
        key = (b.get("model_key") or model_key(b.get("provider", ""),
                                               b.get("model", "")),
               b.get("domain", "") or UNCLASSIFIED)
        c = cells.setdefault(key, {
            "n": 0, "completed": 0, "outcomes": {o: 0 for o in OUTCOMES},
            "runs_with_fb": 0, "verified_n": 0, "latency_ms": [], "cost_usd": 0.0,
            "config_ids": set()})
        c["n"] += 1
        c["config_ids"].add(b.get("config_id", ""))
        if b.get("result") == "ok":
            c["completed"] += 1
        if isinstance(b.get("latency_ms"), (int, float)):
            c["latency_ms"].append(float(b["latency_ms"]))
        if isinstance(b.get("cost_usd"), (int, float)):
            c["cost_usd"] += float(b["cost_usd"])
        run_fb = fb_by_run.get(run, [])
        if run_fb:
            c["runs_with_fb"] += 1
            runs_with_feedback += 1
        for fbb in run_fb:
            o = str(fbb.get("outcome", ""))
            if o in c["outcomes"]:
                c["outcomes"][o] += 1
            if fbb.get("evidence_level") == EV_VERIFIED:
                c["verified_n"] += 1
        missing_total += sum(1 for f in _EVIDENCE_FIELDS if f not in b)

    def _rate(k, n):
        return round(k / n, 4) if n else None

    out_cells = []
    for (mk, domain), c in sorted(cells.items(), key=lambda kv: kv[0]):
        n = c["n"]
        oc = c["outcomes"]
        # Explicit-feedback denominator (approve/reject/edit-as-explicit/pref).
        explicit_n = (oc[APPROVED] + oc[APPROVED_AFTER_EDIT] + oc[REJECTED]
                      + oc[PREFERENCE])
        row = {
            "model_key": mk, "domain": domain, "samples": n,
            "min_samples": _MIN_SAMPLES,
            "insufficient_evidence": n < _MIN_SAMPLES,
            "mixed_config": len([x for x in c["config_ids"] if x]) > 1,
            "runs_with_feedback": c["runs_with_fb"],
            "evidence_coverage": _rate(c["runs_with_fb"], n),
            "total_cost_usd": round(c["cost_usd"], 6),
        }
        if c["latency_ms"]:
            row["latency_ms_mean"] = round(
                sum(c["latency_ms"]) / len(c["latency_ms"]), 2)
        if n >= _MIN_SAMPLES:
            lo, hi = wilson_interval(c["completed"], n)
            # LEVEL 1 — completion (did it run). NOT quality.
            row["completion_rate"] = round(c["completed"] / n, 4)
            row["completion_ci95"] = [round(lo, 4), round(hi, 4)]
            # LEVEL 3 — explicit satisfaction (kept separate from completion).
            row["explicit_feedback_samples"] = explicit_n
            if explicit_n:
                row["approval_rate"] = _rate(oc[APPROVED] + oc[APPROVED_AFTER_EDIT],
                                             explicit_n)
                row["rejection_rate"] = _rate(oc[REJECTED], explicit_n)
            # LEVEL 2 — implicit behavioural signals (edit/retry), over all runs.
            row["edit_rate"] = _rate(oc[EDITED] + oc[APPROVED_AFTER_EDIT], n)
            row["retry_rate"] = _rate(oc[RETRIED], n)
            # LEVEL 4 — externally verified outcome.
            row["verified_outcome_rate"] = _rate(c["verified_n"], n)
        out_cells.append(row)

    wins: dict[str, int] = {}
    total_cmp = 0
    for e in cmp_:
        b = e.get("body", {})
        chosen = str(b.get("chosen_model", ""))
        if chosen:
            wins[chosen] = wins.get(chosen, 0) + 1
            total_cmp += 1
    comparisons = {
        "total": total_cmp, "min_samples": _MIN_SAMPLES,
        "insufficient_evidence": total_cmp < _MIN_SAMPLES,
        "wins": wins,
        "win_rate": ({m: round(w / total_cmp, 4) for m, w in wins.items()}
                     if total_cmp >= _MIN_SAMPLES else {}),
    }

    denom = len(obs) * len(_EVIDENCE_FIELDS)
    return {
        "schema": SCHEMA, "taxonomy_version": DOMAIN_TAXONOMY_VERSION,
        "observations": len(obs), "feedback": len(fb), "comparisons": len(cmp_),
        "cells": out_cells,
        "comparison_summary": comparisons,
        "freshness": _freshness(_read_raw(), now=now),
        # Separate, explicitly-named coverage measures (never one 'success').
        "completion_note": ("completion_rate = execution completed; it is NOT a "
                            "quality or satisfaction measure"),
        "unclassified_domain_rate": _rate(unclassified_obs, len(obs)) or 0.0,
        "missing_feedback_rate": (round(1 - runs_with_feedback / len(obs), 4)
                                  if obs else 0.0),
        "missing_evidence_rate": round(missing_total / denom, 4) if denom else 0.0,
        "note": ("Inferred metrics. Completion, satisfaction, and verified "
                 "quality are reported separately and never combined. No "
                 "decision policy consumes this report."),
    }


def rank_models(domain: str = "", metric: str = "completion",
                now: float | None = None) -> dict:
    """Rank configurations on ONE evidence level — or REFUSE, which is the point.
    Refuses when: samples are below policy; evidence types aren't comparable (a
    non-completion metric is missing for some candidate); confidence intervals
    substantially overlap; domain coverage is inadequate (cross-domain without a
    single `domain`); or a model_key mixes configurations without grouping.
    Only completion is rankable here — satisfaction/verified ranking is a
    separate, deliberately unbuilt decision."""
    if metric != "completion":
        return {"ranked": False,
                "reason": (f"metric '{metric}' is not comparable for ranking in "
                           "this prototype — only completion is rankable, and it "
                           "is NOT a quality measure; satisfaction/verified "
                           "ranking is a separate decision")}
    rep = report(now=now)
    rows = [c for c in rep["cells"] if (not domain or c["domain"] == domain)]
    if not rows:
        return {"ranked": False, "reason": "no observations for this scope",
                "candidates": []}
    if not domain and len({c["domain"] for c in rows}) > 1:
        return {"ranked": False,
                "reason": ("inadequate domain coverage: candidates span "
                           f"{len({c['domain'] for c in rows})} domains — pass a "
                           "single `domain` to rank within it, never across"),
                "candidates": sorted({c["model_key"] for c in rows})}
    thin = [c["model_key"] for c in rows if c["insufficient_evidence"]]
    if thin:
        return {"ranked": False,
                "reason": (f"insufficient evidence: {len(thin)} cell(s) below "
                           f"{_MIN_SAMPLES} samples ({', '.join(sorted(set(thin)))})"),
                "candidates": [c["model_key"] for c in rows]}
    mixed = [c["model_key"] for c in rows if c["mixed_config"]]
    if mixed:
        return {"ranked": False,
                "reason": (f"mixed model configurations without grouping: "
                           f"{', '.join(sorted(set(mixed)))} — same name, "
                           "different endpoint/effort; group by config_id first"),
                "candidates": [c["model_key"] for c in rows]}
    ranked = sorted(rows, key=lambda c: c["completion_rate"], reverse=True)
    tied = (len(ranked) > 1
            and ranked[0]["completion_ci95"][0] <= ranked[1]["completion_ci95"][1])
    return {"ranked": True, "separated": not tied, "metric": "completion",
            "note": ("completion intervals overlap — the leader is not "
                     "statistically separated (and completion is not quality)"
                     if tied else
                     "leader's completion interval clears the runner-up "
                     "(completion only — not a quality claim)"),
            "order": [{"model_key": c["model_key"], "domain": c["domain"],
                       "completion_rate": c["completion_rate"],
                       "ci95": c["completion_ci95"], "samples": c["samples"]}
                      for c in ranked]}


def health() -> dict:
    """Collection-health snapshot with NO sensitive content — for the local
    inspection command. Reports totals, chain status, signed/unsigned, the
    unclassified rate, feedback coverage, oldest/newest timestamps, the schema
    versions present, and any corrupted or incomplete entries."""
    raw = _read_raw()
    v = verify()
    obs = [e for e in raw if e.get("kind") == OBSERVATION]
    unclassified = sum(1 for e in obs
                       if (e.get("body") or {}).get("domain", UNCLASSIFIED)
                       == UNCLASSIFIED)
    fb_runs = {str((e.get("body") or {}).get("ref_run_id", ""))
               for e in raw if e.get("kind") == FEEDBACK}
    obs_runs = {str((e.get("body") or {}).get("run_id", "")) for e in obs}
    schemas = sorted({str(e.get("schema", "")) for e in raw})
    return {
        **status(),
        "total_records": len(raw),
        "observations": len(obs),
        "valid_chain": v["ok"],
        "integrity_states": v["states"],
        "signed": v["signed"], "unsigned": v["unsigned"],
        "unclassified_rate": round(unclassified / len(obs), 4) if obs else 0.0,
        "feedback_coverage": (round(len(obs_runs & fb_runs) / len(obs_runs), 4)
                              if obs_runs else 0.0),
        "oldest_at": raw[0].get("at") if raw else None,
        "newest_at": raw[-1].get("at") if raw else None,
        "schema_versions": schemas,
        "incomplete_trailing_write": v["incomplete_trailing_write"],
        "corrupted": V_CORRUPTED_CHAIN in v["states"],
        "missing_referenced_runs": v["missing_referenced_runs"],
    }


def render_report(now: float | None = None) -> str:
    """Human-facing summary. Names completion as completion — never 'success' —
    and refuses to rank on thin evidence."""
    if not enabled():
        return ("Calibration collection is OFF. Enable with OLYMPUS_CALIBRATION=1 "
                "to begin accumulating provider-neutral reliability evidence "
                "(observation only — it changes no behaviour).")
    rep = report(now=now)
    if not rep["observations"]:
        return "Calibration record is empty — no observations yet."
    lines = [f"Calibration record — {rep['observations']} observation(s), "
             f"{rep['feedback']} feedback, {rep['comparisons']} comparison(s)",
             "(completion = did it run; NOT quality or satisfaction)"]
    for c in rep["cells"]:
        if c["insufficient_evidence"]:
            lines.append(f"  {c['model_key']} [{c['domain']}]: "
                         f"{c['samples']}/{c['min_samples']} samples — "
                         "INSUFFICIENT EVIDENCE, no rates reported")
        else:
            lo, hi = c["completion_ci95"]
            seg = (f"  {c['model_key']} [{c['domain']}]: "
                   f"completion {c['completion_rate']:.0%} "
                   f"(95% CI {lo:.0%}–{hi:.0%}, n={c['samples']})")
            if c.get("approval_rate") is not None:
                seg += (f"; approval {c['approval_rate']:.0%} "
                        f"(n={c['explicit_feedback_samples']} explicit)")
            else:
                seg += "; approval: no explicit feedback yet"
            lines.append(seg)
    lines.append(f"  unclassified-domain rate: {rep['unclassified_domain_rate']:.0%}")
    lines.append(f"  missing-feedback rate: {rep['missing_feedback_rate']:.0%}")
    return "\n".join(lines)
