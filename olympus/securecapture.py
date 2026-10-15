"""Private remembered-login capture with retained local settlement.

Durable prompting/saving records prevent blind repetition after process loss.
Private handles are bounded, process/thread-bound and contain secrets until
their stage is confirmed. Public status contains only fixed known facts.
"""
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from functools import wraps
import os
import re
import secrets
import threading
import time

from . import prefs, prefs_native, native_cleanup, vault
from . import browser_credential_records as records

_PENDING_KEY = 'pending_secure_login'
_PID = os.getpid()
_LOCK = threading.RLock()
_MAX_WORKFLOWS = 64
_WORKFLOWS = OrderedDict()
_ACTIVE = {}
_STAGES = ('claim', 'saving', 'vault', 'authorization', 'cancellation')


class SecureCaptureError(RuntimeError):
    pass


def _cleanup_unknown(error):
    return native_cleanup.unknown(error) or prefs_native.cleanup_unknown(error)


def _operator_schema(data, new_site):
    """Pure prospective grant validation, matching browser policy acceptance."""
    settings = data.get('operator', {})
    if type(settings) is not dict or type(settings.get('enabled', False)) is not bool:
        raise SecureCaptureError('private sign-in operator policy is invalid')
    sites = settings.get('sites', {})
    if type(sites) is not dict or len(sites) > 256:
        raise SecureCaptureError('private sign-in site policy is invalid')
    for key, value in sites.items():
        if (records.domain_name(key) != key or type(value) is not dict
                or value.get('login', 'manual') not in ('manual', 'remember')):
            raise SecureCaptureError('private sign-in site policy is invalid')
    if new_site not in sites and len(sites) == 256:
        raise SecureCaptureError('private sign-in site capacity is exhausted')
    return dict(settings), dict(sites)


