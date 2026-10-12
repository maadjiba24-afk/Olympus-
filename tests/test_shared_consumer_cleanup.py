"""Owned backup/browser cleanup faults, retained primaries and uncertain absence."""
from contextlib import contextmanager
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from olympus import backup, backup_authority as authority, backup_delivery as delivery
from olympus import backup_evidence as evidence, backup_management as management
from olympus import backup_publication as publication, backup_restore as recovery
from olympus import backup_snapshot as snapshot, browser_credential_authority as browser_authority
from olympus import browser_credential_profiles as profiles, config, native_cleanup, vault_storage as storage

OP = '6' * 32


@pytest.fixture
def descriptors(tmp_path):
    pending = set()
    actual_close = os.close
    counter = [0]
    def acquire():
        counter[0] += 1
        fd = os.open(tmp_path / ('descriptor-' + str(counter[0])), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        pending.add(fd)
        return fd
    def release(fd):
        assert fd in pending, 'duplicate or foreign descriptor close'
        pending.remove(fd)
        actual_close(fd)
    yield SimpleNamespace(acquire=acquire, release=release, pending=pending)
    leaked = tuple(pending)
    native_cleanup.drain([lambda fd=fd: release(fd) for fd in leaked])
    assert not leaked, 'test or product required independent descriptor cleanup'


@pytest.fixture(autouse=True)
def directory_guards(monkeypatch):
    tracked=[]
    initialize, close = storage._Directory.__init__, storage._Directory.close
    def initialized(directory, *args, **kwargs):
        initialize(directory, *args, **kwargs)
        tracked.append(directory)
    monkeypatch.setattr(storage._Directory, '__init__', initialized)
    yield
    pending=[directory for directory in tracked if not directory._closed]
    native_cleanup.drain([lambda directory=directory: close(directory) for directory in reversed(pending)])
    assert not pending, 'test or product required independent directory cleanup'


class Resource:
    def __init__(self, pool, failure=None):
        self.pool, self.fd, self.failure = pool, pool.acquire(), failure
        self.calls = 0
    def close(self):
        self.calls += 1
        fd, self.fd = self.fd, None
        assert fd is not None, 'resource closed twice'
        self.pool.release(fd)
        if self.failure is not None: raise self.failure
    def check(self):
        assert self.fd is not None


def root_owner(module, pool, *, held=False, child_failure=None):
    cls = module._Store if module is evidence else module._Root
    owner = cls.__new__(cls)
    owner.pid, owner.thread = os.getpid(), threading.get_ident()
    owner.closed, owner.held, owner._cleanup_unconfirmed = False, held, False
    owner.fd = pool.acquire()
    children = [Resource(pool, child_failure), Resource(pool)] if module is not evidence else [Resource(pool, child_failure)]
    owner.phase, owner.confirmed_stage_facts = 'preparation', ()
    owner.control = children[0]
    if module is not evidence:
        owner.directories, owner.operations = children[:], []
    return owner, children


@pytest.mark.parametrize('module',[publication,recovery,evidence],ids=['publication','restore','evidence'])
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_unlock_primary_survives_close_failure_and_reused_descriptor(descriptors,monkeypatch,module,fault):
    owner, children = root_owner(module,descriptors,held=True,child_failure=OSError('owned child close fault'))
    fd=owner.fd;primary=fault('owned private unlock fault');replacement=[];unlocks=[];closes=[]
    def unlock(value):
        unlocks.append(value)
        raise primary
    actual_close=os.close
    def close(value):
        if value != fd: return actual_close(value)
        closes.append(value)
        descriptors.release(value)
        replacement.append(descriptors.acquire())
        assert replacement[-1] == value
        raise OSError('owned close return lost after actual close')
    with monkeypatch.context() as patch:
        patch.setattr(module,'_unlock',unlock)
        patch.setattr(os,'close',close)
        with pytest.raises(fault) as caught: owner.close()
        assert caught.value is primary and native_cleanup.unknown(primary)
        assert owner.closed and owner.fd is None and not owner.held
        assert owner._cleanup_unconfirmed and all(child.calls==1 for child in children)
        for action in (owner.check,owner.close):
            with pytest.raises(ValueError) as refused:action()
            assert native_cleanup.unknown(refused.value)
        assert unlocks==[fd] and closes==[fd]
        os.fstat(replacement[0])
    descriptors.release(replacement[0])


@pytest.mark.parametrize('module',[publication,recovery,evidence],ids=['publication','restore','evidence'])
def test_clean_drain_cannot_clear_prior_owner_uncertainty(descriptors,monkeypatch,module):
    owner,children=root_owner(module,descriptors)
    owner._cleanup_unconfirmed=True
    fd=owner.fd;actual_close=os.close
    with monkeypatch.context() as patch:
        patch.setattr(os,'close',lambda value:descriptors.release(value) if value==fd else actual_close(value))
        with pytest.raises(ValueError) as caught:owner.close()
        assert native_cleanup.unknown(caught.value)
        assert all(child.calls==1 for child in children)
        with pytest.raises(ValueError) as again:owner.close()
        assert native_cleanup.unknown(again.value)
    assert not descriptors.pending


@pytest.mark.parametrize('module',[publication,recovery,evidence],ids=['publication','restore','evidence'])
@pytest.mark.parametrize('fault,with_primary',[(OSError,False),(OSError,True),(KeyboardInterrupt,True),(SystemExit,True)],
                         ids=['cleanup-only','ordinary-body','keyboard-body','system-body'])
def test_public_context_preserves_primary_or_redacts_cleanup(descriptors,tmp_path,monkeypatch,module,fault,with_primary):
    secondary=OSError('owned private resource close sentinel')
    owner,children=root_owner(module,descriptors,child_failure=secondary)
    fd=owner.fd;actual_close=os.close;primary=fault('owned private body sentinel') if with_primary else None
    with monkeypatch.context() as patch:
        patch.setattr(os,'close',lambda value:descriptors.release(value) if value==fd else actual_close(value))
        if module is publication:
            patch.setattr(module,'_Root',lambda *a,**k:owner)
            scope=module.locked(tmp_path,operation_id=OP);public=module.PublicationError
        elif module is recovery:
            patch.setattr(module,'_Root',lambda *a,**k:owner)
            scope=module._authority(tmp_path/'offline',tmp_path/'live',(),OP,initialize=False);public=module.RestoreError
        else:
            patch.setattr(module,'_Store',lambda *a,**k:owner)
            scope=module._store(None,initialize=False);public=OSError
        expected=fault if with_primary and (module is evidence or fault is not OSError) else public
        with pytest.raises(expected) as caught:
            with scope:
                if primary is not None:raise primary
        assert native_cleanup.unknown(caught.value)
        if primary is not None and (module is evidence or fault is not OSError):assert caught.value is primary
        elif module is not evidence:
            assert 'owned private' not in str(caught.value)
            assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert owner.closed and all(child.calls==1 for child in children)
    assert not descriptors.pending


@pytest.mark.parametrize('fault,with_primary',[(OSError,False),(OSError,True),(KeyboardInterrupt,True),(SystemExit,True)],
                         ids=['cleanup-only','ordinary-body','keyboard-body','system-body'])
def test_scoped_operation_detaches_before_failed_close(descriptors,fault,with_primary):
    primary=fault('owned body failure') if with_primary else None
    close_error=OSError('owned child close failure')
    child=Resource(descriptors,close_error)
    root=SimpleNamespace(directories=[],operations=[],_cleanup_unconfirmed=False,check=lambda:None)
    with pytest.raises(fault if with_primary else OSError) as caught:
        with recovery._held_directory(root,child):
            assert root.directories==[child] and root.operations==[child]
            if primary is not None:raise primary
    assert caught.value is (primary if with_primary else close_error)
    assert native_cleanup.unknown(caught.value) and root._cleanup_unconfirmed
    assert root.directories==root.operations==[] and child.calls==1 and not descriptors.pending


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('module',[snapshot,publication,recovery],ids=['snapshot','publication','restore'])
def test_enumerator_scope_preserves_actual_body_failure(descriptors,module,fault):
    primary=fault('owned enumeration body failure')
    resource=Resource(descriptors,OSError('owned enumeration close failure'))
    with pytest.raises(fault) as caught:
        with module.closing(resource):raise primary
    assert caught.value is primary and native_cleanup.unknown(primary)
    assert resource.calls==1 and not descriptors.pending


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_workflow_capture_drains_both_directories_preserving_body(tmp_path,monkeypatch,fault):
    memory=tmp_path/'memory';memory.mkdir();monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config,'MEMORY_DIR',memory)
    primary=fault('owned workflow body failure');seen=[];attempted=[]
    original=storage._Directory.close
    def close(directory):
        if directory in seen:attempted.append(directory)
        original(directory)
        if directory in seen:raise OSError('owned workflow directory close failure')
    with monkeypatch.context() as patch:
        patch.setattr(storage._Directory,'close',close)
        with pytest.raises(fault) as caught:
            with authority.capture('inspect') as captured:
                seen.extend([captured.memory,captured.working])
                raise primary
        assert caught.value is primary and native_cleanup.unknown(primary)
        assert len(attempted)==2 and set(map(id,attempted))==set(map(id,seen))
        assert all(directory._closed for directory in seen)
        assert captured.memory is captured.working is captured._backups is None
        with pytest.raises(authority.AuthorityError) as refused:captured.check()
        assert native_cleanup.unknown(refused.value)
        with pytest.raises(authority.AuthorityError):captured.close()
        assert len(attempted)==2


