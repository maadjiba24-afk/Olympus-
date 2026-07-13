"""Pre-compaction memory flush: durable facts leave the transcript as typed
memory BEFORE compaction/reset folds the turns away."""

import pytest

from olympus import backend, config, orchestrator, recall


@pytest.fixture(autouse=True)
def small_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(config, "HISTORY_BUDGET_IS_EXPLICIT", True)
    monkeypatch.setattr(config, "HISTORY_TOKEN_BUDGET", 500)
    monkeypatch.setattr(config, "HISTORY_KEEP_TURNS", 4)


def _turns(n, size):
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"turn{i} " + "x" * size} for i in range(n)]


def test_compaction_flushes_old_slice_first(monkeypatch):
    flushed = {}

    def fake_flush(user, text, settings):
        flushed["user"], flushed["text"] = user, text
        return {"created": 1}

    monkeypatch.setattr(recall, "flush_slice", fake_flush)
    monkeypatch.setattr(backend, "complete_text", lambda *a, **k: "STATE")
    bot = orchestrator.Olympus(user="u")
    bot.history = _turns(12, 400)
    bot._maybe_compact()
    assert flushed["user"] == "u"
    assert "turn0" in flushed["text"]              # the folded slice
    assert "turn11" not in flushed["text"]         # the verbatim tail is kept


def test_flush_failure_never_blocks_compaction(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("extractor down")

    monkeypatch.setattr(recall, "flush_slice", boom)
    monkeypatch.setattr(backend, "complete_text", lambda *a, **k: "STATE")
    bot = orchestrator.Olympus(user="u")
    bot.history = _turns(12, 400)
    bot._maybe_compact()                            # must not raise
    assert "Conversation state" in bot.history[0]["content"]


def test_reset_flushes_whole_transcript(monkeypatch):
    flushed = {}
    monkeypatch.setattr(recall, "flush_slice",
                        lambda u, t, s: flushed.setdefault("text", t))
    monkeypatch.setattr(backend, "complete_text", lambda *a, **k: "SUMMARY")
    bot = orchestrator.Olympus(user="u")
    bot.history = _turns(6, 50)
    bot.reset()
    assert "turn0" in flushed["text"] and "turn5" in flushed["text"]


def test_flush_slice_runs_extractor_and_gates(monkeypatch):
    calls = {}

    def fake_json(settings, system, messages, schema, effort="high"):
        calls["convo"] = messages[0]["content"]
        return {"memories": [{"type": "project", "content": "Atlas ships Q3",
                              "confidence": 0.9}], "relationships": []}

    monkeypatch.setattr(recall.backend, "complete_json", fake_json)
    out = recall.flush_slice("u-flush", "user: we decided Atlas ships Q3\n"
                             "assistant: noted" + " pad" * 20,
                             config.Settings(provider="anthropic",
                                             model="m", api_key="k"))
    assert "about to be compacted" in calls["convo"]
    assert sum(out.values()) >= 1                  # something was written/gated


def test_flush_slice_respects_memory_kill_switch(monkeypatch):
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    called = {"n": 0}
    monkeypatch.setattr(recall.backend, "complete_json",
                        lambda *a, **k: called.__setitem__("n", 1))
    out = recall.flush_slice("u", "long text " * 50,
                             config.Settings(provider="anthropic",
                                             model="m", api_key="k"))
    assert out == {} and called["n"] == 0


def test_flush_slice_skips_trivial_text():
    out = recall.flush_slice("u", "hi",
                             config.Settings(provider="anthropic",
                                             model="m", api_key="k"))
    assert out == {}
