"""Binance Spot Testnet adapter.

Driven through an injected transport that replays Binance's documented response
shapes. That makes signing, parsing, idempotency mapping and error handling
exactly testable offline — and leaves one thing untested that no offline test
can cover: whether the venue accepts any of it. Every Binance host is refused by
this environment's egress policy (403 at CONNECT), so this adapter has never
completed a real request. See docs/TRADING_STATUS.md.

The tests are ordered by what would hurt most if wrong: the testnet guard, then
credential hygiene, then the idempotency path, then parsing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading.brokers.base import BrokerCredentials
from olympus.trading.brokers.binance_testnet import BinanceTestnetBroker
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (Instrument, Mode, Order, OrderStatus,
                                       OrderType, Side, TimeInForce)
from olympus.trading.errors import (BrokerError, BrokerUnavailable,
                                    ConfigurationError, DuplicateOrderError,
                                    ExecutionError)

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
BTC = Instrument(symbol="BTCUSDT", exchange="BINANCE",
                 tick_size=Decimal("0.01"), lot_size=Decimal("0.00001"),
                 min_quantity=Decimal("0.00001"), allow_fractional=True)
KEY = BTC.key


class FakeVenue:
    """Replays Binance's documented JSON. Records what it was asked."""

    def __init__(self, *, balances=None, orders=None, trades=None,
                 fail_with=None):
        self.balances = balances if balances is not None else [
            {"asset": "USDT", "free": "10000.00000000", "locked": "0.00000000"},
            {"asset": "BTC", "free": "0.50000000", "locked": "0.00000000"}]
        self.orders = dict(orders or {})
        self.trades = list(trades or [])
        self.fail_with = fail_with
        self.calls: list[tuple[str, str, dict, bool]] = []

    def __call__(self, method, path, *, params, signed):
        self.calls.append((method, path, dict(params), signed))
        if self.fail_with is not None and path == self.fail_with[0]:
            raise self.fail_with[1]
        if path == "/api/v3/ping":
            return {}
        if path == "/api/v3/account":
            return {"balances": self.balances}
        if path == "/api/v3/openOrders":
            return [o for o in self.orders.values() if o["status"] == "NEW"]
        if path == "/api/v3/myTrades":
            return self.trades
        if path == "/api/v3/order" and method == "POST":
            coid = params["newClientOrderId"]
            if coid in self.orders:
                raise BrokerError("binance rejected the request", status=400,
                                  path=path, binance_code=-2010,
                                  binance_msg="Duplicate order sent.")
            order = {
                "symbol": params["symbol"], "orderId": 700 + len(self.orders),
                "clientOrderId": coid, "transactTime": 1767571200000,
                "price": params.get("price", "0.00000000"),
                "origQty": params["quantity"], "executedQty": "0.00000000",
                "cummulativeQuoteQty": "0.00000000", "status": "NEW",
                "type": params["type"], "side": params["side"]}
            self.orders[coid] = order
            return order
        if path == "/api/v3/order" and method == "GET":
            coid = params.get("origClientOrderId")
            if coid not in self.orders:
                raise BrokerError("binance rejected the request", status=400,
                                  path=path, binance_code=-2013,
                                  binance_msg="Order does not exist.")
            return self.orders[coid]
        if path == "/api/v3/order" and method == "DELETE":
            coid = params["origClientOrderId"]
            self.orders[coid] = {**self.orders[coid], "status": "CANCELED"}
            return self.orders[coid]
        if path == "/api/v3/ticker/bookTicker":
            return {"bidPrice": "41999.00", "askPrice": "42001.00",
                    "bidQty": "1.5", "askQty": "2.0"}
        raise AssertionError(f"unexpected call {method} {path}")


def broker(venue=None, **kw):
    b = BinanceTestnetBroker(clock=FixedClock(T0), instruments={KEY: BTC},
                             transport=venue or FakeVenue(), **kw)
    b._connected = True
    b._api_key, b._api_secret = "k", "s"
    return b


def order(coid="olymp-abc", side=Side.BUY, qty="0.01",
          otype=OrderType.MARKET, **kw):
    return Order(client_order_id=coid, instrument_key=KEY, side=side,
                 order_type=otype, quantity=Decimal(qty), created_at=T0,
                 mode=Mode.PAPER, **kw)


# --- the guard that matters most ------------------------------------------

def test_a_production_host_is_refused_at_construction():
    """The one mistake that must be impossible to make quietly."""
    with pytest.raises(ConfigurationError) as excinfo:
        BinanceTestnetBroker(base_url="https://api.binance.com")
    assert "testnet" in str(excinfo.value).lower()


def test_futures_testnet_is_refused_as_a_derivatives_venue():
    with pytest.raises(ConfigurationError):
        BinanceTestnetBroker(base_url="https://testnet.binancefuture.com")


