"""The broker layer: the interface, the credential handle, and the simulator.

Three things are being defended here, in order of how expensive they are to get
wrong:

1. **No look-ahead.** A simulated fill may never use a price the decision could
   already see. Every "too good" backtest in existence is some variation of
   this bug, and fees do not rescue it.
2. **Idempotency.** The venue may have the order while the caller has an
   exception. A retry must return the same order, never a second one.
3. **Credential containment.** An adapter holds a *reference* to a vault entry,
   never a secret, so nothing above the broker layer — least of all a
   forecasting or language-model component — can obtain one by holding the
   wrong object.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.brokers import (BrokerAdapter, BrokerCapabilities,
                                     BrokerCredentials, FeeModel, PaperBroker,
                                     SlippageModel, resolve_credentials)
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (AccountSnapshot, Candle, Instrument,
                                       Mode, Order, OrderStatus, OrderType,
                                       Quote, Side, TimeInForce)
from olympus.trading.errors import (BrokerError, BrokerUnavailable,
                                    ConfigurationError, DuplicateOrderError)

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ")
SECRET = "sk-live-do-not-print-me"


def bar(i, o, h, l, c, volume=10_000.0, timeframe="1d"):
    t = T0 + timedelta(days=i)
    return Candle(instrument_key=INST.key, timeframe=timeframe, ts_open=t,
                  ts_close=t + timedelta(days=1), open=o, high=h, low=l,
                  close=c, volume=volume)


def order(coid="c1", side=Side.BUY, qty="10", otype=OrderType.MARKET, **kw):
    return Order(client_order_id=coid, instrument_key=INST.key, side=side,
                 order_type=otype, quantity=Decimal(qty), created_at=T0,
                 mode=Mode.PAPER, **kw)


def make_broker(**kw):
    kw.setdefault("clock", FixedClock(T0 + timedelta(days=1)))
    kw.setdefault("instruments", {INST.key: INST})
    kw.setdefault("starting_cash", Decimal("100000"))
    kw.setdefault("fee_model", FeeModel(bps=Decimal("1")))
    kw.setdefault("slippage_model", SlippageModel(bps=Decimal("2")))
    b = PaperBroker(**kw)
    b.connect()
    return b


@pytest.fixture()
def broker():
    return make_broker()


# --- 1. no look-ahead ------------------------------------------------------

def test_a_market_order_never_fills_at_the_signal_bars_close(broker):
    """The bar that caused the decision cannot also fill it."""
    broker.submit_order(order())
    assert broker.on_candle(bar(0, 100, 106, 99, 105)) == []
    fill = broker.on_candle(bar(1, 110, 112, 109, 111))[0]
    assert fill.price > Decimal("110"), "fill is the NEXT open (110) plus slippage"
    assert fill.price < Decimal("111"), "and certainly not the signal close of 105"


def test_latency_pushes_the_earliest_fill_further_out():
    """An order is not live until the acknowledgement would have arrived."""
    slow = make_broker(latency_ms=24 * 60 * 60 * 1000)      # one day
    slow.submit_order(order())
    assert slow.on_candle(bar(0, 100, 101, 99, 100)) == []
    assert slow.on_candle(bar(1, 100, 101, 99, 100)) == [], "still in flight"
    assert slow.on_candle(bar(2, 100, 101, 99, 100)), "acknowledged by now"


def test_a_quote_stamped_before_the_order_cannot_fill_it(broker):
    broker.submit_order(order())
    stale = Quote(instrument_key=INST.key, ts=T0, bid=99.0, ask=101.0)
    assert broker.on_quote(stale) == []
    fresh = Quote(instrument_key=INST.key, ts=T0 + timedelta(days=2),
                  bid=99.0, ask=101.0)
    assert broker.on_quote(fresh)


# --- 2. idempotency and duplicates ----------------------------------------

def test_a_resubmitted_order_returns_the_same_order_and_fills_once(broker):
    first = broker.submit_order(order())
    second = broker.submit_order(order())
    assert first.broker_order_id == second.broker_order_id
    broker.on_candle(bar(0, 100, 101, 99, 100))
    broker.on_candle(bar(1, 100, 101, 99, 100))
    assert broker.get_order("c1").filled_quantity == Decimal("10")
    assert len(broker.get_fills()) == 1


def test_reusing_an_id_with_different_terms_is_a_duplicate_not_a_retry(broker):
    """Same id, different quantity: that is a bug, and executing it silently
    would trade something nobody authorised."""
    broker.submit_order(order(qty="10"))
    with pytest.raises(DuplicateOrderError):
        broker.submit_order(order(qty="20"))
    assert broker.get_order("c1").quantity == Decimal("10")


def test_a_conflicting_side_is_also_refused(broker):
    broker.submit_order(order())
    with pytest.raises(DuplicateOrderError):
        broker.submit_order(order(side=Side.SELL))


def test_a_timeout_loses_the_reply_not_the_order(broker):
    """The venue accepted it; only the acknowledgement was lost. The retry must
    therefore find the SAME order and the position must move once."""
    broker.simulate_timeout(1)
    with pytest.raises(BrokerUnavailable):
        broker.submit_order(order())
    live = broker.get_order("c1")
    assert live is not None and live.status is OrderStatus.NEW
    retried = broker.submit_order(order())
    assert retried.broker_order_id == live.broker_order_id
    broker.on_candle(bar(0, 100, 101, 99, 100))
    broker.on_candle(bar(1, 100, 101, 99, 100))
    assert broker.get_order("c1").filled_quantity == Decimal("10")
    assert len(broker.get_fills()) == 1


# --- 3. credentials --------------------------------------------------------

@pytest.fixture()
def vaulted(monkeypatch):
    """A real vault entry, so the leak tests have something real to leak."""
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "test-passphrase")
    from olympus import vault
    if not vault.available():
        pytest.skip("vault backend unavailable")
    vault.put("alice", "broker-live", {"api_key": SECRET})
    return BrokerCredentials(ref="broker-live", user="alice")


def test_an_adapter_resolves_the_handle_itself(vaulted):
    assert resolve_credentials(vaulted)["api_key"] == SECRET


def test_the_handle_never_carries_or_prints_the_secret(vaulted):
    resolve_credentials(vaulted)                     # secret is in memory now
    for rendered in (repr(vaulted), str(vaulted), str(vaulted.to_dict())):
        assert SECRET not in rendered
    assert set(vaulted.__dataclass_fields__) == {"ref", "user"}


def test_a_connected_broker_never_prints_credentials(vaulted):
    b = make_broker()
    b.connect(vaulted)
    for rendered in (repr(b), str(b), str(b.health()), str(b.supports)):
        assert SECRET not in rendered


def test_a_string_credential_is_normalised_without_being_widened(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "test-passphrase")
    from olympus import vault
    if not vault.available():
        pytest.skip("vault backend unavailable")
    vault.put("bob", "token", SECRET)
    assert resolve_credentials(BrokerCredentials(ref="token", user="bob")) == \
        {"value": SECRET}


def test_a_missing_credential_fails_loudly_at_resolve_time(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "test-passphrase")
    from olympus import vault
    if not vault.available():
        pytest.skip("vault backend unavailable")
    with pytest.raises(BrokerUnavailable):
        resolve_credentials(BrokerCredentials(ref="nope", user="alice"))


def test_an_unusable_credential_store_is_an_unavailable_broker(monkeypatch):
    """No key configured means no credentials — that must read as 'broker
    unavailable', not as an authentication failure three calls later."""
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)
    with pytest.raises(BrokerUnavailable):
        resolve_credentials(BrokerCredentials(ref="anything"))


