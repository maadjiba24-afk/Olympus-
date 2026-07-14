"""Phase 3 (constrained evolution) governance: ACE + sleep-time ARE the
self-evolution layer, so their behaviour is (1) exposed as structured logs,
(2) destructive changes are proposed as DIFFS with nothing auto-applying, and
(3) every world-affecting path routes through the approval gate."""

import pytest

from olympus import ace, config, evolve, sleeptime, usermem


# --- (1) structured logs ---------------------------------------------------

def test_log_event_roundtrip_and_filter():
    evolve.log_event("ace", "compaction", {"version": 3, "bullets": 12,
                                           "pinned": 4})
    evolve.log_event("sleeptime", "cycle", {"proposed": 1, "committed": 0,
                                            "clean": True})
    ace_events = evolve.events("ace")
    assert ace_events and ace_events[-1]["kind"] == "compaction"
    assert ace_events[-1]["bullets"] == 12          # structured, not a string
    st_events = evolve.events("sleeptime")
    assert st_events[-1]["clean"] is True
    assert all(e["feature"] == "ace" for e in ace_events)


def test_log_event_is_bounded_and_never_raises():
    for i in range(evolve._MAX_EVENTS + 50):
        evolve.log_event("flood", "tick", {"i": i})
    assert len(evolve.events("flood", limit=evolve._MAX_EVENTS)) <= evolve._MAX_EVENTS
    # junk fields are stringified, not fatal
    evolve.log_event("junk", "kind", {"obj": object(), 123: [1, 2]})
    assert evolve.events("junk")


def test_ace_compaction_emits_structured_delta_counters(monkeypatch, tmp_path):
    from olympus import orchestrator, recall
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(config, "HISTORY_BUDGET_IS_EXPLICIT", True)
    monkeypatch.setattr(config, "HISTORY_TOKEN_BUDGET", 300)
    monkeypatch.setattr(config, "HISTORY_KEEP_TURNS", 2)
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setattr(recall, "flush_slice", lambda *a, **k: {})
    monkeypatch.setattr(
        ace, "default_generator",
        lambda s: lambda text, pb: {"add": [{"content": "a durable fact",
                                             "section": "facts"}]})
    bot = orchestrator.Olympus(user="u")
    bot.conversation_id = "p3-log"
    for i in range(6):
        bot.history.append({"role": "user", "content": "x" * 300})
        bot.history.append({"role": "assistant", "content": "y" * 300})
        bot._maybe_compact()
    events = evolve.events("ace")
    assert events and events[-1]["kind"] == "compaction"
    for key in ("version", "bullets", "pinned", "added", "helpful", "harmful"):
        assert key in events[-1]


def test_sleeptime_cycle_emits_structured_metrics(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SLEEPTIME", "1")
    u = "p3-st"
    usermem.add_memory(u, type="project", confidence=0.7, provenance=["e1"],
                       content="Apollo uses Postgres and Redis for storage")
    usermem.add_memory(u, type="project", confidence=0.7, provenance=["e2"],
                       content="Apollo storage is Postgres and a Redis cache")
    monkeypatch.setattr(sleeptime, "default_generator",
                        lambda s: lambda srcs: "Apollo uses Postgres and Redis.")
    monkeypatch.setattr(sleeptime, "default_verifier",
                        lambda s: lambda srcs, rw: {"supported": True})
    sleeptime.run()
    events = evolve.events("sleeptime")
    assert events and events[-1]["kind"] == "cycle"
    for key in ("proposed", "committed", "rejected", "clean", "clean_cycles",
                "graduated", "autoapply"):
        assert key in events[-1]


# --- (2) destructive changes are proposed as diffs; nothing auto-applies ---

def _seed_pair(u):
    m1 = usermem.add_memory(u, type="project", confidence=0.7, provenance=["e1"],
                            content="Apollo uses Postgres and Redis for storage")
    m2 = usermem.add_memory(u, type="project", confidence=0.7, provenance=["e2"],
                            content="Apollo storage is Postgres and a Redis cache")
    return m1, m2


def test_proposal_renders_as_unified_diff():
    u = "p3-diff"
    _seed_pair(u)
    sleeptime.refine_user(
        u, generator=lambda s: "Apollo uses Postgres and Redis.",
        verifier=lambda s, r: {"supported": True}, auto_apply=False)
    diff = sleeptime.render_diff(sleeptime.proposals(u)[-1])
    assert diff.startswith("# proposal ")
    assert "verified" in diff
    assert "--- memories/" in diff and "+++ proposal/" in diff
    assert "-Apollo uses Postgres and Redis for storage" in diff
    assert "+Apollo uses Postgres and Redis." in diff


def test_ungraduated_loop_never_auto_applies_even_with_env(monkeypatch):
    """The double gate: OLYMPUS_SLEEPTIME_AUTOAPPLY=1 alone must NOT commit —
    graduation (10 clean supervised cycles) is also required."""
    monkeypatch.setenv("OLYMPUS_SLEEPTIME_AUTOAPPLY", "1")
    assert sleeptime.state()["clean_cycles"] == 0     # fresh store: ungraduated
    u = "p3-gate1"
    m1, m2 = _seed_pair(u)
    s = sleeptime.refine_user(
        u, generator=lambda x: "Apollo uses Postgres and Redis.",
        verifier=lambda x, r: {"supported": True})    # auto_apply from gates
    assert s["proposed"] == 1 and s["committed"] == 0
    assert usermem.get_memory(u, m1["id"])["status"] == usermem.ACTIVE
    assert usermem.get_memory(u, m2["id"])["status"] == usermem.ACTIVE


def test_graduated_loop_still_needs_explicit_autoapply(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SLEEPTIME_AUTOAPPLY", raising=False)
    monkeypatch.setattr(config, "SLEEPTIME_GRADUATION", 2)
    for _ in range(2):
        sleeptime._record_cycle(clean=True, proposed=0, committed=0)
    assert sleeptime.graduated()
    u = "p3-gate2"
    m1, _ = _seed_pair(u)
    s = sleeptime.refine_user(
        u, generator=lambda x: "Apollo uses Postgres and Redis.",
        verifier=lambda x, r: {"supported": True})
    assert s["committed"] == 0                        # graduated but not opted in
    assert usermem.get_memory(u, m1["id"])["status"] == usermem.ACTIVE


# --- (3) everything routes through the approval gate ------------------------

def test_gate_inventory_holds():
    """One consolidated assertion of the gate-adjacent invariants:
    payment mandates can never auto-run; tree search only PREPARES actions;
    security tunables are tighten-only; ACE pinned facts are prune-proof."""
    from olympus import actions, mandate, treesearch
    # mandates: FINANCIAL_LEGAL is pinned at 'always ask a human'
    assert mandate.can_auto_execute() is False
    assert actions._min_level_to_auto(mandate.risk_class()) == 99
    # tree search: side effects are withheld, never applied (engine constant)
    assert treesearch.SIDE_EFFECTFUL not in treesearch.SAFE
    # evolve: every security-relevant tunable is registered tighten-only
    for key, t in evolve._TUNABLES.items():
        if t.feature == "operator":
            assert t.tighten_only, f"{key} must be tighten-only"
    # ACE: a pinned fact survives prune pressure (the structural guarantee)
    pb = ace.curate(ace.Playbook(), {"add": [
        {"content": "pinned durable fact", "section": "facts"}]})
    for i in range(50):
        pb = ace.curate(pb, {"add": [
            {"content": f"noise {i}", "section": "notes"}]})
    assert any(b.pinned and b.content == "pinned durable fact"
               for b in pb.bullets)
