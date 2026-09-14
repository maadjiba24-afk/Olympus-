"""Owned/mock adversarial evidence for exact-owner memory and graph transactions."""
import concurrent.futures
import json

import pytest

from olympus import config, memory, memory_evidence, owner_evidence as oe
from olympus import relgraph, store, usermem


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    store.reset()
    yield
    store.reset()


def add(owner, content="owned"):
    return usermem.add_memory(owner, type="preference", content=content, confidence=.8)


@pytest.mark.parametrize("owners", [("a/b", "a-b"), ("A", "a"),
                                   ("x" * 200 + "a", "x" * 200 + "b"),
                                   ("é", "e\u0301"), (" user ", "user")])
def test_exact_owner_isolation_every_collection(owners):
    left, right = owners
    m = add(left)
    usermem.record_event(left, "message", {"text": "owned"}, "user")
    c = usermem.add_candidate(left, {"type": "preference", "content": "held"})
    relgraph.add_edge(left, "Alice", "works_at", "Acme")
    assert usermem.active_memories(right) == []
    assert usermem.events(right) == []
    assert usermem.candidates(right) == []
    assert relgraph.nodes(right) == relgraph.edges(right) == []
    assert usermem.approve_candidate(right, c["id"]) is None
    assert usermem.tombstone(right, m["id"]) is False
    assert relgraph.forget(right, "Alice") is False
    assert usermem.get_memory(left, m["id"]) == m


@pytest.mark.parametrize("module", [usermem, relgraph])
@pytest.mark.parametrize("damage", [b"", b"[", b"null", b"[]", b'{"version":2,"version":2}',
                                    b'{"version":2,"owner":"wrong","data":{}}'])
def test_damaged_snapshot_is_unavailable_and_preserved(module, damage):
    snapshot = module._state("owner")
    path = snapshot.document._file()
    path.parent.mkdir(parents=True)
    path.write_bytes(damage)
    with pytest.raises(oe.OwnerEvidenceStateError):
        module._load(module._MEMS if module is usermem else module._NODES, "owner")
    with pytest.raises(oe.OwnerEvidenceStateError):
        add("owner") if module is usermem else relgraph.add_node("owner", "Alice")
    assert path.read_bytes() == damage
    assert snapshot.status()["state"] == "unavailable"


@pytest.mark.parametrize("module", [usermem, relgraph])
def test_legacy_requires_explicit_empty_initialization_without_claiming(module):
    snapshot = module._state("a/b")
    legacy_ns = snapshot.collections[0]
    raw = b'[{"secret":"ambiguous"}]'
    store.backend().put(legacy_ns, memory.safe_id("a/b"), raw)
    with pytest.raises(oe.OwnerEvidenceStateError):
        with snapshot.transaction():
            pass
    with pytest.raises(ValueError):
        snapshot.initialize_empty()
    result = snapshot.initialize_empty(acknowledge_legacy=True)
    assert result["legacy_unclaimed_preserved"][legacy_ns]["bytes"] == len(raw)
    assert store.backend().get(legacy_ns, memory.safe_id("a/b")) == raw
    with snapshot.transaction() as state:
        assert all(value == [] for value in state.values())
    with pytest.raises(ValueError):
        snapshot.initialize_empty(acknowledge_legacy=True)
    # The colliding owner must make its own explicit decision.
    with pytest.raises(oe.OwnerEvidenceStateError):
        with module._state("a-b").transaction():
            pass


def test_approval_failure_preserves_candidate_and_retry_is_single_publication(monkeypatch):
    c = usermem.add_candidate("owner", {"type": "preference", "content": "approved"})
    before = usermem._state("owner").document._file().read_bytes()
    real = oe.publish
    def fail(*args):
        raise OSError("owned publication fixture")
    monkeypatch.setattr(oe, "publish", fail)
    with pytest.raises(oe.OwnerEvidenceStateError):
        usermem.approve_candidate("owner", c["id"])
    assert usermem._state("owner").document._file().read_bytes() == before
    monkeypatch.setattr(oe, "publish", real)
    result = usermem.approve_candidate("owner", c["id"])
    assert usermem.candidates("owner") == []
    assert usermem.active_memories("owner") == [result]
    assert usermem.approve_candidate("owner", c["id"]) is None
    assert usermem.active_memories("owner") == [result]


