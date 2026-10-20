"""Synthetic M11 codec foundation; no credential files, network or storage."""
import base64
import hashlib
import struct

import pytest

from olympus import vault_codec as vc

PASSWORD = b"owned-v1-fixture-only"
CONTEXT = b"vault\x00exact:owned-fixture"
LEGACY = (b"gAAAAABfXhAANkMKbccBjLE43OlRDEvSjFBVNGsYXsAsCX8z6BWdkKY0qBy1Lw7zC-1"
          b"zrXqzOvKa30x7sumYoT-lbkuLq9tojibu4DYMxKSTAWf-O4BWss8=")


@pytest.fixture(scope="module")
def encrypted():
    return vc.encrypt(b"owned credential", PASSWORD, context=CONTEXT,
                      max_plaintext=100, key_id=b"i" * 16)


def decode(blob, **kwargs):
    values = dict(context=CONTEXT, max_plaintext=100)
    values.update(kwargs)
    return vc.decrypt(blob, PASSWORD, **values)


def test_round_trip_authenticates_context_and_hides_payload_repr(encrypted):
    result = decode(encrypted)
    assert result.data == b"owned credential"
    assert result.format_version == 1 and result.context_authenticated
    assert result.key_id == b"i" * 16
    assert "owned credential" not in repr(result)
    assert b"owned credential" not in encrypted and CONTEXT not in encrypted


def test_salt_and_opaque_identifier_are_random():
    blobs = [vc.encrypt(b"same", PASSWORD, context=CONTEXT, max_plaintext=4)
             for _ in range(2)]
    a, b = (vc._HEADER.unpack(x[:vc._HEADER.size]) for x in blobs)
    assert a[3] != b[3] and a[4] != b[4] and blobs[0] != blobs[1]
    assert hashlib.sha256(PASSWORD).digest() not in blobs[0]
    assert hashlib.sha256(PASSWORD).hexdigest().encode() not in blobs[0]


def test_wrong_password_and_wrong_owner_or_purpose_refuse(encrypted):
    with pytest.raises(vc.EnvelopeError):
        vc.decrypt(encrypted, b"wrong-owned-fixture", context=CONTEXT, max_plaintext=100)
    for ctx in (b"backup\x00exact:owned-fixture", b"vault\x00exact:other-fixture"):
        with pytest.raises(vc.EnvelopeError):
            decode(encrypted, context=ctx)


@pytest.mark.parametrize("offset", [10, 26])
def test_salt_or_key_identifier_substitution_refuses(encrypted, offset):
    changed = bytearray(encrypted)
    changed[offset] ^= 1
    with pytest.raises(vc.EnvelopeError):
        decode(bytes(changed))


def test_ciphertext_authentication_failure_refuses(encrypted):
    body = bytearray(base64.urlsafe_b64decode(encrypted[vc._HEADER.size:]))
    body[30] ^= 1
    with pytest.raises(vc.EnvelopeError):
        decode(encrypted[:vc._HEADER.size] + base64.urlsafe_b64encode(body))


@pytest.mark.parametrize("offset,value", [(8, 0), (8, 2), (8, 255), (9, 0), (9, 2), (9, 255)])
def test_unsupported_versions_and_profiles_refuse_before_kdf(encrypted, monkeypatch, offset, value):
    def forbidden(*args):
        pytest.fail("malformed input reached a KDF")
    monkeypatch.setattr(vc, "_cipher", forbidden)
    changed = bytearray(encrypted)
    changed[offset] = value
    with pytest.raises(vc.EnvelopeError):
        decode(bytes(changed), allow_legacy=True)


@pytest.mark.parametrize("blob", [b"", b"OLYVLT", vc.MAGIC, vc.MAGIC + b"\x01",
                                  b"not-a-token", b"g" * 100, LEGACY + b"\n", LEGACY + b"="])
def test_malformed_input_cannot_downgrade_to_legacy(blob, monkeypatch):
    monkeypatch.setattr(vc, "_cipher", lambda *args: pytest.fail("malformed input reached a KDF"))
    with pytest.raises(vc.EnvelopeError):
        decode(blob, allow_legacy=True)


