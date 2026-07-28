"""Strategies: what they refuse to do, and what they cannot reach.

Organised around the safety claims rather than the API surface. The load-bearing
ones are: an abstained forecast never becomes a position, every intent carries a
stop, sizes are quantised Decimals, and a strategy has no attribute that leads to
a broker, an order store, or a risk limit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (Candle, DataQuality, ForecastPath,
                                       ForecastResult, Instrument, Mode,
                                       OrderType, PortfolioSnapshot, Position,
                                       Side, SignalDirection, TradeIntent)
from olympus.trading.errors import ConfigurationError, RiskError
from olympus.trading.strategy import (BaselineMomentumStrategy, FlatStrategy,
                                      ForecastMomentumStrategy, Strategy,
                                      StrategyContext, StrategyManager,
                                      StrategyRecord, StrategyStatus,
                                      VolatilityTargetedStrategy)

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ")
CRYPTO = Instrument(symbol="BTCUSD", exchange="BINANCE", asset_class="crypto",
                    tick_size=Decimal("0.01"), lot_size=Decimal("0.001"),
                    min_quantity=Decimal("0.001"), allow_fractional=True)


def series(n=60, start=100.0, step=0.5, instrument=INST):
    """A clean trending series. `step` < 0 makes it a downtrend."""
    out = []
    for i in range(n):
        px = start + i * step
        out.append(Candle(
            instrument_key=instrument.key, timeframe="1d",
            ts_open=T0 + timedelta(days=i), ts_close=T0 + timedelta(days=i + 1),
            open=px, high=px + 1.0, low=px - 1.0, close=px + 0.25,
            volume=10_000.0, amount=1_000_000.0))
    return out


def portfolio(cash="100000", positions=None, marks=None, ts=None):
    return PortfolioSnapshot(ts=ts or (T0 + timedelta(days=61)),
                             cash=Decimal(cash), positions=positions or {},
                             marks=marks or {})


def forecast(expected_return=0.03, *, uncertainty=0.2, abstained=False,
             instrument_key=INST.key, quality=DataQuality.OK, horizon=1,
             mean=(135.0,)):
    common = dict(instrument_key=instrument_key, timeframe="1d",
                  input_start=T0, input_end=T0 + timedelta(days=60),
                  created_at=T0 + timedelta(days=60), horizon=horizon)
    if abstained:
        return ForecastResult.abstain(reason="input window too short",
                                      code="DATA_INSUFFICIENT", **common)
    return ForecastResult(mean_close=mean, expected_return=expected_return,
                          uncertainty=uncertainty, data_quality=quality,
                          model_version="kronos-small/abc123", **common)


def context(candles=None, *, fc=None, pf=None, quality=DataQuality.OK,
            instrument=INST):
    candles = candles if candles is not None else series(instrument=instrument)
    return StrategyContext(
        as_of=candles[-1].ts_close, instrument=instrument, timeframe="1d",
        candles=tuple(candles), forecast=fc, portfolio=pf or portfolio(),
        data_quality=quality, clock=FixedClock(candles[-1].ts_close))


# --- the refusals -----------------------------------------------------------

def test_kronos_strategy_refuses_an_abstained_forecast():
    """Abstention is the forecaster telling the truth. Overriding it here would
    convert 'I don't know' into a position."""
    s = ForecastMomentumStrategy()
    assert s.on_bar(context(fc=forecast(abstained=True))) == []
    assert s.last_skip_reason == "FORECAST_ABSTAINED"


def test_kronos_strategy_refuses_an_unusable_forecast():
    fc = ForecastResult(
        instrument_key=INST.key, timeframe="1d", input_start=T0,
        input_end=T0 + timedelta(days=60), created_at=T0 + timedelta(days=60),
        horizon=0, expected_return=0.05, uncertainty=0.1,
        data_quality=DataQuality.UNUSABLE)
    s = ForecastMomentumStrategy()
    assert s.on_bar(context(fc=fc)) == []
    assert s.last_skip_reason == "FORECAST_UNUSABLE"


