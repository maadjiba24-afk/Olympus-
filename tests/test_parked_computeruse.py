"""Parked-item 5 — OS-level computer use, GATED and SANDBOXED.

The most powerful capability, built as safety rails: default-off, a disabled
actuator that refuses everything, every action on the approval spine at
IRREVERSIBLE risk (never auto-executes), a fail-closed `computer.use` ABC
contract (cmdguard + secret scan), and a signed audit of every actuation.
Nothing drives the OS unless an operator both enables it AND installs an
actuator.
"""

import pytest

from olympus import (actions, behavioral_contracts as abc, computeruse, config,
                     deltas, witness)

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "parked5-seed")
    monkeypatch.delenv("OLYMPUS_COMPUTER_USE", raising=False)
    monkeypatch.setattr(deltas, "_locks", {})
    computeruse.reset_actuator()
    yield
    computeruse.reset_actuator()


class _FakeActuator(computeruse.Actuator):
    name = "fake"

    def __init__(self):
        self.calls = []

    def screenshot(self):
        self.calls.append(("screenshot",)); return {"ok": True, "image": "b64…"}

    def move(self, x, y):
        self.calls.append(("move", x, y)); return {"ok": True}

    def click(self, x, y, button="left"):
        self.calls.append(("click", x, y, button)); return {"ok": True}

    def type(self, text):
        self.calls.append(("type", text)); return {"ok": True}

    def key(self, keys):
        self.calls.append(("key", keys)); return {"ok": True}

    def launch(self, command):
        self.calls.append(("launch", command)); return {"ok": True, "pid": 42}


# --- default off / fail closed ---------------------------------------------

def test_disabled_by_default():
    assert not computeruse.enabled()
    with pytest.raises(computeruse.ComputerUseError):
        computeruse.perform("computer_click", {"x": 1, "y": 2})


def test_default_actuator_refuses_even_when_enabled(monkeypatch):
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    assert computeruse.enabled()
    # Enabled but no actuator installed → the contract blocks (actuator not ready).
    with pytest.raises(abc.ContractViolation):
        computeruse.perform("computer_click", {"x": 1, "y": 2})


# --- enabled + actuator installed: it works, and is audited ----------------

@_signing
def test_enabled_with_actuator_performs_and_audits(monkeypatch):
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    act = _FakeActuator()
    res = computeruse.perform("computer_click", {"x": 10, "y": 20},
                              user="alice", actuator=act, approved=True)
    assert res["ok"] and act.calls == [("click", 10, 20, "left")]
    # Signed audit records the INTENT and the OUTCOME; the history verifies.
    events = [s["state"]["event"] for s in computeruse.audit("alice")]
    assert events == ["attempt", "performed"]
    assert deltas.verify_history("computeruse:alice")["ok"]


@_signing
def test_actuation_without_approval_is_refused(monkeypatch):
    # THE hardening: the chokepoint self-enforces approval — a direct perform()
    # with no approval evidence is blocked even when enabled + actuator present.
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    act = _FakeActuator()
    with pytest.raises(abc.ContractViolation):
        computeruse.perform("computer_click", {"x": 1, "y": 2}, user="zed",
                            actuator=act)                # approved defaults False
    assert act.calls == []                               # never actuated
    # And the blocked attempt is on the signed audit.
    events = [s["state"]["event"] for s in computeruse.audit("zed")]
    assert "attempt" in events and "blocked" in events


@_signing
def test_typed_secret_is_not_stored_in_the_audit(monkeypatch):
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    computeruse.perform("computer_type", {"text": "hello world 12345"},
                        user="bob", actuator=_FakeActuator(), approved=True)
    rec = computeruse.audit("bob")[-1]["state"]
    # Typed text is recorded by LENGTH only — never the content.
    assert rec["text_len"] == len("hello world 12345")
    assert "hello world" not in str(rec)


@_signing
def test_launch_command_args_are_redacted_in_audit(monkeypatch):
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    computeruse.perform("computer_launch", {"command": "mysql -psupersecret db"},
                        user="cmdlog", actuator=_FakeActuator(), approved=True)
    rec = computeruse.audit("cmdlog")[-1]["state"]
    # The executable is kept; the (secret-bearing) args are NOT stored verbatim.
    assert rec["exec"] == "mysql" and rec["argc"] == 2 and "command_hash" in rec
    assert "supersecret" not in str(rec)


# --- the computer.use contract: cmdguard + secret scan ---------------------

def test_launch_of_denied_command_is_blocked(monkeypatch):
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    with pytest.raises(abc.ContractViolation):
        computeruse.perform("computer_launch", {"command": "rm -rf /"},
                            actuator=_FakeActuator(), approved=True)


