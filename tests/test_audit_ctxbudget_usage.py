"""INDEPENDENT ADVERSARIAL AUDIT of Wave-1 C4 (calibrated context budgeting)
and C5 (prompt-cache telemetry).

Written by an external auditor, NOT the implementers. These probes deliberately
avoid the circularity of the shipped benchmark (which calibrates a pair on the
exact synthetic cpt it then measures against, guaranteeing ~0% error). Instead:

  * Area 1 builds an honest TRAIN/HELD-OUT estimator harness with per-sample cpt
    variance within each content class, calibrates on TRAIN, and reports the
    full error DISTRIBUTION (mean/median/p90/p95/p99/max/n + bootstrap 95% CI)
    on HELD-OUT, broken down by content class. This exposes whether a single
    scalar cpt per (provider,model) generalises or merely memorises the mean.
  * Areas 2-7 stress the invariants I-C1..I-C5 and I-U1..I-U4 adversarially.

No olympus/ source is modified. Real defects are documented, not patched.
"""

import json
import math
import random
import statistics

import anthropic
import pytest

from olympus import (config, ctxbudget, errors, openai_compat, orchestrator,
                     usage)


# ============================================================ stats helpers ==

def _pct(xs, q):
    """Linear-interpolated percentile of a list (q in 0..100)."""
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return float(s[0])
    idx = (q / 100.0) * (len(s) - 1)
    lo = int(math.floor(idx))
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _boot_ci_mean(xs, B=2000, seed=20260726):
    """Bootstrap 95% CI on the mean by resampling with replacement."""
    if not xs:
        return (0.0, 0.0)
    r = random.Random(seed)
    n = len(xs)
    means = []
    for _ in range(B):
        acc = 0.0
        for _ in range(n):
            acc += xs[r.randrange(n)]
        means.append(acc / n)
    return (_pct(means, 2.5), _pct(means, 97.5))


def _summary(errs):
    lo, hi = _boot_ci_mean(errs)
    return {
        "n": len(errs),
        "mean": statistics.fmean(errs),
        "median": statistics.median(errs),
        "p90": _pct(errs, 90),
        "p95": _pct(errs, 95),
        "p99": _pct(errs, 99),
        "max": max(errs),
        "ci_lo": lo,
        "ci_hi": hi,
    }


# ================================================ AREA 1: honest estimator ===

# Content classes with a REALISTIC per-sample cpt distribution. `spread` is the
# within-class relative std-dev — the natural variance a single scalar cpt
# cannot capture. `mixed` is deliberately BIMODAL (English doc + CJK doc under
# one model) to show where scalar calibration structurally fails.
_CLASSES = {
    "english":  {"mean": 4.10, "spread": 0.08, "kind": "unimodal"},
    "code":     {"mean": 3.10, "spread": 0.18, "kind": "unimodal"},
    "cjk":      {"mean": 1.70, "spread": 0.15, "kind": "unimodal"},
    "cyrillic": {"mean": 2.60, "spread": 0.12, "kind": "unimodal"},
    "json":     {"mean": 3.40, "spread": 0.14, "kind": "unimodal"},
    "mixed":    {"modes": [(4.10, 0.06), (1.70, 0.10)], "kind": "bimodal"},
}

_N_TRAIN = 200
_N_TEST = 200


def _draw_cpt(rng, spec):
    if spec["kind"] == "bimodal":
        mu, sd = rng.choice(spec["modes"])
        cpt = rng.gauss(mu, mu * sd)
    else:
        cpt = rng.gauss(spec["mean"], spec["mean"] * spec["spread"])
    # Keep every generated sample inside observe()'s accepted bounds so the
    # harness measures GENERALISATION, not observe's rejection path.
    return min(max(cpt, 0.6), 19.0)


def _gen(rng, spec, n):
    """n (chars, tokens, true_cpt) samples for a content class."""
    out = []
    for _ in range(n):
        cpt = _draw_cpt(rng, spec)
        tokens = rng.randint(400, 3000)
        chars = int(round(cpt * tokens))
        if chars < tokens:            # guard: keep chars/tokens >= ~0.6
            chars = tokens
        out.append((chars, tokens, cpt))
    return out


