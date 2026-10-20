"""Bounded exact-owner operator policy read through a captured native root.

Missing policy is first-use state; corrupt, unreadable, ambiguous gateway or
unresolved legacy evidence refuses credentials. No ambient config override is
used. Preference coordination can be created only after the captured root
identity is verified; policy and recovery directories are never created here.
"""
from dataclasses import dataclass
from functools import wraps
import hashlib
import os

from . import browserlease, browser_credential_authority as authority
from . import browser_credential_records as records
from . import config, prefs, prefs_native, providers, security



def _guard(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        failed = False
        cleanup_unknown = False
        try:
            return function(*args, **kwargs)
        except BaseException as error:
            cleanup_unknown = authority._cleanup_unknown(error) or prefs_native.cleanup_unknown(error)
            if not isinstance(error, Exception):
                if cleanup_unknown:
                    # Preserve the actual KI/SystemExit object even when its
                    # notes attribute is malformed. No add_note callback here.
                    attributes = BaseException.__getattribute__(error, '__dict__')
                    if type(attributes) is dict:
                        attributes[authority._CLEANUP_KEY] = True
                raise
            failed = True
        if failed:
            error = records.BrowserCredentialError('browser credential authorization is invalid or unavailable')
            if cleanup_unknown:
                authority._mark_cleanup_unknown(error)
            raise error from None
    return guarded




def _paths(owner):
    return prefs.credential_paths(owner)


def _digest(raw):
    return None if raw is None else hashlib.sha256(raw).digest()


@dataclass(frozen=True, repr=False)
class Policy:
    owner: str
    site: str
    root: object
    root_identity: tuple
    parts: tuple
    legacy_parts: tuple | None
    digest: bytes | None
    file_identity: tuple | None
    allowed: bool
    manual: bool
    sovereign: bool
    allowlist: tuple
    local_hosts: frozenset
    preference_view: object = None

    @_guard
    def permits_destination(self, host):
        selected = records.domain_name(host)
        if not self.allowed or not (selected == self.site or selected.endswith('.' + self.site)):
            return False
        return (not self.sovereign or security._host_is_loopback(selected)
                or selected in self.local_hosts
                or any(security._entry_matches(selected, item) for item in self.allowlist))

    @_guard
    def verify(self, captured):
        if type(captured) is not authority.Authority:
            raise records.BrowserCredentialError('browser credential policy authority is invalid')
        captured.check()
        if captured.root != self.root or captured.root_identity != self.root_identity:
            raise records.BrowserCredentialError('browser credential policy root changed')
        if type(self.preference_view) is not prefs.Snapshot:
            raise records.BrowserCredentialError('browser credential preference view is unbound')
        observed = prefs._capture_for_credentials(self.owner, captured.root, captured.root_identity,
                                                  captured=self.preference_view)
        if observed.quarantined:
            raise records.BrowserCredentialError('browser credential policy requires reconciliation')
        observed.data()  # Bounded strict record validation is part of authority.
        captured.check()


@_guard
def snapshot(owner, domain, captured):
    name, site = browserlease.require_owner(owner), records.domain_name(domain)
    if type(captured) is not authority.Authority:
        raise records.BrowserCredentialError('browser credential policy authority is invalid')
    captured.check()
    # Freeze only the policy inputs used by this operation. No raw environment
    # or provider credentials are retained or reported.
    enabled = os.environ.get('OLYMPUS_OPERATOR', '').strip().lower() in ('1', 'true', 'yes', 'on')
    raw_domains = os.environ.get('OLYMPUS_OPERATOR_DOMAINS', '')
    if len(raw_domains) > 65536:
        raise records.BrowserCredentialError('browser operator domain policy exceeds its bound')
    domains = tuple(records.domain_name(item) for item in raw_domains.split(',') if item.strip())
    if len(domains) > 256:
        raise records.BrowserCredentialError('browser operator domain policy exceeds its bound')
    sovereign = config.sovereign_mode()
    allowlist = tuple(config.egress_allowlist())
    if len(allowlist) > 256 or any(type(item) is not str or len(item) > 4096 for item in allowlist):
        raise records.BrowserCredentialError('browser egress policy exceeds its bound')
    local_hosts = frozenset(providers.local_provider_hosts())
    parts, legacy_parts = _paths(name)
    observed = prefs._capture_for_credentials(name, captured.root, captured.root_identity)
    if observed.quarantined:
        raise records.BrowserCredentialError('browser credential preferences require reconciliation')
    data = observed.data()
    raw, identity = observed.raw, observed.identity
    captured.check()
    operator = data.get('operator', {})
    if type(operator) is not dict or type(operator.get('enabled', False)) is not bool:
        raise records.BrowserCredentialError('browser operator preference is invalid')
    sites = operator.get('sites', {})
    if type(sites) is not dict or len(sites) > 256:
        raise records.BrowserCredentialError('browser operator site policy is invalid')
    for key, value in sites.items():
        if records.domain_name(key) != key or type(value) is not dict or value.get('login', 'manual') not in ('manual', 'remember'):
            raise records.BrowserCredentialError('browser operator site policy is invalid')
    permitted_site = any(site == item or site.endswith('.' + item) for item in (*domains, *sites))
    permitted_egress = (not sovereign or security._host_is_loopback(site) or site in local_hosts
                        or any(security._entry_matches(site, item) for item in allowlist))
    selected = Policy(name, site, captured.root, captured.root_identity, parts, legacy_parts,
                      _digest(raw), identity, (enabled or operator.get('enabled', False)) and permitted_site and permitted_egress,
                      site in sites and sites[site].get('login', 'manual') == 'manual',
                      sovereign, allowlist, local_hosts, observed)
    selected.verify(captured)
    return selected