def test_a_non_https_base_url_is_refused():
    with pytest.raises(ConfigurationError):
        BinanceTestnetBroker(base_url="http://testnet.binance.vision")


def test_the_spot_testnet_host_is_accepted():
    b = BinanceTestnetBroker(base_url="https://testnet.binance.vision")
    assert b.base_url == "https://testnet.binance.vision"


# --- credential hygiene ----------------------------------------------------

def test_connect_without_credentials_is_refused():
    with pytest.raises(BrokerUnavailable):
        BinanceTestnetBroker(transport=FakeVenue()).connect(None)


def test_credentials_never_appear_in_repr():
    b = broker()
    b._api_secret = "SUPERSECRET"
    assert "SUPERSECRET" not in repr(b)
    assert "testnet.binance.vision" in repr(b)


def test_a_missing_secret_in_the_vault_entry_is_refused(monkeypatch):
    from olympus.trading.brokers import base as base_mod
    monkeypatch.setattr(base_mod, "resolve_credentials",
                        lambda handle: {"api_key": "only-a-key"})
    import olympus.trading.brokers.binance_testnet as mod
    monkeypatch.setattr(mod, "resolve_credentials",
                        lambda handle: {"api_key": "only-a-key"})
    b = BinanceTestnetBroker(transport=FakeVenue())
    with pytest.raises(BrokerUnavailable):
        b.connect(BrokerCredentials(ref="binance-testnet"))


def test_an_http_error_does_not_carry_the_signed_url():
    """A signed URL contains the HMAC; putting it in an exception is how a
    credential derivative reaches a log file."""
    venue = FakeVenue(fail_with=("/api/v3/account",
                                 BrokerError("binance rejected the request",
                                             status=401, path="/api/v3/account",
                                             binance_code=-2015,
                                             binance_msg="Invalid API-key")))
    b = broker(venue)
    with pytest.raises(BrokerError) as excinfo:
        b.get_account()
    rendered = str(excinfo.value)
    assert "signature=" not in rendered
    assert "http" not in rendered.lower() or "path" in rendered


# --- idempotency: the branch that prevents a doubled position -------------

def test_a_duplicate_client_order_id_adopts_rather_than_creating_a_second():
    venue = FakeVenue()
    b = broker(venue)
    first = b.submit_order(order())
    second = b.submit_order(order())          # the timeout retry
    assert first.broker_order_id == second.broker_order_id
    posts = [c for c in venue.calls if c[0] == "POST"]
    assert len(posts) == 2, "both attempts were made"
    assert len(venue.orders) == 1, "but only one order exists at the venue"


def test_a_duplicate_the_venue_will_not_return_raises_rather_than_guessing():
    class Stubborn(FakeVenue):
        def __call__(self, method, path, *, params, signed):
            if path == "/api/v3/order" and method == "POST":
                raise BrokerError("binance rejected the request", status=400,
                                  path=path, binance_code=-2010,
                                  binance_msg="Duplicate order sent.")
            if path == "/api/v3/order" and method == "GET":
                raise BrokerError("binance rejected the request", status=400,
                                  path=path, binance_code=-2013,
                                  binance_msg="Order does not exist.")
            return super().__call__(method, path, params=params, signed=signed)

    with pytest.raises(DuplicateOrderError):
        broker(Stubborn()).submit_order(order())


def test_get_order_looks_up_by_our_client_order_id():
    venue = FakeVenue()
    b = broker(venue)
    b.submit_order(order(coid="olymp-xyz"))
    found = b.get_order("olymp-xyz")
    assert found is not None and found.client_order_id == "olymp-xyz"
    assert b.get_order("olymp-missing") is None


# --- long-only / spot-only -------------------------------------------------

def test_an_uncovered_sell_is_refused_before_reaching_the_venue():
    venue = FakeVenue(balances=[
        {"asset": "USDT", "free": "10000", "locked": "0"},
        {"asset": "BTC", "free": "0.001", "locked": "0"}])
    b = broker(venue)
    with pytest.raises(ExecutionError) as excinfo:
        b.submit_order(order(side=Side.SELL, qty="5"))
    assert "long-only" in str(excinfo.value)
    assert not [c for c in venue.calls if c[0] == "POST"]


def test_a_covered_sell_is_allowed():
    b = broker(FakeVenue())                     # 0.5 BTC held
    result = b.submit_order(order(side=Side.SELL, qty="0.1"))
    assert result.side is Side.SELL


def test_capabilities_declare_no_shorting_and_no_replace():
    caps = broker().capabilities
    assert caps.supports_short is False
    assert caps.supports_replace is False
    assert OrderType.STOP not in caps.order_types


