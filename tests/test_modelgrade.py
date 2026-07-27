"""Tests for Wave-2 Capability C1 — unified measured model qualification.

Fixture-driven and KEYLESS: every observation is injected, so no model calls
occur. Covers the W2-C1.10 acceptance list — a manual `_CAPABILITIES` claim
never promotes (gate A2), promotion only on sufficient measured evidence,
staleness demotion and retirement (W2-I1.3), conflicts preserved with their
resolution rule named, `modelgate` freeze/quarantine reflected, full-key
completeness on every row (W2-I1.2), rebuild-from-evidence equivalence,
corrupt-artifact handling, flag-off inertness, bounds validation, and the
operator-facing `explain()`.
"""

from __future__ import annotations

import json
import math
import time

import pytest

from olympus import config, modelgate, modelgrade

MEMBER = "anthropic/claude-opus-4-8"          # matches a _CAPABILITIES entry
UNKNOWN = "acme/zzz-nonexistent-model"        # matches nothing in the table
NOW = 1_800_000_000.0                         # fixed clock for every derivation

CELL = modelgrade.cell_of("coding", "en", "short",
                          needs_tools=True, needs_structured=True)


@pytest.fixture(autouse=True)
def grade_enabled(monkeypatch):
    """The store is off by default; most tests exercise it ON."""
    monkeypatch.setenv("OLYMPUS_MODELGRADE", "1")
    modelgrade._STATS["discarded"] = 0
    yield


def seed(*, member: str = MEMBER, cell=CELL, n: int = 25, success=True,
         dimension: str = "quality", source: str = "eval:golden",
         age_days: float = 0.0, now: float = NOW) -> int:
    """Append `n` measured rows, oldest last (one second apart)."""
    provider, model = member.split("/", 1)
    written = 0
    for i in range(n):
        val = success(i) if callable(success) else success
        ok = modelgrade.observe(
            provider=provider, model=model, cell=cell, dimension=dimension,
            success=val, source=source, latency_ms=120, cost_usd=0.002,
            ts=now - age_days * 86400.0 - i)
        assert ok is True
        written += 1
    return written


def _rows() -> list[dict]:
    path = modelgrade.evidence_path()
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


# --- the task cell ---------------------------------------------------------

def test_cell_key_is_short_deterministic_and_round_trips():
    assert CELL.key == "coding|en|short|tools|struct"
    assert modelgrade.cell_key(CELL) == CELL.key
    assert modelgrade.cell_from_key(CELL.key) == CELL
    # Any accepted form normalises to the same key.
    assert modelgrade.cell_of(CELL.key) == CELL
    assert modelgrade.cell_of(CELL) is CELL
    assert modelgrade.cell_of("Coding ", " EN", 1_000, needs_tools=True,
                              needs_structured=True) == CELL
    assert modelgrade.CELL_ANY.key == "any|any|any|notools|nostruct"
    # Separator injection cannot forge a different cell.
    assert modelgrade.cell_of("a|b").key.count("|") == 4


# --- W2-I1.1 / gate A2: a manual claim NEVER promotes ----------------------

def test_capabilities_claim_seeds_prior_but_never_qualifies():
    # No evidence at all — only the hand-typed config._CAPABILITIES table.
    assert config.capability_score("claude-opus-4-8", "coding") >= 9
    card = modelgrade.card(MEMBER, CELL, now=NOW)
    assert card.status == modelgrade.OBSERVING       # prior seeds, at most
    assert card.status != modelgrade.QUALIFIED
    assert modelgrade.qualified(MEMBER, CELL, now=NOW) is False
    assert card.prior["source"] == "config._CAPABILITIES"
    assert card.prior["measured"] is False
    assert card.evidence_rows == 0
    assert "manual claim never promotes" in card.reason


def test_a_perfect_manual_claim_still_never_qualifies(monkeypatch):
    """Even a 10/10 hand-typed entry, with thresholds relaxed to the floor,
    cannot promote: promotion reads MEASURED rows only (gate A2)."""
    monkeypatch.setitem(config._CAPABILITIES, "zzz-nonexistent",
                        {"reasoning": 10, "coding": 10, "general": 10,
                         "verify": 10})
    monkeypatch.setenv("OLYMPUS_GRADE_MIN_N", "1")
    monkeypatch.setenv("OLYMPUS_GRADE_FLOOR", "0.0")
    assert modelgrade.status(UNKNOWN, CELL, now=NOW) == modelgrade.OBSERVING
    assert modelgrade.qualified(UNKNOWN, CELL, now=NOW) is False