@pytest.mark.parametrize('endpoint',['delivery','browser-record','browser-profiles','restore-read','restore-capacity'])
@pytest.mark.parametrize('uncertain',[False,True])
def test_optional_absence_requires_confirmed_cleanup(tmp_path,monkeypatch,endpoint,uncertain):
    error=FileNotFoundError('owned private missing sentinel')
    if uncertain:native_cleanup.mark(error)
    calls=[]
    def absent(*a,**k):calls.append('open');raise error
    directory=SimpleNamespace(path=tmp_path,check=lambda:None,_case_spelling=lambda *a:None,read_with_identity=absent)
    if endpoint=='delivery':
        action=lambda:delivery._optional(directory,'owned.json');clean=None
    elif endpoint=='browser-record':
        captured=browser_authority.Authority.__new__(browser_authority.Authority)
        captured.directory=directory;captured._check_lock=lambda:None
        action=captured._read;clean=None
    elif endpoint=='browser-profiles':
        captured=browser_authority.Authority.__new__(browser_authority.Authority)
        captured.directory=directory;captured.check=lambda:None
        action=lambda:profiles._load(captured);clean=[]
    elif endpoint=='restore-read':
        captured=recovery._Root.__new__(recovery._Root)
        captured._cleanup_unconfirmed=False;captured.root=directory;captured.check=lambda:None
        monkeypatch.setattr(recovery,'_Directory',absent)
        action=lambda:captured.read_target('child/owned.json',100);clean=None
    else:
        captured=SimpleNamespace(path=tmp_path,check=lambda:None)
        monkeypatch.setattr(recovery,'_Directory',absent)
        action=lambda:recovery._capacity(captured,[]);clean=None
    if uncertain:
        expected=(browser_authority.browserlease.OwnershipRefused if endpoint=='browser-record'
                  else profiles.records.BrowserCredentialError if endpoint=='browser-profiles' else FileNotFoundError)
        with pytest.raises(expected) as caught:action()
        assert native_cleanup.unknown(caught.value)
        if endpoint.startswith('browser-'):
            assert captured._cleanup_unconfirmed
            assert 'owned private' not in str(caught.value)
        else:
            assert caught.value is error
            if endpoint=='restore-read':assert captured._cleanup_unconfirmed
    else:assert action()==clean
    assert calls==['open']


