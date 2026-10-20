"""Owned preference JSON shape/resource controls, independent of disk policy."""
import json

import pytest

from olympus import prefs_records as records


def test_json_values_round_trip_and_detach_nested_mutable_containers():
    value = {'enabled': True, 'empty': None, 'budget': 1.25, 'limit': 0,
             'language': 'français', 'scopes': ['notes', {'nested': [False, -7]}]}
    copied = records.detach(value)
    assert copied == value and copied is not value
    value['scopes'][1]['nested'].append('later')
    assert copied['scopes'][1]['nested'] == [False, -7]
    assert records.decode(records.encode(copied)) == copied


@pytest.mark.parametrize('raw', [
    b'{"authority": 1, "authority": 0}',
    b'{"nested": {"authority": 1, "authority": 0}}',
    b'{"x": NaN}', b'{"x": Infinity}', b'{"x": -Infinity}', b'{"x": 1e9999}',
    b'{"x": "\\ud800"}', b'{"x": "\\udfff"}',
    b'\xff', b'[]', b'null', b'"object"', b'{', b'{} trailing', b'',
])
def test_invalid_raw_evidence_is_refused_without_input_in_error(raw):
    with pytest.raises(records.PreferenceRecordError) as raised:
        records.decode(raw)
    assert str(raised.value) == 'preference object is invalid or exceeds its bounds'


@pytest.mark.parametrize('value', [
    {'x': float('nan')}, {'x': float('inf')}, {'x': float('-inf')},
    {'x': b'bytes'}, {'x': ('tuple',)}, {1: 'integer key'}, {'x': '\ud800'},
    {'x': 1 << records.MAX_INTEGER_BITS},
])
def test_invalid_write_values_are_refused(value):
    with pytest.raises(records.PreferenceRecordError):
        records.encode(value)


def test_container_and_scalar_subclasses_cannot_supply_conversion_hooks():
    class Object(dict):
        def items(self): pytest.fail('custom mapping hook reached')
    class Text(str):
        def encode(self, *args): pytest.fail('custom string hook reached')
    class Number(int):
        def bit_length(self): pytest.fail('custom numeric hook reached')
    for value in (Object(), {'x': Object()}, {'x': Text('text')},
                  {Text('key'): 1}, {'x': Number(1)}):
        with pytest.raises(records.PreferenceRecordError):
            records.encode(value)


def test_cycles_refused_but_shared_acyclic_values_are_detached():
    recursive = []
    recursive.append(recursive)
    with pytest.raises(records.PreferenceRecordError):
        records.encode({'x': recursive})
    shared = [1]
    result = records.detach({'a': shared, 'b': shared})
    result['a'].append(2)
    assert result['b'] == [1] and shared == [1]


