"""Durable exclusive-owner lease for the installation's single browser.

Olympus drives ONE browser per installation (`browser.session()`), and that
browser is a credentialed shared resource: it holds cookies, authenticated tabs,
and page state belonging to whichever user last signed in through it. On a
multi-user instance — the posture `deploy/README.md` documents — a second tenant
reaching that session reads and drives the first tenant's logged-in accounts.

This module is the ownership boundary. It answers exactly two questions:

  * **Is this caller a principal we may bind a credentialed browser to?**
    (`require_owner`) — see PRINCIPAL POLICY below.
  * **Does the installation's browser already belong to somebody else?**
    (`claim` / `verify`) — a durable, cross-process, fail-closed lease.

WHY A DURABLE LEASE AND NOT A MODULE GLOBAL
    A process-global `_session_owner` is defeated three ways in this codebase:
    `browser.reset()` does not terminate `browser._launched`, so a reset
    reattaches to the *same* live Chrome with every cookie intact; nothing
    terminates that Chrome at exit, so it outlives the process; and on the
    remote-CDP path `_resolve_page_ws` attaches to `pages[0]` — whatever
    authenticated tab the browser lists first — so the first caller after a
    restart inherits a stranger's session. Ownership therefore has to outlive
    the process that claimed it, which means it has to be on disk.

FINGERPRINT IS DIAGNOSTIC, NEVER AUTHORITY
    The record carries a browser fingerprint when one is cheaply available, for
    operator diagnosis only. It is NOT proof the profile is clean and it is NOT
    an ownership signal. A mismatch never clears a lease and never lets a
    different owner in; it is recorded and ignored. Reassignment happens only
    through verified destruction (`browser.relinquish`).

NO TIME-BASED RELEASE
    There is deliberately no TTL and no idle expiry. A lease that lapsed on a
    timer would hand a credentialed browser to the next caller precisely when
    the owner stopped watching. Ownership ends when the credentials are
    verifiably gone, and at no other moment.

LOCK ORDER (deadlock discipline — one order, everywhere)
    ``browserlease._lock()``  →  ``browser._lock``

    The cross-process lease lock is ALWAYS the outer lock. `browser.py` acquires
    it in `session()` / `relinquish()` and only then takes its in-process
    `_lock`. Nothing in this module takes `browser._lock`, and nothing holding
    `browser._lock` may call into here. `browser._lock` alone is insufficient:
    the heartbeat, web, and CLI run as separate OS processes sharing MEMORY_DIR.

WHY A PRIVATE LOCK AND NOT `proclock`
    `proclock` degrades to *in-process* thread locking wherever `fcntl` is
    unavailable — Windows (ADR 0005). That is fatal here, and specifically for
    RELEASE. `O_CREAT|O_EXCL` makes the initial CLAIM atomic, but it protects
    creation only; it does nothing for a read-validate-then-unlink. Without a
    real cross-process lock this interleaving loses a live lease:

        R1 reads and validates owner A's lease, and is preempted.
        R2 completes A's release; the record is gone.
        B claims; a NEW record exists, owned by B.
        R1 resumes and unlinks the path — deleting B's lease.
        C now claims a browser that still holds B's session state.

    So every mutation — claim, fingerprint backfill, release, corruption
    handling — runs inside `_lock()`, which is a genuine byte-range file lock on
    both platforms (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows), and
    the whole read/validate/write-or-delete transaction happens inside it.

    Defence in depth on top of the lock: every record carries an immutable
    random `lease_id`. Release must name the exact `lease_id` it validated, and
    the delete re-checks it under the lock. In the interleaving above, R1 would
    be holding L1 while the disk holds B's L2, so the unlink is refused even if
    the lock were somehow lost.

    The lock FILE is never deleted — only the lease record is. Unlinking a lock
    file is itself a race (two processes can end up locking different inodes).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time

from . import config

try:                                   # POSIX
    import fcntl
except ImportError:                    # pragma: no cover - Windows
    fcntl = None

try:                                   # Windows
    import msvcrt
except ImportError:                    # pragma: no cover - POSIX
    msvcrt = None

#: On-disk format version for the lease record. A record whose version this
#: build does not understand is treated as malformed and FAILS CLOSED, rather
#: than being guessed at or silently overwritten.
SCHEMA_VERSION = 1

#: proclock name. See LOCK ORDER above — this is always the OUTER lock.
LOCK_NAME = "browser-lease"

#: Operator-configured trusted owner. Required before Olympus will attach to a
#: remote CDP browser that has no lease yet (that browser may already hold a
#: person's live sessions, so "first caller wins" is not safe there).
OWNER_ENV = "OLYMPUS_BROWSER_OWNER"

#: The ONE refusal string every browser tool returns on an ownership failure.
#: It is deliberately constant and content-free: it names no owner, domain,
#: host, URL, tab title, cookie, transport, lease path, or fingerprint, so a
#: denial cannot be used to probe what the browser is currently doing or for
#: whom. Never interpolate anything into this.
REFUSAL = ("Error: the browser is reserved for another session and is not "
           "available here.")

#: Returned when a remote personal browser cannot be reassigned automatically.
#: Administrative guidance only — still names nothing about the current owner.
REASSIGN_NOTICE = (
    "Error: this browser is attached over OLYMPUS_BROWSER_CDP_URL and its "
    "ownership cannot be transferred automatically. Olympus will not wipe a "
    "browser profile it does not own. To reassign it, point "
    "OLYMPUS_BROWSER_CDP_URL at a dedicated, freshly-provisioned browser "
    "profile and set OLYMPUS_BROWSER_OWNER to the new owner.")


class OwnershipRefused(RuntimeError):
    """The caller may not use this installation's browser.

    Deliberately NOT a subclass of ``browser.BrowserUnavailable``: that
    exception's text is interpolated into tool output, and this one's must not
    be. Every tool boundary catches this type and returns :data:`REFUSAL`
    verbatim.
    """

    def __init__(self, message: str = REFUSAL) -> None:
        super().__init__(message)


class LeaseUnreadable(OwnershipRefused):
    """The lease record exists but could not be trusted (truncated, malformed,
    unknown schema). Fails closed as a refusal — an unreadable lease must never
    read as "unowned"."""


# --- principal policy -------------------------------------------------------
#
# TRUSTED: an identity the platform authenticated, or one an operator
# configured. UNTRUSTED: anything the caller could choose for itself.
#
# The web default matters most here. With OLYMPUS_REQUIRE_LOGIN off,
# `web.py:_user_for` derives the principal from a caller-supplied `session=`
# value, and web.py's own docstring notes that id leaks through access logs and
# Referer headers. A `web-*` principal is therefore NEVER trusted with a
# credentialed browser; account principals (`u:<id>`, only minted for a valid
# login token) are.

#: Exact-match principals that may never hold a lease. "shared" is the ambient
#: system namespace the heartbeat runs under: background maintenance must never
#: claim or drive a user's authenticated browser.
UNTRUSTED_EXACT = frozenset({"", "shared"})

#: Unauthenticated web session namespace (`web.py:_user_for`). Always refused.
WEB_PREFIX = "web-"

#: Logged-in account namespaces. `accounts.namespace_for_token` mints "u:<id>";
#: `memory.set_user` normalizes that to "u-<id>", so both spellings are the same
#: authenticated principal and both are trusted.
ACCOUNT_PREFIXES = ("u:", "u-")

#: The local terminal principal (`cli.py`). Shell access is box ownership.
CLI_PRINCIPAL = "cli"


def canonical(owner) -> str:
    """The exact identity used for lease equality.

    Whitespace-stripped and NOTHING else. In particular this never runs
    ``memory.safe_id``: that collapses distinct principals ("a.b" and "a-b"
    both become "a-b") and truncates at 64 characters, so using it as the
    equality identity would let two different people share one lease. safe_id
    remains correct for building filesystem paths; it is wrong for deciding who
    someone is.
    """
    if owner is None:
        return ""
    return str(owner).strip()


def is_trusted(owner) -> bool:
    """Whether `owner` is a principal we may bind a credentialed browser to.

    Default deny: an identity this function does not positively recognise is
    untrusted, so a new transport or entry point cannot quietly acquire browser
    rights by inventing a namespace.

    Note there is no `OLYMPUS_BROWSER_OWNER` clause here. That variable selects
    WHICH trusted principal owns a pre-existing remote browser; it cannot make
    an untrusted one trusted. See :func:`configured_owner`.
    """
    name = canonical(owner)
    if not name or name in UNTRUSTED_EXACT:
        return False
    if name.startswith(WEB_PREFIX):
        return False                       # caller-selectable when login is off
    if name == CLI_PRINCIPAL:
        return True
    if name.startswith(ACCOUNT_PREFIXES):
        return True
    # Platform-authenticated chat identities (tg-/dc-/sl-/sg-/wa-/email-/hook-).
    # Reuses capprofile's list rather than duplicating it, so a new transport
    # registers its prefix in exactly one place.
    try:
        from . import capprofile
        if name.startswith(capprofile.CHANNEL_PREFIXES):
            return True
    except Exception:
        pass
    return False


def configured_owner() -> str:
    """The operator-configured browser owner, or "" when unset or unusable.

    SELECTS an owner; never PROMOTES one. The value must itself be an
    intrinsically trusted, server-derived principal — the same test every other
    caller passes. Setting it to `shared`, to a `web-*` session id, or to any
    unrecognised string yields "" and therefore grants nothing: an operator
    typo cannot hand the browser to the heartbeat's namespace, and an operator
    who pastes a caller-controlled no-login web identity does not turn it into
    an authentication.

    This is why `is_trusted` does not consult it: making it a trust clause
    would let the env var define its own way in.
    """
    name = canonical(os.environ.get(OWNER_ENV, ""))
    return name if name and is_trusted(name) else ""


def require_owner(owner) -> str:
    """Return the canonical owner, or raise :class:`OwnershipRefused`.

    This is the principal half of the boundary; :func:`verify` is the lease
    half. Callers must pass an explicit principal — there is deliberately no
    fallback to ``memory.current_user()`` here, because the ambient contextvar
    defaults to "shared" and an implicit default is exactly how a background
    job would end up owning somebody's logged-in browser.
    """
    name = canonical(owner)
    if not is_trusted(name):
        raise OwnershipRefused()
    return name


# --- the durable record -----------------------------------------------------

def _path():
    # Pure: no mkdir. Every real use of this path happens inside `_lock()`, and
    # `_lock_path()` creates MEMORY_DIR (as the parent of MEMORY_DIR/locks)
    # before the lock is taken — so the directory always exists by then.
    return config.MEMORY_DIR / "browser_lease.json"


def _decode(raw: str) -> dict:
    """Parse a lease record, or raise LeaseUnreadable. Fails closed on anything
    it cannot fully vouch for — a truncated write, a hand-edited file, a record
    from a future schema."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as err:
        raise LeaseUnreadable() from err
    if not isinstance(data, dict):
        raise LeaseUnreadable()
    if data.get("schema_version") != SCHEMA_VERSION:
        raise LeaseUnreadable()
    owner = data.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        raise LeaseUnreadable()
    transport = data.get("transport")
    if not isinstance(transport, str) or not transport:
        raise LeaseUnreadable()
    # The generation token. Immutable for the life of one lease; a record
    # without it cannot be safely released (see release()), so it is required
    # rather than defaulted — a missing lease_id is a malformed record.
    lease_id = data.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id.strip():
        raise LeaseUnreadable()
    return {
        "schema_version": SCHEMA_VERSION,
        "owner": owner,                     # exact; never re-normalized
        "lease_id": lease_id,
        "transport": transport,
        "claimed_at": float(data.get("claimed_at", 0.0) or 0.0),
        "fingerprint": str(data.get("fingerprint", "") or ""),
    }


