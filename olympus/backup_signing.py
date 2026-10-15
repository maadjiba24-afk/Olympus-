"""Bounded backup signer/trust capture; historical witness identity unchanged.

Backup trust accepts frozen public pins or the captured local signing identity.
The public default key is always forgeable local integrity, even when pinned.
Pinned verification never needs private custody and explicitly reports that it
was not inspected. Local fallback distinguishes unavailable custody from absent
selection. These are backup policies, not changes to release verification.
"""
from __future__ import annotations

from . import native_cleanup
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import logging
import os
import re

from . import archive_io, backup_archive as fmt, vault_custody as custody, witness

SCHEMA = 'olympus-backup/1'
MAX_PIN_BYTES = 16 * 1024
MAX_PINS = 64
_KEY = re.compile(r'[0-9a-f]{64}\Z')
_SIGNATURE = re.compile(r'[0-9a-f]{128}\Z')
_NAMES = ('OLYMPUS_SIGNING_SEED','OLYMPUS_SIGNING_SEED_FILE','OLYMPUS_PINNED_PUBKEY',
          'OLYMPUS_ENV','OLYMPUS_SOVEREIGN','OLYMPUS_SOVEREIGN_ALLOW_DEV_SEED')


class SignatureError(ValueError):
    pass


def _text(value,cap):
    if value is None:
        return ''
    if type(value) is not str or len(value) > cap:
        raise SignatureError('backup custody or policy bound exceeded')
    return value.strip()


def _selection():
    return tuple((name,os.environ.get(name)) for name in _NAMES)


def _check_selection(selection):
    if any(os.environ.get(name) != value for name,value in selection):
        raise SignatureError('backup signing policy changed during the captured operation')


def _bound_selection(selection,cwd=None):
    selected = dict(selection)
    path = _text(selected.get('OLYMPUS_SIGNING_SEED_FILE'),custody.MAX_PATH_CHARS)
    if path:
        original = Path(path)
        archive_io._filename(original.name)
        if original.drive and not original.root:
            raise SignatureError('drive-relative signing custody paths are unsupported')
        selected['OLYMPUS_SIGNING_SEED_FILE'] = str(archive_io.selected_path(Path(os.getcwd() if cwd is None else cwd)/path))
    return selected


def _read_seed(path,named):
    """Reuse the bounded native custody reader; signing seed derivation is separate."""
    return custody._read_file(path,named)


def _pins(selected):
    """Bound allocation before reading pin data, preserving witness pin syntax."""
    env = _text(selected.get('OLYMPUS_PINNED_PUBKEY'),MAX_PIN_BYTES)
    if len(env.encode('utf-8')) > MAX_PIN_BYTES:
        raise SignatureError('backup pin bound exceeded')
    raw = archive_io.read_file(witness._package_dir()/'witness_pubkey.txt',MAX_PIN_BYTES,optional=True)
    lines = [] if raw is None else raw.decode('utf-8','strict').splitlines()
    result = set()
    for text in [*env.split(','),*(line for line in lines if not line.strip().startswith('#'))]:
        pin = text.strip().lower()
        if not pin:
            continue
        if not _KEY.fullmatch(pin):
            raise SignatureError('backup pin syntax is invalid')
        result.add(pin)
        if len(result) > MAX_PINS:
            raise SignatureError('backup pin count bound exceeded')
    return frozenset(result)


def _seed(selected):
    """Same strip/UTF-8/universal-newline seed bytes; bounded held-file custody.

    Files are ordinary single-link objects <=16KiB. POSIX requires private mode
    and current ownership; named Windows deployments require a private DACL.
    No permissions are changed. No vault key-strength/KDF rule is applied to
    signing identity. Its derivation remains Ed25519(SHA256(seed bytes)).
    """
    env = _text(selected.get('OLYMPUS_SIGNING_SEED'),custody.MAX_FILE_BYTES)
    path = _text(selected.get('OLYMPUS_SIGNING_SEED_FILE'),custody.MAX_PATH_CHARS)
    mode = _text(selected.get('OLYMPUS_ENV'),64).lower()
    if env and path:
        raise SignatureError('backup signing custody selection is ambiguous')
    if path:
        absolute = archive_io.selected_path(path)
        raw = _read_seed(absolute,mode in ('staging','production','prod'))
        value = raw.decode('utf-8','strict').replace('\r\n','\n').replace('\r','\n').strip()
        if not value:
            raise SignatureError('selected backup signing custody is empty')
    else:
        value = env or witness._DEFAULT_SEED
    raw = value.encode('utf-8','strict')
    if not 0 < len(raw) <= custody.MAX_FILE_BYTES:
        raise SignatureError('backup signing custody bound exceeded')
    return raw


