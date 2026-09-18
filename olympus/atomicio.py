"""Durable atomic publish: tmp file + fsync + `os.replace` (+ parent-dir fsync).

Every durable store here publishes with the tmp-file + `os.replace` idiom, which
buys ATOMICITY — a reader in another process sees the old bytes or the new ones,
never a torn blob. It does not buy DURABILITY. `os.replace` is atomic with
respect to other processes, not with respect to power loss: the rename can reach
disk before the data blocks it points at, so a crash at the wrong moment leaves a
file that exists and is EMPTY.

That failure is silent by construction. Every consumer maps an unreadable or
empty file to `{}` / `[]` and rebuilds from defaults — the right behaviour for a
torn read, and the wrong outcome for a lost one. A crash can reset the day's
spend ledger, empty a user's memory document, or un-revoke a capability, with no
error raised anywhere.

The fix is the one `sessionlog` already uses (`_append_records`, `compact`):
flush and `os.fsync` the temp file's descriptor BEFORE the replace, so the data
blocks are on stable storage before anything points at them.

Two halves, and only one is portable:

  * **The file fsync** puts the new contents on stable storage. Works everywhere.
  * **The parent-directory fsync** is what makes the RENAME itself durable. It
    is POSIX-only — Windows cannot open a directory as a file descriptor, so
    `os.O_DIRECTORY` does not exist there. `fsync_dir` degrades to a clean no-op
    rather than failing, and the larger half still applies.

Sites that sit on a measured hot path pass `fsync=False` under their own policy
knob (see `usage._fsync_ledger`), mirroring `sessionlog._fsync_always`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

#: Whether this platform can fsync a directory. Probed by capability rather
#: than by platform name: `os.O_DIRECTORY` is absent on Windows (os.name "nt")
#: and present on Linux/macOS.
CAN_FSYNC_DIR = hasattr(os, "O_DIRECTORY")
_WINDOWS = os.name == "nt"
_REPLACE_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)


def fsync_dir(directory) -> None:
    """fsync a directory so a rename INTO it survives power loss.

    A no-op where the platform cannot open a directory. Best-effort otherwise:
    a directory that will not open is not a reason to fail a write whose data
    has already landed and been synced."""
    if not CAN_FSYNC_DIR:
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(tmp, path, data, *, encoding: str = "utf-8",
            chmod: int | None = None, fsync: bool = True,
            exclusive: bool = False, retry_windows_sharing: bool = False) -> None:
    """Write `data` to `tmp`, fsync it, then atomically replace `path`.

    `data` is `str` or `bytes`. Text is written through text mode with the same
    defaults `Path.write_text` uses, so the on-disk bytes — including Windows
    newline translation — are byte-for-byte what each call site produced before
    the fsync was added; a binary write here would silently change every
    `\\n` to `\\r\\n`-free content on Windows and move file hashes with it.

    `fsync=False` keeps the atomic-but-not-durable behaviour, for a call site
    that has measured the cost and opted out through its own policy knob.

    `exclusive=True` refuses to overwrite an existing staging file. Optional
    `retry_windows_sharing` retries only denied Windows renames, after writing
    and closing the staging file once. Permanent errors retain its bytes and
    the previous destination. Other callers keep the single-attempt contract.
    """
    tmp, path = Path(tmp), Path(path)
    mode = "x" if exclusive else "w"
    handle = (open(tmp, mode, encoding=encoding) if isinstance(data, str)
              else open(tmp, mode + "b"))
    try:
        handle.write(data)
        if fsync:
            # flush() moves Python's buffer into the OS; fsync moves the OS's
            # buffer onto the platter. Both are needed, in that order.
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        handle.close()
    if chmod is not None:
        try:
            os.chmod(tmp, chmod)
        except OSError:
            pass
    _replace(tmp, path, retry_windows_sharing)
    if fsync:
        fsync_dir(path.parent)


def _replace(tmp, path, retry_windows_sharing):
    """At most six rename attempts and 310 ms of sleep when explicitly enabled.

    Retry the same already-written bytes, never the caller's read-modify-write.
    No chmod, unlink or error suppression occurs. Uncontended calls do not wait.
    """
    for attempt in range(len(_REPLACE_DELAYS) + 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as err:
            if (not retry_windows_sharing or not _WINDOWS
                    or getattr(err, "winerror", None) not in (5, 32, 33)
                    or attempt == len(_REPLACE_DELAYS)):
                raise
            time.sleep(_REPLACE_DELAYS[attempt])
