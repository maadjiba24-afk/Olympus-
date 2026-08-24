"""Conversational operator setup — a non-engineer authorizes sites by asking,
never by touching env vars or a CLI. Per-user, persisted in prefs.
"""

import pytest

from olympus import (actions, browser, builtin_actions, config, memory,  # noqa: F401
                     operator, security, tools)
from olympus.specialists import SPECIALISTS

builtin_actions.register_builtins()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    # The browser is leased to an AUTHENTICATED principal (browserlease).
    # These tests run as the local terminal owner; the ambient default
    # ("shared", the heartbeat namespace) is refused by design.
    memory.set_user("cli")
    from olympus import browserlease
    browserlease.clear_for_tests()
    # No OLYMPUS_OPERATOR / _DOMAINS env — prove it works without them.
    monkeypatch.delenv("OLYMPUS_OPERATOR", raising=False)
    monkeypatch.delenv("OLYMPUS_OPERATOR_DOMAINS", raising=False)
    browser.set_transport_factory(None)
    yield
    browserlease.clear_for_tests()
    memory.set_user("shared")
    browser.set_transport_factory(None)


# --- off by default, friendly refusals ------------------------------------

def test_operator_off_by_default():
    assert operator.enabled("cli") is False
    assert "set it up" in tools._browser_login("amazon.com").lower()
    assert "off" in tools._operator_status().lower()


# --- conversational authorize turns it on (no env, no CLI) -----------------

def test_authorize_manual_enables_and_persists(monkeypatch):
    out = tools._operator_authorize_site("Amazon.com", "manual")
    assert "set up" in out.lower() and "sign in yourself" in out.lower()
    assert operator.enabled("cli") is True
    assert operator.authorized("cli", "amazon.com") is True
    assert operator.login_mode("cli", "amazon.com") == "manual"
    # manual sign-in never asks for a password — it points the user to log in
    assert "manual sign-in" in tools._browser_login("amazon.com").lower()


def test_status_lists_authorized_sites():
    tools._operator_authorize_site("amazon.com", "manual")
    tools._operator_authorize_site("github.com", "manual")
    s = tools._operator_status()
    assert "amazon.com" in s and "github.com" in s


def test_forget_site_revokes(monkeypatch):
    tools._operator_authorize_site("amazon.com", "manual")
    assert operator.authorized("cli", "amazon.com")
    out = tools._operator_forget_site("amazon.com")
    assert "removed" in out.lower()
    assert operator.authorized("cli", "amazon.com") is False


def test_authorization_is_per_user():
    operator.authorize_site("alice", "amazon.com", "manual")
    assert operator.authorized("alice", "amazon.com") is True
    assert operator.authorized("bob", "amazon.com") is False     # not shared


# --- end to end with NO env: authorize + operate auto-runs within policy ---

def test_operate_works_without_env_after_authorize(monkeypatch):
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    user = memory.current_user()
    tools._operator_authorize_site("shop.com", "manual")          # conversational
    actions.set_autonomy(user, actions.L4_STANDING)
    actions.grant_scope(user, operator.OPERATE_SCOPE)
    browser.set_template("shop.com", "buy", "notable",
                         [{"op": "click", "selector": "#buy"}],
                         success_selector="#done")
    browser.set_transport_factory(
        lambda: browser.FakeTransport(present=["#buy", "#done"]))
    out = tools._browser_operate("shop.com", "buy")
    assert "Executed 'buy' on shop.com" in out


# --- 'remember' mode stores creds in the vault, switches login mode --------

def test_remember_credentials_uses_vault(monkeypatch):
    stored = {}
    monkeypatch.setattr(operator, "_settings", operator._settings)  # real prefs
    from olympus import vault
    monkeypatch.setattr(vault, "available", lambda: True)
    monkeypatch.setattr(vault, "put",
                        lambda u, n, v: stored.__setitem__((u, n), v))
    operator.remember_credentials("cli", "bank.com", "alice", "s3cret")
    assert stored[("cli", "site:bank.com")]["username"] == "alice"
    assert operator.login_mode("cli", "bank.com") == "remember"


# --- the new tools stay bound to the threat model -------------------------

def test_onboarding_tools_threat_modeled():
    from olympus import threatmodel
    documented = threatmodel.documented_tools(
        threatmodel.doc_path().read_text(encoding="utf-8"))
    for name in ("operator_authorize_site", "operator_forget_site",
                 "operator_status"):
        assert name in tools.HANDLERS and name in documented
    assert threatmodel.check_repo() == []


def test_hermes_has_onboarding_tools():
    names = {d.get("name") for d in SPECIALISTS["hermes"].tool_defs("anthropic")}
    assert {"operator_authorize_site", "operator_forget_site",
            "operator_status"} <= names
