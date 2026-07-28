"""Exhaustive tests for the deterministic risk engine.

The engine's dangerous failure is not refusing a good trade — it is approving a
bad one. These tests are written from that asymmetry:

* **Boundaries, both sides.** For each limit there is a case just *inside* that
  must pass and a case just *outside* that must be refused. A test that only
  checks the outside case cannot tell a working limit from one that rejects
  everything.
* **The exact reason code.** Asserting `not approved` would not notice the
  engine rejecting for the wrong reason, and the codes are what an operator
  greps for a year later.
* **Fail closed on a missing measurement.** Every limit that depends on a number
  gets a test in which that number is absent, because "we could not check" and
  "it was fine" produce identical-looking decisions if you let them.
* **Projected, not current.** Exposure is asserted against the book *after* the
  fill, including the case the naive delta model gets wrong: a trade big enough
  to flip the sign of a position both closes an exposure and opens a new one.
* **Reduce, never increase.** Asserted as a property over a grid of sizes, not
  just on the one example that motivated the code.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (AccountSnapshot, Candle, DataQuality,
                                       DataQualityReport, Instrument, Mode,
                                       OrderType, PortfolioSnapshot, Position,
                                       Quote, Side, TradeIntent, Verdict)
from olympus.trading.errors import ConfigurationError, LimitsImmutableError
from olympus.trading.killswitch import KillSwitchRegistry, KillSwitchScope
from olympus.trading import risk as R

T0 = datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc)

INST = Instrument(symbol="AAPL", exchange="NASDAQ", tick_size=Decimal("0.01"),
                  lot_size=Decimal("1"), min_quantity=Decimal("1"))
#: Fractional-lot venue, for the "reduced to a legal but sub-minimum size" path.
FRAC = Instrument(symbol="BTCUSDT", exchange="BINANCE", asset_class="crypto",
                  tick_size=Decimal("0.01"), lot_size=Decimal("0.1"),
                  min_quantity=Decimal("1"), allow_fractional=True)
#: Venue with a minimum ticket size.
MINNOT = Instrument(symbol="TSLA", exchange="NASDAQ", lot_size=Decimal("1"),
                    min_quantity=Decimal("1"), min_notional=Decimal("1000"))


class _Forecast:
    """Duck-typed stand-in: the engine reads only `.confidence`/`.uncertainty`,
    and depending on the full ForecastResult here would couple risk tests to the
    forecasting layer's construction rules."""

    def __init__(self, confidence=0.9, uncertainty=0.2):
        self.confidence = confidence
        self.uncertainty = uncertainty


def _intent(qty="5", instrument=INST, **kw):
    base = dict(
        intent_id="i1", strategy_id="s1", strategy_version="1",
        instrument_key=instrument.key, side=Side.BUY,
        order_type=OrderType.MARKET, quantity=Decimal(qty), created_at=T0,
        intended_entry=Decimal("100"), stop_loss=Decimal("98"), confidence=0.9)
    base.update(kw)
    return TradeIntent(**base)


def _ctx(**kw):
    base = dict(
        as_of=T0, mode=Mode.PAPER, instrument=INST, reference_price=100.0,
        data_age_s=10.0, data_quality=DataQuality.OK, forecast=_Forecast(),
        session_open=True, broker_connected=True, reconciliation_age_s=60.0,
        recent_order_count=0, daily_pnl=Decimal("0"),
        strategy_drawdown=Decimal("0"), portfolio_drawdown=Decimal("0"),
        equity=Decimal("100000"))
    base.update(kw)
    return R.RiskContext(**base)


def _limits(**kw):
    """Permissive base, so each test isolates the ONE limit it is about.

    Every field is spelled out rather than relying on the dataclass defaults:
    a default that later changes would otherwise silently re-point dozens of
    tests at a limit they were never meant to exercise.
    """
    base = dict(
        approved_instruments=frozenset(), approved_exchanges=frozenset(),
        approved_order_types=frozenset(),
        max_order_value=None, max_position_size=None, max_position_notional=None,
        max_gross_exposure=None, max_net_exposure=None, max_leverage=None,
        max_open_positions=None, max_concentration=None,
        max_correlated_exposure=None, correlation_groups={},
        max_daily_loss=None, max_strategy_drawdown=None,
        max_portfolio_drawdown=None,
        min_liquidity=None, max_spread_bps=None, max_price_deviation_pct=None,
        max_data_age_s=None, min_data_quality=DataQuality.DEGRADED,
        require_market_open=False, require_stop_loss=False,
        min_forecast_confidence=None, max_forecast_uncertainty=None,
        max_orders_per_minute=None, require_broker_connected=False,
        require_reconciliation=False, max_reconciliation_age_s=None,
        allowed_modes=frozenset({Mode.BACKTEST, Mode.PAPER, Mode.SHADOW}))
    base.update(kw)
    return R.RiskLimits(**base)


def _engine(limits=None, killswitches=None, clock=None, **kw):
    return R.RiskEngine(limits=limits if limits is not None else _limits(),
                        killswitches=killswitches,
                        clock=clock or FixedClock(T0), **kw)


def _codes(decision):
    return set(decision.reason_codes)


def _portfolio(positions=None, marks=None, cash="100000"):
    positions = positions or {}
    return PortfolioSnapshot(
        ts=T0, cash=Decimal(cash),
        positions={k: Position(instrument_key=k, quantity=Decimal(str(q)),
                               average_price=Decimal("100"))
                   for k, q in positions.items()},
        marks=marks if marks is not None else {k: 100.0 for k in positions})


def _check(decision, name):
    for c in decision.checks:
        if c.name == name:
            return c
    raise AssertionError(f"no check named {name!r} in {[c.name for c in decision.checks]}")


# ===========================================================================
# the clean path
# ===========================================================================

def test_a_fully_clean_intent_is_approved_at_the_requested_size():
    d = _engine().authorise(_intent(), _ctx())
    assert d.verdict is Verdict.APPROVED
    assert d.approved_quantity == Decimal("5")
    assert d.approved is True
    assert d.reason_codes == ()          # nothing to explain when nothing tripped


def test_an_approved_decision_carries_the_policy_hash_and_mode():
    limits = _limits(max_order_value=Decimal("10000"))
    d = _engine(limits).authorise(_intent(), _ctx())
    assert d.policy_hash == limits.policy_hash()
    assert d.mode is Mode.PAPER
    assert d.intent_id == "i1"


