"""Self-discovery (olympus/discovery.py) — acquire knowledge + propose features."""

import pytest

from olympus import discovery, security, tools


# --- gap ledger -------------------------------------------------------------

def test_note_gap_dedups_and_counts():
    a = discovery.note_gap("knowledge", "How QUIC works")
    b = discovery.note_gap("knowledge", "  how quic works  ")   # same → dedup
    assert a["id"] == b["id"]
    assert b["hits"] == 2
    assert len(discovery.open_gaps(kind="knowledge")) == 1


def test_note_gap_requires_topic():
    with pytest.raises(ValueError):
        discovery.note_gap("knowledge", "   ")


def test_open_gaps_ranked_by_hits():
    discovery.note_gap("knowledge", "rare topic")
    for _ in range(3):
        discovery.note_gap("knowledge", "hot topic")
    top = discovery.open_gaps(kind="knowledge")[0]
    assert top["topic"] == "hot topic"                          # most-hit first


def test_kinds_are_separated():
    discovery.note_gap("knowledge", "topic x")
    discovery.note_gap("capability", "topic x")                 # same topic, diff kind
    assert len(discovery.open_gaps(kind="knowledge")) == 1
    assert len(discovery.open_gaps(kind="capability")) == 1


# --- capability-gap derivation from friction --------------------------------

def test_derive_capability_gaps_from_outcomes(monkeypatch):
    from olympus import outcomes
    monkeypatch.setattr(outcomes, "insights", lambda user: [
        {"ref": "send_email", "friction": 0.8, "message": "declined 80%"}])
    noted = discovery.derive_capability_gaps()
    assert any("send_email" in g["topic"] for g in noted)
    assert any(g["kind"] == "capability" for g in discovery.open_gaps(kind="capability"))


# --- closing gaps: knowledge acquisition ------------------------------------

def test_acquire_knowledge_degrades_without_result():
    discovery.note_gap("knowledge", "obscure topic")
    gap = discovery.open_gaps(kind="knowledge")[0]
    # A degraded research report ("no usable evidence") must NOT be learned.
    out = discovery.acquire_knowledge(
        gap, runner=lambda q: "# Research\n\nNo usable evidence could be collected.")
    assert "queued" in out
    assert len(discovery.open_gaps(kind="knowledge")) == 1      # stays open


def test_acquire_knowledge_learns_substantive(monkeypatch):
    from olympus import wiki
    saved = {}
    monkeypatch.setattr(wiki, "upsert",
                        lambda user, title, content, **k: saved.setdefault("slug", "quic") or "quic")
    discovery.note_gap("knowledge", "QUIC congestion control")
    gap = discovery.open_gaps(kind="knowledge")[0]
    report = "QUIC congestion control resembles TCP CUBIC/BBR. " * 8
    out = discovery.acquire_knowledge(gap, runner=lambda q: report)
    assert "learned" in out
    assert saved.get("slug") == "quic"
    assert discovery.open_gaps(kind="knowledge") == []          # resolved


def test_short_report_is_not_learned():
    discovery.note_gap("knowledge", "tiny")
    gap = discovery.open_gaps(kind="knowledge")[0]
    assert "queued" in discovery.acquire_knowledge(gap, runner=lambda q: "too short")
    assert len(discovery.open_gaps(kind="knowledge")) == 1


# --- closing gaps: feature proposal -----------------------------------------

def test_propose_feature_files_a_proposal():
    discovery.note_gap("capability", "export findings to PDF", evidence="asked twice")
    gap = discovery.open_gaps(kind="capability")[0]
    out = discovery.propose_feature(gap)
    assert "proposed feature" in out
    assert discovery.open_gaps(kind="capability") == []         # marked proposed
    # It landed in the upgrades store the digest/CLI surface.
    from olympus import memory
    items = memory.load("upgrades") if hasattr(memory, "load") else []
    # Best-effort: the proposal file exists under the user's upgrades dir.
    from olympus import config
    up = config.MEMORY_DIR / "upgrades"
    assert up.exists() and any(up.iterdir())


# --- the cycle --------------------------------------------------------------

