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
    # This test is about the approval hold, so opt into the (default-off)
    # high-risk template gate; the default-off behavior itself is covered in
    # test_operator_hardening.py.
    monkeypatch.setenv("OLYMPUS_ENABLE_BROWSER_FINANCIAL", "1")
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


def test_operate_hands_off_on_a_human_checkpoint(monkeypatch):
    # A blocking human-verification checkpoint must be reported as a handoff —
    # never solved, and never mistaken for template drift (no self-heal proposal).
    _authorize(monkeypatch)
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    _buy_template(risk="notable")            # asserts #buy, which is absent below
    browser.set_transport_factory(lambda: browser.FakeTransport(
        checkpoint={"type": "captcha", "detail": "recaptcha"}))
    user = memory.current_user()
    action = operator.run(user, "shop.com", "buy", {})
    assert action.status == actions.FAILED
    assert "human-verification checkpoint" in (action.error or "").lower()
    assert "captcha" in (action.error or "").lower()


# --- earned per-domain autonomy: a proven site auto-runs safe actions ----

def _seed_clean_runs(user, domain, n):
    """Write `n` EXECUTED governed runs into the audit trail (earns trust)."""
    import uuid
    for i in range(n):
        actions._save(actions.Action(
            id=uuid.uuid4().hex[:12], user=memory.safe_id(user),
            type="browser_operate", title="op", payload={"domain": domain},
            risk_class=actions.NOTABLE, reversible=True,
            status=actions.EXECUTED, created_at=100 + i))


def test_earned_autonomy_auto_runs_reversible_on_a_proven_site(monkeypatch):
    from olympus import trust
    # A cautious user at the DEFAULT autonomy (L1 prepare) — no blanket standing
    # authority anywhere.
    _authorize(monkeypatch, autonomy=actions.L1_PREPARE)
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    _buy_template(risk="notable")
    browser.set_transport_factory(
        lambda: browser.FakeTransport(present=["#buy", "#done"]))
    user = memory.current_user()
    # Without earned autonomy, a notable template holds for approval at L1.
    held = operator.run(user, "shop.com", "buy", {})
    assert held.status == actions.PREPARED
    # Opt in and let the site prove itself over a long clean run…
    trust.set_enabled(user, True)
    _seed_clean_runs(user, "shop.com", 20)                 # → established tier
    ran = operator.run(user, "shop.com", "buy", {})
    assert ran.status == actions.EXECUTED                  # earned freedom, no ask


def test_earned_autonomy_never_lifts_an_irreversible_template(monkeypatch):
    from olympus import trust
    _authorize(monkeypatch, autonomy=actions.L1_PREPARE)
    monkeypatch.setenv("OLYMPUS_ENABLE_BROWSER_FINANCIAL", "1")
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    _buy_template(risk="irreversible")
    browser.set_transport_factory(
        lambda: browser.FakeTransport(present=["#buy", "#done"]))
    user = memory.current_user()
    trust.set_enabled(user, True)
    _seed_clean_runs(user, "shop.com", 30)                 # maximally trusted
    action = operator.run(user, "shop.com", "buy", {})
    # However trusted the site, an irreversible step still waits for a human.
    assert action.status == actions.PREPARED
    assert action.risk_class == actions.IRREVERSIBLE


def test_operate_feeds_feature_evolution(monkeypatch):
    from olympus import evolve
    _authorize(monkeypatch)
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    _buy_template(risk="notable")
    browser.set_transport_factory(
        lambda: browser.FakeTransport(present=["#buy", "#done"]))
    tools._browser_operate("shop.com", "buy")
    assert evolve.health("operator")["operator"]["ok_rate"] == 1.0    # OK recorded


def test_checkpoint_records_operator_failure(monkeypatch):
    from olympus import evolve
    _authorize(monkeypatch)
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    _buy_template(risk="notable")
    browser.set_transport_factory(lambda: browser.FakeTransport(
        checkpoint={"type": "captcha", "detail": "recaptcha"}))
    operator.run(memory.current_user(), "shop.com", "buy", {})
    h = evolve.health("operator")["operator"]
    assert h["fail_rate"] == 1.0 and "checkpoint:captcha" in h["last_failure"]


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


def test_json_string_params_parse_without_nameerror(monkeypatch):
    # Regression: the JSON-string argument path used `json` with no module-level
    # import, so a real JSON params/steps string raised NameError instead of
    # being parsed. Exercise all three handlers with a non-empty JSON string.
    assert tools._parse_json_arg('{"qty": 2}', {}) == {"qty": 2}
    assert tools._parse_json_arg('[{"op": "click"}]', []) == [{"op": "click"}]
    # malformed JSON must surface the friendly error, not a NameError
    _authorize(monkeypatch)
    _buy_template()
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    assert "must be a JSON object" in tools._browser_operate("shop.com", "buy",
                                                             params="{not json")
    assert "must be a JSON array" in tools._site_template_record(
        "shop.com", "t", "notable", steps="{not json")
    assert "must be a JSON object" in tools._operator_schedule(
        "job", "shop.com", "buy", "daily", params="{not json")


def test_operator_tools_threat_modeled():
    from olympus import threatmodel
    documented = threatmodel.documented_tools(
        threatmodel.doc_path().read_text(encoding="utf-8"))
    for name in ("browser_operate", "site_template_record", "operator_schedule",
                 "operator_review", "propose_site_profile"):
        assert name in tools.HANDLERS and name in documented
    assert threatmodel.check_repo() == []
