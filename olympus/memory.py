"""Persistent file-based memory — the substrate of self-improvement.

Layout:
    memory/
      lessons/ corrections/ feedback/        shared (system-generated)
      owners/<owner-key>/lessons|corrections|feedback/   exact private notes
      users/<safe-id>/                         ambiguous legacy, preserved
      reports/ upgrades/ prompt_backups/ evals/       always shared (system)
      conversations/<id>.json                persisted chat histories
      conversations/<id>.owner               trusted search-index owner
      sessions/<id>.journal.jsonl            sealed per-session journal (sessionlog)
      skills/                                the self-built skill library

User-scoped categories keep one person's lessons, corrections, and feedback
out of everyone else's sessions. The active user is a context variable set by
the orchestrator at the start of each conversation turn.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import io
import json
import os
import re
import tarfile
import threading
import time
from pathlib import Path

from . import config

CATEGORIES = ("lessons", "corrections", "feedback", "reports", "upgrades",
              "prompt_backups", "evals", "job_reports")
USER_SCOPED = {"lessons", "corrections", "feedback"}

#: Categories stored per EXACT owner and never swept by a shared read.
#:
#: `reports` is deliberately installation-global: `opportunity_scan`,
#: `evolution_audit`, skill curation, feature evolution and the replay gate all
#: write genuinely shared system notes there, and every owner is meant to find
#: them. A scheduled JOB's answer is the opposite — it is one owner's private
#: output, produced by their prompt under their identity — so it gets its own
#: category rather than making `reports` user-scoped and breaking the shared
#: notes that belong there.
#:
#: A private category is NEVER part of a shared sweep. An ordinary `search()`
#: reads exactly one private directory — `current_owner()`'s own — so the owner
#: of a job report finds it and nobody else does. That authorization comes from
#: the exact-owner ContextVar the request binding sets; `current_user()` is
#: `safe_id`-normalized (see `owner_key`) and must never authorize a private
#: read, because it merges distinct principals.
#:
#: Generic APIs now resolve exact owners for USER_SCOPED notes. They retain
#: the explicit job_reports refusal as the established scheduler API boundary. The
#: `*_for` helpers take the principal as an argument and are the trusted
#: background/admin path for code that has a durable owner but no request
#: context (the heartbeat, export, retention).
PRIVATE_CATEGORIES = frozenset({"job_reports"})

# On-disk format versions (Olympus's data-sovereignty contract — see
# docs/MEMORY_FORMAT.md). NOTE_SCHEMA_VERSION stamps each markdown note's
# frontmatter; ARCHIVE_SCHEMA_VERSION stamps an export's manifest. Import
# refuses any archive version it doesn't understand rather than guessing.
NOTE_SCHEMA_VERSION = 2
ARCHIVE_SCHEMA_VERSION = 2
SUPPORTED_ARCHIVE_VERSIONS = frozenset({1, 2})

_USER: contextvars.ContextVar[str] = contextvars.ContextVar(
    "olympus_user", default="shared"
)

#: The EXACT durable principal, kept alongside `_USER`.
#:
#: `_USER` is `safe_id`-normalized because it is used to build paths, and that
#: normalization is lossy: `tg-a.b`, `tg-a@b`, `tg-a b` and `tg-a-b` all become
#: one value, as do any two ids sharing a 64-character sanitized prefix. It can
#: therefore never authorize a private read. `_OWNER` carries the principal
#: verbatim and is what PRIVATE_CATEGORIES are keyed and authorized on. Both are
#: set together by `set_user`, so every existing caller gets the exact identity
#: without changing its call.
_OWNER: contextvars.ContextVar[str] = contextvars.ContextVar(
    "olympus_owner", default="shared"
)


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value)).strip("-")[:64] or "shared"


def canonical_owner(owner) -> str:
    """The EXACT durable principal, with only the legacy default applied.

    A missing/empty/blank owner becomes "shared" — the pre-existing contract
    everywhere else in the tree — so a legacy record stays addressable instead
    of acquiring a separate, unreachable blank identity. Nothing else is
    changed: no stripping of interior characters, no case folding, no
    truncation, and never `safe_id`.
    """
    if owner is None:
        return "shared"
    text = str(owner)
    return text if text.strip() else "shared"


def set_user(user: str) -> None:
    """Set the memory namespace for the current thread/conversation.

    Sets BOTH the `safe_id` path namespace and the exact-owner context, so
    every existing caller (orchestrator, gateway, tools) supplies the exact
    principal for private-category authorization without changing its call.

    Canonicalized ONCE, with both values derived from that result. Deriving
    them independently splits the identity for a blank owner: `safe_id(None)`
    is the literal string "None" while `canonical_owner(None)` is "shared", so
    the path namespace and the authorization context would name two different
    principals.
    """
    exact = canonical_owner(user)
    _USER.set(safe_id(exact))
    _OWNER.set(exact)


def current_user() -> str:
    """The `safe_id` path namespace. Lossy — never use it as an identity."""
    return _USER.get()


def current_owner() -> str:
    """The EXACT durable principal for the current request. The only value
    that may authorize a private-category read or write."""
    return _OWNER.get()


@contextlib.contextmanager
def user_context(user: str):
    """Run a block as `user`, then restore BOTH contexts to exactly what they
    were — by ContextVar token, not by re-setting a guessed value like
    "shared", which discards the caller's namespace just as thoroughly. Unwinds
    on every exit path, exception included."""
    exact = canonical_owner(user)
    user_token = _USER.set(safe_id(exact))
    owner_token = _OWNER.set(exact)
    try:
        yield
    finally:
        _OWNER.reset(owner_token)
        _USER.reset(user_token)


def owner_key(owner) -> str:
    """A path-safe directory key for an EXACT principal.

    `safe_id` alone is not usable as an identity: it collapses every run of
    non-[A-Za-z0-9_-] to a single "-" and truncates at 64 characters, so
    `tg-a.b`, `tg-a@b`, `tg-a b` and `tg-a-b` all become one key, as do any two
    ids agreeing on their first 64 sanitized characters. A private store keyed
    that way would file two different people's output in one directory.

    The key is a bounded readable label for a human browsing the store, plus
    the COMPLETE SHA-256 hex digest of the exact canonical principal. The label
    may collide; the digest is collision-resistant, so the pair is too. The
    full digest is used rather than a prefix — a truncated hash trades a
    security property for filename length that nothing here needs.
    """
    exact = canonical_owner(owner)
    digest = hashlib.sha256(exact.encode("utf-8")).hexdigest()
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", exact).strip("-")[:32] or "owner"
    return f"{label}-{digest}"


#: Reserved, installation-owned namespaces. These are NOT tenants: they name
#: the installation itself, and their stores are addressed by the literal name
#: rather than by an owner digest so they stay stable across this migration and
#: keep working for code that hardcodes them.
#:
#: * ``shared`` — the ambient default. The heartbeat and every background task
#:   with no request context run as ``shared``, and installation-wide settings
#:   (the daily budget cap, shared system notes) live under it.
#: * ``operator`` — the installation's own credential namespace: ``opconfig``
#:   config secrets and ``secretref`` entries.
#:
#: THE RESERVATION IS A POLICY, NOT AN INFERENCE. A name in this set must never
#: be issued as an authenticated tenant principal: anyone bound to the literal
#: string ``operator`` would address the installation's config-secret vault.
#: Every real principal Olympus mints is prefixed (``tg-``/``dc-``/``sl-``/
#: ``u:``/``web-``) or is the literal ``cli``, so nothing collides today, and
#: `assert_not_system_owner` is available for a binding that accepts an
#: externally-supplied id.
SYSTEM_OWNERS = frozenset({"shared", "operator"})

# Gateway prefixes whose pre-v2 identity constructor accepted a keyspace wider
# than safe_id's alphabet. Their old `<prefix>-<safe_id(raw)>` principals are
# ambiguous and must not retain unattended authority after the v2 digest mint.
# Discord/Slack/Telegram/WhatsApp are absent: their preserved platform ids are
# validated numeric/alphanumeric values at the authenticated transport boundary.
AMBIGUOUS_GATEWAY_PREFIXES = frozenset(
    {"ol", "email", "hook", "sg", "mx", "mm", "gc", "sms"})
GATEWAY_OWNER_VERSION = "v2"
_GATEWAY_DIGEST = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def is_system_owner(owner) -> bool:
    """True when `owner` names the installation rather than a tenant."""
    return canonical_owner(owner) in SYSTEM_OWNERS


def assert_not_system_owner(owner) -> str:
    """Return the exact principal, refusing a reserved installation namespace.

    For request bindings that accept an externally-supplied identity: being
    authenticated as `operator` must never be a way to reach the installation's
    own credential vault.
    """
    exact = canonical_owner(owner)
    if exact in SYSTEM_OWNERS:
        raise ValueError(
            f"'{exact}' is a reserved installation namespace and cannot be a "
            "tenant principal")
    return exact


def is_ambiguous_gateway_owner(owner) -> bool:
    """Whether ``owner`` was minted by the old lossy gateway constructor.

    New derived principals are `<prefix>-v2-<full SHA-256 base64url>`. An old
    principal under a derivation-required channel records only `safe_id(raw)`;
    it cannot be attributed to one external account and is therefore suitable
    for operator inspection only, never unattended execution or credentials.
    """
    exact = canonical_owner(owner)
    if is_system_owner(exact):
        return False
    for prefix in AMBIGUOUS_GATEWAY_PREFIXES:
        marker = f"{prefix}-{GATEWAY_OWNER_VERSION}-"
        if exact.startswith(marker):
            return _GATEWAY_DIGEST.fullmatch(exact[len(marker):]) is None
        if exact.startswith(f"{prefix}-"):
            return True
    return False


def storage_key(owner) -> str:
    """The storage key for a per-owner private store (vault, prefs).

    A reserved installation namespace keeps its literal name; every tenant is
    keyed by `owner_key`, the exact principal plus its complete SHA-256 digest.
    `owner_key` always appends `-<64 hex>`, so it can never collide with a
    reserved bare name.

    There is deliberately NO fallback to the `safe_id` form. A pre-migration
    store was keyed by a value that several distinct principals share, so
    reading it for "whoever's exact identity equals that string" would hand one
    principal another's credentials — the merge this key exists to remove.
    """
    exact = canonical_owner(owner)
    if exact in SYSTEM_OWNERS:
        return exact
    return owner_key(exact)


def _dir(category: str, user: str = "shared") -> Path:
    """Create the directory and retain the configured root's path spelling.

    Windows extended paths belong at the I/O boundary inside notes.mkdir;
    returning that spelling here breaks existing Path containment consumers.
    """
    from . import note_evidence as notes
    with notes.guard():
        path = notes.directory(user, category)
        notes.mkdir(path)
        return path


def _reject_private(category: str, api: str) -> None:
    """Preserve the scheduler's explicit owner-bound job-report API contract."""
    if category in PRIVATE_CATEGORIES:
        raise ValueError(
            f"'{category}' is a private category; use the owner-bound API "
            f"({api}_for) with an exact principal")