def test_honest_heldout_estimator_distribution(capsys):
    """C4 headline check. Calibrate on TRAIN, measure on HELD-OUT with genuine
    within-class cpt variance. Reports the full distributional table (verbatim
    into the audit doc) and asserts the honest generalisation story:

      * held-out error is FAR above the circular ~0.1% headline;
      * on non-English classes calibration still beats flat chars//4 on the MEAN;
      * the bimodal 'mixed' class exposes the scalar-cpt structural limit.
    """
    rng = random.Random(424242)
    prov = "audit"
    rows = {}
    for cls, spec in _CLASSES.items():
        model = f"cls-{cls}"
        # --- calibrate on TRAIN ---
        for chars, tokens, _cpt in _gen(rng, spec, _N_TRAIN):
            ctxbudget.observe(prov, model, chars, tokens)
        cal = ctxbudget.calibration_status()["pairs"][f"{prov}/{model}"]["cpt"]
        # --- measure on HELD-OUT ---
        cal_errs, flat_errs = [], []
        for chars, tokens, _cpt in _gen(rng, spec, _N_TEST):
            true = tokens
            est = ctxbudget.estimate_tokens("x" * chars, provider=prov,
                                            model=model)
            flat = chars // 4
            cal_errs.append(abs(est - true) / true)
            flat_errs.append(abs(flat - true) / true)
        target = (spec["mean"] if spec["kind"] == "unimodal"
                  else sum(m for m, _ in spec["modes"]) / len(spec["modes"]))
        rows[cls] = {
            "target_cpt": target, "cal_cpt": cal,
            "cal": _summary(cal_errs), "flat": _summary(flat_errs),
        }

    # ---- distributional table (goes verbatim into the audit doc) ----
    lines = []
    lines.append("HELD-OUT calibrated-estimator ABSOLUTE RELATIVE ERROR by "
                 "content class")
    lines.append(f"(TRAIN n={_N_TRAIN}, HELD-OUT n={_N_TEST} per class; "
                 "bootstrap 95% CI on the mean, B=2000)")
    hdr = (f"{'class':9s} {'tgt_cpt':>7s} {'cal_cpt':>7s} | "
           f"{'mean':>7s} {'95%CI':>15s} {'med':>6s} {'p90':>6s} "
           f"{'p95':>6s} {'p99':>6s} {'max':>7s} | {'flat_mean':>9s}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for cls in _CLASSES:
        r = rows[cls]
        c = r["cal"]
        lines.append(
            f"{cls:9s} {r['target_cpt']:7.2f} {r['cal_cpt']:7.2f} | "
            f"{c['mean']:7.1%} "
            f"[{c['ci_lo']:5.1%},{c['ci_hi']:5.1%}] "
            f"{c['median']:6.1%} {c['p90']:6.1%} {c['p95']:6.1%} "
            f"{c['p99']:6.1%} {c['max']:7.1%} | {r['flat']['mean']:9.1%}")
    table = "\n".join(lines)
    print("\n" + table + "\n")
    # Persist for the auditor to lift verbatim.
    with capsys.disabled():
        pass

    # ---- honest assertions ----
    # 1) The circular ~0.1% headline does NOT survive genuine variance.
    assert rows["english"]["cal"]["mean"] > 0.01, \
        "English held-out error implausibly tiny — variance not modelled?"
    # 2) Calibration converged near each unimodal class mean.
    for cls, spec in _CLASSES.items():
        if spec["kind"] == "unimodal":
            assert abs(rows[cls]["cal_cpt"] - spec["mean"]) / spec["mean"] \
                < 0.10, f"{cls}: calibrated cpt did not converge to the mean"
    # 3) For classes whose true cpt is far from 4.0, calibration beats flat on
    #    the mean (the legitimate win — capturing the mean, not the variance).
    for cls in ("code", "cjk", "cyrillic"):
        assert rows[cls]["cal"]["mean"] < rows[cls]["flat"]["mean"], \
            f"{cls}: calibrated did not beat flat chars//4 on the mean"
    # 4) The bimodal class is materially worse than a clean unimodal one —
    #    a single scalar cpt cannot serve two content modes at once.
    assert rows["mixed"]["cal"]["mean"] > rows["english"]["cal"]["mean"]


# ============================================ AREA 2: I-C2 property sweep =====

def test_ic2_property_sweep_2000():
    """I-C2: across >=2000 seeded random block mixes (incl. 0, huge ints, and
    exactly-at-boundary totals), plan() is 'fit' IFF total+reserve <= window —
    never 'fit' when it overflows, and always 'fit' when it fits."""
    rng = random.Random(0xC2C2)
    models = ["claude-opus-4-8", "gpt-4o", "gpt-5",
              "gemini-2.5-pro", "unknown-xyz", "kimi-k2"]
    names = ["system", "history", "task", "tool_schemas",
             "tool_output_allowance", "other"]
    checked = 0
    for i in range(2000):
        model = rng.choice(models)
        window = config.context_window(model)
        reserve = max(0, min(config.MAX_TOKENS,
                             int(window * ctxbudget.output_reserve_fraction())))
        chosen = rng.sample(names, rng.randint(1, len(names)))
        blocks = {}
        for nm in chosen:
            roll = rng.random()
            if roll < 0.15:
                blocks[nm] = 0
            elif roll < 0.25:
                blocks[nm] = 10 ** rng.randint(15, 25)      # huge ints
            elif roll < 0.45:
                # Aim a single block exactly at the fit boundary.
                blocks[nm] = max(0, window - reserve - 64)
            else:
                blocks[nm] = rng.randint(0, 300_000)
        p = ctxbudget.plan(blocks, provider="p", model=model)
        total = sum(max(0, v) for v in blocks.values()) + 64 + reserve
        if total > window:
            assert p.verdict != "fit", (i, model, blocks, total, window)
        else:
            assert p.verdict == "fit", (i, model, blocks, total, window)
        checked += 1
    assert checked == 2000

    # Explicit exact-boundary triple on a known window.
    OPUS = "claude-opus-4-8"
    win = config.context_window(OPUS)
    res = min(config.MAX_TOKENS, int(win * 0.20))
    at = win - res - 64
    assert ctxbudget.plan({"task": at}, provider="p",
                          model=OPUS).verdict == "fit"
    assert ctxbudget.plan({"task": at + 1}, provider="p",
                          model=OPUS).verdict != "fit"


def test_plan_rejects_bool_block():
    """A bool masquerading as a token count must raise, never be summed as 1."""
    with pytest.raises(TypeError):
        ctxbudget.plan({"task": True}, provider="p", model="claude-opus-4-8")


# =================================== AREA 3: I-C1 flag-off byte identity ======

def _legacy_estimate(history):
    chars = sum(len(str(m.get("content", ""))) for m in history)
    return chars // 4


def _rand_history(rng):
    kinds = ["ascii", "cjk", "cyrillic", "struct", "int", "blocks", "empty"]
    h = []
    for _ in range(rng.randint(0, 8)):
        k = rng.choice(kinds)
        if k == "ascii":
            c = "hello world " * rng.randint(0, 40)
        elif k == "cjk":
            c = "漢字テスト" * rng.randint(0, 40)
        elif k == "cyrillic":
            c = "Привет мир " * rng.randint(0, 40)
        elif k == "struct":
            c = {"nested": [1, 2, {"k": "v" * rng.randint(0, 20)}]}
        elif k == "int":
            c = rng.randint(0, 10 ** 9)
        elif k == "blocks":
            c = [{"type": "text", "text": "t" * rng.randint(0, 30)},
                 "raw" * rng.randint(0, 10)]
        else:
            c = ""
        h.append({"role": rng.choice(["user", "assistant"]), "content": c})
    return h


def test_ic1_flag_off_exact_chars_div_4(monkeypatch):
    """I-C1: with OLYMPUS_CTX_BUDGET off, orchestrator._estimate_tokens is
    EXACTLY chars//4 across 1000 randomised histories — even after a rich
    calibration table exists on disk (which must be ignored when the flag is
    off). Byte-identity, not approximate."""
    monkeypatch.delenv("OLYMPUS_CTX_BUDGET", raising=False)
    # Poison the calibration store to prove the flag-off path never consults it.
    for _ in range(60):
        ctxbudget.observe("anthropic", "claude-opus-4-8", 1800, 1000)  # cpt 1.8
    rng = random.Random(31337)
    for _ in range(1000):
        h = _rand_history(rng)
        got = orchestrator.Olympus._estimate_tokens(
            h, "anthropic", "claude-opus-4-8")
        assert got == _legacy_estimate(h), h
        # Unbound (tui) call shape also stays legacy.
        assert orchestrator.Olympus._estimate_tokens(h) == _legacy_estimate(h)


def test_ic1_flag_on_uses_calibration(monkeypatch):
    """Contrast: flag ON must diverge from legacy once calibrated (so the
    flag-off identity above is meaningful, not a no-op estimator)."""
    monkeypatch.setenv("OLYMPUS_CTX_BUDGET", "1")
    for _ in range(60):
        ctxbudget.observe("anthropic", "claude-opus-4-8", 1800, 1000)
    h = [{"role": "user", "content": "x" * 4000}]
    got = orchestrator.Olympus._estimate_tokens(
        h, "anthropic", "claude-opus-4-8")
    assert got != _legacy_estimate(h)


# ================================= AREA 4: calibration poisoning / bounds =====

def test_poison_out_of_bound_ratios_discarded():
    """Implied cpt 0.01 and 1000 are both rejected + counted, never written."""
    d0 = ctxbudget.calibration_status()["discarded_observations"]
    assert ctxbudget.observe("prov", "mod", 10, 1000) is False       # cpt 0.01
    assert ctxbudget.observe("prov", "mod", 1_000_000, 1000) is False  # cpt 1000
    assert "prov/mod" not in ctxbudget.calibration_status()["pairs"]
    assert ctxbudget.calibration_status()["discarded_observations"] >= d0 + 2


@pytest.mark.parametrize("bad", [
    3.5, "1000", None, True, float("nan"), float("inf"),
])
def test_poison_non_int_types_discarded(bad):
    """NaN/inf/float/str/None/bool are all rejected cleanly (no write, no raise)."""
    assert ctxbudget.observe("p", "m", bad, 1000) is False
    assert ctxbudget.observe("p", "m", 1000, bad) is False
    assert "p/m" not in ctxbudget.calibration_status()["pairs"]


def test_flood_cannot_drive_ratchet_out_of_bounds():
    """A sustained flood of boundary-valid extreme samples moves the EMA toward
    that value but the ratchet stays clamped inside [CPT_MIN, CPT_MAX] — it can
    never be driven to an absurd (out-of-bounds) ratio. Also proves per-key
    isolation: flooding one pair leaves an unrelated pair on the prior."""
    # Flood high (cpt 20 at the upper bound).
    for _ in range(600):
        assert ctxbudget.observe("flood", "hi", 20_000, 1000) is True
    hi = ctxbudget.calibration_status()["pairs"]["flood/hi"]["cpt"]
    assert ctxbudget.CPT_MIN <= hi <= ctxbudget.CPT_MAX
    assert hi == pytest.approx(ctxbudget.CPT_MAX, abs=1e-6)   # converged to cap
    # Flood low (cpt 0.5 at the lower bound).
    for _ in range(600):
        assert ctxbudget.observe("flood", "lo", 500, 1000) is True
    lo = ctxbudget.calibration_status()["pairs"]["flood/lo"]["cpt"]
    assert ctxbudget.CPT_MIN <= lo <= ctxbudget.CPT_MAX
    assert lo == pytest.approx(ctxbudget.CPT_MIN, abs=1e-6)
    # Per-key isolation: an untouched pair is unaffected (still on the prior).
    assert "flood/other" not in ctxbudget.calibration_status()["pairs"]
    assert ctxbudget.estimate_tokens("x" * 400, provider="flood",
                                     model="other") == 100          # chars/4.0


def test_single_sample_step_clamped_to_10pct():
    """Even a valid-but-extreme single sample moves cpt at most +-10% (I-C3)."""
    assert ctxbudget.observe("s", "m", 19_000, 1000) is True   # sample cpt 19
    cpt = ctxbudget.calibration_status()["pairs"]["s/m"]["cpt"]
    assert cpt <= ctxbudget.PRIOR_CPT * 1.10 + 1e-9


def test_observe_huge_int_overflow_is_discarded_not_propagated():
    """FIXED (Wave-1 audit robustness gap): observe() now guards the
    `prompt_chars / reported_input_tokens` division. An astronomically huge int
    whose float ratio would exceed ~1.8e308 is caught and cleanly discarded-and-
    counted (returns False), rather than propagating an OverflowError. Unreachable
    with real prompt sizes, but the discard-and-count contract now holds for all
    numeric inputs."""
    assert ctxbudget.observe("p", "m", 10 ** 400, 1) is False
    # No exception escapes; the sample is simply not recorded.
    assert ctxbudget.observe("p", "m", 10 ** 400, 1) is False


# ================================= AREA 5: corrupt calibration artifact =======

@pytest.mark.parametrize("garbage", [
    "{not json!!",                       # invalid json
    '{"v":1,"ratios":',                  # truncated / partial
    "",                                  # empty file
    '{"v":1,"ratios":[1,2,3]}',          # wrong schema (ratios not a dict)
    '[1,2,3]',                           # top-level not a dict
    "\x00\x01\x02 garbage bytes",        # binary-ish
])
def test_corrupt_calibration_quarantined_fresh_no_exception(garbage):
    """Reject-never-repair: a corrupt/partial calibration file is quarantined
    aside (not patched in place), estimation falls back to the prior WITHOUT
    raising, and calibration restarts fresh and accepts new records."""
    path = config.MEMORY_DIR / "ctx_calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(garbage, encoding="utf-8")
    # Estimation never raises and uses the prior (chars/4.0).
    assert ctxbudget.estimate_tokens("x" * 400, provider="p", model="m") == 100
    # The corrupt artifact is moved aside verbatim, original gone.
    q = list(path.parent.glob("ctx_calibration.corrupt.*.json"))
    assert len(q) >= 1
    assert any(f.read_text(encoding="utf-8") == garbage for f in q)
    # Fresh start: a valid observation is accepted and persisted.
    assert ctxbudget.observe("p", "m", 4000, 1000) is True
    assert ctxbudget.calibration_status()["pairs"]["p/m"]["n"] == 1


def test_hand_edited_bad_row_falls_back_without_quarantine():
    """A structurally-valid file with a poisoned ROW (non-numeric cpt) is NOT
    quarantined (the file parses) but the bad row is defensively ignored and the
    pair reads as 'prior' — a hand edit can never poison estimates."""
    path = config.MEMORY_DIR / "ctx_calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"v": 1, "ratios": {"p/m": {"cpt": "not-a-number", "n": 5}}}),
        encoding="utf-8")
    assert ctxbudget.estimate_tokens("x" * 400, provider="p", model="m") == 100
    assert ctxbudget.calibration_status()["pairs"]["p/m"]["label"] == "prior"
    assert not list(path.parent.glob("ctx_calibration.corrupt.*.json"))


