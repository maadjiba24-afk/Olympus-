"""Off-droplet, encrypted, signed data backups — survive losing the machine.

A public Olympus instance keeps everything irreplaceable on one box: per-user
memory, accounts, the *encrypted* OAuth tokens that reach people's Gmail and
Calendar, and the signed decision log. If that droplet dies, it is gone. This
module makes a recoverable copy and gets it off the machine.

What it does, and why each part is here:

- **Archive** `MEMORY_DIR` into a single deterministic `.tar.gz` with a manifest
  that records every file's SHA-256. The replay caches (`responses/`,
  `tool_results/`, `context/`) are large and reproducible, so they're excluded
  unless `--full`; user/account/audit data is always included.
- **Encrypt at rest** with the vault key (`OLYMPUS_SECRET_KEY` or
  `OLYMPUS_SECRET_KEY_FILE`, Fernet). The off-droplet copy must not be plaintext
  PII/tokens — delivering an unencrypted archive off-machine is refused unless
  explicitly allowed.
- **Sign** the archive's hash with the witness Ed25519 root of trust, so a
  restored archive is provably the one we wrote (tamper-evident).
- **Deliver** via `OLYMPUS_BACKUP_CMD` ({path} → archive path): you bring the
  uploader (rclone / aws / scp), we never touch your storage credentials.
- **Restore** verifies the signature and every file hash, refuses path-traversal
  entries, and won't clobber a non-empty target unless forced.

Everything is best-effort at the edges (a failed scheduled backup alerts the
operator and never crashes the heartbeat) but strict at the core (a corrupt or
tampered archive fails restore loudly rather than silently restoring garbage).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path

from . import config, backup_facts as stage_facts
from . import native_cleanup

SCHEMA = "olympus-backup/1"
_MANIFEST_NAME = "BACKUP_MANIFEST.json"
# Reproducible operational caches — excluded by default to keep archives small;
# losing them only forfeits replay of *old* runs, never user data.
_REPLAY_CACHE = ("responses", "tool_results", "context")
# Files that must NEVER leave the machine inside an archive, even a full one.
# The signing seed is the release/audit root of trust; archives are encrypted,
# but under the vault key — a different (often weaker) custody domain
# than the seed itself (docs/SIGNING.md).
_NEVER_BACKUP = ("signing_seed",)
_LOCK = threading.Lock()


class BackupError(RuntimeError):
    pass


def _io(path) -> Path:
    from .assessment_evidence import _windows_extended_path
    return Path(_windows_extended_path(path)) if os.name == "nt" else Path(path)


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("olympus-council")
    except Exception:
        return "unknown"


def _backups_dir() -> Path:
    d = config.MEMORY_DIR / "backups"
    _io(d).mkdir(parents=True, exist_ok=True)
    return d


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with _io(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _custody_paths() -> set[Path]:
    """Exact configured key files that must never enter their own backup.

    The basename guard remains for the default signing-seed location, while
    this covers custom filenames and the file-backed vault key.
    """
    out: set[Path] = set()
    for name in ("OLYMPUS_SIGNING_SEED_FILE", "OLYMPUS_SECRET_KEY_FILE"):
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            out.add(_io(Path(raw).expanduser()).resolve())
        except (OSError, RuntimeError) as err:
            raise BackupError("backup custody path is unavailable") from err
    return out


def _included_files(root: Path, *, full: bool) -> list[Path]:
    """Every file under MEMORY_DIR to back up, excluding the backups dir itself
    (no recursive growth), temp files, the signing seed (never leaves the
    machine), and — unless `full` — the replay caches."""
    # Path.rglob/is_file can suppress traversal/stat failures, including
    # native Windows MAX_PATH errors, and certify an incomplete inventory.
    # Explicit scandir/stat must succeed for every non-excluded entry.
    root = Path(root)
    custody_paths = _custody_paths()
    out, pending = [], [root]
    try:
        while pending:
            directory = pending.pop()
            info = _io(directory).lstat()
            if (not stat.S_ISDIR(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise BackupError("backup inventory has a linked or non-directory path")
            with os.scandir(_io(directory)) as entries:
                for entry in entries:
                    p = directory / entry.name
                    top = p.relative_to(root).parts[0]
                    if top == "backups" or (not full and top in _REPLAY_CACHE):
                        continue
                    if p.name in _NEVER_BACKUP or _io(p).resolve() in custody_paths:
                        continue
                    if p.name.startswith(".") and p.suffix == ".tmp":
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if (stat.S_ISLNK(info.st_mode)
                            or getattr(info, "st_file_attributes", 0) & 0x400):
                        raise BackupError("backup inventory has a linked entry")
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(p)
                    elif stat.S_ISREG(info.st_mode):
                        out.append(p)
                    else:
                        raise BackupError("backup inventory has a nonregular entry")
    except (OSError, RuntimeError) as err:
        if isinstance(err, BackupError):
            raise
        raise BackupError("backup inventory is unavailable; no archive was published") from err
    return sorted(out)


# --- create --------------------------------------------------------------

def create(*, full: bool = False, label: str = "", operation_id: str | None = None) -> dict:
    """Publish one captured signed pair with a retained recovery operation ID."""
    return _create(full=full, label=label, operation_id=operation_id)


def _create(*, full=False, label='', operation_id=None, _authority=None):
    """Publish one signed pair from the exact bytes captured for its manifest.

    A UUID32 operation ID owns the immutable plan and recovery images. Inspect
    that ID after interruption. A durable active intent can be resumed using
    retained bytes; pre-intent failures may not be resumable. Do not recreate
    an interrupted logical operation by reacquiring custody. This copies
    filesystem memory only, not PostgreSQL.
    Nonempty historical filename labels are refused for the managed namespace.
    """
    from contextlib import nullcontext
    import uuid
    from . import archive_io as aio, backup_archive as fmt, backup_signing as signing
    from . import backup_snapshot as snapshot, backup_publication as publication
    from .backup_restore import _raise_public_error
    from .vault_storage import _Directory
    if type(full) is not bool or type(label) is not str or label:
        raise BackupError('backup creation requires a boolean full option and an empty filename label')
    op = uuid.uuid4().hex if operation_id is None else operation_id
    publication._opid(op)
    failure = None; result = None; confirmed = ()
    try:
        from . import prefs
        prefs._backup_collection_preflight()
        if _authority is not None:
            from .backup_authority import require
            require(_authority, 'create')
            _authority.preflight_creation()
        root_path = aio.selected_directory(config.MEMORY_DIR) if _authority is None else _authority.memory_path
        selected = ({name:os.environ.get(name) for name in (
            'OLYMPUS_SECRET_KEY','OLYMPUS_SECRET_KEY_FILE','OLYMPUS_SIGNING_SEED_FILE','OLYMPUS_ENV')}
            if _authority is None else {**dict(_authority.signing_selection), **dict(_authority.raw_vault_selection)})
        excluded = snapshot.custody_paths(selected) if _authority is None else _authority.excluded
        def unchanged():
            if _authority is not None:
                require(_authority, 'create')
                _authority.preflight_creation()
                return
            if (aio.selected_directory(config.MEMORY_DIR) != root_path
                    or any(os.environ.get(name) != value for name,value in selected.items())):
                raise BackupError('backup configuration changed during creation')
        # Admission and relative key binding precede signing callbacks; acquire
        # each private material once before collecting potentially large data.
        with aio.operation('backup',selectors=selected if _authority is None else _authority.vault_selectors,
                capture_key=False,cwd=None if _authority is None else _authority.cwd) as session:
            with (_held_backup_directory(root_path) if _authority is None else nullcontext(_authority.memory)) as root:
                protected = snapshot.capture_exclusions(excluded) if _authority is None else _authority.exclusion_identities
                root.check(); unchanged()
                signer = (signing.capture_signer() if _authority is None else signing.capture_signer(
                    selection=_authority.signing_selection,cwd=_authority.cwd,policy=_authority.policy))
                root.check(); unchanged(); snapshot.check_exclusions(excluded,protected)
                session.select_key()
                root.check(); unchanged(); snapshot.check_exclusions(excluded,protected)
                images = snapshot.collect(root,full=full,excluded=excluded,captured_exclusions=protected)
                stamp = time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())
                manifest = {'schema':SCHEMA,'created':stamp,'olympus_version':_version(),
                    'host':'','full':full,'file_count':len(images),
                    'files':[{'path':name,'sha256':_sha256_bytes(raw),'bytes':len(raw)}
                             for name,raw in images.items()]}
                payloads = {'data/'+name:raw for name,raw in images.items()}
                fmt.validate_inventory(manifest,payloads)
                encoded = aio.BoundedBuffer(fmt.MAX_MANIFEST)
                encoder = json.JSONEncoder(sort_keys=True,separators=(',',':'),ensure_ascii=True)
                for chunk in encoder.iterencode(manifest):
                    encoded.write(chunk.encode('utf-8'))
                manifest_bytes = encoded.getvalue()
                raw = aio.encode_tar([(fmt.MANIFEST,manifest_bytes),*payloads.items()],
                    max_expanded=fmt.MAX_EXPANDED,max_compressed=aio.MAX_PLAINTEXT)
                encrypted = session.passphrase is not None
                payload = session.encrypt(raw) if encrypted else raw
                digest = _sha256_bytes(payload)
                sidecar = signer.signature(digest)
                verification = signer.verify(digest,sidecar)
                if not verification['signature_ok']:
                    raise BackupError('captured backup signer did not verify its own signature')
                unchanged(); root.check(); root._case_spelling('backups')
                with (_held_backup_directory(root.child('backups',create=True)) if _authority is None
                      else nullcontext(_authority.backups(create=True))) as destination:
                    root.check(); unchanged()
                    receipt = publication.publish(destination,payload,sidecar,
                        {'encrypted':encrypted,'files':len(images),'full':full},operation_id=op)
                    confirmed = (stage_facts.ConfirmedStage('publication', receipt['operation_id'],
                        receipt['archive_sha256'], receipt['signature_sha256'], receipt['plan_sha256']),)
                    destination.check(); root.check(); unchanged()
                result = {**receipt,**verification,'sha256':digest,
                    'archive_encryption':({'encrypted':True,'format_version':1,
                        'context_authenticated':True,'legacy_unbound':False} if encrypted else
                        aio.encryption_metadata(plaintext=True)),
                    'backup_scope':'filesystem_memory_only','database_included':False,
                    'application_consistent_snapshot':False}
    except BaseException as error:
        failure = error
    if failure is not None:
        if isinstance(failure,Exception):
            public = BackupError('backup creation did not complete; retain evidence and inspect its operation ID; no PLAINTEXT fallback')
            public.operation_id = op
            if type(failure) is publication.PublicationError:
                if type(failure.operation_id) is str and publication._ID.fullmatch(failure.operation_id):
                    public.operation_id = failure.operation_id
                public.phase = failure.phase
            stage_facts.carry(failure, public, confirmed)
            failure = None
            _raise_public_error(public)
        stage_facts.carry(failure, failure, confirmed)
        stage_facts.retain_id(failure, op)
        raise failure
    return result



def _write_signature(archive: Path, digest: str) -> bool:
    """Write `<archive>.sig.json` signing the archive's SHA-256 with the witness
    key. Best-effort: returns False (unsigned) if crypto is unavailable."""
    try:
        from . import witness
        if not witness.available():
            return False
        sig = {"schema": SCHEMA, "sha256": digest,
               "signature": witness.sign(digest.encode()),
               "publicKey": witness.public_key_hex(),
               "seedDerivation": witness.SEED_DERIVATION}
        _io(archive.parent / (archive.name + ".sig.json")).write_text(
            json.dumps(sig, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


# --- deliver / prune -----------------------------------------------------

def run(*, full: bool = False, deliver_off: bool = True,
        operation_id: str | None = None, delivery_id: str | None = None) -> dict:
    """Run captured local stages once and retain partial progress on failure."""
    from . import backup_workflow
    return backup_workflow.run(full=full, deliver_off=deliver_off,
                               operation_id=operation_id, delivery_id=delivery_id)


# --- restore -------------------------------------------------------------

def _backup_boundary(function):
    """One public failure without native/provider text, chained locals or keys."""
    from functools import wraps
    @wraps(function)
    def guarded(*args,**kwargs):
        from . import backup_publication as publication, backup_restore as recovery
        from . import backup_delivery as delivery
        failure = None
        try:
            return function(*args,**kwargs)
        except Exception as error:
            failure = error
        public = BackupError('backup '+function.__name__+' did not complete; retain evidence and inspect any operation ID')
        if type(failure) in (publication.PublicationError,recovery.RestoreError):
            op = failure.operation_id
            if type(op) is str and publication._ID.fullmatch(op):public.operation_id = op
        if type(failure) is publication.PublicationError:
            completed=getattr(failure,'completed_retirement_ids',None)
            if (type(completed) is tuple and len(completed)<=publication.MAX_OPERATIONS
                    and all(type(value) is str and publication._ID.fullmatch(value) for value in completed)):
                public.completed_retirement_ids=completed
                public.files_deleted=0
        if type(failure) is delivery.DeliveryError:
            for key in ('publication_id', 'delivery_id'):
                value = getattr(failure, key)
                if value is None or (type(value) is str and publication._ID.fullmatch(value)):
                    setattr(public, key, value)
            public.delivery_progress = dict(failure.delivery_progress)
        stage_facts.carry(failure, public)
        failure = None
        recovery._raise_public_error(public)
    return guarded


@_backup_boundary
def deliver(archive_path: str, *, delivery_id: str | None = None) -> dict:
    """Attempt one journaled archive/signature command pair without retry.

    Requires a completed managed publication and an authenticated nondefault
    signer. Explicit plaintext permission affects only the encryption gate.
    Command completion is not evidence of remote retention or restorability.
    """
    from . import backup_delivery
    return backup_delivery.deliver(archive_path, delivery_id=delivery_id)


@_backup_boundary
def inspect_delivery(archive_path: str) -> dict:
    """Observe retained delivery evidence without keys, commands or settlement."""
    from . import backup_delivery
    return backup_delivery.inspect_delivery(archive_path)


@_backup_boundary
def list_backups() -> list[dict]:
    """Observe local states without creating directories or settling receipts."""
    from . import backup_management
    return backup_management.list_backups()


@_backup_boundary
def prune(keep: int | None = None) -> dict:
    """Logically retire old managed pairs; retain all bytes and recovery evidence.

    Returns explicit batch progress and observed prior retirement states.
    This operation never deletes files or reclaims storage/history capacity.
    """
    from . import backup_management
    return backup_management.retire_backups(keep)


def _restore_boundary(operation_id):
    """Retain this operation ID even if an outer authority's cleanup fails."""
    from contextlib import contextmanager
    from . import backup_restore as recovery
    recovery._opid(operation_id)
    @contextmanager
    def held():
        failure = None
        confirmed = []
        try:
            yield confirmed
        except BaseException as error:
            failure = error
        if failure is not None:
            public_id = operation_id
            if type(failure) is recovery.RestoreError:
                candidate = failure.operation_id
                if type(candidate) is str and recovery._ID.fullmatch(candidate):public_id = candidate
            if isinstance(failure,Exception):
                public = recovery.RestoreError(public_id)
                stage_facts.carry(failure, public, tuple(confirmed))
                failure = None
                recovery._raise_public_error(public)
            stage_facts.carry(failure, failure, tuple(confirmed))
            stage_facts.retain_id(failure, public_id)
            raise failure
    return held()


