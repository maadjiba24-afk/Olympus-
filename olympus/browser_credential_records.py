"""Bounded browser credential records; no browser, storage or network access.

This is an application record policy, not full cookie acceptance validation.
The browser remains responsible for cookie prefix and public-suffix rules.
Protocol fields: https://github.com/ChromeDevTools/devtools-protocol/blob/master/pdl/domains/Network.pdl
"""
from functools import wraps
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit

from . import vault_records as records

MAX_COOKIES = 256
MAX_BUNDLE_BYTES = 256 * 1024
MAX_CREDENTIAL_BYTES = 16 * 1024
_FIELDS = frozenset({'name', 'value', 'domain', 'url', 'path', 'secure', 'httpOnly',
    'sameSite', 'expires', 'priority', 'sourceScheme', 'sourcePort', 'partitionKey',
    'size', 'session', 'partitionKeyOpaque', 'sameParty'})


class BrowserCredentialError(ValueError):
    pass


def _guard(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        failed = False
        try:
            return function(*args, **kwargs)
        except BrowserCredentialError:
            raise
        except Exception:
            failed = True
        if failed:
            raise BrowserCredentialError('browser credential record is invalid or unavailable') from None
    return guarded


def _text(value, maximum, *, empty=False):
    if type(value) is not str or len(value) > maximum or (not empty and not value):
        raise BrowserCredentialError('browser credential field is invalid')
    if len(value.encode('utf-8')) > maximum:
        raise BrowserCredentialError('browser credential field exceeds its bound')
    return value


@_guard
def domain_name(value):
    """Canonical DNS/IPv4 site selector, without DNS resolution or ports."""
    value = _text(value, 253)
    if not value.isascii():
        raise BrowserCredentialError('browser credential domain requires ASCII spelling')
    value = value.strip().lower()
    if not value or any(c in value for c in '/\\:@?#%\x00'):
        raise BrowserCredentialError('browser credential domain is invalid')
    # Python's built-in IDNA2003 codec is not the browser's UTS46 policy.
    # Require ASCII wire hosts rather than checking one Unicode spelling and
    # dispatching another. International names must use their ASCII A-labels.
    host = value
    if not host.isascii() or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                                    for part in host.split('.')):
        raise BrowserCredentialError('browser credential domain is invalid')
    last = host.rsplit('.', 1)[-1]
    if last.isdecimal() or re.fullmatch(r'0x[0-9a-f]*', last):
        # Browser URL parsing accepts historical numeric IPv4 aliases. Only
        # canonical dotted decimal is admitted by this record policy.
        if str(ipaddress.IPv4Address(host)) != host:
            raise BrowserCredentialError('browser credential IPv4 spelling is invalid')
    return host


def _related(host, domain):
    try:
        ipaddress.ip_address(host)
        return host == domain
    except ValueError:
        pass
    try:
        ipaddress.ip_address(domain)
        return host == domain
    except ValueError:
        return host == domain or host.endswith('.' + domain) or domain.endswith('.' + host)