def _read() -> dict | None:
    """The current lease record, or None when unowned.

    Raises :class:`LeaseUnreadable` on a corrupt record. The caller MUST hold
    :func:`_lock`; use :func:`current` for a locked read.
    """
    _require_lock()
    path = _path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as err:
        # Present but unreadable (permissions, I/O). Fail closed: an unowned
        # answer here would hand the browser to the next caller.
        raise LeaseUnreadable() from err
    if not raw.strip():
        # A zero-length file is the classic torn/interrupted write. Refuse.
        raise LeaseUnreadable()
    return _decode(raw)


def _write(record: dict) -> None:
    """Durably replace the lease record. Caller MUST hold :func:`_lock` AND must
    already have validated ownership — this overwrites unconditionally.

    Atomic (tmp + os.replace) AND durable (fsync before the replace) via
    atomicio, so a crash cannot leave a present-but-empty record that would
    read as "unowned". Mode 0600 where the platform honours it.
    """
    _require_lock()
    path = _path()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    from . import atomicio
    atomicio.publish(tmp, path, json.dumps(record, indent=2),
                     chmod=0o600, fsync=True)


def _create_exclusive(record: dict) -> bool:
    """Create the lease ONLY if it does not exist. True when we created it.

    Belt to `_lock`'s braces: `O_CREAT|O_EXCL` is atomic in the filesystem on
    both platforms, so even if the lock were somehow lost, two claimants cannot
    both create. It protects CREATION ONLY and is explicitly not the protection
    for release — that is `_lock` plus the `lease_id` check in `_remove_if`.
    """
    _require_lock()
    path = _path()
    data = json.dumps(record, indent=2).encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    from . import atomicio
    atomicio.fsync_dir(path.parent)
    return True