def test_kronos_strategy_refuses_a_forecast_for_another_instrument():
    """A mis-keyed forecast is a silent way to trade the wrong thing."""
    s = ForecastMomentumStrategy()
    assert s.on_bar(context(fc=forecast(instrument_key="NASDAQ:MSFT"))) == []
    assert s.last_skip_reason == "FORECAST_INSTRUMENT_MISMATCH"


def test_kronos_strategy_refuses_a_forecast_that_is_too_uncertain():
    s = ForecastMomentumStrategy(max_uncertainty=0.5)
    assert s.on_bar(context(fc=forecast(uncertainty=0.9))) == []
    assert s.last_skip_reason == "FORECAST_TOO_UNCERTAIN"


def test_no_strategy_trades_on_unusable_data_quality():
    s = ForecastMomentumStrategy()
    assert s.on_bar(context(fc=forecast(), quality=DataQuality.UNUSABLE)) == []
    assert s.last_skip_reason == "DATA_UNUSABLE"


def test_strategy_stands_aside_during_warmup():
    s = ForecastMomentumStrategy()
    assert s.on_bar(context(series(5), fc=forecast())) == []
    assert s.last_skip_reason == "WARMUP"


def test_strategy_will_not_stack_a_second_entry():
    """One entry per instrument: otherwise exposure is a function of how often
    the loop runs rather than of conviction."""
    pf = portfolio(positions={INST.key: Position(instrument_key=INST.key,
                                                 quantity=Decimal("10"),
                                                 average_price=Decimal("120"))},
                   marks={INST.key: 130.0})
    s = ForecastMomentumStrategy()
    assert s.on_bar(context(fc=forecast(), pf=pf)) == []
    assert s.last_skip_reason == "ALREADY_POSITIONED"


def test_strategy_refuses_to_size_without_equity():
    s = ForecastMomentumStrategy()
    assert s.on_bar(context(fc=forecast(), pf=portfolio(cash="0"))) == []
    assert s.last_skip_reason == "NO_EQUITY"


def test_expected_return_below_threshold_is_not_traded():
    s = ForecastMomentumStrategy(min_expected_return=0.05)
    assert s.on_bar(context(fc=forecast(expected_return=0.001))) == []
    assert s.last_skip_reason == "EXPECTED_RETURN_BELOW_THRESHOLD"


# --- the positive path ------------------------------------------------------

def test_forecast_long_intent_is_complete_and_protected():
    s = ForecastMomentumStrategy()
    intents = s.on_bar(context(fc=forecast(expected_return=0.03)))
    assert len(intents) == 1
    intent = intents[0]
    assert isinstance(intent, TradeIntent)
    assert intent.side is Side.BUY
    assert intent.order_type is OrderType.MARKET
    assert intent.stop_loss is not None and intent.stop_loss < intent.intended_entry
    assert intent.take_profit is not None and intent.take_profit > intent.intended_entry
    assert intent.strategy_id == "forecast-momentum"
    # Provenance must be complete enough to explain the trade later.
    # The *model version* is whatever the forecaster reported — that is
    # provenance and stays verbatim. The *source label* is the strategy's, and
    # must not name a model.
    assert intent.model_versions["forecast"] == "kronos-small/abc123"
    assert intent.data_ts_start and intent.data_ts_end
    assert intent.supporting_signals[0].source == "forecast"
    assert intent.reason_codes == ("FORECAST_DIRECTIONAL",)


def test_short_intent_puts_the_stop_above_the_entry():
    s = ForecastMomentumStrategy()
    ctx = context(series(60, start=140.0, step=-0.5),
                  fc=forecast(expected_return=-0.03))
    intents = s.on_bar(ctx)
    assert len(intents) == 1
    intent = intents[0]
    assert intent.side is Side.SELL
    assert intent.stop_loss > intent.intended_entry
    assert intent.take_profit < intent.intended_entry


