"""AP2 payment-mandate user-facing flow (ADR 0002): the approval spine action
`authorize_payment` — never auto-runs, the approval signs the mandate, the
payment.mandate ABC contract gates recording, and NO money moves."""

import pytest

from olympus import actions, mandate, mandate_store, witness
from olympus import builtin_actions  # noqa: F401  (registers built-ins)

pytestmark = pytest.mark.skipif(not witness.available(),
                                reason="cryptography backend required")


def _payload(user="u", amount=12000, cap=15000, merchant="acme",
             merchants=("acme",)):
    return {"_user": user, "amount": amount, "currency": "USD",
            "merchant": merchant, "items": ["Shoe X"], "amount_cap": cap,
            "merchants": list(merchants), "item": "running shoes",
            "expires_in": 3600}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ABC", "on")
    # ensure builtins are registered on a real registry (module import did it)
    actions.grant_scope("u", "payment.authorize")


# --- never auto-runs ------------------------------------------------------

def test_authorize_payment_is_financial_legal_and_never_auto_runs():
    at = actions.registered()["authorize_payment"]
    assert at.risk_class == actions.FINANCIAL_LEGAL
    assert actions._min_level_to_auto(actions.FINANCIAL_LEGAL) == 99
    # even at max autonomy the action cannot auto-execute
    actions.set_autonomy("u", actions.L4_STANDING)
    a = actions.prepare("u", "authorize_payment", _payload())
    assert actions.can_auto_execute(a) is False


def test_prepare_does_not_move_money_or_record():
    a = actions.prepare("u", "authorize_payment", _payload())
    assert a.status == actions.PREPARED
    assert "does NOT move money" in a.preview
    assert mandate_store.records("u") == []       # nothing recorded yet


# --- the happy path: approval signs + records -----------------------------

def test_approval_signs_verifies_and_records_no_money():
    a = actions.prepare("u", "authorize_payment", _payload())
    done = actions.approve("u", a.id)
    assert done.status == actions.EXECUTED
    assert done.result["moved_money"] is False
    assert done.result["recorded"] is True
    recs = mandate_store.records("u")
    assert len(recs) == 1
    assert recs[0]["amount"] == 12000 and recs[0]["merchant"] == "acme"
    assert recs[0]["moved_money"] is False
    # the cart nonce is now consumed (replay defense)
    assert recs[0]["nonce"] in mandate_store.consumed_nonces("u")


# --- the contract gates recording -----------------------------------------

def test_over_cap_cart_fails_closed_and_records_nothing():
    a = actions.prepare("u", "authorize_payment",
                        _payload(amount=99999, cap=15000))
    done = actions.approve("u", a.id)
    assert done.status == actions.FAILED
    assert "contract" in done.error.lower()
    assert mandate_store.records("u") == []       # blocked before recording


def test_wrong_merchant_fails_closed():
    a = actions.prepare("u", "authorize_payment",
                        _payload(merchant="evilcorp", merchants=("acme",)))
    done = actions.approve("u", a.id)
    assert done.status == actions.FAILED
    assert mandate_store.records("u") == []


def test_bad_amount_input_is_rejected():
    a = actions.prepare("u", "authorize_payment", _payload(amount=-5))
    done = actions.approve("u", a.id)
    assert done.status == actions.FAILED          # mandate.create_cart validates


# --- store: append-only, bounded, replay-safe -----------------------------

def test_store_is_append_only_across_authorizations():
    for _ in range(3):
        a = actions.prepare("u", "authorize_payment", _payload())
        actions.approve("u", a.id)
    assert len(mandate_store.records("u")) == 3
    nonces = mandate_store.consumed_nonces("u")
    assert len({r["nonce"] for r in mandate_store.records("u")}) == 3
    assert all(r["nonce"] in nonces for r in mandate_store.records("u"))


def test_store_corrupt_blob_starts_empty(monkeypatch):
    from olympus import store, memory
    store.backend().put("mandates", memory.safe_id("v"), b"{not json")
    assert mandate_store.records("v") == []
    assert mandate_store.consumed_nonces("v") == set()


def test_preview_shows_bounded_authorization():
    a = actions.prepare("u", "authorize_payment", _payload())
    assert "120.00 USD" in a.preview          # amount, formatted from minor units
    assert "150.00 USD" in a.preview          # the cap
    assert "acme" in a.preview
