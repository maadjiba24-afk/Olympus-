"""Private captured directory and policy authority for coherent backup stages.

Draft interface: callers must pass this context into their mutation boundaries.
An outer comparison alone does not authorize a stage to recapture ambient
settings. Derived passphrases and signers are confined to each stage. Frozen
private environment selector strings remain in this bounded context until
cleanup; they never enter reports, repr output or the uploader environment.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from types import MappingProxyType
import threading

from . import archive_io as aio, backup_signing as signing
from . import backup_snapshot as snapshot, backup_publication as publication, config
from .backup_restore import _exclusive_child
from .vault_storage import _Directory
from . import native_cleanup

_MODES = frozenset(('create', 'deliver', 'restore', 'run', 'drill', 'manage', 'inspect'))
_SIGNING_MODES = frozenset(('create', 'deliver', 'restore', 'run', 'drill'))
_CREATION_MODES = frozenset(('create', 'run'))
_STAGES = frozenset(('create', 'deliver', 'catalog', 'retire', 'restore', 'evidence', 'inspect'))
_MODE_STAGES = {
    'create': frozenset(('create',)), 'deliver': frozenset(('deliver',)),
    'restore': frozenset(('restore',)), 'run': frozenset(('create', 'deliver', 'retire', 'evidence')),
    'drill': frozenset(('catalog', 'restore', 'evidence')), 'manage': frozenset(('retire',)),
    'inspect': frozenset(('catalog', 'inspect')),
}


class AuthorityError(RuntimeError):
    pass


def _bound_directory(value, cwd):
    original = Path(value)
    if original.drive and not original.root:
        raise AuthorityError('drive-relative workflow paths are unsupported')
    return aio.selected_directory(original if original.is_absolute() else Path(cwd) / original)


def _bound_file(value, cwd):
    original = Path(value)
    aio._filename(original.name)
    if original.drive and not original.root:
        raise AuthorityError('drive-relative workflow paths are unsupported')
    return aio.selected_path(original if original.is_absolute() else Path(cwd) / original)


def _creation_entries(memory):
    """Return one locally owned raw iterator; no nested generator cleanup."""
    return memory.win.entries() if memory.win is not None else os.scandir(memory.fd)


class _WorkflowAuthority:
    """Owned only by its capturing process/thread and one active stage at a time."""

    def __init__(self, mode, *, include_delivery=False, retention=None):
        if (mode not in _MODES or type(include_delivery) is not bool
                or include_delivery and mode != 'run'):
            raise AuthorityError('invalid backup authority mode')
        self.mode = mode
        self.allowed_stages = _MODE_STAGES[mode]
        if mode == 'run' and not include_delivery:
            self.allowed_stages = self.allowed_stages - {'deliver'}
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.active = False
        self._closed = False
        self._cleanup_unconfirmed = False
        self._stage = None
        self.memory = self.working = self._backups = None
        self._backups_absent = None
        self._creation_cleanup_unconfirmed = False
        self._uncertain_creation_scan = None
        self.cwd = os.getcwd()
        self.raw_memory_selection = config.MEMORY_DIR
        self.memory_path = _bound_directory(self.raw_memory_selection, self.cwd)
        self.signing_selection = None
        self.vault_selectors = None
        self.raw_vault_selection = None
        self.excluded = frozenset()
        self.exclusion_identities = None
        self.policy = None
        self.command_selection = self.environment = self.command = None
        self.keep = None
        self._returned_evidence_tokens = {}
        # Snapshot raw inputs before directory/pin/custody I/O callbacks.
        if mode in _SIGNING_MODES:
            self.signing_selection = signing._selection()
            self.raw_vault_selection = tuple((name, os.environ.get(name)) for name in
                ('OLYMPUS_SECRET_KEY', 'OLYMPUS_SECRET_KEY_FILE', 'OLYMPUS_ENV'))
            # The narrow archive_io extension preserves deferred-invalid-key
            # behavior while binding relative key paths against this cwd.
            self.vault_selectors = MappingProxyType(aio.freeze_selectors(dict(self.raw_vault_selection), cwd=self.cwd))
            if mode in ('create', 'run', 'restore', 'drill'):
                selected = {**dict(self.signing_selection), **dict(self.raw_vault_selection)}
                paths = []
                for name in ('OLYMPUS_SECRET_KEY_FILE', 'OLYMPUS_SIGNING_SEED_FILE'):
                    value = selected.get(name)
                    if value is None:
                        continue
                    if type(value) is not str or len(value) > aio.custody.MAX_PATH_CHARS:
                        raise AuthorityError('invalid bounded backup custody selector')
                    if value.strip():
                        paths.append(_bound_file(value.strip(), self.cwd))
                self.excluded = frozenset(paths)
        if mode == 'deliver' or mode == 'run' and include_delivery:
            from . import backup_delivery as delivery
            self.command_selection = delivery._command_selection()
            self.environment = MappingProxyType(delivery._capture_environment())
        if mode in ('run', 'manage'):
            self.keep = config.backup_keep() if retention is None else retention
            if type(self.keep) is not int or not 0 <= self.keep <= publication.MAX_OPERATIONS:
                raise AuthorityError('backup retention exceeds its bound')

    def _owner(self):
        if type(self) is not _WorkflowAuthority:
            raise AuthorityError('backup authority is invalid')
        if (self.pid, self.thread) != (os.getpid(), threading.get_ident()):
            raise AuthorityError('backup authority belongs to another process or thread')
        if self._cleanup_unconfirmed or self._creation_cleanup_unconfirmed:
            error = AuthorityError('backup authority cleanup requires reconciliation')
            native_cleanup.mark(error)
            raise error
        if not self.active:
            raise AuthorityError('backup authority is no longer active')

    def check(self):
        self._owner()
        if os.getcwd() != self.cwd or _bound_directory(config.MEMORY_DIR, self.cwd) != self.memory_path:
            raise AuthorityError('backup authority selection changed')
        self.memory.check()
        self.working.check()
        if self._backups is not None:
            self._backups.check()
        if self.signing_selection is not None:
            signing._check_selection(self.signing_selection)
            if any(os.environ.get(name) != value for name, value in self.raw_vault_selection):
                raise AuthorityError('backup archive custody selection changed')
        if self.command_selection is not None:
            from . import backup_delivery as delivery
            if delivery._command_selection() != self.command_selection:
                raise AuthorityError('backup command selection changed')
        if self.exclusion_identities is not None:
            snapshot.check_exclusions(self.excluded, self.exclusion_identities)

    def require_stage(self, name):
        self.check()
        if name not in self.allowed_stages or self._stage != name:
            raise AuthorityError('backup operation does not own this stage')

    @contextmanager
    def stage(self, name):
        self.check()
        if name not in self.allowed_stages or self._stage is not None:
            raise AuthorityError('backup stages cannot overlap or reenter')
        self._stage = name
        failure = None
        try:
            yield self
            self.check()
        except BaseException as error:
            failure = error
            if native_cleanup.unknown(error):self._cleanup_unconfirmed = True
        finally:
            try:
                self._owner()
            except BaseException as error:
                if failure is None:
                    failure = error
                elif native_cleanup.unknown(error):
                    native_cleanup.mark(failure)
            else:
                self._stage = None
        if failure is not None:
            raise failure

    def preflight_creation(self):
        """Refuse a changed captured absence before custody or collection effects.

        This bounded observation does not reserve the name. Final publication
        must still use the existing exclusive child creation boundary.
        """
        self.require_stage('create')
        if self._backups is not None:
            return
        if self._backups_absent is not True:
            raise AuthorityError('backup child absence was not captured')
        self.memory.check()
        entries = _creation_entries(self.memory)
        primary = None
        try:
            for count, entry in enumerate(entries, 1):
                self.memory.check()
                if count > snapshot.MAX_DIRECTORY_ENTRIES:
                    raise AuthorityError('backup creation inventory exceeds its bound')
                aio._filename(entry.name)
                if entry.name.casefold() == 'backups':
                    raise AuthorityError('captured backup child absence changed')
            self.memory.check()
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                closed = native_cleanup.drain([entries.close], primary)
            except BaseException:
                self._creation_cleanup_unconfirmed = True
                self._uncertain_creation_scan = entries
                raise
            else:
                if not closed:
                    self._creation_cleanup_unconfirmed = True
                    self._uncertain_creation_scan = entries
        self.require_stage('create')

    def backups(self, *, create=False):
        self.check()
        if self._stage is None or type(create) is not bool:
            raise AuthorityError('backup child access requires an active stage')
        if self._backups is None and create:
            if self._stage != 'create' or self._backups_absent is not True:
                raise AuthorityError('backup child creation was not captured as absent')
            # An independently appearing child must not become our authority.
            self._backups = _exclusive_child(self.memory, 'backups')
            self._backups_absent = False
        self.check()
        return self._backups

    def bind_file(self, value):
        self.check()
        return _bound_file(value, self.cwd)

    def bind_directory(self, value):
        self.check()
        return _bound_directory(value, self.cwd)

    def close(self):
        if (self.pid, self.thread) != (os.getpid(), threading.get_ident()):
            raise AuthorityError('backup authority cleanup belongs to another process or thread')
        if self._closed:
            if self._cleanup_unconfirmed or self._creation_cleanup_unconfirmed:
                error = AuthorityError('backup authority cleanup remains unconfirmed')
                native_cleanup.mark(error)
                raise error
            return
        self._closed, self.active = True, False
        actions = []
        for name in ('_backups', 'memory', 'working'):
            held = getattr(self, name)
            setattr(self, name, None)
            if held is not None: actions.append(held.close)
        self._stage = None
        self._returned_evidence_tokens.clear()
        self.vault_selectors = self.raw_vault_selection = self.signing_selection = None
        self.environment = self.command_selection = self.command = self.policy = None
        native_cleanup.drain_owned(self, actions)
        if self._cleanup_unconfirmed or self._creation_cleanup_unconfirmed:
            error = AuthorityError('backup authority cleanup remains unconfirmed')
            native_cleanup.mark(error)
            raise error


@contextmanager
def capture(mode, *, include_delivery=False, retention=None):
    """Acquire only the policy required by this action; never an archive slot."""
    authority = _WorkflowAuthority(mode, include_delivery=include_delivery, retention=retention)
    failure = None
    try:
        authority.working = _Directory(authority.cwd)
        authority.memory = _Directory(authority.memory_path)
        authority.active = True
        authority.check()
        authority.memory._case_spelling('backups')
        try:
            authority._backups = authority.memory.child('backups')
        except FileNotFoundError as error:
            if native_cleanup.unknown(error): raise
            authority.memory.check()
            authority._backups_absent = True
        else:
            authority._backups_absent = False
        if mode in _CREATION_MODES:
            authority.exclusion_identities = snapshot.capture_exclusions(authority.excluded)
        authority.check()
        if authority.command_selection is not None:
            from . import backup_delivery as delivery
            authority.command = delivery._capture_command(authority.command_selection, authority.cwd,
                                                           dict(authority.environment))
        authority.check()
        if (authority.signing_selection is not None
                and (mode != 'deliver' or authority.command is not None)):
            authority.policy = signing.capture_policy(selection=authority.signing_selection, cwd=authority.cwd)
        authority.check()
        yield authority
        authority.check()
    except BaseException as error:
        failure = error
    finally:
        try:
            native_cleanup.drain_owned(authority, [authority.close], failure)
        except BaseException as error:
            failure = error
    if failure is not None:
        raise failure



def require(authority, stage):
    """Accept only the exact owned context at a private mutation boundary."""
    if type(authority) is not _WorkflowAuthority:
        raise AuthorityError('backup stage requires an exact captured authority')
    authority.require_stage(stage)
    return authority
