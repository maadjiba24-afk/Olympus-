"""Order management — the local record of what we asked for and what happened.

This module is *not* the broker and *not* the execution policy. It is the
durable, crash-safe ledger of orders and fills, plus the one thing that makes
retrying safe: a deterministic idempotency key.

The idempotency key
-------------------
`make_client_order_id(decision, intent, attempt)` is a pure function of the
authorising decision. That matters because of the failure mode it exists to
prevent:

    submit → network timeout → we do not know whether the venue got it

The naive response — resubmit — doubles the position when the first request did
in fact land. The safe response is *query then adopt*, and querying requires
knowing the identifier the venue would have recorded. Deriving that identifier
from the decision means a retry after a timeout, after a process restart, or
after a full redeploy produces the *same* id, so the broker's own duplicate
detection becomes a second line of defence rather than something we have to
hope about.

Deliberately derived from the decision rather than from a UUID or a timestamp:
a random or time-based id is different on every attempt, which is exactly the
property you must not have here.

State machine
-------------
Transitions are validated against `LEGAL_TRANSITIONS`. An illegal transition
(`FILLED → NEW`) is an error rather than a silent overwrite, because it almost
always means two sources of truth have diverged — and silently accepting the
later one is how a filled order gets resubmitted.

Re-asserting the status an order already has is *not* an illegal transition:
brokers redeliver the same status routinely, and treating a redelivery as an
error would make ordinary polling noisy. Only a move to a *different*, unlisted
status is refused.

Durability
----------
Every mutation is written through to `olympus.store` before it returns, under a
`proclock` covering the read-modify-write. Buffering would leave a window in
which a crash loses the record of an order that is live at the venue — exposure
you do not know you have, which is the worst state to recover from. The lock is
per-order rather than global so two instruments do not serialise on each other.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import (Fill, Instrument, Order, OrderStatus, RiskDecision,
                        TradeIntent, ensure_utc, jsonable, to_decimal)
from .errors import DuplicateOrderError, ExecutionError, TradingError

_NS = "trading.orders"
_FILL_NS = "trading.fills"
#: broker_order_id -> client_order_id. An index rather than a scan because the
#: lookup happens on every venue callback, and a linear scan over every order
#: this account has ever placed is a latency cliff that only appears in
#: production.
_BROKER_INDEX_SUFFIX = ".broker_ids"

#: Legal order-status transitions. Anything not listed is refused.
LEGAL_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_NEW: frozenset({
        OrderStatus.NEW, OrderStatus.REJECTED, OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED}),
    OrderStatus.NEW: frozenset({
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.PENDING_CANCEL, OrderStatus.CANCELED,
        OrderStatus.REJECTED, OrderStatus.EXPIRED}),
    OrderStatus.PARTIALLY_FILLED: frozenset({
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.PENDING_CANCEL, OrderStatus.CANCELED,
        OrderStatus.EXPIRED}),
    OrderStatus.PENDING_CANCEL: frozenset({
        OrderStatus.CANCELED, OrderStatus.FILLED,
        OrderStatus.PARTIALLY_FILLED, OrderStatus.EXPIRED}),
    # Terminal states go nowhere.
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}


def make_client_order_id(decision: RiskDecision, intent: TradeIntent,
                         attempt: int = 0, *, prefix: str = "olymp") -> str:
    """A deterministic, venue-safe idempotency key.

    Includes the approved quantity and the policy hash as well as the intent id:
    if the same intent were ever re-authorised under different limits or at a
    different size, that is a genuinely different order and must not collide
    with the first. `attempt` is here for the rare case where an order was
    provably rejected and the operator wants a fresh identity — it is not
    incremented on ordinary retries, which must reuse the same id.
    """
    if not decision.approved:
        raise ExecutionError(
            "cannot derive an order id from a rejected decision",
            intent_id=intent.intent_id)
    if decision.intent_id != intent.intent_id:
        raise ExecutionError(
            "decision does not authorise this intent",
            decision_intent=decision.intent_id, intent=intent.intent_id)
    material = "|".join([
        intent.intent_id, intent.strategy_id, intent.strategy_version,
        intent.instrument_key, intent.side.value, intent.order_type.value,
        str(decision.approved_quantity), decision.policy_hash or "",
        decision.mode.value, str(int(attempt)),
    ])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


@dataclass(frozen=True)
class OrderRecord:
    """An order plus its fills, as persisted."""
    order: Order
    fills: tuple[Fill, ...] = ()

    def to_dict(self) -> dict:
        return {"order": self.order.to_dict(),
                "fills": [f.to_dict() for f in self.fills]}


class OrderStore:
    """Durable order + fill records. Write-through on every mutation.

    Write-through rather than periodic flushing because the window a buffered
    write leaves open is precisely the window in which a crash loses the record
    of an order that is live at the venue — the worst state to recover from,
    since you have exposure you do not know about.
    """

    def __init__(self, *, clock: Clock | None = None, namespace: str = _NS,
                 fill_namespace: str = _FILL_NS, audit=None,
                 store_ns: str | None = None):
        self.clock = clock or default_clock()
        self.namespace = store_ns or namespace
        self.fill_namespace = fill_namespace
        self.broker_index_namespace = self.namespace + _BROKER_INDEX_SUFFIX
        self.audit = audit

    def _store(self):
        from olympus import store
        return store.backend()

    def _lock(self, client_order_id: str):
        """Serialise the read-modify-write of one order across processes.

        Per-order: the poller, the reconciler, and the submitting thread all
        touch the same record, and a lost update there means a fill that was
        counted disappears from the local book.
        """
        from olympus import proclock
        return proclock.lock(f"trading-oms-{self.namespace}-{client_order_id}")

    # -- reads ------------------------------------------------------------

    def get(self, client_order_id: str) -> Order | None:
        raw = self._store().get(self.namespace, client_order_id)
        if not raw:
            return None
        return _order_from_dict(json.loads(raw.decode("utf-8"))["order"])

    def fills_for(self, client_order_id: str) -> list[Fill]:
        raw = self._store().get(self.namespace, client_order_id)
        if not raw:
            return []
        return [_fill_from_dict(f)
                for f in json.loads(raw.decode("utf-8")).get("fills", [])]

    def all_orders(self) -> list[Order]:
        out = []
        for key in self._store().keys(self.namespace):
            order = self.get(key)
            if order is not None:
                out.append(order)
        return sorted(out, key=lambda o: o.created_at)

    def open_orders(self) -> list[Order]:
        return [o for o in self.all_orders() if o.is_open]

    def by_broker_id(self, broker_order_id: str) -> Order | None:
        """Resolve a venue identifier to our order. Index first, scan as a
        fallback so records written before the index existed still resolve."""
        raw = self._store().get(self.broker_index_namespace, _safe_key(broker_order_id))
        if raw:
            order = self.get(raw.decode("utf-8"))
            if order is not None and order.broker_order_id == broker_order_id:
                return order
        for order in self.all_orders():
            if order.broker_order_id == broker_order_id:
                return order
        return None

    # -- writes -----------------------------------------------------------

    def _put(self, order: Order, fills: Sequence[Fill]) -> None:
        payload = json.dumps(
            {"order": order.to_dict(), "fills": [f.to_dict() for f in fills]},
            sort_keys=True).encode("utf-8")
        self._store().put(self.namespace, order.client_order_id, payload)
        if order.broker_order_id:
            # Written on every mutation, not only in `link_broker_id`: the id
            # usually arrives as a side effect of a status transition.
            self._store().put(self.broker_index_namespace,
                              _safe_key(order.broker_order_id),
                              order.client_order_id.encode("utf-8"))

    def create(self, order: Order) -> Order:
        """Record a new order. Refuses to overwrite an existing id.

        Surfacing the duplicate rather than swallowing it lets the caller decide
        between "this is a safe retry, give me the existing order" and "this is
        a bug" — a distinction the store itself cannot make.
        """
        existing = self.get(order.client_order_id)
        if existing is not None:
            raise DuplicateOrderError(
                "an order with this client_order_id already exists",
                client_order_id=order.client_order_id,
                existing_status=existing.status.value)
        self._put(order, ())
        self._audit("ORDER_SUBMITTED", order)
        return order

    def get_or_create(self, order: Order) -> tuple[Order, bool]:
        """Idempotent create. Returns (order, created)."""
        existing = self.get(order.client_order_id)
        if existing is not None:
            return existing, False
        return self.create(order), True

    def transition(self, client_order_id: str, new_status: OrderStatus,
                   **changes: Any) -> Order:
        order = self._require(client_order_id)
        new_status = OrderStatus(new_status)
        allowed = LEGAL_TRANSITIONS.get(order.status, frozenset())
        if new_status != order.status and new_status not in allowed:
            raise ExecutionError(
                "illegal order state transition",
                client_order_id=client_order_id,
                current=order.status.value, requested=new_status.value)
        updated = order.evolve(status=new_status,
                               updated_at=self.clock.now(), **changes)
        self._put(updated, self.fills_for(client_order_id))
        self._audit("BROKER_RESPONSE", updated)
        return updated

    def link_broker_id(self, client_order_id: str, broker_order_id: str) -> Order:
        order = self._require(client_order_id)
        updated = order.evolve(broker_order_id=broker_order_id,
                               updated_at=self.clock.now())
        self._put(updated, self.fills_for(client_order_id))
        return updated

    def record_fill(self, fill: Fill) -> Order:
        """Apply a fill. Idempotent on `fill_id`; refuses to over-fill.

        Over-filling is treated as an error rather than clamped: a fill larger
        than the order's remaining quantity means our record and the venue's
        disagree, and quietly clamping would hide the divergence that
        reconciliation exists to catch.
        """
        order = self._require(fill.client_order_id)
        fills = self.fills_for(fill.client_order_id)
        if any(f.fill_id == fill.fill_id for f in fills):
            return order

        total = sum((f.quantity for f in fills), Decimal("0")) + fill.quantity
        if total > order.quantity:
            raise ExecutionError(
                "fill would exceed the order quantity",
                client_order_id=order.client_order_id,
                order_quantity=str(order.quantity), filled=str(total))

        fills.append(fill)
        notional = sum((f.quantity * f.price for f in fills), Decimal("0"))
        avg = notional / total if total > 0 else None
        fees = sum((f.fee for f in fills), Decimal("0"))
        status = OrderStatus.FILLED if total == order.quantity \
            else OrderStatus.PARTIALLY_FILLED
        allowed = LEGAL_TRANSITIONS.get(order.status, frozenset())
        if status != order.status and status not in allowed:
            raise ExecutionError(
                "fill implies an illegal state transition",
                client_order_id=order.client_order_id,
                current=order.status.value, implied=status.value)

        updated = order.evolve(filled_quantity=total, average_fill_price=avg,
                               fees=fees, status=status,
                               updated_at=self.clock.now())
        self._put(updated, fills)
        self._store().put(self.fill_namespace, fill.fill_id,
                          json.dumps(fill.to_dict(), sort_keys=True).encode("utf-8"))
        self._audit("FILL", updated, extra=fill.to_dict())
        return updated

    # -- helpers ----------------------------------------------------------

    def _require(self, client_order_id: str) -> Order:
        order = self.get(client_order_id)
        if order is None:
            raise ExecutionError("unknown order", client_order_id=client_order_id)
        return order

    def _audit(self, event: str, order: Order, extra: Mapping | None = None) -> None:
        if self.audit is None:
            return
        try:
            payload = order.to_dict()
            if extra:
                payload = {**payload, "fill": dict(extra)}
            self.audit.record(event, payload, actor="oms",
                              subject=order.instrument_key,
                              correlation_id=order.intent_id)
        except Exception:                                # noqa: BLE001
            pass


def _order_from_dict(data: Mapping[str, Any]) -> Order:
    from .contracts import Mode, OrderType, Side, TimeInForce
    return Order(
        client_order_id=data["client_order_id"],
        instrument_key=data["instrument_key"], side=Side(data["side"]),
        order_type=OrderType(data["order_type"]),
        quantity=Decimal(data["quantity"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        status=OrderStatus(data["status"]),
        limit_price=_dec(data.get("limit_price")),
        stop_price=_dec(data.get("stop_price")),
        time_in_force=TimeInForce(data.get("time_in_force", "day")),
        filled_quantity=Decimal(data.get("filled_quantity", "0")),
        average_fill_price=_dec(data.get("average_fill_price")),
        broker_order_id=data.get("broker_order_id"),
        updated_at=(datetime.fromisoformat(data["updated_at"])
                    if data.get("updated_at") else None),
        intent_id=data.get("intent_id", ""),
        decision_id=data.get("decision_id", ""),
        strategy_id=data.get("strategy_id", ""),
        mode=Mode(data.get("mode", "paper")),
        reject_reason=data.get("reject_reason", ""),
        fees=Decimal(data.get("fees", "0")))


def _fill_from_dict(data: Mapping[str, Any]) -> Fill:
    from .contracts import Side
    return Fill(
        fill_id=data["fill_id"], client_order_id=data["client_order_id"],
        instrument_key=data["instrument_key"], side=Side(data["side"]),
        quantity=Decimal(data["quantity"]), price=Decimal(data["price"]),
        ts=datetime.fromisoformat(data["ts"]), fee=Decimal(data.get("fee", "0")),
        fee_currency=data.get("fee_currency", "USD"),
        broker_fill_id=data.get("broker_fill_id"),
        liquidity=data.get("liquidity", ""))


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


__all__ = ["OrderStore", "OrderRecord", "make_client_order_id",
           "LEGAL_TRANSITIONS"]
