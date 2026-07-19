"""AP2 payment mandates: creation + verification only (no rail). Includes the
Phase-2 adversarial cases — spoofing, construction-injection, replay, escalation.

Signing needs the Ed25519 backend (`cryptography`), present in the test env."""

import pytest

from olympus import mandate, witness, behavioral_contracts as abc

pytestmark = pytest.mark.skipif(not witness.available(),
                                reason="cryptography backend required")


def _intent(now=1000.0, trusted=True, cap=15000, merchants=("acme",)):
    return mandate.create_intent(
        "u", amount_cap=cap, currency="USD", merchants=list(merchants),
        item="running shoes", expires_in=3600, trusted=trusted, now=now)


def _cart(intent, now=1000.0, amount=12000, merchant="acme", currency="USD"):
    return mandate.create_cart(intent, amount=amount, currency=currency,
                               merchant=merchant, items=["Shoe X"], now=now)


# --- creation validation --------------------------------------------------

def test_intent_validates_fields():
    with pytest.raises(mandate.MandateError):
        mandate.create_intent("u", amount_cap=0, currency="USD",
                              merchants=["acme"], item="x", trusted=True)
    with pytest.raises(mandate.MandateError):
        mandate.create_intent("u", amount_cap=100, currency="ZZZ",
                              merchants=["acme"], item="x", trusted=True)
    with pytest.raises(mandate.MandateError):
        mandate.create_intent("u", amount_cap=100, currency="USD",
                              merchants=[], item="x", trusted=True)
    with pytest.raises(mandate.MandateError):
        mandate.create_intent("u", amount_cap=100, currency="USD",
                              merchants=["acme"], item="", trusted=True)


def test_amount_cap_rejects_bool_and_float():
    with pytest.raises(mandate.MandateError):
        mandate.create_intent("u", amount_cap=True, currency="USD",
                              merchants=["acme"], item="x", trusted=True)


# --- happy path -----------------------------------------------------------

def test_sign_and_verify_intent():
    im = _intent()
    signed = mandate.sign(im)
    r = mandate.verify(signed, now=1000.0)
    assert r.ok, r.reasons


def test_sign_and_verify_cart_within_intent():
    im = _intent()
    cm = _cart(im)
    signed = mandate.sign(cm)
    r = mandate.verify(signed, intent=im, now=1000.0)
    assert r.ok, r.reasons


# --- T1: spoofing ---------------------------------------------------------

def test_unsigned_or_tampered_signature_fails():
    im = _intent()
    signed = mandate.sign(im)
    forged = mandate.SignedMandate(kind="intent", payload=signed.payload,
                                   public_key=signed.public_key,
                                   signature="00" * 64)
    assert not mandate.verify(forged, now=1000.0).ok


def test_tampering_a_field_invalidates_signature():
    im = _intent()
    signed = mandate.sign(im)
    tampered_payload = dict(signed.payload)
    tampered_payload["amount_cap"] = 999999          # raise the cap post-signing
    tampered = mandate.SignedMandate(kind="intent", payload=tampered_payload,
                                     public_key=signed.public_key,
                                     signature=signed.signature)
    assert not mandate.signature_ok(tampered)
    assert "invalid signature" in mandate.verify(tampered, now=1000.0).reasons


def test_wrong_public_key_is_rejected():
    im = _intent()
    signed = mandate.sign(im)
    wrong = mandate.SignedMandate(kind="intent", payload=signed.payload,
                                  public_key="ab" * 32, signature=signed.signature)
    assert not mandate.signature_ok(wrong)


# --- T2: construction-injection ------------------------------------------

def test_untrusted_intent_cannot_be_signed():
    im = _intent(trusted=False)             # constraints from untrusted content
    with pytest.raises(mandate.MandateError):
        mandate.sign(im)


def test_cart_exceeding_cap_is_not_contained():
    im = _intent(cap=15000)
    cm = _cart(im, amount=20000)            # injection inflates the cart
    signed = mandate.sign(cm)
    r = mandate.verify(signed, intent=im, now=1000.0)
    assert not r.ok
    assert any("exceeds cap" in x for x in r.reasons)


def test_cart_wrong_merchant_is_not_contained():
    im = _intent(merchants=("acme",))
    cm = _cart(im, merchant="evilcorp")
    r = mandate.verify(mandate.sign(cm), intent=im, now=1000.0)
    assert any("not in allowlist" in x for x in r.reasons)


# --- T3: replay + expiry --------------------------------------------------

def test_nonce_replay_is_rejected():
    im = _intent()
    signed = mandate.sign(im)
    seen: set[str] = set()
    assert mandate.verify(signed, now=1000.0, seen_nonces=seen).ok
    # second use of the same nonce → replay
    assert not mandate.verify(signed, now=1000.0, seen_nonces=seen).ok


def test_expired_intent_is_rejected():
    im = _intent(now=1000.0)               # expires at 4600
    signed = mandate.sign(im)
    assert not mandate.verify(signed, now=9999.0).ok


# --- T5: autonomy escalation ---------------------------------------------

def test_mandate_never_auto_executes():
    assert mandate.risk_class() == mandate.FINANCIAL_LEGAL
    assert mandate.can_auto_execute() is False


def test_financial_legal_never_auto_runs_in_actions():
    from olympus import actions
    assert actions._min_level_to_auto(actions.FINANCIAL_LEGAL) == 99


# --- ABC governance -------------------------------------------------------

