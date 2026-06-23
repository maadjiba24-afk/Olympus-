"""The replay gate must never produce a false green: a clean run passes, a
divergent run FAILS, a provider/account problem SKIPS, and an unexpected
internal error FAILS loudly (logged, not swallowed)."""

from olympus import errors, replaygate, replaystore


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


def test_clean_run_passes(monkeypatch):
    monkeypatch.setattr(replaygate.trace, "load_run", lambda rid: {"decisions": [{}, {}]})
    monkeypatch.setattr(replaygate.orchestrator, "replay_run", lambda rid: ({}, {}, []))
    all_pass, results = replaygate.run_exit_check(
        ["a", "b", "c"], make_bot=lambda: _Bot(), report=_quiet)
    assert all_pass is True
    assert all(replaygate._ok(r) for r in results)
    assert replaygate.genuine_failures(results) == []


def test_divergent_run_fails(monkeypatch):
    monkeypatch.setattr(replaygate.trace, "load_run", lambda rid: {"decisions": [{}]})
    monkeypatch.setattr(replaygate.orchestrator, "replay_run",
                        lambda rid: ({}, {}, [{"index": 0, "original": {}, "replayed": {}}]))
    all_pass, results = replaygate.run_exit_check(
        ["a", "b", "c"], make_bot=lambda: _Bot(), report=_quiet)
    assert all_pass is False
    assert all(r["completed"] and not r["replayable"] for r in results)
    assert replaygate.genuine_failures(results)            # divergence is a real fail


def test_replay_divergence_exception_fails(monkeypatch):
    monkeypatch.setattr(replaygate.trace, "load_run", lambda rid: {"decisions": [{}]})

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
    monkeypatch.setattr(replaygate.trace, "load_run", lambda rid: {"decisions": [{}]})

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