@pytest.mark.parametrize('uncertain',[False,True])
def test_claim_never_reobserves_an_uncertain_exclusive_write(monkeypatch,uncertain):
    assert browser_authority.browserlease.require_owner('cli')=='cli'
    error=FileExistsError('owned private write sentinel')
    if uncertain:native_cleanup.mark(error)
    calls=[]
    captured=browser_authority.Authority.__new__(browser_authority.Authority)
    @contextmanager
    def locked():yield
    def read():
        calls.append('read')
        return None if calls.count('read')==1 else {'owner':'cli','lease_id':'owned'}
    def write(*a):calls.append('write');raise error
    captured.lock=locked;captured._check_lock=lambda:None;captured._read=read
    captured._confirm=lambda record:record;captured.directory=SimpleNamespace(write_new=write)
    if uncertain:
        with pytest.raises(browser_authority.browserlease.OwnershipRefused) as caught:captured.claim('cli','owned')
        assert native_cleanup.unknown(caught.value) and captured._cleanup_unconfirmed
        assert calls==['read','write'] and 'owned private' not in str(caught.value)
    else:
        assert captured.claim('cli','owned')['owner']=='cli'
        assert calls==['read','write','read']


"""Additional acknowledgment, descriptor-transfer and marker controls."""

@pytest.mark.parametrize('endpoint',['archive-input','catalog','delivery'])
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_secondary_cleanup_preserves_primary_id_and_both_acknowledgments(tmp_path,monkeypatch,endpoint,fault):
    from olympus import backup_facts as facts
    primary=fault('owned private primary');primary.operation_id='7'*32
    secondary=OSError('owned private secondary');native_cleanup.mark(secondary)
    before=facts.ConfirmedStage('restore','7'*32,'1'*64,None,'2'*64)
    after=facts.ConfirmedStage('publication',OP,'3'*64,'4'*64,'5'*64)
    facts.carry(primary,primary,(before,));facts.carry(secondary,secondary,(after,))
    root=SimpleNamespace(check=lambda:None,root=object(),control=object())
    @contextmanager
    def locked(*a,**k):
        yield root
        raise secondary
    @contextmanager
    def operation(*a,**k):yield object()
    monkeypatch.setattr(publication,'locked',locked)
    if endpoint=='archive-input':
        monkeypatch.setattr(publication,'read_completed',lambda *a:(b'owned',None,
            {'operation_id':OP,'plan_sha256':'5'*64,'archive_sha256':'3'*64,'signature_sha256':'4'*64}))
        scope=backup._read_pair(tmp_path/publication.archive_name(OP,False),external=False)
    elif endpoint=='catalog':
        monkeypatch.setattr(publication,'_names',lambda *a:[publication.CONTROL])
        monkeypatch.setattr(management,'_legacy',lambda *a:[])
        monkeypatch.setattr(publication,'_catalog_held',lambda *a:[])
        scope=management._catalog(object())
    else:
        monkeypatch.setattr(publication,'_held_operation',operation)
        scope=delivery._publication_authority(object(),OP)
    with pytest.raises(fault) as caught:
        with scope:raise primary
    assert caught.value is primary and caught.value.operation_id=='7'*32
    assert native_cleanup.unknown(primary)
    assert set(facts.observed(primary))=={before,after}


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('outcome',['transferred','closed-and-reused'])
def test_browser_lost_file_object_transfer_never_guesses_raw_close(tmp_path,monkeypatch,fault,outcome):
    monkeypatch.setattr(config,'MEMORY_DIR',tmp_path)
    captured=browser_authority.Authority()
    actual_fdopen,actual_close=os.fdopen,os.close
    primary=fault('owned private wrapping return lost');files=[];reused=[];target=[];attempted=[]
    def wrap(fd,*args,**kwargs):
        target.append(fd)
        stream=actual_fdopen(fd,*args,**kwargs)
        if outcome=='transferred':files.append(stream)
        else:
            stream.close()
            reused.append(os.open(tmp_path/'replacement',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600))
            assert reused[-1]==fd
        raise primary
    def close(fd):
        if fd in target:attempted.append(fd)
        actual_close(fd)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os,'fdopen',wrap);patch.setattr(os,'close',close)
            expected=browser_authority.browserlease.OwnershipRefused if fault is OSError else fault
            with pytest.raises(expected) as caught:captured._open_lock()
            if fault is not OSError:assert caught.value is primary
            assert browser_authority._cleanup_unknown(caught.value) and captured._cleanup_unconfirmed
            assert attempted==[] and len(target)==1
            os.fstat(target[0])
            with pytest.raises(browser_authority.browserlease.OwnershipRefused) as refused:captured.check()
            assert browser_authority._cleanup_unknown(refused.value)
            with pytest.raises(browser_authority.browserlease.OwnershipRefused):captured._open_lock()
            assert len(target)==1 and attempted==[]
            with pytest.raises(browser_authority.browserlease.OwnershipRefused) as closed:captured.close()
            assert browser_authority._cleanup_unknown(closed.value)
            assert captured._closed and captured.directory is captured.locks is None
            with pytest.raises(browser_authority.browserlease.OwnershipRefused):captured.close()
            os.fstat(target[0]);assert attempted==[]
    finally:
        actions=[stream.close for stream in files]+[lambda fd=fd:actual_close(fd) for fd in reused]
        native_cleanup.drain(actions)
        if not captured._closed:captured.close()