def _guard(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        cleanup = False
        try:
            return function(*args, **kwargs)
        except BaseException as error:
            cleanup = _cleanup_unknown(error)
            if isinstance(error, SecureCaptureError) or not isinstance(error, Exception):
                if cleanup:
                    native_cleanup.mark(error)
                raise
        error = SecureCaptureError('private sign-in capture is unavailable or requires reconciliation')
        if cleanup:
            native_cleanup.mark(error)
        raise error from None
    return guarded


def _process():
    if os.getpid() != _PID:
        raise SecureCaptureError('private sign-in capture belongs to another process')


def _identifier(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise SecureCaptureError('private sign-in request identity is invalid')
    return value


def _record(value):
    if value is None:
        return None
    if (type(value) is not dict or type(value.get('version')) is not int or value['version'] != 1
            or type(value.get('state')) is not str or value['state'] not in ('requested', 'prompting', 'saving')):
        raise SecureCaptureError('private sign-in request requires explicit reconciliation')
    keys = {'version', 'request_id', 'domain', 'state'}
    if value['state'] != 'requested':
        keys.add('claim_id')
    if set(value) != keys:
        raise SecureCaptureError('private sign-in request requires explicit reconciliation')
    request_id = _identifier(value['request_id'])
    domain = records.domain_name(value['domain'])
    if domain != value['domain']:
        raise SecureCaptureError('private sign-in request requires explicit reconciliation')
    result = {'version': 1, 'request_id': request_id, 'domain': domain, 'state': value['state']}
    if value['state'] != 'requested':
        result['claim_id'] = _identifier(value['claim_id'])
    return result


def _matching(data, request_id, domain, state, claim_id=None):
    record = _record(data.get(_PENDING_KEY))
    if (record is None or record['request_id'] != request_id
            or record['domain'] != domain or record['state'] != state):
        raise SecureCaptureError('private sign-in request changed')
    if state != 'requested' and record['claim_id'] != _identifier(claim_id):
        raise SecureCaptureError('private sign-in request belongs to another claimant')
    return record


@_guard
def request(user, domain):
    """Create or replace a requested UUID; never pre-authorize the site."""
    _process()
    owner, site = prefs._selected_owner(user), records.domain_name(domain)
    selected = prefs.capture(owner)
    if selected.quarantined:
        raise SecureCaptureError('private sign-in preferences require reconciliation')
    request_id = _identifier(secrets.token_hex(16))
    def apply(data):
        previous = _record(data.get(_PENDING_KEY))
        if previous is not None and previous['state'] != 'requested':
            raise SecureCaptureError('private sign-in request is already claimed')
        if previous is not None and previous['request_id'] == request_id:
            raise SecureCaptureError('private sign-in request identity is unavailable')
        data[_PENDING_KEY] = {'version': 1, 'request_id': request_id, 'domain': site, 'state': 'requested'}
        return data
    prefs.mutate(owner, apply, captured=selected)
    return request_id


@_guard
def pending(user):
    """Compatibility read: only an unclaimed, valid request is promptable."""
    _process()
    value = _record(prefs.get(user, _PENDING_KEY))
    return value['domain'] if value is not None and value['state'] == 'requested' else None


@_guard
def clear(user, *, request_id=None):
    """Explicitly cancel only the named currently requested UUID."""
    _process()
    owner = prefs._selected_owner(user)
    selected = prefs.capture(owner)
    if selected.quarantined:
        raise SecureCaptureError('private sign-in preferences require reconciliation')
    current = _record(selected.data().get(_PENDING_KEY))
    if current is None:
        return False
    _identifier(request_id)
    if current['request_id'] != request_id:
        return False
    if current['state'] != 'requested':
        raise SecureCaptureError('a claimed private sign-in requires its original workflow')
    def apply(data):
        _matching(data, request_id, current['domain'], 'requested')
        data[_PENDING_KEY] = None
        return data
    prefs.mutate(owner, apply, captured=selected)
    return True


@dataclass(repr=False)
class _Attempt:
    identity: str
    owner: str
    pid: int
    thread: int
    request_id: object = None
    domain: object = None
    before: object = None
    selected: object = None
    claim: object = None
    prompting_image: object = None
    saving: object = None
    saving_image: object = None
    update: object = None
    authorization: object = None
    cancellation: object = None
    busy: bool = True
    state: str = 'capturing'
    prompted: bool = False
    complete: bool = False
    cleanup_unconfirmed: bool = False
    stages: dict = field(default_factory=lambda: dict.fromkeys(_STAGES, 'not_attempted'))


def _public(attempt):
    return {'workflow_id': attempt.identity, 'request_id': attempt.request_id,
        'domain': attempt.domain, 'state': attempt.state, 'busy': attempt.busy,
        'prompt_dispatched': attempt.prompted, 'complete': attempt.complete,
        'cleanup_unconfirmed': attempt.cleanup_unconfirmed,
        **{stage: attempt.stages[stage] for stage in _STAGES}}


def _check(attempt):
    _process()
    if (type(attempt) is not _Attempt or attempt.pid != os.getpid()
            or attempt.thread != threading.get_ident() or _WORKFLOWS.get(attempt.identity) is not attempt):
        raise SecureCaptureError('private sign-in capture requires its original executor')


def _reserve(owner):
    _process()
    with _LOCK:
        prior = _ACTIVE.get(owner)
        if prior is not None:
            previous = _WORKFLOWS[prior]
            _check(previous)
            if not previous.complete or previous.busy or previous.cleanup_unconfirmed:
                return previous, False
        identity = _identifier(secrets.token_hex(16))
        if identity in _WORKFLOWS:
            raise SecureCaptureError('private sign-in workflow identity is unavailable')
        if len(_WORKFLOWS) >= _MAX_WORKFLOWS:
            completed = next((key for key, item in _WORKFLOWS.items()
                if item.complete and not item.busy and not item.cleanup_unconfirmed), None)
            if completed is None:
                raise SecureCaptureError('private sign-in capacity requires reconciliation')
            previous = _WORKFLOWS[completed]
            if _ACTIVE.get(previous.owner) == completed:
                del _ACTIVE[previous.owner]
            del _WORKFLOWS[completed]
        attempt = _Attempt(identity, owner, os.getpid(), threading.get_ident())
        _WORKFLOWS[identity] = attempt
        _ACTIVE[owner] = identity
        return attempt, True


def _selected_attempt(owner, workflow_id):
    _process()
    _identifier(workflow_id)
    attempt = _WORKFLOWS.get(workflow_id)
    if attempt is None or attempt.owner != owner:
        raise SecureCaptureError('private sign-in workflow is unavailable')
    _check(attempt)
    return attempt


@_guard
def status(user, workflow_id):
    _process()
    owner = prefs._selected_owner(user)
    with _LOCK:
        return _public(_selected_attempt(owner, workflow_id))


@_guard
def active_status(user):
    """Read the retained local reservation without a workflow ID or any I/O."""
    _process()
    owner = prefs._selected_owner(user)
    with _LOCK:
        identity = _ACTIVE.get(owner)
        return None if identity is None else _public(_selected_attempt(owner, identity))


def _finish(attempt):
    _process()
    with _LOCK:
        _check(attempt)
        attempt.busy = False
        if attempt.complete:
            attempt.before = attempt.selected = attempt.claim = attempt.prompting_image = None
            attempt.saving = attempt.saving_image = attempt.update = None
            attempt.authorization = attempt.cancellation = None
        # Keep the reservation even after success. Only a later _reserve may
        # replace/evict an entry proven complete, idle and free of uncertainty.
        # A finalizer failure after these effects can therefore never release
        # an uncertain owner or erase the public stage acknowledgments.


def _attach(error, attempt):
    facts = _public(attempt)
    facts.pop('domain')
    attributes = BaseException.__getattribute__(error, '__dict__')
    if type(attributes) is dict:
        attributes['_secure_capture_workflow'] = facts


def _run(attempt, action):
    _check(attempt)
    if not attempt.busy or attempt.cleanup_unconfirmed:
        raise SecureCaptureError('private sign-in workflow is unavailable')
    primary = None
    try:
        action()
    except BaseException as error:
        primary = error
        if _cleanup_unknown(error):
            attempt.cleanup_unconfirmed = True
            native_cleanup.mark(error)
        attempt.state = 'unconfirmed'
        if not isinstance(error, Exception):
            raise
        # Ordinary failures become fixed facts, without retaining their chain.
    finally:
        try:
            if not native_cleanup.drain([lambda: _finish(attempt)], primary):
                attempt.cleanup_unconfirmed = True
                attempt.state, attempt.complete = 'unconfirmed', False
        except BaseException as cleanup:
            attempt.cleanup_unconfirmed = True
            attempt.state, attempt.complete = 'unconfirmed', False
            if not isinstance(cleanup, Exception):
                _attach(cleanup, attempt)
                raise
            # Preserve workflow ID and acknowledged stages on ordinary
            # finalization failure. The retained reservation blocks reuse.
        if primary is not None and not isinstance(primary, Exception):
            _attach(primary, attempt)
    return _public(attempt)


def _transition(attempt, before, previous, next_state):
    request_id, domain, claim_id = attempt.request_id, attempt.domain, attempt.identity
    def apply(data):
        record = _matching(data, request_id, domain, previous, claim_id)
        data[_PENDING_KEY] = {**record, 'state': next_state, 'claim_id': claim_id}
        return data
    return prefs.prepare_mutation(attempt.owner, apply, captured=before)


def _settle_preference(attempt, name, pending):
    if attempt.stages[name] != 'confirmed':
        attempt.stages[name] = 'unconfirmed'
        facts = prefs.settle_mutation(attempt.owner, pending)
        if facts['publication_confirmed']:
            attempt.stages[name] = 'confirmed'
        if facts['cleanup_unconfirmed']:
            attempt.cleanup_unconfirmed = True
        if not facts['publication_confirmed'] or attempt.cleanup_unconfirmed:
            raise SecureCaptureError('private sign-in preference outcome is unconfirmed')
    # A historical-image getter can fail after publication was acknowledged.
    # Keep that acknowledgment and retry only this non-I/O getter later.
    if name in ('claim', 'saving'):
        return prefs._confirmed_mutation_snapshot(pending)
    return None


@_guard
def begin(user):
    """Capture authority, then confirm a durable prompt claim before any input."""
    _process()
    owner = prefs._selected_owner(user)
    attempt, fresh = _reserve(owner)
    if not fresh:
        with _LOCK:
            _check(attempt)
            return _public(attempt)
    def capture():
        selected = prefs.capture(owner)
        if selected.quarantined:
            raise SecureCaptureError('private sign-in preferences require reconciliation')
        current = _record(selected.data().get(_PENDING_KEY))
        if current is None:
            attempt.state, attempt.complete = 'no_request', True
            return
        attempt.request_id, attempt.domain = current['request_id'], current['domain']
        if current['state'] != 'requested':
            attempt.state = 'reconciliation_required'
            return
        _operator_schema(selected.data(), attempt.domain)
        attempt.before = selected
        name = 'site:' + attempt.domain
        batch = vault._snapshots_for_preferences(owner, (name,),
            root=selected.binding.root, root_identity=selected.binding.root_identity, for_update=True)
        attempt.selected = replace(batch[name], value=None)
        prefs._capture_for_credentials(owner, selected.binding.root,
            selected.binding.root_identity, captured=selected)
        attempt.claim = _transition(attempt, selected, 'requested', 'prompting')
        attempt.prompting_image = _settle_preference(attempt, 'claim', attempt.claim)
        attempt.state = 'ready'
    return _run(attempt, capture)


@_guard
def start_prompt(user, workflow_id):
    """Admit one private input call after the durable claim is confirmed."""
    _process()
    owner = prefs._selected_owner(user)
    with _LOCK:
        attempt = _selected_attempt(owner, workflow_id)
        if (attempt.busy or attempt.complete or attempt.cleanup_unconfirmed or attempt.prompted
                or attempt.state != 'ready' or attempt.stages['claim'] != 'confirmed'):
            raise SecureCaptureError('private sign-in prompt is not available')
        attempt.busy = True
    def admit():
        image = attempt.prompting_image
        prefs._capture_for_credentials(owner, image.binding.root,
            image.binding.root_identity, captured=image)
        attempt.selected._captured.check()
        attempt.prompted = True
        attempt.state = 'prompting'
    return _run(attempt, admit)


def _grant(attempt):
    request_id, domain, created = attempt.request_id, attempt.domain, int(time.time())
    def apply(data):
        _matching(data, request_id, domain, 'saving', attempt.identity)
        settings, sites = _operator_schema(data, domain)
        settings['enabled'] = True
        settings['sites'] = {**sites, domain: {'login': 'remember', 'created': created}}
        data['operator'] = settings
        data[_PENDING_KEY] = None
        return data
    return prefs.prepare_mutation(attempt.owner, apply, captured=attempt.saving_image)


def _advance(attempt):
    if attempt.claim is None:
        raise SecureCaptureError('private sign-in workflow requires explicit reconciliation')
    if attempt.stages['claim'] != 'confirmed' or attempt.prompting_image is None:
        attempt.prompting_image = _settle_preference(attempt, 'claim', attempt.claim)
    if attempt.cancellation is not None:
        _settle_preference(attempt, 'cancellation', attempt.cancellation)
        attempt.state, attempt.complete = 'canceled', True
        return
    if not attempt.prompted:
        attempt.state = 'ready'
        return
    if attempt.update is None and attempt.stages['vault'] != 'confirmed':
        raise SecureCaptureError('private input is unavailable; explicit reconciliation is required')
    if attempt.saving is None:
        attempt.saving = _transition(attempt, attempt.prompting_image, 'prompting', 'saving')
    if attempt.stages['saving'] != 'confirmed' or attempt.saving_image is None:
        attempt.saving_image = _settle_preference(attempt, 'saving', attempt.saving)
    if attempt.stages['vault'] != 'confirmed':
        image = attempt.saving_image
        prefs._capture_for_credentials(attempt.owner, image.binding.root,
            image.binding.root_identity, captured=image)
        attempt.stages['vault'] = 'unconfirmed'
        vault.settle_snapshot_update(attempt.update)
        attempt.stages['vault'] = 'confirmed'
        attempt.update = attempt.selected = None
    if attempt.authorization is None:
        attempt.authorization = _grant(attempt)
    _settle_preference(attempt, 'authorization', attempt.authorization)
    attempt.state, attempt.complete = 'complete', True


@_guard
def fulfill(user, domain, username, password, *, workflow_id=None):
    """Accept private input only through the original prepared prompt handle."""
    _process()
    owner, site = prefs._selected_owner(user), records.domain_name(domain)
    with _LOCK:
        attempt = _selected_attempt(owner, workflow_id)
        if (attempt.busy or attempt.complete or attempt.cleanup_unconfirmed or not attempt.prompted
                or attempt.state != 'prompting' or attempt.domain != site or attempt.update is not None):
            raise SecureCaptureError('private sign-in input cannot be repeated')
        attempt.busy = True
    def store_input():
        bundle = records.site_bundle({'username': username, 'password': password})
        if not bundle['password']:
            raise SecureCaptureError('private sign-in input is incomplete')
        attempt.update = vault.prepare_snapshot_update(attempt.selected, bundle)
        _advance(attempt)
    try:
        return _run(attempt, store_input)
    finally:
        username = password = None


@_guard
def cancel(user, workflow_id):
    """Cancel the original confirmed prompting image; never recapture a request."""
    _process()
    owner = prefs._selected_owner(user)
    with _LOCK:
        attempt = _selected_attempt(owner, workflow_id)
        if (attempt.busy or attempt.complete or attempt.cleanup_unconfirmed
                or attempt.stages['claim'] != 'confirmed' or attempt.saving is not None
                or attempt.update is not None or attempt.stages['vault'] != 'not_attempted'):
            raise SecureCaptureError('private sign-in cancellation requires reconciliation')
        attempt.busy = True
    def cancellation():
        if attempt.prompting_image is None:
            attempt.prompting_image = _settle_preference(attempt, 'claim', attempt.claim)
        if attempt.cancellation is None:
            request_id, domain = attempt.request_id, attempt.domain
            def apply(data):
                _matching(data, request_id, domain, 'prompting', attempt.identity)
                data[_PENDING_KEY] = None
                return data
            attempt.cancellation = prefs.prepare_mutation(owner, apply, captured=attempt.prompting_image)
        _settle_preference(attempt, 'cancellation', attempt.cancellation)
        attempt.state, attempt.complete = 'canceled', True
    return _run(attempt, cancellation)


@_guard
def settle(user, workflow_id):
    """Settle retained local intents only; never prompt or reselect authority."""
    _process()
    owner = prefs._selected_owner(user)
    with _LOCK:
        attempt = _selected_attempt(owner, workflow_id)
        if attempt.busy or attempt.cleanup_unconfirmed:
            raise SecureCaptureError('private sign-in settlement is unavailable')
        if attempt.complete:
            return _public(attempt)
        attempt.busy = True
    return _run(attempt, lambda: _advance(attempt))
