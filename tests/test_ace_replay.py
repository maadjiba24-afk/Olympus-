"""ACE integration: a 50+ turn session compacts delta-only through the
orchestrator with ZERO loss of pinned facts.

The Generator is driven deterministically (no network): it parses the slice for
lines shaped `FACT: ...` (→ pinned facts) and `NOTE: ...` (→ prunable notes),
so the whole run is reproducible — the "replay" property is that the same input
stream produces the same playbook every time, and no pinned fact is ever lost
regardless of how many compaction cycles fire.
"""

import pytest

from olympus import ace, config, orchestrator, recall


@pytest.fixture(autouse=True)
def _tiny(monkeypatch, tmp_path):
    # Force frequent compaction and an aggressive non-pinned cap so pruning is
    # under real pressure across the 50-turn run.
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(config, "HISTORY_BUDGET_IS_EXPLICIT", True)
    monkeypatch.setattr(config, "HISTORY_TOKEN_BUDGET", 400)
    monkeypatch.setattr(config, "HISTORY_KEEP_TURNS", 4)
    monkeypatch.setattr(ace, "_MAX_BULLETS", 5)     # tiny non-pinned cap
    # Don't let the async typed-memory flush make a real model call.
    monkeypatch.setattr(recall, "flush_slice", lambda *a, **k: {})
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)


def _deterministic_generator(slice_text, pb):
    add = []
    for line in slice_text.splitlines():
        line = line.split(":", 1)[-1].strip() if ":" in line else line
        if "FACT#" in line:
            fact = line[line.index("FACT#"):]
            add.append({"content": fact, "section": "facts"})
        elif "NOTE#" in line:
            note = line[line.index("NOTE#"):]
            add.append({"content": note, "section": "notes"})
    return {"add": add}


def _install(monkeypatch):
    # Route the orchestrator's ACE Generator to the deterministic one.
    monkeypatch.setattr(ace, "default_generator",
                        lambda settings: _deterministic_generator)


def test_50_turn_session_keeps_every_pinned_fact(monkeypatch):
    _install(monkeypatch)
    bot = orchestrator.Olympus(user="u")
    bot.conversation_id = "sess-1"

    facts_stated = []
    for i in range(50):
        # Every turn asserts a durable FACT and some throwaway NOTEs, each padded
        # so the running history crosses the compaction budget repeatedly.
        fact = f"FACT#{i} the durable value for topic {i} is locked"
        facts_stated.append(fact)
        pad = "x" * 200
        bot.history.append({"role": "user",
                            "content": f"{fact} {pad}"})
        bot.history.append({"role": "assistant",
                            "content": f"NOTE#{i} acknowledged {pad}"})
        bot._maybe_compact()

    # After 50 turns of repeated delta compaction, EVERY stated fact must still
    # be present in the retained context — either aged into the pinned playbook
    # or still verbatim in the recent tail. Neither path can lose it.
    pb = ace.load("sess-1")
    state = ace.render(pb)
    tail = "\n".join(str(m.get("content", "")) for m in bot.history)
    context = state + "\n" + tail
    for fact in facts_stated:
        assert fact in context, f"lost pinned fact: {fact}"

    # And the point of the test: the vast majority genuinely went THROUGH
    # compaction into the pinned playbook (not merely sitting in the tail),
    # proving delta compaction preserved them rather than a lucky short history.
    reached = sum(1 for f in facts_stated
                  if any(f in b.content for b in pb.bullets if b.pinned))
    assert reached >= 44
    assert 'source="playbook"' in state


def test_compaction_actually_fired_and_stayed_delta(monkeypatch):
    _install(monkeypatch)
    bot = orchestrator.Olympus(user="u")
    bot.conversation_id = "sess-2"
    for i in range(50):
        pad = "x" * 200
        bot.history.append({"role": "user",
                            "content": f"FACT#{i} value {i} {pad}"})
        bot.history.append({"role": "assistant", "content": f"ok {pad}"})
        bot._maybe_compact()
    pb = ace.load("sess-2")
    # 50 delta merges (version bumps only happen when compaction fired), and the
    # history stays bounded to a state block + verbatim tail.
    assert pb.version >= 40
    assert len(bot.history) <= 2 + config.HISTORY_KEEP_TURNS
    assert bot.history[0]["content"].startswith("[Conversation state")


def test_pinned_facts_survive_a_reload_midway(monkeypatch):
    """Persistence check: the playbook is reloaded from disk each compaction, so
    facts pinned early survive process-restart-shaped reloads."""
    _install(monkeypatch)
    bot = orchestrator.Olympus(user="u")
    bot.conversation_id = "sess-3"
    for i in range(10):
        pad = "x" * 200
        bot.history.append({"role": "user",
                            "content": f"FACT#{i} early value {i} {pad}"})
        bot.history.append({"role": "assistant", "content": f"ok {pad}"})
        bot._maybe_compact()
    pinned_before = {b.content for b in ace.load("sess-3").bullets if b.pinned}
    assert pinned_before                       # some facts aged into the playbook

    # Simulate a fresh process: new orchestrator, same conversation id. It reads
    # the playbook from disk and keeps evolving it with 20 more noise turns.
    bot2 = orchestrator.Olympus(user="u")
    bot2.conversation_id = "sess-3"
    for i in range(10, 30):
        pad = "x" * 200
        bot2.history.append({"role": "user",
                             "content": f"NOTE#{i} filler {i} {pad}"})
        bot2.history.append({"role": "assistant", "content": f"ok {pad}"})
        bot2._maybe_compact()
    pinned_after = {b.content for b in ace.load("sess-3").bullets if b.pinned}
    # Every fact pinned before the reload survives the reload AND 20 further
    # compaction cycles under prune pressure — no durable fact is ever lost.
    assert pinned_before <= pinned_after