@pytest.mark.parametrize('module',[publication,recovery,evidence],ids=['publication','restore','evidence'])
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_marker_metadata_primary_survives_close_and_poison_is_local(descriptors,monkeypatch,module,fault):
    cls=module._Store if module is evidence else module._Root
    owner=cls.__new__(cls);owner._cleanup_unconfirmed=False
    owner.marker=b'owned marker';owner.marker_identity=[1,2];owner.check=lambda:None
    fd=descriptors.acquire();owner.control=SimpleNamespace(_open=lambda *a:fd)
    primary=fault('owned private marker metadata failure');closes=[];later=[]
    def information(value):assert value==fd;raise primary
    actual_close=os.close
    def close(value):
        if value!=fd:return actual_close(value)
        closes.append(value);descriptors.release(value)
        raise OSError('owned marker close failed after release')
    with monkeypatch.context() as patch:
        patch.setattr(module,'_file_information',information);patch.setattr(os,'close',close)
        if module is evidence:
            patch.setattr(module,'_payload',lambda value:value)
            owner.rows=lambda:({},0)
            action=lambda:owner.put({'kind':'backup','operation_id':OP})
        else:action=lambda:owner.confirm_witnesses({})
        with pytest.raises(fault) as caught:action()
        assert caught.value is primary and native_cleanup.unknown(primary)
        assert owner._cleanup_unconfirmed and closes==[fd] and not descriptors.pending
        with pytest.raises(ValueError) as refused:cls.check(owner)
        assert native_cleanup.unknown(refused.value)
    unrelated=SimpleNamespace(_cleanup_unconfirmed=False)
    native_cleanup.drain_owned(unrelated,[lambda:later.append('independent')])
    assert later==['independent'] and not unrelated._cleanup_unconfirmed


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_browser_lock_body_and_unlock_failures_poison_only_captured_owner(tmp_path,monkeypatch,fault):
    monkeypatch.setattr(config,'MEMORY_DIR',tmp_path)
    captured=browser_authority.Authority();primary=fault('owned private browser body')
    original=browser_authority.browserlease._release_file_lock;calls=[]
    def release(stream):
        calls.append(stream)
        original(stream)
        raise OSError('owned unlock acknowledgment lost')
    with monkeypatch.context() as patch:
        patch.setattr(browser_authority.browserlease,'_release_file_lock',release)
        with pytest.raises(fault) as caught:
            with captured.lock():raise primary
        assert caught.value is primary and native_cleanup.unknown(primary)
        assert browser_authority._cleanup_unknown(primary) and captured._cleanup_unconfirmed
        assert len(calls)==1 and calls[0].closed
        assert browser_authority.browserlease._LOCAL.depth==0
        assert browser_authority.browserlease._LOCAL.capture is None
        with pytest.raises(browser_authority.browserlease.OwnershipRefused) as refused:captured.check()
        assert browser_authority._cleanup_unknown(refused.value)
        with pytest.raises(browser_authority.browserlease.OwnershipRefused):captured.close()
    assert captured._closed and captured.directory is captured.locks is None
    with browser_authority.capture() as independent:
        independent.check()
        with independent.lock():pass


