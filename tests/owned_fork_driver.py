"""Owned Linux fork experiments, isolated from pytest's existing threads.

Draft only. Parent callsite grants, source freeze and runtime qualification are
separate. Importing this module does not launch, inspect processes or import
Olympus. Non-Linux callers retain their original test bodies.
"""
from pathlib import Path
import hashlib
import json
import multiprocessing
import threading
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time

_CASES = {
    "owner": "tests/test_owner_workspace_evidence.py",
    "vault": "tests/test_vault_storage.py",
    "capture": "tests/test_securecapture_workflow.py",
    "rlimit": "tests/test_trading_native_isolation_adversarial.py",
}
_REQUEST_CAP = 16 * 1024
_RESULT_CAP = 64 * 1024
_LOG_CAP = 64 * 1024
_DRIVER_SECONDS = 45
_ROOT = Path(__file__).resolve().parent.parent


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path, cap):
    handle = Path(path).open("rb")
    primary = None
    try:
        raw = handle.read(cap + 1)
        if len(raw) > cap:
            raise AssertionError("owned fork file exceeds its bound")
        return raw
    except BaseException as error:
        primary = error
        raise
    finally:
        _drain([handle.close], primary)


def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise AssertionError("duplicate owned fork field")
        result[key] = value
    return result


def _json(raw, cap):
    if type(raw) is not bytes or not 0 < len(raw) <= cap:
        raise AssertionError("owned fork document bound")
    return json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(
                          AssertionError("nonfinite owned fork field")))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _mark(error):
    BaseException.__dict__["__dict__"].__get__(error, type(error))[
        "_owned_fork_cleanup_unconfirmed"] = True


def _drain(actions, primary):
    first = None
    for action in actions:
        try:
            action()
        except BaseException as error:
            if first is None:
                first = error
    if first is not None:
        retained = primary if primary is not None else first
        _mark(retained)
        if primary is None:
            raise first


def _products():
    rows = {}
    total = 0
    for path in sorted((_ROOT / "olympus").rglob("*.py")):
        if path.is_symlink() or not path.is_file():
            raise AssertionError("owned product source is not regular")
        raw = _read(path, 4 * 1024 * 1024)
        total += len(raw)
        if len(rows) >= 2048 or total > 256 * 1024 * 1024:
            raise AssertionError("owned product inventory exceeds its bound")
        rows[path.relative_to(_ROOT).as_posix()] = _hash(raw)
    if not rows:
        raise AssertionError("owned product inventory is empty")
    return rows, _hash(_canonical(rows))


def _single_thread():
    if not sys.platform.startswith("linux"):
        raise AssertionError("native thread proof is Linux-only")
    names = []
    stream = os.scandir("/proc/self/task")
    primary = None
    try:
        for entry in stream:
            if len(names) >= 2 or not entry.name.isascii() or not entry.name.isdigit():
                raise AssertionError("owned driver has unexpected native threads")
            names.append(entry.name)
        if names != [str(threading.get_native_id())]:
            raise AssertionError("owned driver is not a proved single native thread")
        return 1
    except BaseException as error:
        primary = error
        raise
    finally:
        _drain([stream.close], primary)


def _start_ticks(pid):
    raw = _read(Path("/proc") / str(pid) / "stat", 8192).decode("ascii")
    tail = raw[raw.rfind(")") + 2:].split()
    if len(tail) < 20:
        raise AssertionError("owned child identity malformed")
    return int(tail[19])


def _observe_child(pid):
    """WNOWAIT proves current direct-child ownership without consuming exit."""
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)


def _pidfd(pid):
    _observe_child(pid)
    before = _start_ticks(pid)
    descriptor = os.pidfd_open(pid, 0)
    primary = None
    try:
        if _start_ticks(pid) != before:
            raise AssertionError("owned child identity changed")
        _observe_child(pid)
        return descriptor, before
    except BaseException as error:
        primary = error
        raise
    finally:
        if primary is not None:
            _drain([lambda: os.close(descriptor)], primary)


