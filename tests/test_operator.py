"""HERMES operator, Phase 1: site profiles + structured predicates + vault-backed
login, all behind the OLYMPUS_OPERATOR master switch and deny-first gates.

These tests pin the safety properties from docs/DESIGN_OPERATOR.md:
- the operator is off by default and fails closed at every missing gate;
- capability separation still holds (the actuator never coexists with web
  ingestion);
- credentials come from the vault and the password never appears in output;
- site profiles carry provenance + a reliability score.
"""

import pytest

from olympus import browser, config, security, tools, vault
from olympus.specialists import SPECIALISTS


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    browser.set_transport_factory(None)
    yield
    browser.set_transport_factory(None)


def _enable(monkeypatch, domain="example.com"):
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    monkeypatch.setenv("OLYMPUS_OPERATOR_DOMAINS", domain)
    # egress allowlist gate is a no-op unless sovereign mode is on; keep it off.


# --- capability separation: HERMES holds the actuator, Argus never does ----

def test_hermes_holds_login_but_argus_does_not():
    hermes = {d.get("name") for d in SPECIALISTS["hermes"].tool_defs("anthropic")}
    argus = {d.get("name") for d in SPECIALISTS["argus"].tool_defs("anthropic")}
    assert "browser_login" in hermes               # non-ingesting → keeps actuator
    assert "browser_open" not in hermes and "browser_read" not in hermes
    assert "browser_login" not in argus            # ingesting → no actuator


def test_browser_login_is_an_action_tool_and_not_ingestion():
    assert "browser_login" in security.ACTION_TOOLS
    assert not security.should_wrap("browser_exists")   # bool predicate, not prose


# --- deny-first: every missing gate fails closed --------------------------

def test_login_refused_when_operator_disabled(monkeypatch):
    monkeypatch.delenv("OLYMPUS_OPERATOR", raising=False)
    out = tools._browser_login("example.com")
    assert out.startswith("Error") and "set it up" in out.lower()


def test_login_refused_when_domain_not_authorized(monkeypatch):
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    monkeypatch.setenv("OLYMPUS_OPERATOR_DOMAINS", "other.com")
    assert "isn't authorized" in tools._browser_login("example.com")


def test_login_refused_without_a_profile(monkeypatch):
    _enable(monkeypatch)
    assert "sign-in recipe" in tools._browser_login("example.com").lower()


def test_login_refused_without_vault_credentials(monkeypatch):
    _enable(monkeypatch)
    browser.record_profile("example.com", login_url="http://93.184.216.34/login",
                           success_selector="#account")
    monkeypatch.setattr(vault, "available", lambda: True)
    monkeypatch.setattr(vault, "get", lambda user, name: None)
    out = tools._browser_login("example.com")
    assert "saved credentials" in out.lower()


# --- the happy path: vaulted login, password never surfaced ---------------

def test_login_succeeds_and_hides_password(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    browser.record_profile(
        "example.com", login_url="http://93.184.216.34/login",
        username_selector="#user", password_selector="#pass",
        submit_selector="#go", success_selector="#account")
    fake = browser.FakeTransport(present=["#user", "#pass", "#go", "#account"])
    browser.set_transport_factory(lambda: fake)
    monkeypatch.setattr(vault, "available", lambda: True)
    monkeypatch.setattr(vault, "get",
                        lambda user, name: {"username": "alice",
                                            "password": "hunter2"})
    out = tools._browser_login("example.com")
    assert "Logged in to example.com" in out
    assert "hunter2" not in out                       # secret never surfaced
    assert browser.get_profile("example.com").reliability == 1.0


def test_login_reports_blocked_on_missing_success_marker(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    browser.record_profile(
        "example.com", login_url="http://93.184.216.34/login",
        username_selector="#user", password_selector="#pass",
        submit_selector="#go", success_selector="#account")
    # success marker NOT present → login can't confirm (2FA/CAPTCHA/drift)
    fake = browser.FakeTransport(present=["#user", "#pass", "#go"])
    browser.set_transport_factory(lambda: fake)
    monkeypatch.setattr(vault, "available", lambda: True)
    monkeypatch.setattr(vault, "get",
                        lambda user, name: {"username": "a", "password": "b"})
    out = tools._browser_login("example.com")
    assert "didn't go through" in out
    assert browser.get_profile("example.com").reliability == 0.0


# --- structured predicate + profile provenance ----------------------------

def test_browser_exists_returns_yes_no(monkeypatch):
    browser.set_transport_factory(lambda: browser.FakeTransport(present=["#x"]))
    assert tools._browser_exists("#x") == "yes"
    assert tools._browser_exists("#missing") == "no"


def test_site_profile_has_provenance_and_score():
    p = browser.record_profile("shop.com", login_url="http://shop.com/in",
                               success_selector="#me")
    assert p.content_hash.startswith("sha256:")
    assert p.created and p.reliability == 0.0
    listing = tools._site_profiles("shop.com")
    assert "shop.com" in listing and "reliability" in listing


def test_profile_outcomes_drive_reliability():
    browser.record_profile("s.com", login_url="x")
    browser.mark_profile_outcome("s.com", True)
    browser.mark_profile_outcome("s.com", False)
    assert browser.get_profile("s.com").reliability == 0.5


def test_operator_tools_are_threat_modeled():
    from olympus import threatmodel
    documented = threatmodel.documented_tools(
        threatmodel.doc_path().read_text(encoding="utf-8"))
    for name in ("browser_login", "browser_exists", "site_profile_record",
                 "site_profiles"):
        assert name in tools.HANDLERS and name in documented
    assert threatmodel.check_repo() == []
