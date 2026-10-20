"""Capture one bounded vault passphrase without writing or changing custody.

The result deliberately contains no path and hides key material in repr. File
reads use the vault's retained-ancestor primitives, then validate the same descriptor
before/after a bounded read. POSIX requires the effective UID and private mode
bits. Named Windows deployments additionally require a private owner/DACL:
only the effective token user, SYSTEM and Administrators may receive access.
This is a conservative ACL policy, not a general Windows AccessCheck emulator;
object/callback/conditional ACEs and unavailable security metadata are refused.
Environment credentials retain the existing operator-controlled boundary.

Primary API contracts:
https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo
https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation
https://learn.microsoft.com/en-us/windows/win32/secauthz/access-control-entries

No key cache, ACL mutation, migration, signing change or process-lock guarantee
is provided here. Privileged administrators remain trusted. Tests use owned
synthetic material only; deployment custody still needs actual host evidence.
"""
from __future__ import annotations

import ctypes as C
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
import struct

from . import native_cleanup

MAX_FILE_BYTES = 16 * 1024
MAX_PASSPHRASE_BYTES = 4096
MAX_PATH_CHARS = 32768
MAX_ACL_BYTES = 65535
MAX_ACES = 1024
_FAILURE = "vault passphrase custody is unavailable or invalid"
_PLACEHOLDER_RE = re.compile(
    r"(?:replace(?:_|-)?(?:me|with)|change(?:_|-)?me|changeme|"
    r"your(?:_|-)(?:secret|key|token)|example(?:_|-)(?:secret|key|token))",
    re.IGNORECASE)
_SYSTEM_SID = b"\x01\x01\x00\x00\x00\x00\x00\x05" + struct.pack("<I", 18)
_ADMINS_SID = b"\x01\x02\x00\x00\x00\x00\x00\x05" + struct.pack("<II", 32, 544)


class CustodyError(ValueError):
    """A generic refusal: no credential, path, or underlying exception text."""


@dataclass(frozen=True)
class CapturedPassphrase:
    material: bytes = field(repr=False)
    source: str
    named_deployment: bool
    windows_acl_checked: bool


def _text(value, maximum):
    if value is None:
        return ""
    if type(value) is not str or len(value) > maximum:
        raise CustodyError(_FAILURE)
    return value.strip()


def configured(environ=None) -> bool:
    """Inspect selectors only; configured does not imply usable custody."""
    selected = os.environ if environ is None else environ
    failed = False
    try:
        secret = _text(selected.get("OLYMPUS_SECRET_KEY"), MAX_FILE_BYTES)
        path = _text(selected.get("OLYMPUS_SECRET_KEY_FILE"), MAX_PATH_CHARS)
        return bool(secret or path)
    except Exception:
        failed = True
    if failed:
        raise CustodyError(_FAILURE) from None


def _valid_sid(raw):
    return (type(raw) is bytes and 8 <= len(raw) <= 68
            and raw[0] == 1 and raw[1] <= 15 and len(raw) == 8 + 4 * raw[1])


def _check_windows_acl(owner, acl, control, user):
    """Validate copied, bounded security metadata; never dereference pointers."""
    if (not _valid_sid(owner) or not _valid_sid(user)
            or type(control) is not int or not 0 <= control <= 0xffff or not control & 0x0004
            or type(acl) is not bytes or not 8 <= len(acl) <= MAX_ACL_BYTES):
        raise CustodyError(_FAILURE)  # missing/NULL DACL is not private
    trusted = {user, _SYSTEM_SID, _ADMINS_SID}
    if owner not in trusted:
        raise CustodyError(_FAILURE)
    revision, zero, size, count, reserved = struct.unpack_from("<BBHHH", acl)
    if revision not in (2, 4) or zero or reserved or size != len(acl) or count > MAX_ACES:
        raise CustodyError(_FAILURE)
    offset = 8
    for _ in range(count):
        if offset + 8 > len(acl):
            raise CustodyError(_FAILURE)
        kind, flags, length, mask = struct.unpack_from("<BBHI", acl, offset)
        if (kind not in (0, 1) or flags & ~0x1f or length < 16
                or length % 4 or offset + length > len(acl)):
            raise CustodyError(_FAILURE)
        sid = acl[offset + 8:offset + length]
        if not _valid_sid(sid):
            raise CustodyError(_FAILURE)
        # INHERIT_ONLY does not apply to this file; DENY never adds authority.
        if kind == 0 and mask and not flags & 0x08 and sid not in trusted:
            raise CustodyError(_FAILURE)
        offset += length
    # Unused ACL allocation bytes are permitted by Windows, but not hidden ACEs.
    if any(acl[offset:]):
        raise CustodyError(_FAILURE)
    return owner, acl, control, user


