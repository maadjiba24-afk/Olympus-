"""Web build-proposals — discoveries auto-drafted into human-actionable plans."""

import pytest

from olympus import config, webproposals


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    return tmp_path


_DISCOVERIES = [
    {"key": "interactive:3", "kind": "proposal",
     "title": "3 domains need interaction", "detail": "a.com, b.com, c.com"},
    {"key": "flaky:z:2", "kind": "proposal",
     "title": "2 domains fetch poorly", "detail": "d.com, e.com"},
    {"key": "jsonld:5", "kind": "capability",       # not a proposal
     "title": "5 expose JSON-LD", "detail": "f.com"},
]


def test_drafts_only_proposal_kind_with_full_frame(store):
    drafted = webproposals.draft_from_discoveries(_DISCOVERIES)
    assert len(drafted) == 2                          # capability kind excluded
    p = next(d for d in drafted if d.key == "interactive:3")
    # a proposal is a full engineering artifact, not a one-liner
    assert p.motivation and p.proposal and p.safety and p.acceptance
    assert "a.com" in p.evidence                      # cites the real evidence
    assert p.status == "drafted"


def test_drafting_is_idempotent_by_key(store):
    webproposals.draft_from_discoveries(_DISCOVERIES)
    again = webproposals.draft_from_discoveries(_DISCOVERIES)
    assert again == []                                # deduped by discovery key
    assert len(webproposals.all_()) == 2


def test_deterministic_id_survives_reload(store):
    d1 = webproposals.draft_from_discoveries(_DISCOVERIES)
    ids = sorted(p.id for p in d1)
    # id is a hash of the key — same key, same id, no clock/randomness
    assert webproposals._pid("interactive:3") in ids


def test_operator_decision_lifecycle(store):
    webproposals.draft_from_discoveries(_DISCOVERIES)
    p = webproposals.pending()[0]
    assert webproposals.set_status(p.id, "accepted") is True
    assert webproposals.get(p.id).status == "accepted"
    assert p.id not in {q.id for q in webproposals.pending()}
    # unknown status / unknown id are rejected cleanly
    assert webproposals.set_status(p.id, "bogus") is False
    assert webproposals.set_status("nope", "declined") is False
    s = webproposals.stats()
    assert s["total"] == 2 and s["accepted"] == 1


def test_inert_under_replay(store, monkeypatch):
    monkeypatch.setenv("OLYMPUS_REPLAY", "1")
    assert webproposals.draft_from_discoveries(_DISCOVERIES) == []
    assert webproposals.all_() == []


def test_corrupt_store_is_quarantined_not_wiped(store):
    webproposals.draft_from_discoveries(_DISCOVERIES)
    p = webproposals._path()
    p.write_text("{ this is not json", encoding="utf-8")
    assert webproposals.all_() == []                  # reads empty, doesn't crash
    assert p.with_name(p.name + ".corrupt").exists()  # old data preserved


def test_unknown_kind_gets_generic_frame(store):
    drafted = webproposals.draft_from_discoveries(
        [{"key": "novel:1", "kind": "proposal", "title": "x", "detail": "y.com"}])
    assert len(drafted) == 1
    assert drafted[0].title and drafted[0].proposal   # still well-formed
