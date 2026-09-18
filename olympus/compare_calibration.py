"""Acknowledged, idempotent publication of the comparison outbox.

This narrow bridge validates the complete bounded existing chain and appends
the run observations and their comparison as one atomic file publication under
calibration's existing lock. It does not repair the general M15 reader/writer,
activate collection, or manufacture verified/genuine quality evidence.
"""
from __future__ import annotations

import json

from . import calibration as cal, compare_execution as execution
from . import compare_state as state, note_evidence as files
from . import owner_evidence as ev, proclock, witness

MAX_CHAIN = 16 * 1024 * 1024
MAX_ENTRIES = 100000


def _read():
    raw = ev.read_bytes(cal.path(), MAX_CHAIN, "comparison calibration") or b""
    if raw and not raw.endswith(b"\n"):
        raise ValueError("incomplete calibration tail")
    lines = raw.splitlines()
    if len(lines) > MAX_ENTRIES:
        raise ValueError("calibration entry bound")
    entries, seen, previous = [], set(), None
    expected_public = None
    for seq, line in enumerate(lines):
        row = ev.decode(line, "comparison calibration", MAX_CHAIN)
        ev.fields(row, ("schema", "seq", "prev", "kind", "at", "event_key", "body",
                        "entry_hash", "publicKey", "signature"))
        if row["schema"] != cal.SCHEMA or type(row["seq"]) is not int or row["seq"] != seq:
            raise ValueError("calibration schema/sequence mismatch")
        if row["kind"] not in cal._KINDS or row["prev"] != previous or not isinstance(row["body"], dict):
            raise ValueError("calibration chain mismatch")
        ev.text(row["at"], 64)
        ev.text(row["event_key"], 512)
        ev.text(row["signature"], 256, empty=True)
        ev.text(row["publicKey"], 128, empty=True)
        if row["event_key"] in seen:
            raise ValueError("duplicate calibration event")
        seen.add(row["event_key"])
        core = cal._core(seq, previous, row["kind"], row["at"], row["body"], row["event_key"])
        if row["entry_hash"] != cal._entry_hash(core):
            raise ValueError("calibration digest mismatch")
        if bool(row["signature"]) != bool(row["publicKey"]):
            raise ValueError("incomplete calibration signature")
        if row["signature"]:
            if expected_public is None:
                expected_public = witness.sub_public_key_hex(cal.LABEL)
            if row["publicKey"] != expected_public or not witness.verify_signature(
                    expected_public, row["entry_hash"].encode(), row["signature"]):
                raise ValueError("calibration signature mismatch")
        previous = row["entry_hash"]
        entries.append(row)
    return raw, entries


def _events(uid, rec):
    owner_hash = execution.digest(uid)
    comparison_id = "compare-v2:" + owner_hash + ":" + rec["id"]
    common = {"compare_id": comparison_id, "owner_sha256": owner_hash,
              "source_kind": "blind_compare", "verified": False,
              "eligible_for_promotion": False}
    events = []
    for member in rec["members"]:
        body = {**common, "run_id": member["run_id"], "config_id": member["identity"],
                "provider": member["configured"]["provider"], "model": member["configured"]["model"],
                "model_key": state.model(member), "execution_state": member["state"],
                "result": "ok" if member["state"] == "answered" else "error",
                "evidence_level": cal.EV_COMPLETION,
                "domain": cal.UNCLASSIFIED, "provenance": member["provenance"],
                "configured": member["configured"]}
        events.append((cal.OBSERVATION, comparison_id + ":" + member["run_id"], body))
    winner = state.winner(rec)
    body = {**common, "blind": True, "chosen_model": winner["model"] if winner else None,
            "chosen_identity": winner["identity"] if winner else None,
            "models": [state.model(m) for m in rec["members"]],
            "identities": [m["identity"] for m in rec["members"]],
            "run_ids": [m["run_id"] for m in rec["members"]], "choice": rec["choice"]}
    events.append((cal.COMPARISON, comparison_id, body))
    return events


def link(uid, rec):
    """Return the exact comparison entry hash, or leave its outbox pending."""
    if not cal.enabled():
        return None  # No chain/key/lock reads or writes while collection is off.
    try:
        files._check_dir(cal.path().parent / "locks")
        with proclock.lock("calibration", timeout=cal.lock_timeout()):
            raw, entries = _read()
            by_key = {r["event_key"]: r for r in entries}
            tombstones = {r["body"].get("ref_event_key") for r in entries if r["kind"] == cal.TOMBSTONE}
            previous = entries[-1]["entry_hash"] if entries else None
            seq, additions = len(entries), []
            for kind, key, body in _events(uid, rec):
                if key in tombstones:
                    raise ValueError("comparison event tombstoned")
                existing = by_key.get(key)
                if existing is not None:
                    if existing["kind"] != kind or existing["body"] != body or existing["at"] != rec["decision_at"]:
                        raise ValueError("comparison event conflicts")
                    receipt = existing["entry_hash"]
                    continue
                entry = cal._seal(seq, previous, kind, rec["decision_at"], body, key)
                addition = json.dumps(entry, sort_keys=True, allow_nan=False).encode() + b"\n"
                additions.append(addition)
                seq += 1
                previous = receipt = entry["entry_hash"]
            combined = raw + b"".join(additions)
            if len(combined) > MAX_CHAIN or seq > MAX_ENTRIES:
                raise ValueError("calibration capacity reached")
            if additions:
                files.publish(cal.path(), combined)
            else:
                # A prior successful replace may have lost its directory-sync
                # acknowledgement. Retry that barrier before acknowledging.
                files.sync_dir(cal.path().parent)
            return receipt.removeprefix("sha256:")
    except (ev.OwnerEvidenceStateError, witness.WitnessError, OSError, TimeoutError,
            ValueError, TypeError, KeyError, OverflowError, RecursionError):
        # The owner snapshot remains pending; this never means a provider call
        # failed or a comparison vote was rolled back. CLI/API expose pending.
        return None
