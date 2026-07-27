"""Event-driven backtester.

The design goal is not speed, it is **not lying**. A backtester's only job is to
produce a number you can act on, and the ways it silently fails to are well
known. Each is closed structurally here rather than by convention:

* **Look-ahead.** Bars are delivered strictly in `ts_close` order and only after
  the clock has passed that close. `validate.assert_no_lookahead` runs on every
  window handed to the strategy — not once at the start, every bar — and
  `assert_deliverable` refuses a bar the clock has not reached at all.
* **Replayed or duplicated bars.** Every `(instrument, ts_close)` may be
  delivered exactly once and timestamps are strictly non-decreasing. A repeated
  bar is a `DataValidationError`, not a doubled position.
* **Fill-at-signal-price.** Orders go to `PaperBroker`, which stages them and
  refuses to fill against a bar that was already open when the order was
  created. A market order therefore fills at the *next* bar's open, plus
  slippage. `latency_bars > 1` delays the submission itself on top of that.
* **Costless trading.** Fees and slippage are always modelled, and the report
  carries gross and net side by side (`perf.PerformanceReport` cannot be built
  with only one), so the difference cannot be hidden.
* **Forward-filled holes.** A missing bar is never invented. When an instrument
  goes silent for longer than the configured tolerance the engine warns, keeps
  the stale mark visibly stale, and skips the first post-gap strategy call
  because that window straddles a hole.
* **Different code from live.** The engine drives the *real* `RiskEngine`,
  `OrderStore`, `ExecutionEngine`, `PortfolioManager` and `PaperBroker`. The
  only things that differ from paper trading are the clock and the data source.
  A strategy that trades here runs unchanged in paper.
* **Survivorship.** The universe changes over time via `listings` (an inclusion
  date and a delisting date per instrument). When it does not, and the window is
  long, the engine warns rather than letting a static survivor set pass
  unremarked — it cannot *remove* the bias, so it names it.

What it does not do: intrabar simulation. Fills are evaluated against a bar's
OHLC, so a stop and a limit that would both trigger within one bar are resolved
by a documented rule, not by tick data. That is a real limitation and it is
stated in the result's warnings rather than buried.

Walk-forward
------------
`WalkForward` evaluates a sequence of `WalkForwardWindow`s and returns the
out-of-sample segments *only*. Windows validate that their training span ends
before their test span begins, so an overlapping split is a construction error
rather than a quietly optimistic track record.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from . import perf
from .clock import FixedClock
from .contracts import (Candle, DataQuality, Instrument, Mode, Order, Side,
                        TradeIntent, ensure_utc, jsonable, to_decimal)
from .errors import ConfigurationError, DataValidationError, TradingError
from .oms import OrderStore
from .perf import PerformanceReport
from .portfolio import PortfolioManager
from .validate import assert_no_lookahead

_ZERO = Decimal("0")

#: Disclosed on every run. Not a warning about *this* run's data — a statement
#: about what bar-resolution simulation can and cannot know.
INTRABAR_CAVEAT = (
    "fills are evaluated against bar OHLC, not ticks: a stop and a limit that "
    "would both trigger inside one bar are resolved by rule, not by observed "
    "sequence")


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BacktestConfig:
    """Everything that changes a result, and nothing that does not.

    `hash()` covers every field, so two runs that report the same `config_hash`
    really were configured identically. A knob that affected results without
    entering the hash would make two incomparable runs look comparable, which is
    worse than having no hash at all.
    """
    timeframe: str = "1d"
    #: Inclusive bounds on `ts_close`. `None` means "whatever the data covers".
    start: datetime | None = None
    end: datetime | None = None
    #: The declared universe. Empty means "every key present in `data`";
    #: naming it explicitly is what lets the engine notice an instrument that
    #: went silent rather than inferring the universe from surviving bars.
    instruments: tuple[str, ...] = ()
    starting_cash: Decimal = Decimal("100000")
    #: `brokers.paper.FeeModel` / `SlippageModel`. `None` uses the broker's
    #: defaults, which are deliberately non-zero.
    fee_model: Any = None
    slippage_model: Any = None
    #: Bars of history a strategy needs before it may trade. No intents are
    #: accepted before this many bars have been delivered for that instrument.
    warmup_bars: int = 20
    #: Longest window handed to a strategy in one call. Bounds memory and makes
    #: the strategy's input size explicit rather than "everything so far".
    max_bars_per_strategy_call: int = 512
    #: Minimum bars between a signal and its earliest fill. Must be >= 1 — zero
    #: would mean filling on the bar that produced the signal.
    latency_bars: int = 1
    seed: int = 0
    #: 24/7 venue. Drives annualisation; see `perf.periods_per_year`.
    continuous_market: bool = False
    #: Warn when the universe never changes over a window longer than this.
    survivorship_warn_days: int = 365
    #: How many bar-durations of silence before an instrument's mark is called
    #: stale and its next window is skipped as discontinuous.
    max_mark_staleness_bars: int = 3
    risk_free_rate: float = 0.0

    def __post_init__(self):
        if self.latency_bars < 1:
            raise ConfigurationError(
                "latency_bars must be >= 1; zero means filling on the bar that "
                "produced the signal, which is the classic backtest lie",
                got=self.latency_bars)
        if self.warmup_bars < 0:
            raise ConfigurationError("warmup_bars must be >= 0")
        if self.max_bars_per_strategy_call < 1:
            raise ConfigurationError("max_bars_per_strategy_call must be >= 1")
        if self.max_mark_staleness_bars < 1:
            raise ConfigurationError("max_mark_staleness_bars must be >= 1")
        object.__setattr__(self, "starting_cash",
                           to_decimal(self.starting_cash,
                                      field_name="starting_cash"))
        if self.starting_cash <= 0:
            raise ConfigurationError("starting_cash must be > 0")
        object.__setattr__(self, "instruments", tuple(self.instruments))
        for name in ("start", "end"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, ensure_utc(value, field_name=name))
        if (self.start is not None and self.end is not None
                and self.end < self.start):
            raise ConfigurationError("end must not precede start")

    @property
    def max_lookback(self) -> int:
        """Historical spelling of `max_bars_per_strategy_call`."""
        return self.max_bars_per_strategy_call

    def _cost_fingerprint(self) -> dict:
        """Fee/slippage models reduced to their fields.

        Included in the hash because a run with zero fees and a run with real
        fees are different experiments, and the whole point of reporting gross
        beside net is that the difference is material.
        """
        out = {}
        for name, model in (("fee", self.fee_model),
                            ("slippage", self.slippage_model)):
            if model is None:
                out[name] = None
                continue
            try:
                out[name] = {f.name: str(getattr(model, f.name))
                             for f in fields(model)}
            except TypeError:                    # not a dataclass
                out[name] = repr(model)
        return out

    def hash(self) -> str:
        payload = {
            "timeframe": self.timeframe,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "instruments": list(self.instruments),
            "starting_cash": str(self.starting_cash),
            "warmup_bars": self.warmup_bars,
            "max_bars_per_strategy_call": self.max_bars_per_strategy_call,
            "latency_bars": self.latency_bars,
            "seed": self.seed,
            "continuous_market": self.continuous_market,
            "survivorship_warn_days": self.survivorship_warn_days,
            "max_mark_staleness_bars": self.max_mark_staleness_bars,
            "risk_free_rate": self.risk_free_rate,
            "costs": self._cost_fingerprint(),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# the strategy's view of the world
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyContext:
    """The immutable window a strategy decides from.

    Frozen, and `candles` is a tuple rather than the engine's own list. A
    strategy that mutated the engine's history would corrupt every later bar in
    a way that looks like alpha; making the container immutable means the
    attempt raises instead of succeeding quietly.
    """
    as_of: datetime
    instrument: Instrument
    timeframe: str
    candles: tuple[Candle, ...]
    bar: Candle
    portfolio: Any
    clock: Any
    position: Any = None
    equity: Decimal = _ZERO
    #: Present for interface parity with the live pipeline, which fills them in.
    forecasts: Mapping[str, Any] = field(default_factory=dict)
    signals: tuple = ()
    fused: Any = None
    quote: Any = None
    regime: Any = None
    seed: int = 0


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BacktestResult:
    """A run, in a form that can be compared to another run.

    `config_hash` and `data_hash` are what make "we improved the strategy"
    checkable: two results with different data hashes are two different
    experiments, however similar their Sharpe ratios look.
    """
    equity_curve: tuple[tuple[datetime, Decimal], ...]
    cost_curve: tuple[tuple[datetime, Decimal], ...]
    trades: tuple[Any, ...]
    orders: tuple[Order, ...]
    fills: tuple[Any, ...]
    intents: tuple[TradeIntent, ...]
    decisions: tuple[Any, ...]
    report: PerformanceReport
    per_instrument: Mapping[str, PerformanceReport]
    per_regime: Mapping[str, PerformanceReport]
    per_period: Mapping[str, PerformanceReport]
    bars_processed: int
    config_hash: str
    data_hash: str
    warnings: tuple[str, ...] = ()

    @property
    def rejected(self) -> tuple[Any, ...]:
        return tuple(d for d in self.decisions if not d.approved)

    @property
    def final_equity(self) -> Decimal:
        return self.equity_curve[-1][1] if self.equity_curve else _ZERO

    def to_dict(self) -> dict:
        return {
            "equity_curve": [[jsonable(t), str(v)] for t, v in self.equity_curve],
            "n_trades": len(self.trades), "n_orders": len(self.orders),
            "n_fills": len(self.fills), "n_intents": len(self.intents),
            "n_rejected": len(self.rejected),
            "report": self.report.to_dict(),
            "per_instrument": {k: v.to_dict() for k, v in self.per_instrument.items()},
            "per_regime": {k: v.to_dict() for k, v in self.per_regime.items()},
            "per_period": {k: v.to_dict() for k, v in self.per_period.items()},
            "bars_processed": self.bars_processed,
            "config_hash": self.config_hash, "data_hash": self.data_hash,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Replays history through the real decision stack."""

    def __init__(self, config: BacktestConfig | None = None,
                 data: Mapping[str, Sequence[Candle]] | None = None,
                 strategy: Any = None, risk_engine: Any = None,
                 instruments: Mapping[str, Instrument] | None = None,
                 clock: FixedClock | None = None, *,
                 broker: Any = None,
                 listings: Mapping[str, tuple] | None = None,
                 regimes: Mapping[datetime, Any] | Callable | None = None,
                 regime_classifier: Any = None,
                 audit: Any = None):
        if not data:
            raise ConfigurationError("backtest needs at least one instrument")
        if strategy is None or risk_engine is None:
            raise ConfigurationError(
                "a backtest needs both a strategy and the real risk engine; "
                "running without the risk engine would test code that never "
                "reaches production")
        self.config = config or BacktestConfig()
        self.instruments = dict(instruments or {})
        self.strategy = strategy
        self.risk_engine = risk_engine
        self.listings = dict(listings or {})
        self.regimes = regimes
        self.regime_classifier = regime_classifier
        self.audit = audit

        cfg = self.config
        self.universe: tuple[str, ...] = (cfg.instruments
                                          or tuple(sorted(data.keys())))
        self._stream = self._build_stream(data)
        self._slices = self._group_by_close(self._stream)

        from .instruments import parse_timeframe
        self._step = parse_timeframe(cfg.timeframe)
        self._gap_threshold = self._step * (1 + cfg.max_mark_staleness_bars)

        # Start one microsecond before the first close so the very first
        # `clock.set` is a genuine forward move and `assert_deliverable` is
        # meaningful on bar one rather than trivially true.
        self.clock = FixedClock(self._stream[0].ts_close - timedelta(microseconds=1))
        run_id = f"{cfg.hash()}.{self._data_hash()}"
        self.portfolio = PortfolioManager(starting_cash=cfg.starting_cash,
                                          clock=self.clock,
                                          account_id=f"bt-{run_id}")
        self.orders = OrderStore(clock=self.clock,
                                 namespace=f"trading.bt.orders.{run_id}")
        if broker is None:
            from .brokers.paper import PaperBroker
            broker = PaperBroker(clock=self.clock, instruments=self.instruments,
                                 starting_cash=cfg.starting_cash,
                                 fee_model=cfg.fee_model,
                                 slippage_model=cfg.slippage_model,
                                 seed=cfg.seed)
        self.broker = broker
        self.broker.connect()

    # -- stream construction ----------------------------------------------

    def _build_stream(self, data: Mapping[str, Sequence[Candle]]) -> list[Candle]:
        """Merge every instrument's bars into one chronological stream.

        Sorting by `ts_close` (never `ts_open`) is what makes "deliver only what
        has closed" expressible at all: a 1-day bar that opened yesterday is not
        knowable until it closes, and ordering by open would hand it over a full
        bar early.
        """
        cfg = self.config
        merged: list[Candle] = []
        seen: set[tuple[str, datetime]] = set()
        for key, bars in data.items():
            if cfg.instruments and key not in cfg.instruments:
                continue
            for bar in bars:
                if not bar.is_final:
                    # A non-final bar's close has not happened yet. Replaying
                    # one is look-ahead by construction.
                    continue
                if cfg.start is not None and bar.ts_close < cfg.start:
                    continue
                if cfg.end is not None and bar.ts_close > cfg.end:
                    continue
                identity = (bar.instrument_key, bar.ts_close)
                if identity in seen:
                    raise DataValidationError(
                        "duplicate bar in backtest data; replaying the same bar "
                        "twice doubles every position it opens",
                        instrument=bar.instrument_key,
                        ts_close=bar.ts_close.isoformat())
                seen.add(identity)
                merged.append(bar)
        merged.sort(key=lambda c: (c.ts_close, c.instrument_key))
        if not merged:
            raise ConfigurationError("no final candles to replay")
        return merged

    @staticmethod
    def _group_by_close(stream: Sequence[Candle]) -> list[list[Candle]]:
        """One list per distinct `ts_close`.

        The loop advances a *time slice* at a time, not a bar at a time, so a
        multi-instrument portfolio is marked and valued once per timestamp. Bar-
        at-a-time marking would record intermediate equity states that no real
        portfolio ever held and invent drawdowns out of dict ordering.
        """
        slices: list[list[Candle]] = []
        current: list[Candle] = []
        for bar in stream:
            if current and bar.ts_close != current[0].ts_close:
                slices.append(current)
                current = []
            current.append(bar)
        if current:
            slices.append(current)
        return slices

    # -- guards ------------------------------------------------------------

    def assert_deliverable(self, bar: Candle) -> None:
        """Refuse a bar the clock has not yet reached.

        The loop runs this on every bar before the strategy sees anything. It is
        public because "the guard exists" is a claim that deserves a test of its
        own rather than being inferred from the absence of failures.
        """
        now = self.clock.now()
        if bar.ts_close > now:
            raise DataValidationError(
                "look-ahead: bar closes after the engine clock",
                instrument=bar.instrument_key,
                ts_close=bar.ts_close.isoformat(), as_of=now.isoformat())
        if not bar.is_final:
            raise DataValidationError(
                "a non-final bar has no knowable close",
                instrument=bar.instrument_key,
                ts_close=bar.ts_close.isoformat())

    # -- the loop ----------------------------------------------------------

    def run(self) -> BacktestResult:
        cfg = self.config
        history: dict[str, list[Candle]] = {}
        last_close: dict[str, datetime] = {}
        discontinuous: set[str] = set()
        intents: list[TradeIntent] = []
        decisions: list[Any] = []
        orders: list[Order] = []
        fills: list[Any] = []
        warnings: list[str] = []
        equity_curve: list[tuple[datetime, Decimal]] = []
        cost_curve: list[tuple[datetime, Decimal]] = []
        regime_series: dict[datetime, Any] = {}
        pending: list[tuple[str, int, TradeIntent, Any, Instrument, Candle]] = []
        bar_index: dict[str, int] = {}
        delivered = 0
        slices_in_market = 0
        traded_notional = _ZERO
        slippage_cost = _ZERO
        previous_ts: datetime | None = None

        from .execution import ExecutionConfig, ExecutionEngine
        execution = ExecutionEngine(
            broker=self.broker, order_store=self.orders,
            portfolio=self.portfolio, clock=self.clock, audit=self.audit,
            mode=Mode.BACKTEST,
            # A backtest's "decision age" is zero by construction and its
            # reference price is the bar it just saw, so the live staleness and
            # collar guards would only ever fire on the FixedClock's arithmetic.
            config=ExecutionConfig(max_decision_age_s=10 ** 9,
                                   max_price_deviation_pct=10 ** 6))

        # A backtest must be reproducible AND isolated. The order namespace is
        # derived from (config, data) so re-running identical inputs is
        # deterministic, but it MUST be cleared first: otherwise a second run
        # finds the first run's orders, `get_or_create` short-circuits, nothing
        # reaches the broker, and the two runs silently disagree. Found exactly
        # that way — two identical runs produced different equity curves.
        self._clear_run_state()
        if self.regime_classifier is not None:
            reset = getattr(self.regime_classifier, "reset", None)
            if callable(reset):
                reset()

        for slice_bars in self._slices:
            ts = slice_bars[0].ts_close
            # 1. Time advances to this slice's close. `FixedClock.set` refuses
            #    to move backwards, so an out-of-order stream raises here rather
            #    than quietly re-deciding the past.
            if previous_ts is not None and ts <= previous_ts:
                raise DataValidationError(
                    "backtest stream is not strictly increasing in ts_close",
                    previous=previous_ts.isoformat(), got=ts.isoformat())
            previous_ts = ts
            self.clock.set(ts)

            live_bars = [b for b in slice_bars
                         if self._is_listed(b.instrument_key, ts.date())]

            # 2. Drive the venue FIRST: orders staged on earlier bars fill
            #    against this one, which is what enforces the latency floor. A
            #    fill produced here happened at this bar's OPEN, before any
            #    decision made from its close.
            for bar in live_bars:
                self.assert_deliverable(bar)
                for fill in self.broker.on_candle(bar):
                    fills.append(fill)
                    try:
                        self.orders.record_fill(fill)
                    except TradingError:
                        # The store is a mirror, not the source of truth for
                        # P&L; a rejected mirror update must not lose the fill.
                        pass
                    instrument = self.instruments.get(fill.instrument_key)
                    self.portfolio.apply_fill(
                        fill, instrument,
                        strategy_id=getattr(self.strategy, "id", ""))
                    multiplier = (instrument.multiplier if instrument
                                  else Decimal("1"))
                    traded_notional += abs(fill.quantity) * fill.price * multiplier
                    slippage_cost += self._slippage_cost(fill, bar, multiplier)

            # 3. Only now does the bar become history and a mark.
            for bar in live_bars:
                previous_close = last_close.get(bar.instrument_key)
                if (previous_close is not None
                        and bar.ts_open - previous_close > self._gap_threshold):
                    # Missing data is reported, never interpolated. The next
                    # window straddles the hole, so it is not handed out.
                    discontinuous.add(bar.instrument_key)
                    message = (f"{bar.instrument_key}: no data between "
                               f"{previous_close.isoformat()} and "
                               f"{bar.ts_open.isoformat()}; the gap is left "
                               "open rather than forward-filled")
                    if message not in warnings:
                        warnings.append(message)
                last_close[bar.instrument_key] = bar.ts_close

                window = history.setdefault(bar.instrument_key, [])
                window.append(bar)
                if len(window) > cfg.max_bars_per_strategy_call:
                    del window[:-cfg.max_bars_per_strategy_call]
                bar_index[bar.instrument_key] = bar_index.get(
                    bar.instrument_key, 0) + 1
                delivered += 1
                self.portfolio.mark(bar.instrument_key, bar.close)

            # 4. One equity point per timestamp, after fills and marks.
            equity = self.portfolio.snapshot(ts).equity
            equity_curve.append((ts, equity))
            cost_curve.append((ts, self.portfolio.fees_paid + slippage_cost))
            if any(not p.is_flat for p in self.portfolio.positions().values()):
                slices_in_market += 1

            # 5. The strategy sees only closed bars, re-checked every time.
            for bar in live_bars:
                key = bar.instrument_key
                window = history[key]
                if key in discontinuous:
                    discontinuous.discard(key)
                    continue
                if len(window) <= cfg.warmup_bars:
                    continue
                instrument = self.instruments.get(key)
                if instrument is None:
                    continue

                view = tuple(window)
                assert_no_lookahead(view, self.clock.now())
                regime = self._regime(view, regime_series, ts)

                for intent in self._propose(instrument, view, bar, regime):
                    intents.append(intent)
                    decision = self._authorise(intent, instrument, bar)
                    decisions.append(decision)
                    if not decision.approved:
                        continue
                    # 6. Approved orders wait out the configured latency. The
                    #    broker adds one more bar on top, so `latency_bars=1`
                    #    means "submitted now, filled at the next open".
                    pending.append((key, bar_index[key] + cfg.latency_bars - 1,
                                    intent, decision, instrument, bar))

            # 7. Release anything whose latency has elapsed.
            still_waiting = []
            for entry in pending:
                key, due, intent, decision, instrument, bar = entry
                if bar_index.get(key, 0) < due:
                    still_waiting.append(entry)
                    continue
                try:
                    orders.append(execution.execute(
                        intent, decision, instrument, reference_price=bar.close))
                except TradingError:
                    # A refused execution is a real outcome (size rounds to
                    # zero, mode forbids it); the decision is already recorded.
                    continue
            pending = still_waiting

        warnings.extend(self._structural_warnings())
        report, per_instrument, per_regime, per_period = self._reports(
            equity_curve, cost_curve, regime_series,
            exposure=(slices_in_market / len(self._slices)) if self._slices else 0.0,
            turnover=self._turnover(traded_notional, equity_curve),
            slippage=slippage_cost, warnings=warnings)

        return BacktestResult(
            equity_curve=tuple(equity_curve), cost_curve=tuple(cost_curve),
            trades=tuple(self.portfolio.trades), orders=tuple(orders),
            fills=tuple(fills), intents=tuple(intents),
            decisions=tuple(decisions), report=report,
            per_instrument=per_instrument, per_regime=per_regime,
            per_period=per_period, bars_processed=delivered,
            config_hash=self.config.hash(), data_hash=self._data_hash(),
            warnings=tuple(warnings))

    # -- reporting ---------------------------------------------------------

    def _reports(self, equity_curve, cost_curve, regime_series, *, exposure,
                 turnover, slippage, warnings):
        cfg = self.config
        trades = self.portfolio.trades
        if not equity_curve:                             # pragma: no cover
            empty = PerformanceReport.empty(timeframe=cfg.timeframe)
            return empty, {}, {}, {}
        common = dict(timeframe=cfg.timeframe, continuous=cfg.continuous_market,
                      risk_free_rate=cfg.risk_free_rate)
        report = perf.compute_report(
            equity_curve, trades, cost_curve=cost_curve,
            fees=self.portfolio.fees_paid, slippage=slippage,
            exposure=exposure, turnover=turnover,
            label=f"backtest {getattr(self.strategy, 'id', '?')}",
            warnings=warnings, **common)
        start_equity = equity_curve[0][1]
        per_instrument = perf.by_instrument(trades, starting_equity=start_equity,
                                            **common)
        per_regime = perf.by_regime(trades, regime_series or self.regimes,
                                    starting_equity=start_equity, **common)
        per_period = perf.by_period(equity_curve, "M", trades=trades,
                                    cost_curve=cost_curve, **common)
        return report, per_instrument, per_regime, per_period

    @staticmethod
    def _turnover(traded_notional: Decimal,
                  equity_curve: Sequence[tuple[datetime, Decimal]]) -> float:
        """Traded notional over average equity.

        Counting orders instead (an easy mistake) makes a strategy that trades
        one share look as busy as one that trades the whole book.
        """
        if not equity_curve:
            return 0.0
        total = sum((value for _, value in equity_curve), _ZERO)
        average = total / len(equity_curve)
        if average <= 0:
            return 0.0
        return float(traded_notional / average)

    def _structural_warnings(self) -> list[str]:
        out: list[str] = []
        cfg = self.config
        if not self.listings:
            span = (self._stream[-1].ts_close - self._stream[0].ts_close).days
            if span > cfg.survivorship_warn_days:
                out.append(
                    f"the instrument universe is static over {span} days; if it "
                    "was chosen from instruments that exist today, these results "
                    "carry survivorship bias that this engine cannot remove")
        out.append(INTRABAR_CAVEAT)
        return out

    # -- helpers -----------------------------------------------------------

    def _slippage_cost(self, fill, bar: Candle, multiplier: Decimal) -> Decimal:
        """Modelled execution shortfall for one fill.

        Measured against the price a frictionless venue would have given:
        the bar's open for a market order, the stop for a triggered stop. A
        limit fill contributes nothing — by construction it can never fill worse
        than its limit, so calling the difference "slippage" would invent a cost
        that was never paid.
        """
        order = None
        getter = getattr(self.broker, "get_order", None)
        if callable(getter):
            order = getter(fill.client_order_id)
        from .contracts import OrderType
        if order is None:
            reference = to_decimal(bar.open, field_name="open")
        elif order.order_type is OrderType.MARKET:
            reference = to_decimal(bar.open, field_name="open")
        elif order.order_type is OrderType.STOP and order.stop_price is not None:
            reference = order.stop_price
        else:
            return _ZERO
        return abs(fill.price - reference) * abs(fill.quantity) * multiplier

    def _regime(self, window: Sequence[Candle],
                series: dict[datetime, Any], ts: datetime) -> Any:
        if self.regime_classifier is None:
            if callable(self.regimes):
                return None
            if isinstance(self.regimes, Mapping):
                return self.regimes.get(ts)
            return None
        try:
            state = self.regime_classifier.classify(window)
        except TradingError:
            return None
        regime = getattr(state, "regime", state)
        series[ts] = regime
        return regime

    def _clear_run_state(self) -> None:
        """Drop any persisted orders/fills from a previous run of this exact
        (config, data) pair, so repeated runs are independent."""
        from olympus import store
        backend = store.backend()
        for namespace in (self.orders.namespace, self.orders.fill_namespace,
                          self.orders.broker_index_namespace):
            try:
                for key in backend.keys(namespace):
                    backend.delete(namespace, key)
            except Exception:                            # noqa: BLE001
                pass

    def _propose(self, instrument, window, bar, regime) -> list[TradeIntent]:
        ctx = StrategyContext(
            as_of=self.clock.now(), instrument=instrument,
            timeframe=self.config.timeframe, candles=window, bar=bar,
            portfolio=self.portfolio.snapshot(), clock=self.clock,
            position=self.portfolio.position(instrument.key),
            equity=self.portfolio.snapshot().equity,
            regime=regime, seed=self.config.seed)
        try:
            proposed = self.strategy.on_bar(ctx)
        except Exception:                                # noqa: BLE001
            # A strategy that raises is a strategy that produced no intent. It
            # must not abort the run — the alternative is a backtest that
            # silently stops early and reports the truncated curve as complete.
            return []
        return [i for i in (proposed or ()) if isinstance(i, TradeIntent)]

    def _authorise(self, intent, instrument, bar):
        from .risk import RiskContext
        snapshot = self.portfolio.snapshot()
        ctx = RiskContext(
            as_of=self.clock.now(), mode=Mode.BACKTEST, instrument=instrument,
            portfolio=snapshot, reference_price=bar.close, candle=bar,
            data_age_s=0.0, data_quality=DataQuality.OK, session_open=True,
            broker_connected=True, reconciliation_age_s=0.0,
            recent_order_count=0,
            daily_pnl=self.portfolio.daily_pnl(self.clock.now()),
            strategy_id=intent.strategy_id, equity=snapshot.equity)
        return self.risk_engine.authorise(intent, ctx)

    def _is_listed(self, key: str, day: date) -> bool:
        """Was `key` in the tradable universe on `day`?

        `listings[key] == (inclusion_date, delisting_date)`; either may be
        `None` for "always". An instrument absent from the map is assumed
        always-listed, which is exactly the survivorship assumption the engine
        warns about when the map is empty.
        """
        window = self.listings.get(key)
        if not window:
            return True
        start, end = window
        if start and day < start:
            return False
        if end and day > end:
            return False
        return True

    def _data_hash(self) -> str:
        h = hashlib.sha256()
        for bar in self._stream:
            h.update(f"{bar.instrument_key}|{bar.ts_open.isoformat()}|"
                     f"{bar.ts_close.isoformat()}|{bar.open}|{bar.high}|"
                     f"{bar.low}|{bar.close}|{bar.volume}".encode())
        return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# walk-forward
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WalkForwardWindow:
    """One train/test split, validated to be a split rather than an overlap.

    The whole value of walk-forward is the guarantee that the reported numbers
    come from data the fitting never touched. That guarantee is one comparison
    wide, so it is enforced at construction: `train_end <= test_start`, always.
    """
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    label: str = ""

    def __post_init__(self):
        for name in ("train_start", "train_end", "test_start", "test_end"):
            object.__setattr__(self, name,
                               ensure_utc(getattr(self, name), field_name=name))
        if self.train_end < self.train_start:
            raise ConfigurationError("train_end precedes train_start")
        if self.test_end < self.test_start:
            raise ConfigurationError("test_end precedes test_start")
        if self.test_start < self.train_end:
            raise ConfigurationError(
                "the test window overlaps the training window; every metric "
                "produced from an overlapping split is in-sample and will not "
                "survive contact with new data",
                train_end=self.train_end.isoformat(),
                test_start=self.test_start.isoformat())
        if not self.label:
            object.__setattr__(self, "label",
                               f"{self.test_start.date()}..{self.test_end.date()}")

    def to_dict(self) -> dict:
        return {f.name: jsonable(getattr(self, f.name)) for f in fields(self)}