def test_every_configured_check_is_recorded_on_the_clean_path():
    """The trail must show what was *evaluated*, not merely what tripped —
    otherwise a later reader cannot tell 'we checked and it was fine' from
    'we never checked'."""
    limits = _limits(
        approved_instruments=frozenset({MINNOT.key}),
        approved_exchanges=frozenset({"NASDAQ"}),
        approved_order_types=frozenset({OrderType.MARKET}),
        max_order_value=Decimal("10000"), max_position_size=Decimal("1000"),
        max_position_notional=Decimal("100000"),
        max_gross_exposure=Decimal("100000"), max_net_exposure=Decimal("100000"),
        max_leverage=Decimal("10"), max_open_positions=10,
        max_concentration=0.9, max_correlated_exposure=0.9,
        min_liquidity=Decimal("1"), max_spread_bps=500.0,
        max_price_deviation_pct=50.0, max_data_age_s=600.0,
        require_market_open=True, require_stop_loss=True,
        min_forecast_confidence=0.1, max_forecast_uncertainty=0.9,
        max_orders_per_minute=10, require_broker_connected=True,
        require_reconciliation=True, max_reconciliation_age_s=3600.0,
        max_daily_loss=Decimal("1000"), max_strategy_drawdown=Decimal("1000"),
        max_portfolio_drawdown=Decimal("1000"))
    ks = KillSwitchRegistry(clock=FixedClock(T0))
    d = _engine(limits, killswitches=ks).authorise(
        _intent(qty="20", instrument=MINNOT),
        _ctx(instrument=MINNOT, average_volume=Decimal("10000"),
             quote=Quote(instrument_key=MINNOT.key, ts=T0, bid=99.99, ask=100.01)))
    assert d.verdict is Verdict.APPROVED, d.reason_codes
    assert {c.name for c in d.checks} == {
        "kill_switch", "mode_allowed", "strategy_active", "instrument_approved",
        "exchange_approved", "order_type_approved", "data_freshness",
        "data_quality", "spread", "reference_price", "price_deviation",
        "liquidity", "forecast_confidence", "forecast_uncertainty",
        "market_open", "stop_loss", "broker_connected", "reconciliation",
        "order_rate", "daily_loss", "strategy_drawdown", "portfolio_drawdown",
        "max_order_value", "max_position_size", "max_position_notional",
        "max_gross_exposure", "max_net_exposure", "max_leverage",
        "max_open_positions", "max_concentration", "max_correlated_exposure",
        "min_quantity", "min_notional"}
    assert all(c.passed for c in d.checks)


def test_a_rejection_records_the_checks_that_ran_before_it():
    d = _engine(_limits(require_stop_loss=True)).authorise(_intent(stop_loss=None),
                                                           _ctx())
    assert d.verdict is Verdict.REJECTED
    assert _check(d, "mode_allowed").passed is True      # ran, and passed
    assert _check(d, "stop_loss").passed is False        # ran, and tripped


def test_the_engine_needs_either_a_store_or_explicit_limits():
    with pytest.raises(ConfigurationError):
        R.RiskEngine(clock=FixedClock(T0))


# ===========================================================================
# kill switches — checked first, short-circuiting
# ===========================================================================

@pytest.mark.parametrize("scope,target", [
    (KillSwitchScope.GLOBAL, ""),
    (KillSwitchScope.STRATEGY, "s1"),
    (KillSwitchScope.INSTRUMENT, INST.key),
])
def test_a_kill_switch_at_any_scope_rejects(scope, target):
    ks = KillSwitchRegistry(clock=FixedClock(T0))
    ks.engage(scope, target, reason="halt", by="op")
    d = _engine(killswitches=ks).authorise(_intent(), _ctx())
    assert d.verdict is Verdict.REJECTED
    assert R.R_KILL_SWITCH in _codes(d)
    assert d.approved_quantity == Decimal("0")


def test_the_kill_switch_is_checked_before_anything_else():
    """Short-circuiting matters: a halted system must not spend time (or emit
    audit noise) evaluating limits it is not going to act on."""
    ks = KillSwitchRegistry(clock=FixedClock(T0))
    ks.engage(KillSwitchScope.GLOBAL, reason="halt", by="op")
    d = _engine(_limits(require_stop_loss=True),
                killswitches=ks).authorise(_intent(stop_loss=None), _ctx())
    assert [c.name for c in d.checks] == ["kill_switch"]


def test_a_switch_engaged_by_another_process_blocks_this_engine():
    """The registry the engine holds is not the one that engaged the switch."""
    engine = _engine(killswitches=KillSwitchRegistry(clock=FixedClock(T0)))
    assert engine.authorise(_intent(), _ctx()).approved is True
    KillSwitchRegistry(clock=FixedClock(T0)).engage(
        KillSwitchScope.GLOBAL, reason="cli halt", by="operator")
    assert R.R_KILL_SWITCH in _codes(engine.authorise(_intent(), _ctx()))


def test_an_unreadable_kill_switch_registry_fails_closed():
    class Broken(KillSwitchRegistry):
        def check(self, *a, **k):
            from olympus.trading.errors import TradingError
            raise TradingError("store unreachable")

    d = _engine(killswitches=Broken(clock=FixedClock(T0))).authorise(_intent(), _ctx())
    assert d.verdict is Verdict.REJECTED
    assert R.R_KILL_SWITCH_UNREADABLE in _codes(d)


# ===========================================================================
# permissions
# ===========================================================================

def test_mode_outside_the_allowed_set_is_rejected():
    d = _engine(_limits(allowed_modes=frozenset({Mode.BACKTEST}))).authorise(
        _intent(), _ctx(mode=Mode.PAPER))
    assert R.R_MODE_NOT_ALLOWED in _codes(d)


def test_mode_inside_the_allowed_set_is_approved():
    assert _engine(_limits(allowed_modes=frozenset({Mode.PAPER}))).authorise(
        _intent(), _ctx(mode=Mode.PAPER)).approved is True


def test_an_unapproved_instrument_is_rejected():
    d = _engine(_limits(approved_instruments=frozenset({"NASDAQ:MSFT"}))).authorise(
        _intent(), _ctx())
    assert R.R_INSTRUMENT_NOT_APPROVED in _codes(d)
    assert _engine(_limits(approved_instruments=frozenset({INST.key}))).authorise(
        _intent(), _ctx()).approved is True


