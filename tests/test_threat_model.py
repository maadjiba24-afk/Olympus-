"""Minimal named surface + threat model (Task 6 of the moat plan).

What these prove:
1. Every exposed tool has a THREAT_MODEL.md entry, and there are no stale
   entries — the surface and its threat model are bound (enforced in CI).
2. Deny-first defaults: an ungranted action is blocked (fails closed); an
   irreversible action never auto-executes; a specialist that ingests untrusted
   content holds no direct actuators.
"""

from olympus import actions, builtin_actions, security, threatmodel  # noqa: F401
from olympus.specialists import SPECIALISTS


# --- 1. the threat model is bound to the tool surface --------------------

def test_every_exposed_tool_is_documented():
    assert threatmodel.check_repo() == []


def test_doc_lists_exactly_the_exposed_tools():
    text = threatmodel.doc_path().read_text(encoding="utf-8")
    assert threatmodel.documented_tools(text) == threatmodel.exposed_tools()


def test_check_flags_an_undocumented_tool(monkeypatch):
    real = threatmodel.exposed_tools()
    monkeypatch.setattr(threatmodel, "exposed_tools",
                        lambda: real | {"new_risky_tool"})
    problems = threatmodel.check(threatmodel.doc_path().read_text(encoding="utf-8"))
    assert any("new_risky_tool" in p and "no THREAT_MODEL" in p for p in problems)


def test_check_flags_a_stale_entry():
    problems = threatmodel.check("| `ghost_tool` | x | pure | n/a | none |")
    assert any("ghost_tool" in p and "stale" in p for p in problems)


# --- 2. deny-first defaults ----------------------------------------------

def test_ungranted_action_is_blocked():
    a = actions.prepare("u", "gmail_send",
                        {"to": "x@y.z", "subject": "Hi", "body": "yo"})
    # irreversible -> never auto-executes, regardless of autonomy
    assert a.risk_class == actions.IRREVERSIBLE
    assert not actions.can_auto_execute(a)
    # no scope -> approval fails closed
    done = actions.approve("u", a.id)
    assert done.status == actions.FAILED
    assert "not granted" in (done.error or "")


def test_granting_scope_unblocks(monkeypatch):
    # With the scope granted the same action can execute (deny-first, not
    # deny-always): proves the block is the *default*, not a dead end.
    actions.grant_scope("u2", "gmail.send")
    # gmail send hits the network; stub the gmail layer so we test the gate only.
    # Use monkeypatch so the stub is restored — a raw `gmail.send = ...` leaked
    # into every later test in the session (masked only by file ordering).
    from olympus import gmail
    sent = {}

    def fake_send(to, subject, body):
        sent["ok"] = True
        return {"id": "x"}

    monkeypatch.setattr(gmail, "send", fake_send)
    a = actions.prepare("u2", "gmail_send",
                        {"to": "x@y.z", "subject": "S", "body": "B"})
    done = actions.approve("u2", a.id)
    assert done.status == actions.EXECUTED and sent.get("ok")


def test_ingesting_specialist_has_no_direct_actuators():
    # Angelos reads untrusted email, so it must hold only prepare_action,
    # never direct actuators (capability separation).
    names = {d.get("name") for d in SPECIALISTS["angelos"].tool_defs("anthropic")}
    assert "prepare_action" in names
    assert "send_email" not in names and "call_webhook" not in names


def test_untrusted_readers_are_wrapped():
    assert security.should_wrap("read_inbox")
    assert security.should_wrap("read_email")