def test_replace_is_refused_rather_than_faked():
    """Cancel-and-new is a different operation with a real race window."""
    with pytest.raises(BrokerError):
        broker().replace_order("olymp-abc", quantity=Decimal("1"))


def test_an_unsupported_order_type_is_refused_up_front():
    with pytest.raises(BrokerError):
        broker().submit_order(order(otype=OrderType.STOP,
                                    stop_price=Decimal("40000")))


# --- parsing ---------------------------------------------------------------

def test_account_maps_the_quote_balance_to_cash():
    account = broker().get_account()
    assert account.cash == Decimal("10000.00000000")
    assert account.currency == "USDT"


def test_positions_report_non_quote_balances_without_inventing_a_basis():
    positions = {p.instrument_key: p for p in broker().get_positions()}
    assert positions[KEY].quantity == Decimal("0.5")
    assert positions[KEY].average_price == Decimal("0"), (
        "the venue reports no cost basis; inventing one would corrupt P&L and "
        "make a reconciliation break look like a rounding difference")


def test_a_partially_filled_order_maps_quantities_and_average_price():
    venue = FakeVenue(orders={"olymp-p": {
        "symbol": "BTCUSDT", "orderId": 42, "clientOrderId": "olymp-p",
        "price": "42000.00", "origQty": "0.10000000",
        "executedQty": "0.04000000", "cummulativeQuoteQty": "1680.00000000",
        "status": "PARTIALLY_FILLED", "type": "LIMIT", "side": "BUY",
        "time": 1767571200000}})
    result = broker(venue).get_order("olymp-p")
    assert result.status is OrderStatus.PARTIALLY_FILLED
    assert result.filled_quantity == Decimal("0.04")
    assert result.average_fill_price == Decimal("42000")   # 1680 / 0.04


def test_a_rejected_order_maps_to_rejected():
    venue = FakeVenue(orders={"olymp-r": {
        "symbol": "BTCUSDT", "orderId": 43, "clientOrderId": "olymp-r",
        "price": "0", "origQty": "0.01", "executedQty": "0",
        "cummulativeQuoteQty": "0", "status": "REJECTED", "type": "MARKET",
        "side": "BUY", "time": 1767571200000}})
    assert broker(venue).get_order("olymp-r").status is OrderStatus.REJECTED


def test_an_unrecognised_status_raises_rather_than_reading_as_open():
    """An unmapped status silently meaning 'still open' is how a terminal order
    gets resubmitted."""
    venue = FakeVenue(orders={"olymp-u": {
        "symbol": "BTCUSDT", "orderId": 44, "clientOrderId": "olymp-u",
        "price": "0", "origQty": "0.01", "executedQty": "0",
        "cummulativeQuoteQty": "0", "status": "SOME_NEW_STATUS",
        "type": "MARKET", "side": "BUY", "time": 1767571200000}})
    with pytest.raises(BrokerError):
        broker(venue).get_order("olymp-u")


def test_fills_carry_fees_liquidity_and_broker_ids():
    venue = FakeVenue(trades=[{
        "id": 9001, "orderId": 700, "clientOrderId": "olymp-abc",
        "price": "42000.00", "qty": "0.01000000",
        "commission": "0.00001000", "commissionAsset": "BTC",
        "time": 1767571260000, "isBuyer": True, "isMaker": False}])
    fills = broker(venue).get_fills()
    assert len(fills) == 1
    fill = fills[0]
    assert fill.quantity == Decimal("0.01")
    assert fill.fee == Decimal("0.00001")
    assert fill.fee_currency == "BTC"
    assert fill.liquidity == "taker"
    assert fill.broker_fill_id == "9001"


def test_cancel_marks_the_order_canceled():
    venue = FakeVenue()
    b = broker(venue)
    b.submit_order(order(coid="olymp-c"))
    assert b.cancel_order("olymp-c").status is OrderStatus.CANCELED


def test_cancelling_an_unknown_order_raises():
    with pytest.raises(BrokerError):
        broker().cancel_order("olymp-nope")


def test_health_reports_unreachable_rather_than_raising():
    class Dead(FakeVenue):
        def __call__(self, method, path, *, params, signed):
            raise BrokerUnavailable("binance testnet unreachable", path=path)
    assert broker(Dead()).health()["ok"] is False


def test_operations_require_a_connection():
    b = BinanceTestnetBroker(clock=FixedClock(T0), transport=FakeVenue())
    with pytest.raises(BrokerUnavailable):
        b.get_account()


def test_the_adapter_satisfies_the_broker_contract():
    from olympus.trading.brokers.base import BrokerAdapter
    assert issubclass(BinanceTestnetBroker, BrokerAdapter)
    b = broker()
    for method in ("get_account", "get_positions", "get_open_orders",
                   "get_order", "submit_order", "cancel_order", "get_fills"):
        assert callable(getattr(b, method))
