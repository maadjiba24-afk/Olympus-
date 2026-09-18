"""A denied rename must not replay an observation or discard pending bytes.

Injected errors exercise the bounded policy on every platform. The Windows
cases separately hold a real file handle that denies replacement.
"""
import contextlib
import ctypes
import errno
import json
import os

import pytest

from olympus import atomicio, ctxbudget


def _denied(code):
    error = PermissionError(errno.EACCES, "owned rename denial")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32, 33])
def test_transient_rename_publishes_one_observation_without_rewriting(tmp_path, monkeypatch, code):
    assert ctxbudget.observe("owned", "model", 400, 100)
    path = ctxbudget._cal_path()
    prior = path.read_bytes()
    abandoned = path.with_name(path.name + ".tmp")
    abandoned.write_bytes(b"preserve earlier interrupted publication")
    replace, sleep = os.replace, []
    attempts = []
    def deny_once(source, destination):
        attempts.append((source, source.stat().st_mtime_ns, source.read_bytes()))
        assert destination == path
        if len(attempts) == 1:
            assert path.read_bytes() == prior
            raise _denied(code)
        return replace(source, destination)
    monkeypatch.setattr(atomicio, "_WINDOWS", True)
    monkeypatch.setattr(ctxbudget.os, "replace", deny_once)
    monkeypatch.setattr(ctxbudget.time, "sleep", sleep.append)
    assert ctxbudget.observe("owned", "model", 500, 100)
    assert attempts[0] == attempts[1]
    assert len(attempts) == 2 and sleep == [0.01]
    row = json.loads(path.read_text())["ratios"]["owned/model"]
    assert row["n"] == 2 and row["cpt"] == pytest.approx(4.2)
    assert abandoned.read_bytes() == b"preserve earlier interrupted publication"
    assert not attempts[0][0].exists()


@pytest.mark.parametrize("code,windows,attempts", [(5, True, 6), (32, True, 6),
    (33, True, 6), (87, True, 1), (None, True, 1), (5, False, 1)])
def test_permanent_denial_preserves_old_and_pending_bytes(monkeypatch, code, windows, attempts):
    assert ctxbudget.observe("owned", "model", 400, 100)
    path = ctxbudget._cal_path()
    before = path.read_bytes()
    calls, sleeps = [], []
    failure = _denied(code)
    def deny(source, destination):
        calls.append((source, source.read_bytes()))
        raise failure
    monkeypatch.setattr(atomicio, "_WINDOWS", windows)
    monkeypatch.setattr(ctxbudget.os, "replace", deny)
    monkeypatch.setattr(ctxbudget.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError) as caught:
        ctxbudget.observe("owned", "model", 500, 100)
    assert caught.value is failure
    assert len(calls) == attempts and len(set(calls)) == 1
    assert sum(sleeps) <= .31
    assert path.read_bytes() == before
    assert calls[0][0].read_bytes() == calls[0][1]
    pending = json.loads(calls[0][1])
    assert pending["ratios"]["owned/model"]["n"] == 2
    assert json.loads(before)["ratios"]["owned/model"]["n"] == 1


def test_other_replace_errors_are_not_retried(monkeypatch):
    calls = []
    def fail(source, destination):
        calls.append(source)
        raise OSError(errno.ENOSPC, "owned full device")
    monkeypatch.setattr(ctxbudget.os, "replace", fail)
    with pytest.raises(OSError) as caught:
        ctxbudget._save({"v": 1, "ratios": {}})
    assert caught.value.errno == errno.ENOSPC and len(calls) == 1
    assert calls[0].is_file()
    assert not ctxbudget._cal_path().exists()


def test_default_publisher_keeps_single_attempt_on_windows(tmp_path, monkeypatch):
    target, pending = tmp_path / "state", tmp_path / "pending"
    target.write_bytes(b"old state")
    calls = []
    def deny(source, destination):
        calls.append(source)
        raise _denied(5)
    monkeypatch.setattr(atomicio, "_WINDOWS", True)
    monkeypatch.setattr(atomicio.os, "replace", deny)
    with pytest.raises(PermissionError):
        atomicio.publish(pending, target, b"new state")
    assert len(calls) == 1
    assert target.read_bytes() == b"old state" and pending.read_bytes() == b"new state"


@pytest.mark.parametrize("data", [b"new state", "new state\n"])
def test_exclusive_publisher_preserves_existing_staging(tmp_path, data):
    target, pending = tmp_path / "state", tmp_path / "pending"
    target.write_bytes(b"old state")
    pending.write_bytes(b"earlier interrupted state")
    with pytest.raises(FileExistsError):
        atomicio.publish(pending, target, data, exclusive=True)
    assert target.read_bytes() == b"old state"
    assert pending.read_bytes() == b"earlier interrupted state"


def test_durable_retry_syncs_once_before_replace_and_directory_after(tmp_path, monkeypatch):
    target, pending = tmp_path / "state", tmp_path / "pending"
    target.write_bytes(b"old state")
    events = []
    replace, fsync = os.replace, os.fsync
    def sync(fd):
        events.append("file sync")
        fsync(fd)
    def deny_once(source, destination):
        events.append("replace")
        if events.count("replace") == 1:
            assert target.read_bytes() == b"old state"
            raise _denied(32)
        return replace(source, destination)
    monkeypatch.setattr(atomicio, "_WINDOWS", True)
    monkeypatch.setattr(atomicio.os, "replace", deny_once)
    monkeypatch.setattr(atomicio.os, "fsync", sync)
    monkeypatch.setattr(atomicio, "fsync_dir", lambda path: events.append("directory sync"))
    atomicio.publish(pending, target, b"new state", exclusive=True, retry_windows_sharing=True)
    assert events == ["file sync", "replace", "replace", "directory sync"]
    assert target.read_bytes() == b"new state" and not pending.exists()


@contextlib.contextmanager
def _deny_delete_handle(path):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = (wintypes.HANDLE,), wintypes.BOOL
    handle = create(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    active = True
    def release():
        nonlocal active
        if active:
            assert close(handle), ctypes.get_last_error()
            active = False
    try:
        yield release
    finally:
        release()


@pytest.mark.skipif(os.name != "nt", reason="native Windows file sharing contract")
def test_native_windows_calibration_recovers_after_sharing_handle_release(monkeypatch):
    assert ctxbudget.observe("owned", "model", 400, 100)
    path = ctxbudget._cal_path()
    before = path.read_bytes()
    replace = os.replace
    denials = []
    with _deny_delete_handle(path) as release:
        def observed_replace(source, destination):
            try:
                return replace(source, destination)
            except PermissionError as exc:
                denials.append(exc.winerror)
                assert path.read_bytes() == before
                release()
                raise
        monkeypatch.setattr(ctxbudget.os, "replace", observed_replace)
        assert ctxbudget.observe("owned", "model", 500, 100)
    assert denials and all(code in (5, 32, 33) for code in denials)
    assert json.loads(path.read_text())["ratios"]["owned/model"]["n"] == 2


@pytest.mark.skipif(os.name != "nt", reason="native Windows file sharing contract")
def test_native_windows_calibration_persistent_handle_preserves_both_files():
    assert ctxbudget.observe("owned", "model", 400, 100)
    path = ctxbudget._cal_path()
    before = path.read_bytes()
    with _deny_delete_handle(path):
        with pytest.raises(PermissionError):
            ctxbudget.observe("owned", "model", 500, 100)
    assert path.read_bytes() == before
    pending = list(path.parent.glob(".ctx_calibration.json-*.tmp"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_text())["ratios"]["owned/model"]["n"] == 2
