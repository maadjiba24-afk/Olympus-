"""Native captured-root profile reads and observational login telemetry.

This does not migrate the general profile store or establish cross-writer CAS.
Only cooperating credential workflows use the captured lease lock here.
"""
from functools import wraps
import json
import math

from . import browser, browser_credential_authority as authority
from . import browser_credential_records as records, vault_records
from . import native_cleanup

_FILE = 'site_profiles.json'
_MAX_BYTES = 1024 * 1024
_MAX_PROFILES = 256
_FIELDS = frozenset({'domain', 'login_url', 'username_selector', 'password_selector',
    'submit_selector', 'success_selector', 'source', 'author', 'created', 'runs',
    'successes', 'templates', 'content_hash', 'reliability'})


def _guard(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        failed = False
        cleanup_unknown = False
        try:
            return function(*args, **kwargs)
        except BaseException as error:
            cleanup_unknown = authority._cleanup_unknown(error)
            captured = kwargs.get('captured', args[-1] if args else None)
            if cleanup_unknown and type(captured) is authority.Authority:
                captured._cleanup_unconfirmed = True
            if not isinstance(error, Exception):raise
            failed = True
        if failed:
            error = records.BrowserCredentialError('browser credential profile is invalid or unavailable')
            if cleanup_unknown:
                authority._mark_cleanup_unknown(error)
            raise error from None
    return guarded


def _check(captured):
    if type(captured) is not authority.Authority:
        raise records.BrowserCredentialError('browser credential profile authority is invalid')
    captured.check()


def _profile(row):
    if type(row) is not dict or set(row) - _FIELDS or 'domain' not in row:
        raise records.BrowserCredentialError('browser credential profile shape is invalid')
    row = dict(row)
    row['domain'] = records.domain_name(row['domain'])
    for name in ('login_url', 'username_selector', 'password_selector', 'submit_selector',
                 'success_selector', 'source', 'author', 'created'):
        if name in row:
            records._text(row[name], 4096, empty=True)
    if row.get('login_url'):
        host = records._url_host(row['login_url'])
        # A recipe cannot send credentials to a separate site. This mirrors
        # authorization's exact-or-descendant host scope, not cookie ancestry.
        if not (host == row['domain'] or host.endswith('.' + row['domain'])):
            raise records.BrowserCredentialError('browser sign-in URL is outside its authorized site')
    for name in ('runs', 'successes'):
        if name in row and (type(row[name]) is not int or not 0 <= row[name] <= 2 ** 53 - 1):
            raise records.BrowserCredentialError('browser credential profile counter is invalid')
    if row.get('successes', 0) > row.get('runs', 0):
        raise records.BrowserCredentialError('browser credential profile counters disagree')
    if 'templates' in row and type(row['templates']) is not dict:
        raise records.BrowserCredentialError('browser credential profile templates are invalid')
    if 'content_hash' in row:
        records._text(row['content_hash'], 128, empty=True)
    if 'reliability' in row and (type(row['reliability']) not in (int, float)
            or not math.isfinite(row['reliability']) or not 0 <= row['reliability'] <= 1):
        raise records.BrowserCredentialError('browser credential profile reliability is invalid')
    return browser.SiteProfile.from_dict(row)


@_guard
def _load(captured):
    _check(captured)
    try:
        raw, _identity = captured.directory.read_with_identity(_FILE, _MAX_BYTES)
    except FileNotFoundError as error:
        if authority._cleanup_unknown(error): raise
        _check(captured)
        return []
    decoded = json.loads(raw.decode('utf-8'))
    if type(decoded) is not list or len(decoded) > _MAX_PROFILES:
        raise records.BrowserCredentialError('browser credential profile list is invalid')
    copied = vault_records.validate_entries({'profile_store': {'profiles': decoded}})['profile_store']['profiles']
    profiles = [_profile(row) for row in copied]
    if len({profile.domain for profile in profiles}) != len(profiles):
        raise records.BrowserCredentialError('browser credential profiles repeat a site')
    _check(captured)
    return profiles


@_guard
def get_profile(domain, captured):
    site = records.domain_name(domain)
    local = _load(captured)
    selected = next((profile for profile in local if profile.domain == site), None)
    if selected is None:
        for builtin in browser.builtin_profiles():
            if builtin.domain == site:
                selected = _profile(builtin.to_dict())
                break
    _check(captured)
    return selected


@_guard
def mark_outcome(domain, success, captured):
    if type(success) is not bool:
        raise records.BrowserCredentialError('browser login outcome is invalid')
    site = records.domain_name(domain)
    _check(captured)
    with captured.lock():
        profiles = _load(captured)
        selected = next((profile for profile in profiles if profile.domain == site), None)
        if selected is None:
            return False
        if selected.runs >= 2 ** 53 - 1:
            raise records.BrowserCredentialError('browser credential profile counter is full')
        selected.runs += 1
        selected.successes += int(success)
        payload = json.dumps([profile.to_dict() for profile in profiles], ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(payload) > _MAX_BYTES:
            raise records.BrowserCredentialError('browser credential profiles exceed their bound')
        _check(captured)
        captured.directory.publish(_FILE, payload)
        _check(captured)
    return True