def test_an_empty_credential_ref_is_a_configuration_error():
    with pytest.raises(ConfigurationError):
        BrokerCredentials(ref="")


# --- 4. capabilities -------------------------------------------------------

def test_capabilities_refuse_an_unsupported_order_type_before_submission():
    b = make_broker(capabilities=BrokerCapabilities(
        order_types=frozenset({OrderType.MARKET})))
    with pytest.raises(BrokerError):
        b.submit_order(order(otype=OrderType.LIMIT, limit_price=Decimal("50")))
    assert b.get_order("c1") is None, "a refused order never reached the venue"


def test_capabilities_refuse_an_unsupported_time_in_force():
    b = make_broker(capabilities=BrokerCapabilities(
        order_types=frozenset({OrderType.MARKET}),
        time_in_force=frozenset({TimeInForce.DAY})))
    with pytest.raises(BrokerError):
        b.submit_order(order(time_in_force=TimeInForce.FOK))


def test_supports_is_a_json_safe_declaration(broker):
    supports = broker.supports
    assert "market" in supports["order_types"] and "limit" in supports["order_types"]
    assert supports["honours_client_order_id"] is True
    import json
    json.dumps(supports)                             # must survive the audit trail


# --- 5. order types --------------------------------------------------------

def test_a_limit_order_only_fills_when_the_range_covers_it(broker):
    broker.submit_order(order(otype=OrderType.LIMIT, limit_price=Decimal("50")))
    broker.on_candle(bar(0, 100, 101, 99, 100))
    assert broker.on_candle(bar(1, 100, 101, 99, 100)) == [], "low never reached 50"
    fill = broker.on_candle(bar(2, 60, 61, 49, 55))[0]
    assert fill.price <= Decimal("50"), "a limit can never fill worse than its limit"


