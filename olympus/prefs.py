"""Per-user preferences (language, etc.), namespaced like memory.

Small JSON store so a user's choices — most importantly their language —
persist across sessions and interfaces. Kept separate from lessons/memory
because preferences are settings, not learned knowledge.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import config, memory


#: Bumped when the per-principal path changed from `users/<safe_id>/prefs.json`
#: to `prefs/owners/<owner-key>.json`. See `legacy_owners` for the migration.
PREFS_KEY_VERSION = 2

# Keep repair archives below the traditional Windows MAX_PATH boundary even
# when the exact-owner storage key and pytest's temporary root are both long.
# A 128-bit SHA-256 prefix remains content-addressed; the full digest is
# returned to the operator, and an existing prefix collision is byte-checked
# and refused rather than overwritten.
_QUARANTINE_DIGEST_HEX = 32


class PreferencesStateError(RuntimeError):
    """The current preference-policy blob exists but cannot be trusted.

    Missing is valid first-run state.  Existing-but-unreadable, malformed, or
    non-object JSON is different: treating it as ``{}`` can discard a guest
    profile, an L0 autonomy assignment, granted-scope limits, or the global
    spend cap.  Callers may clamp individual security decisions, but mutations
    must stop until an operator explicitly repairs the evidence.
    """

    def __init__(self, user: str, path: Path, reason: str) -> None:
        self.user = memory.canonical_owner(user)
        self.path = Path(path)
        self.reason = str(reason)
        self.repair_command = _repair_command(self.user)
        super().__init__(
            f"Preference policy evidence for '{self.user}' is unavailable "
            f"({self.reason}). Security decisions are clamped and writes are "
            f"refused. Inspect or preserve-and-reset it with "
            f"`{self.repair_command}`.")


def _repair_command(user: str) -> str:
    """Copy-safe command for ordinary ids; never interpolate shell syntax."""
    exact = memory.canonical_owner(user)
    if exact and all(ch.isalnum() or ch in "-_.:@" for ch in exact):
        owner_arg = exact
    else:
        owner_arg = "<exact-owner>"
    return f"olympus prefs-evidence {owner_arg} --repair"


def _path(user: str) -> Path:
    """Where a principal's preferences live.

    THE CENTRAL FIX. This used to be `users/<safe_id(user)>/prefs.json`, and
    preferences are not cosmetic here: this one file carries the autonomy
    level, granted action scopes, per-action daily limits, the capability
    profile (including its `max_autonomy` cap), operator settings and their
    authorized-site list, earned-autonomy opt-in and pending secure-capture
    state. `safe_id` collapses punctuation and truncates at 64 characters, so
    `tg-a.b`, `tg-a@b`, `tg-a b` and `tg-a-b` shared ONE settings file: raising
    one principal to autonomy L4 raised all of them, and granting one an action
    scope granted it to all of them.

    The reserved installation namespaces keep an explicit, stable location —
    `shared` stays exactly where it was (`MEMORY_DIR/prefs.json`, which carries
    the installation-wide daily budget cap), and any other reserved name gets
    its own file under `prefs/system/`. A tenant is keyed by
    `memory.storage_key`: the exact principal plus its complete SHA-256 digest.
    This is an explicit rule, NOT a generic tenant fallback — no tenant can
    reach a system namespace by having a name that normalizes to it.

    The tenant tree is deliberately outside `users/` and `owners/`, the two
    trees `memory._memory_roots` sweeps, so a memory export or a whole-scope
    memory delete does not carry or wipe security settings.
    """
    exact = memory.canonical_owner(user)
    if exact == "shared":
        base = config.MEMORY_DIR                       # unchanged, by policy
    elif memory.is_system_owner(exact):
        base = config.MEMORY_DIR / "prefs" / "system" / exact
    else:
        base = config.MEMORY_DIR / "prefs" / "owners" / memory.owner_key(exact)
    base.mkdir(parents=True, exist_ok=True)
    return base / "prefs.json"


# --- the central quarantine API ------------------------------------------
#
# THE ONE PLACE unresolved-legacy state is detected. Every consumer reads its
# settings through `get`, so clamping here reaches autonomy, granted scopes,
# action limits, the capability profile, operator settings, earned autonomy and
# secure capture without any of those modules re-implementing legacy detection.
#
# WHY A POSTURE AND NOT DEFAULTS. Refusing to attribute a legacy file and then
# falling back to ordinary defaults WIDENS privilege: a legacy
# `capability_profile="guest"` became "full", a legacy `autonomy=0` became L1,
# and a strict `action_limits` entry vanished into an unlimited class default.
# The safe reading of "we do not know who this file belongs to" is not "assume
# nothing was ever restricted" — it is "assume the most restrictive thing that
# was ever true of this collision group, until an operator resolves it".

#: Preference keys whose value IS a security decision. While a principal's
#: collision group has an unresolved legacy file, each reads as its restrictive
#: posture regardless of what any exact file says.
QUARANTINE_POSTURE: dict[str, object] = {
    "autonomy": 0,                     # actions.L0 — nothing runs unattended
    "scopes": [],                      # no integration scope is granted
    "capability_profile": "guest",     # locked deny posture, max_autonomy 0
    "action_limits": {},               # see actions.daily_limit: NOT unlimited
    "operator": {"sites": {}, "advanced": False},   # no authorized site
    "earned_autonomy": False,          # trust ladder disabled
    "pending_secure_login": None,      # no password prompt may be pending
}

QUARANTINE_REASON = (
    "settings for this identity are quarantined: either an unmigrated "
    "preference file or a pre-v2 lossy gateway principal cannot be attributed "
    "to one external account. An operator must migrate or discard the legacy "
    "data (mapping it to a verified v2 principal when migrating) before "
    "restricted settings can be "
    "trusted.")

# Unlike cosmetic preferences, the installation-wide budget cannot fall back
# to configuration when its saved blob exists but is unavailable: that can
# widen a tighter saved cap, and an explicitly configured 0 means unlimited.
# Its consumer converts this typed state error into BudgetPolicyUnavailable.
_STRICT_EVIDENCE_KEYS = frozenset({"daily_budget"})


def is_quarantined(user: str) -> bool:
    """True when legacy ownership or current evidence is unresolved.

    Membership is the collision GROUP, not the individual: `tg-a.b` and
    `tg-a-b` both normalize to `tg-a-b`, and the legacy file names only that
    normalized value. Either of them could be its author, so both are held.

    Reserved installation namespaces are exempt only from legacy ambiguity.
    Their exact current files can still be corrupt; in particular, a damaged
    ``shared`` blob must not turn a saved daily spend cap into "unlimited".
    """
    exact = memory.canonical_owner(user)
    if (not memory.is_system_owner(exact)
            and (memory.is_ambiguous_gateway_owner(exact)
                 or (config.MEMORY_DIR / "users" / memory.safe_id(exact)
                     / "prefs.json").is_file())):
        return True
    try:
        load(user)
    except PreferencesStateError:
        return True
    return False


def quarantine_reason(user: str) -> str | None:
    """The operator-facing explanation, or None when not quarantined."""
    try:
        load(user)
    except PreferencesStateError as err:
        return str(err)
    return QUARANTINE_REASON if is_quarantined(user) else None


def _posture(key: str):
    """A fresh copy, so a caller mutating the result cannot poison the map."""
    value = QUARANTINE_POSTURE[key]
    if isinstance(value, dict):
        return json.loads(json.dumps(value))
    if isinstance(value, list):
        return list(value)
    return value


def load(user: str) -> dict:
    """Load one exact preference object, distinguishing missing from corrupt."""
    path = _path(user)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise PreferencesStateError(
            user, path, f"unreadable {type(err).__name__}") from err
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as err:
        raise PreferencesStateError(user, path, "invalid UTF-8") from err
    except json.JSONDecodeError as err:
        raise PreferencesStateError(user, path, "malformed JSON") from err
    if not isinstance(data, dict):
        raise PreferencesStateError(user, path, "JSON root is not an object")
    return data


def _raw_get(user: str, key: str, default=None):
    """The stored value, with NO quarantine clamp.

    Only for resolving quarantine itself (`migrate_legacy` comparing what is
    already present) and for operator inspection. Never for an authorization
    decision — that is what `get` is for.
    """
    return load(user).get(key, default)


def get(user: str, key: str, default=None):
    """A preference, clamped when authorization evidence is unavailable.

    Cosmetic keys may use the caller's explicit display default.  Security
    keys take ``QUARANTINE_POSTURE``.  Keys whose normal default itself widens
    policy (currently the shared daily budget, where 0 means unlimited) retain
    the typed error so their consumer can refuse new work.
    """
    if key in QUARANTINE_POSTURE and is_quarantined(user):
        # Legacy quarantine is a read clamp: an exact file can be prepared
        # while the ambiguous source remains out of effect. A corrupt CURRENT
        # file is different — `set` loads strictly and refuses to overwrite it.
        return _posture(key)
    try:
        return load(user).get(key, default)
    except PreferencesStateError:
        if key in QUARANTINE_POSTURE:
            return _posture(key)
        if key in _STRICT_EVIDENCE_KEYS:
            raise
        return default


def set(user: str, key: str, value) -> None:
    # Cross-process RMW under proclock + atomic replace (ADR 0005): the
    # shared-scope file carries daily_budget — a lost update or torn read
    # here silently deletes the budget guard's persisted cap.
    from . import proclock
    # Lock on the STORAGE key, not the raw id: two colliding principals now
    # write different files and must not serialize against each other, while
    # one principal reaching the same file through two spellings must.
    with proclock.lock(f"prefs-{memory.storage_key(user)}"):
        data = load(user)
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        path = _path(user)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        from . import atomicio
        atomicio.publish(tmp, path, json.dumps(data, indent=2))


def state_status(user: str) -> dict:
    """Non-sensitive evidence status for an operator or the CLI."""
    exact = memory.canonical_owner(user)
    path = _path(exact)
    try:
        data = load(exact)
    except PreferencesStateError as err:
        return {
            "owner": exact,
            "state": "unavailable",
            "quarantined": True,
            "reason": err.reason,
            "repair_command": err.repair_command,
        }
    state = "valid" if path.is_file() else "missing"
    legacy = is_quarantined(exact)
    return {
        "owner": exact,
        "state": state,
        "quarantined": legacy,
        "reason": QUARANTINE_REASON if legacy else None,
        # Key names make policy presence auditable without exposing values.
        "keys": sorted(str(key) for key in data),
        "repair_command": None,
    }


def repair(user: str) -> dict:
    """Explicitly preserve a corrupt exact blob, then reset it to ``{}``.

    Repair is never called by a reader or background loop.  The original bytes
    are first durably published beside the source under a content-addressed
    SHA-256-prefix name; only then is a valid empty object published at
    ``prefs.json``.  A missing or already-valid file is reported and left
    byte-for-byte alone.
    """
    from . import atomicio, proclock

    exact = memory.canonical_owner(user)
    path = _path(exact)
    with proclock.lock(f"prefs-{memory.storage_key(exact)}"):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            result = state_status(exact)
            result["repaired"] = False
            return result
        except OSError as err:
            raise PreferencesStateError(
                exact, path, f"unreadable {type(err).__name__}") from err

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            result = state_status(exact)
            result["repaired"] = False
            return result

        digest = hashlib.sha256(raw).hexdigest()
        quarantine = path.with_name(
            f"prefs.corrupt.{digest[:_QUARANTINE_DIGEST_HEX]}.json")
        try:
            existing = quarantine.read_bytes()
        except FileNotFoundError:
            # Do not derive the temporary name from the archive name: doing so
            # crossed MAX_PATH on Windows for long exact-owner storage keys.
            tmp_q = quarantine.with_name(f".prefs-repair.{os.getpid()}.tmp")
            atomicio.publish(tmp_q, quarantine, raw)
        except OSError as err:
            raise PreferencesStateError(
                exact, path, f"quarantine unreadable {type(err).__name__}") from err
        else:
            if existing != raw:
                raise PreferencesStateError(
                    exact, path, "content-addressed quarantine collision")

        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        atomicio.publish(tmp, path, json.dumps({}, indent=2))
        return {
            "owner": exact,
            "state": "valid",
            "quarantined": is_quarantined(exact),
            "reason": QUARANTINE_REASON if is_quarantined(exact) else None,
            "keys": [],
            "repair_command": None,
            "repaired": True,
            "quarantined_sha256": digest,
            "quarantine_file": quarantine.name,
        }


# --- pre-v2 preference files: quarantined, never implicitly claimed -------
#
# `users/<safe_id>/prefs.json` was written under a key several principals map
# to, so nothing in it says whose settings those are. It is NOT read any more —
# not even for a principal whose exact identity equals that normalized string,
# because equalling a lossy value is exactly what every collision victim also
# does. Affected principals are clamped by ``QUARANTINE_POSTURE``: autonomy L0,
# no granted scopes, guest capability profile, no operator sites/advanced mode,
# no earned-autonomy opt-in and no pending secure-login capture.  Ordinary
# defaults are deliberately not treated as a security posture.


def legacy_owners() -> list[str]:
    """Pre-v2 per-user preference files still on disk. Operator inspection."""
    root = config.MEMORY_DIR / "users"
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and (d / "prefs.json").is_file())


def legacy_keys(legacy_id: str) -> list[str]:
    """The preference KEYS held in a quarantined file, so an operator can see
    what would be migrated without the values being surfaced in a listing."""
    path = config.MEMORY_DIR / "users" / legacy_id / "prefs.json"
    if not path.is_file():
        return []
    try:
        return sorted(json.loads(path.read_text(encoding="utf-8")).keys())
    except json.JSONDecodeError:
        return []


def migrate_legacy(legacy_id: str, owner: str, *, overwrite: bool = False) -> int:
    """Claim a quarantined preference file for an EXACT principal.

    The explicit decision the code refuses to make on its own: the operator
    states out of band whose settings these were. Returns the number of keys
    migrated; the legacy file is removed so it cannot be claimed twice.
    """
    exact = memory.assert_not_system_owner(owner)
    if legacy_id not in legacy_owners():
        raise ValueError(f"'{legacy_id}' is not a quarantined preference file")
    path = config.MEMORY_DIR / "users" / legacy_id / "prefs.json"
    try:
        legacy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(
            "quarantined legacy preference evidence is unreadable or "
            "malformed; it was preserved. Discard it explicitly if recovery "
            "is not required") from err
    if not isinstance(legacy, dict):
        raise ValueError(
            "quarantined legacy preference evidence is not a JSON object; it "
            "was preserved. Discard it explicitly if recovery is not required")
    moved = 0
    for key, value in legacy.items():
        # `_raw_get`, not `get`: the legacy file is still on disk here, so the
        # quarantine clamp is active and `get` would report a posture value for
        # every security key — making each look "already present" and skipping
        # the whole migration.
        if not overwrite and _raw_get(exact, key) is not None:
            continue
        set(exact, key, value)
        moved += 1
    path.unlink()                     # resolves the quarantine for this group
    return moved


def discard_legacy(legacy_id: str) -> bool:
    """Delete a quarantined preference file outright. Returns False if it was
    not quarantined."""
    if legacy_id not in legacy_owners():
        return False
    (config.MEMORY_DIR / "users" / legacy_id / "prefs.json").unlink()
    return True