def _remove_if(owner: str, lease_id: str) -> None:
    """Delete the lease ONLY if it is still the exact one the caller validated.

    Caller MUST hold :func:`_lock`. The re-read inside the lock is what makes
    this a compare-and-delete rather than a check-then-unlink: a record that
    changed since validation (released and re-claimed by somebody else in the
    interim) has a different `lease_id`, and deleting it would strand a live
    browser with no lease. That case is a refusal, not a delete.
    """
    _require_lock()
    try:
        record = _read()
    except LeaseUnreadable:
        # Corrupt on the way out. Do NOT unlink blind — a corrupt record still
        # means "owned by someone" to every reader, and removing it would open
        # the browser to the next claimant. An operator resolves this.
        raise
    if record is None:
        return                                   # already released; idempotent
    if owner_of(record) != canonical(owner) or record.get("lease_id") != lease_id:
        raise OwnershipRefused()
    try:
        _path().unlink()
    except FileNotFoundError:
        pass
    except OSError as err:
        # Could not release. Fail closed — the lease stands.
        raise OwnershipRefused() from err


# --- the cross-process transaction lock -------------------------------------

#: Bound on acquisition. A byte-range lock cannot go stale — the OS drops it
#: when the holding process dies or its descriptor closes, kill -9 included — so
#: the only unbounded wait is a live-but-wedged holder. A bounded default turns
#: that into a visible TimeoutError instead of a silent hang.
LOCK_TIMEOUT = 30.0

