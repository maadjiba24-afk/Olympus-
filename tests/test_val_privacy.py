"""Phase-4 Stage E: privacy & retention validation.

The programme rule is explicit: *raw logs must not automatically become
permanent moat data*. Before this suite, `sweep_dated_files` covered only
`traces` and `usage` — every store the absorption programme added grew without
bound. These tests pin the retention contract for the new evidence ledgers.
"""
from __future__ import annotations

import json
import time

from olympus import config, memory

DAY = 86400


def _write_ledger(sub: str, name: str, rows: list[dict]) -> None:
    d = config.MEMORY_DIR / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _rows(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def test_every_absorption_ledger_is_swept():
    """Each evidence ledger must be covered — a store added later without a
    retention entry is exactly the drift this test exists to catch."""
    now = time.time()
    old, new = now - 90 * DAY, now - 1 * DAY
    for sub, name in memory._EVIDENCE_LEDGERS:
        _write_ledger(sub, name, [{"ts": old, "k": "stale"},
                                  {"ts": new, "k": "fresh"}])

    removed = memory.sweep_evidence(30)

    assert removed == len(memory._EVIDENCE_LEDGERS)
    for sub, name in memory._EVIDENCE_LEDGERS:
        kept = _rows(config.MEMORY_DIR / sub / name)
        assert [r["k"] for r in kept] == ["fresh"], f"{sub}/{name} not pruned"


def test_watchdog_forensics_are_swept_by_age():
    d = config.MEMORY_DIR / "watchdog" / "forensics"
    d.mkdir(parents=True, exist_ok=True)
    stale, fresh = d / "old.json", d / "new.json"
    stale.write_text("{}", encoding="utf-8")
    fresh.write_text("{}", encoding="utf-8")
    import os
    old = time.time() - 90 * DAY
    os.utime(stale, (old, old))

    memory.sweep_evidence(30)

    assert not stale.exists()
    assert fresh.exists()


def test_sweep_never_repairs_a_malformed_row():
    """Reject-never-repair: an unparseable line is KEPT, so a corrupt ledger
    still trips its owner's quarantine path instead of being silently rewritten
    by the retention sweep."""
    _write_ledger("modelgrade", "evidence.jsonl",
                  [{"ts": time.time() - 90 * DAY, "k": "stale"}])
    p = config.MEMORY_DIR / "modelgrade" / "evidence.jsonl"
    p.write_text(p.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")

    memory.sweep_evidence(30)

    text = p.read_text(encoding="utf-8")
    assert "{not json" in text, "a malformed row was silently dropped"
    assert "stale" not in text, "the aged row should still have been pruned"


def test_rows_without_a_timestamp_are_kept():
    """Absent ts means 'cannot judge age' — keeping is the safe direction; a
    sweep must never delete data it cannot date."""
    _write_ledger("routesub", "decisions.jsonl", [{"k": "no-ts"}])
    memory.sweep_evidence(30)
    assert _rows(config.MEMORY_DIR / "routesub" / "decisions.jsonl")


def test_sweep_is_idempotent_and_safe_on_absent_stores():
    assert memory.sweep_evidence(30) == 0        # nothing present at all
    _write_ledger("ingest", "refusals.jsonl", [{"ts": time.time(), "k": "fresh"}])
    assert memory.sweep_evidence(30) == 0        # nothing aged
    assert memory.sweep_evidence(30) == 0        # repeat is a no-op


def test_retention_default_aligns_with_grade_ttl():
    """Pruning modelgrade evidence lowers a card's sample count. That is only
    honest if the pruned rows were already weightless — i.e. retention must not
    be shorter than the grade TTL."""
    from olympus import modelgrade
    assert config.RETAIN_DAYS >= modelgrade.ttl_days(), (
        f"RETAIN_DAYS={config.RETAIN_DAYS} is shorter than the grade TTL "
        f"{modelgrade.ttl_days()}: retention would delete evidence that still "
        "carried weight")
