"""Milestone 0.1 regression suite — trust root & fail-closed signing.

Pins the Done criteria for M0.1 as named, re-runnable invariants (Phase 2 wants
this suite re-run after every change):

  * a DEFAULT-seed instance is 'dev' and every default-seed signature is
    UNATTESTED, even when it self-verifies (the public seed is forgeable);
  * a CONFIGURED secret seed flips is_default_seed()→False, posture→production,
    and its runs verify as ATTESTED;
  * tampering breaks the signature;
  * broken key custody fails CLOSED — never a silent downgrade to the default.

This suite pins invariants; it does not re-implement any signing mechanism
(that lives in witness.py — FUSION RULE).
"""

import json

import pytest

from olympus import config, trace, witness

pytestmark = pytest.mark.skipif(not witness.available(),
                                reason="cryptography backend unavailable")

REAL_SEED = "m0-secret-seed-not-the-public-default"


def _run(tmp_path, monkeypatch) -> str:
    """A flushed, signed decision run under whatever seed is configured."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    tr = trace.Trace("ask", "shared")
    tr.decision("route", {"name": "zeus"}, {"mode": "delegate"}, status="ok")
    tr.decision("review", {"name": "athena"}, {"verdict": "approve"}, status="ok")
    tr.flush()
    return tr.id


# --- posture & seed custody ------------------------------------------------

def test_default_seed_is_dev(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED_FILE", raising=False)
    assert witness.is_default_seed() is True
    assert witness.posture() == "dev"


def test_configured_seed_is_production(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", REAL_SEED)
    assert witness.is_default_seed() is False
    assert witness.posture() == "production"
    assert witness.posture() != "dev"


def test_broken_custody_fails_closed_never_downgrades(tmp_path, monkeypatch):
    # A world-readable seed file is not custody: hard error, never a silent
    # fall back to the forgeable default seed.
    seed_file = tmp_path / "seed"
    seed_file.write_text(REAL_SEED, encoding="utf-8")
    import os
    if os.name == "posix":
        os.chmod(seed_file, 0o644)
        monkeypatch.setenv("OLYMPUS_SIGNING_SEED_FILE", str(seed_file))
        with pytest.raises(witness.WitnessError):
            witness.is_default_seed()
    # An empty seed file is also a hard error, not a downgrade.
    empty = tmp_path / "empty"
    empty.write_text("   \n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(empty, 0o600)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED_FILE", str(empty))
    with pytest.raises(witness.WitnessError):
        witness.is_default_seed()


# --- attestation: default-seed signatures are UNATTESTED -------------------

def test_default_seed_run_is_signed_ok_but_unattested(tmp_path, monkeypatch):
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    run_id = _run(tmp_path, monkeypatch)
    res = witness.verify_run(run_id)
    # Integrity holds (self-consistent signature)…
    assert res["ok"] is True and res["signed"] is True
    # …but the public default seed is forgeable, so it is NOT attested.
    assert res["attested"] is False


def test_configured_seed_run_is_attested(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", REAL_SEED)
    run_id = _run(tmp_path, monkeypatch)
    res = witness.verify_run(run_id)
    assert res["ok"] is True and res["signed"] is True
    assert res["attested"] is True


def test_default_seed_stays_unattested_even_when_correctly_verified(
        tmp_path, monkeypatch):
    # Sign a run under the DEFAULT seed, then verify it against the default key
    # explicitly (what the CLI does for a dev run). It verifies (ok) but must
    # STILL be reported unattested — "all default-seed signatures are unattested"
    # regardless of how the verifier checks them.
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    run_id = _run(tmp_path, monkeypatch)
    res = witness.verify_run(run_id, pin=witness.default_public_key_hex())
    assert res["ok"] is True
    assert res["attested"] is False


# --- tamper evidence -------------------------------------------------------

def test_tampered_run_is_not_ok_and_not_attested(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", REAL_SEED)
    run_id = _run(tmp_path, monkeypatch)
    path = next((config.MEMORY_DIR / "traces").glob("*.jsonl"))
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    rec["decisions"][0]["rationale"] = {"mode": "direct"}      # forge a decision
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    res = witness.verify_run(run_id)
    assert res["ok"] is False and res["attested"] is False
    assert any("INVALID" in p for p in res["problems"])


def test_sign_verify_roundtrip_and_tamper_at_the_primitive(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", REAL_SEED)
    data = b"m0.1 primitive round trip"
    sig = witness.sign(data)
    assert witness.verify_signature(witness.public_key_hex(), data, sig) is True
    assert witness.verify_signature(witness.public_key_hex(), b"x", sig) is False


# --- manifest: default-seed manifest is dev/unattested ---------------------

def test_default_seed_manifest_refuses_to_sign_as_release(tmp_path, monkeypatch):
    # The manifest re-sign path is fail-closed: under the public default seed it
    # REFUSES to produce a release manifest (which would be forgeable) unless a
    # dev manifest is explicitly requested — and a dev manifest is flagged is_dev
    # so a verifier can never mistake it for an attested release.
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    monkeypatch.delenv("OLYMPUS_PINNED_PUBKEY", raising=False)
    with pytest.raises(witness.WitnessError):
        witness.write_manifest(tmp_path / "release.json")        # dev=False
    out = tmp_path / "dev.json"
    witness.write_manifest(out, dev=True)                        # explicit dev
    manifest = json.loads(out.read_text(encoding="utf-8"))
    strict = witness.verify_manifest(manifest, allow_dev=False)
    assert strict["is_dev"] is True and strict["ok"] is False    # unattested


def test_configured_seed_signs_a_release_manifest(tmp_path, monkeypatch):
    # With a real seed, the manifest re-signs without the dev escape hatch.
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", REAL_SEED)
    out = tmp_path / "release.json"
    witness.write_manifest(out)                                  # no dev flag needed
    manifest = json.loads(out.read_text(encoding="utf-8"))
    integrity = manifest.get("integrity") or {}
    assert integrity.get("signature") and integrity.get("publicKey")
    assert integrity["publicKey"] == witness.public_key_hex()    # signed by real key
    assert witness.is_default_seed() is False