def test_a_limit_that_gaps_through_fills_at_the_better_open(broker):
    broker.submit_order(order(otype=OrderType.LIMIT, limit_price=Decimal("100")))
    broker.on_candle(bar(0, 100, 101, 99, 100))
    fill = broker.on_candle(bar(1, 90, 95, 89, 94))[0]
    # Crossing at the open is an aggressive fill, so slippage applies — but it
    # is still priced off the 90 open, not the 100 limit the order asked for.
    assert Decimal("90") <= fill.price < Decimal("91")


def test_a_stop_triggers_only_on_the_bar_range(broker):
    broker.submit_order(order(otype=OrderType.STOP, stop_price=Decimal("120")))
    broker.on_candle(bar(0, 100, 101, 99, 100))
    assert broker.on_candle(bar(1, 100, 110, 99, 105)) == [], "never touched 120"
    assert broker.on_candle(bar(2, 100, 125, 99, 120))
    assert broker.get_order("c1").status is OrderStatus.FILLED


def test_a_stop_that_gaps_through_fills_at_the_open_not_the_stop(broker):
    """Gap realism. Pretending a stop fills exactly at its price is how a
    backtest invents a loss cap that does not exist."""
    broker.submit_order(order(side=Side.SELL, otype=OrderType.STOP,
                              stop_price=Decimal("90")))
    broker.on_candle(bar(0, 100, 101, 99, 100))
    fill = broker.on_candle(bar(1, 80, 82, 79, 81))[0]
    assert fill.price <= Decimal("80"), "the gap-down open, not the 90 stop"


def test_a_stop_limit_can_trigger_and_still_not_fill(broker):
    """Its trigger and its fill are two separate events — that gap IS the risk
    of using one."""
    broker.submit_order(order(otype=OrderType.STOP_LIMIT,
                              stop_price=Decimal("110"),
                              limit_price=Decimal("108")))
    broker.on_candle(bar(0, 100, 101, 99, 100))
    assert broker.on_candle(bar(1, 111, 120, 111, 119)) == [], (
        "triggered at 110, but the bar never traded back down to the 108 limit")
    assert broker.get_order("c1").status is OrderStatus.NEW


def test_an_ioc_order_expires_rather_than_resting(broker):
    broker.submit_order(order(otype=OrderType.LIMIT, limit_price=Decimal("50"),
                              time_in_force=TimeInForce.IOC))
    broker.on_candle(bar(0, 100, 101, 99, 100))
    broker.on_candle(bar(1, 100, 101, 99, 100))
    assert broker.get_order("c1").status is OrderStatus.EXPIRED


# --- 6. partial fills ------------------------------------------------------

