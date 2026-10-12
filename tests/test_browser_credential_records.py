"""Strict browser credential records; no provider, browser or vault mutation."""
import json
import traceback

import pytest

from olympus import browser_credential_records as records


def _cookie(**fields):
    return {'name': 'sid', 'value': 'owned-private-cookie', 'domain': '.owned.test', **fields}


def test_site_bundle_detaches_exact_bounded_values_without_coercion():
    value = {'username': 'owned-user', 'password': 'owned-private-password'}
    result = records.site_bundle(value)
    assert result == value and result is not value
    result['password'] = 'changed-copy'
    assert value['password'] == 'owned-private-password'
    assert records.site_bundle({'username': 'owned-user', 'password': ''})['password'] == ''


@pytest.mark.parametrize('invalid', [None, '', {}, [], {'username': 'owned-user'},
    {'username': '', 'password': 'owned-private'}, {'username': True, 'password': 'owned-private'},
    {'username': 'owned-user', 'password': None}, {'username': 'owned-user', 'password': 'x' * 16385},
    {'username': 'owned-user', 'password': '\U0001f600' * 4097},
    {'username': 'owned-user', 'password': 'owned-private', 'extra': 'not-supported'}])
def test_invalid_site_bundle_is_not_missing(invalid):
    with pytest.raises(records.BrowserCredentialError):
        records.site_bundle(invalid)


@pytest.mark.parametrize('kind', ['bundle', 'cookie', 'site'])
def test_foreign_mappings_are_not_coerced(kind):
    calls = []
    class Foreign(dict):
        def __iter__(self):
            calls.append('iterated')
            raise AssertionError('foreign mapping')
        def items(self):
            calls.append('items')
            raise AssertionError('foreign mapping')
    with pytest.raises(records.BrowserCredentialError):
        if kind == 'bundle':
            records.cookie_bundle(Foreign(cookies=[]), 'owned.test')
        elif kind == 'cookie':
            records.cookie_bundle({'cookies': [Foreign(_cookie())]}, 'owned.test')
        else:
            records.site_bundle(Foreign(username='owned', password='owned-private'))
    assert calls == []


def test_cookie_roundtrip_preserves_known_metadata_and_detaches_nested_partition():
    partition = {'topLevelSite': 'https://other-owned.test', 'hasCrossSiteAncestor': True}
    cookie = _cookie(path='/', secure=True, httpOnly=True, sameSite='None', expires=2000000000,
        priority='High', sourceScheme='Secure', sourcePort=443, size=25,
        session=False, partitionKey=partition, partitionKeyOpaque=False, sameParty=False)
    result = records.cookie_bundle({'cookies': [cookie]}, 'app.owned.test')
    assert result == {'cookies': [cookie]}
    result['cookies'][0]['partitionKey']['topLevelSite'] = 'https://changed.test'
    assert partition['topLevelSite'] == 'https://other-owned.test'
    projected = records.cookie_parameters({'cookies': [cookie]}, 'owned.test')[0]
    assert projected == {k: v for k, v in cookie.items()
                         if k not in ('size', 'session', 'partitionKeyOpaque', 'sameParty')}


def test_empty_bundle_is_valid_and_distinct_from_missing():
    assert records.cookie_bundle({'cookies': []}, 'owned.test') == {'cookies': []}
    with pytest.raises(records.BrowserCredentialError):
        records.cookie_bundle(None, 'owned.test')


@pytest.mark.parametrize('invalid', [None, [], {}, {'cookies': None}, {'cookies': ()},
    {'cookies': [], 'extra': True}, {'cookies': ['not-a-cookie']}, {'cookies': [None]},
    {'cookies': [_cookie()] * 257}])
def test_invalid_cookie_containers_refuse_whole_bundle(invalid):
    with pytest.raises(records.BrowserCredentialError):
        records.cookie_bundle(invalid, 'owned.test')


