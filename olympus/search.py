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
(`memory/search_index.db`); it can be rebuilt from the conversation files at any
time with `reindex()`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import config, memory


_TURN_COLUMNS = ("owner", "conversation", "role", "content", "turn")


def _db_path():
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return str(config.MEMORY_DIR / "search_index.db")


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _connect() -> tuple[sqlite3.Connection, bool]:
    conn = sqlite3.connect(_db_path())
    # WAL: readers never block the writer (gateways index while chat searches),
    # and a crash mid-write can't corrupt the index. Best-effort — some
    # filesystems (network mounts) refuse WAL; the default journal still works.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    fts = _fts5_available(conn)
    # The pre-owner index is unsafe derived data: it cannot be attributed to a
    # principal after the fact. Drop it instead of guessing. Reindexing below
    # rebuilds only conversations with durable ownership metadata.
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'turns'"
    ).fetchone()
    if existing:
        columns = tuple(row[1] for row in conn.execute(
            "PRAGMA table_info(turns)").fetchall())
        was_fts = "using fts5" in str(existing[0] or "").lower()
        if columns != _TURN_COLUMNS or was_fts != fts:
            conn.execute("DROP TABLE turns")
    if fts:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS turns "
                     "USING fts5(owner UNINDEXED, conversation, role, "
                     "content, turn UNINDEXED)")
    else:
        conn.execute("CREATE TABLE IF NOT EXISTS turns "
                     "(owner TEXT NOT NULL, conversation TEXT, role TEXT, "
                     "content TEXT, turn INT)")
        conn.execute("CREATE INDEX IF NOT EXISTS turns_owner_idx "
                     "ON turns(owner)")
    conn.commit()
    return conn, fts


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
    return memory.safe_id(memory.current_user() if value is None else value)


def _conversations() -> list[tuple[str, list, str]]:
    d = config.MEMORY_DIR / "conversations"
    out = []
    if not d.exists():
        return out
    # Ownerless snapshots predate this boundary. Never guess who owns them:
    # they remain unsearchable until explicitly migrated or resaved.
    for path in sorted(d.glob("*.json")):
        try:
            owner = memory.conversation_owner(path.stem)
            if owner is None:
                continue
            out.append((path.stem,
                        json.loads(path.read_text(encoding="utf-8")), owner))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def index_conversation(conversation_id: str, history: list[dict], *,
                       owner: str | None = None) -> int:
    """(Re)index one conversation's turns. Returns the number of turns indexed."""
    principal = _owner(owner)
    conn, _ = _connect()
    try:
        conn.execute("DELETE FROM turns WHERE owner = ? AND conversation = ?",
                     (principal, conversation_id))
        rows = [(principal, conversation_id, str(m.get("role", "")),
                 str(m.get("content", "")), i)
                for i, m in enumerate(history) if str(m.get("content", "")).strip()]
        conn.executemany("INSERT INTO turns(owner, conversation, role, "
                         "content, turn) VALUES (?,?,?,?,?)", rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def reindex() -> int:
    """Rebuild the whole index from the conversation files. Returns turn count."""
    conn, _ = _connect()
    conn.execute("DELETE FROM turns")
    conn.commit()
    conn.close()
    total = 0
    for cid, history, owner in _conversations():
        total += index_conversation(cid, history, owner=owner)
    return total


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


def indexed_turns(conversation_id: str) -> int:
    """How many turns remain indexed for one conversation (verification)."""
    conn, _ = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) FROM turns WHERE conversation = ?",
                           (conversation_id,)).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


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
            reindex()
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
