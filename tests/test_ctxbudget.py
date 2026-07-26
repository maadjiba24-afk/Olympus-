"""Capability C4 — calibrated context budgeting (olympus/ctxbudget.py).

Covers spec C4.12: estimator prior vs calibrated (incl. CJK + code-heavy),
convergence (I-C3), observation bounds validation, corrupt-calibration
quarantine, plan() verdicts (fit / needs_compaction / exceeded) with extreme
tool schemas, oversized tool outputs, output-reserve exhaustion and retry
accounting, the I-C2 property sweep, flag-off byte-identity of
orchestrator._estimate_tokens (I-C1), the compaction-failure trace event
(I-C5), and the estimator-accuracy benchmark (flat 4.0 vs calibrated).
"""

import json
import random

import pytest

from olympus import config, ctxbudget, errors, orchestrator, trace


# ---------------------------------------------------------------- flag ------

def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_CTX_BUDGET", raising=False)
    assert ctxbudget.enabled() is False


@pytest.mark.parametrize("val,expect", [
    ("1", True), ("on", True), ("true", True), ("YES", True),
    ("0", False), ("off", False), ("", False), ("banana", False),
])
def test_flag_parsing(monkeypatch, val, expect):
    monkeypatch.setenv("OLYMPUS_CTX_BUDGET", val)
    assert ctxbudget.enabled() is expect


# ------------------------------------------------------------ estimator -----

def test_estimator_prior_uncalibrated():
    # No calibration on disk: chars / 4.0, the legacy prior.
    assert ctxbudget.estimate_tokens("x" * 400, provider="p", model="m") == 100
    assert ctxbudget.estimate_tokens("", provider="p", model="m") == 0


def test_estimator_message_forms():
    msgs = [
        {"role": "user", "content": "abcd" * 10},                    # 40 chars
        {"role": "assistant",
         "content": [{"type": "text", "text": "xy" * 10}, "z" * 4]}, # 24 chars
        {"role": "user", "content": {"weird": 1}},                   # str()'d
    ]
    n = ctxbudget.estimate_tokens(msgs, provider="p", model="m")
    weird = len(str({"weird": 1}))
    assert n == (40 + 24 + weird) // 4


def _calibrate(provider, model, cpt, n=40, tokens=1000):
    """Feed n observations whose true chars-per-token is `cpt`."""
    for _ in range(n):
        assert ctxbudget.observe(provider, model, int(round(cpt * tokens)),
                                 tokens)


def test_estimator_calibrated_cjk():
    # CJK runs ~1.8 chars/token — far from the 4.0 prior. After calibration
    # the pair's estimates must track the true ratio within tolerance (I-C4).
    _calibrate("openai", "kimi-k2", 1.8, n=50)
    text = "漢字" * 900                       # 1800 CJK chars
    est = ctxbudget.estimate_tokens(text, provider="openai", model="kimi-k2")
    true_tokens = 1800 / 1.8                          # = 1000
    assert abs(est - true_tokens) / true_tokens <= 0.15
    # An uncalibrated pair still uses the prior (and is labeled so).
    other = ctxbudget.estimate_tokens(text, provider="openai", model="other")
    assert other == 1800 // 4
    status = ctxbudget.calibration_status()
    assert status["pairs"]["openai/kimi-k2"]["label"] == "calibrated"
    assert status["pairs"]["openai/kimi-k2"]["n"] == 50


def test_estimator_calibrated_code_heavy():
    # Code tokenizes denser than prose (~3.1 chars/token here).
    _calibrate("anthropic", "claude-opus-4-8", 3.1, n=40)
    code = ("def f(x):\n    return [x**2 for x in range(10)]\n" * 80)
    est = ctxbudget.estimate_tokens(code, provider="anthropic",
                                    model="claude-opus-4-8")
    true_tokens = len(code) / 3.1
    assert abs(est - true_tokens) / true_tokens <= 0.15


def test_convergence_ratchet_ic3():
    """I-C3: feeding a shifted true ratio, |cpt - true| is non-increasing per
    ratchet step and the EMA converges within 15% well inside 50 samples."""
    true_cpt, tokens = 2.0, 1000
    errs = []
    for _ in range(50):
        ctxbudget.observe("p", "m", int(true_cpt * tokens), tokens)
        cpt = ctxbudget.calibration_status()["pairs"]["p/m"]["cpt"]
        errs.append(abs(cpt - true_cpt))
    assert all(b <= a + 1e-9 for a, b in zip(errs, errs[1:]))  # monotone
    assert errs[-1] / true_cpt <= 0.15                          # converged


