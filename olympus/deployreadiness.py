"""Deployment durability diagnostics with remote recovery qualification closed.

Existing local lifecycle receipts remain diagnostics subject to the unfinished
M14 target/identity/permission review. Backup workflow receipts live in the
separate native bounded evidence namespace. Their visibility and successful
local uploader commands cannot establish remote receipt, retention or recovery.

Both mandatory remote backup and remote restore checks remain false until a
target-bound M14 remote recovery proof contract is implemented and qualified.
Consequently named deployment readiness cannot currently become ready, even
after a successful local backup and offline restore drill. Legacy caller-dict
backup/restore writers refuse without modifying their retained historical files.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config

SCHEMA = "olympus-deployment-readiness/1"
EVIDENCE_DIR = "deployment"
BACKUP_RECEIPT = "backup-receipt.json"
RESTORE_RECEIPT = "restore-receipt.json"

MIN_FREE_BYTES_ENV = "OLYMPUS_MIN_FREE_BYTES"
BACKUP_MAX_AGE_ENV = "OLYMPUS_BACKUP_MAX_AGE"
EVIDENCE_MAX_AGE_ENV = "OLYMPUS_DURABILITY_EVIDENCE_MAX_AGE"
CHALLENGE_MAX_AGE_ENV = "OLYMPUS_DURABILITY_CHALLENGE_MAX_AGE"

DEFAULT_MIN_FREE_BYTES = 1 << 30
DEFAULT_BACKUP_MAX_AGE = 2 * 86400
DEFAULT_EVIDENCE_MAX_AGE = 30 * 86400
DEFAULT_CHALLENGE_MAX_AGE = 86400

_KINDS = ("container", "host")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class DeploymentEvidenceError(RuntimeError):
    """A durability challenge or evidence write could not be completed."""


def _now() -> float:
    return time.time()


def _utc(timestamp: float | None = None) -> str:
    value = _now() if timestamp is None else timestamp
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _root() -> Path:
    return Path(config.MEMORY_DIR)


def evidence_dir(*, create: bool = False) -> Path:
    path = _root() / EVIDENCE_DIR
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(path, 0o700)
    return path


def _challenge_path(kind: str) -> Path:
    return evidence_dir() / f"{kind}-challenge.json"


def _receipt_path(kind: str) -> Path:
    return evidence_dir() / f"{kind}-receipt.json"


def _fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Publish a private JSON receipt atomically and durably."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(parent, 0o700)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=parent)
    tmp = Path(tmp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
        _fsync_dir(parent)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _build_commit() -> str:
    try:
        return str(config.build_info().get("commit", "unknown"))
    except Exception:
        return "unknown"


def host_boot_id() -> str:
    """Return the Linux boot identity used to prove a real host reboot."""
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8").strip()
    except OSError:
        return ""
    return value if value else ""


def container_id() -> str:
    """Return the container identity used to prove container replacement."""
    value = os.environ.get("HOSTNAME", "").strip()
    if value:
        return value
    try:
        return Path("/etc/hostname").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _runtime_identity(kind: str) -> str:
    if kind == "container":
        return container_id()
    if kind == "host":
        return host_boot_id()
    raise DeploymentEvidenceError(f"unknown durability challenge kind: {kind}")


def challenge(kind: str) -> dict[str, Any]:
    """Record a pre-replacement identity on the persistent state volume."""
    if kind not in _KINDS:
        raise DeploymentEvidenceError(f"kind must be one of: {', '.join(_KINDS)}")
    identity = _runtime_identity(kind)
    if not identity:
        raise DeploymentEvidenceError(
            f"cannot determine current {kind} identity on this platform")
    stamp = _now()
    payload = {
        "schema": SCHEMA,
        "record_type": "durability_challenge",
        "kind": kind,
        "nonce": secrets.token_hex(32),
        "created_at": stamp,
        "created_utc": _utc(stamp),
        "identity": identity,
        "memory_dir": str(_root().resolve()),
        "commit": _build_commit(),
    }
    _atomic_json(evidence_dir(create=True) / f"{kind}-challenge.json", payload)
    return payload


def _positive_env(name: str, default: int) -> tuple[int | None, str | None]:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        return None, f"{name}={raw!r} is not an integer"
    if value <= 0:
        return None, f"{name} must be greater than zero"
    return value, None


def verify(kind: str) -> dict[str, Any]:
    """Verify a challenge after replacement/reboot and publish a receipt."""
    if kind not in _KINDS:
        raise DeploymentEvidenceError(f"kind must be one of: {', '.join(_KINDS)}")
    challenge_data = _read_json(_challenge_path(kind))
    if (not challenge_data
            or challenge_data.get("schema") != SCHEMA
            or challenge_data.get("record_type") != "durability_challenge"
            or challenge_data.get("kind") != kind
            or not challenge_data.get("nonce")
            or challenge_data.get("memory_dir") != str(_root().resolve())
            or challenge_data.get("commit") != _build_commit()):
        raise DeploymentEvidenceError(f"no valid {kind} challenge is present")
    max_age, error = _positive_env(
        CHALLENGE_MAX_AGE_ENV, DEFAULT_CHALLENGE_MAX_AGE)
    if error:
        raise DeploymentEvidenceError(error)
    try:
        age = _now() - float(challenge_data["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DeploymentEvidenceError(
            f"{kind} challenge has an invalid timestamp") from exc
    if age < -300 or age > int(max_age):
        raise DeploymentEvidenceError(
            f"{kind} challenge is stale or future-dated ({int(age)} seconds)")
    old_identity = str(challenge_data.get("identity", ""))
    if not old_identity:
        raise DeploymentEvidenceError(f"{kind} challenge has no prior identity")
    new_identity = _runtime_identity(kind)
    if not new_identity:
        raise DeploymentEvidenceError(
            f"cannot determine current {kind} identity on this platform")
    if new_identity == old_identity:
        action = "replace the container" if kind == "container" else "reboot the host"
        raise DeploymentEvidenceError(
            f"{kind} identity did not change; {action} before verification")
    stamp = _now()
    payload = {
        "schema": SCHEMA,
        "record_type": "durability_receipt",
        "kind": kind,
        "challenge_nonce": challenge_data.get("nonce", ""),
        "challenged_at": challenge_data.get("created_at"),
        "verified_at": stamp,
        "verified_utc": _utc(stamp),
        "from_identity": old_identity,
        "to_identity": new_identity,
        "memory_dir": str(_root().resolve()),
        "commit": _build_commit(),
    }
    _atomic_json(evidence_dir(create=True) / f"{kind}-receipt.json", payload)
    return payload


def record_backup(result: dict[str, Any]) -> dict[str, Any]:
    """Refuse legacy unbound caller assertions without reading or writing them."""
    raise DeploymentEvidenceError(
        'legacy caller-supplied backup receipts cannot establish recovery evidence; use the captured backup workflow')


def record_restore(result: dict[str, Any], *, archive_name: str,
                   sha256: str) -> dict[str, Any]:
    """Refuse legacy restore assertions; keep every existing receipt intact."""
    raise DeploymentEvidenceError(
        'legacy caller-supplied restore receipts cannot establish recovery evidence; use an explicit offline restore drill')


def _local_backup_observation() -> dict[str, Any]:
    """Show bounded native receipt presence without qualifying deployment.

    Native inspection checks its own selected root and receipt namespace. This
    observation does not bind the separate mount/lifecycle probes to that same
    held root, reconstruct write acknowledgments, or attest remote recovery.
    The M14 target-specific qualification path remains required.
    """
    from . import backup_evidence
    scope = {'observational_only': True, 'write_acknowledgment_observed': None,
        'enclosing_workflow_complete': False, 'current_archive_availability_verified': False,
        'remote_retention_verified': False, 'deployment_target_binding_verified': False}
    try:
        observed = backup_evidence.inspect()
        counts = {'backup': 0, 'restore': 0}
        for row in observed['receipts']:
            counts[row['receipt']['kind']] += 1
        return {'state': 'observed', 'visible_receipts': counts, **scope}
    except Exception:
        return {'state': 'unavailable', **scope}


def _check(name: str, ok: bool, detail: str,
           **evidence: Any) -> dict[str, Any]:
    return {"name": name, "status": "pass" if ok else "fail",
            "detail": detail, **evidence}


def _age_check(name: str, receipt: dict[str, Any] | None, field: str,
               maximum: int, predicate: Callable[[dict[str, Any]], bool],
               success: str, failure: str) -> dict[str, Any]:
    if not receipt or receipt.get("schema") != SCHEMA:
        return _check(name, False, f"{failure}: receipt missing or malformed")
    try:
        age = _now() - float(receipt[field])
    except (KeyError, TypeError, ValueError):
        return _check(name, False, f"{failure}: receipt timestamp is invalid")
    try:
        valid = bool(predicate(receipt))
    except (KeyError, TypeError, ValueError):
        valid = False
    ok = -300 <= age <= maximum and valid
    return _check(name, ok, success if ok else failure,
                  age_seconds=max(0, int(age)))


def _durable_write_probe(root: Path) -> tuple[bool, str]:
    """Write, fsync, publish, fsync the directory, and remove a unique probe."""
    probe = root / f".olympus-durability-probe-{secrets.token_hex(8)}"
    tmp = root / f".{probe.name}.tmp"
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tmp.open("wb") as handle:
            handle.write(b"olympus durability probe\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, probe)
        _fsync_dir(root)
        probe.unlink()
        _fsync_dir(root)
        return True, "durable write/fsync/rename probe succeeded"
    except OSError as exc:
        return False, f"durable write probe failed: {exc}"
    finally:
        tmp.unlink(missing_ok=True)
        probe.unlink(missing_ok=True)


def report() -> dict[str, Any]:
    """Return the complete fail-closed P2A deployment readiness report."""
    checks: list[dict[str, Any]] = []
    mode = config.deployment_env()
    named = mode in ("staging", "production")
    checks.append(_check(
        "deployment_mode", named,
        f"named deployment mode: {mode}" if named else
        "OLYMPUS_ENV must name staging or production"))

    root = _root()
    raw_root = os.environ.get("OLYMPUS_MEMORY_DIR", "").strip()
    try:
        exact_path = bool(raw_root) and Path(raw_root).resolve() == root.resolve()
    except (OSError, RuntimeError):
        exact_path = False
    checks.append(_check(
        "explicit_memory_dir", exact_path,
        f"OLYMPUS_MEMORY_DIR resolves to {root}" if exact_path else
        "OLYMPUS_MEMORY_DIR must explicitly match the active memory directory"))

    mounted = False
    try:
        mounted = root.exists() and os.path.ismount(str(root))
    except OSError:
        pass
    checks.append(_check(
        "memory_mount", mounted,
        f"{root} is a mount point" if mounted else
        f"{root} is not proven to be a mount point"))

    private = False
    permission_detail = "memory directory is missing or cannot be inspected"
    try:
        stat = root.stat()
        mode_bits = stat.st_mode & 0o777
        owner_ok = not hasattr(os, "geteuid") or stat.st_uid == os.geteuid()
        private = owner_ok and (mode_bits & 0o077) == 0
        permission_detail = (
            f"mode={mode_bits:04o}, uid={stat.st_uid}"
            if private else
            f"memory directory must be owned by this process and mode 0700 or stricter; "
            f"found mode={mode_bits:04o}, uid={stat.st_uid}")
    except OSError:
        pass
    checks.append(_check("private_memory_permissions", private,
                         permission_detail))

    write_ok, write_detail = _durable_write_probe(root)
    checks.append(_check("durable_write", write_ok, write_detail))

    min_free, min_error = _positive_env(
        MIN_FREE_BYTES_ENV, DEFAULT_MIN_FREE_BYTES)
    free = -1
    if min_error:
        disk_ok, disk_detail = False, min_error
    else:
        try:
            free = shutil.disk_usage(root).free
            disk_ok = free >= int(min_free)
            disk_detail = (
                f"{free} bytes free (minimum {min_free})" if disk_ok else
                f"only {free} bytes free; minimum is {min_free}")
        except OSError as exc:
            disk_ok, disk_detail = False, f"disk usage probe failed: {exc}"
    checks.append(_check("disk_headroom", disk_ok, disk_detail,
                         free_bytes=free))

    commit = _build_commit()
    declared_commit = os.environ.get("OLYMPUS_BUILD_COMMIT", "").strip()
    commit_ok = (bool(_COMMIT_RE.fullmatch(declared_commit))
                 and commit == declared_commit)
    checks.append(_check(
        "build_commit", commit_ok,
        f"exact build commit: {commit}" if commit_ok else
        "OLYMPUS_BUILD_COMMIT must be an exact 40-character lowercase SHA "
        f"matching the running build; declared={declared_commit!r}, "
        f"running={commit!r}"))

    backup_cmd = bool(config.backup_command())
    checks.append(_check(
        "backup_destination", backup_cmd,
        "uploader command is configured; destination and remote retention remain unverified" if backup_cmd else
        "OLYMPUS_BACKUP_CMD is not configured"))
    from . import vault
    secret = False
    if vault.configured():
        try:
            vault.check_ready()
            secret = True
            secret_detail = "backup encryption secret is configured and usable"
        except Exception:
            secret_detail = "backup encryption secret is unusable; inspect vault custody and configuration"
    else:
        secret_detail = (
            "OLYMPUS_SECRET_KEY / OLYMPUS_SECRET_KEY_FILE is not configured")
    checks.append(_check(
        "backup_encryption", secret,
        secret_detail))
    cadence, cadence_error = _positive_env(
        "OLYMPUS_BACKUP_EVERY", int(getattr(config, "BACKUP_EVERY", 86400)))
    checks.append(_check(
        "backup_cadence", cadence_error is None,
        f"backup cadence is {cadence} seconds" if cadence_error is None else
        cadence_error))

    database = bool(os.environ.get("OLYMPUS_DATABASE_URL", "").strip())
    checks.append(_check(
        "database_coverage", not database,
        "file-backed private state is covered by MEMORY_DIR archives" if not database else
        "OLYMPUS_DATABASE_URL is set, but P2A has no database-backup receipt; "
        "the MEMORY_DIR archive cannot prove database recovery"))

    evidence_age, evidence_error = _positive_env(
        EVIDENCE_MAX_AGE_ENV, DEFAULT_EVIDENCE_MAX_AGE)
    backup_age, backup_age_error = _positive_env(
        BACKUP_MAX_AGE_ENV, DEFAULT_BACKUP_MAX_AGE)
    if evidence_error:
        checks.append(_check("evidence_max_age", False, evidence_error))
        evidence_age = 0
    if backup_age_error:
        checks.append(_check("backup_max_age", False, backup_age_error))
        backup_age = 0

    for kind in _KINDS:
        receipt = _read_json(_receipt_path(kind))
        checks.append(_age_check(
            f"{kind}_survival", receipt, "verified_at", int(evidence_age),
            lambda item, k=kind: (
                item.get("kind") == k
                and item.get("from_identity")
                and item.get("to_identity")
                and item.get("from_identity") != item.get("to_identity")
                and item.get("memory_dir") == str(root.resolve())
                and item.get("commit") == commit),
            f"{kind} replacement survival is verified",
            f"no recent {kind} survival receipt matches this path and build"))

    # Caller-supplied legacy receipts and local command completion cannot
    # attest a remote copy or a restore obtained from that remote destination.
    # Keep both mandatory gates closed until M14 supplies a target-bound remote
    # recovery proof contract. Local receipt visibility is useful diagnostics.
    observation = _local_backup_observation()
    checks.append(_check(
        "recoverable_off_machine_backup", False,
        "off-machine receipt, retention and recovery proof is unavailable; "
        "uploader command completion and local receipts do not establish it",
        local_receipts=observation, remote_recovery_verified=False))
    checks.append(_check(
        "restore_drill", False,
        "no target-bound restore proof from the remote backup destination is available; "
        "local offline drill receipts remain observational",
        local_receipts=observation, remote_recovery_verified=False))

    ready = all(item["status"] == "pass" for item in checks)
    return {
        "schema": SCHEMA,
        "ready": ready,
        "status": "ready" if ready else "not_ready",
        "env": mode or "dev",
        "memory_dir": str(root),
        "commit": commit,
        "checked_at": _utc(),
        "checks": checks,
        "problems": [item["detail"] for item in checks
                     if item["status"] != "pass"],
    }


def problems() -> list[str]:
    return list(report()["problems"])


def render(value: dict[str, Any] | None = None) -> str:
    value = report() if value is None else value
    lines = [
        f"Deployment readiness: {'READY' if value['ready'] else 'NOT READY'}",
        f"  env={value['env']}  commit={value['commit']}",
    ]
    for item in value["checks"]:
        mark = "✓" if item["status"] == "pass" else "✗"
        lines.append(f"  {mark} {item['name']}: {item['detail']}")
    return "\n".join(lines)
