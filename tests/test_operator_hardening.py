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
    memory.set_user("shared")
    monkeypatch.delenv("OLYMPUS_OPERATOR", raising=False)
    monkeypatch.delenv("OLYMPUS_OPERATOR_DOMAINS", raising=False)
    browser.set_transport_factory(None)
    yield
    browser.set_transport_factory(None)


# --- #8: financial_legal maps to the FINANCIAL_LEGAL tier -----------------

def test_financial_legal_gets_tightest_tier():
    assert operator.type_for_risk("financial_legal") == "browser_operate_financial"
    assert operator.type_for_risk("irreversible") == "browser_operate_irreversible"
    assert operator.type_for_risk("notable") == "browser_operate"
    reg = actions.registered()
    assert reg["browser_operate_financial"].risk_class == actions.FINANCIAL_LEGAL
    # tighter daily cap than the generic irreversible tier
    assert actions.daily_limit("shared", "browser_operate_financial") \
        <= actions.daily_limit("shared", "browser_operate_irreversible")


# --- #7: browser_act is authorization-gated -------------------------------

def test_browser_act_refused_when_operator_disabled():
    out = tools._browser_act("click", selector="#buy")
    assert out.startswith("Error") and "set up" in out.lower()


def test_browser_act_refused_on_unauthorized_domain(monkeypatch):
    operator.authorize_site("shared", "shop.com", "manual")   # enables operator
    fake = browser.FakeTransport(present=["#buy"])
    fake._url = "https://evil.com/"                            # current page NOT authorized
    browser.set_transport_factory(lambda: fake)
    out = tools._browser_act("click", selector="#buy")
    assert out.startswith("Error") and "authorized" in out.lower()


def test_browser_act_allowed_on_authorized_domain(monkeypatch):
    operator.authorize_site("shared", "shop.com", "manual")
    fake = browser.FakeTransport(present=["#buy"])
    fake._url = "https://shop.com/cart"
    browser.set_transport_factory(lambda: fake)
    out = tools._browser_act("click", selector="#buy")
    assert "Clicked" in out
