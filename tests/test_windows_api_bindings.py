"""Binding cache controls; actual filesystem adversarial tests stay separate."""
import threading
from types import SimpleNamespace

import pytest

from olympus import gallery_windows as native


class Function:
    def __init__(self, name, implementation=None):
        self.name = name
        self.implementation = implementation
        self.argtypes = self.restype = None

    def __call__(self, *args):
        assert self.argtypes is not None and self.restype is not None
        return 1 if self.implementation is None else self.implementation(*args)


class Library:
    def __init__(self, name):
        self.name = name
        self.functions = {}

    def __getattr__(self, name):
        if name not in self.functions:
            self.functions[name] = Function(name)
        return self.functions[name]


@pytest.fixture
def bindings(monkeypatch):
    monkeypatch.setattr(native, '_API_BINDINGS', None)
    calls = []

    def load(name, **kwargs):
        calls.append((name, kwargs))
        return Library(name)

    monkeypatch.setattr(native.C, 'WinDLL', load, raising=False)
    return calls


def configured(pair):
    kernel, nt = pair
    assert kernel.name == 'kernel32' and nt.name == 'ntdll'
    assert set(kernel.functions) == {'CreateFileW','CloseHandle','GetFileInformationByHandle',
                                     'GetFileInformationByHandleEx','GetFileType'}
    assert set(nt.functions) == {'NtCreateFile','NtSetInformationFile','NtQueryVolumeInformationFile',
                                 'RtlNtStatusToDosError'}
    assert all(fn.argtypes is not None and fn.restype is not None
               for library in pair for fn in library.functions.values())


def test_successful_bindings_are_reused_with_original_last_error_contract(bindings):
    first = native._apis()
    configured(first)
    assert all(native._apis() is first for _ in range(100))
    assert bindings == [('kernel32', {'use_last_error':True}), ('ntdll', {})]
    from olympus import prefs_windows, vault_windows
    assert prefs_windows._apis() is first and vault_windows._apis() is first


@pytest.mark.parametrize('fault', [RuntimeError,KeyboardInterrupt,SystemExit])
@pytest.mark.parametrize('stage', ['kernel','nt','signature'])
def test_incomplete_binding_is_never_cached_and_preserves_actual_failure(bindings, monkeypatch, fault, stage):
    primary = fault('owned binding initialization failure')
    original = native.C.WinDLL
    once = [True]

    def load(name, **kwargs):
        if once[0] and ((stage == 'kernel' and name == 'kernel32')
                        or (stage == 'nt' and name == 'ntdll')):
            once[0] = False
            raise primary
        library = original(name, **kwargs)
        if once[0] and stage == 'signature' and name == 'kernel32':
            once[0] = False
            class Rejecting:
                def __setattr__(self, name, value):
                    raise primary
            library.functions['GetFileInformationByHandle'] = Rejecting()
        return library

    monkeypatch.setattr(native.C, 'WinDLL', load)
    with pytest.raises(fault) as caught:
        native._apis()
    assert caught.value is primary and native._API_BINDINGS is None
    second = native._apis()
    configured(second)
    assert native._apis() is second


def test_process_change_rebinds_without_inheriting_a_lock(bindings, monkeypatch):
    pid = [100]
    monkeypatch.setattr(native.os, 'getpid', lambda: pid[0])
    original = native._apis()
    pid[0] = 101
    inherited_process = native._apis()
    assert inherited_process is not original
    configured(inherited_process)
    assert native._apis() is inherited_process and len(bindings) == 4


def test_concurrent_initialization_publishes_only_complete_pairs(bindings, monkeypatch):
    gate = threading.Barrier(2)
    original = native.C.WinDLL

    def load(name, **kwargs):
        if name == 'kernel32':
            gate.wait(timeout=5)
        return original(name, **kwargs)

    monkeypatch.setattr(native.C, 'WinDLL', load)
    results, errors = [], []

    def work():
        try:
            results.append(native._apis())
        except BaseException as error:
            errors.append(error)

    workers = [threading.Thread(target=work) for _ in range(2)]
    import time
    from olympus import native_cleanup
    primary = None
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(6)
    except BaseException as error:
        primary = error
        raise
    finally:
        # start() may raise after the worker acquired an identity. Inspect every
        # constructed worker, abort its only blocking gate, then independently
        # join all that actually started before monkeypatch restores the cache.
        deadline = time.monotonic() + 6
        def joined(worker):
            if worker.ident is not None:
                worker.join(max(0, deadline-time.monotonic()))
        cleanup_failure = None
        try:
            native_cleanup.drain([gate.abort,
                *[lambda worker=worker: joined(worker) for worker in workers]], primary)
        except BaseException as error:
            cleanup_failure = error
        if any(worker.is_alive() for worker in workers):
            pytest.exit('owned binding initializer thread remains live', returncode=2)
        if cleanup_failure is not None:
            raise cleanup_failure
    assert errors == [] and len(results) == 2 and len(bindings) == 4
    for pair in results:
        configured(pair)
    assert any(native._apis() is pair for pair in results)