def _held_backup_directory(path):
    """Drain a new directory or transferred child; preserve primary failure.

    An exact internal _Directory transfers its ownership to this context.
    Do not pass a borrowed workflow authority here.
    """
    from contextlib import contextmanager
    from .vault_storage import _Directory
    @contextmanager
    def held():
        directory,failure = None,None
        try:
            directory = path if type(path) is _Directory else _Directory(path)
            yield directory
            directory.check()
        except BaseException as error:
            failure = error
        finally:
            if directory is not None:
                owned, directory = directory, None
                try:native_cleanup.drain([owned.close], failure)
                except BaseException as error:failure = error
        if failure is not None:raise failure
    return held()


def _read_pair(archive, *, external, _authority=None):
    """Keep original input authorities until all consumption and writes finish.

    External mode explicitly accepts an off-machine copy without its journal;
    it makes no claim about local publication or retirement. Historical
    unreserved names carry legacy provenance. Reserved names require a local
    completed publication unless external mode was explicitly selected.
    """
    from contextlib import contextmanager, nullcontext
    from . import archive_io as aio, backup_archive as fmt, backup_publication as publication
    from .vault_storage import _Directory
    if type(external) is not bool:raise BackupError('invalid external archive selection')
    captured_parent = None
    if _authority is not None:
        from .backup_authority import require
        require(_authority, 'restore')
        if archive.parent == _authority.memory_path / 'backups':
            captured_parent = _authority.backups()
            if captured_parent is None:
                raise BackupError('the captured backup input directory was absent')
    @contextmanager
    def held():
        if publication.managed_name(archive.name) and not external:
            failure = None
            try:
                with publication.locked(archive.parent if captured_parent is None else captured_parent) as root:
                    raw,signature,receipt = publication.read_completed(root,archive.name)
                    local = {'kind':'local_completed','operation_id':receipt['operation_id'],
                        'plan_sha256':receipt['plan_sha256'],'archive_sha256':receipt['archive_sha256'],
                        'signature_sha256':receipt['signature_sha256']}
                    root.check()
                    try:
                        yield raw,signature,local,(root.root,root.control),root
                        root.check()
                    except BaseException as error:
                        # Preserve the consumer's recovery ID across the input
                        # publication boundary and any secondary cleanup fault.
                        failure = error
            except BaseException as error:
                if failure is None:failure = error
                else:stage_facts.carry(error, failure, stage_facts.observed(failure))
            if failure is not None:raise failure
        else:
            with (_held_backup_directory(archive.parent) if captured_parent is None else nullcontext(captured_parent)) as directory:
                raw = aio.read_file(archive,aio.MAX_CIPHERTEXT)
                directory.check()
                signature = aio.read_file(archive.with_name(archive.name+'.sig.json'),fmt.MAX_SIGNATURE,optional=True)
                directory.check()
                yield raw,signature,{'kind':'external_pair' if external else 'legacy_local'},(directory,),None
                directory.check()
    return held()


