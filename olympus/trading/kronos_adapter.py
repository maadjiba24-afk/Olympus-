"""Olympus's forecasting interface over Kronos.

This is the *only* place Kronos touches the trading domain. It consumes
`Candle`s and produces exactly one `ForecastResult`; it cannot produce a
`Signal`, a `TradeIntent`, or an `Order`, and it imports nothing from the
execution side of the package. A forecasting model that can reach an order
router is a forecasting model that can lose money without passing risk, so the
separation is structural rather than a convention.

The contract with the caller
----------------------------
`forecast()` returns a result or abstains. It never raises for bad *data* —
stale, short, incomplete and malformed windows all produce
`ForecastResult.abstain(...)` with a stable `failure_code`, because refusing to
forecast is a normal outcome that belongs in the audit trail next to the
forecasts. It *does* raise for bad *wiring*: a look-ahead violation, an
unsupported configuration, or a backend that breaks its own output contract are
programming errors, and swallowing those would be the silent-corruption
behaviour this package exists to prevent.

Defects repaired here (docs/KRONOS_TEARDOWN.md §12)
---------------------------------------------------
**§12.6 — `pred_len >= max_context` fails with an opaque pandas shape error.**
`KronosConfig` rejects `horizon >= context_length` and `horizon >= max_context`
at construction, naming both numbers.

**§12.1 — `top_k` and `top_p` are mutually exclusive.** Upstream's filter
returns straight after the top-k branch (`model/kronos.py:347-352`), so `top_p`
is silently ignored whenever `top_k > 0`, contradicting its own docstring. We
take the *reject* option: a config setting both raises `ConfigurationError`. A
sampling policy the operator wrote and the model ignored is a policy that
exists only on paper, and a loud refusal is better than either silently
dropping one or quietly changing the semantics of an existing config.
(`TorchKronosBackend` also composes the two correctly, so if the policy is ever
relaxed the behaviour is defined rather than accidental.)

**§12.9 — normalisation leaks the horizon.** Upstream's `finetune_csv`
computes mean/std over `lookback + predict + 1` bars
(`finetune_base_model.py:125-127`), leaking future statistics into the input
scaling; `finetune/dataset.py` was fixed for exactly this and the fix was never
ported. Here, instance normalisation is computed over the **context window
only** — the last `context_length` validated bars — and nothing else in the
input can influence it. `test_teardown_12_9_normalisation_uses_context_only`
pins that behaviour: extra older history changes nothing.

**§12.7 — sampled paths are averaged inside inference.** Upstream collapses
`sample_count` draws to a mean before returning (`model/kronos.py:467`). Every
path is kept here; the mean, median and quantiles are *views* computed on top.
Without the paths there is no dispersion, and without dispersion there is no
honest uncertainty number.

**§12.7 — outputs carry no structural constraints.** Nothing upstream enforces
`high >= max(open, close)`, `low <= min(open, close)`, or `volume >= 0`, and
averaging paths can break coherence even when every path is individually valid.
Each predicted bar is repaired here, per path, before any aggregation — which
also makes the aggregate coherent for free: element-wise means and order
statistics both preserve pointwise domination, so if every path satisfies
`high >= max(open, close)` then so does their mean and so does any quantile.

Uncertainty
-----------
`uncertainty` is a monotone function of how much the sampled paths disagree,
measured against how much the instrument *already* moves:

    ratio       = stdev(terminal return across paths) / (realised_vol * sqrt(H))
    uncertainty = ratio / (1 + ratio)

Paths in perfect agreement give 0; disagreement equal to a naive random-walk
scale gives 0.5; unbounded disagreement approaches 1. Scaling by realised
volatility is what makes the number comparable across a stablecoin and a
small-cap: 2% dispersion means something different for each.

With `n_samples == 1` it is **1.0, always**. One path carries no dispersion
information whatsoever, and reporting anything lower would be inventing
confidence out of an empty sample — the exact failure mode of upstream's
default `sample_count=1` (§14.2), where users compare runs and see different
answers with no warning.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from .clock import Clock, default_clock
from .contracts import (
    Candle,
    DataQuality,
    ForecastPath,
    ForecastResult,
    ensure_utc,
    jsonable,
)
from .errors import (
    ConfigurationError,
    DataValidationError,
    ForecastError,
    MarketDataError,
)
from .instruments import parse_timeframe
from .kronos_runtime import (
    FEATURE_COLUMNS,
    CheckpointPin,
    ModelBackend,
    SamplingParams,
    calendar_stamps,
    normalise_device,
)
from .validate import (
    DEFAULT_MIN_COMPLETENESS,
    assert_no_lookahead,
    normalise_candles,
    require_usable,
)

#: Matches upstream's `x_std + 1e-5` guard (`model/kronos.py:546`). Kept
#: identical rather than "improved" because the pretrained weights saw inputs
#: scaled exactly this way; a different epsilon is a different input
#: distribution on a near-constant column.
_STD_EPSILON = 1e-5

#: Floor applied to a predicted price. A non-positive price is meaningless, and
#: the alternative — propagating it — puts a negative number into a return
#: calculation and out the other side as a plausible-looking signal.
_MIN_PRICE = 1e-9

#: Below this magnitude a terminal return counts as "flat" for the direction
#: histogram: it is numerically indistinguishable from the last close.
_FLAT_EPSILON = 1e-12

_OPEN, _HIGH, _LOW, _CLOSE, _VOLUME, _AMOUNT = range(6)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KronosConfig:
    """Every knob that changes a forecast, validated at construction.

    Validation is eager and total: an unsupported combination raises here, in
    the operator's process, rather than five hundred bars later inside a
    forward pass. Each rule below either encodes a hard property of the
    checkpoint or repairs a teardown defect, and the error message names the
    numbers involved — upstream's equivalent failure is
    ``ValueError: Shape of passed values is (16, 6), indices imply (20, 6)``,
    which tells the operator nothing about `pred_len` or `max_context`.
    """
    context_length: int = 512
    horizon: int = 24
    n_samples: int = 30
    temperature: float = 1.0
    #: Nucleus threshold. Mutually exclusive with `top_k` — see §12.1 above.
    top_p: float | None = None
    top_k: int | None = None
    seed: int = 0
    #: Post-normalisation clamp, in standard deviations. Upstream default is 5.
    clip: float = 5.0
    device: str | None = None
    tokenizer_pin: CheckpointPin | None = None
    predictor_pin: CheckpointPin | None = None
    #: Freshness tolerance for the newest bar, in seconds. None disables the
    #: check — appropriate for a backtest, never for live.
    max_age_s: float | None = None
    min_completeness: float = DEFAULT_MIN_COMPLETENESS
    quantiles: tuple[float, ...] = (0.05, 0.25, 0.5, 0.75, 0.95)
    #: Abstain when the computed uncertainty exceeds this. None disables the
    #: gate; the caller then has to read `uncertainty` itself.
    abstain_uncertainty_above: float | None = None

    def __post_init__(self):
        for name in ("context_length", "horizon", "n_samples"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigurationError(f"{name} must be an int", got=repr(value))
            object.__setattr__(self, name, int(value))

        # Two returns are the minimum for any dispersion statistic; a shorter
        # context cannot produce a realised volatility to scale against.
        if self.context_length < 3:
            raise ConfigurationError(
                "context_length must be >= 3", got=self.context_length)
        if self.horizon < 1:
            raise ConfigurationError("horizon must be >= 1", got=self.horizon)
        if self.n_samples < 1:
            raise ConfigurationError("n_samples must be >= 1", got=self.n_samples)

        # --- teardown 12.6 -------------------------------------------------
        # Upstream's final decode window is capped at max_context and then
        # sliced by -pred_len, so a horizon at or beyond the context yields too
        # few rows and an opaque pandas reshape error.
        if self.horizon >= self.context_length:
            raise ConfigurationError(
                "horizon must be strictly less than context_length "
                "(teardown 12.6): the model generates inside its own context "
                "window, so a horizon that fills it has nothing to condition on",
                horizon=self.horizon, context_length=self.context_length)

        for label, pin in (("tokenizer_pin", self.tokenizer_pin),
                           ("predictor_pin", self.predictor_pin)):
            if pin is None:
                continue
            if not isinstance(pin, CheckpointPin):
                raise ConfigurationError(f"{label} must be a CheckpointPin",
                                         got=type(pin).__name__)
            if pin.max_context is None:
                continue
            if self.context_length > pin.max_context:
                raise ConfigurationError(
                    "context_length exceeds the checkpoint's max_context",
                    context_length=self.context_length,
                    max_context=pin.max_context, pin=pin.repo_id)
            if self.horizon >= pin.max_context:
                raise ConfigurationError(
                    "horizon must be strictly less than the checkpoint's "
                    "max_context (teardown 12.6)",
                    horizon=self.horizon, max_context=pin.max_context,
                    pin=pin.repo_id)
        if (self.tokenizer_pin is not None
                and self.tokenizer_pin.kind != "tokenizer"):
            raise ConfigurationError("tokenizer_pin must be a tokenizer pin",
                                     got=self.tokenizer_pin.kind)
        if (self.predictor_pin is not None
                and self.predictor_pin.kind != "predictor"):
            raise ConfigurationError("predictor_pin must be a predictor pin",
                                     got=self.predictor_pin.kind)

        object.__setattr__(self, "temperature", float(self.temperature))
        if not (self.temperature > 0) or not math.isfinite(self.temperature):
            raise ConfigurationError(
                "temperature must be > 0; logits are divided by it",
                got=self.temperature)

        object.__setattr__(self, "clip", float(self.clip))
        if not (self.clip > 0) or not math.isfinite(self.clip):
            raise ConfigurationError("clip must be > 0", got=self.clip)

        if self.top_k is not None:
            if not isinstance(self.top_k, int) or isinstance(self.top_k, bool):
                raise ConfigurationError("top_k must be an int or None",
                                         got=repr(self.top_k))
            if self.top_k < 0:
                raise ConfigurationError("top_k must be >= 0", got=self.top_k)
            if self.top_k == 0:
                object.__setattr__(self, "top_k", None)   # 0 means "disabled"
        if self.top_p is not None:
            object.__setattr__(self, "top_p", float(self.top_p))
            if not 0.0 < self.top_p <= 1.0:
                raise ConfigurationError("top_p must be within (0, 1]",
                                         got=self.top_p)
            if self.top_p == 1.0:
                object.__setattr__(self, "top_p", None)   # 1.0 means "disabled"

        # --- teardown 12.1 -------------------------------------------------
        if self.top_k is not None and self.top_p is not None:
            raise ConfigurationError(
                "top_k and top_p may not both be set: upstream's filter returns "
                "after the top-k branch (teardown 12.1), so top_p is silently "
                "ignored whenever top_k > 0 despite the docstring promising "
                "'top-k and/or nucleus'. Olympus refuses the combination rather "
                "than honouring a sampling policy the model would discard.",
                top_k=self.top_k, top_p=self.top_p)

        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ConfigurationError("seed must be an int", got=repr(self.seed))
        if self.seed < 0:
            raise ConfigurationError("seed must be >= 0", got=self.seed)

        if self.max_age_s is not None:
            object.__setattr__(self, "max_age_s", float(self.max_age_s))
            if self.max_age_s <= 0:
                raise ConfigurationError("max_age_s must be > 0",
                                         got=self.max_age_s)
        object.__setattr__(self, "min_completeness", float(self.min_completeness))
        if not 0.0 < self.min_completeness <= 1.0:
            raise ConfigurationError("min_completeness must be within (0, 1]",
                                     got=self.min_completeness)

        quantiles = tuple(float(q) for q in self.quantiles)
        if not quantiles:
            raise ConfigurationError("quantiles must not be empty")
        for q in quantiles:
            if not math.isfinite(q) or not 0.0 < q < 1.0:
                raise ConfigurationError(
                    "quantiles must lie strictly within (0, 1); 0 and 1 are the "
                    "sample min and max, which are not quantile estimates",
                    got=q)
        if len(set(quantiles)) != len(quantiles):
            raise ConfigurationError("quantiles must be unique",
                                     got=list(quantiles))
        object.__setattr__(self, "quantiles", tuple(sorted(quantiles)))

        if self.abstain_uncertainty_above is not None:
            object.__setattr__(self, "abstain_uncertainty_above",
                               float(self.abstain_uncertainty_above))
            if not 0.0 <= self.abstain_uncertainty_above <= 1.0:
                raise ConfigurationError(
                    "abstain_uncertainty_above must be within [0, 1]",
                    got=self.abstain_uncertainty_above)

        if self.device is not None:
            object.__setattr__(self, "device", normalise_device(self.device))

    @property
    def sampling_params(self) -> SamplingParams:
        return SamplingParams(temperature=self.temperature, top_k=self.top_k,
                              top_p=self.top_p, seed=self.seed, clip=self.clip)

    def to_dict(self) -> dict:
        return {
            "context_length": self.context_length, "horizon": self.horizon,
            "n_samples": self.n_samples, "temperature": self.temperature,
            "top_p": self.top_p, "top_k": self.top_k, "seed": self.seed,
            "clip": self.clip, "device": self.device,
            "tokenizer_pin": (self.tokenizer_pin.to_dict()
                              if self.tokenizer_pin else None),
            "predictor_pin": (self.predictor_pin.to_dict()
                              if self.predictor_pin else None),
            "max_age_s": self.max_age_s,
            "min_completeness": self.min_completeness,
            "quantiles": list(self.quantiles),
            "abstain_uncertainty_above": self.abstain_uncertainty_above,
        }


# ---------------------------------------------------------------------------
# small deterministic statistics (stdlib only, no numpy at import time)
# ---------------------------------------------------------------------------

def _quantile(ordered: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted sample.

    Written out rather than delegated so the interpolation rule is *stated*:
    position `(n-1)*q`, linearly interpolated between neighbours (the same
    convention numpy calls `linear`). A forecast's p05 changes materially with
    the convention, so it has to be part of the record, not a library default
    that can move under us.
    """
    n = len(ordered)
    if n == 1:
        return float(ordered[0])
    position = (n - 1) * q
    lower = int(math.floor(position))
    upper = min(lower + 1, n - 1)
    weight = position - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def _quantile_label(q: float) -> str:
    percent = q * 100.0
    if abs(percent - round(percent)) < 1e-9:
        return f"p{int(round(percent)):02d}"
    return "p" + f"{percent:g}".replace(".", "_")