def test_partial_fills_accumulate_to_exactly_the_order_quantity():
    b = make_broker(partial_fill_ratio=Decimal("0.5"))
    b.submit_order(order())
    for i in range(0, 10):
        b.on_candle(bar(i, 100, 101, 99, 100))
    done = b.get_order("c1")
    assert done.status is OrderStatus.FILLED
    assert done.filled_quantity == Decimal("10")
    assert sum(f.quantity for f in b.get_fills()) == Decimal("10")
    assert len(b.get_fills()) > 1, "the point of the test is that it took several"


def test_a_partially_filled_order_reports_a_blended_average_price():
    b = make_broker(partial_fill_ratio=Decimal("0.5"))
    b.submit_order(order())
    b.on_candle(bar(0, 100, 101, 99, 100))
    b.on_candle(bar(1, 100, 101, 99, 100))
    b.on_candle(bar(2, 200, 201, 199, 200))
    o = b.get_order("c1")
    assert o.status is OrderStatus.PARTIALLY_FILLED
    assert Decimal("100") < o.average_fill_price < Decimal("200")


# --- 7. fees and slippage --------------------------------------------------

def test_fees_and_slippage_reach_the_cash_balance():
    b = make_broker()
    b.submit_order(order())
    b.on_candle(bar(0, 100, 101, 99, 100))
    fill = b.on_candle(bar(1, 100, 101, 99, 100))[0]
    assert fill.price > Decimal("100"), "a buy slips upwards"
    assert fill.fee > 0
    expected = Decimal("100000") - fill.quantity * fill.price - fill.fee
    assert b.get_account().cash == expected
    assert b.get_order("c1").fees == fill.fee


def test_maker_and_taker_are_charged_differently():
    b = make_broker(fee_model=FeeModel(bps=Decimal("5"), maker_bps=Decimal("0"),
                                       taker_bps=Decimal("10")),
                    slippage_model=SlippageModel(bps=Decimal("0")))
    b.submit_order(order("taker", otype=OrderType.MARKET))
    b.submit_order(order("maker", otype=OrderType.LIMIT,
                         limit_price=Decimal("100")))
    b.on_candle(bar(0, 100, 101, 99, 100))
    fills = {f.client_order_id: f for f in b.on_candle(bar(1, 100, 101, 99, 100))}
    assert fills["taker"].liquidity == "taker" and fills["taker"].fee > 0
    assert fills["maker"].liquidity == "maker" and fills["maker"].fee == 0


def test_a_fee_floor_is_respected():
    model = FeeModel(bps=Decimal("0"), minimum=Decimal("1"))
    assert model.fee_for(Decimal("1"), Decimal("10")) == Decimal("1")


def test_per_share_and_fixed_components_add_up():
    model = FeeModel(per_share=Decimal("0.005"), bps=Decimal("0"),
                     fixed=Decimal("1"))
    assert model.fee_for(Decimal("200"), Decimal("10")) == Decimal("2.000")


def test_slippage_is_always_adverse_in_both_directions():
    model = SlippageModel(bps=Decimal("10"))
    assert model.apply(Decimal("100"), Side.BUY) > Decimal("100")
    assert model.apply(Decimal("100"), Side.SELL) < Decimal("100")


def test_participation_slippage_punishes_eating_the_whole_bar():
    b = make_broker(slippage_model=SlippageModel(
        bps=Decimal("0"), participation_impact_bps=Decimal("100")))
    b.submit_order(order(qty="10"))
    b.on_candle(bar(0, 100, 101, 99, 100, volume=10.0))
    fill = b.on_candle(bar(1, 100, 101, 99, 100, volume=10.0))[0]
    assert fill.price == Decimal("101"), "100% participation costs the full 100bp"


def test_spread_proportional_slippage_costs_more_on_a_wide_book():
    tight = SlippageModel(bps=Decimal("0"), spread_fraction=Decimal("1"))
    assert tight.apply(Decimal("100"), Side.BUY, spread=Decimal("0.02")) \
        < tight.apply(Decimal("100"), Side.BUY, spread=Decimal("2"))


