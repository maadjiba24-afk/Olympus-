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
LABEL = "calibration/v1"          # witness subkey (domain separation)

_LOCK = threading.Lock()

# Entry kinds.
OBSERVATION = "observation"
FEEDBACK = "feedback"
COMPARISON = "comparison"
TOMBSTONE = "tombstone"
_KINDS = (OBSERVATION, FEEDBACK, COMPARISON, TOMBSTONE)

# Outcome vocabulary — the first four are reused VERBATIM from outcomes.py so the
# two stores speak the same language; the last two cover retry/override.
APPROVED = "approved"
APPROVED_AFTER_EDIT = "approved_after_edit"
REJECTED = "rejected"
UNDONE = "undone"
RETRIED = "retried"
OVERRIDDEN = "overridden"
OUTCOMES = (APPROVED, APPROVED_AFTER_EDIT, REJECTED, UNDONE, RETRIED, OVERRIDDEN)

_RESULTS = ("ok", "error", "refused", "timeout")

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


def path() -> Path:
    return Path(config.MEMORY_DIR) / "calibration.jsonl"


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
    """Append one entry. Returns the entry, or None when collection is disabled
    or the event is a duplicate. Never raises on I/O — a telemetry write must not
    break a run."""
    if kind not in _KINDS:
        raise CalibrationError(f"unknown entry kind {kind!r}")
    if not enabled():
        return None
    with _LOCK:
        try:
            seq, prev, seen = _head()
            if event_key and event_key in seen:
                return None                    # idempotent: already ingested
            entry = _seal(seq, prev, kind, at or _now_iso(), body, event_key)
            p = path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
            return entry
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
    body = _clean({
        "run_id": str(run_id), "domain": domain,
        "provider": provider, "model": model,
        "model_key": model_key(provider, model) if provider else "",
        "config_id": config_id(provider, model, base_url, effort) if provider else "",
        "specialist": specialist, "tool": tool,
        "latency_ms": latency_ms, "tokens_in": tokens_in,
        "tokens_out": tokens_out, "cost_usd": cost_usd,
        "result": result, "task_hash": text_ref(task),
        "provenance": _clean({"trace_id": trace_id, "compare_id": compare_id}) or None,
    })
    return _append(OBSERVATION, body,
                   _event_key(OBSERVATION, {"run_id": str(run_id),
                                            "tool": tool, "specialist": specialist}),
                   at=at)


