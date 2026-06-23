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
    payload = witness.canonical_json(core)
    core["integrity"] = {"manifestHash": "x", "publicKey": witness.public_key_hex(),
                         "signature": witness.sign(payload),
                         "seedDerivation": witness.SEED_DERIVATION}
    return core


def test_verify_passes_on_intact_tree(tmp_path):
    pkg = _make_tree(tmp_path)
    result = witness.verify_manifest(_manifest_for(pkg), base=pkg)
    assert result["ok"] is True
    assert result["signature_ok"] and result["pubkey_trusted"]
    assert result["drifted"] == [] and result["missing"] == []


def test_verify_fails_naming_a_tampered_file(tmp_path):
    pkg = _make_tree(tmp_path)
    manifest = _manifest_for(pkg)
    (pkg / "b.py").write_text("print('TAMPERED')\n")     # drift one file
    result = witness.verify_manifest(manifest, base=pkg)
    assert result["ok"] is False
    assert "b.py" in result["drifted"]
    assert any("b.py" in p for p in result["problems"])


def test_verify_fails_on_missing_file(tmp_path):
    pkg = _make_tree(tmp_path)
    manifest = _manifest_for(pkg)
    (pkg / "a.py").unlink()
    result = witness.verify_manifest(manifest, base=pkg)
    assert result["ok"] is False and "a.py" in result["missing"]


def test_verify_rejects_manifest_resigned_with_foreign_key(tmp_path, monkeypatch):
    pkg = _make_tree(tmp_path)
    # Attacker tampers a file AND re-signs the manifest with their own key.
    (pkg / "a.py").write_text("print('evil')\n")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "attacker-key")
    forged = _manifest_for(pkg)        # internally valid, but wrong key
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED")
    result = witness.verify_manifest(forged, base=pkg)
    assert result["ok"] is False
    assert result["pubkey_trusted"] is False
    assert any("UNTRUSTED" in p for p in result["problems"])


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