def test_baseline_trades_momentum_with_no_forecast_at_all():
    """The control arm must work without any model in the picture."""
    s = BaselineMomentumStrategy()
    ctx = context()                      # forecast is None
    intents = s.on_bar(ctx)
    assert len(intents) == 1
    assert intents[0].side is Side.BUY
    assert intents[0].supporting_signals[0].source == "momentum"
    assert intents[0].model_versions == {"momentum": "1"}


def test_flat_strategy_never_trades():
    s = FlatStrategy()
    assert s.on_bar(context(fc=forecast())) == []
    assert s.calls == 1


# --- sizing -----------------------------------------------------------------

def test_size_is_a_quantised_decimal_on_the_lot_grid():
    s = ForecastMomentumStrategy()
    ctx = context(series(60, start=100.0, step=0.5, instrument=CRYPTO),
                  fc=forecast(instrument_key=CRYPTO.key), instrument=CRYPTO)
    intent = s.on_bar(ctx)[0]
    assert isinstance(intent.quantity, Decimal)
    assert intent.quantity > 0
    # On the lot grid, exactly — brokers reject off-grid quantities.
    assert intent.quantity % CRYPTO.lot_size == 0
    assert intent.quantity == CRYPTO.quantize_quantity(intent.quantity)


def test_size_respects_the_max_position_fraction_cap():
    """A low-volatility instrument sizes into an enormous notional unless the
    fraction-of-equity cap bites."""
    s = ForecastMomentumStrategy(max_position_fraction=0.05)
    intent = s.on_bar(context(fc=forecast()))[0]
    entry = intent.intended_entry
    assert INST.notional(intent.quantity, entry) <= Decimal("100000") * Decimal("0.05")


def test_lower_conviction_takes_a_smaller_position():
    """Sizing is scaled by conviction, not switched on and off by it."""
    # A target below the series' realised volatility keeps the vol-target
    # formula binding, rather than the leverage cap, so conviction is visible.
    strong = ForecastMomentumStrategy(target_volatility=0.001,
                                    max_position_fraction=1.0)
    weak = ForecastMomentumStrategy(target_volatility=0.001,
                                  max_position_fraction=1.0)
    big = strong.on_bar(context(fc=forecast(expected_return=0.05)))[0]
    small = weak.on_bar(context(fc=forecast(expected_return=0.005)))[0]
    assert small.quantity < big.quantity


def test_zero_volatility_series_is_never_sized():
    """A zero volatility estimate means a dead market or a broken estimator;
    the formula's answer there is 'infinite size'."""
    flat = [Candle(instrument_key=INST.key, timeframe="1d",
                   ts_open=T0 + timedelta(days=i),
                   ts_close=T0 + timedelta(days=i + 1),
                   open=100.0, high=100.0, low=100.0, close=100.0,
                   volume=1.0) for i in range(60)]
    s = ForecastMomentumStrategy()
    assert s.on_bar(context(flat, fc=forecast())) == []
    assert s.last_skip_reason in ("NO_VOLATILITY", "NO_STOP")


# --- the experimental design ------------------------------------------------

def test_kronos_and_baseline_share_every_line_of_sizing_and_exit_code():
    """The comparison in evaluate.py is only meaningful if the signal source is
    the single free variable, so this pins the sharing structurally."""
    for name in ("_size", "_exits", "_stop_distance", "_conviction",
                 "_volatility", "on_bar"):
        assert (getattr(ForecastMomentumStrategy, name)
                is getattr(BaselineMomentumStrategy, name))
        assert (getattr(ForecastMomentumStrategy, name)
                is getattr(VolatilityTargetedStrategy, name))
    assert getattr(ForecastMomentumStrategy, "_view") is not \
        getattr(BaselineMomentumStrategy, "_view")


