"""Owned ABI/relative-authority unit checks; NOT native Windows evidence."""
import ctypes as C
from types import SimpleNamespace

import pytest
from olympus import gallery_windows as native


def test_native_open_contract_uses_parent_component_and_noreparse(monkeypatch):
    observed = {}
    def create(result, access, attrs, status, allocation, attributes, sharing, disposition, options, ea, length):
        obj = C.cast(attrs, C.POINTER(native.ObjectAttributes)).contents
        name = obj.ObjectName.contents
        observed.update(parent=obj.RootDirectory, name=C.string_at(name.Buffer, name.Length).decode('utf-16-le'),
                        flags=obj.Attributes, access=access, sharing=sharing, disposition=disposition, options=options)
        C.cast(result, C.POINTER(native.HANDLE)).contents.value = 0x123456789
        return 0
    monkeypatch.setattr(native, '_apis', lambda: (None, SimpleNamespace(NtCreateFile=create)))
    checks = []
    monkeypatch.setattr(native, 'information', lambda handle, directory: checks.append((handle, directory)))
    assert native.open_relative(0xABCDEF123, 'owned.blob', create=True, exclusive=True, write=True) == 0x123456789
    assert observed['parent'] == 0xABCDEF123 and observed['name'] == 'owned.blob'
    assert observed['flags'] & 0x1000
    assert observed['options'] & 0x200000
    assert observed['disposition'] == 2 and observed['sharing'] == 3
    assert checks == [(0x123456789, False)]


@pytest.mark.parametrize('name', ['', '.', '..', '../x', 'a/b', 'a\\b', 'C:x', 'x\0y'])
def test_native_component_refuses_path_authority(name):
    with pytest.raises(OSError):
        native._component(name)


def test_native_rename_uses_open_source_same_parent_basename(monkeypatch):
    observed = {}
    def rename(handle, status, buffer, length, info_class):
        header = native.RenameHeader.from_buffer(buffer)
        observed.update(handle=handle, root=header.RootDirectory, replace=header.ReplaceIfExists,
                        name=C.string_at(C.addressof(buffer) + native.RenameHeader.FileName.offset,
                                         header.FileNameLength).decode('utf-16-le'),
                        length=length, name_length=header.FileNameLength, info_class=info_class)
        return 0
    monkeypatch.setattr(native, '_apis', lambda: (None, SimpleNamespace(NtSetInformationFile=rename)))
    native.rename_same_directory(0x123456789, 'state.json')
    assert observed['handle'] == 0x123456789 and observed['root'] is None
    assert observed['name'] == 'state.json' and observed['replace'] == 1 and observed['info_class'] == 10
    assert observed['length'] >= C.sizeof(native.RenameHeader) + observed['name_length']


def test_native_failed_handle_validation_closes_same_handle(monkeypatch):
    def create(result, *args):
        C.cast(result, C.POINTER(native.HANDLE)).contents.value = 0x123456789
        return 0
    monkeypatch.setattr(native, '_apis', lambda: (None, SimpleNamespace(NtCreateFile=create)))
    def unsafe(*args):
        raise OSError('owned reparse evidence')
    monkeypatch.setattr(native, 'information', unsafe)
    closed = []
    monkeypatch.setattr(native, 'close', closed.append)
    with pytest.raises(OSError):
        native.open_relative(0xABC, 'owned')
    assert closed == [0x123456789]