"""Delegated profile and nested lock uncertainty must remain owner-local."""

@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('endpoint',['read','write'])
def test_profile_uncertainty_refuses_later_use_before_native_work(tmp_path,monkeypatch,fault,endpoint):
    import json
    monkeypatch.setattr(config,'MEMORY_DIR',tmp_path)
    captured=browser_authority.Authority()
    profile=profiles.browser.SiteProfile(domain='owned.test',login_url='https://owned.test/login',
        username_selector='#username',password_selector='#password',submit_selector='#submit',success_selector='#success')
    captured.directory.write_new('site_profiles.json',json.dumps([profile.to_dict()]).encode('utf-8'))
    primary=fault('owned private profile fault');native_cleanup.mark(primary);calls=[]
    directory,locks=captured.directory,captured.locks
    def failed(*a,**k):calls.append(endpoint);raise primary
    with monkeypatch.context() as patch:
        patch.setattr(directory,'read_with_identity' if endpoint=='read' else 'publish',failed)
        action=(lambda:profiles.get_profile('owned.test',captured)) if endpoint=='read' else (lambda:profiles.mark_outcome('owned.test',True,captured))
        expected=profiles.records.BrowserCredentialError if fault is OSError else fault
        with pytest.raises(expected) as caught:action()
        if fault is not OSError:assert caught.value is primary
        assert browser_authority._cleanup_unknown(caught.value) and captured._cleanup_unconfirmed
        assert calls==[endpoint]
        def forbidden(*a,**k):pytest.fail('uncertain authority reached native work')
        patch.setattr(directory,'check',forbidden);patch.setattr(locks,'check',forbidden)
        for later in (captured.check,lambda:profiles.get_profile('owned.test',captured),lambda:profiles.mark_outcome('owned.test',True,captured)):
            with pytest.raises((browser_authority.browserlease.OwnershipRefused,profiles.records.BrowserCredentialError)) as refused:later()
            assert browser_authority._cleanup_unknown(refused.value)
        with pytest.raises(browser_authority.browserlease.OwnershipRefused):captured.close()
        assert directory._closed and locks._closed
        assert captured.directory is captured.locks is None
    with browser_authority.capture() as independent:independent.check()


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('nested',[False,True],ids=['outer','nested'])
def test_flagged_lock_body_latches_owner_even_when_cleanup_succeeds(tmp_path,monkeypatch,fault,nested):
    monkeypatch.setattr(config,'MEMORY_DIR',tmp_path)
    captured=browser_authority.Authority();primary=fault('owned flagged body')
    native_cleanup.mark(primary);directory,locks=captured.directory,captured.locks
    with pytest.raises(fault) as caught:
        with captured.lock():
            if nested:
                with captured.lock():raise primary
            raise primary
    assert caught.value is primary and captured._cleanup_unconfirmed
    assert browser_authority.browserlease._LOCAL.depth==0 and browser_authority.browserlease._LOCAL.capture is None
    with monkeypatch.context() as patch:
        def forbidden(*a,**k):pytest.fail('uncertain authority reused native state')
        patch.setattr(directory,'check',forbidden);patch.setattr(locks,'check',forbidden)
        with pytest.raises(browser_authority.browserlease.OwnershipRefused) as refused:captured.check()
        assert browser_authority._cleanup_unknown(refused.value)
        with pytest.raises(browser_authority.browserlease.OwnershipRefused):captured.close()
    assert directory._closed and locks._closed and captured.directory is captured.locks is None
    with browser_authority.capture() as independent:independent.check()


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_scoped_flagged_primary_stays_sticky_after_clean_close(descriptors,fault):
    primary=fault('owned already uncertain acquisition')
    native_cleanup.mark(primary)
    child=Resource(descriptors)
    root=SimpleNamespace(directories=[],operations=[],_cleanup_unconfirmed=False,check=lambda:None)
    with pytest.raises(fault) as caught:
        with recovery._held_directory(root,child):raise primary
    assert caught.value is primary and native_cleanup.unknown(primary)
    assert root._cleanup_unconfirmed and root.directories==root.operations==[]
    assert child.calls==1 and not descriptors.pending


