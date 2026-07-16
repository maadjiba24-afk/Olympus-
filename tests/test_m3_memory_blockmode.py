"""Milestone 3.3 — reflection engine Target B (memory).

Two capabilities:
  * relgraph VALIDITY INTERVALS — relations carry a [valid_from, valid_to) span,
    so a "true at T" query returns only what held at that time;
  * Aletheia BLOCK-MODE — once the loop has GRADUATED (10 clean supervised
    cycles), a consolidation rewrite whose confidence is below the floor is
    REJECTED and QUARANTINED (before graduation the block is inert). Consolidation
    stays the only rewriter of existing memories; trust labels are preserved.

Done bar: a test demonstrates a BLOCKED rewrite.
"""

import pytest

from olympus import (behavioral_contracts as abc, config, relgraph, sleeptime,
                     store, usermem, witness)

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "m3.3-memory-seed")
    store.reset()


# --- relgraph validity intervals: "true at T" -----------------------------

def test_relation_valid_interval_true_at_query():
    # Sarah worked at Acme 2019–2021, then at Beta from 2022 (open-ended).
    relgraph.add_edge("u", "Sarah", "works_at", "Acme",
                      valid_from=2019.0, valid_to=2021.0)
    relgraph.add_edge("u", "Sarah", "works_at", "Beta", valid_from=2022.0)

    at_2020 = {e["rel"] + "->" + _dst(e) for e in relgraph.relations_at("u", "Sarah", 2020.0)}
    at_2023 = {e["rel"] + "->" + _dst(e) for e in relgraph.relations_at("u", "Sarah", 2023.0)}
    assert at_2020 == {"works_at->Acme"}          # only Acme held in 2020
    assert at_2023 == {"works_at->Beta"}          # only Beta holds in 2023
    # No time filter → both facts.
    assert len(relgraph.relations_at("u", "Sarah")) == 2


def _dst(edge):
    ns = relgraph.nodes("u")
    return next(n["label"] for n in ns if n["id"] == edge["dst"])


def test_open_interval_and_close_relation():
    relgraph.add_edge("u", "Sarah", "lives_in", "Paris", valid_from=2018.0)
    assert relgraph.relations_at("u", "Sarah", 2030.0)      # open-ended: still true
    assert relgraph.close_relation("u", "Sarah", "lives_in", "Paris", valid_to=2025.0)
    assert relgraph.relations_at("u", "Sarah", 2020.0)      # true before the close
    assert not relgraph.relations_at("u", "Sarah", 2030.0)  # false after


def test_neighbors_respects_as_of():
    acme = relgraph.add_edge("u", "Sarah", "works_at", "Acme",
                             valid_from=2019.0, valid_to=2021.0)
    labels_2020 = [n["label"] for _, n in
                   relgraph.neighbors("u", acme["src"], as_of=2020.0)]
    labels_2025 = [n["label"] for _, n in
                   relgraph.neighbors("u", acme["src"], as_of=2025.0)]
    assert "Acme" in labels_2020 and "Acme" not in labels_2025


def test_timeless_edges_unaffected():
    # An edge with no interval is always "true at T" — backward compatible.
    relgraph.add_edge("u", "Sarah", "knows", "Tom")
    assert len(relgraph.relations_at("u", "Sarah", 1234.0)) == 1
    assert len(relgraph.relations_at("u", "Sarah")) == 1


def test_same_relation_distinct_spans_coexist():
    relgraph.add_edge("u", "Sarah", "member_of", "Club", valid_from=2010.0,
                      valid_to=2012.0)
    relgraph.add_edge("u", "Sarah", "member_of", "Club", valid_from=2020.0)
    assert len(relgraph.edges("u")) == 2            # two distinct temporal facts


# --- Aletheia block-mode: the blocked rewrite -----------------------------

def _graduate():
    st = sleeptime.state()
    st["clean_cycles"] = config.SLEEPTIME_GRADUATION
    sleeptime._save(sleeptime._STATE_NS, "state", st)


def test_block_mode_inert_before_graduation():
    # Pre-graduation: block-mode does not reject even a low-confidence rewrite
    # (Aletheia annotates only). The contract passes.
    assert not sleeptime.block_mode_active()
    abc.enforce("memory.rewrite", {                 # must not raise
        "source_provenance": [], "new_provenance": [],
        "source_sensitivities": ["normal"], "new_sensitivity": "normal",
        "source_type": "note", "new_type": "note", "verify_supported": True,
        "unsupported_claims": [], "block_mode_active": False,
        "rewrite_confidence": 0.1, "confidence_threshold": 0.5})