def _key(selected):
    witness._require_crypto()
    # Byte-for-byte historical witness derivation; never use the vault KDF.
    return witness.ed25519.Ed25519PrivateKey.from_private_bytes(hashlib.sha256(_seed(selected)).digest())


def _public(key):
    return key.public_key().public_bytes(witness.serialization.Encoding.Raw,
                                         witness.serialization.PublicFormat.Raw).hex()


def _sidecar(raw):
    value = fmt.strict_json(raw,fmt.MAX_SIGNATURE)
    if (type(value) is not dict or set(value) != {'schema','sha256','signature','publicKey','seedDerivation'}
            or value['schema'] != SCHEMA or value['seedDerivation'] != witness.SEED_DERIVATION
            or type(value['sha256']) is not str or not _KEY.fullmatch(value['sha256'])
            or type(value['publicKey']) is not str or not _KEY.fullmatch(value['publicKey'].lower())
            or type(value['signature']) is not str or not _SIGNATURE.fullmatch(value['signature'].lower())):
        raise SignatureError('backup signature schema is invalid')
    return value


@dataclass(frozen=True,repr=False)
class Verifier:
    public_key: str | None
    default_key: str
    pins: frozenset[str]
    local_custody: str
    selection: tuple = field(repr=False)

    def verify(self,digest,raw):
        _check_selection(self.selection)
        if type(digest) is not str or not _KEY.fullmatch(digest):
            raise SignatureError('invalid bounded backup digest')
        if raw is None:
            return {'signed':False,'signature_ok':False,'signature_math_valid':False,
                    'signer_trusted':False,'signer_trust':'unsigned','signer_authenticated':False,
                    'signer_default':False,'publicKey':None,'signature_sha256':None,
                    'local_signing_custody':self.local_custody}
        value = _sidecar(raw)
        public = value['publicKey'].lower()
        default = public == self.default_key
        trust = 'pinned' if public in self.pins else 'local' if public == self.public_key else 'unknown'
        if default and trust != 'unknown':
            trust = 'public_default_local_integrity'
        math_valid = bool(witness.verify_signature(public,digest.encode('ascii'),value['signature']))
        _check_selection(self.selection)
        accepted = math_valid and value['sha256'] == digest and trust != 'unknown'
        return {'signed':True,'signature_ok':accepted,'signature_math_valid':math_valid,
                'signer_trusted':trust != 'unknown','signer_trust':trust,
                'signer_authenticated':accepted and not default,'signer_default':default,
                'publicKey':public,'signature_sha256':hashlib.sha256(raw).hexdigest(),
                'local_signing_custody':self.local_custody}


@dataclass(frozen=True,repr=False)
class Signer(Verifier):
    key: object = field(repr=False)

    def signature(self,digest):
        _check_selection(self.selection)
        if type(digest) is not str or not _KEY.fullmatch(digest):
            raise SignatureError('invalid bounded backup digest')
        signature = self.key.sign(digest.encode('ascii')).hex()
        _check_selection(self.selection)
        value = {'schema':SCHEMA,'sha256':digest,'signature':signature,
                 'publicKey':self.public_key,'seedDerivation':witness.SEED_DERIVATION}
        raw = json.dumps(value,sort_keys=True,separators=(',',':')).encode('ascii')
        if len(raw) > fmt.MAX_SIGNATURE:
            raise SignatureError('backup signature bound exceeded')
        return raw


