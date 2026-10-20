"""Captured browser credential workflows with retained local-only settlement.

The bounded registry survives only in this process. It is not recovery from
process death, a lost browser response, or an external account state change.
Unresolved entries are never expired or evicted to admit another action.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
import os
import secrets
import threading

from . import browser, browserlease, browser_credential_authority as authority
from . import browser_credential_records as records, vault
from . import browser_credential_profiles as profiles, browser_credential_policy as policies

_MAX_WORKFLOWS = 64
_PID = os.getpid()
_LOCK = threading.RLock()
_WORKFLOWS = OrderedDict()
_ACTIVE = {}
_STAGES = ('factory', 'login', 'cookies', 'injection', 'vault', 'telemetry', 'authorization')


class BrowserCredentialWorkflowError(RuntimeError):
    pass


@dataclass(repr=False)
class _Attempt:
    identity: str
    owner: str
    domain: str
    operation: str
    root: object
    root_identity: tuple
    pid: int
    thread: int
    binding: object = None
    policy: object = None
    pending: object = None
    busy: bool = True
    complete: bool = False
    cleanup_unconfirmed: bool = False
    stopped: bool = False
    login_result: bool | None = None
    stages: dict = field(default_factory=lambda: dict.fromkeys(_STAGES, 'not_attempted'))


def _check_process():
    if os.getpid() != _PID:
        raise BrowserCredentialWorkflowError('browser credential workflow belongs to another process')


def _check_attempt(attempt):
    _check_process()
    if (type(attempt) is not _Attempt or attempt.pid != os.getpid()
            or attempt.thread != threading.get_ident()
            or _WORKFLOWS.get(attempt.identity) is not attempt):
        raise BrowserCredentialWorkflowError('browser credential workflow requires its original executor')


def _public(attempt):
    # Exact known fields only: no exception, root, owner, cookies or vault data.
    return {'workflow_id': attempt.identity, 'domain': attempt.domain,
            'operation': attempt.operation, 'complete': attempt.complete,
            'busy': attempt.busy, 'login_result': attempt.login_result,
            'cleanup_unconfirmed': attempt.cleanup_unconfirmed, 'stopped': attempt.stopped,
            **{name: attempt.stages[name] for name in _STAGES}}


def status(owner, workflow_id):
    name = browserlease.require_owner(owner)
    _check_process()
    with _LOCK:
        if type(workflow_id) is not str or len(workflow_id) != 32:
            raise BrowserCredentialWorkflowError('browser credential workflow is unavailable')
        attempt = _WORKFLOWS.get(workflow_id)
        if attempt is None or attempt.owner != name:
            raise BrowserCredentialWorkflowError('browser credential workflow is unavailable')
        return _public(attempt)


def _message(attempt):
    result = _public(attempt)
    login = ('not_attempted' if result['login_result'] is None and result['login'] == 'not_attempted'
             else 'unconfirmed' if result['login_result'] is None
             else 'succeeded' if result['login_result'] else 'failed')
    return (f"Credential workflow {result['workflow_id']} for {result['domain']}: "
            f"login={login}; cookies={result['cookies']}; injection={result['injection']}; "
            f"vault={result['vault']}; telemetry={result['telemetry']}; "
            f"authorization={result['authorization']}; factory={result['factory']}; "
            f"cleanup={'pending' if result['busy'] else 'unconfirmed' if result['cleanup_unconfirmed'] else 'confirmed'}. "
            + ('Completed.' if result['complete'] else
               'External actions will not be repeated; retained local settlement or reconciliation is required.'))


def _reserve(owner, domain, operation, captured):
    _check_process()
    captured.check()
    with _LOCK:
        key = (owner, domain)
        prior = _ACTIVE.get(key)
        if prior is not None:
            return _WORKFLOWS[prior], False
        if len(_WORKFLOWS) >= _MAX_WORKFLOWS:
            completed = next((identity for identity, item in _WORKFLOWS.items() if item.complete and not item.busy), None)
            if completed is None:
                raise BrowserCredentialWorkflowError('browser credential workflow capacity requires reconciliation')
            del _WORKFLOWS[completed]
        identity = secrets.token_hex(16)
        if identity in _WORKFLOWS:
            raise BrowserCredentialWorkflowError('browser credential workflow identity is unavailable')
        attempt = _Attempt(identity, owner, domain, operation,
                           captured.root, captured.root_identity, os.getpid(), threading.get_ident())
        _WORKFLOWS[attempt.identity] = attempt
        _ACTIVE[key] = attempt.identity
        return attempt, True


def _start(attempt, stage):
    _check_process()
    with _LOCK:
        _check_attempt(attempt)
        if not attempt.busy or attempt.stages[stage] != 'not_attempted':
            raise BrowserCredentialWorkflowError('browser credential action cannot be repeated')
        attempt.stages[stage] = 'unconfirmed'


def _confirmed(attempt, stage):
    _check_process()
    with _LOCK:
        _check_attempt(attempt)
        if not attempt.busy or attempt.stages[stage] != 'unconfirmed':
            raise BrowserCredentialWorkflowError('browser credential outcome is unavailable')
        attempt.stages[stage] = 'confirmed'


def _attach(error, attempt):
    if attempt is None:
        return
    _check_process()
    with _LOCK:
        facts = _public(attempt)
        facts.pop('domain')
        facts.pop('busy')
        attributes = BaseException.__getattribute__(error, '__dict__')
        if type(attributes) is dict:
            attributes['_browser_credential_workflow'] = facts


def _finish(attempt):
    if attempt is None:
        return
    _check_process()  # Never acquire an inherited mutex in a forked child.
    with _LOCK:
        _check_attempt(attempt)
        attempt.busy = False
        uncertain = attempt.stopped or attempt.cleanup_unconfirmed or 'unconfirmed' in attempt.stages.values()
        attempt.complete = not uncertain
        if attempt.complete:
            attempt.pending = None
            if _ACTIVE.get((attempt.owner, attempt.domain)) == attempt.identity:
                del _ACTIVE[(attempt.owner, attempt.domain)]


def _verify(attempt, captured):
    captured.verify(attempt.binding, attempt.owner)
    if (type(attempt.policy) is not policies.Policy
            or attempt.policy.owner != attempt.owner or attempt.policy.site != attempt.domain):
        raise BrowserCredentialWorkflowError('browser credential policy is unavailable')
    attempt.policy.verify(captured)


def _session(attempt, captured):
    attempt.policy.verify(captured)
    _start(attempt, 'factory')
    selected = browser.session(attempt.owner, _authority=captured)
    with _LOCK:
        attempt.binding = selected._credential_binding
    _confirmed(attempt, 'factory')
    _verify(attempt, captured)
    return selected


def _save(attempt, selected, session, captured):
    _verify(attempt, captured)
    _start(attempt, 'cookies')
    cookies = session.get_cookies(attempt.domain, _authority=captured)
    _confirmed(attempt, 'cookies')
    _verify(attempt, captured)
    bundle = records.cookie_bundle({'cookies': cookies}, attempt.domain)
    _start(attempt, 'vault')
    pending = vault.prepare_snapshot_update(selected, bundle)
    with _LOCK:
        attempt.pending = pending
    _verify(attempt, captured)
    vault.settle_snapshot_update(pending)
    _confirmed(attempt, 'vault')
    _verify(attempt, captured)


def _invoke(owner, domain, operation, body):
    retained = [None]
    result = None
    refused = False
    migration_required = False
    def reserve(name, site, requested_operation, captured):
        if requested_operation != operation:
            raise BrowserCredentialWorkflowError('browser credential operation is unavailable')
        attempt, fresh = _reserve(name, site, operation, captured)
        if fresh:
            retained[0] = attempt  # Before construction or any browser action.
        return attempt, fresh
    try:
        name = browserlease.require_owner(owner)
        selected_domain = records.domain_name(domain)
        _check_process()
        with _LOCK:
            prior = _ACTIVE.get((name, selected_domain))
            if prior is not None:
                return _message(_WORKFLOWS[prior])
        with browser._capture_credentials(name) as captured:
            # An existing durable owner remains authoritative after reset,
            # even when there is no local session object to select a binding.
            current = captured.current()
            if current is not None and browserlease.owner_of(current) != name:
                raise browserlease.OwnershipRefused()
            result, _returned_attempt = body(name, selected_domain, captured, reserve)
    except (KeyboardInterrupt, SystemExit) as error:
        attempt = retained[0]
        if attempt is not None:
            attempt.stopped = True
            if authority._cleanup_unknown(error):
                attempt.cleanup_unconfirmed = True
        _attach(error, attempt)
        raise
    except Exception as error:
        result = None
        attempt = retained[0]
        if attempt is not None:
            attempt.stopped = True
            if authority._cleanup_unknown(error):
                attempt.cleanup_unconfirmed = True
        refused = isinstance(error, browserlease.OwnershipRefused)
        migration_required = isinstance(error, vault.VaultMigrationRequired)
    finally:
        _finish(retained[0])
    if retained[0] is not None:
        return _message(retained[0])
    if result is not None:
        return result
    if migration_required:
        return 'Error: explicit vault format migration is required before saving credentials.'
    return browserlease.REFUSAL if refused else 'Error: browser credentials are invalid or unavailable.'


def save_auth(owner, domain):
    def body(name, site, captured, reserve):
        policy = policies.snapshot(name, site, captured)
        if not policy.allowed:
            return 'Error: the site is not authorized for this operation.', None
        selected = vault.snapshot(name, f'cookies:{site}', for_update=True)
        captured.check()
        attempt, fresh = reserve(name, site, 'save', captured)
        if not fresh:
            return _message(attempt), None
        attempt.policy = policy
        try:
            session = _session(attempt, captured)
            _save(attempt, selected, session, captured)
            return None, attempt
        except BaseException as error:
            _attach(error, attempt)
            raise
    return _invoke(owner, domain, 'save', body)


def restore_auth(owner, domain):
    def body(name, site, captured, reserve):
        policy = policies.snapshot(name, site, captured)
        if not policy.allowed:
            return 'Error: the site is not authorized for this operation.', None
        selected = vault.snapshot(name, f'cookies:{site}')
        captured.check()
        if selected.value is None:
            return f'No saved session for {site}.', None
        cookies = records.cookie_parameters(selected.value, site)
        attempt, fresh = reserve(name, site, 'restore', captured)
        if not fresh:
            return _message(attempt), None
        attempt.policy = policy
        try:
            session = _session(attempt, captured)
            _verify(attempt, captured)
            _start(attempt, 'injection')
            count = session.set_cookies(cookies, _authority=captured)
            if type(count) is not int or count != len(cookies):
                raise BrowserCredentialWorkflowError('browser cookie restoration is unconfirmed')
            _confirmed(attempt, 'injection')
            _verify(attempt, captured)
            return None, attempt
        except BaseException as error:
            _attach(error, attempt)
            raise
    return _invoke(owner, domain, 'restore', body)


def login(owner, domain):
    def body(name, site, captured, reserve):
        policy = policies.snapshot(name, site, captured)
        if not policy.allowed:
            return 'Error: the site is not authorized for this operation.', None
        if policy.manual:
            captured.check()
            return f'{site} uses manual sign-in. Sign in through your browser, including any 2FA.', None
        profile = profiles.get_profile(site, captured)
        captured.check()
        if profile is None or not profile.login_url:
            return f'Error: no sign-in recipe is available for {site}.', None
        batch = vault.snapshots(name, [f'site:{site}', f'cookies:{site}'])
        captured.check()
        saved = batch[f'site:{site}']
        if saved.value is None:
            return f'Error: no saved credentials are available for {site}.', None
        credentials = records.site_bundle(saved.value)
        if profile.password_selector and not credentials['password']:
            raise records.BrowserCredentialError('the sign-in recipe requires a password')
        selected = batch[f'cookies:{site}']
        if selected.value is not None:
            records.cookie_bundle(selected.value, site)
        migration = False
        try:
            vault.require_snapshot_writable(selected)
        except vault.VaultMigrationRequired:
            migration = True
        captured.check()
        attempt, fresh = reserve(name, site, 'login', captured)
        if not fresh:
            return _message(attempt), None
        attempt.policy = policy
        if migration:
            attempt.stages['vault'] = 'migration_required'
        try:
            session = _session(attempt, captured)
            _start(attempt, 'login')
            ok = session.login(profile, credentials, _authority=captured, _policy=policy)
            if type(ok) is not bool:
                raise BrowserCredentialWorkflowError('browser sign-in completion is unconfirmed')
            with _LOCK:
                attempt.login_result = ok
            _confirmed(attempt, 'login')
            _verify(attempt, captured)
            _start(attempt, 'telemetry')
            try:
                recorded = profiles.mark_outcome(site, ok, captured)
                if type(recorded) is not bool:
                    raise BrowserCredentialWorkflowError('browser telemetry completion is unconfirmed')
                if recorded:
                    _confirmed(attempt, 'telemetry')
                else:
                    attempt.stages['telemetry'] = 'not_applicable'
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as error:
                # A telemetry write may remain unconfirmed without losing the
                # login result. Uncertain lock/handle cleanup blocks more work.
                if authority._cleanup_unknown(error):
                    attempt.cleanup_unconfirmed = True
                    raise
            if ok and not migration:
                captured.check()
                _save(attempt, selected, session, captured)
            return None, attempt
        except BaseException as error:
            _attach(error, attempt)
            raise
    return _invoke(owner, domain, 'login', body)


def has_saved_auth(owner, domain):
    name, site = browserlease.require_owner(owner), records.domain_name(domain)
    selected = vault.snapshot(name, f'cookies:{site}')
    if selected.value is None:
        return False
    records.cookie_bundle(selected.value, site)
    return True


def settle(owner, workflow_id):
    name = browserlease.require_owner(owner)
    _check_process()
    with _LOCK:
        if type(workflow_id) is not str:
            raise BrowserCredentialWorkflowError('browser credential workflow is unavailable')
        attempt = _WORKFLOWS.get(workflow_id)
        if attempt is None or attempt.owner != name:
            raise BrowserCredentialWorkflowError('browser credential workflow is unavailable')
        _check_attempt(attempt)
        if attempt.busy:
            raise BrowserCredentialWorkflowError('browser credential workflow is still active')
        if attempt.pending is None or attempt.stages['vault'] == 'confirmed':
            return _message(attempt)
        attempt.busy = True
    try:
        with authority.capture(binding=attempt.binding) as captured:
            if captured.root != attempt.root or captured.root_identity != attempt.root_identity:
                raise browserlease.OwnershipRefused()
            _verify(attempt, captured)
            vault.settle_snapshot_update(attempt.pending)
            _confirmed(attempt, 'vault')
            _verify(attempt, captured)
            attempt.stopped = False
    except (KeyboardInterrupt, SystemExit) as error:
        if authority._cleanup_unknown(error):
            attempt.cleanup_unconfirmed = True
        _attach(error, attempt)
        raise
    except Exception as error:
        if authority._cleanup_unknown(error):
            attempt.cleanup_unconfirmed = True
    finally:
        _finish(attempt)
    return _message(attempt)
