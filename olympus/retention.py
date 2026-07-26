"""Retention policy surface and principal lifecycle (Phase 5, Steps 11–12).

Two named Phase-4 blockers are addressed here, and NEITHER is "fixed" by
inventing an answer that is not ours to invent.

STEP 11 — CONVERSATION RETENTION
`PRIVACY_RETENTION_REVIEW.md` §5 records that conversation snapshots and their
journals have no retention bound: `RETAIN_DAYS` governs traces, usage, frozen
payloads and the absorption evidence ledgers, but never the conversations
themselves. The right retention period for conversation content is a PRODUCT
AND LEGAL decision — it depends on jurisdiction, contract, and what the
operator told their users. Olympus cannot know it.

So this module ships the MECHANISM and leaves the POLICY unset. With no policy
configured, `policy_status()` reports `unset` and
`deployment_blocked_reason()` returns a refusal that names what is missing.
Phase 5 does not clear that blocker; it makes it clearable in one configuration
step, and makes the consequence of leaving it unset explicit rather than
silent.

STEP 12 — LEGACY `api-v1` DATA
Phase-4 B-F2 gave every `/v1` API key its own memory principal. Before that fix
every key shared the literal namespace `api-v1`, so whatever sits under
`users/api-v1/` is the COMMINGLED memory of every key holder the deployment
ever had. It cannot be attributed. Automatically handing it to any one
principal would give one key holder another's data — the exact defect B-F2
closed. Every operation here is therefore explicit, dry-run-first, and audited,
and `adopt()` refuses without a written acknowledgement of what is being
claimed.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from . import config, memory

LEGACY_PRINCIPAL = "api-v1"

#: Everything derived from a principal's conversations. Deleting a principal
#: without these leaves the content recoverable, which is not deletion.
_DERIVED_ROOTS = (
    "users/{uid}",                       # lessons, corrections, feedback, prefs
    "conversations/{uid}.json",          # the snapshot
    "sessions/{uid}.journal.jsonl",      # the sealed journal
)

#: Stores keyed by principal that live outside `users/`.
_DERIVED_GLOBS = (
    "sessions/quarantine/{uid}.*.journal",
    "ctxheat/users/{uid}",
    "documents/{uid}",
    "docrag/{uid}",
)


# ---------------------------------------------------------------------------
# policy surface
# ---------------------------------------------------------------------------

def conversation_retain_days() -> int | None:
    """The operator's conversation-retention policy, in days.

    `None` means UNSET — deliberately distinct from 0. Returning a default
    would be inventing a legal position; returning 0 would silently start
    deleting user content. Neither is ours to choose.

    `OLYMPUS_CONVERSATION_RETAIN_DAYS=forever` is the explicit way to say
    "retain indefinitely" — it is a decision on record, not an omission, and it
    clears the deployment block."""
    raw = os.environ.get("OLYMPUS_CONVERSATION_RETAIN_DAYS", "").strip().lower()
    if not raw:
        return None
    if raw in ("forever", "never", "indefinite", "unlimited"):
        return 0                          # 0 = keep forever, explicitly chosen
    try:
        days = int(raw)
    except ValueError:
        return None
    return days if days > 0 else 0


def policy_status() -> str:
    """`unset`, `forever`, or `N days`."""
    days = conversation_retain_days()
    if days is None:
        return "unset"
    return "forever" if days == 0 else f"{days} days"


def legal_hold() -> set[str]:
    """Principals exempt from every deletion path (OLYMPUS_LEGAL_HOLD, comma-
    separated). A hold outranks retention: litigation and regulatory holds are
    not negotiable against a cleanup job."""
    raw = os.environ.get("OLYMPUS_LEGAL_HOLD", "")
    return {memory.safe_id(p) for p in raw.split(",") if p.strip()}


def deployment_blocked_reason() -> str:
    """Why this deployment may not serve regulated or multi-user personal data,
    or "" if it may.

    Returned rather than raised: this is a posture report an operator reads,
    not a runtime failure. `olympus retention status` surfaces it, and the
    Phase-5 completion report quotes it."""
    if conversation_retain_days() is None:
        return (
            "OLYMPUS_CONVERSATION_RETAIN_DAYS is unset. Conversation snapshots "
            "and their sealed journals grow without bound, so this deployment "
            "has no retention position for user content. Set it to a number of "
            "days, or to 'forever' if indefinite retention is the deliberate, "
            "disclosed policy. Until then, do not process regulated or "
            "multi-user personal data on this instance.")
    return ""


# ---------------------------------------------------------------------------
# what a principal owns
# ---------------------------------------------------------------------------

def _paths_for(uid: str) -> list[Path]:
    base = config.MEMORY_DIR
    out: list[Path] = []
    for tpl in _DERIVED_ROOTS:
        p = base / tpl.format(uid=uid)
        if p.exists():
            out.append(p)
    for tpl in _DERIVED_GLOBS:
        pattern = tpl.format(uid=uid)
        parent = base / str(Path(pattern).parent)
        if parent.exists():
            out.extend(sorted(parent.glob(Path(pattern).name)))
    return out


def _size_of(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    return sum(p.stat().st_size for p in path.rglob("*")
               if p.is_file())


def inspect_principal(principal: str) -> dict:
    """Everything on disk attributable to `principal`. PURE — reads only."""
    uid = memory.safe_id(principal)
    paths = _paths_for(uid)
    return {
        "principal": uid,
        "exists": bool(paths),
        "paths": [str(p.relative_to(config.MEMORY_DIR)) for p in paths],
        "bytes": sum(_size_of(p) for p in paths),
        "files": sum(1 for p in paths
                     for _ in ([p] if p.is_file() else p.rglob("*"))),
        "legal_hold": uid in legal_hold(),
    }


def list_principals() -> list[str]:
    d = config.MEMORY_DIR / "users"
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# audit log — append-only, same discipline as every other evidence store
# ---------------------------------------------------------------------------

def _audit(event: str, **fields) -> None:
    d = config.MEMORY_DIR / "retention"
    try:
        d.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "event": event, **fields}
        with open(d / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
    except OSError as err:
        from . import errors
        errors.capture("retention.audit", err, context=event)


def audit_log(limit: int = 0) -> list[dict]:
    p = config.MEMORY_DIR / "retention" / "audit.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue                      # skip, never repair
    return out[-limit:] if limit else out


# ---------------------------------------------------------------------------
# deletion
# ---------------------------------------------------------------------------

def delete_principal(principal: str, *, dry_run: bool = True,
                     reason: str = "") -> dict:
    """Delete everything derived from `principal`.

    `dry_run=True` by DEFAULT. A deletion API whose default is destructive is a
    mis-click away from an incident, and the dry run is also the verification
    artifact — Step 11 requires a dry-run report that is accurate, so the same
    code path must produce both.

    Tombstones the journal before removing it, so a replica or a backup taken
    mid-operation records the intent rather than just losing bytes.

    A legal hold refuses outright: a hold outranks a deletion request."""
    uid = memory.safe_id(principal)
    plan = inspect_principal(uid)
    plan.update(dry_run=dry_run, reason=reason, deleted=[], errors=[],
                refused="")

    if uid in legal_hold():
        plan["refused"] = (
            f"principal '{uid}' is under legal hold (OLYMPUS_LEGAL_HOLD). A "
            f"hold outranks retention and deletion; clear the hold first.")
        _audit("delete_refused_legal_hold", principal=uid, reason=reason)
        return plan

    if dry_run:
        _audit("delete_dry_run", principal=uid, files=plan["files"],
               bytes=plan["bytes"], reason=reason)
        return plan

    # Tombstone first: if the process dies between tombstone and unlink, the
    # journal says the deletion was intended rather than looking corrupted.
    try:
        from . import sessionlog
        records, _status = sessionlog.read_verified(uid)
        if records:
            sessionlog.append_tombstone(uid, 1, records[-1]["seq"])
    except Exception as err:                                    # noqa: BLE001
        plan["errors"].append(f"tombstone: {type(err).__name__}: {err}")

    for path in _paths_for(uid):
        rel = str(path.relative_to(config.MEMORY_DIR))
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            plan["deleted"].append(rel)
        except OSError as err:
            plan["errors"].append(f"{rel}: {err}")

    plan["verified"] = verify_deleted(uid)
    _audit("delete", principal=uid, deleted=len(plan["deleted"]),
           errors=len(plan["errors"]), verified=plan["verified"],
           reason=reason)
    return plan


def verify_deleted(principal: str) -> bool:
    """Whether nothing attributable to `principal` remains.

    Step 13's rule applied to deletion: an unverified deletion is not a
    verified deletion. Called automatically after a real delete, and available
    standalone for an auditor who wants to check independently."""
    return not _paths_for(memory.safe_id(principal))


def sweep_conversations(retain_days: int | None = None, *,
                        dry_run: bool = True) -> dict:
    """Apply the conversation-retention policy.

    No policy → NO-OP with an explicit reason. Never guesses, never defaults to
    deleting. Principals under legal hold are skipped and counted."""
    days = retain_days if retain_days is not None else conversation_retain_days()
    out = {"policy": policy_status(), "dry_run": dry_run,
           "candidates": [], "held": [], "removed": 0, "skipped_reason": ""}
    if days is None:
        out["skipped_reason"] = deployment_blocked_reason()
        return out
    if days == 0:
        out["skipped_reason"] = "policy is 'forever' — nothing is swept"
        return out

    cutoff = time.time() - days * 86400
    held = legal_hold()
    conv = config.MEMORY_DIR / "conversations"
    if not conv.exists():
        return out
    for path in sorted(conv.glob("*.json")):
        uid = path.stem
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        if uid in held:
            out["held"].append(uid)
            continue
        out["candidates"].append(
            {"principal": uid,
             "age_days": round((time.time() - mtime) / 86400, 1)})
        if not dry_run:
            res = delete_principal(uid, dry_run=False,
                                   reason=f"retention {days}d")
            out["removed"] += 1 if res["verified"] else 0
    _audit("sweep_conversations", policy=out["policy"], dry_run=dry_run,
           candidates=len(out["candidates"]), held=len(out["held"]),
           removed=out["removed"])
    return out


def report(dry_run: bool = True) -> dict:
    """The Step-11 dry-run retention report: what every policy would do, with
    nothing deleted unless the caller says so."""
    return {
        "conversation_policy": policy_status(),
        "deployment_blocked": deployment_blocked_reason(),
        "evidence_retain_days": config.RETAIN_DAYS,
        "legal_hold": sorted(legal_hold()),
        "principals": [inspect_principal(p) for p in list_principals()],
        "conversation_sweep": sweep_conversations(dry_run=dry_run),
        "legacy": inspect_legacy(),
    }


# ---------------------------------------------------------------------------
# Step 12 — the legacy `api-v1` namespace
# ---------------------------------------------------------------------------

def inspect_legacy() -> dict:
    """What sits under the pre-B-F2 shared namespace."""
    info = inspect_principal(LEGACY_PRINCIPAL)
    info["why_it_exists"] = (
        "Before the Phase-4 B-F2 fix, every /v1 API key ran as the single "
        "principal 'api-v1'. Whatever is here is the COMMINGLED memory of "
        "every key holder this deployment ever had, and it cannot be "
        "attributed to any one of them.")
    info["safe_operations"] = ["inspect", "export", "quarantine", "delete"]
    info["unsafe_operation"] = (
        "adopt — assigning this data to a principal gives one key holder "
        "every other key holder's material. Requires an explicit written "
        "acknowledgement and is never automatic.")
    return info


def export_legacy(dest: str | Path) -> dict:
    """Copy the legacy namespace out for offline review. Non-destructive."""
    src = config.MEMORY_DIR / "users" / LEGACY_PRINCIPAL
    dest = Path(dest)
    if not src.exists():
        return {"exported": False, "reason": "no legacy namespace present"}
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"api-v1-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copytree(src, target)
    _audit("legacy_export", dest=str(target))
    return {"exported": True, "path": str(target),
            "bytes": _size_of(target)}


def quarantine_legacy(*, dry_run: bool = True) -> dict:
    """Move the legacy namespace aside so no principal can read it, without
    destroying it.

    The recommended default action: it removes the cross-principal exposure
    immediately and keeps the data available for a considered decision. It is
    reversible by moving the directory back."""
    src = config.MEMORY_DIR / "users" / LEGACY_PRINCIPAL
    dst = config.MEMORY_DIR / "legacy_quarantine" / LEGACY_PRINCIPAL
    out = {"dry_run": dry_run, "from": str(src), "to": str(dst),
           "moved": False}
    if not src.exists():
        out["reason"] = "no legacy namespace present"
        return out
    out["bytes"] = _size_of(src)
    if dry_run:
        _audit("legacy_quarantine_dry_run", bytes=out["bytes"])
        return out
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst = dst.with_name(f"{LEGACY_PRINCIPAL}-{int(time.time())}")
        out["to"] = str(dst)
    shutil.move(str(src), str(dst))
    out["moved"] = True
    _audit("legacy_quarantine", to=str(dst), bytes=out["bytes"])
    return out


def restore_legacy_quarantine() -> dict:
    """Undo `quarantine_legacy` — the documented rollback."""
    dst = config.MEMORY_DIR / "users" / LEGACY_PRINCIPAL
    for src in sorted((config.MEMORY_DIR / "legacy_quarantine").glob(
            f"{LEGACY_PRINCIPAL}*"), reverse=True):
        if dst.exists():
            return {"restored": False,
                    "reason": f"{dst} already exists — move it first"}
        shutil.move(str(src), str(dst))
        _audit("legacy_quarantine_restore", to=str(dst))
        return {"restored": True, "path": str(dst)}
    return {"restored": False, "reason": "nothing in quarantine"}


class LegacyAdoptionRefused(RuntimeError):
    """`adopt_legacy` was called without an explicit acknowledgement."""


ADOPTION_ACK = ("I accept that this data is commingled across API key holders "
                "and that assigning it to one principal may expose another "
                "key holder's material")


def adopt_legacy(principal: str, *, acknowledgement: str = "",
                 dry_run: bool = True) -> dict:
    """Assign the legacy namespace to `principal`.

    NEVER automatic, and never a default. The data is commingled, so adopting
    it hands one key holder everybody else's material — precisely the defect
    Phase-4 B-F2 closed. The caller must pass `ADOPTION_ACK` verbatim, which
    exists so the acknowledgement cannot be given by accident or by a script
    that was not written for this.

    P5-A13 asserts that no code path reaches this without the acknowledgement."""
    uid = memory.safe_id(principal)
    if acknowledgement.strip() != ADOPTION_ACK:
        _audit("legacy_adopt_refused", principal=uid)
        raise LegacyAdoptionRefused(
            "adopting the legacy api-v1 namespace requires the explicit "
            "acknowledgement, passed verbatim as `acknowledgement=`:\n\n  "
            f'"{ADOPTION_ACK}"\n\n'
            "Prefer `quarantine_legacy()` (reversible, removes the exposure) "
            "or `delete_principal('api-v1', dry_run=False)` unless you have "
            "established that exactly one key holder ever used this instance.")
    src = config.MEMORY_DIR / "users" / LEGACY_PRINCIPAL
    dst = config.MEMORY_DIR / "users" / uid
    out = {"dry_run": dry_run, "principal": uid, "from": str(src),
           "to": str(dst), "adopted": False}
    if not src.exists():
        out["reason"] = "no legacy namespace present"
        return out
    if dry_run:
        _audit("legacy_adopt_dry_run", principal=uid)
        return out
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if target.exists():
            target = dst / f"legacy-{item.name}"
        shutil.move(str(item), str(target))
    out["adopted"] = True
    _audit("legacy_adopt", principal=uid)
    return out