class _Forks:
    def __init__(self, case):
        self.case = case
        self.original_pid = os.getpid()
        self.current_pid = self.original_pid
        self.role = "driver"
        self.maximum = 8 if case == "owner" else 1
        self.attempts = 0
        self.children = {}
        self.dispatch = False
        self.native_threads = []
        self.raw_fork = os.fork
        self.raw_waitpid = os.waitpid
        self.raw_kill = os.kill
        self.cleanup_unconfirmed = False
        self.closed = False
        self.builtin_refusal = None
        self.nested_facts = None

    def install(self):
        os.fork = self.fork
        os.waitpid = self.waitpid
        os.kill = self.kill
        sys.addaudithook(self.audit)

    def audit(self, event, args):
        if event.startswith("socket.") or event in (
            "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
            "os.posix_spawnp", "pty.spawn", "os.forkpty"):
            raise AssertionError("owned fork driver reached an ungranted operation")
        if event == "os.fork" and not self.dispatch:
            raise AssertionError("owned fork bypassed exact fork gate")

    def fork(self):
        # A refusal belongs to one exact builtin invocation, never a prior call.
        self.builtin_refusal = None
        if (self.closed or self.cleanup_unconfirmed or os.getpid() != self.current_pid
                or self.attempts >= self.maximum):
            raise AssertionError("owned fork topology exceeded")
        self.native_threads.append(_single_thread())
        self.attempts += 1
        self.dispatch = True
        try:
            pid = self.raw_fork()
        except OSError as error:
            self.builtin_refusal = error
            raise
        except BaseException as error:
            self.cleanup_unconfirmed = True
            _mark(error)
            raise
        finally:
            self.dispatch = False
        try:
            if type(pid) is not int or pid < 0:
                raise AssertionError("owned fork return domain refused")
            if pid == 0:
                inherited = [row["pidfd"] for row in self.children.values()
                             if row["pidfd"] is not None]
                self.children = {}
                self.current_pid = os.getpid()
                self.role = "measurement-child" if self.case == "rlimit" else "child"
                self.maximum = 1 if self.case == "rlimit" else 0
                self.attempts = 0
                self.native_threads = []
                self.builtin_refusal = None
                self.nested_facts = None
                _drain([lambda fd=fd: os.close(fd) for fd in inherited], None)
                signal.alarm(20)
                return 0
            self.children[pid] = {"pidfd": None, "start": None, "reaped": False,
                                  "status": None}
            descriptor, start = _pidfd(pid)
            self.children[pid]["pidfd"] = descriptor
            self.children[pid]["start"] = start
            return pid
        except BaseException as error:
            # The builtin already succeeded. No later OSError is a fork refusal.
            self.cleanup_unconfirmed = True
            _mark(error)
            raise

    def waitpid(self, pid, options):
        value = self.raw_waitpid(pid, options)
        if value[0] > 0 and value[0] in self.children:
            self.children[value[0]]["reaped"] = True
            self.children[value[0]]["status"] = value[1]
        return value

    def kill(self, pid, number):
        row = self.children.get(pid)
        if (row is None or row["reaped"] or row["pidfd"] is None
                or number not in (signal.SIGTERM, signal.SIGKILL)):
            raise AssertionError("owned fork signal lacks retained authority")
        _observe_child(pid)
        return signal.pidfd_send_signal(row["pidfd"], number)

    def close(self):
        """Retain the first error and drain every pidfd even if waiting fails."""
        if self.closed:
            if self.cleanup_unconfirmed:
                error = AssertionError("owned fork cleanup remains unconfirmed")
                _mark(error)
                raise error
            return
        self.closed = True
        failures = []
        pending = []
        try:
            for pid, row in self.children.items():
                if row["reaped"]:
                    continue
                try:
                    observed = _observe_child(pid)
                    if observed is not None:
                        self.waitpid(pid, os.WNOHANG)
                    elif row["pidfd"] is not None:
                        signal.pidfd_send_signal(row["pidfd"], signal.SIGTERM)
                        pending.append(pid)
                    else:
                        raise AssertionError("owned child lacks a retained pidfd")
                except BaseException as error:
                    failures.append(error)
            for number, seconds in ((None, 2.0), (signal.SIGKILL, 2.0)):
                if not pending:
                    break
                if number is not None:
                    for pid in pending:
                        try:
                            row = self.children[pid]
                            if not row["reaped"]:
                                _observe_child(pid)
                                signal.pidfd_send_signal(row["pidfd"], number)
                        except BaseException as error:
                            failures.append(error)
                until = time.monotonic() + seconds
                while pending and time.monotonic() < until:
                    for pid in pending[:]:
                        try:
                            observed = _observe_child(pid)
                            if observed is not None:
                                value = self.waitpid(pid, os.WNOHANG)
                                if value[0] == pid:
                                    pending.remove(pid)
                        except BaseException as error:
                            failures.append(error)
                            pending.remove(pid)
                    if pending:
                        time.sleep(0.01)
        except BaseException as error:
            # Includes initial/repeated monotonic reads and sleep interruptions.
            failures.append(error)
        finally:
            # This drain is independent of every child observation and wait loop.
            for row in self.children.values():
                descriptor, row["pidfd"] = row["pidfd"], None
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except BaseException as error:
                        failures.append(error)
        if pending or any(not row["reaped"] for row in self.children.values()):
            failures.append(AssertionError("owned fork child closure unresolved"))
        if self.cleanup_unconfirmed and not failures:
            failures.append(AssertionError("owned fork uncertainty is retained"))
        if failures:
            self.cleanup_unconfirmed = True
            _mark(failures[0])
            raise failures[0]

    def facts(self):
        return {"attempts": self.attempts, "observed_children": len(self.children),
                "reaped_children": sum(row["reaped"] for row in self.children.values()),
                "native_thread_checks": len(self.native_threads),
                "all_thread_checks_one": all(value == 1 for value in self.native_threads),
                "cleanup_unconfirmed": self.cleanup_unconfirmed}