def test_reused_bindings_still_query_current_handle_information_each_time(bindings):
    pair = native._apis()
    observed = []

    def info(handle, destination):
        observed.append(('info', handle))
        value = destination._obj
        value.attributes = 0x10
        value.volume = 7
        value.index_low = len(observed)
        value.links = 1
        return 1

    def kind(handle):
        observed.append(('type', handle))
        return 1

    pair[0].GetFileInformationByHandle.implementation = info
    pair[0].GetFileType.implementation = kind
    first = native.information(123, True)
    second = native.information(123, True)
    assert first.st_ino == 1 and second.st_ino == 3
    assert observed == [('info',123),('type',123),('info',123),('type',123)]
    assert len(bindings) == 2
    pair[0].GetFileType.implementation = lambda handle: 0
    with pytest.raises(OSError, match='unsafe native gallery handle identity'):
        native.information(123, True)
    assert len([event for event in observed if event[0] == 'info']) == 3


def test_reused_binding_keeps_current_thread_error_result(bindings, monkeypatch):
    pair = native._apis()
    pair[0].GetFileInformationByHandle.implementation = lambda *args: 0
    code = [101]
    observed = []
    monkeypatch.setattr(native.C, 'get_last_error', lambda: code[0], raising=False)

    def win_error(number):
        observed.append(number)
        return OSError('owned native failure '+str(number))

    monkeypatch.setattr(native.C, 'WinError', win_error, raising=False)
    with pytest.raises(OSError, match='101'):
        native.information(123, True)
    code[0] = 202
    with pytest.raises(OSError, match='202'):
        native.information(123, True)
    assert observed == [101,202] and len(bindings) == 2



def _information_validation_probe(bindings):
    pair = native._apis()
    state = {"attributes": 16, "kind": 1, "links": 1, "status": 1}
    events = []

    def info(handle, destination):
        events.append(("info", handle))
        value = destination._obj
        value.attributes = state["attributes"]
        value.links = state["links"]
        value.volume = 7
        value.index_high, value.index_low = 1, 9
        value.size_high, value.size_low = 2, 11
        ticks = 116444736000000000 + 123456
        value.write.high, value.write.low = ticks >> 32, ticks & 0xffffffff
        return state["status"]

    def kind(handle):
        events.append(("type", handle))
        return state["kind"]

    pair[0].GetFileInformationByHandle.implementation = info
    pair[0].GetFileType.implementation = kind
    return pair, state, events


def _information_validation_directory(handles=(123,)):
    # Synthetic handles only: do not acquire, release, or address native resources.
    directory = object.__new__(native.NativeDirectory)
    directory.handles = list(handles)
    return directory


@pytest.mark.parametrize("directory", [None, False, True])
def test_information_keeps_public_signature_and_complete_metadata(bindings, directory):
    import inspect
    import stat
    _pair, state, events = _information_validation_probe(bindings)
    is_directory = directory is True
    state["attributes"] = 16 if is_directory else 0
    state["links"] = 2 if is_directory else 1
    signature = inspect.signature(native.information)
    assert tuple(signature.parameters) == ("handle", "directory")
    assert signature.parameters["handle"].default is inspect.Parameter.empty
    assert signature.parameters["directory"].default is None
    assert all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
               for p in signature.parameters.values())
    result = native.information(123, directory)
    assert type(result) is SimpleNamespace
    assert vars(result) == {
        "st_mode": (stat.S_IFDIR if is_directory else stat.S_IFREG) | 0o600,
        "st_file_attributes": state["attributes"], "st_nlink": state["links"],
        "st_dev": 7, "st_ino": (1 << 32) | 9,
        "st_size": (2 << 32) | 11, "st_mtime_ns": 12345600,
    }
    assert events == [("info", 123), ("type", 123)]
    if not is_directory:
        state["links"] = 2
        with pytest.raises(OSError, match="unsafe native gallery handle identity"):
            native.information(123, directory)
        assert events == [("info", 123), ("type", 123)] * 2