def test_typing_a_secret_is_refused(monkeypatch):
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    from olympus import security
    monkeypatch.setattr(security, "secret_exfil_reason",
                        lambda text, user=None: "matches a held API key"
                        if "sk-secret" in text else None)
    with pytest.raises(abc.ContractViolation):
        computeruse.perform("computer_type", {"text": "sk-secret-abc"},
                            actuator=_FakeActuator(), approved=True)
    # A benign string still types fine.
    assert computeruse.perform("computer_type", {"text": "hello"},
                               actuator=_FakeActuator(), approved=True)["ok"]


def test_contract_enabled_actuator_and_approval_predicates():
    ok_ctx = {"computer_use_enabled": True, "computer_actuator_ready": True,
              "computer_action_approved": True}
    assert abc.check("computer.use", ok_ctx).ok
    # Each of the three preconditions/governance missing blocks (recovery block).
    for miss in ("computer_use_enabled", "computer_actuator_ready",
                 "computer_action_approved"):
        v = abc.check("computer.use", {**ok_ctx, miss: False})
        assert v.blocking and v.recovery == abc.BLOCK


# --- approval spine: registered, irreversible, never auto ------------------

def test_actions_registered_on_the_spine():
    reg = actions.registered()
    for name in ("computer_screenshot", "computer_click", "computer_type",
                 "computer_key", "computer_move", "computer_launch"):
        assert name in reg and reg[name].scope == "computer.use"
    # ALL computer-use actions are IRREVERSIBLE — including screenshot (it can
    # capture on-screen secrets) — so none can ever auto-execute.
    for name in reg:
        if name.startswith("computer_"):
            assert reg[name].risk_class == actions.IRREVERSIBLE


def test_computer_action_never_auto_executes():
    a = actions.prepare("carol", "computer_click", {"x": 1, "y": 1})
    # Irreversible ⇒ needs level 99 ⇒ never auto (no scope granted either).
    assert actions.can_auto_execute(a) is False
    assert a.risk_class == actions.IRREVERSIBLE


def test_unknown_action_rejected(monkeypatch):
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    with pytest.raises(computeruse.ComputerUseError):
        computeruse.perform("computer_mind_control", {},
                            actuator=_FakeActuator(), approved=True)


@_signing
def test_key_action_secret_is_refused(monkeypatch):
    # H2: a `key` action can enter text too — a secret keyed one field at a time
    # must be refused just like a typed one.
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    from olympus import security
    monkeypatch.setattr(security, "secret_exfil_reason",
                        lambda text, user=None: "matches a held API key"
                        if "sk-secret" in str(text) else None)
    with pytest.raises(abc.ContractViolation):
        computeruse.perform("computer_key", {"keys": "sk-secret-abc"},
                            actuator=_FakeActuator(), approved=True)


@_signing
def test_key_audit_redacts_non_keyname_content(monkeypatch):
    # A legit key chord is kept for forensics; anything else is length-only.
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    for i, (keys, user) in enumerate([
            ("ctrl+c", "k1"), ("cmd+shift+4", "kc"), ("F5", "kf"),
            ("Return", "kr"), ("a", "ka")]):
        computeruse.perform("computer_key", {"keys": keys}, user=user,
                            actuator=_FakeActuator(), approved=True)
        assert computeruse.audit(user)[-1]["state"].get("keys") == keys
    # A pasted blob AND a short alphanumeric secret (which a permissive regex
    # would wrongly keep) are BOTH stored by length only, never verbatim.
    for keys, user in [("hunter2-swordfish-longsecret", "k2"),
                       ("sk12AB34cd56EF78", "k3")]:      # 16-char alnum secret
        computeruse.perform("computer_key", {"keys": keys}, user=user,
                            actuator=_FakeActuator(), approved=True)
        st = computeruse.audit(user)[-1]["state"]
        assert "keys" not in st and st["keys_len"] == len(keys)
        assert keys not in str(st)


@_signing
def test_attempt_audit_failure_fails_closed(monkeypatch):
    # H2: if the pre-actuation 'attempt' record can't be signed, the actuation
    # is REFUSED before it touches the OS — the audit never fails open.
    monkeypatch.setenv("OLYMPUS_COMPUTER_USE", "1")
    act = _FakeActuator()

    def _boom(*a, **k):
        raise RuntimeError("ledger write failed")
    monkeypatch.setattr(deltas, "record_snapshot", _boom)
    with pytest.raises(computeruse.ComputerUseError):
        computeruse.perform("computer_click", {"x": 1, "y": 2},
                            actuator=act, approved=True)
    assert act.calls == []                 # never actuated — failed closed


def test_no_gui_dependency_imported():
    import inspect
    src = inspect.getsource(computeruse)
    for forbidden in ("import pyautogui", "import Xlib", "from Xlib",
                      "import pynput"):
        assert forbidden not in src, f"core must not import {forbidden!r}"
