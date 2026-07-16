"""FINAL milestone — AP2 mandate co-signature, capability binding, scope.

The Phase-2 adversarial tests named in the milestone-4 addendum of
docs/AP2_THREAT_MODEL.md. A mandate is now a TWO-PARTY, capability-bound,
scope-explicit authorization: exercisable only with both the system signature
and the user co-signature, within the capability token it is bound to, and never
auto-executing.
"""

import pytest

from olympus import (behavioral_contracts as abc, config, identity, mandate,
                     witness)

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "m4-mandate-seed")


def _intent(cap=15000):
    return mandate.create_intent(
        "alice", amount_cap=cap, currency="USD", merchants=["acme"],
        item="widgets", expires_in=3600.0, trusted=True, now=1000.0)


def _cart(intent, *, amount=10000, jti=""):
    return mandate.create_cart(
        intent, amount=amount, currency="USD", merchant="acme",
        items=["widget"], capability_jti=jti, now=1000.0)


def _grant(*, scopes=(mandate.PAYMENT_SCOPE,),
           risk=mandate.FINANCIAL_LEGAL, ttl=3600.0):
    return identity.grant("alice", scopes=list(scopes), risk_ceiling=risk,
                          ttl=ttl)


# --- T7: dual-signature required ------------------------------------------

@_signing
def test_cosigned_mandate_verifies():
    im = _intent()
    signed = mandate.co_sign(mandate.sign(_cart(im)))
    assert signed.cosigned and mandate.user_signature_ok(signed)
    res = mandate.verify(signed, intent=im, now=1000.0, require_cosignature=True)
    assert res.ok


@_signing
def test_system_only_mandate_refused_when_cosignature_required():
    im = _intent()
    signed = mandate.sign(_cart(im))                 # NOT co-signed
    res = mandate.verify(signed, intent=im, now=1000.0, require_cosignature=True)
    assert not res.ok and any("co-signature" in r for r in res.reasons)
    # ...but still accepted when co-signature isn't required (backward compat).
    assert mandate.verify(signed, intent=im, now=1000.0).ok


@_signing
def test_forged_user_cosignature_refused():
    signed = mandate.co_sign(mandate.sign(_cart(_intent())))
    forged = mandate.SignedMandate(
        kind=signed.kind, payload=signed.payload, public_key=signed.public_key,
        signature=signed.signature, user_public_key=signed.user_public_key,
        user_signature="ab" * 32)                    # bogus co-signature
    assert not mandate.user_signature_ok(forged)
    assert not mandate.verify(forged, intent=_intent(), now=1000.0,
                              require_cosignature=True).ok


@_signing
def test_cosignature_bound_to_transaction_cannot_be_lifted():
    # A valid co-signature lifted onto a mandate with any changed field fails.
    signed = mandate.co_sign(mandate.sign(_cart(_intent())))
    tampered_payload = {**signed.payload, "amount": 99999}   # raise the amount
    lifted = mandate.SignedMandate(
        kind="cart", payload=tampered_payload, public_key=signed.public_key,
        signature=signed.signature, user_public_key=signed.user_public_key,
        user_signature=signed.user_signature)
    assert not mandate.user_signature_ok(lifted)     # co-sig no longer matches
    assert not mandate.signature_ok(lifted)          # system sig no longer matches either


# --- T8: capability containment -------------------------------------------

@_signing
def test_capability_bound_mandate_within_grant_passes():
    im = _intent()
    tok = _grant()
    jti = tok["payload"]["jti"]
    signed = mandate.co_sign(mandate.sign(_cart(im, jti=jti)))
    assert mandate.capability_ok(signed, tok, now=1000.0)
    mandate.enforce_commit(signed, im, capability=tok, now=1000.0)  # no raise


@_signing
def test_capability_scope_escalation_refused():
    # A token that does NOT grant the payment scope can't back a payment mandate.
    tok = _grant(scopes=["browser.click"])
    jti = tok["payload"]["jti"]
    signed = mandate.co_sign(mandate.sign(_cart(_intent(), jti=jti)))
    assert not mandate.capability_ok(signed, tok, now=1000.0)
    with pytest.raises(abc.ContractViolation) as ei:
        mandate.enforce_commit(signed, _intent(), capability=tok, now=1000.0)
    assert ei.value.contract == "payment_mandate"


@_signing
def test_capability_risk_below_financial_refused():
    tok = _grant(risk="reversible_notable")          # ceiling below FINANCIAL_LEGAL
    jti = tok["payload"]["jti"]
    signed = mandate.co_sign(mandate.sign(_cart(_intent(), jti=jti)))
    assert not mandate.capability_ok(signed, tok, now=1000.0)


@_signing
def test_revoked_capability_refused():
    tok = _grant()
    jti = tok["payload"]["jti"]
    signed = mandate.co_sign(mandate.sign(_cart(_intent(), jti=jti)))
    assert mandate.capability_ok(signed, tok, now=1000.0)
    identity.revoke(jti)
    assert not mandate.capability_ok(signed, tok, now=1000.0)     # kill switch
    with pytest.raises(abc.ContractViolation):
        mandate.enforce_commit(signed, _intent(), capability=tok, now=1000.0)


@_signing
def test_wrong_token_presented_refused():
    bound = _grant()
    other = _grant()
    signed = mandate.co_sign(mandate.sign(
        _cart(_intent(), jti=bound["payload"]["jti"])))
    # Presenting a DIFFERENT (valid) token than the one bound is refused.
    assert not mandate.capability_ok(signed, other, now=1000.0)


@_signing
def test_expired_capability_refused():
    tok = _grant(ttl=1.0)
    jti = tok["payload"]["jti"]
    signed = mandate.co_sign(mandate.sign(_cart(_intent(), jti=jti)))
    exp = tok["payload"]["expires_at"] + 1
    assert not mandate.capability_ok(signed, tok, now=exp)


# --- T9: display / sign parity --------------------------------------------

@_signing
def test_transaction_scope_derived_from_signed_payload():
    tok = _grant()
    signed = mandate.co_sign(mandate.sign(
        _cart(_intent(), jti=tok["payload"]["jti"])))
    scope = mandate.transaction_scope(signed)
    # The human-visible scope is the exact signed payload — no divergent copy.
    assert scope["amount"] == signed.payload["amount"]
    assert scope["merchant"] == signed.payload["merchant"]
    assert scope["capability_jti"] == signed.payload["capability_jti"]
    assert scope["risk_class"] == mandate.FINANCIAL_LEGAL
    assert scope["auto_executable"] is False


# --- escalation (unchanged) + round trip ----------------------------------

def test_no_autonomy_level_auto_executes():
    assert mandate.can_auto_execute() is False
    assert mandate.risk_class() == mandate.FINANCIAL_LEGAL


@_signing
def test_signed_mandate_roundtrips_with_cosignature():
    signed = mandate.co_sign(mandate.sign(_cart(_intent())))
    back = mandate.SignedMandate.from_dict(signed.to_dict())
    assert back.user_signature == signed.user_signature
    assert mandate.user_signature_ok(back)


@_signing
def test_unbound_cart_payload_is_backward_compatible():
    # A cart with no capability binding produces the identical canonical payload
    # (and signature) it did before M4 — the binding key is omitted when empty.
    im = _intent()
    unbound = _cart(im)
    assert "capability_jti" not in unbound.payload()
    assert mandate.verify(mandate.sign(unbound), intent=im, now=1000.0).ok
