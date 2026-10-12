"""Bounded, versioned password encryption; no storage or key-custody access.

This M11 foundation is not yet wired into vault/backup callers. Legacy reads
are explicit and report their lack of authenticated purpose/owner binding.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import hmac
import os
import struct
import threading

MAGIC = b"OLYVLT\x00\x00"
FORMAT_VERSION = 1
PROFILE = 1  # scrypt N=2**17, r=8, p=1; 128 MiB principal work array
MAX_PLAINTEXT = 64 * 1024 * 1024
MAX_CONTEXT = 1024
MAX_PASSPHRASE = 4096
_HEADER = struct.Struct(">8sBB16s16s")
_INNER = b"olympus-vault-payload\x00"
_KDF_DOMAIN = b"olympus-vault-scrypt-v1\x00"
_FAILURE = "encrypted data is invalid, unsupported, or does not match its key/context"
_KDF_GATE = threading.BoundedSemaphore(1)
_MESSAGE_GATE = threading.BoundedSemaphore(1)


def _after_fork():
    global _KDF_GATE, _MESSAGE_GATE
    # A child has its own address space; never inherit a vanished thread's
    # admission state. This is a per-process cap, not a host-wide memory cap.
    _KDF_GATE = threading.BoundedSemaphore(1)
    _MESSAGE_GATE = threading.BoundedSemaphore(1)


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


class EnvelopeError(ValueError):
    """An input, cryptographic backend, or authentication refusal."""


def _message(function):
    """Admit before allocating payload/base64 buffers, including legacy reads.

    This bounds active codec work per process, not caller-owned input or
    returned plaintext buffers, nor the total RSS of independent processes.
    """
    @wraps(function)
    def bounded(*args, **kwargs):
        gate = _MESSAGE_GATE
        if not gate.acquire(blocking=False):
            raise EnvelopeError('password encryption capacity is busy')
        try:
            return function(*args, **kwargs)
        finally:
            gate.release()
    return bounded


@dataclass(frozen=True)
class Decoded:
    data: bytes = field(repr=False)
    format_version: int
    context_authenticated: bool
    key_id: bytes | None


def _limits(passphrase: bytes, context: bytes, max_plaintext: int) -> None:
    if (type(passphrase) is not bytes or not 1 <= len(passphrase) <= MAX_PASSPHRASE
            or type(context) is not bytes or not 1 <= len(context) <= MAX_CONTEXT
            or type(max_plaintext) is not int or not 0 <= max_plaintext <= MAX_PLAINTEXT):
        raise EnvelopeError(_FAILURE)


def _fernet_size(plaintext_size: int) -> int:
    # version + timestamp + IV + padded AES blocks + HMAC, then base64
    binary_size = 57 + (plaintext_size // 16 + 1) * 16
    return 4 * ((binary_size + 2) // 3)


def _token_shape(token: bytes, maximum: int) -> None:
    if not 100 <= len(token) <= maximum:
        raise EnvelopeError(_FAILURE)
    try:
        decoded = base64.b64decode(token, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise EnvelopeError(_FAILURE) from None
    if (base64.urlsafe_b64encode(decoded) != token or len(decoded) < 73
            or decoded[0] != 0x80 or (len(decoded) - 57) % 16):
        raise EnvelopeError(_FAILURE)


def _backend():
    try:
        from cryptography.fernet import Fernet, InvalidToken
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError:
        raise EnvelopeError("cryptography with scrypt support is required") from None
    return Fernet, InvalidToken, UnsupportedAlgorithm, Scrypt


def _cipher(passphrase: bytes, salt: bytes | None):
    Fernet, _, UnsupportedAlgorithm, Scrypt = _backend()
    try:
        if salt is None:
            # Read-only compatibility with the historical format. New writes
            # never select this branch, and no password verifier is exported.
            key = hashlib.sha256(passphrase).digest()
        else:
            gate = _KDF_GATE
            if not gate.acquire(blocking=False):
                raise EnvelopeError('password encryption capacity is busy')
            try:
                key = Scrypt(salt=_KDF_DOMAIN + salt, length=32,
                             n=2**17, r=8, p=1).derive(passphrase)
            finally:
                gate.release()
        return Fernet(base64.urlsafe_b64encode(key))
    except (UnsupportedAlgorithm, MemoryError):
        raise EnvelopeError("password encryption backend is unavailable") from None


@_message
def encrypt(data: bytes, passphrase: bytes, *, context: bytes,
            max_plaintext: int, key_id: bytes | None = None) -> bytes:
    """Create v1 only; ``key_id`` is an opaque identifier, never a password hash.

    Callers must capture custody and supply an exact purpose/owner context.
    The limit is explicit because backup archives and vault rows differ.
    """
    _limits(passphrase, context, max_plaintext)
    if type(data) is not bytes or len(data) > max_plaintext:
        raise EnvelopeError(_FAILURE)
    if key_id is None:
        key_id = os.urandom(16)
    if type(key_id) is not bytes or len(key_id) != 16:
        raise EnvelopeError(_FAILURE)
    salt = os.urandom(16)
    header = _HEADER.pack(MAGIC, FORMAT_VERSION, PROFILE, salt, key_id)
    payload = _INNER + header + struct.pack(">H", len(context)) + context + data
    return header + _cipher(passphrase, salt).encrypt(payload)


@_message
def decrypt(blob: bytes, passphrase: bytes, *, context: bytes,
            max_plaintext: int, allow_legacy: bool = False) -> Decoded:
    """Authenticate before returning bytes; never mutate or migrate ciphertext.

    Unknown/malformed v1 envelopes cannot select legacy parsing. A legacy
    result is explicitly unbound and must not authorize ownership/migration.
    No plaintext is returned before Fernet authentication and header/context
    comparison. Parsing and the approved KDF profile are bounded beforehand.
    """
    _limits(passphrase, context, max_plaintext)
    if type(blob) is not bytes or type(allow_legacy) is not bool:
        raise EnvelopeError(_FAILURE)
    overhead = len(_INNER) + _HEADER.size + 2 + MAX_CONTEXT
    if len(blob) > _HEADER.size + _fernet_size(max_plaintext + overhead):
        raise EnvelopeError(_FAILURE)
    if blob.startswith(MAGIC):
        if len(blob) < _HEADER.size:
            raise EnvelopeError(_FAILURE)
        header, token = blob[:_HEADER.size], blob[_HEADER.size:]
        _, version, profile, salt, key_id = _HEADER.unpack(header)
        if version != FORMAT_VERSION or profile != PROFILE:
            raise EnvelopeError(_FAILURE)
        _token_shape(token, _fernet_size(max_plaintext + overhead))
    else:
        if not allow_legacy:
            raise EnvelopeError(_FAILURE)
        # A legacy token is strictly canonical Fernet base64. Altered/truncated
        # magic bytes fail this check before any password derivation.
        token, salt, key_id, header = blob, None, None, None
        _token_shape(token, _fernet_size(max_plaintext))
    _, InvalidToken, _, _ = _backend()
    try:
        payload = _cipher(passphrase, salt).decrypt(token)
    except (InvalidToken, ValueError):
        raise EnvelopeError(_FAILURE) from None
    if header is None:
        if len(payload) > max_plaintext:
            raise EnvelopeError(_FAILURE)
        return Decoded(payload, 0, False, None)
    expected = _INNER + header + struct.pack(">H", len(context)) + context
    if (len(payload) < len(expected)
            or not hmac.compare_digest(payload[:len(expected)], expected)
            or len(payload) - len(expected) > max_plaintext):
        raise EnvelopeError(_FAILURE)
    return Decoded(payload[len(expected):], FORMAT_VERSION, True, key_id)
