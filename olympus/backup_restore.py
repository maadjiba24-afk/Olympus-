"""Destination-bound, retained roll-forward evidence for offline backup restore.

New destinations must be absent. Existing destinations require intact restore
initialization. This lock serializes restore peers, not arbitrary applications
or administrators: keep the destination offline until completion. There is no
deletion, automatic rollback, live-state restore, or power-loss guarantee beyond
the underlying filesystem's supported barriers (Windows has no directory fsync).
"""
from __future__ import annotations

from contextlib import contextmanager
from .native_cleanup import closing
import hashlib
import json
import os
import re
import threading
from pathlib import Path

from . import archive_io as io, backup_archive as fmt, backup_facts as stage_facts
from .vault_storage import (_Directory, _directory_identity, _file_information,
                            _file_identity, _try_lock, _unlock)
from . import native_cleanup

CONTROL = '.olympus-restore'
SCHEMA = 'olympus-backup-restore/1'
MAX_OPERATIONS = 1024
MAX_PLAN = 8 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 3072  # native publisher cap is 4096; reserve staging headroom
MAX_FORBIDDEN_PATHS = 8
_ID = re.compile(r'[0-9a-f]{32}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')


_PUBLIC_REASONS = frozenset({
    'an unfinished restore owns this destination; resume its ID',
    'existing restore target requires force=True',
    'invalid bounded restore image',
    'invalid restore request',
    'non-empty restore-owned destination requires force=True for a new operation',
    'requested restore ID is not the active retained operation',
    'restore ID completed historically but its target changed; refusing to reapply',
    'restore images exceed aggregate bound',
    'restore target changed after preflight; retain evidence and resolve conflict',
    'restore target conflicts with retained before/after evidence; no further files published',
})


class RestoreError(RuntimeError):
    def __init__(self, operation_id, reason='restore did not complete; retain evidence and inspect its operation ID', *, phase=None):
        self.operation_id = operation_id
        self.phase = phase
        super().__init__(reason)


def _raise_public_error(error):
    """Detach native/private failures even inside contextmanager.throw().

    The caller supplies a fresh, generic typed exception. A bare re-raise after
    clearing the chain avoids Python attaching the active body exception again;
    the fresh identity also avoids contextlib restoring the body's traceback.
    """
    try:
        raise error from None
    except Exception:
        error.__cause__ = None
        error.__context__ = None
        raise


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    if len(raw) > MAX_PLAN:
        raise ValueError('restore evidence bound exceeded')
    return raw