def test_no_evidence_and_no_claim_is_untested():
    assert modelgrade.status(UNKNOWN, CELL, now=NOW) == modelgrade.UNTESTED
    assert modelgrade.qualified(UNKNOWN, CELL, now=NOW) is False


# --- promotion on measured evidence ---------------------------------------

def test_promotion_on_sufficient_measured_evidence():
    seed(n=25, success=True)
    card = modelgrade.card(MEMBER, CELL, now=NOW)
    assert card.status == modelgrade.QUALIFIED
    assert modelgrade.qualified(MEMBER, CELL, now=NOW) is True
    quality = card.dimensions["quality"]
    assert quality["n"] == 25
    assert quality["lo"] >= modelgrade.floor_for(CELL)
    assert card.confidence >= modelgrade.floor_for(CELL)
    # Qualification is PER CELL — another cell is untouched by this evidence.
    assert modelgrade.status(MEMBER, modelgrade.CELL_ANY, now=NOW) \
        == modelgrade.OBSERVING


def test_insufficient_n_stays_observing():
    seed(n=5, success=True)
    card = modelgrade.card(MEMBER, CELL, now=NOW)
    assert card.status == modelgrade.OBSERVING
    assert "MIN_N" in card.reason and "n=5" in card.reason


def test_low_wilson_lower_bound_stays_observing():
    # 40 rows, half of them failures: the point rate is 0.5 and the Wilson
    # lower bound is far below the 0.7 floor even with plenty of samples.
    seed(n=40, success=lambda i: bool(i % 2))
    card = modelgrade.card(MEMBER, CELL, now=NOW)
    assert card.dimensions["quality"]["n"] == 40      # n alone is sufficient
    assert card.status == modelgrade.OBSERVING
    assert "wilson lower bound" in card.reason
    assert card.dimensions["quality"]["lo"] < modelgrade.floor_for(CELL)


def test_partial_credit_scores_are_accepted():
    seed(n=25, success=0.95)
    assert modelgrade.status(MEMBER, CELL, now=NOW) == modelgrade.QUALIFIED


def test_measured_deficit_on_another_dimension_restricts():
    seed(n=25, success=True, dimension="quality")
    seed(n=25, success=False, dimension="security", source="aletheia")
    card = modelgrade.card(MEMBER, CELL, now=NOW)
    assert card.status == modelgrade.RESTRICTED
    assert "security" in card.reason
    assert modelgrade.qualified(MEMBER, CELL, now=NOW) is False


# --- W2-I1.3 staleness -----------------------------------------------------

def test_staleness_demotes_qualified_to_observing():
    seed(n=30, success=True, age_days=45)             # TTL default is 30d
    card = modelgrade.card(MEMBER, CELL, now=NOW)
    assert card.status == modelgrade.OBSERVING
    assert "TTL" in card.reason
    # Evidence was not deleted — it lost CONFIDENCE (interval widened, decay).
    assert card.dimensions["quality"]["n"] == 30
    assert card.dimensions["quality"]["effective_n"] < 30
    assert card.confidence < card.dimensions["quality"]["lo"]


def test_silence_past_two_ttl_retires():
    seed(n=30, success=True, age_days=70)             # > 2 x 30d
    card = modelgrade.card(MEMBER, CELL, now=NOW)
    assert card.status == modelgrade.RETIRED
    assert card.confidence == 0.0
    assert modelgrade.qualified(MEMBER, CELL, now=NOW) is False


def test_ttl_is_env_tunable(monkeypatch):
    seed(n=30, success=True, age_days=45)
    monkeypatch.setenv("OLYMPUS_GRADE_TTL_DAYS", "90")
    assert modelgrade.status(MEMBER, CELL, now=NOW) == modelgrade.QUALIFIED


# --- contradictory evidence: preserved, resolution named -------------------