def _validate_request(raw):
    value = _json(raw, _REQUEST_CAP)
    fields = {"version", "case", "nonce", "source_root", "memory_root", "case_root",
              "driver_sha256", "caller_sha256", "products_sha256", "python_sha256"}
    if type(value) is not dict or set(value) != fields or type(value["version"]) is not int or value["version"] != 1:
        raise AssertionError("owned fork request schema refused")
    if any(type(value[k]) is not str for k in fields - {"version"}) or value["case"] not in _CASES:
        raise AssertionError("owned fork request types refused")
    for key in ("driver_sha256", "caller_sha256", "products_sha256", "python_sha256"):
        if len(value[key]) != 64 or any(c not in "0123456789abcdef" for c in value[key]):
            raise AssertionError("owned fork digest refused")
    if len(value["nonce"]) != 32 or any(c not in "0123456789abcdef" for c in value["nonce"]):
        raise AssertionError("owned fork nonce refused")
    if sys.argv != [str(Path(__file__)), "--case", value["case"]]:
        raise AssertionError("owned fork invocation refused")
    source = Path(value["source_root"])
    memory = Path(value["memory_root"])
    work = Path(value["case_root"])
    for path in (source, memory, work):
        if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
            raise AssertionError("owned fork path refused")
    if source != _ROOT or work.parent != memory or Path.cwd().resolve() != work:
        raise AssertionError("owned fork root binding refused")
    info = work.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AssertionError("owned fork workspace ownership refused")
    if (work / "result.json").exists():
        raise AssertionError("owned fork result already exists")
    if _hash(_read(Path(__file__), 2 * 1024 * 1024)) != value["driver_sha256"]:
        raise AssertionError("owned fork driver changed")
    if _hash(_read(_ROOT / _CASES[value["case"]], 2 * 1024 * 1024)) != value["caller_sha256"]:
        raise AssertionError("owned fork caller changed")
    if _hash(_read(Path(sys.executable).resolve(), 128 * 1024 * 1024)) != value["python_sha256"]:
        raise AssertionError("owned fork Python changed")
    return value


def _setup(case, root):
    # No pytest session/plugins are initialized in this process.
    import pytest
    from olympus import config, memory, store
    globals().update(pytest=pytest, config=config, memory=memory, store=store)
    config.MEMORY_DIR = root
    memory.set_user("shared")
    store._backend = store.FileStore()
    if case == "owner":
        from olympus import todos, outcomes, routing_outcomes, discovery
        globals().update(todos=todos, outcomes=outcomes,
                         routing_outcomes=routing_outcomes, discovery=discovery,
                         OWNER="lab.a@owned")
    elif case == "vault":
        from olympus import vault_storage as vs
        globals()["vs"] = vs
    elif case == "capture":
        from collections import OrderedDict
        from olympus import prefs, securecapture as capture, vault
        globals().update(prefs=prefs, capture=capture, vault=vault,
                         OWNER="owned-secure-capture")
        capture._WORKFLOWS = OrderedDict()
        capture._ACTIVE = {}
        assert capture._MAX_WORKFLOWS == 64


def _loaded_products(before):
    rows = {}
    for name, module in list(sys.modules.items()):
        if name == "olympus" or name.startswith("olympus."):
            path = Path(module.__file__).resolve(strict=True)
            relative = path.relative_to(_ROOT).as_posix()
            raw = _read(path, 4 * 1024 * 1024)
            if before.get(relative) != _hash(raw):
                raise AssertionError("loaded owned product binding changed")
            rows[name] = {"path": relative, "sha256": before[relative]}
    if not rows or len(rows) > 512:
        raise AssertionError("loaded owned product inventory refused")
    return rows


def _driver_main():
    global _GUARD
    if not sys.platform.startswith("linux") or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise AssertionError("owned fork driver startup refused")
    raw = sys.stdin.buffer.read(_REQUEST_CAP + 1)
    request = _validate_request(raw)
    before, digest = _products()
    if digest != request["products_sha256"]:
        raise AssertionError("owned fork product source changed")
    sys.path.insert(0, str(_ROOT))
    guard = _GUARD = _Forks(request["case"])
    guard.install()
    primary = None
    measurement = None
    bindings = {}
    recorded = []
    observation_failed = False
    import warnings
    original_warning = warnings.showwarning
    def observe_warning(message, category, filename, lineno, file=None, line=None):
        nonlocal observation_failed
        # Normal warning emission remains enabled in both the driver and fork
        # children. No filters are changed; children still emit to owned stderr.
        original_warning(message, category, filename, lineno, file=file, line=line)
        try:
            text = str(message)
            if len(recorded) >= 32 or len(text) > 16384:
                observation_failed = True
            else:
                recorded.append({"category": category.__name__,
                    "message_sha256": _hash(text.encode("utf-8", "strict"))})
        except Exception:
            observation_failed = True
    warnings.showwarning = observe_warning
    def deadline(signum, frame):
        raise TimeoutError("owned fork driver deadline or interruption")
    signal.signal(signal.SIGALRM, deadline)
    signal.signal(signal.SIGTERM, deadline)
    signal.alarm(40)
    try:
        _setup(request["case"], Path(request["memory_root"]))
        _single_thread()
        if request["case"] == "owner":
            _case_owner()
        elif request["case"] == "vault":
            _case_vault(Path(request["memory_root"]))
        elif request["case"] == "capture":
            _case_capture(Path(request["memory_root"]), None)
        else:
            measurement = _case_rlimit_measurement()
    except BaseException as error:
        primary = error
    finally:
        try:
            _drain([lambda: signal.alarm(0), guard.close, lambda: _close_resources(primary)], primary)
        except BaseException as error:
            primary = error
    try:
        _recheck_inputs(request)
        bindings = _loaded_products(before)
        after, after_digest = _products()
        if after != before or after_digest != digest:
            raise AssertionError("owned fork source preservation failed")
    except BaseException as error:
        if primary is None:
            primary = error
    warnings.showwarning = original_warning
    if (recorded or observation_failed) and primary is None:
        primary = AssertionError("owned fork driver warning observation refused")
    result = {
        "version": 1, "case": request["case"], "nonce": request["nonce"],
        "request_sha256": _hash(raw), "driver_sha256": request["driver_sha256"],
        "caller_sha256": request["caller_sha256"], "products_sha256": digest,
        "python_sha256": request["python_sha256"], "experiment_returned": primary is None,
        "failure_type": type(primary).__name__ if primary is not None else None,
        "cleanup_unconfirmed": guard.cleanup_unconfirmed or _unknown(primary),
        "forks": guard.facts(), "warnings": recorded,
        "measurement": measurement, "loaded_products": bindings,
        "nested_fork": guard.nested_facts,
    }
    encoded = _canonical(result)
    if len(encoded) > _RESULT_CAP:
        raise AssertionError("owned fork result exceeds its bound")
    primary = _write_result(Path(request["case_root"]) / "result.json", encoded, primary)
    if primary is None:
        return 0
    uncertain_report = BaseException.__dict__["__dict__"].__get__(
        primary, type(primary)).get("_owned_fork_reporting_unconfirmed") is True
    return 3 if _unknown(primary) or uncertain_report else 2