def _stdev(sample: Sequence[float]) -> float:
    """Sample standard deviation; 0 for fewer than two points.

    Sample (n-1) rather than population: the paths are a *draw* from the
    model's predictive distribution, not the whole of it.
    """
    if len(sample) < 2:
        return 0.0
    try:
        return float(statistics.stdev(sample))
    except statistics.StatisticsError:      # pragma: no cover - guarded above
        return 0.0


def _repair_bar(row: Sequence[float]) -> tuple[tuple[float, ...], bool]:
    """Force one predicted bar into a coherent candle. Teardown §12.7.

    Nothing upstream enforces `high >= max(open, close)`, `low <= min(open,
    close)`, or `volume >= 0`; downstream consumers compensate ad hoc (a ±10%
    clamp in one example, a "validation pass" in another). Olympus repairs at
    the boundary and *records that it did*, so a checkpoint producing constant
    violations is visible in the warnings rather than hidden by the fix.

    The repair only ever widens the bar (raise the high, lower the low) —
    moving open or close would be inventing a price the model did not predict.
    """
    values = [float(v) for v in row]
    repaired = False

    for index in (_OPEN, _HIGH, _LOW, _CLOSE):
        if values[index] < _MIN_PRICE:
            values[index] = _MIN_PRICE
            repaired = True
    for index in (_VOLUME, _AMOUNT):
        if values[index] < 0.0:
            values[index] = 0.0
            repaired = True

    body_high = max(values[_OPEN], values[_CLOSE])
    body_low = min(values[_OPEN], values[_CLOSE])
    if values[_HIGH] < body_high:
        values[_HIGH] = body_high
        repaired = True
    if values[_LOW] > body_low:
        values[_LOW] = body_low
        repaired = True
    if values[_HIGH] < values[_LOW]:        # pragma: no cover - unreachable
        values[_HIGH], values[_LOW] = values[_LOW], values[_HIGH]
        repaired = True
    return tuple(values), repaired


