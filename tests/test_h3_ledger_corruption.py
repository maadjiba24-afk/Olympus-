"""H3 — trust root + ledgers hardening: a corrupt (unparseable) ledger line is
a FAIL-CLOSED verdict, never an uncaught exception.

The signed append-only logs (deltas snapshots, checkpoint chains, speculation
records) are tamper-evident against an attacker with filesystem write access.
Before H3, a syntactically corrupt line made the readers raise a bare
JSONDecodeError, so verify_history / verify_ledger / verify_speculation crashed
instead of returning their documented {ok: False} verdict — and a corrupt
payment ledger crashed a charge instead of cleanly refusing. These tests pin
the fail-closed behavior.
"""

import json

import pytest

from olympus import (config, deltas, identity, ledger, mandate, payrail,
                     witness)

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "h3-corruption-seed")
    monkeypatch.delenv("OLYMPUS_PAYMENT_LIVE", raising=False)
    monkeypatch.delenv("OLYMPUS_MANDATE_USER_PUBKEY", raising=False)
    monkeypatch.setattr(deltas, "_locks", {})
    monkeypatch.setattr(ledger, "_locks", {})
    mandate.reset_user_signer()
    yield
    mandate.reset_user_signer()


def _corrupt_last_line(path):
    text = path.read_text()
    path.write_text(text + "{ this is not valid json\n")


# --- deltas ------------------------------------------------------------------

@_signing
def test_deltas_verify_history_fails_closed_on_corrupt_line():
    tid = "playbook:c1"
    deltas.record_snapshot(tid, kind="playbook", state={"v": 1})
    _corrupt_last_line(deltas._path(tid))
    v = deltas.verify_history(tid)
    # A verdict, not an exception — and it is NOT ok.
    assert v["ok"] is False and v["problems"]
    assert any("corrupt" in p for p in v["problems"])


@_signing
def test_deltas_snapshots_raises_typed_error_on_corruption():
    tid = "playbook:c2"
    deltas.record_snapshot(tid, kind="playbook", state={"v": 1})
    _corrupt_last_line(deltas._path(tid))
    with pytest.raises(deltas.DeltaError):
        deltas.snapshots(tid)


@_signing
def test_deltas_record_refuses_to_extend_a_corrupt_history():
    tid = "playbook:c3"
    deltas.record_snapshot(tid, kind="playbook", state={"v": 1})
    _corrupt_last_line(deltas._path(tid))
    # Appending on top of a corrupt history would extend a broken chain — refuse.
    with pytest.raises(deltas.DeltaError):
        deltas.record_snapshot(tid, kind="playbook", state={"v": 2})


# --- ledger (checkpoints + speculation) --------------------------------------

@_signing
def test_ledger_verify_fails_closed_on_corrupt_middle_line():
    led = ledger.open_run("run-c1")
    led.append({"op": "a"}, {"s": 1})
    led.append({"op": "b"}, {"s": 2})
    # Corrupt a MIDDLE line (not the tolerated torn-final line).
    p = ledger._path("run-c1")
    lines = p.read_text().splitlines()
    lines[0] = "{ broken"
    p.write_text("\n".join(lines) + "\n")
    v = ledger.verify_ledger("run-c1")
    assert v["ok"] is False and v["problems"]


@_signing
def test_speculation_verify_fails_closed_on_corrupt_line():
    ledger.record_speculation("run-c2", "branch", {"why": "explore"})
    _corrupt_last_line(ledger._spec_path("run-c2"))
    v = ledger.verify_speculation("run-c2")
    assert v["ok"] is False and v["problems"]
    with pytest.raises(ledger.LedgerError):
        ledger.speculation_records("run-c2")


# --- the money path must cleanly refuse, not crash ---------------------------

@_signing
def test_charge_cleanly_refuses_on_corrupt_payment_ledger():
    im = mandate.create_intent("zoe", amount_cap=15000, currency="USD",
                               merchants=["acme"], item="widgets",
                               trusted=True, now=1000.0)
    cart = mandate.create_cart(im, amount=5000, currency="USD", merchant="acme",
                               items=["widget"], now=1000.0)
    m = mandate.co_sign(mandate.sign(cart))
    payrail.charge(m, im, user="zoe", now=1000.0)          # one real charge
    _corrupt_last_line(deltas._path("payment:zoe"))
    # A corrupt payment ledger must produce a clean PaymentError refusal, NOT an
    # uncaught JSONDecodeError.
    cart2 = mandate.create_cart(im, amount=1000, currency="USD", merchant="acme",
                                items=["widget"], now=1000.0)
    m2 = mandate.co_sign(mandate.sign(cart2))
    with pytest.raises(payrail.PaymentError):
        payrail.charge(m2, im, user="zoe", now=1000.0)