def _require_private(category: str, api: str) -> None:
    if category not in PRIVATE_CATEGORIES:
        raise ValueError(f"'{category}' is not a private category; {api} is owner-bound")


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "note"


# --- versioned note frontmatter ------------------------------------------

def _frontmatter(schema_version: int, created: str) -> str:
    return f"---\nschema_version: {schema_version}\ncreated: {created}\n---\n"


def render_note(title: str, content: str, *,
                schema_version: int = NOTE_SCHEMA_VERSION,
                created: str | None = None) -> str:
    """A note's on-disk text: a small versioned frontmatter block, then the
    same `# title` + body Olympus has always written."""
    if schema_version == 2:
        from . import note_evidence as notes
        return notes.render("shared", "lessons", title, content, created=created).decode("utf-8")
    created = created or time.strftime("%Y%m%d-%H%M%S")
    return _frontmatter(schema_version, created) + f"# {title}\n\n{content.strip()}\n"


def parse_note(text: str) -> tuple[dict, str]:
    """Split a note into (metadata, body). A note with no frontmatter is a v0
    note (`{}` metadata) — readers tolerate both so old memory still loads."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            meta: dict = {}
            for line in text[4:end].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            return meta, text[end + 5:]
    return {}, text


def note_schema_version(text: str) -> int:
    meta, _ = parse_note(text)
    try:
        return int(meta.get("schema_version", 0))
    except (TypeError, ValueError):
        return 0


def note_title(text: str) -> str:
    """The human title of a note, ignoring any frontmatter."""
    _, body = parse_note(text)
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
        if line.strip():
            return line.strip()
    return ""


def save(category: str, title: str, content: str) -> Path:
    """Compatibility Path API; optional mirror failures also produce a receipt/warning."""
    return Path(save_with_status(category, title, content)["path"])


def save_with_status(category: str, title: str, content: str) -> dict:
    """Canonical save and explicit optional-mirror outcome for CLI/tool consumers."""
    _reject_private(category, "save")
    from . import note_evidence as notes
    path = notes.create(current_owner(), category, title, content)
    mirror = _mirror_to_vault(category, path)
    return {"path": str(path), "canonical_saved": True, "mirror": mirror}


def retry_note_mirror(owner: str, category: str, filename: str) -> dict:
    """Retry only an existing exact-owner derived copy; never create another note."""
    from . import note_evidence as notes
    if (not isinstance(filename, str) or Path(filename).name != filename
            or not filename.endswith(".md") or "/" in filename or "\\" in filename):
        raise ValueError("select a canonical note filename")
    with user_context(owner), notes.guard():
        root = notes._check_scope(current_owner(), category)
        path = root / filename
        notes.validate_note(notes.read_raw(path), current_owner(), category)
        return _mirror_to_vault(category, path)


def _save_note(d: Path, category: str, title: str, content: str) -> Path:
    from . import note_evidence as notes
    owner = current_owner()
    if notes.logical(d) != notes.logical(notes.directory(owner, category)):
        raise notes.NoteStateError("note sink does not match the bound owner")
    path = notes.create(owner, category, title, content)
    _mirror_to_vault(category, path)
    return path


# Categories worth reading in a knowledge GUI (Obsidian is just a folder).
#: Mirrors use a new full exact-owner key plus category. Old flat mirrors
#: remain unclaimed and untouched. job_reports remains excluded entirely by
#: its established private-report contract.
_VAULT_CATEGORIES = frozenset({"lessons", "reports", "corrections"})


def _mirror_to_vault(category: str, path: Path) -> dict:
    """Optional derived copy; a mirror failure never repeats a committed note."""
    from . import note_evidence as notes, note_archive
    vault = os.environ.get("OLYMPUS_VAULT_DIR", "").strip()
    if not vault or category not in _VAULT_CATEGORIES:
        return {"state": "disabled", "canonical_saved": True}
    owner = notes.category_owner(current_owner(), category)
    receipt = config.MEMORY_DIR / "note-mirror-v2" / owner_key(owner) / (path.name + ".json")
    result = {"version": 2, "owner": owner, "note": notes.relative(path),
              "canonical_saved": True, "state": "unavailable"}
    try:
        with notes.guard():
            raw = notes.read_raw(path)
            notes.validate_note(raw, owner, category)
            # New owner-aware tree; previous flat/mixed mirrors stay untouched.
            output = Path(vault).expanduser() / "olympus-notes-v2" / owner_key(owner) / category / path.name
            note_archive.external_publish(output, raw)
            if note_archive.external_read(output, notes.MAX_NOTE) != raw:
                raise OSError("mirror verification failed")
            result.update(state="available", sha256=notes.digest(raw))
    except (OSError, ValueError, notes.evidence.OwnerEvidenceStateError):
        result["reason"] = "mirror publication unconfirmed; canonical note is saved; do not repeat it"
    try:
        with notes.guard():
            notes.publish(receipt, json.dumps(result, sort_keys=True).encode())
    except (OSError, notes.evidence.OwnerEvidenceStateError):
        result["receipt_state"] = "unavailable"
    if result["state"] != "available" or result.get("receipt_state"):
        notes.mirror_warning("Canonical note saved; optional mirror evidence unavailable. Inspect memory notes-status.")
    return result


def _search_dirs() -> list[Path]:
    from . import note_evidence as notes
    dirs = [notes.directory("shared", c) for c in CATEGORIES if c not in PRIVATE_CATEGORIES]
    if current_owner() != "shared":
        dirs += [notes.directory(current_owner(), c) for c in USER_SCOPED]
    dirs += [notes.directory(current_owner(), c) for c in PRIVATE_CATEGORIES]
    return dirs


def _note_rows(category, owner, *, include_shared):
    from . import note_evidence as notes
    with notes.guard():
        rows = notes.notes(owner, category)
        if include_shared and category in USER_SCOPED and canonical_owner(owner) != "shared":
            rows += notes.notes("shared", category)
        return rows


def _recent_rows(category, owner, n, include_shared):
    from . import note_evidence as notes
    notes.evidence.integer(n, minimum=0, maximum=notes.MAX_FILES)
    return sorted(_note_rows(category, owner, include_shared=include_shared),
                  key=lambda row: row["path"].name, reverse=True)[:n]


def _prune_rows(owner, category, keep):
    from . import note_evidence as notes
    notes.evidence.integer(keep, minimum=0, maximum=notes.MAX_FILES)
    with notes.guard():
        rows = sorted(notes.notes(owner, category), key=lambda row: row["path"].name, reverse=True)
        return len(notes.delete_rows(rows[keep:]))


def search(query: str, limit: int = 5) -> str:
    from . import note_evidence as notes
    notes.evidence.text(query, 32768, empty=True)
    notes.evidence.integer(limit, minimum=0, maximum=100)
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    scored = []
    with notes.guard():
        for category in CATEGORIES:
            for row in _note_rows(category, current_owner(), include_shared=True):
                lower = row["body"].lower()
                title = lower.splitlines()[0] if lower else ""
                score = sum(1 + min(lower.count(t), 5) * 0.2 + (2 if t in title else 0)
                            for t in terms if t in lower)
                if score:
                    scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], str(item[1]["path"])))
    if not scored:
        return "No memory entries match that query."
    return "\n\n".join(
        f"--- {row['category']}/{row['path'].name} ---\n{row['body'][:1500]}"
        for _, row in scored[:limit])


# --- explicit owner-bound API (private categories) -------------------------
#
# Authorization here is an ARGUMENT, never the ambient namespace. A background
# worker's ContextVar is whatever the last runner happened to leave behind, and
# The historical namespace is `safe_id`-normalized, so it can neither be trusted nor
# even represent the exact principal. Every private read and write names its
# owner explicitly.


def save_for(owner: str, category: str, title: str, content: str) -> Path:
    _require_private(category, "save_for")
    from . import note_evidence as notes
    with user_context(owner):
        path = notes.create(owner, category, title, content)
        _mirror_to_vault(category, path)
        return path


def search_for(owner: str, query: str, limit: int = 5) -> str:
    """Search shared memory plus `owner`'s own private categories.

    A convenience wrapper that establishes the trusted context explicitly, for
    background work (the heartbeat) that has a durable owner but no request
    context. Production request paths call `search()`, which reads the context
    the authenticated request already set.
    """
    with user_context(owner):
        return search(query, limit)


def recent_for(owner: str, category: str, n: int = 5) -> list[str]:
    _require_private(category, "recent_for")
    return [row["body"] for row in _recent_rows(category, owner, n, False)]


def count_for(owner: str, category: str) -> int:
    _require_private(category, "count_for")
    return len(_note_rows(category, owner, include_shared=False))


def prune_for(owner: str, category: str, keep: int = 200) -> int:
    _require_private(category, "prune_for")
    return _prune_rows(owner, category, keep)


def recent(category: str, n: int = 5) -> str:
    _reject_private(category, "recent")
    rows = _recent_rows(category, current_owner(), n, True)
    if not rows:
        return f"(no {category} recorded yet)"
    return "\n\n".join(f"--- {row['path'].name} ---\n{row['body'][:1500]}" for row in rows)


def recent_titles(category: str, n: int = 5) -> list[str]:
    _reject_private(category, "recent_titles")
    return [note_title(row["body"]) or row["path"].stem
            for row in _recent_rows(category, current_owner(), n, True)]


def prune(category: str, keep: int = 200) -> str:
    _reject_private(category, "prune")
    removed = _prune_rows(current_owner(), category, keep)
    from . import note_evidence as notes
    remaining = len(notes.notes(current_owner(), category))
    return f"Pruned {removed} old {category} entries (kept {remaining})."


def category_count(category: str) -> int:
    _reject_private(category, "category_count")
    return len(_note_rows(category, current_owner(), include_shared=True))


# --- persisted conversations ---------------------------------------------

def _conversation_path(conversation_id: str) -> Path:
    d = config.MEMORY_DIR / "conversations"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe_id(conversation_id)}.json"


def _conversation_owner_path(conversation_id: str) -> Path:
    return config.MEMORY_DIR / "conversations" / f"{safe_id(conversation_id)}.owner-v2"


def conversation_binding(conversation_id: str) -> dict | None:
    """Read exact attribution; old `.owner` files remain preserved and unclaimed."""
    from . import owner_evidence as evidence
    raw = evidence.read_bytes(_conversation_owner_path(conversation_id), 32768,
                              "conversation ownership")
    if raw is None:
        return None
    value = evidence.decode(raw, "conversation ownership", 32768)
    try:
        evidence.fields(value, ("version", "owner", "conversation"))
        if type(value["version"]) is not int or value["version"] != 2:
            raise ValueError("invalid version")
        evidence.text(value["owner"], 8192)
        evidence.text(value["conversation"], 8192)
        if (value["owner"] != canonical_owner(value["owner"])
                or safe_id(value["conversation"]) != safe_id(conversation_id)):
            raise ValueError("invalid attribution")
    except (TypeError, ValueError, KeyError) as err:
        raise evidence.OwnerEvidenceStateError("conversation ownership", "invalid binding") from err
    return value


def conversation_owner(conversation_id: str) -> str | None:
    binding = conversation_binding(conversation_id)
    return None if binding is None else binding["owner"]


def _bind_conversation_owner(conversation_id: str, owner: str) -> str:
    """Write-once exact owner and exact conversation identity, before snapshot."""
    from . import owner_evidence as evidence, proclock
    principal = canonical_owner(owner)
    evidence.text(principal, 8192)
    evidence.text(conversation_id, 8192)
    path = _conversation_owner_path(conversation_id)
    with proclock.lock(f"conversation-owner-{safe_id(conversation_id)}"):
        existing = conversation_binding(conversation_id)
        if existing is not None:
            if existing["owner"] != principal or existing["conversation"] != conversation_id:
                raise PermissionError("conversation belongs to a different memory principal or identifier")
            return principal
        legacy = path.with_suffix(".owner")
        snapshot = path.with_suffix(".json")
        if legacy.exists() or legacy.is_symlink() or snapshot.exists() or snapshot.is_symlink():
            raise evidence.OwnerEvidenceStateError("conversation ownership", "ambiguous legacy snapshot or binding")
        evidence.publish(path, json.dumps({"version": 2, "owner": principal,
                         "conversation": conversation_id}, allow_nan=False).encode(),
                         "conversation ownership")
    return principal


def load_conversation(conversation_id: str) -> list[dict]:
    path = _conversation_path(conversation_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            # Corrupt snapshot: rebuild from the session journal's verified
            # prefix (C1) instead of silently dropping the whole history.
            try:
                from . import errors, sessionlog
                if sessionlog.enabled():
                    recovered = sessionlog.recover_history(conversation_id)
                    if recovered:
                        errors.capture(
                            "memory.load_conversation", err,
                            context=f"corrupt snapshot {safe_id(conversation_id)}; "
                                    f"recovered {len(recovered)} messages "
                                    "from the session journal")
                        return recovered
            except Exception:
                pass
            return []
    return []


def save_conversation(conversation_id: str, history: list[dict], *,
                      owner: str | None = None) -> None:
    # Atomic publish: load_conversation maps a torn file to [], so a crash
    # mid-write would drop the whole history (ADR 0005).
    principal = _bind_conversation_owner(
        conversation_id, current_owner() if owner is None else owner)
    p = _conversation_path(conversation_id)
    # The temp name must be unique per WRITER, not per process: two threads of
    # one process saving the same conversation shared `.{name}.{pid}.tmp`, so
    # one write silently clobbered the other and the loser got FileNotFoundError
    # from os.replace raised into its reply path (Phase-4 Stage-C defect D-3).
    # `os.replace` is atomic, so distinct temps make concurrent saves last-writer
    # -wins instead of corrupt-or-crash; the sealed journal remains the ordered
    # record of every turn.
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    from . import atomicio
    atomicio.publish(tmp, p, json.dumps(history, indent=1))
    # Seal this turn's delta into the session journal (C1) — purely additive:
    # the snapshot above stays the source of truth, and a journal failure must
    # never block the reply (sync captures its own errors; belt and braces).
    try:
        from . import sessionlog
        sessionlog.sync(conversation_id, history)
    except Exception as err:
        from . import errors
        errors.capture("memory.save_conversation", err, context="session journal")
    # Keep the cross-session search index fresh (best-effort; never block a save).
    try:
        from . import search
        search.index_conversation(safe_id(conversation_id), history,
                                  owner=principal)
    except Exception as err:
        from . import errors
        errors.capture("memory.save_conversation", err,
                       context="search evidence unavailable; snapshot saved")


def _conversation_preview(history: list[dict]) -> str:
    """One line that identifies a session at a glance. Prefer the distilled
    state block (what the conversation is durably ABOUT) over the literal
    first message; fall back to the first user turn."""
    for m in history:
        content = str(m.get("content", ""))
        if content.startswith("[Conversation state"):
            body = content.partition("\n")[2].strip() or content
            return " ".join(body.split())[:100]
    for m in history:
        if m.get("role") == "user":
            return " ".join(str(m.get("content", "")).split())[:100]
    return "(empty)"


def list_conversations(prefix: str = "") -> list[dict]:
    """Saved conversations, newest first: {id, mtime, turns, preview}.
    `prefix` filters by id prefix (e.g. 'cli' for terminal sessions)."""
    d = config.MEMORY_DIR / "conversations"
    if not d.exists():
        return []
    out: list[dict] = []
    for path in d.glob("*.json"):
        cid = path.stem
        if prefix and not cid.startswith(prefix):
            continue
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "id": cid,
            "mtime": path.stat().st_mtime,
            "turns": sum(1 for m in history if m.get("role") == "user"),
            "preview": _conversation_preview(history),
        })
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out


# --- YouTube watch queue ------------------------------------------------

def _watchlist_path() -> Path:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return config.MEMORY_DIR / "watchlist.txt"


def watchlist_add(url: str) -> None:
    from . import proclock
    with proclock.lock("watchlist"):
        with _watchlist_path().open("a", encoding="utf-8") as f:
            f.write(url.strip() + "\n")


def watchlist_pop() -> str | None:
    # Read-modify-write under the cross-process lock (ADR 0005): the heartbeat
    # and web processes both pop; unlocked, a concurrent add or pop could be
    # silently dropped by this rewrite. A lock timeout (wedged peer) returns
    # None — the entry stays queued for the next tick, nothing is lost.
    from . import proclock
    try:
        return _watchlist_pop_locked(proclock)
    except TimeoutError:
        return None


def _watchlist_pop_locked(proclock) -> str | None:
    with proclock.lock("watchlist"):
        path = _watchlist_path()
        if not path.exists():
            return None
        lines = [l for l in path.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        if not lines:
            return None
        url, rest = lines[0], lines[1:]
        # Atomic rewrite: a crash mid-write_text would drop every remaining
        # queued URL; with os.replace the queue is always old-or-new.
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        from . import atomicio
        atomicio.publish(tmp, path,
                         "\n".join(rest) + ("\n" if rest else ""))
        return url


def sweep_dated_files(retain_days: int) -> int:
    """Delete per-day trace/usage files older than retain_days. Returns count.

    Files are named YYYYMMDD.* / YYYY-MM-DD.json — old ones are bounded growth
    on a long-running instance."""
    import datetime
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=max(retain_days, 1)))
    removed = 0
    for sub in ("traces", "usage"):
        d = config.MEMORY_DIR / sub
        if not d.exists():
            continue
        for path in d.glob("*"):
            digits = re.sub(r"\D", "", path.stem)[:8]
            if len(digits) != 8:
                continue
            try:
                stamp = datetime.date(int(digits[:4]), int(digits[4:6]),
                                      int(digits[6:8]))
            except ValueError:
                continue
            if stamp < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def sweep_orphan_responses() -> int:
    """Delete frozen LLM responses no surviving trace references. Returns count.

    The re-executable decision log freezes each LLM response at
    `responses/<hash>.json` (see `replaystore`). Those files are content-
    addressed, not dated, so `sweep_dated_files` can't bound them; instead we
    keep a response exactly as long as some retained run still references it
    (via a decision's `model_response_ref`). Once `sweep_dated_files` has pruned
    old traces, the responses only they referenced become orphans and are
    removed here — a recorded run stays fully re-executable for its whole life."""
    resp_dir = config.MEMORY_DIR / "responses"
    if not resp_dir.exists():
        return 0
    referenced: set[str] = set()
    traces = config.MEMORY_DIR / "traces"
    if traces.exists():
        for path in traces.glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for d in rec.get("decisions", []):
                    ref = d.get("model_response_ref")
                    if ref:
                        referenced.add(ref)
    removed = 0
    for path in resp_dir.glob("*.json"):
        if path.stem not in referenced:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def sweep_tool_results(retain_days: int) -> int:
    """Delete frozen client-side tool results and frozen run-state context older
    than retain_days. Returns count. These are keyed by tool_use id / run id (not
    trace-referenced), so they're pruned by age — aligned with trace retention,
    keeping a run replayable for its whole retained life while bounding growth."""
    import time as _time
    cutoff = _time.time() - max(retain_days, 1) * 86400
    removed = 0
    for sub in ("tool_results", "context"):
        d = config.MEMORY_DIR / sub
        if not d.exists():
            continue
        for path in d.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


# Append-only evidence ledgers written by the Colibri-absorption capabilities.
# Each row carries a "ts" epoch seconds; pruning keeps rows newer than the
# cutoff. Retention is deliberate policy, not housekeeping: the programme's rule
# is that raw operational logs must NOT silently become permanent moat data.
_EVIDENCE_LEDGERS = (
    ("modelgrade", "evidence.jsonl"),
    ("routesub", "decisions.jsonl"),
    ("streamguard", "pathologies.jsonl"),
    ("ingest", "refusals.jsonl"),
    ("experiments", "state.jsonl"),
)


def sweep_evidence(retain_days: int) -> int:
    """Prune absorption evidence ledgers and watchdog forensics by age.

    Returns the number of records + files removed. Without this the stores added
    by Waves 1-2 grow unbounded — `sweep_dated_files` only ever covered `traces`
    and `usage`.

    Note on `modelgrade`: pruning evidence legitimately lowers a card's sample
    count, which is the SAME staleness policy the module already applies
    (evidence past its TTL loses confidence). `OLYMPUS_RETAIN_DAYS` and
    `OLYMPUS_GRADE_TTL_DAYS` both default to 30, so a pruned row was already
    weightless. Never repairs a malformed row — an unparseable line is kept, so
    a corrupt ledger still trips its owner's reject-never-repair path rather
    than being silently rewritten here."""
    import json as _json
    import time as _time
    cutoff = _time.time() - max(retain_days, 1) * 86400
    removed = 0

    for sub, name in _EVIDENCE_LEDGERS:
        path = config.MEMORY_DIR / sub / name
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        keep, dropped = [], 0
        for line in lines:
            if not line.strip():
                continue
            try:
                ts = float(_json.loads(line).get("ts", 0) or 0)
            except Exception:
                keep.append(line)          # unparseable: keep, never repair
                continue
            if ts and ts < cutoff:
                dropped += 1
            else:
                keep.append(line)
        if not dropped:
            continue
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            from . import atomicio
            atomicio.publish(tmp, path,
                             "\n".join(keep) + ("\n" if keep else ""))
            removed += dropped
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    forensics = config.MEMORY_DIR / "watchdog" / "forensics"
    if forensics.exists():
        for path in forensics.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


# --- sovereign memory contract: migrate / export / import / delete -------
#
# File memory is a data-sovereignty contract: you can version it, carry it out
# whole, grep it, and delete exactly what you name. The unit of export is a
# self-describing tar.gz whose `manifest.json` carries the schema_version, so a
# future Olympus refuses an archive it doesn't understand instead of guessing.

def _memory_roots(user: str | None = None, all_users: bool = False) -> list[Path]:
    from . import note_archive
    return note_archive.roots(user, all_users)


def _collect_files(roots: list[Path]) -> list[Path]:
    from . import note_archive
    return note_archive.collect(roots)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _created_from_name(name: str) -> str:
    m = re.match(r"(\d{8}-\d{6})", name)
    return m.group(1) if m else time.strftime("%Y%m%d-%H%M%S")


def migrate_notes() -> dict:
    from . import note_archive
    return note_archive.migrate()


def _maybe_decrypt(raw: bytes) -> bytes:
    """A plain export is gzip (magic 1f 8b). Anything else we treat as a
    vault-encrypted export and decrypt with the same Fernet key vault.py uses
    (no new crypto dependency)."""
    if raw[:2] == b"\x1f\x8b":
        return raw
    from . import vault
    try:
        return vault._fernet().decrypt(raw)
    except Exception as err:   # InvalidToken, VaultError, ...
        raise ValueError(
            "this export is encrypted and could not be decrypted — set "
            "OLYMPUS_SECRET_KEY to the key it was exported with.") from err


def export_memory(out_path, *, user: str | None = None, all_users: bool = False,
                  encrypt: bool = False) -> dict:
    from . import note_archive
    return note_archive.export(out_path, user=user, all_users=all_users, encrypt=encrypt)


def _gate_import(manifest: dict, origin: str) -> None:
    """Run an export manifest through the persistent-artifact ingestion gate
    (W2-C5) BEFORE a single restored byte is written.

    A restored archive is durable belief: kind `memory_import`, persistent,
    reject-never-repair. The manifest is the artifact — it is what names every
    file, its size and its hash — so gating it gates the restore. Provenance is
    required for this kind; a local archive carries no publisher attestation,
    so the record binds the artifact-as-read to the archive path (which the
    gate's source-safety rules then check), while the per-file sha256s inside
    `entries` remain what the restore loop verifies file by file.

    Raises ValueError — import_memory's existing refusal vocabulary. Because it
    runs before the restore loop, a refusal leaves MEMORY_DIR untouched.

    Inert while `OLYMPUS_INGESTGATE` is off: the gate is not consulted at all.
    """
    from . import ingestgate
    if not ingestgate.enabled():
        return
    version = manifest.get("schema_version")
    artifact = {
        "version": version if isinstance(version, str) else str(version),
        "entries": manifest.get("files"),
        "origin": str(origin),
    }
    scope = manifest.get("scope")
    if isinstance(scope, dict) and isinstance(scope.get("user"), str):
        artifact["user"] = scope["user"]
    try:
        prov_sha = ingestgate.payload_sha256(artifact)
    except Exception:            # unserializable manifest — check() refuses it
        prov_sha = ""
    provenance = {"source": str(origin), "sha256": prov_sha}
    try:
        ingestgate.check("memory_import", artifact, source=str(origin),
                         provenance=provenance)
    except ingestgate.IngestRefused as refusal:
        ref = (f"; evidence {refusal.evidence_ref}"
               if refusal.evidence_ref else "")
        raise ValueError(
            "the ingestion gate refused this memory export "
            f"({'; '.join(refusal.reasons)}{ref}). "
            "Nothing was restored.") from refusal


def import_memory(archive, *, overwrite: bool = True, user=None, all_users=None) -> dict:
    from . import note_archive
    return note_archive.restore(archive, overwrite=overwrite, user=user, all_users=all_users)


def delete_memory(user: str | None = None, *, category: str | None = None,
                  note_id: str | None = None, preview=None) -> list[str]:
    """Delete selected canonical file notes; retained recovery bytes are not erasure."""
    from . import note_archive
    return note_archive.delete(user, category=category, note_id=note_id, preview=preview)


# --- Heartbeat state -----------------------------------------------------

_STATE_LOCK = threading.Lock()


def load_state() -> dict:
    path = config.MEMORY_DIR / "heartbeat_state.json"
    with _STATE_LOCK:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
    return {}


def save_state(state: dict) -> None:
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    path = config.MEMORY_DIR / "heartbeat_state.json"
    with _STATE_LOCK:
        # Atomic publish: readers in OTHER processes (admin panel, digest,
        # hibernate) map a torn file to {} — "never ran" — and a crash
        # mid-write would reset every cadence timestamp (ADR 0005). Only the
        # heartbeat writes, so the thread lock suffices for exclusion.
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        from . import atomicio
        atomicio.publish(tmp, path, json.dumps(state, indent=2))


def bump_conversation_count() -> int:
    """Atomically increment and return the cross-session conversation counter.

    Kept in its own file (not heartbeat_state.json) so concurrent web/Telegram
    increments can't clobber the heartbeat's last-run timestamps, and vice
    versa. Guarded by the CROSS-process lock (ADR 0005): a threading.Lock
    only serialized one process's increments while the heartbeat and web
    processes both bump this counter."""
    from . import proclock
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    path = config.MEMORY_DIR / "conversation_count.txt"
    try:
        return _bump_conversation_locked(proclock, path)
    except TimeoutError:
        return 0                       # wedged peer: skip the audit trigger


def _bump_conversation_locked(proclock, path) -> int:
    with proclock.lock("conversation-count"):
        try:
            n = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            n = 0
        n += 1
        path.write_text(str(n), encoding="utf-8")
        return n


def reset_conversation_count() -> None:
    # Same cross-process lock as bump_conversation_count: a reset under only
    # the thread lock could interleave with the other process's bump.
    from . import proclock
    path = config.MEMORY_DIR / "conversation_count.txt"
    with proclock.lock("conversation-count"):
        path.write_text("0", encoding="utf-8")
