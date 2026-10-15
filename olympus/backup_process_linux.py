"""Linux trusted-uploader guard with exclusive child-wait ownership.

The supported controller has no generic child reaper or foreign waiter for
this child. An unreaped leader reserves its PID until observed group drainage.
Identity checks do not solve a racing foreign waitpid(-1): that topology is
unsupported. Deliberate setsid/service escapes and controller death after gate
release are outside this guard. Unknown ownership never permits numeric kill.
"""
from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time


_PENDING = {}  # Strongly retain any unreconciled Popen; never let __del__ reap it.
_PENDING_LOCK = threading.Lock()
_MAX_PENDING = 8
_MAX_PROCESSES = 32768
_CONFIG_LIMIT = 131072
_GATE = '''import json,os,signal,sys
config_fd,gate_fd=map(int,sys.argv[1:])
with os.fdopen(config_fd,'rb') as stream:
    raw=stream.read(131073)
if len(raw)>131072:os._exit(125)
config=json.loads(raw)
release=os.read(gate_fd,1)
os.close(gate_fd)
if release!=b'1':os._exit(125)
for name in ('SIGPIPE','SIGXFZ','SIGXFSZ'):
    value=getattr(signal,name,None)
    if value is not None:signal.signal(value,signal.SIG_DFL)
os.execve(config['argv'][0],config['argv'],config['environment'])
'''


class OwnedProcessError(RuntimeError):
    def __init__(self, state):
        self.owned_process_state = dict(state)
        super().__init__('owned Linux uploader state requires reconciliation')


def _default_child_signal():
    """Read glibc's Linux sigaction, including otherwise invisible NOCLDWAIT."""
    if (not sys.platform.startswith('linux') or os.uname().machine not in ('x86_64', 'aarch64')
            or ctypes.sizeof(ctypes.c_void_p) != 8
            or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL):
        raise RuntimeError('Linux uploader requires supported default child-wait ownership')
    # glibc Linux x86_64/aarch64 struct sigaction: handler, 1024-bit mask,
    # flags, restorer. Unsupported libc/ABI is refused, never guessed.
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, 'gnu_get_libc_version'):
        raise RuntimeError('Linux uploader requires a reviewed glibc signal ABI')
    class Action(ctypes.Structure):
        _fields_ = [('handler', ctypes.c_void_p), ('mask', ctypes.c_ulong * 16),
                    ('flags', ctypes.c_int), ('restorer', ctypes.c_void_p)]
    read = libc.sigaction
    read.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(Action)]
    read.restype = ctypes.c_int
    action = Action()
    if read(signal.SIGCHLD, None, ctypes.byref(action)) != 0:
        raise RuntimeError('Linux child-wait disposition is unavailable')
    if action.handler is not None or action.flags & 2:  # SA_NOCLDWAIT
        raise RuntimeError('Linux uploader refuses automatic or foreign child reaping')