def test_the_two_arms_size_identically_for_an_identical_view():
    """Same expected return, same confidence ⟹ same quantity, stop and take."""
    candles = series()
    ctx_k = context(candles, fc=forecast(expected_return=0.0833333333333))
    kron = ForecastMomentumStrategy(min_confidence=0.0, full_conviction_return=0.02)
    base = BaselineMomentumStrategy(min_confidence=0.0, full_conviction_return=0.02)
    # The rising series gives the baseline a +8.333% momentum read with full
    # agreement (confidence 1.0); the forecast is set to match, and the Kronos
    # arm's confidence is 1 - uncertainty.
    ctx_k = context(candles, fc=forecast(expected_return=0.0833333333333,
                                         uncertainty=0.0))
    a = kron.on_bar(ctx_k)[0]
    b = base.on_bar(context(candles))[0]
    assert a.quantity == b.quantity
    assert (a.stop_loss, a.take_profit) == (b.stop_loss, b.take_profit)


# --- the structural guarantee ----------------------------------------------

def test_strategy_context_exposes_no_route_to_a_venue_or_to_limits():
    ctx = context(fc=forecast())
    banned = ("broker", "brokers", "order_store", "orders", "oms", "execution",
              "risk_engine", "risk", "limits", "limits_store", "killswitch",
              "killswitches", "vault", "credentials")
    for name in banned:
        assert not hasattr(ctx, name), f"StrategyContext exposes {name!r}"


def test_strategy_instances_expose_no_route_to_a_venue_or_to_limits():
    """If a strategy can reach a broker, 'no model can submit an order' is false
    regardless of what any policy document says."""
    banned = ("broker", "order_store", "oms", "execution", "risk_engine",
              "limits", "limits_store", "submit", "place_order", "cancel")
    for strategy in (ForecastMomentumStrategy(), BaselineMomentumStrategy(),
                     FlatStrategy()):
        for name in banned:
            assert not hasattr(strategy, name), \
                f"{type(strategy).__name__} exposes {name!r}"


def test_coerce_drops_anything_outside_the_permitted_surface():
    """A caller cannot widen a strategy's view by handing it a richer object."""
    class Rich:
        as_of = T0 + timedelta(days=61)
        instrument = INST
        timeframe = "1d"
        candles = tuple(series())
        portfolio = portfolio()
        broker = object()
        risk_engine = object()
        limits = {"max_order_value": 10}

    ctx = StrategyContext.coerce(Rich())
    assert not hasattr(ctx, "broker")
    assert not hasattr(ctx, "risk_engine")
    assert not hasattr(ctx, "limits")
    assert ctx.instrument is INST


def test_coerce_accepts_the_backtester_namespace_shape():
    """`backtest.BacktestEngine` builds its own namespace with `forecasts` and
    `fused`; a strategy must read it without either module importing the other."""
    from types import SimpleNamespace
    candles = series()
    fc = forecast()
    raw = SimpleNamespace(as_of=candles[-1].ts_close, instrument=INST,
                          timeframe="1d", candles=tuple(candles),
                          bar=candles[-1], portfolio=portfolio(),
                          clock=FixedClock(candles[-1].ts_close),
                          forecasts={INST.key: fc}, signals=(), fused=None,
                          quote=None)
    ctx = StrategyContext.coerce(raw)
    assert ctx.forecast is fc
    assert len(ctx.candles) == len(candles)
    assert ForecastMomentumStrategy().on_bar(raw)          # works end to end


def test_intent_ids_are_deterministic_not_random():
    """A replayed bar must produce the same id, or a retry becomes a second
    trade."""
    ctx = context(fc=forecast())
    a = ForecastMomentumStrategy().on_bar(ctx)[0]
    b = ForecastMomentumStrategy().on_bar(ctx)[0]
    assert a.intent_id == b.intent_id
    # ...and a different bar must not collide with it.
    other = context(series(61), fc=forecast())
    assert ForecastMomentumStrategy().on_bar(other)[0].intent_id != a.intent_id


