"""Parked-item 1 — on-device user-key custody.

Closes ADR 0002's documented residual: with the user co-signing key vault-local,
one vault compromise forges both signatures. Now the user key can be held
externally (a pluggable signer — the agent host never sees the private key) and
verification can be pinned to the expected user public key out-of-band, so a
vault-forged co-signature is refused.

No payment rail; no autonomy change — a mandate still never auto-executes.
"""

import json
import sys

import pytest

from olympus import mandate, witness

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "parked1-vault-seed")
    monkeypatch.delenv("OLYMPUS_MANDATE_USER_PUBKEY", raising=False)
    mandate.reset_user_signer()
    yield
    mandate.reset_user_signer()


def _intent():
    return mandate.create_intent("alice", amount_cap=15000, currency="USD",
                                 merchants=["acme"], item="widgets",
                                 trusted=True, now=1000.0)


def _cart(im):
    return mandate.create_cart(im, amount=10000, currency="USD",
                               merchant="acme", items=["widget"], now=1000.0)


# An external holder: a DISTINCT Ed25519 key that never enters the vault.
def _external_keypair(seed_hex: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    import hashlib
    sk = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(seed_hex.encode()).digest())
    pub = sk.public_key().public_bytes_raw().hex()

    def signer(canonical: bytes):
        return pub, sk.sign(canonical).hex()
    return pub, signer


# --- backward compatibility (vault-local default) --------------------------

@_signing
def test_default_vault_cosigner_still_works():
    signed = mandate.co_sign(mandate.sign(_cart(_intent())))
    assert mandate.user_signature_ok(signed)
    # The default co-signing key is the vault subkey.
    assert signed.user_public_key.lower() == \
        witness.sub_public_key_hex("mandate-user/v1").lower()


# --- external / on-device signer -------------------------------------------

@_signing
def test_external_signer_key_never_is_the_vault_key(monkeypatch):
    pub, signer = _external_keypair("device-held-key")
    mandate.register_user_signer(signer)
    signed = mandate.co_sign(mandate.sign(_cart(_intent())))
    assert signed.user_public_key == pub
    assert signed.user_public_key.lower() != \
        witness.sub_public_key_hex("mandate-user/v1").lower()
    # With the pin set to the device key, it verifies.
    monkeypatch.setenv("OLYMPUS_MANDATE_USER_PUBKEY", pub)
    assert mandate.user_signature_ok(signed)


@_signing
def test_command_signer_relays_to_external_process(monkeypatch, tmp_path):
    # A stand-in "device": a python script that holds the key and signs stdin.
    pub, _ = _external_keypair("cmd-device-key")
    script = tmp_path / "device_signer.py"
    script.write_text(
        "import sys, json, hashlib\n"
        "from cryptography.hazmat.primitives.asymmetric.ed25519 import "
        "Ed25519PrivateKey\n"
        "sk = Ed25519PrivateKey.from_private_bytes("
        "hashlib.sha256(b'cmd-device-key').digest())\n"
        "data = sys.stdin.buffer.read()\n"
        "print(json.dumps({'public_key': sk.public_key().public_bytes_raw()"
        ".hex(), 'signature': sk.sign(data).hex()}))\n",
        encoding="utf-8")
    mandate.register_user_signer(mandate.command_signer([sys.executable,
                                                         str(script)]))
    signed = mandate.co_sign(mandate.sign(_cart(_intent())))
    assert signed.user_public_key == pub
    monkeypatch.setenv("OLYMPUS_MANDATE_USER_PUBKEY", pub)
    assert mandate.user_signature_ok(signed)


# --- the whole point: vault compromise can't forge the user half -----------

@_signing
def test_pinned_external_key_defeats_a_vault_forged_cosignature(monkeypatch):
    # The user co-signs on their device; the mandate is pinned to that key.
    device_pub, device_signer = _external_keypair("alices-phone")
    monkeypatch.setenv("OLYMPUS_MANDATE_USER_PUBKEY", device_pub)
    legit = mandate.co_sign(mandate.sign(_cart(_intent())), signer=device_signer)
    assert mandate.user_signature_ok(legit)

    # Now the attacker OWNS THE VAULT — they can co-sign under the vault's own
    # mandate-user subkey (the default signer). That is exactly the residual.
    forged = mandate.co_sign(mandate.sign(_cart(_intent())),
                             signer=mandate._default_user_signer)
    # ...but the pin refuses it: the vault key is not the pinned device key.
    assert not mandate.user_signature_ok(forged)
    # And the contract gate blocks it.
    from olympus import behavioral_contracts as abc
    im = _intent()
    forged2 = mandate.co_sign(mandate.sign(_cart(im)),
                              signer=mandate._default_user_signer)
    with pytest.raises(abc.ContractViolation):
        mandate.enforce_commit(forged2, im, now=1000.0)


@_signing
def test_pin_rejects_unknown_key_even_if_signature_is_valid(monkeypatch):
    _, other_signer = _external_keypair("some-other-key")
    monkeypatch.setenv("OLYMPUS_MANDATE_USER_PUBKEY",
                       "aa" * 32)                     # pin a DIFFERENT key
    signed = mandate.co_sign(mandate.sign(_cart(_intent())), signer=other_signer)
    # The signature is cryptographically valid for its key, but that key isn't
    # pinned → refused (no unknown-key acceptance).
    assert not mandate.user_signature_ok(signed)


@_signing
def test_multiple_pinned_keys_for_rotation(monkeypatch):
    old_pub, old_signer = _external_keypair("old-device")
    new_pub, new_signer = _external_keypair("new-device")
    monkeypatch.setenv("OLYMPUS_MANDATE_USER_PUBKEY", f"{old_pub},{new_pub}")
    assert mandate.user_signature_ok(
        mandate.co_sign(mandate.sign(_cart(_intent())), signer=old_signer))
    assert mandate.user_signature_ok(
        mandate.co_sign(mandate.sign(_cart(_intent())), signer=new_signer))


# --- external signer failures are hard errors, never silent ----------------

def test_command_signer_nonzero_exit_raises():
    signer = mandate.command_signer([sys.executable, "-c",
                                     "import sys; sys.exit(3)"])
    with pytest.raises(mandate.MandateError):
        signer(b"payload")


def test_command_signer_malformed_output_raises():
    signer = mandate.command_signer([sys.executable, "-c",
                                     "print('not json')"])
    with pytest.raises(mandate.MandateError):
        signer(b"payload")


# --- no rail, no autonomy change -------------------------------------------

def test_no_autonomy_change():
    assert mandate.can_auto_execute() is False
    assert mandate.risk_class() == mandate.FINANCIAL_LEGAL
