"""Owned synthetic custody fixtures; never inspect operator credentials/ACLs.

Windows native ACL fixtures receive their descriptor at CREATE_NEW time. No
existing file or directory ACL is changed. Pure ACL controls run on both OSes.
"""
import ctypes as C
import os
from pathlib import Path
import stat
import struct
import traceback
from types import SimpleNamespace

import pytest

from olympus import vault_custody as vc

MATERIAL = b"owned-custody-fixture-0123456789-abcdef"
USER = b"\x01\x02\x00\x00\x00\x00\x00\x05" + struct.pack("<II", 21, 1001)
OTHER = b"\x01\x02\x00\x00\x00\x00\x00\x05" + struct.pack("<II", 21, 1002)
EVERYONE = b"\x01\x01\x00\x00\x00\x00\x00\x01" + struct.pack("<I", 0)


def file_fixture(tmp_path, raw=MATERIAL, *, name="owned-key", mode=0o600):
    path = tmp_path / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
    finally:
        os.close(fd)
    return path


def file_env(path, mode="development"):
    return {"OLYMPUS_SECRET_KEY_FILE": str(path), "OLYMPUS_ENV": mode}


def ace(sid, *, kind=0, flags=0, mask=0x1f01ff):
    return struct.pack("<BBHI", kind, flags, 8 + len(sid), mask) + sid


def acl(*entries, revision=2):
    body = b"".join(entries)
    return struct.pack("<BBHHH", revision, 0, 8 + len(body), len(entries), 0) + body


def test_unconfigured_is_distinct_from_configured_but_missing(tmp_path):
    assert not vc.configured({}) and vc.capture({}) is None
    assert vc.capture({"OLYMPUS_SECRET_KEY": " \t", "OLYMPUS_SECRET_KEY_FILE": " "}) is None
    values = file_env(tmp_path / "owned-missing-key")
    assert vc.configured(values)
    with pytest.raises(vc.CustodyError):
        vc.capture(values)


def test_configured_never_opens_a_file(monkeypatch):
    monkeypatch.setattr(vc, "_read_file", lambda *a: pytest.fail("selector probe read a key"))
    assert vc.configured(file_env("owned-unopened-key"))
    assert vc.configured({"OLYMPUS_SECRET_KEY": "x", "OLYMPUS_SECRET_KEY_FILE": "y"})


def test_ambiguity_refuses_before_read(monkeypatch):
    monkeypatch.setattr(vc, "_read_file", lambda *a: pytest.fail("ambiguous source was opened"))
    with pytest.raises(vc.CustodyError):
        vc.capture({"OLYMPUS_SECRET_KEY": "owned", "OLYMPUS_SECRET_KEY_FILE": "owned-file"})


def test_environment_normalization_and_repr():
    result = vc.capture({"OLYMPUS_SECRET_KEY": " \t\u2003owned-é\n"})
    assert result.material == "owned-é".encode()
    assert result.source == "environment" and not result.named_deployment
    assert not result.windows_acl_checked and "owned-é" not in repr(result)


def test_file_normalization_preserves_utf8_bom_as_material(tmp_path):
    path = file_fixture(tmp_path, b" \t" + b"\xef\xbb\xbf" + "owned-é".encode() + b"\r\n")
    result = vc.capture(file_env(path))
    assert result.material == b"\xef\xbb\xbf" + "owned-é".encode()
    assert result.source == "file" and str(path) not in repr(result)


def test_file_universal_newlines_match_prior_read_text_normalization(tmp_path):
    path = file_fixture(tmp_path, b"  owned\r\nfirst\rsecond\nlast\r\n")
    assert vc.capture(file_env(path)).material == b"owned\nfirst\nsecond\nlast"
    # Environment values never passed through text-file newline decoding.
    assert vc.capture({"OLYMPUS_SECRET_KEY": "owned\r\nfirst"}).material == b"owned\r\nfirst"


@pytest.mark.parametrize("mode", ["staging", "production", "prod", " PRODUCTION "])
def test_named_environment_accepts_owned_strong_material(mode):
    result = vc.capture({"OLYMPUS_SECRET_KEY": MATERIAL.decode(), "OLYMPUS_ENV": mode})
    assert result.material == MATERIAL and result.named_deployment


@pytest.mark.parametrize("secret", ["short", "x" * 32 + "change_me", "your-secret-" + "x" * 40,
                                    "replace-with-" + "x" * 40, "example_token" + "x" * 40])
