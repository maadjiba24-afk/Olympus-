"""Persistent file-based memory — the substrate of self-improvement.

Layout:
    memory/
      lessons/ corrections/ feedback/        shared (system-generated)
      users/<user-id>/lessons|corrections|feedback/   per-user namespaces
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
              "prompt_backups", "evals")
USER_SCOPED = {"lessons", "corrections", "feedback"}

# On-disk format versions (Olympus's data-sovereignty contract — see
# docs/MEMORY_FORMAT.md). NOTE_SCHEMA_VERSION stamps each markdown note's
# frontmatter; ARCHIVE_SCHEMA_VERSION stamps an export's manifest. Import
# refuses any archive version it doesn't understand rather than guessing.
NOTE_SCHEMA_VERSION = 1
ARCHIVE_SCHEMA_VERSION = 1
SUPPORTED_ARCHIVE_VERSIONS = frozenset({1})

_USER: contextvars.ContextVar[str] = contextvars.ContextVar(
    "olympus_user", default="shared"
)


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value)).strip("-")[:64] or "shared"


def set_user(user: str) -> None:
    """Set the memory namespace for the current thread/conversation."""
    _USER.set(safe_id(user))


def current_user() -> str:
    return _USER.get()


def _dir(category: str, user: str = "shared") -> Path:
    if category not in CATEGORIES:
        raise ValueError(f"unknown memory category: {category}")
    if category in USER_SCOPED and user != "shared":
        d = config.MEMORY_DIR / "users" / user / category
    else:
        d = config.MEMORY_DIR / category
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    """Save into the current user's namespace (or shared for system work).

    Sanitization is enforced HERE, at the sink: every title and body written to
    durable memory is defanged (`security.sanitize_for_memory`) regardless of
    whether the caller remembered to. This closes the bypass where a new writer
    path forgets to sanitize — there is only one door into memory, and it is
    guarded. The pass is idempotent, so callers that already sanitize are safe.
    """
    from . import security
    title = security.sanitize_for_memory(title)
    content = security.sanitize_for_memory(content)
    d = _dir(category, current_user())
    # Unique, collision-proof filename (ADR 0005): the old second-granularity
    # name let two concurrent writers of the same title silently overwrite
    # each other. pid + O_EXCL create (atomic across processes) guarantees
    # every save lands in its own file; the timestamp prefix is unchanged so
    # date-parsing readers (first 14 digits) and name-sorted listings keep
    # working.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{os.getpid()}-{_slug(title)}"
    body = render_note(title, content)
    # Write the body FULLY to a private tmp file, then publish it under the
    # unique name with os.link — atomic and exclusive (EEXIST on collision),
    # so a reader can never glob a half-written note and a crash mid-write
    # leaves only an invisible tmp, never a corrupt note.
    tmp = d / f".tmp-{os.getpid()}-{threading.get_ident()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        for n in range(1000):
            path = d / (f"{base}.md" if n == 0 else f"{base}-{n}.md")
            try:
                os.link(tmp, path)
            except FileExistsError:
                continue
            _mirror_to_vault(category, path)
            return path
        raise OSError(f"could not allocate a unique note file for '{base}'")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# Categories worth reading in a knowledge GUI (Obsidian is just a folder).
_VAULT_CATEGORIES = frozenset({"lessons", "reports", "corrections"})


def _mirror_to_vault(category: str, path: Path) -> None:
    """Write-through mirror into OLYMPUS_VAULT_DIR — a plain-markdown knowledge
    base the user curates in any editor/GUI. A MIRROR, not a second source of
    truth: the gated store stays canonical; deleting/editing vault files never
    affects Olympus. Best-effort — a broken vault path must never break a save."""
    vault = os.environ.get("OLYMPUS_VAULT_DIR", "").strip()
    if not vault or category not in _VAULT_CATEGORIES:
        return
    try:
        out = Path(vault).expanduser() / category
        out.mkdir(parents=True, exist_ok=True)
        (out / path.name).write_text(path.read_text(encoding="utf-8"),
                                     encoding="utf-8")
    except OSError:
        pass


def _search_dirs() -> list[Path]:
    dirs = [_dir(c) for c in CATEGORIES]
    if current_user() != "shared":
        dirs += [_dir(c, current_user()) for c in USER_SCOPED]
    return dirs


def search(query: str, limit: int = 5) -> str:
    """Ranked keyword search across shared memory + the current user's own."""
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    scored: list[tuple[float, Path, str]] = []
    for d in _search_dirs():
        for path in d.glob("*.md"):
            _, body = parse_note(path.read_text(encoding="utf-8", errors="replace"))
            lower = body.lower()
            # diminishing returns per term + title hits weighted higher
            title = lower.splitlines()[0] if lower else ""
            score = 0.0
            for t in terms:
                hits = lower.count(t)
                if hits:
                    score += 1 + min(hits, 5) * 0.2 + (2 if t in title else 0)
            if score:
                scored.append((score, path, body))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return "No memory entries match that query."
    out = []
    for _, path, body in scored[:limit]:
        out.append(f"--- {path.parent.name}/{path.name} ---\n{body[:1500]}")
    return "\n\n".join(out)