@pytest.mark.parametrize('fields', [
    {'name': ''}, {'value': None}, {'value': 'x' * 16385}, {'domain': 'other.test'},
    {'domain': 'evilowned.test'}, {'domain': '.test/'}, {'url': 'https://other.test/'},
    {'url': 'https://owned.test@other.test/'}, {'url': 'https://owned.test:99999/'},
    {'path': 'relative'}, {'path': '/owned\x00path'}, {'secure': 1}, {'httpOnly': 'true'},
    {'session': 0}, {'partitionKeyOpaque': 1}, {'sameParty': 'false'},
    {'sameSite': 'lax'}, {'priority': 'Highest'}, {'sourceScheme': 'HTTPS'},
    {'expires': True}, {'expires': float('nan')}, {'expires': float('inf')},
    {'expires': None}, {'expires': -2}, {'expires': -0.5},
    {'expires': -0.000001}, {'session': False, 'expires': -0.5},
    {'session': False, 'expires': -0.000001}, {'session': True, 'expires': 2000000000},
    {'size': True}, {'size': -1},
    {'sourcePort': True}, {'sourcePort': 0}, {'sourcePort': 65536},
    {'partitionKey': {'topLevelSite': 'https://other-owned.test'}},
    {'partitionKey': {'topLevelSite': 'file:///owned', 'hasCrossSiteAncestor': True}},
    {'partitionKey': {'topLevelSite': 'https://other-owned.test', 'hasCrossSiteAncestor': 1}},
    {'unknownField': 'owned-private'},
])
def test_malformed_cookie_fields_are_rejected_before_any_projection(fields):
    invalid = {'cookies': [_cookie(), _cookie(**fields)]}
    with pytest.raises(records.BrowserCredentialError):
        records.cookie_parameters(invalid, 'owned.test')


def test_cookie_requires_explicit_scope_and_url_domain_agreement():
    row = {'name': 'sid', 'value': 'owned-private'}
    with pytest.raises(records.BrowserCredentialError):
        records.cookie_bundle({'cookies': [row]}, 'owned.test')
    row['url'] = 'https://app.owned.test/login'
    assert records.cookie_parameters({'cookies': [row]}, 'owned.test') == [row]
    row['domain'] = 'sibling.owned.test'
    with pytest.raises(records.BrowserCredentialError):
        records.cookie_bundle({'cookies': [row]}, 'owned.test')


def test_aggregate_limit_rejects_many_individually_valid_cookie_values():
    value = {'cookies': [_cookie(name=f'sid{i}', value='x' * 16384) for i in range(17)]}
    with pytest.raises(records.BrowserCredentialError, match='bundle exceeds'):
        records.cookie_bundle(value, 'owned.test')


@pytest.mark.parametrize('fields', [{'partitionKeyOpaque': True}, {'sameParty': True}])
def test_unrecreatable_scope_is_retained_but_never_silently_broadened(fields):
    value = {'cookies': [_cookie(**fields)]}
    assert records.cookie_bundle(value, 'owned.test') == value
    with pytest.raises(records.BrowserCredentialError, match='cannot be restored'):
        records.cookie_parameters(value, 'owned.test')


@pytest.mark.parametrize('fields', [{'expires': -1}, {'session': True, 'expires': -1}])
def test_session_cookie_projection_omits_persistent_expiration(fields):
    result = records.cookie_parameters({'cookies': [_cookie(**fields)]}, 'owned.test')
    assert 'expires' not in result[0] and 'session' not in result[0]


@pytest.mark.parametrize('invalid', ['', 'https://owned.test', 'owned.test/path', 'owned.test:443',
    'a..owned.test', '.owned.test', 'owned.test.', 'owned.test\x00', 'x' * 254])
def test_domain_selection_is_explicit_and_bounded(invalid):
    with pytest.raises(records.BrowserCredentialError):
        records.domain_name(invalid)


def test_ascii_alabel_and_ip_scope_remain_exact_without_dns():
    assert records.domain_name(' XN--BCHER-KVA.example ') == 'xn--bcher-kva.example'
    value = {'cookies': [_cookie(domain='127.0.0.1')]}
    assert records.cookie_bundle(value, '127.0.0.1') == value
    with pytest.raises(records.BrowserCredentialError):
        records.cookie_bundle(value, 'evil.127.0.0.1')


def test_validation_failure_has_no_private_record_or_raw_exception_context():
    private_value = {'cookies': [_cookie(value='owned-private-\ud800')]}
    with pytest.raises(records.BrowserCredentialError) as raised:
        records.cookie_bundle(private_value, 'owned.test')
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert 'owned-private-' not in ''.join(traceback.format_exception(raised.value))


@pytest.mark.parametrize('host', ['faß.de', 'fass.。de', 'owned‍.test', '１２７.0.0.1', 'K.test',
    '127.1', '0177.0.0.1', '0x7f.0.0.1', '2130706433', 'owned.0x', 'owned.123'])