def test_named_weak_and_placeholder_material_refuses(secret):
    with pytest.raises(vc.CustodyError):
        vc.capture({"OLYMPUS_SECRET_KEY": secret, "OLYMPUS_ENV": "production"})


@pytest.mark.parametrize("raw", [b"", b" \r\n", b"\xff", b"x" * 4097,
                                 b" " * (vc.MAX_FILE_BYTES + 1), "é".encode() * 2049], ids=['empty', 'whitespace', 'invalid-utf8', 'long-ascii-material', 'oversized-file', 'long-utf8-material'])
def test_file_bytes_decoding_and_encoded_material_bounds(tmp_path, raw):
    path = file_fixture(tmp_path, raw)
    before = path.read_bytes()
    with pytest.raises(vc.CustodyError):
        vc.capture(file_env(path))
    assert path.read_bytes() == before


@pytest.mark.parametrize("secret", ["x" * 4097, "é" * 2049, " " * (vc.MAX_FILE_BYTES + 1),
                                    "\ud800", 7, b"not-text"], ids=['long-ascii', 'long-utf8', 'oversized-whitespace', 'surrogate', 'integer', 'bytes'])
def test_environment_bounds_and_types(secret):
    with pytest.raises(vc.CustodyError):
        vc.capture({"OLYMPUS_SECRET_KEY": secret})


def test_exact_passphrase_byte_limit_and_legacy_short_development_values():
    assert vc.capture({"OLYMPUS_SECRET_KEY": "é" * 2048}).material == "é".encode() * 2048
    assert vc.capture({"OLYMPUS_SECRET_KEY": "x"}).material == b"x"


def test_file_byte_bound_precedes_permission_and_content_reads(tmp_path, monkeypatch):
    path = file_fixture(tmp_path, b"x" * (vc.MAX_FILE_BYTES + 1))
    monkeypatch.setattr(vc, "_permissions", lambda *a: pytest.fail("oversize file reached permissions"))
    monkeypatch.setattr(vc.os, "read", lambda *a: pytest.fail("oversize file was read"))
    with pytest.raises(vc.CustodyError):
        vc.capture(file_env(path))


def test_only_selected_environment_fields_are_captured_once(monkeypatch):
    class Selected:
        def __init__(self):
            self.calls = []
        def get(self, key):
            self.calls.append(key)
            return {"OLYMPUS_SECRET_KEY_FILE": "owned-selected-path", "OLYMPUS_ENV": "dev"}.get(key)
    selected = Selected()
    observed = []
    def read(path, named):
        observed.append((path, named))
        return MATERIAL
    monkeypatch.setattr(vc, "_read_file", read)
    assert vc.capture(selected).material == MATERIAL
    assert selected.calls == ["OLYMPUS_SECRET_KEY", "OLYMPUS_SECRET_KEY_FILE", "OLYMPUS_ENV"]
    assert observed == [("owned-selected-path", False)]


def test_refusal_has_no_raw_path_value_or_chained_source_error(monkeypatch):
    marker = "owned-sensitive-path-and-material"
    def fail(*args):
        raise OSError(marker)
    monkeypatch.setattr(vc, "_read_file", fail)
    with pytest.raises(vc.CustodyError) as caught:
        vc.capture(file_env(marker))
    assert marker not in str(caught.value) and marker not in repr(caught.value)
    assert marker not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_fragmented_reads_remain_bounded_and_complete(tmp_path, monkeypatch):
    path = file_fixture(tmp_path)
    original = os.read
    sizes = []
    def fragmented(fd, size):
        sizes.append(size)
        return original(fd, min(size, 3))
    monkeypatch.setattr(vc.os, "read", fragmented)
    assert vc.capture(file_env(path)).material == MATERIAL
    assert max(sizes) == len(MATERIAL) + 1 and all(0 < n <= len(MATERIAL) + 1 for n in sizes)


@pytest.mark.parametrize("fault", [OSError, KeyboardInterrupt, SystemExit])
def test_descriptor_closed_after_information_failure(tmp_path, monkeypatch, fault):
    path = file_fixture(tmp_path)
    descriptors = []
    def broken(fd):
        descriptors.append(fd)
        raise fault("owned interruption")
    monkeypatch.setattr(vc, "_information", broken)
    expected = vc.CustodyError if fault is OSError else fault
    with pytest.raises(expected):
        vc.capture(file_env(path))
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.parametrize("fault", [OSError, KeyboardInterrupt, SystemExit])
def test_native_parent_cleanup_drains_all_owned_handles(monkeypatch, fault):
    from olympus import gallery_windows
    closed = []
    def close(handle):
        closed.append(handle)
        if handle == 3:
            raise fault("owned close interruption")
    monkeypatch.setattr(gallery_windows, "close", close)
    native = SimpleNamespace(handles=[1, 2, 3], owned_start=0)
    directory = SimpleNamespace(win=native, fd=None)
    with pytest.raises(fault):
        vc._close_directory(directory)
    assert closed == [3, 2, 1] and native.handles == [] and directory.win is None