def recent(category: str, n: int = 5) -> str:
    files = list(_dir(category).glob("*.md"))
    if category in USER_SCOPED and current_user() != "shared":
        files += list(_dir(category, current_user()).glob("*.md"))
    files = sorted(files, key=lambda p: p.name, reverse=True)[:n]
    if not files:
        return f"(no {category} recorded yet)"
    return "\n\n".join(
        f"--- {p.name} ---\n"
        f"{parse_note(p.read_text(encoding='utf-8', errors='replace'))[1][:1500]}"
        for p in files
    )


def recent_titles(category: str, n: int = 5) -> list[str]:
    """The titles of the most recent notes in a category (newest first) — a
    glanceable list without the bodies."""
    files = list(_dir(category).glob("*.md"))
    if category in USER_SCOPED and current_user() != "shared":
        files += list(_dir(category, current_user()).glob("*.md"))
    files = sorted(files, key=lambda p: p.name, reverse=True)[:n]
    out: list[str] = []
    for p in files:
        body = parse_note(p.read_text(encoding="utf-8", errors="replace"))[1]
        first = body.lstrip().splitlines()[0] if body.strip() else ""
        # Strip only the single leading "# " heading marker — NOT a character
        # class (lstrip("# ") would mangle a title like "#1 priority").
        title = first[2:] if first.startswith("# ") else first
        out.append(title.strip() or p.stem)
    return out


def prune(category: str, keep: int = 200) -> str:
    """Keep only the newest `keep` files in a category (current user's
    namespace). Older entries are deleted — Metis distills the durable ones
    into skills before they're pruned, so signal survives, noise doesn't."""
    files = sorted(_dir(category, current_user()).glob("*.md"),
                   key=lambda p: p.name, reverse=True)
    removed = 0
    for path in files[keep:]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return f"Pruned {removed} old {category} entries (kept {min(len(files), keep)})."


def category_count(category: str) -> int:
    n = len(list(_dir(category).glob("*.md")))
    if category in USER_SCOPED and current_user() != "shared":
        n += len(list(_dir(category, current_user()).glob("*.md")))
    return n


# --- persisted conversations ---------------------------------------------

def _conversation_path(conversation_id: str) -> Path:
    d = config.MEMORY_DIR / "conversations"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe_id(conversation_id)}.json"


def _conversation_owner_path(conversation_id: str) -> Path:
    return _conversation_path(conversation_id).with_suffix(".owner")


def conversation_owner(conversation_id: str) -> str | None:
    """Return a snapshot's durable owner, or None for legacy/invalid metadata."""
    path = _conversation_owner_path(conversation_id)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw if raw and raw == safe_id(raw) else None


def _bind_conversation_owner(conversation_id: str, owner: str) -> str:
    """Create an immutable owner binding before a snapshot can be indexed."""
    principal = safe_id(owner)
    path = _conversation_owner_path(conversation_id)
    # This metadata is write-once. The lock makes the check/create pair safe
    # across both threads and processes; exclusive creation prevents a second
    # writer from replacing an established principal. A torn/invalid file is
    # refused rather than "repaired" into an attacker-chosen owner.
    from . import proclock
    with proclock.lock(f"conversation-owner-{safe_id(conversation_id)}"):
        existing = conversation_owner(conversation_id)
        if existing is not None:
            if existing != principal:
                raise PermissionError(
                    "conversation belongs to a different memory principal")
            return principal
        if path.exists():
            raise PermissionError("conversation owner metadata is invalid")
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(principal + "\n")
            handle.flush()
            os.fsync(handle.fileno())
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
        conversation_id, current_user() if owner is None else owner)
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
    except Exception:
        pass


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
    """The on-disk roots that make up 'memory' for an export/delete scope.

    - all_users: every shared category, every per-user namespace, conversations.
    - user='shared' (default): the shared (system) categories only.
    - user=<id>: just that person's namespace (`users/<id>/`)."""
    base = config.MEMORY_DIR
    if all_users:
        roots = [base / c for c in CATEGORIES]
        roots += [base / "users", base / "conversations"]
        return [r for r in roots if r.exists()]
    uid = safe_id(user or "shared")
    if uid == "shared":
        return [base / c for c in CATEGORIES if (base / c).exists()]
    ud = base / "users" / uid
    return [ud] if ud.exists() else []


def _collect_files(roots: list[Path]) -> list[Path]:
    files = {p for r in roots for p in r.rglob("*") if p.is_file()}
    return sorted(files)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _created_from_name(name: str) -> str:
    m = re.match(r"(\d{8}-\d{6})", name)
    return m.group(1) if m else time.strftime("%Y%m%d-%H%M%S")


