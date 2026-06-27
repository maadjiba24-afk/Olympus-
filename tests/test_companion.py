"""Per-user adaptive evolution: interaction counting, growth tiers, the
per-user working model, its context injection, and the evolve cycle."""

from olympus import companion, config, usermem


def test_note_interaction_counts_per_user():
    assert companion.note_interaction("alice") == 1
    assert companion.note_interaction("alice") == 2
    assert companion.note_interaction("bob") == 1          # independent per user


def test_due_checkpoints(monkeypatch):
    monkeypatch.setattr(companion, "EVOLVE_EVERY", 6)
    assert companion.due(6) and companion.due(12)
    assert not companion.due(5) and not companion.due(0)


def test_growth_levels():
    assert companion.growth_level(0)[0] == "new"
    assert companion.growth_level(3)[0] == "acquainted"
    assert companion.growth_level(10)[0] == "familiar"
    assert companion.growth_level(30)[0] == "attuned"
    assert companion.growth_level(250)[0] == "trusted companion"


def test_model_block_empty_until_learned():
    assert companion.model_block("nobody") == ""
    companion.save("u", {"interactions": 9, "evolutions": 1,
                         "model": "- prefers terse answers", "updated": 1.0})
    block = companion.model_block("u")
    assert "prefers terse answers" in block and "working with this user" in block


def test_model_block_is_per_user_private():
    companion.save("u1", {"interactions": 1, "model": "u1 secret", "evolutions": 1})
    assert "u1 secret" in companion.model_block("u1")
    assert companion.model_block("u2") == ""               # never leaks across users


def test_evolve_synthesizes_and_persists(monkeypatch):
    usermem.add_memory("alice", type="preference",
                       content="wants answers in French", confidence=0.9)
    captured = {}

    def fake_complete_text(settings, system, messages, effort="low"):
        captured["system"] = system
        captured["prompt"] = messages[0]["content"]
        return "- Communicates in French\n- Prefers concise, cited answers"

    monkeypatch.setattr("olympus.backend.complete_text", fake_complete_text)
    model = companion.evolve("alice")
    assert "French" in model
    assert "French" in captured["prompt"]                  # fed the user's facts
    state = companion.load("alice")
    assert state["evolutions"] == 1 and state["model"] == model


def test_evolve_keeps_prior_on_failure(monkeypatch):
    companion.save("c", {"interactions": 6, "evolutions": 2,
                         "model": "- established model", "updated": 1.0})

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr("olympus.backend.complete_text", boom)
    assert companion.evolve("c") == "- established model"   # never wiped
    assert companion.load("c")["evolutions"] == 2           # unchanged


def test_evolve_caps_model_size(monkeypatch):
    monkeypatch.setattr("olympus.backend.complete_text",
                        lambda *a, **k: "x" * 5000)
    model = companion.evolve("big")
    assert len(model) <= companion.MODEL_MAX_CHARS


def test_summary_shows_level_and_model():
    companion.save("s", {"interactions": 12, "evolutions": 3,
                         "model": "- likes diagrams", "updated": 1.0})
    out = companion.summary("s")
    assert "familiar" in out and "conversations: 12" in out
    assert "likes diagrams" in out


def test_maybe_evolve_only_on_checkpoint(monkeypatch):
    monkeypatch.setattr(companion, "EVOLVE_EVERY", 6)
    ran = {"n": 0}
    monkeypatch.setattr(companion, "evolve", lambda u, s=None: ran.__setitem__("n", ran["n"] + 1))
    assert companion.maybe_evolve("u", 5) is False and ran["n"] == 0
    assert companion.maybe_evolve("u", 6) is True and ran["n"] == 1