def run_owned_case(case, memory_root, pytest_request):
    """Source-bound Linux subprocess; result closure is independent of success."""
    if not sys.platform.startswith("linux") or type(case) is not str or case not in _CASES:
        raise AssertionError("owned fork isolation is Linux-only")
    root = Path(memory_root).absolute()
    root.mkdir(parents=True, exist_ok=True)
    if root.resolve(strict=True) != root or root.is_symlink():
        raise AssertionError("owned fork memory root refused")
    work = Path(tempfile.mkdtemp(prefix="owned-fork-", dir=root))
    product_rows, digest = _products()
    driver = Path(__file__).resolve(strict=True)
    value = {
        "version": 1, "case": case, "nonce": os.urandom(16).hex(),
        "source_root": str(_ROOT), "memory_root": str(root), "case_root": str(work),
        "driver_sha256": _hash(_read(driver, 2 * 1024 * 1024)),
        "caller_sha256": _hash(_read(_ROOT / _CASES[case], 2 * 1024 * 1024)),
        "products_sha256": digest,
        "python_sha256": _hash(_read(Path(sys.executable).resolve(), 128 * 1024 * 1024)),
    }
    encoded = _canonical(value)
    if len(encoded) > _REQUEST_CAP:
        raise AssertionError("owned fork request exceeds its bound")
    initial = _write_result(work / "request.json", encoded, None)
    if initial is not None:
        raise initial
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(work), "TMPDIR": str(work),
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "GIT_ALLOW_PROTOCOL": "file",
           "OLYMPUS_ENV": "test", "OLYMPUS_MODEL": "claude-opus-4-8"}
    if case == "capture":
        env["OLYMPUS_SECRET_KEY"] = "owned-capture-key-material"
    inputs = output = errors = child = None
    descriptor = None
    primary = None
    uncertain = False
    dispatched = False
    result = None
    try:
        output = (work / "stdout").open("xb")
        errors = (work / "stderr").open("xb")
        inputs = (work / "request.json").open("rb")
        dispatched = True
        child = subprocess.Popen([sys.executable, "-I", "-B", str(driver), "--case", case],
            stdin=inputs, stdout=output, stderr=errors,
            cwd=str(work), env=env, close_fds=True)
        descriptor, start = _pidfd(child.pid)
        child.wait(timeout=_DRIVER_SECONDS)
    except BaseException as error:
        primary = error
    finally:
        if dispatched and child is None:
            uncertain = True  # A failed return does not prove no child exists.
        if child is not None:
            try:
                if child.poll() is None:
                    if descriptor is None:
                        raise AssertionError("owned fork driver identity unavailable")
                    _observe_child(child.pid)
                    signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        uncertain = True  # Forced exit can cut off descendant drain.
                        _observe_child(child.pid)
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                        child.wait(timeout=2)
                if child.returncode is None:
                    raise AssertionError("owned fork driver closure unresolved")
            except BaseException as error:
                uncertain = True
                if primary is None:
                    primary = error
                else:
                    _mark(primary)
        actions = []
        if descriptor is not None:
            owned_fd, descriptor = descriptor, None
            actions.append(lambda: os.close(owned_fd))
        for handle in (inputs, errors, output):
            if handle is not None:
                actions.append(handle.close)
        try:
            _drain(actions, primary)
        except BaseException as error:
            primary = error
        uncertain = uncertain or _unknown(primary)
    # Inspect a nonzero child's bounded closure record too. Body failure with
    # proved cleanup differs from missing evidence or unconfirmed cleanup.
    if child is not None:
        try:
            result = _validated_result(work, value, encoded, product_rows)
            nested = result["nested_fork"]
            if nested is not None and nested["parent_pid"] != child.pid:
                uncertain = True
                raise AssertionError("nested report belongs to another driver")
            forks = result["forks"]
            if child.returncode not in (0, 2) or (child.returncode == 2 and result["experiment_returned"]):
                uncertain = True
            if (result["cleanup_unconfirmed"] or forks["cleanup_unconfirmed"]
                    or forks["reaped_children"] != forks["observed_children"]):
                uncertain = True
            if _read(work / "stdout", _LOG_CAP) or _read(work / "stderr", _LOG_CAP):
                raise AssertionError("owned fork driver emitted unapproved output")
            if _products()[0] != product_rows:
                raise AssertionError("owned fork product source changed")
            _recheck_inputs(value)
            if (child.returncode != 0 or result["experiment_returned"] is not True
                    or result["warnings"] or not result["loaded_products"]):
                raise AssertionError("owned fork experiment failed; bounded evidence retained")
            maximum = 8 if case == "owner" else 1
            if (forks["attempts"] != maximum or forks["observed_children"] != maximum
                    or forks["native_thread_checks"] != maximum
                    or forks["all_thread_checks_one"] is not True):
                raise AssertionError("owned fork measurement count changed")
        except BaseException as error:
            if result is None:
                uncertain = True
            if primary is None:
                primary = error
    if uncertain:
        if primary is None:
            primary = AssertionError("owned fork cleanup requires reconciliation")
        _mark(primary)
        try:
            pytest_request.session.shouldstop = "owned fork cleanup requires reconciliation"
        except BaseException:
            BaseException.__dict__["__dict__"].__get__(primary, type(primary))[
                "_owned_fork_reporting_unconfirmed"] = True
    if primary is not None:
        raise primary
    if result is None:
        raise AssertionError("owned fork result absent")
    return result