def test_block_mode_active_after_graduation():
    _graduate()
    assert sleeptime.block_mode_active()
    with pytest.raises(abc.ContractViolation) as ei:
        abc.enforce("memory.rewrite", {
            "source_provenance": [], "new_provenance": [],
            "source_sensitivities": ["normal"], "new_sensitivity": "normal",
            "source_type": "note", "new_type": "note", "verify_supported": True,
            "unsupported_claims": [], "block_mode_active": True,
            "rewrite_confidence": 0.2, "confidence_threshold": 0.5})
    assert ei.value.recovery == abc.BLOCK


@_signing
def test_low_confidence_rewrite_is_blocked_and_quarantined():
    # THE Done bar: a graduated loop, an Aletheia-supported but LOW-confidence
    # consolidation → blocked (not committed), quarantined, streak reset.
    user = "alice"
    a = usermem.add_memory(user, type="project", content="Alpha release ships during the first quarter.",
                           confidence=0.9, provenance=["conversation"])
    b = usermem.add_memory(user, type="project", content="Alpha release ships in the first quarter.",
                           confidence=0.9, provenance=["conversation"])
    _graduate()

    def gen(grp):
        return "Alpha ships in Q1."

    def low_conf_verify(grp, rewrite):
        return {"supported": True, "unsupported_claims": [], "confidence": 0.2}

    before = usermem.active_memories(user)
    summary = sleeptime.refine_user(user, generator=gen, verifier=low_conf_verify,
                                    auto_apply=True)
    # Blocked: nothing committed, the sources are untouched (only rewriter is
    # consolidation, and it was refused).
    assert summary["committed"] == 0 and summary["clean"] is False
    assert {m["id"] for m in usermem.active_memories(user)} \
        == {m["id"] for m in before}
    # Quarantined for review, with the reason and the rejected rewrite.
    q = sleeptime.quarantined(user)
    assert len(q) == 1 and q[0]["confidence"] == 0.2
    assert "confidence" in q[0]["reason"].lower()
    # And recorded as a signed, verifiable delta-substrate record.
    from olympus import deltas, memory
    assert deltas.verify_history(f"quarantine:{memory.safe_id(user)}")["ok"]


@_signing
def test_high_confidence_rewrite_still_commits_when_graduated():
    user = "bob"
    usermem.add_memory(user, type="project", content="Product launch happens on May third downtown.",
                       confidence=0.9, provenance=["conversation"])
    usermem.add_memory(user, type="project", content="Product launch happens on May third as scheduled.",
                       confidence=0.9, provenance=["conversation"])
    _graduate()

    summary = sleeptime.refine_user(
        user, generator=lambda grp: "Launch is May 3.",
        verifier=lambda grp, rw: {"supported": True, "unsupported_claims": [],
                                  "confidence": 0.95}, auto_apply=True)
    assert summary["committed"] == 1
    assert not sleeptime.quarantined(user)


def test_production_verifier_schema_emits_confidence():
    # The guardrail must be LIVE in production: the real Aletheia verifier's
    # schema must require a numeric confidence, else block-mode is inert (every
    # supported rewrite would default to 1.0 and never be blocked).
    props = sleeptime._VERIFY_SCHEMA["properties"]
    assert "confidence" in props and props["confidence"]["type"] == "number"
    assert "confidence" in sleeptime._VERIFY_SCHEMA["required"]


def test_derived_confidence_keeps_supported_rewrites_unblocked():
    # A verifier that returns no numeric confidence: a SUPPORTED rewrite is
    # treated as confident (1.0), so graduated auto-apply is not spuriously
    # blocked by the new clause.
    user = "carol"
    usermem.add_memory(user, type="preference", content="The grey cat named Milo sleeps often here.",
                       confidence=0.9, provenance=["conversation"])
    usermem.add_memory(user, type="preference", content="The grey cat named Milo sleeps often there.",
                       confidence=0.9, provenance=["conversation"])
    _graduate()
    summary = sleeptime.refine_user(
        user, generator=lambda grp: "The cat Milo.",
        verifier=lambda grp, rw: {"supported": True, "unsupported_claims": []},
        auto_apply=True)
    assert summary["committed"] == 1
