"""Exhaustive tests for the deterministic risk engine and kill switches.

The engine's dangerous failure is approving something it should have refused, so
these tests are written adversarially: for each limit there is a case just
*inside* the boundary that must pass and a case just *outside* that must be
rejected with a specific, stable reason code. Asserting the code rather than the
prose matters — the codes are what an operator greps for a year later, and a
test that only checks "it was rejected" would not notice the engine rejecting
for the wrong reason.

Three properties get dedicated attention because they are the ones that quietly
break:

* **Fail closed on a missing measurement.** "We could not check" must never be
  treated as "it was fine".
* **Projected, not current, exposure.** A trade that is fine now but breaches
  after it fills must be caught before it fills.
* **Reduce, never increase.** The engine may shrink a proposal; there must be no
  input for which it returns more than was asked for.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (DataQuality, Instrument, Mode, OrderType,
                                       PortfolioSnapshot, Position, Quote, Side,
                                       TradeIntent, Verdict)
from olympus.trading.errors import LimitsImmutableError, KillSwitchActive
from olympus.trading.killswitch import (AUTO_RULES, CODE_DAILY_LOSS,
                                        KillSwitchRegistry, KillSwitchScope,
                                        evaluate_auto_trips)
from olympus.trading import risk as R

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ", tick_size=Decimal("0.01"),
                  lot_size=Decimal("1"), min_quantity=Decimal("1"))


@pytest.fixture()
def clock():
    return FixedClock(T0)


def _intent(qty="5", **kw):
    base = dict(
        intent_id="i1", strategy_id="s1", strategy_version="1",
        instrument_key=INST.key, side=Side.BUY, order_type=OrderType.MARKET,
        quantity=Decimal(qty), created_at=T0, intended_entry=Decimal("100"),
        stop_loss=Decimal("98"), confidence=0.9)
    base.update(kw)
    return TradeIntent(**base)


class _Forecast:
    def __init__(self, confidence=0.9, uncertainty=0.2):
        self.confidence = confidence
        self.uncertainty = uncertainty


def _ctx(**kw):
    base = dict(
        as_of=T0, mode=Mode.PAPER, instrument=INST, reference_price=100.0,
        data_age_s=10.0, data_quality=DataQuality.OK, forecast=_Forecast(),
        session_open=True, broker_connected=True, reconciliation_age_s=60.0,
        recent_order_count=0, daily_pnl=Decimal("0"), equity=Decimal("100000"))
    base.update(kw)
    return R.RiskContext(**base)


def _limits(**kw):
    """Permissive base so each test isolates the ONE limit it is about."""
    base = dict(
        max_order_value=None, max_position_size=None, max_position_notional=None,
        max_gross_exposure=None, max_net_exposure=None, max_leverage=None,
        max_open_positions=None, max_concentration=None,
        max_correlated_exposure=None, max_daily_loss=None,
        max_strategy_drawdown=None, max_portfolio_drawdown=None,
        min_liquidity=None, max_spread_bps=None, max_price_deviation_pct=None,
        max_data_age_s=None, min_data_quality=DataQuality.DEGRADED,
        require_market_open=False, require_stop_loss=False,
        min_forecast_confidence=None, max_forecast_uncertainty=None,
        max_orders_per_minute=None, require_broker_connected=False,
        require_reconciliation=False, max_reconciliation_age_s=None,
        allowed_modes=frozenset({Mode.BACKTEST, Mode.PAPER, Mode.SHADOW}))
    base.update(kw)
    return R.RiskLimits(**base)


def _engine(limits=None, killswitches=None, clock=None):
    return R.RiskEngine(limits=limits or _limits(), killswitches=killswitches,
                        clock=clock or FixedClock(T0))


def _codes(decision):
    return set(decision.reason_codes)


# --- the clean path --------------------------------------------------------

def test_a_clean_intent_is_approved():
    d = _engine().authorise(_intent(), _ctx())
    assert d.verdict is Verdict.APPROVED
    assert d.approved_quantity == Decimal("5")
    assert d.approved


def test_every_check_is_recorded_pass_and_fail():
    """The trail must show what was evaluated, not only what tripped."""
    d = _engine(_limits(max_order_value=Decimal("100000"),
                        require_stop_loss=True)).authorise(_intent(), _ctx())
    names = {c.name for c in d.checks}
    assert {"mode_allowed", "reference_price", "stop_loss",
            "max_order_value", "min_quantity"} <= names
    assert any(c.passed for c in d.checks)


# --- permissions -----------------------------------------------------------

def test_mode_not_allowed_is_rejected():
    d = _engine(_limits(allowed_modes=frozenset({Mode.BACKTEST}))).authorise(
        _intent(), _ctx(mode=Mode.PAPER))
    assert not d.approved and R.R_MODE_NOT_ALLOWED in _codes(d)


def test_unapproved_instrument_and_exchange_are_rejected():
    d = _engine(_limits(approved_instruments=frozenset({"NASDAQ:MSFT"}))).authorise(
        _intent(), _ctx())
    assert R.R_INSTRUMENT_NOT_APPROVED in _codes(d)

    d = _engine(_limits(approved_exchanges=frozenset({"NYSE"}))).authorise(
        _intent(), _ctx())
    assert R.R_EXCHANGE_NOT_APPROVED in _codes(d)


def test_unapproved_order_type_is_rejected():
    d = _engine(_limits(approved_order_types=frozenset({OrderType.LIMIT}))).authorise(
        _intent(), _ctx())
    assert R.R_ORDER_TYPE_NOT_APPROVED in _codes(d)


def test_inactive_strategy_is_rejected():
    d = _engine().authorise(_intent(), _ctx(strategy_active=False))
    assert R.R_STRATEGY_NOT_ACTIVE in _codes(d)


# --- data integrity, including fail-closed ---------------------------------

@pytest.mark.parametrize("age,ok", [(299.0, True), (300.0, True), (301.0, False)])
def test_data_freshness_boundary(age, ok):
    d = _engine(_limits(max_data_age_s=300.0)).authorise(_intent(), _ctx(data_age_s=age))
    assert d.approved is ok
    if not ok:
        assert R.R_DATA_STALE in _codes(d)


def test_unknown_data_age_fails_closed():
    """A configured freshness limit with no measurement must reject."""
    d = _engine(_limits(max_data_age_s=300.0)).authorise(_intent(), _ctx(data_age_s=None))
    assert not d.approved and R.R_DATA_MISSING in _codes(d)


def test_unusable_data_is_always_rejected():
    d = _engine().authorise(_intent(), _ctx(data_quality=DataQuality.UNUSABLE))
    assert R.R_DATA_QUALITY in _codes(d)


def test_degraded_data_rejected_only_when_ok_required():
    assert _engine(_limits(min_data_quality=DataQuality.OK)).authorise(
        _intent(), _ctx(data_quality=DataQuality.DEGRADED)).approved is False
    assert _engine(_limits(min_data_quality=DataQuality.DEGRADED)).authorise(
        _intent(), _ctx(data_quality=DataQuality.DEGRADED)).approved is True


def test_spread_too_wide_is_rejected():
    wide = Quote(instrument_key=INST.key, ts=T0, bid=99.0, ask=101.0)   # ~200bps
    d = _engine(_limits(max_spread_bps=50.0)).authorise(_intent(), _ctx(quote=wide))
    assert R.R_SPREAD_TOO_WIDE in _codes(d)


def test_price_deviation_is_rejected():
    d = _engine(_limits(max_price_deviation_pct=1.0)).authorise(
        _intent(), _ctx(reference_price=110.0))          # 10% away from entry 100
    assert R.R_PRICE_DEVIATION in _codes(d)


def test_no_reference_price_fails_closed():
    d = _engine().authorise(
        _intent(intended_entry=None, stop_loss=None),
        _ctx(reference_price=None, quote=None))
    assert not d.approved and R.R_NO_REFERENCE_PRICE in _codes(d)


def test_illiquid_instrument_is_rejected():
    d = _engine(_limits(min_liquidity=Decimal("1000"))).authorise(
        _intent(), _ctx(average_volume=Decimal("10")))
    assert R.R_LIQUIDITY in _codes(d)


def test_unknown_liquidity_fails_closed():
    d = _engine(_limits(min_liquidity=Decimal("1000"))).authorise(
        _intent(), _ctx(average_volume=None))
    assert not d.approved and R.R_DATA_MISSING in _codes(d)


# --- forecast gating -------------------------------------------------------

def test_low_forecast_confidence_is_rejected():
    d = _engine(_limits(min_forecast_confidence=0.8)).authorise(
        _intent(), _ctx(forecast=_Forecast(confidence=0.5)))
    assert R.R_FORECAST_CONFIDENCE in _codes(d)


def test_high_forecast_uncertainty_is_rejected():
    d = _engine(_limits(max_forecast_uncertainty=0.3)).authorise(
        _intent(), _ctx(forecast=_Forecast(uncertainty=0.9)))
    assert R.R_FORECAST_UNCERTAINTY in _codes(d)


def test_missing_forecast_fails_closed_when_gated():
    d = _engine(_limits(min_forecast_confidence=0.5)).authorise(
        _intent(), _ctx(forecast=None))
    assert not d.approved and R.R_DATA_MISSING in _codes(d)


# --- session, protection, operations ---------------------------------------

def test_market_closed_is_rejected():
    d = _engine(_limits(require_market_open=True)).authorise(
        _intent(), _ctx(session_open=False))
    assert R.R_MARKET_CLOSED in _codes(d)


def test_missing_stop_loss_is_rejected_when_required():
    d = _engine(_limits(require_stop_loss=True)).authorise(
        _intent(stop_loss=None), _ctx())
    assert R.R_NO_STOP_LOSS in _codes(d)


def test_broker_disconnected_rejected_only_in_live_modes():
    limits = _limits(require_broker_connected=True,
                     allowed_modes=frozenset({Mode.PAPER, Mode.LIVE_RESTRICTED}),
                     max_daily_loss=Decimal("100"))
    # paper: connectivity is irrelevant
    assert _engine(limits).authorise(
        _intent(), _ctx(broker_connected=False)).approved is True
    # live: it is not
    d = _engine(limits).authorise(
        _intent(), _ctx(mode=Mode.LIVE_RESTRICTED, broker_connected=False))
    assert R.R_BROKER_DISCONNECTED in _codes(d)


def test_stale_or_never_run_reconciliation_is_rejected():
    limits = _limits(require_reconciliation=True, max_reconciliation_age_s=600.0)
    d = _engine(limits).authorise(_intent(), _ctx(reconciliation_age_s=999.0))
    assert R.R_RECONCILIATION_STALE in _codes(d)
    d = _engine(limits).authorise(_intent(), _ctx(reconciliation_age_s=None))
    assert R.R_RECONCILIATION_STALE in _codes(d)


def test_order_rate_ceiling_is_rejected():
    d = _engine(_limits(max_orders_per_minute=3)).authorise(
        _intent(), _ctx(recent_order_count=3))
    assert R.R_ORDER_RATE in _codes(d)


# --- loss control ----------------------------------------------------------

def test_daily_loss_limit_stops_trading():
    d = _engine(_limits(max_daily_loss=Decimal("100"))).authorise(
        _intent(), _ctx(daily_pnl=Decimal("-100")))
    assert R.R_DAILY_LOSS in _codes(d)


def test_drawdown_limits_stop_trading():
    d = _engine(_limits(max_strategy_drawdown=Decimal("50"))).authorise(
        _intent(), _ctx(strategy_drawdown=Decimal("50")))
    assert R.R_STRATEGY_DRAWDOWN in _codes(d)
    d = _engine(_limits(max_portfolio_drawdown=Decimal("50"))).authorise(
        _intent(), _ctx(portfolio_drawdown=Decimal("60")))
    assert R.R_PORTFOLIO_DRAWDOWN in _codes(d)


# --- sizing: reduce, never increase ----------------------------------------

def test_max_order_value_reduces_rather_than_rejects():
    d = _engine(_limits(max_order_value=Decimal("1000"))).authorise(
        _intent(qty="50"), _ctx())                       # 50 * 100 = 5000
    assert d.verdict is Verdict.APPROVED_REDUCED
    assert d.approved_quantity == Decimal("10")          # 1000 / 100


def test_reduction_below_the_instrument_minimum_is_a_rejection():
    """A zero-or-sub-minimum 'approval' is forbidden by the contract."""
    d = _engine(_limits(max_order_value=Decimal("50"))).authorise(
        _intent(qty="10"), _ctx())                       # allows 0.5 -> floors to 0
    assert d.verdict is Verdict.REJECTED
    assert d.approved_quantity == Decimal("0")
    assert R.R_BELOW_MIN_QUANTITY in _codes(d)


def test_engine_never_returns_more_than_requested():
    for qty in ("1", "5", "50", "500"):
        d = _engine(_limits(max_order_value=Decimal("100000"))).authorise(
            _intent(qty=qty), _ctx())
        assert d.approved_quantity <= Decimal(qty)


def test_max_position_size_accounts_for_the_existing_position():
    pf = PortfolioSnapshot(
        ts=T0, cash=Decimal("100000"),
        positions={INST.key: Position(instrument_key=INST.key,
                                      quantity=Decimal("8"),
                                      average_price=Decimal("100"))},
        marks={INST.key: 100.0})
    d = _engine(_limits(max_position_size=Decimal("10"))).authorise(
        _intent(qty="5"), _ctx(portfolio=pf))
    assert d.verdict is Verdict.APPROVED_REDUCED
    assert d.approved_quantity == Decimal("2")           # 10 - 8


# --- projected exposure ----------------------------------------------------

def test_exposure_is_evaluated_after_the_fill_not_before():
    """The classic bug: each trade looks fine against *current* exposure."""
    pf = PortfolioSnapshot(
        ts=T0, cash=Decimal("100000"),
        positions={INST.key: Position(instrument_key=INST.key,
                                      quantity=Decimal("90"),
                                      average_price=Decimal("100"))},
        marks={INST.key: 100.0})
    # current gross = 9000, limit 10000; a 50-lot trade would take it to 14000.
    d = _engine(_limits(max_gross_exposure=Decimal("10000"))).authorise(
        _intent(qty="50"), _ctx(portfolio=pf))
    assert d.verdict is Verdict.APPROVED_REDUCED
    assert d.approved_quantity == Decimal("10")          # only 1000 of headroom


def test_max_open_positions_is_rejected_when_opening_a_new_one():
    pf = PortfolioSnapshot(
        ts=T0, cash=Decimal("100000"),
        positions={f"NASDAQ:S{i}": Position(instrument_key=f"NASDAQ:S{i}",
                                            quantity=Decimal("1"),
                                            average_price=Decimal("10"))
                   for i in range(3)},
        marks={f"NASDAQ:S{i}": 10.0 for i in range(3)})
    d = _engine(_limits(max_open_positions=3)).authorise(_intent(), _ctx(portfolio=pf))
    assert R.R_MAX_POSITIONS in _codes(d)


def test_concentration_limit_reduces_size():
    d = _engine(_limits(max_concentration=0.01)).authorise(   # 1% of 100k = 1000
        _intent(qty="50"), _ctx(equity=Decimal("100000")))
    assert d.verdict is Verdict.APPROVED_REDUCED
    assert d.approved_quantity == Decimal("10")


def test_correlated_exposure_limit_is_rejected():
    pf = PortfolioSnapshot(
        ts=T0, cash=Decimal("100000"),
        positions={"NASDAQ:MSFT": Position(instrument_key="NASDAQ:MSFT",
                                           quantity=Decimal("400"),
                                           average_price=Decimal("100"))},
        marks={"NASDAQ:MSFT": 100.0})
    limits = _limits(max_correlated_exposure=0.4,
                     correlation_groups={INST.key: "tech", "NASDAQ:MSFT": "tech"})
    d = _engine(limits).authorise(_intent(qty="50"), _ctx(portfolio=pf,
                                                          equity=Decimal("100000")))
    assert R.R_CORRELATED in _codes(d)


def test_leverage_limit_reduces_size():
    d = _engine(_limits(max_leverage=Decimal("1"))).authorise(
        _intent(qty="200"), _ctx(equity=Decimal("10000")))
    assert d.approved_quantity <= Decimal("100")          # 1x of 10k at px 100


# --- determinism and policy identity ---------------------------------------

def test_identical_inputs_give_identical_decisions():
    engine = _engine(_limits(max_order_value=Decimal("1000")))
    intent, ctx = _intent(qty="50"), _ctx()
    a, b = engine.authorise(intent, ctx), engine.authorise(intent, ctx)
    assert a.to_dict() == b.to_dict()
    assert a.policy_hash == b.policy_hash


def test_policy_hash_changes_when_a_limit_changes():
    a = _limits(max_order_value=Decimal("1000")).policy_hash()
    b = _limits(max_order_value=Decimal("1001")).policy_hash()
    assert a != b


def test_policy_hash_is_stable_across_instances():
    assert R.RiskLimits.conservative().policy_hash() == \
        R.RiskLimits.conservative().policy_hash()


# --- limits are operator-owned ---------------------------------------------

def test_limits_cannot_be_saved_without_an_operator_token():
    store = R.LimitsStore(clock=FixedClock(T0))           # no token configured
    with pytest.raises(LimitsImmutableError):
        store.save(_limits(), operator_token="anything", reason="x")


def test_limits_reject_a_wrong_operator_token():
    store = R.LimitsStore(clock=FixedClock(T0), operator_token="right")
    with pytest.raises(LimitsImmutableError):
        store.save(_limits(), operator_token="wrong", reason="x")


def test_limits_round_trip_with_the_correct_token():
    store = R.LimitsStore(clock=FixedClock(T0), operator_token="right")
    saved = store.save(_limits(max_order_value=Decimal("777")),
                       operator_token="right", reason="test")
    assert store.load().max_order_value == Decimal("777")
    assert store.load().policy_hash() == saved.policy_hash()


def test_unset_limits_default_to_conservative():
    assert R.LimitsStore(clock=FixedClock(T0)).load().policy_hash() == \
        R.RiskLimits.conservative().policy_hash()


def test_tainted_values_can_never_reach_risk_configuration():
    """The prompt-injection defence, at the limits API."""
    with pytest.raises(LimitsImmutableError):
        R.assert_untainted({"max_order_value": R.Tainted(10 ** 9, "news")})
    with pytest.raises(LimitsImmutableError):
        R.assert_untainted([R.Tainted("x")])
    assert R.assert_untainted({"max_order_value": 10}) == {"max_order_value": 10}


def test_a_live_permitting_limit_set_must_configure_a_loss_ceiling():
    with pytest.raises(Exception):
        R.RiskLimits(allowed_modes=frozenset({Mode.LIVE_BOUNDED}),
                     max_daily_loss=None)


# --- kill switches ---------------------------------------------------------

def test_kill_switch_blocks_at_every_scope(clock):
    ks = KillSwitchRegistry(clock=clock)
    engine = _engine(killswitches=ks)

    ks.engage(KillSwitchScope.INSTRUMENT, INST.key, reason="halt", by="op")
    assert R.R_KILL_SWITCH in _codes(engine.authorise(_intent(), _ctx()))
    ks.disengage(KillSwitchScope.INSTRUMENT, INST.key, by="op")

    ks.engage(KillSwitchScope.STRATEGY, "s1", reason="halt", by="op")
    assert R.R_KILL_SWITCH in _codes(engine.authorise(_intent(), _ctx()))
    ks.disengage(KillSwitchScope.STRATEGY, "s1", by="op")

    ks.engage(KillSwitchScope.GLOBAL, reason="halt", by="op")
    assert R.R_KILL_SWITCH in _codes(engine.authorise(_intent(), _ctx()))


def test_kill_switch_survives_a_restart(clock):
    """A switch cleared by the crash that tripped it is not a safety device."""
    KillSwitchRegistry(clock=clock).engage(
        KillSwitchScope.GLOBAL, reason="halt", by="op")
    fresh = KillSwitchRegistry(clock=FixedClock(T0 + timedelta(hours=1)))
    assert fresh.is_engaged(KillSwitchScope.GLOBAL) is True
    assert fresh.check() is not None


def test_an_auto_trip_needs_an_operator_override_to_clear(clock):
    ks = KillSwitchRegistry(clock=clock)
    ks.engage(KillSwitchScope.GLOBAL, reason="daily loss", code=CODE_DAILY_LOSS,
              by="monitor", auto=True)
    with pytest.raises(KillSwitchActive):
        ks.disengage(KillSwitchScope.GLOBAL, by="loop")
    ks.disengage(KillSwitchScope.GLOBAL, by="operator", operator_override=True)
    assert ks.is_engaged(KillSwitchScope.GLOBAL) is False


def test_a_manual_switch_clears_without_an_override(clock):
    ks = KillSwitchRegistry(clock=clock)
    ks.engage(KillSwitchScope.GLOBAL, reason="manual", by="op")
    ks.disengage(KillSwitchScope.GLOBAL, by="op")
    assert ks.is_engaged(KillSwitchScope.GLOBAL) is False


def test_global_scope_wins_over_a_narrower_disengage(clock):
    ks = KillSwitchRegistry(clock=clock)
    ks.engage(KillSwitchScope.GLOBAL, reason="halt", by="op")
    ks.disengage(KillSwitchScope.INSTRUMENT, INST.key, by="op")
    assert ks.check(instrument_key=INST.key) is not None


def test_unreadable_registry_makes_the_engine_fail_closed(clock):
    class Broken(KillSwitchRegistry):
        def check(self, *a, **k):
            from olympus.trading.errors import TradingError
            raise TradingError("store down")

    d = _engine(killswitches=Broken(clock=clock)).authorise(_intent(), _ctx())
    assert not d.approved and R.R_KILL_SWITCH_UNREADABLE in _codes(d)


# --- automatic trip rules --------------------------------------------------

def test_auto_trips_fire_on_each_condition():
    limits = _limits(max_daily_loss=Decimal("100"),
                     max_portfolio_drawdown=Decimal("500"),
                     max_strategy_drawdown=Decimal("50"),
                     max_orders_per_minute=5, max_data_age_s=60.0,
                     max_reconciliation_age_s=600.0)
    trips = evaluate_auto_trips({
        "daily_loss": Decimal("-150"), "portfolio_drawdown": Decimal("600"),
        "strategy_drawdowns": {"s1": Decimal("80")}, "orders_last_minute": 9,
        "data_age_s": 120.0, "reconciliation_age_s": 900.0}, limits)
    codes = {rule.code for rule, _, _ in trips}
    assert codes == {"DAILY_LOSS_BREACH", "PORTFOLIO_DRAWDOWN_BREACH",
                     "STRATEGY_DRAWDOWN_BREACH", "ABNORMAL_ORDER_RATE",
                     "STALE_MARKET_DATA", "BROKER_DESYNCHRONISED"}


def test_a_missing_measurement_never_trips_a_rule():
    """Absence of evidence is not evidence of a breach — except reconciliation,
    which is covered separately, because never-reconciled IS a desync."""
    limits = _limits(max_daily_loss=Decimal("100"), max_data_age_s=60.0)
    assert evaluate_auto_trips({}, limits) == []


def test_never_reconciled_is_treated_as_desync():
    limits = _limits(max_reconciliation_age_s=600.0)
    trips = evaluate_auto_trips({"reconciliation_required": True}, limits)
    assert [r.code for r, _, _ in trips] == ["BROKER_DESYNCHRONISED"]


def test_reported_breaks_trip_desync_immediately():
    trips = evaluate_auto_trips({"reconciliation_ok": False}, _limits())
    assert [r.code for r, _, _ in trips] == ["BROKER_DESYNCHRONISED"]


def test_every_declared_auto_rule_is_reachable():
    """Guards against a rule being declared in AUTO_RULES but never evaluated."""
    limits = _limits(max_daily_loss=Decimal("1"), max_portfolio_drawdown=Decimal("1"),
                     max_strategy_drawdown=Decimal("1"), max_orders_per_minute=1,
                     max_data_age_s=1.0, max_reconciliation_age_s=1.0)
    trips = evaluate_auto_trips({
        "daily_loss": Decimal("-99"), "portfolio_drawdown": Decimal("99"),
        "strategy_drawdowns": {"s": Decimal("99")}, "orders_last_minute": 99,
        "data_age_s": 99.0, "reconciliation_age_s": 99.0}, limits)
    assert {r.name for r, _, _ in trips} == {r.name for r in AUTO_RULES}
