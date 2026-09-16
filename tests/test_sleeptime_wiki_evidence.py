"""M03 adversarial acceptance: actual entry points, owned fixtures, no providers."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from olympus import (config, deltas, discovery, discovery_publication as dp,
                     memory, note_evidence as notes, owner_evidence as oe,
                     sleeptime, sleeptime_cycles as cycles, sleeptime_evidence as se,
                     store, supervise, usermem, wiki, wiki_evidence as we, witness)


@pytest.fixture(autouse=True)
def owned(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "m03-adversarial-owned-fixture")
    monkeypatch.delenv("OLYMPUS_ANCHOR", raising=False)
    monkeypatch.delenv("OLYMPUS_DATABASE_URL", raising=False)
    store.reset()


def seed(user="Case:A"):
    return [usermem.add_memory(user, type="project", confidence=.9,
        content="Owned Alpha test project ships in the first quarter " + suffix,
        provenance=["owned-fixture"], sensitivity="high")
        for suffix in ("this year", "as scheduled")]


def propose(user="Case:A", **kwargs):
    result = sleeptime.refine_user(user,
        generator=lambda rows: "Owned Alpha test project ships in the first quarter.",
        verifier=lambda rows, text: {"supported": True, "confidence": .95},
        auto_apply=False, **kwargs)
    assert result["clean"] and result["proposed"] == 1
    return result["proposal_ids"][0]


@pytest.mark.parametrize("owners", [("Case:A", "Case_A"), ("Alice", "alice"),
                                  ("é", "e\u0301"), ("x" * 500, "x" * 499 + "y")])
def test_consolidation_and_wiki_never_alias_exact_owners(owners):
    first, second = owners
    seed(first)
    identifier = propose(first)
    wiki.upsert(first, "Topic", "First owner's evidence")
    assert sleeptime.proposals(second) == [] and wiki.pages(second) == []
    assert sleeptime.approve(second, identifier) is None
    assert len(usermem.active_memories(first)) == 2


@pytest.mark.parametrize("namespace", se.LEGACY)
def test_legacy_consolidation_stays_unclaimed_and_byte_identical(namespace):
    user = "Case:A"
    raw = b'{"ambiguous": "legacy bytes"}'
    store.backend().put(namespace, memory.safe_id(user), raw)
    seed(user)
    with pytest.raises(oe.OwnerEvidenceStateError, match="unclaimed legacy"):
        sleeptime.proposals(user)
    assert se.initialize(user, acknowledge_legacy=True)["state"] == "valid"
    assert sleeptime.proposals(user) == []
    assert store.backend().get(namespace, memory.safe_id(user)) == raw


def test_legacy_wiki_is_preserved_until_explicit_empty_initialization():
    old = config.MEMORY_DIR / "users" / memory.safe_id("Case:A") / "wiki" / "topic.md"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"ambiguous legacy page")
    with pytest.raises(oe.OwnerEvidenceStateError, match="unclaimed legacy"):
        wiki.read("Case:A", "topic")
    we.initialize("Case:A", acknowledge_legacy=True)
    assert wiki.pages("Case:A") == [] and old.read_bytes() == b"ambiguous legacy page"


def test_atomic_rewrite_lost_acknowledgement_retries_without_duplicate(monkeypatch):
    user = "Case:A"
    before = seed(user)
    identifier = propose(user)
    real = oe.publish
    def fail_after_publish(path, raw, name):
        real(path, raw, name)
        raise OSError("owned lost acknowledgement")
    monkeypatch.setattr(oe, "publish", fail_after_publish)
    with pytest.raises(oe.OwnerEvidenceStateError):
        sleeptime.approve(user, identifier)
    monkeypatch.setattr(oe, "publish", real)
    rewritten = sleeptime.approve(user, identifier)
    assert len(usermem.all_memories(user)) == 3
    assert len(sleeptime.snapshots(user)) == 1
    assert sleeptime.snapshots(user)[0]["sources"] == before
    assert sleeptime.snapshots(user)[0]["rewrite_id"] == rewritten
    assert sleeptime.revert(user, identifier) is True
    assert sleeptime.revert(user, identifier) is True
    assert usermem.active_memories(user) == before


def test_stale_approval_and_revert_refuse_all_mutation():
    user = "Case:A"
    rows = seed(user)
    identifier = propose(user)
    with usermem._guard(user) as data:
        data[usermem._MEMS][0]["content"] = "Operator changed source evidence."
    before = usermem._state(user).document._raw()
    with pytest.raises(ValueError, match="sources changed"):
        sleeptime.approve(user, identifier)
    assert usermem._state(user).document._raw() == before
    with usermem._guard(user) as data:
        data[usermem._MEMS][0] = rows[0]
    rewrite_id = sleeptime.approve(user, identifier)
    with usermem._guard(user) as data:
        next(r for r in data[usermem._MEMS] if r["id"] == rewrite_id)["content"] = "Edited rewrite"
    before = usermem._state(user).document._raw()
    with pytest.raises(ValueError, match="stale revert"):
        sleeptime.revert(user, identifier)
    assert usermem._state(user).document._raw() == before


def test_concurrent_approvals_publish_one_rewrite():
    seed()
    identifier = propose()
    barrier = threading.Barrier(4)
    def worker(_):
        barrier.wait()
        return sleeptime.approve("Case:A", identifier)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(worker, range(4)))
    assert len(set(results)) == 1
    assert len(usermem.all_memories("Case:A")) == 3


@pytest.mark.parametrize("damage", [b"", b"{}", b"[]", b"{broken", b'{"version":2,"version":2}'])
def test_damaged_wiki_is_unavailable_and_preserved(damage):
    wiki.upsert("Case:A", "Topic", "Evidence")
    path = we.path("Case:A")
    notes.io(path).write_bytes(damage)
    with pytest.raises(oe.OwnerEvidenceStateError):
        wiki.upsert("Case:A", "Topic", "Replacement")
    assert notes.io(path).read_bytes() == damage


def test_empty_or_missing_proposal_evidence_cannot_graduate():
    for _ in range(config.SLEEPTIME_GRADUATION + 1):
        sleeptime._record_cycle(True, 1, 0)
    assert sleeptime.state()["clean_cycles"] == 0 and not sleeptime.graduated()
    assert supervise.run_supervised_cycle()["grade"] == "DIRTY"


def test_signed_cycle_and_counters_share_one_lost_ack_boundary(monkeypatch):
    seed()
    real = notes.publish
    def fail_after_publish(path, raw):
        real(path, raw)
        if path == deltas._path(cycles.TARGET):
            raise OSError("owned lost acknowledgement")
    monkeypatch.setattr(notes, "publish", fail_after_publish)
    with pytest.raises(deltas.DeltaError, match="retry this cycle ID"):
        supervise.run_supervised_cycle(cycle_id="owned-cycle", generator=lambda rows: "Alpha ships in Q1.",
            verifier=lambda rows, text: {"supported": True, "confidence": .95})
    monkeypatch.setattr(notes, "publish", real)
    report = supervise.run_supervised_cycle(cycle_id="owned-cycle",
        generator=lambda rows: pytest.fail("retry regenerated model output"))
    assert report["grade"] == "CLEAN" and report["streak"] == 1
    assert len(supervise.scoreboard()) == 1
    assert len(sleeptime.proposals("Case:A")) == 1


def test_anchor_failure_cannot_authorize_graduation_and_retries_same_record(monkeypatch):
    from olympus import anchor
    seed()
    monkeypatch.setenv("OLYMPUS_ANCHOR", "owned-fixture")
    monkeypatch.setattr(anchor, "publish_head", lambda *args: False)
    with pytest.raises(deltas.DeltaError):
        supervise.run_supervised_cycle(cycle_id="anchor-cycle", generator=lambda rows: "Alpha ships Q1.",
            verifier=lambda rows, text: {"supported": True, "confidence": .95})
    with pytest.raises(oe.OwnerEvidenceStateError):
        sleeptime.graduated()
    saved = deltas.snapshots(cycles.TARGET)[0]
    class OwnedSink:
        def read(self, kind, target):
            return anchor._seal(kind, target, saved["seq"], saved["snapshot_hash"])
    monkeypatch.setattr(anchor, "_sink", OwnedSink)
    monkeypatch.setattr(anchor, "publish_head", lambda *args: True)
    report = supervise.run_supervised_cycle(cycle_id="anchor-cycle",
        generator=lambda rows: pytest.fail("anchor retry regenerated"))
    assert report["streak"] == 1 and len(supervise.scoreboard()) == 1


def test_unsigned_cycle_refused_and_rl_does_not_report_zero(monkeypatch):
    from olympus import rlscaffold
    seed()
    monkeypatch.setattr(witness, "sign_with", lambda *args: (_ for _ in ()).throw(witness.WitnessError("owned")))
    with pytest.raises(deltas.DeltaError):
        supervise.run_supervised_cycle(generator=lambda rows: "Alpha ships Q1.",
            verifier=lambda rows, text: {"supported": True, "confidence": .95})
    assert deltas.snapshots(cycles.TARGET) == []
    notes.publish(deltas._path(cycles.TARGET), b"broken\n")
    health = rlscaffold._scoreboard_health()
    assert health["available"] is False and health["clean_cycles"] is None


def test_signing_failure_before_append_replays_durable_draft_without_models(monkeypatch):
    seed()
    real = witness.sign_with
    monkeypatch.setattr(witness, "sign_with", lambda *a: (_ for _ in ()).throw(witness.WitnessError("owned signer")))
    with pytest.raises(deltas.DeltaError):
        supervise.run_supervised_cycle(cycle_id="signer-retry", generator=lambda rows: "Alpha ships Q1.",
            verifier=lambda rows, text: {"supported": True, "confidence": .95})
    assert deltas.snapshots(cycles.TARGET) == []
    assert cycles.status()["state"] == "unavailable"
    monkeypatch.setattr(witness, "sign_with", real)
    report = supervise.run_supervised_cycle(cycle_id="signer-retry",
        generator=lambda rows: pytest.fail("recovery repeated the generator"),
        verifier=lambda *a: pytest.fail("recovery repeated the verifier"))
    assert report["grade"] == "CLEAN" and report["streak"] == 1
    assert len(sleeptime.proposals("Case:A")) == 1


def test_cycle_identity_rejects_conflicting_retry_without_new_record():
    seed()
    identifier = propose()
    original = cycles.record(True, 1, 0, proposal_refs=[("Case:A", identifier)], cycle_id="bound")
    assert cycles.record(True, 1, 0, proposal_refs=[("Case:A", identifier)], cycle_id="bound") == original
    with pytest.raises(ValueError, match="different evidence"):
        cycles.record(False, 1, 0, proposal_refs=[("Case:A", identifier)], cycle_id="bound")
    assert len(cycles.records()) == 1


def test_competing_cycles_do_not_prepare_against_unconfirmed_counters():
    seed()
    entered, release, second_started, second_provider = (threading.Event() for _ in range(4))
    def first_generator(rows):
        entered.set()
        assert release.wait(5)
        return "Alpha ships Q1."
    def second_generator(rows):
        second_provider.set()
        return "Alpha ships Q1."
    verifier = lambda *a: {"supported": True, "confidence": .95}
    def second():
        second_started.set()
        return supervise.run_supervised_cycle(cycle_id="second", generator=second_generator, verifier=verifier)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(supervise.run_supervised_cycle, cycle_id="first", generator=first_generator, verifier=verifier)
        try:
            assert entered.wait(5)
            other = pool.submit(second)
            assert second_started.wait(5)
            assert not second_provider.wait(.1)
        finally:
            release.set()
        assert first.result(timeout=5)["streak"] == 1
        assert other.result(timeout=5)["streak"] == 2


@pytest.mark.parametrize("operation", ["approve", "revert", "delta"])
def test_idempotent_retry_reconfirms_failed_directory_durability(monkeypatch, operation):
    seed()
    identifier = propose()
    if operation == "revert":
        sleeptime.approve("Case:A", identifier)
    target = (deltas._path("owned-durability") if operation == "delta" else
              usermem._state("Case:A").document._file())
    real = notes.sync_dir
    calls = []
    def fail(directory):
        if notes.logical(directory) == target.parent:
            calls.append(directory)
            raise OSError("owned directory durability failure")
        return real(directory)
    monkeypatch.setattr(notes, "sync_dir", fail)
    def run():
        if operation == "delta":
            return deltas.record_snapshot("owned-durability", kind="owned", state={"value": 1},
                                           operation_id="same", require_signature=True)
        return getattr(sleeptime, operation)("Case:A", identifier)
    for _ in range(2):
        with pytest.raises((oe.OwnerEvidenceStateError, deltas.DeltaError, OSError)):
            run()
    assert len(calls) >= 2
    monkeypatch.setattr(notes, "sync_dir", real)
    assert run()
    if operation == "delta":
        assert len(deltas.snapshots("owned-durability")) == 1
    else:
        assert len(usermem.all_memories("Case:A")) == 3


@pytest.mark.skipif(os.name != "posix", reason="M13: Windows remains single process per state directory")
def test_separate_processes_serialize_approvals_and_delta_history():
    seed()
    identifier = propose()
    code = '''from pathlib import Path
import sys
from olympus import config, sleeptime, deltas, store
config.MEMORY_DIR = Path(sys.argv[1])
store.reset()
print(sleeptime.approve("Case:A", sys.argv[2]), flush=True)
for n in range(4):
    deltas.record_snapshot("owned-contention", kind="owned", state={"worker": sys.argv[3], "n": n},
                          operation_id=sys.argv[3] + ":" + str(n), require_signature=True)
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    children = [subprocess.Popen([sys.executable, "-B", "-c", code, str(config.MEMORY_DIR), identifier, str(i)],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
    results = []
    for child in children:
        out, err = child.communicate(timeout=25)
        assert child.returncode == 0, (out, err)
        results.append(out.strip())
    assert len(set(results)) == 1 and len(usermem.all_memories("Case:A")) == 3
    assert deltas.verify_history("owned-contention")["ok"]
    assert len(deltas.snapshots("owned-contention")) == 12


@pytest.mark.parametrize("damage", ["marker", "transplant", "hidden"])
def test_owner_enumeration_refuses_ambiguous_evidence(damage):
    notes.initialize("Case:A", "lessons")
    root = notes.directory("Case:A", "lessons")
    if damage == "hidden":
        notes.publish(root / ".unknown", b"unattributed")
    elif damage == "marker":
        notes.publish(root / ".initialized-v2.json", b"{}")
    else:
        wiki.upsert("Case_A", "Topic", "Other owner")
        notes.publish(we.path("Case:A"), notes.read_raw(we.path("Case_A")))
    with pytest.raises(oe.OwnerEvidenceStateError):
        wiki.users_with_material()


def test_actual_cli_rewrite_revert_and_unavailable_run(monkeypatch, capsys):
    from olympus import cli
    seed()
    identifier = propose()
    assert cli.main(["sleeptime", "apply", identifier, "--user", "Case:A"]) == 0
    assert len(usermem.active_memories("Case:A")) == 1
    assert cli.main(["sleeptime", "revert", identifier, "--user", "Case:A"]) == 0
    assert len(usermem.active_memories("Case:A")) == 2
    monkeypatch.setattr(config, "sleeptime_enabled", lambda: True)
    notes.publish(deltas._path(cycles.TARGET), b"damaged\n")
    assert cli.main(["sleeptime", "run"]) == 1
    assert "unavailable" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows extended-path acceptance")
def test_native_windows_wiki_and_consolidation_long_paths(monkeypatch, tmp_path):
    root = tmp_path / ("owned-" + "x" * 90) / ("state-" + "y" * 90)
    monkeypatch.setattr(config, "MEMORY_DIR", root)
    store.reset()
    owner = "Long:A" + "é" * 300
    seed(owner)
    identifier = propose(owner)
    assert sleeptime.approve(owner, identifier)
    assert sleeptime.revert(owner, identifier)
    wiki.upsert(owner, "Topic", "Exact owner evidence")
    assert "Exact owner evidence" in wiki.read(owner, "topic")
    assert owner in wiki.users_with_material()


def test_supervision_restores_exact_caller_context_on_failure(monkeypatch):
    memory.set_user("Caller:A")
    monkeypatch.setattr(cycles, "state", lambda: (_ for _ in ()).throw(ValueError("owned failure")))
    with pytest.raises(ValueError):
        supervise.run_supervised_cycle()
    assert memory.current_owner() == "Caller:A"


def test_invalid_last_generated_wiki_row_cannot_publish_first_page_or_checkpoint():
    seed()
    result = wiki.dream("Case:A", runner=lambda *args: {"pages": [
        {"title": "Valid", "content": "Must not be published"},
        {"title": "Invalid", "content": "Refuse", "durable": "false"}]})
    assert "failed" in result and wiki.pages("Case:A") == []
    assert wiki.last_dream("Case:A") == 0


@pytest.mark.parametrize("source", ["typed", "wiki", "note"])
def test_concurrent_source_or_destination_edits_refuse_stale_dream(source):
    seed()
    def runner(*args):
        if source == "typed":
            usermem.add_memory("Case:A", type="project", confidence=.9, content="Concurrent operator information")
        elif source == "wiki":
            wiki.upsert("Case:A", "Topic", "Concurrent wiki edit")
        else:
            with memory.user_context("Case:A"):
                memory.save("lessons", "Operator", "Concurrent note")
        return {"pages": [{"title": "Generated", "content": "Stale model output"}]}
    with pytest.raises(oe.OwnerEvidenceStateError):
        wiki.dream("Case:A", runner=runner)
    assert all(p["title"] != "Generated" for p in wiki.pages("Case:A"))
    assert wiki.last_dream("Case:A") == 0


def test_large_note_checkpoint_tracks_only_chunks_read_by_each_dream():
    with memory.user_context("Case:A"):
        memory.save("lessons", "Long note", "Owned information. " * 2000)
    calls = []
    def runner(system, prompt, schema):
        calls.append(prompt)
        return {"pages": []}
    wiki.dream("Case:A", runner=runner)
    first = we.read("Case:A")[0]["seen"]
    assert 0 < len(first) < len(wiki._material("Case:A")[1])
    wiki.dream("Case:A", runner=runner)
    second = we.read("Case:A")[0]["seen"]
    assert len(second) > len(first) and calls[0] != calls[1]


def test_discovery_final_ack_failure_recovers_without_research_replay(monkeypatch):
    gap = discovery.note_gap("knowledge", "Owned topic", user="Case:A")
    real = notes.publish
    fired = []
    def fail_on_gap_ack(path, raw):
        if path == discovery._gaps_path("Case:A") and b'"acquired"' in raw and not fired:
            fired.append(True)
            raise OSError("owned acknowledgement failure")
        return real(path, raw)
    monkeypatch.setattr(notes, "publish", fail_on_gap_ack)
    output = discovery.acquire_knowledge(gap, "Case:A", runner=lambda q: "Owned research evidence. " * 20)
    assert "queued" in output and fired
    monkeypatch.setattr(notes, "publish", real)
    active = json.loads(notes.read_raw(notes._active(), 4096))
    notes.recover(active["id"], "resume")
    output = discovery.acquire_knowledge(gap, "Case:A",
        runner=lambda q: pytest.fail("research repeated after durable preparation"))
    assert "learned" in output and len(wiki.pages("Case:A")) == 1
    assert discovery.open_gaps("Case:A") == []


def test_discovery_rejects_foreign_gap_before_provider_and_preserves_context():
    gap = discovery.note_gap("knowledge", "Owned topic", user="Case:A")
    memory.set_user("Caller:A")
    output = discovery.acquire_knowledge(gap, "Case_A", runner=lambda q: pytest.fail("foreign provider call"))
    assert "unavailable" in output and memory.current_owner() == "Caller:A"
    assert discovery.open_gaps("Case:A") == [gap]


def test_feature_publication_retry_has_one_shared_note():
    gap = discovery.note_gap("capability", "Owned capability", user="Case:A")
    assert "proposed feature" in discovery.propose_feature(gap, "Case:A")
    assert "proposed feature" in discovery.propose_feature(gap, "Case:A")
    assert len(notes.notes("shared", "upgrades")) == 1
