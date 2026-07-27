"""Tests for the kill switches — the ability to stop and have it stick.

A kill switch is easy to write and easy to get subtly wrong, and the wrong
versions all fail the same way: they look engaged in the test that engages them
and are quietly gone by the time they are needed. These tests therefore attack
the properties rather than the API surface:

* **Survives a restart.** Every "is it still engaged" assertion is made through
  a *different* registry instance from the one that engaged it, because an
  in-memory switch passes any test that reuses the same object.
* **No read caching.** A registry constructed *before* the engage must still see
  it, since the CLI, the monitor thread, and the trading loop are different
  objects (often different processes) and a cache with any TTL is a window in
  which a halted system keeps trading.
* **An automatic trip cannot be cleared by ordinary code.** The failed
  disengage must also leave the switch ENGAGED — a "safety" device that raises
  and unlatches anyway is worse than one that never raised.
* **Fails closed when the store is unreadable or corrupt.** Not "no record
  found, carry on".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus import store
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import PortfolioSnapshot, Position
from olympus.trading.errors import ConfigurationError, KillSwitchActive, TradingError
from olympus.trading import killswitch as K
from olympus.trading.killswitch import (AUTO_RULES, AutoTripRules,
                                        KillSwitchRegistry, KillSwitchScope,
                                        KillSwitchState, Trip, TripRule,
                                        evaluate_auto_trips)
from olympus.trading.risk import RiskLimits

T0 = datetime(2026, 3, 2, 9, 30, tzinfo=timezone.utc)
INST = "NASDAQ:AAPL"


@pytest.fixture()
def clock():
    return FixedClock(T0)


def _registry(clock=None, **kw):
    return KillSwitchRegistry(clock=clock or FixedClock(T0), **kw)


def _limits(**kw):
    """Only the fields the trip rules read; everything else stays unset so a
    test's failure points at the one rule it is about."""
    base = dict(require_stop_loss=False,
                allowed_modes=frozenset(),
                max_daily_loss=None, max_portfolio_drawdown=None,
                max_strategy_drawdown=None, max_orders_per_minute=None,
                max_data_age_s=None, max_reconciliation_age_s=None)
    base.update(kw)
    return RiskLimits(**base)


# ---------------------------------------------------------------------------
# the state record
# ---------------------------------------------------------------------------

def test_a_global_switch_must_not_carry_a_target():
    """A GLOBAL switch with a target would key differently from the one
    `check()` looks up, i.e. engage successfully and block nothing."""
    with pytest.raises(ConfigurationError):
        KillSwitchState(scope=KillSwitchScope.GLOBAL, target=INST, engaged=True)


@pytest.mark.parametrize("scope", [KillSwitchScope.STRATEGY,
                                   KillSwitchScope.INSTRUMENT])
def test_a_scoped_switch_requires_a_target(scope):
    with pytest.raises(ConfigurationError):
        KillSwitchState(scope=scope, target="", engaged=True)


def test_state_round_trips_through_its_dict_form():
    state = KillSwitchState(scope=KillSwitchScope.INSTRUMENT, target=INST,
                            engaged=True, reason="halt", code=K.CODE_DAILY_LOSS,
                            engaged_at=T0, engaged_by="op", auto=True)
    back = KillSwitchState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert back == state


def test_state_rejects_a_naive_timestamp():
    with pytest.raises(Exception):
        KillSwitchState(scope=KillSwitchScope.GLOBAL, target="", engaged=True,
                        engaged_at=datetime(2026, 3, 2, 9, 30))


def test_the_key_is_scope_qualified():
    """Two switches of different scope on the same name must not collide."""
    strat = KillSwitchState(scope=KillSwitchScope.STRATEGY, target="x", engaged=True)
    inst = KillSwitchState(scope=KillSwitchScope.INSTRUMENT, target="x", engaged=True)
    assert strat.key != inst.key


# ---------------------------------------------------------------------------
# engage / disengage / check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scope,target,probe", [
    (KillSwitchScope.GLOBAL, "", {}),
    (KillSwitchScope.STRATEGY, "s1", {"strategy_id": "s1"}),
    (KillSwitchScope.INSTRUMENT, INST, {"instrument_key": INST}),
])
def test_each_scope_blocks_through_check(scope, target, probe, clock):
    ks = _registry(clock)
    assert ks.check(**probe) is None
    ks.engage(scope, target, reason="halt", by="op")
    found = ks.check(**probe)
    assert found is not None and found.scope is scope and found.engaged


