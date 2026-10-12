"""Benign native uploader containment; no provider, network or backup delivery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from olympus import backup_process as process


def _fixture_identity(pid):
    with open('/proc/' + str(pid) + '/stat', 'rb') as stream:
        raw = stream.read(4097)
    assert len(raw) <= 4096
    fields = raw.rsplit(b')', 1)[1].split()
    return {'pid': pid, 'uid': os.stat('/proc/' + str(pid)).st_uid,
        'pgid': int(fields[2]), 'sid': int(fields[3]), 'state': fields[0].decode('ascii'),
        'start_ticks': int(fields[19])}


def _fixture_members(identity):
    # Only a separately owned session is a group cleanup target. Helpers that
    # inherited pytest's group are observed through their stable pidfd only.
    if identity['pgid'] != identity['pid'] or identity['sid'] != identity['pid']:
        try:
            current = _fixture_identity(identity['pid'])
        except (FileNotFoundError, ProcessLookupError):
            return []
        return [current] if current['start_ticks'] == identity['start_ticks'] else []
    members = []
    with os.scandir('/proc') as entries:
        count = 0
        for entry in entries:
            if not entry.name.isdigit():
                continue
            count += 1
            assert count <= 32768
            try:
                current = _fixture_identity(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            if current['pgid'] == identity['pgid']:
                assert current['sid'] == identity['sid'] and current['uid'] == identity['uid']
                members.append(current)
    return members


@pytest.fixture(autouse=True)
def retained_execution_facts(tmp_path, monkeypatch):
    """Retain public outcome facts, never command inputs or output."""
    actual = process.execute
    executions = []
    children = []
    if sys.platform.startswith('linux'):
        import signal
        actual_popen = subprocess.Popen
        # Capture syscalls before any test injects product observation faults.
        pidfd_open, waitid, killpg = os.pidfd_open, os.waitid, os.killpg
        send_signal, close_fd = signal.pidfd_send_signal, os.close
        def tracked_popen(*args, **kwargs):
            assert len(children) < 64
            child = actual_popen(*args, **kwargs)
            owned = {'process': child, 'pidfd': None, 'identity': None}
            children.append(owned)
            path = tmp_path / ('native-child-basic-%03d.json' % len(children))
            with path.open('x', encoding='utf-8') as stream:
                json.dump({'pid': child.pid, 'identity_acquisition_complete': False}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            owned['pidfd'] = pidfd_open(child.pid, 0)
            owned['identity'] = _fixture_identity(child.pid)
            path = tmp_path / ('native-child-%03d.json' % len(children))
            with path.open('x', encoding='utf-8') as stream:
                json.dump(owned['identity'], stream)
                stream.flush()
                os.fsync(stream.fileno())
            return child
        monkeypatch.setattr(subprocess, 'Popen', tracked_popen)
    def observed(captured, *, record_started, check_authority):
        identities = []
        def record(identity):
            identities.append(dict(identity))
            record_started(identity)
        try:
            result = actual(captured, record_started=record, check_authority=check_authority)
        except BaseException as error:
            executions.append({'identities': identities, 'failure_type': type(error).__name__,
                'outcome': dict(getattr(error, 'process_outcome', {}))})
            raise
        executions.append({'identities': identities, 'failure_type': None, 'outcome': result})
        return result
    monkeypatch.setattr(process, 'execute', observed)
    yield
    cleanup = []
    cleanup_failure = None
    for owned in children:
        item = {'pid': owned['process'].pid, 'identity': owned['identity'], 'independent_termination_needed': False,
                'cleanup_observed': False, 'failure_type': None}
        try:
            identity, descriptor = owned['identity'], owned['pidfd']
            deadline = time.monotonic() + 5
            if descriptor is None:
                # The returned Popen is strongly owned by this fixture, even
                # if the wrapper failed before returning it to the product.
                waitid(os.P_PID, owned['process'].pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                descriptor = pidfd_open(owned['process'].pid, 0)
                owned['pidfd'] = descriptor
            if identity is None:
                # Do not infer a session from a numeric PID. Stop only this
                # stable leader, observe/reap it, and retain descendant uncertainty.
                item['independent_termination_needed'] = True
                send_signal(descriptor, signal.SIGKILL, None, 0)
                while True:
                    exit_info = waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG)
                    if exit_info is not None:
                        owned['process'].returncode = (exit_info.si_status if exit_info.si_code == os.CLD_EXITED
                                                       else -exit_info.si_status)
                        item['leader_exit_observed_identity_unavailable'] = True
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError('fixture leader observation remains unavailable')
                    time.sleep(.02)
                raise RuntimeError('fixture session identity unavailable; descendant cleanup remains unqualified')
            while True:
                live = [row for row in _fixture_members(identity) if row['state'] not in ('Z', 'X')]
                if not live:
                    item['cleanup_observed'] = True
                    break
                # This independent controller takes over only after the test
                # returned. ECHILD or a changed leader forbids numeric signals.
                try:
                    waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    current = _fixture_identity(identity['pid'])
                    same = all(current[key] == identity[key] for key in ('pid', 'uid', 'pgid', 'sid', 'start_ticks'))
                except (ChildProcessError, ProcessLookupError, FileNotFoundError):
                    same = False
                if same and not item['independent_termination_needed']:
                    item['independent_termination_needed'] = True
                    if identity['pid'] == identity['pgid'] == identity['sid']:
                        killpg(identity['pid'], signal.SIGKILL)
                    else:
                        send_signal(descriptor, signal.SIGKILL, None, 0)
                if time.monotonic() >= deadline:
                    raise RuntimeError('fixture-owned session requires reconciliation; no unowned signals')
                time.sleep(.02)
            try:
                exit_info = waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG)
                assert exit_info is not None
                owned['process'].returncode = (exit_info.si_status if exit_info.si_code == os.CLD_EXITED
                                               else -exit_info.si_status)
                item['leader_reaped_by_fixture'] = True
            except ChildProcessError:
                item['leader_reaped_by_fixture'] = False
        except BaseException as error:
            item['failure_type'] = type(error).__name__
            if cleanup_failure is None:
                cleanup_failure = error
        finally:
            descriptor = owned['pidfd']
            owned['pidfd'] = None
            if descriptor is not None:
                try:
                    close_fd(descriptor)
                except BaseException as error:
                    item['descriptor_close_unknown'] = True
                    item['failure_type'] = item['failure_type'] or type(error).__name__
                    if cleanup_failure is None:
                        cleanup_failure = error
            cleanup.append(item)
    with (tmp_path / 'native-session-cleanup.json').open('x', encoding='utf-8') as stream:
        json.dump(cleanup, stream, allow_nan=False, indent=2)
    with (tmp_path / 'uploader-executions.json').open('x', encoding='utf-8') as stream:
        json.dump(executions, stream, allow_nan=False, indent=2)
    if cleanup_failure is not None:
        raise cleanup_failure
    assert all(row['cleanup_observed'] and row['failure_type'] is None for row in cleanup)
    assert not any(row['independent_termination_needed'] for row in cleanup), 'product/test left a live owned child'
    if sys.platform.startswith('linux'):
        from olympus import backup_process_linux as native
        assert not native._PENDING, 'fixture left an unreconciled owned native controller'


def command(tmp_path, code, *arguments, timeout=5):
    env = {key: value for key, value in os.environ.items()
           if key.upper() in ('SYSTEMROOT', 'WINDIR')}
    return process.capture([sys.executable, '-I', '-B', '-c', code, *map(str, arguments)],
        env=env, cwd=str(tmp_path), timeout=timeout)


def run(captured, records=None, check=lambda: None):
    records = [] if records is None else records
    return process.execute(captured, record_started=records.append, check_authority=check)


@pytest.mark.parametrize('code', [0, 7])
def test_native_exact_exit_observation_and_natural_cleanup(tmp_path, code):
    records = []
    result = run(command(tmp_path, 'import sys;sys.exit(int(sys.argv[1]))', code), records)
    assert result['exit_code'] == code
    assert result['natural_tree_exit'] and result['cleanup_confirmed'] and result['handles_closed']
    assert not result['timed_out'] and not result['reconciliation_required']
    assert len(records) == 1 and records[0]['pid'] == result['pid']
    assert 'executable' not in records[0]


def test_identity_evidence_precedes_any_command_execution(tmp_path):
    marker = tmp_path / 'executed'
    records = []
    def record(value):
        assert not marker.exists()
        records.append(value)
    captured = command(tmp_path, 'from pathlib import Path;import sys;Path(sys.argv[1]).write_bytes(b"owned")', marker)
    result = process.execute(captured, record_started=record, check_authority=lambda: None)
    assert marker.read_bytes() == b'owned' and result['exit_code'] == 0
    assert len(records) == 1


@pytest.mark.parametrize('interrupt', [False, True])
def test_pre_release_evidence_failure_never_runs_command(tmp_path, interrupt):
    marker = tmp_path / 'must-not-exist'
    error = KeyboardInterrupt() if interrupt else RuntimeError('SENSITIVE_CALLBACK_TEXT')
    def refuse(value):
        raise error
    captured = command(tmp_path, 'from pathlib import Path;import sys;Path(sys.argv[1]).write_bytes(b"bad")', marker)
    with pytest.raises(KeyboardInterrupt if interrupt else process.ProcessError) as caught:
        process.execute(captured, record_started=refuse, check_authority=lambda: None)
    if interrupt:
        assert caught.value is error
    else:
        assert caught.value.__context__ is None and 'SENSITIVE' not in str(caught.value)
    outcome = caught.value.process_outcome
    assert outcome['cleanup_confirmed'] and outcome['handles_closed']
    assert not outcome['natural_tree_exit'] and not marker.exists()


def test_parent_zero_with_live_descendant_is_timeout_not_success(tmp_path):
    descendant = tmp_path / 'descendant.json'
    code = '''import json,subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(30)'])
Path(sys.argv[1]).write_text(json.dumps({'pid':child.pid}))
'''
    with pytest.raises(process.ProcessError) as caught:
        run(command(tmp_path, code, descendant, timeout=2))
    assert descendant.is_file()
    result = caught.value.process_outcome
    assert result['exit_code'] == 0 and result['timed_out']
    assert not result['natural_tree_exit']
    assert result['cleanup_confirmed'] and result['handles_closed']


def test_live_parent_timeout_has_no_invented_exit_status(tmp_path):
    with pytest.raises(process.ProcessError) as caught:
        run(command(tmp_path, 'import time;time.sleep(30)', timeout=.3))
    result = caught.value.process_outcome
    assert result['exit_code'] is None and result['timed_out']
    assert result['cleanup_confirmed'] and not result['natural_tree_exit']


def test_natural_descendant_exit_is_awaited(tmp_path):
    marker = tmp_path / 'descendant-done'
    code = '''import subprocess,sys
subprocess.Popen([sys.executable,'-I','-B','-c',
    'import time,sys;from pathlib import Path;time.sleep(.25);Path(sys.argv[1]).write_bytes(b"done")',sys.argv[1]])
'''
    result = run(command(tmp_path, code, marker))
    assert result['exit_code'] == 0 and result['natural_tree_exit']
    assert marker.read_bytes() == b'done'


def test_command_output_is_discarded_without_unbounded_capture(tmp_path):
    result = run(command(tmp_path, 'import os;os.write(1,b"x"*1000000);os.write(2,b"y"*1000000)'))
    assert result['exit_code'] == 0 and 'stdout' not in result and 'stderr' not in result


def test_environment_is_frozen_and_repr_does_not_disclose_it(tmp_path):
    env = {key: value for key, value in os.environ.items() if key.upper() in ('SYSTEMROOT', 'WINDIR')}
    env['OWNED_VALUE'] = 'SENSITIVE_OWNED_FIXTURE'
    marker = tmp_path / 'value'
    captured = process.capture([sys.executable, '-I', '-B', '-c',
        'import os,sys;from pathlib import Path;Path(sys.argv[1]).write_text(os.environ["OWNED_VALUE"])', str(marker)],
        env=env, cwd=str(tmp_path), timeout=5)
    env['OWNED_VALUE'] = 'changed'
    assert 'SENSITIVE' not in repr(captured)
    assert run(captured)['exit_code'] == 0
    assert marker.read_text() == 'SENSITIVE_OWNED_FIXTURE'


@pytest.mark.parametrize('timeout', [True, 0, -1, 901, float('nan'), float('inf')])
def test_invalid_timeout_refused_before_launch(tmp_path, timeout):
    with pytest.raises(ValueError):
        command(tmp_path, 'pass', timeout=timeout)


@pytest.mark.parametrize('kind', ['relative_executable', 'nul_argument', 'argument_count',
    'argument_size', 'environment_count', 'environment_size', 'environment_key', 'relative_cwd'])
def test_input_bounds_refuse_before_launch(tmp_path, kind):
    argv, env, cwd = [sys.executable], {}, str(tmp_path)
    if kind == 'relative_executable': argv = ['python']
    if kind == 'nul_argument': argv.append('bad\0value')
    if kind == 'argument_count': argv += ['x'] * 256
    if kind == 'argument_size': argv.append('x' * 8193)
    if kind == 'environment_count': env = {str(i): 'x' for i in range(513)}
    if kind == 'environment_size': env = {'owned': 'x' * 32768}
    if kind == 'environment_key': env = {'bad=key': 'x'}
    if kind == 'relative_cwd': cwd = 'relative'
    with pytest.raises(ValueError):
        process.capture(argv, env=env, cwd=cwd)


def test_authority_loss_after_release_drains_and_preserves_interruption(tmp_path):
    checks = 0
    interruption = KeyboardInterrupt()
    def check():
        nonlocal checks
        checks += 1
        if checks == 5:
            raise interruption
    with pytest.raises(KeyboardInterrupt) as caught:
        run(command(tmp_path, 'import time;time.sleep(30)'), check=check)
    assert caught.value is interruption
    assert interruption.process_outcome['cleanup_confirmed']
    assert not interruption.process_outcome['natural_tree_exit']


def test_identity_recording_cannot_release_after_deadline(tmp_path):
    marker = tmp_path / 'must-not-run'
    def delayed(value):
        time.sleep(.2)
    with pytest.raises(process.ProcessError) as caught:
        process.execute(command(tmp_path,
            'import sys;from pathlib import Path;Path(sys.argv[1]).write_bytes(b"bad")', marker, timeout=.1),
            record_started=delayed, check_authority=lambda: None)
    result = caught.value.process_outcome
    assert result['timed_out'] and not result['release_attempted']
    assert result['cleanup_confirmed'] and not marker.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows native job control')
def test_windows_batch_file_is_not_implicitly_launched(tmp_path):
    for suffix in ('.cmd', '.bat', '.COM'):
        with pytest.raises(ValueError):
            process.capture([str(tmp_path / ('fixture' + suffix))], env={}, cwd=str(tmp_path))


@pytest.mark.skipif(os.name != 'nt', reason='Windows rendered command-line bound')
def test_windows_quoting_expansion_is_bounded_before_launch(tmp_path):
    with pytest.raises(ValueError, match='native bounds'):
        process.capture([sys.executable, *['"' * 6000] * 3], env={}, cwd=str(tmp_path))


@pytest.mark.skipif(os.name != 'nt', reason='Windows native job control')
def test_windows_accounting_failure_does_not_report_success(tmp_path, monkeypatch):
    from olympus import backup_process_windows as native
    actual = native.OwnedWindowsProcess.accounting
    seen = []
    def broken(self):
        seen.append(self)
        raise OSError('SENSITIVE_ACCOUNTING_TEXT')
    monkeypatch.setattr(native.OwnedWindowsProcess, 'accounting', broken)
    with pytest.raises(process.ProcessError) as caught:
        run(command(tmp_path, 'pass'))
    result = caught.value.process_outcome
    assert result['exit_code'] == 0 and not result['natural_tree_exit']
    assert not result['cleanup_confirmed'] and result['handles_closed']
    assert result['reconciliation_required'] and seen
    assert seen[0].state['termination_attempted']
    assert 'SENSITIVE' not in str(caught.value) and caught.value.__context__ is None


@pytest.mark.skipif(os.name != 'nt', reason='Windows native release acknowledgment')
def test_windows_actual_resume_with_lost_acknowledgment_is_uncertain(tmp_path, monkeypatch):
    from olympus import backup_process_windows as native
    kernel = native._apis()
    class Proxy:
        def __getattr__(self, name):
            return getattr(kernel, name)
        def ResumeThread(self, handle):
            result = kernel.ResumeThread(handle)
            assert result == 1
            raise KeyboardInterrupt()
    monkeypatch.setattr(native, '_apis', lambda: Proxy())
    with pytest.raises(KeyboardInterrupt) as caught:
        run(command(tmp_path, 'import time;time.sleep(30)'))
    result = caught.value.process_outcome
    assert result['release_attempted'] and not result['release_confirmed']
    assert result['cleanup_confirmed'] and result['handles_closed']


@pytest.mark.skipif(os.name != 'nt', reason='Windows native creation acknowledgment')
def test_windows_actual_create_lost_ack_does_not_invent_cleanup(tmp_path, monkeypatch):
    import _winapi
    from olympus import backup_process_windows as native
    create = _winapi.CreateProcess
    created = []
    def lost_ack(*args, **kwargs):
        value = create(*args, **kwargs)
        created.append(value)
        raise KeyboardInterrupt()
    monkeypatch.setattr(_winapi, 'CreateProcess', lost_ack)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run(command(tmp_path, 'pass'))
        result = caught.value.process_outcome
        assert len(created) == 1 and not result['cleanup_confirmed']
        assert result['reconciliation_required'] and not result['release_attempted']
    finally:
        # Only this injected fixture retained the handles whose acknowledgement
        # was lost. The product correctly cannot invent their identity/drainage.
        for handle, thread, pid, tid in created:
            _winapi.TerminateProcess(handle, 125)
            assert _winapi.WaitForSingleObject(handle, 5000) == 0
            _winapi.CloseHandle(thread)
            _winapi.CloseHandle(handle)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux exclusive waiter control')
def test_linux_actual_stolen_wait_refuses_numeric_signalling(tmp_path, monkeypatch):
    from olympus import backup_process_linux as native
    with open(os.devnull, 'wb') as output:
        child = native.OwnedLinuxProcess([sys.executable, '-I', '-B', '-c', 'pass'],
            env={}, cwd=str(tmp_path), stdout=output, stderr=output, record_started=lambda value: None)
        deadline = time.monotonic() + 5
        while child.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(.01)
        pid, status = os.waitpid(child.pid, 0)  # Deliberately unsupported external waiter.
        assert pid == child.pid and os.waitstatus_to_exitcode(status) == 0
        monkeypatch.setattr(native.os, 'killpg', lambda *args: pytest.fail('lost child ownership must not signal'))
        with pytest.raises(native.OwnedProcessError):
            child.close()
        assert child.wait_lost and child.state['reconciliation_required']
        assert not child.state['cleanup_confirmed']
        # This fixture independently reaped the exact known child. Production
        # does not invent cleanup or automatically reconcile this unsupported case.
        child.process.returncode = 0
        native._PENDING.pop(id(child))


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux identity mismatch refusal')
def test_linux_identity_mismatch_stops_only_stable_leader_and_retains_uncertainty(tmp_path, monkeypatch):
    from olympus import backup_process_linux as native
    original_identity = native._identity
    original_new = process._new_process
    owned = []
    checks = 0
    def save_new(*args):
        child = original_new(*args)
        owned.append(child)
        return child
    def wrong_identity(pid):
        value = original_identity(pid)
        value['start_ticks'] += 1
        return value
    def check():
        nonlocal checks
        checks += 1
        if checks == 5:
            monkeypatch.setattr(native, '_identity', wrong_identity)
    monkeypatch.setattr(process, '_new_process', save_new)
    monkeypatch.setattr(native.os, 'killpg', lambda *args: pytest.fail('inconsistent identity must not signal a group'))
    try:
        with pytest.raises(process.ProcessError) as caught:
            run(command(tmp_path, 'import time;time.sleep(30)'), check=check)
        result = caught.value.process_outcome
        assert result['reconciliation_required'] and not result['cleanup_confirmed']
        assert owned[0].state['leader_termination_attempted']
    finally:
        for child in owned:
            assert child.process.wait(timeout=5) == -9
            native._PENDING.pop(id(child))


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux interrupted native observation')
@pytest.mark.parametrize('probe', ['waitid', 'identity'])
def test_linux_interrupted_poll_drains_live_child(tmp_path, monkeypatch, probe):
    from olympus import backup_process_linux as native
    interruption = KeyboardInterrupt()
    checks = 0
    original = native.os.waitid if probe == 'waitid' else native._identity
    fired = False
    def interrupt_once(*args, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            raise interruption
        return original(*args, **kwargs)
    def check():
        nonlocal checks
        checks += 1
        if checks == 5:  # Constructor has released the real child.
            if probe == 'waitid': monkeypatch.setattr(native.os, 'waitid', interrupt_once)
            else: monkeypatch.setattr(native, '_identity', interrupt_once)
    with pytest.raises(KeyboardInterrupt) as caught:
        run(command(tmp_path, 'import time;time.sleep(30)'), check=check)
    assert caught.value is interruption and fired
    assert interruption.process_outcome['release_confirmed']
    assert interruption.process_outcome['cleanup_confirmed']
    assert interruption.process_outcome['handles_closed']


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux pre-gate acquisition failure')
@pytest.mark.parametrize('probe', ['pidfd', 'identity'])
def test_linux_initial_ownership_probe_failure_reaps_unreleased_helper(tmp_path, monkeypatch, probe):
    from olympus import backup_process_linux as native
    marker = tmp_path / 'must-not-run'
    def refuse(*args, **kwargs):
        raise OSError('owned acquisition failure')
    if probe == 'pidfd': monkeypatch.setattr(native.os, 'pidfd_open', refuse)
    else: monkeypatch.setattr(native, '_identity', refuse)
    with pytest.raises(process.ProcessError) as caught:
        run(command(tmp_path, 'import sys;from pathlib import Path;Path(sys.argv[1]).write_bytes(b"bad")', marker))
    result = caught.value.process_outcome
    assert result['cleanup_confirmed'] and result['handles_closed']
    assert not result['release_attempted'] and not marker.exists()


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux release acknowledgment loss')
def test_linux_lost_release_acknowledgment_is_not_not_started(tmp_path, monkeypatch):
    from olympus import backup_process_linux as native
    original = native.os.write
    def lost_ack(fd, raw):
        count = original(fd, raw)
        if raw == b'1':
            raise KeyboardInterrupt()
        return count
    monkeypatch.setattr(native.os, 'write', lost_ack)
    with pytest.raises(KeyboardInterrupt) as caught:
        run(command(tmp_path, 'import time;time.sleep(30)'))
    result = caught.value.process_outcome
    assert result['release_attempted'] and not result['release_confirmed']
    assert result['cleanup_confirmed'] and not result['natural_tree_exit']


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux constructor acknowledgment loss')
def test_linux_actual_popen_lost_ack_does_not_invent_cleanup(tmp_path, monkeypatch):
    from olympus import backup_process_linux as native
    original = native.subprocess.Popen
    created = []
    previous = set(native._PENDING)
    def lost_ack(*args, **kwargs):
        value = original(*args, **kwargs)
        created.append(value)
        raise KeyboardInterrupt()
    monkeypatch.setattr(native.subprocess, 'Popen', lost_ack)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            run(command(tmp_path, 'pass'))
        result = caught.value.process_outcome
        assert len(created) == 1 and not result['cleanup_confirmed']
        assert result['reconciliation_required'] and not result['release_attempted']
    finally:
        for child in created:
            assert child.wait(timeout=5) == 125  # Gate EOF, no payload execution.
        for key in set(native._PENDING) - previous:
            native._PENDING.pop(key)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux process observation bounds')
def test_linux_scan_overflow_is_not_empty_group(tmp_path, monkeypatch):
    from olympus import backup_process_linux as native
    monkeypatch.setattr(native, '_MAX_PROCESSES', 0)
    with pytest.raises(RuntimeError, match='exceeds bounds'):
        native._members(os.getpid())


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux pre-release gate control')
@pytest.mark.parametrize('release', [b'', b'2'])
def test_linux_gate_eof_or_malformed_release_does_not_execute_payload(tmp_path, release):
    from olympus import backup_process_linux as native
    import fcntl
    marker = tmp_path / 'must-not-run'
    config = os.memfd_create('owned-eof-fixture', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    reader, writer = os.pipe2(os.O_CLOEXEC)
    try:
        raw = json.dumps({'argv': [sys.executable, '-I', '-B', '-c',
            'from pathlib import Path;import sys;Path(sys.argv[1]).write_bytes(b"bad")', str(marker)],
            'environment': {}}).encode()
        assert os.write(config, raw) == len(raw)
        os.lseek(config, 0, os.SEEK_SET)
        fcntl.fcntl(config, fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
        if release:
            assert os.write(writer, release) == len(release)
        os.close(writer)
        writer = None
        result = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', native._GATE, str(config), str(reader)],
            env={}, cwd=tmp_path, pass_fds=(config, reader), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        assert result.returncode == 125 and not marker.exists()
    finally:
        for descriptor in (reader, writer, config):
            if descriptor is not None:
                os.close(descriptor)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux exec failure observation')
def test_linux_missing_executable_is_observed_failure_not_success(tmp_path):
    captured = process.capture([str(tmp_path / 'missing-executable')], env={}, cwd=str(tmp_path), timeout=5)
    result = run(captured)
    assert result['exit_code'] != 0 and result['natural_tree_exit'] and result['cleanup_confirmed']


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux signal ownership refusal')
def test_linux_nocldwait_is_refused_even_when_python_reports_default(tmp_path):
    # A separate benign interpreter owns this temporary signal disposition.
    # The main test process and every previously launched child are unaffected.
    code = '''import ctypes,signal,sys
sys.path.insert(0,sys.argv[1])
from olympus import backup_process_linux as native
class Action(ctypes.Structure):
    _fields_=[('handler',ctypes.c_void_p),('mask',ctypes.c_ulong*16),('flags',ctypes.c_int),('restorer',ctypes.c_void_p)]
libc=ctypes.CDLL(None,use_errno=True)
fn=libc.sigaction
fn.argtypes=[ctypes.c_int,ctypes.POINTER(Action),ctypes.POINTER(Action)]
fn.restype=ctypes.c_int
before=Action();assert fn(signal.SIGCHLD,None,ctypes.byref(before))==0
changed=Action();changed.flags=2
assert fn(signal.SIGCHLD,ctypes.byref(changed),None)==0
try:
    assert signal.getsignal(signal.SIGCHLD)==signal.SIG_DFL
    try:native._default_child_signal()
    except RuntimeError:pass
    else:raise AssertionError('NOCLDWAIT must be refused')
finally:assert fn(signal.SIGCHLD,ctypes.byref(before),None)==0
'''
    result = run(command(tmp_path, code, Path(process.__file__).parents[1]))
    assert result['exit_code'] == 0 and result['cleanup_confirmed']


def _cold_linux_guard_without_fcntl(monkeypatch):
    import builtins
    import importlib.util
    import olympus
    path = Path(olympus.__file__).resolve().parent / 'backup_process_linux.py'
    calls = []
    actual_import = builtins.__import__
    def limited_import(name, *args, **kwargs):
        if name == 'fcntl':
            calls.append(name)
            raise AssertionError('fcntl imported before supported native admission')
        return actual_import(name, *args, **kwargs)
    spec = importlib.util.spec_from_file_location('_owned_cold_linux_uploader', path)
    native = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, '__import__', limited_import)
        spec.loader.exec_module(native)
    assert Path(native.__file__).resolve() == path and calls == []
    return native, limited_import, calls


def test_linux_uploader_module_is_directly_importable_without_fcntl(monkeypatch):
    native, _, calls = _cold_linux_guard_without_fcntl(monkeypatch)
    assert callable(native.OwnedLinuxProcess) and native._PENDING == {}
    assert calls == []


def test_linux_uploader_refuses_foreign_platform_before_fcntl_or_resources(monkeypatch):
    import builtins
    from types import SimpleNamespace
    native, limited_import, calls = _cold_linux_guard_without_fcntl(monkeypatch)
    native.sys = SimpleNamespace(platform='win32', executable=sys.executable)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, '__import__', limited_import)
        with pytest.raises(RuntimeError, match='supported default child-wait ownership'):
            native.OwnedLinuxProcess([], env={}, cwd=None, stdout=None, stderr=None,
                                     record_started=lambda value: pytest.fail('child dispatched'))
    assert calls == [] and native._PENDING == {}


def test_linux_uploader_refuses_missing_primitives_before_fcntl_or_resources(monkeypatch):
    import builtins
    from types import SimpleNamespace
    native, limited_import, calls = _cold_linux_guard_without_fcntl(monkeypatch)
    native._default_child_signal = lambda: None
    native.os = SimpleNamespace()
    with monkeypatch.context() as patch:
        patch.setattr(builtins, '__import__', limited_import)
        with pytest.raises(RuntimeError, match='ownership primitives are unavailable'):
            native.OwnedLinuxProcess([], env={}, cwd=None, stdout=None, stderr=None,
                                     record_started=lambda value: pytest.fail('child dispatched'))
    assert calls == [] and native._PENDING == {}
