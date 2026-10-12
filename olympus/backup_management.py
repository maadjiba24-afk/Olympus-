"""Bounded backup listing and logical retention; no private custody or deletion.

Local managed pairs carry journal decisions. Legacy files remain visible as
unverified objects and are never automatically retired. Listing does not prove
decryption or signer authentication. Retirement retains every archive, sidecar,
recovery image and interrupted stage, so it reclaims no disk/history capacity.
"""
from __future__ import annotations

from contextlib import contextmanager
import os

from . import archive_io as aio, backup_publication as publication, backup_facts as stage_facts, config
from .vault_storage import _Directory
from . import native_cleanup


@contextmanager
def _directories(*, _authority=None, _stage=None):
    """Borrow the same selected root and backups child for the whole action."""
    from .backup import _held_backup_directory
    if _authority is not None:
        from .backup_authority import require
        require(_authority, _stage)
        child = _authority.backups()
        yield _authority.memory, child
        require(_authority, _stage)
        return
    selected = aio.selected_directory(config.MEMORY_DIR)
    with _held_backup_directory(selected) as memory:
        child,failure = None,None
        try:
            memory._case_spelling('backups')
            try:child = memory.child('backups')
            except FileNotFoundError as error:
                if native_cleanup.unknown(error): raise
                memory.check()
            if aio.selected_directory(config.MEMORY_DIR)!=selected:
                raise ValueError('backup root selection changed')
            yield memory,child
            memory.check()
            if child is not None:child.check()
            if aio.selected_directory(config.MEMORY_DIR)!=selected:
                raise ValueError('backup root selection changed')
        except BaseException as error:
            failure = error
        finally:
            if child is not None:
                owned, child = child, None
                try:native_cleanup.drain([owned.close], failure)
                except BaseException as error:failure = error
        if failure is not None:raise failure


def _legacy(directory,names):
    rows=[]
    for name in sorted(names):
        if (not name.startswith('olympus-backup-') or publication.managed_name(name)
                or name.endswith('.sig.json')):continue
        path=directory.path/name
        identity=aio.file_identity(path,aio.MAX_CIPHERTEXT)
        directory.check()
        rows.append({'name':name,'path':str(path),'bytes':identity[2],'mtime':identity[3]/1_000_000_000,
            'status':'legacy_unverified','publication_complete':False,'selection_candidate':False,
            'encrypted':None,'encryption_verified':False,'encryption_source':'not_verified',
            'signer_authenticated':None})
    return rows


@contextmanager
def _catalog(directory):
    """No missing/damaged journal downgrade for any reserved v2 namespace."""
    names=publication._names(directory,publication.MAX_DIRECTORY_ENTRIES)
    controls=[name for name in names if name.casefold()==publication.CONTROL.casefold()]
    legacy=_legacy(directory,names)
    if not controls:
        if any(publication.managed_name(name) for name in names):
            raise ValueError('reserved backup namespace has no publication journal')
        directory.check()
        yield None,legacy
        directory.check()
        return
    if controls!=[publication.CONTROL]:raise ValueError('backup publication journal spelling is ambiguous')
    # Preserve consumer failures across this context's own normalization.
    failure=None
    try:
        with publication.locked(directory) as root:
            rows=publication._catalog_held(root)
            root.check()
            try:
                yield root,[*rows,*legacy]
                root.check()
            except BaseException as error:failure=error
    except BaseException as error:
        if failure is None:failure=error
        else:stage_facts.carry(error, failure, stage_facts.observed(failure))
    if failure is not None:raise failure


def list_backups():
    return _list_backups()


def _list_backups(*, _authority=None):
    """Observe every bounded local state; retired/incomplete rows stay visible."""
    if _authority is not None:
        from .backup_authority import require
        require(_authority, 'catalog')
    with aio.operation('backup',selectors={},capture_key=False):
        with _directories(_authority=_authority, _stage='catalog') as (_memory,directory):
            if directory is None:return []
            with _catalog(directory) as (_root,rows):
                return sorted(rows,key=lambda row:(-(row.get('mtime') or 0),row.get('name',row.get('operation_id',''))))


