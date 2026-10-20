"""Actual owned vault rotation must preserve historical signing identities."""
import base64
import hashlib
import json
import os
import uuid

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from olympus import backup_signing, config, opconfig, store, vault, vault_custody, witness


@pytest.mark.parametrize('custody_kind', ['environment', 'file'])
@pytest.mark.parametrize('source_format', ['legacy', 'current'])
def test_real_vault_rotation_recovery_and_hydration_preserve_signing_identity(
        tmp_path, monkeypatch, custody_kind, source_format):
    root = tmp_path/'owned-state'
    package = tmp_path/'owned-package'
    package.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setattr(witness, '_package_dir', lambda: package)
    for name in (*backup_signing._NAMES, 'OLYMPUS_SECRET_KEY_FILE',
                 'OLYMPUS_DATABASE_URL', 'SMTP_PASS'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-original-vault-key')
    canonical_seed = 'owned stable\nsigning identity'
    seed_file = tmp_path/'owned-signing-seed'
    if custody_kind == 'environment':
        monkeypatch.setenv('OLYMPUS_SIGNING_SEED', '  '+canonical_seed+'  ')
    else:
        raw_seed = b'  owned stable\r\nsigning identity  \r\n'
        fd = os.open(seed_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        try:
            assert os.write(fd, raw_seed) == len(raw_seed)
        finally:
            os.close(fd)
        monkeypatch.setenv('OLYMPUS_SIGNING_SEED_FILE', str(seed_file))
    before_selection = tuple((name, os.environ.get(name)) for name in backup_signing._NAMES)
    before_file = (seed_file.read_bytes(), seed_file.stat().st_mode) if custody_kind == 'file' else None
    historical_key = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(canonical_seed.encode()).digest())
    public = historical_key.public_key().public_bytes(serialization.Encoding.Raw,
                                                      serialization.PublicFormat.Raw).hex()
    payload, digest = b'owned signing compatibility payload', hashlib.sha256(b'owned archive').hexdigest()
    historical_signature = historical_key.sign(payload).hex()
    assert witness.public_key_hex() == public
    assert witness.sign(payload) == historical_signature
    signer = backup_signing.capture_signer()
    before_signature = signer.signature(digest)
    assert signer.public_key == public
    assert json.loads(before_signature)['signature'] == historical_key.sign(digest.encode()).hex()
    store.reset()
    try:
        entries = {'config:SMTP_PASS': 'owned-hydrated-smtp',
                   'config:OLYMPUS_SIGNING_SEED': 'owned-unregistered-must-not-be-hydrated'}
        if source_format == 'legacy':
            old_blob = Fernet(base64.urlsafe_b64encode(hashlib.sha256(
                b'owned-original-vault-key').digest())).encrypt(json.dumps(entries).encode())
            store.backend().put('vault', vault._key('operator'), old_blob)
        else:
            for name, value in entries.items():
                vault.put('operator', name, value)
            old_blob = store.backend().get('vault', vault._key('operator'))
        replacement = vault_custody.capture({'OLYMPUS_SECRET_KEY': 'owned-replacement-vault-key'})
        operation = uuid.uuid4().hex
        assert vault.rotate('operator', operation_id=operation, replacement_custody=replacement)['committed']
        assert store.backend().get('vault_recovery', 'change-'+operation+'-source') == old_blob
        assert vault.resume_change(operation, owner='operator',
                                   replacement_custody=replacement)['already_committed']
        assert os.environ['OLYMPUS_SECRET_KEY'] == 'owned-original-vault-key'
        monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-replacement-vault-key')
        assert vault.entries('operator') == entries
        # Record an undo action even if SMTP_PASS was initially absent: the
        # real hydration function writes os.environ directly.
        monkeypatch.setenv('SMTP_PASS', 'owned-reset-marker')
        monkeypatch.delenv('SMTP_PASS')
        assert opconfig.apply_secrets() == 1
        assert os.environ['SMTP_PASS'] == 'owned-hydrated-smtp'
        assert tuple((name, os.environ.get(name)) for name in backup_signing._NAMES) == before_selection
        if before_file is not None:
            assert (seed_file.read_bytes(), seed_file.stat().st_mode) == before_file
        assert witness.public_key_hex() == public
        assert witness.sign(payload) == historical_signature
        assert signer.signature(digest) == before_signature
        assert backup_signing.capture_signer().signature(digest) == before_signature
        assert backup_signing.capture_verifier(before_signature).verify(digest, before_signature)['signature_ok']
    finally:
        store.reset()