# ============================== AREA 6: cache telemetry correctness (C5) ======

def _anthropic_msg(in_toks, cr, cc, out=5):
    return anthropic.types.Message.model_validate({
        "id": "m", "type": "message", "role": "assistant",
        "model": "claude-test", "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": in_toks, "output_tokens": out,
                  "cache_read_input_tokens": cr,
                  "cache_creation_input_tokens": cc},
    })


@pytest.mark.parametrize("in_toks,cr,cc", [
    (100, 400, 300), (0, 0, 0), (1, 0, 0), (0, 500, 0), (0, 0, 700),
    (12345, 6789, 1011), (1, 1, 1), (999999, 1, 2),
])
def test_iu2_split_equals_provider_total(in_toks, cr, cc):
    """I-U2: recorded in + cache_read + cache_creation == the provider-reported
    total input, across varied fake Anthropic usage blocks; in == UNCACHED."""
    usage.record("claude-test", in_toks, 5, cache_read=cr, cache_creation=cc,
                 provider="anthropic", prefix_fp="fp0000000001")
    day = config.MEMORY_DIR / "usage" / \
        f"{__import__('time').strftime('%Y-%m-%d')}.json"
    row = usage.ledger()["model:claude-test"]
    assert row["in"] == in_toks
    assert row["cache_read"] == cr
    assert row["cache_creation"] == cc
    provider_total = in_toks + cr + cc
    assert row["in"] + row["cache_read"] + row["cache_creation"] \
        == provider_total