def test_a_narrow_switch_does_not_block_other_targets(clock):
    ks = _registry(clock)
    ks.engage(KillSwitchScope.INSTRUMENT, INST, reason="halt", by="op")
    assert ks.check(instrument_key="NASDAQ:MSFT") is None
    ks.engage(KillSwitchScope.STRATEGY, "s1", reason="halt", by="op")
    assert ks.check(strategy_id="s2") is None


def test_global_wins_over_a_narrower_disengage(clock):
    """Broadest-first ordering: clearing a narrow switch must not re-open a
    global halt."""
    ks = _registry(clock)
    ks.engage(KillSwitchScope.GLOBAL, reason="halt", by="op")
    ks.disengage(KillSwitchScope.INSTRUMENT, INST, by="op")
    found = ks.check(instrument_key=INST, strategy_id="s1")
    assert found is not None and found.scope is KillSwitchScope.GLOBAL


def test_check_reports_the_broadest_engaged_switch(clock):
    ks = _registry(clock)
    ks.engage(KillSwitchScope.INSTRUMENT, INST, reason="i", by="op")
    ks.engage(KillSwitchScope.STRATEGY, "s1", reason="s", by="op")
    ks.engage(KillSwitchScope.GLOBAL, reason="g", by="op")
    assert ks.check(instrument_key=INST,
                    strategy_id="s1").scope is KillSwitchScope.GLOBAL


def test_engage_is_idempotent_and_refreshes_the_reason(clock):
    ks = _registry(clock)
    ks.engage(KillSwitchScope.GLOBAL, reason="first", by="op")
    ks.engage(KillSwitchScope.GLOBAL, reason="second", by="op2")
    assert len([s for s in ks.all() if s.scope is KillSwitchScope.GLOBAL]) == 1
    assert ks.check().reason == "second"


def test_engaged_at_comes_from_the_injected_clock():
    """No wall-clock reads: the recorded time is the one we supplied."""
    clock = FixedClock(T0)
    state = _registry(clock).engage(KillSwitchScope.GLOBAL, reason="halt", by="op")
    assert state.engaged_at == T0
    clock.advance(timedelta(hours=3))
    later = _registry(clock).engage(KillSwitchScope.GLOBAL, reason="halt", by="op")
    assert later.engaged_at == T0 + timedelta(hours=3)


def test_all_lists_engaged_and_disengaged_switches(clock):
    ks = _registry(clock)
    ks.engage(KillSwitchScope.GLOBAL, reason="halt", by="op")
    ks.engage(KillSwitchScope.INSTRUMENT, INST, reason="halt", by="op")
    ks.disengage(KillSwitchScope.INSTRUMENT, INST, by="op")
    assert {s.key for s in ks.all()} == {"global", f"instrument:{INST}"}
    assert [s.key for s in ks.engaged()] == ["global"]


# ---------------------------------------------------------------------------
# persistence — the property the whole device rests on
# ---------------------------------------------------------------------------

def test_a_switch_survives_a_restart(clock):
    """A switch cleared by the crash that tripped it is not a safety device."""
    _registry(clock).engage(KillSwitchScope.GLOBAL, reason="halt", by="op")

    restarted = _registry(FixedClock(T0 + timedelta(days=1)))   # new object
    assert restarted.is_engaged(KillSwitchScope.GLOBAL) is True
    assert restarted.check() is not None
    assert restarted.check().reason == "halt"


@pytest.mark.parametrize("scope,target", [
    (KillSwitchScope.GLOBAL, ""),
    (KillSwitchScope.STRATEGY, "s1"),
    (KillSwitchScope.INSTRUMENT, INST),
])
def test_every_scope_survives_a_restart(scope, target, clock):
    _registry(clock).engage(scope, target, reason="halt", by="op")
    assert _registry(FixedClock(T0)).is_engaged(scope, target) is True


def test_a_disengage_also_survives_a_restart(clock):
    """The clearing must persist too, or a restart re-halts a healthy system."""
    _registry(clock).engage(KillSwitchScope.GLOBAL, reason="halt", by="op")
    _registry(clock).disengage(KillSwitchScope.GLOBAL, by="op")
    assert _registry(FixedClock(T0)).is_engaged(KillSwitchScope.GLOBAL) is False