def _verification(raw, signature, publication, policy):
    from . import backup_signing as signing
    digest = _sha256_bytes(raw)
    result = signing.capture_verifier(signature,policy=policy).verify(digest,signature)
    return {**result,'sha256':digest,'local_publication':publication}


@_backup_boundary
def verify_archive(archive_path: str, *, external: bool = False) -> dict:
    """Verify one captured archive/signature image; do not claim restoration."""
    from . import archive_io as aio, backup_signing as signing
    selection,cwd = signing._selection(),os.getcwd()
    archive = aio.selected_path(archive_path)
    with aio.operation('backup',capture_key=False):
        policy = signing.capture_policy(selection=selection,cwd=cwd)
        with _read_pair(archive,external=external) as (raw,signature,publication,_directories,_lease):
            return {**_verification(raw,signature,publication,policy),
                'verification_scope':'signature_of_captured_archive_bytes',
                'decryption_verified':False,'manifest_integrity_verified':False}


def _restore_result(receipt):
    """Only the exact consumed digest and retained decisions reach the report."""
    evidence = receipt['evidence']
    decision = evidence.get('backup_verification',{
        'signer_authenticated':False,'signer_trust':'not_recorded_in_legacy_restore_plan',
        'local_publication':{'kind':'not_recorded'}})
    return {**receipt,**decision,'mismatched':[],
        'sha256':evidence['archive_sha256'],'consumed_archive_sha256':evidence['archive_sha256'],
        'signature_sha256':evidence['signature_sha256'],'signed':evidence['signed'],
        'signature_ok':evidence['signature_ok'],'signature_override':evidence['signature_override'],
        'archive_encryption':evidence['archive_encryption'],
        'restore_complete':True,'backup_scope':'filesystem_memory_only',
        'application_consistent_snapshot':False,'database_included':False}


