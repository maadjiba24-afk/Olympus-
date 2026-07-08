"""Phase 3 — blind multi-model compare. The same prompt runs against every
usable pool member; labels are shuffled so label order never tracks pool order;
the label→model mapping is hidden until reveal; blind picks accumulate into a
per-user tally. Each model is called WITHOUT cross-member failover so a failing
model shows its own error rather than another model's answer.
"""

import pytest

from olympus import backend, compare, config


def _settings(model):
    return config.Settings(provider="anthropic", model=model, api_key="k")


@pytest.fixture()
def two_models(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    ms = [_settings("claude-a"), _settings("gpt-b")]
    monkeypatch.setattr(compare, "available_models", lambda: ms)

    def fake_once(settings, system, messages, effort="high"):
        return f"answer from {settings.model}"

    monkeypatch.setattr(backend, "complete_text_once", fake_once)
    return ms


# --- guards ----------------------------------------------------------------

def test_run_needs_two_models(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(compare, "available_models",
                        lambda: [_settings("solo")])
    out = compare.run("u", "hi")
    assert "error" in out and out["models"] == ["anthropic/solo"]
    # the refusal carries an actionable hint + copy-paste snippet
    assert "OLYMPUS_MODELS" in out["hint"]
    assert out["snippet"].startswith("OLYMPUS_MODELS=")
    assert "env:ANTHROPIC_API_KEY" in out["snippet"]


def test_setup_hint_mentions_second_model():
    h = compare.setup_hint()
    assert "two configured models" in h and "OLYMPUS_MODELS" in h


# --- blind run -------------------------------------------------------------

def test_run_returns_blind_answers(two_models):
    out = compare.run("u", "what is 2+2")
    labels = [a["label"] for a in out["answers"]]
    assert labels == ["A", "B"]
    # answers present, but no mapping leaked in the run payload
    assert "mapping" not in out
    assert all("answer from" in a["text"] for a in out["answers"])
    assert out["id"]


def test_labels_do_not_leak_model_order(two_models, monkeypatch):
    # Force the shuffle to reverse order; label A must then map to the 2nd model.
    monkeypatch.setattr(compare.random, "shuffle",
                        lambda seq: seq.reverse())
    out = compare.run("u", "q")
    rev = compare.reveal("u", out["id"])
    assert rev["mapping"]["A"] == "anthropic/gpt-b"
    assert rev["mapping"]["B"] == "anthropic/claude-a"


def test_failing_model_shows_own_error_not_substitute(two_models, monkeypatch):
    def half_fail(settings, system, messages, effort="high"):
        if settings.model == "gpt-b":
            raise RuntimeError("rate limited")
        return "real answer"

    monkeypatch.setattr(backend, "complete_text_once", half_fail)
    monkeypatch.setattr(compare.random, "shuffle", lambda seq: None)
    out = compare.run("u", "q")
    texts = {a["label"]: a["text"] for a in out["answers"]}
    # B is the failing model here (no shuffle) — it reports its own failure.
    assert "failed to answer" in texts["B"] and "gpt-b" in texts["B"]
    assert texts["A"] == "real answer"


# --- reveal + tally --------------------------------------------------------

def test_reveal_records_pick_into_tally(two_models, monkeypatch):
    monkeypatch.setattr(compare.random, "shuffle", lambda seq: None)
    out = compare.run("u", "q")
    rev = compare.reveal("u", out["id"], "A")
    assert rev["choice"] == "A"
    assert rev["chosen_model"] == "anthropic/claude-a"
    assert compare.tally("u") == {"anthropic/claude-a": 1}


def test_reveal_without_pick_leaves_tally_untouched(two_models):
    out = compare.run("u", "q")
    rev = compare.reveal("u", out["id"])
    assert rev["choice"] is None and rev["chosen_model"] is None
    assert compare.tally("u") == {}


def test_reveal_unknown_id_is_none(two_models):
    assert compare.reveal("u", "nope") is None


def test_tally_accumulates_across_runs(two_models, monkeypatch):
    monkeypatch.setattr(compare.random, "shuffle", lambda seq: None)
    for _ in range(3):
        out = compare.run("u", "q")
        compare.reveal("u", out["id"], "A")
    out = compare.run("u", "q")
    compare.reveal("u", out["id"], "B")
    assert compare.tally("u") == {"anthropic/claude-a": 3, "anthropic/gpt-b": 1}


def test_render_tally(two_models, monkeypatch):
    monkeypatch.setattr(compare.random, "shuffle", lambda seq: None)
    out = compare.run("u", "q")
    compare.reveal("u", out["id"], "A")
    assert "anthropic/claude-a: 1" in compare.render_tally("u")


def test_stored_comparisons_are_bounded(two_models, monkeypatch):
    monkeypatch.setattr(compare, "_MAX_STORED", 3)
    ids = [compare.run("u", f"q{i}")["id"] for i in range(6)]
    kept = list((compare._dir("u")).glob("*.json"))
    kept = [p for p in kept if p.name != "_tally.json"]
    assert len(kept) <= 3
    # the oldest ids were pruned; the newest survives
    assert compare.reveal("u", ids[-1]) is not None


# --- no-failover backend seam ----------------------------------------------

def test_complete_text_once_does_not_fail_over(monkeypatch):
    calls = []

    def boom(s, system, messages, effort):
        calls.append(s.model)
        raise RuntimeError("provider down")

    monkeypatch.setattr(backend, "_dispatch_text", boom)
    with pytest.raises(RuntimeError):
        backend.complete_text_once(_settings("x"), "sys", [], effort="high")
    assert calls == ["x"]         # exactly one attempt, no fallback chain