def test_directory_and_hardlinked_key_files_refuse(tmp_path):
    with pytest.raises(vc.CustodyError):
        vc.capture(file_env(tmp_path))
    path = file_fixture(tmp_path)
    os.link(path, tmp_path / "owned-other-link")
    with pytest.raises(vc.CustodyError):
        vc.capture(file_env(path))


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX owner/mode and no-follow paths")
def test_posix_private_owner_mode_and_links(tmp_path):
    private = file_fixture(tmp_path)
    assert vc.capture(file_env(private, "production")).material == MATERIAL
    public = file_fixture(tmp_path, name="owned-public")
    # This new synthetic file needs genuinely broad bits even under umask077.
    # Only fixture setup changes its mode; custody must refuse without repair.
    public.chmod(0o644)
    before = public.stat()
    assert stat.S_IMODE(before.st_mode)==0o644 and public.read_bytes()==MATERIAL
    with pytest.raises(vc.CustodyError):
        vc.capture(file_env(public))
    after = public.stat()
    assert (after.st_dev,after.st_ino,after.st_mode)==(before.st_dev,before.st_ino,before.st_mode)
    assert public.read_bytes()==MATERIAL
    with pytest.raises(vc.CustodyError):
        vc._permissions(-1, SimpleNamespace(st_uid=os.geteuid() + 1, st_mode=stat.S_IFREG | 0o600), False)
    link = tmp_path / "owned-symlink"
    link.symlink_to(private)
    with pytest.raises(vc.CustodyError):
        vc.capture(file_env(link))
    directory = tmp_path / "owned-directory-link"
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(vc.CustodyError):
        vc.capture(file_env(directory / private.name))


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX rename of an open owned fixture")
def test_replacement_after_read_refuses_without_returning_stale_material(tmp_path, monkeypatch):
    path = file_fixture(tmp_path)
    original = os.read
    changed = False
    def replace(fd, size):
        nonlocal changed
        data = original(fd, size)
        if not changed:
            changed = True
            path.rename(tmp_path / "retained-original-key")
            file_fixture(tmp_path, b"owned-replacement-material")
        return data
    monkeypatch.setattr(vc.os, "read", replace)
    with pytest.raises(vc.CustodyError):
        vc.capture(file_env(path))
    assert (tmp_path / "retained-original-key").read_bytes() == MATERIAL


def test_inplace_growth_after_read_refuses(tmp_path, monkeypatch):
    path = file_fixture(tmp_path)
    original = os.read
    changed = False
    def grow(fd, size):
        nonlocal changed
        data = original(fd, size)
        if not changed:
            changed = True
            with path.open("ab") as stream:
                stream.write(b"owned-growth")
        return data
    monkeypatch.setattr(vc.os, "read", grow)
    with pytest.raises(vc.CustodyError):
        vc.capture(file_env(path))


def test_windows_acl_policy_accepts_only_private_supported_grants():
    private = acl(ace(USER), ace(vc._SYSTEM_SID), ace(vc._ADMINS_SID))
    assert vc._check_windows_acl(USER, private, 0x8004, USER)
    assert vc._check_windows_acl(vc._SYSTEM_SID, private, 0x8004, USER)
    assert vc._check_windows_acl(USER, acl(ace(EVERYONE, kind=1), ace(USER)), 4, USER)
    assert vc._check_windows_acl(USER, acl(ace(OTHER, flags=8), ace(USER)), 4, USER)


@pytest.mark.parametrize("owner,body,control", [
    (OTHER, acl(ace(USER)), 4), (USER, None, 4), (USER, acl(ace(USER)), 0),
    (USER, acl(ace(USER), ace(EVERYONE)), 4), (USER, acl(ace(OTHER, flags=16)), 4),
    (USER, acl(ace(USER, kind=5)), 4), (USER, acl(ace(USER, kind=9)), 4),
    (USER, acl(ace(USER, flags=64)), 4), (USER, acl(ace(USER), revision=1), 4),
    (USER, b"\x02" * 7, 4), (USER, acl(ace(USER)) + b"\0", 4),
    (USER, acl(ace(USER)), -1), (USER, acl(ace(USER)), 0x10004),
])
def test_windows_acl_policy_refuses_public_opaque_and_malformed(owner, body, control):
    with pytest.raises(vc.CustodyError):
        vc._check_windows_acl(owner, body, control, USER)


