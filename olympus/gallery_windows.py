"""Gallery-only Windows handle-relative filesystem primitives.

Attribute mutation is NOT prevented by Windows share flags. Child lookup uses
NtCreateFile with RootDirectory and OBJ_DONT_REPARSE instead. Publication uses
the already-open staging handle and a same-directory basename rename. No
absolute child path, path-based enumeration, or path-based rename is used.

These primitives do not upgrade the single-process Windows store topology or
claim POSIX directory-fsync durability. Native adversarial tests are required.
"""
from __future__ import annotations

import ctypes as C
import os
import stat
import struct
import time
from pathlib import Path
from types import SimpleNamespace

U32 = C.c_uint32
I32 = C.c_int32
U16 = C.c_uint16
U64 = C.c_uint64
I64 = C.c_int64
HANDLE = C.c_void_p


class UnicodeString(C.Structure):
    _fields_ = [('Length', U16), ('MaximumLength', U16), ('Buffer', C.c_void_p)]


class ObjectAttributes(C.Structure):
    _fields_ = [('Length', U32), ('RootDirectory', HANDLE),
                ('ObjectName', C.POINTER(UnicodeString)), ('Attributes', U32),
                ('SecurityDescriptor', C.c_void_p), ('SecurityQualityOfService', C.c_void_p)]


class StatusUnion(C.Union):
    _fields_ = [('Status', I32), ('Pointer', C.c_void_p)]


class IoStatus(C.Structure):
    _fields_ = [('value', StatusUnion), ('Information', C.c_size_t)]


class FileTime(C.Structure):
    _fields_ = [('low', U32), ('high', U32)]


class HandleInfo(C.Structure):
    _fields_ = [('attributes', U32), ('creation', FileTime), ('access', FileTime),
                ('write', FileTime), ('volume', U32), ('size_high', U32),
                ('size_low', U32), ('links', U32), ('index_high', U32), ('index_low', U32)]


class RenameHeader(C.Structure):
    _fields_ = [('ReplaceIfExists', C.c_ubyte), ('RootDirectory', HANDLE),
                ('FileNameLength', U32), ('FileName', U16 * 1)]


class VolumeSize(C.Structure):
    _fields_ = [('total', I64), ('available', I64), ('actual', I64),
                ('sectors', U32), ('sector_bytes', U32)]


