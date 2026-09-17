"""Owned/mock evidence only: attribution, preservation and whole transactions."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
import sqlite3

import pytest

from olympus import (actions, annindex, bandit_routing, config, discovery,
                     docrag, documents, emailstyle, embed, learned_routing,
                     memory, outcomes, playbooks, retention, routing_outcomes,
                     search, store, todos, tools)
from olympus import owner_evidence as evidence

OWNER = "lab.a@owned"
ALIASES = ["lab.a@owned", "lab-a-owned", "lab/a/owned", "Lab.a@owned",
           "étudiant", "e\u0301tudiant", "a" * 100 + "x", "a" * 100 + "y",
           "shared", " shared ", "SHARED"]
FAMILIES = ["todos", "playbooks", "email", "discovery", "outcomes", "routing",
            "chunks", "ann"]


def family(name, owner=OWNER):
    if name == "todos":
        todos.add(owner, "owned task")
        return todos._evidence(owner)
    if name == "playbooks":
        playbooks.save(owner, "owned procedure", ["ask for approval"])
        return playbooks._evidence(owner)
    if name == "email":
        instance = emailstyle._evidence(owner)
        instance.save({"guide": "owned voice", "built": 1.0, "samples": 3})
        return instance
    if name == "discovery":
        discovery.note_gap("knowledge", "owned topic", user=owner)
        return discovery._evidence(owner)
    if name == "outcomes":
        outcomes.record(owner, "owned mock action", outcomes.APPROVED)
        return outcomes._evidence(owner)
    if name == "routing":
        route(owner, "owned-run")
        return routing_outcomes._evidence(owner)
    if name == "chunks":
        instance = docrag._evidence(owner)
        instance.save({"owned": {"sha256": "a" * 64, "name": "Owned",
                                "chunks": ["owned passage"], "embeddings": None}})
        return instance
    instance = docrag._ann_evidence(owner)
    instance.save({"signature": "a" * 64,
                   "graph": json.loads(annindex.build({"owned": [1.0, 0.0]}).to_bytes())})
    return instance


def route(owner, rid):
    return routing_outcomes.record_run(owner, rid, "mock research", ["argus"],
        models={"argus": "owned-mock-model"}, roles={"argus": "reasoning"},
        review_verdict="approve", synthetic=True)


@pytest.mark.parametrize("name", FAMILIES)
def test_exact_identity_survives_all_normalization_collisions(name):
    paths = []
    for owner in ALIASES:
        instance = family(name, owner)
        path = evidence._io(instance._file())
        paths.append(path)
        assert json.loads(path.read_bytes())["owner"] == owner
        assert instance.load()
    assert len(set(paths)) == len(ALIASES)


@pytest.mark.parametrize("name", FAMILIES)
@pytest.mark.parametrize("damage", [b"{", b"[]", b'{"version":2,"version":2}',
                                    b'{"value":NaN}', b'\xff'])
def test_damage_is_unavailable_and_never_replaced(name, damage):
    instance = family(name)
    value = instance.load()
    path = evidence._io(instance._file())
    path.write_bytes(damage)
    assert instance.status()["state"] == "unavailable"
    assert instance.status()["count"] is None
    with pytest.raises(evidence.OwnerEvidenceStateError):
        instance.load()
    with pytest.raises(evidence.OwnerEvidenceStateError):
        instance.save(value)
    assert path.read_bytes() == damage


@pytest.mark.parametrize("name", FAMILIES)
def test_valid_envelope_for_another_owner_is_unavailable(name):
    instance = family(name)
    path = evidence._io(instance._file())
    envelope = json.loads(path.read_bytes())
    envelope["owner"] = memory.safe_id(OWNER)
    raw = json.dumps(envelope).encode()
    path.write_bytes(raw)
    with pytest.raises(evidence.OwnerEvidenceStateError):
        instance.load()
    assert path.read_bytes() == raw


@pytest.mark.parametrize("name", FAMILIES)
def test_size_bound_is_enforced_before_decoding(name):
    instance = family(name)
    path = evidence._io(instance._file())
    before = path.read_bytes()
    with pytest.raises(evidence.OwnerEvidenceStateError, match="size bound"):
        replace(instance, max_bytes=16).load()
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", FAMILIES)
def test_linked_evidence_is_not_followed_or_replaced(name, tmp_path):
    instance = family(name)
    path = evidence._io(instance._file())
    value = instance.load()
    target = tmp_path / "unrelated-owned-fixture"
    target.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    before = target.read_bytes()
    with pytest.raises(evidence.OwnerEvidenceStateError):
        instance.load()
    with pytest.raises(evidence.OwnerEvidenceStateError):
        instance.save(value)
    assert path.is_symlink() and target.read_bytes() == before


@pytest.mark.parametrize("name", FAMILIES)
def test_failed_publication_preserves_prior_bytes(name, monkeypatch):
    instance = family(name)
    value = instance.load()
    path = evidence._io(instance._file())
    before = path.read_bytes()
    def refuse(*args, **kwargs):
        raise OSError("owned fault injection before replace")
    monkeypatch.setattr(evidence.atomicio, "publish", refuse)
    with pytest.raises(evidence.OwnerEvidenceStateError, match="publication unconfirmed"):
        instance.save(value)
    assert path.read_bytes() == before


def test_legacy_records_remain_byte_identical_and_unclaimed():
    uid = memory.safe_id(OWNER)
    paths = [config.MEMORY_DIR / "users" / uid / name for name in
             ("todos.json", "doc_index.json", "email_style.json", "documents/owned.md",
              "discovery/gaps.json")]
    paths += [config.MEMORY_DIR / "store" / ns / uid for ns in
              ("outcomes", "routing_outcomes", "playbooks", "docrag.ann", "docrag.ann.sig")]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ambiguous owned mock legacy canary")
    assert documents.read(OWNER, "owned") is None
    assert todos.listing(OWNER) == []
    assert outcomes.events(OWNER) == []
    assert routing_outcomes._all_rows() == []
    for name in FAMILIES:
        family(name)
    assert all(p.read_bytes() == b"ambiguous owned mock legacy canary" for p in paths)


def test_document_undo_binds_owner_and_generation():
    first = documents.save(OWNER, "receipt", "initial")
    with pytest.raises(PermissionError):
        documents.undo_save(first, user=memory.safe_id(OWNER))
    documents.save(OWNER, "receipt", "newer change")
    with pytest.raises(evidence.OwnerEvidenceStateError, match="stale"):
        documents.undo_save(first, user=OWNER)
    assert documents.read(OWNER, "receipt") == "newer change"


def test_document_backup_failure_prevents_replacement(monkeypatch):
    documents.save(OWNER, "receipt", "initial")
    real = evidence.publish
    def fail_backup(path, raw, name):
        if "document_backups" in path.parts:
            raise evidence.OwnerEvidenceStateError(name, "mock archive failure")
        return real(path, raw, name)
    monkeypatch.setattr(evidence, "publish", fail_backup)
    with pytest.raises(evidence.OwnerEvidenceStateError):
        documents.save(OWNER, "receipt", "replacement")
    assert documents.read(OWNER, "receipt") == "initial"


def test_tools_bind_exact_request_context():
    documents.save(OWNER, "owned", "owner canary")
    documents.save(memory.safe_id(OWNER), "owned", "other canary")
    with memory.user_context(OWNER):
        assert tools.HANDLERS["read_document"]("owned") == "owner canary"
        tools.HANDLERS["add_todo"]("owned item")
    assert todos.listing(OWNER)[0]["text"] == "owned item"
    assert todos.listing(memory.safe_id(OWNER)) == []


def test_unavailable_context_exposes_no_damaged_fragments(monkeypatch):
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    for name in ("email", "chunks", "playbooks"):
        instance = family(name)
        evidence._io(instance._file()).write_bytes(b"damaged-private-canary {")
    blocks = [emailstyle.context_block(OWNER), docrag.context_block(OWNER, "mock query"),
              playbooks.context_block(OWNER, "mock procedure")]
    assert all("unavailable" in b.lower() and "damaged-private-canary" not in b for b in blocks)


def test_email_damage_refuses_before_mail_or_provider(monkeypatch):
    from olympus import gmail, backend
    evidence._io(family("email")._file()).write_bytes(b"{")
    def unexpected(*args, **kwargs):
        pytest.fail("provider access preceded evidence validation")
    monkeypatch.setattr(gmail, "list_sent_bodies", unexpected)
    monkeypatch.setattr(backend, "complete_json", unexpected)
    with pytest.raises(evidence.OwnerEvidenceStateError):
        emailstyle.build(OWNER)


def test_routing_no_partial_aggregate_no_stale_selector(monkeypatch):
    first = family("routing", "source-a")
    second = family("routing", "source-b")
    assert len(routing_outcomes._all_rows()) == 2
    learned_routing._cache.update(ts=10**20, cells={"old": "success"}, gate_met=True)
    evidence._io(second._file()).write_bytes(b"{")
    with pytest.raises(evidence.OwnerEvidenceStateError):
        routing_outcomes._all_rows()
    with pytest.raises(evidence.OwnerEvidenceStateError):
        learned_routing._cells()
    gate = routing_outcomes.gate_status()
    assert gate["met"] is False and gate["stats"] is None
    for module in (learned_routing, bandit_routing):
        status = module.status()
        assert status["active"] is False and status["evidence_state"] == "unavailable"
    assert first.load()


def test_retry_does_not_mint_observations_and_feedback_is_owned():
    assert route(OWNER, "same") == 1
    assert route(OWNER, "same") == 0
    assert routing_outcomes.apply_feedback(memory.safe_id(OWNER), "same", "down") == 0
    assert routing_outcomes.apply_feedback(OWNER, "same", "down") == 1
    assert len(routing_outcomes.events(OWNER)) == 1
    assert routing_outcomes.events(OWNER)[0]["outcome_signal"] == routing_outcomes.NEGATIVE


def _transaction_worker(directory, index):
    from pathlib import Path
    config.MEMORY_DIR = Path(directory)
    store.reset()
    todos.add(OWNER, f"owned concurrent {index}")
    outcomes.record(OWNER, f"mock-{index}", outcomes.APPROVED)
    route(OWNER, f"mock-{index}")
    discovery.note_gap("knowledge", "one topic", user=OWNER)


def test_thread_transactions_do_not_lose_updates():
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: _transaction_worker(config.MEMORY_DIR, i), range(18)))
    assert len(todos.listing(OWNER)) == len(outcomes.events(OWNER)) == 18
    assert len(routing_outcomes.events(OWNER)) == 18
    assert discovery.open_gaps(user=OWNER)[0]["hits"] == 18


@pytest.mark.skipif(os.name == "nt", reason="Windows shared-state multi-process topology unsupported")
def test_posix_process_transactions_do_not_lose_updates():
    ctx = multiprocessing.get_context("fork")
    processes = [ctx.Process(target=_transaction_worker, args=(str(config.MEMORY_DIR), i)) for i in range(8)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert not process.is_alive() and process.exitcode == 0
    assert len(todos.listing(OWNER)) == len(outcomes.events(OWNER)) == 8
    assert len(routing_outcomes.events(OWNER)) == 8
    assert discovery.open_gaps(user=OWNER)[0]["hits"] == 8


def test_action_completion_survives_outcome_recording_failure(monkeypatch):
    action = actions.Action(id="mock", user=OWNER, type="mock", title="mock",
        payload={}, risk_class=actions.TRIVIAL, reversible=True, status=actions.EXECUTED)
    def refuse(*args, **kwargs):
        raise evidence.OwnerEvidenceStateError("outcomes", "mock publication failure")
    monkeypatch.setattr(outcomes, "record", refuse)
    actions._record_outcome(action, outcomes.APPROVED)
    assert action.status == actions.EXECUTED
    assert "Do not repeat" in action.error and "unavailable" in action.error


def test_discovery_damaged_wiki_refuses_before_research():
    from olympus import wiki_evidence, note_evidence
    note_evidence.publish(wiki_evidence.path(OWNER), b"damaged wiki evidence")
    gap = discovery.note_gap("knowledge", "owned topic", user=OWNER)
    result = discovery.acquire_knowledge(gap, OWNER,
        runner=lambda *_: pytest.fail("unqualified destination must precede research"))
    assert "unavailable" in result and "wiki" in result
    assert discovery.open_gaps(user=OWNER)[0]["status"] == "open"


def test_exact_conversation_owner_survives_rebuild():
    for i, owner in enumerate(ALIASES):
        memory.save_conversation(f"owned-cid-{i}", [{"role": "user", "content": f"canary {i}"}], owner=owner)
    assert search.reindex() == len(ALIASES)
    for i, owner in enumerate(ALIASES):
        assert [h.content for h in search.search("canary", owner=owner)] == [f"canary {i}"]


def test_ambiguous_conversation_binding_cannot_be_adopted_by_resave():
    directory = config.MEMORY_DIR / "conversations"
    directory.mkdir(parents=True)
    legacy = directory / "old.owner"
    legacy.write_text(memory.safe_id(OWNER))
    snapshot = directory / "old.json"
    snapshot.write_text('[{"role":"user","content":"legacy canary"}]')
    before = snapshot.read_bytes()
    assert memory.conversation_owner("old") is None
    with pytest.raises(evidence.OwnerEvidenceStateError, match="ambiguous legacy"):
        memory.save_conversation("old", [], owner=memory.safe_id(OWNER))
    assert snapshot.read_bytes() == before
    assert search.reindex() == 0


def test_failed_rebuild_preserves_entire_previous_index():
    memory.save_conversation("a", [{"role": "user", "content": "previous canary"}], owner=OWNER)
    memory.save_conversation("b", [{"role": "user", "content": "second canary"}], owner=OWNER)
    memory._conversation_path("b").write_bytes(b"{")
    with pytest.raises(evidence.OwnerEvidenceStateError):
        search.reindex()
    assert len(search.search("canary", owner=OWNER)) == 2


def test_retention_refuses_ambiguous_or_incomplete_erasure():
    documents.save(OWNER, "owned", "preserve owned data")
    path = documents.path_for(OWNER, "owned")
    before = path.read_bytes()
    report = retention.delete_principal(memory.safe_id(OWNER), dry_run=False)
    assert report["refused"] and report["verified"] is False
    assert report["deleted"] == [] and path.read_bytes() == before
    assert retention.verify_deleted(OWNER) is False


@pytest.mark.parametrize("name", FAMILIES)
@pytest.mark.parametrize("damage", ["extra", "wrong-type", "nonfinite"])
def test_record_schema_damage_is_preserved(name, damage):
    instance = family(name)
    path = evidence._io(instance._file())
    envelope = json.loads(path.read_bytes())
    data = envelope["data"]
    row = data[0] if isinstance(data, list) else (data["owned"] if name == "chunks" else data)
    if damage == "extra":
        row["unexpected"] = "owned mock poison"
    elif damage == "wrong-type":
        envelope["data"] = 7
    elif name == "chunks":
        row["embeddings"] = [[float("nan"), 0.0]]
    elif name == "ann":
        row["graph"]["vectors"]["owned"] = [float("nan"), 0.0]
    else:
        key = {"todos": "due", "email": "built", "playbooks": "updated_at",
               "discovery": "last_seen", "outcomes": "ts", "routing": "ts"}[name]
        row[key] = float("nan")
    raw = json.dumps(envelope).encode()
    path.write_bytes(raw)
    with pytest.raises(evidence.OwnerEvidenceStateError):
        instance.load()
    assert path.read_bytes() == raw


def test_offline_action_export_recovers_exact_owners_and_refuses_partial_data():
    from olympus import rlscaffold
    for owner in ALIASES:
        outcomes.record(owner, "owned mock action", outcomes.APPROVED)
    assert set(rlscaffold._known_users()) == set(ALIASES)
    assert len(rlscaffold.collect_action_pairs()) == len(ALIASES)
    path = evidence._io(outcomes._evidence(ALIASES[-1])._file())
    path.write_bytes(b"{")
    with pytest.raises(evidence.OwnerEvidenceStateError):
        rlscaffold.collect_action_pairs()
    assert path.read_bytes() == b"{"


def test_http_boundary_reports_unavailable_without_empty_success():
    from olympus import web
    responses = []
    class Handler:
        def _json(self, body, status):
            responses.append((body, status))
    @web._owner_evidence_response
    def request(handler):
        raise evidence.OwnerEvidenceStateError("todos", "invalid stored JSON")
    request(Handler())
    assert responses[0][1] == 503
    assert responses[0][0]["evidence_state"] == "unavailable"
    assert "todos" in responses[0][0]["error"]


def test_playbook_proposals_cannot_replace_active_procedure():
    approved = playbooks.save(OWNER, "approved workflow", ["require approval"])
    def attempt(i):
        with pytest.raises(ValueError, match="active playbook"):
            playbooks.propose(OWNER, "approved workflow", [f"unapproved {i}"])
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(attempt, range(18)))
    assert playbooks.get(OWNER, approved["id"]) == approved


def test_ann_signature_cannot_hide_wrong_embedding_values():
    items = {"owned": [1.0, 0.0]}
    index = {"owned": {"sha256": "a" * 64, "name": "Owned", "chunks": ["owned"],
                       "embeddings": [[1.0, 0.0]]}}
    docrag._persistent_index(OWNER, index, items)
    path = evidence._io(docrag._ann_evidence(OWNER)._file())
    envelope = json.loads(path.read_bytes())
    envelope["data"]["graph"]["vectors"]["owned"] = [0.0, 1.0]
    raw = json.dumps(envelope).encode()
    path.write_bytes(raw)
    with pytest.raises(evidence.OwnerEvidenceStateError, match="embedding mismatch"):
        docrag._persistent_index(OWNER, index, items)
    assert path.read_bytes() == raw


@pytest.mark.skipif(os.name != "nt", reason="native Windows extended-path evidence contract")
def test_windows_long_owner_workspace_preserves_full_digest(tmp_path, monkeypatch):
    # No registry change, shortened digest, shared namespace or existing venv.
    from pathlib import Path
    state = tmp_path / "owned-state"
    monkeypatch.setattr(config, "MEMORY_DIR", state)
    owner = "a" * 300 + "exact-owner-end"
    name = "owned-document-" + "x" * 90
    documents.save(owner, name, "one")
    receipt = documents.save(owner, name, "two")
    assert len(str(documents.path_for(owner, name))) > 260
    documents.undo_save(receipt, user=owner)
    assert documents.read(owner, name) == "one"
    for name in FAMILIES:
        instance = family(name, owner)
        assert instance.load()
        assert hashlib.sha256(owner.encode()).hexdigest() in str(instance._file())
