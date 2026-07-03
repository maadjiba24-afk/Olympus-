"""Storage abstraction — file by default, Postgres when you want it.

Olympus's defining property is that it runs anywhere with zero setup, so the
default backend is the local filesystem (no database required). When you host
for real users and want a real database, set OLYMPUS_DATABASE_URL to a Postgres
URL and the same interface persists there instead — no code changes.

This is a small key-value store keyed by (namespace, key). The encrypted vault
(OAuth tokens) and other product state use it, so "database-ready" is a config
switch, not a rewrite.

Interface: get(ns, key) / put(ns, key, bytes) / delete(ns, key) / keys(ns).
Values are opaque bytes (the vault stores ciphertext here).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import config


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:128] or "_"


class FileStore:
    """Default backend: bytes under MEMORY_DIR/store/<ns>/<key>."""

    def _dir(self, ns: str) -> Path:
        d = config.MEMORY_DIR / "store" / _safe(ns)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def put(self, ns: str, key: str, value: bytes) -> None:
        path = self._dir(ns) / _safe(key)
        path.write_bytes(value)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def get(self, ns: str, key: str) -> bytes | None:
        path = self._dir(ns) / _safe(key)
        return path.read_bytes() if path.exists() else None

    def delete(self, ns: str, key: str) -> None:
        path = self._dir(ns) / _safe(key)
        if path.exists():
            path.unlink()

    def keys(self, ns: str) -> list[str]:
        return sorted(p.name for p in self._dir(ns).glob("*"))


class PostgresStore:
    """Optional backend: a single kv table. Requires `psycopg` and
    OLYMPUS_DATABASE_URL. Verified against a live Postgres before relying on it.
    """

    def __init__(self, dsn: str):
        import psycopg  # imported lazily so it's a soft dependency
        self._psycopg = psycopg
        self._dsn = dsn
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS olympus_kv ("
                "ns TEXT NOT NULL, k TEXT NOT NULL, v BYTEA NOT NULL,"
                "PRIMARY KEY (ns, k))")

    def _conn(self):
        return self._psycopg.connect(self._dsn, autocommit=True)

    def put(self, ns: str, key: str, value: bytes) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO olympus_kv (ns,k,v) VALUES (%s,%s,%s) "
                "ON CONFLICT (ns,k) DO UPDATE SET v=EXCLUDED.v",
                (ns, key, value))

    def get(self, ns: str, key: str) -> bytes | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT v FROM olympus_kv WHERE ns=%s AND k=%s",
                (ns, key)).fetchone()
        return bytes(row[0]) if row else None

    def delete(self, ns: str, key: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM olympus_kv WHERE ns=%s AND k=%s", (ns, key))

    def keys(self, ns: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT k FROM olympus_kv WHERE ns=%s ORDER BY k", (ns,)).fetchall()
        return [r[0] for r in rows]


_backend = None


def backend():
    """The active store. Postgres if OLYMPUS_DATABASE_URL is set, else files."""
    global _backend
    if _backend is None:
        dsn = os.environ.get("OLYMPUS_DATABASE_URL")
        if dsn:
            _backend = PostgresStore(dsn)
        else:
            _backend = FileStore()
    return _backend


def reset() -> None:
    """Forget the cached backend (tests)."""
    global _backend
    _backend = None
