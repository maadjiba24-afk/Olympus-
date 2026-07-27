"""Reconciliation — proving local state and the venue still agree.

The question this answers is the one that matters most after a restart, an
outage, or a missed message: *do we actually hold what we think we hold?*
Everything upstream — risk limits, exposure checks, P&L — is computed from
Olympus's own book, so if that book has drifted from the venue then every
downstream guarantee is being enforced against fiction. A limit checked against
a position you do not have is not a limit.

Deliberately limited auto-repair
--------------------------------
Orders and fills the broker knows about **are** adopted: those are cases where
the venue has strictly more information than we do (a fill we missed, a status
we did not see), and adopting them moves us toward truth. Adoption there is
also *additive* — it books something that demonstrably happened.

A **position** mismatch is never silently overwritten. A position break has two
possible causes with opposite correct responses:

* we missed a fill — the venue's number is right, and the repair is to find and
  book the fill (which changes cash and realised P&L too, not just quantity); or
* the venue is wrong, or reporting a position from another system sharing the
  account — the repair is to dispute it, and copying the number would destroy
  the evidence.

A reconciler cannot tell those apart, and guessing produces a book that *looks*
reconciled while being wrong — strictly worse than a break that is visibly open,
because nobody investigates a green check. Position breaks therefore surface,
trip the desync kill switch, and wait for an operator.

Failure to *perform* the check is also a break. If the broker cannot be read,
this returns `ok=False`: "I could not verify" and "I verified and it agrees"
must never be the same answer, because `last_success_ts` is what the risk
engine's `max_reconciliation_age_s` check reads to decide whether trading may
continue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .clock import Clock, default_clock
from .contracts import (AccountSnapshot, Position, ensure_utc, jsonable)
from .errors import TradingError

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
    """The verdict of one reconciliation pass. Written to the audit trail whole."""
    ts: datetime
    ok: bool
    position_breaks: tuple[Break, ...] = ()
    order_breaks: tuple[Break, ...] = ()
    cash_break: Break | None = None
    adopted_orders: tuple[str, ...] = ()
    adopted_fills: tuple[str, ...] = ()
    #: Orders the venue has and we do not.
    missing_locally: tuple[str, ...] = ()
    #: Orders we believe are open and the venue has never heard of.
    missing_remotely: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    #: Free-form counts and context. Never load-bearing — anything a caller must
    #: act on has its own typed field above.
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        for name in ("position_breaks", "order_breaks", "adopted_orders",
                     "adopted_fills", "missing_locally", "missing_remotely",
                     "errors"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "details", dict(self.details))

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
            "details": jsonable(dict(self.details)),
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
    """Compares local state against the venue. Repairs only what is safe."""

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
        adopted_orders: list[str] = []
        adopted_fills: list[str] = []

        order_breaks, missing_locally, missing_remotely = self._reconcile_orders(
            adopt=adopt, adopted=adopted_orders, errors=errors)
        if adopt:
            self._adopt_fills(adopted=adopted_fills, errors=errors)
        position_breaks = self._reconcile_positions(errors=errors)
        cash_break = self._reconcile_cash(errors=errors)

        ok = not (position_breaks or order_breaks or cash_break or errors)
        report = ReconciliationReport(
            ts=now, ok=ok, position_breaks=tuple(position_breaks),
            order_breaks=tuple(order_breaks), cash_break=cash_break,
            adopted_orders=tuple(adopted_orders),
            adopted_fills=tuple(adopted_fills),
            missing_locally=tuple(missing_locally),
            missing_remotely=tuple(missing_remotely), errors=tuple(errors),
            details={"adopt": adopt,
                     "position_breaks": len(position_breaks),
                     "order_breaks": len(order_breaks),
                     "cash_break": cash_break is not None,
                     "errors": len(errors)})

        self._persist(report)
        self._record(report)
        if not ok:
            self._trip_desync(report)
        return report

    # -- orders: the venue is authoritative about status --------------------

    def _reconcile_orders(self, *, adopt: bool, adopted: list[str],
                          errors: list[str]):
        breaks: list[Break] = []
        missing_locally: list[str] = []
        missing_remotely: list[str] = []
        try:
            remote_orders = {o.client_order_id: o
                             for o in self.broker.get_open_orders()}
        except Exception as exc:                         # noqa: BLE001
            errors.append(f"could not read broker orders: {exc}")
            return breaks, missing_locally, missing_remotely

        local_open = {o.client_order_id: o for o in self.orders.open_orders()}

        for coid, remote in remote_orders.items():
            local = local_open.get(coid) or self.orders.get(coid)
            if local is None:
                # An order at the venue we have no record of is the most
                # dangerous shape of break: unbounded exposure we are not
                # risk-checking. It is never auto-adopted — we do not know what
                # authorised it, and an order without provenance must not be
                # given some.
                missing_locally.append(coid)
                breaks.append(Break(
                    kind="order", key=coid, local=None,
                    remote=remote.status.value,
                    detail="the venue has an order we have no record of"))
                continue
            if local.status is not remote.status and adopt:
                if self._adopt_status(coid, remote):
                    adopted.append(coid)
                else:
                    breaks.append(Break(
                        kind="order", key=coid, local=local.status.value,
                        remote=remote.status.value,
                        detail="the venue's status cannot legally follow ours"))

        for coid, local in local_open.items():
            if coid in remote_orders:
                continue
            # We think it is open; the venue does not list it among open orders.
            # Ask directly before calling it a break — it may simply have
            # filled, which is a status change rather than a disagreement.
            try:
                remote = self.broker.get_order(coid)
            except Exception as exc:                     # noqa: BLE001
                errors.append(f"could not read broker order {coid}: {exc}")
                continue
            if remote is None:
                missing_remotely.append(coid)
                breaks.append(Break(
                    kind="order", key=coid, local=local.status.value, remote=None,
                    detail="we believe this order is open; the venue has no record"))
            elif remote.status is not local.status and adopt:
                if self._adopt_status(coid, remote):
                    adopted.append(coid)
                else:
                    breaks.append(Break(
                        kind="order", key=coid, local=local.status.value,
                        remote=remote.status.value,
                        detail="the venue's status cannot legally follow ours"))
        return breaks, missing_locally, missing_remotely

    def _adopt_status(self, client_order_id: str, remote) -> bool:
        changes = ({"broker_order_id": remote.broker_order_id}
                   if remote.broker_order_id else {})
        try:
            self.orders.transition(client_order_id, remote.status, **changes)
        except TradingError:
            return False
        return True

    # -- fills: adopting is always safe (the venue knows more) --------------

    def _adopt_fills(self, *, adopted: list[str], errors: list[str]) -> None:
        try:
            fills = self.broker.get_fills()
        except Exception as exc:                         # noqa: BLE001
            errors.append(f"could not read broker fills: {exc}")
            return
        for fill in fills:
            # Skip anything already stored. The OMS and the portfolio are both
            # idempotent on `fill_id`, but checking first is what keeps
            # `adopted_fills` honest — a report that lists redelivered fills as
            # newly adopted reads like a repair that never happened.
            if self.orders.fill(fill.fill_id) is not None:
                continue
            try:
                self.orders.record_fill(fill)
            except TradingError:
                # Unknown order, wrong instrument, or an over-fill: a real
                # break, surfaced by the position comparison below rather than
                # papered over here.
                continue
            if self.portfolio is not None:
                self.portfolio.apply_fill(fill)
            adopted.append(fill.fill_id)

    # -- positions: NEVER auto-repaired -------------------------------------

    def _reconcile_positions(self, *, errors: list[str]) -> list[Break]:
        breaks: list[Break] = []
        try:
            remote_positions = {p.instrument_key: p
                                for p in self.broker.get_positions()}
        except Exception as exc:                         # noqa: BLE001
            errors.append(f"could not read broker positions: {exc}")
            return breaks

        local_positions = (self.portfolio.open_positions()
                           if self.portfolio is not None else {})
        for key in sorted(set(remote_positions) | set(local_positions)):
            local_qty = local_positions.get(
                key, Position(instrument_key=key)).quantity
            remote_qty = remote_positions.get(
                key, Position(instrument_key=key)).quantity
            if abs(local_qty - remote_qty) > self.tolerances.quantity:
                breaks.append(Break(
                    kind="position", key=key, local=str(local_qty),
                    remote=str(remote_qty),
                    detail="position mismatch — requires an operator; never "
                           "auto-corrected because the right response depends "
                           "on whether we missed a fill or the venue is wrong"))
        return breaks

    def _reconcile_cash(self, *, errors: list[str]) -> Break | None:
        try:
            account: AccountSnapshot | None = self.broker.get_account()
        except Exception as exc:                         # noqa: BLE001
            errors.append(f"could not read broker account: {exc}")
            return None
        if account is None or self.portfolio is None:
            return None
        diff = abs(account.cash - self.portfolio.cash)
        if diff <= self.tolerances.cash:
            return None
        return Break(kind="cash", key="cash", local=str(self.portfolio.cash),
                     remote=str(account.cash),
                     detail=f"cash differs by {diff}")

    # -- state -------------------------------------------------------------

    def _store(self):
        from olympus import store
        return store.backend()

    def _persist(self, report: ReconciliationReport) -> None:
        """Write every pass, but move `last_success` only on a clean one.

        Two keys rather than one because they answer different questions: the
        latest pass is for an operator reading the dashboard, `last_success` is
        the safety input the risk engine gates on. Advancing the second on a
        failed pass would make "we have not verified anything for an hour" look
        like "everything verified a moment ago".
        """
        blob = json.dumps(report.to_dict(), sort_keys=True).encode("utf-8")
        self._store().put(self.namespace, "last", blob)
        if report.ok:
            self._store().put(self.namespace, "last_success", blob)

    def last_report(self) -> dict | None:
        raw = self._store().get(self.namespace, "last")
        return json.loads(raw.decode("utf-8")) if raw else None

    def last_success_ts(self) -> datetime | None:
        """Feeds the risk engine's `max_reconciliation_age_s` check."""
        raw = self._store().get(self.namespace, "last_success")
        if not raw:
            return None
        return ensure_utc(
            datetime.fromisoformat(json.loads(raw.decode("utf-8"))["ts"]),
            field_name="last_success_ts")

    def age_seconds(self, now: datetime | None = None) -> float | None:
        """Seconds since the last *successful* reconciliation, or None if never.

        None is not zero. A caller that treats "never reconciled" as "just
        reconciled" would let a freshly deployed process trade before it has
        ever checked its own book against the venue.
        """
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
            # A kill switch that cannot be engaged is itself an emergency, but
            # raising here would lose the report that documents why. The
            # monitor re-checks the switch independently.
            pass

    def _record(self, report: ReconciliationReport) -> None:
        if self.audit is None:
            return
        try:
            from .audit import EventType
            self.audit.record(EventType.RECONCILIATION, report.to_dict(),
                              actor="reconciler", subject="", correlation_id="")
        except Exception:                                # noqa: BLE001
            pass


__all__ = ["Reconciler", "ReconciliationReport", "Break", "Tolerances"]