def test_a_marketable_order_crosses_the_spread_not_the_mid(broker):
    """Filling at the mid hands the strategy a half-spread nobody gets."""
    b = make_broker(slippage_model=SlippageModel(bps=Decimal("0")))
    b.submit_order(order())
    fills = b.on_quote(Quote(instrument_key=INST.key,
                             ts=T0 + timedelta(days=2), bid=99.0, ask=101.0))
    assert fills[0].price == Decimal("101"), "bought at the ask, not the 100 mid"


def test_slippage_never_invents_a_non_positive_price():
    absurd = SlippageModel(bps=Decimal("100000"))
    assert absurd.apply(Decimal("10"), Side.SELL) == Decimal("10")


# --- 8. failure modes ------------------------------------------------------

def test_a_named_rejection_is_terminal_and_never_fills():
    b = make_broker(reject_order_ids=["c1"])
    rejected = b.submit_order(order())
    assert rejected.status is OrderStatus.REJECTED
    b.on_candle(bar(0, 100, 101, 99, 100))
    assert b.on_candle(bar(1, 100, 101, 99, 100)) == []


def test_random_rejections_are_reproducible_from_the_seed():
    def run():
        b = make_broker(reject_probability=0.5, seed=1234)
        return [b.submit_order(order(f"c{i}")).status for i in range(20)]

    first, second = run(), run()
    assert first == second, "same seed, same run — otherwise it is not evidence"
    assert OrderStatus.REJECTED in first and OrderStatus.NEW in first


def test_stochastic_rejection_without_a_seed_is_refused():
    with pytest.raises(ConfigurationError):
        PaperBroker(clock=FixedClock(T0), reject_probability=0.1)


def test_nonsensical_simulation_parameters_are_refused():
    with pytest.raises(ConfigurationError):
        PaperBroker(clock=FixedClock(T0), reject_probability=1.5, seed=1)
    with pytest.raises(ConfigurationError):
        PaperBroker(clock=FixedClock(T0), latency_ms=-1)
    with pytest.raises(ConfigurationError):
        FeeModel(minimum=Decimal("-1"))
    with pytest.raises(ConfigurationError):
        SlippageModel(spread_fraction=Decimal("-0.5"))


def test_an_outage_fails_every_call_loudly(broker):
    broker.simulate_outage(True)
    assert broker.is_connected() is False
    for call in (broker.get_account, broker.get_positions,
                 broker.get_open_orders, broker.get_fills):
        with pytest.raises(BrokerUnavailable):
            call()
    with pytest.raises(BrokerUnavailable):
        broker.submit_order(order("c-out"))
    with pytest.raises(BrokerUnavailable):
        broker.cancel_order("c1")


def test_recovery_from_an_outage_keeps_prior_state(broker):
    broker.submit_order(order())
    broker.simulate_outage(True)
    broker.simulate_outage(False)
    assert broker.is_connected() is True
    assert broker.get_order("c1").status is OrderStatus.NEW


def test_health_reports_failure_instead_of_raising(broker):
    broker.simulate_outage(True)
    health = broker.health()
    assert health["ok"] is False and health["name"] == "paper"


def test_cancelling_prevents_a_fill_and_unknown_orders_raise(broker):
    broker.submit_order(order())
    assert broker.cancel_order("c1").status is OrderStatus.CANCELED
    broker.on_candle(bar(0, 100, 101, 99, 100))
    assert broker.on_candle(bar(1, 100, 101, 99, 100)) == []
    with pytest.raises(BrokerError):
        broker.cancel_order("unknown")


def test_replace_adjusts_a_live_order_but_not_a_terminal_one(broker):
    broker.submit_order(order())
    replaced = broker.replace_order("c1", quantity=Decimal("5"))
    assert replaced.quantity == Decimal("5")
    broker.cancel_order("c1")
    with pytest.raises(BrokerError):
        broker.replace_order("c1", quantity=Decimal("7"))


# --- 9. book keeping and reconciliation hooks ------------------------------