"""Already flagged bodies must poison exact backup owners after clean resource drain."""

@pytest.mark.parametrize('module',[publication,recovery,evidence],ids=['publication','restore','evidence'])
@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_flagged_body_keeps_backup_owner_uncertain_after_clean_drain(descriptors,tmp_path,monkeypatch,module,fault):
    owner,children=root_owner(module,descriptors)
    primary=fault('owned already uncertain delegated body');native_cleanup.mark(primary)
    fd=owner.fd;actual_close=os.close
    with monkeypatch.context() as patch:
        patch.setattr(os,'close',lambda value:descriptors.release(value) if value==fd else actual_close(value))
        if module is publication:
            patch.setattr(module,'_Root',lambda *a,**k:owner)
            scope=module.locked(tmp_path,operation_id=OP);public=module.PublicationError
        elif module is recovery:
            patch.setattr(module,'_Root',lambda *a,**k:owner)
            scope=module._authority(tmp_path/'offline',tmp_path/'live',(),OP,initialize=False);public=module.RestoreError
        else:
            patch.setattr(module,'_Store',lambda *a,**k:owner)
            scope=module._store(None,initialize=False);public=OSError
        with pytest.raises(fault if fault is not OSError or module is evidence else public) as caught:
            with scope:raise primary
        if fault is not OSError or module is evidence:assert caught.value is primary
        assert native_cleanup.unknown(primary) and native_cleanup.unknown(caught.value)
        assert owner.closed and owner.fd is None and all(child.calls==1 for child in children)
        assert owner._cleanup_unconfirmed and not descriptors.pending
        for action in (owner.check,owner.close):
            with pytest.raises(ValueError) as refused:action()
            assert native_cleanup.unknown(refused.value)
        assert all(child.calls==1 for child in children)