@_backup_boundary
def restore(archive_path: str, into: str | Path | None = None, *, force: bool = False,
            insecure: bool = False, external: bool = False, operation_id: str | None = None) -> dict:
    """Verify one consumed image and restore into an explicit offline target."""
    return _restore(archive_path, into=into, force=force, insecure=insecure,
                    external=external, operation_id=operation_id)


def _restore(archive_path, into=None, *, force=False, insecure=False, external=False,
             operation_id=None, _authority=None):
    """Verify/decode ALL images before preparing an explicit offline restore.

    Force permits only another operation in an intact restore-owned root.
    Insecure overrides a well-formed signature decision only; custody, archive
    bounds, path rules and integrity remain mandatory.
    """
    from contextlib import nullcontext
    import uuid
    from . import archive_io as aio, backup_archive as fmt, backup_restore as recovery
    from . import backup_snapshot as snapshot, backup_signing as signing
    from .vault_storage import _Directory
    if (into is None or type(force) is not bool or type(insecure) is not bool
            or type(external) is not bool):
        raise BackupError('restore requires an explicit offline destination and boolean options')
    op = uuid.uuid4().hex if operation_id is None else operation_id
    recovery._opid(op)
    if _authority is not None:
        from .backup_authority import require
        require(_authority, 'restore')
    selection,cwd = ((signing._selection(),os.getcwd()) if _authority is None else
                     (_authority.signing_selection,_authority.cwd))
    with _restore_boundary(op) as confirmed:
        archive = aio.selected_path(archive_path) if _authority is None else _authority.bind_file(archive_path)
        destination = aio.selected_directory(into) if _authority is None else _authority.bind_directory(into)
        live_path = aio.selected_directory(config.MEMORY_DIR) if _authority is None else _authority.memory_path
        selected = ({name:os.environ.get(name) for name in (
            'OLYMPUS_SECRET_KEY','OLYMPUS_SECRET_KEY_FILE','OLYMPUS_SIGNING_SEED_FILE','OLYMPUS_ENV')}
            if _authority is None else {**dict(_authority.signing_selection), **dict(_authority.raw_vault_selection)})
        excluded = snapshot.custody_paths(selected) if _authority is None else _authority.excluded
        def unchanged():
            if _authority is not None:
                require(_authority, 'restore')
                return
            signing._check_selection(selection)
            if (aio.selected_directory(config.MEMORY_DIR)!=live_path
                    or any(os.environ.get(name)!=value for name,value in selected.items())):
                raise BackupError('backup restore selection changed during validation')
        with aio.operation('backup',selectors=selected if _authority is None else _authority.vault_selectors,
                capture_key=False,cwd=None if _authority is None else _authority.cwd) as session:
            with (_held_backup_directory(live_path) if _authority is None else nullcontext(_authority.memory)) as live:
                policy = signing.capture_policy(selection=selection,cwd=cwd) if _authority is None else _authority.policy
                live.check();unchanged()
                with _read_pair(archive,external=external,_authority=_authority) as (raw,signature,publication,directories,lease):
                    verified = _verification(raw,signature,publication,policy)
                    live.check();unchanged()
                    for directory in directories:directory.check()
                    if not verified['signature_ok'] and not insecure:
                        raise BackupError('archive signature is unavailable, invalid or untrusted')
                    if raw[:2]==b'\x1f\x8b':
                        payload = raw;encryption = aio.encryption_metadata(plaintext=True)
                    else:
                        decoded = session.decrypt(raw)
                        payload = decoded.data;encryption = aio.encryption_metadata(decoded)
                    _manifest,changes = fmt.decode(payload)
                    evidence = {'archive_sha256':verified['sha256'],
                        'signature_sha256':verified['signature_sha256'],'signed':verified['signed'],
                        'signature_ok':verified['signature_ok'],
                        'signature_override':not verified['signature_ok'] and insecure,
                        'archive_encryption':encryption,
                        'backup_verification':{key:verified[key] for key in (
                            'signature_math_valid','signer_trusted','signer_trust','signer_authenticated',
                            'signer_default','publicKey','local_signing_custody','local_publication')}}
                    live.check();unchanged()
                    for directory in directories:directory.check()
                    receipt = recovery.restore(destination,changes,evidence,operation_id=op,live_root=live,
                        forbidden=(*excluded,archive,archive.with_name(archive.name+'.sig.json')),
                        protected_directories=directories,input_lease=lease,force=force)
                    confirmed.append(stage_facts.ConfirmedStage('restore',receipt['operation_id'],
                        receipt['evidence']['archive_sha256'],receipt['evidence']['signature_sha256'],receipt['plan_sha256']))
                    live.check();unchanged()
                    return _restore_result(receipt)


