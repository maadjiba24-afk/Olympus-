"""Cross-session search — find anything Olympus has discussed before.

Conversations persist to `memory/conversations/<id>.json` (per CLI session,
Telegram chat, web cookie, …), but nothing could search *across* them. This
adds the Hermes-style capability: a full-text index over every persisted turn,
so "what did we decide about pricing last month?" finds the exact exchange.

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

from . import config


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
    fts = _fts5_available(conn)
    if fts:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS turns "
                     "USING fts5(conversation, role, content, turn UNINDEXED)")
    else:
        conn.execute("CREATE TABLE IF NOT EXISTS turns "
                     "(conversation TEXT, role TEXT, content TEXT, turn INT)")
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


def _conversations() -> list[tuple[str, list]]:
    d = config.MEMORY_DIR / "conversations"
    out = []
    if not d.exists():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            out.append((path.stem, json.loads(path.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def index_conversation(conversation_id: str, history: list[dict]) -> int:
    """(Re)index one conversation's turns. Returns the number of turns indexed."""
    conn, _ = _connect()
    try:
        conn.execute("DELETE FROM turns WHERE conversation = ?",
                     (conversation_id,))
        rows = [(conversation_id, str(m.get("role", "")),
                 str(m.get("content", "")), i)
                for i, m in enumerate(history) if str(m.get("content", "")).strip()]
        conn.executemany("INSERT INTO turns(conversation, role, content, turn) "
                         "VALUES (?,?,?,?)", rows)
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
    for cid, history in _conversations():
        total += index_conversation(cid, history)
    return total


def search(query: str, limit: int = 20,
           conversation: str | None = None) -> list[Hit]:
    """Search indexed turns. Auto-reindexes if the index is empty so a first
    call just works."""
    query = (query or "").strip()
    if not query:
        return []
    conn, fts = _connect()
    try:
        if conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0:
            conn.close()
            reindex()
            conn, fts = _connect()
        params: list = []
        if fts:
            sql = ("SELECT conversation, role, content, turn FROM turns "
                   "WHERE turns MATCH ?")
            params.append(query)
            if conversation:
                sql += " AND conversation = ?"
                params.append(conversation)
            sql += " ORDER BY rank LIMIT ?"
        else:
            sql = ("SELECT conversation, role, content, turn FROM turns "
                   "WHERE content LIKE ?")
            params.append(f"%{query}%")
            if conversation:
                sql += " AND conversation = ?"
                params.append(conversation)
            sql += " LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # malformed FTS query (e.g. stray quotes) → fall back to LIKE
            rows = conn.execute(
                "SELECT conversation, role, content, turn FROM turns "
                "WHERE content LIKE ? LIMIT ?",
                (f"%{query}%", limit)).fetchall()
        return [Hit(*r) for r in rows]
    finally:
        conn.close()