def capture_signer(*, selection=None, cwd=None, policy=None):
    """Acquire private custody once. Unavailability never becomes unsigned."""
    selection = _selection() if selection is None else selection
    selected = {};failed = False;cleanup_unconfirmed = False
    try:
        if type(selection) is not tuple or tuple(name for name,_ in selection) != _NAMES:
            raise SignatureError('invalid captured signing selection')
        captured_cwd = os.getcwd() if cwd is None else cwd
        if type(captured_cwd) is not str or not os.path.isabs(captured_cwd) or '\0' in captured_cwd:
            raise SignatureError('invalid captured signing directory')
        if policy is not None and (type(policy) is not VerificationPolicy
                or policy.selection != selection or policy.cwd != captured_cwd):
            raise SignatureError('captured signer policy does not match its selection')
        _check_selection(selection)
        selected = _bound_selection(selection,captured_cwd)
        sovereign = _text(selected.get('OLYMPUS_SOVEREIGN'),64).lower() in ('1','true','yes','on')
        allow_dev = _text(selected.get('OLYMPUS_SOVEREIGN_ALLOW_DEV_SEED'),64).lower() in ('1','true','yes','on')
        production = _text(selected.get('OLYMPUS_ENV'),64).lower() in ('production','prod')
        key = _key(selected)
        public = _public(key)
        default,pins = ((witness.default_public_key_hex(),_pins(selected)) if policy is None else
                        (policy.default_key,policy.pins))
        _check_selection(selection)
        if public == default:
            if production or sovereign and not allow_dev:
                raise SignatureError('backup signing requires a non-default key in this production posture')
            if sovereign:
                logging.getLogger('olympus.witness').warning(
                    'SOVEREIGN MODE IS USING THE PUBLIC DEV SIGNING SEED '
                    '(OLYMPUS_SOVEREIGN_ALLOW_DEV_SEED=1): every signature produced is '
                    'FORGEABLE - acceptable for labs/CI, never for production.')
        return Signer(public,default,pins,'captured',selection,key)
    except Exception as error:
        failed = True
        cleanup_unconfirmed = native_cleanup.unknown(error)
    finally:
        selected.clear()
    if failed:
        error = SignatureError('backup signer or trust policy is unavailable')
        if cleanup_unconfirmed:
            native_cleanup.mark(error)
        raise error from None


@dataclass(frozen=True,repr=False)
class VerificationPolicy:
    selection: tuple = field(repr=False)
    cwd: str = field(repr=False)
    pins: frozenset[str]
    default_key: str


def capture_policy(*, selection=None, cwd=None):
    """Freeze public trust before archive I/O; defer unused private custody.

    The pin file is a captured snapshot, not a later mutable lookup. Relative
    private selection is bound against this cwd only if fallback is needed.
    """
    frozen = _selection() if selection is None else selection
    selected = {};failed = False;cleanup_unconfirmed = False
    try:
        if type(frozen) is not tuple or tuple(name for name,_ in frozen)!=_NAMES:
            raise SignatureError('invalid signing policy selection')
        captured_cwd = os.getcwd() if cwd is None else cwd
        _check_selection(frozen)
        selected = dict(frozen)
        witness._require_crypto()
        pins,default = _pins(selected),witness.default_public_key_hex()
        _check_selection(frozen)
        return VerificationPolicy(frozen,captured_cwd,pins,default)
    except Exception as error:
        failed = True
        cleanup_unconfirmed = native_cleanup.unknown(error)
    finally:
        selected.clear()
    if failed:
        error = SignatureError('backup public verification policy is unavailable')
        if cleanup_unconfirmed:
            native_cleanup.mark(error)
        raise error from None


def capture_verifier(raw, *, policy=None):
    """Use captured public pins; acquire private fallback once only if needed."""
    selected = {};failed = False;cleanup_unconfirmed = False
    try:
        captured = capture_policy() if policy is None else policy
        if type(captured) is not VerificationPolicy:
            raise SignatureError('invalid captured backup verification policy')
        selection = captured.selection
        _check_selection(selection)
        public = None if raw is None else _sidecar(raw)['publicKey'].lower()
        if public is None:
            local,status = None,'not_inspected_unsigned'
        elif public in captured.pins:
            local,status = None,'not_inspected_pinned_verification'
        else:
            selected = _bound_selection(selection,captured.cwd)
            local,status = _public(_key(selected)),'captured_local_fallback'
        _check_selection(selection)
        return Verifier(local,captured.default_key,captured.pins,status,selection)
    except Exception as error:
        failed = True
        cleanup_unconfirmed = native_cleanup.unknown(error)
    finally:
        selected.clear()
    if failed:
        error = SignatureError('backup verification policy or selected local custody is unavailable')
        if cleanup_unconfirmed:
            native_cleanup.mark(error)
        raise error from None
