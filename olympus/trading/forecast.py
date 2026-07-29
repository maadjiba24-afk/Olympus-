"""The forecasting service layer — everything about forecasting that is *not* a model.

A forecasting model is a small, replaceable component. The machinery around it
is where the safety properties live, and that machinery is what this module
owns: uniform contracts, error containment, latency measurement, audit, caching,
and the honest baselines that make "is the model any good?" an answerable
question.

Three decisions shape the design.

**A forecaster may never break the loop.** `ForecastService.run` catches
*everything* a forecaster can throw — a `TradingError`, a `KeyError` from a
half-written adapter, a torch OOM — and converts it into an abstaining
`ForecastResult` carrying the error's code. A trading loop that dies because one
of four forecasters raised is a loop that stops managing open positions, which
is strictly worse than a loop that trades with three opinions and records the
fourth's failure. The exception is *programmer* error at the wiring level
(unknown forecaster name, a "forecaster" with no `.forecast`): those raise,
because they can only be fixed by a human editing code.

**Abstention is a result, not a failure.** The baselines below abstain rather
than guess when the window is short, contains an unfinished bar, is out of
order, or — most importantly — contains a bar that closes *after* `as_of`. That
last one is look-ahead: the single defect that makes a backtest a lie. A
forecaster handed the future must refuse it, not silently trim it, because
silently trimming hides the caller's bug for months.

**Baselines are first-class.** `PersistenceForecaster` — tomorrow's price is
today's price — is the bar every learned model has to clear. It is not a
placeholder; it is the null hypothesis. It therefore produces a *complete*
`ForecastResult` with the same fields, same timestamp convention, and same
abstention rules as any other forecaster, so `evaluate.py` can score them
against each other without a single special case.

The single-path uncertainty rule
--------------------------------
A deterministic forecaster emits exactly one path. One sample says nothing about
dispersion, so `forecast_volatility` is 0.0 meaning *unmeasured*, and
`uncertainty` is **1.0** — the maximum. This is deliberate and it is the
convention every consumer in this package relies on: `uncertainty` answers "how
much do I know about the distribution", not "how confident is the point
estimate". Emitting `uncertainty=0.0` for a single path would tell a strategy
that a straight-line extrapolation is a certainty. Baselines consequently
produce `confidence == 0.0`, which is exactly right: they exist to be measured,
not to be traded.

Caching
-------
`ForecastCache` keys on `(instrument, timeframe, as_of, model_version,
params_hash)`. The params hash is not optional. Two runs of the same checkpoint
at `temperature=1.0, n_paths=30` and `temperature=0.6, n_paths=1` are different
forecasts; a cache keyed only on model version would serve one where the caller
asked for the other, and the audit record would then attribute a trade to
sampling settings that never ran. Abstentions are never cached — an abstention
is usually transient (a late bar, a missing checkpoint) and caching it would
suppress the retry that fixes it.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import (Candle, DataQuality, ForecastPath, ForecastResult,
                        ensure_utc, jsonable)
from .errors import (ConfigurationError, DataValidationError,
                     InsufficientDataError, StaleDataError, TradingError)

# ---------------------------------------------------------------------------
# failure codes
# ---------------------------------------------------------------------------
# Reuse the error taxonomy's codes wherever a condition already has one, so a
# forecast abstention and the exception for the same condition are searchable by
# the same string. Only conditions with no exception of their own get a new code.

CODE_INSUFFICIENT = InsufficientDataError.code          # "DATA_INSUFFICIENT"
CODE_INVALID = DataValidationError.code                 # "DATA_VALIDATION_FAILED"
CODE_STALE = StaleDataError.code                        # "DATA_STALE"
#: A bar in the input window closes after `as_of`. The caller leaked the future.
CODE_LOOKAHEAD = "FORECAST_LOOKAHEAD"
#: A forecaster raised something the service did not anticipate.
CODE_UNEXPECTED = "FORECAST_UNEXPECTED_ERROR"
#: A forecaster returned something that is not a `ForecastResult`, or one that
#: describes a different instrument than the one that was asked for.
CODE_CONTRACT = "FORECAST_CONTRACT_VIOLATION"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def params_hash(params: Mapping[str, Any]) -> str:
    """Stable short hash of a parameter mapping.

    `sort_keys` plus `jsonable` is what makes it stable across processes and
    across Python's dict ordering: the same settings must hash the same in the
    cache key today and in the audit replay next year. Truncated to 16 hex
    characters because it is an identity tag, not a security primitive.
    """
    payload = json.dumps(jsonable(dict(params)), sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _log_returns(closes: Sequence[float]) -> list[float]:
    """Per-bar log returns. Log space because it composes additively, so a
    projected path is a straight line rather than a compounding approximation."""
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def _stdev(sample: Sequence[float]) -> float:
    """Sample standard deviation, 0.0 when there is nothing to disperse."""
    if len(sample) < 2:
        return 0.0
    try:
        return float(statistics.stdev(sample))
    except statistics.StatisticsError:      # pragma: no cover - defensive
        return 0.0


def _bar_delta(bars: Sequence[Candle], timeframe: str) -> timedelta | None:
    """Spacing between consecutive bars, for stamping the forecast horizon.

    Prefers the declared timeframe (exact, and the same rule `candles.py` and
    `instruments.py` use) and falls back to the observed spacing of the last two
    bars. Returns None when neither is available — the contract permits an
    empty `forecast_timestamps`, and inventing a spacing would put wrong times
    into the audit trail.
    """
    if timeframe:
        try:
            from .instruments import parse_timeframe
            return parse_timeframe(timeframe)
        except Exception:                    # noqa: BLE001 - unparseable is fine
            pass
    if len(bars) >= 2:
        delta = bars[-1].ts_close - bars[-2].ts_close
        if delta > timedelta(0):
            return delta
    return None


# ---------------------------------------------------------------------------
# the forecaster interface
# ---------------------------------------------------------------------------

class Forecaster(ABC):
    """What every forecasting model must look like from outside.

    A persistence baseline, a third-party model and an Olympus-native model are
    interchangeable here by construction: the service, the cache, the signal
    layer and the evaluator all depend on this interface and never on a concrete
    class. That is what keeps "any model is one component" true rather than
    aspirational — the day one stops earning its place it is removed by deleting
    a registry entry, and `tests/test_trading_independence.py` proves that
    nothing else has to change.
    """

    #: Identity of the model, written into every result and into cache keys.
    #: Bump it whenever the numbers a given input produces would change.
    MODEL_VERSION: str = "unknown"

    def __init__(self, *, name: str | None = None, clock: Clock | None = None):
        self.name = name or type(self).__name__
        self.clock = clock or default_clock()

    # -- identity ---------------------------------------------------------

    @property
    def model_version(self) -> str:
        return self.MODEL_VERSION

    @property
    def params(self) -> Mapping[str, Any]:
        """Settings that change the numbers. Everything here enters the cache
        key; anything omitted is something the cache is allowed to ignore."""
        return {}

    @property
    def required_bars(self) -> int:
        """Minimum usable bars before this forecaster will produce anything."""
        return 1

    @property
    def deterministic(self) -> bool:
        """True when identical input always produces byte-identical output."""
        return False

    def params_hash(self) -> str:
        return params_hash(self.params)

    def describe(self) -> dict:
        """Self-description for the audit trail and the model registry."""
        return {
            "name": self.name,
            "class": type(self).__name__,
            "model_version": self.model_version,
            "deterministic": self.deterministic,
            "required_bars": self.required_bars,
            "params": jsonable(dict(self.params)),
            "params_hash": self.params_hash(),
        }

    # -- the one method that matters --------------------------------------

    @abstractmethod
    def forecast(self, candles: Sequence[Candle], *,
                 as_of: datetime | None = None,
                 instrument_key: str = "",
                 timeframe: str = "") -> ForecastResult:
        """Forecast forward from `candles`, as known at `as_of`.

        Implementations return an abstaining `ForecastResult` for any data
        condition they cannot handle. Raising is permitted only for conditions a
        human must fix (a broken configuration); the service converts anything
        else into an abstention anyway.
        """

    def __repr__(self) -> str:               # pragma: no cover - trivial
        return f"{type(self).__name__}(name={self.name!r}, v={self.model_version})"


# ---------------------------------------------------------------------------
# baselines
# ---------------------------------------------------------------------------

class BaselineForecaster(Forecaster):
    """Shared machinery for the deterministic baselines.

    The baselines differ in exactly one method — `_project`, which turns the
    input closes into the forecast closes. Everything else (screening, path
    construction, statistics, abstention) is identical on purpose: if a baseline
    and a learned model disagree it must be because their predictions differ,
    not because they measured returns from different anchors or stamped their
    horizons differently.
    """

    def __init__(self, *, horizon: int = 1, name: str | None = None,
                 clock: Clock | None = None,
                 max_data_age_s: float | None = None):
        super().__init__(name=name, clock=clock)
        if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 1:
            raise ConfigurationError("horizon must be a positive int",
                                     horizon=repr(horizon))
        if max_data_age_s is not None and max_data_age_s <= 0:
            raise ConfigurationError("max_data_age_s must be > 0",
                                     max_data_age_s=max_data_age_s)
        self.horizon = horizon
        self.max_data_age_s = max_data_age_s

    @property
    def deterministic(self) -> bool:
        return True

    @property
    def params(self) -> Mapping[str, Any]:
        return {"horizon": self.horizon, "max_data_age_s": self.max_data_age_s}

    # -- subclass hook -----------------------------------------------------

    @abstractmethod
    def _project(self, closes: Sequence[float]) -> list[float]:
        """Return `horizon` future closes given the input closes."""

    # -- the template ------------------------------------------------------

    def forecast(self, candles: Sequence[Candle], *,
                 as_of: datetime | None = None,
                 instrument_key: str = "",
                 timeframe: str = "") -> ForecastResult:
        now = ensure_utc(as_of, field_name="as_of") if as_of is not None \
            else self.clock.now()
        bars = list(candles or ())
        key = instrument_key or (bars[-1].instrument_key if bars else "")
        tf = timeframe or (bars[-1].timeframe if bars else "")

        def abstain(reason: str, code: str, *, quality=DataQuality.UNUSABLE,
                    warnings: Sequence[str] = ()) -> ForecastResult:
            start = bars[0].ts_open if bars else now
            end = bars[-1].ts_close if bars else now
            # A window that extends past `as_of` would itself fail the contract's
            # ordering check, so clamp the recorded window to the decision time.
            start = min(start, now)
            end = min(max(end, start), now)
            return ForecastResult.abstain(
                instrument_key=key, timeframe=tf, input_start=start,
                input_end=end, created_at=now, horizon=self.horizon,
                reason=reason, code=code, data_quality=quality,
                warnings=tuple(warnings), model_version=self.model_version,
                model_identity=self._identity(),
                inference_params=jsonable(dict(self.params)))

        if not bars:
            return abstain("no candles supplied", CODE_INSUFFICIENT)

        warnings: list[str] = []
        # An unfinished bar is a price that has not happened yet in full. Drop
        # it rather than forecast from a partial print, and say so.
        unfinished = [c for c in bars if not c.is_final]
        if unfinished:
            bars = [c for c in bars if c.is_final]
            warnings.append(f"dropped {len(unfinished)} non-final bar(s)")
            if not bars:
                return abstain("every candle was non-final", CODE_INSUFFICIENT,
                               warnings=warnings)

        if any(c.instrument_key != bars[0].instrument_key for c in bars):
            return abstain("window mixes instruments", CODE_INVALID,
                           warnings=warnings)
        if any(c.timeframe != bars[0].timeframe for c in bars):
            return abstain("window mixes timeframes", CODE_INVALID,
                           warnings=warnings)
        for i in range(1, len(bars)):
            if bars[i].ts_close <= bars[i - 1].ts_close:
                return abstain(
                    "window is not strictly increasing in time; normalise it "
                    "with validate.normalise_candles first",
                    CODE_INVALID, warnings=warnings)
        # Look-ahead. Loud, always: a window containing a bar that closes after
        # the decision time means the caller leaked the future into the model.
        future = [c for c in bars if c.ts_close > now]
        if future:
            return abstain(
                f"{len(future)} bar(s) close after as_of; refusing to forecast "
                "from data that did not exist yet",
                CODE_LOOKAHEAD, warnings=warnings)

        if len(bars) < self.required_bars:
            return abstain(
                f"need {self.required_bars} bars, have {len(bars)}",
                CODE_INSUFFICIENT, warnings=warnings)

        age = (now - bars[-1].ts_close).total_seconds()
        if self.max_data_age_s is not None and age > self.max_data_age_s:
            return abstain(f"newest bar is {age:.0f}s old (limit "
                           f"{self.max_data_age_s:.0f}s)", CODE_STALE,
                           warnings=warnings)

        closes = [c.close for c in bars]
        projected = self._project(closes)
        if len(projected) != self.horizon or not all(
                math.isfinite(v) and v > 0 for v in projected):
            # A baseline producing a non-positive or non-finite price is a bug
            # in the projection, not a market condition — abstain rather than
            # hand a strategy an impossible price.
            return abstain("projection produced non-positive or non-finite "
                           "prices", CODE_INVALID, warnings=warnings)

        return self._assemble(bars, projected, now=now, key=key, tf=tf,
                              warnings=warnings)

    # -- result construction ----------------------------------------------

    def _identity(self) -> dict:
        """Baselines have no checkpoint; their identity is their source."""
        return {"kind": "baseline", "class": type(self).__name__,
                "model_version": self.model_version}

    def _assemble(self, bars: Sequence[Candle], projected: Sequence[float], *,
                  now: datetime, key: str, tf: str,
                  warnings: Sequence[str]) -> ForecastResult:
        closes = [c.close for c in bars]
        last = closes[-1]
        return_path = tuple(p / last - 1.0 for p in projected)
        total_return = return_path[-1]

        # OHLC for the single path. Each projected bar opens where the previous
        # closed and has no intrabar excursion beyond its own body: a
        # deterministic baseline has no opinion about wicks, and inventing them
        # would put unearned range into anything that consumes `paths`.
        opens = [last] + list(projected[:-1])
        highs = [max(o, c) for o, c in zip(opens, projected)]
        lows = [min(o, c) for o, c in zip(opens, projected)]
        mean_volume = statistics.fmean([c.volume for c in bars]) if bars else 0.0
        path = ForecastPath(closes=tuple(projected), opens=tuple(opens),
                            highs=tuple(highs), lows=tuple(lows),
                            volumes=tuple([mean_volume] * len(projected)))

        delta = _bar_delta(bars, tf)
        stamps: tuple[datetime, ...] = ()
        if delta is not None:
            anchor = bars[-1].ts_close
            stamps = tuple(anchor + delta * (i + 1) for i in range(len(projected)))

        realised = _stdev(_log_returns(closes))
        # Degenerate by construction: one path is one sample. Recorded as such
        # (see the module docstring) rather than dressed up as a distribution.
        tol = 1e-12
        probs = {"up": 1.0 if total_return > tol else 0.0,
                 "down": 1.0 if total_return < -tol else 0.0,
                 "flat": 1.0 if abs(total_return) <= tol else 0.0}

        return ForecastResult(
            instrument_key=key, timeframe=tf,
            input_start=bars[0].ts_open, input_end=bars[-1].ts_close,
            created_at=now, horizon=len(projected),
            forecast_timestamps=stamps, paths=(path,),
            mean_close=tuple(projected), median_close=tuple(projected),
            # Only p50: with a single path there are no quantiles to report and
            # emitting equal p05/p95 bands would fake a confidence interval.
            quantiles={"p50": tuple(projected)},
            expected_return=total_return, expected_return_path=return_path,
            forecast_volatility=0.0,          # unmeasured, not zero-risk
            realised_volatility=realised,
            direction_probabilities=probs,
            uncertainty=1.0,                  # the single-path rule
            model_version=self.model_version,
            model_identity=self._identity(),
            inference_params=jsonable(dict(self.params)),
            data_quality=DataQuality.OK,
            warnings=tuple(warnings) + ("single deterministic path: dispersion "
                                        "is unmeasured, uncertainty pinned to 1.0",))


class PersistenceForecaster(BaselineForecaster):
    """Tomorrow's price is today's price. **The** baseline.

    On any liquid instrument at short horizons this is close to unbeatable, and
    it costs nothing to run. Every learned model in this package is judged
    against it out-of-sample and net of costs (`evaluate.py`); a model that does
    not beat it is a model that should be switched off, which is the whole
    reason this class exists rather than being an afterthought in a test file.
    """

    MODEL_VERSION = "persistence-1"

    @property
    def required_bars(self) -> int:
        return 1

    def _project(self, closes: Sequence[float]) -> list[float]:
        return [closes[-1]] * self.horizon


class DriftForecaster(BaselineForecaster):
    """Constant-drift extrapolation: the recent average log return, continued.

    Log space, so the projection is a straight line in log price and can never
    produce a negative price. `damping` shrinks the drift toward zero; at 0.0
    this degenerates exactly into `PersistenceForecaster`, which makes it a
    useful knob for showing how much of a strategy's edge is really just trend
    continuation.
    """

    MODEL_VERSION = "drift-1"

    def __init__(self, *, window: int = 20, damping: float = 1.0, **kwargs):
        super().__init__(**kwargs)
        if not isinstance(window, int) or isinstance(window, bool) or window < 1:
            raise ConfigurationError("window must be a positive int",
                                     window=repr(window))
        if not isinstance(damping, (int, float)) or isinstance(damping, bool):
            raise ConfigurationError("damping must be a number",
                                     damping=repr(damping))
        damping = float(damping)
        if not math.isfinite(damping) or damping < 0.0:
            raise ConfigurationError("damping must be finite and >= 0",
                                     damping=damping)
        self.window = window
        self.damping = damping

    @property
    def params(self) -> Mapping[str, Any]:
        return {**super().params, "window": self.window, "damping": self.damping}

    @property
    def required_bars(self) -> int:
        return self.window + 1           # `window` returns need one extra close

    def _project(self, closes: Sequence[float]) -> list[float]:
        recent = list(closes[-(self.window + 1):])
        # The mean log return over the window telescopes to the endpoints, which
        # is both cheaper and immune to accumulated float error mid-window.
        drift = (math.log(recent[-1] / recent[0]) / (len(recent) - 1)) * self.damping
        last = closes[-1]
        return [last * math.exp(drift * (i + 1)) for i in range(self.horizon)]


class SeasonalNaiveForecaster(BaselineForecaster):
    """Replay the pattern from one season ago.

    Two modes, and the default is the less obvious one on purpose:

    * `use_returns=True` (default) — replay the *log returns* of the previous
      season, compounded onto the last close. Continuous at the join, so the
      forecast starts where the market actually is.
    * `use_returns=False` — textbook seasonal naive on levels, ``ŷ[T+h] =
      y[T+h-m]``. Correct for a stationary series, but on a price it teleports
      the forecast back to the level of a season ago, producing a step
      discontinuity at the first forecast bar that no strategy should act on.

    Horizons longer than the season wrap around it rather than reaching further
    back, so the seasonal claim stays a *seasonal* claim.
    """

    MODEL_VERSION = "seasonal-naive-1"

    def __init__(self, *, season: int = 24, use_returns: bool = True, **kwargs):
        super().__init__(**kwargs)
        if not isinstance(season, int) or isinstance(season, bool) or season < 1:
            raise ConfigurationError("season must be a positive int",
                                     season=repr(season))
        self.season = season
        self.use_returns = bool(use_returns)

    @property
    def params(self) -> Mapping[str, Any]:
        return {**super().params, "season": self.season,
                "use_returns": self.use_returns}

    @property
    def required_bars(self) -> int:
        return self.season + 1

    def _project(self, closes: Sequence[float]) -> list[float]:
        n = len(closes)
        out: list[float] = []
        if not self.use_returns:
            for step in range(self.horizon):
                out.append(closes[n - self.season + (step % self.season)])
            return out
        cumulative = 0.0
        for step in range(self.horizon):
            source = n - self.season + (step % self.season)
            cumulative += math.log(closes[source] / closes[source - 1])
            out.append(closes[-1] * math.exp(cumulative))
        return out


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

class ForecastCache:
    """TTL cache for forecasts, keyed on everything that changes the numbers.

    The key is `(instrument, timeframe, as_of, model_version, params_hash)`.
    Dropping any one of those five is a correctness bug, and the params hash is
    the one people forget: the same checkpoint sampled 30 times at temperature
    1.0 and once at 0.6 are different forecasts, and serving one for the other
    silently invalidates both the trade and its audit record.

    Expiry is measured against the injected clock, never wall time, so a
    backtest's cache expires on backtest time. Eviction is insertion-ordered
    (oldest first) and therefore deterministic — an LRU would make the contents
    depend on read order, and two identical runs must produce identical state.
    """

    def __init__(self, *, ttl_s: float = 60.0, clock: Clock | None = None,
                 max_entries: int = 256):
        if ttl_s <= 0:
            raise ConfigurationError("ttl_s must be > 0", ttl_s=ttl_s)
        if max_entries < 1:
            raise ConfigurationError("max_entries must be >= 1",
                                     max_entries=max_entries)
        self.ttl_s = float(ttl_s)
        self.max_entries = int(max_entries)
        self.clock = clock or default_clock()
        self._entries: dict[str, tuple[datetime, ForecastResult]] = {}
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @staticmethod
    def key(*, instrument_key: str, timeframe: str, as_of: datetime,
            model_version: str, params_hash: str) -> str:
        """The full cache key. Positional-free so a caller cannot silently
        transpose two of the five components."""
        stamp = ensure_utc(as_of, field_name="as_of").isoformat()
        return "|".join((instrument_key, timeframe, stamp, model_version,
                         params_hash))

    def get(self, key: str) -> ForecastResult | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires_at, result = entry
        if self.clock.now() >= expires_at:
            del self._entries[key]
            self.misses += 1
            return None
        self.hits += 1
        return result

    def put(self, key: str, result: ForecastResult) -> None:
        """Store a result. Abstentions are refused, not stored.

        An abstention usually means "not yet" — a late bar, a checkpoint still
        downloading. Caching it would hold the refusal for the whole TTL and
        turn a one-bar hiccup into a minute of silence.
        """
        if result.abstained:
            return
        if key in self._entries:
            del self._entries[key]           # re-insert so ordering stays honest
        self._entries[key] = (self.clock.now() + timedelta(seconds=self.ttl_s),
                              result)
        while len(self._entries) > self.max_entries:
            oldest = next(iter(self._entries))
            del self._entries[oldest]
            self.evictions += 1

    def purge_expired(self) -> int:
        now = self.clock.now()
        dead = [k for k, (expires, _) in self._entries.items() if now >= expires]
        for k in dead:
            del self._entries[k]
        return len(dead)

    def clear(self) -> None:
        self._entries.clear()

    def stats(self) -> dict:
        return {"entries": len(self._entries), "hits": self.hits,
                "misses": self.misses, "evictions": self.evictions,
                "ttl_s": self.ttl_s, "max_entries": self.max_entries}

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        """Membership does not count as a hit and does not resurrect an expired
        entry — it answers "is this live right now"."""
        entry = self._entries.get(key)
        return entry is not None and self.clock.now() < entry[0]

    def __repr__(self) -> str:               # pragma: no cover - trivial
        return f"ForecastCache(entries={len(self._entries)}, ttl_s={self.ttl_s})"


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

class ForecastService:
    """Runs registered forecasters and guarantees the shape of what comes back.

    The contract this class offers its callers is narrow and absolute:
    `run`/`run_all` return `ForecastResult`s and never raise on anything a
    forecaster does. Strategies therefore contain no `try` blocks around
    forecasting, which means there is no strategy-local error handling to get
    wrong, and every failure is visible in the same place — the abstention's
    `failure_code` — whether it came from bad data, a missing checkpoint, or a
    crash inside a third-party model.
    """

    def __init__(self, forecasters: Mapping[str, Forecaster] | None = None,
                 clock: Clock | None = None, audit: Any = None, *,
                 cache: ForecastCache | None = None):
        self.forecasters: dict[str, Any] = {}
        for name, forecaster in dict(forecasters or {}).items():
            if not name or not str(name).strip():
                raise ConfigurationError("forecaster name must be non-empty")
            if not hasattr(forecaster, "forecast") or not callable(
                    forecaster.forecast):
                raise ConfigurationError(
                    "forecaster must expose a callable .forecast()",
                    name=name, got=type(forecaster).__name__)
            self.forecasters[str(name)] = forecaster
        self.clock = clock or default_clock()
        self.audit = audit
        self.cache = cache
        #: Audit sinks are best-effort; a broken one must be countable, not fatal.
        self.audit_failures = 0

    # -- registration ------------------------------------------------------

    def register(self, name: str, forecaster: Forecaster) -> None:
        if name in self.forecasters:
            raise ConfigurationError(
                "a forecaster with this name is already registered; replacing "
                "it silently would make old audit records ambiguous", name=name)
        if not hasattr(forecaster, "forecast"):
            raise ConfigurationError("forecaster must expose .forecast()",
                                     name=name)
        self.forecasters[name] = forecaster

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self.forecasters)

    def describe(self) -> dict:
        out: dict[str, Any] = {}
        for name, forecaster in self.forecasters.items():
            try:
                out[name] = forecaster.describe()
            except Exception as exc:         # noqa: BLE001 - description is optional
                out[name] = {"name": name, "error": str(exc)}
        return out

    # -- running -----------------------------------------------------------

    def run(self, name: str, *, candles: Sequence[Candle],
            as_of: datetime | None = None, instrument_key: str = "",
            timeframe: str = "", correlation_id: str = "") -> ForecastResult:
        """Run one forecaster. Returns a `ForecastResult` in every case.

        Raises only `ConfigurationError`, and only for an unknown name — that is
        a wiring mistake, and turning it into an abstention would let a typo in
        a config file look like a market condition forever.
        """
        forecaster = self.forecasters.get(name)
        if forecaster is None:
            raise ConfigurationError("no such forecaster", name=name,
                                     known=sorted(self.forecasters))

        bars = list(candles or ())
        now = ensure_utc(as_of, field_name="as_of") if as_of is not None \
            else self.clock.now()
        key = instrument_key or (bars[-1].instrument_key if bars else "")
        tf = timeframe or (bars[-1].timeframe if bars else "")

        cache_key = self._cache_key(forecaster, key, tf, now)
        if cache_key is not None and self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        started = self.clock.monotonic_ns()
        try:
            result = forecaster.forecast(bars, as_of=now, instrument_key=key,
                                         timeframe=tf)
        except TradingError as exc:
            # A typed failure already carries a stable code; keep it, so the
            # abstention and the exception are the same event in the audit trail.
            result = self._abstention(
                key, tf, bars, now, forecaster,
                reason=f"{type(exc).__name__}: {exc.message}", code=exc.code)
        except Exception as exc:             # noqa: BLE001 - containment is the point
            result = self._abstention(
                key, tf, bars, now, forecaster,
                reason=f"{type(exc).__name__}: {exc}", code=CODE_UNEXPECTED)
        else:
            result = self._verify(result, key, tf, bars, now, forecaster)

        elapsed_ms = max(0.0, (self.clock.monotonic_ns() - started) / 1e6)
        if result.latency_ms is None:
            result = replace(result, latency_ms=elapsed_ms)

        self._record(name, result, correlation_id)
        if cache_key is not None and self.cache is not None:
            self.cache.put(cache_key, result)
        return result

    def run_all(self, *, candles: Sequence[Candle],
                as_of: datetime | None = None, instrument_key: str = "",
                timeframe: str = "",
                correlation_id: str = "") -> dict[str, ForecastResult]:
        """Run every registered forecaster over the same window.

        Insertion-ordered and fully isolated: one forecaster's failure changes
        nothing about the others' results, which is what makes a baseline
        comparison meaningful even on a day the learned model is broken.
        """
        return {name: self.run(name, candles=candles, as_of=as_of,
                               instrument_key=instrument_key, timeframe=timeframe,
                               correlation_id=correlation_id)
                for name in tuple(self.forecasters)}

    # -- internals ---------------------------------------------------------

    def _cache_key(self, forecaster: Any, instrument_key: str, timeframe: str,
                   as_of: datetime) -> str | None:
        """Build the cache key, or None if this forecaster cannot describe
        itself well enough to be cached safely.

        Failing to *not* cache is the safe direction: an un-keyable forecaster
        simply runs every time.
        """
        if self.cache is None:
            return None
        try:
            version = str(forecaster.model_version)
            digest = forecaster.params_hash()
        except Exception:                    # noqa: BLE001
            return None
        return ForecastCache.key(instrument_key=instrument_key,
                                 timeframe=timeframe, as_of=as_of,
                                 model_version=version, params_hash=digest)

    def _verify(self, result: Any, key: str, tf: str, bars: Sequence[Candle],
                now: datetime, forecaster: Any) -> ForecastResult:
        """Refuse a result that is not what it claims to be."""
        if not isinstance(result, ForecastResult):
            return self._abstention(
                key, tf, bars, now, forecaster,
                reason=f"forecaster returned {type(result).__name__}, "
                       "not a ForecastResult", code=CODE_CONTRACT)
        if key and result.instrument_key and result.instrument_key != key:
            return self._abstention(
                key, tf, bars, now, forecaster,
                reason=f"forecaster returned a result for "
                       f"{result.instrument_key!r} when asked for {key!r}",
                code=CODE_CONTRACT)
        return result

    def _abstention(self, key: str, tf: str, bars: Sequence[Candle],
                    now: datetime, forecaster: Any, *, reason: str,
                    code: str) -> ForecastResult:
        start = bars[0].ts_open if bars else now
        end = bars[-1].ts_close if bars else now
        start = min(start, now)
        end = min(max(end, start), now)
        version, identity, params = "unknown", {}, {}
        try:                                  # a broken forecaster may fail here too
            version = str(forecaster.model_version)
            identity = {"class": type(forecaster).__name__}
            params = jsonable(dict(forecaster.params))
        except Exception:                     # noqa: BLE001
            pass
        horizon = int(getattr(forecaster, "horizon", 0) or 0)
        return ForecastResult.abstain(
            instrument_key=key, timeframe=tf, input_start=start, input_end=end,
            created_at=now, horizon=horizon, reason=reason, code=code,
            model_version=version, model_identity=identity,
            inference_params=params)

    def _record(self, name: str, result: ForecastResult,
                correlation_id: str) -> None:
        """Best-effort audit.

        Deliberately swallowing: the pipeline records its own copy of every
        forecast, and a failing sink must not be able to stop the trading loop.
        The failure is counted so a monitor can alert on a silent audit sink.
        """
        if self.audit is None:
            return
        try:
            record = getattr(self.audit, "record", None)
            if callable(record):
                record("FORECAST_PRODUCED", jsonable(result), actor=name,
                       subject=result.instrument_key,
                       correlation_id=correlation_id)
            elif callable(self.audit):
                self.audit(name, result)
        except Exception:                     # noqa: BLE001
            self.audit_failures += 1

    def __repr__(self) -> str:               # pragma: no cover - trivial
        return f"ForecastService(forecasters={sorted(self.forecasters)})"


__all__ = [
    "CODE_CONTRACT", "CODE_INSUFFICIENT", "CODE_INVALID", "CODE_LOOKAHEAD",
    "CODE_STALE", "CODE_UNEXPECTED",
    "params_hash", "Forecaster", "BaselineForecaster", "PersistenceForecaster",
    "DriftForecaster", "SeasonalNaiveForecaster", "ForecastCache",
    "ForecastService",
]