def test_ratchet_step_clamped():
    # A single observation can move the ratio at most +-10% (poison guard).
    ctxbudget.observe("p", "m", 19_000, 1000)          # sample cpt 19
    cpt = ctxbudget.calibration_status()["pairs"]["p/m"]["cpt"]
    assert cpt <= 4.0 * 1.10 + 1e-9
    ctxbudget.observe("p", "m", 600, 1000)             # sample cpt 0.6
    cpt2 = ctxbudget.calibration_status()["pairs"]["p/m"]["cpt"]
    assert cpt2 >= cpt * 0.90 - 1e-9


# ----------------------------------------------------- observe validation ---

@pytest.mark.parametrize("chars,tokens", [
    (100, 1000),        # cpt 0.1 — implausibly low
    (100_000, 1000),    # cpt 100 — implausibly high
    (-100, 50),         # negative chars
    (100, -50),         # negative tokens
    (0, 10),            # zero chars
    (10, 0),            # zero tokens (also div-by-zero guard)
    (4.0, 1),           # non-int float
    ("400", 100),       # non-int str
    (None, 100),        # None
    (400, True),        # bool is not a count
])
def test_observe_discards_garbage(chars, tokens):
    before = len(errors.recent(1000))
    assert ctxbudget.observe("prov", "mod", chars, tokens) is False
    # Nothing written to the calibration table...
    assert "prov/mod" not in ctxbudget.calibration_status()["pairs"]
    # ...and the discard was captured (counted).
    assert len(errors.recent(1000)) == before + 1
    assert ctxbudget.calibration_status()["discarded_observations"] >= 1


def test_observe_survives_malformed_rows_burst():
    # A burst of malformed usage rows never raises and never poisons a
    # previously calibrated pair.
    _calibrate("p", "m", 2.0, n=30)
    cpt_before = ctxbudget.calibration_status()["pairs"]["p/m"]["cpt"]
    for garbage in [(0, 0), (None, None), ("a", "b"), (-1, -1),
                    (10**9, 1), (1, 10**9)]:
        assert ctxbudget.observe("p", "m", *garbage) is False
    assert ctxbudget.calibration_status()["pairs"]["p/m"]["cpt"] == cpt_before


def test_persistence_schema_and_reload():
    _calibrate("openai", "gpt-5", 3.0, n=5)
    path = config.MEMORY_DIR / "ctx_calibration.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["v"] == 1
    row = data["ratios"]["openai/gpt-5"]
    assert row["n"] == 5 and row["est_ver"] == 1
    assert row["first"] <= row["last"]
    assert 0.5 <= row["cpt"] <= 20


# ------------------------------------------------------ corrupt file --------

def test_corrupt_calibration_quarantined_fresh_no_exception():
    path = config.MEMORY_DIR / "ctx_calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json!!", encoding="utf-8")
    # Reading never raises and falls back to the prior...
    assert ctxbudget.estimate_tokens("x" * 400, provider="p", model="m") == 100
    # ...the corrupt artifact is quarantined aside, not repaired in place...
    quarantined = list(path.parent.glob("ctx_calibration.corrupt.*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not json!!"
    assert not path.exists()
    # ...and calibration restarts fresh.
    assert ctxbudget.observe("p", "m", 4000, 1000) is True
    assert ctxbudget.calibration_status()["pairs"]["p/m"]["n"] == 1


def test_corrupt_schema_quarantined():
    path = config.MEMORY_DIR / "ctx_calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"v": 1, "ratios": ["not", "a", "dict"]}),
                    encoding="utf-8")
    assert ctxbudget.estimate_tokens("x" * 40, provider="p", model="m") == 10
    assert list(path.parent.glob("ctx_calibration.corrupt.*.json"))


def test_hand_edited_garbage_row_falls_back_to_prior():
    path = config.MEMORY_DIR / "ctx_calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"v": 1, "ratios": {"p/m": {"cpt": "NaN-ish", "n": 3}}}),
        encoding="utf-8")
    assert ctxbudget.estimate_tokens("x" * 400, provider="p", model="m") == 100
    assert ctxbudget.calibration_status()["pairs"]["p/m"]["label"] == "prior"


# ------------------------------------------------------------- plan() -------