_LOCAL = threading.local()
_THREAD_LOCK = threading.Lock()


def _lock_path():
    d = config.MEMORY_DIR / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{LOCK_NAME}.lock"


def _acquire_file_lock(fh, deadline) -> None:
    """Take an exclusive byte-range lock on `fh`, polling until `deadline`.

    Byte-range locks are owned by the file HANDLE on both platforms, so two
    descriptors contend even inside one process — the same property `fcntl.flock`
    has, and the reason this works cross-thread as well as cross-process.
    """
    while True:
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                # LK_NBLCK locks one byte from the current file position and
                # fails immediately when another handle holds it — the
                # non-blocking primitive the polling loop needs. LK_LOCK would
                # block with its own fixed retry policy we cannot bound.
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:                                   # pragma: no cover
                raise OwnershipRefused()            # no lock primitive: refuse
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"browser lease lock not acquired in {LOCK_TIMEOUT}s")
            time.sleep(0.01)


def _release_file_lock(fh) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass                     # closing the handle drops the lock regardless


@contextlib.contextmanager
def _lock(timeout: float = LOCK_TIMEOUT):
    """The lease transaction lock. ALWAYS the outer lock (see LOCK ORDER).

    A REAL cross-process lock on both platforms — `fcntl.flock` on POSIX,
    `msvcrt.locking` on Windows — because `proclock` has no cross-process teeth
    without `fcntl` and release is a read-validate-then-unlink that O_EXCL
    cannot protect.

    Held across the COMPLETE read/validate/write-or-delete transaction.
    Reentrant per thread, so a nested `verify()` inside a `session()` claim does
    not self-deadlock. An in-process `threading.Lock` is taken first so thread
    exclusion never depends on platform byte-range semantics; the file lock then
    adds the cross-process half. Both are released in `finally`, and the OS
    drops the file lock anyway if the process dies mid-transaction.
    """
    depth = getattr(_LOCAL, "depth", 0)
    if depth:                                   # reentrant: already ours
        _LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _LOCAL.depth -= 1
        return

    deadline = time.monotonic() + max(0.0, timeout)
    if not _THREAD_LOCK.acquire(timeout=max(0.0, timeout)):
        raise TimeoutError(f"browser lease lock not acquired in {timeout}s")
    try:
        fh = open(_lock_path(), "a+b")
        try:
            _acquire_file_lock(fh, deadline)
            _LOCAL.depth = 1
            try:
                yield
            finally:
                _LOCAL.depth = 0
                _release_file_lock(fh)
        finally:
            fh.close()
    finally:
        _THREAD_LOCK.release()


def _holding_lock() -> bool:
    """Whether this thread is inside `_lock()`. Used to assert the transaction
    discipline at each mutation rather than trusting call sites."""
    return bool(getattr(_LOCAL, "depth", 0))


def _require_lock() -> None:
    if not _holding_lock():
        raise OwnershipRefused()            # fail closed on a discipline break


# --- public API (each acquires the lock itself) -----------------------------