def _url_host(value):
    value = _text(value, 4096)
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise BrowserCredentialError('browser credential URL is invalid')
    parsed = urlsplit(value)
    if (not parsed.netloc.isascii() or parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise BrowserCredentialError('browser credential URL is invalid')
    # Accessing port also rejects malformed numeric/range values.
    _ = parsed.port
    return domain_name(parsed.hostname)


def _partition_site(value):
    """Require canonical scheme/host shape; no public-suffix-list claim."""
    host = _url_host(value)
    parsed = urlsplit(value)
    if parsed.port is not None or parsed.path or parsed.query or value != parsed.scheme + '://' + host:
        raise BrowserCredentialError('stored cookie partition site is invalid')
    return value


@_guard
def site_bundle(value):
    """Only inspected None means absence; callers handle it before this API."""
    if (type(value) is not dict or any(type(key) is not str for key in value)
            or set(value) != {'username', 'password'}):
        raise BrowserCredentialError('stored site credential bundle is invalid')
    username = _text(value['username'], MAX_CREDENTIAL_BYTES)
    password = _text(value['password'], MAX_CREDENTIAL_BYTES, empty=True)
    return {'username': username, 'password': password}


@_guard
def cookie_bundle(value, domain):
    """Validate and detach a complete scoped bundle; never drop malformed rows.

    Empty valid bundles remain distinct from an absent entry. Ancestor and
    descendant domain matching retains the existing browser cookie policy.
    This does not independently grant authorization for either scope.
    The encoded 256 KiB bundle ceiling is checked after the existing 4 MiB
    bounded JSON validation/copy; it is not a 256 KiB allocation ceiling.
    """
    selected = domain_name(domain)
    if (type(value) is not dict or any(type(key) is not str for key in value) or set(value) != {'cookies'}
            or type(value['cookies']) is not list or len(value['cookies']) > MAX_COOKIES):
        raise BrowserCredentialError('stored cookie bundle is invalid')
    detached = records.validate_entries({'bundle': value})['bundle']
    for cookie in detached['cookies']:
        if type(cookie) is not dict or set(cookie) - _FIELDS or not {'name', 'value'} <= set(cookie):
            raise BrowserCredentialError('stored cookie record is invalid')
        _text(cookie['name'], 4096)
        _text(cookie['value'], MAX_CREDENTIAL_BYTES, empty=True)
        scope = None
        if 'domain' in cookie:
            raw_domain = _text(cookie['domain'], 254)
            scope = domain_name(raw_domain[1:] if raw_domain.startswith('.') else raw_domain)
            cookie['domain'] = ('.' if raw_domain.startswith('.') else '') + scope
            if not _related(scope, selected):
                raise BrowserCredentialError('stored cookie scope does not match the selected site')
        if 'url' in cookie:
            host = _url_host(cookie['url'])
            if not _related(host, selected) or (scope and not (host == scope or host.endswith('.' + scope))):
                raise BrowserCredentialError('stored cookie URL scope is invalid')
        elif scope is None:
            raise BrowserCredentialError('stored cookie scope is missing')
        if 'path' in cookie:
            path = _text(cookie['path'], 4096)
            if not path.startswith('/') or '\x00' in path:
                raise BrowserCredentialError('stored cookie path is invalid')
        for name in ('secure', 'httpOnly', 'session', 'partitionKeyOpaque', 'sameParty'):
            if name in cookie and type(cookie[name]) is not bool:
                raise BrowserCredentialError('stored cookie flag is invalid')
        for name, allowed in (('sameSite', ('Strict', 'Lax', 'None')),
                ('priority', ('Low', 'Medium', 'High')), ('sourceScheme', ('Unset', 'NonSecure', 'Secure'))):
            if name in cookie and (type(cookie[name]) is not str or cookie[name] not in allowed):
                raise BrowserCredentialError('stored cookie enum is invalid')
        if 'expires' in cookie:
            expires = cookie['expires']
            if type(expires) not in (int, float) or not math.isfinite(expires) or (expires != -1 and expires < 0):
                raise BrowserCredentialError('stored cookie expiration is invalid')
            if cookie.get('session') is True and expires != -1:
                raise BrowserCredentialError('stored cookie session and expiration disagree')
        if cookie.get('session') is False and ('expires' not in cookie or cookie['expires'] == -1):
            raise BrowserCredentialError('stored persistent cookie expiration is missing')
        if 'size' in cookie and (type(cookie['size']) is not int or not 0 <= cookie['size'] <= MAX_BUNDLE_BYTES):
            raise BrowserCredentialError('stored cookie size is invalid')
        if 'sourcePort' in cookie and (type(cookie['sourcePort']) is not int
                or not (cookie['sourcePort'] == -1 or 1 <= cookie['sourcePort'] <= 65535)):
            raise BrowserCredentialError('stored cookie source port is invalid')
        if 'partitionKey' in cookie:
            partition = cookie['partitionKey']
            if (type(partition) is not dict or set(partition) != {'topLevelSite', 'hasCrossSiteAncestor'}
                    or type(partition['hasCrossSiteAncestor']) is not bool):
                raise BrowserCredentialError('stored cookie partition is invalid')
            _partition_site(partition['topLevelSite'])
    if len(json.dumps(detached, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')) > MAX_BUNDLE_BYTES:
        raise BrowserCredentialError('stored cookie bundle exceeds its bound')
    return detached


@_guard
def cookie_parameters(value, domain):
    """Project validated saved cookies to writable protocol fields.

    Opaque partition identity cannot be recreated. Unsupported historical
    same-party scope refuses; it must not silently become ordinary scope.
    """
    cookies = cookie_bundle(value, domain)['cookies']
    result = []
    for cookie in cookies:
        if cookie.get('partitionKeyOpaque') or cookie.get('sameParty'):
            raise BrowserCredentialError('stored cookie scope cannot be restored')
        row = {name: field for name, field in cookie.items()
               if name not in ('size', 'session', 'partitionKeyOpaque', 'sameParty')}
        if cookie.get('session') or cookie.get('expires') == -1:
            row.pop('expires', None)
        result.append(row)
    return result


@_guard
def acquired_cookies(value, domain=''):
    """Validate the entire bounded response before selecting a site's cookies.

    A malformed unrelated row also refuses the response; filtering must not
    turn malformed acquisition into an apparently absent or empty session.
    The full response uses the same 4 MiB copy / 256 KiB encoded policy as a
    stored bundle. These are record bounds, not network allocation limits.
    """
    if type(domain) is not str:
        raise BrowserCredentialError('browser cookie site is invalid')
    site = domain_name(domain) if domain else None
    if (type(value) is not dict or any(type(key) is not str for key in value)
            or set(value) != {'cookies'} or type(value['cookies']) is not list
            or len(value['cookies']) > MAX_COOKIES):
        raise BrowserCredentialError('browser cookie response is invalid')
    copied = records.validate_entries({'bundle': value})['bundle']
    if len(json.dumps(copied, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')) > MAX_BUNDLE_BYTES:
        raise BrowserCredentialError('browser cookie response exceeds its bound')
    result = []
    for cookie in copied['cookies']:
        if type(cookie) is not dict:
            raise BrowserCredentialError('browser cookie record is invalid')
        if 'domain' in cookie:
            raw = _text(cookie['domain'], 254)
            scope = domain_name(raw[1:] if raw.startswith('.') else raw)
        elif 'url' in cookie:
            scope = _url_host(cookie['url'])
        else:
            raise BrowserCredentialError('browser cookie scope is missing')
        row = cookie_bundle({'cookies': [cookie]}, scope)['cookies'][0]
        if site is None or _related(scope, site):
            result.append(row)
    return result