# Source-bound experiment bodies are appended by the source-only preparer.



_RESOURCES = []
_GUARD = None


def _recheck_inputs(expected):
    for path, field, cap in ((Path(__file__), 'driver_sha256', 2 * 1024 * 1024),
            (_ROOT / _CASES[expected['case']], 'caller_sha256', 2 * 1024 * 1024),
            (Path(sys.executable).resolve(), 'python_sha256', 128 * 1024 * 1024)):
        if _hash(_read(path, cap)) != expected[field]:
            raise AssertionError('owned fork input preservation failed')


def _retain_processes(processes):
    for process in processes:
        _RESOURCES.append(process)
    return processes


def _retain_pipe(context):
    endpoints = context.Pipe(duplex=False)
    _RESOURCES.extend(endpoints)
    return endpoints


def _close_once(owner):
    for index, retained in enumerate(_RESOURCES):
        if retained is owner:
            del _RESOURCES[index]  # Detach before the one cleanup attempt.
            try:
                owner.close()
            except BaseException as error:
                _mark(error)
                raise
            return


def _close_resources(primary):
    owned, _RESOURCES[:] = list(_RESOURCES), []
    _drain([owner.close for owner in owned], primary)


def _unknown(error):
    return error is not None and BaseException.__dict__["__dict__"].__get__(
        error, type(error)).get("_owned_fork_cleanup_unconfirmed") is True


def _write_result(path, raw, primary):
    handle = None
    try:
        handle = path.open("xb")
        if handle.write(raw) != len(raw):
            raise OSError("owned fork short result write")
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException as error:
        if primary is None:
            primary = error
        BaseException.__dict__["__dict__"].__get__(primary, type(primary))[
            "_owned_fork_reporting_unconfirmed"] = True
    finally:
        if handle is not None:
            try:
                _drain([handle.close], primary)
            except BaseException as error:
                primary = error
    return primary