def test_an_unapproved_exchange_is_rejected():
    d = _engine(_limits(approved_exchanges=frozenset({"NYSE"}))).authorise(
        _intent(), _ctx())
    assert R.R_EXCHANGE_NOT_APPROVED in _codes(d)
    assert _engine(_limits(approved_exchanges=frozenset({"NASDAQ"}))).authorise(
        _intent(), _ctx()).approved is True


def test_an_unapproved_order_type_is_rejected():
    d = _engine(_limits(approved_order_types=frozenset({OrderType.LIMIT}))).authorise(
        _intent(), _ctx())
    assert R.R_ORDER_TYPE_NOT_APPROVED in _codes(d)


def test_an_inactive_strategy_is_rejected():
    d = _engine().authorise(_intent(), _ctx(strategy_active=False))
    assert R.R_STRATEGY_NOT_ACTIVE in _codes(d)


# ===========================================================================
# market data
# ===========================================================================

@pytest.mark.parametrize("age,ok", [(299.0, True), (300.0, True), (300.1, False)])
def test_data_age_boundary(age, ok):
    d = _engine(_limits(max_data_age_s=300.0)).authorise(_intent(), _ctx(data_age_s=age))
    assert d.approved is ok
    if not ok:
        assert R.R_DATA_STALE in _codes(d)


def test_unknown_data_age_fails_closed():
    d = _engine(_limits(max_data_age_s=300.0)).authorise(_intent(), _ctx(data_age_s=None))
    assert d.verdict is Verdict.REJECTED and R.R_DATA_MISSING in _codes(d)


def test_unusable_data_is_rejected_under_any_configuration():
    d = _engine(_limits(min_data_quality=DataQuality.DEGRADED)).authorise(
        _intent(), _ctx(data_quality=DataQuality.UNUSABLE))
    assert R.R_DATA_QUALITY in _codes(d)


@pytest.mark.parametrize("required,observed,ok", [
    (DataQuality.OK, DataQuality.OK, True),
    (DataQuality.OK, DataQuality.DEGRADED, False),
    (DataQuality.DEGRADED, DataQuality.DEGRADED, True),
])
def test_degraded_data_is_rejected_only_when_ok_is_required(required, observed, ok):
    d = _engine(_limits(min_data_quality=required)).authorise(
        _intent(), _ctx(data_quality=observed))
    assert d.approved is ok
    if not ok:
        assert R.R_DATA_QUALITY in _codes(d)


@pytest.mark.parametrize("bid,ask,ok", [
    (99.99, 100.01, True),        # 2 bps
    (99.75, 100.25, True),        # exactly 50 bps
    (99.70, 100.30, False),       # 60 bps
])
def test_spread_boundary(bid, ask, ok):
    q = Quote(instrument_key=INST.key, ts=T0, bid=bid, ask=ask)
    d = _engine(_limits(max_spread_bps=50.0)).authorise(_intent(), _ctx(quote=q))
    assert d.approved is ok
    if not ok:
        assert R.R_SPREAD_TOO_WIDE in _codes(d)


@pytest.mark.parametrize("ref,ok", [(101.0, True), (105.0, True), (105.1, False)])
def test_price_deviation_boundary(ref, ok):
    """Entry is 100; the collar is 5%."""
    d = _engine(_limits(max_price_deviation_pct=5.0)).authorise(
        _intent(), _ctx(reference_price=ref))
    assert d.approved is ok
    if not ok:
        assert R.R_PRICE_DEVIATION in _codes(d)


@pytest.mark.parametrize("volume,ok", [
    (Decimal("1001"), True), (Decimal("1000"), True), (Decimal("999"), False)])
def test_liquidity_floor_boundary(volume, ok):
    d = _engine(_limits(min_liquidity=Decimal("1000"))).authorise(
        _intent(), _ctx(average_volume=volume))
    assert d.approved is ok
    if not ok:
        assert R.R_LIQUIDITY in _codes(d)


def test_unknown_liquidity_fails_closed():
    d = _engine(_limits(min_liquidity=Decimal("1000"))).authorise(
        _intent(), _ctx(average_volume=None))
    assert d.verdict is Verdict.REJECTED and R.R_DATA_MISSING in _codes(d)


def test_no_reference_price_at_all_fails_closed():
    d = _engine().authorise(_intent(intended_entry=None, stop_loss=None),
                            _ctx(reference_price=None, quote=None, candle=None))
    assert d.verdict is Verdict.REJECTED and R.R_NO_REFERENCE_PRICE in _codes(d)


def test_reference_price_prefers_independent_marks_over_the_intent():
    """Valuing a trade at the price its proposer supplied is how a stale
    strategy sizes itself past a notional limit."""
    quote = Quote(instrument_key=INST.key, ts=T0, bid=199.0, ask=201.0)
    candle = Candle(instrument_key=INST.key, timeframe="1m", ts_open=T0,
                    ts_close=T0 + timedelta(minutes=1), open=300.0, high=301.0,
                    low=299.0, close=300.0)
    # explicit reference price wins over everything
    d = _engine().authorise(_intent(), _ctx(reference_price=100.0, quote=quote,
                                            candle=candle))
    assert _check(d, "reference_price").observed == 100.0
    # then the quote mid
    d = _engine().authorise(_intent(), _ctx(reference_price=None, quote=quote,
                                            candle=candle))
    assert _check(d, "reference_price").observed == 200.0
    # then the last close
    d = _engine().authorise(_intent(), _ctx(reference_price=None, quote=None,
                                            candle=candle))
    assert _check(d, "reference_price").observed == 300.0
    # and only then the intent's own opinion
    d = _engine().authorise(_intent(), _ctx(reference_price=None, quote=None,
                                            candle=None))
    assert _check(d, "reference_price").observed == 100.0


# ===========================================================================
# RiskContext derivations — evidence in must not be evidence ignored
# ===========================================================================

def test_a_data_quality_report_supplies_quality_and_age():
    report = DataQualityReport(
        instrument_key=INST.key, timeframe="1m", status=DataQuality.DEGRADED,
        bars_expected=100, bars_present=90, age_seconds=900.0)
    ctx = R.RiskContext(as_of=T0, mode=Mode.PAPER, instrument=INST,
                        reference_price=100.0, data_quality_report=report)
    assert ctx.data_quality is DataQuality.DEGRADED
    assert ctx.data_age_s == 900.0
    # and the engine acts on it without the caller restating it
    d = _engine(_limits(max_data_age_s=300.0)).authorise(_intent(), ctx)
    assert R.R_DATA_STALE in _codes(d)