def test_post_publication_failure_retry_does_not_duplicate(monkeypatch):
    c = usermem.add_candidate("owner", {"type": "preference", "content": "approved"})
    real = oe.publish
    def published_then_failed(*args):
        real(*args)
        raise OSError("owned directory-fsync uncertainty")
    monkeypatch.setattr(oe, "publish", published_then_failed)
    with pytest.raises(oe.OwnerEvidenceStateError):
        usermem.approve_candidate("owner", c["id"])
    monkeypatch.setattr(oe, "publish", real)
    assert usermem.approve_candidate("owner", c["id"]) is None
    assert len(usermem.active_memories("owner")) == 1
    assert usermem.candidates("owner") == []


def test_approval_preserves_candidate_when_memory_cap_would_drop_it(monkeypatch):
    monkeypatch.setattr(usermem, "_MAX_MEMORIES", 1)
    original = add("owner")
    c = usermem.add_candidate("owner", {"type": "preference", "content": "weak", "confidence": .1})
    with pytest.raises(ValueError, match="pruned"):
        usermem.approve_candidate("owner", c["id"])
    assert usermem.active_memories("owner") == [original]
    assert usermem.candidates("owner") == [c]


def test_conflict_approval_is_atomic_and_preserves_history():
    old = add("owner", "old")
    c = usermem.add_candidate("owner", {"type": "preference", "content": "new",
                                        "conflicts_with": old["id"]})
    result = usermem.approve_candidate("owner", c["id"])
    assert usermem.active_memories("owner") == [result]
    assert usermem.get_memory("owner", old["id"])["superseded_by"] == result["id"]


def test_cross_owner_supersession_is_refused():
    old = add("owner")
    other = add("other")
    with pytest.raises(ValueError):
        usermem.supersede("owner", old["id"], other)
    assert usermem.get_memory("owner", old["id"]) == old


@pytest.mark.parametrize("op", ["add", "forget"])
def test_graph_writes_have_one_publication_and_preserve_on_failure(monkeypatch, op):
    relgraph.add_edge("owner", "Alice", "works_at", "Acme")
    path = relgraph._state("owner").document._file()
    before = path.read_bytes()
    calls = []
    real = oe.publish
    def fail(*args):
        calls.append(args)
        raise OSError("owned write failure")
    monkeypatch.setattr(oe, "publish", fail)
    with pytest.raises(oe.OwnerEvidenceStateError):
        if op == "add":
            relgraph.add_edge("owner", "Bob", "works_at", "Other")
        else:
            relgraph.forget("owner", "Alice")
    assert len(calls) == 1 and path.read_bytes() == before
    monkeypatch.setattr(oe, "publish", real)
    if op == "forget":
        assert relgraph.forget("owner", "Alice")
        assert relgraph.edges("owner") == []


def test_graph_cap_does_not_publish_half_created_nodes(monkeypatch):
    monkeypatch.setattr(relgraph, "_MAX_NODES", 1)
    assert relgraph.add_edge("owner", "Alice", "works_at", "Acme") is None
    assert relgraph.nodes("owner") == []


def test_graph_invalid_interval_does_not_publish_nodes():
    with pytest.raises(oe.OwnerEvidenceStateError):
        relgraph.add_edge("owner", "Alice", "works_at", "Acme", valid_from=10, valid_to=1)
    assert relgraph.nodes("owner") == relgraph.edges("owner") == []


def test_enumeration_returns_exact_principals():
    add("a/b")
    add("a_b")
    assert usermem.owners() == ["a/b", "a_b"]