def record_feedback(run_id: str, outcome: str, *, note: str | None = None,
                    at: str | None = None, seq_hint: str = "") -> dict | None:
    """Record a human verdict on an earlier run. This is a NEW entry referencing
    the observation — the observation is never rewritten."""
    if not run_id:
        raise CalibrationError("run_id is required")
    if outcome not in OUTCOMES:
        raise CalibrationError(f"unknown outcome {outcome!r}")
    body = _clean({"ref_run_id": str(run_id), "outcome": outcome,
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

def verify() -> dict:
    """Verify the whole chain: every entry's content address recomputes, `prev`
    linkage is intact, and any signature validates. Reports signed/unsigned
    honestly rather than failing closed (see the module docstring)."""
    raw = _read_raw()
    problems: list[str] = []
    signed = unsigned = 0
    prev_hash: str | None = None
    try:
        expected_pub = witness.sub_public_key_hex(LABEL)
    except Exception:
        expected_pub = ""
    for i, e in enumerate(raw):
        core = _core(e.get("seq", i), e.get("prev"), e.get("kind", ""),
                     e.get("at", ""), e.get("body", {}),
                     e.get("event_key", ""))
        if _entry_hash(core) != e.get("entry_hash"):
            problems.append(f"entry {i} (seq {e.get('seq')}): content hash "
                            "mismatch — entry was altered")
        if e.get("prev") != prev_hash:
            problems.append(f"entry {i} (seq {e.get('seq')}): broken chain — "
                            "an earlier entry was edited, reordered, or removed")
        prev_hash = e.get("entry_hash")
        sig, pub = str(e.get("signature", "")), str(e.get("publicKey", ""))
        if not sig:
            unsigned += 1
            continue
        ok = bool(expected_pub) and pub.lower() == expected_pub.lower() and \
            witness.verify_signature(expected_pub,
                                     str(e.get("entry_hash", "")).encode("utf-8"),
                                     sig)
        if ok:
            signed += 1
        else:
            problems.append(f"entry {i} (seq {e.get('seq')}): signature invalid")
    return {"ok": not problems, "entries": len(raw), "signed": signed,
            "unsigned": unsigned, "problems": problems}


# --- export / import -------------------------------------------------------

_EXPORT_HEADER = {"schema": SCHEMA, "kind": "_export_header"}


def export_jsonl(dest: str | Path) -> int:
    """Export the record as documented JSONL (one entry per line, first line a
    header). Explicit and operator-invoked — never automatic, never networked."""
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


_EVIDENCE_FIELDS = ("provider", "model", "domain", "latency_ms", "tokens_in",
                    "tokens_out", "cost_usd", "result")


def report(now: float | None = None) -> dict:
    """Inferred metrics. NEVER ranks below the minimum-sample bar — an
    under-sampled cell reports `insufficient_evidence` and omits its rates."""
    obs = entries(OBSERVATION)
    fb = entries(FEEDBACK)
    cmp_ = entries(COMPARISON)

    # feedback joined to observations by run_id
    fb_by_run: dict[str, list[str]] = {}
    for e in fb:
        b = e.get("body", {})
        fb_by_run.setdefault(str(b.get("ref_run_id", "")), []).append(
            str(b.get("outcome", "")))

    cells: dict[tuple, dict] = {}
    missing_total = 0
    for e in obs:
        b = e.get("body", {})
        key = (b.get("model_key") or model_key(b.get("provider", ""),
                                               b.get("model", "")),
               b.get("domain", "") or "unclassified")
        c = cells.setdefault(key, {"n": 0, "ok": 0, "outcomes": {o: 0 for o in OUTCOMES},
                                   "feedback_n": 0, "latency_ms": [], "cost_usd": 0.0})
        c["n"] += 1
        if b.get("result") == "ok":
            c["ok"] += 1
        if isinstance(b.get("latency_ms"), (int, float)):
            c["latency_ms"].append(float(b["latency_ms"]))
        if isinstance(b.get("cost_usd"), (int, float)):
            c["cost_usd"] += float(b["cost_usd"])
        for o in fb_by_run.get(str(b.get("run_id", "")), []):
            if o in c["outcomes"]:
                c["outcomes"][o] += 1
                c["feedback_n"] += 1
        missing_total += sum(1 for f in _EVIDENCE_FIELDS if f not in b)

    out_cells = []
    for (mk, domain), c in sorted(cells.items()):
        n, ok = c["n"], c["ok"]
        row = {"model_key": mk, "domain": domain, "samples": n,
               "min_samples": _MIN_SAMPLES,
               "insufficient_evidence": n < _MIN_SAMPLES,
               "total_cost_usd": round(c["cost_usd"], 6)}
        if c["latency_ms"]:
            row["latency_ms_mean"] = round(
                sum(c["latency_ms"]) / len(c["latency_ms"]), 2)
        if n >= _MIN_SAMPLES:
            lo, hi = wilson_interval(ok, n)
            row["success_rate"] = round(ok / n, 4)
            row["success_ci95"] = [round(lo, 4), round(hi, 4)]
            fn = c["feedback_n"]
            if fn:
                row["feedback_samples"] = fn
                row["rates"] = {o: round(c["outcomes"][o] / fn, 4)
                                for o in OUTCOMES}
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
        "schema": SCHEMA,
        "observations": len(obs), "feedback": len(fb), "comparisons": len(cmp_),
        "cells": out_cells,
        "comparison_summary": comparisons,
        "freshness": _freshness(_read_raw(), now=now),
        "missing_evidence_rate": round(missing_total / denom, 4) if denom else 0.0,
        "note": ("Inferred metrics computed from raw observations and verified "
                 "outcomes. No decision policy consumes this report."),
    }


def rank_models(domain: str = "", now: float | None = None) -> dict:
    """Rank models by success rate — or REFUSE. Surfacing insufficient evidence
    is the point; manufacturing a ranking from thin data is the failure mode."""
    rep = report(now=now)
    rows = [c for c in rep["cells"]
            if (not domain or c["domain"] == domain)]
    if not rows:
        return {"ranked": False, "reason": "no observations for this scope",
                "candidates": []}
    thin = [c["model_key"] for c in rows if c["insufficient_evidence"]]
    if thin:
        return {"ranked": False,
                "reason": (f"insufficient evidence: {len(thin)} model/domain "
                           f"cell(s) below {_MIN_SAMPLES} samples "
                           f"({', '.join(sorted(set(thin)))})"),
                "candidates": [c["model_key"] for c in rows]}
    ranked = sorted(rows, key=lambda c: c["success_rate"], reverse=True)
    # Overlapping confidence intervals are not a real difference — say so.
    tied = (len(ranked) > 1
            and ranked[0]["success_ci95"][0] <= ranked[1]["success_ci95"][1])
    return {"ranked": True, "separated": not tied,
            "note": ("confidence intervals overlap — the leader is not "
                     "statistically separated" if tied else
                     "leader's interval clears the runner-up"),
            "order": [{"model_key": c["model_key"], "domain": c["domain"],
                       "success_rate": c["success_rate"],
                       "ci95": c["success_ci95"], "samples": c["samples"]}
                      for c in ranked]}


def render_report(now: float | None = None) -> str:
    """Human-facing summary. Explicitly refuses to rank on thin evidence."""
    if not enabled():
        return ("Calibration collection is OFF. Enable with OLYMPUS_CALIBRATION=1 "
                "to begin accumulating provider-neutral reliability evidence "
                "(observation only — it changes no behaviour).")
    rep = report(now=now)
    if not rep["observations"]:
        return "Calibration record is empty — no observations yet."
    lines = [f"Calibration record — {rep['observations']} observation(s), "
             f"{rep['feedback']} feedback, {rep['comparisons']} comparison(s)"]
    for c in rep["cells"]:
        if c["insufficient_evidence"]:
            lines.append(f"  {c['model_key']} [{c['domain']}]: "
                         f"{c['samples']}/{c['min_samples']} samples — "
                         "INSUFFICIENT EVIDENCE, no rate reported")
        else:
            lo, hi = c["success_ci95"]
            lines.append(f"  {c['model_key']} [{c['domain']}]: "
                         f"success {c['success_rate']:.0%} "
                         f"(95% CI {lo:.0%}–{hi:.0%}, n={c['samples']})")
    cs = rep["comparison_summary"]
    if cs["insufficient_evidence"]:
        lines.append(f"  blind comparisons: {cs['total']}/{cs['min_samples']} — "
                     "INSUFFICIENT EVIDENCE, no win rate reported")
    else:
        for m, r in sorted(cs["win_rate"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  blind win rate {m}: {r:.0%} ({cs['wins'][m]})")
    lines.append(f"  missing-evidence rate: {rep['missing_evidence_rate']:.0%}")
    return "\n".join(lines)