def test_an_explicit_measurement_overrides_the_report():
    report = DataQualityReport(instrument_key=INST.key, timeframe="1m",
                               status=DataQuality.DEGRADED, bars_expected=1,
                               bars_present=1, age_seconds=900.0)
    ctx = R.RiskContext(as_of=T0, mode=Mode.PAPER, instrument=INST,
                        reference_price=100.0, data_quality_report=report,
                        data_quality=DataQuality.OK, data_age_s=1.0)
    assert ctx.data_quality is DataQuality.OK and ctx.data_age_s == 1.0


def test_reconciliation_age_is_derived_from_the_last_success():
    ctx = R.RiskContext(as_of=T0, mode=Mode.PAPER, instrument=INST,
                        reference_price=100.0,
                        last_reconciliation_ts=T0 - timedelta(minutes=30))
    assert ctx.reconciliation_age_s == 1800.0
    d = _engine(_limits(require_reconciliation=True,
                        max_reconciliation_age_s=600.0)).authorise(_intent(), ctx)
    assert R.R_RECONCILIATION_STALE in _codes(d)


def test_a_future_reconciliation_timestamp_clamps_to_zero_age():
    """A negative age would sail through every `age <= max` comparison."""
    ctx = R.RiskContext(as_of=T0, mode=Mode.PAPER, instrument=INST,
                        reference_price=100.0,
                        last_reconciliation_ts=T0 + timedelta(hours=1))
    assert ctx.reconciliation_age_s == 0.0


def test_equity_is_taken_from_the_account_snapshot():
    account = AccountSnapshot(ts=T0, cash=Decimal("1000"), equity=Decimal("2500"))
    ctx = R.RiskContext(as_of=T0, mode=Mode.PAPER, instrument=INST,
                        reference_price=100.0, account=account)
    assert ctx.equity == Decimal("2500")


def test_the_context_rejects_a_naive_timestamp():
    with pytest.raises(Exception):
        R.RiskContext(as_of=datetime(2026, 4, 6, 14, 0), mode=Mode.PAPER,
                      instrument=INST)


# ===========================================================================
# forecast gating
# ===========================================================================

@pytest.mark.parametrize("confidence,ok", [(0.81, True), (0.8, True), (0.79, False)])
def test_forecast_confidence_boundary(confidence, ok):
    d = _engine(_limits(min_forecast_confidence=0.8)).authorise(
        _intent(), _ctx(forecast=_Forecast(confidence=confidence)))
    assert d.approved is ok
    if not ok:
        assert R.R_FORECAST_CONFIDENCE in _codes(d)


@pytest.mark.parametrize("uncertainty,ok", [(0.29, True), (0.3, True), (0.31, False)])
def test_forecast_uncertainty_boundary(uncertainty, ok):
    d = _engine(_limits(max_forecast_uncertainty=0.3)).authorise(
        _intent(), _ctx(forecast=_Forecast(uncertainty=uncertainty)))
    assert d.approved is ok
    if not ok:
        assert R.R_FORECAST_UNCERTAINTY in _codes(d)


@pytest.mark.parametrize("limit_kw", [
    {"min_forecast_confidence": 0.5}, {"max_forecast_uncertainty": 0.5}])
def test_a_missing_forecast_fails_closed_when_gated(limit_kw):
    d = _engine(_limits(**limit_kw)).authorise(_intent(), _ctx(forecast=None))
    assert d.verdict is Verdict.REJECTED and R.R_DATA_MISSING in _codes(d)


def test_no_forecast_is_fine_when_nothing_gates_on_one():
    """A rule-based strategy must not be forced to invent a forecast."""
    assert _engine().authorise(_intent(), _ctx(forecast=None)).approved is True


# ===========================================================================
# session, protection, operations
# ===========================================================================

def test_a_closed_market_is_rejected():
    d = _engine(_limits(require_market_open=True)).authorise(
        _intent(), _ctx(session_open=False))
    assert R.R_MARKET_CLOSED in _codes(d)


def test_an_unknown_session_state_fails_closed():
    d = _engine(_limits(require_market_open=True)).authorise(
        _intent(), _ctx(session_open=None))
    assert d.verdict is Verdict.REJECTED and R.R_DATA_MISSING in _codes(d)


def test_a_missing_stop_loss_is_rejected_when_required():
    d = _engine(_limits(require_stop_loss=True)).authorise(_intent(stop_loss=None),
                                                           _ctx())
    assert R.R_NO_STOP_LOSS in _codes(d)
    assert _engine(_limits(require_stop_loss=True)).authorise(
        _intent(), _ctx()).approved is True


def test_broker_connectivity_only_binds_in_live_modes():
    """Paper and backtest have no broker to be disconnected from; enforcing it
    there would only train operators to disable the check."""
    limits = _limits(require_broker_connected=True,
                     allowed_modes=frozenset({Mode.PAPER, Mode.LIVE_RESTRICTED}),
                     max_daily_loss=Decimal("1000"))
    assert _engine(limits).authorise(
        _intent(), _ctx(broker_connected=False)).approved is True
    d = _engine(limits).authorise(
        _intent(), _ctx(mode=Mode.LIVE_RESTRICTED, broker_connected=False))
    assert R.R_BROKER_DISCONNECTED in _codes(d)


@pytest.mark.parametrize("age,ok", [(599.0, True), (600.0, True), (601.0, False)])
def test_reconciliation_age_boundary(age, ok):
    d = _engine(_limits(require_reconciliation=True,
                        max_reconciliation_age_s=600.0)).authorise(
        _intent(), _ctx(reconciliation_age_s=age))
    assert d.approved is ok
    if not ok:
        assert R.R_RECONCILIATION_STALE in _codes(d)


def test_never_reconciled_is_rejected():
    d = _engine(_limits(require_reconciliation=True,
                        max_reconciliation_age_s=600.0)).authorise(
        _intent(), _ctx(reconciliation_age_s=None, last_reconciliation_ts=None))
    assert d.verdict is Verdict.REJECTED and R.R_RECONCILIATION_STALE in _codes(d)


@pytest.mark.parametrize("count,ok", [(1, True), (2, True), (3, False), (9, False)])
def test_order_rate_boundary(count, ok):
    """A ceiling of 3/min means the 4th order in a minute is refused, so the
    check is on the count *before* this one."""
    d = _engine(_limits(max_orders_per_minute=3)).authorise(
        _intent(), _ctx(recent_order_count=count))
    assert d.approved is ok
    if not ok:
        assert R.R_ORDER_RATE in _codes(d)