def test_the_venue_keeps_its_own_position_and_realised_pnl(broker):
    broker.submit_order(order("buy", qty="10"))
    broker.on_candle(bar(0, 100, 101, 99, 100))
    broker.on_candle(bar(1, 100, 101, 99, 100))
    broker.submit_order(order("sell", side=Side.SELL, qty="5"))
    broker.on_candle(bar(2, 200, 201, 199, 200))
    position = {p.instrument_key: p for p in broker.get_positions()}[INST.key]
    assert position.quantity == Decimal("5")
    assert position.realised_pnl > 0, "sold half at double the cost basis"
    assert position.fees_paid > 0


def test_force_desync_gives_reconciliation_a_real_disagreement(broker):
    broker.submit_order(order())
    broker.on_candle(bar(0, 100, 101, 99, 100))
    broker.on_candle(bar(1, 100, 101, 99, 100))
    broker.force_desync(INST.key, Decimal("25"))
    position = {p.instrument_key: p for p in broker.get_positions()}[INST.key]
    assert position.quantity == Decimal("35")
    assert sum(f.signed_quantity for f in broker.get_fills()) == Decimal("10"), (
        "the fills say 10 and the venue says 35 — exactly the break a "
        "reconciler must detect")


def test_the_account_snapshot_marks_open_positions(broker):
    broker.submit_order(order())
    broker.on_candle(bar(0, 100, 101, 99, 100))
    broker.on_candle(bar(1, 100, 101, 99, 100))
    account = broker.get_account()
    assert isinstance(account, AccountSnapshot)
    assert account.equity > account.cash, "the shares are worth something"


def test_get_fills_since_filters_by_time(broker):
    broker.submit_order(order("a"))
    broker.on_candle(bar(0, 100, 101, 99, 100))
    broker.on_candle(bar(1, 100, 101, 99, 100))
    broker.submit_order(order("b"))
    broker.on_candle(bar(2, 100, 101, 99, 100))
    broker.on_candle(bar(3, 100, 101, 99, 100))
    assert len(broker.get_fills()) == 2
    assert len(broker.get_fills(since=T0 + timedelta(days=3))) == 1


def test_the_venue_only_replays_bars_it_was_actually_given(broker):
    for i in range(3):
        broker.on_candle(bar(i, 100, 101, 99, 100))
    assert len(broker.get_candles(INST.key, "1d")) == 3
    assert len(broker.get_candles(INST.key, "1d",
                                  start=T0 + timedelta(days=1))) == 2
    assert broker.get_candles(INST.key, "1h") == []
    assert broker.get_candles("NASDAQ:MSFT", "1d") == []


def test_a_quote_is_synthesised_only_once_a_price_is_known(broker):
    assert broker.get_quote(INST.key) is None
    broker.on_candle(bar(0, 100, 101, 99, 100))
    quote = broker.get_quote(INST.key)
    assert quote is not None and quote.bid < quote.ask


# --- 10. the abstract contract --------------------------------------------

class _BareAdapter(BrokerAdapter):
    """The minimum a venue must implement; everything else has a safe default."""
    name = "bare"
    venue = "nowhere"

    @property
    def capabilities(self):
        return BrokerCapabilities()

    def connect(self, credentials=None): self._up = True
    def disconnect(self): self._up = False
    def is_connected(self): return getattr(self, "_up", False)
    def get_account(self): raise NotImplementedError
    def get_positions(self): return []
    def submit_order(self, order): raise NotImplementedError
    def cancel_order(self, client_order_id): raise NotImplementedError
    def get_order(self, client_order_id): return None
    def get_open_orders(self): return []
    def get_fills(self, since=None): return []


def test_the_interface_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        BrokerAdapter()


def test_an_adapter_that_cannot_replace_says_so_rather_than_pretending():
    adapter = _BareAdapter()
    adapter.connect()
    with pytest.raises(BrokerError):
        adapter.replace_order("c1", quantity=Decimal("1"))


def test_optional_market_data_defaults_are_empty_not_invented():
    adapter = _BareAdapter()
    assert adapter.get_quote(INST.key) is None
    assert adapter.get_candles(INST.key, "1d") == []


def test_a_disconnected_adapter_guard_raises_broker_unavailable():
    adapter = _BareAdapter()
    with pytest.raises(BrokerUnavailable):
        adapter._require_connected()
    assert adapter.health()["ok"] is False