@_backup_boundary
def resume_restore(into, *, operation_id):
    """Use retained verified images; never reopen the archive or reacquire keys."""
    from . import archive_io as aio, backup_restore as recovery, backup_snapshot as snapshot
    with _restore_boundary(operation_id) as confirmed:
        destination = aio.selected_directory(into)
        live = aio.selected_directory(config.MEMORY_DIR)
        excluded = snapshot.custody_paths(os.environ)
        with aio.operation('backup',capture_key=False):
            receipt=recovery.resume(destination,operation_id=operation_id,live_root=live,forbidden=excluded)
            confirmed.append(stage_facts.ConfirmedStage('restore',receipt['operation_id'],
                receipt['evidence']['archive_sha256'],receipt['evidence']['signature_sha256'],receipt['plan_sha256']))
            return _restore_result(receipt)


@_backup_boundary
def inspect_restore(into, *, operation_id):
    """Observe retained evidence without publication or archive/key reads."""
    from . import archive_io as aio, backup_restore as recovery, backup_snapshot as snapshot
    with _restore_boundary(operation_id):
        destination = aio.selected_directory(into)
        live = aio.selected_directory(config.MEMORY_DIR)
        excluded = snapshot.custody_paths(os.environ)
        with aio.operation('backup',capture_key=False):
            return recovery.inspect(destination,operation_id=operation_id,live_root=live,forbidden=excluded)


def drill(archive_path: str | None = None, *, into=None, external: bool = False,
          operation_id: str | None = None) -> dict:
    """Restore one image offline and retain its directory and recovery evidence."""
    from . import backup_workflow
    return backup_workflow.drill(archive_path, into=into, external=external, operation_id=operation_id)


def _rmtree(p: Path) -> None:
    p = _io(p)
    if not p.exists():
        return
    for child in sorted(p.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
        else:
            child.rmdir()
    p.rmdir()
