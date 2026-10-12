"""Retained, operation-bound publication of a local backup/signature pair.

The lock serializes cooperating backup publishers and consumers. It is not an
application-wide snapshot or protection against administrators. A completed
pair requires both exact retained images and a completion receipt. Never treat
a generated v2 name whose journal is missing as a legacy local publication.
External archive/signature snapshots are verified separately by the facade;
this local journal is not required to authenticate an off-machine pair.

All recovery copies and interrupted stages are retained. POSIX file/directory
barriers are used; Windows performs file flushes and fresh ancestor checks,
without a directory-fsync or physical-power-loss claim.
"""
from __future__ import annotations

from contextlib import contextmanager
from .native_cleanup import closing
import hashlib
import json
import os
import re
import threading

from . import archive_io as io, backup_archive as fmt, backup_facts as stage_facts
from .backup_restore import _exclusive_child, _held_directory, _raise_public_error
from .vault_storage import (_Directory, _directory_identity, _file_information,
                            _file_identity, _try_lock, _unlock)
from . import native_cleanup

CONTROL = '.olympus-backup-publication'
SCHEMA = 'olympus-backup-publication/1'
PREFIX = 'olympus-backup-v2-'
MAX_PLAN = 64 * 1024
MAX_OPERATIONS = 1024
MAX_DIRECTORY_ENTRIES = 3072
_ID = re.compile(r'[0-9a-f]{32}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_NAME = re.compile(r'olympus-backup-v2-([0-9a-f]{32})\.tar\.gz(?:\.enc)?\Z')


_PUBLIC_REASONS = frozenset({
    'backup pair is retired or retirement is unfinished; inspect its ID',
    'backup publication must complete before retirement',
    'backup pair changed; retirement refuses to replace any bytes',
    'active backup publication plan is missing',
    'an unfinished backup publication exists; resume its ID',
    'backup filename conflicts with its retained plan',
    'backup pair changed after preflight; retain evidence and resolve conflict',
    'backup pair changed during completion settlement',
    'backup pair publication is incomplete',
    'backup publication conflicts with retained images; no further files published',
    'completed backup pair bytes changed',
    'completed backup pair changed; refusing to reapply historical data',
    'invalid bounded backup publication bytes',
    'new backup pair targets must be absent',
    'requested backup ID is not the active publication',
})


class PublicationError(RuntimeError):
    def __init__(self, operation_id, reason='backup publication did not complete; retain evidence and inspect its ID', *, phase=None):
        self.operation_id, self.phase = operation_id, phase
        super().__init__(reason)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    if len(raw) > MAX_PLAN:
        raise ValueError('backup publication evidence bound exceeded')
    return raw


def _opid(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError('backup publication requires a lowercase UUID32 operation ID')
    return value


def _digest(value):
    return type(value) is str and bool(_HASH.fullmatch(value))


def _ident(directory):
    return list(_directory_identity(directory))


def _names(directory, cap):
    names = []
    with closing(directory.entries()) as entries:
        for entry in entries:
            if len(names) >= cap:
                raise ValueError('backup publication directory capacity exceeded')
            names.append(entry.name)
    return names


def archive_name(operation_id, encrypted):
    _opid(operation_id)
    if type(encrypted) is not bool:
        raise ValueError('backup publication requires explicit encryption metadata')
    return PREFIX + operation_id + '.tar.gz' + ('.enc' if encrypted else '')


def managed_name(name):
    """Recognize the reserved namespace even when its suffix is malformed."""
    return type(name) is str and name.casefold().startswith(PREFIX.casefold())


class _Root:
    def __init__(self, directory, *, initialize):
        borrowed = type(directory) is _Directory
        self.path = io.selected_path(directory.path if borrowed else directory)
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.directories, self.fd, self.held = [], None, False
        self.operations = []
        self.closed, self.marker, self.marker_identity = False, None, None
        self._cleanup_unconfirmed = False
        self.phase = 'initialization'
        self.confirmed_stage_facts = ()
        try:
            if borrowed:
                # Keep the caller's original native chain; a pathname reopen
                # could initialize a replacement directory at this handoff.
                root = directory
                root.check()
            else:
                root = _Directory(self.path)
                self.directories.append(root)
            self.root = root
            names = _names(root, MAX_DIRECTORY_ENTRIES)
            root._case_spelling(CONTROL)
            created = False
            if CONTROL not in names:
                if not initialize:
                    raise ValueError('backup publication journal is absent')
                if any(managed_name(name) for name in names):
                    raise ValueError('generated backup names exist without their publication journal')
                control = _exclusive_child(root, CONTROL)
                created = True
            else:
                control = root.child(CONTROL)
            self.directories.append(control); self.control = control
            self.fd = control._open('publication.lock', os.O_RDWR | (os.O_CREAT | os.O_EXCL if created else 0))
            self.lock_identity = list(_file_identity(_file_information(self.fd))[:2])
            if not _try_lock(self.fd):
                raise ValueError('backup publication directory is busy')
            self.held = True
            expected = {'schema':SCHEMA, 'directory':str(self.path),
                        'root':_ident(root), 'control':_ident(control), 'lock':self.lock_identity}
            if created:
                os.fsync(self.fd); control.sync()
                self._put_new('initialization.json', _json(expected))
            raw, identity = control.read_with_identity('initialization.json', MAX_PLAN)
            if fmt.strict_json(raw, MAX_PLAN) != expected:
                raise ValueError('backup publication initialization identity mismatch')
            self.marker, self.marker_identity = raw, [identity['device'],identity['inode']]
            if created:
                self._put_new('head.json', _json({'schema':SCHEMA, 'operation_id':None, 'plan_sha256':None,
                                                'initialization_identity':self.marker_identity}))
            self.check(); self.phase = 'preparation'
        except BaseException as error:
            native_cleanup.drain_owned(self, [self.close], error)
            raise

    def _put_new(self, name, raw):
        self.control.write_new(name, raw)
        if io.read_file(self.control.path / name, len(raw)) != raw:
            raise ValueError('backup publication evidence verification failed')

    def check(self):
        if self._cleanup_unconfirmed:
            error = ValueError('backup publication cleanup requires reconciliation')
            native_cleanup.mark(error)
            raise error
        if (self.closed or not self.held or self.fd is None
                or (self.pid,self.thread) != (os.getpid(),threading.get_ident())):
            raise ValueError('backup publication authority is inactive or belongs to another process/thread')
        self.root.check()
        self.control.check()
        for operation in self.operations:
            operation.check()
        if list(_file_identity(_file_information(self.fd))[:2]) != self.lock_identity:
            raise ValueError('backup publication lock identity changed')
        if self.control.win is None:
            entry = os.stat('publication.lock', dir_fd=self.control.fd, follow_symlinks=False)
            if list(_file_identity(entry)[:2]) != self.lock_identity:
                raise ValueError('backup publication lock path was replaced')
        if self.marker is not None:
            raw, identity = self.control.read_with_identity('initialization.json',MAX_PLAN)
            if raw != self.marker or [identity['device'],identity['inode']] != self.marker_identity:
                raise ValueError('backup publication initialization changed')

    def publish(self, path, raw):
        self.check()
        io.publish_file(path, raw)
        self.check()
        if io.read_file(path, len(raw)) != raw:
            raise ValueError('backup publication publication verification failed')

    def head(self):
        self.check()
        value = fmt.strict_json(io.read_file(self.control.path/'head.json', MAX_PLAN), MAX_PLAN)
        if (type(value) is not dict or set(value) != {'schema','operation_id','plan_sha256','initialization_identity'}
                or value['schema'] != SCHEMA or value['initialization_identity'] != self.marker_identity):
            raise ValueError('backup publication active witness is invalid')
        if value['operation_id'] is None:
            if value['plan_sha256'] is not None:
                raise ValueError('backup publication active witness is invalid')
        else:
            _opid(value['operation_id'])
            if not _digest(value['plan_sha256']):
                raise ValueError('backup publication active witness is invalid')
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
                raise ValueError('backup publication initialization identity changed')
            if os.read(fd,len(self.marker)+1) != self.marker:
                raise ValueError('backup publication initialization changed')
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

    def close(self):
        if (self.pid, self.thread) != (os.getpid(), threading.get_ident()):
            raise ValueError('backup publication authority belongs to another process/thread')
        if self.closed:
            if self._cleanup_unconfirmed:
                error = ValueError('backup publication cleanup remains unconfirmed')
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
            error = ValueError('backup publication cleanup remains unconfirmed')
            native_cleanup.mark(error)
            raise error


@contextmanager
def locked(directory, *, initialize=False, operation_id=None):
    """Held publication namespace for publishers, listings and delivery/prune.

    Callers must keep this lease until their local consumer operation is done.
    An internal _Directory argument borrows its live native authority without
    reopening its pathname or closing it; the caller owns that outer lifetime.
    Missing journals are errors here, including generated v2 filenames. Legacy
    unjournaled archives require the facade's explicit legacy-only read path.
    """
    if operation_id is not None:
        _opid(operation_id)
    if type(initialize) is not bool:
        raise ValueError('invalid backup publication request')
    root, failure = None, None
    try:
        root = _Root(directory, initialize=initialize)
        yield root
    except BaseException as error:
        failure = error
    finally:
        if root is not None:
            try: native_cleanup.drain_owned(root, [root.close], failure)
            except BaseException as error: failure = error
    if failure is not None:
        if isinstance(failure, Exception):
            # Only UUIDs and exact known public reasons can cross this boundary.
            # Rebuild the exception so native chains and old tracebacks cannot.
            public_id = operation_id
            if (type(failure) is PublicationError and type(failure.operation_id) is str
                    and _ID.fullmatch(failure.operation_id)):
                public_id = failure.operation_id
            public = PublicationError(public_id,phase=root.phase if root is not None else 'initialization')
            if (type(failure) is PublicationError and len(failure.args)==1
                    and type(failure.args[0]) is str and failure.args[0] in _PUBLIC_REASONS):
                public.args = (failure.args[0],)
            stage_facts.carry(failure, public, () if root is None else root.confirmed_stage_facts)
            failure = None
            _raise_public_error(public)
        stage_facts.carry(failure, failure, () if root is None else root.confirmed_stage_facts)
        stage_facts.retain_id(failure, operation_id)
        raise failure


def _details(value):
    if (type(value) is not dict or set(value) != {'encrypted','files','full'}
            or type(value['encrypted']) is not bool or type(value['full']) is not bool
            or type(value['files']) is not int or not 0 <= value['files'] <= fmt.MAX_FILES):
        raise ValueError('invalid backup publication details')
    return dict(value)


def _operation(root, operation_id):
    root.check()
    return root.control.child(_opid(operation_id))


@contextmanager
def _held_operation(root, operation_id):
    with _held_directory(root, _operation(root, operation_id)) as directory:
        yield directory


def _load(root, operation_id, expected_hash=None):
    """Read a bounded immutable plan and both complete retained byte images."""
    with _held_operation(root,operation_id) as directory:
        raw = io.read_file(directory.path/'plan.json', MAX_PLAN)
        digest = _hash(raw)
        if expected_hash is not None and digest != expected_hash:
            raise ValueError('backup publication plan digest mismatch')
        value = fmt.strict_json(raw, MAX_PLAN)
        keys = {'schema','operation_id','initialization_sha256','initialization_identity',
                'operation_identity','name','archive_sha256','archive_bytes',
                'signature_sha256','signature_bytes','details'}
        if (type(value) is not dict or set(value) != keys or value['schema'] != SCHEMA
                or value['operation_id'] != operation_id
                or value['initialization_sha256'] != _hash(root.marker)
                or value['initialization_identity'] != root.marker_identity
                or value['operation_identity'] != _ident(directory)):
            raise ValueError('backup publication plan identity mismatch')
        details = _details(value['details'])
        if value['name'] != archive_name(operation_id, details['encrypted']):
            raise ValueError('backup publication name does not match its operation')
        images = []
        for kind, cap in (('archive',io.MAX_CIPHERTEXT),('signature',fmt.MAX_SIGNATURE)):
            size, checksum = value[kind+'_bytes'], value[kind+'_sha256']
            if type(size) is not int or not 0 < size <= cap or not _digest(checksum):
                raise ValueError('invalid bounded backup publication image')
            image = io.read_file(directory.path/(kind+'.image'),size)
            if len(image) != size or _hash(image) != checksum:
                raise ValueError('backup publication retained image mismatch')
            images.append(image)
        root.check()
        return value, digest, tuple(images)


def _completion(root, plan, plan_hash):
    receipt = {'schema':SCHEMA,'operation_id':plan['operation_id'],'plan_sha256':plan_hash,
               'name':plan['name'],'archive_sha256':plan['archive_sha256'],
               'signature_sha256':plan['signature_sha256'],'publication_complete':True}
    raw = io.read_file(root.control.path/plan['operation_id']/'complete.json',MAX_PLAN,optional=True)
    if raw is not None and fmt.strict_json(raw,MAX_PLAN) != receipt:
        raise ValueError('backup publication completion witness mismatch')
    return raw, receipt


def _current(root, plan):
    root.check()
    raw = io.read_file(root.path/plan['name'],io.MAX_CIPHERTEXT,optional=True)
    sig = io.read_file(root.path/(plan['name']+'.sig.json'),fmt.MAX_SIGNATURE,optional=True)
    root.check()
    return raw, sig


def _retirement_state(root, plan, plan_hash):
    """Observe a plan-bound retirement without deleting either published image."""
    receipt = {'schema':SCHEMA,'operation_id':plan['operation_id'],'plan_sha256':plan_hash,
               'name':plan['name'],'archive_sha256':plan['archive_sha256'],
               'signature_sha256':plan['signature_sha256'],'retired':True,
               'retained_images':True,'files_deleted':0}
    directory = root.control.path/plan['operation_id']
    intent = io.read_file(directory/'retirement-intent.json',MAX_PLAN,optional=True)
    completed = io.read_file(directory/'retired.json',MAX_PLAN,optional=True)
    if completed is not None and intent is None:
        raise ValueError('backup retirement completion has no retained intent')
    for raw in (intent,completed):
        if raw is not None and fmt.strict_json(raw,MAX_PLAN) != receipt:
            raise ValueError('backup retirement witness conflicts with its publication plan')
    root.check()
    return ('completion_visible' if completed is not None else 'intent_visible' if intent is not None else None),receipt


def _require_unretired(root, plan, plan_hash):
    state,_receipt = _retirement_state(root,plan,plan_hash)
    if state is not None:
        raise PublicationError(plan['operation_id'],'backup pair is retired or retirement is unfinished; inspect its ID')


def _retire_held(root, operation_id):
    """Retire one exact ID while borrowing the caller's publication lease."""
    _opid(operation_id)
    if type(root) is not _Root:
        raise ValueError('backup retirement requires an exact publication lease')
    root.check()
    with _held_operation(root,operation_id):
        head = root.head()
        expected = head['plan_sha256'] if head['operation_id']==operation_id else None
        plan,digest,images = _load(root,operation_id,expected)
        completed,_receipt = _completion(root,plan,digest)
        if completed is None:
            raise PublicationError(operation_id,'backup publication must complete before retirement')
        state,retirement = _retirement_state(root,plan,digest)
        if _current(root,plan) != images:
            raise PublicationError(operation_id,'backup pair changed; retirement refuses to replace any bytes')
        raw = _json(retirement)
        root.phase = 'retirement_intent'
        root.publish(root.control.path/operation_id/'retirement-intent.json',raw)
        if _current(root,plan) != images:
            raise PublicationError(operation_id,'backup pair changed; retirement refuses to replace any bytes')
        root.root.sync()
        root.phase = 'retirement_completion'
        root.publish(root.control.path/operation_id/'retired.json',raw)
        if _current(root,plan) != images:
            raise PublicationError(operation_id,'backup pair changed; retirement refuses to replace any bytes')
        root.confirmed_stage_facts = stage_facts.accumulate(root.confirmed_stage_facts,
            stage_facts.ConfirmedStage('retirement', operation_id, plan['archive_sha256'], plan['signature_sha256'], digest))
        return {**retirement,'retirement_complete':True,'historical':state is not None,
                'retained_pair_bytes':plan['archive_bytes']+plan['signature_bytes']}



def retire(directory, *, operation_id):
    """Retire a completed local pair; resume the same ID after interruption.

    Retirement withdraws it from automatic listing/delivery selection. Both
    final files and all immutable recovery images remain. No disk space is
    reclaimed, no historical bytes are republished, and no later ID is affected.
    Explicit future storage reclamation is a separate maintenance operation.
    """
    _opid(operation_id)
    with locked(directory,operation_id=operation_id) as root:
        return _retire_held(root,operation_id)


def _apply_held(root, operation_id, plan_hash):
    plan, digest, images = _load(root,operation_id,plan_hash)
    _require_unretired(root,plan,digest)
    completed, receipt = _completion(root,plan,digest)
    current = _current(root,plan)
    if completed is not None:
        if current != images:
            raise PublicationError(operation_id,'completed backup pair changed; refusing to reapply historical data')
    elif any(now is not None and now != after for now,after in zip(current,images)):
        raise PublicationError(operation_id,'backup publication conflicts with retained images; no further files published')
    if completed is None:
        root.phase = 'applying'
        for index,name in enumerate((plan['name'],plan['name']+'.sig.json')):
            now = _current(root,plan)
            if any(value is not None and value != after for value,after in zip(now,images)):
                raise PublicationError(operation_id,'backup pair changed after preflight; retain evidence and resolve conflict')
            root.publish(root.path/name,images[index])
            root.publish(root.control.path/operation_id/'progress.json',_json({
                'schema':SCHEMA,'operation_id':operation_id,'plan_sha256':digest,'confirmed_files':index+1}))
    if _current(root,plan) != images:
        raise ValueError('backup pair final verification failed')
    root.root.sync()
    root.publish(root.control.path/operation_id/'complete.json',_json(receipt))
    root.confirmed_stage_facts = stage_facts.accumulate(root.confirmed_stage_facts,
        stage_facts.ConfirmedStage('publication', operation_id, plan['archive_sha256'], plan['signature_sha256'], digest))
    return {**receipt, 'path':str(root.path/plan['name']), 'bytes':plan['archive_bytes'],
            'historical':completed is not None, **plan['details']}


def _apply(root, operation_id, plan_hash):
    with _held_operation(root,operation_id):
        return _apply_held(root,operation_id,plan_hash)


def publish(directory, archive, signature, details, *, operation_id):
    """Publish a newly generated pair; retry interruption only with resume(ID)."""
    _opid(operation_id)
    selected = _details(details)
    if (type(archive) is not bytes or not 0 < len(archive) <= io.MAX_CIPHERTEXT
            or type(signature) is not bytes or not 0 < len(signature) <= fmt.MAX_SIGNATURE):
        raise PublicationError(operation_id,'invalid bounded backup publication bytes')
    name = archive_name(operation_id,selected['encrypted'])
    with locked(directory,initialize=True,operation_id=operation_id) as root:
        head = root.head()
        if head['operation_id'] is not None:
            with _held_operation(root,head['operation_id']):
                old, old_hash, _old_images = _load(root,head['operation_id'],head['plan_sha256'])
                completed, _ = _completion(root,old,old_hash)
                if completed is None:
                    raise PublicationError(head['operation_id'],'an unfinished backup publication exists; resume its ID')
        names = set(_names(root.root,MAX_DIRECTORY_ENTRIES))
        if len(names | {name,name+'.sig.json'}) > MAX_DIRECTORY_ENTRIES:
            raise ValueError('backup publication target capacity exceeded')
        if any(io.read_file(root.path/target,io.MAX_CIPHERTEXT,optional=True) is not None
               for target in (name,name+'.sig.json')):
            raise PublicationError(operation_id,'new backup pair targets must be absent')
        history = _names(root.control,MAX_OPERATIONS+16)
        if sum(bool(_ID.fullmatch(name)) for name in history) >= MAX_OPERATIONS:
            raise ValueError('backup publication operation history capacity exceeded')
        directory = _exclusive_child(root.control,operation_id)
        with _held_directory(root,directory):
            directory.write_new('archive.image',archive)
            directory.write_new('signature.image',signature)
            plan = {'schema':SCHEMA,'operation_id':operation_id,'initialization_sha256':_hash(root.marker),
                    'initialization_identity':root.marker_identity,'operation_identity':_ident(directory),
                    'name':name,'archive_sha256':_hash(archive),'archive_bytes':len(archive),
                    'signature_sha256':_hash(signature),'signature_bytes':len(signature),'details':selected}
            raw = _json(plan); digest = _hash(raw)
            directory.write_new('plan.json',raw)
            _load(root,operation_id,digest)
            head = {'schema':SCHEMA,'operation_id':operation_id,'plan_sha256':digest,
                    'initialization_identity':root.marker_identity}
            root.phase = 'intent_publication'
            root.publish(root.control.path/'head.json',_json(head))
            root.confirm_witnesses(head)
            return _apply(root,operation_id,digest)


def resume(directory, *, operation_id):
    """Settle this active ID using retained bytes; never reacquire key/archive."""
    _opid(operation_id)
    with locked(directory,operation_id=operation_id) as root:
        head = root.head()
        if head['operation_id'] != operation_id:
            raise PublicationError(operation_id,'requested backup ID is not the active publication')
        root.phase = 'intent_confirmation'
        root.confirm_witnesses(head)
        return _apply(root,operation_id,head['plan_sha256'])


def _read_completed_held(root, name):
    """Consume a locally published pair only while its namespace lease is held."""
    if type(name) is not str or not _NAME.fullmatch(name):
        raise ValueError('invalid generated backup filename')
    operation_id = _NAME.fullmatch(name).group(1)
    head = root.head()  # Missing or corrupted active control evidence is never downgraded.
    expected = head['plan_sha256'] if head['operation_id']==operation_id else None
    plan, digest, images = _load(root,operation_id,expected)
    _require_unretired(root,plan,digest)
    if plan['name'] != name:
        raise PublicationError(operation_id,'backup filename conflicts with its retained plan')
    completed, receipt = _completion(root,plan,digest)
    if completed is None:
        raise PublicationError(operation_id,'backup pair publication is incomplete')
    if _current(root,plan) != images:
        raise PublicationError(operation_id,'completed backup pair bytes changed')
    # A visible receipt can follow an unacknowledged post-rename barrier. Settle
    # that exact receipt under the held operation lease before consumption.
    root.root.sync()
    root.publish(root.control.path/operation_id/'complete.json',_json(receipt))
    if _current(root,plan) != images:
        raise PublicationError(operation_id,'backup pair changed during completion settlement')
    return images[0], images[1], {**receipt, **plan['details'],'completion_barrier_reconfirmed':True}


def read_completed(root, name):
    """Return exact pair bytes after reconfirming receipt barriers under a lease.

    This settles the same completion receipt; inspect() is the nonmutating
    observational interface. It never recreates or reapplies archive bytes.
    """
    if type(name) is not str or not _NAME.fullmatch(name):
        raise ValueError('invalid generated backup filename')
    with _held_operation(root,_NAME.fullmatch(name).group(1)):
        return _read_completed_held(root,name)


def inspect(directory, *, operation_id):
    """Read retained state; never write, reset initialization, or publish data."""
    _opid(operation_id)
    with locked(directory,operation_id=operation_id) as root:
        with _held_operation(root,operation_id):
            head = root.head(); active = head['operation_id']==operation_id
            with closing(_operation(root,operation_id)) as op:
                raw = io.read_file(op.path/'plan.json',MAX_PLAN,optional=True)
            if raw is None:
                if active:
                    raise PublicationError(operation_id,'active backup publication plan is missing')
                return {'operation_id':operation_id,'status':'preparation_incomplete','resumable':False,
                        'final_files_changed':False}
            plan,digest,images = _load(root,operation_id,head['plan_sha256'] if active else None)
            completed,receipt = _completion(root,plan,digest)
            retirement,_retirement_receipt = _retirement_state(root,plan,digest)
            if retirement is not None and completed is None:
                raise ValueError('retirement cannot precede backup publication completion')
            now = _current(root,plan)
            conflicts = [kind for kind,before,after in zip(('archive','signature'),now,images)
                         if before != after and (completed is not None or before is not None)]
            return {'operation_id':operation_id,'plan_sha256':digest,'name':plan['name'],
                    'status':'completion_visible' if completed is not None else 'active' if active else 'prepared_inactive',
                    'conflicts':conflicts,'resumable':active and not conflicts and retirement is None,
                    'retirement_status':retirement,
                    'retirement_resumable':retirement is not None and not conflicts,
                    'final_files_changed':None if active or completed is not None else False}


MAX_CATALOG_IMAGE_BYTES = 1024 * 1024 * 1024  # aggregate retained/current image reads


def _catalog_held(root):
    """Bounded, observational journal catalog under one caller-held lease.

    Each complete row binds both current files to retained images. This does
    not decrypt an archive or authenticate its signer. Rows include unfinished
    preparation and retirement so callers cannot silently report a clean set.
    No completion barrier is republished by listing. A selection candidate
    still requires read_completed() under its consuming lease; this catalog
    does not authorize delivery or prove a settled publication barrier.
    """
    root.check()
    head = root.head()
    names = _names(root.control,MAX_OPERATIONS+16)
    operations = sorted(name for name in names if _ID.fullmatch(name))
    if len(operations)>MAX_OPERATIONS:
        raise ValueError('backup publication operation history capacity exceeded')
    if head['operation_id'] is not None and head['operation_id'] not in operations:
        raise ValueError('backup active operation is missing')
    rows,known,image_bytes = [],set(),0
    for operation_id in operations:
        with _held_operation(root,operation_id):
            active = head['operation_id']==operation_id
            raw = io.read_file(root.control.path/operation_id/'plan.json',MAX_PLAN,optional=True)
            if raw is None:
                if active:raise PublicationError(operation_id,'active backup publication plan is missing')
                for name in ('progress.json','complete.json','retirement-intent.json','retired.json'):
                    if io.read_file(root.control.path/operation_id/name,MAX_PLAN,optional=True) is not None:
                        raise ValueError('backup history has post-plan evidence but no retained plan')
                rows.append({'operation_id':operation_id,'status':'preparation_incomplete',
                    'publication_complete':False,'selection_candidate':False})
                continue
            preview_digest = _hash(raw)
            if active and preview_digest!=head['plan_sha256']:
                raise ValueError('active backup catalog budget preview conflicts with its head')
            preview = fmt.strict_json(raw,MAX_PLAN)
            if type(preview) is not dict:
                raise ValueError('backup catalog plan is invalid')
            for kind,cap in (('archive',io.MAX_CIPHERTEXT),('signature',fmt.MAX_SIGNATURE)):
                size = preview.get(kind+'_bytes')
                if type(size) is not int or not 0<size<=cap:
                    raise ValueError('backup catalog image size is invalid')
                image_bytes += 2*size
                if image_bytes>MAX_CATALOG_IMAGE_BYTES:
                    raise ValueError('backup catalog aggregate image read budget exceeded; inspect individual IDs')
            # Bind the budget preview to the immutable plan used by _load.
            plan,digest,images = _load(root,operation_id,preview_digest)
            known.update((plan['name'],plan['name']+'.sig.json'))
            completed,_receipt = _completion(root,plan,digest)
            retirement,_retirement = _retirement_state(root,plan,digest)
            if retirement is not None and completed is None:
                raise ValueError('retirement cannot precede backup publication completion')
            # Bind ordering metadata to the same acquired file identity. A
            # completed pair cannot change between byte and metadata reads.
            before_identity = None if completed is None else io.file_identity(root.path/plan['name'],io.MAX_CIPHERTEXT)
            root.check()
            current = (io.read_file(root.path/plan['name'],plan['archive_bytes'],optional=True),
                io.read_file(root.path/(plan['name']+'.sig.json'),plan['signature_bytes'],optional=True))
            root.check()
            if completed is not None and current != images:
                raise PublicationError(operation_id,'completed backup pair bytes changed')
            if completed is None and any(now is not None and now!=after for now,after in zip(current,images)):
                raise PublicationError(operation_id,'backup publication conflicts with retained images; no further files published')
            identity = None if current[0] is None else io.file_identity(root.path/plan['name'],io.MAX_CIPHERTEXT)
            if completed is not None and identity!=before_identity:
                raise ValueError('backup catalog archive identity changed')
            root.check()
            rows.append({'operation_id':operation_id,'name':plan['name'],'path':str(root.path/plan['name']),
                'plan_sha256':digest,'sha256':plan['archive_sha256'],'signature_sha256':plan['signature_sha256'],
                'bytes':plan['archive_bytes'],'mtime':None if identity is None else identity[3]/1_000_000_000,
                'status':'completion_visible' if completed is not None else 'active' if active else 'prepared_inactive',
                'publication_complete':None if completed is not None else False,
                'completion_visible':completed is not None,'completion_barrier_reconfirmed':False,
                'retirement_status':retirement,
                'selection_candidate':completed is not None and retirement is None,
                'encrypted':plan['details']['encrypted'],'encryption_verified':False,
                'encryption_source':'retained_publication_plan','signer_authenticated':None,
                'ordering_source':'observed_filesystem_mtime',
                'files':plan['details']['files'],'full':plan['details']['full']})
            del current,images
    if any(managed_name(name) and name not in known for name in _names(root.root,MAX_DIRECTORY_ENTRIES)):
        raise ValueError('reserved backup pair has no retained publication plan')
    root.check()
    return sorted(rows,key=lambda row:(-(row.get('mtime') or 0),row['operation_id']))