def rolling_windows(start: datetime, end: datetime, *, train: timedelta,
                    test: timedelta, step: timedelta | None = None
                    ) -> list[WalkForwardWindow]:
    """Fixed-length training window that slides forward with the test window."""
    start = ensure_utc(start, field_name="start")
    end = ensure_utc(end, field_name="end")
    if train <= timedelta(0) or test <= timedelta(0):
        raise ConfigurationError("train and test spans must be positive")
    step = step or test
    if step <= timedelta(0):
        raise ConfigurationError("step must be positive")
    out: list[WalkForwardWindow] = []
    cursor = start
    while cursor + train + test <= end:
        out.append(WalkForwardWindow(
            train_start=cursor, train_end=cursor + train,
            test_start=cursor + train, test_end=cursor + train + test))
        cursor += step
    return out


def anchored_windows(start: datetime, end: datetime, *, train: timedelta,
                     test: timedelta, step: timedelta | None = None
                     ) -> list[WalkForwardWindow]:
    """Training window that always begins at `start` and only grows.

    Anchored is the honest default when the strategy is meant to learn from all
    available history; rolling is honest when it is meant to adapt. Choosing one
    silently is how a walk-forward becomes a hyperparameter.
    """
    windows = rolling_windows(start, end, train=train, test=test, step=step)
    return [WalkForwardWindow(train_start=start, train_end=w.train_end,
                              test_start=w.test_start, test_end=w.test_end)
            for w in windows]