def test_legacy_fixture_read_requires_explicit_permission_and_is_unbound():
    before = bytes(LEGACY)
    with pytest.raises(vc.EnvelopeError):
        vc.decrypt(LEGACY, b"owned-legacy-fixture-only", context=CONTEXT, max_plaintext=100)
    result = vc.decrypt(LEGACY, b"owned-legacy-fixture-only", context=CONTEXT,
                        max_plaintext=100, allow_legacy=True)
    assert result.data == b"owned legacy credential"
    assert result.format_version == 0 and not result.context_authenticated
    assert result.key_id is None and LEGACY == before
    with pytest.raises(vc.EnvelopeError):
        vc.decrypt(LEGACY, b"wrong-legacy-fixture", context=CONTEXT,
                   max_plaintext=100, allow_legacy=True)


@pytest.mark.parametrize("kwargs", [dict(context=b""), dict(context=b"x" * 1025),
    dict(context="not-bytes"), dict(max_plaintext=-1), dict(max_plaintext=True),
    dict(max_plaintext=vc.MAX_PLAINTEXT + 1), dict(max_plaintext=1.5)])
def test_invalid_caller_limits_refuse_before_kdf(encrypted, monkeypatch, kwargs):
    monkeypatch.setattr(vc, "_cipher", lambda *args: pytest.fail("invalid limits reached a KDF"))
    with pytest.raises(vc.EnvelopeError):
        decode(encrypted, **kwargs)


def test_oversize_ciphertext_and_plaintext_refuse_before_kdf(monkeypatch):
    monkeypatch.setattr(vc, "_cipher", lambda *args: pytest.fail("oversize input reached a KDF"))
    with pytest.raises(vc.EnvelopeError):
        decode(vc.MAGIC + b"x" * 2000, max_plaintext=0)
    with pytest.raises(vc.EnvelopeError):
        vc.encrypt(b"too big", PASSWORD, context=CONTEXT, max_plaintext=1)


def test_authenticated_plaintext_limit_is_enforced(encrypted):
    # Envelope bounds permit padding; successful authentication cannot bypass
    # the caller's exact plaintext cap.
    with pytest.raises(vc.EnvelopeError):
        decode(encrypted, max_plaintext=15)


def test_zero_length_payload_with_zero_limit():
    encrypted = vc.encrypt(b"", PASSWORD, context=CONTEXT, max_plaintext=0)
    assert decode(encrypted, max_plaintext=0).data == b""


def test_profile_is_fixed_and_executes_real_scrypt(monkeypatch):
    from cryptography.hazmat.primitives.kdf import scrypt
    real = scrypt.Scrypt
    calls = []
    def observed(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)
    monkeypatch.setattr(scrypt, "Scrypt", observed)
    vc.encrypt(b"profile", PASSWORD, context=CONTEXT, max_plaintext=7)
    assert len(calls) == 1
    assert {k: calls[0][k] for k in ("n", "r", "p", "length")} == {
        "n": 2**17, "r": 8, "p": 1, "length": 32}
    assert len(calls[0]["salt"]) == len(vc._KDF_DOMAIN) + 16


def test_backend_memory_refusal_returns_no_plaintext(monkeypatch):
    from cryptography.hazmat.primitives.kdf import scrypt
    def unavailable(**kwargs):
        raise MemoryError("owned injected backend refusal")
    monkeypatch.setattr(scrypt, "Scrypt", unavailable)
    with pytest.raises(vc.EnvelopeError, match="backend is unavailable"):
        vc.encrypt(b"never returned", PASSWORD, context=CONTEXT, max_plaintext=100)


def test_outer_header_is_authenticated_inside_ciphertext(encrypted):
    # Re-encrypt an otherwise valid body with an authenticated but inconsistent
    # inner header. This controls both keys and proves the header comparison.
    header = encrypted[:vc._HEADER.size]
    salt = vc._HEADER.unpack(header)[3]
    bad_inner = bytearray(header)
    bad_inner[26] ^= 1
    payload = vc._INNER + bytes(bad_inner) + struct.pack(">H", len(CONTEXT)) + CONTEXT + b"owned"
    forged = header + vc._cipher(PASSWORD, salt).encrypt(payload)
    with pytest.raises(vc.EnvelopeError):
        decode(forged)


@pytest.mark.parametrize("operation", ["encrypt", "decrypt-v1", "decrypt-legacy"])
def test_missing_backend_has_same_typed_refusal(encrypted, monkeypatch, operation):
    import builtins
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ModuleNotFoundError("owned missing backend")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    with pytest.raises(vc.EnvelopeError, match="scrypt support is required"):
        if operation == "encrypt":
            vc.encrypt(b"owned", PASSWORD, context=CONTEXT, max_plaintext=100)
        elif operation == "decrypt-v1":
            decode(encrypted)
        else:
            vc.decrypt(LEGACY, b"owned-legacy-fixture-only", context=CONTEXT,
                       max_plaintext=100, allow_legacy=True)


