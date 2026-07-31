"""The replay gate must never produce a false green: a clean run passes, a
divergent run FAILS, a provider/account problem SKIPS, and an unexpected
internal error FAILS loudly (logged, not swallowed)."""

from olympus import config, errors, replaygate, replaystore


class _Bot:
    """A stand-in bot: `ask` returns a reply or raises the given exception."""
    def __init__(self, reply="an answer", run_id="r1"):
        self._reply = reply
        self.last_run_id = run_id

    def ask(self, prompt):
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def _quiet(*_a):
    pass


def _recorded(h="reqhash1"):
    """One decision from a REAL recorded run: it froze a model call, so it
    carries `model_request_hash`. A decision WITHOUT one means the pipeline
    answered without reaching a provider — see the E25 tests below."""
    return {"decision_type": "route", "model_request_hash": h,
            "model_response_ref": h}


def test_gate_disables_memory_learning_during_run(monkeypatch):
    """The gate must run with the background learner OFF, so a write between a
    run's record and replay passes can't cause a false divergence — and it must
    restore the prior setting afterward."""
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    seen = {}

    def fake_check_one(prompt, make_bot, report):
        seen["during"] = config.MEMORY_ENABLED        # captured mid-run
        return {"prompt": prompt, "completed": True, "replayable": True,
                "decisions": 2, "run_id": "r", "detail": "ok", "skipped": False}
    monkeypatch.setattr(replaygate, "check_one", fake_check_one)

    replaygate.run_exit_check(["a", "b", "c"], make_bot=lambda: None, report=_quiet)
    assert seen["during"] is False        # learner OFF during the gate...
    assert config.MEMORY_ENABLED is True  # ...and restored afterward


def test_gate_bot_uses_isolated_memory_namespace(monkeypatch):
    """The gate bot runs in its own empty 'gate' namespace, never the live
    'shared' one, so accumulated user memory can't perturb a replay."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    bot = replaygate._gate_bot()
    assert bot.user == "gate"


def test_clean_run_passes(monkeypatch):
    monkeypatch.setattr(replaygate.trace, "load_run", lambda rid: {"decisions": [_recorded(), _recorded()]})
    monkeypatch.setattr(replaygate.orchestrator, "replay_run", lambda rid: ({}, {}, []))
    all_pass, results = replaygate.run_exit_check(
        ["a", "b", "c"], make_bot=lambda: _Bot(), report=_quiet)
    assert all_pass is True
    assert all(replaygate._ok(r) for r in results)
    assert replaygate.genuine_failures(results) == []


def test_divergent_run_fails(monkeypatch):
    monkeypatch.setattr(replaygate.trace, "load_run", lambda rid: {"decisions": [_recorded()]})
    monkeypatch.setattr(replaygate.orchestrator, "replay_run",
                        lambda rid: ({}, {}, [{"index": 0, "original": {}, "replayed": {}}]))
    all_pass, results = replaygate.run_exit_check(
        ["a", "b", "c"], make_bot=lambda: _Bot(), report=_quiet)
    assert all_pass is False
    assert all(r["completed"] and not r["replayable"] for r in results)
    assert replaygate.genuine_failures(results)            # divergence is a real fail


def test_replay_divergence_exception_fails(monkeypatch):
    monkeypatch.setattr(replaygate.trace, "load_run", lambda rid: {"decisions": [_recorded()]})

    def diverge(rid):
        raise replaystore.ReplayDivergence("h", {"model": "m"})
    monkeypatch.setattr(replaygate.orchestrator, "replay_run", diverge)
    all_pass, results = replaygate.run_exit_check(
        ["a", "b", "c"], make_bot=lambda: _Bot(), report=_quiet)
    assert all_pass is False
    assert replaygate.genuine_failures(results)
    assert not any(r.get("skipped") for r in results)      # divergence != skip


def test_provider_error_skips(monkeypatch):
    bot = lambda: _Bot(reply=RuntimeError("Your credit balance is too low"))
    all_pass, results = replaygate.run_exit_check(
        ["a", "b", "c"], make_bot=bot, report=_quiet)
    assert all_pass is False
    assert all(r["skipped"] for r in results)
    assert replaygate.genuine_failures(results) == []      # skip is neither pass nor fail


def test_unexpected_error_fails_loudly_and_is_logged(monkeypatch):
    captured = []
    monkeypatch.setattr(errors, "capture",
                        lambda where, exc, context="": captured.append((where, repr(exc))))
    monkeypatch.setattr(replaygate.trace, "load_run", lambda rid: {"decisions": [_recorded()]})

    def boom(rid):
        raise RuntimeError("internal kaboom")
    monkeypatch.setattr(replaygate.orchestrator, "replay_run", boom)

    all_pass, results = replaygate.run_exit_check(
        ["a", "b", "c"], make_bot=lambda: _Bot(), report=_quiet)
    assert all_pass is False
    assert replaygate.genuine_failures(results)             # a real failure, not a pass
    assert not any(r.get("skipped") for r in results)       # not swallowed as a skip
    assert captured and any("kaboom" in r for _, r in captured)   # logged


def test_unexpected_ask_error_fails_and_is_logged(monkeypatch):
    captured = []
    monkeypatch.setattr(errors, "capture",
                        lambda where, exc, context="": captured.append(repr(exc)))
    bot = lambda: _Bot(reply=ValueError("pipeline blew up"))
    all_pass, results = replaygate.run_exit_check(
        ["a", "b", "c"], make_bot=bot, report=_quiet)
    assert all_pass is False
    assert not any(r.get("skipped") for r in results)       # genuine fail, not skip
    assert captured and any("pipeline blew up" in r for r in captured)


# --- E25: "the gate never ran" must not masquerade as a reliability failure ---

def test_run_with_no_frozen_model_call_skips_instead_of_failing(monkeypatch):
    """The audit's E25. Keyless/degraded, the pipeline still answers and records
    decisions, but freezes NO model call — so replay recomputes a hash, finds
    nothing, and "diverges". That is a missing precondition, not a determinism
    regression, and reporting it as a genuine failure tells an operator their
    release is broken when the gate simply never ran."""
    monkeypatch.setattr(replaygate.trace, "load_run",
                        lambda rid: {"decisions": [{"decision_type": "route",
                                                    "model_request_hash": None,
                                                    "cost": 0.0}] * 3})

    def must_not_replay(rid):                    # replay must not even be tried
        raise AssertionError("replay attempted on a run with no frozen call")
    monkeypatch.setattr(replaygate.orchestrator, "replay_run", must_not_replay)

    all_pass, results = replaygate.run_exit_check(
        ["a", "b", "c"], make_bot=lambda: _Bot(), report=_quiet)
    assert all_pass is False
    assert all(r["skipped"] for r in results)
    assert replaygate.genuine_failures(results) == []     # inconclusive, not FAIL
    assert all("nothing to replay" in r["detail"] for r in results)


def test_a_partially_recorded_run_is_still_replayed(monkeypatch):
    """Only a run with NO frozen call at all is inconclusive. If any decision
    froze one, the run is replayable and a divergence is a genuine failure."""
    monkeypatch.setattr(replaygate.trace, "load_run",
                        lambda rid: {"decisions": [{"model_request_hash": None},
                                                   _recorded()]})
    monkeypatch.setattr(replaygate.orchestrator, "replay_run",
                        lambda rid: ({}, {}, [{"index": 0}]))
    _all_pass, results = replaygate.run_exit_check(
        ["a", "b", "c"], make_bot=lambda: _Bot(), report=_quiet)
    assert not any(r.get("skipped") for r in results)
    assert replaygate.genuine_failures(results)