class _AsOfClock(Clock):
    """A clock frozen at the decision instant, for data validation only.

    Staleness and future-bar checks are measured against `as_of` rather than
    wall time so that `forecast(candles, as_of=t)` is a pure function of its
    arguments. If validation read the wall clock, replaying an audit record
    would produce a different verdict purely because time had passed, and
    "reproducible from its own record" would be false.
    """

    def __init__(self, as_of: datetime):
        self._as_of = as_of

    def now(self) -> datetime:
        return self._as_of

    def monotonic_ns(self) -> int:      # pragma: no cover - never used here
        return 0


# ---------------------------------------------------------------------------
# the forecaster
# ---------------------------------------------------------------------------

class KronosForecaster:
    """Turns a validated candle window into one `ForecastResult`.

    Holds no mutable state between calls beyond its configuration and backend,
    so the same instance is safe to reuse across instruments and the result of
    any call depends only on that call's arguments.
    """

    def __init__(self, config: KronosConfig, backend: Any,
                 clock: Clock | None = None, *,
                 instrument_registry: Any = None):
        if not isinstance(config, KronosConfig):
            raise ConfigurationError("config must be a KronosConfig",
                                     got=type(config).__name__)
        # A `KronosRuntime` is accepted for convenience; what is used is its
        # backend. Nothing downstream of here knows about tokenizers.
        resolved = backend.backend() if hasattr(backend, "backend") else backend
        if not isinstance(resolved, ModelBackend):
            raise ConfigurationError(
                "backend must be a ModelBackend (or a KronosRuntime)",
                got=type(backend).__name__)
        max_context = int(resolved.max_context)
        if config.context_length > max_context:
            raise ConfigurationError(
                "context_length exceeds the loaded checkpoint's max_context",
                context_length=config.context_length, max_context=max_context)
        if config.horizon >= max_context:
            raise ConfigurationError(
                "horizon must be strictly less than the loaded checkpoint's "
                "max_context (teardown 12.6)",
                horizon=config.horizon, max_context=max_context)
        self._config = config
        self._backend = resolved
        self._clock = clock or default_clock()
        self._registry = instrument_registry

    @property
    def config(self) -> KronosConfig:
        return self._config

    @property
    def backend(self) -> ModelBackend:
        return self._backend

    # -- public API --------------------------------------------------------
    def forecast(self, candles: Sequence[Candle], *, as_of: datetime,
                 instrument_key: str | None = None,
                 timeframe: str | None = None,
                 future_timestamps: Sequence[datetime] | None = None
                 ) -> ForecastResult:
        """Forecast `config.horizon` bars ahead of `as_of`.

        Abstains (never raises) on unusable input data; raises on look-ahead,
        misconfiguration, and backend contract violations.
        """
        config = self._config
        started_ns = self._clock.monotonic_ns()
        as_of = ensure_utc(as_of, field_name="as_of")
        created_at = self._clock.now()
        # A forecast anchored after the clock is a look-ahead bug in the
        # caller's scheduling, and it would silently validate future bars as
        # present. Refuse rather than trust the argument.
        if as_of > created_at:
            raise DataValidationError(
                "as_of is ahead of the clock: a forecast cannot be anchored in "
                "the future", as_of=as_of.isoformat(),
                now=created_at.isoformat())

        candles = list(candles or ())
        instrument_key, timeframe = self._resolve_identity(
            candles, instrument_key, timeframe)
        start, end = _window_bounds(candles, as_of)

        def abstain(reason: str, code: str, *,
                    quality: DataQuality = DataQuality.UNUSABLE,
                    warnings: Sequence[str] = ()) -> ForecastResult:
            return ForecastResult.abstain(
                instrument_key=instrument_key, timeframe=timeframe,
                input_start=start, input_end=end, created_at=created_at,
                horizon=config.horizon, reason=reason, code=code,
                data_quality=quality, warnings=tuple(warnings),
                model_version=self._model_version(),
                model_identity=self._identity(),
                inference_params=self._inference_params())

        # 0. Structural sanity. A non-Candle in the window is malformed input,
        #    not a crash: abstain with the data-validation code.
        for item in candles:
            if not isinstance(item, Candle):
                return abstain(
                    f"input contains a {type(item).__name__}, not a Candle",
                    DataValidationError.code)

        # 1. Look-ahead FIRST, on the raw window, and loudly. Validation would
        #    otherwise silently drop future bars (`drop_future=True`) and hide
        #    the caller's slicing bug behind a completeness warning. A bar that
        #    closes after the decision instant could not have been known, and
        #    that includes an in-progress bar stamped to close later.
        assert_no_lookahead(candles, as_of)

        # 2. Quality gate.
        try:
            clean, report = normalise_candles(
                candles, timeframe=timeframe, clock=_AsOfClock(as_of),
                instrument=instrument_key, max_age=config.max_age_s,
                min_completeness=config.min_completeness, drop_future=True)
            require_usable(report, min_bars=config.context_length)
        except MarketDataError as exc:
            return abstain(str(exc), exc.code)

        context = clean[-config.context_length:]
        start, end = context[0].ts_open, context[-1].ts_close
        warnings: list[str] = list(report.warnings)

        # 3. Instance normalisation over the CONTEXT ONLY — teardown §12.9.
        matrix = [_row(candle) for candle in context]
        means, stds = _column_stats(matrix)
        normalised = [
            tuple(max(-config.clip,
                      min(config.clip,
                          (value - means[i]) / (stds[i] + _STD_EPSILON)))
                  for i, value in enumerate(row))
            for row in matrix]

        stamps = calendar_stamps([c.ts_open for c in context])
        forecast_ts = self._forecast_timestamps(
            context, timeframe, future_timestamps)
        future_stamps = calendar_stamps(forecast_ts)

        # 4. Draw every path and keep every path — teardown §12.7.
        raw_paths = self._backend.generate(
            normalised, context_stamps=stamps, future_stamps=future_stamps,
            horizon=config.horizon, n_samples=config.n_samples,
            params=config.sampling_params)
        _check_backend_output(raw_paths, config)

        # 5. Denormalise, then repair OHLC coherence per bar — teardown §12.7.
        paths: list[tuple[tuple[float, ...], ...]] = []
        repairs = 0
        for path in raw_paths:
            rebuilt = []
            for step in path:
                denormalised = [step[i] * (stds[i] + _STD_EPSILON) + means[i]
                                for i in range(len(FEATURE_COLUMNS))]
                if not all(math.isfinite(v) for v in denormalised):
                    return abstain(
                        "the model produced a non-finite value; the forecast "
                        "is unusable and will not be reported as a number",
                        ForecastError.code, warnings=warnings)
                fixed, was_repaired = _repair_bar(denormalised)
                repairs += int(was_repaired)
                rebuilt.append(fixed)
            paths.append(tuple(rebuilt))
        if repairs:
            warnings.append(
                f"repaired OHLC coherence / non-negativity on {repairs} of "
                f"{config.n_samples * config.horizon} predicted bars "
                "(teardown 12.7: the model applies no structural constraints)")

        # 6. Aggregate. Every statistic below is a *view* over `paths`.
        last_close = context[-1].close
        result_paths = tuple(
            ForecastPath(
                closes=tuple(step[_CLOSE] for step in path),
                opens=tuple(step[_OPEN] for step in path),
                highs=tuple(step[_HIGH] for step in path),
                lows=tuple(step[_LOW] for step in path),
                volumes=tuple(step[_VOLUME] for step in path))
            for path in paths)

        by_step = [sorted(path[step][_CLOSE] for path in paths)
                   for step in range(config.horizon)]
        mean_close = tuple(statistics.fmean(column) for column in by_step)
        median_close = tuple(_quantile(column, 0.5) for column in by_step)
        quantiles = {
            _quantile_label(q): tuple(_quantile(column, q) for column in by_step)
            for q in config.quantiles}

        expected_return_path = tuple(value / last_close - 1.0
                                     for value in mean_close)
        expected_return = expected_return_path[-1]

        terminal_returns = [path[-1][_CLOSE] / last_close - 1.0
                            for path in paths]
        forecast_volatility = _stdev(terminal_returns)
        realised_volatility = _realised_volatility(context)
        direction = _direction_probabilities(terminal_returns)
        uncertainty = _uncertainty(
            terminal_returns, realised_volatility, config.horizon,
            n_paths=len(paths))

        # An aggregate built from coherent parts is coherent (means and order
        # statistics both preserve pointwise domination), but the claim is
        # cheap to check and expensive to be wrong about.
        _assert_aggregate_coherent(paths, config.horizon)

        latency_ms = max(0.0, (self._clock.monotonic_ns() - started_ns) / 1e6)
        params = self._inference_params(
            context_bars=len(context), n_paths=len(paths),
            means=means, stds=stds, last_close=last_close)

        # 7. Refuse to publish a forecast we do not believe.
        if (config.abstain_uncertainty_above is not None
                and uncertainty > config.abstain_uncertainty_above):
            return ForecastResult.abstain(
                instrument_key=instrument_key, timeframe=timeframe,
                input_start=start, input_end=end, created_at=created_at,
                horizon=config.horizon,
                reason=(f"uncertainty {uncertainty:.4f} exceeds the configured "
                        f"limit {config.abstain_uncertainty_above:.4f}: the "
                        "sampled paths disagree too much to act on"),
                code="FORECAST_UNCERTAIN", data_quality=report.status,
                warnings=tuple(warnings), model_version=self._model_version(),
                model_identity=self._identity(), inference_params=params)

        # 8. A record that fully describes how it was produced.
        return ForecastResult(
            instrument_key=instrument_key, timeframe=timeframe,
            input_start=start, input_end=end, created_at=created_at,
            horizon=config.horizon, forecast_timestamps=forecast_ts,
            paths=result_paths, mean_close=mean_close,
            median_close=median_close, quantiles=quantiles,
            expected_return=expected_return,
            expected_return_path=expected_return_path,
            forecast_volatility=forecast_volatility,
            realised_volatility=realised_volatility,
            direction_probabilities=direction, uncertainty=uncertainty,
            model_version=self._model_version(),
            model_identity=self._identity(), inference_params=params,
            data_quality=report.status, warnings=tuple(warnings),
            latency_ms=latency_ms)

    # -- helpers -----------------------------------------------------------
    def _resolve_identity(self, candles: Sequence[Candle],
                          instrument_key: str | None,
                          timeframe: str | None) -> tuple[str, str]:
        first = candles[0] if candles else None
        key = instrument_key or (getattr(first, "instrument_key", None))
        tf = timeframe or (getattr(first, "timeframe", None))
        if not key or not tf:
            raise ConfigurationError(
                "instrument_key and timeframe must be supplied when the candle "
                "window is empty or heterogeneous",
                instrument_key=key, timeframe=tf)
        parse_timeframe(tf)                 # rejects a malformed timeframe now
        if self._registry is not None:
            # An unknown instrument is a wiring error: forecasting something we
            # could never size or route an order for is wasted work at best.
            self._registry.get(key)
        return str(key), str(tf)

    def _forecast_timestamps(self, context: Sequence[Candle], timeframe: str,
                             supplied: Sequence[datetime] | None
                             ) -> tuple[datetime, ...]:
        horizon = self._config.horizon
        last_close = context[-1].ts_close
        if supplied is None:
            step = parse_timeframe(timeframe)
            return tuple(last_close + step * i for i in range(1, horizon + 1))
        stamps = tuple(ensure_utc(t, field_name="future_timestamp")
                       for t in supplied)
        if len(stamps) != horizon:
            raise ConfigurationError(
                "future_timestamps length must equal the horizon",
                horizon=horizon, got=len(stamps))
        previous = last_close
        for stamp in stamps:
            if stamp <= previous:
                raise ConfigurationError(
                    "future_timestamps must be strictly increasing and start "
                    "after the last input bar closes",
                    got=stamp.isoformat(), previous=previous.isoformat())
            previous = stamp
        return stamps

    def _model_version(self) -> str:
        identity = self._backend.identity or {}
        return str(identity.get("model_version", "unknown"))

    def _identity(self) -> dict:
        return {str(k): jsonable(v)
                for k, v in dict(self._backend.identity or {}).items()}

    def _inference_params(self, *, context_bars: int | None = None,
                          n_paths: int | None = None,
                          means: Sequence[float] | None = None,
                          stds: Sequence[float] | None = None,
                          last_close: float | None = None) -> dict:
        """Everything needed to rerun this exact forecast.

        The normalisation statistics are included deliberately: without them a
        replay would have to re-derive them from the input window, and any
        disagreement about which bars formed the context would silently produce
        different numbers.
        """
        params = self._config.to_dict()
        params["feature_columns"] = list(FEATURE_COLUMNS)
        params["std_epsilon"] = _STD_EPSILON
        params["quantile_interpolation"] = "linear"
        if context_bars is not None:
            params["context_bars"] = context_bars
        if n_paths is not None:
            params["n_paths"] = n_paths
        if means is not None:
            params["context_mean"] = [float(v) for v in means]
        if stds is not None:
            params["context_std"] = [float(v) for v in stds]
        if last_close is not None:
            params["last_close"] = float(last_close)
        return params

    def __repr__(self) -> str:      # pragma: no cover - trivial
        return (f"KronosForecaster({self._model_version()}, "
                f"context={self._config.context_length}, "
                f"horizon={self._config.horizon}, "
                f"n_samples={self._config.n_samples})")