def test_directory_validation_keeps_every_native_query_without_metadata_formatting(bindings, monkeypatch):
    _pair, state, events = _information_validation_probe(bindings)
    directory = _information_validation_directory((11, 22))

    def unused_formatter(**values):
        raise AssertionError("directory-only validation must not construct metadata")

    monkeypatch.setattr(native, "SimpleNamespace", unused_formatter)
    assert directory.check() is None
    assert directory.check() is None
    assert events == [("info", 11), ("type", 11), ("info", 22), ("type", 22)] * 2
    state["attributes"] |= 0x400
    with pytest.raises(OSError, match="unsafe native gallery handle identity"):
        directory.check()
    assert events[-2:] == [("info", 11), ("type", 11)] and len(events) == 10
    assert directory.handles == [11, 22]


@pytest.mark.parametrize("unsafe", ["file_type", "reparse", "not_directory"])
def test_directory_validation_refuses_each_native_safety_violation(bindings, unsafe):
    _pair, state, events = _information_validation_probe(bindings)
    if unsafe == "file_type":
        state["kind"] = 0
    elif unsafe == "reparse":
        state["attributes"] |= 0x400
    else:
        state["attributes"] = 0
    directory = _information_validation_directory()
    with pytest.raises(OSError, match="unsafe native gallery handle identity"):
        directory.check()
    assert events == [("info", 123), ("type", 123)]
    assert directory.handles == [123]


@pytest.mark.parametrize("entry", ["public", "directory_check"])
@pytest.mark.parametrize("stage", ["information", "type"])
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt, SystemExit])
def test_information_paths_preserve_native_primary_and_stop_order(bindings, monkeypatch, entry, stage, failure):
    pair, _state, events = _information_validation_probe(bindings)
    target = pair[0].GetFileInformationByHandle if stage == "information" else pair[0].GetFileType
    original = target.implementation
    primary = failure("owned native validation fault")

    def fail(*args):
        original(*args)
        raise primary

    target.implementation = fail
    closed = []
    monkeypatch.setattr(native, "close", closed.append)
    directory = _information_validation_directory((123, 456))
    with pytest.raises(failure) as caught:
        if entry == "public":
            native.information(123, True)
        else:
            directory.check()
    assert caught.value is primary
    assert events == ([("info", 123)] if stage == "information"
                      else [("info", 123), ("type", 123)])
    assert closed == [] and directory.handles == [123, 456]


@pytest.mark.parametrize("failure", [None, OSError, KeyboardInterrupt, SystemExit])
def test_directory_validation_honors_public_information_replacements(bindings, monkeypatch, failure):
    _pair, _state, events = _information_validation_probe(bindings)
    original = native.information
    calls = []
    primary = failure("owned information replacement fault") if failure else None

    def replacement(handle, directory=None):
        calls.append((handle, directory))
        result = original(handle, directory)
        assert type(result) is SimpleNamespace and result.st_dev == 7
        if primary is not None:
            raise primary
        return result

    monkeypatch.setattr(native, "information", replacement)
    directory = _information_validation_directory((123, 456))
    if primary is not None:
        with pytest.raises(failure) as caught:
            directory.check()
        assert caught.value is primary
        assert calls == [(123, True)]
        assert events == [("info", 123), ("type", 123)]
    else:
        assert directory.check() is None
        assert calls == [(123, True), (456, True)]
        assert events == [("info", 123), ("type", 123), ("info", 456), ("type", 456)]


def test_directory_validation_observes_replacement_before_next_handle(bindings, monkeypatch):
    pair, _state, events = _information_validation_probe(bindings)
    primary = KeyboardInterrupt("owned next-handle replacement")
    replaced = []

    def replacement(handle, directory=None):
        replaced.append((handle, directory))
        raise primary

    original_type = pair[0].GetFileType.implementation

    def kind(handle):
        result = original_type(handle)
        monkeypatch.setattr(native, "information", replacement)
        return result

    pair[0].GetFileType.implementation = kind
    directory = _information_validation_directory((123, 456))
    with pytest.raises(KeyboardInterrupt) as caught:
        directory.check()
    assert caught.value is primary and replaced == [(456, True)]
    assert events == [("info", 123), ("type", 123)]


def test_directory_validation_preserves_current_native_error_without_type_query(bindings, monkeypatch):
    _pair, state, events = _information_validation_probe(bindings)
    state["status"] = 0
    code = [101]
    errors = {101: OSError("owned native error one"), 202: OSError("owned native error two")}
    failures = []
    monkeypatch.setattr(native.C, "get_last_error", lambda: code[0], raising=False)

    def win_error(number):
        failures.append(number)
        return errors[number]

    monkeypatch.setattr(native.C, "WinError", win_error, raising=False)
    directory = _information_validation_directory()
    for number in (101, 202):
        code[0] = number
        with pytest.raises(OSError) as caught:
            directory.check()
        assert caught.value is errors[number]
    assert events == [("info", 123), ("info", 123)]
    assert failures == [101, 202]