def migrate_notes() -> dict:
    """Upgrade frontmatter-less (v0) notes to the current note schema in place,
    preserving the body verbatim. Returns {scanned, migrated}. Idempotent —
    notes already at the current version are left untouched."""
    scanned = migrated = 0
    for root in _memory_roots(all_users=True):
        for path in root.rglob("*.md"):
            scanned += 1
            text = path.read_text(encoding="utf-8", errors="replace")
            if note_schema_version(text) >= NOTE_SCHEMA_VERSION:
                continue
            created = _created_from_name(path.name)
            body = text if text.endswith("\n") else text + "\n"
            path.write_text(_frontmatter(NOTE_SCHEMA_VERSION, created) + body,
                            encoding="utf-8")
            migrated += 1
    return {"scanned": scanned, "migrated": migrated}


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
    """Write a self-describing tar.gz of the scoped memory to `out_path`.

    The archive holds `manifest.json` (schema_version + every file's path,
    sha256 and size) and `data/<relpath>` members byte-for-byte. Returns the
    manifest. With encrypt=True the whole archive is Fernet-encrypted via
    vault.py."""
    files = _collect_files(_memory_roots(user, all_users))
    manifest = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scope": {"all": True} if all_users
                 else {"user": safe_id(user or "shared")},
        "files": [],
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in files:
            rel = p.relative_to(config.MEMORY_DIR).as_posix()
            data = p.read_bytes()
            manifest["files"].append(
                {"path": rel, "sha256": _sha256(data), "bytes": len(data)})
            info = tarfile.TarInfo(name=f"data/{rel}")
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
        mdata = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        minfo = tarfile.TarInfo(name="manifest.json")
        minfo.size = len(mdata)
        minfo.mtime = 0
        tar.addfile(minfo, io.BytesIO(mdata))
    raw = buf.getvalue()
    if encrypt:
        from . import vault
        raw = vault._fernet().encrypt(raw)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    return manifest


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


def import_memory(archive, *, overwrite: bool = True) -> dict:
    """Restore a memory export into MEMORY_DIR. Validates the manifest's
    schema_version and REFUSES an unknown one (raises ValueError) rather than
    importing data it can't vouch for. Returns {schema_version, restored,
    count, verified}."""
    raw = _maybe_decrypt(Path(archive).read_bytes())
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        try:
            mf = tar.extractfile("manifest.json")
        except KeyError:
            mf = None
        if mf is None:
            raise ValueError(
                "not an Olympus memory export — no manifest.json inside.")
        manifest = json.loads(mf.read().decode("utf-8"))
        version = manifest.get("schema_version")
        if version not in SUPPORTED_ARCHIVE_VERSIONS:
            raise ValueError(
                f"refusing to import unknown schema_version {version!r} "
                f"(this build supports {sorted(SUPPORTED_ARCHIVE_VERSIONS)}). "
                "Upgrade Olympus, or migrate the archive first.")
        _gate_import(manifest, str(archive))     # W2-C5, before any restore
        restored, verified = [], 0
        for entry in manifest.get("files", []):
            rel = entry["path"]
            member = tar.extractfile(f"data/{rel}")
            if member is None:
                continue
            data = member.read()
            if entry.get("sha256") and _sha256(data) == entry["sha256"]:
                verified += 1
            dest = config.MEMORY_DIR / rel
            # Importing an archive is a trust boundary (the data-sovereignty
            # contract supports archives produced elsewhere), and entry["path"]
            # is attacker-controlled — reject any path that escapes MEMORY_DIR so
            # a crafted `../../` or absolute entry can't overwrite files outside.
            try:
                dest_r, mem_r = dest.resolve(), config.MEMORY_DIR.resolve()
            except (OSError, RuntimeError):
                continue
            if dest_r != mem_r and mem_r not in dest_r.parents:
                continue
            if dest.exists() and not overwrite:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            restored.append(rel)
    return {"schema_version": version, "restored": sorted(restored),
            "count": len(restored), "verified": verified}


def delete_memory(user: str | None = None, *, category: str | None = None,
                  note_id: str | None = None) -> list[str]:
    """Hard-delete scoped memory from disk and return exactly the relative
    paths removed. Scope narrows from the user's whole namespace, to one
    category, to a single note (by filename or stem). Files are unlinked, not
    tombstoned — when this returns, they are gone."""
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"unknown memory category: {category}")
    uid = safe_id(user or "shared")
    if category is not None:
        roots = [_dir(category, uid)]
    else:
        roots = _memory_roots(uid)
    removed: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if note_id is not None and note_id not in (p.name, p.stem):
                continue
            rel = p.relative_to(config.MEMORY_DIR).as_posix()
            try:
                p.unlink()
                removed.append(rel)
            except OSError:
                pass
    return sorted(removed)


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

