"""Synthetic owner/schema/structural controls for the M11 record boundary."""
import base64
import hashlib
import json

import pytest
from cryptography.fernet import Fernet

from olympus import vault_records as vr, vault_codec as vc

PASSWORD = b'owned-record-fixture-only'


def legacy(raw):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(PASSWORD).digest())).encrypt(raw)


def test_new_record_round_trip_and_exact_owner_binding():
    entries = {'google': {'token': 'owned', 'expiry': 10}, 'site': 'owned-string'}
    blob = vr.encode('owner.a', entries, PASSWORD)
    result, metadata = vr.decode('owner.a', blob, PASSWORD)
    assert result == entries and metadata.context_authenticated
    with pytest.raises(vc.EnvelopeError):
        vr.decode('owner@a', blob, PASSWORD)
    with pytest.raises(vc.EnvelopeError):
        vr.decode_legacy(blob, PASSWORD)


def test_legacy_record_is_readable_and_explicitly_unbound():
    raw = b'{"google":{"token":"owned"}}'
    blob = legacy(raw)
    entries, metadata = vr.decode('owned-exact-address', blob, PASSWORD)
    assert entries == {'google': {'token': 'owned'}}
    assert metadata.format_version == 0 and not metadata.context_authenticated
    assert vr.decode_legacy(blob, PASSWORD) == entries
    assert not blob.startswith(vc.MAGIC)


@pytest.mark.parametrize('raw', [b'{"a":"x","a":"y"}', b'{"a":{"x":NaN}}',
    b'{"a":{"x":Infinity}}', b'[]', b'{"a":7}', b'"text"', b'{', b'\xff'])
def test_invalid_legacy_json_refuses(raw):
    with pytest.raises(vr.RecordError):
        vr.decode_legacy(legacy(raw), PASSWORD)


@pytest.mark.parametrize('entries', [{'': 'x'}, {'x': None}, {'x': []}, {'x': 1},
    {'x': {'bad': object()}}, {'x': {'large': 2**63}}, {'x': {'number': float('inf')}},
    {'x': 'y' * (vr.MAX_STRING_BYTES + 1)}])
def test_invalid_entries_refuse_before_encryption(entries, monkeypatch):
    monkeypatch.setattr(vc, 'encrypt', lambda *a, **kw: pytest.fail('invalid record reached encryption'))
    with pytest.raises(vr.RecordError):
        vr.encode('owned', entries, PASSWORD)


def test_depth_node_and_entry_bounds():
    nested = 'value'
    for _ in range(vr.MAX_DEPTH + 2):
        nested = {'child': nested}
    with pytest.raises(vr.RecordError):
        vr.validate_entries({'entry': nested})
    with pytest.raises(vr.RecordError):
        vr.validate_entries({'entry': {'items': [0] * (vr.MAX_NODES + 1)}})
    with pytest.raises(vr.RecordError):
        vr.validate_entries({str(i): 'x' for i in range(vr.MAX_ENTRIES + 1)})


@pytest.mark.parametrize('owner', ['', None, 1, 'x' * 4097])
def test_invalid_owner_refuses(owner):
    with pytest.raises(vr.RecordError):
        vr.owner_context(owner)


def test_valid_ciphertext_with_wrong_inner_owner_is_rejected():
    data = json.dumps({'schema': 1, 'owner': 'other', 'revision': 'a'*32,
                       'entries': {'x': 'owned'}}).encode()
    blob = vc.encrypt(data, PASSWORD, context=vr.owner_context('intended'), max_plaintext=vr.MAX_BYTES)
    with pytest.raises(vr.RecordError, match='owner or schema'):
        vr.decode('intended', blob, PASSWORD)


def test_aggregate_size_refuses_before_encoding_or_encryption(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('oversized aggregate reached encoding or encryption')
    monkeypatch.setattr(json.JSONEncoder, 'iterencode', forbidden)
    monkeypatch.setattr(vc, 'encrypt', forbidden)
    # A small caller-side list can denote more than 500 MiB of JSON.
    entries = {'x': {'items': ['x' * vr.MAX_STRING_BYTES] * 8000}}
    with pytest.raises(vr.RecordError, match='aggregate bound'):
        vr.encode('owned', entries, PASSWORD)


def test_escaping_counts_toward_aggregate_limit_before_encoding(monkeypatch):
    monkeypatch.setattr(json.JSONEncoder, 'iterencode',
                        lambda *a, **k: pytest.fail('aggregate reached encoder'))
    entries = {'x': {'items': ['\x00' * vr.MAX_STRING_BYTES] * 12}}
    with pytest.raises(vr.RecordError, match='aggregate bound'):
        vr.encode('owned', entries, PASSWORD)


@pytest.mark.parametrize('text', ['abc', '\\"\b\f\n\r\t', '\x00\x1f',
                                  'é€😀', 'a\x00é\\😀'])
def test_string_budget_matches_actual_json_encoding(text):
    assert vr._string_size(text) == len(json.dumps(text, ensure_ascii=False).encode())


def test_record_envelope_overhead_is_bounded_before_encryption(monkeypatch):
    monkeypatch.setattr(vr, 'MAX_BYTES', 128)
    monkeypatch.setattr(vc, 'encrypt', lambda *a, **k: pytest.fail('oversized record reached codec'))
    with pytest.raises(vr.RecordError):
        vr.encode('owner' * 30, {'entry': 'small'}, PASSWORD)


@pytest.mark.parametrize('value', ['\ud800', '\udfff'])
def test_surrogates_fail_with_generic_record_error(value):
    with pytest.raises(vr.RecordError):
        vr.encode('owned', {'entry': value}, PASSWORD)
    with pytest.raises(vr.RecordError):
        vr.owner_context(value)


def test_validated_snapshot_is_independent_of_mutable_caller_data():
    entries = {'entry': {'nested': ['original']}}
    captured = vr.validate_entries(entries)
    entries['entry']['nested'][0] = 'changed'
    entries['entry']['extra'] = 'new'
    assert captured == {'entry': {'nested': ['original']}}