def retire_backups(keep=None):
    return _retire_backups(keep)


def _retire_backups(keep=None, *, _authority=None):
    """Withdraw oldest completed managed pairs under one namespace lease.

    The first failed retirement stops the batch. Completed IDs are attached to
    the public failure so callers can report partial progress without claiming
    deletion. An unfinished retirement remains visible for explicit same-ID
    inspection/settlement and is never silently counted as complete. The
    batch_complete field describes only newly selected IDs in this call. Old
    visible retirements are reported separately; their barriers are not
    reconfirmed by a later no-op batch.
    """
    if _authority is not None:
        from .backup_authority import require
        require(_authority, 'retire')
        if keep is not None:
            raise ValueError('a captured retention policy cannot be replaced at stage entry')
        selected = _authority.keep
    else:
        selected=config.backup_keep() if keep is None else keep
    if type(selected) is not int or not 0<=selected<=publication.MAX_OPERATIONS:
        raise ValueError('backup keep must be an integer within the retained history bound')
    completed=[];failure=None;root=None
    try:
        with aio.operation('backup',selectors={},capture_key=False):
            with _directories(_authority=_authority, _stage='retire') as (memory,directory):
                if directory is None:
                    return {'retired_count':0,'retired_operations':[],'pending_retirement_ids':[],'observed_retired_ids':[],
                        'files_deleted':0,'bytes_reclaimed':0,'history_capacity_reclaimed':0,'batch_complete':True}
                with _catalog(directory) as (root,rows):
                    pending=[row['operation_id'] for row in rows if row.get('retirement_status')=='intent_visible']
                    visible=[row['operation_id'] for row in rows if row.get('retirement_status')=='completion_visible']
                    eligible=sorted((row for row in rows if row['selection_candidate']),
                        key=lambda row:(-row['mtime'],row['operation_id']))
                    # ALL eligible pairs have passed catalog preflight before
                    # the first retained retirement intent can be published.
                    for row in eligible[selected:]:
                        if _authority is not None: require(_authority, 'retire')
                        memory.check();directory.check();root.check()
                        try:
                            receipt=publication._retire_held(root,row['operation_id'])
                        except Exception as error:
                            # The source publication lease has no default
                            # operation ID. Capture this failed batch member
                            # before any surrounding cleanup can obscure it.
                            public=publication.PublicationError(row['operation_id'],phase=root.phase)
                            stage_facts.carry(error, public)
                            publication._raise_public_error(public)
                        except BaseException as error:
                            stage_facts.retain_id(error,row['operation_id'])
                            raise
                        if receipt['retirement_complete'] is not True or receipt['files_deleted']!=0:
                            raise ValueError('backup retirement receipt is invalid')
                        completed.append(row['operation_id'])
                    return {'retired_count':len(completed),'retired_operations':completed,
                        'pending_retirement_ids':pending,'observed_retired_ids':visible,
                        'observed_retirement_barriers_reconfirmed':False,'files_deleted':0,'bytes_reclaimed':0,
                        'history_capacity_reclaimed':0,'batch_complete':True}
    except BaseException as error:failure=error
    if failure is not None:
        stage_facts.carry(failure,failure,() if root is None else root.confirmed_stage_facts)
        completed=list(dict.fromkeys([*completed, *(item.operation_id for item in stage_facts.observed(failure)
            if item.kind=='retirement')]))
        if isinstance(failure,Exception):
            failed_id=None
            if type(failure) is publication.PublicationError:
                candidate=failure.operation_id
                if type(candidate) is str and publication._ID.fullmatch(candidate):failed_id=candidate
            public=publication.PublicationError(failed_id)
            public.completed_retirement_ids=tuple(completed)
            public.files_deleted=0
            stage_facts.carry(failure,public)
            failure=None
            publication._raise_public_error(public)
        failure.completed_retirement_ids=tuple(completed)
        raise failure
