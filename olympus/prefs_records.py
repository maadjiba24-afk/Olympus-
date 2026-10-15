"""Bounded detached JSON preference objects; no storage or policy decisions.

Values include ordinary JSON scalars, arrays and objects. This is deliberately
separate from the vault's entry schema. Errors never include input values.
"""
import json
import math

MAX_BYTES = 1024 * 1024
MAX_NODES = 8192
MAX_DEPTH = 16
MAX_STRING_BYTES = 65536
MAX_INTEGER_BITS = 4096
MAX_INTEGER_DIGITS = 1234  # Decimal digits in 2**4096 - 1.


class PreferenceRecordError(ValueError):
    pass


def _invalid():
    return PreferenceRecordError('preference object is invalid or exceeds its bounds')


def _string(value):
    if type(value) is not str or len(value) > MAX_STRING_BYTES:
        raise _invalid()
    size = None
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        pass
    # Raise after leaving the handler: suppressed contexts still retain the
    # original input in UnicodeError.object and JSONDecodeError.doc.
    if size is None or size > MAX_STRING_BYTES:
        raise _invalid()
    return size


def detach(value):
    """Copy only bounded built-in JSON containers, refusing cycles and hooks."""
    if type(value) is not dict:
        raise _invalid()
    nodes = text_bytes = 0
    active = set()

    def visit(item, depth):
        nonlocal nodes, text_bytes
        nodes += 1
        if nodes > MAX_NODES:
            raise _invalid()
        kind = type(item)
        if kind is str:
            text_bytes += _string(item)
            if text_bytes > MAX_BYTES:
                raise _invalid()
            return item
        if item is None or kind is bool:
            return item
        if kind is int:
            if item.bit_length() > MAX_INTEGER_BITS:
                raise _invalid()
            return item
        if kind is float:
            if not math.isfinite(item):
                raise _invalid()
            return item
        if kind not in (dict, list) or depth >= MAX_DEPTH or id(item) in active:
            raise _invalid()
        if len(item) > MAX_NODES - nodes:
            raise _invalid()
        active.add(id(item))
        try:
            if kind is list:
                return [visit(child, depth + 1) for child in item]
            copied = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise _invalid()
                copied[visit(key, depth + 1)] = visit(child, depth + 1)
            return copied
        finally:
            active.remove(id(item))

    try:
        return visit(value, 0)
    except RuntimeError:
        # Concurrent container modification is not a valid stable input.
        pass
    raise _invalid()


def _depth_guard(text):
    """Bound nesting and lexical value/key starts before tree allocation.

    This is a resource guard, not a second JSON parser. json.loads still
    validates syntax and duplicate keys. Counting malformed token starts may
    reject early, but cannot admit an over-budget valid JSON tree.
    """
    quoted = escaped = primitive = False
    depth = nodes = 0
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted, primitive = True, False
            nodes += 1
        elif char in '[{':
            depth += 1
            nodes += 1
            primitive = False
            if depth > MAX_DEPTH:
                raise _invalid()
        elif char in ']}':
            depth -= 1
            primitive = False
        elif char in ' \t\n\r,:':
            primitive = False
        elif not primitive:
            nodes += 1
            primitive = True
        if nodes > MAX_NODES:
            raise _invalid()


def _pairs(items):
    value = {}
    for key, child in items:
        if key in value:
            raise _invalid()
        value[key] = child
    return value


def _constant(_value):
    raise _invalid()


def _integer(token):
    # The JSON parser supplies an ASCII signed-decimal token. Bound it before
    # conversion even when the interpreter-wide digit policy is disabled.
    digits = len(token) - (1 if token.startswith('-') else 0)
    if digits > MAX_INTEGER_DIGITS:
        raise _invalid()
    value = int(token)
    if value.bit_length() > MAX_INTEGER_BITS:
        raise _invalid()
    return value


def decode(raw):
    """Decode a strict UTF-8 object; absence is handled by the storage caller."""
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise _invalid()
    try:
        text = raw.decode('utf-8')
        _depth_guard(text)
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant, parse_int=_integer)
        return detach(value)
    except (ValueError, UnicodeError, RecursionError):
        pass
    raise _invalid()


def encode(value):
    """Encode one detached object without constructing an unbounded JSON blob."""
    copied = detach(value)
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, indent=2)
    raw = bytearray()
    try:
        for chunk in encoder.iterencode(copied):
            encoded = chunk.encode('utf-8')
            if len(raw) + len(encoded) > MAX_BYTES:
                raise _invalid()
            raw.extend(encoded)
        return bytes(raw)
    except (ValueError, UnicodeError):
        pass
    raise _invalid()
