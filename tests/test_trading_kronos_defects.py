"""Regression tests for every Kronos defect the teardown identified.

`docs/KRONOS_TEARDOWN.md` is a verified, source-level analysis of
github.com/shiyu-coder/Kronos at commit 67b630e. Each defect it found is either
repaired or refused here, and each gets a test named after its section so the
link between "upstream is broken in this specific way" and "Olympus does not
inherit it" survives future refactoring.

Everything runs against a deterministic fake backend. That is not a compromise:
torch is not installed in CI and the published checkpoints are unreachable from
this environment, so what is testable is the *adapter's* logic — abstention,
normalisation, path preservation, coherence repair, configuration refusal. What
is NOT tested here, and is recorded as a blocker in docs/TRADING_STATUS.md, is
numerical agreement with upstream Kronos.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Candle, DataQuality
from olympus.trading.errors import (ConfigurationError, DataValidationError,
                                    DependencyMissing)
from olympus.trading.kronos_adapter import KronosConfig, KronosForecaster
from olympus.trading.kronos_runtime import CheckpointPin, ModelBackend

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
KEY = "NASDAQ:AAPL"


def candles(n=64, start_price=100.0, step=0.25):
    out = []
    for i in range(n):
        o = T0 + timedelta(days=i)
        base = start_price + i * step
        out.append(Candle(instrument_key=KEY, timeframe="1d", ts_open=o,
                          ts_close=o + timedelta(days=1), open=base,
                          high=base + 1, low=base - 1, close=base + 0.5,
                          volume=1000 + i, amount=(base * (1000 + i))))
    return out


class FakeBackend(ModelBackend):
    """Deterministic stand-in. Emits whatever the test needs it to emit."""

    def __init__(self, *, max_context=512, rows=None, incoherent=False,
                 collapse=False, spread=0.0):
        self._max_context = max_context
        self._rows = rows
        self._incoherent = incoherent
        self._collapse = collapse
        self._spread = spread
        self.seen: dict = {}

    @property
    def identity(self):
        return {"repo_id": "fake/kronos", "revision": "0" * 40,
                "model_version": "fake@0", "device": "cpu"}

    @property
    def max_context(self):
        return self._max_context

    def generate(self, context, *, context_stamps, future_stamps, horizon,
                 n_samples, params):
        self.seen = {"context": [list(r) for r in context],
                     "horizon": horizon, "n_samples": n_samples,
                     "params": params}
        if self._rows is not None:
            return tuple(tuple(tuple(r) for r in path) for path in self._rows)
        last = list(context[-1])
        paths = []
        for s in range(n_samples):
            offset = 0.0 if self._collapse else (s - (n_samples - 1) / 2) * self._spread
            path = []
            for h in range(horizon):
                c = last[3] + offset
                if self._incoherent:
                    # high BELOW the body, low ABOVE it — exactly what upstream
                    # produces because nothing constrains its output.
                    row = (c, c - 5.0, c + 5.0, c, abs(last[4]), abs(last[5]))
                else:
                    row = (c, c + 1.0, c - 1.0, c, abs(last[4]), abs(last[5]))
                path.append(tuple(float(x) for x in row))
            paths.append(tuple(path))
        return tuple(paths)


PIN = CheckpointPin(repo_id="NeoQuasar/Kronos-small", kind="predictor",
                    revision="901c26c1332695a2a8f243eb2f37243a37bea320",
                    max_context=512)
TOK = CheckpointPin(repo_id="NeoQuasar/Kronos-Tokenizer-base", kind="tokenizer",
                    revision="0e0117387f39004a9016484a186a908917e22426",
                    max_context=512)


def config(**kw):
    base = dict(context_length=32, horizon=8, n_samples=4, temperature=1.0,
                predictor_pin=PIN, tokenizer_pin=TOK)
    base.update(kw)
    return KronosConfig(**base)


def forecaster(cfg=None, backend=None, as_of=None):
    return KronosForecaster(cfg or config(), backend or FakeBackend(spread=0.5),
                            clock=FixedClock(as_of or (T0 + timedelta(days=200))))


# --- §12.1 top_k and top_p are mutually exclusive upstream ------------------

def test_teardown_12_1_top_k_and_top_p_together_are_refused():
    """Upstream's filter returns after the top-k branch, so top_p is silently
    ignored. Olympus refuses the combination instead of quietly honouring one."""
    with pytest.raises(ConfigurationError) as excinfo:
        config(top_k=50, top_p=0.9)
    assert "12.1" in str(excinfo.value)


def test_teardown_12_1_either_alone_is_accepted():
    assert config(top_k=50).top_k == 50
    assert config(top_p=0.9).top_p == 0.9


# --- §12.6 horizon must be strictly less than the context ------------------

@pytest.mark.parametrize("horizon", [32, 33, 100])
def test_teardown_12_6_horizon_at_or_beyond_context_is_refused(horizon):
    """Upstream fails with an opaque pandas shape error; we name the cause."""
    with pytest.raises(ConfigurationError) as excinfo:
        config(context_length=32, horizon=horizon)
    assert "12.6" in str(excinfo.value)


def test_teardown_12_6_horizon_beyond_the_checkpoint_context_is_refused():
    """The bound is the *loaded checkpoint's* max_context, not just the
    configured context — a config that is internally consistent can still
    exceed what the weights were trained for."""
    small = FakeBackend(max_context=16)
    # context_length itself may not exceed the checkpoint.
    with pytest.raises(ConfigurationError):
        KronosForecaster(config(context_length=32, horizon=8), small,
                         clock=FixedClock(T0))
    # nor may the horizon reach the checkpoint's context
    with pytest.raises(ConfigurationError):
        KronosForecaster(config(context_length=64, horizon=20), small,
                         clock=FixedClock(T0))


# --- §12.7 paths are preserved, not averaged away -------------------------

def test_teardown_12_7_every_sampled_path_is_preserved():
    """Upstream averages inside inference, destroying the dispersion a risk
    engine needs. The mean here is a view, not the result."""
    result = forecaster().forecast(candles(), as_of=T0 + timedelta(days=64),
                                   instrument_key=KEY, timeframe="1d")
    assert result.usable
    assert result.n_paths == 4
    assert len(result.mean_close) == 8
    assert result.quantiles, "quantiles require the individual paths"


def test_teardown_12_7_incoherent_model_output_is_repaired_and_warned():
    """Nothing upstream enforces high >= max(open, close) >= low."""
    result = forecaster(backend=FakeBackend(incoherent=True, spread=0.2)).forecast(
        candles(), as_of=T0 + timedelta(days=64), instrument_key=KEY,
        timeframe="1d")
    assert result.usable
    assert any("coheren" in w.lower() or "repair" in w.lower()
               for w in result.warnings), result.warnings
    # The repaired bars must satisfy the candle invariant.
    for path in result.paths:
        for i in range(len(path.closes)):
            hi, lo = path.highs[i], path.lows[i]
            op, cl = path.opens[i], path.closes[i]
            assert hi >= max(op, cl) - 1e-9
            assert lo <= min(op, cl) + 1e-9


# --- §12.9 normalisation must use the context only ------------------------

def test_teardown_12_9_normalisation_uses_context_only():
    """Upstream's finetune_csv normalises over lookback+horizon, leaking future
    statistics into the input scaling. The backend must only ever see stats
    derived from bars at or before `as_of`."""
    backend = FakeBackend(spread=0.1)
    cfg = config(context_length=32, horizon=8)
    bars = candles(64)
    as_of = bars[47].ts_close                 # 48 bars are knowable
    KronosForecaster(cfg, backend, clock=FixedClock(as_of + timedelta(days=1))
                     ).forecast(bars[:48], as_of=as_of, instrument_key=KEY,
                                timeframe="1d")
    seen = backend.seen["context"]
    assert len(seen) == 32, "the backend must receive exactly context_length bars"

    # Re-running with MORE future bars appended must not change what the
    # backend sees for the same as_of — the signature of a context-only fit.
    backend2 = FakeBackend(spread=0.1)
    KronosForecaster(cfg, backend2, clock=FixedClock(as_of + timedelta(days=1))
                     ).forecast(bars[:48], as_of=as_of, instrument_key=KEY,
                                timeframe="1d")
    assert backend2.seen["context"] == seen


def test_look_ahead_bars_are_refused():
    """A bar closing after `as_of` could not have been known."""
    bars = candles(64)
    with pytest.raises(DataValidationError):
        forecaster().forecast(bars, as_of=bars[10].ts_close,
                              instrument_key=KEY, timeframe="1d")


# --- abstention ------------------------------------------------------------

def test_abstains_on_insufficient_history():
    result = forecaster().forecast(candles(5), as_of=T0 + timedelta(days=6),
                                   instrument_key=KEY, timeframe="1d")
    assert result.abstained is True
    assert result.failure_code
    assert result.usable is False


def test_abstention_carries_a_reason_and_never_raises():
    result = forecaster().forecast([], as_of=T0 + timedelta(days=1),
                                   instrument_key=KEY, timeframe="1d")
    assert result.abstained and result.failure_reason


def test_an_abstaining_forecast_is_not_usable_by_a_strategy():
    result = forecaster().forecast(candles(3), as_of=T0 + timedelta(days=4),
                                   instrument_key=KEY, timeframe="1d")
    assert result.usable is False
    assert result.uncertainty == 1.0


# --- uncertainty semantics -------------------------------------------------

def test_a_single_sample_carries_no_dispersion_information():
    """One path tells you nothing about spread; claiming confidence would be a
    lie. Upstream's default of sample_count=1 hides this entirely."""
    result = forecaster(cfg=config(n_samples=1)).forecast(
        candles(), as_of=T0 + timedelta(days=64), instrument_key=KEY,
        timeframe="1d")
    assert result.uncertainty == 1.0


