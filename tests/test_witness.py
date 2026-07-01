"""Signed releases + signed decision log (Task 5 of the moat plan).

What these prove:
1. A release manifest signs every tracked file's SHA-256 with an Ed25519 key;
   `verify` passes on an intact tree and FAILS naming a file once it's tampered.
2. Re-signing a tampered manifest with a different key is caught (untrusted key).
3. The same root-of-trust signs the Task-1 decision log; tampering the recorded
   decisions makes the signature check FAIL.
"""

import json
from pathlib import Path

import pytest

from olympus import config, trace, witness

pytestmark = pytest.mark.skipif(not witness.available(),
                                reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate_from_shipped_pin(tmp_path_factory, monkeypatch):
    """Olympus ships a production pinned key (olympus/witness_pubkey.txt), and
    verify_manifest() falls back to it when no pin is passed. These unit tests
    exercise the dev / no-pin / explicit-pin semantics, so neutralize the
    ambient shipped pin: clear the env override and point _package_dir at an
    empty dir. Tests that want a pin pass it explicitly."""
    monkeypatch.delenv("OLYMPUS_PINNED_PUBKEY", raising=False)
    empty = tmp_path_factory.mktemp("no_committed_pin")
    monkeypatch.setattr(witness, "_package_dir", lambda: empty)


# --- canonical encoding + keys -------------------------------------------

def test_canonical_json_sorts_and_drops_none():
    a = witness.canonical_json({"b": 1, "a": None, "c": {"y": None, "x": 2}})
    b = witness.canonical_json({"c": {"x": 2}, "b": 1})
    assert a == b


def test_sign_and_verify_roundtrip():
    sig = witness.sign(b"payload")
    assert witness.verify_signature(witness.public_key_hex(), b"payload", sig)
    assert not witness.verify_signature(witness.public_key_hex(), b"other", sig)


def test_signing_seed_changes_the_key(monkeypatch):
    base = witness.public_key_hex()
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "a-different-secret")
    assert witness.public_key_hex() != base


# --- release manifest: tamper a tracked file -> verify fails -------------

def _make_tree(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("print('a')\n")
    (pkg / "b.py").write_text("print('b')\n")
    (pkg / "note.md").write_text("# note\n")
    return pkg


def _manifest_for(pkg: Path) -> dict:
    files = [{"path": p.relative_to(pkg).as_posix(),
              "sha256": witness._sha256_file(p)}
             for p in witness.tracked_files(pkg)]
    core = {"schema": witness.SCHEMA, "gitCommit": None, "branch": None,
            "files": files,
            "summary": {"total": len(files), "verified": 0, "failed": 0}}
    if witness.is_default_seed():            # mirror build_manifest's dev marker
        core["dev"] = True
    payload = witness.canonical_json(core)
    core["integrity"] = {"manifestHash": "x", "publicKey": witness.public_key_hex(),
                         "signature": witness.sign(payload),
                         "seedDerivation": witness.SEED_DERIVATION}
    return core


def test_verify_passes_on_intact_tree(tmp_path):
    pkg = _make_tree(tmp_path)
    # default seed in tests -> a dev manifest, accepted for local use
    result = witness.verify_manifest(_manifest_for(pkg), base=pkg, allow_dev=True)
    assert result["ok"] is True
    assert result["signature_ok"] and result["pubkey_trusted"]
    assert result["drifted"] == [] and result["missing"] == []


def test_verify_fails_naming_a_tampered_file(tmp_path):
    pkg = _make_tree(tmp_path)
    manifest = _manifest_for(pkg)
    (pkg / "b.py").write_text("print('TAMPERED')\n")     # drift one file
    result = witness.verify_manifest(manifest, base=pkg, allow_dev=True)
    assert result["ok"] is False
    assert "b.py" in result["drifted"]
    assert any("b.py" in p for p in result["problems"])


def test_verify_fails_on_missing_file(tmp_path):
    pkg = _make_tree(tmp_path)
    manifest = _manifest_for(pkg)
    (pkg / "a.py").unlink()
    result = witness.verify_manifest(manifest, base=pkg, allow_dev=True)
    assert result["ok"] is False and "a.py" in result["missing"]


def test_verify_rejects_manifest_resigned_with_foreign_key(tmp_path, monkeypatch):
    pkg = _make_tree(tmp_path)
    pin = witness.public_key_hex()                  # the legitimate key, pinned
    # Attacker tampers a file AND re-signs with their own key.
    (pkg / "a.py").write_text("print('evil')\n")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "attacker-key")
    forged = _manifest_for(pkg)        # internally valid, but wrong key
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED")
    result = witness.verify_manifest(forged, base=pkg, pin=pin)
    assert result["ok"] is False
    assert result["pubkey_trusted"] is False
    assert any("UNTRUSTED" in p for p in result["problems"])


# --- F1: pinned key + default-seed refusal -------------------------------