# ===========================================================================
# loss control — a breach is a stop, not a reason to trade smaller
# ===========================================================================

@pytest.mark.parametrize("pnl,ok", [
    (Decimal("50"), True), (Decimal("-99.99"), True),
    (Decimal("-100"), False), (Decimal("-500"), False)])
def test_daily_loss_boundary(pnl, ok):
    d = _engine(_limits(max_daily_loss=Decimal("100"))).authorise(
        _intent(), _ctx(daily_pnl=pnl))
    assert d.approved is ok
    if not ok:
        assert R.R_DAILY_LOSS in _codes(d)


@pytest.mark.parametrize("dd,ok", [
    (Decimal("49.99"), True), (Decimal("50"), False), (Decimal("60"), False)])
def test_strategy_drawdown_boundary(dd, ok):
    d = _engine(_limits(max_strategy_drawdown=Decimal("50"))).authorise(
        _intent(), _ctx(strategy_drawdown=dd))
    assert d.approved is ok
    if not ok:
        assert R.R_STRATEGY_DRAWDOWN in _codes(d)


@pytest.mark.parametrize("dd,ok", [
    (Decimal("49.99"), True), (Decimal("50"), False), (Decimal("60"), False)])
def test_portfolio_drawdown_boundary(dd, ok):
    d = _engine(_limits(max_portfolio_drawdown=Decimal("50"))).authorise(
        _intent(), _ctx(portfolio_drawdown=dd))
    assert d.approved is ok
    if not ok:
        assert R.R_PORTFOLIO_DRAWDOWN in _codes(d)


@pytest.mark.parametrize("limit_kw,missing", [
    ({"max_daily_loss": Decimal("100")}, "daily_pnl"),
    ({"max_strategy_drawdown": Decimal("100")}, "strategy_drawdown"),
    ({"max_portfolio_drawdown": Decimal("100")}, "portfolio_drawdown"),
])
def test_an_unmeasured_loss_limit_fails_closed(limit_kw, missing):
    """A configured loss ceiling with nothing measuring P&L against it is an
    unbounded system that merely looks protected."""
    d = _engine(_limits(**limit_kw)).authorise(_intent(), _ctx(**{missing: None}))
    assert d.verdict is Verdict.REJECTED and R.R_PNL_MISSING in _codes(d)


# ===========================================================================
# sizing: reduce, never increase
# ===========================================================================

@pytest.mark.parametrize("qty,limit,expected,verdict", [
    ("10", "1000", "10", Verdict.APPROVED),          # 1000 exactly — inside
    ("11", "1000", "10", Verdict.APPROVED_REDUCED),  # 1100 — trimmed to 10
    ("50", "1000", "10", Verdict.APPROVED_REDUCED),
])
def test_max_order_value_boundary_and_reduction(qty, limit, expected, verdict):
    d = _engine(_limits(max_order_value=Decimal(limit))).authorise(
        _intent(qty=qty), _ctx())
    assert d.verdict is verdict
    assert d.approved_quantity == Decimal(expected)


def test_a_reduction_records_its_reason_code():
    """An operator asking why the fill was smaller than the strategy asked for
    needs the code, not just a smaller number."""
    d = _engine(_limits(max_order_value=Decimal("1000"))).authorise(
        _intent(qty="50"), _ctx())
    assert d.verdict is Verdict.APPROVED_REDUCED
    assert R.R_MAX_ORDER_VALUE in _codes(d)
    assert _check(d, "max_order_value").passed is True    # it was handled, not failed


@pytest.mark.parametrize("qty", ["1", "2", "5", "17", "50", "500", "10000"])
def test_the_engine_never_approves_more_than_was_requested(qty):
    """The engine's entire authority is to shrink a proposal or refuse it."""
    limits = _limits(max_order_value=Decimal("10000000"),
                     max_position_size=Decimal("10000000"),
                     max_gross_exposure=Decimal("100000000"),
                     max_net_exposure=Decimal("100000000"),
                     max_leverage=Decimal("1000"), max_concentration=1.0)
    d = _engine(limits).authorise(_intent(qty=qty), _ctx())
    assert d.approved_quantity <= Decimal(qty)


@pytest.mark.parametrize("held,requested,expected", [
    ("0", "5", "5"),        # nothing held, fits
    ("5", "5", "5"),        # exactly reaches the cap
    ("8", "5", "2"),        # trimmed to the headroom
    ("10", "5", None),      # no headroom at all -> rejected, never a zero fill
])
def test_max_position_size_uses_the_projected_position(held, requested, expected):
    pf = _portfolio({INST.key: held}) if held != "0" else _portfolio()
    d = _engine(_limits(max_position_size=Decimal("10"))).authorise(
        _intent(qty=requested), _ctx(portfolio=pf))
    if expected is None:
        assert d.verdict is Verdict.REJECTED
        assert R.R_BELOW_MIN_QUANTITY in _codes(d)
    else:
        assert d.approved_quantity == Decimal(expected)


def test_a_position_size_cap_allows_closing_and_reversing_up_to_the_limit():
    """Buying against a short first closes it and only then opens a long, so
    the headroom is limit + |position|, not limit - |position|. Subtracting
    would block the trades that de-risk the book."""
    pf = _portfolio({INST.key: "-10"})
    d = _engine(_limits(max_position_size=Decimal("20"))).authorise(
        _intent(qty="100"), _ctx(portfolio=pf))
    assert d.verdict is Verdict.APPROVED_REDUCED
    assert d.approved_quantity == Decimal("30")          # -10 + 30 = +20


@pytest.mark.parametrize("held,requested,expected", [
    ("0", "20", "20"),      # 2000 notional, exactly at the cap
    ("0", "50", "20"),
    ("15", "20", "5"),
    ("-10", "100", "30"),   # closes the short, opens 20 long
])
def test_max_position_notional_uses_the_projected_position(held, requested, expected):
    pf = _portfolio({INST.key: held}) if held != "0" else _portfolio()
    d = _engine(_limits(max_position_notional=Decimal("2000"))).authorise(
        _intent(qty=requested), _ctx(portfolio=pf))
    assert d.approved_quantity == Decimal(expected)


# ===========================================================================
# projected portfolio exposure
# ===========================================================================

