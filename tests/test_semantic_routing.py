"""Semantic routing — relevance-ordered roster, opt-in, replay-safe."""

from olympus import embed, specialists, store


def _fake_embed(marker):
    """Embed so the description containing `marker` is the clear match."""
    return lambda texts: [[1.0, 0.0] if marker in t else [0.0, 1.0]
                          for t in texts]


# --- gating: no-op by default -------------------------------------------

def test_off_by_default_returns_plain_roster(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SEMANTIC_ROUTING", raising=False)
    assert specialists.semantic_roster("anything") == specialists.roster()


def test_small_roster_stays_plain(monkeypatch):
    # flag on, but the default 13-specialist roster is below the threshold
    monkeypatch.setenv("OLYMPUS_SEMANTIC_ROUTING", "1")
    monkeypatch.setattr(embed, "available", lambda: True)
    assert specialists.semantic_roster("write code") == specialists.roster()


def test_no_embeddings_stays_plain(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SEMANTIC_ROUTING", "1")
    monkeypatch.setattr(specialists, "_SEMANTIC_ROSTER_MIN", 2)
    monkeypatch.setattr(embed, "available", lambda: False)
    assert specialists.semantic_roster("write code") == specialists.roster()


# --- the reorder ---------------------------------------------------------

def test_reorders_most_relevant_first(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SEMANTIC_ROUTING", "1")
    monkeypatch.setattr(specialists, "_SEMANTIC_ROSTER_MIN", 2)
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "embed", _fake_embed("Hephaestus"))
    monkeypatch.setattr(embed, "embed_one", lambda q: [1.0, 0.0])
    store.reset()
    out = specialists.semantic_roster("help me debug my program")
    lines = out.splitlines()
    assert lines[0].startswith("- hephaestus:")          # most relevant first
    # REORDER ONLY: every specialist is still present, nothing trimmed
    assert len(lines) == len(specialists.SPECIALISTS)
    assert {ln.split(":")[0] for ln in lines} == {
        f"- {k}" for k in specialists.SPECIALISTS}
    store.reset()


def test_description_vectors_are_cached(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SEMANTIC_ROUTING", "1")
    monkeypatch.setattr(specialists, "_SEMANTIC_ROSTER_MIN", 2)
    monkeypatch.setattr(embed, "available", lambda: True)
    calls = {"n": 0}

    def counting_embed(texts):
        calls["n"] += 1
        return _fake_embed("Hephaestus")(texts)

    monkeypatch.setattr(embed, "embed", counting_embed)
    monkeypatch.setattr(embed, "embed_one", lambda q: [1.0, 0.0])
    store.reset()
    specialists.semantic_roster("q1")
    specialists.semantic_roster("q2")
    assert calls["n"] == 1                 # descriptions embedded once, then cached
    store.reset()


def test_falls_back_to_plain_on_embed_failure(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SEMANTIC_ROUTING", "1")
    monkeypatch.setattr(specialists, "_SEMANTIC_ROSTER_MIN", 2)
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "embed", lambda texts: None)     # endpoint failed
    monkeypatch.setattr(embed, "embed_one", lambda q: None)
    store.reset()
    assert specialists.semantic_roster("write code") == specialists.roster()
    store.reset()


# --- replay safety on the critical route path ----------------------------

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
                         "specialists": ["argus"], "brief": "b",
                         "needs_verification": False}))
                if "steps" in props:
                    return _msg(_json.dumps({"steps": [
                        {"id": "s1", "specialist": "argus", "task": "t",
                         "depends_on": []}]}))
                if "verdict" in props:
                    return _msg(_json.dumps(
                        {"verdict": "approve", "feedback": "",
                         "retry_specialists": []}))
                return _msg("findings")
        return _S()

    msgs = types.SimpleNamespace(stream=stream)
    return types.SimpleNamespace(messages=msgs,
                                 beta=types.SimpleNamespace(messages=msgs))


def test_semantic_routing_recorded_run_replays_clean(monkeypatch):
    """A run recorded with semantic routing on replays byte-identically even
    when the replay process has the flag unset — replay_run restores it from meta
    and the reordered roster is frozen."""
    from olympus import config, orchestrator, recall
    monkeypatch.setattr(recall, "context_block", lambda *a, **k: "")
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OLYMPUS_SEMANTIC_ROUTING", "1")
    monkeypatch.setattr(specialists, "_SEMANTIC_ROSTER_MIN", 2)
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "embed", _fake_embed("Hephaestus"))
    monkeypatch.setattr(embed, "embed_one", lambda q: [1.0, 0.0])
    monkeypatch.setattr(llm, "client", lambda settings=None: _client())
    store.reset()

    bot = orchestrator.Olympus(pool=config.ModelPool.from_env())
    bot.ask("please help with a task")
    rid = bot.last_run_id

    monkeypatch.delenv("OLYMPUS_SEMANTIC_ROUTING", raising=False)
    monkeypatch.setattr(llm, "client", lambda settings=None: (
        _ for _ in ()).throw(AssertionError("replay must not call the API")))
    original, _fresh, diffs = orchestrator.replay_run(rid)

    assert original["meta"]["semantic_routing"] is True     # recorded in meta
    assert diffs == []                                      # byte-identical replay
    store.reset()