OPUS = "claude-opus-4-8"      # 200k window per config._CONTEXT_WINDOW


def test_plan_fit():
    p = ctxbudget.plan({"system": "s" * 4000, "history": "h" * 4000,
                        "task": "t" * 400},
                       provider="anthropic", model=OPUS)
    assert p.verdict == "fit"
    assert p.window == config.context_window(OPUS) == 200_000
    assert p.output_reserve == min(config.MAX_TOKENS, int(200_000 * 0.20))
    acc = p.accounting
    assert acc["blocks"]["system"] == 1000
    assert acc["provider_overhead"] == 64
    assert acc["total_with_reserve"] == (acc["total_input"]
                                         + acc["output_reserve"])
    assert acc["headroom"] > 0


def test_plan_needs_compaction():
    # Only the history is over — shrinking it to the compacted floor fits.
    p = ctxbudget.plan({"system": 2_000, "history": 190_000, "task": 500},
                       provider="anthropic", model=OPUS)
    assert p.verdict == "needs_compaction"


def test_plan_exceeded_and_context_exceeded_carries_accounting():
    p = ctxbudget.plan({"system": 2_000, "history": 1_000, "task": 250_000},
                       provider="anthropic", model=OPUS)
    assert p.verdict == "exceeded"
    err = ctxbudget.ContextExceeded(accounting=p.accounting)
    assert err.accounting["blocks"]["task"] == 250_000
    assert err.accounting["window"] == 200_000
    assert isinstance(err, Exception)


def test_plan_extreme_tool_schemas_100kb():
    schemas = json.dumps([{"name": f"tool{i}", "schema": "x" * 400}
                          for i in range(240)])
    assert len(schemas) >= 100_000
    p = ctxbudget.plan({"system": "s" * 400, "tool_schemas": schemas,
                        "task": "t" * 400},
                       provider="anthropic", model=OPUS)
    assert p.accounting["blocks"]["tool_schemas"] == len(schemas) // 4
    assert p.verdict == "fit"                      # 25k tokens fits 200k
    # The same schemas blow a small window into needs-nothing-to-compact land.
    p2 = ctxbudget.plan({"tool_schemas": "x" * 600_000},
                        provider="openai", model="gpt-4o")   # 128k window
    assert p2.verdict == "exceeded"


def test_plan_oversized_tool_outputs():
    p = ctxbudget.plan({"system": 1_000, "history": 5_000,
                        "tool_output_allowance": 500_000},
                       provider="anthropic", model=OPUS)
    assert p.verdict == "exceeded"                 # history shrink can't save it