@pytest.mark.parametrize('fault',[OSError,KeyboardInterrupt,SystemExit])
def test_flagged_stage_body_refuses_reentry_before_native_work(tmp_path,monkeypatch,fault):
    memory=tmp_path/'memory';memory.mkdir();monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config,'MEMORY_DIR',memory)
    primary=fault('owned flagged stage body');native_cleanup.mark(primary)
    held=[];attempted=[];original_close=storage._Directory.close
    def close(directory):
        if directory in held:attempted.append(directory)
        original_close(directory)
    with pytest.raises(authority.AuthorityError) as outer:
        with authority.capture('inspect') as captured:
            held.extend([captured.memory,captured.working])
            with pytest.raises(fault) as caught:
                with captured.stage('catalog'):raise primary
            assert caught.value is primary and captured._cleanup_unconfirmed
            def forbidden(*a,**k):pytest.fail('uncertain workflow authority reached native work')
            with monkeypatch.context() as patch:
                for directory in held:patch.setattr(directory,'check',forbidden)
                with pytest.raises(authority.AuthorityError) as refused:
                    with captured.stage('catalog'):pytest.fail('uncertain stage reentered')
                assert native_cleanup.unknown(refused.value)
                with pytest.raises(authority.AuthorityError):captured.backups()
            monkeypatch.setattr(storage._Directory,'close',close)
    assert native_cleanup.unknown(outer.value)
    assert captured._closed and captured.memory is captured.working is None
    assert set(map(id,attempted))==set(map(id,held)) and len(attempted)==2
    assert all(directory._closed for directory in held)
    with pytest.raises(authority.AuthorityError):captured.close()
    assert len(attempted)==2
    with authority.capture('inspect') as independent:
        independent.check()