def _security_apis():
    kernel = C.WinDLL("kernel32", use_last_error=True)
    advapi = C.WinDLL("advapi32", use_last_error=True)
    pointer, word, boolean = C.c_void_p, C.c_uint32, C.c_int
    pp = C.POINTER(pointer)
    signatures = {
        "GetSecurityInfo": ([pointer, C.c_int, word, pp, pp, pp, pp, pp], word),
        "IsValidSecurityDescriptor": ([pointer], boolean),
        "GetSecurityDescriptorControl": ([pointer, C.POINTER(C.c_uint16), C.POINTER(word)], boolean),
        "IsValidSid": ([pointer], boolean),
        "GetLengthSid": ([pointer], word),
        "IsValidAcl": ([pointer], boolean),
        "OpenThreadToken": ([pointer, word, boolean, pp], boolean),
        "OpenProcessToken": ([pointer, word, pp], boolean),
        "GetTokenInformation": ([pointer, C.c_int, pointer, word, C.POINTER(word)], boolean),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(advapi, name)
        fn.argtypes, fn.restype = args, result
    for name, args, result in (
        ("GetCurrentThread", [], pointer), ("GetCurrentProcess", [], pointer),
        ("CloseHandle", [pointer], boolean), ("LocalFree", [pointer], pointer),
    ):
        fn = getattr(kernel, name)
        fn.argtypes, fn.restype = args, result
    return kernel, advapi


def _copy_sid(advapi, pointer):
    if not pointer or not advapi.IsValidSid(pointer):
        raise CustodyError(_FAILURE)
    length = advapi.GetLengthSid(pointer)
    if not 8 <= length <= 68:
        raise CustodyError(_FAILURE)
    result = C.string_at(pointer, length)
    if not _valid_sid(result):
        raise CustodyError(_FAILURE)
    return result


def _windows_user_sid(kernel, advapi):
    token = C.c_void_p()
    primary = None
    try:
        # Inspect the effective thread identity when impersonating, otherwise the
        # process identity. Errors other than ERROR_NO_TOKEN must not downgrade.
        if not advapi.OpenThreadToken(kernel.GetCurrentThread(), 8, True, C.byref(token)):
            if token.value is not None or C.get_last_error() != 1008:
                raise CustodyError(_FAILURE)
            if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, C.byref(token)):
                raise CustodyError(_FAILURE)
        if not token.value:
            raise CustodyError(_FAILURE)
        size = C.c_uint32()
        ok = advapi.GetTokenInformation(token, 1, None, 0, C.byref(size))
        if ok or C.get_last_error() != 122 or not C.sizeof(C.c_void_p) <= size.value <= 4096:
            raise CustodyError(_FAILURE)
        buffer = C.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, len(buffer), C.byref(size)):
            raise CustodyError(_FAILURE)
        if not C.sizeof(C.c_void_p) <= size.value <= len(buffer):
            raise CustodyError(_FAILURE)
        pointer = C.c_void_p.from_buffer(buffer).value
        start, end = C.addressof(buffer), C.addressof(buffer) + size.value
        if not pointer or not start <= pointer <= end - 8:
            raise CustodyError(_FAILURE)
        header = C.string_at(pointer, 8)
        if header[0] != 1 or header[1] > 15 or pointer + 8 + 4 * header[1] > end:
            raise CustodyError(_FAILURE)
        return _copy_sid(advapi, pointer)
    except BaseException as error:
        primary = error
        raise
    finally:
        owned, token.value = token.value, None
        if owned is not None:
            def release():
                if not kernel.CloseHandle(owned):
                    raise CustodyError(_FAILURE)
            native_cleanup.drain([release], primary)


def _windows_permissions(fd):
    import msvcrt
    kernel, advapi = _security_apis()
    user = _windows_user_sid(kernel, advapi)
    owner, dacl, descriptor = C.c_void_p(), C.c_void_p(), C.c_void_p()
    primary = None
    try:
        code = advapi.GetSecurityInfo(msvcrt.get_osfhandle(fd), 1, 0x0001 | 0x0004,
                                     C.byref(owner), None, C.byref(dacl), None,
                                     C.byref(descriptor))
        if code or not descriptor or not advapi.IsValidSecurityDescriptor(descriptor):
            raise CustodyError(_FAILURE)
        control, revision = C.c_uint16(), C.c_uint32()
        if not advapi.GetSecurityDescriptorControl(descriptor, C.byref(control), C.byref(revision)):
            raise CustodyError(_FAILURE)
        if not dacl or not advapi.IsValidAcl(dacl):
            raise CustodyError(_FAILURE)
        header = C.string_at(dacl, 8)
        size = struct.unpack_from("<H", header, 2)[0]
        if not 8 <= size <= MAX_ACL_BYTES:
            raise CustodyError(_FAILURE)
        acl = C.string_at(dacl, size)
        return _check_windows_acl(_copy_sid(advapi, owner), acl, control.value, user)
    except BaseException as error:
        primary = error
        raise
    finally:
        owned, descriptor.value = descriptor.value, None
        if owned is not None:
            def release():
                if kernel.LocalFree(owned):
                    raise CustodyError(_FAILURE)
            native_cleanup.drain([release], primary)