def test_enforce_commit_passes_for_valid_cart():
    im = _intent()
    cm = _cart(im)
    signed = mandate.co_sign(mandate.sign(cm))   # M4: co-signature now required
    seen: set[str] = set()
    res = mandate.enforce_commit(signed, im, now=1000.0, seen_nonces=seen)
    assert res.ok


def test_enforce_commit_blocks_over_cap_cart():
    im = _intent(cap=15000)
    cm = _cart(im, amount=99999)
    signed = mandate.sign(cm)
    with pytest.raises(abc.ContractViolation) as ei:
        mandate.enforce_commit(signed, im, now=1000.0)
    assert ei.value.contract == "payment_mandate"


def test_enforce_commit_blocks_untrusted_intent():
    # an intent flagged untrusted can't even be signed; simulate a cart whose
    # intent is untrusted by constructing the ctx path via enforce_commit
    im_trusted = _intent(trusted=True)
    cm = _cart(im_trusted)
    signed = mandate.sign(cm)
    # forge an intent object that claims untrusted construction
    im_untrusted = mandate.IntentMandate(
        **{**im_trusted.__dict__, "trusted": False})
    with pytest.raises(abc.ContractViolation):
        mandate.enforce_commit(signed, im_untrusted, now=1000.0)


def test_enforce_commit_blocks_replay():
    im = _intent()
    cm = _cart(im)
    signed = mandate.sign(cm)
    seen = {str(signed.payload["nonce"])}     # nonce already consumed
    with pytest.raises(abc.ContractViolation):
        mandate.enforce_commit(signed, im, now=1000.0, seen_nonces=seen)


# --- serialization roundtrip ---------------------------------------------

def test_signed_mandate_roundtrip():
    signed = mandate.sign(_intent())
    back = mandate.SignedMandate.from_dict(signed.to_dict())
    assert back == signed
    assert mandate.verify(back, now=1000.0).ok


def test_from_dict_rejects_junk():
    with pytest.raises(mandate.MandateError):
        mandate.SignedMandate.from_dict({"nope": 1})


def test_cart_verification_requires_intent():
    im = _intent()
    signed = mandate.sign(_cart(im))
    r = mandate.verify(signed, intent=None, now=1000.0)   # no intent supplied
    assert not r.ok and any("requires its intent" in x for x in r.reasons)


def test_verify_rejects_non_signed_mandate():
    r = mandate.verify("not a mandate", now=1000.0)       # type: ignore[arg-type]
    assert not r.ok


# --- subkey separation ----------------------------------------------------

def test_mandate_key_is_separate_from_root():
    assert witness.sub_public_key_hex("mandate/v1") != witness.public_key_hex()
    # and stable
    assert witness.sub_public_key_hex("mandate/v1") == \
        witness.sub_public_key_hex("mandate/v1")


# --- H1 hardening: capability subject binding, signed intent, co-sign verify -

def test_capability_bound_to_a_different_user_is_refused():
    """A capability granted to one subject cannot back another user's mandate,
    even if the jti matches and the token is otherwise valid."""
    from olympus import identity
    im = _intent()                                   # user "u"
    tok = identity.grant("someone-else", scopes=[mandate.PAYMENT_SCOPE],
                         risk_ceiling=mandate.FINANCIAL_LEGAL, ttl=3600.0)
    cm = mandate.create_cart(im, amount=12000, currency="USD", merchant="acme",
                             items=["Shoe X"], capability_jti=tok["payload"]["jti"],
                             now=1000.0)
    signed = mandate.co_sign(mandate.sign(cm))
    assert mandate.capability_ok(signed, tok, now=1000.0) is False


def test_capability_bound_to_same_user_is_accepted():
    from olympus import identity
    im = _intent()                                   # user "u"
    tok = identity.grant("u", scopes=[mandate.PAYMENT_SCOPE],
                         risk_ceiling=mandate.FINANCIAL_LEGAL, ttl=3600.0)
    cm = mandate.create_cart(im, amount=12000, currency="USD", merchant="acme",
                             items=["Shoe X"], capability_jti=tok["payload"]["jti"],
                             now=1000.0)
    signed = mandate.co_sign(mandate.sign(cm))
    assert mandate.capability_ok(signed, tok, now=1000.0) is True


def test_signed_intent_is_verified_for_containment():
    """A cart can be verified against a SIGNED intent; a forged/edited signed
    intent (tampered cap) is rejected rather than trusted."""
    im = _intent(cap=15000)
    signed_intent = mandate.sign(im)
    cm = mandate.co_sign(mandate.sign(_cart(im, amount=12000)))
    assert mandate.verify(cm, intent=signed_intent, now=1000.0,
                          require_cosignature=True).ok
    # Tamper the signed intent's cap upward — signature no longer valid.
    forged = mandate.SignedMandate.from_dict({
        **signed_intent.to_dict(),
        "payload": {**signed_intent.payload, "amount_cap": 1}})
    r = mandate.verify(cm, intent=forged, now=1000.0)
    assert not r.ok and "intent signature invalid" in r.reasons


def test_co_sign_rejects_a_bad_external_signer():
    """co_sign verifies the signer's output before trusting it — a signer that
    returns a garbage signature fails loudly, attributably."""
    im = _intent()
    signed = mandate.sign(_cart(im))
    bad = lambda canonical: ("00" * 32, "de" * 32)   # valid-looking hex, wrong sig
    with pytest.raises(mandate.MandateError):
        mandate.co_sign(signed, signer=bad)
