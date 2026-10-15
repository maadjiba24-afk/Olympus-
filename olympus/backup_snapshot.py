"""Bounded filesystem backup images, captured once through held native roots.

This is an enumerated per-file snapshot, not an application transaction. It
does not dump PostgreSQL. Preference guard acquisition may initialize its
coordination directory/file; existing payloads, permissions and recovery images
are preserved. The caller must hold archive allocation admission while collecting.
"""
from __future__ import annotations

from .native_cleanup import closing
import os
import stat

from . import archive_io as io, backup_archive as fmt
from .vault_storage import _Directory, _directory_identity

MAX_DEPTH = 64
MAX_DIRECTORY_ENTRIES = 3072
REPLAY_CACHE = frozenset(('responses','tool_results','context'))
NEVER_BACKUP = frozenset(('signing_seed',))


def custody_paths(selectors):
    """Bind literal custody paths before callbacks or a CWD change.

    Tilde is an ordinary filename, matching custody capture. No expanduser,
    resolve, linked-path traversal or permissive invalid-selection fallback.
    """
    result = set()
    for name in ('OLYMPUS_SECRET_KEY_FILE','OLYMPUS_SIGNING_SEED_FILE'):
        raw = selectors.get(name)
        if raw is None:
            continue
        if type(raw) is not str or len(raw) > io.custody.MAX_PATH_CHARS:
            raise ValueError('invalid bounded backup custody selector')
        if raw.strip():
            result.add(io.selected_path(raw.strip()))
    return frozenset(result)



def capture_exclusions(excluded):
    """Freeze private-file identities before any signing/encryption callbacks."""
    if type(excluded) is not frozenset or len(excluded)>2:
        raise ValueError('invalid bounded backup exclusions')
    return tuple((path,io.file_identity(path,io.custody.MAX_FILE_BYTES))
                 for path in sorted(excluded,key=str))


def check_exclusions(excluded, captured):
    """Recheck prior authority; never adopt replacement custody as a new file."""
    if (type(captured) is not tuple or len(captured)>2
            or any(type(row) is not tuple or len(row)!=2 for row in captured)):
        raise ValueError('invalid frozen backup exclusions')
    if frozenset(path for path,_identity in captured)!=excluded or len(captured)!=len(excluded):
        raise ValueError('backup exclusion selection changed')
    for path,identity in captured:
        if type(identity) is not tuple or len(identity)!=5:
            raise ValueError('invalid backup exclusion identity')
        if io.file_identity(path,io.custody.MAX_FILE_BYTES)!=identity:
            raise ValueError('backup custody exclusion identity changed')
    return dict(captured)


def collect(root, *, full, excluded, captured_exclusions=None):
    """Capture before recovery under the same native preference mutex.

    The already-held archive root supplies authority. Acquisition may create
    coordination files, but cannot adopt a replacement root. The guard exits
    successfully before captured images become available for publication.
    """
    from . import prefs, prefs_native
    if type(full) is not bool or type(excluded) is not frozenset or len(excluded)>2:
        raise ValueError('invalid backup inventory selection')
    prefs._backup_collection_preflight()
    root.check()
    identity = _directory_identity(root)
    with prefs_native.transaction(root.path, root_identity=identity, allow_nested=False) as tx:
        root.check(); tx.check()
        images = _collect_locked(root, full=full, excluded=excluded,
                                 captured_exclusions=captured_exclusions)
        tx.check(); root.check()
    return images


def _collect_locked(root, *, full, excluded, captured_exclusions=None):
    """Return bounded {portable relative name: exact bytes} under held root.

    The root must exist. Every descendant acquisition is compared to the
    inventory entry before reading beneath it; all active ancestors remain
    checked. This refuses detected replacement and additional file links,
    but does not exclude metadata-preserving hostile writes by administrators.
    """
    if type(full) is not bool or type(excluded) is not frozenset or len(excluded)>2:
        raise ValueError('invalid backup inventory selection')
    root.check()
    excluded_names = {os.path.normcase(str(path)) for path in excluded}
    if captured_exclusions is None:
        captured_exclusions = capture_exclusions(excluded)
    protected = check_exclusions(excluded,captured_exclusions)
    protected_ids = {identity[:2] for identity in protected.values()}
    images = {}; total = 0; entries_seen = 0

    def walk(directory, prefix, depth):
        nonlocal total, entries_seen
        if depth > MAX_DEPTH:
            raise ValueError('backup directory depth bound exceeded')
        spellings = set(); local_count = 0
        with closing(directory.entries()) as entries:
            for entry in entries:
                local_count += 1; entries_seen += 1
                if local_count > MAX_DIRECTORY_ENTRIES or entries_seen > fmt.MAX_ENTRIES:
                    raise ValueError('backup directory inventory bound exceeded')
                io._filename(entry.name)
                folded = entry.name.casefold()
                if folded in spellings:
                    raise ValueError('backup directory has case-colliding entries')
                spellings.add(folded)
                path = directory.path/entry.name
                relative = prefix + entry.name
                from . import prefs
                kind = prefs._backup_path_kind(fmt.relative(relative))
                if kind == 'recovery':
                    raise ValueError('native preference recovery authority is not portable')
                if kind == 'coordination':
                    continue
                if (depth == 0 and (folded in ('backups', '.olympus-backup-evidence') or not full and folded in REPLAY_CACHE)
                        or folded in NEVER_BACKUP
                        or os.path.normcase(str(path)) in excluded_names
                        or entry.name.startswith('.') and entry.name.endswith('.tmp')):
                    continue
                info = entry.stat(follow_symlinks=False)
                if (info.st_dev,info.st_ino) in protected_ids:
                    continue
                if getattr(info,'st_file_attributes',0) & 0x400:
                    raise ValueError('backup inventory contains a reparse entry')
                directory._case_spelling(entry.name)
                if stat.S_ISDIR(info.st_mode):
                    # _Directory.child is restricted to vault key names;
                    # ordinary backup folders retain portable Unicode names.
                    with closing(_Directory(path)) as child:
                        if _directory_identity(child) != (info.st_dev,info.st_ino):
                            raise ValueError('backup inventory directory was replaced')
                        directory.check(); directory._case_spelling(entry.name)
                        walk(child,relative+'/',depth+1)
                        directory.check(); directory._case_spelling(entry.name)
                elif stat.S_ISREG(info.st_mode):
                    if len(images) >= fmt.MAX_FILES:
                        raise ValueError('backup file count bound exceeded')
                    remaining = min(fmt.MAX_FILE,fmt.MAX_SOURCE-total)
                    before = io._identity(info,remaining)
                    raw = io.read_file(path,remaining,expected_identity=before)
                    directory.check(); directory._case_spelling(entry.name)
                    if io._identity(entry.stat(follow_symlinks=False),remaining) != before:
                        raise ValueError('backup inventory file changed during capture')
                    total += len(raw)
                    images[relative] = raw
                else:
                    raise ValueError('backup inventory contains a linked or nonregular entry')
                root.check()
        directory.check()

    walk(root,'',0)
    fmt.validate_paths(images)
    if any(io.file_identity(path,io.custody.MAX_FILE_BYTES) != identity for path,identity in protected.items()):
        raise ValueError('backup custody exclusion identity changed during collection')
    root.check()
    return dict(sorted(images.items()))