def test_gross_exposure_is_evaluated_after_the_fill_not_before():
    """The classic bug: every trade looks fine against *current* exposure, and
    the book breaches in aggregate."""
    pf = _portfolio({INST.key: "90"})                     # gross 9000
    d = _engine(_limits(max_gross_exposure=Decimal("10000"))).authorise(
        _intent(qty="50"), _ctx(portfolio=pf))            # would be 14000
    assert d.verdict is Verdict.APPROVED_REDUCED
    assert d.approved_quantity == Decimal("10")           # 1000 of headroom


def test_a_trade_that_flips_the_position_cannot_escape_the_gross_limit():
    """Modelling the trade as a delta subtracted from gross is wrong when the
    fill is big enough to flip the sign: it both closes the old exposure and
    opens a new, larger one. A short of 10 must not become a long of 190 just
    because the trade 'reduces' an existing position."""
    pf = _portfolio({INST.key: "-10"})                    # gross 1000
    d = _engine(_limits(max_gross_exposure=Decimal("2000"))).authorise(
        _intent(qty="200"), _ctx(portfolio=pf))
    assert d.verdict is Verdict.APPROVED_REDUCED
    assert d.approved_quantity == Decimal("30")           # -10 + 30 = +20 => 2000
    assert R.R_MAX_GROSS_EXPOSURE in _codes(d)


def test_gross_exposure_ignores_other_instruments_headroom_correctly():
    pf = _portfolio({"NASDAQ:MSFT": "50", INST.key: "10"})   # 5000 + 1000
    d = _engine(_limits(max_gross_exposure=Decimal("8000"))).authorise(
        _intent(qty="100"), _ctx(portfolio=pf))
    # 8000 - 5000 (MSFT) = 3000 for AAPL => 30 lots total, 20 more than held
    assert d.approved_quantity == Decimal("20")


def test_a_reducing_trade_is_not_treated_as_exposure_adding():
    """Blocking the trades that make the book safer is its own failure mode."""
    pf = _portfolio({INST.key: "-50"})                    # gross 5000
    d = _engine(_limits(max_gross_exposure=Decimal("5000"))).authorise(
        _intent(qty="20"), _ctx(portfolio=pf))            # buy back 20 of the short
    assert d.verdict is Verdict.APPROVED
    assert d.approved_quantity == Decimal("20")


def test_net_exposure_is_evaluated_after_the_fill():
    pf = _portfolio({INST.key: "40"})                     # net +4000
    d = _engine(_limits(max_net_exposure=Decimal("5000"))).authorise(
        _intent(qty="50"), _ctx(portfolio=pf))
    assert d.approved_quantity == Decimal("10")


def test_net_exposure_headroom_spans_the_whole_band_for_a_short_book():
    """Net exposure is signed: a long trade against a short book has room to
    travel the full width of the band, not just from the current magnitude."""
    pf = _portfolio({INST.key: "-10"})                    # net -1000
    d = _engine(_limits(max_net_exposure=Decimal("2000"))).authorise(
        _intent(qty="200"), _ctx(portfolio=pf))
    assert d.approved_quantity == Decimal("30")           # -1000 -> +2000


def test_a_sell_is_bounded_by_the_negative_net_limit():
    d = _engine(_limits(max_net_exposure=Decimal("2000"))).authorise(
        _intent(qty="100", side=Side.SELL, stop_loss=Decimal("102"),
                take_profit=None), _ctx(portfolio=_portfolio()))
    assert d.approved_quantity == Decimal("20")           # -2000 net


@pytest.mark.parametrize("qty,expected,verdict", [
    ("100", "100", Verdict.APPROVED),                  # exactly 1x of 10k
    ("200", "100", Verdict.APPROVED_REDUCED),
])
def test_leverage_boundary_and_reduction(qty, expected, verdict):
    d = _engine(_limits(max_leverage=Decimal("1"))).authorise(
        _intent(qty=qty), _ctx(equity=Decimal("10000"), portfolio=_portfolio()))
    assert d.verdict is verdict
    assert d.approved_quantity == Decimal(expected)


def test_unknown_equity_fails_closed_when_leverage_is_limited():
    d = _engine(_limits(max_leverage=Decimal("1"))).authorise(
        _intent(), _ctx(equity=None, portfolio=None))
    assert d.verdict is Verdict.REJECTED and R.R_EQUITY_MISSING in _codes(d)


def test_zero_equity_cannot_be_levered():
    d = _engine(_limits(max_leverage=Decimal("1"))).authorise(
        _intent(), _ctx(equity=Decimal("0")))
    assert d.verdict is Verdict.REJECTED and R.R_MAX_LEVERAGE in _codes(d)


@pytest.mark.parametrize("existing,ok", [(2, True), (3, False)])
def test_max_open_positions_binds_only_when_opening_a_new_one(existing, ok):
    pf = _portfolio({f"NASDAQ:S{i}": "1" for i in range(existing)})
    d = _engine(_limits(max_open_positions=3)).authorise(_intent(), _ctx(portfolio=pf))
    assert d.approved is ok
    if not ok:
        assert R.R_MAX_POSITIONS in _codes(d)


def test_adding_to_an_existing_position_does_not_count_as_a_new_one():
    pf = _portfolio({INST.key: "1", "NASDAQ:S1": "1", "NASDAQ:S2": "1"})
    d = _engine(_limits(max_open_positions=3)).authorise(_intent(), _ctx(portfolio=pf))
    assert d.approved is True


@pytest.mark.parametrize("qty,expected,verdict", [
    ("10", "10", Verdict.APPROVED),                    # 1000 = 1% of 100k
    ("50", "10", Verdict.APPROVED_REDUCED),
])
def test_concentration_boundary_and_reduction(qty, expected, verdict):
    d = _engine(_limits(max_concentration=0.01)).authorise(
        _intent(qty=qty), _ctx(equity=Decimal("100000")))
    assert d.verdict is verdict
    assert d.approved_quantity == Decimal(expected)


def test_unknown_equity_fails_closed_when_concentration_is_limited():
    d = _engine(_limits(max_concentration=0.5)).authorise(
        _intent(), _ctx(equity=None, portfolio=None))
    assert d.verdict is Verdict.REJECTED and R.R_EQUITY_MISSING in _codes(d)


def test_correlated_group_exposure_is_rejected_when_breached():
    pf = _portfolio({"NASDAQ:MSFT": "400"})               # 40k of "tech"
    limits = _limits(max_correlated_exposure=0.4,
                     correlation_groups={INST.key: "tech", "NASDAQ:MSFT": "tech"})
    d = _engine(limits).authorise(_intent(qty="50"),
                                  _ctx(portfolio=pf, equity=Decimal("100000")))
    assert R.R_CORRELATED in _codes(d)


