"""Bounded uploader execution for trusted commands, with retained outcome facts.

This guards ordinary descendants. It is not a sandbox, a remote-storage probe,
or protection against a command deliberately escaping through another service.
The caller durably records intent before calling execute and outcome afterward.
No command output, arguments or environment values enter public evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import subprocess
import time
from types import MappingProxyType


class ProcessError(RuntimeError):
    def __init__(self, outcome):
        self.process_outcome = dict(outcome)
        super().__init__('backup command did not complete with confirmed descendant cleanup')


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...] = field(repr=False)
    environment: object = field(repr=False)
    cwd: str = field(repr=False)
    timeout: float


def capture(argv, *, env, cwd, timeout=900):
    """Copy already resolved, explicit inputs without reading private values."""
    if (type(argv) not in (tuple, list) or not 1 <= len(argv) <= 256
            or any(type(s) is not str or '\0' in s or len(s) > 8192 for s in argv)
            or sum(len(s.encode('utf-8')) + 1 for s in argv) > 32768
            or not os.path.isabs(argv[0])):
        raise ValueError('backup command requires bounded arguments and an absolute executable')
    if os.name == 'nt' and Path(argv[0]).suffix.lower() != '.exe':
        raise ValueError('Windows backup commands require an explicit executable; batch files are refused')
    if os.name == 'nt' and len(subprocess.list2cmdline(argv).encode('utf-16-le')) // 2 + 1 > 32767:
        raise ValueError('Windows backup command line exceeds native bounds')
    if (type(env) is not dict or len(env) > 512
            or any(type(k) is not str or type(v) is not str or len(k) > 32768 or len(v) > 32768
                   or not k or '=' in k
                   or '\0' in k or '\0' in v for k, v in env.items())
            or sum(len(k.encode('utf-8')) + len(v.encode('utf-8')) + 2
                   for k, v in env.items()) > 32768):
        raise ValueError('backup command requires a bounded explicit environment')
    if (type(cwd) is not str or not os.path.isabs(cwd) or '\0' in cwd
            or len(cwd) > 8192 or type(timeout) not in (int, float)
            or not 0 < timeout <= 900 or not math.isfinite(timeout)):
        raise ValueError('backup command requires an absolute directory and a finite bounded timeout')
    return Command(tuple(argv), MappingProxyType(dict(env)), cwd, float(timeout))


def _new_process(command, record_started, before_release, output):
    if os.name == 'nt':
        from .backup_process_windows import OwnedWindowsProcess
        return OwnedWindowsProcess(list(command.argv), env=dict(command.environment),
            cwd=command.cwd, stdout=output, stderr=output, record_started=record_started,
            before_release=before_release)
    from .backup_process_linux import OwnedLinuxProcess
    return OwnedLinuxProcess(list(command.argv), env=dict(command.environment),
        cwd=command.cwd, stdout=output, stderr=output, record_started=record_started,
        before_release=before_release)


def execute(command, *, record_started, check_authority):
    """Wait for a natural tree exit; cleanup never retroactively makes success.

The callback must retain identity before the user command is released. A
callback failure still drains this owned child. A durable caller intent with
no callback receipt remains dispatch-unknown, even if no process is visible.
"""
    if type(command) is not Command or not callable(record_started) or not callable(check_authority):
        raise ValueError('backup execution requires a captured command and evidence callbacks')
    # Revalidate even a directly constructed dataclass before creating anything.
    command = capture(command.argv, env=dict(command.environment), cwd=command.cwd, timeout=command.timeout)
    outcome = {'exit_code': None, 'timed_out': False, 'natural_tree_exit': False,
        'cleanup_confirmed': False, 'handles_closed': False,
        'reconciliation_required': True, 'pid': None,
        'release_attempted': False, 'release_confirmed': False}
    process = None
    failure = None
    deadline = time.monotonic() + command.timeout
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            outcome['timed_out'] = True
            raise TimeoutError('backup command deadline expired')
        return value
    def before_release():
        remaining()
        check_authority()
        remaining()
    try:
        before_release()
        with open(os.devnull, 'wb') as output:
            def started(identity):
                before_release()
                # The native adapters may have richer private state. Only the
                # fixed bounded native identity contract crosses this boundary.
                allowed = ('pid', 'pgid', 'sid', 'start_ticks', 'created_suspended',
                    'job_assigned_before_resume', 'kill_on_job_close',
                    'breakaway_enabled', 'only_owned_standard_handles_inherited',
                    'gate_before_exec', 'exclusive_waiter_required')
                record_started({key: identity[key] for key in allowed if key in identity})
                before_release()
            process = _new_process(command, started, before_release, output)
            outcome['pid'] = process.pid
            while True:
                before_release()
                code = process.poll()
                if code is not None:
                    outcome['exit_code'] = code
                    if process.tree_empty():
                        remaining()
                        outcome['natural_tree_exit'] = True
                        break
                time.sleep(min(.02, remaining()))
    except BaseException as error:
        failure = error
        state = getattr(error, 'owned_process_state', {})
        if type(state) is dict:
            outcome.update(pid=state.get('pid'), release_confirmed=state.get('resumed') is True,
                release_attempted=state.get('release_attempted') is True,
                cleanup_confirmed=state.get('cleanup_confirmed') is True,
                handles_closed=state.get('handles_closed') is True,
                reconciliation_required=state.get('reconciliation_required') is not False)
    finally:
        if process is not None:
            try:
                process.close()
            except BaseException as error:
                if failure is None:
                    failure = error
            state = process.state
            outcome.update(pid=process.pid, release_confirmed=state.get('resumed') is True,
                release_attempted=state.get('release_attempted') is True,
                cleanup_confirmed=state.get('cleanup_confirmed') is True,
                handles_closed=state.get('handles_closed') is True,
                reconciliation_required=state.get('reconciliation_required') is not False)
    if failure is not None:
        if not isinstance(failure, Exception):
            failure.process_outcome = dict(outcome)
            raise failure
        failure = None
        raise ProcessError(outcome) from None
    if not (outcome['natural_tree_exit'] and outcome['cleanup_confirmed']
            and outcome['handles_closed'] and not outcome['reconciliation_required']):
        raise ProcessError(outcome) from None
    # Nonzero command exits are observations, not transport exceptions. The
    # caller must stop the pair and durably retain this exact outcome.
    return outcome
