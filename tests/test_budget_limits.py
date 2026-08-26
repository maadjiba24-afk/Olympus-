"""Tests for the budget guard (BYOK bill protection) and action rate limits."""

import time

import pytest

from olympus import actions, builtin_actions, config, memory, usage, prefs  # noqa: F401


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    # point all state at a clean temp dir and clear any env/prefs budget
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(config, "DAILY_BUDGET", 0.0)
    prefs.set("shared", "daily_budget", 0.0)
    yield


# --- the budget guard ----------------------------------------------------

def _spend(amount):
    """Force today's ledger to show a given total spend."""
    import json
    day = time.strftime("%Y-%m-%d")
    path = config.MEMORY_DIR / "usage" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"__all__": {"cost": amount}}))


def test_concurrent_record_keeps_ledger_consistent_and_atomic():
    # Many concurrent record() calls must not lose an update or leave a torn
    # ledger; today_spend() reads a single consistent total, and no temp file
    # is left behind by the atomic write.
    import threading
    memory.set_user("concur")
    n = 40
    threads = [threading.Thread(target=usage.record,
                                args=("claude-opus-4-8", 1000, 500))
               for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    per_call = usage.estimate_cost("claude-opus-4-8", 1000, 500)
    assert abs(usage.today_spend() - n * per_call) < 1e-6      # no lost updates
    usage_dir = config.MEMORY_DIR / "usage"
    assert not list(usage_dir.glob(".*tmp*"))                  # no torn temp file


def test_no_budget_means_no_cap():
    _spend(1000.0)
    assert usage.daily_budget() == 0.0
    usage.check_budget()                       # must not raise
    assert usage.budget_status()["enabled"] is False


def test_budget_from_env(monkeypatch):
    monkeypatch.setattr(config, "DAILY_BUDGET", 5.0)
    prefs.set("shared", "daily_budget", None)  # no override → env wins
    assert usage.daily_budget() == 5.0


def test_set_budget_overrides_env(monkeypatch):
    monkeypatch.setattr(config, "DAILY_BUDGET", 5.0)
    usage.set_budget(2.0)
    assert usage.daily_budget() == 2.0


def test_check_budget_raises_when_reached():
    usage.set_budget(1.0)
    _spend(0.50)
    usage.check_budget()                       # under budget, fine
    _spend(1.25)
    with pytest.raises(usage.BudgetExceeded, match="Daily budget"):
        usage.check_budget()


def test_budget_status_fields():
    usage.set_budget(10.0)
    _spend(4.0)
    b = usage.budget_status()
    assert b == {"enabled": True, "limit": 10.0, "spent": 4.0,
                 "remaining": 6.0, "exceeded": False}


def test_set_budget_zero_disables():
    usage.set_budget(3.0)
    assert usage.daily_budget() == 3.0
    msg = usage.set_budget(0)
    assert "removed" in msg.lower()
    assert usage.daily_budget() == 0.0


def test_ask_returns_budget_message_when_over(monkeypatch):
    from olympus import orchestrator
    usage.set_budget(1.0)
    _spend(2.0)
    bot = orchestrator.Olympus(user="cli")
    reply = bot.ask("hello")
    assert "Daily budget" in reply                 # stopped before any model call


def test_background_routine_skips_when_over():
    from olympus import orchestrator
    usage.set_budget(1.0)
    _spend(2.0)
    out = orchestrator.opportunity_scan()
    assert "skipped" in out.lower() and "budget" in out.lower()


# --- action rate limits --------------------------------------------------

@pytest.fixture()
def reg(monkeypatch):
    monkeypatch.setattr(actions, "_REGISTRY", {})
    actions.register(actions.ActionType(
        name="t_send", risk_class=actions.IRREVERSIBLE, scope="email",
        preview=lambda p: "send", execute=lambda p: {"sent": True}))
    actions.register(actions.ActionType(
        name="t_note", risk_class=actions.TRIVIAL, scope="",
        preview=lambda p: "note", execute=lambda p: {"ok": True}))


def test_default_limits_by_risk_class(reg):
    # irreversible gets a generous runaway guard; trivial is unlimited
    assert actions.daily_limit("u", "t_send") == 50
    assert actions.daily_limit("u", "t_note") == 0


def test_set_and_clear_limit(reg):
    actions.set_limit("u", "t_send", 2)
    assert actions.daily_limit("u", "t_send") == 2
    actions.set_limit("u", "t_send", 0)
    assert actions.daily_limit("u", "t_send") == 0   # 0 = unlimited


def test_limit_blocks_after_cap_reached(reg):
    actions.grant_scope("u", "email")
    actions.set_limit("u", "t_send", 2)
    ids = []
    for _ in range(3):
        a = actions.prepare("u", "t_send", {"to": "x@y.z"})
        ids.append(a.id)
    assert actions.approve("u", ids[0]).status == actions.EXECUTED
    assert actions.approve("u", ids[1]).status == actions.EXECUTED
    blocked = actions.approve("u", ids[2])             # third trips the cap
    assert blocked.status == actions.FAILED
    assert "daily limit" in blocked.error.lower()


def test_limit_records_audit_event(reg):
    actions.grant_scope("u", "email")
    actions.set_limit("u", "t_send", 1)
    a1 = actions.prepare("u", "t_send", {"to": "x@y.z"})
    actions.approve("u", a1.id)
    a2 = actions.prepare("u", "t_send", {"to": "x@y.z"})
    actions.approve("u", a2.id)
    import json
    # The store is owner-keyed now (actions/owners/<owner_key>/), so ask
    # the module for the directory instead of hardcoding a layout.
    log = (actions._dir("u") / "audit.jsonl").read_text()
    events = [json.loads(l)["event"] for l in log.splitlines()]
    assert "blocked_rate_limit" in events


def test_rejected_drafts_do_not_count(reg):
    actions.grant_scope("u", "email")
    actions.set_limit("u", "t_send", 1)
    # rejecting drafts must not consume the daily allowance
    for _ in range(3):
        a = actions.prepare("u", "t_send", {"to": "x@y.z"})
        actions.reject("u", a.id, "no")
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    assert actions.approve("u", a.id).status == actions.EXECUTED


def test_unlimited_by_default_for_trivial(reg):
    actions.grant_scope("u", "")
    for _ in range(5):
        a = actions.prepare("u", "t_note", {})
        assert actions.approve("u", a.id).status == actions.EXECUTED
