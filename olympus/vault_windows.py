"""Vault-directory Windows acquisition with explicit cleanup uncertainty.

ABI structures and non-owning primitives remain the gallery implementations.
Only vault_native.Directory and its transaction owners use these adapters.
"""
import ctypes as C
import os
from . import native_cleanup, vault_storage as storage
from .gallery_windows import (
    HANDLE, IoStatus, ObjectAttributes, RenameHeader, UnicodeString, NativeDirectory as _BaseDirectory,
    _apis, _component, _error, close, information, rename_same_directory,
)


def _valid_handle(handle):
    return type(handle) is int and handle not in (0, -1, HANDLE(-1).value)


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
    returned = False
    code = None
    try:
        code = nt.NtCreateFile(C.byref(result), access, C.byref(attributes), C.byref(status),
                               None, 0, 3, disposition, options, None, 0)
        returned = True
        if type(code) is not int:
            raise storage.StorageError('native acquisition status was not confirmed')
        if code < 0:
            _error(code)
        if not _valid_handle(result.value):
            raise storage.StorageError('native acquisition returned no valid handle')
        information(result.value, directory)
        return result.value
    except BaseException as primary:
        owned, result.value = result.value, None
        if _valid_handle(owned):
            native_cleanup.drain([lambda: close(owned)], primary)
            normalized = storage._acquired_error(primary)
            if normalized is not primary:
                raise normalized from primary
        elif not returned or type(code) is not int or code >= 0:
            # No resource number was observed and no failed status confirmed.
            native_cleanup.mark(primary)
        raise


def _anchor(path):
    kernel, _ = _apis()
    text = str(path)
    if not text.startswith('\\\\?\\'):
        text = '\\\\?\\UNC\\' + text[2:] if text.startswith('\\\\') else '\\\\?\\' + text
    try:
        handle = kernel.CreateFileW(text, 0x001000A1, 3, None, 3, 0x02200000, None)
    except BaseException as primary:
        # The return was not observed. Do not guess whether a resource exists
        # or try to recover its numeric value; the outcome needs reconciliation.
        native_cleanup.mark(primary)
        raise
    if handle == HANDLE(-1).value:
        raise C.WinError(C.get_last_error())
    if not _valid_handle(handle):
        error = storage.StorageError('native anchor returned no valid handle')
        native_cleanup.mark(error)
        raise error
    try:
        information(handle, True)
        return handle
    except BaseException as primary:
        owned, handle = handle, None
        native_cleanup.drain([lambda: close(owned)], primary)
        normalized = storage._acquired_error(primary)
        if normalized is not primary:
            raise normalized from primary
        raise


def to_fd(handle, flags):
    if not _valid_handle(handle):
        raise storage.StorageError('native descriptor requires a valid handle')
    try:
        import msvcrt
        # Complete local preparation while raw ownership is still known.
        mode = os.O_RDWR if flags & os.O_RDWR else (os.O_WRONLY if flags & os.O_WRONLY else os.O_RDONLY)
        mode |= os.O_BINARY
    except BaseException as primary:
        owned, handle = handle, None
        native_cleanup.drain([lambda: close(owned)], primary)
        normalized = storage._acquired_error(primary)
        if normalized is not primary:
            raise normalized from primary
        raise
    # Once dispatched, an exceptional or invalid return cannot establish that
    # the raw HANDLE still belongs to us. It may already belong to a CRT fd.
    # Retain uncertainty; a raw close/retry could close a reused numeric handle.
    try:
        fd = msvcrt.open_osfhandle(handle, mode)
        if type(fd) is not int or fd < 0:
            raise storage.StorageError('native descriptor transfer was not confirmed')
        return fd
    except BaseException as primary:
        native_cleanup.mark(primary)
        raise


class NativeDirectory(_BaseDirectory):
    """Constructed and closed only by the vault storage resource owner."""
    def __init__(self, *args, **kwargs):
        raise RuntimeError('vault storage owns native directory construction')

    def child(self, *args, **kwargs):
        raise RuntimeError('vault storage owns native child construction')

    def close(self):
        raise RuntimeError('vault storage owns native directory cleanup')

    def open_fd(self, name, flags):
        self.owner.check()
        try:
            handle = open_relative(self.handle, name, create=bool(flags & os.O_CREAT),
                                   exclusive=bool(flags & os.O_EXCL), write=bool(flags & (os.O_RDWR | os.O_WRONLY)))
            return to_fd(handle, flags)
        except BaseException as error:
            if native_cleanup.unknown(error):
                self.owner._cleanup_unconfirmed = True
            raise



def entry_information(directory, name, attributes):
    directory.check()
    handle = open_relative(directory.handle, name, directory=bool(attributes & 0x10))
    primary = None
    try:
        return information(handle)
    except BaseException as error:
        primary = storage._acquired_error(error)
        if primary is not error:
            raise primary from error
        raise
    finally:
        owned, handle = handle, None
        native_cleanup.drain([lambda: close(owned)], primary)