def _create_windows_acl_fixture(path, *, public=False):
    """Set initial security at CREATE_NEW; never alter an existing object's ACL."""
    kernel, advapi = vc._security_apis()
    sid = vc._windows_user_sid(kernel, advapi)
    sid_buffer = C.create_string_buffer(sid)
    text = C.c_void_p()
    advapi.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(C.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = C.c_int
    assert advapi.ConvertSidToStringSidW(sid_buffer, C.byref(text))
    try:
        user = C.wstring_at(text)
    finally:
        assert not kernel.LocalFree(text)
    sddl = "D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;" + user + ")"
    if public:
        sddl += "(A;;FR;;;WD)"
    descriptor = C.c_void_p()
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        C.c_wchar_p, C.c_uint32, C.POINTER(C.c_void_p), C.POINTER(C.c_uint32)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = C.c_int
    assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, C.byref(descriptor), None)
    class Attributes(C.Structure):
        _fields_ = [("length", C.c_uint32), ("descriptor", C.c_void_p), ("inherit", C.c_int)]
    attributes = Attributes(C.sizeof(Attributes), descriptor, 0)
    kernel.CreateFileW.argtypes = [C.c_wchar_p, C.c_uint32, C.c_uint32, C.c_void_p,
                                   C.c_uint32, C.c_uint32, C.c_void_p]
    kernel.CreateFileW.restype = C.c_void_p
    try:
        handle = kernel.CreateFileW(str(path), 0x40000000, 0, C.byref(attributes), 1, 0x80, None)
        assert handle != C.c_void_p(-1).value
        import msvcrt
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        with os.fdopen(fd, "wb") as stream:
            stream.write(MATERIAL)
    finally:
        assert not kernel.LocalFree(descriptor)


@pytest.mark.skipif(os.name != "nt", reason="requires actual Windows held-handle owner/DACL inspection")
def test_native_windows_acl_private_accepts_and_world_read_refuses(tmp_path):
    private, public = tmp_path / "owned-private", tmp_path / "owned-public"
    _create_windows_acl_fixture(private)
    _create_windows_acl_fixture(public, public=True)
    result = vc.capture(file_env(private, "production"))
    assert result.material == MATERIAL and result.windows_acl_checked
    before = public.read_bytes()
    with pytest.raises(vc.CustodyError):
        vc.capture(file_env(public, "production"))
    assert public.read_bytes() == before
    # Development keeps the prior Windows boundary; no ACL guarantee is implied.
    result = vc.capture(file_env(public))
    assert result.material == MATERIAL and not result.windows_acl_checked


@pytest.mark.skipif(os.name != "nt", reason="requires actual Windows descriptor security observations")
def test_native_windows_acl_is_checked_before_and_after_same_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "owned-private"
    _create_windows_acl_fixture(path)
    original = vc._windows_permissions
    calls = []
    def observed(fd):
        result = original(fd)
        calls.append((fd, result))
        return result
    monkeypatch.setattr(vc, "_windows_permissions", observed)
    assert vc.capture(file_env(path, "production")).material == MATERIAL
    assert len(calls) == 2 and calls[0] == calls[1]


@pytest.mark.skipif(os.name != "nt", reason="requires actual Windows descriptor security observations")
def test_native_windows_changed_security_snapshot_refuses(tmp_path, monkeypatch):
    path = tmp_path / "owned-private"
    _create_windows_acl_fixture(path)
    original = vc._windows_permissions
    calls = []
    def changed(fd):
        observed = original(fd)
        calls.append(fd)
        if len(calls) == 2:
            # Inject an observed metadata change, never mutate the fixture ACL.
            owner, raw, control, user = observed
            return owner, raw, control ^ 0x1000, user
        return observed
    monkeypatch.setattr(vc, "_windows_permissions", changed)
    with pytest.raises(vc.CustodyError):
        vc.capture(file_env(path, "production"))
    assert len(calls) == 2 and calls[0] == calls[1]
    assert path.read_bytes() == MATERIAL
