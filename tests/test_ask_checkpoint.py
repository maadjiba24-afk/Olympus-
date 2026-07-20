"""Ask-path checkpoint/resume (the M2.1 ledger threaded through orchestrator).

The ledger checkpoint primitive is proven in test_m2_ledger.py; here we prove it
is wired into the live ask-path: a run killed after the expensive verified
council committed its answer can be RESUMED (recovering the answer) instead of
recomputing the council. Gated OFF by default, so the normal ask path is
unchanged.
"""
import pytest

from olympus import config, ledger, orchestrator, witness

_signing = pytest.mark.skipif(
    not witness.available(),
    reason="ledger checkpoints are witness-signed; needs the crypto backend")


# --- the gate: off by default, and never on during replay ---------------

def test_checkpoint_off_by_default(monkeypatch):
    monkeypatch.delenv("OLYMPUS_ASK_CHECKPOINT", raising=False)
    assert config.ask_checkpoint_enabled() is False


def test_checkpoint_on_when_flag_set(monkeypatch):
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    monkeypatch.setenv("OLYMPUS_ASK_CHECKPOINT", "1")
    assert config.ask_checkpoint_enabled() is True


def test_checkpoint_forced_off_under_replay(monkeypatch):
    # Even with the flag on, a replay must not checkpoint (it reconstructs
    # deterministically from the trace and must not be perturbed).
    monkeypatch.setenv("OLYMPUS_ASK_CHECKPOINT", "1")
    monkeypatch.setenv("OLYMPUS_REPLAY", "some-run-id")
    assert config.ask_checkpoint_enabled() is False


def test_resume_ask_unknown_run_is_none():
    # Reading a run that never checkpointed needs no crypto and is safe.
    assert orchestrator.resume_ask("no-such-run-abc123") is None


# --- the mechanism: drive → kill → resume-skips-committed ----------------

@_signing
def test_run_checkpointed_skips_committed_stage_on_resume():
    run_id = "ask-resume-1"
    calls = []

    def s0(_prev):
        calls.append("s0")
        return {"v": "A"}

    def s1(prev):
        calls.append("s1")
        return {"v": prev["v"] + "B"}

    stages = [("s0", s0), ("s1", s1)]
    plan = [{"stage": "s0"}, {"stage": "s1"}]

    # Simulate a kill after stage 0 commits (stop_before=1 leaves a valid prefix).
    ledger.drive(run_id, plan,
                 lambda prev, _step, i: (s0 if i == 0 else s1)(prev),
                 stop_before=1)
    assert calls == ["s0"]

    # Resume with the SAME run_id + stages: stage 0 is skipped (already
    # committed), only stage 1 runs, and the chain completes.
    calls.clear()
    final = orchestrator.run_checkpointed(run_id, stages)
    assert calls == ["s1"]
    assert final == {"v": "AB"}


@_signing
def test_resume_ask_recovers_the_committed_answer_without_recompute():
    run_id = "ask-resume-2"
    computed = []

    def council(_prev):
        computed.append(1)
        return {"reply": "the verified council answer"}

    # First run commits the council answer, then "the process dies".
    orchestrator.run_checkpointed(run_id, [("council", council)])
    assert computed == [1]

    # Recovery: resume_ask returns the committed answer and NEVER recomputes.
    assert orchestrator.resume_ask(run_id) == "the verified council answer"
    # And a re-drive of the same stage skips it entirely (council not re-run).
    computed.clear()
    orchestrator.run_checkpointed(run_id, [("council", council)])
    assert computed == []


@_signing
def test_resume_is_replay_hash_stable():
    # A killed-and-resumed run shares the committed step chain with an
    # uninterrupted one — the reasoning path is identical.
    plan = [{"stage": "a"}, {"stage": "b"}]

    def ex(prev, _step, i):
        return {"n": (prev or {}).get("n", 0) + 1}

    ledger.drive("uninterrupted", plan, ex)
    ledger.drive("killed", plan, ex, stop_before=1)
    ledger.drive("killed", plan, ex)          # resume
    assert ledger.replay_hash("uninterrupted") == ledger.replay_hash("killed")