def test_sign_refuses_default_seed_without_dev(tmp_path, monkeypatch):
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)   # default seed
    assert witness.is_default_seed() is True
    with pytest.raises(witness.WitnessError, match="default seed"):
        witness.write_manifest(tmp_path / "m.json")            # no dev -> refuse
    # with --dev it writes, and the manifest is marked dev
    path = witness.write_manifest(tmp_path / "m.json", dev=True)
    assert json.loads(path.read_text())["dev"] is True


def test_dev_manifest_requires_allow_dev(tmp_path, monkeypatch):
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    pkg = _make_tree(tmp_path)
    manifest = _manifest_for(pkg)                  # dev (default seed)
    blocked = witness.verify_manifest(manifest, base=pkg, allow_dev=False)
    assert blocked["ok"] is False
    assert any("DEV" in p or "allow-dev" in p for p in blocked["problems"])
    ok = witness.verify_manifest(manifest, base=pkg, allow_dev=True)
    assert ok["ok"] is True


def test_pinned_key_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "prod-secret-seed")
    pkg = _make_tree(tmp_path)
    pin = witness.public_key_hex()                 # the production key
    manifest = _manifest_for(pkg)                  # non-dev (real seed)
    result = witness.verify_manifest(manifest, base=pkg, pin=pin)
    assert result["ok"] is True and result["pubkey_trusted"] is True
    assert result["is_dev"] is False


def test_no_pin_non_dev_manifest_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "prod-secret-seed")
    pkg = _make_tree(tmp_path)
    manifest = _manifest_for(pkg)                  # non-dev, but no pin to trust
    result = witness.verify_manifest(manifest, base=pkg)   # no pin, no allow_dev
    assert result["ok"] is False
    assert any("no pinned key" in p for p in result["problems"])


def test_pinned_pubkey_from_env(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PINNED_PUBKEY", "ABCDEF")
    assert witness.pinned_pubkey() == "abcdef"
    monkeypatch.delenv("OLYMPUS_PINNED_PUBKEY")
    # falls back to None when neither env nor witness_pubkey.txt is set
    monkeypatch.setattr(witness, "_package_dir", lambda: __import__("pathlib").Path("/nonexistent"))
    assert witness.pinned_pubkey() is None


def test_witness_pubkey_is_stable_per_seed(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "seed-A")
    a1 = witness.public_key_hex()
    a2 = witness.public_key_hex()
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "seed-B")
    b = witness.public_key_hex()
    assert a1 == a2 and a1 != b and len(a1) == 64


# --- signed decision log: tamper decisions -> signature fails ------------

def _signed_run(tmp_path, monkeypatch) -> str:
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    tr = trace.Trace("ask", "shared")
    tr.decision("route", {"name": "zeus"}, {"mode": "delegate"}, status="ok")
    tr.decision("review", {"name": "athena"}, {"verdict": "approve"}, status="ok")
    tr.flush()
    return tr.id


def test_decision_log_is_signed_on_flush(tmp_path, monkeypatch):
    run_id = _signed_run(tmp_path, monkeypatch)
    run = trace.load_run(run_id)
    assert run.get("log_signature"), "flush should sign the decision log"
    assert witness.verify_run(run_id)["ok"] is True


def test_tampered_decision_log_fails_signature(tmp_path, monkeypatch):
    run_id = _signed_run(tmp_path, monkeypatch)
    # Tamper the recorded decisions on disk.
    path = next((config.MEMORY_DIR / "traces").glob("*.jsonl"))
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["decisions"][0]["rationale"] = {"mode": "direct"}   # altered decision
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    result = witness.verify_run(run_id)
    assert result["ok"] is False
    assert any("INVALID" in p for p in result["problems"])


def test_verify_run_reports_unsigned_and_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    assert witness.verify_run("nope")["found"] is False


def test_verify_run_pin_binds_to_expected_key(tmp_path, monkeypatch):
    # The pinning path: a third-party verifier holding the expected public key
    # accepts a log signed by it and REJECTS one signed under a different seed,
    # even though that other log's own signature is self-consistent.
    run_id = _signed_run(tmp_path, monkeypatch)
    assert witness.verify_run(run_id, pin=witness.public_key_hex())["ok"] is True
    res = witness.verify_run(run_id, pin="00" * 32)      # a different key
    assert res["ok"] is False
    assert any("UNTRUSTED" in p for p in res["problems"])


def test_verify_log_pin_via_env(tmp_path, monkeypatch):
    run_id = _signed_run(tmp_path, monkeypatch)
    monkeypatch.setenv("OLYMPUS_LOG_PIN", "ff" * 32)     # wrong pin → reject
    assert witness.verify_run(run_id)["ok"] is False
    monkeypatch.setenv("OLYMPUS_LOG_PIN", witness.public_key_hex())
    assert witness.verify_run(run_id)["ok"] is True