def _information(fd):
    if os.name == "nt":
        import msvcrt
        from .gallery_windows import information
        return information(msvcrt.get_osfhandle(fd), False)
    return os.fstat(fd)


def _identity(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400
            or not 0 <= info.st_size <= MAX_FILE_BYTES):
        raise CustodyError(_FAILURE)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            getattr(info, "st_ctime_ns", None))


def _permissions(fd, info, named):
    if os.name == "posix":
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise CustodyError(_FAILURE)
        return info.st_uid, stat.S_IMODE(info.st_mode)
    if os.name == "nt":
        return _windows_permissions(fd) if named else None
    raise CustodyError(_FAILURE)


def _close_directory(directory):
    """Legacy owner helper: detach once and drain every independently owned handle."""
    actions = []
    if directory.win is not None:
        from .gallery_windows import close
        native, directory.win = directory.win, None
        handles, native.handles = native.handles[native.owned_start:], []
        actions.extend(lambda handle=handle: close(handle) for handle in reversed(handles))
    if directory.fd is not None:
        fd, directory.fd = directory.fd, None
        actions.append(lambda: os.close(fd))
    native_cleanup.drain(actions)


def _read_file(path, named):
    from .vault_native import Directory
    from .archive_io import selected_path
    # Reject drive-relative and trailing-dot/space aliases before abspath can
    # erase their original spelling. Relative paths bind CWD before acquisition.
    selected = selected_path(path)
    directory, fd, primary = None, None, None
    try:
        directory = Directory(selected.parent)
        directory.check()
        directory._case_spelling(selected.name)
        if os.name == "nt":
            fd = directory.win.open_fd(selected.name, os.O_RDONLY)
        else:
            fd = os.open(selected.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory.fd)
        directory._case_spelling(selected.name)
        before = _information(fd)
        identity = _identity(before)
        permissions = _permissions(fd, before, named)
        chunks, remaining = [], before.st_size + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        result = b"".join(chunks)
        after = _information(fd)
        if (len(result) != before.st_size or _identity(after) != identity
                or _permissions(fd, after, named) != permissions):
            raise CustodyError(_FAILURE)
        if os.name == "posix":
            entry = os.stat(selected.name, dir_fd=directory.fd, follow_symlinks=False)
            if _identity(entry) != identity or _permissions(fd, entry, named) != permissions:
                raise CustodyError(_FAILURE)
        directory._case_spelling(selected.name)
        directory.check()
        return result
    except BaseException as error:
        primary = error
        raise
    finally:
        actions = []
        if fd is not None:
            owned_fd, fd = fd, None
            actions.append(lambda: os.close(owned_fd))
        if directory is not None:
            owned_directory, directory = directory, None
            actions.append(owned_directory.close)
        native_cleanup.drain(actions, primary)


def capture(environ=None) -> CapturedPassphrase | None:
    """Capture selectors once; None means genuinely unconfigured, never broken.

    An explicit mapping can freeze the caller's environment selection. Only
    the three relevant keys are read. Leading/trailing Unicode whitespace and
    strict UTF-8 retain the old vault's .strip()/encode() normalization.
    """
    selected = os.environ if environ is None else environ
    cleanup_unconfirmed = False
    try:
        secret = _text(selected.get("OLYMPUS_SECRET_KEY"), MAX_FILE_BYTES)
        path = _text(selected.get("OLYMPUS_SECRET_KEY_FILE"), MAX_PATH_CHARS)
        mode = _text(selected.get("OLYMPUS_ENV"), 64).lower()
        if secret and path:
            raise CustodyError(_FAILURE)
        if not secret and not path:
            return None
        named = mode in ("staging", "production", "prod")
        if path:
            if "\x00" in path:
                raise CustodyError(_FAILURE)
            # Path.read_text used universal-newline decoding in the old vault.
            secret = (_read_file(path, named).decode("utf-8", "strict")
                      .replace("\r\n", "\n").replace("\r", "\n").strip())
        material = secret.encode("utf-8", "strict")
        if not 1 <= len(material) <= MAX_PASSPHRASE_BYTES:
            raise CustodyError(_FAILURE)
        if named and (len(secret) < 32 or _PLACEHOLDER_RE.search(secret)):
            raise CustodyError(_FAILURE)
        return CapturedPassphrase(material, "file" if path else "environment",
                                  named, bool(path and named and os.name == "nt"))
    except Exception as error:
        cleanup_unconfirmed = native_cleanup.unknown(error)
    # Raise outside the handler: no private source context, notes, path or value.
    error = CustodyError(_FAILURE)
    if cleanup_unconfirmed:
        native_cleanup.mark(error)
    raise error from None
