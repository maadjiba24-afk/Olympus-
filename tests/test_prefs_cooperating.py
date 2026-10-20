"""Owned overlapping preference caller controls; no recovery/remember acceptance."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from olympus import actions, config, operator, prefs, prefs_native as native, profile, vault

OWNER = 'owned-preference-caller'


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'owned-preference-callers'
    root.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setattr(native, '_GATE', threading.RLock())
    monkeypatch.setattr(native, '_LOCAL', threading.local())
    monkeypatch.setattr(native, '_IN_FLIGHT', False)
    monkeypatch.setattr(native, '_POISONED', False)
    monkeypatch.setattr(prefs, '_TRANSFORM', threading.local())
    monkeypatch.setattr(vault, 'delete_many', lambda *args: None)
    return root


def _overlap(monkeypatch, first, second):
    """Both calls reach the mutation boundary before either can read for RMW.

    A caller that still reads before that boundary retains a stale snapshot:
    the delayed second call would overwrite the first call's completed edit.
    """
    real_mutate = prefs.mutate
    first_arrived, second_arrived, first_done = [threading.Event() for _ in range(3)]
    errors = []
    admission = []
    transforms = []
    def barrier(user, transform, **kwargs):
        name = threading.current_thread().name
        admission.append(name)
        def checked(data):
            assert getattr(native._LOCAL, 'current', None) is not None
            assert prefs._TRANSFORM.active
            transforms.append(name)
            return transform(data)
        if name == 'owned-first-editor':
            first_arrived.set()
            assert second_arrived.wait(4)
            try:
                return real_mutate(user, checked, **kwargs)
            finally:
                first_done.set()
        assert name == 'owned-second-editor'
        second_arrived.set()
        assert first_arrived.wait(4) and first_done.wait(4)
        return real_mutate(user, checked, **kwargs)
    def run(callback):
        try:
            callback()
        except BaseException as error:
            errors.append(type(error).__name__)
    threads = [threading.Thread(target=run, args=(callback,), name=name, daemon=True)
        for callback, name in ((first, 'owned-first-editor'), (second, 'owned-second-editor'))]
    started = []
    with monkeypatch.context() as patch:
        patch.setattr(prefs, 'mutate', barrier)
        try:
            for thread in threads:
                thread.start(); started.append(thread)
            for thread in started:
                thread.join(timeout=12)
            assert not any(thread.is_alive() for thread in started)
            assert not errors
            assert sorted(admission) == ['owned-first-editor', 'owned-second-editor']
            assert transforms == ['owned-first-editor', 'owned-second-editor']
        finally:
            first_arrived.set(); second_arrived.set(); first_done.set()
            for thread in started:
                thread.join(timeout=6)
            assert not any(thread.is_alive() for thread in started)


@pytest.mark.parametrize('kind', ['scopes', 'limits', 'sites', 'forget-site', 'enabled', 'advanced', 'profile-about', 'profile-facts'])
def test_overlapping_callers_keep_completed_other_edit(owned, monkeypatch, kind):
    if kind == 'scopes':
        prefs.set(OWNER, 'scopes', ['remove'])
        first = lambda: actions.revoke_scope(OWNER, 'remove')
        second = lambda: actions.grant_scope(OWNER, 'add')
        expected = {'scopes': ['add']}
    elif kind == 'limits':
        prefs.set(OWNER, 'action_limits', {'first': 9})
        first = lambda: actions.set_limit(OWNER, 'first', 1)
        second = lambda: actions.set_limit(OWNER, 'second', 2)
        expected = {'action_limits': {'first': 1, 'second': 2}}
    elif kind in ('sites', 'forget-site', 'enabled', 'advanced'):
        monkeypatch.setattr(operator.time, 'time', lambda: 123)
        operator.authorize_site(OWNER, 'remove.example')
        if kind == 'sites':
            first = lambda: operator.authorize_site(OWNER, 'first.example')
            second = lambda: operator.authorize_site(OWNER, 'second.example')
            expected_sites = ['remove.example', 'first.example', 'second.example']
        else:
            first = lambda: operator.forget_site(OWNER, 'remove.example')
            if kind == 'forget-site':
                second = lambda: operator.authorize_site(OWNER, 'second.example')
                expected_sites = ['second.example']
            elif kind == 'enabled':
                second = lambda: operator.set_enabled(OWNER, False)
                expected_sites = []
            else:
                second = lambda: operator.set_advanced(OWNER, True)
                expected_sites = []
        settings = {'sites': {name: {'login': 'manual', 'created': 123} for name in expected_sites}, 'enabled': kind != 'enabled'}
        if kind == 'advanced': settings['advanced'] = True
        expected = {'operator': settings}
    else:
        profile.set_about(OWNER, 'old note')
        profile.set_fact(OWNER, 'role', 'old role')
        if kind == 'profile-about':
            first = lambda: profile.set_fact(OWNER, 'role', 'new role')
            second = lambda: profile.set_about(OWNER, 'new note')
            expected = {'profile': {'about': 'new note', 'facts': {'role': 'new role'}}}
        else:
            first = lambda: profile.set_fact(OWNER, 'role', '')
            second = lambda: profile.set_fact(OWNER, 'timezone', 'UTC')
            expected = {'profile': {'about': 'old note', 'facts': {'timezone': 'UTC'}}}
    prefs.set(OWNER, 'unrelated', {'retained': [1, 2]})
    _overlap(monkeypatch, first, second)
    assert prefs.load(OWNER) == {**expected, 'unrelated': {'retained': [1, 2]}}
    assert not native._IN_FLIGHT and not native._POISONED


_CHILD = '''import json,socket,sys,time
from pathlib import Path
def denied(*args,**kwargs):raise AssertionError('owned caller child network disabled')
socket.socket.connect=denied;socket.socket.connect_ex=denied
socket.socket.bind=denied;socket.create_connection=denied
sys.path.insert(0,sys.argv[1])
from olympus import actions,config,prefs,prefs_native as native
config.MEMORY_DIR=Path(sys.argv[2])
signals=Path(sys.argv[3]);owner=sys.argv[4];kind=sys.argv[5]
real_mutate=prefs.mutate;real_probe=native.storage._try_lock
seen={'attempts':0,'denied':False,'acquired':False}
def mutate(*args,**kwargs):
    seen['attempts']+=1
    with (signals/'ready').open('xb') as stream:stream.write(b'owned-ready')
    assert sys.stdin.buffer.read(1)==b'g'
    return real_mutate(*args,**kwargs)
def probe(fd):
    result=real_probe(fd)
    if not result and not seen['denied']:
        with (signals/'denied').open('xb') as stream:stream.write(b'owned-denied')
    seen['acquired' if result else 'denied']=True
    return result
prefs.mutate=mutate;native.storage._try_lock=probe
if kind=='grant':actions.grant_scope(owner,'add')
else:actions.revoke_scope(owner,'remove')
assert not native._IN_FLIGHT and not native._POISONED
print(json.dumps(seen))
'''


def _await_owned_file(path, child):
    deadline = time.monotonic() + 4
    while not path.exists():
        assert child.poll() is None
        if time.monotonic() >= deadline:
            raise TimeoutError('owned caller child did not reach its expected boundary')
        time.sleep(0.01)


@pytest.mark.parametrize('child_kind', ['grant', 'revoke'])
def test_fresh_process_callers_contend_on_native_mutex_and_keep_revocation(owned, child_kind):
    prefs.set(OWNER, 'scopes', ['remove'])
    signals = owned / 'owned-child-signals'
    signals.mkdir()
    child = None
    primary = None
    cleanup = []
    try:
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', _CHILD,
            str(Path(native.__file__).resolve().parents[1]), str(owned), str(signals), OWNER, child_kind],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _await_owned_file(signals / 'ready', child)
        with native.transaction(owned):
            if child_kind == 'grant': actions.revoke_scope(OWNER, 'remove')
            else: actions.grant_scope(OWNER, 'add')
            child.stdin.write(b'g'); child.stdin.flush()
            _await_owned_file(signals / 'denied', child)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0 and stderr == b'' and len(stdout) < 200
        assert json.loads(stdout) == {'attempts': 1, 'denied': True, 'acquired': True}
        assert prefs.get(OWNER, 'scopes') == ['add']
    except BaseException as error:
        primary = error
        raise
    finally:
        if child is not None:
            try:
                if child.poll() is None:
                    child.kill()  # Retained handle / direct unreaped child only.
                    child.wait(timeout=3)
            except BaseException as error:
                cleanup.append(type(error).__name__)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    try: stream.close()
                    except BaseException as error: cleanup.append(type(error).__name__)
        if cleanup:
            if primary is not None:
                BaseException.__dict__['__dict__'].__get__(primary)['_owned_child_cleanup_unconfirmed'] = True
            else:
                raise AssertionError('owned cooperating preference child cleanup is unconfirmed')


def test_daily_limit_uses_one_current_and_quarantine_view(owned, monkeypatch):
    prefs.set(OWNER, 'action_limits', {'owned-action': 2})
    original = prefs.capture
    captures = []
    def capture(user):
        observed = original(user)
        captures.append(observed)
        legacy = prefs._io(owned.joinpath(*prefs._relative_paths(user)[1]))
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b'{"action_limits":{"owned-action":1}}')
        return observed
    with monkeypatch.context() as patch:
        patch.setattr(prefs, 'capture', capture)
        assert actions.daily_limit(OWNER, 'owned-action') == 2
    assert len(captures) == 1
    assert actions.daily_limit(OWNER, 'owned-action') == 1


@pytest.mark.parametrize('raw', [b'{', b'[]', b'{"action_limits":1,"action_limits":2}'])
def test_daily_limit_clamps_corrupt_current(owned, raw):
    path = prefs._io(owned.joinpath(*prefs._relative_paths(OWNER)[0]))
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    assert actions.daily_limit(OWNER, 'unregistered-owned-action') == 1
    assert path.read_bytes() == raw


@pytest.mark.parametrize('api', ['get', 'quarantine'])
def test_captured_policy_view_cannot_cross_owner_or_executor(owned, api):
    observed = prefs.capture(OWNER)
    call = (lambda owner: prefs.get(owner, 'scopes', captured=observed)) if api == 'get' else (lambda owner: prefs.is_quarantined(owner, captured=observed))
    with pytest.raises(prefs.PreferenceConflict): call('different-owned-caller')
    errors = []
    def other_thread():
        try: call(OWNER)
        except native.PreferenceStorageError: errors.append(True)
    thread = threading.Thread(target=other_thread, daemon=True)
    thread.start(); thread.join(timeout=3)
    assert not thread.is_alive() and errors == [True]


@pytest.mark.parametrize('kind', ['scopes', 'operator', 'limits'])
def test_quarantined_caller_edits_do_not_merge_hidden_security_grants(owned, kind):
    prefs.set(OWNER, 'scopes', ['hidden'])
    prefs.set(OWNER, 'operator', {'enabled': True, 'sites': {'hidden.example': {'login': 'remember'}}})
    prefs.set(OWNER, 'action_limits', {'hidden-action': 999})
    legacy = prefs._io(owned.joinpath(*prefs._relative_paths(OWNER)[1]))
    legacy.parent.mkdir(parents=True, exist_ok=True); legacy.write_bytes(b'{}')
    if kind == 'scopes':
        actions.grant_scope(OWNER, 'new')
        assert prefs.load(OWNER)['scopes'] == ['new']
        assert prefs.get(OWNER, 'scopes') == []
    elif kind == 'operator':
        operator.authorize_site(OWNER, 'new.example')
        assert set(prefs.load(OWNER)['operator']['sites']) == {'new.example'}
        assert operator.sites(OWNER) == {}
    else:
        actions.set_limit(OWNER, 'new-action', 3)
        assert prefs.load(OWNER)['action_limits'] == {'new-action': 3}
        assert actions.daily_limit(OWNER, 'new-action') == 1
    assert legacy.read_bytes() == b'{}'


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_unconfirmed_revocation_does_not_start_vault_cleanup(owned, monkeypatch, fault_type):
    operator.authorize_site(OWNER, 'owned.example')
    fault = fault_type('owned-private-publication-fault')
    original = native.Transaction.publish
    def publish_then_fail(tx, *args):
        original(tx, *args)
        raise fault
    monkeypatch.setattr(native.Transaction, 'publish', publish_then_fail)
    monkeypatch.setattr(vault, 'delete_many', lambda *args: pytest.fail('unconfirmed revocation started vault cleanup'))
    expected = prefs.PreferenceOperationError if fault_type is OSError else fault_type
    with pytest.raises(expected) as raised: operator.forget_site(OWNER, 'owned.example')
    if fault_type is not OSError: assert raised.value is fault
    assert 'owned.example' not in operator.sites(OWNER)


def test_vault_cleanup_runs_after_revocation_and_outside_preference_lock(owned, monkeypatch):
    operator.authorize_site(OWNER, 'owned.example')
    observed = []
    def cleanup(user, names):
        assert not native._IN_FLIGHT and getattr(native._LOCAL, 'current', None) is None
        assert not operator.sites(user)
        observed.append(names)
    monkeypatch.setattr(vault, 'delete_many', cleanup)
    assert operator.forget_site(OWNER, 'owned.example')
    assert observed == [['site:owned.example', 'cookies:owned.example']]