def test_a_registry_created_before_the_engage_still_sees_it(clock):
    """No read caching. The trading loop's registry predates the operator's."""
    trading_loop = _registry(clock)
    assert trading_loop.check() is None
    _registry(clock).engage(KillSwitchScope.GLOBAL, reason="halt", by="cli")
    assert trading_loop.check() is not None


def test_switches_are_isolated_by_namespace(clock):
    """`store_ns` is the documented spelling; `namespace` is the older one."""
    a = KillSwitchRegistry(clock=clock, store_ns="trading.killswitch.a")
    b = KillSwitchRegistry(clock=clock, namespace="trading.killswitch.b")
    a.engage(KillSwitchScope.GLOBAL, reason="halt", by="op")
    assert a.is_engaged(KillSwitchScope.GLOBAL) is True
    assert b.is_engaged(KillSwitchScope.GLOBAL) is False
    assert a.store_ns == "trading.killswitch.a"


# ---------------------------------------------------------------------------
# automatic trips need an operator to clear
# ---------------------------------------------------------------------------

def test_an_auto_trip_needs_an_operator_override(clock):
    ks = _registry(clock)
    ks.engage(KillSwitchScope.GLOBAL, reason="daily loss", code=K.CODE_DAILY_LOSS,
              by="monitor", auto=True)
    with pytest.raises(KillSwitchActive):
        ks.disengage(KillSwitchScope.GLOBAL, by="trading-loop")
    ks.disengage(KillSwitchScope.GLOBAL, by="operator", operator_override=True)
    assert ks.is_engaged(KillSwitchScope.GLOBAL) is False


def test_a_refused_disengage_leaves_the_switch_engaged(clock):
    """Raising and unlatching anyway would be the worst of both worlds."""
    ks = _registry(clock)
    ks.engage(KillSwitchScope.INSTRUMENT, INST, reason="stale data",
              code=K.CODE_STALE_DATA, by="monitor", auto=True)
    with pytest.raises(KillSwitchActive):
        ks.disengage(KillSwitchScope.INSTRUMENT, INST, by="loop")
    assert _registry(clock).is_engaged(KillSwitchScope.INSTRUMENT, INST) is True


def test_the_override_requirement_survives_a_restart(clock):
    """The `auto` flag is persisted, not held in the object that tripped it."""
    _registry(clock).engage(KillSwitchScope.GLOBAL, reason="auto",
                            code=K.CODE_ORDER_RATE, by="monitor", auto=True)
    fresh = _registry(FixedClock(T0 + timedelta(hours=2)))
    with pytest.raises(KillSwitchActive):
        fresh.disengage(KillSwitchScope.GLOBAL, by="loop")


def test_a_manual_switch_clears_without_an_override(clock):
    ks = _registry(clock)
    ks.engage(KillSwitchScope.GLOBAL, reason="manual", by="op")
    ks.disengage(KillSwitchScope.GLOBAL, by="op")
    assert ks.is_engaged(KillSwitchScope.GLOBAL) is False


def test_disengaging_an_absent_switch_is_allowed(clock):
    """Nothing to clear is not an error; the post-condition is what matters."""
    ks = _registry(clock)
    ks.disengage(KillSwitchScope.STRATEGY, "never-engaged", by="op")
    assert ks.is_engaged(KillSwitchScope.STRATEGY, "never-engaged") is False


def test_an_auto_switch_cleared_by_override_no_longer_needs_one(clock):
    ks = _registry(clock)
    ks.engage(KillSwitchScope.GLOBAL, reason="auto", by="monitor", auto=True)
    ks.disengage(KillSwitchScope.GLOBAL, by="op", operator_override=True)
    ks.engage(KillSwitchScope.GLOBAL, reason="manual", by="op")
    ks.disengage(KillSwitchScope.GLOBAL, by="op")           # no override needed
    assert ks.is_engaged(KillSwitchScope.GLOBAL) is False


# ---------------------------------------------------------------------------
# failing closed
# ---------------------------------------------------------------------------

