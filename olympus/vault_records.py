"""Bounded vault records and exact-purpose contexts, without key custody/I/O."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid

from . import vault_codec

MAX_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 1024
MAX_NODES = 8192
MAX_DEPTH = 16
MAX_STRING_BYTES = 65536
MAX_OWNER_BYTES = 4096
SCHEMA = 1


class RecordError(ValueError):
    pass


def owner_context(owner: str) -> bytes:
    if type(owner) is not str or not owner:
        raise RecordError('invalid exact vault owner')
    _string_size(owner, MAX_OWNER_BYTES)
    # Purpose and fixed-size owner digest form an unambiguous external context.
    # The full owner is also checked inside the authenticated record.
    return b'olympus-vault-owner-v1\x00' + hashlib.sha256(owner.encode('utf-8')).digest()


def entry_name(name):
    if type(name) is not str or not name or '\x00' in name:
        raise RecordError('invalid vault entry name')
    _string_size(name, 256)


def _string_size(value, maximum=MAX_STRING_BYTES):
    """Exact UTF-8 JSON size with bounded temporary string allocation."""
    if type(value) is not str or len(value) > maximum:
        raise RecordError('vault value exceeds its string bound')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        raise RecordError('invalid vault string') from None
    if size > maximum:
        raise RecordError('vault value exceeds its string bound')
    # ensure_ascii=False still escapes controls, quotes and backslashes.
    # Each addition counts the bytes beyond the original UTF-8 spelling.
    size += 2
    for char in value:
        if char in '\\"\b\f\n\r\t':
            size += 1
        elif ord(char) < 32:
            size += 5
    return size


def validate_entries(entries):
    if type(entries) is not dict or len(entries) > MAX_ENTRIES:
        raise RecordError('invalid or oversized vault entries')
    nodes = 0
    total = 0
    def add(size):
        nonlocal total
        total += size
        if total > MAX_BYTES:
            raise RecordError('vault plaintext exceeds its aggregate bound')

    def walk(value, depth):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise RecordError('vault record exceeds structural bounds')
        kind = type(value)
        if kind is str:
            add(_string_size(value))
        elif value is None or kind is bool:
            add(4 if value is None or value is True else 5)
        elif kind is int:
            if value.bit_length() > 63:
                raise RecordError('vault integer exceeds its bound')
            add(len(str(value)))
        elif kind is float:
            if not math.isfinite(value):
                raise RecordError('vault numeric value is nonfinite')
            add(len(repr(value)))
        elif kind is list:
            if len(value) > MAX_NODES - nodes:
                raise RecordError('vault record exceeds structural bounds')
            add(2 + max(0, len(value) - 1))
            return [walk(item, depth + 1) for item in value]
        elif kind is dict:
            if 2 * len(value) > MAX_NODES - nodes:
                raise RecordError('vault record exceeds structural bounds')
            add(2 + max(0, len(value) - 1) + len(value))
            copied = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise RecordError('invalid vault object key')
                walk(key, depth + 1)
                copied[key] = walk(item, depth + 1)
            return copied
        else:
            raise RecordError('unsupported vault value type')
        return value
    add(2 + max(0, len(entries) - 1) + len(entries))
    copied = {}
    try:
        for name, value in entries.items():
            entry_name(name)
            walk(name, 0)
            if type(value) not in (dict, str):
                raise RecordError('vault entries must be objects or strings')
            copied[name] = walk(value, 0)
    except RuntimeError:
        raise RecordError('vault entries changed during capture') from None
    return copied


def _pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise RecordError('duplicate vault JSON key')
        data[key] = value
    return data


def _invalid_constant(value):
    raise RecordError('nonfinite vault JSON value')


def _json_load(raw):
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise RecordError('vault plaintext exceeds its bound')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise RecordError('invalid bounded vault JSON') from None


def encode(owner, entries, passphrase, *, key_id=None):
    context = owner_context(owner)
    entries = validate_entries(entries)
    record = {'schema': SCHEMA, 'owner': owner, 'revision': uuid.uuid4().hex,
              'entries': entries}
    try:
        # The shape pass bounds the entire entries payload before encoding.
        # A streaming size guard includes owner/schema/revision overhead and
        # holds at most MAX_BYTES plus one bounded string chunk in memory.
        encoder = json.JSONEncoder(sort_keys=True, separators=(',', ':'),
                                   ensure_ascii=False, allow_nan=False)
        data = bytearray()
        for chunk in encoder.iterencode(record):
            raw = chunk.encode('utf-8')
            if len(data) + len(raw) > MAX_BYTES:
                raise RecordError('vault plaintext exceeds its bound')
            data.extend(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise RecordError('invalid vault record') from None
    return vault_codec.encrypt(bytes(data), passphrase, context=context,
                               max_plaintext=MAX_BYTES, key_id=key_id)


def decode(owner, blob, passphrase):
    decoded = vault_codec.decrypt(blob, passphrase, context=owner_context(owner),
                                  max_plaintext=MAX_BYTES, allow_legacy=True)
    value = _json_load(decoded.data)
    if decoded.format_version == 0:
        return validate_entries(value), decoded
    if (type(value) is not dict or set(value) != {'schema', 'owner', 'revision', 'entries'}
            or type(value['schema']) is not int or value['schema'] != SCHEMA
            or type(value['owner']) is not str or value['owner'] != owner
            or type(value['revision']) is not str
            or not re.fullmatch('[0-9a-f]{32}', value['revision'])):
        raise RecordError('vault record does not match its owner or schema')
    return validate_entries(value['entries']), decoded


def decode_legacy(blob, passphrase):
    """Quarantined legacy-key reads cannot accept a new owner-bound envelope."""
    decoded = vault_codec.decrypt(blob, passphrase, context=b'legacy-quarantine',
                                  max_plaintext=MAX_BYTES, allow_legacy=True)
    if decoded.format_version != 0:
        raise RecordError('owner-bound vault cannot be claimed through legacy addressing')
    return validate_entries(_json_load(decoded.data))
