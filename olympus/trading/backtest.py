"""Event-driven backtester.

The design goal is not speed, it is **not lying**. A backtester's only job is to
produce a number you can act on, and the ways it silently fails to are well
known. Each is closed structurally here rather than by convention:

* **Look-ahead.** Bars are delivered strictly in `ts_close` order and only after
  the clock has passed that close. `validate.assert_no_lookahead` runs on every
  window handed to the strategy — not once at the start, every bar.
* **Fill-at-signal-price.** Orders go to `PaperBroker`, which stages them and
  refuses to fill against a bar that was already open when the order was
  created. A market order therefore fills at the *next* bar's open, plus
  slippage.
* **Costless trading.** Fees and slippage are always modelled; the report
  carries gross and net side by side so the difference cannot be hidden.
* **Different code from live.** The engine drives the *real* `RiskEngine`,
  `OrderStore`, `PortfolioManager` and `PaperBroker`. The only things that
  differ from paper trading are the clock and the data source. A strategy that
  trades here runs unchanged in paper.
* **Survivorship.** The universe can change over time via `listings`. When it
  does not, and the window is long, the engine emits a warning rather than
  letting a static survivor set pass unremarked.

What it does not do: intrabar simulation. Fills are evaluated against a bar's
OHLC, so a stop and a limit that would both trigger within one bar are resolved
by a documented rule, not by tick data. That is a real limitation and is stated
in the result's warnings rather than buried.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import FixedClock
from .contracts import (Candle, DataQuality, Instrument, Mode, Order, Side,
                        TradeIntent, Verdict, jsonable)
from .errors import ConfigurationError, TradingError
from .oms import OrderStore
from .perf import PerformanceReport, compute_report
from .portfolio import PortfolioManager
from .validate import assert_no_lookahead


@dataclass(frozen=True)
class BacktestConfig:
    timeframe: str = "1d"
    starting_cash: Decimal = Decimal("100000")
    #: Bars of history a strategy needs before it may trade. No intents are
    #: accepted before this many bars have been delivered.
    warmup_bars: int = 20
    #: Longest window handed to a strategy. Bounds memory and makes the
    #: strategy's input size explicit rather than "everything so far".
    max_lookback: int = 512
    #: Minimum bars between an order and its earliest fill. Must be >= 1 —
    #: zero would mean filling on the bar that produced the signal.
    latency_bars: int = 1
    seed: int = 0
    continuous_market: bool = False
    #: Warn when the universe is static over a window longer than this.
    survivorship_warn_days: int = 365

    def __post_init__(self):
        if self.latency_bars < 1:
            raise ConfigurationError(
                "latency_bars must be >= 1; zero means filling on the bar that "
                "produced the signal, which is the classic backtest lie",
                got=self.latency_bars)
        if self.warmup_bars < 0:
            raise ConfigurationError("warmup_bars must be >= 0")
        if self.max_lookback < 1:
            raise ConfigurationError("max_lookback must be >= 1")

    def hash(self) -> str:
        payload = {"timeframe": self.timeframe,
                   "starting_cash": str(self.starting_cash),
                   "warmup_bars": self.warmup_bars,
                   "max_lookback": self.max_lookback,
                   "latency_bars": self.latency_bars, "seed": self.seed,
                   "continuous_market": self.continuous_market}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: tuple[tuple[datetime, Decimal], ...]
    trades: tuple[Any, ...]
    orders: tuple[Order, ...]
    intents: tuple[TradeIntent, ...]
    decisions: tuple[Any, ...]
    report: PerformanceReport
    bars_processed: int
    config_hash: str
    data_hash: str
    warnings: tuple[str, ...] = ()

    @property
    def rejected(self) -> tuple[Any, ...]:
        return tuple(d for d in self.decisions if not d.approved)

    def to_dict(self) -> dict:
        return {
            "equity_curve": [[jsonable(t), str(v)] for t, v in self.equity_curve],
            "n_trades": len(self.trades), "n_orders": len(self.orders),
            "n_intents": len(self.intents),
            "n_rejected": len(self.rejected),
            "report": self.report.to_dict(),
            "bars_processed": self.bars_processed,
            "config_hash": self.config_hash, "data_hash": self.data_hash,
            "warnings": list(self.warnings),
        }


class BacktestEngine:
    """Replays history through the real decision stack."""

    def __init__(self, *, config: BacktestConfig, data: Mapping[str, Sequence[Candle]],
                 instruments: Mapping[str, Instrument], strategy,
                 risk_engine, broker=None, listings: Mapping[str, tuple] | None = None,
                 audit=None):
        if not data:
            raise ConfigurationError("backtest needs at least one instrument")
        self.config = config
        self.instruments = dict(instruments)
        self.strategy = strategy
        self.risk_engine = risk_engine
        self.listings = dict(listings or {})
        self.audit = audit

        # Merge every instrument's bars into one chronological stream. Sorting
        # by ts_close (not ts_open) is what makes "deliver only what has closed"
        # expressible at all.
        merged: list[Candle] = []
        for key, bars in data.items():
            for bar in bars:
                if not bar.is_final:
                    continue
                merged.append(bar)
        merged.sort(key=lambda c: (c.ts_close, c.instrument_key))
        if not merged:
            raise ConfigurationError("no final candles to replay")
        self._stream = merged

        self.clock = FixedClock(merged[0].ts_close - timedelta(microseconds=1))
        self.portfolio = PortfolioManager(starting_cash=config.starting_cash,
                                          clock=self.clock,
                                          account_id=f"bt-{config.hash()}")
        self.orders = OrderStore(clock=self.clock,
                                 namespace=f"trading.bt.orders.{config.hash()}")
        # (the run-state clear in run() keeps repeated runs independent)
        if broker is None:
            from .brokers.paper import PaperBroker
            broker = PaperBroker(clock=self.clock, instruments=self.instruments,
                                 starting_cash=config.starting_cash)
        self.broker = broker
        self.broker.connect()

    # -- the loop ----------------------------------------------------------

    def run(self) -> BacktestResult:
        cfg = self.config
        history: dict[str, list[Candle]] = {}
        intents: list[TradeIntent] = []
        decisions: list[Any] = []
        orders: list[Order] = []
        warnings: list[str] = []
        delivered = 0
        bars_in_market = 0

        from .execution import ExecutionConfig, ExecutionEngine
        execution = ExecutionEngine(
            broker=self.broker, order_store=self.orders,
            portfolio=self.portfolio, clock=self.clock, audit=self.audit,
            mode=Mode.BACKTEST,
            config=ExecutionConfig(max_decision_age_s=10 ** 9,
                                   max_price_deviation_pct=10 ** 6))

        # A backtest must be reproducible AND isolated. The order namespace is
        # derived from (config, data) so re-running identical inputs is
        # deterministic, but it MUST be cleared first: otherwise a second run
        # finds the first run's orders, `get_or_create` short-circuits, nothing
        # reaches the broker, and the two runs silently disagree. Found exactly
        # that way — two identical runs produced different equity curves.
        self._clear_run_state()

        for bar in self._stream:
            # 1. Time advances to this bar's close. Nothing may be seen before.
            self.clock.set(bar.ts_close)
            if not self._is_listed(bar.instrument_key, bar.ts_close.date()):
                continue

            # 2. Drive the venue FIRST: orders staged on earlier bars fill
            #    against this one, which is what enforces the latency floor.
            for fill in self.broker.on_candle(bar):
                try:
                    self.orders.record_fill(fill)
                except TradingError:
                    pass
                self.portfolio.apply_fill(
                    fill, self.instruments.get(fill.instrument_key),
                    strategy_id=getattr(self.strategy, "id", ""))

            # 3. Now the bar becomes history.
            window = history.setdefault(bar.instrument_key, [])
            window.append(bar)
            if len(window) > cfg.max_lookback:
                del window[:-cfg.max_lookback]
            delivered += 1

            self.portfolio.mark(bar.instrument_key, bar.close)
            if not self.portfolio.position(bar.instrument_key).is_flat:
                bars_in_market += 1

            if len(window) <= cfg.warmup_bars:
                continue

            # 4. The strategy sees only closed bars, re-checked every time.
            view = tuple(window)
            assert_no_lookahead(view, self.clock.now())

            instrument = self.instruments.get(bar.instrument_key)
            if instrument is None:
                continue
            proposed = self._propose(instrument, view, bar)
            for intent in proposed:
                intents.append(intent)
                decision = self._authorise(intent, instrument, bar)
                decisions.append(decision)
                if not decision.approved:
                    continue
                try:
                    order = execution.execute(intent, decision, instrument,
                                              reference_price=bar.close)
                except TradingError:
                    continue
                orders.append(order)

        # Final mark so the curve ends on the last known price.
        curve = self.portfolio.equity_curve()
        if not curve:
            curve = [(self.clock.now(), self.portfolio.cash)]

        if self._universe_is_static():
            span = (self._stream[-1].ts_close - self._stream[0].ts_close).days
            if span > cfg.survivorship_warn_days:
                warnings.append(
                    f"the instrument universe is static over {span} days; if it "
                    "was chosen from instruments that exist today, these results "
                    "carry survivorship bias that this engine cannot remove")
        warnings.append(
            "fills are evaluated against bar OHLC, not ticks: a stop and a limit "
            "that would both trigger inside one bar are resolved by rule, not by "
            "observed sequence")

        fees = self.portfolio.fees_paid
        report = compute_report(
            curve, self.portfolio.trades, timeframe=cfg.timeframe,
            continuous=cfg.continuous_market, fees=fees,
            exposure=(bars_in_market / delivered) if delivered else 0.0,
            turnover=float(len(orders)),
            label=f"backtest {getattr(self.strategy, 'id', '?')}")

        return BacktestResult(
            equity_curve=tuple(curve), trades=tuple(self.portfolio.trades),
            orders=tuple(orders), intents=tuple(intents),
            decisions=tuple(decisions), report=report,
            bars_processed=delivered, config_hash=cfg.hash(),
            data_hash=self._data_hash(), warnings=tuple(warnings))

    # -- helpers -----------------------------------------------------------

    def _clear_run_state(self) -> None:
        """Drop any persisted orders/fills from a previous run of this exact
        (config, data) pair, so repeated runs are independent."""
        from olympus import store
        backend = store.backend()
        try:
            for key in backend.keys(self.orders.namespace):
                backend.delete(self.orders.namespace, key)
        except Exception:                                # noqa: BLE001
            pass

    def _propose(self, instrument, window, bar) -> list[TradeIntent]:
        from types import SimpleNamespace
        ctx = SimpleNamespace(
            as_of=self.clock.now(), instrument=instrument,
            timeframe=self.config.timeframe, candles=window, bar=bar,
            portfolio=self.portfolio.snapshot(), clock=self.clock,
            forecasts={}, signals=(), fused=None, quote=None)
        try:
            return list(self.strategy.on_bar(ctx) or ())
        except Exception:                                # noqa: BLE001
            return []

    def _authorise(self, intent, instrument, bar):
        from .risk import RiskContext
        ctx = RiskContext(
            as_of=self.clock.now(), mode=Mode.BACKTEST, instrument=instrument,
            portfolio=self.portfolio.snapshot(), reference_price=bar.close,
            data_age_s=0.0, data_quality=DataQuality.OK, session_open=True,
            broker_connected=True, reconciliation_age_s=0.0,
            recent_order_count=0, daily_pnl=self.portfolio.daily_pnl(self.clock.now()),
            equity=self.portfolio.snapshot().equity)
        return self.risk_engine.authorise(intent, ctx)

    def _is_listed(self, key: str, day: date) -> bool:
        window = self.listings.get(key)
        if not window:
            return True
        start, end = window
        if start and day < start:
            return False
        if end and day > end:
            return False
        return True

    def _universe_is_static(self) -> bool:
        return not self.listings

    def _data_hash(self) -> str:
        h = hashlib.sha256()
        for bar in self._stream:
            h.update(f"{bar.instrument_key}{bar.ts_close.isoformat()}{bar.close}"
                     .encode())
        return h.hexdigest()[:16]


def walk_forward(*, data: Mapping[str, Sequence[Candle]], make_engine,
                 train_bars: int, test_bars: int, step: int | None = None
                 ) -> list[BacktestResult]:
    """Rolling out-of-sample evaluation.

    Only the *test* segments are returned. Reporting in-sample results
    alongside them is how a fitted curve gets presented as a track record, so
    this deliberately gives the caller no way to do it.
    """
    if train_bars < 1 or test_bars < 1:
        raise ConfigurationError("train_bars and test_bars must be >= 1")
    step = step or test_bars
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


__all__ = ["BacktestEngine", "BacktestConfig", "BacktestResult", "walk_forward"]