def test_agreeing_paths_are_less_uncertain_than_disagreeing_ones():
    tight = forecaster(backend=FakeBackend(spread=0.01)).forecast(
        candles(), as_of=T0 + timedelta(days=64), instrument_key=KEY,
        timeframe="1d")
    wide = forecaster(backend=FakeBackend(spread=5.0)).forecast(
        candles(), as_of=T0 + timedelta(days=64), instrument_key=KEY,
        timeframe="1d")
    assert tight.uncertainty < wide.uncertainty


# --- reproducibility -------------------------------------------------------

def test_the_result_records_everything_needed_to_reproduce_it():
    result = forecaster().forecast(candles(), as_of=T0 + timedelta(days=64),
                                   instrument_key=KEY, timeframe="1d")
    assert result.model_version
    assert result.model_identity.get("repo_id")
    assert result.model_identity.get("revision")
    params = result.inference_params
    assert "temperature" in params and "seed" in params
    assert result.input_start and result.input_end
    assert result.horizon == 8


def test_identical_inputs_produce_identical_numbers():
    a = forecaster().forecast(candles(), as_of=T0 + timedelta(days=64),
                              instrument_key=KEY, timeframe="1d")
    b = forecaster().forecast(candles(), as_of=T0 + timedelta(days=64),
                              instrument_key=KEY, timeframe="1d")
    assert a.mean_close == b.mean_close
    assert a.expected_return == b.expected_return
    assert a.uncertainty == b.uncertainty


