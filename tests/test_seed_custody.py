"""Signing-seed custody hardening: file-based acquisition, keygen, the
sovereign fail-closed gate, and multi-key pinning (rotation overlap).

Like test_verify_and_custody.py, this file deliberately has NO crypto
skip-guard — on a broken cryptography backend these tests must ERROR, not
skip, because custody is exactly what must not silently degrade. The only
skip is the POSIX-only file-permission case.
"""

import logging
import os

import pytest

from olympus import cli, config, witness


def _write_seed(tmp_path, content="unit-test-seed-0123456789abcdef", mode=0o600):
    p = tmp_path / "seed"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, mode)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    return p


@pytest.fixture(autouse=True)
def _clean_custody_env(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED_FILE", raising=False)
    monkeypatch.delenv("OLYMPUS_SOVEREIGN", raising=False)
    monkeypatch.delenv("OLYMPUS_SOVEREIGN_ALLOW_DEV_SEED", raising=False)
    monkeypatch.delenv("OLYMPUS_PINNED_PUBKEY", raising=False)


# --- 1. file-based acquisition -----------------------------------------------

@pytest.mark.requires_crypto
def test_seed_file_configures_production_posture(tmp_path, monkeypatch):
    p = _write_seed(tmp_path, "  file-held-secret \n")   # stripped on read
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED_FILE", str(p))
    assert witness.is_default_seed() is False
    assert witness.posture() == "production"
    pub = witness.public_key_hex()
    assert pub != witness.default_public_key_hex()
    # and the derived key actually signs/verifies
    payload = b"custody-roundtrip"
    assert witness.verify_signature(pub, payload, witness.sign(payload)) is True
    # strip() semantics: same key as the bare seed string
    assert pub == witness.pubkey_for_seed("file-held-secret")


def test_both_env_and_file_set_is_ambiguous(tmp_path, monkeypatch):
    p = _write_seed(tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED_FILE", str(p))
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "also-set")
    with pytest.raises(witness.WitnessError, match="ambiguous"):
        witness.is_default_seed()
    with pytest.raises(witness.WitnessError):
        witness.public_key_hex()


def test_missing_seed_file_never_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED_FILE", str(tmp_path / "nope"))
    with pytest.raises(witness.WitnessError, match="nope"):
        witness.is_default_seed()


def test_empty_seed_file_is_an_error(tmp_path, monkeypatch):
    p = _write_seed(tmp_path, "   \n\t ")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED_FILE", str(p))
    with pytest.raises(witness.WitnessError, match="empty"):
        witness.is_default_seed()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_seed_file_permissions_enforced(tmp_path, monkeypatch):
    p = _write_seed(tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED_FILE", str(p))
    os.chmod(p, 0o644)
    with pytest.raises(witness.WitnessError, match="644"):
        witness.is_default_seed()
    os.chmod(p, 0o600)
    assert witness.is_default_seed() is False        # accepted again


# --- 2. keygen ----------------------------------------------------------------

def _run_cli(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr()
    return (rc or 0), out.out, out.err


@pytest.mark.requires_crypto
def test_keygen_writes_0600_and_prints_matching_pubkey(tmp_path, monkeypatch,
                                                       capsys):
    out_path = tmp_path / "creds" / "signing_seed"     # parent dirs created
    rc, out, _ = _run_cli(["keygen", "--out", str(out_path)], capsys)
    assert rc == 0 and out_path.exists()
    if os.name == "posix":
        assert (os.stat(out_path).st_mode & 0o777) == 0o600
    seed = out_path.read_text().strip()
    assert seed and seed not in out                    # never printed
    printed = [ln for ln in out.splitlines() if ln.strip().startswith("public key:")]
    assert printed, out
    pub = printed[0].split("public key:")[1].strip()
    # printed key matches recomputation from the file, via the real code paths
    assert pub == witness.pubkey_for_seed(seed)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED_FILE", str(out_path))
    assert witness.public_key_hex() == pub
    assert "OLYMPUS_SIGNING_SEED_FILE" in out          # exact next step named


def test_keygen_refuses_overwrite_without_force(tmp_path, capsys):
    out_path = tmp_path / "signing_seed"
    rc1, _, _ = _run_cli(["keygen", "--out", str(out_path)], capsys)
    first = out_path.read_text()
    rc2, out2, _ = _run_cli(["keygen", "--out", str(out_path)], capsys)
    assert rc1 == 0 and rc2 == 1
    assert "refusing to overwrite" in out2
    assert out_path.read_text() == first               # untouched
    rc3, _, _ = _run_cli(["keygen", "--out", str(out_path), "--force"], capsys)
    assert rc3 == 0
    assert out_path.read_text() != first               # fresh entropy


def test_keygen_two_runs_differ(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    _run_cli(["keygen", "--out", str(a)], capsys)
    _run_cli(["keygen", "--out", str(b)], capsys)
    assert a.read_text() != b.read_text()


# --- 3. sovereign fails closed -------------------------------------------------

def test_sovereign_default_seed_refuses_at_boot(monkeypatch, capsys):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    rc, _, err = _run_cli(["version"], capsys)
    assert rc == 1
    assert "signing seed" in err and "forgeable" in err
    assert "keygen" in err and "OLYMPUS_SIGNING_SEED_FILE" in err   # actionable


def test_sovereign_default_seed_refuses_at_sign_time(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    with pytest.raises(witness.WitnessError, match="forgeable"):
        witness.sign(b"decision-log-payload")


def test_sovereign_boot_allows_keygen_itself(monkeypatch, tmp_path, capsys):
    # The prescribed fix must not be blocked by the gate it fixes.
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    rc, _, _ = _run_cli(["keygen", "--out", str(tmp_path / "s")], capsys)
    assert rc == 0


@pytest.mark.requires_crypto
def test_sovereign_configured_seed_boots_and_signs(tmp_path, monkeypatch,
                                                   capsys):
    p = _write_seed(tmp_path)
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED_FILE", str(p))
    rc, _, _ = _run_cli(["version"], capsys)
    assert rc == 0
    assert witness.sign(b"x")                          # no raise


@pytest.mark.requires_crypto
def test_sovereign_escape_hatch_proceeds_with_loud_warning(monkeypatch, capsys,
                                                           caplog):
    monkeypatch.setenv("OLYMPUS_SOVEREIGN", "1")
    monkeypatch.setenv("OLYMPUS_SOVEREIGN_ALLOW_DEV_SEED", "1")
    with caplog.at_level(logging.WARNING, logger="olympus.witness"):
        rc, _, _ = _run_cli(["version"], capsys)
        assert rc == 0
        assert witness.sign(b"x")                      # sign path permits too
    warned = [r for r in caplog.records if "FORGEABLE" in r.getMessage()]
    assert warned, "escape-hatch use must log an unmissable warning"


# --- 4. multi-key pinning (rotation overlap) ------------------------------------

@pytest.mark.requires_crypto
def test_manifest_signed_with_second_pinned_key_verifies(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "rotation-seed-B")
    manifest = witness.build_manifest()
    key_b = witness.public_key_hex()
    key_a = witness.pubkey_for_seed("rotation-seed-A")

    monkeypatch.setenv("OLYMPUS_PINNED_PUBKEY", f"{key_a},{key_b}")
    res = witness.verify_manifest(manifest)
    assert res["pubkey_trusted"] is True and res["signature_ok"] is True

    monkeypatch.setenv("OLYMPUS_PINNED_PUBKEY", key_a)
    res2 = witness.verify_manifest(manifest)
    assert res2["pubkey_trusted"] is False
    assert any("UNTRUSTED" in p for p in res2["problems"])


def test_pinned_pubkeys_env_then_file_deduped(monkeypatch):
    file_key = "350f970ac5159b30f6736c124a1e468cd1cc82ddd73cb24799057c5c3b0b0336"
    monkeypatch.setenv("OLYMPUS_PINNED_PUBKEY", f"AAA111, aaa111 ,{file_key}")
    pins = witness.pinned_pubkeys()
    assert pins[0] == "aaa111"                        # lowercased, order kept
    assert pins.count("aaa111") == 1                  # deduped
    assert pins.count(file_key) == 1                  # env+file entry deduped
    assert witness.pinned_pubkey() == pins[0]         # back-compat wrapper


# --- 5. the seed never leaves the machine inside a backup archive ---------------

def test_seed_file_is_never_backed_up(tmp_path, monkeypatch, capsys):
    from olympus import backup
    mem = tmp_path / "mem"
    (mem / "usermem").mkdir(parents=True)
    (mem / "usermem" / "alice.json").write_text('{"facts": []}')
    monkeypatch.setattr(config, "MEMORY_DIR", mem)
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "a-strong-secret")
    rc, _, _ = _run_cli(["keygen"], capsys)          # default MEMORY_DIR path
    assert rc == 0 and (mem / "signing_seed").exists()

    assert all(p.name != "signing_seed"              # belt: excluded at listing
               for p in backup._included_files(mem, full=True))

    res = backup.create()                            # and truly not in archive
    assert res["files"] == 1                         # only alice.json
    restored = tmp_path / "restored"
    backup.restore(res["path"], into=restored)
    assert (restored / "usermem" / "alice.json").exists()
    assert not (restored / "signing_seed").exists()


# --- 6. regression: unconfigured, non-sovereign = today's behavior --------------

@pytest.mark.requires_crypto
def test_unconfigured_nonsovereign_dev_behavior_intact(tmp_path, monkeypatch,
                                                       capsys):
    assert witness.is_default_seed() is True
    assert witness.posture() == "dev"
    assert witness.sign(b"x")                          # dev signing still works
    out = tmp_path / "verification.json"
    with pytest.raises(witness.WitnessError):          # sign refuses w/o --dev
        witness.write_manifest(out)
    rc, printed, _ = _run_cli(["sign", "--out", str(out), "--dev"], capsys)
    assert rc == 0 and "DEV" in printed
    import json
    res = witness.verify_manifest(json.loads(out.read_text()),
                                  pin="", allow_dev=True)
    assert res["is_dev"] is True and res["pubkey_trusted"] is True