def current() -> dict | None:
    """The lease record, or None when unowned. Raises on a corrupt record."""
    with _lock():
        return _read()


def owner_of(record: dict | None) -> str:
    return canonical((record or {}).get("owner", ""))


def check(owner) -> dict | None:
    """Validate `owner` against the lease WITHOUT claiming.

    Returns the existing record (None when unowned). Raises
    :class:`OwnershipRefused` when the caller is untrusted or the browser
    belongs to somebody else.

    ALWAYS performs its own locked read. An earlier revision let a caller pass
    a pre-read record with `_loaded=True` to avoid re-entering the lock — an
    escape hatch that trusted a record whose provenance this function could not
    check, and which nothing used. `_lock` is reentrant, so a caller already
    inside the transaction pays only a depth counter for re-reading.
    """
    name = require_owner(owner)
    with _lock():
        record = _read()
        if record is not None and owner_of(record) != name:
            raise OwnershipRefused()
        return record


def verify(owner) -> dict | None:
    """The read-side gate used by cookie custody. Alias of :func:`check`."""
    return check(owner)


def claim(owner: str, transport: str, fingerprint: str = "") -> dict:
    """Record `owner` as the browser's owner.

    Acquires :func:`_lock` itself (reentrant, so a caller already inside the
    transaction — `browser.session` — nests safely). The whole
    read/validate/create-or-update runs inside it, so the fingerprint backfill
    below cannot race a concurrent release: that release either happens wholly
    before our read or wholly after our write.

    Idempotent for the same owner. Refuses for a different one. Called only
    AFTER a transport was successfully built, so a failed build cannot strand a
    lease that (on the remote-CDP path) could never be released.

    Enforces the principal policy ITSELF rather than trusting its caller. This
    is a public entry point: without the check, a direct caller could mint a
    lease for `shared`, a `web-*` session id, or any unrecognised string, and
    every later owner comparison would then honour it.
    """
    name = require_owner(owner)
    with _lock():
        record = _read()
        if record is not None:
            if owner_of(record) != name:
                raise OwnershipRefused()
            # Same owner re-entering. Keep claimed_at AND lease_id stable — the
            # generation token must not move under a release that already
            # validated it. Refresh only the diagnostic fingerprint, and only
            # when we learned one and had none.
            if fingerprint and not record.get("fingerprint"):
                record["fingerprint"] = fingerprint
                _write(record)
            return record
        record = {
            "schema_version": SCHEMA_VERSION,
            "owner": name,
            # Immutable generation token: distinguishes THIS lease from a later
            # one held by the same owner, so a paused release cannot delete a
            # lease that was already released and re-claimed.
            "lease_id": secrets.token_hex(16),
            "transport": str(transport or "unknown"),
            "claimed_at": time.time(),
            "fingerprint": str(fingerprint or ""),
        }
        if _create_exclusive(record):
            return record
        # Lost the exclusive create. Re-read and accept the winner's answer —
        # never overwrite it.
        existing = _read()
        if existing is None or owner_of(existing) != name:
            raise OwnershipRefused()
        return existing


def release(owner: str, lease_id: str) -> None:
    """Drop the lease. Caller MUST already have verifiably destroyed the
    credentialed browser state.

    `lease_id` is the token read during validation. It is re-checked against
    disk under the lock, so this is a compare-and-delete: a lease that was
    released and re-claimed by somebody else since validation has a different
    token and is REFUSED, never deleted. Without that check the interleaving in
    this module's header deletes a live owner's lease.

    This function does not and cannot check that destruction happened — it is
    the last step of `browser.relinquish`, which does. Nothing else may call it.
    """
    name = canonical(owner)
    if not lease_id:
        raise OwnershipRefused()      # no token ⇒ nothing safe to delete
    with _lock():
        _remove_if(name, lease_id)


def clear_for_tests() -> None:
    """Remove the lease unconditionally. TEST-ONLY.

    The one unconditional drop in the codebase, and it exists ONLY so a test
    fixture can reset the world between cases without simulating a browser
    teardown. It must have NO caller under `olympus/` — a structural test
    (`test_clear_for_tests_has_no_production_callers`) enforces that, because
    any production path reaching this becomes a way to transfer a credentialed
    browser without destroying its state.

    Production release goes through `browser.relinquish`, which verifies
    destruction first. Reconfiguration (`browser.set_transport_factory`)
    deliberately does NOT call this: it preserves owner and `lease_id`.
    """
    with _lock():
        try:
            _path().unlink()
        except (FileNotFoundError, OSError):
            pass