def test_describe_is_json_safe_and_names_the_signal_source():
    import json
    for strategy in (ForecastMomentumStrategy(), BaselineMomentumStrategy()):
        described = strategy.describe()
        json.dumps(described)                            # must not raise
        assert described["id"] == strategy.id
        assert described["parameters"]["signal_source"]


def test_strategy_is_abstract_and_rejects_nonsense_configuration():
    with pytest.raises(TypeError):
        Strategy()                                       # abstract
    with pytest.raises(ConfigurationError):
        ForecastMomentumStrategy(target_volatility=0)
    with pytest.raises(ConfigurationError):
        ForecastMomentumStrategy(max_position_fraction=2.0)
    with pytest.raises(ConfigurationError):
        ForecastMomentumStrategy(max_uncertainty=1.5)
    with pytest.raises(ConfigurationError):
        BaselineMomentumStrategy(lookback=0)


def test_context_requires_a_real_instrument():
    """Without tick and lot sizes there is no tradable quantity to compute."""
    with pytest.raises(ConfigurationError):
        StrategyContext(as_of=T0, instrument="NASDAQ:AAPL")


# --- the manager ------------------------------------------------------------

class FakeSwitches:
    """Duck-typed kill-switch registry. `strategy.py` may not import the real
    one (layer graph), which is exactly why it is injected."""

    def __init__(self, engaged=(), raises=False):
        self.engaged = set(engaged)
        self.raises = raises

    def check(self, instrument_key=None, strategy_id=None):
        if self.raises:
            raise RuntimeError("registry unreadable")
        return "ENGAGED" if strategy_id in self.engaged else None


class FakeAudit:
    def __init__(self):
        self.events = []

    def record(self, event_type, payload=None, *, actor="system", subject="",
               correlation_id=""):
        self.events.append((str(event_type), dict(payload or {}), actor, subject))


def manager(**kw):
    return StrategyManager(clock=FixedClock(T0), **kw)


def test_manager_registers_and_activates():
    audit = FakeAudit()
    m = manager(audit=audit)
    s = FlatStrategy()
    record = m.register(s)
    assert record.status is StrategyStatus.ENABLED
    assert m.active() == [s]
    assert audit.events and audit.events[0][0] == "CONFIG_CHANGED"


def test_pause_and_disable_remove_a_strategy_from_the_active_set():
    m = manager()
    m.register(FlatStrategy())
    m.pause("flat", by="alice", reason="maintenance")
    assert m.active() == []
    assert m.status("flat") is StrategyStatus.PAUSED
    m.enable("flat", by="alice")
    assert [s.id for s in m.active()] == ["flat"]
    m.disable("flat", by="alice")
    assert m.status("flat") is StrategyStatus.DISABLED
    assert m.active() == []


def test_an_unknown_strategy_is_not_enabled_by_default():
    """Unknown means 'not authorised', never 'enabled'."""
    m = manager()
    assert m.status("never-registered") is StrategyStatus.DISABLED
    assert not m.is_active("never-registered")
    with pytest.raises(ConfigurationError):
        m.pause("never-registered")


def test_status_survives_a_restart_and_re_registration():
    """A disabled strategy that comes back enabled because the process bounced
    is the failure this persistence exists to prevent."""
    first = manager()
    first.register(FlatStrategy())
    first.disable("flat", by="alice", reason="under review")

    second = manager()                                   # fresh process
    assert second.status("flat") is StrategyStatus.DISABLED
    second.register(FlatStrategy())                      # re-registration
    assert second.status("flat") is StrategyStatus.DISABLED
    assert second.active() == []


def test_kill_switch_removes_a_strategy_without_changing_its_status():
    m = manager(killswitches=FakeSwitches(engaged={"flat"}))
    m.register(FlatStrategy())
    assert m.active() == []
    assert m.status("flat") is StrategyStatus.ENABLED     # not overwritten
    m.killswitches.engaged.clear()
    assert [s.id for s in m.active()] == ["flat"]