def test_plan_output_reserve_never_negative(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CTX_OUTPUT_RESERVE_FRACTION", "-3")
    p = ctxbudget.plan({"task": 100}, provider="p", model=OPUS)
    assert p.output_reserve == 0 and p.verdict == "fit"


def test_plan_output_reserve_exhaustion(monkeypatch):
    # Reserve eats the window: input that fits without a reserve is flagged.
    monkeypatch.setenv("OLYMPUS_CTX_OUTPUT_RESERVE_FRACTION", "0.5")
    monkeypatch.setattr(config, "MAX_TOKENS", 10**9)   # fraction path wins
    window = config.context_window(OPUS)
    p = ctxbudget.plan({"task": window // 2}, provider="p", model=OPUS)
    assert p.output_reserve == window // 2
    assert p.verdict in ("needs_compaction", "exceeded")   # never "fit"
    assert p.verdict == "exceeded"                         # no history block


def test_plan_repair_reserve_accounting(monkeypatch):
    window = config.context_window(OPUS)
    reserve = min(config.MAX_TOKENS, int(window * 0.20))
    boundary = window - reserve - 64                # exactly fits, repair=0
    p = ctxbudget.plan({"task": boundary}, provider="p", model=OPUS)
    assert p.verdict == "fit"
    monkeypatch.setenv("OLYMPUS_CTX_REPAIR_RESERVE", "1000")
    p2 = ctxbudget.plan({"task": boundary}, provider="p", model=OPUS)
    assert p2.accounting["repair_reserve"] == 1000
    assert p2.verdict != "fit"                      # the reserve is accounted


def test_plan_ic2_property_sweep():
    """I-C2: across 500 seeded random block mixes and windows, plan() never
    says "fit" when estimates + overhead + reserve exceed the window."""
    rng = random.Random(1337)
    models = [OPUS, "gpt-4o", "gemini-2.5-pro", "unknown-model", "kimi-k2"]
    for _ in range(500):
        model = rng.choice(models)
        blocks = {name: rng.randint(0, 300_000)
                  for name in rng.sample(
                      ["system", "history", "task", "tool_schemas",
                       "tool_output_allowance", "other"],
                      rng.randint(1, 6))}
        p = ctxbudget.plan(blocks, provider="p", model=model)
        window = config.context_window(model)
        total = (sum(blocks.values()) + 64
                 + min(config.MAX_TOKENS, int(window * 0.20)))
        if total > window:
            assert p.verdict != "fit", (blocks, model)
        else:
            assert p.verdict == "fit", (blocks, model)


def test_window_for_rides_config_context_window(monkeypatch):
    assert ctxbudget.window_for("openai", "gemini-x") \
        == config.context_window("gemini-x") == 1_000_000
    # Single source of truth: patching config.context_window moves BOTH
    # window_for and plan() — there is no second table to desync.
    monkeypatch.setattr(config, "context_window", lambda m: 4242)
    assert ctxbudget.window_for("openai", "anything") == 4242
    assert ctxbudget.plan({"task": 1}, provider="p", model="m").window == 4242


# ------------------------------------------- orchestrator wiring (I-C1) -----

def _legacy(history):
    return sum(len(str(m.get("content", ""))) for m in history) // 4


SAMPLE_HISTORIES = [
    [],
    [{"role": "user", "content": "hello world"}],
    [{"role": "user", "content": "x" * 4001},
     {"role": "assistant", "content": "y" * 37}],
    [{"role": "user", "content": {"structured": True}},
     {"role": "assistant", "content": 12345}],
    [{"role": "user", "content": "漢字" * 500}],
]


@pytest.mark.parametrize("history", SAMPLE_HISTORIES)
def test_flag_off_byte_identity(monkeypatch, history):
    """I-C1: flag off, orchestrator._estimate_tokens is EXACTLY chars//4 —
    both via the class (tui call shape) and on a constructed Olympus."""
    monkeypatch.delenv("OLYMPUS_CTX_BUDGET", raising=False)
    assert orchestrator.Olympus._estimate_tokens(history) == _legacy(history)
    bot = orchestrator.Olympus(user="ctxb")
    assert bot._estimate_tokens(bot.history + history) \
        == _legacy(bot.history + history)


def test_flag_on_delegates_to_calibrated(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CTX_BUDGET", "1")
    _calibrate("anthropic", "claude-opus-4-8", 2.0, n=50)
    history = [{"role": "user", "content": "x" * 2000}]
    # Explicit pair (the _maybe_compact call shape: pool primary member).
    est = orchestrator.Olympus._estimate_tokens(
        history, "anthropic", "claude-opus-4-8")
    assert est == ctxbudget.estimate_tokens(
        history, provider="anthropic", model="claude-opus-4-8")
    assert est != _legacy(history)                 # calibration took effect
    # Unbound/no-pair call shape (tui) resolves the env-configured model
    # (conftest sets OLYMPUS_MODEL=claude-opus-4-8) and default provider.
    assert orchestrator.Olympus._estimate_tokens(history) == est


# ------------------------------------- compaction-failure trace event -------

def _failing_compaction_bot(monkeypatch):
    from olympus import recall
    bot = orchestrator.Olympus(user="ctxb")
    monkeypatch.setattr(recall, "flush_slice",
                        lambda *a, **k: {})                 # no LLM calls
    monkeypatch.setattr(orchestrator.Olympus, "_ace_compress",
                        lambda self, text: None)            # compaction fails
    monkeypatch.setattr(orchestrator.Olympus, "_legacy_compress",
                        lambda self, text: None)
    bot.history = [{"role": "user", "content": f"turn {i}: " + "x" * 50}
                   for i in range(config.HISTORY_KEEP_TURNS + 5)]
    return bot


def test_compaction_failure_emits_trace_event(monkeypatch):
    """The silent-drop fallback is event-covered, flag-INDEPENDENT (I-C5)."""
    monkeypatch.delenv("OLYMPUS_CTX_BUDGET", raising=False)
    bot = _failing_compaction_bot(monkeypatch)
    dropped = len(bot.history) - config.HISTORY_KEEP_TURNS
    tr = trace.Trace("test", "ctxb")
    token = trace.set_current(tr)
    try:
        bot._compress_history()
    finally:
        trace.reset_current(token)
    events = [e for e in tr.events if e["stage"] == "context.truncated"]
    assert len(events) == 1
    assert events[0]["dropped"] == dropped
    assert events[0]["reason"] == "compaction_failed"
    # The fallback behaviour itself is unchanged: verbatim tail kept.
    assert len(bot.history) == config.HISTORY_KEEP_TURNS


def test_compaction_failure_flag_on_captures_error(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CTX_BUDGET", "1")
    bot = _failing_compaction_bot(monkeypatch)
    before = len(errors.recent(1000))
    tr = trace.Trace("test", "ctxb")
    token = trace.set_current(tr)
    try:
        bot._compress_history()
    finally:
        trace.reset_current(token)
    recs = errors.recent(1000)[before:]
    assert any(r["where"] == "orchestrator.compress_history" for r in recs)
    assert any(e["stage"] == "context.truncated" for e in tr.events)


def test_compaction_failure_without_ambient_trace_is_safe(monkeypatch):
    monkeypatch.delenv("OLYMPUS_CTX_BUDGET", raising=False)
    bot = _failing_compaction_bot(monkeypatch)
    assert trace.current() is None
    bot._compress_history()                        # must not raise
    assert len(bot.history) == config.HISTORY_KEEP_TURNS


# ------------------------------------------------- accuracy benchmark -------

def test_estimator_accuracy_benchmark_calibrated_beats_flat(capsys):
    """A7-style benchmark: mean |relative error| of flat chars//4 vs the
    calibrated estimator on a small multilingual corpus. Reference
    chars-per-token values approximate real tokenizer behaviour; 'true'
    token counts derive from them. Calibrated must improve overall and on
    every non-English pair; numbers are printed for the completion report."""
    corpus = {
        # (provider, model): (reference cpt, sample text)
        ("anthropic", "english-model"): (4.2, (
            "The quick brown fox jumps over the lazy dog. " * 40)),
        ("openai", "code-model"): (3.2, (
            "def handler(req):\n    return {'status': 200, "
            "'body': json.dumps(payload)}\n" * 30)),
        ("openai", "cjk-model"): (1.7, "今日は良い"
                                       "天気ですね" * 120),
        ("openai", "cyrillic-model"): (2.6, (
            "Быстрая лис"
            "а прыгает " * 60)),
    }
    # Calibrate each pair from simulated provider-reported usage.
    for (prov, model), (cpt, _text) in corpus.items():
        _calibrate(prov, model, cpt, n=50, tokens=2000)

    flat_errs, cal_errs, rows = [], [], []
    for (prov, model), (cpt, text) in corpus.items():
        true_tokens = len(text) / cpt
        flat = len(text) // 4
        cal = ctxbudget.estimate_tokens(text, provider=prov, model=model)
        fe = abs(flat - true_tokens) / true_tokens
        ce = abs(cal - true_tokens) / true_tokens
        flat_errs.append(fe)
        cal_errs.append(ce)
        rows.append((f"{prov}/{model}", cpt, fe, ce))
        assert ce <= 0.15                          # I-C4 tolerance per pair
        if abs(cpt - 4.0) > 0.5:                   # non-English-ish pairs
            assert ce < fe                         # calibrated beats flat

    mean_flat = sum(flat_errs) / len(flat_errs)
    mean_cal = sum(cal_errs) / len(cal_errs)
    print("\nestimator accuracy (mean |relative error|):")
    for name, cpt, fe, ce in rows:
        print(f"  {name:28s} ref_cpt={cpt:>4.1f}  "
              f"flat4.0={fe:6.1%}  calibrated={ce:6.1%}")
    print(f"  {'MEAN':28s}              "
          f"flat4.0={mean_flat:6.1%}  calibrated={mean_cal:6.1%}")
    assert mean_cal < mean_flat


def test_plan_overhead_under_1ms():
    import time as _t
    blocks = {"system": "s" * 8000, "history": "h" * 40000, "task": "t" * 2000,
              "tool_schemas": "x" * 20000}
    ctxbudget.plan(blocks, provider="p", model=OPUS)          # warm
    t0 = _t.perf_counter()
    n = 50
    for _ in range(n):
        ctxbudget.plan(blocks, provider="p", model=OPUS)
    per_call = (_t.perf_counter() - t0) / n
    assert per_call < 0.001, f"plan() took {per_call * 1e3:.3f} ms"