def test_iu1_positional_record_byte_identity():
    """I-U1: a legacy positional record() (no cache kwargs) produces exactly the
    pre-C5 row shape/values; cache keys are additively zero, cost is unchanged."""
    from olympus import memory
    memory.set_user("iu1")
    try:
        usage.record("claude-x", 1000, 200)
    finally:
        memory.set_user("shared")
    day = config.MEMORY_DIR / "usage" / \
        f"{__import__('time').strftime('%Y-%m-%d')}.json"
    row = usage.ledger()["model:claude-x"]
    exp_cost = round((1000 * 1.0 + 200 * 3.0) / 1e6, 6)   # DEFAULT_PRICE
    assert row["calls"] == 1 and row["in"] == 1000 and row["out"] == 200
    assert row["cost"] == exp_cost
    assert row["cache_read"] == 0 and row["cache_creation"] == 0
    # estimate_cost with zero cache fields == pre-C5 arithmetic.
    assert usage.estimate_cost("claude-x", 1000, 200) == exp_cost
    # No prefix key created when prefix_fp omitted.
    assert "prefix" not in usage.ledger()


def test_verdict_active_inert_no_signal():
    day = __import__('time').strftime('%Y-%m-%d')
    base = config.MEMORY_DIR / "usage"
    base.mkdir(parents=True, exist_ok=True)
    # no_signal: nothing recorded.
    assert usage.cache_stats(7)["verdict"] == "no_signal"
    # inert: >=20 fp calls, zero cache reads.
    (base / f"{day}.json").write_text(json.dumps({
        "model:m": {"calls": 25, "in": 250, "out": 25, "cost": 0.0,
                    "cache_read": 0, "cache_creation": 0},
        "prefix": {"aaaaaaaaaaaa": {"calls": 25, "hits": 0, "cache_read": 0}},
    }))
    s = usage.cache_stats(7)
    assert s["verdict"] == "inert" and s["fp_calls"] == 25
    # active: any cache_read > 0.
    (base / f"{day}.json").write_text(json.dumps({
        "model:m": {"calls": 5, "in": 50, "out": 5, "cost": 0.0,
                    "cache_read": 500, "cache_creation": 100},
        "prefix": {"bbbbbbbbbbbb": {"calls": 5, "hits": 4, "cache_read": 500}},
    }))
    s = usage.cache_stats(7)
    assert s["verdict"] == "active"
    assert s["hit_rate"] == pytest.approx(0.8)
    assert s["savings_usd"] == pytest.approx(500 * 1.0 * 0.9 / 1e6)


