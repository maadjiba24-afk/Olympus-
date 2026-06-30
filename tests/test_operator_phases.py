"""HERMES operator Phases 2-4: credentialed operate on the approval spine,
always-on heartbeat jobs, and the Metis/Prometheus weave. Pins the governance
from docs/DESIGN_OPERATOR.md to code.
"""

import json

import pytest

from olympus import (actions, browser, builtin_actions, config, memory,  # noqa: F401
                     operator, security, tools)
from olympus.specialists import SPECIALISTS

builtin_actions.register_builtins()      # ensure operate ActionTypes are on the spine


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    memory.set_user("shared")
    browser.set_transport_factory(None)
    yield
    browser.set_transport_factory(None)


def _authorize(monkeypatch, domain="shop.com", *, autonomy=actions.L4_STANDING,
               scope=True):
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    monkeypatch.setenv("OLYMPUS_OPERATOR_DOMAINS", domain)
    user = memory.current_user()
    actions.set_autonomy(user, autonomy)
    if scope:
        actions.grant_scope(user, operator.OPERATE_SCOPE)


def _buy_template(domain="shop.com", risk="notable"):
    browser.set_template(domain, "buy", risk,
                         [{"op": "click", "selector": "#buy"}],
                         success_selector="#done")


# --- the operate ActionTypes are on the spine -----------------------------

def test_operate_action_types_registered():
    reg = actions.registered()
    assert reg["browser_operate"].risk_class == actions.NOTABLE
    assert reg["browser_operate_irreversible"].risk_class == actions.IRREVERSIBLE
    assert reg["browser_operate"].scope == operator.OPERATE_SCOPE


def test_capability_separation_for_operate():
    assert "browser_operate" in security.ACTION_TOOLS
    hermes = {d.get("name") for d in SPECIALISTS["hermes"].tool_defs("anthropic")}
    argus = {d.get("name") for d in SPECIALISTS["argus"].tool_defs("anthropic")}
    assert {"browser_operate", "site_template_record"} <= hermes
    assert "browser_operate" not in argus      # Argus ingests → no actuator


# --- Phase 2: deny-first + auto-run vs. approval --------------------------

def test_operate_refused_when_disabled(monkeypatch):
    monkeypatch.delenv("OLYMPUS_OPERATOR", raising=False)
    assert "set it up" in tools._browser_operate("shop.com", "buy").lower()


def test_operate_refused_unknown_template(monkeypatch):
    _authorize(monkeypatch)
    assert "no template" in tools._browser_operate("shop.com", "buy").lower()


def test_notable_template_auto_runs_within_scope(monkeypatch):
    _authorize(monkeypatch)
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    _buy_template(risk="notable")
    browser.set_transport_factory(
        lambda: browser.FakeTransport(present=["#buy", "#done"]))
    out = tools._browser_operate("shop.com", "buy")
    assert "Executed 'buy' on shop.com" in out
    assert browser.get_profile("shop.com").runs == 1      # outcome recorded


def test_irreversible_template_always_waits_for_approval(monkeypatch):
    _authorize(monkeypatch)                                # scope + L4 autonomy
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    _buy_template(risk="irreversible")
    browser.set_transport_factory(
        lambda: browser.FakeTransport(present=["#buy", "#done"]))
    user = memory.current_user()
    action = operator.run(user, "shop.com", "buy", {})
    assert action.status == actions.PREPARED               # held despite autonomy
    assert action.risk_class == actions.IRREVERSIBLE
    # explicit human approval executes it
    done = actions.approve(user, action.id)
    assert done.status == actions.EXECUTED
    assert done.result.get("steps") == ["click #buy"]


def test_operate_blocked_without_scope(monkeypatch):
    _authorize(monkeypatch, scope=False)                   # no browser.operate scope
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    _buy_template(risk="notable")
    browser.set_transport_factory(
        lambda: browser.FakeTransport(present=["#buy", "#done"]))
    out = tools._browser_operate("shop.com", "buy")
    # L4 autonomy but no scope → cannot auto-execute → held for approval
    assert "Awaiting your approval" in out


# --- Phase 3: always-on jobs through the heartbeat ------------------------

def test_run_due_is_noop_when_operator_disabled(monkeypatch):
    monkeypatch.delenv("OLYMPUS_OPERATOR", raising=False)
    operator.schedule("shared", "nightly", "shop.com", "buy", 300, {})
    assert operator.run_due(now=10_000) == []


def test_due_job_runs_through_spine(monkeypatch):
    _authorize(monkeypatch)
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    _buy_template(risk="notable")
    browser.set_transport_factory(
        lambda: browser.FakeTransport(present=["#buy", "#done"]))
    operator.schedule(memory.current_user(), "nightly", "shop.com", "buy", 300, {})
    lines = operator.run_due(now=10_000)
    assert any("executed on shop.com" in ln for ln in lines)
    # not due again immediately
    assert operator.run_due(now=10_001) == []


# --- Phase 4: Metis review + Prometheus proposals -------------------------

def test_review_prunes_flaky_profiles():
    browser.record_profile("flaky.com", login_url="x")
    for _ in range(3):
        browser.mark_profile_outcome("flaky.com", False)   # 0/3 → below floor
    browser.record_profile("solid.com", login_url="x")
    browser.mark_profile_outcome("solid.com", True)
    report = operator.review_profiles()
    domains = {p.domain for p in browser.list_profiles()}
    assert "flaky.com" not in domains and "solid.com" in domains
    assert "flaky.com" in report


def test_propose_site_profile_files_for_review():
    out = tools._propose_site_profile("shop.com", "selector drifted",
                                      success_selector="#new")
    assert "proposal" in out.lower() and "shop.com" in out


def test_operator_tools_threat_modeled():
    from olympus import threatmodel
    documented = threatmodel.documented_tools(
        threatmodel.doc_path().read_text(encoding="utf-8"))
    for name in ("browser_operate", "site_template_record", "operator_schedule",
                 "operator_review", "propose_site_profile"):
        assert name in tools.HANDLERS and name in documented
    assert threatmodel.check_repo() == []