def test_correlated_exposure_does_not_double_count_the_traded_instrument():
    """The group total already contains this instrument's current leg; adding
    the trade's whole notional on top would refuse trades that fit."""
    pf = _portfolio({"NASDAQ:MSFT": "100", INST.key: "100"})   # 10k + 10k
    limits = _limits(max_correlated_exposure=0.25,
                     correlation_groups={INST.key: "tech", "NASDAQ:MSFT": "tech"})
    # Projected group = 10k (MSFT) + 11k (AAPL after +10) = 21k of 100k = 21%.
    d = _engine(limits).authorise(_intent(qty="10"),
                                  _ctx(portfolio=pf, equity=Decimal("100000")))
    assert d.approved is True
    assert _check(d, "max_correlated_exposure").observed == pytest.approx(0.21)


def test_an_instrument_outside_every_group_is_unaffected():
    limits = _limits(max_correlated_exposure=0.01,
                     correlation_groups={"NASDAQ:MSFT": "tech"})
    d = _engine(limits).authorise(_intent(), _ctx(equity=Decimal("100000")))
    assert d.approved is True
    assert _check(d, "max_correlated_exposure").detail == "no correlation group"


# ===========================================================================
# the floor: a zero-size approval is forbidden by the contract
# ===========================================================================

def test_a_reduction_to_zero_is_a_rejection_not_a_zero_fill():
    d = _engine(_limits(max_order_value=Decimal("50"))).authorise(
        _intent(qty="10"), _ctx())                        # allows 0.5 -> floors to 0
    assert d.verdict is Verdict.REJECTED
    assert d.approved_quantity == Decimal("0")
    assert R.R_BELOW_MIN_QUANTITY in _codes(d)


def test_a_reduction_below_the_instrument_minimum_is_a_rejection():
    """0.5 is a legal size on this venue's lot grid but below its minimum
    ticket, so the engine must refuse rather than submit an unfillable order."""
    d = _engine(_limits(max_order_value=Decimal("50"))).authorise(
        _intent(qty="10", instrument=FRAC), _ctx(instrument=FRAC))
    assert d.verdict is Verdict.REJECTED
    assert R.R_BELOW_MIN_QUANTITY in _codes(d)
    assert _check(d, "min_quantity").observed == "0.5"


def test_a_reduction_below_the_minimum_notional_is_a_rejection():
    d = _engine(_limits(max_order_value=Decimal("500"))).authorise(
        _intent(qty="20", instrument=MINNOT), _ctx(instrument=MINNOT))
    assert d.verdict is Verdict.REJECTED
    assert d.approved_quantity == Decimal("0")
    assert R.R_BELOW_MIN_NOTIONAL in _codes(d)


def test_a_size_exactly_at_the_minimum_notional_is_approved():
    d = _engine(_limits(max_order_value=Decimal("1000"))).authorise(
        _intent(qty="20", instrument=MINNOT), _ctx(instrument=MINNOT))
    assert d.approved is True and d.approved_quantity == Decimal("10")


def test_the_approved_quantity_sits_on_the_venue_lot_grid():
    d = _engine(_limits(max_order_value=Decimal("777"))).authorise(
        _intent(qty="50", instrument=FRAC), _ctx(instrument=FRAC))
    assert d.approved_quantity == Decimal("7.7")          # 7.77 floored to 0.1
    assert d.approved_quantity % FRAC.lot_size == 0


# ===========================================================================
# determinism
# ===========================================================================

def test_identical_inputs_give_byte_identical_decisions():
    engine = _engine(_limits(max_order_value=Decimal("1000")))
    intent, ctx = _intent(qty="50"), _ctx()
    a, b = engine.authorise(intent, ctx), engine.authorise(intent, ctx)
    assert a.to_dict() == b.to_dict()


def test_a_separate_engine_instance_reaches_the_same_decision():
    """Determinism across processes, not just across calls."""
    limits = _limits(max_order_value=Decimal("1000"),
                     max_gross_exposure=Decimal("50000"))
    intent, ctx = _intent(qty="50"), _ctx(portfolio=_portfolio({INST.key: "10"}))
    a = _engine(limits, clock=FixedClock(T0)).authorise(intent, ctx)
    b = _engine(limits, clock=FixedClock(T0 + timedelta(days=9))).authorise(intent, ctx)
    assert a.to_dict() == b.to_dict()
    assert a.policy_hash == b.policy_hash


def test_the_decision_time_comes_from_the_context_not_the_wall_clock():
    """A decision stamped with wall time cannot be replayed in a backtest."""
    d = _engine(clock=FixedClock(T0 + timedelta(days=365))).authorise(
        _intent(), _ctx(as_of=T0))
    assert d.decided_at == T0


def test_the_policy_hash_is_stable_across_equal_limit_objects():
    assert _limits(max_order_value=Decimal("100")).policy_hash() == \
        _limits(max_order_value=Decimal("100")).policy_hash()
    assert R.RiskLimits.conservative().policy_hash() == \
        R.RiskLimits.conservative().policy_hash()


@pytest.mark.parametrize("field,value", [
    ("max_order_value", Decimal("1001")),
    ("max_position_size", Decimal("7")),
    ("max_daily_loss", Decimal("3")),
    ("max_concentration", 0.11),
    ("max_orders_per_minute", 4),
    ("require_stop_loss", True),
    ("min_data_quality", DataQuality.OK),
    ("approved_instruments", frozenset({"NASDAQ:AAPL"})),
    ("allowed_modes", frozenset({Mode.PAPER})),
    ("correlation_groups", {"NASDAQ:AAPL": "tech"}),
])
def test_the_policy_hash_moves_when_any_limit_moves(field, value):
    """A hash that misses a field lets a decision be 'replayed' against a
    policy that is not the one that produced it."""
    assert _limits().policy_hash() != _limits(**{field: value}).policy_hash()


def test_the_policy_hash_does_not_depend_on_set_ordering():
    a = _limits(approved_instruments=frozenset({"A:1", "B:2"}))
    b = _limits(approved_instruments=frozenset({"B:2", "A:1"}))
    assert a.policy_hash() == b.policy_hash()


# ===========================================================================
# limits are operator-owned and immutable
# ===========================================================================

def test_risk_limits_cannot_be_mutated_in_place():
    limits = _limits()
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.max_order_value = Decimal("10") ** 9


