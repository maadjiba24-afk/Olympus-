"""A simulated venue.

This is not a stub. It is the paper-trading counterparty *and* the backtester's
fill engine, and using one implementation for both is what makes the claim "the
same strategy and risk code runs in backtest, paper, and live" checkable rather
than aspirational — if the fill semantics differed between them, a backtest
result would say nothing about paper performance.

The fill rule that matters
--------------------------
A market order fills at the **next** bar's open, never the current bar's close.

That single decision is the difference between a backtester and a fantasy. When
a strategy decides on bar *t*, the close of bar *t* is the price that *caused*
the decision; filling there means buying at a price you only knew because it
already happened. Every published backtest that looks too good has some version
of this bug in it. `on_candle` therefore stages triggered orders and fills them
on the following bar.

What is simulated, because omitting it flatters results
------------------------------------------------------
fees, slippage, partial fills, rejections, and outages. A simulator that always
fills completely at the mid, for free, will make almost any strategy look
profitable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from ..clock import Clock, default_clock
from ..contracts import (AccountSnapshot, Candle, Fill, Instrument, Order,
                         OrderStatus, OrderType, Position, Quote, Side,
                         TimeInForce, to_decimal)
from ..errors import (BrokerError, BrokerUnavailable, ConfigurationError,
                      DuplicateOrderError)
from .base import BrokerAdapter, BrokerCapabilities, BrokerCredentials


@dataclass(frozen=True)
class FeeModel:
    """Commission. Defaults are non-zero on purpose — a zero-fee default is a
    silent thumb on the scale, and someone will forget to override it."""
    per_share: Decimal = Decimal("0")
    bps: Decimal = Decimal("1")          # 1bp of notional
    fixed: Decimal = Decimal("0")
    minimum: Decimal = Decimal("0")

    def fee_for(self, quantity: Decimal, price: Decimal) -> Decimal:
        notional = abs(quantity * price)
        fee = (self.fixed + abs(quantity) * self.per_share
               + notional * self.bps / Decimal("10000"))
        return max(fee, self.minimum)


@dataclass(frozen=True)
class SlippageModel:
    """Adverse price movement between decision and fill.

    Always against the trader — buys fill higher, sells lower. Modelling it
    symmetrically (or not at all) is equivalent to assuming free liquidity.
    """
    bps: Decimal = Decimal("2")
    #: Extra slippage as a fraction of the bar's volume consumed.
    participation_impact_bps: Decimal = Decimal("0")

    def apply(self, price: Decimal, side: Side, *,
              participation: Decimal = Decimal("0")) -> Decimal:
        total_bps = self.bps + self.participation_impact_bps * participation
        adjustment = price * total_bps / Decimal("10000")
        return price + adjustment if side is Side.BUY else price - adjustment


class PaperBroker(BrokerAdapter):
    """A deterministic simulated venue driven by candles or quotes."""

    name = "paper"
    venue = "paper"

    def __init__(self, *, clock: Clock | None = None,
                 instruments: Mapping[str, Instrument] | None = None,
                 starting_cash: Decimal | float | str = 100_000,
                 currency: str = "USD",
                 fee_model: FeeModel | None = None,
                 slippage_model: SlippageModel | None = None,
                 partial_fill_ratio: Decimal | None = None,
                 reject_order_ids: Sequence[str] = (),
                 capabilities: BrokerCapabilities | None = None):
        self.clock = clock or default_clock()
        self._instruments = dict(instruments or {})
        self._cash = to_decimal(starting_cash, field_name="starting_cash")
        self._starting_cash = self._cash
        self.currency = currency
        self.fees = fee_model or FeeModel()
        self.slippage = slippage_model or SlippageModel()
        #: When set, each fill event executes only this fraction of the
        #: remainder, so partial-fill handling is exercised rather than assumed.
        self.partial_fill_ratio = (to_decimal(partial_fill_ratio,
                                              field_name="partial_fill_ratio")
                                   if partial_fill_ratio is not None else None)
        self._reject = set(reject_order_ids)
        self._capabilities = capabilities or BrokerCapabilities(
            order_types=frozenset({OrderType.MARKET, OrderType.LIMIT,
                                   OrderType.STOP, OrderType.STOP_LIMIT}),
            time_in_force=frozenset({TimeInForce.DAY, TimeInForce.GTC,
                                     TimeInForce.IOC, TimeInForce.FOK}),
            supports_short=True, supports_replace=True,
            honours_client_order_id=True)

        self._connected = False
        self._outage = False
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._positions: dict[str, Position] = {}
        self._last_price: dict[str, Decimal] = {}
        #: Orders that triggered on the current bar and must fill on the NEXT
        #: one. This is where the no-look-ahead rule is enforced.
        self._pending_fill: list[str] = []
        #: client_order_id -> the moment the order was staged. A bar that opened
        #: before this cannot fill the order; see `on_candle`.
        self._staged_at: dict[str, datetime] = {}
        self._fill_seq = 0

    # -- capabilities / lifecycle -----------------------------------------

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._capabilities

    def connect(self, credentials: BrokerCredentials | None = None) -> None:
        # A paper venue needs no credentials; accepting the argument keeps the
        # adapter substitutable for a real one without special-casing.
        self._connected = True
        self._outage = False

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and not self._outage

    def simulate_outage(self, on: bool = True) -> None:
        """Force the venue offline so failure handling can be tested."""
        self._outage = bool(on)

    def force_desync(self, instrument_key: str, quantity: Decimal) -> None:
        """Move the venue's position without telling Olympus.

        Exists so reconciliation has something real to detect. A reconciler that
        has never seen a genuine break is untested.
        """
        pos = self._positions.get(instrument_key,
                                  Position(instrument_key=instrument_key))
        self._positions[instrument_key] = Position(
            instrument_key=instrument_key,
            quantity=pos.quantity + to_decimal(quantity, field_name="quantity"),
            average_price=pos.average_price or Decimal("1"))

    def register_instrument(self, instrument: Instrument) -> None:
        self._instruments[instrument.key] = instrument

    # -- account ----------------------------------------------------------

    def get_account(self) -> AccountSnapshot:
        self._require_connected()
        equity = self._cash
        for key, pos in self._positions.items():
            price = self._last_price.get(key)
            if price is not None:
                equity += pos.quantity * price
        return AccountSnapshot(ts=self.clock.now(), cash=self._cash,
                               equity=equity, currency=self.currency,
                               source=self.name)

    def get_positions(self) -> list[Position]:
        self._require_connected()
        return [p for p in self._positions.values() if not p.is_flat]

    # -- orders -----------------------------------------------------------

    def submit_order(self, order: Order) -> Order:
        self._require_connected()
        self._require_supported(order)

        existing = self._orders.get(order.client_order_id)
        if existing is not None:
            # Idempotency: the same id is the SAME order, not a second one.
            # This is what makes a retry after a timeout safe.
            return existing

        if order.client_order_id in self._reject:
            rejected = order.evolve(status=OrderStatus.REJECTED,
                                    reject_reason="simulated rejection",
                                    updated_at=self.clock.now())
            self._orders[order.client_order_id] = rejected
            return rejected

        accepted = order.evolve(
            status=OrderStatus.NEW, updated_at=self.clock.now(),
            broker_order_id=f"PAPER-{_short_hash(order.client_order_id)}")
        self._orders[order.client_order_id] = accepted
        # Stage rather than fill, and remember WHEN it was staged. The timestamp
        # is what makes the no-look-ahead rule self-enforcing instead of relying
        # on the caller feeding bars in a particular order: a bar that opened
        # before the order existed can never fill it, however it is fed in.
        self._staged_at[order.client_order_id] = self.clock.now()
        self._pending_fill.append(order.client_order_id)
        return accepted

    def cancel_order(self, client_order_id: str) -> Order:
        self._require_connected()
        order = self._orders.get(client_order_id)
        if order is None:
            raise BrokerError("unknown order", client_order_id=client_order_id)
        if order.status.is_terminal:
            return order
        cancelled = order.evolve(status=OrderStatus.CANCELED,
                                 updated_at=self.clock.now())
        self._orders[client_order_id] = cancelled
        if client_order_id in self._pending_fill:
            self._pending_fill.remove(client_order_id)
        return cancelled

    def replace_order(self, client_order_id: str, *, quantity=None,
                      limit_price=None) -> Order:
        self._require_connected()
        order = self._orders.get(client_order_id)
        if order is None:
            raise BrokerError("unknown order", client_order_id=client_order_id)
        if order.status.is_terminal:
            raise BrokerError("cannot replace a terminal order",
                              client_order_id=client_order_id,
                              status=order.status.value)
        changes: dict[str, Any] = {"updated_at": self.clock.now()}
        if quantity is not None:
            new_qty = to_decimal(quantity, field_name="quantity")
            if new_qty < order.filled_quantity:
                raise BrokerError("cannot reduce below the filled quantity",
                                  client_order_id=client_order_id)
            changes["quantity"] = new_qty
        if limit_price is not None:
            changes["limit_price"] = to_decimal(limit_price, field_name="limit_price")
        updated = order.evolve(**changes)
        self._orders[client_order_id] = updated
        return updated

    def get_order(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    def get_open_orders(self) -> list[Order]:
        self._require_connected()
        return [o for o in self._orders.values() if o.is_open]

    def get_fills(self, since: datetime | None = None) -> list[Fill]:
        self._require_connected()
        if since is None:
            return list(self._fills)
        return [f for f in self._fills if f.ts >= since]

    # -- market driving ---------------------------------------------------

    def on_candle(self, candle: Candle) -> list[Fill]:
        """Advance the venue by one bar and return any fills it produced.

        Orders staged on the previous bar are evaluated against *this* bar, so
        the earliest possible fill is one bar after the decision.
        """
        self._last_price[candle.instrument_key] = to_decimal(
            candle.close, field_name="close")

        due, self._pending_fill = self._pending_fill, []
        produced: list[Fill] = []
        for coid in due:
            order = self._orders.get(coid)
            if order is None or not order.is_open:
                continue
            if order.instrument_key != candle.instrument_key:
                self._pending_fill.append(coid)     # not this instrument's bar
                continue
            staged = self._staged_at.get(coid)
            if staged is not None and candle.ts_open < staged:
                # This bar was already open when the order was created, so its
                # prices were knowable to whoever decided to send it. Filling
                # here is look-ahead; wait for a bar that opens after the order.
                self._pending_fill.append(coid)
                continue
            fill = self._try_fill(order, candle)
            if fill is None:
                if order.time_in_force in (TimeInForce.IOC, TimeInForce.FOK):
                    self._orders[coid] = order.evolve(
                        status=OrderStatus.EXPIRED, updated_at=self.clock.now())
                else:
                    self._pending_fill.append(coid)   # rest until it can fill
                continue
            produced.append(fill)
        return produced

    def _try_fill(self, order: Order, candle: Candle) -> Fill | None:
        high = to_decimal(candle.high, field_name="high")
        low = to_decimal(candle.low, field_name="low")
        open_ = to_decimal(candle.open, field_name="open")

        if order.order_type is OrderType.MARKET:
            price = open_
        elif order.order_type is OrderType.LIMIT:
            limit = order.limit_price
            if order.side is Side.BUY:
                if low > limit:
                    return None                     # never traded that low
                price = min(open_, limit)
            else:
                if high < limit:
                    return None
                price = max(open_, limit)
        elif order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            stop = order.stop_price
            triggered = (high >= stop) if order.side is Side.BUY else (low <= stop)
            if not triggered:
                return None
            price = stop if order.order_type is OrderType.STOP else order.limit_price
        else:                                        # pragma: no cover
            return None

        remaining = order.remaining_quantity
        quantity = remaining
        if self.partial_fill_ratio is not None:
            instrument = self._instruments.get(order.instrument_key)
            quantity = remaining * self.partial_fill_ratio
            if instrument is not None:
                quantity = instrument.quantize_quantity(quantity)
            if quantity <= 0:
                quantity = remaining                 # never stall on a rounding
        if quantity <= 0:
            return None

        fill_price = self.slippage.apply(price, order.side)
        instrument = self._instruments.get(order.instrument_key)
        if instrument is not None:
            fill_price = instrument.quantize_price(fill_price)
        fee = self.fees.fee_for(quantity, fill_price)

        self._fill_seq += 1
        fill = Fill(fill_id=f"pf-{self._fill_seq}-{_short_hash(order.client_order_id)}",
                    client_order_id=order.client_order_id,
                    instrument_key=order.instrument_key, side=order.side,
                    quantity=quantity, price=fill_price, ts=candle.ts_close,
                    fee=fee, fee_currency=self.currency, liquidity="taker")
        self._apply(order, fill)
        return fill

    def _apply(self, order: Order, fill: Fill) -> None:
        self._fills.append(fill)
        filled = order.filled_quantity + fill.quantity
        notional = sum((f.quantity * f.price for f in self._fills
                        if f.client_order_id == order.client_order_id),
                       Decimal("0"))
        avg = notional / filled if filled > 0 else None
        status = (OrderStatus.FILLED if filled >= order.quantity
                  else OrderStatus.PARTIALLY_FILLED)
        self._orders[order.client_order_id] = order.evolve(
            filled_quantity=filled, average_fill_price=avg, status=status,
            fees=order.fees + fill.fee, updated_at=fill.ts)
        if status is OrderStatus.PARTIALLY_FILLED:
            self._pending_fill.append(order.client_order_id)

        signed = fill.quantity * fill.side.sign
        self._cash -= signed * fill.price
        self._cash -= fill.fee
        pos = self._positions.get(fill.instrument_key,
                                  Position(instrument_key=fill.instrument_key))
        new_qty = pos.quantity + signed
        if pos.quantity == 0 or (pos.quantity > 0) == (signed > 0):
            total = pos.average_price * abs(pos.quantity) + fill.price * abs(signed)
            avg_px = total / abs(new_qty) if new_qty != 0 else Decimal("0")
        elif new_qty == 0:
            avg_px = Decimal("0")
        elif (new_qty > 0) == (pos.quantity > 0):
            avg_px = pos.average_price
        else:
            avg_px = fill.price
        self._positions[fill.instrument_key] = Position(
            instrument_key=fill.instrument_key, quantity=new_qty,
            average_price=avg_px, updated_at=fill.ts)

    def on_quote(self, quote: Quote) -> None:
        self._last_price[quote.instrument_key] = to_decimal(
            quote.mid, field_name="mid")

    def get_quote(self, instrument_key: str) -> Quote | None:
        price = self._last_price.get(instrument_key)
        if price is None:
            return None
        half = price * Decimal("0.0001")
        return Quote(instrument_key=instrument_key, ts=self.clock.now(),
                     bid=float(price - half), ask=float(price + half),
                     source=self.name)


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


__all__ = ["PaperBroker", "FeeModel", "SlippageModel"]
