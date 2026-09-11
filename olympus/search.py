"""Cross-session search — find anything Olympus has discussed before.

Conversations persist to `memory/conversations/<id>.json` (per CLI session,
Telegram chat, web cookie, …), but nothing could search *across* them. This
adds the Hermes-style capability: a full-text index over persisted turns, so
"what did we decide about pricing last month?" finds the exact exchange. Every
row is bound to the trusted memory principal that saved it; searches are always
restricted to that principal.

Uses SQLite's built-in **FTS5** when the runtime has it (fast, ranked by
relevance), and transparently falls back to a substring scan when it doesn't —
so it works everywhere, no extra dependency. The index is derived data
(`memory/search_index-v2.db`; legacy index preserved unclaimed); it can be rebuilt from the conversation files at any
time with `reindex()`.
"""

from __future__ import annotations

import json
import sqlite3
import stat
from functools import wraps
from pathlib import Path
from dataclasses import dataclass

from . import config, memory, owner_evidence as evidence


_TURN_COLUMNS = ("owner", "conversation", "role", "content", "turn")


def _db_path():
    return str(config.MEMORY_DIR / "search_index-v2.db")


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _connect() -> tuple[sqlite3.Connection, bool]:
    path = Path(_db_path())
    evidence._parents(path, "conversation search")
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024 * 1024:
            raise evidence.OwnerEvidenceStateError("conversation search", "invalid database file or size")
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    conn = sqlite3.connect(str(path))
    try:
        existing = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'turns'").fetchone()
        if existing:
            columns = tuple(row[1] for row in conn.execute("PRAGMA table_info(turns)"))
            if columns != _TURN_COLUMNS:
                raise evidence.OwnerEvidenceStateError("conversation search", "invalid database schema")
            fts = "using fts5" in str(existing[0] or "").lower()
        elif existed:
            raise evidence.OwnerEvidenceStateError("conversation search", "incomplete database schema")
        else:
            fts = _fts5_available(conn)
            if fts:
                conn.execute("CREATE VIRTUAL TABLE turns USING fts5(owner UNINDEXED, conversation, role, content, turn UNINDEXED)")
            else:
                conn.execute("CREATE TABLE turns (owner TEXT NOT NULL, conversation TEXT, role TEXT, content TEXT, turn INT)")
                conn.execute("CREATE INDEX turns_owner_idx ON turns(owner)")
            conn.commit()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # The existing filesystem fallback remains supported.
        return conn, fts
    except Exception:
        conn.close()
        raise


def _errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            with evidence.guard("search-v2", "search-database"):
                return fn(*args, **kwargs)
        except (sqlite3.Error, OSError) as err:
            raise evidence.OwnerEvidenceStateError("conversation search", "database or snapshot unavailable") from err
    return wrapped


def _rows(principal, conversation_id, history):
    try:
        evidence.text(principal, 8192)
        evidence.text(conversation_id, 8192)
        evidence.records(history, 10000)
        rows = []
        total = 0
        for i, message in enumerate(history):
            if not isinstance(message, dict):
                raise ValueError("invalid turn")
            role, content = message.get("role"), message.get("content")
            evidence.text(role, 128)
            evidence.text(content, 1000000, empty=True)
            total += len(content.encode("utf-8"))
            if total > 16 * 1024 * 1024:
                raise ValueError("history too large")
            if content.strip():
                rows.append((principal, conversation_id, role, content, i))
        return rows
    except (ValueError, TypeError, KeyError) as err:
        raise evidence.OwnerEvidenceStateError("conversation search", "invalid turn evidence") from err


@dataclass(frozen=True)
class Hit:
    conversation: str
    role: str
    content: str
    turn: int

    def render(self) -> str:
        snippet = self.content.strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        return f"[{self.conversation}#{self.turn} {self.role}] {snippet}"


def _owner(value: str | None = None) -> str:
    """The trusted search namespace; never sourced from a tool argument."""
    return memory.canonical_owner(memory.current_owner() if value is None else value)


def _conversations(owner=None) -> list[tuple[str, list, str]]:
    directory = config.MEMORY_DIR / "conversations"
    evidence._parents(directory, "conversation search")
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise evidence.OwnerEvidenceStateError("conversation search", "invalid snapshot directory")
    out = []
    for i, path in enumerate(sorted(directory.glob("*.json"))):
        if i >= 10000:
            raise evidence.OwnerEvidenceStateError("conversation search", "snapshot-count bound exceeded")
        binding = memory.conversation_binding(path.stem)
        if binding is None or (owner is not None and binding["owner"] != owner):
            continue
        raw = evidence.read_bytes(path, 16 * 1024 * 1024, "conversation search")
        if raw is None:
            raise evidence.OwnerEvidenceStateError("conversation search", "snapshot disappeared")
        history = evidence.decode(raw, "conversation search", 16 * 1024 * 1024)
        _rows(binding["owner"], path.stem, history)
        out.append((path.stem, history, binding["owner"]))
    return out


@_errors
def index_conversation(conversation_id: str, history: list[dict], *,
                       owner: str | None = None) -> int:
    """(Re)index one conversation's turns. Returns the number of turns indexed."""
    principal = _owner(owner)
    rows = _rows(principal, conversation_id, history)
    conn, _ = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM turns WHERE owner = ? AND conversation = ?", (principal, conversation_id))
            conn.executemany("INSERT INTO turns(owner, conversation, role, content, turn) VALUES (?,?,?,?,?)", rows)
        return len(rows)
    finally:
        conn.close()


