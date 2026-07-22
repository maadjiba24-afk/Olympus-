"""Semantic skill retrieval — task-scoped top-K skill index, opt-in, replay-safe.

The full per-specialist skill index is injected into every system prompt. Once a
specialist accrues many skills that bloats the prompt; when the library is large
and embeddings are configured, `_skill_index_for` narrows the block to the
skills most relevant to the task. It is opt-in (`OLYMPUS_SEMANTIC_SKILLS`),
engages only above a size threshold, is replay-frozen at the call site, and
always degrades to the full index — no skill ever becomes unreachable.
"""

from olympus import embed, skills, specialists, store
from olympus.specialists import SPECIALISTS


def _vec(text: str) -> list[float]:
    """One-hot fake embedding keyed on a topic word, so only the skill whose
    text shares the query's topic scores > 0 (real cosine over these vectors)."""
    t = text.lower()
    if "pricing" in t:
        return [1.0, 0.0, 0.0]
    if "harden" in t:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


def _stub_embed(monkeypatch):
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "_model", lambda: "fake")
    monkeypatch.setattr(embed, "embed_one", _vec)


def _seed_many(n_filler: int = 25):
    """Two topical skills plus filler, so count() clears the size threshold."""
    skills.create("Pricing Playbook", "value pricing", "anchor on value")
    skills.create("Hardening Guide", "harden systems", "patch fast")
    for i in range(n_filler):
        skills.create(f"Filler {i}", f"misc topic {i}", "generic advice")


# --- scoped_index primitive ---------------------------------------------

def test_scoped_index_ranks_relevant_first(monkeypatch):
    _stub_embed(monkeypatch)
    _seed_many()
    block = skills.scoped_index(None, "help me set pricing", limit=5)
    assert block is not None
    assert "Pricing Playbook" in block          # topical match surfaces
    assert "Hardening Guide" not in block        # off-topic (cosine 0) dropped
    assert "Filler 0" not in block


def test_scoped_index_none_without_embeddings(monkeypatch):
    monkeypatch.setattr(embed, "available", lambda: False)
    _seed_many()
    assert skills.scoped_index(None, "pricing", limit=5) is None


def test_scoped_index_lines_match_index_format(monkeypatch):
    _stub_embed(monkeypatch)
    _seed_many()
    block = skills.scoped_index(None, "harden my server", limit=5)
    # Same `- Name: description` shape as index(), so read_skill still resolves.
    assert block.startswith("- Hardening Guide: harden systems")


# --- _skill_index_for gating --------------------------------------------

def test_full_index_when_flag_off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SEMANTIC_SKILLS", raising=False)
    _stub_embed(monkeypatch)
    _seed_many()
    block = specialists._skill_index_for("chiron", "help me set pricing")
    assert block == skills.index("chiron")       # untouched full index


