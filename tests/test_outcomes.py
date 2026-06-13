"""Tests for outcome tracking and the growth-insight loop."""

import pytest

from olympus import outcomes, actions, builtin_actions, config, store  # noqa: F401


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    store.reset()
    yield
    store.reset()


@pytest.fixture()
def reg(monkeypatch):
    monkeypatch.setattr(actions, "_REGISTRY", {})
    actions.register(actions.ActionType(
        name="t_note", risk_class=actions.TRIVIAL, scope="notes",
        preview=lambda p: "note", execute=lambda p: {"ok": True},
        undo=lambda r: "deleted"))


# --- the ledger ----------------------------------------------------------

def test_record_and_stats():
    for o in (outcomes.APPROVED, outcomes.APPROVED, outcomes.REJECTED):
        outcomes.record("u", "send_email", o)
    s = outcomes.stats("u")
    assert s["overall"]["total"] == 3
    assert s["by_ref"]["send_email"]["approved"] == 2
    assert s["by_ref"]["send_email"]["rejected"] == 1
    assert s["by_ref"]["send_email"]["approve_rate"] == round(2/3, 2)


def test_per_user():
    outcomes.record("alice", "x", outcomes.APPROVED)
    assert outcomes.stats("bob")["overall"]["total"] == 0


def test_insights_only_above_sample_floor():
    # 4 events, all friction → still below the 5-sample floor → no insight
    for _ in range(4):
        outcomes.record("u", "send_email", outcomes.REJECTED)
    assert outcomes.insights("u") == []
    outcomes.record("u", "send_email", outcomes.REJECTED)   # now 5
    ins = outcomes.insights("u")
    assert len(ins) == 1 and ins[0]["ref"] == "send_email"


def test_no_insight_when_mostly_approved():
    for _ in range(8):
        outcomes.record("u", "send_email", outcomes.APPROVED)
    outcomes.record("u", "send_email", outcomes.REJECTED)
    assert outcomes.insights("u") == []                     # low friction


# --- wired into the action lifecycle ------------------------------------

def test_approve_records_approved(reg):
    actions.grant_scope("u", "notes")
    a = actions.prepare("u", "t_note", {"_user": "u"})
    actions.approve("u", a.id)
    assert outcomes.stats("u")["by_ref"]["t_note"]["approved"] == 1


def test_edit_then_approve_records_after_edit(reg):
    actions.grant_scope("u", "notes")
    a = actions.prepare("u", "t_note", {"_user": "u"})
    actions.edit("u", a.id, {"body": "fixed"})
    actions.approve("u", a.id)
    s = outcomes.stats("u")["by_ref"]["t_note"]
    assert s["approved_after_edit"] == 1 and s["approved"] == 0


def test_reject_records_rejected(reg):
    a = actions.prepare("u", "t_note", {"_user": "u"})
    actions.reject("u", a.id, "no")
    assert outcomes.stats("u")["by_ref"]["t_note"]["rejected"] == 1


def test_blocked_action_records_nothing(reg):
    # no scope granted → approve fails → no outcome (it never ran)
    a = actions.prepare("u", "t_note", {"_user": "u"})
    actions.approve("u", a.id)
    assert outcomes.stats("u")["overall"]["total"] == 0
