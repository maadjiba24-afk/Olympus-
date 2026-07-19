"""Teardown-loop iteration 2: email spoof-guard, memory card (#36), vault
mirror (#37), visible memory (#39), soul scaffold (#40), hit distillation (#42)."""

import pytest

from olympus import (config, email_gateway, gateway, gmail, memory, recall,
                     soul, tools, usermem)


# --- email spoof-guard -------------------------------------------------------

def _msg(**kw):
    base = {"id": "m1", "sender": "boss@x.com", "subject": "s", "body": "b",
            "authenticated": True}
    base.update(kw)
    return base


def test_allowlist_requires_authentication(monkeypatch):
    monkeypatch.setenv("OLYMPUS_EMAIL_ALLOW", "boss@x.com")
    monkeypatch.setattr(gmail, "send", lambda *a, **k: {"id": "1"})
    monkeypatch.setattr(gateway, "reply_for", lambda *a, **k: ["ok"])
    # Spoofed From: right address, but Gmail's auth verdict failed → rejected.
    assert email_gateway.handle_message({}, _msg(authenticated=False)) is None
    # Authenticated message from the same address → answered.
    assert email_gateway.handle_message({}, _msg(authenticated=True)) == "ok"


def test_no_allowlist_means_no_auth_demand(monkeypatch):
    # Without an allowlist, sender identity isn't load-bearing — keep replying.
    monkeypatch.delenv("OLYMPUS_EMAIL_ALLOW", raising=False)
    monkeypatch.setattr(gmail, "send", lambda *a, **k: {"id": "1"})
    monkeypatch.setattr(gateway, "reply_for", lambda *a, **k: ["ok"])
    assert email_gateway.handle_message({}, _msg(authenticated=False)) == "ok"


def test_gmail_authenticated_parses_header():
    ok = {"payload": {"headers": [
        {"name": "Authentication-Results",
         "value": "mx.google.com; dkim=pass; spf=pass; dmarc=pass"}]}}
    spoof = {"payload": {"headers": [
        {"name": "Authentication-Results",
         "value": "mx.google.com; spf=fail; dmarc=fail"}]}}
    missing = {"payload": {"headers": []}}
    assert gmail._authenticated(ok) is True
    assert gmail._authenticated(spoof) is False
    assert gmail._authenticated(missing) is False    # absent verdict = fail closed


# --- memory card (#36) -------------------------------------------------------

def test_memory_card_shows_facts_with_receipts():
    usermem.add_memory("card-u", type="preference",
                       content="prefers concise replies", confidence=0.9)
    card = usermem.render_card("card-u")
    assert "prefers concise replies" in card
    assert "conf 0.9" in card or "conf 0.8" in card   # live (decayed) value
    assert "projection of the gated store" in card    # governance stance


def test_memory_card_empty_user():
    card = usermem.render_card("nobody-here")
    assert "What Olympus knows" in card


# --- vault mirror (#37) ------------------------------------------------------

def test_vault_mirror_writes_through(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    monkeypatch.setenv("OLYMPUS_VAULT_DIR", str(vault))
    memory.set_user("shared")
    memory.save("lessons", "vault test", "a lesson body")
    mirrored = list((vault / "lessons").glob("*.md"))
    assert len(mirrored) == 1
    assert "a lesson body" in mirrored[0].read_text()


def test_vault_mirror_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OLYMPUS_VAULT_DIR", raising=False)
    memory.set_user("shared")
    path = memory.save("reports", "no vault", "body")
    assert path.exists()                              # normal save unaffected


def test_vault_mirror_never_breaks_save(tmp_path, monkeypatch):
    # An unwritable vault path must not break the canonical save.
    monkeypatch.setenv("OLYMPUS_VAULT_DIR", "/proc/definitely/not/writable")
    memory.set_user("shared")
    assert memory.save("reports", "resilient", "body").exists()


# --- visible memory activity (#39) ------------------------------------------

def test_extract_reports_committed_facts(monkeypatch):
    monkeypatch.setattr(recall.backend, "complete_json",
                        lambda *a, **k: {"memories": [
                            {"type": "preference", "content": "likes tables",
                             "confidence": 0.9, "importance": 0.5}],
                            "relationships": []})
    lines: list[str] = []
    settings = config.Settings(provider="openai", model="m", api_key="k",
                               base_url="http://x")
    summary = recall.extract("vis-u", "please always use tables in replies from now on, every time",
                             "noted", settings, report=lines.append)
    assert summary.get("committed") == 1
    assert any("🧠" in l and "likes tables" in l for l in lines)


def test_extract_report_is_optional(monkeypatch):
    monkeypatch.setattr(recall.backend, "complete_json",
                        lambda *a, **k: {"memories": [], "relationships": []})
    settings = config.Settings(provider="openai", model="m", api_key="k",
                               base_url="http://x")
    # no report param — must not raise
    recall.extract("vis-u2", "a message long enough to pass the floor check",
                   "reply", settings)


# --- soul scaffold (#40) -----------------------------------------------------

def test_soul_template_has_role_and_focus(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_HOME", str(tmp_path))
    text = soul.ensure_scaffold().read_text()
    assert "## Role" in text and "## Current focus" in text


# --- hit-set distillation (#42) ----------------------------------------------

def test_small_hit_sets_returned_verbatim(monkeypatch):
    from olympus import search
    hit = search.Hit("conv", "user", "short question about pricing", 0)
    monkeypatch.setattr(search, "search", lambda q, limit=10: [hit])
    out = tools._search_sessions("pricing")
    assert "short question about pricing" in out
    assert "distilled" not in out


def test_large_hit_sets_distilled(monkeypatch):
    from olympus import backend, search
    hits = [search.Hit(f"conv{i}", "user", "x" * 400, i) for i in range(10)]
    monkeypatch.setattr(search, "search", lambda q, limit=10: hits)
    monkeypatch.setattr(backend, "complete_text",
                        lambda *a, **k: "the essentials [conv1#1]")
    out = tools._search_sessions("anything")
    assert out.startswith("[distilled from 10 matching turns]")
    assert "the essentials" in out


def test_distillation_falls_back_to_truncation(monkeypatch):
    from olympus import backend, search
    hits = [search.Hit(f"c{i}", "user", "y" * 400, i) for i in range(10)]
    monkeypatch.setattr(search, "search", lambda q, limit=10: hits)
    monkeypatch.setattr(backend, "complete_text",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    out = tools._search_sessions("anything")
    assert "…[truncated]" in out and len(out) < 4000
