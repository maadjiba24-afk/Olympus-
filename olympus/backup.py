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
- **Encrypt at rest** with the vault key (`OLYMPUS_SECRET_KEY`, Fernet). The
  off-droplet copy must not be plaintext PII/tokens — delivering an unencrypted
  archive off-machine is refused unless explicitly allowed.
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
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path

from . import config

SCHEMA = "olympus-backup/1"
_MANIFEST_NAME = "BACKUP_MANIFEST.json"
# Reproducible operational caches — excluded by default to keep archives small;
# losing them only forfeits replay of *old* runs, never user data.
_REPLAY_CACHE = ("responses", "tool_results", "context")
# Files that must NEVER leave the machine inside an archive, even a full one.
# The signing seed is the release/audit root of trust; archives are encrypted,
# but under OLYMPUS_SECRET_KEY — a different (often weaker) custody domain
# than the seed itself (docs/SIGNING.md).
_NEVER_BACKUP = ("signing_seed",)
_LOCK = threading.Lock()


class BackupError(RuntimeError):
    pass


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("olympus-council")
    except Exception:
        return "unknown"


def _backups_dir() -> Path:
    d = config.MEMORY_DIR / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _included_files(root: Path, *, full: bool) -> list[Path]:
    """Every file under MEMORY_DIR to back up, excluding the backups dir itself
    (no recursive growth), temp files, the signing seed (never leaves the
    machine), and — unless `full` — the replay caches."""
    backups = (root / "backups").resolve()
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rp = p.resolve()
        if rp == backups or backups in rp.parents:
            continue
        rel = p.relative_to(root)
        top = rel.parts[0] if rel.parts else ""
        if not full and top in _REPLAY_CACHE:
            continue
        if p.name in _NEVER_BACKUP:
            continue
        if p.name.startswith(".") and p.suffix == ".tmp":
            continue
        out.append(p)
    return out


# --- create --------------------------------------------------------------