def test_ambiguous_browser_hosts_refuse_selection_cookie_and_url(host):
    with pytest.raises(records.BrowserCredentialError):
        records.domain_name(host)
    with pytest.raises(records.BrowserCredentialError):
        records.cookie_parameters({'cookies': [_cookie(domain=host)]}, 'owned.test')
    row = {'name': 'sid', 'value': 'owned-private', 'url': 'https://' + host + '/'}
    with pytest.raises(records.BrowserCredentialError):
        records.cookie_parameters({'cookies': [row]}, 'owned.test')


def test_normalized_cookie_domain_is_the_same_wire_scope_checked():
    result = records.cookie_parameters({'cookies': [_cookie(domain='.OWNED.TEST')]}, 'owned.test')
    assert result[0]['domain'] == '.owned.test'
    result = records.cookie_parameters({'cookies': [_cookie(domain='OWNED.TEST')]}, 'owned.test')
    assert result[0]['domain'] == 'owned.test'


@pytest.mark.parametrize('url', [' https://owned.test/', 'https://own\ned.test/',
    'https://owned.test/\tpath', 'https://owned.test/with space', 'https://owned.test/\x7f'])
def test_url_parser_must_not_silently_remove_controls_or_whitespace(url):
    row = {'name': 'sid', 'value': 'owned-private', 'url': url}
    with pytest.raises(records.BrowserCredentialError):
        records.cookie_parameters({'cookies': [row]}, 'owned.test')


@pytest.mark.parametrize('fields', [{'session': False}, {'session': False, 'expires': -1}])
def test_explicit_persistent_cookie_never_silently_becomes_session_cookie(fields):
    with pytest.raises(records.BrowserCredentialError):
        records.cookie_parameters({'cookies': [_cookie(**fields)]}, 'owned.test')


@pytest.mark.parametrize('site', ['https://other-owned.test/', 'https://other-owned.test/path',
    'https://other-owned.test?query', 'https://other-owned.test:443',
    'https://other-owned.test:8443', 'HTTPS://OTHER-OWNED.TEST',
    'https://other-owned.test#fragment', 'https://user@other-owned.test'])
def test_partition_site_requires_canonical_scheme_host_shape(site):
    value = {'cookies': [_cookie(partitionKey={'topLevelSite': site, 'hasCrossSiteAncestor': False})]}
    with pytest.raises(records.BrowserCredentialError):
        records.cookie_bundle(value, 'owned.test')


def _encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))


def test_cookie_encoded_aggregate_boundary_is_exact():
    value = {'cookies': [_cookie(name=f'sid{i}', value='') for i in range(17)]}
    remaining = records.MAX_BUNDLE_BYTES - _encoded_size(value)
    for cookie in value['cookies']:
        amount = min(remaining, records.MAX_CREDENTIAL_BYTES)
        cookie['value'] = 'x' * amount
        remaining -= amount
    assert remaining == 0 and _encoded_size(value) == records.MAX_BUNDLE_BYTES
    assert records.cookie_bundle(value, 'owned.test') == value
    value['cookies'][-1]['value'] += 'x'
    assert _encoded_size(value) == records.MAX_BUNDLE_BYTES + 1
    assert len(value['cookies'][-1]['value']) <= records.MAX_CREDENTIAL_BYTES
    with pytest.raises(records.BrowserCredentialError, match='bundle exceeds'):
        records.cookie_bundle(value, 'owned.test')


@pytest.mark.parametrize('payload,count', [('"' * 16000, 9), ('\x01' * 8000, 6), ('\U0001f600' * 4000, 17)], ids=['escaped-quotes', 'escaped-controls', 'utf8-four-byte'])
def test_cookie_aggregate_counts_escaped_and_utf8_bytes(payload, count):
    value = {'cookies': [_cookie(name=f'sid{i}', value=payload) for i in range(count)]}
    assert len(payload.encode('utf-8')) <= records.MAX_CREDENTIAL_BYTES
    assert _encoded_size(value) > records.MAX_BUNDLE_BYTES
    with pytest.raises(records.BrowserCredentialError, match='bundle exceeds'):
        records.cookie_bundle(value, 'owned.test')


def test_exact_cookie_count_and_utf8_field_boundaries_are_supported():
    value = {'cookies': [_cookie(name=f'sid{i}') for i in range(records.MAX_COOKIES)]}
    assert records.cookie_bundle(value, 'owned.test') == value
    password = '\U0001f600' * (records.MAX_CREDENTIAL_BYTES // 4)
    assert records.site_bundle({'username': 'owned-user', 'password': password})['password'] == password
    value = {'cookies': [_cookie(value=password)]}
    assert records.cookie_bundle(value, 'owned.test') == value
