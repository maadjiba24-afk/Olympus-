"""Error visibility (F3): load-bearing paths log failures instead of hiding
them as a silent empty result. Telemetry swallows are intentionally untouched."""

import logging

from olympus import config, errors, gmail, recall, usage


_SETTINGS = config.Settings(provider="anthropic", model="claude-test")


def test_real_extract_error_is_logged_not_silent(monkeypatch):
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_MIN_CHARS", 1)
    monkeypatch.setattr(usage, "check_budget", lambda: None)
    # An unexpected failure in the extractor body (here: the model call):
    def boom(*a, **k):
        raise RuntimeError("extractor backend exploded")
    monkeypatch.setattr(recall.backend, "complete_json", boom)
    captured = []
    monkeypatch.setattr(errors, "capture",
                        lambda where, exc, context="": captured.append((where, repr(exc))))

    # Best-effort: must not raise into the caller...
    summary = recall.extract("u", "a real message worth extracting", "a reply",
                             _SETTINGS)
    assert summary == {}
    # ...but the real error is surfaced, not swallowed.
    assert captured and captured[0][0] == "recall.extract"
    assert "exploded" in captured[0][1]


def test_expected_empty_paths_do_not_log(monkeypatch):
    captured = []
    monkeypatch.setattr(errors, "capture",
                        lambda *a, **k: captured.append(a))
    # memory disabled -> quiet no-op, NOT an error
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    assert recall.extract("u", "anything", "reply", _SETTINGS) == {}
    # trivial turn -> quiet no-op
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_MIN_CHARS", 9999)
    assert recall.extract("u", "hi", "reply", _SETTINGS) == {}
    # over budget -> quiet no-op
    monkeypatch.setattr(config, "MEMORY_MIN_CHARS", 1)
    monkeypatch.setattr(usage, "check_budget",
                        lambda: (_ for _ in ()).throw(usage.BudgetExceeded("cap")))
    assert recall.extract("u", "a real message", "reply", _SETTINGS) == {}
    assert captured == []          # none of the expected paths logged an error


def test_connector_swallow_now_logs(monkeypatch, caplog):
    # gmail's OAuth-token lookup falls back on failure, but now logs it.
    monkeypatch.setenv("GMAIL_ACCESS_TOKEN", "env-tok")
    import olympus.google_oauth as go
    monkeypatch.setattr(go, "connected",
                        lambda user: (_ for _ in ()).throw(RuntimeError("oauth store broken")))
    with caplog.at_level(logging.WARNING, logger="olympus.gmail"):
        tok = gmail._access_token()
    assert tok == "env-tok"                      # fell back, didn't crash
    assert any("OAuth token lookup failed" in r.message for r in caplog.records)
