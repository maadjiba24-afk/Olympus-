"""In-run tool-transcript compaction: bounds a single agent run's context by
shrinking OLD tool_result contents in place. Pure and replay-safe by
construction (never removes/reorders messages, never splits a tool_use from its
tool_result). Off by default.
"""

import pytest

from olympus import config, transcript


def _msgs(big=2000):
    b = "R" * big
    return [
        {"role": "user", "content": "the task"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                           "name": "web_fetch", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": b}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2",
                                           "name": "web_fetch", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2",
                                      "content": b}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t3",
                                           "name": "web_fetch", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3",
                                      "content": b}]},
    ]


def _tool_results(messages):
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out.append(b)
    return out


# --- flags ----------------------------------------------------------------

def test_config_reads_env_live(monkeypatch):
    # The fix: settings are read live (not frozen at import) so replay_run can
    # restore them via env vars and get deterministic replay.
    monkeypatch.setenv("OLYMPUS_INRUN_BUDGET", "5000")
    monkeypatch.setenv("OLYMPUS_INRUN_KEEP_RECENT", "3")
    assert config.inrun_budget() == 5000 and config.inrun_keep_recent() == 3
    monkeypatch.setenv("OLYMPUS_INRUN_COMPACT", "summarize")
    assert transcript.enabled() and transcript.mode() == "summarize"


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("OLYMPUS_INRUN_COMPACT", raising=False)
    assert transcript.enabled() is False
    msgs = _msgs()
    assert transcript.maybe_compact(msgs) is msgs      # exact no-op


@pytest.mark.parametrize("val,on,mode", [
    ("", False, "elide"), ("1", True, "elide"), ("elide", True, "elide"),
    ("summarize", True, "summarize"), ("on", True, "elide"),
])
def test_flag_parsing(monkeypatch, val, on, mode):
    monkeypatch.setenv("OLYMPUS_INRUN_COMPACT", val)
    assert transcript.enabled() is on
    assert transcript.mode() == mode


# --- under budget: identity ----------------------------------------------

def test_under_budget_is_unchanged():
    msgs = _msgs(big=50)
    assert transcript.compact(msgs, budget=10_000) is msgs


# --- over budget: shrink old, keep recent, preserve structure -------------

def test_shrinks_old_keeps_recent_and_preserves_structure():
    msgs = _msgs(big=2000)
    out = transcript.compact(msgs, budget=1000, keep_recent=1)

    # structure invariants — nothing removed or reordered
    assert len(out) == len(msgs)
    assert [m["role"] for m in out] == [m["role"] for m in msgs]
    # tool_use ↔ tool_result pairing intact (same ids, same order)
    assert [b["tool_use_id"] for b in _tool_results(out)] == ["t1", "t2", "t3"]

    trs = _tool_results(out)
    assert len(trs[0]["content"]) < 2000 and "elided" in trs[0]["content"]  # t1
    assert len(trs[1]["content"]) < 2000 and "elided" in trs[1]["content"]  # t2
    assert trs[2]["content"] == "R" * 2000                                  # t3 verbatim


def test_input_is_not_mutated():
    msgs = _msgs(big=2000)
    transcript.compact(msgs, budget=1000, keep_recent=1)
    assert _tool_results(msgs)[0]["content"] == "R" * 2000     # original intact


def test_deterministic_same_input_same_output():
    a = transcript.compact(_msgs(2000), budget=1000, keep_recent=1)
    b = transcript.compact(_msgs(2000), budget=1000, keep_recent=1)
    assert a == b                                              # replay-safe: pure


# --- summarizer path (injected; falls back on error) ----------------------

def test_summarizer_used_for_old_blocks():
    out = transcript.compact(_msgs(2000), budget=1000, keep_recent=1,
                             summarizer=lambda c: "SUMMARY")
    trs = _tool_results(out)
    assert trs[0]["content"] == "SUMMARY" and trs[1]["content"] == "SUMMARY"
    assert trs[2]["content"] == "R" * 2000                    # recent kept verbatim


def test_summarizer_error_falls_back_to_elision():
    def boom(_c):
        raise RuntimeError("no model")
    out = transcript.compact(_msgs(2000), budget=1000, keep_recent=1,
                             summarizer=boom)
    assert "elided" in _tool_results(out)[0]["content"]        # graceful fallback