def test_utf8_string_limit_is_bytes_and_applies_to_keys_and_values():
    at_limit = 'é' * (records.MAX_STRING_BYTES // 2)
    assert records.decode(records.encode({'x': at_limit}))['x'] == at_limit
    assert records.decode(records.encode({at_limit: None})) == {at_limit: None}
    for value in ({'x': at_limit + 'é'}, {at_limit + 'é': 1}):
        with pytest.raises(records.PreferenceRecordError): records.encode(value)
        with pytest.raises(records.PreferenceRecordError):
            records.decode(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def test_node_limit_includes_dictionary_keys_and_values():
    # One root, one key, one list, then 8189 scalar elements.
    value = {'x': [0] * (records.MAX_NODES - 3)}
    assert records.decode(records.encode(value)) == value
    value['x'].append(0)
    with pytest.raises(records.PreferenceRecordError): records.encode(value)
    with pytest.raises(records.PreferenceRecordError):
        records.decode(json.dumps(value).encode())


def test_depth_limit_precedes_parser_and_handles_brackets_inside_strings(monkeypatch):
    value = 0
    for _ in range(records.MAX_DEPTH - 1): value = [value]
    assert records.decode(records.encode({'x': value})) == {'x': value}
    too_deep = b'{"x":' + b'[' * records.MAX_DEPTH + b'0' + b']' * records.MAX_DEPTH + b'}'
    with pytest.raises(records.PreferenceRecordError): records.encode({'x': [value]})
    with monkeypatch.context() as patch:
        patch.setattr(records.json, 'loads', lambda *a, **k: pytest.fail('overdeep parser reached'))
        with pytest.raises(records.PreferenceRecordError): records.decode(too_deep)
    tricky = {'x': '[{' * 100 + '\\"' + ']}' * 100}
    assert records.decode(records.encode(tricky)) == tricky


def test_total_read_bound_is_checked_before_decoder(monkeypatch):
    monkeypatch.setattr(records.json, 'loads', lambda *a, **k: pytest.fail('oversize parser reached'))
    with pytest.raises(records.PreferenceRecordError):
        records.decode(b' ' * (records.MAX_BYTES + 1))


def test_total_encoding_is_bounded_before_building_full_serialized_blob():
    value = {str(index): 'x' * records.MAX_STRING_BYTES for index in range(17)}
    with pytest.raises(records.PreferenceRecordError): records.encode(value)
    value.pop('16')
    value.pop('15')
    assert len(records.encode(value)) < records.MAX_BYTES


def test_duplicate_keys_are_detected_after_json_escape_decoding():
    with pytest.raises(records.PreferenceRecordError):
        records.decode(b'{"scope": 0, "\\u0073cope": 1}')


def test_integer_limit_round_trips_at_boundary_and_rejects_oversized_raw():
    value = {'x': (1 << records.MAX_INTEGER_BITS) - 1}
    assert records.decode(records.encode(value)) == value
    with pytest.raises(records.PreferenceRecordError):
        records.decode(json.dumps({'x': 1 << records.MAX_INTEGER_BITS}).encode())


@pytest.mark.parametrize('raw', [
    b'{"private": "owned-private-input"',
    b'{"private": "owned-private-input\xff"}',
    b'{"private": "\\ud800"}',
])
def test_decode_error_does_not_retain_parser_or_unicode_input_in_exception_chain(raw):
    with pytest.raises(records.PreferenceRecordError) as raised:
        records.decode(raw)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert not hasattr(raised.value, 'doc') and not hasattr(raised.value, 'object')


def test_write_surrogate_error_does_not_retain_unicode_input_in_exception_chain():
    with pytest.raises(records.PreferenceRecordError) as raised:
        records.encode({'private': 'owned-private-input\ud800'})
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert not hasattr(raised.value, 'object')


def test_changed_integer_digit_policy_has_fixed_context_free_encoding_error():
    import sys
    original = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        with pytest.raises(records.PreferenceRecordError) as raised:
            records.encode({'integer': (1 << records.MAX_INTEGER_BITS) - 1})
        assert str(raised.value) == 'preference object is invalid or exceeds its bounds'
        assert raised.value.__cause__ is None and raised.value.__context__ is None
    finally:
        sys.set_int_max_str_digits(original)


def test_node_budget_precedes_parser_for_flat_array_and_dictionary(monkeypatch):
    raw_array = b'{"x":[' + b','.join([b'0'] * records.MAX_NODES) + b']}'
    raw_object = json.dumps({str(i): 0 for i in range(records.MAX_NODES // 2)}).encode()
    monkeypatch.setattr(records.json, 'loads', lambda *a, **k: pytest.fail('over-budget tree parser reached'))
    for raw in (raw_array, raw_object):
        assert len(raw) < records.MAX_BYTES
        with pytest.raises(records.PreferenceRecordError): records.decode(raw)


def test_aggregate_text_budget_bounds_work_before_encoder(monkeypatch):
    value = {'x': ['x' * records.MAX_STRING_BYTES] * (records.MAX_NODES - 3)}
    original = records._string
    calls = []
    def count(item):
        calls.append(len(item))
        return original(item)
    monkeypatch.setattr(records, '_string', count)
    monkeypatch.setattr(records.json.JSONEncoder, 'iterencode', lambda *a, **k: pytest.fail('oversized detached text reached encoder'))
    with pytest.raises(records.PreferenceRecordError): records.encode(value)
    assert len(calls) <= 18
    assert sum(calls) <= records.MAX_BYTES + records.MAX_STRING_BYTES


def test_escaped_controls_obey_total_encoded_byte_budget():
    value = {'x': ['\x00' * records.MAX_STRING_BYTES] * 3}
    with pytest.raises(records.PreferenceRecordError): records.encode(value)
    value['x'].pop()
    assert len(records.encode(value)) <= records.MAX_BYTES


def test_bytes_subclass_and_non_object_roots_are_refused_without_hooks():
    class Raw(bytes):
        def decode(self, *a, **k): pytest.fail('raw subclass hook reached')
    with pytest.raises(records.PreferenceRecordError): records.decode(Raw(b'{}'))
    for value in ([], None, True, 7, 'object', b'{}'):
        with pytest.raises(records.PreferenceRecordError): records.encode(value)


def test_negative_integer_boundary():
    value = {'x': -((1 << records.MAX_INTEGER_BITS) - 1)}
    assert records.decode(records.encode(value)) == value
    with pytest.raises(records.PreferenceRecordError): records.encode({'x': -(1 << records.MAX_INTEGER_BITS)})


@pytest.mark.parametrize('negative', [False, True])
def test_integer_token_budget_precedes_conversion_with_global_limit_disabled(monkeypatch, negative):
    import sys
    original = sys.get_int_max_str_digits()
    seen = []
    parser_integer = records._integer
    def observed(token):
        seen.append(len(token))
        return parser_integer(token)
    def conversion_forbidden(*args):
        pytest.fail('oversized integer reached conversion')
    raw = b'{"x":' + (b'-' if negative else b'') + b'9' * (records.MAX_BYTES - 16) + b'}'
    try:
        sys.set_int_max_str_digits(0)
        with monkeypatch.context() as patch:
            patch.setattr(records, '_integer', observed)
            patch.setattr(records, 'int', conversion_forbidden, raising=False)
            with pytest.raises(records.PreferenceRecordError) as raised:
                records.decode(raw)
            assert len(seen) == 1 and seen[0] > records.MAX_INTEGER_DIGITS
            assert raised.value.__cause__ is None and raised.value.__context__ is None
    finally:
        sys.set_int_max_str_digits(original)
