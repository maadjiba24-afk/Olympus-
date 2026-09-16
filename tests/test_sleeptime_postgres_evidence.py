"""Real M03 database contract, executed only in a new owned Unix-socket fixture.

No ambient DATABASE_URL is accepted. The local validation controller creates
the fixture marker and cluster, runs this file, restarts it and inspects its
preserved rows. Default full suites explicitly skip this separate required leg.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from olympus import config, memory, owner_evidence as oe, sleeptime, sleeptime_evidence as se, store, usermem


@pytest.fixture
def owned_pg(monkeypatch):
    root_name = os.environ.get("OLYMPUS_M03_POSTGRES_FIXTURE")
    if not root_name:
        pytest.skip("required separate M03 owned PostgreSQL fixture leg")
    assert sys.platform == "linux", "owned database validation uses native Linux storage"
    import psycopg
    root = Path(root_name)
    assert root.resolve(strict=True) == root and root.name.startswith("olympus-m03-pg-")
    assert not str(root).startswith("/mnt/") and root.stat().st_uid == os.getuid()
    marker = json.loads((root / "fixture-identity.json").read_text())
    assert marker == {"purpose": "M03 isolated PostgreSQL fixture", "root": str(root), "uid": os.getuid()}
    dsn = psycopg.conninfo.make_conninfo(host=str(root / "socket"), dbname="postgres", connect_timeout=5)
    with psycopg.connect(dsn) as conn:
        actual = conn.execute("SHOW data_directory").fetchone()[0]
        assert Path(actual).resolve() == root / "data"
    monkeypatch.delenv("OLYMPUS_DATABASE_URL", raising=False)
    backend = store.PostgresStore(dsn)
    monkeypatch.setattr(store, "_backend", backend)
    owner = "M03:owned:" + uuid.uuid4().hex
    rows = [usermem.add_memory(owner, type="project", content="Owned Alpha project ships in Q1 " + word,
                               confidence=.9, provenance=["owned-pg"])
            for word in ("this year", "as planned")]
    result = sleeptime.refine_user(owner, generator=lambda rows: "Owned Alpha project ships in Q1.",
        verifier=lambda rows, text: {"supported": True, "confidence": .95}, auto_apply=False)
    assert result["clean"] and len(result["proposal_ids"]) == 1
    return root, backend, owner, result["proposal_ids"][0], rows


@pytest.mark.parametrize("phase", ["before_commit", "lost_ack"])
def test_postgres_rewrite_transaction_interruption(owned_pg, monkeypatch, phase):
    root, backend, owner, identifier, original = owned_pg
    real = backend.owner_transaction
    @contextmanager
    def interrupted(ns, key):
        wrote = []
        with real(ns, key) as transaction:
            class Proxy:
                def get(self, *args):
                    return transaction.get(*args)
                def put(self, *args):
                    transaction.put(*args)
                    wrote.append(True)
            yield Proxy()
            if wrote and phase == "before_commit":
                raise backend._psycopg.OperationalError("owned failure before database commit")
        if wrote and phase == "lost_ack":
            raise backend._psycopg.OperationalError("owned lost commit acknowledgement")
    monkeypatch.setattr(backend, "owner_transaction", interrupted)
    with pytest.raises(oe.OwnerEvidenceStateError):
        sleeptime.approve(owner, identifier)
    monkeypatch.setattr(backend, "owner_transaction", real)
    assert len(usermem.all_memories(owner)) == (2 if phase == "before_commit" else 3)
    rewrite = sleeptime.approve(owner, identifier)
    assert sleeptime.approve(owner, identifier) == rewrite
    assert len(usermem.all_memories(owner)) == 3
    assert sleeptime.snapshots(owner)[0]["sources"] == original
    assert sleeptime.revert(owner, identifier) and sleeptime.revert(owner, identifier)
    assert usermem.active_memories(owner) == original


def test_postgres_exact_owner_and_stale_revert(owned_pg):
    root, backend, owner, identifier, original = owned_pg
    collision = memory.safe_id(owner)
    assert sleeptime.proposals(collision) == []
    assert sleeptime.approve(collision, identifier) is None
    rewrite = sleeptime.approve(owner, identifier)
    with usermem._guard(owner) as state:
        next(row for row in state[usermem._MEMS] if row["id"] == rewrite)["content"] = "Operator revision"
    before = usermem._state(owner).document._raw()
    with pytest.raises(ValueError, match="stale revert"):
        sleeptime.revert(owner, identifier)
    assert usermem._state(owner).document._raw() == before


def test_postgres_competing_processes_use_database_lock(owned_pg):
    root, backend, owner, identifier, original = owned_pg
    import psycopg
    group = root / ("workers-" + uuid.uuid4().hex)
    group.mkdir(mode=0o700)
    code = '''from pathlib import Path
import sys
from olympus import config, sleeptime, store
import psycopg
config.MEMORY_DIR = Path(sys.argv[2])
store._backend = store.PostgresStore(psycopg.conninfo.make_conninfo(
    host=sys.argv[1], dbname="postgres", connect_timeout=5, application_name=sys.argv[5]))
print(sleeptime.approve(sys.argv[3], sys.argv[4]), flush=True)
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    names = ["m03-" + uuid.uuid4().hex for _ in range(3)]
    lock_id = int.from_bytes(hashlib.sha256(("usermem.state.v3\0" + memory.storage_key(owner)).encode()).digest()[:8], "big", signed=True)
    children, observed, results = [], set(), []
    try:
        with backend._conn() as blocker:
            blocker.execute("SELECT pg_advisory_lock(%s)", (lock_id,))
            for index, name in enumerate(names):
                directory = group / str(index)
                directory.mkdir(mode=0o700)
                children.append(subprocess.Popen([sys.executable, "-B", "-c", code,
                    str(root / "socket"), str(directory / "state"), owner, identifier, name],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                rows = blocker.execute("SELECT application_name FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND application_name = ANY(%s)", (names,)).fetchall()
                observed.update(row[0] for row in rows)
                if observed == set(names):
                    break
                time.sleep(.05)
            assert observed == set(names), "did not observe all three independent database waiters"
            blocker.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
        for child in children:
            out, err = child.communicate(timeout=20)
            assert child.returncode == 0, (out, err)
            results.append(out.decode().strip())
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.communicate(timeout=5)
    assert len(set(results)) == 1 and len(usermem.all_memories(owner)) == 3
    assert len(sleeptime.snapshots(owner)) == 1
    (group / "contention-result.json").write_text(json.dumps({
        "owner": owner, "proposal": identifier, "distinct_state_directories": 3,
        "advisory_waiters_observed": sorted(observed), "rewrite_ids": results,
    }, indent=2) + "\n")
