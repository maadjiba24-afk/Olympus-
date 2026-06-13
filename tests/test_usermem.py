"""Tests for durable user memory: store, write policy, and retrieval."""

import time

import pytest

from olympus import usermem, recall, config, store


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    store.reset()
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_CONFIDENCE_FLOOR", 0.6)
    yield
    store.reset()


class _S:  # a stand-in Settings; the model call is always mocked
    provider = "anthropic"; model = "x"; api_key = None; base_url = None


# --- storage layer -------------------------------------------------------

def test_add_and_active():
    m = usermem.add_memory("u", type="preference", content="likes concise",
                           confidence=0.9)
    assert usermem.get_memory("u", m["id"])["content"] == "likes concise"
    assert len(usermem.active_memories("u")) == 1


def test_reinforce_raises_confidence():
    m = usermem.add_memory("u", type="preference", content="x", confidence=0.7)
    usermem.reinforce("u", m["id"])
    got = usermem.get_memory("u", m["id"])
    assert got["confidence"] > 0.7 and got["use_count"] == 1


def test_supersede_keeps_history():
    old = usermem.add_memory("u", type="project", content="uses MySQL",
                             confidence=0.8, key="db")
    new = usermem.add_memory("u", type="project", content="uses Postgres",
                             confidence=0.9, key="db")
    usermem.supersede("u", old["id"], new)
    active = usermem.active_memories("u")
    assert len(active) == 1 and active[0]["content"] == "uses Postgres"
    assert usermem.get_memory("u", old["id"])["status"] == usermem.SUPERSEDED


def test_tombstone_forgets():
    m = usermem.add_memory("u", type="episodic", content="x", confidence=0.9)
    assert usermem.tombstone("u", m["id"])
    assert usermem.active_memories("u") == []


def test_confidence_decays_with_age():
    now = time.time()
    fresh = {"confidence": 1.0, "half_life_days": 30,
             "created_at": now, "last_used_at": now}
    old = {"confidence": 1.0, "half_life_days": 30,
           "created_at": now, "last_used_at": now - 30 * 86400}
    assert usermem.effective_confidence(fresh, now) > 0.99
    assert abs(usermem.effective_confidence(old, now) - 0.5) < 0.02


def test_candidate_queue():
    c = usermem.add_candidate("u", {"type": "preference", "content": "x"})
    assert len(usermem.candidates("u")) == 1
    assert usermem.pop_candidate("u", c["id"])["content"] == "x"
    assert usermem.candidates("u") == []


def test_memory_is_per_user():
    usermem.add_memory("alice", type="identity", content="a", confidence=0.9)
    assert usermem.active_memories("bob") == []


# --- write policy (the gate) --------------------------------------------

def test_gate_discards_low_confidence():
    assert recall._gate("u", {"type": "preference", "content": "maybe",
                              "confidence": 0.3}, "e1") == "discarded_low_confidence"
    assert usermem.active_memories("u") == []


def test_gate_commits_confident_normal():
    assert recall._gate("u", {"type": "preference", "content": "wants bullet points",
                              "confidence": 0.9, "sensitivity": "normal"},
                        "e1") == "committed"
    assert len(usermem.active_memories("u")) == 1


def test_gate_holds_sensitive_for_approval():
    assert recall._gate("u", {"type": "identity", "content": "has condition X",
                              "confidence": 0.95, "sensitivity": "high"},
                        "e1") == "held_sensitive"
    assert usermem.active_memories("u") == []        # NOT auto-committed
    assert len(usermem.candidates("u")) == 1


def test_gate_reinforces_duplicate():
    usermem.add_memory("u", type="preference", content="prefers concise replies",
                       confidence=0.7)
    action = recall._gate("u", {"type": "preference",
                                "content": "prefers concise replies",
                                "confidence": 0.8, "sensitivity": "normal"}, "e2")
    assert action == "reinforced"
    assert len(usermem.active_memories("u")) == 1     # no duplicate row


def test_gate_supersedes_conflict_when_confident():
    usermem.add_memory("u", type="project", content="DB is MySQL",
                       confidence=0.7, key="db")
    action = recall._gate("u", {"type": "project", "content": "DB is Postgres",
                                "confidence": 0.9, "sensitivity": "normal",
                                "key": "db"}, "e3")
    assert action == "superseded"
    active = usermem.active_memories("u")
    assert len(active) == 1 and "Postgres" in active[0]["content"]


# --- extractor end-to-end (model mocked) --------------------------------

def test_extract_commits_from_model(monkeypatch):
    monkeypatch.setattr(recall.backend, "complete_json", lambda *a, **k: {
        "memories": [{"type": "preference", "content": "keep replies short",
                      "confidence": 0.9, "sensitivity": "normal"}]})
    summary = recall.extract("u", "Please always keep your replies short and "
                             "to the point, no preamble.", "Got it.", _S())
    assert summary.get("committed") == 1
    assert any("short" in m["content"] for m in usermem.active_memories("u"))


def test_extract_skips_trivial_turns(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(recall.backend, "complete_json",
                        lambda *a, **k: called.__setitem__("n", called["n"]+1) or {"memories": []})
    recall.extract("u", "ok", "sure", _S())          # under MEMORY_MIN_CHARS
    assert called["n"] == 0


def test_extract_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(recall.backend, "complete_json", boom)
    assert recall.extract("u", "a long enough message about my business goals",
                          "reply", _S()) == {}


# --- retrieval -----------------------------------------------------------

def test_retrieve_relevance_floor():
    usermem.add_memory("u", type="project", content="building a bakery app called Crumb",
                       confidence=0.9, importance=0.7)
    assert recall.retrieve("u", "tell me about the bakery app")     # overlaps
    assert recall.retrieve("u", "what is the weather today") == []  # no overlap → floor


def test_context_block_formats_and_is_empty_when_irrelevant():
    usermem.add_memory("u", type="relationship", content="Sarah is the cofounder",
                       confidence=0.9, importance=0.6)
    block = recall.context_block("u", "draft a note to Sarah")
    assert "Sarah is the cofounder" in block and "remember about this user" in block
    assert recall.context_block("u", "unrelated quantum physics question") == ""


def test_retrieve_respects_budget():
    for i in range(20):
        usermem.add_memory("u", type="project",
                           content=f"project alpha note number {i} " + "detail " * 10,
                           confidence=0.9, importance=0.8)
    hits = recall.retrieve("u", "project alpha note detail", budget_tokens=120)
    used = sum(len(m["content"]) // 4 + 4 for m in hits)
    assert used <= 120 and len(hits) >= 1


# --- pipeline injection --------------------------------------------------

def test_memory_injected_into_router(monkeypatch):
    from olympus import orchestrator, backend
    usermem.add_memory("memu", type="project",
                       content="is launching a coffee subscription called DripDrop",
                       confidence=0.9, importance=0.8)
    captured = {}
    monkeypatch.setattr(backend, "complete_json",
                        lambda settings, system, messages, schema, effort="high":
                        captured.update(system=system) or
                        {"mode": "direct", "direct_reply": "ok", "specialists": [],
                         "brief": None, "needs_verification": False})
    bot = orchestrator.Olympus(user="memu")
    bot._route("how's the coffee subscription going?")
    assert "DripDrop" in captured["system"]