def test_a_corrupt_record_raises_rather_than_reading_as_clear(clock):
    """If we cannot tell what an operator set, the safe reading is 'stopped'."""
    ks = _registry(clock)
    ks.engage(KillSwitchScope.GLOBAL, reason="halt", by="op")
    store.backend().put(ks.namespace, "global", b"{not json")
    with pytest.raises(TradingError):
        ks.check()
    with pytest.raises(TradingError):
        ks.is_engaged(KillSwitchScope.GLOBAL)


def test_an_unreadable_store_raises_rather_than_returning_none(clock, monkeypatch):
    class Broken:
        def get(self, ns, key):
            raise OSError("disk gone")

        def keys(self, ns):
            raise OSError("disk gone")

    monkeypatch.setattr(store, "backend", lambda: Broken())
    ks = _registry(clock)
    with pytest.raises(TradingError):
        ks.check()
    with pytest.raises(TradingError):
        ks.all()


def test_a_failed_write_propagates_out_of_engage(clock, monkeypatch):
    """`engage` has no failure mode that quietly leaves trading enabled."""
    class WriteOnlyFails:
        def get(self, ns, key):
            return None

        def put(self, ns, key, value):
            raise OSError("read-only filesystem")

    monkeypatch.setattr(store, "backend", lambda: WriteOnlyFails())
    with pytest.raises(OSError):
        _registry(clock).engage(KillSwitchScope.GLOBAL, reason="halt", by="op")


# ---------------------------------------------------------------------------
# automatic trip rules
# ---------------------------------------------------------------------------

def test_each_condition_trips_its_own_rule_and_scope():
    limits = _limits(max_daily_loss=Decimal("100"),
                     max_portfolio_drawdown=Decimal("500"),
                     max_strategy_drawdown=Decimal("50"),
                     max_orders_per_minute=5, max_data_age_s=60.0,
                     max_reconciliation_age_s=600.0)
    trips = AutoTripRules().evaluate(stats={
        "daily_pnl": Decimal("-150"), "portfolio_drawdown": Decimal("600"),
        "strategy_drawdowns": {"s1": Decimal("80")}, "orders_last_minute": 9,
        "data_age_s": 120.0, "reconciliation_age_s": 900.0}, limits=limits)
    by_code = {t.code: t for t in trips}
    assert set(by_code) == {K.CODE_DAILY_LOSS, K.CODE_PORTFOLIO_DRAWDOWN,
                            K.CODE_STRATEGY_DRAWDOWN, K.CODE_ORDER_RATE,
                            K.CODE_STALE_DATA, K.CODE_BROKER_DESYNC}
    # The scope/target pairing is the part that silently blocks nothing when
    # it is wrong, so assert it explicitly.
    assert by_code[K.CODE_STRATEGY_DRAWDOWN].scope is KillSwitchScope.STRATEGY
    assert by_code[K.CODE_STRATEGY_DRAWDOWN].target == "s1"
    assert by_code[K.CODE_DAILY_LOSS].scope is KillSwitchScope.GLOBAL
    assert by_code[K.CODE_DAILY_LOSS].target == ""


@pytest.mark.parametrize("stats,limit_kw,code", [
    ({"daily_pnl": Decimal("-100")}, {"max_daily_loss": Decimal("100")},
     K.CODE_DAILY_LOSS),
    ({"portfolio_drawdown": Decimal("500")},
     {"max_portfolio_drawdown": Decimal("500")}, K.CODE_PORTFOLIO_DRAWDOWN),
    ({"strategy_drawdowns": {"s": Decimal("50")}},
     {"max_strategy_drawdown": Decimal("50")}, K.CODE_STRATEGY_DRAWDOWN),
    ({"orders_last_minute": 6}, {"max_orders_per_minute": 5}, K.CODE_ORDER_RATE),
    ({"data_age_s": 60.1}, {"max_data_age_s": 60.0}, K.CODE_STALE_DATA),
    ({"reconciliation_age_s": 600.1}, {"max_reconciliation_age_s": 600.0},
     K.CODE_BROKER_DESYNC),
])
def test_each_rule_trips_at_its_boundary(stats, limit_kw, code):
    trips = AutoTripRules().evaluate(stats=stats, limits=_limits(**limit_kw))
    assert [t.code for t in trips] == [code]