def _apis():
    kernel = C.WinDLL('kernel32', use_last_error=True)
    nt = C.WinDLL('ntdll')
    signatures = {
        'CreateFileW': ([C.c_wchar_p, U32, U32, C.c_void_p, U32, U32, HANDLE], HANDLE),
        'CloseHandle': ([HANDLE], I32),
        'GetFileInformationByHandle': ([HANDLE, C.POINTER(HandleInfo)], I32),
        'GetFileInformationByHandleEx': ([HANDLE, I32, C.c_void_p, U32], I32),
        'GetFileType': ([HANDLE], U32),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(kernel, name)
        fn.argtypes, fn.restype = args, result
    nt.NtCreateFile.argtypes = [C.POINTER(HANDLE), U32, C.POINTER(ObjectAttributes),
                                C.POINTER(IoStatus), C.c_void_p, U32, U32,
                                U32, U32, C.c_void_p, U32]
    nt.NtCreateFile.restype = I32
    nt.NtSetInformationFile.argtypes = [HANDLE, C.POINTER(IoStatus), C.c_void_p, U32, I32]
    nt.NtSetInformationFile.restype = I32
    nt.NtQueryVolumeInformationFile.argtypes = [HANDLE, C.POINTER(IoStatus), C.c_void_p, U32, I32]
    nt.NtQueryVolumeInformationFile.restype = I32
    nt.RtlNtStatusToDosError.argtypes, nt.RtlNtStatusToDosError.restype = [I32], U32
    return kernel, nt


def _error(status):
    _, nt = _apis()
    raise C.WinError(nt.RtlNtStatusToDosError(status))


def close(handle):
    kernel, _ = _apis()
    if not kernel.CloseHandle(handle):
        raise C.WinError(C.get_last_error())


def _component(name):
    if (not isinstance(name, str) or name in ('', '.', '..')
            or any(c in name for c in '/\\:\x00') or len(name.encode('utf-16-le')) > 65532):
        raise OSError('unsafe native relative component')


def information(handle, directory=None):
    kernel, _ = _apis()
    info = HandleInfo()
    if not kernel.GetFileInformationByHandle(handle, C.byref(info)):
        raise C.WinError(C.get_last_error())
    is_directory = bool(info.attributes & 0x10)
    if (kernel.GetFileType(handle) != 1 or info.attributes & 0x400
            or directory is not None and is_directory != directory
            or not is_directory and info.links != 1):
        raise OSError('unsafe native gallery handle identity')
    ticks = (info.write.high << 32) | info.write.low
    return SimpleNamespace(st_mode=(stat.S_IFDIR if is_directory else stat.S_IFREG) | 0o600,
                           st_file_attributes=info.attributes, st_nlink=info.links,
                           st_dev=info.volume, st_ino=(info.index_high << 32) | info.index_low,
                           st_size=(info.size_high << 32) | info.size_low,
                           st_mtime_ns=(ticks - 116444736000000000) * 100)


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
    except BaseException:
        close(result.value)
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
    except BaseException:
        close(handle)
        raise


def to_fd(handle, flags):
    import msvcrt
    try:
        # CRT only receives descriptor mode flags, not creation/disposition bits.
        mode = os.O_RDWR if flags & os.O_RDWR else (os.O_WRONLY if flags & os.O_WRONLY else os.O_RDONLY)
        return msvcrt.open_osfhandle(handle, mode | os.O_BINARY)
    except BaseException:
        close(handle)
        raise


def rename_same_directory(handle, name):
    """Rename the open source in its existing parent; never resolve a path."""
    _component(name)
    _, nt = _apis()
    raw = name.encode('utf-16-le')
    offset = RenameHeader.FileName.offset
    buffer = C.create_string_buffer(C.sizeof(RenameHeader) + len(raw))
    header = RenameHeader.from_buffer(buffer)
    header.ReplaceIfExists = 1
    header.RootDirectory = None
    header.FileNameLength = len(raw)
    C.memmove(C.addressof(buffer) + offset, raw, len(raw))
    status = IoStatus()
    code = nt.NtSetInformationFile(handle, C.byref(status), buffer, len(buffer), 10)
    if code < 0:
        _error(code)


def publish(directory, staging, target, data, check):
    """Keep the exclusive staging identity open through flush and rename."""
    import msvcrt
    from .atomicio import _REPLACE_DELAYS
    check()
    handle = open_relative(directory.handle, staging, create=True, exclusive=True, write=True, delete=True)
    fd = to_fd(handle, os.O_RDWR)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        check()
        try:
            previous = open_relative(directory.handle, target)
        except FileNotFoundError:
            pass
        else:
            close(previous)
        handle = msvcrt.get_osfhandle(stream.fileno())
        for attempt in range(len(_REPLACE_DELAYS) + 1):
            try:
                check()
                rename_same_directory(handle, target)
                break
            except PermissionError as exc:
                if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == len(_REPLACE_DELAYS):
                    raise
                time.sleep(_REPLACE_DELAYS[attempt])
        check()


class Entry:
    def __init__(self, directory, name, attributes):
        self.directory, self.name, self.attributes = directory, name, attributes

    def stat(self, *, follow_symlinks=False):
        if follow_symlinks:
            raise OSError('gallery enumeration never follows links')
        self.directory.check()
        handle = open_relative(self.directory.handle, self.name, directory=bool(self.attributes & 0x10))
        try:
            return information(handle)
        finally:
            close(handle)


class NativeDirectory:
    def __init__(self, path, create=False):
        path = Path(os.path.abspath(path))
        self.handles = []
        self.owned_start = 0
        try:
            self.handles.append(_anchor(path.anchor))
            for part in path.parts[1:]:
                self.check()
                self.handles.append(open_relative(self.handles[-1], part, directory=True, create=create))
            self.handle = self.handles[-1]
        except BaseException:
            self.close()
            raise

    def child(self, name, create=False):
        self.check()
        child = object.__new__(NativeDirectory)
        child.handles = list(self.handles)
        child.owned_start = len(child.handles)
        child.handles.append(open_relative(self.handle, name, directory=True, create=create))
        child.handle = child.handles[-1]
        return child

    def check(self):
        for handle in self.handles:
            information(handle, True)

    def close(self):
        handles, self.handles = self.handles, []
        for handle in reversed(handles[self.owned_start:]):
            close(handle)

    def open_fd(self, name, flags):
        self.check()
        handle = open_relative(self.handle, name, create=bool(flags & os.O_CREAT),
                               exclusive=bool(flags & os.O_EXCL), write=bool(flags & (os.O_RDWR | os.O_WRONLY)))
        return to_fd(handle, flags)

    def entries(self):
        kernel, _ = _apis()
        self.check()
        buffer = C.create_string_buffer(65536)
        first = True
        while True:
            self.check()
            if not kernel.GetFileInformationByHandleEx(self.handle, 15 if first else 14, buffer, len(buffer)):
                error = C.get_last_error()
                if error == 18:  # NO_MORE_FILES, not an arbitrary I/O failure
                    return
                raise C.WinError(error)
            first = False
            offset = 0
            while True:
                if offset + 68 > len(buffer):
                    raise OSError('invalid native directory record')
                next_offset, = struct.unpack_from('<I', buffer, offset)
                attributes, length = struct.unpack_from('<II', buffer, offset + 56)
                if length % 2 or length > len(buffer) - offset - 68:
                    raise OSError('invalid native directory filename')
                name = buffer.raw[offset + 68:offset + 68 + length].decode('utf-16-le', 'strict')
                if name not in ('.', '..'):
                    _component(name)
                    yield Entry(self, name, attributes)
                if next_offset == 0:
                    break
                if next_offset < 68 + length or next_offset % 8 or offset + next_offset >= len(buffer):
                    raise OSError('invalid native directory offset')
                offset += next_offset

    def free_bytes(self):
        self.check()
        _, nt = _apis()
        status, size = IoStatus(), VolumeSize()
        code = nt.NtQueryVolumeInformationFile(self.handle, C.byref(status), C.byref(size), C.sizeof(size), 7)
        if code < 0:
            _error(code)
        self.check()
        if size.available < 0 or not size.sectors or not size.sector_bytes:
            raise OSError('invalid native volume capacity')
        return size.available * size.sectors * size.sector_bytes
