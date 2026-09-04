"""ADR 0005 Phase 1: the Aletheia verdict is ENFORCED on the interactive path.

Covers: the structured-verdict parser, the answer.verify behavioral contract,
reject → exactly one forced rework → UNVERIFIED banner on a second reject,
visible degradation on infrastructure failure, and the ordering proof that the
aletheia_verified predicate now receives a real verdict AFTER verification
(killing the vacuous-true bug where it was checked pre-verify with no
verify_verdict key).
"""

import pytest

from olympus import behavioral_contracts as abc_mod
from olympus import config, memory, orchestrator, trace


# --- the parser -----------------------------------------------------------

def test_parse_verdict_extracts_and_strips():
    text = ('Corrected findings here.\n\n'
            'VERDICT: {"status": "pass", "unsupported_claims": [], '
            '"confidence": 0.9}')
    content, verdict = orchestrator._parse_verdict(text)
    assert content == "Corrected findings here."
    assert verdict == {"status": "pass", "unsupported_claims": [],
                       "confidence": 0.9}


def test_parse_verdict_reject_with_claims():
    text = ('body\nVERDICT: {"status": "REJECT", '
            '"unsupported_claims": ["X is 5x faster", "Y was acquired"], '
            '"confidence": 0.8}')
    content, verdict = orchestrator._parse_verdict(text)
    assert verdict["status"] == "reject"
    assert verdict["unsupported_claims"] == ["X is 5x faster", "Y was acquired"]


def test_parse_verdict_takes_last_wellformed_line():
    text = ('VERDICT: {"status": "pass", "unsupported_claims": [], '
            '"confidence": 1.0}\nmore content\n'
            'VERDICT: {"status": "warn", "unsupported_claims": ["z"], '
            '"confidence": 0.5}')
    _, verdict = orchestrator._parse_verdict(text)
    assert verdict["status"] == "warn"


@pytest.mark.parametrize("bad", [
    "no verdict at all",
    "VERDICT: {broken json",
    'VERDICT: {"status": "maybe", "unsupported_claims": [], "confidence": 1}',
    'VERDICT: ["not", "a", "dict"]',
    "",
])
def test_parse_verdict_malformed_returns_none(bad):
    content, verdict = orchestrator._parse_verdict(bad)
    assert verdict is None                      # → visible degraded path


def test_parse_verdict_caps_claims_without_coercing_confidence():
    text = ('VERDICT: {"status": "reject", '
            '"unsupported_claims": ' + str([f"c{i}" for i in range(50)]).replace("'", '"') +
            ', "confidence": 1.0}')
    _, verdict = orchestrator._parse_verdict(text)
    assert verdict["confidence"] == 1.0
    assert len(verdict["unsupported_claims"]) == 20


@pytest.mark.parametrize("bad", [
    ('{"status": "pass", "unsupported_claims": [], '
     '"confidence": "1.0"}'),
    ('{"status": "pass", "unsupported_claims": [], '
     '"confidence": true}'),
    ('{"status": "pass", "unsupported_claims": [], '
     '"confidence": 7}'),
    ('{"status": "pass", "unsupported_claims": [], '
     '"confidence": NaN}'),
    ('{"status": "pass", "unsupported_claims": ["contradiction"], '
     '"confidence": 1.0}'),
    ('{"status": "warn", "unsupported_claims": [1], '
     '"confidence": 0.5}'),
    ('{"status": "warn", "unsupported_claims": [""], '
     '"confidence": 0.5}'),
    ('{"status": "warn", "unsupported_claims": [], '
     '"confidence": 0.5, "extra": true}'),
    '{"status": "warn", "unsupported_claims": []}',
])
def test_parse_verdict_rejects_malformed_evidence(bad):
    source = "body\nVERDICT: " + bad
    content, verdict = orchestrator._parse_verdict(source)
    assert content == source
    assert verdict is None


def test_parse_verdict_does_not_fall_back_past_invalid_latest_evidence():
    source = (
        'VERDICT: {"status": "pass", "unsupported_claims": [], '
        '"confidence": 1.0}\nbody\n'
        'VERDICT: {"status": "reject", "unsupported_claims": [], '
        '"confidence": "high"}')
    content, verdict = orchestrator._parse_verdict(source)
    assert content == source
    assert verdict is None


# --- the contract ---------------------------------------------------------

def test_answer_verify_contract_rejects_reject_verdict():
    v = abc_mod.check("answer.verify", {"verify_verdict": "reject"})
    assert not v.ok
    v2 = abc_mod.check("answer.verify", {"verify_verdict": "pass"})
    assert v2.ok
    v3 = abc_mod.check("answer.verify", {"verify_verdict": "warn"})
    assert v3.ok


# --- pipeline harness -----------------------------------------------------

def _delegate_route(*_a, **_k):
    return {"mode": "delegate", "direct_reply": None, "brief": "the brief",
            "specialists": ["hermes"], "clarifying_questions": [],
            "needs_verification": True}