def test_an_unreadable_kill_switch_registry_fails_closed():
    m = manager(killswitches=FakeSwitches(raises=True))
    m.register(FlatStrategy())
    assert m.active() == []


def test_drawdown_tracking_halts_a_strategy_at_its_limit():
    m = manager()
    m.register(FlatStrategy(), max_drawdown=0.2)
    m.record_equity("flat", Decimal("100000"))
    m.record_equity("flat", Decimal("95000"))
    assert m.drawdown("flat") == pytest.approx(0.05)
    assert m.status("flat") is StrategyStatus.ENABLED

    record = m.record_equity("flat", Decimal("80000"))    # exactly 20%
    assert record.halted_by_drawdown
    assert record.status is StrategyStatus.PAUSED
    assert m.active() == []


def test_clearing_a_drawdown_halt_needs_an_operator_override():
    """The loop that lost the money must not be able to authorise itself to
    continue — same rule as an automatic kill switch."""
    m = manager()
    m.register(FlatStrategy(), max_drawdown=0.1)
    m.record_equity("flat", Decimal("100000"))
    m.record_equity("flat", Decimal("80000"))
    with pytest.raises(RiskError):
        m.enable("flat", by="the-loop")
    record = m.enable("flat", by="alice", operator_override=True)
    assert record.status is StrategyStatus.ENABLED
    assert not record.halted_by_drawdown


def test_peak_equity_only_ever_rises():
    m = manager()
    m.register(FlatStrategy())
    m.record_equity("flat", Decimal("100"))
    m.record_equity("flat", Decimal("50"))
    m.record_equity("flat", Decimal("60"))
    assert m.record("flat").peak_equity == Decimal("100")
    assert m.drawdown("flat") == pytest.approx(0.4)


def test_records_round_trip_through_the_store():
    record = StrategyRecord(strategy_id="x", version="2",
                            status=StrategyStatus.PAUSED, updated_at=T0,
                            peak_equity=Decimal("10.5"), equity=Decimal("9.25"),
                            max_drawdown=0.3, drawdown=0.119)
    again = StrategyRecord.from_dict(record.to_dict())
    assert again == record
    assert isinstance(again.peak_equity, Decimal)


def test_manager_rejects_a_strategy_without_an_id_or_on_bar():
    m = manager()

    class Nameless(FlatStrategy):
        id = ""

    with pytest.raises(ConfigurationError):
        m.register(Nameless())

    class NotAStrategy:
        id = "impostor"

    with pytest.raises(ConfigurationError):
        m.register(NotAStrategy())


def test_manager_rejects_an_out_of_range_drawdown_limit():
    m = manager()
    with pytest.raises(ConfigurationError):
        m.register(FlatStrategy(), max_drawdown=1.5)


# --- integration with the real backtester -----------------------------------

def test_the_baseline_strategy_runs_unchanged_in_the_backtester():
    """Same strategy code in backtest as in paper: the property the whole
    'no leakage' claim rests on."""
    from olympus.trading.backtest import BacktestConfig, BacktestEngine
    from olympus.trading.killswitch import KillSwitchRegistry
    from olympus.trading import risk as R

    limits = R.RiskLimits(approved_instruments=frozenset({INST.key}),
                          allowed_modes=frozenset({Mode.BACKTEST}),
                          max_order_value=Decimal("200000"),
                          require_stop_loss=True,
                          require_broker_connected=False,
                          max_daily_loss=None, max_data_age_s=None)
    engine = BacktestEngine(
        config=BacktestConfig(timeframe="1d", warmup_bars=25),
        data={INST.key: series(120)}, instruments={INST.key: INST},
        strategy=BaselineMomentumStrategy(min_confidence=0.0),
        risk_engine=R.RiskEngine(limits=limits,
                                 killswitches=KillSwitchRegistry()))
    result = engine.run()
    assert result.bars_processed > 0
    assert result.intents, "the strategy should have proposed something"
    assert all(i.stop_loss is not None for i in result.intents)