def test_openai_cached_tokens_without_fingerprint_is_active():
    """The OpenAI-compat seam records cache_read but NO prefix_fp. cache_stats
    must still read 'active' from cache_read alone (fp_calls == 0), never fall
    through to 'no_signal'. This pins the fingerprint-less cache-read path."""
    day = __import__('time').strftime('%Y-%m-%d')
    base = config.MEMORY_DIR / "usage"
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{day}.json").write_text(json.dumps({
        "model:gpt-x": {"calls": 3, "in": 30, "out": 3, "cost": 0.0,
                        "cache_read": 900, "cache_creation": 0},
        # deliberately NO "prefix" key (openai path passes no prefix_fp)
    }))
    s = usage.cache_stats(7)
    assert s["fp_calls"] == 0 and s["by_fp"] == {}
    assert s["cache_read"] == 900
    assert s["verdict"] == "active"


def test_openai_compat_records_cached_split(monkeypatch):
    """End-to-end openai_compat: prompt_tokens_details.cached_tokens splits into
    (uncached in, cache_read), and observe() is fed the FULL prompt total."""
    body = {"choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10,
                      "prompt_tokens_details": {"cached_tokens": 900}}}

    class _Resp:
        def read(self): return json.dumps(body).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(openai_compat.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp())
    seen = []
    monkeypatch.setattr(ctxbudget, "observe", lambda *a, **k: seen.append(a))
    settings = config.Settings(provider="openai", model="gpt-x",
                               api_key="sk-t")
    out = openai_compat.complete_text(settings, "sys",
                                      [{"role": "user", "content": "q"}])
    assert out == "ok"
    day = config.MEMORY_DIR / "usage" / \
        f"{__import__('time').strftime('%Y-%m-%d')}.json"
    row = usage.ledger()["model:gpt-x"]
    assert row["in"] == 100 and row["cache_read"] == 900
    assert row["cache_creation"] == 0
    assert seen == [("openai-compat", "gpt-x", len("sys") + len("q"), 1000)]