def test_concurrent_approvals_have_one_winner():
    c = usermem.add_candidate("owner", {"type": "preference", "content": "approved"})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: usermem.approve_candidate("owner", c["id"]), range(12)))
    assert sum(r is not None for r in results) == 1
    assert len(usermem.active_memories("owner")) == 1


def test_nested_transaction_failure_restores_savepoint():
    with usermem._guard("owner"):
        first = add("owner", "first")
        try:
            with usermem._guard("owner"):
                add("owner", "must roll back")
                raise ValueError("owned failure")
        except ValueError:
            pass
    assert usermem.active_memories("owner") == [first]


def test_cli_approval_calls_atomic_operation(monkeypatch, capsys):
    from olympus import cli
    c = usermem.add_candidate("cli", {"type": "preference", "content": "approved"})
    assert cli.main(["memory", "approve", c["id"]]) == 0
    assert "Saved." in capsys.readouterr().out
    assert usermem.candidates("cli") == []
    assert len(usermem.active_memories("cli")) == 1


def test_cli_reports_unclaimed_state_and_explicit_initialization(capsys):
    from olympus import cli
    store.backend().put(usermem._MEMS, "cli", b"[]")
    assert cli.main(["memory", "state-status"]) == 1
    assert "unavailable" in capsys.readouterr().out
    assert cli.main(["memory", "initialize-empty", "--acknowledge-unclaimed-legacy"]) == 0
    assert "initialized empty" in capsys.readouterr().out
    assert store.backend().get(usermem._MEMS, "cli") == b"[]"


def test_emem_does_not_hide_memory_evidence_failure(monkeypatch):
    from olympus import emem
    def broken(*args):
        raise oe.OwnerEvidenceStateError("memory", "owned failure")
    monkeypatch.setattr(usermem, "active_memories", broken)
    with pytest.raises(oe.OwnerEvidenceStateError):
        emem.gather("owner", "query")


