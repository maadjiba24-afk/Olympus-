"""Windows trusted-uploader process-tree guard; no automatic execution.

Create the child suspended, assign an unnamed kill-on-close job, record identity,
then resume. Only explicitly duplicated log/NUL handles are inherited. No
breakaway flag, PID-based killing, unrelated enumeration, environment output,
installation or permission mutation. Callers supply a sanitized environment.

Primary contracts:
https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject
https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-setinformationjobobject
Python312 Lib/subprocess.py:1538 supplies the local _winapi.CreateProcess shape.
"""
from __future__ import annotations

import ctypes as C
import math
import os
import subprocess
import time


class _BasicLimits(C.Structure):
    _fields_=[('process_time',C.c_int64),('job_time',C.c_int64),('flags',C.c_uint32),
              ('min_working_set',C.c_size_t),('max_working_set',C.c_size_t),
              ('active_limit',C.c_uint32),('affinity',C.c_size_t),
              ('priority',C.c_uint32),('scheduling',C.c_uint32)]


class _IoCounters(C.Structure):
    _fields_=[(name,C.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]


class _ExtendedLimits(C.Structure):
    _fields_=[('basic',_BasicLimits),('io',_IoCounters),('process_memory',C.c_size_t),
              ('job_memory',C.c_size_t),('peak_process_memory',C.c_size_t),('peak_job_memory',C.c_size_t)]


class _Accounting(C.Structure):
    _fields_=[('user_time',C.c_int64),('kernel_time',C.c_int64),('period_user_time',C.c_int64),
              ('period_kernel_time',C.c_int64),('page_faults',C.c_uint32),('total_processes',C.c_uint32),
              ('active_processes',C.c_uint32),('terminated_processes',C.c_uint32)]


def _apis():
    if os.name!='nt':raise RuntimeError('Windows owned-job guard requires Windows')
    kernel=C.WinDLL('kernel32',use_last_error=True)
    pointer,word,boolean=C.c_void_p,C.c_uint32,C.c_int
    specifications={
        'CreateJobObjectW':([pointer,C.c_wchar_p],pointer),
        'SetInformationJobObject':([pointer,C.c_int,pointer,word],boolean),
        'QueryInformationJobObject':([pointer,C.c_int,pointer,word,C.POINTER(word)],boolean),
        'AssignProcessToJobObject':([pointer,pointer],boolean),
        'IsProcessInJob':([pointer,pointer,C.POINTER(boolean)],boolean),
        'TerminateJobObject':([pointer,word],boolean),
        'TerminateProcess':([pointer,word],boolean),
        'ResumeThread':([pointer],word),
        'WaitForSingleObject':([pointer,word],word),
        'GetExitCodeProcess':([pointer,C.POINTER(word)],boolean),
        'CloseHandle':([pointer],boolean),
    }
    for name,(args,result) in specifications.items():
        fn=getattr(kernel,name);fn.argtypes=args;fn.restype=result
    return kernel


def _require(value):
    if not value:raise C.WinError(C.get_last_error())
    return value


class OwnedProcessError(RuntimeError):
    def __init__(self, message, state):
        self.owned_process_state = dict(state)
        super().__init__(message)


class OwnedWindowsProcess:
    """Single-controller-thread lifetime; descendants must use CreateProcess.

    Service/WMI-launched work is outside this guard. Forcible controller death
    between CreateProcess and job assignment can leave a suspended child; that
    narrow interval is not an atomic Windows JOB_LIST launch. Once assigned,
    the unnamed, noninherited kill-on-close job contains ordinary descendants.
    All observed failures retain identity and truthful cleanup confirmation.
    """
    def __init__(self, argv, *, env, cwd, stdout, stderr, record_started, before_release=None):
        import _winapi, msvcrt, threading
        if (type(argv) is not list or not argv or not all(type(x) is str for x in argv)
                or not os.path.isabs(argv[0]) or type(env) is not dict):
            raise ValueError('owned process requires explicit argv/executable/environment')
        self.owner = (os.getpid(), threading.get_ident())
        self.kernel = _apis(); self.job = self.handle = self.thread = None
        self.pid = None; self.assigned = False; self.closed = False; self.events = []
        self.args = tuple(argv); self.confirmed_drain = None
        self.exit_confirmed = False; self.job_zero_confirmed = False
        self.quarantined_handles = []
        self.state = {'pid':None, 'job_assigned':False, 'resumed':False, 'release_attempted':False,
                      'creation_attempted':False, 'creation_returned':False,
                      'termination_attempted':False, 'cleanup_confirmed':False,
                      'handles_closed':False, 'reconciliation_required':False}
        duplicates = []; failure = None
        try:
            self.job = _require(self.kernel.CreateJobObjectW(None,None))
            limits = _ExtendedLimits(); limits.basic.flags = 0x2000
            _require(self.kernel.SetInformationJobObject(self.job,9,C.byref(limits),C.sizeof(limits)))
            with open(os.devnull,'rb') as stdin:
                current = _winapi.GetCurrentProcess()
                for stream in (stdin,stdout,stderr):
                    duplicates.append(_winapi.DuplicateHandle(current,msvcrt.get_osfhandle(stream.fileno()),
                        current,0,True,_winapi.DUPLICATE_SAME_ACCESS))
                startup = subprocess.STARTUPINFO(dwFlags=_winapi.STARTF_USESTDHANDLES,
                    hStdInput=duplicates[0],hStdOutput=duplicates[1],hStdError=duplicates[2],
                    lpAttributeList={'handle_list':list(duplicates)})
                self.state['creation_attempted'] = True
                self.handle,self.thread,self.pid,_tid = _winapi.CreateProcess(
                    argv[0],subprocess.list2cmdline(argv),None,None,True,
                    0x4|subprocess.CREATE_NO_WINDOW,env,str(cwd),startup)
                self.state['creation_returned'] = True
                self.state['pid'] = self.pid
            _require(self.kernel.AssignProcessToJobObject(self.job,self.handle))
            self.assigned = True; self.state['job_assigned'] = True
            member = C.c_int()
            _require(self.kernel.IsProcessInJob(self.handle,self.job,C.byref(member)))
            if not member.value:
                raise RuntimeError('owned job membership was not confirmed')
            record_started({'pid':self.pid,'executable':argv[0],'created_suspended':True,
                'job_assigned_before_resume':True,'kill_on_job_close':True,
                'breakaway_enabled':False,'only_owned_standard_handles_inherited':True})
            close_failure = None
            while duplicates:
                try:
                    _winapi.CloseHandle(duplicates.pop())
                except BaseException as error:
                    self._quarantine('duplicate_standard_handle')
                    if close_failure is None: close_failure = error
            if close_failure is not None: raise close_failure
            if before_release is not None:
                before_release()
            self.state['release_attempted'] = True
            previous = self.kernel.ResumeThread(self.thread)
            if previous != 1:
                raise RuntimeError('owned child main-thread resume was not confirmed')
            self.state['resumed'] = True
            self._close_handle('thread')
        except BaseException as error:
            failure = error
        if failure is not None:
            try:
                self.close()
            except BaseException as error:
                self.state['cleanup_failure_type'] = type(error).__name__
            while duplicates:
                try:
                    _winapi.CloseHandle(duplicates.pop())
                except BaseException as error:
                    self._quarantine('duplicate_standard_handle')
                    self.state['duplicate_close_failure_type'] = type(error).__name__
                    self.state['reconciliation_required'] = True
            self.state['creation_failure_type'] = type(failure).__name__
            if isinstance(failure, Exception):
                raise OwnedProcessError('owned process creation failed; inspect retained identity and cleanup state',self.state) from None
            failure.owned_process_state = dict(self.state)
            raise failure

    def _owner(self):
        import threading
        if self.owner != (os.getpid(),threading.get_ident()):
            raise OwnedProcessError('owned process handle belongs to another controller',self.state)

    def _quarantine(self, name):
        # A failed/interrupted close may have already reached the kernel. Never
        # reuse that integer: another thread could have obtained its value.
        self.quarantined_handles.append(name)
        self.state['quarantined_handles'] = list(self.quarantined_handles)
        self.state['handles_closed'] = False
        self.state['reconciliation_required'] = True

    def _close_handle(self, name):
        handle = getattr(self,name)
        setattr(self,name,None)
        if handle is not None:
            try:
                _require(self.kernel.CloseHandle(handle))
            except BaseException:
                self._quarantine(name)
                raise

    def poll(self):
        self._owner()
        if self.handle is None: raise OwnedProcessError('owned process handle is closed',self.state)
        result = self.kernel.WaitForSingleObject(self.handle,0)
        if result == 258: return None
        if result != 0: raise C.WinError(C.get_last_error())
        code = C.c_uint32(); _require(self.kernel.GetExitCodeProcess(self.handle,C.byref(code)))
        self.exit_confirmed = True
        return int(code.value)

    def wait(self, timeout=360):
        self._owner()
        if self.handle is None: raise OwnedProcessError('owned process handle is closed',self.state)
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0 <= timeout <= 86400:
            raise ValueError('owned wait requires a finite bounded timeout')
        result = self.kernel.WaitForSingleObject(self.handle,int(timeout*1000))
        if result == 258: raise subprocess.TimeoutExpired(self.args,timeout)
        if result != 0: raise C.WinError(C.get_last_error())
        code = C.c_uint32(); _require(self.kernel.GetExitCodeProcess(self.handle,C.byref(code)))
        self.exit_confirmed = True
        return int(code.value)

    def accounting(self):
        self._owner()
        if self.job is None: raise OwnedProcessError('owned job handle is closed',self.state)
        value = _Accounting()
        _require(self.kernel.QueryInformationJobObject(self.job,1,C.byref(value),C.sizeof(value),None))
        return {'total_processes':int(value.total_processes),'active_processes':int(value.active_processes),
                'terminated_processes':int(value.terminated_processes)}

    def tree_empty(self):
        """Observe natural job drainage without converting termination to success."""
        if self.poll() is None:
            return False
        empty = self.accounting()['active_processes'] == 0
        if empty:
            self.job_zero_confirmed = True
        return empty

    def terminate(self):
        self._owner()
        self.state['termination_attempted'] = True
        if self.assigned and self.job is not None:
            _require(self.kernel.TerminateJobObject(self.job,124))
        elif self.handle is not None:
            # Never gate this attempt on poll/wait. Those may be the failed API.
            _require(self.kernel.TerminateProcess(self.handle,124))
        elif self.pid is not None and self.confirmed_drain is None:
            raise OwnedProcessError('owned process cleanup cannot be confirmed after handle loss',self.state)

    kill = terminate

    def drain(self):
        self._owner()
        if self.closed:
            if self.state['reconciliation_required'] or self.confirmed_drain is None:
                raise OwnedProcessError('owned process cleanup remains unconfirmed',self.state)
            return dict(self.confirmed_drain)
        # Cleanup attempts proceed independently of observation failures. A
        # failed accounting/poll can never prevent direct owned termination.
        termination_error = None
        if self.handle is not None and not (self.exit_confirmed and self.job_zero_confirmed):
            try:
                self.terminate()
            except BaseException as error:
                termination_error = error
        try:
            after = None
            if self.state['creation_attempted'] and not self.state['creation_returned']:
                raise RuntimeError('owned process construction has an unobserved outcome')
            if self.job is not None:
                deadline = time.monotonic()+10
                after = self.accounting()
                while after['active_processes'] and time.monotonic() < deadline:
                    time.sleep(.05); after = self.accounting()
                if after['active_processes']:
                    raise RuntimeError('owned job still has active processes')
                self.job_zero_confirmed = True
            if self.handle is not None:
                self.wait(10)
            if self.pid is not None and not self.exit_confirmed:
                raise RuntimeError('owned process exit was never confirmed')
            if self.assigned and not self.job_zero_confirmed:
                raise RuntimeError('owned job emptiness was never confirmed')
            # If no process was created, there is no process lifetime to infer.
            receipt = {'process_created':self.pid is not None,
                       'job_accounting':after, 'process_exit_confirmed':self.exit_confirmed,
                       'cleanup_confirmed':True}
            self.confirmed_drain = receipt
            self.state['cleanup_confirmed'] = True
            self.state['reconciliation_required'] = bool(self.quarantined_handles)
            self.events.append({'action':'owned_cleanup_confirmed',**receipt})
        except BaseException as error:
            self.state['cleanup_confirmed'] = False
            self.state['reconciliation_required'] = True
            self.state['observation_failure_type'] = type(error).__name__
            if termination_error is not None:
                self.state['termination_failure_type'] = type(termination_error).__name__
            if isinstance(error, Exception):
                raise OwnedProcessError('owned process cleanup unconfirmed; reconcile retained identity',self.state) from None
            error.owned_process_state = dict(self.state)
            raise
        if termination_error is not None and not isinstance(termination_error, Exception):
            termination_error.owned_process_state = dict(self.state)
            raise termination_error
        if self.quarantined_handles:
            raise OwnedProcessError('owned handle close has an unknown outcome; do not reuse handles',self.state)
        return dict(self.confirmed_drain)

    def close(self):
        self._owner()
        if self.closed:
            if self.state['reconciliation_required']:
                raise OwnedProcessError('owned process cleanup remains unconfirmed',self.state)
            return dict(self.state)
        failure = None
        try:
            self.drain()
        except BaseException as error:
            failure = error
        # Always close the owned kill-on-close job even if observation failed.
        # Failed/interrupted close calls quarantine the value; never retry a
        # potentially recycled handle. Other independently owned handles drain.
        for name in ('job','thread','handle'):
            handle = getattr(self,name)
            if handle is not None:
                try:
                    self._close_handle(name)
                except BaseException as error:
                    self.state['reconciliation_required'] = True
                    self.state['handle_close_failure_type'] = type(error).__name__
                    if failure is None: failure = error
        self.closed = all(getattr(self,name) is None for name in ('job','thread','handle'))
        self.state['handles_closed'] = self.closed and not self.quarantined_handles
        if failure is not None:
            if isinstance(failure, Exception):
                raise OwnedProcessError('owned process close requires reconciliation',self.state) from None
            failure.owned_process_state = dict(self.state)
            raise failure
        return dict(self.state)
