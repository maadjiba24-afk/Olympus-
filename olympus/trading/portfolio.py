"""Position and P&L accounting.

All arithmetic here is `Decimal`. Not stylistic: a portfolio kept in binary
floats accumulates error that shows up as a reconciliation break against the
broker weeks later, at which point nobody can tell whether it is a rounding
artefact or a real missing fill. Exactness makes "local and broker disagree"
mean something.

The one genuinely hard piece is a fill that **crosses through zero** — long 10,
sell 15, leaving short 5. The correct treatment is:

    * realise P&L on the 10 that closed, at the fill price
    * open the remaining 5 at the fill price, with a *fresh* cost basis

Getting this wrong (carrying the old average across the flip) silently corrupts
every subsequent P&L number for that instrument, and it is the single most
common accounting bug in hand-rolled trading systems. It is implemented exactly
once — in the pure function `apply_fill_to_position` — and tested in both
directions. `PortfolioManager` and the per-strategy attribution books both call
that one function, so there is no second copy of the rule to drift out of sync.

Cost basis is weighted-average, not FIFO/LIFO. Weighted-average is what most
brokers report for a cash account and is path-independent, which means a
reconciliation mismatch points at a missing fill rather than at a lot-matching
policy disagreement. The choice is documented here because changing it later
would silently reprice history.

The four rules, stated once
---------------------------
Let `q` be the signed position before the fill and `d` the signed fill delta.

1. `q == 0`             → open at the fill price.
2. `sign(d) == sign(q)` → weighted average: (avg·|q| + px·|d|) / |q + d|.
3. `|d| <= |q|`         → reduce: realise (px − avg)·min(|q|,|d|)·sign(q)·multiplier,
                          and *keep* the old average on the remainder. A partial
                          close does not reprice what is still open.
4. `|d| > |q|`          → cross through zero: rule 3 on the |q| that closed, then
                          rule 1 on the |d| − |q| that remains.

Realised P&L is reported both gross and net of fees (`realised_pnl` /
`realised_pnl_net`). Gross is what the price movement earned; net is what the
account actually kept. Reporting only one of them is how a strategy that is
profitable before costs and loss-making after costs stays undetected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import Clock, default_clock
from .contracts import (Fill, Instrument, PortfolioSnapshot, Position, Side,
                        ensure_utc, jsonable, to_decimal)
from .errors import TradingError

_NS = "trading.portfolio"

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True)
class TradeRecord:
    """A closed round trip, produced when a position reduces or flips.

    Recorded at *close* time rather than open time because that is when P&L
    becomes a fact; the backtester's trade statistics and the per-regime
    attribution both key off this.
    """
    instrument_key: str
    strategy_id: str
    side: Side                      # the side of the CLOSING fill
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime | None
    closed_at: datetime
    gross_pnl: Decimal
    fees: Decimal

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.fees

    def to_dict(self) -> dict:
        data = {f.name: jsonable(getattr(self, f.name)) for f in fields(self)}
        data["net_pnl"] = jsonable(self.net_pnl)
        return data


@dataclass(frozen=True)
class FillEffect:
    """What one fill did to one position — the output of the cost-basis rules.

    Returned as a value rather than applied in place so the same computation can
    drive the account book, the per-strategy book, and a what-if without any of
    them re-deriving the arithmetic.
    """
    position: Position
    #: Realised P&L on the closed quantity, BEFORE the fill's fee.
    gross_realised: Decimal
    #: How much of the prior position this fill closed (0 when opening/adding).
    closed_quantity: Decimal
    #: The cost basis that was closed — the entry price of the round trip.
    closed_basis: Decimal
    #: True when the fill took the position through zero to the other side.
    crossed_zero: bool


def apply_fill_to_position(position: Position, fill: Fill, *,
                           multiplier: Decimal = _ONE) -> FillEffect:
    """Apply `fill` to `position` under weighted-average cost. Pure.

    Pure and free of cash/fee bookkeeping on purpose: the zero-crossing rule is
    the part that must be provably right, so it is isolated from everything that
    merely has to be added up. `multiplier` scales P&L only — `average_price`
    stays a per-unit price, which is what a broker statement shows and what a
    reconciliation compares against.
    """
    old_qty = position.quantity
    delta = fill.quantity * fill.side.sign
    new_qty = old_qty + delta
    price = fill.price

    gross = _ZERO
    closed = _ZERO
    closed_basis = position.average_price
    crossed = False
    opened_at = position.opened_at

    if old_qty == 0:
        # Rule 1 — opening.
        avg = price
        opened_at = fill.ts
    elif (old_qty > 0) == (delta > 0):
        # Rule 2 — adding to the same side; weighted-average the basis.
        total_cost = position.average_price * abs(old_qty) + price * abs(delta)
        avg = total_cost / abs(new_qty)
    else:
        # Rules 3 and 4 — reducing, closing, or flipping. Only the quantity
        # that actually closed realises P&L, at the OLD basis.
        closed = min(abs(old_qty), abs(delta))
        direction = _ONE if old_qty > 0 else -_ONE
        gross = (price - position.average_price) * closed * direction * multiplier
        if new_qty == 0:
            avg = _ZERO
            opened_at = None
        elif (new_qty > 0) == (old_qty > 0):
            # Partial close: the remainder keeps its original basis. Repricing
            # it here would book the same P&L twice, once now and once later.
            avg = position.average_price
        else:
            # Crossed through zero. The remainder is a NEW position opened at
            # the fill price with a fresh clock; carrying the old basis across
            # the flip is the classic silent-corruption bug this branch exists
            # to prevent.
            avg = price
            opened_at = fill.ts
            crossed = True

    updated = Position(
        instrument_key=position.instrument_key or fill.instrument_key,
        quantity=new_qty, average_price=avg,
        realised_pnl=position.realised_pnl + gross,
        fees_paid=position.fees_paid + fill.fee,
        opened_at=opened_at, updated_at=fill.ts)
    return FillEffect(position=updated, gross_realised=gross,
                      closed_quantity=closed, closed_basis=closed_basis,
                      crossed_zero=crossed)


class PortfolioManager:
    """Olympus's own view of positions, cash, and P&L.

    Deliberately *not* the broker's view. Keeping an independent account is what
    makes reconciliation meaningful — if this simply mirrored the broker there
    would be nothing to compare and a silent fill loss would be invisible.
    """

    def __init__(self, *, starting_cash: Decimal | float | str = 0,
                 currency: str = "USD", clock: Clock | None = None,
                 account_id: str = "default", namespace: str = _NS,
                 store_ns: str | None = None):
        self.clock = clock or default_clock()
        self.currency = currency
        self.account_id = account_id
        #: `store_ns` is the spelling used by the composition root; `namespace`
        #: is kept because existing call sites pass it.
        self.namespace = store_ns or namespace
        self._cash = to_decimal(starting_cash, field_name="starting_cash")
        self._starting_cash = self._cash
        self._positions: dict[str, Position] = {}
        self._marks: dict[str, float] = {}
        self._realised = _ZERO
        self._fees = _ZERO
        self._trades: list[TradeRecord] = []
        self._equity_curve: list[tuple[datetime, Decimal]] = []
        self._peak_equity = self._cash
        self._applied_fills: set[str] = set()
        # Per-strategy attribution keeps a *parallel book*: the same cost-basis
        # rules applied to that strategy's fills only. Deriving attribution from
        # the account book instead would be impossible the moment two strategies
        # trade the same instrument — their P&L would be inseparable.
        self._strategy_books: dict[str, dict[str, Position]] = {}
        self._strategy_stats: dict[str, dict[str, Decimal]] = {}

    # -- state ------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def starting_cash(self) -> Decimal:
        return self._starting_cash

    @property
    def realised_pnl(self) -> Decimal:
        """Gross realised P&L — price movement only, before costs."""
        return self._realised

    @property
    def realised_pnl_gross(self) -> Decimal:
        return self._realised

    @property
    def realised_pnl_net(self) -> Decimal:
        """Realised P&L after every fee the account has paid."""
        return self._realised - self._fees

    @property
    def fees_paid(self) -> Decimal:
        return self._fees

    @property
    def trades(self) -> list[TradeRecord]:
        return list(self._trades)

    def position(self, instrument_key: str) -> Position:
        return self._positions.get(
            instrument_key, Position(instrument_key=instrument_key))

    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def open_positions(self) -> dict[str, Position]:
        return {k: p for k, p in self._positions.items() if not p.is_flat}

    # -- marking ----------------------------------------------------------

    def _set_mark(self, instrument_key: str, price: float) -> None:
        price = float(price)
        if price <= 0:
            raise TradingError("mark price must be > 0",
                               instrument=instrument_key, got=price)
        self._marks[instrument_key] = price

    def mark(self, instrument_key: str, price: float) -> None:
        self._set_mark(instrument_key, price)
        self._update_peak()

    def mark_all(self, marks: Mapping[str, float]) -> None:
        """Mark several instruments, then revalue once.

        One revaluation rather than one per instrument: the intermediate states
        of a multi-leg mark are not real portfolios, and letting them set the
        equity peak would invent drawdowns out of update ordering.
        """
        for key, price in marks.items():
            self._set_mark(key, price)
        self._update_peak()

    def snapshot(self, ts: datetime | None = None) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            ts=ts or self.clock.now(), cash=self._cash,
            positions=dict(self._positions), marks=dict(self._marks),
            currency=self.currency, realised_pnl=self._realised,
            fees_paid=self._fees)

    # -- the core --------------------------------------------------------

    def apply_fill(self, fill: Fill, instrument: Instrument | None = None, *,
                   strategy_id: str = "") -> Position:
        """Apply an execution. Idempotent on `fill_id`.

        Idempotency matters more than it looks: fills arrive from polling, from
        websockets, and from post-restart recovery, and the same fill routinely
        arrives twice. Applying it twice doubles a position silently.
        """
        if fill.fill_id in self._applied_fills:
            return self.position(fill.instrument_key)
        self._applied_fills.add(fill.fill_id)

        key = fill.instrument_key
        multiplier = instrument.multiplier if instrument is not None else _ONE
        current = self._positions.get(key, Position(instrument_key=key))
        effect = apply_fill_to_position(current, fill, multiplier=multiplier)

        # Cash: buying spends, selling receives; fees always reduce, on both
        # sides, and are never netted into the basis (that would hide them).
        signed = fill.quantity * fill.side.sign
        self._cash -= signed * fill.price * multiplier
        self._cash -= fill.fee
        self._fees += fill.fee
        self._realised += effect.gross_realised
        self._positions[key] = effect.position

        if effect.closed_quantity > 0:
            self._trades.append(TradeRecord(
                instrument_key=key, strategy_id=strategy_id, side=fill.side,
                quantity=effect.closed_quantity,
                entry_price=effect.closed_basis, exit_price=fill.price,
                opened_at=current.opened_at, closed_at=fill.ts,
                gross_pnl=effect.gross_realised, fees=fill.fee))

        if strategy_id:
            self._attribute(strategy_id, fill, multiplier)

        # A position with no mark contributes nothing to exposure, which would
        # understate equity; seed the mark from the fill so equity is never
        # wrong merely because no quote has arrived yet.
        self._marks.setdefault(key, float(fill.price))
        self._record_equity(fill.ts)
        return effect.position

    def _attribute(self, strategy_id: str, fill: Fill,
                   multiplier: Decimal) -> None:
        book = self._strategy_books.setdefault(strategy_id, {})
        key = fill.instrument_key
        current = book.get(key, Position(instrument_key=key))
        effect = apply_fill_to_position(current, fill, multiplier=multiplier)
        book[key] = effect.position
        stats = self._strategy_stats.setdefault(
            strategy_id, {"realised": _ZERO, "fees": _ZERO})
        stats["realised"] += effect.gross_realised
        stats["fees"] += fill.fee

    def apply_fills(self, fills: Iterable[Fill],
                    instruments: Mapping[str, Instrument] | None = None,
                    *, strategy_id: str = "") -> None:
        for fill in fills:
            inst = (instruments or {}).get(fill.instrument_key)
            self.apply_fill(fill, inst, strategy_id=strategy_id)

    # -- performance ------------------------------------------------------

    def equity(self, ts: datetime | None = None) -> Decimal:
        return self.snapshot(ts).equity

    def unrealised_pnl(self) -> Decimal:
        return self.snapshot().unrealised_pnl

    def _update_peak(self) -> Decimal:
        value = self.snapshot().equity
        if value > self._peak_equity:
            self._peak_equity = value
        return value

    def _record_equity(self, ts: datetime) -> None:
        ts = ensure_utc(ts, field_name="ts")
        value = self.snapshot(ts).equity
        self._equity_curve.append((ts, value))
        if value > self._peak_equity:
            self._peak_equity = value

    def record_equity_point(self, ts: datetime | None = None) -> Decimal:
        """Append a mark-driven point to the equity curve.

        Fills alone are not enough: a book that is held over a crash in prices
        has a real drawdown and no fills to record it. The backtester and the
        live monitor call this once per bar so the curve reflects marks, not
        just trades.
        """
        stamp = ensure_utc(ts or self.clock.now(), field_name="ts")
        value = self.snapshot(stamp).equity
        self._equity_curve.append((stamp, value))
        if value > self._peak_equity:
            self._peak_equity = value
        return value

    def equity_curve(self) -> list[tuple[datetime, Decimal]]:
        return list(self._equity_curve)

    @property
    def peak_equity(self) -> Decimal:
        return self._peak_equity

    def drawdown(self) -> Decimal:
        """Absolute drawdown from peak equity (a positive number)."""
        current = self.snapshot().equity
        return max(_ZERO, self._peak_equity - current)

    def drawdown_fraction(self) -> Decimal:
        if self._peak_equity <= 0:
            return _ZERO
        return self.drawdown() / self._peak_equity

    def opening_equity(self, day: date) -> Decimal | None:
        """The reference equity for `day`, or None if the book has no history.

        Defined as the first equity observation recorded on that UTC day, and —
        before any observation exists for it — the last observation of an
        earlier day. The second half is what makes the daily-loss limit work on
        a quiet morning: marks can move a book deep into loss before the day's
        first fill, and anchoring only on fills would report zero.
        """
        first_today: Decimal | None = None
        first_ts: datetime | None = None
        last_prior: Decimal | None = None
        prior_ts: datetime | None = None
        for ts, value in self._equity_curve:
            when = ts.date()
            if when == day:
                if first_ts is None or ts < first_ts:
                    first_ts, first_today = ts, value
            elif when < day:
                if prior_ts is None or ts > prior_ts:
                    prior_ts, last_prior = ts, value
        if first_today is not None:
            return first_today
        return last_prior

    def daily_pnl(self, as_of: datetime | None = None) -> Decimal:
        """P&L since the opening equity of the current UTC day.

        UTC-day boundaries rather than exchange sessions: the risk engine's
        daily-loss limit needs a definition that is unambiguous across venues,
        and a venue-specific one would differ per instrument in a mixed book.
        """
        now = ensure_utc(as_of or self.clock.now(), field_name="as_of")
        start = self.opening_equity(now.date())
        if start is None:
            return _ZERO
        return self.snapshot(now).equity - start

    def strategy_pnl(self, strategy_id: str) -> dict[str, Decimal]:
        """Realised, fees, net, and unrealised P&L attributed to one strategy."""
        stats = self._strategy_stats.get(strategy_id)
        realised = stats["realised"] if stats else _ZERO
        fees = stats["fees"] if stats else _ZERO
        unrealised = _ZERO
        for key, pos in self._strategy_books.get(strategy_id, {}).items():
            mark = self._marks.get(key)
            if mark is None or pos.is_flat:
                continue
            unrealised += pos.unrealised_pnl(mark)
        return {"realised": realised, "fees": fees,
                "net": realised - fees, "unrealised": unrealised}

    def strategy_positions(self, strategy_id: str) -> dict[str, Position]:
        return dict(self._strategy_books.get(strategy_id, {}))

    def strategy_ids(self) -> list[str]:
        return sorted(self._strategy_books)

    # -- persistence ------------------------------------------------------

    def _store(self):
        from olympus import store
        return store.backend()

    def _key(self) -> str:
        return self.account_id

    def save(self) -> None:
        """Write through. Called after every mutation by the execution engine.

        Uses `proclock` because a save is a read-modify-write from the caller's
        point of view: two processes flushing concurrently would otherwise
        interleave and lose one of the updates.
        """
        from olympus import proclock
        payload = {
            "cash": str(self._cash),
            "starting_cash": str(self._starting_cash),
            "currency": self.currency,
            "realised": str(self._realised),
            "fees": str(self._fees),
            "peak_equity": str(self._peak_equity),
            "positions": {k: p.to_dict() for k, p in self._positions.items()},
            "marks": dict(self._marks),
            "applied_fills": sorted(self._applied_fills),
            "equity_curve": [[ts.isoformat(), str(v)] for ts, v in self._equity_curve],
            "strategy_stats": {k: {kk: str(vv) for kk, vv in v.items()}
                               for k, v in self._strategy_stats.items()},
            "strategy_books": {
                sid: {k: p.to_dict() for k, p in book.items()}
                for sid, book in self._strategy_books.items()},
            "trades": [t.to_dict() for t in self._trades],
        }
        blob = json.dumps(payload, sort_keys=True).encode("utf-8")
        with proclock.lock(f"trading-portfolio-{self._key()}"):
            self._store().put(self.namespace, self._key(), blob)

    def load(self) -> bool:
        """Restore. Returns False when there is nothing stored.

        Restart recovery is a safety property, not a convenience: a portfolio
        that forgets its positions on restart will happily re-open them, and —
        worse — will re-apply fills it has already booked. `applied_fills` is
        restored for exactly that reason.
        """
        raw = self._store().get(self.namespace, self._key())
        if not raw:
            return False
        data = json.loads(raw.decode("utf-8"))
        self._cash = Decimal(data["cash"])
        self._starting_cash = Decimal(data.get("starting_cash", data["cash"]))
        self.currency = data.get("currency", self.currency)
        self._realised = Decimal(data.get("realised", "0"))
        self._fees = Decimal(data.get("fees", "0"))
        self._peak_equity = Decimal(data.get("peak_equity", data["cash"]))
        self._marks = {k: float(v) for k, v in (data.get("marks") or {}).items()}
        self._applied_fills = set(data.get("applied_fills") or [])
        self._positions = {k: _position_from_dict(v)
                           for k, v in (data.get("positions") or {}).items()}
        self._equity_curve = [
            (ensure_utc(datetime.fromisoformat(ts), field_name="ts"), Decimal(v))
            for ts, v in (data.get("equity_curve") or [])]
        self._strategy_stats = {
            k: {kk: Decimal(vv) for kk, vv in v.items()}
            for k, v in (data.get("strategy_stats")
                         or data.get("by_strategy") or {}).items()}
        self._strategy_books = {
            sid: {k: _position_from_dict(v) for k, v in book.items()}
            for sid, book in (data.get("strategy_books") or {}).items()}
        self._trades = [_trade_from_dict(t) for t in (data.get("trades") or [])]
        return True


def _position_from_dict(data: Mapping[str, Any]) -> Position:
    return Position(
        instrument_key=data["instrument_key"],
        quantity=Decimal(data["quantity"]),
        average_price=Decimal(data["average_price"]),
        realised_pnl=Decimal(data.get("realised_pnl", "0")),
        fees_paid=Decimal(data.get("fees_paid", "0")),
        opened_at=_parse_ts(data.get("opened_at")),
        updated_at=_parse_ts(data.get("updated_at")))


def _trade_from_dict(data: Mapping[str, Any]) -> TradeRecord:
    return TradeRecord(
        instrument_key=data["instrument_key"],
        strategy_id=data.get("strategy_id", ""), side=Side(data["side"]),
        quantity=Decimal(data["quantity"]),
        entry_price=Decimal(data["entry_price"]),
        exit_price=Decimal(data["exit_price"]),
        opened_at=_parse_ts(data.get("opened_at")),
        closed_at=_parse_ts(data["closed_at"]),
        gross_pnl=Decimal(data["gross_pnl"]), fees=Decimal(data.get("fees", "0")))


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    return ensure_utc(datetime.fromisoformat(value), field_name="ts")


__all__ = ["PortfolioManager", "TradeRecord", "FillEffect",
           "apply_fill_to_position"]