def test_conflicting_evidence_is_preserved_and_resolution_recorded():
    seed(n=6, success=True, source="eval:golden", now=NOW - 10_000)
    seed(n=6, success=False, source="prod:live", now=NOW)

    data = modelgrade.rebuild(now=NOW)
    conflicts = data["conflicts"]
    assert conflicts, "disagreeing observation sets must be recorded"
    src = [c for c in conflicts if c["kind"] == "source_disagreement"]
    assert src, "the two disagreeing sources must be named"
    c = src[0]
    assert c["delta"] > modelgrade.CONFLICT_DELTA
    assert c["resolution"] == modelgrade.CONFLICT_RESOLUTION == \
        "recency_weighted_wilson"
    assert "recency-weighted Wilson" in c["resolution_note"]
    assert {c["a"]["label"], c["b"]["label"]} == {"source:eval:golden",
                                                 "source:prod:live"}

    # NOTHING was overwritten: every original row is still in the ledger.
    rows = _rows()
    assert len(rows) == 12
    assert sorted({r["source"] for r in rows}) == ["eval:golden", "prod:live"]
    assert sum(1 for r in rows if r["success"] == 1.0) == 6
    assert sum(1 for r in rows if r["success"] == 0.0) == 6
    # And the resolution is the one the card actually applied (recency-weighted:
    # the newer, failing set dominates without the older set being deleted).
    card = modelgrade.card(MEMBER, CELL, now=NOW)
    assert card.dimensions["quality"]["rate"] < 0.5
    assert card.status != modelgrade.QUALIFIED
    assert card.conflicts


def test_agreeing_evidence_records_no_conflict():
    seed(n=6, success=True, source="eval:golden", now=NOW - 10_000)
    seed(n=6, success=True, source="prod:live", now=NOW)
    assert modelgrade.rebuild(now=NOW)["conflicts"] == []


# --- modelgate freeze / quarantine reflected -------------------------------

def test_modelgate_freeze_and_quarantine_are_reflected():
    seed(n=25, success=True)
    assert modelgrade.status(MEMBER, CELL, now=NOW) == modelgrade.QUALIFIED

    modelgate._write_freeze(MEMBER, modelgate.FREEZE, "multi-domain drop", "1")
    card = modelgrade.card(MEMBER, CELL, now=NOW)
    assert card.status == modelgrade.FROZEN
    assert "modelgate" in card.reason and "multi-domain drop" in card.reason
    assert modelgrade.qualified(MEMBER, CELL, now=NOW) is False

    # Reversible: the marker is a file (modelgate I-M3), so unfreezing restores.
    assert modelgate.unfreeze(MEMBER) is True
    assert modelgrade.status(MEMBER, CELL, now=NOW) == modelgrade.QUALIFIED

    modelgate._write_freeze(MEMBER, modelgate.QUARANTINE, "error rate 40%", "2")
    assert modelgrade.status(MEMBER, CELL, now=NOW) == modelgrade.QUARANTINED

    modelgate.unfreeze(MEMBER)
    modelgate._write_freeze(MEMBER, modelgate.BLOCK, "verify regression", "3")
    assert modelgrade.status(MEMBER, CELL, now=NOW) == modelgrade.FROZEN


# --- W2-I1.2 full-key completeness ----------------------------------------

def test_every_evidence_row_carries_the_full_key(monkeypatch):
    monkeypatch.setenv("OLYMPUS_COMMIT", "deadbeef1234")
    monkeypatch.setenv("OLYMPUS_POLICY_VERSION", "7")
    assert modelgrade.observe(provider="anthropic", model="claude-opus-4-8",
                              revision="2026-05-01", cell=CELL,
                              dimension="quality", success=True,
                              source="eval:golden", run_id="r1") is True
    seed(n=3, success=True)
    rows = _rows()
    assert len(rows) == 4
    for row in rows:
        for field in ("provider", "model", "revision", "commit", "tmpl_ver",
                      "policy_ver", "eval_set_ver", "cell", "date", "ts",
                      "member", "dimension", "success", "n", "source"):
            assert field in row, f"missing key field: {field}"
        assert row["commit"] == "deadbeef1234"
        assert row["policy_ver"] == "7"
        assert len(row["tmpl_ver"]) == 12          # reused modelgate fingerprint
        assert len(row["eval_set_ver"]) == 12
        assert row["cell"] == CELL.key
        assert row["member"] == MEMBER
        assert row["date"] == time.strftime("%Y-%m-%d", time.gmtime(row["ts"]))
    assert rows[0]["revision"] == "2026-05-01"
    assert rows[0]["extra"]["run_id"] == "r1"      # extras are kept as provenance
    # The derived card carries the same full key.
    key = modelgrade.card(MEMBER, CELL, now=NOW).key
    for field in ("provider", "model", "revision", "commit", "tmpl_ver",
                  "policy_ver", "eval_set_ver", "cell", "date"):
        assert field in key
    assert key["mixed_key"] is True                # two revisions seen, visible


