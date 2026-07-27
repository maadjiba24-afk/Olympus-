"""Reconciliation — proving local state and the venue still agree.

The question this answers is the one that matters most after a restart, an
outage, or a missed message: *do we actually hold what we think we hold?*
Everything upstream — risk limits, exposure checks, P&L — is computed from
Olympus's own book, so if that book has drifted from the venue then every
downstream guarantee is being enforced against fiction.

Deliberately limited auto-repair
--------------------------------
Orders and fills the broker knows about **are** adopted: those are cases where
the venue has strictly more information than we do (a fill we missed, a status
we did not see), and adopting them moves us toward truth.

A **position** mismatch is never silently overwritten. A position break means
either we missed a fill or the venue is wrong, and those have opposite correct
responses — one requires accepting the venue's number, the other requires
disputing it. Guessing produces a book that looks reconciled while being wrong,
which is worse than a break that is visibly open. Position breaks therefore
surface, trip the desync kill switch, and wait for an operator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import (AccountSnapshot, Order, OrderStatus, Position,
                        ensure_utc, jsonable, to_decimal)
from .errors import ReconciliationError, TradingError

_NS = "trading.reconcile"


@dataclass(frozen=True)
class Break:
    """One disagreement between local and remote state."""
    kind: str                       # "position" | "order" | "cash"
    key: str
    local: Any
    remote: Any
    detail: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "key": self.key, "local": jsonable(self.local),
                "remote": jsonable(self.remote), "detail": self.detail}


@dataclass(frozen=True)
class ReconciliationReport:
    ts: datetime
    ok: bool
    position_breaks: tuple[Break, ...] = ()
    order_breaks: tuple[Break, ...] = ()
    cash_break: Break | None = None
    adopted_orders: tuple[str, ...] = ()
    adopted_fills: tuple[str, ...] = ()
    missing_locally: tuple[str, ...] = ()
    missing_remotely: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))

    @property
    def breaks(self) -> tuple[Break, ...]:
        out = list(self.position_breaks) + list(self.order_breaks)
        if self.cash_break is not None:
            out.append(self.cash_break)
        return tuple(out)

    def to_dict(self) -> dict:
        return {
            "ts": jsonable(self.ts), "ok": self.ok,
            "position_breaks": [b.to_dict() for b in self.position_breaks],
            "order_breaks": [b.to_dict() for b in self.order_breaks],
            "cash_break": self.cash_break.to_dict() if self.cash_break else None,
            "adopted_orders": list(self.adopted_orders),
            "adopted_fills": list(self.adopted_fills),
            "missing_locally": list(self.missing_locally),
            "missing_remotely": list(self.missing_remotely),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class Tolerances:
    """How much disagreement is noise rather than a break.

    Non-zero for cash because fees, interest accruals and rounding at the venue
    legitimately differ by small amounts. Zero for quantity because a share is
    a share — a fractional position discrepancy is always a real break.
    """
    quantity: Decimal = Decimal("0")
    cash: Decimal = Decimal("0.01")


class Reconciler:
    def __init__(self, *, broker, order_store, portfolio,
                 clock: Clock | None = None, audit=None,
                 tolerances: Tolerances | None = None,
                 killswitches=None, namespace: str = _NS):
        self.broker = broker
        self.orders = order_store
        self.portfolio = portfolio
        self.clock = clock or default_clock()
        self.audit = audit
        self.tolerances = tolerances or Tolerances()
        self.killswitches = killswitches
        self.namespace = namespace

    # -- the check ---------------------------------------------------------

    def reconcile(self, *, adopt: bool = True) -> ReconciliationReport:
        now = self.clock.now()
        errors: list[str] = []
        position_breaks: list[Break] = []
        order_breaks: list[Break] = []
        adopted_orders: list[str] = []
        adopted_fills: list[str] = []
        missing_locally: list[str] = []
        missing_remotely: list[str] = []
        cash_break: Break | None = None

        # -- orders: the venue is authoritative about status ---------------
        try:
            remote_orders = {o.client_order_id: o for o in self.broker.get_open_orders()}
        except Exception as exc:                         # noqa: BLE001
            errors.append(f"could not read broker orders: {exc}")
            remote_orders = {}

        local_open = {o.client_order_id: o for o in self.orders.open_orders()}

        for coid, remote in remote_orders.items():
            local = local_open.get(coid) or self.orders.get(coid)
            if local is None:
                missing_locally.append(coid)
                order_breaks.append(Break(
                    kind="order", key=coid, local=None,
                    remote=remote.status.value,
                    detail="the venue has an order we have no record of"))
                continue
            if local.status is not remote.status and adopt:
                try:
                    self.orders.transition(coid, remote.status,
                                           broker_order_id=remote.broker_order_id)
                    adopted_orders.append(coid)
                except TradingError as exc:
                    order_breaks.append(Break(
                        kind="order", key=coid, local=local.status.value,
                        remote=remote.status.value, detail=str(exc)))

        for coid, local in local_open.items():
            if coid in remote_orders:
                continue
            # We think it is open; the venue does not list it. Ask directly
            # before calling it a break — it may simply have filled.
            try:
                remote = self.broker.get_order(coid)
            except Exception:                            # noqa: BLE001
                remote = None
            if remote is None:
                missing_remotely.append(coid)
                order_breaks.append(Break(
                    kind="order", key=coid, local=local.status.value, remote=None,
                    detail="we believe this order is open; the venue has no record"))
            elif remote.status is not local.status and adopt:
                try:
                    self.orders.transition(coid, remote.status,
                                           broker_order_id=remote.broker_order_id)
                    adopted_orders.append(coid)
                except TradingError as exc:
                    order_breaks.append(Break(
                        kind="order", key=coid, local=local.status.value,
                        remote=remote.status.value, detail=str(exc)))

        # -- fills: adopting is always safe (venue knows more) -------------
        if adopt:
            try:
                for fill in self.broker.get_fills():
                    try:
                        self.orders.record_fill(fill)
                    except TradingError:
                        continue
                    if self.portfolio is not None:
                        before = self.portfolio.position(fill.instrument_key).quantity
                        self.portfolio.apply_fill(fill)
                        if self.portfolio.position(fill.instrument_key).quantity != before:
                            adopted_fills.append(fill.fill_id)
            except Exception as exc:                     # noqa: BLE001
                errors.append(f"could not read broker fills: {exc}")

        # -- positions: NEVER auto-repaired -------------------------------
        try:
            remote_positions = {p.instrument_key: p for p in self.broker.get_positions()}
        except Exception as exc:                         # noqa: BLE001
            errors.append(f"could not read broker positions: {exc}")
            remote_positions = {}

        local_positions = (self.portfolio.open_positions()
                           if self.portfolio is not None else {})
        for key in set(remote_positions) | set(local_positions):
            local_qty = local_positions.get(
                key, Position(instrument_key=key)).quantity
            remote_qty = remote_positions.get(
                key, Position(instrument_key=key)).quantity
            if abs(local_qty - remote_qty) > self.tolerances.quantity:
                position_breaks.append(Break(
                    kind="position", key=key, local=str(local_qty),
                    remote=str(remote_qty),
                    detail="position mismatch — requires an operator; never "
                           "auto-corrected because the right response depends "
                           "on whether we missed a fill or the venue is wrong"))

        # -- cash ----------------------------------------------------------
        try:
            account: AccountSnapshot = self.broker.get_account()
        except Exception as exc:                         # noqa: BLE001
            errors.append(f"could not read broker account: {exc}")
            account = None
        if account is not None and self.portfolio is not None:
            diff = abs(account.cash - self.portfolio.cash)
            if diff > self.tolerances.cash:
                cash_break = Break(kind="cash", key="cash",
                                   local=str(self.portfolio.cash),
                                   remote=str(account.cash),
                                   detail=f"cash differs by {diff}")

        ok = not (position_breaks or order_breaks or cash_break or errors)
        report = ReconciliationReport(
            ts=now, ok=ok, position_breaks=tuple(position_breaks),
            order_breaks=tuple(order_breaks), cash_break=cash_break,
            adopted_orders=tuple(adopted_orders), adopted_fills=tuple(adopted_fills),
            missing_locally=tuple(missing_locally),
            missing_remotely=tuple(missing_remotely), errors=tuple(errors))

        self._persist(report)
        self._record(report)
        if not ok:
            self._trip_desync(report)
        return report

    # -- state -------------------------------------------------------------

    def _store(self):
        from olympus import store
        return store.backend()

    def _persist(self, report: ReconciliationReport) -> None:
        import json
        blob = json.dumps(report.to_dict(), sort_keys=True).encode("utf-8")
        self._store().put(self.namespace, "last", blob)
        if report.ok:
            self._store().put(self.namespace, "last_success", blob)

    def last_success_ts(self) -> datetime | None:
        """Feeds the risk engine's `max_reconciliation_age_s` check."""
        import json
        raw = self._store().get(self.namespace, "last_success")
        if not raw:
            return None
        return datetime.fromisoformat(json.loads(raw.decode("utf-8"))["ts"])

    def age_seconds(self, now: datetime | None = None) -> float | None:
        last = self.last_success_ts()
        if last is None:
            return None
        return ((now or self.clock.now()) - last).total_seconds()

    def _trip_desync(self, report: ReconciliationReport) -> None:
        if self.killswitches is None:
            return
        from .killswitch import CODE_BROKER_DESYNC, KillSwitchScope
        try:
            self.killswitches.engage(
                KillSwitchScope.GLOBAL,
                reason=f"reconciliation found {len(report.breaks)} break(s)",
                code=CODE_BROKER_DESYNC, by="reconciler", auto=True)
        except Exception:                                # noqa: BLE001
            pass

    def _record(self, report: ReconciliationReport) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record("RECONCILIATION", report.to_dict(),
                              actor="reconciler", subject="", correlation_id="")
        except Exception:                                # noqa: BLE001
            pass


__all__ = ["Reconciler", "ReconciliationReport", "Break", "Tolerances"]
