"""Per-user preferences (language, etc.), namespaced like memory.

Small JSON store so a user's choices — most importantly their language —
persist across sessions and interfaces. Kept separate from lessons/memory
because preferences are settings, not learned knowledge.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path

from . import config, memory


#: Bumped when the per-principal path changed from `users/<safe_id>/prefs.json`
#: to `prefs/owners/<owner-key>.json`. See `legacy_owners` for the migration.
PREFS_KEY_VERSION = 2

# Keep repair archives below the traditional Windows MAX_PATH boundary even
# when the exact-owner storage key and pytest's temporary root are both long.
# A 128-bit SHA-256 prefix remains content-addressed; the full digest is
# returned to the operator, and an existing prefix collision is byte-checked
# and refused rather than overwritten.
_QUARANTINE_DIGEST_HEX = 32


class PreferencesStateError(RuntimeError):
    """The current preference-policy blob exists but cannot be trusted.

    Missing is valid first-run state.  Existing-but-unreadable, malformed, or
    non-object JSON is different: treating it as ``{}`` can discard a guest
    profile, an L0 autonomy assignment, granted-scope limits, or the global
    spend cap.  Callers may clamp individual security decisions, but mutations
    must stop until an operator explicitly repairs the evidence.
    """

    def __init__(self, user: str, path: Path, reason: str) -> None:
        self.user = memory.canonical_owner(user)
        self.path = Path(path)
        self.reason = str(reason)
        self.repair_command = _repair_command(self.user)
        super().__init__(
            f"Preference policy evidence for '{self.user}' is unavailable "
            f"({self.reason}). Security decisions are clamped and writes are "
            f"refused. Inspect or preserve-and-reset it with "
            f"`{self.repair_command}`.")


def _repair_command(user: str) -> str:
    """Copy-safe command for ordinary ids; never interpolate shell syntax."""
    exact = memory.canonical_owner(user)
    if exact and all(ch.isalnum() or ch in "-_.:@" for ch in exact):
        owner_arg = exact
    else:
        owner_arg = "<exact-owner>"
    return f"olympus prefs-evidence {owner_arg} --repair"


def _io(path) -> Path:
    from .assessment_evidence import _windows_extended_path
    return Path(_windows_extended_path(path)) if os.name == "nt" else Path(path)


def _legacy_path_for(user):
    """Quarantine address only; never an exact live preference destination."""
    exact = memory.canonical_owner(user)
    if memory.is_system_owner(exact):
        return None
    return ('users', memory.safe_id(exact), 'prefs.json')


def _relative_paths(user):
    """Pure exact/legacy path selection shared by ordinary and native readers."""
    exact = memory.canonical_owner(user)
    if exact == 'shared':
        return ('prefs.json',), None
    if memory.is_system_owner(exact):
        return ('prefs', 'system', exact, 'prefs.json'), None
    return (('prefs', 'owners', memory.owner_key(exact), 'prefs.json'),
            _legacy_path_for(exact))


def credential_paths(owner):
    """Select credential policy paths without creating directories or reading state.

    A native caller must check the returned legacy path and strictly validate
    the current blob through its original held root. This API never grants
    authorization merely because an exact preference address exists.
    """
    if (type(owner) is not str or not owner or memory.canonical_owner(owner) != owner
            or len(owner.encode('utf-8')) > 4096):
        raise ValueError('credential preference owner is invalid')
    if not memory.is_system_owner(owner) and memory.is_ambiguous_gateway_owner(owner):
        raise ValueError('credential preferences require ownership reconciliation')
    return _relative_paths(owner)


def _path(user: str) -> Path:
    """Where a principal's preferences live.

    THE CENTRAL FIX. This used to be `users/<safe_id(user)>/prefs.json`, and
    preferences are not cosmetic here: this one file carries the autonomy
    level, granted action scopes, per-action daily limits, the capability
    profile (including its `max_autonomy` cap), operator settings and their
    authorized-site list, earned-autonomy opt-in and pending secure-capture
    state. `safe_id` collapses punctuation and truncates at 64 characters, so
    `tg-a.b`, `tg-a@b`, `tg-a b` and `tg-a-b` shared ONE settings file: raising
    one principal to autonomy L4 raised all of them, and granting one an action
    scope granted it to all of them.

    The reserved installation namespaces keep an explicit, stable location —
    `shared` stays exactly where it was (`MEMORY_DIR/prefs.json`, which carries
    the installation-wide daily budget cap), and any other reserved name gets
    its own file under `prefs/system/`. A tenant is keyed by
    `memory.storage_key`: the exact principal plus its complete SHA-256 digest.
    This is an explicit rule, NOT a generic tenant fallback — no tenant can
    reach a system namespace by having a name that normalizes to it.

    The tenant tree is deliberately outside `users/` and `owners/`, the two
    trees `memory._memory_roots` sweeps, so a memory export or a whole-scope
    memory delete does not carry or wipe security settings.
    """
    parts, _legacy = _relative_paths(user)
    path = config.MEMORY_DIR.joinpath(*parts)
    _io(path.parent).mkdir(parents=True, exist_ok=True)
    return path


# Uninstalled recovery integration draft. Browser/remembered-credential
# integration and exact-source acceptance remain pending.

_TRANSFORM = threading.local()


class PreferenceConflict(RuntimeError):
    """Captured evidence changed; no new replacement was dispatched in this call."""


class PreferenceOperationError(RuntimeError):
    """A native operation did not produce a confirmed public result."""


def _operation_error(cleanup):
    from . import prefs_native
    error = PreferenceOperationError('preference operation is unconfirmed; inspect before retrying')
    if cleanup:
        prefs_native._mark_cleanup(error)
    return error


def _assert_not_transform():
    from . import prefs_native
    available = True
    try:
        prefs_native._process_available()
    except prefs_native.PreferenceStorageError:
        available = False
    if not available:
        raise _operation_error(True)  # Before owner/root selectors or callbacks.
    if getattr(_TRANSFORM, 'active', False):
        raise PreferenceConflict('nested preference access during a pure mutation is refused')


@dataclass(frozen=True, repr=False)
class Snapshot:
    owner: str
    binding: object
    current_parts: tuple
    legacy_parts: object
    raw: object
    identity: object
    legacy_raw: object
    legacy_identity: object
    quarantined: bool
    recovery_stamp: tuple = ()
    recovery_pending: bool = False
    recovery_operation: object = None

    @property
    def path(self):
        return self.binding.root.joinpath(*self.current_parts)

    def data(self):
        from . import prefs_records
        if self.raw is None:
            return {}
        try:
            return prefs_records.decode(self.raw)
        except prefs_records.PreferenceRecordError:
            pass
        # Do not attach parser/Unicode errors or stored values to the state error.
        raise PreferencesStateError(self.owner, self.path, 'invalid bounded JSON object')


def _selected_owner(user):
    exact = memory.canonical_owner(user)
    if len(exact) > 4096:
        raise ValueError('preference owner exceeds its bound')
    encoded = None
    try:
        encoded = exact.encode('utf-8')
    except UnicodeError:
        pass
    if encoded is None or len(encoded) > 4096:
        raise ValueError('preference owner exceeds its bound')
    return exact


def _observe(tx, exact):
    from . import prefs_recovery_records as records
    current, legacy = _relative_paths(exact)
    raw, identity = tx.read(current)
    legacy_raw, legacy_identity = (None, None) if legacy is None else tx.read(legacy)
    failed = False
    try:
        _journal_value, stamp, pending, identifier = _recovery_observation(tx, exact)
    except records.RecoveryEvidenceError:
        failed = True
    if failed:
        raise PreferencesStateError(exact, tx.binding.root.joinpath(*current), 'recovery evidence is unavailable')
    return Snapshot(exact, tx.binding, current, legacy, raw, identity,
                    legacy_raw, legacy_identity,
                    pending or legacy is not None and (legacy_raw is not None
                        or memory.is_ambiguous_gateway_owner(exact)), stamp, pending, identifier)


def _same_evidence(left, right):
    return (left.owner == right.owner and left.binding == right.binding
            and left.current_parts == right.current_parts and left.legacy_parts == right.legacy_parts
            and left.raw == right.raw and left.identity == right.identity
            and left.legacy_raw == right.legacy_raw and left.legacy_identity == right.legacy_identity
            and left.quarantined == right.quarantined
            and left.recovery_stamp == right.recovery_stamp
            and left.recovery_pending == right.recovery_pending
            and left.recovery_operation == right.recovery_operation)


def capture(user):
    """Capture bounded raw images and native authority without mutable aliases."""
    from . import archive_io, prefs_native
    _assert_not_transform()
    exact = _selected_owner(user)
    root = archive_io.selected_directory(config.MEMORY_DIR)
    try:
        with prefs_native.transaction(root) as tx:
            observed = _observe(tx, exact)
        return observed
    except (OSError, prefs_native.PreferenceStorageError, prefs_native.storage.StorageError) as error:
        cleanup = prefs_native.cleanup_unknown(error)
    except Exception as error:
        if not prefs_native.cleanup_unknown(error):
            raise
        cleanup = True
    # Preserve KI/SystemExit identity. Ordinary native failures leave neither
    # native text nor their exception context on the public error.
    if cleanup:
        raise _operation_error(True)
    current, _legacy = _relative_paths(exact)
    raise PreferencesStateError(exact, root.joinpath(*current), 'native evidence is unavailable')


def _mutation_guard(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        from . import prefs_native, prefs_recovery_records
        try:
            return function(*args, **kwargs)
        except (OSError, prefs_native.PreferenceStorageError, prefs_native.storage.StorageError,
                prefs_recovery_records.RecoveryEvidenceError) as error:
            cleanup = prefs_native.cleanup_unknown(error)
        except Exception as error:
            if not prefs_native.cleanup_unknown(error):
                raise
            cleanup = True
        raise _operation_error(cleanup)
    return guarded


@dataclass(repr=False)
class _MutationAttempt:
    dispatched: bool = False
    confirmed: bool = False
    confirmed_identity: object = None
    cleanup_unconfirmed: bool = False


@dataclass(frozen=True, repr=False)
class PendingMutation:
    """Exact local before/after images, retained by a bounded trusted caller.

    This object belongs to the captured process/thread and is not a durable
    recovery journal or a capability for another executor. The caller must
    retain it before dispatch and must never infer completion from a thrown
    exception or rerun its transform to reconstruct it.
    """
    before: Snapshot
    after: bytes
    _attempt: _MutationAttempt = field(default_factory=_MutationAttempt, compare=False)

    def status(self):
        self.before.binding.check_executor()
        return {'publication_dispatched': self._attempt.dispatched,
                'publication_confirmed': self._attempt.confirmed,
                'cleanup_unconfirmed': self._attempt.cleanup_unconfirmed}


def _mutation_selection(user, transform, captured, clamp_keys):
    from . import archive_io
    _assert_not_transform()
    exact = _selected_owner(user)
    if (not callable(transform) or type(clamp_keys) is not tuple
            or any(type(key) is not str or key not in QUARANTINE_POSTURE for key in clamp_keys)):
        raise ValueError('invalid preference mutation selection')
    if captured is not None and (type(captured) is not Snapshot or captured.owner != exact):
        raise PreferenceConflict('captured preference owner changed')
    root = captured.binding.root if captured is not None else archive_io.selected_directory(config.MEMORY_DIR)
    return exact, root, None if captured is None else captured.binding


def _prepare_mutation(tx, exact, transform, captured, clamp_keys):
    from . import prefs_records
    before = _observe(tx, exact)
    if before.recovery_pending:
        raise PreferenceConflict('preference recovery is unresolved; ordinary mutation refused')
    if captured is not None and not _same_evidence(before, captured):
        raise PreferenceConflict('captured preference evidence changed')
    data = before.data()
    if before.quarantined:
        for key in clamp_keys:
            data[key] = _posture(key)
    _TRANSFORM.active = True
    try:
        encoded = prefs_records.encode(transform(data))
    finally:
        _TRANSFORM.active = False
    if not _same_evidence(before, _observe(tx, exact)):
        raise PreferenceConflict('preference evidence changed during mutation')
    return PendingMutation(before, encoded)


def _unchanged_quarantine(left, right):
    return (left.owner == right.owner and left.binding == right.binding
            and left.current_parts == right.current_parts and left.legacy_parts == right.legacy_parts
            and left.legacy_raw == right.legacy_raw and left.legacy_identity == right.legacy_identity
            and left.quarantined == right.quarantined
            and left.recovery_stamp == right.recovery_stamp
            and left.recovery_pending == right.recovery_pending
            and left.recovery_operation == right.recovery_operation)


def _settle_mutation(tx, pending):
    from . import prefs_recovery_records as records
    before, attempt = pending.before, pending._attempt
    observed = _observe(tx, before.owner)
    if not _unchanged_quarantine(before, observed):
        raise PreferenceConflict('captured preference authority or quarantine changed')
    if (attempt.confirmed and (observed.raw != pending.after
            or observed.identity != attempt.confirmed_identity)):
        raise PreferenceConflict('confirmed preference publication changed; replay refused')
    _confirm_recovery_authority(tx, before)
    if _same_evidence(before, observed) and not attempt.confirmed:
        attempt.dispatched = True  # Retain intent before a replace can lose its acknowledgment.
        records.checked_publish(tx, before.current_parts, pending.after)
        raw, identity = tx.read(before.current_parts)
        if raw != pending.after:
            raise _operation_error(False)  # A replacement was already dispatched.
    elif attempt.dispatched and observed.raw == pending.after:
        # Visible bytes alone are not a durability receipt. Reconfirm the
        # exact currently held image, without another replace or transform.
        tx.confirm(before.current_parts, pending.after, observed.identity)
        identity = observed.identity
    else:
        raise PreferenceConflict('captured preference evidence changed')
    attempt.confirmed_identity = identity
    attempt.confirmed = True


@_mutation_guard
def prepare_mutation(user, transform, *, captured=None, clamp_keys=()):
    """Prepare immutable local intent under the native mutex without writing it."""
    from . import prefs_native
    exact, root, binding = _mutation_selection(user, transform, captured, clamp_keys)
    with prefs_native.transaction(root, binding=binding) as tx:
        pending = _prepare_mutation(tx, exact, transform, captured, clamp_keys)
    return pending


@_mutation_guard
def settle_mutation(user, pending):
    """Settle a retained local intent against its original authority; no callback."""
    from . import prefs_native
    _assert_not_transform()
    exact = _selected_owner(user)
    if type(pending) is not PendingMutation or pending.before.owner != exact:
        raise PreferenceConflict('captured preference mutation belongs to another owner')
    before = pending.before
    before.binding.check_executor()
    try:
        with prefs_native.transaction(before.binding.root, binding=before.binding) as tx:
            _settle_mutation(tx, pending)
    except BaseException as error:
        if prefs_native.cleanup_unknown(error):
            pending._attempt.cleanup_unconfirmed = True
        raise
    return pending.status()


def _confirmed_mutation_snapshot(pending):
    """Return the historical confirmed after-image, with no current-state I/O.

    This grants no fresh authority. A later prepare_mutation(captured=image)
    must compare all original authority, current, legacy and recovery evidence
    before its transform. Never recapture/rebase after a concurrent change.
    """
    from dataclasses import replace
    _assert_not_transform()
    if type(pending) is not PendingMutation or type(pending.before) is not Snapshot:
        raise PreferenceConflict('invalid retained preference mutation')
    pending.before.binding.check_executor()
    attempt = pending._attempt
    if (type(attempt) is not _MutationAttempt or attempt.dispatched is not True
            or attempt.confirmed is not True or attempt.cleanup_unconfirmed is not False):
        raise PreferenceConflict('preference mutation has no clean confirmed image')
    identity = attempt.confirmed_identity
    if (type(identity) is not tuple or len(identity) != 5
            or any(type(value) is not int for value in identity[:4])
            or identity[4] is not None and type(identity[4]) is not int):
        raise PreferenceConflict('preference confirmed identity is unavailable')
    return replace(pending.before, raw=pending.after, identity=identity)


@_mutation_guard
def mutate(user, transform, *, captured=None, clamp_keys=()):
    """Transform and publish under one lock; no prompts, KDFs or external callbacks.

    Callers that need local settlement must use prepare_mutation, retain that
    exact object before dispatch, then settle_mutation. A thrown mutation is
    never permission to repeat credential capture or authorization work.
    """
    from . import prefs_native, prefs_records
    exact, root, binding = _mutation_selection(user, transform, captured, clamp_keys)
    with prefs_native.transaction(root, binding=binding) as tx:
        pending = _prepare_mutation(tx, exact, transform, captured, clamp_keys)
        _settle_mutation(tx, pending)
    return prefs_records.decode(pending.after)


# --- the central quarantine API ------------------------------------------
#
# THE ONE PLACE unresolved-legacy state is detected. Every consumer reads its
# settings through `get`, so clamping here reaches autonomy, granted scopes,
# action limits, the capability profile, operator settings, earned autonomy and
# secure capture without any of those modules re-implementing legacy detection.
#
# WHY A POSTURE AND NOT DEFAULTS. Refusing to attribute a legacy file and then
# falling back to ordinary defaults WIDENS privilege: a legacy
# `capability_profile="guest"` became "full", a legacy `autonomy=0` became L1,
# and a strict `action_limits` entry vanished into an unlimited class default.
# The safe reading of "we do not know who this file belongs to" is not "assume
# nothing was ever restricted" — it is "assume the most restrictive thing that
# was ever true of this collision group, until an operator resolves it".

#: Preference keys whose value IS a security decision. While a principal's
#: collision group has an unresolved legacy file, each reads as its restrictive
#: posture regardless of what any exact file says.
QUARANTINE_POSTURE: dict[str, object] = {
    "autonomy": 0,                     # actions.L0 — nothing runs unattended
    "scopes": [],                      # no integration scope is granted
    "capability_profile": "guest",     # locked deny posture, max_autonomy 0
    "action_limits": {},               # see actions.daily_limit: NOT unlimited
    "operator": {"sites": {}, "advanced": False},   # no authorized site
    "earned_autonomy": False,          # trust ladder disabled
    "pending_secure_login": None,      # no password prompt may be pending
}

QUARANTINE_REASON = (
    "settings for this identity are quarantined: either an unmigrated "
    "preference file or a pre-v2 lossy gateway principal cannot be attributed "
    "to one external account. An operator must migrate or discard the legacy "
    "data (mapping it to a verified v2 principal when migrating) before "
    "restricted settings can be "
    "trusted.")

# Unlike cosmetic preferences, the installation-wide budget cannot fall back
# to configuration when its saved blob exists but is unavailable: that can
# widen a tighter saved cap, and an explicitly configured 0 means unlimited.
# Its consumer converts this typed state error into BudgetPolicyUnavailable.
_STRICT_EVIDENCE_KEYS = frozenset({"daily_budget"})


def _captured_view(user, captured):
    _assert_not_transform()
    if captured is None:
        return capture(user)
    exact = _selected_owner(user)
    if type(captured) is not Snapshot or captured.owner != exact:
        raise PreferenceConflict('captured preference view belongs to another owner')
    captured.binding.check_executor()
    return captured


def is_quarantined(user: str, *, captured=None) -> bool:
    try:
        observed = _captured_view(user, captured)
        observed.data()
        return observed.quarantined
    except PreferencesStateError:
        return True


def quarantine_reason(user: str) -> str | None:
    try:
        observed = capture(user)
        observed.data()
    except PreferencesStateError as err:
        return str(err)
    if observed.recovery_pending:
        return 'preference recovery is unresolved; reconcile its retained operation ID'
    return QUARANTINE_REASON if observed.quarantined else None


def _posture(key: str):
    """A fresh copy, so a caller mutating the result cannot poison the map."""
    value = QUARANTINE_POSTURE[key]
    if isinstance(value, dict):
        return json.loads(json.dumps(value))
    if isinstance(value, list):
        return list(value)
    return value


def load(user: str) -> dict:
    """One detached exact-owner view, without the legacy security clamp."""
    return capture(user).data()


def _raw_get(user: str, key: str, default=None):
    """The stored value, with NO quarantine clamp.

    Only for resolving quarantine itself (`migrate_legacy` comparing what is
    already present) and for operator inspection. Never for an authorization
    decision — that is what `get` is for.
    """
    return load(user).get(key, default)


def get(user: str, key: str, default=None, *, captured=None):
    """Read a historical captured view for one policy decision.

    Passing captured= does not refresh or revalidate authority. Consumers use
    it only to resolve related fields from one observation. A later action
    must capture or conditionally mutate again at its own execution boundary.
    """
    try:
        observed = _captured_view(user, captured)
        data = observed.data()
    except PreferencesStateError:
        if key in QUARANTINE_POSTURE:
            return _posture(key)
        if key in _STRICT_EVIDENCE_KEYS:
            raise
        return default
    if observed.recovery_pending and key in _STRICT_EVIDENCE_KEYS:
        raise PreferencesStateError(observed.owner, observed.path, 'preference recovery is unresolved')
    if key in QUARANTINE_POSTURE and observed.quarantined:
        return _posture(key)
    return data.get(key, default)


def set(user: str, key: str, value) -> None:
    """Set one bounded JSON value, or delete it with None, under native RMW."""
    _assert_not_transform()
    from . import prefs_records
    if type(key) is not str:
        raise prefs_records.PreferenceRecordError('preference key is invalid')
    selected = prefs_records.detach({key: value})
    def apply(data):
        if selected[key] is None:
            data.pop(key, None)
        else:
            data[key] = selected[key]
        return data
    mutate(user, apply)


def _status_observed(observed):
    try:
        data = observed.data()
    except PreferencesStateError as error:
        return {'owner': observed.owner, 'state': 'unavailable', 'quarantined': True,
                'reason': error.reason, 'repair_command': error.repair_command,
                'operation_id': observed.recovery_operation}
    reason = ('preference recovery is unresolved; reconcile its retained operation ID'
              if observed.recovery_pending else QUARANTINE_REASON if observed.quarantined else None)
    return {'owner': observed.owner,
            'state': 'recovery-pending' if observed.recovery_pending else 'missing' if observed.raw is None else 'valid',
            'quarantined': observed.quarantined, 'reason': reason, 'keys': sorted(data),
            'repair_command': None, 'operation_id': observed.recovery_operation}


def state_status(user: str) -> dict:
    """Report one captured evidence view without preference values."""
    _assert_not_transform()
    exact = _selected_owner(user)
    try:
        return _status_observed(capture(exact))
    except PreferencesStateError as error:
        return {'owner': exact, 'state': 'unavailable', 'quarantined': True,
                'reason': error.reason, 'repair_command': error.repair_command,
                'operation_id': None}


def repair(user: str, *, operation_id=None) -> dict:
    """Preserve a corrupt exact blob through a retained native recovery plan.

    A missing or valid file is inert. Retain the operation ID before dispatch
    when the caller needs to reconcile an interrupted operation after restart.
    """
    _assert_not_transform()
    result = _run_recovery(_recovery_request('repair', user, None, False, operation_id))
    status = _status_observed(result.pop('_snapshot'))
    status.update({key: value for key, value in result.items() if key != 'count'})
    status['repaired'] = result['completed']
    return status


# --- pre-v2 preference files: quarantined, never implicitly claimed -------
#
# `users/<safe_id>/prefs.json` was written under a key several principals map
# to, so nothing in it says whose settings those are. It is NOT read any more —
# not even for a principal whose exact identity equals that normalized string,
# because equalling a lossy value is exactly what every collision victim also
# does. Affected principals are clamped by ``QUARANTINE_POSTURE``: autonomy L0,
# no granted scopes, guest capability profile, no operator sites/advanced mode,
# no earned-autonomy opt-in and no pending secure-login capture.  Ordinary
# defaults are deliberately not treated as a security posture.


@_mutation_guard
def legacy_owners() -> list[str]:
    """Bounded native inspection of retained pre-v2 groups, without values."""
    from . import archive_io, prefs_native
    _assert_not_transform()
    root = archive_io.selected_directory(config.MEMORY_DIR)
    with prefs_native.transaction(root) as tx:
        entries = tx.inventory(('users',), limit=48)
        found = []
        for name, kind, _identity_value in entries or ():
            if kind != 'directory':
                continue
            _legacy_identifier(name)
            raw, _identity_value = tx.read(('users', name, 'prefs.json'))
            if raw is not None:
                found.append(name)
        return sorted(found)


@_mutation_guard
def legacy_keys(legacy_id: str) -> list[str]:
    """Inspect bounded legacy keys under the native mutex; never return values."""
    from . import archive_io, prefs_native, prefs_records
    _assert_not_transform()
    selected = _legacy_identifier(legacy_id)
    root = archive_io.selected_directory(config.MEMORY_DIR)
    with prefs_native.transaction(root) as tx:
        raw, _identity_value = tx.read(('users', selected, 'prefs.json'))
        if raw is None:
            return []
        try:
            return sorted(prefs_records.decode(raw))
        except prefs_records.PreferenceRecordError:
            return []


def migrate_legacy(legacy_id: str, owner: str, *, overwrite: bool = False, operation_id=None) -> int:
    """Explicitly assign a legacy group to one exact owner and retain its inode.

    The immutable before/after images and source claim make interrupted work
    resumable by its original operation ID, without recomputing a merge.
    """
    _assert_not_transform()
    result = _run_recovery(_recovery_request('migrate', owner, legacy_id, overwrite, operation_id))
    return result['count']


def discard_legacy(legacy_id: str, *, operation_id=None) -> bool:
    """Explicitly retire a legacy group under a retained native recovery plan."""
    _assert_not_transform()
    result = _run_recovery(_recovery_request('discard', None, legacy_id, False, operation_id))
    return bool(result['count'])


# Uninstalled fragment for prefs.py; no standalone execution.
from builtins import set as _key_set

_RECOVERY_CLAIM_LEAF = 'prefs.claim.json'


def _backup_collection_preflight():
    """Reject nested backup before private input, key callbacks or collection."""
    _assert_not_transform()
    from . import prefs_native
    prefs_native.require_outside_transaction()


def _backup_path_kind(parts):
    """Classify validated portable names without observing ambient state.

    Recovery authority is not portable. The exact coordination file is a
    separate exclusion; it cannot be read through another Windows descriptor
    while the collection guard holds its byte-range lock.
    """
    if (type(parts) not in (tuple, list) or not parts or len(parts) > 65
            or any(type(part) is not str or not part for part in parts)):
        raise ValueError('invalid bounded preference backup path')
    folded = tuple(part.casefold() for part in parts)
    if folded[0] == _recovery_layout()['root'][0] or _RECOVERY_CLAIM_LEAF in folded:
        return 'recovery'
    from . import prefs_native
    if folded == prefs_native.coordination_path():
        return 'coordination'
    return None


def _recovery_layout():
    root = ('prefs-recovery',)
    return {'root': root, 'operations': root + ('operations',),
            'namespace': root + ('namespace.json',), 'head': root + ('head.json',)}


def _recovery_locations(identifier):
    from . import prefs_recovery_records as records
    records.operation_id(identifier)
    root = _recovery_layout()['operations'] + (identifier,)
    return {'source': root + ('source.bin',), 'before': root + ('before.bin',),
            'after': root + ('after.bin',), 'plan': root + ('plan.json',),
            'completion': root + ('completion.json',)}


def _legacy_identifier(value):
    if (type(value) is not str or not 1 <= len(value) <= 64
            or memory.safe_id(value) != value):
        raise ValueError('invalid legacy preference group')
    from . import prefs_native
    prefs_native.Transaction._address(('users', value, 'prefs.json'))
    return value


def _claim_paths(exact):
    current, legacy = _relative_paths(exact)
    return (current[:-1] + (_RECOVERY_CLAIM_LEAF,),
            None if legacy is None else legacy[:-1] + (_RECOVERY_CLAIM_LEAF,))


def _recovery_selection(request, source_digest):
    from . import prefs_recovery_records as records
    identifier = records.operation_id(request['operation'])
    records._sha(source_digest)
    kind, owner, legacy_id = request['kind'], request['owner'], request['legacy_id']
    if kind not in ('repair', 'migrate', 'discard') or type(request['overwrite']) is not bool:
        records._refuse()
    if kind == 'discard':
        if owner is not None or request['overwrite']:
            records._refuse()
        current, owner_claim = None, None
    else:
        if type(owner) is not str or _selected_owner(owner) != owner:
            records._refuse()
        if kind == 'migrate' and (memory.is_system_owner(owner) or memory.is_ambiguous_gateway_owner(owner)):
            records._refuse()
        current = _relative_paths(owner)[0]
        owner_claim = _claim_paths(owner)[0]
    if kind == 'repair':
        if legacy_id is not None or request['overwrite']:
            records._refuse()
        source = current
        retained = current[:-1] + (f'prefs.corrupt.{source_digest[:_QUARANTINE_DIGEST_HEX]}.json',)
        legacy_claim = None
    else:
        legacy_id = _legacy_identifier(legacy_id)
        source = ('users', legacy_id, 'prefs.json')
        retained = source[:-1] + (f'prefs.retired.{identifier}.json',)
        legacy_claim = source[:-1] + (_RECOVERY_CLAIM_LEAF,)
    return {'source': source, 'target': current, 'retained': retained,
            'owner_claim': owner_claim, 'legacy_claim': legacy_claim}


def _resolve_recovery_plan(plan):
    from . import prefs_native, prefs_recovery_records as records
    required = {'version', 'operation', 'kind', 'owner', 'legacy_id', 'overwrite',
                'source', 'target', 'retained', 'owner_claim', 'legacy_claim',
                'images', 'source_identity', 'before_identity', 'authority',
                'previous', 'result_count'}
    if (type(plan) is not dict or _key_set(plan) != required or type(plan['version']) is not int
            or plan['version'] != 1 or type(plan['result_count']) is not int
            or not 0 <= plan['result_count'] <= 8192):
        records._refuse()
    authority = plan['authority']
    if type(authority) is not dict or _key_set(authority) != {'root', 'locks', 'lock'}:
        records._refuse()
    for value in authority.values():
        if type(value) is not list or len(value) != 2 or any(type(item) is not int for item in value):
            records._refuse()
    images = plan['images']
    if type(images) is not dict or _key_set(images) != _key_set(records.IMAGE_NAMES):
        records._refuse()
    for descriptor in images.values():
        records._image_descriptor(descriptor)
    if images['source'] is None:
        records._refuse()
    source_identity = records._identity(plan['source_identity'])
    if source_identity[2] != images['source']['size']:
        records._refuse()
    before_identity = records._identity(plan['before_identity'], absent=True)
    if (images['before'] is None) != (before_identity is None):
        records._refuse()
    if before_identity is not None and before_identity[2] != images['before']['size']:
        records._refuse()
    if plan['kind'] == 'discard':
        if images['before'] is not None or images['after'] is not None or plan['result_count'] != 1:
            records._refuse()
    else:
        if images['after'] is None:
            records._refuse()
        if plan['kind'] == 'repair' and images['after'] != records.image_descriptor(records.codec.encode({})):
            records._refuse()
        if plan['kind'] == 'repair' and (images['source'] != images['before']
                or plan['source_identity'] != plan['before_identity'] or plan['result_count'] != 1):
            records._refuse()
    invalid = False
    try:
        selected = _recovery_selection(plan, images['source']['sha256'])
    except (ValueError, records.RecoveryEvidenceError, prefs_native.PreferenceStorageError,
            prefs_native.storage.StorageError, prefs_native.archive_io.ArchiveError):
        invalid = True
    if invalid:
        records._refuse()
    for key, address in selected.items():
        if plan[key] != (None if address is None else list(address)):
            records._refuse()
        if address is not None:
            prefs_native.Transaction._address(address)
    previous = plan['previous']
    if type(previous) is not dict or _key_set(previous) != {'head', 'owner_claim', 'legacy_claim'}:
        records._refuse()
    head = records.prior_record(previous['head']).value()
    if head is None or records._head(head)['state'] not in ('idle', 'complete'):
        records._refuse()
    for role in ('owner_claim', 'legacy_claim'):
        old = records.prior_record(previous[role])
        pointer = records.claim(old)
        if selected[role] is None and pointer is not None:
            records._refuse()
        if role == 'legacy_claim' and pointer is not None:
            records._refuse()
    return selected



def _validate_recovery_images(plan, images):
    """Check retained images only; never rebuild an intent from live state."""
    from . import prefs_records as codec, prefs_recovery_records as records
    if plan['kind'] == 'discard':
        return  # Explicit retirement accepts bounded malformed source evidence.
    failed = False
    try:
        after = codec.decode(images['after'])
        if plan['kind'] == 'repair':
            if images['after'] != codec.encode({}):
                records._refuse()
            corrupt = False
            try:
                codec.decode(images['source'])
            except codec.PreferenceRecordError:
                corrupt = True
            if not corrupt:
                records._refuse()
        else:
            legacy = codec.decode(images['source'])
            expected = {} if images['before'] is None else codec.decode(images['before'])
            count = 0
            for key, value in legacy.items():
                if not plan['overwrite'] and expected.get(key) is not None:
                    continue
                if value is None:
                    expected.pop(key, None)
                else:
                    expected[key] = value
                count += 1
            if codec.encode(expected) != images['after'] or count != plan['result_count']:
                records._refuse()
    except codec.PreferenceRecordError:
        failed = True
    if failed:
        records._refuse()


def _journal(tx):
    from . import prefs_recovery_records as records
    return records.Journal(tx, _recovery_layout(), _recovery_locations, _resolve_recovery_plan, _validate_recovery_images)


def _recovery_observation(tx, exact):
    from . import prefs_recovery_engine as recovery
    journal = _journal(tx)
    view = recovery.View(journal)
    owner_claim, legacy_claim = _claim_paths(exact)
    stamp, pending = view.snapshot(owner_claim, legacy_claim)
    head = journal.head.value()
    identifier = head['operation'] if head is not None and head['state'] == 'pending' else None
    for role, address in (('owner_claim', owner_claim), ('legacy_claim', legacy_claim)):
        record, unresolved = view.claim_state(role, address)
        if unresolved and identifier is None:
            identifier = record.value()['operation']
    return journal, stamp, pending, identifier


def _confirm_recovery_authority(tx, observed):
    from . import prefs_recovery_records as records
    failed = False
    try:
        journal, stamp, pending, _identifier = _recovery_observation(tx, observed.owner)
        if pending or stamp != observed.recovery_stamp:
            raise PreferenceConflict('preference recovery changed; publication refused')
        if journal.initialized:
            journal.initialize()  # Reconfirm visible metadata before new effects.
    except records.RecoveryEvidenceError:
        failed = True
    if failed:
        raise PreferencesStateError(observed.owner, observed.path, 'recovery evidence is unavailable')


def _recovery_request(kind, owner, legacy_id, overwrite, identifier):
    from . import prefs_recovery_records as records
    import uuid
    records.operation_id(identifier) if identifier is not None else None
    selected_id = identifier if identifier is not None else uuid.uuid4().hex
    exact = None if kind == 'discard' else _selected_owner(owner)
    request = {'operation': selected_id, 'kind': kind, 'owner': exact,
               'legacy_id': None if kind == 'repair' else _legacy_identifier(legacy_id),
               'overwrite': overwrite}
    # Validate parameters and their entire address selection before opening root.
    _recovery_selection(request, '0' * 64)
    return request


def _run_recovery(request, *, root=None, root_identity=None):
    from . import archive_io, prefs_native, prefs_records, prefs_recovery_engine as recovery
    from . import prefs_recovery_records as records
    _assert_not_transform()
    identifier = request['operation']
    result = None
    failed = None
    cleanup = False
    try:
        if root is None and root_identity is None:
            root = archive_io.selected_directory(config.MEMORY_DIR)
        elif (type(root) is not type(Path()) or not root.is_absolute()
                or type(root_identity) is not tuple or len(root_identity) != 2
                or any(type(value) is not int or value < 0 for value in root_identity)):
            raise ValueError('invalid captured preference recovery root')
        # A supplied identity is checked before coordination creation. CLI
        # callers retain its read-only ancestor owner across their ID output.
        with prefs_native.transaction(root, root_identity=root_identity) as tx:
            journal = _journal(tx)
            if identifier not in journal.operations:
                # Even an inert new request must validate all retained authority.
                # A new UUID cannot conceal the ID that still needs settlement.
                view = recovery.View(journal)
                _claims, blocking = view.all_claims()
                head = journal.head.value()
                if head is not None and head['state'] == 'pending':
                    blocking.add(head['operation'])
                if blocking:
                    identifier = sorted(blocking)[0]
                    records._refuse()
                selected = _recovery_selection(request, '0' * 64)
                source_raw, source_identity = tx.read(selected['source'])
                before_raw, before_identity = ((None, None) if selected['target'] is None
                                               else tx.read(selected['target']))
                if request['kind'] == 'repair':
                    valid = source_raw is None
                    if source_raw is not None:
                        try:
                            prefs_records.decode(source_raw)
                            valid = True
                        except prefs_records.PreferenceRecordError:
                            pass
                    if valid:
                        if journal.interrupted_initialization:
                            records._refuse()
                        return {'operation_id': None, 'completed': False, 'historical': False,
                                'count': 0, 'repaired': False, '_snapshot': _observe(tx, request['owner'])}
                    after_raw, count = prefs_records.encode({}), 1
                elif source_raw is None:
                    if request['kind'] == 'discard':
                        if journal.interrupted_initialization:
                            records._refuse()
                        return {'operation_id': None, 'completed': False, 'historical': False, 'count': 0}
                    raise ValueError('legacy preference evidence is absent; use its retained operation ID to reconcile')
                elif request['kind'] == 'discard':
                    after_raw, count = None, 1
                else:
                    legacy = prefs_records.decode(source_raw)
                    target = {} if before_raw is None else prefs_records.decode(before_raw)
                    count = 0
                    for key, value in legacy.items():
                        if not request['overwrite'] and target.get(key) is not None:
                            continue
                        if value is None:
                            target.pop(key, None)
                        else:
                            target[key] = value
                        count += 1
                    after_raw = prefs_records.encode(target)
                selected = _recovery_selection(request, records.digest(source_raw))
                previous = recovery.prepare_new(journal, identifier, selected)
                images = {'source': source_raw, 'before': before_raw, 'after': after_raw}
                plan = {'version': 1, **request,
                    **{key: None if value is None else list(value) for key, value in selected.items()},
                    'images': {key: records.image_descriptor(value) for key, value in images.items()},
                    'source_identity': list(source_identity),
                    'before_identity': None if before_identity is None else list(before_identity),
                    'authority': records.authority(tx), 'previous': previous, 'result_count': count}
                journal.retain_plan(identifier, plan, images,
                    tuple(value for value in selected.values() if value is not None))
            result = recovery.settle(journal, identifier, request)
            if request['kind'] == 'repair':
                result['_snapshot'] = _observe(tx, request['owner'])
                loaded = journal.read_plan(identifier)
                result['quarantined_sha256'] = loaded[1]['images']['source']['sha256']
                result['quarantine_file'] = loaded[2]['retained'][-1]
    except (records.RecoveryEvidenceError, prefs_records.PreferenceRecordError) as error:
        failed = 'preference recovery evidence requires explicit reconciliation'
        cleanup = prefs_native.cleanup_unknown(error)
    except (OSError, prefs_native.PreferenceStorageError, prefs_native.storage.StorageError) as error:
        failed = 'preference recovery result is unconfirmed'
        cleanup = prefs_native.cleanup_unknown(error)
    except BaseException as error:
        attributes = BaseException.__getattribute__(error, '__dict__')
        if type(attributes) is dict:
            attributes['_preference_recovery_operation'] = identifier
        if isinstance(error, Exception) and prefs_native.cleanup_unknown(error):
            failed = 'preference recovery result is unconfirmed'
            cleanup = True
        else:
            raise
    if failed is not None:
        error = PreferenceOperationError(f'{failed}; operation {identifier}')
        error.operation_id = identifier
        if cleanup:
            prefs_native._mark_cleanup(error)
        raise error
    return result


# Uninstalled fragment for prefs.py. Requires expected-root native admission.

@_mutation_guard
def _capture_for_credentials(owner, root, root_identity, *, captured=None):
    """Observe credential preferences under an already captured native root.

    Only preference coordination may be created, after the original root
    identity is checked. No ambient memory selector is consulted.
    """
    from . import prefs_native
    _assert_not_transform()
    if (type(root_identity) is not tuple or len(root_identity) != 2
            or any(type(item) is not int for item in root_identity)):
        raise PreferenceConflict('captured credential preference root identity is invalid')
    credential_paths(owner)  # Exact bounded credential owner before native IO.
    if captured is not None:
        if type(captured) is not Snapshot or captured.owner != owner:
            raise PreferenceConflict('captured credential preference owner changed')
        captured.binding.check_executor()
        if captured.binding.root_identity != root_identity:
            raise PreferenceConflict('captured credential preference root changed')
    with prefs_native.transaction(root, root_identity=root_identity,
            binding=None if captured is None else captured.binding) as tx:
        observed = _observe(tx, owner)
        if captured is not None and not _same_evidence(observed, captured):
            raise PreferenceConflict('captured credential preference evidence changed')
        return observed