def _opid(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError('restore requires a lowercase UUID32 operation ID')
    return value


def _digest(value):
    return type(value) is str and bool(_HASH.fullmatch(value))


def _ident(directory):
    return list(_directory_identity(directory))


def _same_or_below(path, parent):
    return path == parent or parent in path.parents


def _normalized(path):
    selected = io.selected_path(path)
    if os.name == 'nt':
        text = str(selected)
        if text.startswith('\\\\?\\UNC\\'):
            text = '\\\\' + text[8:]
        elif text.startswith('\\\\?\\') and re.match(r'[A-Za-z]:\\', text[4:]):
            text = text[4:]
        elif text.startswith(('\\\\?\\','\\\\.\\')):
            raise ValueError('unsupported restore path namespace')
        selected = Path(text)
    return selected


def _chain(directory):
    directory.check()
    if directory.win is not None:
        from .gallery_windows import information
        infos = [information(handle,True) for handle in directory.win.handles]
    else:
        infos = [os.fstat(fd) for fd in directory._parents]
    directory.check()
    return {(info.st_dev,info.st_ino) for info in infos}


def _destination(destination, live_root, forbidden):
    selected, live = _normalized(destination), _normalized(live_root)
    if _same_or_below(selected, live) or _same_or_below(live, selected):
        raise ValueError('restore destination overlaps the active memory directory')
    for path in forbidden:
        if _same_or_below(_normalized(path), selected):
            raise ValueError('restore destination contains selected input or custody')
    return selected


def _frozen_paths(paths):
    if isinstance(paths,(str,bytes,Path)):
        raise ValueError('restore protected paths must be a bounded collection')
    selected = []
    for path in paths:
        if len(selected) >= MAX_FORBIDDEN_PATHS:
            raise ValueError('restore protected path count exceeds bound')
        selected.append(_normalized(path))
    return tuple(selected)


def _target_name(name):
    parts = fmt.relative(name)
    from . import prefs
    if prefs._backup_path_kind(parts) is not None:
        raise ValueError('restore cannot transplant native preference recovery or coordination')
    if (parts[0].casefold() in (CONTROL.casefold(), '.olympus-backup-evidence')
            or any(part.casefold() == 'signing_seed' for part in parts)):
        raise ValueError('restore target overlaps reserved recovery or signing custody')
    return parts


def _exclusive_child(parent, name):
    """Create only this absent leaf under a held parent; retain it on failure."""
    io._filename(name)
    parent.check()
    parent._case_spelling(name)
    if parent.win is not None:
        from . import vault_windows as native
        handle = native.open_relative(parent.win.handle, name, directory=True,
                                      create=True, exclusive=True)
        primary = None
        try:
            native.information(handle, True)
            parent.check()
        except BaseException as error:
            primary = error
            raise
        finally:
            owned, handle = handle, None
            native_cleanup.drain([lambda: native.close(owned)], primary)
    else:
        os.mkdir(name, mode=0o700, dir_fd=parent.fd)
    parent.sync()
    child = _Directory(parent.path / name)
    try:
        parent.check()
        return child
    except BaseException as error:
        owned, child = child, None
        native_cleanup.drain([owned.close], error)
        raise


class _Root:
    def __init__(self, destination, *, initialize, live_root, forbidden, protected_directories=(), input_lease=None):
        self.path = destination
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.directories, self.fd, self.held = [], None, False
        self.operations = []
        self.closed, self.marker, self.marker_identity = False, None, None
        self._cleanup_unconfirmed = False
        self.phase = 'initialization'
        self.confirmed_stage_facts = ()
        self.protected = []
        self.input_lease = input_lease
        try:
            if input_lease is not None:
                from .backup_publication import _Root as PublicationRoot
                if type(input_lease) is not PublicationRoot:
                    raise ValueError('restore requires an exact publication input lease')
                input_lease.check()
            if type(protected_directories) is not tuple or len(protected_directories)>MAX_FORBIDDEN_PATHS:
                raise ValueError('restore borrowed authority bound exceeded')
            for directory in protected_directories:
                if type(directory) is not _Directory:
                    raise ValueError('restore requires exact borrowed directory authorities')
                directory.check()
            if type(live_root) is _Directory:
                live = live_root
                live.check()
            else:
                live = _Directory(_normalized(live_root))
                self.directories.append(live)
            self.protected = [live,*protected_directories]
            # Held identities catch short-name/UNC aliases beyond spelling
            # normalization, without exposing or reading any custody file.
            protected = self.protected
            for path in forbidden:
                directory = _Directory(_normalized(path).parent)
                self.directories.append(directory)
                protected.append(directory)
            parent = _Directory(destination.parent)
            self.directories.append(parent)
            if _directory_identity(live) in _chain(parent):
                raise ValueError('restore destination is inside the active memory directory')
            self._check_protected()
            created = False
            if initialize:
                try:
                    root = _exclusive_child(parent, destination.name)
                    created = True
                except FileExistsError as error:
                    if native_cleanup.unknown(error): raise
                    root = _Directory(destination)
            else:
                root = _Directory(destination)
            self.directories.append(root)
            self.root = root
            if any(_directory_identity(root) in _chain(directory) for directory in protected):
                raise ValueError('restore destination contains active memory, input or custody')
            self._check_protected()
            control = (_exclusive_child(root, CONTROL) if created else root.child(CONTROL))
            self.directories.append(control)
            self.control = control
            self._check_protected()
            self.fd = control._open('restore.lock', os.O_RDWR | (os.O_CREAT | os.O_EXCL if created else 0))
            self.lock_identity = list(_file_identity(_file_information(self.fd))[:2])
            if not _try_lock(self.fd):
                raise ValueError('restore destination is busy')
            self.held = True
            expected = {'schema':SCHEMA, 'destination':str(destination),
                        'root':_ident(root), 'control':_ident(control),
                        'lock':self.lock_identity}
            if created:
                # Lock and namespace persistence precede the initialization marker.
                os.fsync(self.fd)
                control.sync()
                self._put_new('initialization.json', _json(expected))
            raw, identity = control.read_with_identity('initialization.json', MAX_PLAN)
            if fmt.strict_json(raw, MAX_PLAN) != expected:
                raise ValueError('restore initialization identity mismatch')
            self.marker = raw
            self.marker_identity = [identity['device'],identity['inode']]
            if created:
                self._put_new('head.json', _json({'schema':SCHEMA, 'operation_id':None, 'plan_sha256':None,
                                                 'initialization_identity':self.marker_identity}))
            self.check()
            self.phase = 'preparation'
        except BaseException as error:
            native_cleanup.drain_owned(self, [self.close], error)
            raise

    def _check_protected(self):
        if self.input_lease is not None:self.input_lease.check()
        for directory in self.protected:
            directory.check()

    def _put_new(self, name, raw):
        self._check_protected()
        self.control.write_new(name, raw)
        if io.read_file(self.control.path / name, len(raw)) != raw:
            raise ValueError('restore evidence verification failed')

    def check(self):
        if self._cleanup_unconfirmed:
            error = ValueError('restore cleanup requires reconciliation')
            native_cleanup.mark(error)
            raise error
        if (self.closed or not self.held or self.fd is None
                or (self.pid,self.thread) != (os.getpid(),threading.get_ident())):
            raise ValueError('restore authority is inactive or belongs to another process/thread')
        self._check_protected()
        self.root.check()
        self.control.check()
        for operation in self.operations:
            operation.check()
        if list(_file_identity(_file_information(self.fd))[:2]) != self.lock_identity:
            raise ValueError('restore lock identity changed')
        if self.control.win is None:
            entry = os.stat('restore.lock', dir_fd=self.control.fd, follow_symlinks=False)
            if list(_file_identity(entry)[:2]) != self.lock_identity:
                raise ValueError('restore lock path was replaced')
        if self.marker is not None:
            raw, identity = self.control.read_with_identity('initialization.json',MAX_PLAN)
            if raw != self.marker or [identity['device'],identity['inode']] != self.marker_identity:
                raise ValueError('restore initialization changed')

    def publish(self, path, raw):
        self.check()
        io.publish_file(path, raw)
        self.check()
        if io.read_file(path, len(raw)) != raw:
            raise ValueError('restore publication verification failed')

    def head(self):
        self.check()
        value = fmt.strict_json(io.read_file(self.control.path/'head.json', MAX_PLAN), MAX_PLAN)
        if (type(value) is not dict or set(value) != {'schema','operation_id','plan_sha256','initialization_identity'}
                or value['schema'] != SCHEMA or value['initialization_identity'] != self.marker_identity):
            raise ValueError('restore active witness is invalid')
        if value['operation_id'] is None:
            if value['plan_sha256'] is not None:
                raise ValueError('restore active witness is invalid')
        else:
            _opid(value['operation_id'])
            if not _digest(value['plan_sha256']):
                raise ValueError('restore active witness is invalid')
        return value

    def confirm_witnesses(self, head):
        """Repeat barriers on initialization and active intent before data writes.

        Do not replace the immutable initialization inode. The held native
        writable descriptor confirms its exact bytes, identity and file fsync;
        head may be republished as the same plan-bound bytes.
        """
        self.check()
        fd = self.control._open('initialization.json',os.O_RDWR)
        primary = None
        try:
            if list(_file_identity(_file_information(fd))[:2]) != self.marker_identity:
                raise ValueError('restore initialization identity changed')
            if os.read(fd,len(self.marker)+1) != self.marker:
                raise ValueError('restore initialization changed')
            os.fsync(fd)
            self.check()
        except BaseException as error:
            primary = error
            raise
        finally:
            owned, fd = fd, None
            native_cleanup.drain_owned(self, [lambda: os.close(owned)], primary)
        self.control.sync()
        self.root.sync()
        self.publish(self.control.path/'head.json',_json(head))

    def read_target(self, rel, cap):
        parts = _target_name(rel)
        self.check()
        # Optional absence applies to a missing component only after opening
        # its parent. Links/non-directory parents are failures, never absence.
        guards, result, primary = [], None, None
        try:
            parent = self.root
            for part in parts[:-1]:
                parent.check()
                parent._case_spelling(part)
                try:
                    child = _Directory(parent.path / part)
                except FileNotFoundError as error:
                    if native_cleanup.unknown(error): raise
                    parent.check()
                    return None
                guards.append(child)
                parent = child
            parent._case_spelling(parts[-1])
            result = io.read_file(parent.path / parts[-1], cap, optional=True)
            self.check()
            return result
        except BaseException as error:
            primary = error
            raise
        finally:
            owned, guards = guards, []
            native_cleanup.drain_owned(self, [child.close for child in reversed(owned)], primary)

    def close(self):
        if (self.pid, self.thread) != (os.getpid(), threading.get_ident()):
            raise ValueError('restore authority belongs to another process/thread')
        if self.closed:
            if self._cleanup_unconfirmed:
                error = ValueError('restore cleanup remains unconfirmed')
                native_cleanup.mark(error)
                raise error
            return
        self.closed = True
        fd, held, self.fd, self.held = self.fd, self.held, None, False
        directories, self.directories = self.directories, []
        self.operations = []
        actions = []
        if fd is not None:
            if held: actions.append(lambda: _unlock(fd))
            actions.append(lambda: os.close(fd))
        actions.extend(directory.close for directory in reversed(directories))
        native_cleanup.drain_owned(self, actions)
        if self._cleanup_unconfirmed:
            error = ValueError('restore cleanup remains unconfirmed')
            native_cleanup.mark(error)
            raise error


@contextmanager
def _held_directory(root, directory):
    """Hold operation identity; detach it before one scoped close attempt."""
    failure, registered = None, False
    try:
        root.directories.append(directory)
        root.operations.append(directory)
        registered = True
        root.check()
        yield directory
        root.check()
    except BaseException as error:
        failure = error
    finally:
        if registered:
            root.operations.remove(directory)
        if directory in root.directories:
            root.directories.remove(directory)
        owned, directory = directory, None
        try:
            native_cleanup.drain_owned(root, [owned.close], failure)
        except BaseException as error:
            failure = error
    if failure is not None:
        raise failure


@contextmanager
def _held_operation(root, operation_id):
    with _held_directory(root, _operation(root, operation_id)) as directory:
        yield directory


@contextmanager
def _authority(destination, live_root, forbidden, operation_id, *, initialize, protected_directories=(), input_lease=None):
    _opid(operation_id)
    root = None
    failure = None
    try:
        if type(live_root) is _Directory:
            live_root.check()
            live = _normalized(live_root.path)
        else:
            live = _normalized(live_root)
        selected = _normalized(destination)
        protected = _frozen_paths(forbidden)
        selected = _destination(selected,live,protected)
        root = _Root(selected, initialize=initialize,
            live_root=live_root if type(live_root) is _Directory else live,
            forbidden=protected,protected_directories=protected_directories,input_lease=input_lease)
        yield root
    except BaseException as error:
        failure = error
    finally:
        if root is not None:
            try:
                native_cleanup.drain_owned(root, [root.close], failure)
            except BaseException as error:
                failure = error
    if failure is not None:
        if isinstance(failure, Exception):
            # Only UUIDs and exact known public reasons can cross this boundary.
            # Rebuild the exception so native chains and old tracebacks cannot.
            public_id = operation_id
            if (type(failure) is RestoreError and type(failure.operation_id) is str
                    and _ID.fullmatch(failure.operation_id)):
                public_id = failure.operation_id
            public = RestoreError(public_id,phase=root.phase if root is not None else 'initialization')
            if (type(failure) is RestoreError and len(failure.args)==1
                    and type(failure.args[0]) is str and failure.args[0] in _PUBLIC_REASONS):
                public.args = (failure.args[0],)
            stage_facts.carry(failure, public, () if root is None else root.confirmed_stage_facts)
            failure = None
            _raise_public_error(public)
        stage_facts.carry(failure, failure, () if root is None else root.confirmed_stage_facts)
        stage_facts.retain_id(failure, operation_id)
        raise failure


def _backup_verification(value, evidence):
    """Bounded decisions retained for the exact archive/signature consumed.

    This validates recorded decisions, not a new signature or private custody.
    Legacy six-field plans remain readable and cannot acquire new trust claims.
    """
    keys = {'signature_math_valid','signer_trusted','signer_trust','signer_authenticated',
            'signer_default','publicKey','local_signing_custody','local_publication'}
    booleans = ('signature_math_valid','signer_trusted','signer_authenticated','signer_default')
    if (type(value) is not dict or set(value)!=keys
            or any(type(value[name]) is not bool for name in booleans)
            or type(value['signer_trust']) is not str
            or value['signer_trust'] not in ('unsigned','unknown','pinned','local','public_default_local_integrity')
            or type(value['local_signing_custody']) is not str
            or value['local_signing_custody'] not in ('not_inspected_unsigned',
                'not_inspected_pinned_verification','captured_local_fallback','captured')):
        raise ValueError('invalid bounded backup verification decisions')
    if evidence['signed']:
        if not _digest(value['publicKey']) or evidence['signature_sha256'] is None:
            raise ValueError('signed restore evidence lacks its exact signature identity')
    elif (value['publicKey'] is not None or evidence['signature_sha256'] is not None
            or any(value[name] for name in booleans) or value['signer_trust']!='unsigned'
            or value['local_signing_custody']!='not_inspected_unsigned'):
        raise ValueError('unsigned restore evidence makes a signing claim')
    trusted = value['signer_trust'] in ('pinned','local','public_default_local_integrity')
    if value['signer_trusted'] != trusted:
        raise ValueError('restore signer trust metadata is inconsistent')
    if evidence['signature_ok'] and not (evidence['signed'] and value['signature_math_valid'] and trusted):
        raise ValueError('restore accepted signature lacks verification and trust')
    if value['signer_authenticated'] != (evidence['signature_ok'] and not value['signer_default']):
        raise ValueError('restore signer authentication metadata is inconsistent')
    if value['signer_trust']=='public_default_local_integrity' and not value['signer_default']:
        raise ValueError('restore public-default trust metadata is inconsistent')
    if value['signer_default'] and trusted and value['signer_trust']!='public_default_local_integrity':
        raise ValueError('restore public-default key cannot claim a private trusted identity')
    if evidence['signature_override'] != (not evidence['signature_ok']):
        raise ValueError('restore signature refusal requires an explicit recorded override')
    local = value['local_publication']
    if type(local) is not dict or type(local.get('kind')) is not str:
        raise ValueError('invalid local publication evidence')
    if local['kind']=='local_completed':
        if (set(local)!={'kind','operation_id','plan_sha256','archive_sha256','signature_sha256'}
                or not _digest(local['plan_sha256'])
                or local['archive_sha256']!=evidence['archive_sha256']
                or local['signature_sha256']!=evidence['signature_sha256']
                or not evidence['signed']):
            raise ValueError('local publication does not bind the consumed pair')
        _opid(local['operation_id'])
    elif local['kind'] not in ('external_pair','legacy_local') or set(local)!={'kind'}:
        raise ValueError('unsupported local publication evidence')
    enc = evidence['archive_encryption']
    actual = (enc['encrypted'],enc['format_version'],enc['context_authenticated'],enc.get('legacy_unbound',False))
    if actual not in ((False,None,False,False),(True,0,False,True),(True,1,True,False)):
        raise ValueError('restore encryption decisions are inconsistent')


def _evidence(value):
    """Validate old plans and bounded exact-consumption decisions in new plans."""
    keys = {'archive_sha256','signature_sha256','signed','signature_ok',
            'signature_override','archive_encryption'}
    if (type(value) is not dict or set(value) not in (keys,keys|{'backup_verification'})
            or not _digest(value['archive_sha256'])
            or value['signature_sha256'] is not None and not _digest(value['signature_sha256'])
            or any(type(value[k]) is not bool for k in ('signed','signature_ok','signature_override'))):
        raise ValueError('invalid restore verification evidence')
    enc = value['archive_encryption']
    if (type(enc) is not dict or set(enc) - {'encrypted','format_version','context_authenticated','legacy_unbound'}
            or not {'encrypted','format_version','context_authenticated'} <= set(enc)
            or type(enc['encrypted']) is not bool or type(enc['context_authenticated']) is not bool
            or type(enc.get('legacy_unbound',False)) is not bool
            or enc['format_version'] is not None and type(enc['format_version']) is not int
            or enc['format_version'] not in (None,0,1)):
        raise ValueError('invalid restore encryption evidence')
    if 'backup_verification' in value:
        _backup_verification(value['backup_verification'],value)
    return fmt.strict_json(_json(value), MAX_PLAN)


def _operation(root, operation_id):
    root.check()
    return root.control.child(_opid(operation_id))


def _image_path(directory, index, kind):
    # Keep every native spelling-check enumeration well below 4096 entries.
    return directory.path/f'images-{index // 512:03d}'/f'{index:05d}.{kind}'


def _plan(root, operation_id, expected_hash):
    """Authenticate complete immutable plan and ALL retained images first."""
    with _held_operation(root, operation_id) as directory:
        raw = io.read_file(directory.path/'plan.json', MAX_PLAN)
        if _hash(raw) != expected_hash:
            raise ValueError('restore plan digest mismatch')
        plan = fmt.strict_json(raw, MAX_PLAN)
        if (type(plan) is not dict or set(plan) != {'schema','operation_id','initialization_sha256','initialization_identity',
                'operation_identity','evidence','files'} or plan['schema'] != SCHEMA
                or plan['operation_id'] != operation_id
                or plan['initialization_sha256'] != _hash(root.marker)
                or plan['initialization_identity'] != root.marker_identity
                or plan['operation_identity'] != _ident(directory)
                or type(plan['files']) is not list or len(plan['files']) > fmt.MAX_FILES):
            raise ValueError('restore plan identity or schema mismatch')
        _evidence(plan['evidence'])
        images, folded, before_total, after_total = [], set(), 0, 0
        for index,row in enumerate(plan['files']):
            if type(row) is not dict or set(row) != {'path','before_sha256','before_bytes','after_sha256','after_bytes'}:
                raise ValueError('restore plan file schema mismatch')
            rel = row['path']; _target_name(rel)
            if rel.casefold() in folded:
                raise ValueError('restore plan path collision')
            folded.add(rel.casefold())
            if (type(row['after_bytes']) is not int or not 0 <= row['after_bytes'] <= fmt.MAX_FILE
                    or not _digest(row['after_sha256'])):
                raise ValueError('restore plan image declaration mismatch')
            before = None
            if row['before_sha256'] is None:
                if row['before_bytes'] is not None:
                    raise ValueError('restore plan absence declaration mismatch')
            else:
                if (not _digest(row['before_sha256']) or type(row['before_bytes']) is not int
                        or not 0 <= row['before_bytes'] <= fmt.MAX_FILE):
                    raise ValueError('restore plan before declaration mismatch')
                before_total += row['before_bytes']
                if before_total > fmt.MAX_SOURCE:
                    raise ValueError('restore before images exceed aggregate bound')
                before = io.read_file(_image_path(directory,index,'before'), row['before_bytes'])
                if len(before) != row['before_bytes'] or _hash(before) != row['before_sha256']:
                    raise ValueError('restore before image mismatch')
            after_total += row['after_bytes']
            if after_total > fmt.MAX_SOURCE:
                raise ValueError('restore after images exceed aggregate bound')
            after = io.read_file(_image_path(directory,index,'after'), row['after_bytes'])
            if len(after) != row['after_bytes'] or _hash(after) != row['after_sha256']:
                raise ValueError('restore after image mismatch')
            images.append((rel,before,after))
        fmt.validate_paths(rel for rel,_before,_after in images)
        root.check()
        return plan, images


def _completion(root, operation_id, plan_hash, count):
    path = root.control.path/operation_id/'complete.json'
    raw = io.read_file(path, MAX_PLAN, optional=True)
    expected = {'schema':SCHEMA,'operation_id':operation_id,'plan_sha256':plan_hash,'restored':count}
    if raw is not None and fmt.strict_json(raw, MAX_PLAN) != expected:
        raise ValueError('restore completion witness mismatch')
    return raw, expected


def _capacity(root, images):
    """Bound projected target-directory inventories before any data write."""
    projected = {'':{CONTROL}}
    for rel,_before,_after in images:
        parts = _target_name(rel)
        for index,part in enumerate(parts):
            projected.setdefault('/'.join(parts[:index]),set()).add(part)
    for rel,needed in projected.items():
        root.check()
        path = root.path/Path(*rel.split('/')) if rel else root.path
        try:
            directory = _Directory(path)
        except FileNotFoundError as error:
            if native_cleanup.unknown(error): raise
            root.check()
            names = set()
        else:
            with closing(directory), closing(directory.entries()) as entries:
                names = set()
                for entry in entries:
                    names.add(entry.name)
                    if len(names) > MAX_DIRECTORY_ENTRIES:
                        raise ValueError('restore target directory exceeds bounded publication capacity')
        folded = {name.casefold():name for name in names}
        for name in needed:
            if name.casefold() in folded and folded[name.casefold()] != name:
                raise ValueError('restore target directory contains a case alias')
        if len(names | needed) > MAX_DIRECTORY_ENTRIES:
            raise ValueError('restore projected target directory exceeds bounded publication capacity')


def _apply_held(root, operation_id, plan_hash):
    plan, images = _plan(root, operation_id, plan_hash)
    completed, receipt = _completion(root, operation_id, plan_hash, len(images))
    _capacity(root,images)
    for rel,before,after in images:
        current = root.read_target(rel, fmt.MAX_FILE)
        if completed is not None:
            if current != after:
                raise RestoreError(operation_id,'restore ID completed historically but its target changed; refusing to reapply')
        elif current != before and current != after:
            raise RestoreError(operation_id,'restore target conflicts with retained before/after evidence; no further files published')
    if completed is not None:
        # A visible receipt may be the result of a lost completion-barrier
        # acknowledgment. Reconfirm only that receipt after the ALL-target
        # check; never silently reapply changed historical target data.
        root.root.sync()
        root.publish(root.control.path/operation_id/'complete.json', _json(receipt))
        root.confirmed_stage_facts = stage_facts.accumulate(root.confirmed_stage_facts,
            stage_facts.ConfirmedStage('restore', operation_id, plan['evidence']['archive_sha256'], plan['evidence']['signature_sha256'], plan_hash))
        return {**receipt, 'historical':True, 'into':str(root.path), 'evidence':plan['evidence']}
    # Every copy and every target passed preflight. Recheck each before replace.
    root.phase = 'applying'
    for index,(rel,before,after) in enumerate(images):
        if root.read_target(rel, fmt.MAX_FILE) not in (before,after):
            raise RestoreError(operation_id,'restore target changed after preflight; retain evidence and resolve conflict')
        # Even an already-visible after image is republished to repeat barriers.
        root.publish(root.path/Path(*_target_name(rel)), after)
        progress = {'schema':SCHEMA,'operation_id':operation_id,'plan_sha256':plan_hash,'confirmed_files':index+1}
        root.publish(root.control.path/operation_id/'progress.json', _json(progress))
    for rel,_before,after in images:
        if root.read_target(rel, fmt.MAX_FILE) != after:
            raise ValueError('restore final image mismatch')
    root.root.sync()
    root.publish(root.control.path/operation_id/'complete.json', _json(receipt))
    root.confirmed_stage_facts = stage_facts.accumulate(root.confirmed_stage_facts,
        stage_facts.ConfirmedStage('restore', operation_id, plan['evidence']['archive_sha256'], plan['evidence']['signature_sha256'], plan_hash))
    return {**receipt, 'historical':False, 'into':str(root.path), 'evidence':plan['evidence']}


def _apply(root, operation_id, plan_hash):
    with _held_operation(root,operation_id):
        return _apply_held(root,operation_id,plan_hash)


def restore(destination, changes, evidence, *, operation_id, live_root, forbidden=(), force=False, protected_directories=(), input_lease=None):
    """Prepare a new operation; recover an interrupted operation via resume().

    live_root must exist to anchor native exclusion. On a replacement machine,
    select an initialized (possibly empty) memory directory and restore into a
    distinct absent offline destination. Missing-live-root recovery is refused;
    the engine does not create or reconstruct that configured directory.
    """
    _opid(operation_id)
    if type(force) is not bool or type(changes) is not dict or len(changes) > fmt.MAX_FILES:
        raise RestoreError(operation_id,'invalid restore request')
    changes = dict(changes)  # caller mutation during filesystem I/O cannot change the plan
    # Validate every after image before creating even the offline destination.
    total, folded = 0, set()
    for rel,raw in changes.items():
        _target_name(rel)
        if type(raw) is not bytes or len(raw) > fmt.MAX_FILE or rel.casefold() in folded:
            raise RestoreError(operation_id,'invalid bounded restore image')
        total += len(raw); folded.add(rel.casefold())
        if total > fmt.MAX_SOURCE:
            raise RestoreError(operation_id,'restore images exceed aggregate bound')
    fmt.validate_paths(changes)
    verified = _evidence(evidence)
    with _authority(destination,live_root,forbidden,operation_id,initialize=True,protected_directories=protected_directories,input_lease=input_lease) as root:
        head = root.head()
        if head['operation_id'] is not None:
            with _held_operation(root,head['operation_id']):
                old_plan,old_images = _plan(root,head['operation_id'],head['plan_sha256'])
                completed,_ = _completion(root,head['operation_id'],head['plan_sha256'],len(old_images))
                if completed is None:
                    raise RestoreError(head['operation_id'],'an unfinished restore owns this destination; resume its ID')
                if not force:
                    raise RestoreError(operation_id,'non-empty restore-owned destination requires force=True for a new operation')
                del old_images
        count = 0
        with closing(root.control.entries()) as entries:
            for entry in entries:
                count += 1
                if count > MAX_OPERATIONS + 16:
                    raise ValueError('restore history bound exceeded')
        root.check()
        directory = _exclusive_child(root.control, operation_id)
        with _held_directory(root,directory):
            rows, total = [], 0
            for index,(rel,after) in enumerate(sorted(changes.items())):
                before = root.read_target(rel, min(fmt.MAX_FILE,fmt.MAX_SOURCE-total))
                if before is not None and not force:
                    raise RestoreError(operation_id,'existing restore target requires force=True')
                root.check()
                with closing(directory.child(f'images-{index // 512:03d}', create=True)) as bucket:
                    if before is not None:
                        total += len(before)
                        bucket.write_new(f'{index:05d}.before', before)
                    bucket.write_new(f'{index:05d}.after', after)
                rows.append({'path':rel,'before_sha256':None if before is None else _hash(before),
                             'before_bytes':None if before is None else len(before),
                             'after_sha256':_hash(after),'after_bytes':len(after)})
            plan = {'schema':SCHEMA,'operation_id':operation_id,'initialization_sha256':_hash(root.marker),
                    'initialization_identity':root.marker_identity,
                    'operation_identity':_ident(directory),'evidence':verified,'files':rows}
            raw = _json(plan)
            directory.write_new('plan.json',raw)
            plan_hash = _hash(raw)
            _plan(root,operation_id,plan_hash)
            head = {'schema':SCHEMA,'operation_id':operation_id,'plan_sha256':plan_hash,
                    'initialization_identity':root.marker_identity}
            root.phase = 'intent_publication'
            root.publish(root.control.path/'head.json',_json(head))
            root.confirm_witnesses(head)
            return _apply(root,operation_id,plan_hash)


def resume(destination, *, operation_id, live_root, forbidden=(), protected_directories=(), input_lease=None):
    """Resume solely from retained verified images; never reopen the archive."""
    with _authority(destination,live_root,forbidden,operation_id,initialize=False,protected_directories=protected_directories,input_lease=input_lease) as root:
        head = root.head()
        if head['operation_id'] != operation_id:
            raise RestoreError(operation_id,'requested restore ID is not the active retained operation')
        # Validate copies/targets first in _apply; this barrier only settles the
        # existing intent and cannot authorize a different plan or target.
        root.phase = 'intent_confirmation'
        root.confirm_witnesses(head)
        return _apply(root,operation_id,head['plan_sha256'])


def inspect(destination, *, operation_id, live_root, forbidden=(), protected_directories=(), input_lease=None):
    """Read retained state without publishing, repairing, or reapplying files.

    Preparation failures before active intent have changed no target data. An
    inactive/incomplete preparation is retained for inspection; it cannot be
    resumed by substituting another archive. Use a fresh absent destination.
    Initialization failures likewise require a fresh destination, preserving
    the incomplete original. Active operations resume only their retained ID.
    """
    with _authority(destination,live_root,forbidden,operation_id,initialize=False,protected_directories=protected_directories,input_lease=input_lease) as root:
        with _held_operation(root,operation_id):
            head = root.head()
            active = head['operation_id'] == operation_id
            if active:
                plan_hash = head['plan_sha256']
            else:
                with closing(_operation(root,operation_id)) as directory:
                    raw = io.read_file(directory.path/'plan.json',MAX_PLAN,optional=True)
                if raw is None:
                    return {'operation_id':operation_id,'status':'preparation_incomplete',
                            'resumable':False,'target_data_changed':False}
                plan_hash = _hash(raw)
            plan,images = _plan(root,operation_id,plan_hash)
            completed,_ = _completion(root,operation_id,plan_hash,len(images))
            conflicts = []
            for rel,before,after in images:
                current = root.read_target(rel,fmt.MAX_FILE)
                if current != after and (completed is not None or current != before):
                    conflicts.append(rel)
            status = ('completed' if completed is not None else 'active' if active else 'prepared_inactive')
            return {'operation_id':operation_id,'status':status,'resumable':active and not conflicts,
                    'conflicts':conflicts,'plan_sha256':plan_hash,
                    'target_data_changed':None if active or completed is not None else False}