def test_openai_cached_tokens_clamped_to_prompt(monkeypatch):
    """Defensive: a provider reporting cached_tokens > prompt_tokens must not
    yield a NEGATIVE uncached count — it's clamped so in >= 0."""
    body = {"choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 1,
                      "prompt_tokens_details": {"cached_tokens": 999}}}

    class _Resp:
        def read(self): return json.dumps(body).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(openai_compat.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp())
    settings = config.Settings(provider="openai", model="gpt-clamp",
                               api_key="sk-t")
    openai_compat.complete_text(settings, "s", [{"role": "user",
                                                 "content": "q"}])
    day = config.MEMORY_DIR / "usage" / \
        f"{__import__('time').strftime('%Y-%m-%d')}.json"
    row = usage.ledger()["model:gpt-clamp"]
    assert row["in"] == 0 and row["cache_read"] == 100     # clamped to prompt


def test_fingerprint_stable_and_sensitive():
    from olympus import llm
    import hashlib
    a = "You are Zeus.\n\nRules:\n- be brief"
    assert llm._prefix_fp(a) == llm._prefix_fp(a)                 # stable
    assert llm._prefix_fp(a) == hashlib.sha256(a.encode()).hexdigest()[:12]
    assert len(llm._prefix_fp(a)) == 12
    assert llm._prefix_fp(a) != llm._prefix_fp(a + " ")          # 1-char edit
    assert llm._prefix_fp("") == hashlib.sha256(b"").hexdigest()[:12]  # no crash