@dataclass(frozen=True)
class WalkForwardResult:
    """Out-of-sample segments and their stitched aggregate.

    There is deliberately no in-sample field. A `WalkForwardResult` that carried
    training results would be one attribute access away from a track record made
    of curve fits.
    """
    windows: tuple[WalkForwardWindow, ...]
    results: tuple[BacktestResult, ...]
    combined: PerformanceReport
    combined_curve: tuple[tuple[datetime, Decimal], ...]
    warnings: tuple[str, ...] = ()

    @property
    def n_windows(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict:
        return {
            "windows": [w.to_dict() for w in self.windows],
            "results": [r.to_dict() for r in self.results],
            "combined": self.combined.to_dict(),
            "warnings": list(self.warnings),
        }


class WalkForward:
    """Rolling or anchored walk-forward evaluation.

    `engine_factory(window)` returns a configured `BacktestEngine` restricted to
    that window's *test* span. Fitting, if any, happens inside the factory using
    the training span — the engine never sees it, which is why the returned
    results are out-of-sample by construction rather than by discipline.
    """

    def __init__(self, engine_factory: Callable[[WalkForwardWindow], Any],
                 windows: Sequence[WalkForwardWindow]):
        if not callable(engine_factory):
            raise ConfigurationError("engine_factory must be callable")
        windows = tuple(windows)
        if not windows:
            raise ConfigurationError("walk-forward needs at least one window")
        for a, b in zip(windows, windows[1:]):
            if b.test_start < a.test_end:
                raise ConfigurationError(
                    "walk-forward test windows overlap each other; the same "
                    "bars would be counted twice in the combined report",
                    first=a.label, second=b.label)
        self.engine_factory = engine_factory
        self.windows = windows

    def run(self) -> WalkForwardResult:
        results: list[BacktestResult] = []
        warnings: list[str] = []
        for window in self.windows:
            engine = self.engine_factory(window)
            if engine is None:
                warnings.append(f"{window.label}: no engine produced, skipped")
                continue
            results.append(engine.run())

        combined_curve = self._stitch(results)
        trades = [t for r in results for t in r.trades]
        fees = sum((r.report.total_fees for r in results), _ZERO)
        slippage = sum((r.report.total_slippage for r in results), _ZERO)
        timeframe = (results[0].report.timeframe if results else "1d")
        combined = perf.compute_report(
            combined_curve, trades, timeframe=timeframe, fees=fees,
            slippage=slippage,
            exposure=(sum(r.report.exposure for r in results) / len(results)
                      if results else 0.0),
            turnover=sum(r.report.turnover for r in results),
            label="walk-forward (out-of-sample only)",
            warnings=warnings)
        return WalkForwardResult(
            windows=self.windows, results=tuple(results), combined=combined,
            combined_curve=tuple(combined_curve), warnings=tuple(warnings))

    @staticmethod
    def _stitch(results: Sequence[BacktestResult]
                ) -> list[tuple[datetime, Decimal]]:
        """Chain the out-of-sample curves by *return*, not by level.

        Each window restarts from the configured starting cash, so concatenating
        raw equity levels would draw a sawtooth and report a drawdown at every
        window boundary that no account ever suffered. Compounding the windows'
        returns onto a running equity is the only join that preserves both the
        total return and the drawdown shape.
        """
        curve: list[tuple[datetime, Decimal]] = []
        running: Decimal | None = None
        for result in results:
            segment = result.equity_curve
            if len(segment) < 2:
                continue
            base = segment[0][1]
            if base <= 0:
                continue
            if running is None:
                running = base
                curve.append((segment[0][0], running))
            for ts, value in segment[1:]:
                running = running * (value / base)
                base = value
                curve.append((ts, running))
        return curve


def walk_forward(*, data: Mapping[str, Sequence[Candle]], make_engine,
                 train_bars: int, test_bars: int, step: int | None = None
                 ) -> list[BacktestResult]:
    """Bar-indexed convenience wrapper over `WalkForward`.

    Only the *test* segments are returned. Reporting in-sample results alongside
    them is how a fitted curve gets presented as a track record, so this
    deliberately gives the caller no way to do it.
    """
    if train_bars < 1 or test_bars < 1:
        raise ConfigurationError("train_bars and test_bars must be >= 1")
    step = step or test_bars
    if step < 1:
        raise ConfigurationError("step must be >= 1")
    key = next(iter(data))
    series = list(data[key])
    results = []
    start = 0
    while start + train_bars + test_bars <= len(series):
        test_slice = series[start + train_bars: start + train_bars + test_bars]
        engine = make_engine({key: test_slice})
        results.append(engine.run())
        start += step
    return results


__all__ = ["BacktestConfig", "BacktestEngine", "BacktestResult",
           "INTRABAR_CAVEAT", "StrategyContext", "WalkForward",
           "WalkForwardResult", "WalkForwardWindow", "anchored_windows",
           "rolling_windows", "walk_forward"]