@_errors
def reindex(*, owner=None) -> int:
    """Validate every included snapshot before one transactional replacement."""
    principal = None if owner is None else _owner(owner)
    rows = []
    for cid, history, attributed in _conversations(principal):
        rows.extend(_rows(attributed, cid, history))
        if len(rows) > 100000:
            raise evidence.OwnerEvidenceStateError("conversation search", "rebuild row bound exceeded")
    conn, _ = _connect()
    try:
        with conn:
            if principal is None:
                conn.execute("DELETE FROM turns")
            else:
                conn.execute("DELETE FROM turns WHERE owner = ?", (principal,))
            conn.executemany("INSERT INTO turns(owner, conversation, role, content, turn) VALUES (?,?,?,?,?)", rows)
        return len(rows)
    finally:
        conn.close()


@_errors
def purge_owner(owner: str) -> int:
    conn, _ = _connect()
    try:
        with conn:
            cursor = conn.execute("DELETE FROM turns WHERE owner = ?", (_owner(owner),))
        return cursor.rowcount
    finally:
        conn.close()


@_errors
def owner_turns(owner: str) -> int:
    conn, _ = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM turns WHERE owner = ?", (_owner(owner),)).fetchone()[0]
    finally:
        conn.close()


@_errors
def purge_conversation(conversation_id: str) -> int:
    """Drop every indexed turn for one conversation. Returns rows removed.

    Deletion-path primitive, not hygiene: `maintain()` reaps orphans lazily on
    the heartbeat's schedule, which is far too slow a guarantee for a
    right-to-be-forgotten request — the searchable copy of a user's messages
    would outlive the deletion that reported success. `retention` calls this
    synchronously so the index is purged in the same operation that removes the
    conversation file."""
    conn, _ = _connect()
    try:
        cur = conn.execute("DELETE FROM turns WHERE conversation = ?",
                           (conversation_id,))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


@_errors
def indexed_turns(conversation_id: str) -> int:
    """How many turns remain indexed for one conversation (verification)."""
    conn, _ = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) FROM turns WHERE conversation = ?",
                           (conversation_id,)).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


@_errors
def maintain(retain_days: int | None = None) -> dict:
    """Index hygiene, run by the heartbeat's maintenance sweep (a long-lived
    server rarely restarts, so startup-time pruning would never fire):

      * drop index rows for conversations whose FILE is gone (orphans);
      * if OLYMPUS_SEARCH_RETAIN_DAYS > 0, also drop rows for conversations
        idle longer than that (0 = keep forever — the default, because
        "remember any conversation, anytime" is the whole point);
      * VACUUM when anything was removed, so the file actually shrinks.

    Conversation FILES are never touched — they're user data; only the derived
    index is pruned, and reindex() can always rebuild it."""
    import os as _os
    import time as _time
    if retain_days is None:
        try:
            retain_days = int(_os.environ.get("OLYMPUS_SEARCH_RETAIN_DAYS", "0"))
        except ValueError:
            retain_days = 0
    d = config.MEMORY_DIR / "conversations"
    live: set[str] = set()
    aged: set[str] = set()
    cutoff = _time.time() - retain_days * 86400
    if d.exists():
        for path in d.glob("*.json"):
            live.add(path.stem)
            if retain_days > 0 and path.stat().st_mtime < cutoff:
                aged.add(path.stem)
    conn, _ = _connect()
    try:
        indexed = {r[0] for r in conn.execute(
            "SELECT DISTINCT conversation FROM turns").fetchall()}
        drop = (indexed - live) | (aged & indexed)
        for cid in drop:
            conn.execute("DELETE FROM turns WHERE conversation = ?", (cid,))
        conn.commit()
        if drop:
            conn.execute("VACUUM")
        return {"orphans": len(indexed - live), "aged": len(aged & indexed),
                "vacuumed": bool(drop)}
    finally:
        conn.close()


@_errors
def search(query: str, limit: int = 20,
           conversation: str | None = None, *,
           owner: str | None = None) -> list[Hit]:
    """Search indexed turns. Auto-reindexes if the index is empty so a first
    call just works."""
    query = (query or "").strip()
    if not query:
        return []
    principal = _owner(owner)
    conn, fts = _connect()
    try:
        if conn.execute("SELECT COUNT(*) FROM turns WHERE owner = ?",
                        (principal,)).fetchone()[0] == 0:
            conn.close()
            reindex(owner=principal)
            conn, fts = _connect()
        params: list = []
        if fts:
            sql = ("SELECT conversation, role, content, turn FROM turns "
                   "WHERE turns MATCH ? AND owner = ?")
            params.extend((query, principal))
            if conversation:
                sql += " AND conversation = ?"
                params.append(conversation)
            sql += " ORDER BY rank LIMIT ?"
        else:
            sql = ("SELECT conversation, role, content, turn FROM turns "
                   "WHERE owner = ? AND content LIKE ?")
            params.extend((principal, f"%{query}%"))
            if conversation:
                sql += " AND conversation = ?"
                params.append(conversation)
            sql += " LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # malformed FTS query (e.g. stray quotes) → fall back to LIKE
            fallback = ("SELECT conversation, role, content, turn FROM turns "
                        "WHERE owner = ? AND content LIKE ?")
            fallback_params: list = [principal, f"%{query}%"]
            if conversation:
                fallback += " AND conversation = ?"
                fallback_params.append(conversation)
            fallback += " LIMIT ?"
            fallback_params.append(limit)
            rows = conn.execute(fallback, fallback_params).fetchall()
        return [Hit(*r) for r in rows]
    finally:
        conn.close()