def _identity(pid):
    fd = os.open('/proc/' + str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        uid = os.fstat(fd).st_uid
        stat_fd = os.open('stat', os.O_RDONLY | os.O_CLOEXEC, dir_fd=fd)
        try:
            raw = os.read(stat_fd, 4097)
        finally:
            os.close(stat_fd)
    finally:
        os.close(fd)
    if len(raw) > 4096:
        raise RuntimeError('Linux process identity exceeds bounds')
    fields = raw.rsplit(b')', 1)[1].split()
    return {'pid': pid, 'uid': uid, 'state': fields[0].decode('ascii'),
            'pgid': int(fields[2]), 'sid': int(fields[3]), 'start_ticks': int(fields[19])}


def _members(group):
    result = []
    with os.scandir('/proc') as entries:
        count = 0
        for entry in entries:
            if not entry.name.isdigit():
                continue
            count += 1
            if count > _MAX_PROCESSES:
                raise RuntimeError('Linux process observation exceeds bounds')
            try:
                identity = _identity(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            if identity['pgid'] == group:
                if identity['sid'] != group or identity['uid'] != os.getuid():
                    raise RuntimeError('Linux process group authority changed')
                result.append(identity)
    return result


class OwnedLinuxProcess:
    def __init__(self, argv, *, env, cwd, stdout, stderr, record_started, before_release=None):
        _default_child_signal()
        if (not all(hasattr(os, name) for name in ('pidfd_open', 'P_PIDFD', 'memfd_create'))
                or not hasattr(signal, 'pidfd_send_signal') or not os.path.isabs(sys.executable)):
            raise RuntimeError('Linux uploader ownership primitives are unavailable')
        import fcntl
        self.owner = (os.getpid(), threading.get_ident())
        self.process = None
        self.pid = None
        self.pidfd = None
        self.identity = None
        self.wait_lost = False
        self.closed = False
        self.observed_code = None
        self.observed_drained = False
        self.state = {'pid': None, 'resumed': False, 'creation_attempted': False,
            'release_attempted': False, 'termination_attempted': False,
            'cleanup_confirmed': False, 'handles_closed': False,
            'reconciliation_required': True, 'exclusive_waiter_required': True,
            'controller_death_after_release_contained': False}
        descriptors = []
        failure = None
        with _PENDING_LOCK:
            if len(_PENDING) >= _MAX_PENDING:
                raise RuntimeError('Linux uploader ownership capacity is exhausted')
            _PENDING[id(self)] = self
        try:
            raw = json.dumps({'argv': argv, 'environment': env}, ensure_ascii=True).encode('utf-8')
            if len(raw) > _CONFIG_LIMIT:
                raise ValueError('Linux command configuration exceeds bounds')
            config = os.memfd_create('olympus-uploader-config', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            descriptors.append(config)
            offset = 0
            while offset < len(raw):
                count = os.write(config, raw[offset:])
                if count <= 0:
                    raise RuntimeError('Linux command configuration write did not progress')
                offset += count
            os.lseek(config, 0, os.SEEK_SET)
            fcntl.fcntl(config, fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
            reader, writer = os.pipe2(os.O_CLOEXEC)
            descriptors.extend((reader, writer))
            self.state['creation_attempted'] = True
            self.process = subprocess.Popen([sys.executable, '-I', '-B', '-S', '-c', _GATE,
                str(config), str(reader)], stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                cwd=cwd, env={}, start_new_session=True, close_fds=True, pass_fds=(config, reader))
            self.pid = self.process.pid
            self.state['pid'] = self.pid
            self.pidfd = os.pidfd_open(self.pid, 0)
            self.identity = _identity(self.pid)
            if (self.identity['pgid'] != self.pid or self.identity['sid'] != self.pid
                    or self.identity['uid'] != os.getuid()):
                self.wait_lost = True
                raise RuntimeError('Linux child session ownership was not established')
            self._observe()  # Verify this exact pidfd is our waitable child.
            record_started({key: self.identity[key] for key in ('pid', 'pgid', 'sid', 'start_ticks')}
                | {'gate_before_exec': True, 'exclusive_waiter_required': True})
            self._observe()
            if before_release is not None:
                before_release()
            self.state['release_attempted'] = True
            if os.write(writer, b'1') != 1:
                raise RuntimeError('Linux uploader release was not confirmed')
            self.state['resumed'] = True
        except BaseException as error:
            failure = error
        finally:
            while descriptors:
                fd = descriptors.pop()
                try:
                    os.close(fd)
                except BaseException as error:
                    self.state['descriptor_close_unknown'] = True
                    if failure is None:
                        failure = error
        if failure is not None:
            try:
                self.close()
            except BaseException:
                pass  # State records failed cleanup; preserve primary interruption.
            if not isinstance(failure, Exception):
                failure.owned_process_state = dict(self.state)
                raise failure
            failure = None
            raise OwnedProcessError(self.state) from None

    def _owner(self):
        if self.owner != (os.getpid(), threading.get_ident()):
            raise OwnedProcessError(self.state)

    def _observe(self):
        self._owner()
        if self.wait_lost or self.pidfd is None or self.identity is None:
            raise OwnedProcessError(self.state)
        _default_child_signal()
        try:
            observed = os.waitid(os.P_PIDFD, self.pidfd, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            current = _identity(self.pid)
        except (ChildProcessError, ProcessLookupError, FileNotFoundError):
            self.wait_lost = True
            self.state['wait_ownership_lost'] = True
            raise
        if any(current[key] != self.identity[key] for key in ('pid', 'uid', 'pgid', 'sid', 'start_ticks')):
            self.wait_lost = True
            self.state['wait_ownership_lost'] = True
            raise RuntimeError('Linux uploader identity changed')
        if observed is not None:
            self.observed_code = (observed.si_status if observed.si_code == os.CLD_EXITED
                                  else -observed.si_status)
        return self.observed_code

    poll = _observe  # Deliberately does not call Popen.poll(), which would reap.

    def _drain_unreleased(self, deadline):
        # The gate writer has closed without a release attempt. This fixed
        # helper cannot have executed an uploader or created descendants. EOF
        # plus an actual observed/reaped exit can establish its cleanup even
        # when initial pidfd/identity acquisition failed. Missing identity alone
        # never proves the exit. Do not use Popen.wait's ECHILD fallback.
        while True:
            _default_child_signal()
            kind, identity = ((os.P_PIDFD, self.pidfd) if self.pidfd is not None
                              else (os.P_PID, self.pid))
            observed = os.waitid(kind, identity, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if observed is not None:
                self.observed_code = (observed.si_status if observed.si_code == os.CLD_EXITED
                                      else -observed.si_status)
                reaped = os.waitid(kind, identity, os.WEXITED | os.WNOHANG)
                if reaped is None or reaped.si_pid != self.pid:
                    raise RuntimeError('Linux unreleased helper exit was not reaped')
                self.process.returncode = self.observed_code
                self.state['unreleased_gate_exit_observed'] = True
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('Linux unreleased helper cleanup timed out')
            time.sleep(.02)

    def tree_empty(self):
        if self._observe() is None:
            return False
        members = _members(self.pid)
        self._observe()
        live = [member for member in members if member['state'] not in ('Z', 'X')]
        self.state['observed_live_group_members'] = len(live)
        self.state['observed_zombie_group_members'] = len(members) - len(live)
        self.observed_drained = not live
        return not live

    def close(self):
        self._owner()
        if self.closed:
            if self.state['reconciliation_required']:
                raise OwnedProcessError(self.state)
            return dict(self.state)
        failure = None
        deadline = time.monotonic() + 10
        try:
            # An interrupted observation is not proven ownership loss. Make one
            # fresh bounded cleanup attempt, retaining the primary interruption.
            for attempt in range(2):
                try:
                    if self.process is None and self.state['creation_attempted']:
                        raise RuntimeError('Linux process construction has an unobserved outcome')
                    if self.process is not None and not self.state['release_attempted']:
                        self._drain_unreleased(deadline)
                    elif self.process is not None:
                        self._observe()
                        if not self.observed_drained:
                            self.state['termination_attempted'] = True
                            # Exclusive wait ownership is required. This check
                            # is NOT a concurrent foreign-reaper race solution.
                            os.killpg(self.pid, signal.SIGKILL)
                        while not self.tree_empty():
                            if time.monotonic() >= deadline:
                                raise RuntimeError('Linux uploader group cleanup timed out')
                            time.sleep(.02)
                        self.state['group_drain_observed_before_reap'] = True
                        observed = os.waitid(os.P_PIDFD, self.pidfd, os.WEXITED | os.WNOHANG)
                        if observed is None:
                            raise RuntimeError('Linux uploader exit could not be reaped')
                        self.process.returncode = self.observed_code
                    self.state['cleanup_confirmed'] = True
                    break
                except BaseException as error:
                    if failure is None:
                        failure = error
                    if self.wait_lost or time.monotonic() >= deadline:
                        break
            if not self.state['cleanup_confirmed'] and self.pidfd is not None:
                # A stable handle can stop the original leader even if numeric
                # group ownership is unavailable. This proves no descendant
                # outcome and never signals a recycled PID or foreign group.
                self.state['leader_termination_attempted'] = True
                try:
                    signal.pidfd_send_signal(self.pidfd, signal.SIGKILL, None, 0)
                except BaseException:
                    self.state['leader_termination_unconfirmed'] = True
        finally:
            descriptor = self.pidfd
            self.pidfd = None
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as error:
                    self.state['descriptor_close_unknown'] = True
                    if failure is None:
                        failure = error
            self.closed = True
            self.state['handles_closed'] = not self.state.get('descriptor_close_unknown', False)
            self.state['reconciliation_required'] = (
                failure is not None or not self.state['cleanup_confirmed'] or not self.state['handles_closed'])
            if self.state['cleanup_confirmed']:
                with _PENDING_LOCK:
                    _PENDING.pop(id(self), None)
        if failure is not None:
            if not isinstance(failure, Exception):
                failure.owned_process_state = dict(self.state)
                raise failure
            failure = None
            raise OwnedProcessError(self.state) from None
        if self.state['reconciliation_required']:
            raise OwnedProcessError(self.state)
        return dict(self.state)
