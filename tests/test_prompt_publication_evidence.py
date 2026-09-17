"""M03 prompt publication: owned benchmarks, interruption and real entry points."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from olympus import (agent, cli, config, deltas, evals, memory, note_archive,
                     note_evidence as notes, orchestrator, prompt_evidence as pe,
                     reflect, tools)

_REAL_BENCHMARK = evals.run


@pytest.fixture(autouse=True)
def owned(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "owned-prompt-recovery-only")
    monkeypatch.delenv("OLYMPUS_ANCHOR", raising=False)
    root = tmp_path / "prompts"
    root.mkdir()
    monkeypatch.setattr(config, "PROMPTS_DIR", root)
    (root / "argus.md").write_bytes(b"ORIGINAL\r\nexact bytes\r\n")
    cases = [{"id": "owned-case", "specialist": "argus", "task": "Owned task", "criteria": "Owned check"}]
    monkeypatch.setattr(evals, "load_benchmarks", lambda **kw: cases)
    monkeypatch.setattr(evals, "run", lambda *a, **kw: {
        "avg": 8.0, "items": [{"id": "owned-case", "score": 8.0, "justification": "Owned fixture"}]})


def test_candidate_benchmark_does_not_change_live_readers(monkeypatch):
    observed = []
    def benchmark(*args, **kwargs):
        observed.append(agent.load_prompt("argus"))
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(agent.load_prompt, "argus").result() == "ORIGINAL\r\nexact bytes\r\n"
        assert pe.raw_prompt("argus") == b"ORIGINAL\r\nexact bytes\r\n"
        return {"avg": 8.0, "items": [{"id": "owned-case", "score": 8.0}]}
    monkeypatch.setattr(evals, "run", benchmark)
    result = orchestrator.gate_prompt_result("argus", "CANDIDATE", "owned test")
    assert observed == ["ORIGINAL\r\nexact bytes\r\n", "CANDIDATE\n"]
    assert result["status"] == "kept" and agent.load_prompt("argus") == "CANDIDATE\n"
    assert tools._restore_prompt("argus").startswith("Prompt 'argus' restored")
    assert pe.raw_prompt("argus") == b"ORIGINAL\r\nexact bytes\r\n"


@pytest.mark.parametrize("phase", ["destination", "signature", "report", "complete", "clear"])
def test_interrupted_prompt_completion_recovers_without_model_replay(monkeypatch, phase):
    original = pe.raw_prompt("argus")
    calls = []
    real_eval = evals.run
    monkeypatch.setattr(evals, "run", lambda *a, **kw: (calls.append(True), real_eval(*a, **kw))[1])
    if phase == "destination":
        real = note_archive.external_publish
        def fail(path, raw):
            real(path, raw)
            raise OSError("owned lost prompt acknowledgement")
        monkeypatch.setattr(note_archive, "external_publish", fail)
        restore = lambda: monkeypatch.setattr(note_archive, "external_publish", real)
    elif phase == "signature":
        real = deltas.record_snapshot
        monkeypatch.setattr(deltas, "record_snapshot", lambda *a, **kw: (_ for _ in ()).throw(deltas.DeltaError("owned signer failure")))
        restore = lambda: monkeypatch.setattr(deltas, "record_snapshot", real)
    elif phase == "report":
        real = notes.create
        def fail(owner, category, *args, **kwargs):
            if category == "evals":
                raise OSError("owned report failure")
            return real(owner, category, *args, **kwargs)
        monkeypatch.setattr(notes, "create", fail)
        restore = lambda: monkeypatch.setattr(notes, "create", real)
    elif phase == "complete":
        real = notes.publish
        def fail(path, raw):
            real(path, raw)
            if path.name == "state.json" and b'"phase": "complete"' in raw:
                raise OSError("owned completion acknowledgement failure")
        monkeypatch.setattr(notes, "publish", fail)
        restore = lambda: monkeypatch.setattr(notes, "publish", real)
    else:
        real = notes.remove
        def fail(path):
            real(path)
            if path == pe._active("argus"):
                raise OSError("owned pointer acknowledgement failure")
        monkeypatch.setattr(notes, "remove", fail)
        restore = lambda: monkeypatch.setattr(notes, "remove", real)
    result = orchestrator.gate_prompt_result("argus", "CANDIDATE", "owned test")
    assert result["status"] == "unavailable" and not result["kept"]
    assert len(calls) == 2
    if phase != "clear":
        assert agent.load_prompt("argus").encode() == original
    restore()
    monkeypatch.setattr(evals, "run", lambda *a, **kw: pytest.fail("recovery repeated benchmark"))
    recovered = pe.recover("argus", result["operation"], "finish")
    assert recovered["status"] == "kept"
    assert pe.status("argus")["state"] == "available"
    assert pe.raw_prompt("argus") == b"CANDIDATE\n"
    assert len(deltas.snapshots("prompt:argus")) == 1
    assert len(notes.notes("shared", "prompt_backups")) == 1
    assert len(notes.notes("shared", "evals")) == 1


def test_interrupted_preparation_can_rollback_without_benchmark(monkeypatch):
    real = notes.publish
    fired = []
    def fail(path, raw):
        if path.name == "plan.json" and "prompt-operations-v1" in str(path) and not fired:
            fired.append(True)
            raise OSError("owned preparation interruption")
        real(path, raw)
    monkeypatch.setattr(notes, "publish", fail)
    monkeypatch.setattr(evals, "run", lambda *a, **kw: pytest.fail("benchmark ran before durable preparation"))
    result = pe.gate("argus", "CANDIDATE", "owned test")
    assert result["status"] == "unavailable" and fired
    monkeypatch.setattr(notes, "publish", real)
    active = json.loads(notes.read_raw(notes._active(), 4096))
    notes.recover(active["id"], "resume")
    pending = pe.status("argus")
    with pytest.raises(ValueError, match="benchmark evidence incomplete"):
        pe.recover("argus", pending["operation"], "finish")
    assert pe.recover("argus", pending["operation"], "rollback")["status"] == "reverted"
    assert pe.raw_prompt("argus") == b"ORIGINAL\r\nexact bytes\r\n"


def test_preparation_journal_rollback_preserves_evidence_and_allows_new_gate(monkeypatch):
    real = notes.publish
    def fail(path, raw):
        real(path, raw)
        if path.name == "plan.json" and "prompt-operations-v1" in str(path):
            raise OSError("owned interruption after first target")
    monkeypatch.setattr(notes, "publish", fail)
    result = pe.gate("argus", "CANDIDATE", "owned")
    assert result["status"] == "unavailable"
    monkeypatch.setattr(notes, "publish", real)
    active = json.loads(notes.read_raw(notes._active(), 4096))
    notes.recover(active["id"], "rollback")
    assert pe.status("argus")["state"] == "available"
    assert (pe._root("argus") / active["id"]).is_dir()
    assert notes.read_raw(notes._journal_root() / active["id"] / "plan.json")
    assert pe.gate("argus", "SECOND", "owned")["status"] == "kept"


@pytest.mark.parametrize("result", [
    {"avg": 8.0}, {"avg": 8.0, "items": []},
    {"avg": True, "items": [{"id": "owned-case", "score": 1}]},
    {"avg": float("nan"), "items": [{"id": "owned-case", "score": 8}]},
    {"avg": 8.0, "items": [{"id": "other-case", "score": 8}]},
    {"avg": 8.0, "items": [{"id": "owned-case", "score": 2}]},
])
def test_missing_or_forged_benchmark_evidence_cannot_publish(monkeypatch, result):
    monkeypatch.setattr(evals, "run", lambda *a, **kw: result)
    output = pe.gate("argus", "CANDIDATE", "owned gated & kept substring")
    assert output["status"] == "refused" and output["kept"] is False
    assert pe.raw_prompt("argus") == b"ORIGINAL\r\nexact bytes\r\n"


@pytest.mark.parametrize("score", [True, "8", 99, 3.2])
def test_actual_benchmark_rejects_coerced_judge_evidence(monkeypatch, score):
    from olympus import backend
    monkeypatch.setattr(evals, "run", _REAL_BENCHMARK)
    monkeypatch.setattr(backend, "complete_text", lambda *a, **kw: "Owned answer")
    monkeypatch.setattr(backend, "complete_json", lambda *a, **kw: {"score": score, "justification": "Owned invalid verdict"})
    output = pe.gate("argus", "CANDIDATE", "owned")
    assert output["status"] == "refused" and not output["kept"]
    assert pe.raw_prompt("argus") == b"ORIGINAL\r\nexact bytes\r\n"


def test_stale_recovery_preserves_intervening_operator_edit(monkeypatch):
    real = deltas.record_snapshot
    monkeypatch.setattr(deltas, "record_snapshot", lambda *a, **kw: (_ for _ in ()).throw(deltas.DeltaError("owned failure")))
    result = pe.gate("argus", "CANDIDATE", "owned")
    monkeypatch.setattr(deltas, "record_snapshot", real)
    (config.PROMPTS_DIR / "argus.md").write_bytes(b"INTERVENING OPERATOR EDIT")
    with pytest.raises(ValueError, match="stale publication/rollback"):
        pe.recover("argus", result["operation"], "rollback")
    assert pe.raw_prompt("argus") == b"INTERVENING OPERATOR EDIT"


def test_cli_prompt_recovery_uses_specific_operation_and_preserves_context(monkeypatch, capsys):
    real = deltas.record_snapshot
    monkeypatch.setattr(deltas, "record_snapshot", lambda *a, **kw: (_ for _ in ()).throw(deltas.DeltaError("owned")))
    result = pe.gate("argus", "CANDIDATE", "owned")
    monkeypatch.setattr(deltas, "record_snapshot", real)
    memory.set_user("Caller:A")
    assert cli.main(["prompt-status", "argus"]) == 1
    assert "recovery required" in capsys.readouterr().out
    assert cli.main(["prompt-recover", "argus", result["operation"], "--decision", "finish"]) == 0
    assert memory.current_owner() == "Caller:A"


def test_pending_prompt_blocks_new_reflection_before_proposer(monkeypatch):
    real = deltas.record_snapshot
    monkeypatch.setattr(deltas, "record_snapshot", lambda *a, **kw: (_ for _ in ()).throw(deltas.DeltaError("owned")))
    pe.gate("argus", "CANDIDATE", "owned")
    monkeypatch.setattr(deltas, "record_snapshot", real)
    result = reflect.run_cycle("argus", signals=[reflect.FailureSignal("argus", "owned", "failed", "owned")],
        proposer=lambda ctx: pytest.fail("pending operation repeated proposer"))
    assert result["status"] == "unavailable" and not result["kept"]
