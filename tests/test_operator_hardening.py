"""Operator hardening from the audit: financial_legal templates get the tightest
risk tier, and browser_act is authorization-gated like the other credentialed
verbs.
"""

import pytest

from olympus import (actions, browser, builtin_actions, config, memory,  # noqa: F401
                     operator, tools)

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
    monkeypatch.delenv("OLYMPUS_OPERATOR", raising=False)
    monkeypatch.delenv("OLYMPUS_OPERATOR_DOMAINS", raising=False)
    browser.set_transport_factory(None)
    yield
    browserlease.clear_for_tests()
    memory.set_user("shared")
    browser.set_transport_factory(None)


# --- #8: financial_legal maps to the FINANCIAL_LEGAL tier -----------------

def test_financial_legal_gets_tightest_tier():
    assert operator.type_for_risk("financial_legal") == "browser_operate_financial"
    assert operator.type_for_risk("irreversible") == "browser_operate_irreversible"
    assert operator.type_for_risk("notable") == "browser_operate"
    reg = actions.registered()
    assert reg["browser_operate_financial"].risk_class == actions.FINANCIAL_LEGAL
    # tighter daily cap than the generic irreversible tier
    assert actions.daily_limit("cli", "browser_operate_financial") \
        <= actions.daily_limit("cli", "browser_operate_irreversible")


# --- #7: browser_act is authorization-gated -------------------------------

def test_browser_act_refused_when_operator_disabled():
    out = tools._browser_act("click", selector="#buy")
    assert out.startswith("Error") and "set up" in out.lower()


def test_browser_act_refused_on_unauthorized_domain(monkeypatch):
    operator.authorize_site("cli", "shop.com", "manual")   # enables operator
    fake = browser.FakeTransport(present=["#buy"])
    fake._url = "https://evil.com/"                            # current page NOT authorized
    browser.set_transport_factory(lambda: fake)
    out = tools._browser_act("click", selector="#buy")
    assert out.startswith("Error") and "authorized" in out.lower()


def test_browser_act_allowed_on_authorized_domain(monkeypatch):
    operator.authorize_site("cli", "shop.com", "manual")
    fake = browser.FakeTransport(present=["#buy"])
    fake._url = "https://shop.com/cart"
    browser.set_transport_factory(lambda: fake)
    out = tools._browser_act("click", selector="#buy")
    assert "Clicked" in out


# --- financial/irreversible templates are DISABLED BY DEFAULT --------------
#
# OLYMPUS_ENABLE_BROWSER_FINANCIAL gates the two highest-risk action types,
# fail-closed at the execution door: even an explicitly APPROVED action
# refuses without the opt-in (mirroring how the sovereign gates are written).

def _authorized_template(monkeypatch, risk):
    from olympus import security
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    monkeypatch.setenv("OLYMPUS_OPERATOR_DOMAINS", "shop.com")
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    user = memory.current_user()
    actions.set_autonomy(user, actions.L4_STANDING)
    actions.grant_scope(user, operator.OPERATE_SCOPE)
    browser.set_template("shop.com", "buy", risk,
                         [{"op": "click", "selector": "#buy"}],
                         success_selector="#done")
    browser.set_transport_factory(
        lambda: browser.FakeTransport(present=["#buy", "#done"]))
    return user


@pytest.mark.parametrize("risk", ["financial_legal", "irreversible"])
def test_gated_templates_fail_closed_by_default(monkeypatch, risk):
    monkeypatch.delenv("OLYMPUS_ENABLE_BROWSER_FINANCIAL", raising=False)
    user = _authorized_template(monkeypatch, risk)
    action = operator.run(user, "shop.com", "buy", {})
    assert action.status == actions.PREPARED           # held, as ever
    done = actions.approve(user, action.id)            # even explicit approval
    assert done.status == actions.FAILED               # refuses at the door
    assert "OLYMPUS_ENABLE_BROWSER_FINANCIAL" in done.error


@pytest.mark.parametrize("risk", ["financial_legal", "irreversible"])
def test_gated_templates_execute_with_explicit_optin(monkeypatch, risk):
    monkeypatch.setenv("OLYMPUS_ENABLE_BROWSER_FINANCIAL", "1")
    user = _authorized_template(monkeypatch, risk)
    action = operator.run(user, "shop.com", "buy", {})
    assert action.status == actions.PREPARED           # still approval-held
    done = actions.approve(user, action.id)
    assert done.status == actions.EXECUTED


def test_notable_templates_unaffected_by_the_flag(monkeypatch):
    monkeypatch.delenv("OLYMPUS_ENABLE_BROWSER_FINANCIAL", raising=False)
    user = _authorized_template(monkeypatch, "notable")
    action = operator.run(user, "shop.com", "buy", {})
    assert action.status == actions.EXECUTED           # auto-ran within policy
