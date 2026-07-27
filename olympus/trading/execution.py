"""The execution engine.

Takes an **approved RiskDecision** plus the intent it authorises, and drives the
broker. It cannot be persuaded to act on anything less: `execute()` verifies that
the decision names this intent, that it approves rather than rejects, and that it
is not stale. That check is the structural reason no model can trade — a
forecaster or an agent has no way to manufacture a `RiskDecision` that the risk
engine did not produce, and without one this method refuses.

Retry safety
------------
The dangerous moment is a submit that times out: the venue may or may not have
the order. Blind resubmission doubles the position. The rule here is
**query-then-adopt** — on any post-submit uncertainty, ask the broker for the
order by its deterministic `client_order_id` and adopt what comes back. Only
failures that provably happened *before* the request reached the venue
(connection refused, not-connected) are retried, and even then the same id is
reused so the venue's own deduplication is a second line of defence.

Modes
-----
`SHADOW` does everything except submit: the order is recorded locally with its
full provenance so shadow and live traffic can be compared later, but nothing
reaches a venue. `BACKTEST`/`PAPER` submit to the paper broker. `LIVE_*` submit
to the real adapter and are additionally gated by `modes.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import (Fill, Instrument, Mode, Order, OrderStatus, OrderType,
                        RiskDecision, Side, TradeIntent, Verdict, to_decimal)
from .errors import (BrokerError, BrokerUnavailable, DuplicateOrderError,
                     ExecutionError, ModeNotPermitted)
from .oms import OrderStore, make_client_order_id


@dataclass(frozen=True)
class ExecutionConfig:
    """Tunables, all with conservative defaults."""
    #: A decision older than this is refused — market conditions have moved on
    #: and the evidence it was made from is no longer current.
    max_decision_age_s: float = 60.0
    #: Refuse if the reference price has moved more than this from the intent's
    #: intended entry. A collar, not a limit price: it stops the order from
    #: being sent at all rather than resting at a stale level.
    max_price_deviation_pct: float = 2.0
    max_submit_attempts: int = 3
    retry_backoff_s: float = 0.5


class ExecutionEngine:
    def __init__(self, *, broker, order_store: OrderStore, portfolio=None,
                 clock: Clock | None = None, audit=None,
                 mode: Mode = Mode.PAPER,
                 config: ExecutionConfig | None = None,
                 mode_controller=None):
        self.broker = broker
        self.orders = order_store
        self.portfolio = portfolio
        self.clock = clock or default_clock()
        self.audit = audit
        self.mode = mode
        self.config = config or ExecutionConfig()
        self.mode_controller = mode_controller

    # -- the guarded entry point ------------------------------------------

    def execute(self, intent: TradeIntent, decision: RiskDecision,
                instrument: Instrument, *, reference_price: Any = None) -> Order:
        self._verify_authorisation(intent, decision)
        mode = self._effective_mode()
        self._verify_mode_permits_orders(mode)

        quantity = instrument.quantize_quantity(decision.approved_quantity)
        if quantity <= 0:
            raise ExecutionError(
                "authorised quantity rounds to zero at this instrument's lot size",
                intent_id=intent.intent_id,
                approved=str(decision.approved_quantity))

        self._verify_price_collar(intent, instrument, reference_price)

        coid = make_client_order_id(decision, intent)
        order = Order(
            client_order_id=coid, instrument_key=intent.instrument_key,
            side=intent.side, order_type=intent.order_type, quantity=quantity,
            created_at=self.clock.now(), status=OrderStatus.PENDING_NEW,
            limit_price=intent.limit_price,
            stop_price=getattr(intent, "stop_price", None),
            time_in_force=intent.time_in_force, intent_id=intent.intent_id,
            decision_id=decision.policy_hash, strategy_id=intent.strategy_id,
            mode=mode)

        stored, created = self.orders.get_or_create(order)
        if not created:
            # A repeat of an authorisation we already acted on. Returning the
            # existing order is the whole point of the deterministic id.
            return stored

        if mode is Mode.SHADOW:
            # Record the counterfactual; submit nothing.
            shadowed = self.orders.transition(
                coid, OrderStatus.CANCELED, reject_reason="shadow mode: not submitted")
            self._record("ORDER_SUBMITTED", {**shadowed.to_dict(),
                                             "shadow": True}, intent)
            return shadowed

        return self._submit_with_retries(stored, intent)

    # -- verification ------------------------------------------------------

    def _verify_authorisation(self, intent: TradeIntent,
                              decision: RiskDecision) -> None:
        if decision is None:
            raise ExecutionError("execution requires a risk decision",
                                 intent_id=intent.intent_id)
        if decision.intent_id != intent.intent_id:
            raise ExecutionError(
                "the decision does not authorise this intent",
                intent_id=intent.intent_id, decision_intent=decision.intent_id)
        if decision.verdict is Verdict.REJECTED or not decision.approved:
            raise ExecutionError("the risk engine rejected this intent",
                                 intent_id=intent.intent_id,
                                 reasons=list(decision.reason_codes))
        age = (self.clock.now() - decision.decided_at).total_seconds()
        if age > self.config.max_decision_age_s:
            raise ExecutionError(
                "the risk decision is stale; re-authorise before executing",
                intent_id=intent.intent_id, age_s=age,
                limit_s=self.config.max_decision_age_s)
        if age < -1.0:
            raise ExecutionError("the risk decision is dated in the future",
                                 intent_id=intent.intent_id, age_s=age)

    def _verify_mode_permits_orders(self, mode: Mode) -> None:
        if not mode.is_live:
            return
        if self.mode_controller is None:
            raise ModeNotPermitted(
                "live execution requires a mode controller that has passed its "
                "deployment gates", mode=mode.value)
        if not self.mode_controller.permits_live_orders():
            raise ModeNotPermitted("live trading is not enabled", mode=mode.value)

    def _verify_price_collar(self, intent: TradeIntent, instrument: Instrument,
                             reference_price: Any) -> None:
        if reference_price is None or intent.intended_entry is None:
            return
        ref = to_decimal(reference_price, field_name="reference_price")
        entry = to_decimal(intent.intended_entry, field_name="intended_entry")
        if entry <= 0:
            return
        deviation = abs((ref - entry) / entry) * Decimal("100")
        if float(deviation) > self.config.max_price_deviation_pct:
            raise ExecutionError(
                "price collar: the market has moved too far from the intended entry",
                intent_id=intent.intent_id, deviation_pct=float(deviation),
                limit_pct=self.config.max_price_deviation_pct)

    def _effective_mode(self) -> Mode:
        if self.mode_controller is not None:
            try:
                return self.mode_controller.current()
            except Exception:                            # noqa: BLE001
                return Mode.PAPER
        return self.mode

    # -- submission --------------------------------------------------------

    def _submit_with_retries(self, order: Order, intent: TradeIntent) -> Order:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_submit_attempts + 1):
            try:
                acknowledged = self.broker.submit_order(order)
            except BrokerUnavailable as exc:
                # Provably never reached the venue: safe to retry with the SAME
                # id. Before retrying, still query — a previous attempt in an
                # earlier process may have landed.
                last_error = exc
                adopted = self._query_and_adopt(order)
                if adopted is not None:
                    return adopted
                continue
            except BrokerError as exc:
                # Ambiguous: the request may have been received. Never resubmit.
                last_error = exc
                adopted = self._query_and_adopt(order)
                if adopted is not None:
                    return adopted
                break
            else:
                updated = self.orders.transition(
                    order.client_order_id, acknowledged.status,
                    broker_order_id=acknowledged.broker_order_id,
                    reject_reason=acknowledged.reject_reason)
                self._record("BROKER_RESPONSE", updated.to_dict(), intent)
                return updated

        self.orders.transition(order.client_order_id, OrderStatus.REJECTED,
                               reject_reason=f"submit failed: {last_error}")
        raise ExecutionError("order submission failed",
                             client_order_id=order.client_order_id,
                             cause=str(last_error))

    def _query_and_adopt(self, order: Order) -> Order | None:
        """Ask the venue whether it already has this order; adopt if so.

        This is what makes retrying safe. Never call `submit_order` again
        without doing this first.
        """
        try:
            remote = self.broker.get_order(order.client_order_id)
        except Exception:                                # noqa: BLE001
            return None
        if remote is None:
            return None
        return self.orders.transition(
            order.client_order_id, remote.status,
            broker_order_id=remote.broker_order_id,
            reject_reason=remote.reject_reason)

    # -- lifecycle ---------------------------------------------------------

    def poll(self, since: datetime | None = None) -> list[Fill]:
        """Pull fills and statuses from the venue. Idempotent by construction —
        the OMS ignores a `fill_id` it has already recorded."""
        applied: list[Fill] = []
        try:
            fills = self.broker.get_fills(since=since)
        except Exception as exc:                         # noqa: BLE001
            self._record("ERROR", {"stage": "poll", "error": str(exc)}, None)
            return applied
        for fill in fills:
            try:
                self.orders.record_fill(fill)
            except ExecutionError:
                continue                                 # unknown/over-fill: reconcile
            if self.portfolio is not None:
                self.portfolio.apply_fill(fill)
            applied.append(fill)
        return applied

    def recover(self) -> list[Order]:
        """Startup reconciliation of in-flight orders.

        A `PENDING_NEW` order after a crash is the ambiguous case: we may or may
        not have submitted it. Querying by the deterministic id resolves it
        without risking a duplicate.
        """
        recovered: list[Order] = []
        for order in self.orders.open_orders():
            remote = None
            try:
                remote = self.broker.get_order(order.client_order_id)
            except Exception:                            # noqa: BLE001
                continue
            if remote is None:
                continue
            if remote.status != order.status:
                recovered.append(self.orders.transition(
                    order.client_order_id, remote.status,
                    broker_order_id=remote.broker_order_id))
        return recovered

    def cancel_stale_orders(self, max_age: timedelta) -> list[Order]:
        cutoff = self.clock.now() - max_age
        cancelled = []
        for order in self.orders.open_orders():
            if order.created_at > cutoff:
                continue
            try:
                remote = self.broker.cancel_order(order.client_order_id)
            except Exception:                            # noqa: BLE001
                continue
            cancelled.append(self.orders.transition(
                order.client_order_id, remote.status))
        return cancelled

    # -- audit -------------------------------------------------------------

    def _record(self, event: str, payload: Mapping[str, Any],
                intent: TradeIntent | None) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(event, dict(payload), actor="execution",
                              subject=(intent.instrument_key if intent else ""),
                              correlation_id=(intent.intent_id if intent else ""))
        except Exception:                                # noqa: BLE001
            pass


__all__ = ["ExecutionEngine", "ExecutionConfig"]