# ---------------------------------------------------------------------------
# module-level maths
# ---------------------------------------------------------------------------

def _row(candle: Candle) -> tuple[float, ...]:
    return (candle.open, candle.high, candle.low, candle.close,
            candle.volume, candle.amount)


def _column_stats(matrix: Sequence[Sequence[float]]
                  ) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Per-column mean and population stdev over the context window.

    Population (not sample) stdev, matching upstream's `np.std` default: the
    context is the whole of what is being normalised, not a sample of it, and
    using a different divisor would shift the input distribution away from the
    one the pretrained weights saw.
    """
    columns = list(zip(*matrix))
    means = tuple(statistics.fmean(column) for column in columns)
    stds = tuple(statistics.pstdev(column, mu=means[i])
                 for i, column in enumerate(columns))
    return means, stds


def _window_bounds(candles: Sequence[Candle],
                   as_of: datetime) -> tuple[datetime, datetime]:
    """Input window extent, falling back to `as_of` for an empty window."""
    opens = [c.ts_open for c in candles if isinstance(c, Candle)]
    closes = [c.ts_close for c in candles if isinstance(c, Candle)]
    if not opens or not closes:
        return as_of, as_of
    return min(opens), max(max(closes), min(opens))


def _check_backend_output(paths: Any, config: KronosConfig) -> None:
    """A backend that breaks its own contract is a bug, so this raises.

    Abstaining here would convert a wiring error into a data-quality record and
    hide it; the adapter's whole job is to be the place where wrong shapes stop.
    """
    if not isinstance(paths, (list, tuple)):
        raise ForecastError("backend did not return a sequence of paths",
                            got=type(paths).__name__)
    if len(paths) != config.n_samples:
        raise ForecastError(
            "backend returned the wrong number of paths; sampled paths must "
            "never be collapsed (teardown 12.7)",
            expected=config.n_samples, got=len(paths))
    width = len(FEATURE_COLUMNS)
    for index, path in enumerate(paths):
        if len(path) != config.horizon:
            raise ForecastError("backend path has the wrong length",
                                path=index, expected=config.horizon,
                                got=len(path))
        for step in path:
            if len(step) != width:
                raise ForecastError(
                    "backend step is not a full OHLCVA row", path=index,
                    expected=width, got=len(step),
                    columns=list(FEATURE_COLUMNS))


def _realised_volatility(context: Sequence[Candle]) -> float:
    """Per-bar stdev of log returns over the context window.

    Deliberately not annualised: it is compared against a horizon-terminal
    dispersion inside `_uncertainty`, and mixing an annualised number with a
    per-horizon one is the kind of unit error that produces a confident
    forecaster in a crisis.
    """
    closes = [c.close for c in context if c.close > 0]
    if len(closes) < 3:
        return 0.0
    returns = [math.log(closes[i] / closes[i - 1])
               for i in range(1, len(closes))]
    return _stdev(returns)


def _direction_probabilities(terminal_returns: Sequence[float]
                             ) -> dict[str, float]:
    """Empirical histogram over the sampled paths. Sums to exactly 1."""
    total = len(terminal_returns)
    if total == 0:                          # pragma: no cover - guarded upstream
        return {"up": 0.0, "down": 0.0, "flat": 1.0}
    up = sum(1 for r in terminal_returns if r > _FLAT_EPSILON)
    down = sum(1 for r in terminal_returns if r < -_FLAT_EPSILON)
    flat = total - up - down
    return {"up": up / total, "down": down / total, "flat": flat / total}


def _uncertainty(terminal_returns: Sequence[float], realised_volatility: float,
                 horizon: int, *, n_paths: int) -> float:
    """Cross-path dispersion, scaled by realised vol, squashed into [0, 1].

    See the module docstring for the rationale. The two boundary cases are the
    ones that matter:

    * **One path — always 1.0.** A single draw contains no information about
      how much the model disagrees with itself. Reporting anything else would
      be manufacturing confidence from an empty sample.
    * **Zero realised volatility** (a pegged or constant context) leaves
      nothing to scale against, so agreement means 0 and any disagreement at
      all means 1 rather than dividing by zero.
    """
    if n_paths < 2:
        return 1.0
    dispersion = _stdev(terminal_returns)
    scale = realised_volatility * math.sqrt(max(1, horizon))
    if scale <= 0.0:
        return 0.0 if dispersion <= 0.0 else 1.0
    ratio = dispersion / scale
    if not math.isfinite(ratio):            # pragma: no cover - defensive
        return 1.0
    return max(0.0, min(1.0, ratio / (1.0 + ratio)))


def _assert_aggregate_coherent(paths: Sequence[Sequence[Sequence[float]]],
                               horizon: int) -> None:
    """Self-check: the mean candle must be a legal candle.

    Provable from `_repair_bar` — an element-wise mean of bars each satisfying
    `high >= max(open, close)` satisfies it too — but upstream's §12.7 failure
    was exactly "averaging in feature space breaks coherence", so the property
    is asserted rather than assumed.
    """
    for step in range(horizon):
        column = [path[step] for path in paths]
        count = len(column)
        mean = [sum(row[i] for row in column) / count for i in range(6)]
        if (mean[_HIGH] < max(mean[_OPEN], mean[_CLOSE]) - 1e-9
                or mean[_LOW] > min(mean[_OPEN], mean[_CLOSE]) + 1e-9):
            raise ForecastError(
                "averaging the sampled paths produced an incoherent candle",
                step=step, mean_open=mean[_OPEN], mean_high=mean[_HIGH],
                mean_low=mean[_LOW], mean_close=mean[_CLOSE])


__all__ = ["KronosConfig", "KronosForecaster"]
