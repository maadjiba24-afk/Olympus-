"""Tests for token-based conversation state compaction (state diffs)."""

import pytest

from olympus import orchestrator, config, backend, memory


@pytest.fixture(autouse=True)
def small_budget(monkeypatch, tmp_path):
    # tiny budget so tests don't need huge strings; clean memory dir. Setting
    # the explicit-override flag makes history_token_budget() honor the absolute
    # value instead of the model-context fraction.
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(config, "HISTORY_BUDGET_IS_EXPLICIT", True)
    monkeypatch.setattr(config, "HISTORY_TOKEN_BUDGET", 500)   # ~2000 chars
    monkeypatch.setattr(config, "HISTORY_KEEP_TURNS", 4)


def _bot():
    return orchestrator.Olympus(user="u")


def _turns(n, size):
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": f"turn{i} " + "x" * size})
    return out


def test_short_history_not_compacted(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(backend, "complete_text",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    bot = _bot()
    bot.history = _turns(4, 20)          # tiny, well under budget
    bot._maybe_compact()
    assert called["n"] == 0              # no summarization call
    assert len(bot.history) == 4         # untouched


def test_over_budget_compacts_to_state_plus_tail(monkeypatch):
    monkeypatch.setattr(backend, "complete_text",
                        lambda *a, **k: "STATE: user runs a bakery")
    bot = _bot()
    bot.history = _turns(12, 400)        # ~12*400 chars -> over the 2000-char budget
    bot._maybe_compact()
    # becomes [state, ack] + last KEEP_TURNS verbatim
    assert len(bot.history) == 2 + config.HISTORY_KEEP_TURNS
    assert "Conversation state" in bot.history[0]["content"]
    assert "STATE: user runs a bakery" in bot.history[0]["content"]
    assert bot.history[-1]["content"].startswith("turn11")


def test_compaction_falls_back_to_tail_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(backend, "complete_text", boom)
    bot = _bot()
    bot.history = _turns(12, 400)
    bot._maybe_compact()
    assert len(bot.history) == config.HISTORY_KEEP_TURNS   # truncated tail
    assert bot.history[0]["content"].startswith("turn8")


def test_folding_is_incremental(monkeypatch):
    seen = {}
    monkeypatch.setattr(backend, "complete_text",
                        lambda settings, sysmsg, msgs, **k:
                        seen.update(input=msgs[0]["content"]) or "new state")
    bot = _bot()
    # a prior state block plus fresh turns, over budget
    bot.history = ([{"role": "user", "content": "[Conversation state] PRIOR FACTS"}]
                   + _turns(10, 400))
    bot._maybe_compact()
    # the prior state block was part of the slice fed to the summarizer (folds in)
    assert "PRIOR FACTS" in seen["input"]


def test_huge_single_turn_within_tail_is_left_alone(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(backend, "complete_text",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    bot = _bot()
    bot.history = _turns(3, 5000)        # over budget but fewer turns than the tail
    bot._maybe_compact()
    assert called["n"] == 0              # can't fold what's all in the verbatim tail
    assert len(bot.history) == 3


def test_token_estimate_tracks_size():
    small = orchestrator.Olympus._estimate_tokens([{"role": "user", "content": "x" * 40}])
    big = orchestrator.Olympus._estimate_tokens([{"role": "user", "content": "x" * 4000}])
    assert small < big and big >= 900