@pytest.mark.parametrize("stats,limit_kw", [
    ({"daily_pnl": Decimal("-99.99")}, {"max_daily_loss": Decimal("100")}),
    ({"portfolio_drawdown": Decimal("499.99")},
     {"max_portfolio_drawdown": Decimal("500")}),
    ({"strategy_drawdowns": {"s": Decimal("49.99")}},
     {"max_strategy_drawdown": Decimal("50")}),
    ({"orders_last_minute": 5}, {"max_orders_per_minute": 5}),
    ({"data_age_s": 60.0}, {"max_data_age_s": 60.0}),
    ({"reconciliation_age_s": 600.0}, {"max_reconciliation_age_s": 600.0}),
])
def test_just_inside_each_boundary_does_not_trip(stats, limit_kw):
    assert AutoTripRules().evaluate(stats=stats, limits=_limits(**limit_kw)) == []


def test_a_profitable_day_never_trips_the_loss_rule():
    """P&L is signed. Comparing its magnitude would halt on a good day, which
    teaches operators to override the switch reflexively."""
    trips = AutoTripRules().evaluate(stats={"daily_pnl": Decimal("5000")},
                                     limits=_limits(max_daily_loss=Decimal("100")))
    assert trips == []


def test_a_missing_measurement_never_trips():
    """'We did not measure it' is not 'it is fine' — but it is also not a trip;
    the risk engine is what refuses to trade on an unmeasured limit."""
    limits = _limits(max_daily_loss=Decimal("100"), max_data_age_s=60.0,
                     max_orders_per_minute=5)
    assert AutoTripRules().evaluate(stats={}, limits=limits) == []


def test_an_unconfigured_limit_never_trips():
    assert AutoTripRules().evaluate(
        stats={"daily_pnl": Decimal("-99999"), "data_age_s": 99999.0},
        limits=_limits()) == []


def test_never_reconciled_is_a_desync():
    """The one asymmetry: no reconciliation age means it has NEVER succeeded."""
    trips = AutoTripRules().evaluate(
        stats={"reconciliation_required": True},
        limits=_limits(max_reconciliation_age_s=600.0))
    assert [t.code for t in trips] == [K.CODE_BROKER_DESYNC]


def test_reported_reconciliation_breaks_trip_immediately():
    trips = AutoTripRules().evaluate(stats={"reconciliation_ok": False},
                                     limits=_limits())
    assert [t.code for t in trips] == [K.CODE_BROKER_DESYNC]


def test_every_declared_rule_is_reachable():
    """Guards against a rule declared in AUTO_RULES but never evaluated."""
    limits = _limits(max_daily_loss=Decimal("1"),
                     max_portfolio_drawdown=Decimal("1"),
                     max_strategy_drawdown=Decimal("1"), max_orders_per_minute=1,
                     max_data_age_s=1.0, max_reconciliation_age_s=1.0)
    trips = AutoTripRules().evaluate(stats={
        "daily_pnl": Decimal("-99"), "portfolio_drawdown": Decimal("99"),
        "strategy_drawdowns": {"s": Decimal("99")}, "orders_last_minute": 99,
        "data_age_s": 99.0, "reconciliation_age_s": 99.0}, limits=limits)
    assert {t.rule.name for t in trips} == {r.name for r in AUTO_RULES}


def test_a_restricted_rule_set_evaluates_only_its_own_rules():
    only_loss = AutoTripRules([r for r in AUTO_RULES if r.name == "daily_loss"])
    trips = only_loss.evaluate(
        stats={"daily_pnl": Decimal("-99"), "data_age_s": 99.0},
        limits=_limits(max_daily_loss=Decimal("1"), max_data_age_s=1.0))
    assert [t.code for t in trips] == [K.CODE_DAILY_LOSS]


def test_evaluate_is_pure_and_repeatable():
    limits = _limits(max_daily_loss=Decimal("100"))
    stats = {"daily_pnl": Decimal("-150")}
    rules = AutoTripRules()
    a = [t.to_dict() for t in rules.evaluate(stats=stats, limits=limits)]
    b = [t.to_dict() for t in rules.evaluate(stats=stats, limits=limits)]
    assert a == b and a  # non-empty, so equality is not vacuous


def test_evaluate_does_not_mutate_the_caller_s_stats():
    stats = {"peak_equity": Decimal("100000")}
    AutoTripRules().evaluate(portfolio=_portfolio(), stats=stats,
                             limits=_limits(max_portfolio_drawdown=Decimal("1")))
    assert set(stats) == {"peak_equity"}