def create(*, full: bool = False, label: str = "") -> dict:
    """Build one encrypted, signed backup archive of MEMORY_DIR. Returns a
    result dict (path, encrypted, signed, files, bytes). Atomic: a half-written
    archive is never left at the final path."""
    root = config.MEMORY_DIR
    root.mkdir(parents=True, exist_ok=True)
    files = _included_files(root, full=full)

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"olympus-backup-{stamp}" + (f"-{label}" if label else "")
    out_dir = _backups_dir()

    manifest = {
        "schema": SCHEMA,
        "created": stamp,
        "olympus_version": _version(),
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "full": full,
        "file_count": len(files),
        "files": [{"path": p.relative_to(root).as_posix(),
                   "sha256": _sha256_file(p),
                   "bytes": p.stat().st_size} for p in files],
    }

    with _LOCK:
        # 1) Build the .tar.gz into a temp file, then read its bytes.
        _tmp_fd, _tmp_name = tempfile.mkstemp(prefix=".bk-", suffix=".tar.gz",
                                              dir=out_dir)
        os.close(_tmp_fd)          # close the mkstemp fd; tarfile reopens by path
        tmp_tar = Path(_tmp_name)
        try:
            with tarfile.open(tmp_tar, "w:gz") as tar:
                info = tarfile.TarInfo(_MANIFEST_NAME)
                mbytes = json.dumps(manifest, indent=2).encode()
                info.size = len(mbytes)
                info.mtime = 0
                import io
                tar.addfile(info, io.BytesIO(mbytes))
                for p in files:
                    arc = "data/" + p.relative_to(root).as_posix()
                    ti = tar.gettarinfo(str(p), arcname=arc)
                    ti.mtime = 0                    # deterministic-ish archive
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    with p.open("rb") as fh:
                        tar.addfile(ti, fh)
            raw = tmp_tar.read_bytes()
        finally:
            tmp_tar.unlink(missing_ok=True)

        # 2) Encrypt at rest if the vault key is configured. Fall back to
        # plaintext ONLY when no key is set — if a key IS configured but
        # encryption fails, refuse to write a plaintext backup of the user's
        # data rather than silently downgrading (the operator believes it's
        # encrypted).
        from . import vault
        encrypted = False
        payload = raw
        suffix = ".tar.gz"
        if vault.available():
            try:
                payload = vault.encrypt_bytes(raw)
            except Exception as err:
                raise BackupError(
                    "encryption is configured (secret key set) but failed — "
                    "refusing to write a PLAINTEXT backup of your data: "
                    f"{err}") from err
            encrypted = True
            suffix = ".tar.gz.enc"

        final = out_dir / (name + suffix)
        tmp_final = out_dir / ("." + name + suffix + ".tmp")
        tmp_final.write_bytes(payload)
        os.replace(tmp_final, final)    # atomic publish

        # 3) Sign the on-disk archive bytes (tamper-evidence sidecar).
        digest = _sha256_bytes(payload)
        signed = _write_signature(final, digest)

    return {"path": str(final), "name": final.name, "encrypted": encrypted,
            "signed": signed, "files": len(files), "bytes": len(payload),
            "sha256": digest, "full": full}


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
        (archive.parent / (archive.name + ".sig.json")).write_text(
            json.dumps(sig, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


# --- deliver / prune -----------------------------------------------------

def deliver(archive_path: str) -> dict:
    """Hand the archive to OLYMPUS_BACKUP_CMD for off-droplet delivery. Refuses
    to ship a plaintext (unencrypted) archive off-machine unless explicitly
    allowed — that would expose PII/OAuth tokens on third-party storage."""
    cmd = config.backup_command()
    if not cmd:
        return {"delivered": False, "reason": "no OLYMPUS_BACKUP_CMD set "
                "(backup kept local only)"}
    archive = Path(archive_path)
    if not archive.exists():
        raise BackupError(f"archive not found: {archive_path}")
    if not archive.name.endswith(".enc") and not config.backup_allow_plaintext():
        return {"delivered": False, "reason": "refusing to deliver an "
                "UNENCRYPTED archive off-droplet — set OLYMPUS_SECRET_KEY to "
                "encrypt backups (or OLYMPUS_BACKUP_ALLOW_PLAINTEXT=1 if the "
                "destination is itself trusted)."}

    if "{path}" in cmd:
        argv = [a.replace("{path}", str(archive)) for a in shlex.split(cmd)]
    else:
        argv = shlex.split(cmd) + [str(archive)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise BackupError(
            f"delivery command failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}")
    return {"delivered": True, "via": shlex.split(cmd)[0]}


def list_backups() -> list[dict]:
    """Local backup archives, newest first."""
    out = []
    for p in _backups_dir().glob("olympus-backup-*"):
        if p.name.endswith(".sig.json") or p.name.startswith("."):
            continue
        st = p.stat()
        out.append({"name": p.name, "path": str(p), "bytes": st.st_size,
                    "mtime": st.st_mtime,
                    "encrypted": p.name.endswith(".enc")})
    return sorted(out, key=lambda b: b["mtime"], reverse=True)


def prune(keep: int | None = None) -> int:
    """Keep the newest `keep` local archives (and their signatures); delete the
    rest. Returns the number of archives removed. Off-droplet copies are not
    touched — your storage provider's retention governs those."""
    keep = config.backup_keep() if keep is None else keep
    archives = list_backups()
    removed = 0
    for b in archives[keep:]:
        Path(b["path"]).unlink(missing_ok=True)
        Path(b["path"] + ".sig.json").unlink(missing_ok=True)
        removed += 1
    return removed


def run(*, full: bool = False, deliver_off: bool = True) -> dict:
    """Create → deliver → prune, as the scheduled backup. Never raises: on
    failure it captures the error for the operator and returns a status dict, so
    the heartbeat keeps ticking."""
    try:
        res = create(full=full)
    except Exception as err:
        from . import errors
        errors.capture("backup.create", err)
        return {"ok": False, "stage": "create", "error": str(err)[:300]}

    delivery = {"delivered": False, "reason": "delivery skipped"}
    if deliver_off:
        try:
            delivery = deliver(res["path"])
        except Exception as err:
            from . import errors
            errors.capture("backup.deliver", err, context=res["name"])
            return {"ok": False, "stage": "deliver", "error": str(err)[:300],
                    **res}
    try:
        pruned = prune()
    except Exception:
        pruned = 0
    return {"ok": True, "pruned": pruned, **res, **delivery}


# --- restore -------------------------------------------------------------

def _safe_members(tar: tarfile.TarFile, dest: Path):
    """Yield only archive members that extract *inside* dest — rejecting
    absolute paths, `..` traversal, and symlinks/hardlinks (a maliciously
    crafted archive must never write outside the restore directory)."""
    dest = dest.resolve()
    for m in tar.getmembers():
        if m.issym() or m.islnk():
            raise BackupError(f"refusing link member in archive: {m.name}")
        target = (dest / m.name).resolve()
        if target != dest and dest not in target.parents:
            raise BackupError(f"unsafe path in archive (traversal): {m.name}")
        yield m


def verify_archive(archive_path: str) -> dict:
    """Check an archive's signature (if a sidecar exists) against the pinned/
    local witness key. Returns {signed, signature_ok, sha256}."""
    archive = Path(archive_path)
    digest = _sha256_file(archive)
    sig_path = archive.parent / (archive.name + ".sig.json")
    if not sig_path.exists():
        return {"signed": False, "signature_ok": False, "sha256": digest}
    sig = json.loads(sig_path.read_text(encoding="utf-8"))
    ok = False
    try:
        from . import witness
        ok = (sig.get("sha256") == digest and witness.verify_signature(
            sig.get("publicKey", ""), digest.encode(), sig.get("signature", "")))
    except Exception:
        ok = False
    return {"signed": True, "signature_ok": bool(ok), "sha256": digest,
            "publicKey": sig.get("publicKey", "")}


def restore(archive_path: str, into: str | Path | None = None, *,
            force: bool = False, insecure: bool = False) -> dict:
    """Restore an archive into `into` (default: MEMORY_DIR). Verifies the
    signature and every file's SHA-256, refuses path-traversal entries, and
    won't overwrite a non-empty target unless `force=True`. Raises BackupError
    on any integrity failure unless `insecure=True` (signature only)."""
    archive = Path(archive_path)
    if not archive.exists():
        raise BackupError(f"archive not found: {archive_path}")
    dest = Path(into) if into is not None else config.MEMORY_DIR

    v = verify_archive(str(archive))
    if v["signed"] and not v["signature_ok"] and not insecure:
        raise BackupError("archive signature is INVALID — it was altered or "
                          "signed by a different key. Pass insecure=True to "
                          "override (not recommended).")

    payload = archive.read_bytes()
    if archive.name.endswith(".enc"):
        from . import vault
        payload = vault.decrypt_bytes(payload)   # raises on wrong key/tamper

    if dest.exists() and any(dest.iterdir()) and not force:
        raise BackupError(f"refusing to restore into non-empty {dest} — pass "
                          "force=True to overwrite.")
    dest.mkdir(parents=True, exist_ok=True)

    import io
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        members = list(_safe_members(tar, dest))
        tar.extractall(dest, members=members)

    # Move the extracted data/ tree into place. Two safety properties:
    #  1. Every manifest path is checked for traversal — the tar layer is
    #     guarded by _safe_members, but the manifest drives these moves and is
    #     equally attacker-controlled, so a `../escape` entry must be rejected
    #     here too (otherwise os.replace could write above the restore root).
    #  2. All entries are VERIFIED before ANY file is moved, so a corrupt
    #     archive can't leave the target half-restored (a mix of new + old).
    manifest = json.loads((dest / _MANIFEST_NAME).read_text(encoding="utf-8"))
    data_root = dest / "data"
    dest_r, data_r = dest.resolve(), data_root.resolve()
    planned, mismatched = [], []
    for entry in manifest.get("files", []):
        rel = entry["path"]
        src, dst = data_root / rel, dest / rel
        try:
            src_r, dst_r = src.resolve(), dst.resolve()
        except (OSError, RuntimeError):
            mismatched.append(rel + " (unresolvable path)")
            continue
        # Reject any entry whose source or destination escapes its root.
        if (src_r != data_r and data_r not in src_r.parents) or \
           (dst_r != dest_r and dest_r not in dst_r.parents):
            mismatched.append(rel + " (path escapes restore root)")
            continue
        if not src.exists():
            mismatched.append(rel + " (missing)")
            continue
        if _sha256_file(src) != entry["sha256"]:
            mismatched.append(rel + " (hash mismatch)")
            continue
        planned.append((src, dst))

    # Only commit the moves when everything verified (unless insecure override).
    restored = 0
    if not mismatched or insecure:
        for src, dst in planned:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
            restored += 1
    # tidy the staging tree + manifest copy
    (dest / _MANIFEST_NAME).unlink(missing_ok=True)
    _rmtree(data_root)

    if mismatched and not insecure:
        raise BackupError(f"restore integrity check failed for "
                          f"{len(mismatched)} file(s): {mismatched[:5]} "
                          f"— nothing was restored.")
    return {"restored": restored, "into": str(dest), "mismatched": mismatched,
            "signature_ok": v["signature_ok"], "signed": v["signed"]}


def _rmtree(p: Path) -> None:
    if not p.exists():
        return
    for child in sorted(p.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
        else:
            child.rmdir()
    p.rmdir()