def _mk_bot(monkeypatch, verify_results):
    """An Olympus whose model stages are canned. `verify_results` is a list of
    (verified_text, verdict) tuples consumed per _verify call; a callable
    entry is invoked instead (to raise)."""
    memory.set_user("averify")
    bot = orchestrator.Olympus(user="averify")
    calls = {"verify": 0, "dispatch": [], "review": 0}
    monkeypatch.setattr(bot, "_route", _delegate_route)
    monkeypatch.setattr(
        bot, "_plan", lambda brief, keys, **kw: [
            {"id": "s0", "specialist": "hermes", "task": "t",
             "depends_on": []}])
    monkeypatch.setattr(
        bot, "_dispatch_dag",
        lambda steps, tr, **kw: [("hermes", "first output")])

    def fake_verify(brief, outputs):
        i = min(calls["verify"], len(verify_results) - 1)
        calls["verify"] += 1
        r = verify_results[i]
        if callable(r):
            return r()
        return r

    monkeypatch.setattr(bot, "_verify", fake_verify)

    def fake_dispatch(assignments, tr, overrides=None, retry_index=0):
        calls["dispatch"].append([a["specialist"] for a in assignments])
        return [("hermes", "reworked output")]

    monkeypatch.setattr(bot, "_dispatch", fake_dispatch)

    def fake_review(brief, verified):
        calls["review"] += 1
        return {"verdict": "approve", "feedback": "", "retry_specialists": []}

    monkeypatch.setattr(bot, "_review", fake_review)
    return bot, calls


def _run(bot):
    tr = trace.Trace("ask", "averify")
    try:
        return bot._pipeline("question", tr)
    finally:
        tr.flush()


PASS = ("clean findings", {"status": "pass", "unsupported_claims": [],
                           "confidence": 0.9})
REJECT_1 = ("bad findings", {"status": "reject",
                             "unsupported_claims": ["X is 5x faster"],
                             "confidence": 0.9})
REJECT_2 = ("still bad", {"status": "reject",
                          "unsupported_claims": ["X is 5x faster"],
                          "confidence": 0.9})


def test_pass_verdict_no_rework_no_banner(monkeypatch):
    bot, calls = _mk_bot(monkeypatch, [PASS])
    mode, brief, result = _run(bot)
    assert mode == "delegate" and result == "clean findings"
    assert calls["dispatch"] == []              # no forced rework
    assert bot._unverified_banner is None


def test_reject_forces_exactly_one_rework_then_recovers(monkeypatch):
    bot, calls = _mk_bot(monkeypatch, [REJECT_1, PASS])
    mode, brief, result = _run(bot)
    assert result == "clean findings"           # the rework's verified text
    assert len(calls["dispatch"]) == 1          # EXACTLY one forced rework
    assert calls["verify"] == 2
    assert bot._unverified_banner is None       # resolved → no banner


def test_reject_twice_ships_downgraded_with_banner(monkeypatch):
    bot, calls = _mk_bot(monkeypatch, [REJECT_1, REJECT_2])
    mode, brief, result = _run(bot)
    assert result == "still bad"
    assert len(calls["dispatch"]) == 1          # never a second rework
    assert bot._unverified_banner is not None
    assert "UNVERIFIED" in bot._unverified_banner
    assert "X is 5x faster" in bot._unverified_banner
    # the banner is a structural prefix the synthesis model can't drop
    assert bot._apply_unverified_banner("reply").startswith("⚠️ UNVERIFIED")


def test_infra_error_degrades_visibly_not_silently(monkeypatch):
    def boom():
        raise RuntimeError("verifier died")
    bot, calls = _mk_bot(monkeypatch, [boom])
    mode, brief, result = _run(bot)
    assert "first output" in result             # raw findings still ship
    assert calls["dispatch"] == []              # no rework without a verdict
    assert bot._unverified_banner is not None
    assert "could not run" in bot._unverified_banner


def test_missing_verdict_line_degrades_visibly(monkeypatch):
    bot, calls = _mk_bot(monkeypatch, [("prose with no verdict", None)])
    mode, brief, result = _run(bot)
    assert result == "prose with no verdict"
    assert bot._unverified_banner is not None
    assert "UNVERIFIED" in bot._unverified_banner


def test_router_opt_out_means_no_banner(monkeypatch):
    bot, calls = _mk_bot(monkeypatch, [PASS])
    monkeypatch.setattr(
        bot, "_route", lambda *a, **k: {
            "mode": "delegate", "direct_reply": None, "brief": "b",
            "specialists": ["hermes"], "clarifying_questions": [],
            "needs_verification": False})
    mode, brief, result = _run(bot)
    assert calls["verify"] == 0                 # router skipped verification
    assert bot._unverified_banner is None


# --- the ordering proof (kills the vacuous-true bug) ----------------------