def test_web_approval_and_unavailable_response_without_network(monkeypatch):
    import io
    from olympus import web
    owner = "owned-web-fixture"
    # Exercise the actual endpoint without sockets or service startup. Existing
    # local HTTP fixtures remain required in Windows/POSIX full-suite legs.
    def post(payload):
        body = json.dumps(payload).encode()
        class Connection:
            response = bytearray()
            def makefile(self, *a, **kw):
                return io.BytesIO(
                    b"POST /api/memory HTTP/1.0\r\nHost: localhost\r\n"
                    b"Content-Type: application/json\r\nX-Olympus-Token: owned-token\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            def sendall(self, data):
                self.response.extend(data)
        connection = Connection()
        web.Handler(connection, ("127.0.0.1", 12345), object())
        headers, raw = bytes(connection.response).split(b"\r\n\r\n", 1)
        return int(headers.split()[1]), json.loads(raw)
    monkeypatch.setenv("OLYMPUS_REQUIRE_LOGIN", "0")
    monkeypatch.setenv("OLYMPUS_ACCESS_TOKEN", "owned-token")
    monkeypatch.setattr(web, "_user_for", lambda sid: owner)
    monkeypatch.setattr(web, "_HITS", {})
    c = usermem.add_candidate(owner, {"type": "preference", "content": "approved"})
    code, response = post({"kind": "memory", "op": "approve", "id": c["id"]})
    assert code == 200 and response["ok"] is True
    assert usermem.candidates(owner) == []
    assert len(usermem.active_memories(owner)) == 1
    path = usermem._state(owner).document._file()
    path.write_bytes(b"damaged owned fixture")
    code, response = post({"kind": "memory", "op": "approve", "id": c["id"]})
    assert code == 503 and response["evidence_state"] == "unavailable"
    assert path.read_bytes() == b"damaged owned fixture"


def test_extraction_reports_unavailable_without_a_model_call(monkeypatch):
    from olympus import recall, usage
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_MIN_CHARS", 1)
    monkeypatch.setattr(usage, "check_budget", lambda: None)
    add("owner")
    usermem._state("owner").document._file().write_bytes(b"broken")
    calls = []
    monkeypatch.setattr(recall.backend, "complete_json", lambda *a, **k: calls.append(True))
    result = recall.extract("owner", "owned input", "reply", object())
    assert result["evidence_state"] == "unavailable"
    assert calls == []


def test_postgres_owner_transaction_uses_one_connection_and_rolls_back():
    from contextlib import contextmanager
    from copy import deepcopy
    class Connection:
        def __init__(self):
            self.values = {}
            self.commands = []
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        @contextmanager
        def transaction(self):
            before = deepcopy(self.values)
            try:
                yield
            except BaseException:
                self.values = before
                raise
        def execute(self, query, params=()):
            self.commands.append((query, params))
            if query.startswith("INSERT"):
                ns, key, value = params
                self.values[ns, key] = value
            class Cursor:
                def __init__(self, value):
                    self.value = value
                def fetchone(self):
                    return None if self.value is None else (self.value,)
            return Cursor(self.values.get(tuple(params)) if query.startswith("SELECT v") else None)
    connection = Connection()
    backend = object.__new__(store.PostgresStore)
    backend._conn = lambda: connection
    with backend.owner_transaction("owned.namespace", "owner-key") as txn:
        assert txn.get("owned.namespace", "owner-key") is None
        txn.put("owned.namespace", "owner-key", b"before")
        with pytest.raises(ValueError):
            txn.put("owned.namespace", "OTHER", b"forbidden")
    with pytest.raises(RuntimeError):
        with backend.owner_transaction("owned.namespace", "owner-key") as txn:
            assert txn.get("owned.namespace", "owner-key") == b"before"
            txn.put("owned.namespace", "owner-key", b"must roll back")
            raise RuntimeError("owned failure")
    assert connection.values == {("owned.namespace", "owner-key"): b"before"}
    assert sum("pg_advisory_xact_lock" in q for q, _ in connection.commands) == 2
    assert sum("lock_timeout" in q for q, _ in connection.commands) == 2


def test_concurrent_recall_policy_does_not_duplicate(monkeypatch):
    from olympus import recall
    monkeypatch.setattr(config, "MEMORY_CONFIDENCE_FLOOR", .6)
    monkeypatch.setattr(recall, "_maybe_embed", lambda *args: None)
    candidate = {"type": "preference", "content": "prefers concise explanations",
                 "confidence": .9, "sensitivity": "normal"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(lambda i: recall._gate("owner", dict(candidate), str(i)), range(16)))
    assert result.count("committed") == 1
    assert result.count("reinforced") == 15
    assert len(usermem.active_memories("owner")) == 1


def test_embedding_runs_after_snapshot_commit(monkeypatch):
    from olympus import recall
    observed = []
    monkeypatch.setattr(config, "MEMORY_CONFIDENCE_FLOOR", .6)
    def embedding(owner, mem):
        assert memory_evidence._CURRENT.get() in (None, {})
        assert usermem.get_memory(owner, mem["id"]) == mem
        observed.append(mem["id"])
    monkeypatch.setattr(recall, "_maybe_embed", embedding)
    assert recall._gate("owner", {"type": "preference", "content": "likes short replies",
                                   "confidence": .9}, "event") == "committed"
    assert len(observed) == 1


def _process_append_memory(state_dir, index):
    from pathlib import Path
    from olympus import config, store, usermem
    config.MEMORY_DIR = Path(state_dir)
    store.reset()
    for number in range(5):
        usermem.add_memory("owned-process-test", type="preference",
                           content=f"worker {index} row {number}", confidence=.8)


@pytest.mark.skipif(__import__("os").name == "nt",
                    reason="M13: native Windows shared-state multiprocess remains unsupported")
def test_posix_spawn_writers_preserve_every_memory(tmp_path):
    import multiprocessing
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_process_append_memory, args=(str(tmp_path), i))
                 for i in range(3)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    rows = usermem.active_memories("owned-process-test")
    assert len(rows) == 15
    assert {row["content"] for row in rows} == {
        f"worker {i} row {j}" for i in range(3) for j in range(5)}