@pytest.mark.parametrize("password", [b"", b"x" * 4097, "not-bytes", None, 7], ids=[None, 'passphrase-4097-bytes', None, None, None])
def test_passphrase_bounds_apply_before_backend(encrypted, monkeypatch, password):
    monkeypatch.setattr(vc, "_backend", lambda: pytest.fail("invalid password reached backend"))
    for operation in (lambda: vc.encrypt(b"x", password, context=CONTEXT, max_plaintext=100),
                      lambda: vc.decrypt(encrypted, password, context=CONTEXT, max_plaintext=100)):
        with pytest.raises(vc.EnvelopeError):
            operation()


@pytest.mark.parametrize("key_id", [b"", b"x" * 15, b"x" * 17, "x" * 16, 16])
def test_invalid_key_identifiers_refuse_before_backend(monkeypatch, key_id):
    monkeypatch.setattr(vc, "_backend", lambda: pytest.fail("invalid key ID reached backend"))
    with pytest.raises(vc.EnvelopeError):
        vc.encrypt(b"x", PASSWORD, context=CONTEXT, max_plaintext=100, key_id=key_id)


@pytest.mark.parametrize("length", range(vc._HEADER.size))
def test_every_truncated_header_refuses_before_backend(encrypted, monkeypatch, length):
    monkeypatch.setattr(vc, "_backend", lambda: pytest.fail("truncated header reached backend"))
    with pytest.raises(vc.EnvelopeError):
        decode(encrypted[:length], allow_legacy=True)


@pytest.mark.parametrize("size", [15, 16, 17])
def test_payload_padding_boundaries(size):
    data = b"x" * size
    blob = vc.encrypt(data, PASSWORD, context=CONTEXT, max_plaintext=size)
    assert decode(blob, max_plaintext=size).data == data
    with pytest.raises(vc.EnvelopeError):
        decode(blob, max_plaintext=size - 1)


@pytest.mark.parametrize("part", ["domain", "context-length", "context-truncated"])
def test_authenticated_malformed_inner_framing_refuses(encrypted, part):
    header = encrypted[:vc._HEADER.size]
    salt = vc._HEADER.unpack(header)[3]
    domain, count, context = vc._INNER, len(CONTEXT), CONTEXT
    if part == "domain":
        domain = b"different-payload-domain\x00"
    elif part == "context-length":
        count += 1
    else:
        context = context[:-1]
    payload = domain + header + struct.pack(">H", count) + context
    forged = header + vc._cipher(PASSWORD, salt).encrypt(payload)
    with pytest.raises(vc.EnvelopeError):
        decode(forged)


def test_concurrent_kdf_is_refused_before_second_work_array(monkeypatch):
    import threading
    from olympus import vault_codec as codec
    original = codec._backend()
    entered, leave = threading.Event(), threading.Event()
    calls, errors = [], []

    class HeldScrypt:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        def derive(self, password):
            entered.set()
            if not leave.wait(5):
                raise RuntimeError('owned KDF control timed out')
            return b'x' * 32

    monkeypatch.setattr(codec, '_backend', lambda: (*original[:3], HeldScrypt))
    def first():
        try:
            codec._cipher(b'owned-password', b's' * 16)
        except BaseException as exc:
            errors.append(type(exc).__name__)
    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(codec.EnvelopeError, match='capacity is busy'):
            codec._cipher(b'owned-other-password', b't' * 16)
        assert len(calls) == 1
    finally:
        leave.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    codec._cipher(b'owned-after-release', b't' * 16)
    assert len(calls) == 2


@pytest.mark.parametrize('exception', [MemoryError, KeyboardInterrupt, SystemExit])
def test_kdf_capacity_released_after_backend_failure(monkeypatch, exception):
    from olympus import vault_codec as codec
    original = codec._backend()
    class FailedScrypt:
        def __init__(self, **kwargs):
            pass
        def derive(self, password):
            raise exception()
    monkeypatch.setattr(codec, '_backend', lambda: (*original[:3], FailedScrypt))
    expected = codec.EnvelopeError if exception is MemoryError else exception
    with pytest.raises(expected):
        codec._cipher(b'owned-password', b's' * 16)
    assert codec._KDF_GATE.acquire(blocking=False)
    codec._KDF_GATE.release()