def test_full_key_distinguishes_revisions(monkeypatch):
    seed(n=1)
    monkeypatch.setenv("OLYMPUS_POLICY_VERSION", "2")
    assert modelgrade.observe(provider="anthropic", model="claude-opus-4-8",
                              revision="2026-06-01", cell=CELL,
                              success=True) is True
    a, b = (modelgrade.full_key(r) for r in _rows())
    assert a != b                                   # revision + policy_ver differ
    for row in _rows():
        for part in (row["provider"], row["model"], row["cell"],
                     row["tmpl_ver"], row["eval_set_ver"], row["policy_ver"]):
            assert part in modelgrade.full_key(row)


# --- rebuild(): cards.json is derived, deleting it loses nothing -----------

def test_rebuild_from_evidence_reproduces_cards_exactly():
    seed(n=25, success=True)
    seed(n=8, success=False, cell=modelgrade.CELL_ANY, source="prod:live")
    first = modelgrade.rebuild(now=NOW)
    on_disk = modelgrade.cards_path().read_text(encoding="utf-8")
    assert first["cards"][MEMBER][CELL.key]["status"] == modelgrade.QUALIFIED

    modelgrade.cards_path().unlink()               # delete the derived artifact
    assert not modelgrade.cards_path().exists()

    second = modelgrade.rebuild(now=NOW)
    assert second == first                          # nothing was lost
    assert modelgrade.cards_path().read_text(encoding="utf-8") == on_disk


def test_cards_rebuilds_itself_when_missing():
    seed(n=25, success=True)
    assert not modelgrade.cards_path().exists()
    data = modelgrade.cards(now=NOW)
    assert data["cards"][MEMBER][CELL.key]["status"] == modelgrade.QUALIFIED
    assert modelgrade.cards_path().exists()


# --- corrupt artifacts (reject-never-repair / rebuildable cache) -----------

def test_corrupt_evidence_is_quarantined_and_falls_back_to_untested():
    seed(n=25, success=True)
    assert modelgrade.status(MEMBER, CELL, now=NOW) == modelgrade.QUALIFIED
    with modelgrade.evidence_path().open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")

    card = modelgrade.card(MEMBER, CELL, now=NOW)
    # UNTESTED even though this member has a _CAPABILITIES prior: with no
    # trustworthy ledger there is nothing to observe.
    assert card.status == modelgrade.UNTESTED
    assert card.reason == modelgrade.CORRUPT_REASON
    assert modelgrade.qualified(MEMBER, CELL, now=NOW) is False
    assert not modelgrade.evidence_path().exists()   # moved aside, not repaired
    quarantined = list(modelgrade.evidence_path().parent
                       .glob("evidence.corrupt.*.jsonl"))
    assert len(quarantined) == 1
    body = quarantined[0].read_text(encoding="utf-8")
    assert "{not json at all" in body                # every byte preserved
    assert body.count("\n") == 26


def test_malformed_evidence_row_shape_is_quarantined():
    seed(n=3, success=True)
    with modelgrade.evidence_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(["not", "a", "row"]) + "\n")
    # The read that DETECTS the damage reports it (degraded) and grades nothing.
    data = modelgrade.rebuild(now=NOW)
    assert data["degraded"] is True
    assert data["cards"] == {}
    assert list(modelgrade.evidence_path().parent
                .glob("evidence.corrupt.*.jsonl"))
    # Afterwards the store is simply EMPTY — nothing can be qualified from it.
    assert modelgrade.qualified(MEMBER, CELL, now=NOW) is False
    assert modelgrade.rebuild(now=NOW)["counts"]["evidence_rows"] == 0