def test_saving_without_a_configured_operator_token_is_refused():
    """The safe default for an unconfigured system: no one can change limits."""
    store = R.LimitsStore(clock=FixedClock(T0))
    with pytest.raises(LimitsImmutableError):
        store.save(_limits(), operator_token="anything", reason="x")


def test_saving_with_the_wrong_operator_token_is_refused():
    store = R.LimitsStore(clock=FixedClock(T0), operator_token="right")
    with pytest.raises(LimitsImmutableError):
        store.save(_limits(), operator_token="wrong", reason="x")


def test_saving_something_that_is_not_a_limit_set_is_refused():
    store = R.LimitsStore(clock=FixedClock(T0), operator_token="right")
    with pytest.raises(LimitsImmutableError):
        store.save({"max_order_value": 10 ** 9}, operator_token="right", reason="x")


def test_a_limit_change_must_state_a_reason():
    store = R.LimitsStore(clock=FixedClock(T0), operator_token="right")
    with pytest.raises(LimitsImmutableError):
        store.save(_limits(), operator_token="right", reason="")


def test_limits_round_trip_and_persist_across_instances():
    R.LimitsStore(clock=FixedClock(T0), operator_token="tok").save(
        _limits(max_order_value=Decimal("777")), operator_token="tok",
        reason="widen for the pilot", by="alice")
    reloaded = R.LimitsStore(clock=FixedClock(T0)).load()
    assert reloaded.max_order_value == Decimal("777")


def test_a_change_writes_an_audit_record_with_who_why_and_what_moved():
    store = R.LimitsStore(clock=FixedClock(T0), operator_token="tok")
    store.save(_limits(max_order_value=Decimal("100")), operator_token="tok",
               reason="initial", by="alice")
    store.save(_limits(max_order_value=Decimal("500")), operator_token="tok",
               reason="raised after review", by="bob")
    history = store.history()
    assert len(history) == 2
    assert history[-1]["by"] == "bob"
    assert history[-1]["reason"] == "raised after review"
    assert history[-1]["changed"]["max_order_value"] == {"from": "100", "to": "500"}
    assert history[-1]["previous_policy_hash"] == history[0]["policy_hash"]
    assert history[-1]["saved_at"] == T0.isoformat()


def test_an_unconfigured_store_loads_the_conservative_defaults():
    assert R.LimitsStore(clock=FixedClock(T0)).load().policy_hash() == \
        R.RiskLimits.conservative().policy_hash()


def test_the_engine_reads_the_current_limits_from_the_store():
    """A limit change must take effect without restarting the engine."""
    store = R.LimitsStore(clock=FixedClock(T0), operator_token="tok")
    store.save(_limits(max_order_value=Decimal("10000")), operator_token="tok",
               reason="wide")
    engine = R.RiskEngine(limits_store=store, clock=FixedClock(T0))
    assert engine.authorise(_intent(qty="50"), _ctx()).approved_quantity == Decimal("50")
    store.save(_limits(max_order_value=Decimal("1000")), operator_token="tok",
               reason="tightened")
    assert engine.authorise(_intent(qty="50"), _ctx()).approved_quantity == Decimal("10")


# --- the prompt-injection defence, at the limits API -----------------------

def test_a_tainted_value_can_never_reach_risk_configuration():
    """External content — news, web pages, a model's summary of either — must
    never widen a limit. This is that rule as code."""
    with pytest.raises(LimitsImmutableError):
        R.assert_untainted({"max_order_value": R.Tainted(10 ** 9, "news-article")})
    with pytest.raises(LimitsImmutableError):
        R.assert_untainted([R.Tainted("x")])
    with pytest.raises(LimitsImmutableError):
        R.assert_untainted({"nested": {"deep": [R.Tainted(1)]}})


def test_untainted_values_pass_through_unchanged():
    payload = {"max_order_value": 10, "modes": ["paper"]}
    assert R.assert_untainted(payload) == payload


def test_a_tainted_value_is_not_silently_usable_as_a_number():
    """`Tainted` deliberately subclasses nothing useful, so a taint that reaches
    arithmetic fails loudly instead of quietly becoming a limit."""
    assert R.is_tainted(R.Tainted(5)) is True
    with pytest.raises(TypeError):
        R.Tainted(5) + 1


# ===========================================================================
# the conservative defaults
# ===========================================================================

def test_the_conservative_defaults_are_actually_restrictive():
    """An operator who ships the defaults unchanged must get a system that
    trades small and stops early, not one that is wide open until someone
    remembers to tighten it."""
    c = R.RiskLimits.conservative()
    assert not any(m.is_live for m in c.allowed_modes)
    assert c.require_stop_loss is True
    assert c.require_market_open is True
    assert c.require_broker_connected is True
    assert c.require_reconciliation is True
    assert c.min_data_quality is DataQuality.OK
    for name in ("max_order_value", "max_position_size", "max_gross_exposure",
                 "max_daily_loss", "max_portfolio_drawdown", "max_leverage",
                 "max_open_positions", "max_concentration",
                 "max_orders_per_minute", "max_data_age_s"):
        assert getattr(c, name) is not None, name


def test_conservative_accepts_deliberate_overrides():
    c = R.RiskLimits.conservative(max_order_value=Decimal("5"))
    assert c.max_order_value == Decimal("5")
    assert c.require_stop_loss is True                    # the rest is untouched


def test_a_live_permitting_limit_set_must_configure_a_loss_ceiling():
    with pytest.raises(ConfigurationError):
        R.RiskLimits(allowed_modes=frozenset({Mode.LIVE_BOUNDED}),
                     max_daily_loss=None)
    R.RiskLimits(allowed_modes=frozenset({Mode.LIVE_BOUNDED}),
                 max_daily_loss=Decimal("100"))           # fine with one


@pytest.mark.parametrize("kw", [
    {"max_order_value": Decimal("-1")},
    {"max_concentration": 0.0},
    {"max_concentration": 1.5},
    {"max_correlated_exposure": -0.1},
    {"min_forecast_confidence": 1.5},
    {"max_forecast_uncertainty": -0.1},
    {"max_open_positions": -1},
])
def test_nonsensical_limits_are_refused_at_construction(kw):
    with pytest.raises(ConfigurationError):
        _limits(**kw)


def test_limits_serialise_to_a_json_safe_mapping():
    import json
    blob = json.dumps(R.RiskLimits.conservative().to_dict(), sort_keys=True)
    assert "max_order_value" in json.loads(blob)