def test_predicate_now_fed_real_verdict_after_verify(monkeypatch):
    """The aletheia_verified predicate must be evaluated with the REAL parsed
    verdict, and only after a _verify call has produced one — the old wiring
    passed no verify_verdict at all (vacuously true) at specialist-dispatch
    time."""
    seen = []
    real_check = abc_mod.check

    def spy_check(operation, ctx, registry=None):
        if operation == "answer.verify":
            seen.append(dict(ctx))
        return real_check(operation, ctx, registry)

    monkeypatch.setattr(abc_mod, "check", spy_check)
    bot, calls = _mk_bot(monkeypatch, [REJECT_1, PASS])
    _run(bot)
    # fed twice (first verdict, post-rework verdict), each with the REAL status
    assert [c.get("verify_verdict") for c in seen] == ["reject", "pass"]
    # and the first enforcement happened only after a verify call ran
    assert calls["verify"] >= 1


def test_streamed_banner_is_yielded_first(monkeypatch):
    """The streaming path must emit the banner before any synthesis chunk."""
    bot, calls = _mk_bot(monkeypatch, [REJECT_1, REJECT_2])
    from olympus import backend
    monkeypatch.setattr(backend, "complete_text",
                        lambda *a, **k: "streamed reply")
    # non-anthropic path yields once via complete_text (pool is frozen —
    # swap the whole pool on the bot instance)
    class _FakePool:
        def for_role(self, role):
            return config.Settings(provider="openai", model="m", api_key="k")

    monkeypatch.setattr(bot, "pool", _FakePool())
    pieces = list(bot.ask_stream("question"))
    joined = "".join(pieces)
    assert joined.startswith("⚠️ UNVERIFIED")
    assert "streamed reply" in joined


# --- hardening addendum ----------------------------------------------------

def test_fast_mode_does_not_skip_the_gate(monkeypatch):
    """The answer.verify gate is un-bypassable: fast mode skips the Athena
    review, never the verification chokepoint."""
    monkeypatch.setenv("OLYMPUS_FAST", "1")
    bot, calls = _mk_bot(monkeypatch, [REJECT_1, REJECT_2])
    mode, brief, result = _run(bot)
    assert calls["verify"] == 2                 # verify + enforced reverify ran
    assert len(calls["dispatch"]) == 1          # reject still forced a rework
    assert bot._unverified_banner is not None   # and still downgraded
    assert calls["review"] == 0                 # only the review was skipped


def test_rework_dispatch_error_ships_degraded_immediately(monkeypatch):
    """A rework that ERRORS (infrastructure, not verification) must ship
    degraded at once — no second dispatch, no retry loop."""
    bot, calls = _mk_bot(monkeypatch, [REJECT_1])

    def broken_dispatch(assignments, tr, overrides=None, retry_index=0):
        calls["dispatch"].append("boom")
        raise RuntimeError("worker pool exploded")

    monkeypatch.setattr(bot, "_dispatch", broken_dispatch)
    mode, brief, result = _run(bot)
    assert result == "bad findings"             # first verified text ships
    assert calls["dispatch"] == ["boom"]        # exactly one attempt
    assert calls["verify"] == 1                 # no reverify after the error
    assert bot._unverified_banner is not None
    assert "UNVERIFIED" in bot._unverified_banner


def test_verify_timeout_routes_to_infra_error_path(monkeypatch):
    """A hung verifier is cut off by the wall-clock cap and takes the visible
    degraded path — the reply never stalls forever."""
    import time as _time
    monkeypatch.setenv("OLYMPUS_VERIFY_TIMEOUT", "0.3")
    bot, calls = _mk_bot(monkeypatch, [PASS])

    def hung_verify(brief, outputs):
        _time.sleep(5)
        return PASS

    monkeypatch.setattr(bot, "_verify", hung_verify)
    t0 = _time.monotonic()
    mode, brief, result = _run(bot)
    assert _time.monotonic() - t0 < 4           # cut off, not slept out
    assert "first output" in result             # raw findings still ship
    assert bot._unverified_banner is not None   # visibly degraded
    assert "could not run" in bot._unverified_banner


def test_verify_timeout_disabled_by_env_zero(monkeypatch):
    monkeypatch.setenv("OLYMPUS_VERIFY_TIMEOUT", "0")
    from olympus import config as _config
    assert _config.verify_timeout() == 0.0
    bot, calls = _mk_bot(monkeypatch, [PASS])
    mode, brief, result = _run(bot)             # plain path still works
    assert result == "clean findings"


@pytest.mark.parametrize("pathological", [
    'VERDICT: {"status": "pass", "unsupported_claims": {"a": 1}, '
    '"confidence": "very high"}',               # wrong types inside
    'VERDICT: {"status": "reject", "unsupported_claims": null, '
    '"confidence": null}',                      # nulls
    'VERDICT: {"status": 42, "unsupported_claims": [], "confidence": 1}',
    "VERDICT: " + "{" * 5000,                   # pathological nesting prefix
])
def test_parse_verdict_never_crashes_on_garbage(pathological):
    content, verdict = orchestrator._parse_verdict(pathological)
    # Either a schema-coerced verdict or None (infra path) — never a crash,
    # and a verdict with an invalid status never silently passes.
    if verdict is not None:
        assert verdict["status"] in ("pass", "warn", "reject")
        assert isinstance(verdict["unsupported_claims"], list)
        assert 0.0 <= verdict["confidence"] <= 1.0