# --- portfolio fallbacks ---------------------------------------------------

def _portfolio(quantity="10", mark=90.0, realised="0", fees="0"):
    return PortfolioSnapshot(
        ts=T0, cash=Decimal("50000"),
        positions={INST: Position(instrument_key=INST,
                                  quantity=Decimal(quantity),
                                  average_price=Decimal("100"))},
        marks={INST: mark}, realised_pnl=Decimal(realised),
        fees_paid=Decimal(fees))


def test_the_portfolio_supplies_daily_pnl_when_the_caller_did_not():
    # 10 lots bought at 100, marked at 90 => -100 unrealised, minus 20 fees.
    trips = AutoTripRules().evaluate(portfolio=_portfolio(fees="20"),
                                     limits=_limits(max_daily_loss=Decimal("120")))
    assert [t.code for t in trips] == [K.CODE_DAILY_LOSS]


def test_a_supplied_measurement_beats_the_portfolio_derivation():
    """The monitor's own P&L (day boundaries, deposits) must win."""
    trips = AutoTripRules().evaluate(portfolio=_portfolio(fees="20"),
                                     stats={"daily_pnl": Decimal("0")},
                                     limits=_limits(max_daily_loss=Decimal("120")))
    assert trips == []


def test_portfolio_drawdown_is_derived_from_a_supplied_peak():
    pf = _portfolio()                       # equity = 50000 + 900 = 50900
    trips = AutoTripRules().evaluate(
        portfolio=pf, stats={"peak_equity": Decimal("51000")},
        limits=_limits(max_portfolio_drawdown=Decimal("100")))
    assert [t.code for t in trips] == [K.CODE_PORTFOLIO_DRAWDOWN]


def test_no_peak_means_no_derived_drawdown():
    trips = AutoTripRules().evaluate(
        portfolio=_portfolio(),
        limits=_limits(max_portfolio_drawdown=Decimal("1")))
    assert [t.code for t in trips] == []


def test_without_a_portfolio_evaluate_matches_the_low_level_function():
    limits = _limits(max_daily_loss=Decimal("100"))
    stats = {"daily_pnl": Decimal("-150")}
    assert [t.rule for t in AutoTripRules().evaluate(stats=stats, limits=limits)] \
        == [rule for rule, _, _ in evaluate_auto_trips(stats, limits)]


# --- applying trips --------------------------------------------------------

def test_apply_engages_each_trip_as_an_auto_switch(clock):
    ks = _registry(clock)
    trips = AutoTripRules().evaluate(
        stats={"daily_pnl": Decimal("-150"),
               "strategy_drawdowns": {"s1": Decimal("80")}},
        limits=_limits(max_daily_loss=Decimal("100"),
                       max_strategy_drawdown=Decimal("50")))
    states = AutoTripRules().apply(ks, trips)
    assert all(s.auto and s.engaged for s in states)
    assert ks.is_engaged(KillSwitchScope.GLOBAL) is True
    assert ks.is_engaged(KillSwitchScope.STRATEGY, "s1") is True
    # and the whole point: the loop cannot undo them
    with pytest.raises(KillSwitchActive):
        ks.disengage(KillSwitchScope.GLOBAL, by="trading-loop")


def test_applied_trips_carry_their_code_into_the_record(clock):
    ks = _registry(clock)
    AutoTripRules().apply(ks, AutoTripRules().evaluate(
        stats={"data_age_s": 999.0}, limits=_limits(max_data_age_s=1.0)))
    assert ks.check().code == K.CODE_STALE_DATA


def test_applying_no_trips_engages_nothing(clock):
    ks = _registry(clock)
    assert AutoTripRules().apply(ks, []) == []
    assert ks.check() is None


def test_trip_serialises_for_the_audit_trail():
    trip = Trip(rule=TripRule("r", "CODE", KillSwitchScope.STRATEGY),
                scope=KillSwitchScope.STRATEGY, target="s1", code="CODE",
                detail="because")
    assert json.loads(json.dumps(trip.to_dict())) == {
        "rule": "r", "scope": "strategy", "target": "s1", "code": "CODE",
        "detail": "because"}
