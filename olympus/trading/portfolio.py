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
common accounting bug in hand-rolled trading systems. It is implemented once, in
`apply_fill`, and tested in both directions.

Cost basis is weighted-average, not FIFO/LIFO. Weighted-average is what most
brokers report for a cash account and is path-independent, which means a
reconciliation mismatch points at a missing fill rather than at a lot-matching
policy disagreement. The choice is documented here because changing it later
would silently reprice history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import (Fill, Instrument, PortfolioSnapshot, Position, Side,
                        ensure_utc, jsonable, to_decimal)
from .errors import ConfigurationError, TradingError

_NS = "trading.portfolio"


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
        return {k: jsonable(v) for k, v in self.__dict__.items()} | {
            "net_pnl": jsonable(self.net_pnl)}


class PortfolioManager:
    """Olympus's own view of positions, cash, and P&L.

    Deliberately *not* the broker's view. Keeping an independent account is what
    makes reconciliation meaningful — if this simply mirrored the broker there
    would be nothing to compare and a silent fill loss would be invisible.
    """

    def __init__(self, *, starting_cash: Decimal | float | str = 0,
                 currency: str = "USD", clock: Clock | None = None,
                 account_id: str = "default", namespace: str = _NS):
        self.clock = clock or default_clock()
        self.currency = currency
        self.account_id = account_id
        self.namespace = namespace
        self._cash = to_decimal(starting_cash, field_name="starting_cash")
        self._starting_cash = self._cash
        self._positions: dict[str, Position] = {}
        self._marks: dict[str, float] = {}
        self._realised = Decimal("0")
        self._fees = Decimal("0")
        self._trades: list[TradeRecord] = []
        self._equity_curve: list[tuple[datetime, Decimal]] = []
        self._peak_equity = self._cash
        self._applied_fills: set[str] = set()
        self._by_strategy: dict[str, dict[str, Decimal]] = {}
        self._day_anchor: tuple[Any, Decimal] | None = None

    # -- state ------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def realised_pnl(self) -> Decimal:
        return self._realised

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

    def mark(self, instrument_key: str, price: float) -> None:
        price = float(price)
        if price <= 0:
            raise TradingError("mark price must be > 0",
                               instrument=instrument_key, got=price)
        self._marks[instrument_key] = price

    def mark_all(self, marks: Mapping[str, float]) -> None:
        for key, price in marks.items():
            self.mark(key, price)

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
        multiplier = instrument.multiplier if instrument is not None else Decimal("1")
        current = self._positions.get(key, Position(instrument_key=key))
        signed = fill.quantity * fill.side.sign
        old_qty = current.quantity
        new_qty = old_qty + signed
        price = fill.price

        # Cash: buying spends, selling receives; fees always reduce.
        self._cash -= signed * price * multiplier
        self._cash -= fill.fee
        self._fees += fill.fee

        gross_realised = Decimal("0")
        opened_at = current.opened_at

        if old_qty == 0:
            avg = price
            opened_at = fill.ts
        elif (old_qty > 0) == (signed > 0):
            # Adding to the same side: weighted-average the basis.
            total_cost = current.average_price * abs(old_qty) + price * abs(signed)
            avg = total_cost / abs(new_qty)
        else:
            # Reducing, closing, or flipping.
            closing = min(abs(old_qty), abs(signed))
            direction = Decimal(1) if old_qty > 0 else Decimal(-1)
            gross_realised = (price - current.average_price) * closing * direction * multiplier
            self._realised += gross_realised
            self._trades.append(TradeRecord(
                instrument_key=key, strategy_id=strategy_id, side=fill.side,
                quantity=closing, entry_price=current.average_price,
                exit_price=price, opened_at=opened_at, closed_at=fill.ts,
                gross_pnl=gross_realised, fees=fill.fee))
            if new_qty == 0:
                avg = Decimal("0")
                opened_at = None
            elif (new_qty > 0) == (old_qty > 0):
                avg = current.average_price          # partial close keeps basis
            else:
                # Crossed through zero: the remainder is a NEW position opened
                # at the fill price. Carrying the old basis here is the classic
                # silent-corruption bug this branch exists to prevent.
                avg = price
                opened_at = fill.ts

        updated = Position(
            instrument_key=key, quantity=new_qty, average_price=avg,
            realised_pnl=current.realised_pnl + gross_realised,
            fees_paid=current.fees_paid + fill.fee,
            opened_at=opened_at, updated_at=fill.ts)
        self._positions[key] = updated

        if strategy_id:
            bucket = self._by_strategy.setdefault(
                strategy_id, {"realised": Decimal("0"), "fees": Decimal("0")})
            bucket["realised"] += gross_realised
            bucket["fees"] += fill.fee

        self._marks.setdefault(key, float(price))
        self._record_equity(fill.ts)
        return updated

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

    def _record_equity(self, ts: datetime) -> None:
        ts = ensure_utc(ts, field_name="ts")
        value = self.snapshot(ts).equity
        self._equity_curve.append((ts, value))
        if value > self._peak_equity:
            self._peak_equity = value
        if self._day_anchor is None or self._day_anchor[0] != ts.date():
            # Anchor the session's starting equity on the first event of each
            # UTC day so daily P&L rolls over without a scheduler.
            self._day_anchor = (ts.date(), value - Decimal("0"))

    def equity_curve(self) -> list[tuple[datetime, Decimal]]:
        return list(self._equity_curve)

    @property
    def peak_equity(self) -> Decimal:
        return self._peak_equity

    def drawdown(self) -> Decimal:
        """Absolute drawdown from peak equity (a positive number)."""
        current = self.snapshot().equity
        return max(Decimal("0"), self._peak_equity - current)

    def drawdown_fraction(self) -> Decimal:
        if self._peak_equity <= 0:
            return Decimal("0")
        return self.drawdown() / self._peak_equity

    def daily_pnl(self, as_of: datetime | None = None) -> Decimal:
        """P&L since the first recorded equity point of the current UTC day.

        UTC-day boundaries rather than exchange sessions: the risk engine's
        daily-loss limit needs a definition that is unambiguous across venues,
        and a venue-specific one would differ per instrument in a mixed book.
        """
        now = ensure_utc(as_of or self.clock.now(), field_name="as_of")
        today = now.date()
        start = None
        for ts, value in self._equity_curve:
            if ts.date() == today:
                start = value
                break
        if start is None:
            return Decimal("0")
        return self.snapshot(now).equity - start

    def strategy_pnl(self, strategy_id: str) -> dict[str, Decimal]:
        bucket = self._by_strategy.get(strategy_id)
        if bucket is None:
            return {"realised": Decimal("0"), "fees": Decimal("0")}
        return dict(bucket)

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
            "by_strategy": {k: {kk: str(vv) for kk, vv in v.items()}
                            for k, v in self._by_strategy.items()},
            "trades": [t.to_dict() for t in self._trades],
        }
        blob = json.dumps(payload, sort_keys=True).encode("utf-8")
        with proclock.lock(f"trading-portfolio-{self._key()}"):
            self._store().put(self.namespace, self._key(), blob)

    def load(self) -> bool:
        """Restore. Returns False when there is nothing stored.

        Restart recovery is a safety property, not a convenience: a portfolio
        that forgets its positions on restart will happily re-open them.
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
        self._positions = {}
        for key, pdata in (data.get("positions") or {}).items():
            self._positions[key] = Position(
                instrument_key=pdata["instrument_key"],
                quantity=Decimal(pdata["quantity"]),
                average_price=Decimal(pdata["average_price"]),
                realised_pnl=Decimal(pdata.get("realised_pnl", "0")),
                fees_paid=Decimal(pdata.get("fees_paid", "0")),
                opened_at=_parse_ts(pdata.get("opened_at")),
                updated_at=_parse_ts(pdata.get("updated_at")))
        self._equity_curve = [
            (datetime.fromisoformat(ts), Decimal(v))
            for ts, v in (data.get("equity_curve") or [])]
        self._by_strategy = {
            k: {kk: Decimal(vv) for kk, vv in v.items()}
            for k, v in (data.get("by_strategy") or {}).items()}
        return True


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


__all__ = ["PortfolioManager", "TradeRecord"]