def test_corrupt_cards_json_is_rebuilt_from_evidence():
    seed(n=25, success=True)
    modelgrade.rebuild(now=NOW)
    modelgrade.cards_path().write_text("}{ truncated garbage", encoding="utf-8")

    data = modelgrade.cards(now=NOW)
    assert data["cards"][MEMBER][CELL.key]["status"] == modelgrade.QUALIFIED
    assert json.loads(modelgrade.cards_path().read_text(encoding="utf-8"))
    # A card file of the wrong shape is rebuilt too (not trusted blindly).
    modelgrade.cards_path().write_text('{"version": 999}', encoding="utf-8")
    assert modelgrade.cards(now=NOW)["version"] == modelgrade.CARDS_VERSION


def test_status_never_raises_on_a_broken_derivation(monkeypatch):
    seed(n=25, success=True)
    monkeypatch.setattr(modelgrade, "_derive",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert modelgrade.status(MEMBER, CELL, now=NOW) == modelgrade.UNTESTED


# --- flag-off inertness ----------------------------------------------------

def test_flag_off_is_completely_inert(monkeypatch):
    seed(n=25, success=True)
    assert modelgrade.status(MEMBER, CELL, now=NOW) == modelgrade.QUALIFIED
    before = modelgrade.evidence_path().read_text(encoding="utf-8")

    monkeypatch.delenv("OLYMPUS_MODELGRADE", raising=False)
    assert modelgrade.enabled() is False
    assert modelgrade.observe(provider="anthropic", model="claude-opus-4-8",
                              cell=CELL, dimension="quality",
                              success=True) is False
    assert modelgrade.evidence_path().read_text(encoding="utf-8") == before
    assert modelgrade.status(MEMBER, CELL, now=NOW) == modelgrade.UNTESTED
    assert modelgrade.qualified(MEMBER, CELL, now=NOW) is False
    assert modelgrade.rebuild(now=NOW)["cards"] == {}
    assert not modelgrade.cards_path().exists()      # nothing written at all
    assert modelgrade.explain(MEMBER, CELL, now=NOW)["enabled"] is False


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("OLYMPUS_MODELGRADE", raising=False)
    assert modelgrade.enabled() is False
    monkeypatch.setenv("OLYMPUS_MODELGRADE", "off")
    assert modelgrade.enabled() is False


# --- bounds validation (evidence-poisoning defence) ------------------------

@pytest.mark.parametrize("kwargs", [
    {"success": float("nan")},                       # NaN score
    {"success": float("inf")},                       # infinite score
    {"success": -0.5},                               # below range
    {"success": 5.0},                                # above range (1-10 scale)
    {"success": True, "n": -3},                      # negative n
    {"success": True, "n": 0},                       # zero n
    {"success": True, "n": 10_000_000},              # absurd n
    {"success": True, "n": 1.5},                     # fractional n
    {"success": True, "cost_usd": 1e12},             # absurd cost
    {"success": True, "cost_usd": -1.0},             # negative cost
    {"success": True, "cost_usd": float("nan")},     # NaN cost
    {"success": True, "latency_ms": -5},             # negative latency
    {"success": True, "latency_ms": float("inf")},   # infinite latency
    {"success": True, "dimension": "vibes"},         # unknown dimension
    {"success": True, "model": ""},                  # no member identity
    {"success": True, "provider": ""},
    {"success": "not-a-number"},
])
def test_observe_discards_garbage_without_raising(kwargs):
    call = {"provider": "anthropic", "model": "claude-opus-4-8", "cell": CELL,
            "dimension": "quality", "source": "poison"}
    call.update(kwargs)
    assert modelgrade.observe(**call) is False       # discarded, never raised
    assert modelgrade.discarded() == 1
    assert not modelgrade.evidence_path().exists()   # nothing was written
    assert modelgrade.status(MEMBER, CELL, now=NOW) != modelgrade.QUALIFIED


def test_valid_observation_after_garbage_still_records():
    assert modelgrade.observe(provider="anthropic", model="claude-opus-4-8",
                              cell=CELL, success=float("nan")) is False
    seed(n=25, success=True)
    assert modelgrade.discarded() == 1
    assert modelgrade.status(MEMBER, CELL, now=NOW) == modelgrade.QUALIFIED


def test_scores_are_bounded_probabilities_not_self_reports():
    """Every persisted score is a bounded 0..1 rate — a member cannot inflate
    itself past the ceiling, and the row records who produced it."""
    seed(n=3, success=1.0, source="verifier:aletheia")
    for row in _rows():
        assert 0.0 <= row["success"] <= 1.0
        assert math.isfinite(row["success"])
        assert row["source"] == "verifier:aletheia"


# --- explain(): the operator-facing answer ---------------------------------

def test_explain_names_the_blocking_reason():
    seed(n=5, success=True)
    exp = modelgrade.explain(MEMBER, CELL, now=NOW)
    assert exp["status"] == modelgrade.OBSERVING
    assert exp["qualified"] is False
    assert "MIN_N" in exp["blocking"]
    assert exp["evidence_rows"] == 5
    assert exp["thresholds"]["min_n"] == modelgrade.DEFAULT_MIN_N
    assert exp["thresholds"]["floor"] == modelgrade.DEFAULT_FLOOR
    assert exp["thresholds"]["ttl_days"] == modelgrade.DEFAULT_TTL_DAYS
    assert exp["dimensions"]["quality"]["n"] == 5
    assert exp["prior"]["measured"] is False
    assert exp["key"]["provider"] == "anthropic"


def test_explain_blocking_reason_per_state(monkeypatch):
    seed(n=40, success=lambda i: bool(i % 2))
    assert "wilson lower bound" in modelgrade.explain(MEMBER, CELL,
                                                      now=NOW)["blocking"]

    modelgate._write_freeze(MEMBER, modelgate.QUARANTINE, "error rate", "9")
    exp = modelgrade.explain(MEMBER, CELL, now=NOW)
    assert exp["status"] == modelgrade.QUARANTINED
    assert "modelgate" in exp["blocking"]
    modelgate.unfreeze(MEMBER)

    monkeypatch.setenv("OLYMPUS_GRADE_FLOOR", "0.2")
    exp = modelgrade.explain(MEMBER, CELL, now=NOW)
    assert exp["status"] == modelgrade.QUALIFIED
    assert exp["blocking"] == ""                      # nothing is blocking


def test_explain_on_untested_member_names_the_absence_of_evidence():
    exp = modelgrade.explain(UNKNOWN, CELL, now=NOW)
    assert exp["status"] == modelgrade.UNTESTED
    assert "no evidence" in exp["blocking"]


# --- per-cell floors + W2-I1.4 read-only w.r.t. other stores ---------------

def test_per_cell_floor_override(monkeypatch):
    monkeypatch.setenv("OLYMPUS_GRADE_FLOOR", "0.5")
    monkeypatch.setenv("OLYMPUS_GRADE_FLOOR_CODING", "0.95")
    assert modelgrade.floor_for(CELL) == 0.95
    assert modelgrade.floor_for(modelgrade.CELL_ANY) == 0.5
    seed(n=25, success=lambda i: i % 10 != 0)         # ~90% success
    # ~0.9 clears the global 0.5 floor but not the coding cell's 0.95 floor.
    assert modelgrade.status(MEMBER, CELL, now=NOW) == modelgrade.OBSERVING
    assert modelgrade.status(MEMBER, modelgrade.CELL_ANY, now=NOW) \
        == modelgrade.OBSERVING                       # prior only, no evidence


def test_store_writes_only_under_its_own_directory():
    seed(n=25, success=True)
    modelgrade.rebuild(now=NOW)
    modelgrade.explain(MEMBER, CELL, now=NOW)
    # It READS modelgate's freeze markers but never writes its ledger/baseline.
    assert not (config.MEMORY_DIR / "modelgate" / "results.jsonl").exists()
    assert not (config.MEMORY_DIR / "modelgate" / "baseline.json").exists()
    assert not (config.MEMORY_DIR / "modelgate" / "freeze.json").exists()
    assert sorted(p.name for p in modelgrade.evidence_path().parent.iterdir()) \
        == ["cards.json", "evidence.jsonl"]


def test_members_lists_every_member_cell_pair():
    seed(n=2, success=True)
    seed(n=2, success=True, cell=modelgrade.CELL_ANY)
    assert modelgrade.members() == [(MEMBER, modelgrade.CELL_ANY.key),
                                    (MEMBER, CELL.key)]


def test_format_report_mentions_state_and_flag(monkeypatch):
    seed(n=25, success=True)
    report = modelgrade.format_report(now=NOW)
    assert "QUALIFIED" in report and MEMBER in report
    monkeypatch.delenv("OLYMPUS_MODELGRADE", raising=False)
    assert "OFF" in modelgrade.format_report(now=NOW)