def test_run_is_bounded_and_reports(monkeypatch):
    from olympus import outcomes
    monkeypatch.setattr(outcomes, "insights", lambda user: [])
    for i in range(5):
        discovery.note_gap("knowledge", f"topic {i}")
    r = discovery.run(max_knowledge=2, max_features=2,
                      runner=lambda q: "queued still")           # degraded → stays open
    assert isinstance(r["learned"], list) and len(r["learned"]) == 2   # bounded
    assert r["open_knowledge"] == 5                              # none actually learned


def test_run_is_replay_inert(monkeypatch):
    monkeypatch.setenv("OLYMPUS_REPLAY", "1")
    discovery.note_gap("knowledge", "x")
    r = discovery.run()
    assert r.get("skipped") == "replay"
    assert discovery.enabled() is False


def test_enabled_is_opt_in(monkeypatch):
    monkeypatch.delenv("OLYMPUS_DISCOVERY", raising=False)
    assert discovery.enabled() is False
    monkeypatch.setenv("OLYMPUS_DISCOVERY", "1")
    assert discovery.enabled() is True
    monkeypatch.setenv("OLYMPUS_REPLAY", "1")
    assert discovery.enabled() is False                         # replay wins


# --- the agent-facing tool --------------------------------------------------

def test_note_knowledge_gap_tool():
    assert "note_knowledge_gap" in tools.HANDLERS
    assert "note_knowledge_gap" in security.TRUSTED_TOOLS       # own-state write
    assert security.should_wrap("note_knowledge_gap") is False
    out = tools.HANDLERS["note_knowledge_gap"]("how OAuth PKCE works", "user asked")
    assert "Noted knowledge gap" in out
    assert len(discovery.open_gaps(kind="knowledge")) == 1


# --- hands-free auto-signal: UNVERIFIED answer -> knowledge gap --------------

class _FakeTrace:
    def __init__(self):
        self.events = []

    def event(self, name, **kw):
        self.events.append((name, kw))


def _orch():
    # Uninitialized instance: _signal_knowledge_gap touches no other attrs.
    from olympus.orchestrator import Olympus
    return object.__new__(Olympus)


def test_unverified_answer_signals_knowledge_gap(monkeypatch):
    monkeypatch.setenv("OLYMPUS_DISCOVERY", "1")
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    tr = _FakeTrace()
    _orch()._signal_knowledge_gap(
        "How does QUIC congestion control work?",
        ["QUIC uses BBR by default"], "reject_after_rework", tr)
    gaps = discovery.open_gaps(kind="knowledge")
    assert any("quic congestion" in g["topic"] for g in gaps)
    g = next(g for g in gaps if "quic congestion" in g["topic"])
    assert g["source"] == "aletheia:reject_after_rework"
    assert "BBR" in g["evidence"]
    assert ("discovery.gap_noted", {"reason": "reject_after_rework"}) in tr.events


def test_auto_signal_is_opt_in(monkeypatch):
    monkeypatch.delenv("OLYMPUS_DISCOVERY", raising=False)   # discovery OFF
    tr = _FakeTrace()
    _orch()._signal_knowledge_gap("some topic", ["a claim"], "direct_reject", tr)
    assert discovery.open_gaps(kind="knowledge") == []       # nothing recorded
    assert tr.events == []


def test_auto_signal_is_replay_inert(monkeypatch):
    monkeypatch.setenv("OLYMPUS_DISCOVERY", "1")
    monkeypatch.setenv("OLYMPUS_REPLAY", "1")                 # replay wins
    _orch()._signal_knowledge_gap("some topic", ["a claim"], "direct_reject",
                                  _FakeTrace())
    assert discovery.open_gaps(kind="knowledge") == []


def test_auto_signal_never_raises(monkeypatch):
    # Even if discovery blows up, the answer path must not see an exception.
    monkeypatch.setenv("OLYMPUS_DISCOVERY", "1")
    monkeypatch.setattr(discovery, "note_gap",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _orch()._signal_knowledge_gap("t", ["c"], "reject_after_rework", _FakeTrace())
    # No exception propagated — the swallow held.
