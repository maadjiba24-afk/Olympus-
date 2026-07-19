"""Milestone 1.2 — signed identity + capability grants (Plane 1, artifacts 2-3).

Cards and tokens are minted and checked by `olympus.identity`, reusing the
witness root of trust under a domain-separated subkey per artifact type (exactly
as `mandate.py`). A grant is EVALUATED by the ABC `capability_grant` contract on
the ONE authority evaluator — this module is not a second policy engine.

The Done bar: every failure mode is rejected — forged (bad/foreign signature),
expired, revoked, replayed (nonce reuse), and scope-escalating (a scope or risk
beyond the grant's bound) — proven both through `identity.verify_grant` AND
through `abc.check("capability.exercise", verdict.context())`.
"""

import json

import pytest

from olympus import actions, behavioral_contracts as abc, config, identity, witness

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Signed seed so artifacts are attested, and an isolated revocation store.
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "m1.2-identity-seed")


def _fresh_grant(**kw):
    kw.setdefault("scopes", ["browser.fill", "browser.click"])
    kw.setdefault("risk_ceiling", actions.NOTABLE)
    return identity.grant("agent://scout", **kw)


# --- agent cards ----------------------------------------------------------

@_signing
def test_card_round_trip():
    card = identity.issue_card("agent://scout")
    ok, reasons = identity.verify_card(card)
    assert ok and reasons == ()


@_signing
def test_forged_card_rejected():
    card = identity.issue_card("agent://scout")
    card["payload"]["agent_id"] = "agent://impostor"      # tamper post-sign
    ok, reasons = identity.verify_card(card)
    assert not ok and "signature" in reasons[0]


@_signing
def test_expired_card_rejected():
    card = identity.issue_card("agent://scout", ttl=1.0)
    p = card["payload"]
    ok, reasons = identity.verify_card(card, now=p["expires_at"] + 1)
    assert not ok and any("expired" in r for r in reasons)


@_signing
def test_replayed_card_rejected():
    card = identity.issue_card("agent://scout")
    seen: set = set()
    assert identity.verify_card(card, seen_nonces=seen)[0]
    ok, reasons = identity.verify_card(card, seen_nonces=seen)     # same nonce
    assert not ok and any("replay" in r for r in reasons)


# --- capability grants: the five rejection modes --------------------------

@_signing
def test_valid_grant_accepted_and_authorizes_abc():
    tok = _fresh_grant()
    v = identity.verify_grant(tok, requested_scope="browser.fill",
                              requested_risk=actions.NOTABLE)
    assert v.ok and v.reasons == ()
    # ...and the ONE evaluator agrees on the same context.
    assert abc.check("capability.exercise", v.context()).ok


@_signing
def test_forged_grant_rejected():
    tok = _fresh_grant()
    tok["payload"]["risk_ceiling"] = actions.FINANCIAL_LEGAL   # forge a higher ceiling
    v = identity.verify_grant(tok, requested_scope="browser.fill")
    assert not v.ok and not v.signature_valid
    assert abc.check("capability.exercise", v.context()).blocking


@_signing
def test_expired_grant_rejected():
    tok = _fresh_grant(ttl=1.0)
    now = tok["payload"]["expires_at"] + 1
    v = identity.verify_grant(tok, requested_scope="browser.fill", now=now)
    assert not v.ok and not v.not_expired
    assert abc.check("capability.exercise", v.context()).blocking


@_signing
def test_revoked_grant_rejected():
    tok = _fresh_grant()
    identity.revoke(tok["payload"]["jti"])
    assert identity.is_revoked(tok["payload"]["jti"])
    v = identity.verify_grant(tok, requested_scope="browser.fill")
    assert not v.ok and not v.not_revoked
    assert abc.check("capability.exercise", v.context()).blocking


@_signing
def test_replayed_grant_rejected():
    tok = _fresh_grant()
    seen: set = set()
    assert identity.verify_grant(tok, requested_scope="browser.fill",
                                 seen_nonces=seen).ok
    v = identity.verify_grant(tok, requested_scope="browser.fill",
                              seen_nonces=seen)          # nonce already consumed
    assert not v.ok and not v.fresh_nonce
    assert abc.check("capability.exercise", v.context()).blocking


@_signing
def test_scope_escalating_grant_rejected():
    tok = _fresh_grant(scopes=["browser.fill"], risk_ceiling=actions.NOTABLE)
    # scope not granted
    v = identity.verify_grant(tok, requested_scope="payment.charge")
    assert not v.ok and not v.scope_within_bound
    assert abc.check("capability.exercise", v.context()).blocking
    # risk above the ceiling
    v2 = identity.verify_grant(tok, requested_scope="browser.fill",
                               requested_risk=actions.FINANCIAL_LEGAL)
    assert not v2.ok and not v2.scope_within_bound
    assert abc.check("capability.exercise", v2.context()).blocking


@_signing
def test_nonce_not_consumed_on_failed_verify():
    # A rejected verify must not burn the nonce (else a failed attempt would lock
    # out a legitimate later use).
    tok = _fresh_grant()
    seen: set = set()
    identity.verify_grant(tok, requested_scope="nope", seen_nonces=seen)
    assert tok["payload"]["nonce"] not in seen
    assert identity.verify_grant(tok, requested_scope="browser.fill",
                                 seen_nonces=seen).ok


# --- minting guards --------------------------------------------------------

def test_grant_rejects_unknown_risk():
    with pytest.raises(identity.IdentityError):
        identity.grant("agent://scout", scopes=["x"], risk_ceiling="MADE_UP")


def test_card_rejects_overlong_ttl():
    with pytest.raises(identity.IdentityError):
        identity.issue_card("agent://scout", ttl=identity._MAX_TTL + 1)


def test_revocation_persists_across_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    identity.revoke("cap_persisted")
    stored = json.loads((tmp_path / "capability_revocations.json").read_text())
    assert "cap_persisted" in stored and identity.is_revoked("cap_persisted")
