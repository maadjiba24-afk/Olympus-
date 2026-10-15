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
import contextlib
import hashlib
import re
from pathlib import Path

from . import atomicio, config


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:128] or "_"


class FileStore:
    """Default backend: bytes under MEMORY_DIR/store/<ns>/<key>.

    Concurrency contract (ADR 0005): a blind `put` is DEFINED as
    replace-whole-value — last writer wins, which is correct KV semantics and
    is deliberately not versioned. Callers doing read-modify-write on a key
    that more than one process touches must hold `proclock.lock(<name>)`
    around the read+put cycle; the lock is the serialization point, not the
    store."""

    def _dir(self, ns: str) -> Path:
        d = config.MEMORY_DIR / "store" / _safe(ns)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def put(self, ns: str, key: str, value: bytes) -> None:
        # Atomic publish: a reader in the other process must see the old or
        # the new value, never a truncated blob — consumers map a torn read
        # to "empty" and would silently rebuild from defaults (ADR 0005).
        # Durable too (W1-1): usermem, relgraph, docrag and routing_outcomes
        # all ride this, and os.replace alone survives a peer process but not
        # a power cut — see olympus/atomicio.py.
        path = self._dir(ns) / _safe(key)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        atomicio.publish(tmp, path, value, chmod=0o600)

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

    @contextlib.contextmanager
    def vault_transaction(self, *, timeout=60.0):
        """Serialize all vault/recovery rows on one database connection.

        Vault migration spans multiple keys. The namespace-level transaction
        includes absent rows and recovery copies; it does not move any other
        filesystem state into PostgreSQL or upgrade other KV callers.
        """
        import math
        import time
        from .vault_storage import (MAX_BLOB, MAX_KEYS, MAX_KEY_BYTES,
                                    _address, _cap, _Lease, StorageError)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise StorageError('invalid vault transaction timeout')
        lock_id = int.from_bytes(hashlib.sha256(
            b'olympus-vault-namespace-transaction-v1').digest()[:8], 'big', signed=True)
        deadline = time.monotonic() + timeout
        with self._psycopg.connect(self._dsn, autocommit=True,
                                  connect_timeout=max(1, math.ceil(timeout))) as conn:
            with conn.transaction():
                conn.execute("SELECT set_config('statement_timeout', %s, true)",
                             (str(max(1, math.ceil(timeout * 1000))),))
                while not conn.execute('SELECT pg_try_advisory_xact_lock(%s)',
                                       (lock_id,)).fetchone()[0]:
                    if time.monotonic() >= deadline:
                        raise StorageError('vault transaction acquisition timed out')
                    time.sleep(min(0.02, max(0, deadline - time.monotonic())))

                class VaultTransaction:
                    def __init__(self):
                        self._lease = _Lease()
                        self._lease.active = True

                    def _assert_active(self):
                        self._lease.check()

                    def get(self, namespace, key, cap=MAX_BLOB):
                        self._assert_active()
                        _address(namespace, key)
                        _cap(cap)
                        row = conn.execute(
                            'SELECT octet_length(v), CASE WHEN octet_length(v)<=%s '
                            'THEN v ELSE NULL END FROM olympus_kv WHERE ns=%s AND k=%s',
                            (cap, namespace, key)).fetchone()
                        if row is None:
                            return None
                        if (len(row) != 2 or type(row[0]) is not int
                                or not 0 <= row[0] <= cap
                                or type(row[1]) not in (bytes, bytearray, memoryview)):
                            raise StorageError('vault ciphertext exceeds its read bound')
                        size = row[1].nbytes if type(row[1]) is memoryview else len(row[1])
                        if size != row[0]:
                            raise StorageError('vault ciphertext length does not match its metadata')
                        return bytes(row[1])

                    def put(self, namespace, key, data):
                        self._assert_active()
                        _address(namespace, key)
                        if type(data) is not bytes or len(data) > MAX_BLOB:
                            raise StorageError('invalid vault ciphertext size')
                        conn.execute(
                            'INSERT INTO olympus_kv (ns,k,v) VALUES (%s,%s,%s) '
                            'ON CONFLICT (ns,k) DO UPDATE SET v=EXCLUDED.v',
                            (namespace, key, data))
                        if self.get(namespace, key, len(data)) != data:
                            raise StorageError('vault publication verification failed')

                    def write_new(self, namespace, key, data):
                        self._assert_active()
                        _address(namespace, key)
                        if type(data) is not bytes or len(data) > MAX_BLOB:
                            raise StorageError('invalid vault recovery size')
                        conn.execute('INSERT INTO olympus_kv (ns,k,v) VALUES (%s,%s,%s)',
                                     (namespace, key, data))
                        if self.get(namespace, key, len(data)) != data:
                            raise StorageError('vault recovery verification failed')

                    def delete(self, namespace, key):
                        self._assert_active()
                        _address(namespace, key)
                        conn.execute('DELETE FROM olympus_kv WHERE ns=%s AND k=%s',
                                     (namespace, key))

                    def keys(self, namespace, cap=MAX_KEYS):
                        self._assert_active()
                        _address(namespace, 'enumeration')
                        _cap(cap, MAX_KEYS)
                        rows = conn.execute(
                            'SELECT octet_length(k), CASE WHEN octet_length(k)<=%s '
                            'THEN k ELSE NULL END FROM olympus_kv WHERE ns=%s '
                            'ORDER BY k LIMIT %s',
                            (MAX_KEY_BYTES, namespace, cap + 1)).fetchall()
                        if len(rows) > cap:
                            raise StorageError('vault namespace exceeds enumeration bound')
                        keys = []
                        for row in rows:
                            if (len(row) != 2 or type(row[0]) is not int
                                    or not 1 <= row[0] <= MAX_KEY_BYTES):
                                raise StorageError('vault key exceeds its transfer bound')
                            _address(namespace, row[1])
                            if len(row[1]) != row[0]:  # canonical keys are ASCII
                                raise StorageError('vault key length does not match its metadata')
                            keys.append(row[1])
                        if len(set(keys)) != len(keys):
                            raise StorageError('duplicate vault key in transaction result')
                        return keys

                tx = VaultTransaction()
                try:
                    yield tx
                finally:
                    tx._lease.active = False

    @contextlib.contextmanager
    def owner_transaction(self, ns: str, key: str):
        """Serialize a bounded owner's snapshot RMW on the database itself.

        The advisory lock also covers absent rows. Every read and publication
        uses this connection/transaction; a failed operation rolls back. Other
        legacy KV callers still require the M12 caller-by-caller conversion.
        """
        lock_id = int.from_bytes(hashlib.sha256(
            (ns + "\0" + key).encode()).digest()[:8], "big", signed=True)
        with self._conn() as conn:
            with conn.transaction():
                conn.execute("SET LOCAL lock_timeout = '60s'")
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))

                class OwnerTransaction:
                    def get(self, namespace, owner_key):
                        if (namespace, owner_key) != (ns, key):
                            raise ValueError("transaction owner mismatch")
                        row = conn.execute(
                            "SELECT v FROM olympus_kv WHERE ns=%s AND k=%s",
                            (ns, key)).fetchone()
                        return bytes(row[0]) if row else None

                    def put(self, namespace, owner_key, value):
                        if (namespace, owner_key) != (ns, key):
                            raise ValueError("transaction owner mismatch")
                        conn.execute(
                            "INSERT INTO olympus_kv (ns,k,v) VALUES (%s,%s,%s) "
                            "ON CONFLICT (ns,k) DO UPDATE SET v=EXCLUDED.v",
                            (ns, key, value))

                yield OwnerTransaction()

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