def test_cost_math_multipliers_and_env_overrides(monkeypatch):
    price_in = usage.PRICES["claude-opus-4-8"][0]      # 5.0
    price_out = usage.PRICES["claude-opus-4-8"][1]     # 25.0
    # Default multipliers 0.1 (read) / 1.25 (write).
    assert usage.estimate_cost("claude-opus-4-8", 0, 0, cache_read=1000) \
        == pytest.approx(1000 * price_in * 0.1 / 1e6)
    assert usage.estimate_cost("claude-opus-4-8", 0, 0, cache_creation=1000) \
        == pytest.approx(1000 * price_in * 1.25 / 1e6)
    # Combined call.
    assert usage.estimate_cost("claude-opus-4-8", 100, 50, cache_read=200,
                               cache_creation=300) == pytest.approx(
        (100 * price_in + 50 * price_out + 200 * price_in * 0.1
         + 300 * price_in * 1.25) / 1e6)
    # Env overrides.
    monkeypatch.setenv("OLYMPUS_CACHE_READ_MULT", "0.5")
    monkeypatch.setenv("OLYMPUS_CACHE_WRITE_MULT", "2.0")
    assert usage.estimate_cost("claude-opus-4-8", 0, 0, cache_read=1000) \
        == pytest.approx(1000 * price_in * 0.5 / 1e6)
    assert usage.estimate_cost("claude-opus-4-8", 0, 0, cache_creation=1000) \
        == pytest.approx(1000 * price_in * 2.0 / 1e6)
    # Malformed env -> documented defaults, never a crash.
    monkeypatch.setenv("OLYMPUS_CACHE_READ_MULT", "garbage")
    monkeypatch.setenv("OLYMPUS_CACHE_WRITE_MULT", "")
    assert usage.cache_read_mult() == 0.1
    assert usage.cache_write_mult() == 1.25
    # Negative multiplier floored at 0 (no negative cost).
    monkeypatch.setenv("OLYMPUS_CACHE_READ_MULT", "-5")
    assert usage.cache_read_mult() == 0.0