def _validated_result(work, expected, encoded, products):
    result = _json(_read(work / "result.json", _RESULT_CAP), _RESULT_CAP)
    fields = {"version", "case", "nonce", "request_sha256", "driver_sha256",
              "caller_sha256", "products_sha256", "python_sha256",
              "experiment_returned", "failure_type", "cleanup_unconfirmed",
              "forks", "warnings", "measurement", "loaded_products", "nested_fork"}
    if type(result) is not dict or set(result) != fields:
        raise AssertionError("owned fork result schema refused")
    if type(result["version"]) is not int or result["version"] != 1:
        raise AssertionError("owned fork result version refused")
    for key in ("case", "nonce", "driver_sha256", "caller_sha256", "products_sha256", "python_sha256"):
        if type(result[key]) is not str or result[key] != expected[key]:
            raise AssertionError("owned fork result binding refused")
    if result["request_sha256"] != _hash(encoded):
        raise AssertionError("owned fork request digest refused")
    if type(result["experiment_returned"]) is not bool or type(result["cleanup_unconfirmed"]) is not bool:
        raise AssertionError("owned fork result facts refused")
    failure = result["failure_type"]
    if failure is not None and (type(failure) is not str or not failure.isidentifier() or len(failure) > 128):
        raise AssertionError("owned fork failure category refused")
    if (failure is None) != result["experiment_returned"]:
        raise AssertionError("owned fork outcome contradiction")
    forks = result["forks"]
    if type(forks) is not dict or set(forks) != {"attempts", "observed_children", "reaped_children", "native_thread_checks", "all_thread_checks_one", "cleanup_unconfirmed"}:
        raise AssertionError("owned fork child facts refused")
    maximum = 8 if expected["case"] == "owner" else 1
    for field in ("attempts", "observed_children", "reaped_children", "native_thread_checks"):
        if type(forks[field]) is not int or not 0 <= forks[field] <= maximum:
            raise AssertionError("owned fork child count refused")
    if forks["reaped_children"] > forks["observed_children"] or forks["observed_children"] > forks["attempts"]:
        raise AssertionError("owned fork child count contradiction")
    for field in ("all_thread_checks_one", "cleanup_unconfirmed"):
        if type(forks[field]) is not bool:
            raise AssertionError("owned fork child boolean refused")
    if forks["cleanup_unconfirmed"] and not result["cleanup_unconfirmed"]:
        raise AssertionError("owned fork lost cleanup fact")
    warnings = result["warnings"]
    if type(warnings) is not list or len(warnings) > 32:
        raise AssertionError("owned fork warning facts refused")
    for row in warnings:
        if (type(row) is not dict or set(row) != {"category", "message_sha256"}
                or type(row["category"]) is not str or not row["category"].isidentifier()
                or len(row["category"]) > 128 or type(row["message_sha256"]) is not str
                or len(row["message_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in row["message_sha256"])):
            raise AssertionError("owned fork warning row refused")
    bindings = result["loaded_products"]
    if type(bindings) is not dict or len(bindings) > 512:
        raise AssertionError("owned fork module facts refused")
    for name, row in bindings.items():
        if (type(name) is not str or not (name == "olympus" or name.startswith("olympus."))
                or type(row) is not dict or set(row) != {"path", "sha256"}
                or type(row["path"]) is not str or products.get(row["path"]) != row["sha256"]):
            raise AssertionError("owned fork module binding refused")
    nested = result["nested_fork"]
    if expected["case"] != "rlimit":
        if nested is not None:
            raise AssertionError("unexpected nested fork report")
    elif nested is not None:
        _checked_nested(nested)
        if nested["cleanup_unconfirmed"] and not result["cleanup_unconfirmed"]:
            raise AssertionError("nested fork uncertainty was lost")
        if result["experiment_returned"] and nested["failure_type"] is not None:
            raise AssertionError("failed nested fork cannot become success")
    elif result["experiment_returned"] or (forks["observed_children"] and not result["cleanup_unconfirmed"]):
        raise AssertionError("nested fork report absent without uncertainty")
    measurement = result["measurement"]
    if expected["case"] == "rlimit" and result["experiment_returned"]:
        if (type(measurement) is not dict or set(measurement) != {"uid", "same_uid", "outcome"}
                or type(measurement["uid"]) is not int or measurement["uid"] != os.getuid()
                or type(measurement["same_uid"]) is not int or not 0 <= measurement["same_uid"] <= 1_000_000
                or measurement["outcome"] not in ("forked", "blocked", "?")):
            raise AssertionError("owned fork measurement refused")
        if measurement["outcome"] != nested["outcome"]:
            raise AssertionError("nested fork measurement changed")
    elif measurement is not None:
        raise AssertionError("unexpected owned fork measurement")
    return result


def route(owner, rid):
    return routing_outcomes.record_run(owner, rid, "mock research", ["argus"],
        models={"argus": "owned-mock-model"}, roles={"argus": "reasoning"},
        review_verdict="approve", synthetic=True)


def _transaction_worker(directory, index):
    from pathlib import Path
    config.MEMORY_DIR = Path(directory)
    store.reset()
    todos.add(OWNER, f"owned concurrent {index}")
    outcomes.record(OWNER, f"mock-{index}", outcomes.APPROVED)
    route(OWNER, f"mock-{index}")
    discovery.note_gap("knowledge", "one topic", user=OWNER)


def _case_owner():
    ctx = multiprocessing.get_context('fork')
    processes = _retain_processes([ctx.Process(target=_transaction_worker, args=(str(config.MEMORY_DIR), i)) for i in range(8)])
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert not process.is_alive() and process.exitcode == 0
    assert len(todos.listing(OWNER)) == len(outcomes.events(OWNER)) == 8
    assert len(routing_outcomes.events(OWNER)) == 8
    assert discovery.open_gaps(user=OWNER)[0]['hits'] == 8

def _inherited_worker(backend, root, tx, sender):
    results = []
    for action in ('read', 'close', 'nested'):
        try:
            if action == 'read':
                tx.get('vault', 'key')
            elif action == 'close':
                tx.close()
            else:
                with vs.transaction(backend, root, timeout=0.1):
                    pass
        except vs.StorageError:
            results.append(action)
    sender.send(results)
    sender.close()


def _case_vault(tmp_path):
    ctx = multiprocessing.get_context('fork')
    receiver, sender = _retain_pipe(ctx)
    backend = store.FileStore()
    with vs.transaction(backend, tmp_path) as tx:
        tx.put('vault', 'key', b'preserved')
        child = _retain_processes([ctx.Process(target=_inherited_worker, args=(backend, tmp_path, tx, sender))])[0]
        primary = None
        try:
            child.start()
            _close_once(sender)
            assert receiver.poll(10)
            assert receiver.recv() == ['read', 'close', 'nested']
            child.join(10)
            assert child.exitcode == 0
            assert tx.get('vault', 'key') == b'preserved'
        except BaseException as error:
            primary = error
            raise
        finally:
            _drain([_GUARD.close, lambda: _close_once(receiver)], primary)


def forbidden(*args, **kwargs):
    pytest.fail('refused workflow reached a forbidden callback')


def _case_capture(owned, monkeypatch):
    import signal
    completed = capture.begin(OWNER)

    class PoisonLock:

        def __enter__(self):
            raise AssertionError('fork entered inherited registry lock')

        def __exit__(self, *args):
            raise AssertionError('fork exited inherited registry lock')
    import time
    child, reaped, child_status = (None, False, None)

    def reap(deadline):
        nonlocal reaped, child_status
        while True:
            observed, status = os.waitpid(child, os.WNOHANG)
            if observed == child:
                reaped, child_status = (True, status)
                return
            assert observed == 0
            if time.monotonic() >= deadline:
                raise TimeoutError('owned fork control exceeded parent deadline')
            time.sleep(0.01)
    try:
        child = os.fork()
        if child == 0:
            try:
                signal.signal(signal.SIGALRM, lambda *args: os._exit(124))
                signal.alarm(5)
                capture._LOCK = PoisonLock()
                prefs.capture = forbidden
                vault._snapshot_selection = forbidden
                identity = completed['workflow_id']
                for function, args in ((capture.begin, (OWNER,)), (capture.status, (OWNER, identity)), (capture.active_status, (OWNER,)), (capture.start_prompt, (OWNER, identity)), (capture.settle, (OWNER, identity)), (capture.cancel, (OWNER, identity))):
                    try:
                        function(*args)
                    except capture.SecureCaptureError:
                        continue
                    os._exit(2)
                os._exit(0)
            except BaseException:
                os._exit(3)
        reap(time.monotonic() + 8)
        assert os.waitstatus_to_exitcode(child_status) == 0
    finally:
        _drain([_GUARD.close], sys.exc_info()[1])
    assert capture.active_status(OWNER) == completed

_NESTED_CAP = 4096


def _checked_nested(packet, *, pid=None, parent_pid=None, start=None):
    fields = {'version', 'pid', 'parent_pid', 'start_ticks', 'outcome',
              'failure_type', 'cleanup_unconfirmed', 'builtin_refusal', 'forks'}
    if type(packet) is not dict or set(packet) != fields:
        raise AssertionError('nested fork report schema refused')
    if type(packet['version']) is not int or packet['version'] != 1:
        raise AssertionError('nested fork report version refused')
    for name in ('pid', 'parent_pid', 'start_ticks'):
        if type(packet[name]) is not int or not 0 < packet[name] < 2**63:
            raise AssertionError('nested fork report identity refused')
    for name, expected in (('pid', pid), ('parent_pid', parent_pid), ('start_ticks', start)):
        if expected is not None and packet[name] != expected:
            raise AssertionError('nested fork report belongs to another child')
    for name in ('cleanup_unconfirmed', 'builtin_refusal'):
        if type(packet[name]) is not bool:
            raise AssertionError('nested fork report boolean refused')
    failure = packet['failure_type']
    if failure is not None and (type(failure) is not str or not failure.isidentifier() or len(failure) > 128):
        raise AssertionError('nested fork failure category refused')
    if packet['outcome'] is not None and (type(packet['outcome']) is not str or packet['outcome'] not in ('blocked', 'forked')):
        raise AssertionError('nested fork outcome refused')
    facts = packet['forks']
    if type(facts) is not dict or set(facts) != {'attempts', 'observed_children', 'reaped_children',
            'native_thread_checks', 'all_thread_checks_one', 'cleanup_unconfirmed'}:
        raise AssertionError('nested fork closure facts refused')
    for name in ('attempts', 'observed_children', 'reaped_children', 'native_thread_checks'):
        if type(facts[name]) is not int or not 0 <= facts[name] <= 1:
            raise AssertionError('nested fork count refused')
    if facts['reaped_children'] > facts['observed_children'] or facts['observed_children'] > facts['attempts']:
        raise AssertionError('nested fork count contradiction')
    for name in ('all_thread_checks_one', 'cleanup_unconfirmed'):
        if type(facts[name]) is not bool:
            raise AssertionError('nested fork closure boolean refused')
    if facts['cleanup_unconfirmed'] and not packet['cleanup_unconfirmed']:
        raise AssertionError('nested fork cleanup fact lost')
    if failure is None:
        if (packet['cleanup_unconfirmed'] or packet['outcome'] is None
                or facts['attempts'] != 1 or facts['native_thread_checks'] != 1
                or facts['all_thread_checks_one'] is not True
                or facts['reaped_children'] != facts['observed_children']):
            raise AssertionError('nested fork successful outcome lacks closure')
        expected_children = 0 if packet['outcome'] == 'blocked' else 1
        if (facts['observed_children'] != expected_children
                or packet['builtin_refusal'] is not (packet['outcome'] == 'blocked')):
            raise AssertionError('nested fork refusal or publication contradiction')
    elif packet['outcome'] is not None:
        raise AssertionError('failed nested fork cannot report a measurement')
    return packet


def _rlimit_nested(resource):
    """Runs in the direct measurement child; never treats cleanup as refusal."""
    identity = {'pid': os.getpid(), 'parent_pid': os.getppid(),
                'start_ticks': _start_ticks(os.getpid())}
    primary = None
    outcome = None
    refused = False
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (2, 2))
        try:
            child = os.fork()
        except OSError as error:
            if error is not _GUARD.builtin_refusal:
                raise
            refused = True
            outcome = 'blocked'
        else:
            if child == 0:
                os._exit(0)
            os.waitpid(child, 0)
            outcome = 'forked'
    except BaseException as error:
        primary = error
    finally:
        try:
            _drain([_GUARD.close], primary)
        except BaseException as error:
            primary = error
    packet = dict(identity, version=1, outcome=outcome if primary is None else None,
        failure_type=type(primary).__name__ if primary is not None else None,
        cleanup_unconfirmed=_GUARD.cleanup_unconfirmed or _unknown(primary),
        builtin_refusal=refused, forks=_GUARD.facts())
    return primary, packet


def _rlimit_measurement_child(read_fd, write_fd, resource):
    """One bounded pipe report, emitted only after independent nested drain."""
    primary = None
    packet = None
    try:
        closing, read_fd = read_fd, None
        os.close(closing)
        primary, packet = _rlimit_nested(resource)
        encoded = _canonical(packet)
        if len(encoded) > _NESTED_CAP:
            raise AssertionError('nested fork report exceeds its bound')
        if os.write(write_fd, encoded) != len(encoded):
            raise OSError('nested fork report short write')
    except BaseException as error:
        if primary is None:
            primary = error
        _mark(primary)  # No completed bounded report/close acknowledgment.
    finally:
        descriptors = [fd for fd in (read_fd, write_fd) if fd is not None]
        read_fd = write_fd = None
        try:
            _drain([_GUARD.close] + [lambda fd=fd: os.close(fd) for fd in descriptors], primary)
        except BaseException as error:
            primary = error
    # A write/close failure after encoding cannot reuse an earlier clean fact.
    return 3 if _unknown(primary) or _GUARD.cleanup_unconfirmed else (2 if primary is not None else 0)


def _read_nested_pipe(descriptor):
    chunks = []
    total = 0
    while True:
        part = os.read(descriptor, _NESTED_CAP + 1 - total)
        if not part:
            break
        total += len(part)
        if total > _NESTED_CAP:
            raise AssertionError('nested fork report exceeds its bound')
        chunks.append(part)
    return _json(b''.join(chunks), _NESTED_CAP)


def _admit_nested_result(packet, pid, status):
    row = _GUARD.children[pid]
    if row['start'] is None:
        raise AssertionError('nested measurement child identity unavailable')
    packet = _checked_nested(packet, pid=pid, parent_pid=os.getpid(), start=row['start'])
    _GUARD.nested_facts = packet
    exitcode = os.waitstatus_to_exitcode(status)
    if (packet['cleanup_unconfirmed'] or packet['forks']['cleanup_unconfirmed']
            or packet['forks']['reaped_children'] != packet['forks']['observed_children']
            or exitcode not in (0, 2)
            or ((exitcode == 0) != (packet['failure_type'] is None))):
        _GUARD.cleanup_unconfirmed = True
    if exitcode != 0 or packet['failure_type'] is not None:
        raise AssertionError('nested fork measurement failed; fixed child facts retained')
    if _GUARD.cleanup_unconfirmed:
        raise AssertionError('nested fork closure is not confirmed')
    return packet['outcome']


def _case_rlimit_measurement():
    import resource
    uid = os.getuid()
    same_uid = 0
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid == uid:
                same_uid += 1
        except OSError:
            continue
    read_fd, write_fd = os.pipe()
    primary = None
    pid = None
    try:
        pid = os.fork()
        if pid == 0:
            os._exit(_rlimit_measurement_child(read_fd, write_fd, resource))
        closing, write_fd = write_fd, None
        os.close(closing)
        packet = _read_nested_pipe(read_fd)
        closing, read_fd = read_fd, None
        os.close(closing)
        observed, status = os.waitpid(pid, 0)
        if observed != pid:
            raise AssertionError('nested measurement wait did not reap its child')
        outcome = _admit_nested_result(packet, pid, status)
        return {'uid': uid, 'same_uid': same_uid, 'outcome': outcome}
    except BaseException as error:
        primary = error
        if pid is not None and pid > 0 and _GUARD.nested_facts is None:
            # Missing/malformed report cannot prove nested-child closure.
            _GUARD.cleanup_unconfirmed = True
        if _GUARD.cleanup_unconfirmed:
            _mark(primary)
        raise
    finally:
        descriptors = [fd for fd in (read_fd, write_fd) if fd is not None]
        read_fd = write_fd = None
        _drain([lambda fd=fd: os.close(fd) for fd in descriptors], primary)


if __name__ == "__main__":
    raise SystemExit(_driver_main())
