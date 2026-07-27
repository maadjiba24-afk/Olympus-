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
fees (maker/taker), slippage (fixed, spread-proportional, participation-scaled),
partial fills, random-but-seeded rejections, submit timeouts, and outages. A
simulator that always fills completely at the mid, for free, will make almost
any strategy look profitable.

Determinism
-----------
The only stochastic behaviour (`reject_probability`) is driven by an explicitly
seeded `random.Random`; constructing the broker with a probability but no seed
is a `ConfigurationError`. An unreproducible backtest is not evidence.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    silent thumb on the scale, and someone will forget to override it.

    All four venue conventions are here because mixing them up changes the
    answer by more than most strategies' edge: per-share (US equities),
    basis points of notional (crypto), a fixed ticket charge (some FX/CFD
    venues), and a floor. `maker_bps` / `taker_bps`, when set, *replace* `bps`
    for the matching liquidity flag, which is what makes a maker-rebate venue
    representable (a negative `maker_bps` is a rebate).
    """
    per_share: Decimal = Decimal("0")
    bps: Decimal = Decimal("1")          # 1bp of notional
    fixed: Decimal = Decimal("0")
    minimum: Decimal = Decimal("0")
    maker_bps: Decimal | None = None
    taker_bps: Decimal | None = None

    def __post_init__(self):
        for name in ("per_share", "bps", "fixed", "minimum"):
            object.__setattr__(self, name,
                               to_decimal(getattr(self, name), field_name=name))
        for name in ("maker_bps", "taker_bps"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, to_decimal(value, field_name=name))
        if self.minimum < 0:
            raise ConfigurationError("fee minimum must be >= 0")

    def bps_for(self, liquidity: str) -> Decimal:
        if liquidity == "maker" and self.maker_bps is not None:
            return self.maker_bps
        if liquidity == "taker" and self.taker_bps is not None:
            return self.taker_bps
        return self.bps

    def fee_for(self, quantity: Decimal, price: Decimal,
                liquidity: str = "taker") -> Decimal:
        notional = abs(quantity * price)
        fee = (self.fixed + abs(quantity) * self.per_share
               + notional * self.bps_for(liquidity) / Decimal("10000"))
        # A rebate may make the fee negative; the floor is still respected, and
        # `Fill.fee` refuses negatives, so a rebate venue must set `minimum`
        # explicitly rather than silently producing an invalid fill.
        return max(fee, self.minimum)


@dataclass(frozen=True)
class SlippageModel:
    """Adverse price movement between decision and fill.

    Always against the trader — buys fill higher, sells lower. Modelling it
    symmetrically (or not at all) is equivalent to assuming free liquidity.

    Three additive components, because real cost has three causes:

    * `bps` — a flat, always-present cost of crossing.
    * `spread_fraction` — the share of the quoted spread paid. Crossing a wide
      book costs more than crossing a tight one; a fixed bps model prices a
      liquid megacap and an illiquid microcap identically, which is why
      backtests of small names are the ones that never survive contact.
    * `participation_impact_bps` — market impact scaled by the fraction of the
      bar's volume consumed. This is what stops a backtest from "buying" a
      day's entire volume at the open with no consequence.
    """
    bps: Decimal = Decimal("2")
    #: Extra slippage as a fraction of the bar's volume consumed.
    participation_impact_bps: Decimal = Decimal("0")
    #: Fraction of the observed spread paid on top (0.5 == half-spread).
    spread_fraction: Decimal = Decimal("0")

    def __post_init__(self):
        for name in ("bps", "participation_impact_bps", "spread_fraction"):
            object.__setattr__(self, name,
                               to_decimal(getattr(self, name), field_name=name))
        if self.spread_fraction < 0:
            raise ConfigurationError("spread_fraction must be >= 0")

    def apply(self, price: Decimal, side: Side, *,
              participation: Decimal = Decimal("0"),
              spread: Decimal = Decimal("0")) -> Decimal:
        total_bps = self.bps + self.participation_impact_bps * participation
        adjustment = price * total_bps / Decimal("10000")
        adjustment += abs(spread) * self.spread_fraction
        moved = price + adjustment if side is Side.BUY else price - adjustment
        # A slippage model must never invent a non-positive price: `Fill`
        # rejects those, and a venue that fills at zero is not conservative,
        # it is broken.
        if moved <= 0:
            return price
        return moved


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
                 reject_probability: float = 0.0,
                 seed: int | None = None,
                 latency_ms: int = 0,
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
        # Randomness is opt-in AND must be seeded. An unseeded stochastic
        # simulator produces a different equity curve on every run, so a
        # "profitable" backtest cannot be distinguished from a lucky one.
        self.reject_probability = float(reject_probability)
        if not 0.0 <= self.reject_probability <= 1.0:
            raise ConfigurationError("reject_probability must be within [0, 1]",
                                     got=self.reject_probability)
        if self.reject_probability > 0 and seed is None:
            raise ConfigurationError(
                "reject_probability requires an explicit seed; an "
                "unreproducible simulation is not evidence")
        self.seed = seed
        self._random = random.Random(seed)
        #: Simulated round-trip delay. An order cannot fill against a bar that
        #: opened before the acknowledgement would have arrived — a strategy
        #: that only works at zero latency is a strategy that does not work.
        self.latency_ms = int(latency_ms)
        if self.latency_ms < 0:
            raise ConfigurationError("latency_ms must be >= 0")
        #: Pending simulated submit timeouts (see `simulate_timeout`).
        self._timeouts = 0
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
        #: Bars the venue has been driven with, so `get_candles` can answer the
        #: market-data half of the adapter contract from the same source of
        #: truth that produced the fills.
        self._candles: dict[str, list[Candle]] = {}
        self._quotes: dict[str, Quote] = {}
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

    def simulate_timeout(self, count: int = 1) -> None:
        """Make the next `count` submits *accept the order and then time out*.

        This is the nastiest real failure mode and the reason idempotency
        exists: the venue has the order, the caller has an exception, and a
        naive retry double-trades. Simulating a timeout that loses the *reply*
        rather than the order is what makes the retry path honestly testable.
        """
        if count < 0:
            raise ConfigurationError("timeout count must be >= 0")
        self._timeouts = int(count)

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
            #
            # But only when the terms match. Reusing an id with a *different*
            # quantity, side, or price is not a retry, it is a bug (or a
            # collision in the id derivation), and silently returning the old
            # order would execute something nobody asked for. A real venue
            # rejects that; so do we.
            conflict = self._conflict(existing, order)
            if conflict:
                raise DuplicateOrderError(
                    "client_order_id already exists with different terms",
                    client_order_id=order.client_order_id, field=conflict)
            return existing

        # Deterministic given the seed: the Nth submit of a run always draws
        # the same number, so a rejection-heavy scenario replays identically.
        random_reject = (self.reject_probability > 0
                         and self._random.random() < self.reject_probability)
        if order.client_order_id in self._reject or random_reject:
            reason = ("simulated rejection" if not random_reject
                      else "simulated random rejection")
            rejected = order.evolve(status=OrderStatus.REJECTED,
                                    reject_reason=reason,
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
        # Latency pushes that boundary further out: the order is not live until
        # the acknowledgement would have arrived.
        self._staged_at[order.client_order_id] = (
            self.clock.now() + timedelta(milliseconds=self.latency_ms))
        self._pending_fill.append(order.client_order_id)

        if self._timeouts > 0:
            # The order IS live — only the reply is lost. Raising after
            # recording it is the whole point: a caller that retries must get
            # the same order back, not a second one.
            self._timeouts -= 1
            raise BrokerUnavailable("timed out waiting for acknowledgement",
                                    client_order_id=order.client_order_id,
                                    venue=self.venue)
        return accepted

    @staticmethod
    def _conflict(existing: Order, incoming: Order) -> str:
        """Name the first field on which a resubmission disagrees, or ""."""
        for name in ("instrument_key", "side", "order_type", "quantity",
                     "limit_price", "stop_price"):
            if getattr(existing, name) != getattr(incoming, name):
                return name
        return ""

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
        self._candles.setdefault(candle.instrument_key, []).append(candle)

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
        limit = order.limit_price
        liquidity = "taker"

        if order.order_type is OrderType.MARKET:
            price = open_
        elif order.order_type is OrderType.LIMIT:
            price = self._limit_price(order.side, limit, open_, high, low)
            if price is None:
                return None
            # Filling *at* the limit means the order rested and someone else
            # crossed to it — maker. Filling at a better open means we were the
            # aggressor into a gap — taker. The distinction is worth keeping
            # because it is the whole difference on a rebate venue.
            liquidity = "maker" if price == limit else "taker"
        elif order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            stop = order.stop_price
            triggered = (high >= stop) if order.side is Side.BUY else (low <= stop)
            if not triggered:
                return None
            if order.order_type is OrderType.STOP:
                # Gap realism: if the bar opened through the stop, the stop
                # became a market order at the OPEN, not at the stop price.
                # Assuming otherwise is how backtests pretend stop-losses cap
                # losses exactly — the single most expensive fiction in retail
                # backtesting.
                price = max(open_, stop) if order.side is Side.BUY else min(open_, stop)
            else:
                # A triggered stop-limit is just a limit order; it can trigger
                # and still not fill, which is precisely its risk.
                price = self._limit_price(order.side, limit, open_, high, low)
                if price is None:
                    return None
                liquidity = "maker" if price == limit else "taker"
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

        # Market impact scales with how much of the bar we are trying to eat.
        # Volume of zero (a synthetic or illiquid bar) is treated as full
        # participation rather than none: no evidence of liquidity is not
        # evidence of liquidity.
        volume = to_decimal(candle.volume, field_name="volume")
        participation = (min(Decimal("1"), quantity / volume)
                         if volume > 0 else Decimal("1"))
        fill_price = self.slippage.apply(price, order.side,
                                         participation=participation)
        instrument = self._instruments.get(order.instrument_key)
        if instrument is not None:
            fill_price = instrument.quantize_price(fill_price)
        if limit is not None and order.order_type in (OrderType.LIMIT,
                                                      OrderType.STOP_LIMIT):
            # A limit order can fail to fill, but it can never fill worse than
            # its limit. Slippage on a limit fill would be a fiction in the
            # pessimistic direction, which is still a fiction.
            fill_price = (min(fill_price, limit) if order.side is Side.BUY
                          else max(fill_price, limit))
        if fill_price <= 0:                          # pragma: no cover - guard
            return None
        fee = self.fees.fee_for(quantity, fill_price, liquidity)

        self._fill_seq += 1
        fill = Fill(fill_id=f"pf-{self._fill_seq}-{_short_hash(order.client_order_id)}",
                    client_order_id=order.client_order_id,
                    instrument_key=order.instrument_key, side=order.side,
                    quantity=quantity, price=fill_price, ts=candle.ts_close,
                    fee=fee, fee_currency=self.currency, liquidity=liquidity)
        self._apply(order, fill)
        return fill

    @staticmethod
    def _limit_price(side: Side, limit: Decimal, open_: Decimal,
                     high: Decimal, low: Decimal) -> Decimal | None:
        """Price a limit order against one bar, or None if it never traded through.

        The bar's range is the only evidence of what was tradable. If the bar
        opened through the limit the fill is at the open (better than asked);
        otherwise it is at the limit exactly.
        """
        if side is Side.BUY:
            if low > limit:
                return None                          # never traded that low
            return min(open_, limit)
        if high < limit:
            return None
        return max(open_, limit)

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
        realised = Decimal("0")
        if pos.quantity == 0 or (pos.quantity > 0) == (signed > 0):
            total = pos.average_price * abs(pos.quantity) + fill.price * abs(signed)
            avg_px = total / abs(new_qty) if new_qty != 0 else Decimal("0")
        else:
            # Reducing or flipping: the overlapping quantity is closed out, and
            # THAT is where realised P&L comes from. Tracking it on the venue's
            # own book (rather than only in Olympus's portfolio) is what lets
            # reconciliation compare two independently-derived numbers instead
            # of one number with itself.
            closed = min(abs(signed), abs(pos.quantity))
            direction = Decimal("1") if pos.quantity > 0 else Decimal("-1")
            realised = (fill.price - pos.average_price) * closed * direction
            if new_qty == 0:
                avg_px = Decimal("0")
            elif (new_qty > 0) == (pos.quantity > 0):
                avg_px = pos.average_price          # partial reduction
            else:
                avg_px = fill.price                 # flipped through flat
        self._positions[fill.instrument_key] = Position(
            instrument_key=fill.instrument_key, quantity=new_qty,
            average_price=avg_px,
            realised_pnl=pos.realised_pnl + realised,
            fees_paid=pos.fees_paid + fill.fee,
            opened_at=pos.opened_at or fill.ts, updated_at=fill.ts)

    def on_quote(self, quote: Quote) -> list[Fill]:
        """Drive the venue from top-of-book instead of bars.

        Paper trading against live quotes takes this path; the backtester takes
        `on_candle`. The same anti-look-ahead rule applies — a quote stamped
        before the order was acknowledged cannot fill it — and marketable
        orders cross the spread (buy at the ask, sell at the bid) rather than
        trading at the mid, because filling at the mid is a free half-spread
        that nobody actually gets.
        """
        self._last_price[quote.instrument_key] = to_decimal(
            quote.mid, field_name="mid")
        self._quotes[quote.instrument_key] = quote

        due, self._pending_fill = self._pending_fill, []
        produced: list[Fill] = []
        for coid in due:
            order = self._orders.get(coid)
            if order is None or not order.is_open:
                continue
            staged = self._staged_at.get(coid)
            if (order.instrument_key != quote.instrument_key
                    or (staged is not None and quote.ts < staged)):
                self._pending_fill.append(coid)
                continue
            fill = self._try_fill_quote(order, quote)
            if fill is None:
                self._pending_fill.append(coid)
                continue
            produced.append(fill)
        return produced

    def _try_fill_quote(self, order: Order, quote: Quote) -> Fill | None:
        ask = to_decimal(quote.ask, field_name="ask")
        bid = to_decimal(quote.bid, field_name="bid")
        touch = ask if order.side is Side.BUY else bid
        limit = order.limit_price
        liquidity = "taker"

        if order.order_type is OrderType.MARKET:
            price = touch
        elif order.order_type is OrderType.LIMIT:
            marketable = (touch <= limit) if order.side is Side.BUY else (touch >= limit)
            if not marketable:
                return None
            price = touch
        elif order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            stop = order.stop_price
            triggered = (touch >= stop) if order.side is Side.BUY else (touch <= stop)
            if not triggered:
                return None
            if order.order_type is OrderType.STOP_LIMIT:
                marketable = ((touch <= limit) if order.side is Side.BUY
                              else (touch >= limit))
                if not marketable:
                    return None
            price = touch
        else:                                        # pragma: no cover
            return None

        quantity = order.remaining_quantity
        if self.partial_fill_ratio is not None:
            instrument = self._instruments.get(order.instrument_key)
            partial = quantity * self.partial_fill_ratio
            if instrument is not None:
                partial = instrument.quantize_quantity(partial)
            quantity = partial if partial > 0 else quantity
        if quantity <= 0:
            return None

        spread = to_decimal(quote.spread, field_name="spread")
        fill_price = self.slippage.apply(price, order.side, spread=spread)
        instrument = self._instruments.get(order.instrument_key)
        if instrument is not None:
            fill_price = instrument.quantize_price(fill_price)
        if limit is not None and order.order_type in (OrderType.LIMIT,
                                                      OrderType.STOP_LIMIT):
            fill_price = (min(fill_price, limit) if order.side is Side.BUY
                          else max(fill_price, limit))
        if fill_price <= 0:                          # pragma: no cover - guard
            return None
        fee = self.fees.fee_for(quantity, fill_price, liquidity)

        self._fill_seq += 1
        fill = Fill(fill_id=f"pq-{self._fill_seq}-{_short_hash(order.client_order_id)}",
                    client_order_id=order.client_order_id,
                    instrument_key=order.instrument_key, side=order.side,
                    quantity=quantity, price=fill_price, ts=quote.ts,
                    fee=fee, fee_currency=self.currency, liquidity=liquidity)
        self._apply(order, fill)
        return fill

    # -- market data ------------------------------------------------------

    def get_quote(self, instrument_key: str) -> Quote | None:
        quote = self._quotes.get(instrument_key)
        if quote is not None:
            return quote
        price = self._last_price.get(instrument_key)
        if price is None:
            return None
        # No real book: synthesise a symmetric 1bp-wide one around the last
        # trade so callers get a *shaped* quote rather than a fake tight one.
        half = price * Decimal("0.0001")
        return Quote(instrument_key=instrument_key, ts=self.clock.now(),
                     bid=float(price - half), ask=float(price + half),
                     source=self.name)

    def get_candles(self, instrument_key: str, timeframe: str,
                    start: datetime | None = None,
                    end: datetime | None = None) -> list[Candle]:
        """Replay the bars this venue was driven with.

        A simulated venue cannot invent history it was never given; returning
        only what it saw keeps a backtest from quietly acquiring data the live
        path would not have had.
        """
        bars = [c for c in self._candles.get(instrument_key, [])
                if c.timeframe == timeframe]
        if start is not None:
            bars = [c for c in bars if c.ts_open >= start]
        if end is not None:
            bars = [c for c in bars if c.ts_close <= end]
        return bars


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


__all__ = ["PaperBroker", "FeeModel", "SlippageModel"]