def test_iu3_no_ttl_constants_in_usage():
    """I-U3: the substring 'ttl' appears NOWHERE in usage.py (pricing multipliers
    are never tied to a cache-lifetime constant)."""
    from pathlib import Path
    src = Path(usage.__file__).read_text(encoding="utf-8")
    assert "ttl" not in src.lower()


# ============================ AREA 7: old-format migration ====================

def test_old_format_day_file_loads_reports_and_accepts_new_records():
    """A pre-C5 day file (rows WITHOUT cache keys) must load, report, feed
    cache_stats as no_signal, and accept a new cache-aware record without error
    or migration."""
    import time as _t
    base = config.MEMORY_DIR / "usage"
    base.mkdir(parents=True, exist_ok=True)
    # An OLD ledger from a prior day (no cache keys anywhere).
    (base / "2026-07-20.json").write_text(json.dumps({
        "__all__": {"calls": 3, "in": 300, "out": 30, "cost": 0.05},
        "model:claude-old": {"calls": 3, "in": 300, "out": 30, "cost": 0.05},
        "user:shared": {"calls": 3, "in": 300, "out": 30, "cost": 0.05},
    }))
    # report() reads it fine.
    rep = usage.report(7)
    assert "2026-07-20" in rep and "$0.0500" in rep
    # cache_stats treats a cache-less old file as no_signal, no crash.
    s = usage.cache_stats(7)
    assert s["verdict"] == "no_signal"
    assert s["cache_read"] == 0 and s["by_fp"] == {}
    # A NEW cache-aware record onto TODAY's (also old-format) file migrates
    # additively: old keys preserved, new keys appear.
    today = _t.strftime("%Y-%m-%d")
    (base / f"{today}.json").write_text(json.dumps({
        "__all__": {"calls": 1, "in": 10, "out": 1, "cost": 0.0}}))
    usage.record("m2", 5, 1, cache_read=7, cache_creation=2, provider="x",
                 prefix_fp="feed0000cafe")
    # THE MIGRATION SEAM, and W2-2 makes it stronger rather than weaker:
    # the seeded old-format file is now the SNAPSHOT base and the new
    # record is a journal append, so this exercises exactly the
    # old-format + new-format boundary. Read through the one reader --
    # parsing the snapshot alone would miss the append and under-report,
    # which is the direction that costs money.
    ledger = usage.ledger(today)
    assert ledger["__all__"]["calls"] == 2          # old row incremented
    assert ledger["__all__"]["in"] == 15
    assert ledger["__all__"]["cache_read"] == 7     # new key added
    assert ledger["__all__"]["cache_creation"] == 2
    assert ledger["prefix"]["feed0000cafe"] == {
        "calls": 1, "hits": 1, "cache_read": 7}
    # And cache_stats now reads active off the migrated file.
    assert usage.cache_stats(7)["verdict"] == "active"
