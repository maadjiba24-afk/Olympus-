"""Federation CLI surface — the commands that make federation usable property."""

import json

import pytest

from olympus import cli, federation, store, witness


@pytest.fixture(autouse=True)
def _fresh_store():
    store.reset()
    yield
    store.reset()


needs_crypto = pytest.mark.skipif(not witness.available(),
                                  reason="cryptography backend required")


def _run(argv, capsys):
    rc = cli.main(argv)
    return (rc or 0), capsys.readouterr().out


# --- registration --------------------------------------------------------

def test_federation_is_a_registered_command():
    assert "federation" in cli.command_names()


# --- identity ------------------------------------------------------------

@needs_crypto
def test_identity_prints_card(capsys):
    rc, out = _run(["federation", "identity"], capsys)
    assert rc == 0
    card = json.loads(out)
    assert card["publicKey"] == federation.public_key()
    assert card["protocol"] == federation.PROTOCOL


# --- peer lifecycle ------------------------------------------------------

def test_add_list_remove_peer(capsys):
    _, out = _run(["federation", "add-peer", "alice", "aa11bb22",
                   "--url", "https://a.example", "--trust", "trusted"], capsys)
    assert "Pinned peer 'alice'" in out and "trust=trusted" in out

    _, out = _run(["federation", "peers"], capsys)
    assert "alice" in out and "https://a.example" in out

    _, out = _run(["federation", "remove-peer", "alice"], capsys)
    assert "Removed." in out

    _, out = _run(["federation", "peers"], capsys)
    assert "No peers pinned." in out


def test_add_peer_usage_when_incomplete(capsys):
    _, out = _run(["federation", "add-peer", "alice"], capsys)   # no pubkey
    assert "Usage:" in out


def test_remove_missing_peer(capsys):
    _, out = _run(["federation", "remove-peer", "ghost"], capsys)
    assert "No such peer." in out


# --- call (governed; refuses when the feature is off) --------------------

def test_call_refused_when_disabled(monkeypatch, capsys):
    monkeypatch.delenv("OLYMPUS_FEDERATION", raising=False)
    federation.add_peer("peer", "kk", url="https://p.example", trust="task")
    _, out = _run(["federation", "call", "peer", "hello"], capsys)
    assert "Refused:" in out and "disabled" in out


def test_call_usage_when_incomplete(capsys):
    _, out = _run(["federation", "call", "peer"], capsys)         # no message
    assert "Usage:" in out


# --- lessons -------------------------------------------------------------

def test_lessons_empty(capsys):
    _, out = _run(["federation", "lessons"], capsys)
    assert "No staged peer lessons." in out


@needs_crypto
def test_lessons_lists_staged(capsys):
    federation.add_peer("peer", federation.public_key(), trust="trusted")
    federation.import_lessons(federation.export_lessons(["double-check dates"]))
    _, out = _run(["federation", "lessons"], capsys)
    assert "double-check dates" in out and "[peer]" in out


# --- capability discovery + multi-peer aggregation -----------------------

def test_capabilities_usage_when_incomplete(capsys):
    _, out = _run(["federation", "capabilities"], capsys)         # no peer
    assert "Usage:" in out


def test_capabilities_refused_when_disabled(monkeypatch, capsys):
    monkeypatch.delenv("OLYMPUS_FEDERATION", raising=False)
    federation.add_peer("peer", "kk", url="https://p.example", trust="task")
    _, out = _run(["federation", "capabilities", "peer"], capsys)
    assert "Refused:" in out and "disabled" in out


def test_ask_all_usage_when_incomplete(capsys):
    _, out = _run(["federation", "ask-all"], capsys)              # no message
    assert "Usage:" in out


def test_ask_all_no_peers(monkeypatch, capsys):
    monkeypatch.setenv("OLYMPUS_FEDERATION", "1")
    _, out = _run(["federation", "ask-all", "a question"], capsys)
    assert "No pinned peers" in out