def test_full_index_when_no_task(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SEMANTIC_SKILLS", "1")
    _stub_embed(monkeypatch)
    _seed_many()
    assert specialists._skill_index_for("chiron", None) == skills.index("chiron")


def test_full_index_when_library_small(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SEMANTIC_SKILLS", "1")
    _stub_embed(monkeypatch)
    skills.create("Pricing Playbook", "value pricing", "anchor on value")  # tiny
    store.reset()
    block = specialists._skill_index_for("chiron", "pricing")
    assert block == skills.index("chiron")


def test_scopes_when_enabled_and_large(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SEMANTIC_SKILLS", "1")
    monkeypatch.setattr(specialists, "_SEMANTIC_SKILLS_MIN", 2)
    _stub_embed(monkeypatch)
    _seed_many()
    store.reset()
    block = specialists._skill_index_for("chiron", "help me set pricing")
    assert "Pricing Playbook" in block
    assert "Hardening Guide" not in block        # scoped away
    assert block != skills.index("chiron")
    store.reset()


def test_degrades_to_full_when_embeddings_absent(monkeypatch):
    # Flag on, library large, but embeddings unavailable → scoped_index None →
    # the full index is returned (no crash, nothing hidden).
    monkeypatch.setenv("OLYMPUS_SEMANTIC_SKILLS", "1")
    monkeypatch.setattr(specialists, "_SEMANTIC_SKILLS_MIN", 2)
    monkeypatch.setattr(embed, "available", lambda: False)
    _seed_many()
    store.reset()
    assert specialists._skill_index_for("chiron", "pricing") == \
        skills.index("chiron")
    store.reset()


# --- system_prompt threads the task -------------------------------------

def test_system_prompt_scopes_to_task(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SEMANTIC_SKILLS", "1")
    monkeypatch.setattr(specialists, "_SEMANTIC_SKILLS_MIN", 2)
    _stub_embed(monkeypatch)
    _seed_many()
    store.reset()
    prompt = SPECIALISTS["chiron"].system_prompt("help me set pricing")
    assert "Pricing Playbook" in prompt
    assert "Hardening Guide" not in prompt        # irrelevant skill trimmed
    store.reset()


def test_system_prompt_no_task_is_full(monkeypatch):
    # The default (task=None) call keeps the full index — existing callers and
    # tests that call system_prompt() see no behavior change.
    monkeypatch.setenv("OLYMPUS_SEMANTIC_SKILLS", "1")
    monkeypatch.setattr(specialists, "_SEMANTIC_SKILLS_MIN", 2)
    _stub_embed(monkeypatch)
    _seed_many()
    prompt = SPECIALISTS["chiron"].system_prompt()
    assert "Pricing Playbook" in prompt and "Hardening Guide" in prompt


# --- replay safety on the per-specialist prompt path --------------------

import types  # noqa: E402

import anthropic  # noqa: E402

from olympus import llm  # noqa: E402


def _msg(text):
    return anthropic.types.Message.model_validate({
        "id": "m", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })


def _client():
    import json as _json

    def stream(**params):
        props = (((params.get("output_config") or {}).get("format") or {})
                 .get("schema") or {}).get("properties", {})

        class _S:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s):
                if "mode" in props:
                    return _msg(_json.dumps(
                        {"mode": "delegate", "direct_reply": None,
                         "specialists": ["chiron"], "brief": "b",
                         "needs_verification": False}))
                if "steps" in props:
                    return _msg(_json.dumps({"steps": [
                        {"id": "s1", "specialist": "chiron", "task": "t",
                         "depends_on": []}]}))
                if "verdict" in props:
                    return _msg(_json.dumps(
                        {"verdict": "approve", "feedback": "",
                         "retry_specialists": []}))
                return _msg("advice")
        return _S()

    msgs = types.SimpleNamespace(stream=stream)
    return types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))


def test_semantic_skills_recorded_run_replays_clean(monkeypatch):
    """A run recorded with semantic skills on replays byte-identically even when
    the replay process has the flag unset — replay_run restores it from meta and
    the task-scoped skill block is frozen."""
    from olympus import config, orchestrator, recall
    monkeypatch.setattr(recall, "context_block", lambda *a, **k: "")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OLYMPUS_SEMANTIC_SKILLS", "1")
    monkeypatch.setattr(specialists, "_SEMANTIC_SKILLS_MIN", 2)
    _stub_embed(monkeypatch)
    _seed_many()
    monkeypatch.setattr(llm, "client", lambda settings=None: _client())
    store.reset()

    bot = orchestrator.Olympus(pool=config.ModelPool.from_env())
    bot.ask("help me set pricing for my product")
    rid = bot.last_run_id

    monkeypatch.delenv("OLYMPUS_SEMANTIC_SKILLS", raising=False)
    monkeypatch.setattr(llm, "client", lambda settings=None: (
        _ for _ in ()).throw(AssertionError("replay must not call the API")))
    original, _fresh, diffs = orchestrator.replay_run(rid)

    assert original["meta"]["semantic_skills"] is True     # recorded in meta
    assert diffs == []                                     # byte-identical replay
    store.reset()