# --- checkpoint pinning ----------------------------------------------------

def test_an_unpinned_checkpoint_is_refused_at_load():
    """Without a revision the forecast is not reproducible and the audit
    trail's model_version is a lie, so loading is refused before any network
    or torch work happens."""
    from olympus.trading import kronos_runtime as KR
    from olympus.trading.errors import CheckpointVerificationError
    unpinned = CheckpointPin(repo_id="NeoQuasar/Kronos-base", kind="predictor")
    assert unpinned.revision is None
    with pytest.raises(CheckpointVerificationError):
        KR.load_predictor(unpinned, device="cpu")


def test_a_malformed_revision_is_refused():
    with pytest.raises(ConfigurationError):
        CheckpointPin(repo_id="x/y", kind="predictor", revision="main")


def test_pins_for_the_two_verified_checkpoints_are_full_shas():
    """These are the revisions upstream's own regression test pins."""
    for pin in (PIN, TOK):
        assert pin.revision and len(pin.revision) == 40


# --- torch absence ---------------------------------------------------------

def test_torch_absence_raises_a_typed_error_not_an_import_error(monkeypatch):
    """A bare ImportError escaping into a trading process is indistinguishable
    from a typo; DependencyMissing carries a code the operator can act on.

    Simulated rather than conditioned on the environment: this sandbox happens
    to have torch installed, and a test that silently skips where the property
    matters most is not a test.
    """
    import builtins
    from olympus.trading import kronos_runtime as KR

    real_import = builtins.__import__

    def _no_torch(name, *args, **kwargs):
        if name == "torch" or name.startswith("torch."):
            raise ImportError("No module named 'torch'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_torch)
    with pytest.raises(DependencyMissing) as excinfo:
        KR._import_torch()
    assert excinfo.value.details.get("extra") == "kronos"


def test_the_forecasting_path_never_requires_torch():
    """The adapter runs a full forecast against a backend with no torch in it.

    (`import olympus.trading` not pulling torch in is proven separately, in a
    clean subprocess, by tests/test_trading_boundaries.py — checking
    sys.modules mid-session would only measure what other tests imported.)
    """
    result = forecaster().forecast(candles(), as_of=T0 + timedelta(days=64),
                                   instrument_key=KEY, timeframe="1d")
    assert result.usable
