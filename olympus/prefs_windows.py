"""Preference-local Windows acquisition with explicit cleanup uncertainty.

Only preference storage uses these adapters. Existing gallery, vault and
archive acquisition/absence contracts remain unchanged. ABI structures and
non-owning native primitives remain the reviewed gallery implementations.
"""
import ctypes as C
import os
from . import native_cleanup
from .gallery_windows import (
    HANDLE, IoStatus, ObjectAttributes, RenameHeader, UnicodeString, NativeDirectory as _BaseDirectory,
    _apis, _component, _error, close, information, rename_same_directory,
)


def open_relative(parent, name, *, directory=False, create=False, exclusive=False,
                  write=False, delete=False):
    _component(name)
    _, nt = _apis()
    raw = name.encode('utf-16-le', 'strict')
    buffer = C.create_string_buffer(raw + b'\0\0')
    unicode = UnicodeString(len(raw), len(raw) + 2, C.cast(buffer, C.c_void_p))
    attributes = ObjectAttributes(C.sizeof(ObjectAttributes), parent, C.pointer(unicode),
                                  0x40 | 0x1000, None, None)  # CASE_INSENSITIVE | DONT_REPARSE
    result = HANDLE()
    status = IoStatus()
    # Directory traverse/read-attribute/list; READ_CONTROL isn't required.
    access = (0x001000A1 if directory else 0x80100000)
    if write:
        access |= 0x40000000
    if delete:
        access |= 0x00010000
    disposition = 2 if exclusive else (3 if create else 1)  # CREATE / OPEN_IF / OPEN
    options = 0x00200000 | 0x20 | (1 if directory else 0x40)
    code = nt.NtCreateFile(C.byref(result), access, C.byref(attributes), C.byref(status),
                           None, 0, 3, disposition, options, None, 0)
    if code < 0:
        _error(code)
    try:
        information(result.value, directory)
        return result.value
    except BaseException as primary:
        owned, result.value = result.value, None
        native_cleanup.drain([lambda: close(owned)], primary)
        raise


def _anchor(path):
    kernel, _ = _apis()
    text = str(path)
    if not text.startswith('\\\\?\\'):
        text = '\\\\?\\UNC\\' + text[2:] if text.startswith('\\\\') else '\\\\?\\' + text
    handle = kernel.CreateFileW(text, 0x001000A1, 3, None, 3, 0x02200000, None)
    if handle == HANDLE(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        information(handle, True)
        return handle
    except BaseException as primary:
        owned, handle = handle, None
        native_cleanup.drain([lambda: close(owned)], primary)
        raise


def to_fd(handle, flags):
    import msvcrt
    try:
        # CRT only receives descriptor mode flags, not creation/disposition bits.
        mode = os.O_RDWR if flags & os.O_RDWR else (os.O_WRONLY if flags & os.O_WRONLY else os.O_RDONLY)
        return msvcrt.open_osfhandle(handle, mode | os.O_BINARY)
    except BaseException as primary:
        owned, handle = handle, None
        native_cleanup.drain([lambda: close(owned)], primary)
        raise


class NativeDirectory(_BaseDirectory):
    """Constructed and closed only by the preference storage resource owner."""
    def __init__(self, *args, **kwargs):
        raise RuntimeError('preference storage owns native directory construction')

    def child(self, *args, **kwargs):
        raise RuntimeError('preference storage owns native child construction')

    def close(self):
        raise RuntimeError('preference storage owns native directory cleanup')

    def open_fd(self, name, flags):
        self.check()
        handle = open_relative(self.handle, name, create=bool(flags & os.O_CREAT),
                               exclusive=bool(flags & os.O_EXCL), write=bool(flags & (os.O_RDWR | os.O_WRONLY)))
        return to_fd(handle, flags)



def entry_information(directory, name, attributes):
    directory.check()
    handle = open_relative(directory.handle, name, directory=bool(attributes & 0x10))
    primary = None
    try:
        return information(handle)
    except BaseException as error:
        primary = error
        raise
    finally:
        owned, handle = handle, None
        native_cleanup.drain([lambda: close(owned)], primary)


def rename_exclusive(handle, name):
    """Rename the open source in its existing parent; never resolve a path."""
    _component(name)
    _, nt = _apis()
    raw = name.encode('utf-16-le')
    offset = RenameHeader.FileName.offset
    buffer = C.create_string_buffer(C.sizeof(RenameHeader) + len(raw))
    header = RenameHeader.from_buffer(buffer)
    header.ReplaceIfExists = 0
    header.RootDirectory = None
    header.FileNameLength = len(raw)
    C.memmove(C.addressof(buffer) + offset, raw, len(raw))
    status = IoStatus()
    code = nt.NtSetInformationFile(handle, C.byref(status), buffer, len(buffer), 10)
    if code < 0:
        _error(code)
