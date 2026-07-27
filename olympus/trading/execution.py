"""The execution engine.

Takes an **approved RiskDecision** plus the intent it authorises, and drives the
broker. It cannot be persuaded to act on anything less: `execute()` verifies that
the decision names this intent, that it approves rather than rejects, and that it
is not stale. That check is the structural reason no model can trade — a
forecaster or an agent has no way to manufacture a `RiskDecision` that the risk
engine did not produce, and without one this method refuses.

The size that reaches the venue is `decision.approved_quantity`, never
`intent.quantity`. The two differ every time risk down-sized a proposal, and
reading the intent's number would silently discard exactly the reduction the
risk engine exists to impose.

Retry safety
------------
The dangerous moment is a submit that fails *after* the request left the
process: the venue may or may not have the order. Blind resubmission doubles the
position.

The rule here is **query-then-adopt**, and it is applied on *every* failure
rather than only the ones we believe are ambiguous. That is deliberate. The
tempting design is to classify exceptions — "connection refused happened before
the venue saw it, so resubmitting is safe" — but that classification is a
statement about someone else's client library, and it is wrong more often than
it is right. `PaperBroker.simulate_timeout` models the honest case: the venue
accepted the order and only the *reply* was lost, raising what looks like a
connectivity error. So the safety argument here does not rest on the exception
type at all:

1. `client_order_id` is a pure function of the authorisation, so the id is the
   same on every attempt, in every process, across restarts.
2. Before any retry, the venue is asked for that id. If it has the order we
   adopt it and stop. Only a venue that provably does *not* have the id is
   submitted to again.
3. If the *query itself* fails we cannot tell "the venue never got it" from
   "the venue has it and we cannot see it". That is the one case where doing
   nothing is correct: the local order stays `PENDING_NEW` and `recover()`
   resolves it when the venue is reachable. Marking it rejected there would be
   a lie that later invents a duplicate.

The exception type is used for one thing only — deciding whether a *further*
attempt is worth making — and never for deciding whether resubmitting is safe.

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
from typing import Any, Callable, Mapping

from .audit import EventType
from .clock import Clock, FixedClock, default_clock
from .contracts import (Fill, Instrument, Mode, Order, OrderStatus, OrderType,
                        RiskDecision, TradeIntent, Verdict, to_decimal)
from .errors import (BrokerError, BrokerUnavailable, DuplicateOrderError,
                     ExecutionError, ModeNotPermitted, TradingError)
from .oms import OrderStore, make_client_order_id

#: Statuses a *status poll* is allowed to adopt on its own.
#:
#: `FILLED` and `PARTIALLY_FILLED` are excluded on purpose. Adopting them from a
#: status message alone would move the order to a filled state while
#: `filled_quantity` stayed at zero — a local book that believes it is done
#: trading but has no position to show for it. Those two statuses may only be
#: reached by booking the fills that justify them, which `record_fill` does.
_ADOPTABLE_FROM_STATUS = frozenset({
    OrderStatus.NEW, OrderStatus.PENDING_CANCEL, OrderStatus.CANCELED,
    OrderStatus.REJECTED, OrderStatus.EXPIRED,
})


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
    #: Exponential, because a venue that just refused a connection is usually
    #: still refusing it a moment later, and a tight retry loop turns one
    #: outage into a rate-limit ban.
    retry_backoff_multiplier: float = 2.0
    max_retry_backoff_s: float = 30.0
    #: In live modes a missing reference price disables the collar entirely,
    #: which is the one place we are unwilling to fail open.
    require_reference_price_when_live: bool = True


class ExecutionEngine:
    """Drives a broker from authorised decisions. Owns no trading opinion."""

    def __init__(self, *, broker, order_store: OrderStore, portfolio=None,
                 clock: Clock | None = None, audit=None,
                 mode: Mode = Mode.PAPER,
                 config: ExecutionConfig | None = None,
                 mode_controller=None,
                 sleeper: Callable[[float], None] | None = None):
        self.broker = broker
        self.orders = order_store
        self.portfolio = portfolio
        self.clock = clock or default_clock()
        self.audit = audit
        self.mode = mode
        self.config = config or ExecutionConfig()
        self.mode_controller = mode_controller
        self._sleeper = sleeper

    # -- the guarded entry point ------------------------------------------

    def execute(self, intent: TradeIntent, decision: RiskDecision,
                instrument: Instrument, *, reference_price: Any = None) -> Order:
        """Turn one authorisation into at most one order at the venue."""
        self._verify_authorisation(intent, decision)
        mode = self._effective_mode()
        self._verify_mode_permits_orders(mode)

        # The approved quantity, never the requested one. Quantised down so the
        # venue cannot reject an off-grid size — and down rather than up because
        # rounding up would exceed the very limit that produced the number.
        quantity = instrument.quantize_quantity(decision.approved_quantity)
        if quantity <= 0:
            raise self._refuse(
                intent, "QUANTITY_ROUNDS_TO_ZERO",
                "authorised quantity rounds to zero at this instrument's lot size",
                approved=str(decision.approved_quantity),
                lot_size=str(instrument.lot_size))

        self._verify_price_collar(intent, mode, reference_price)

        coid = make_client_order_id(decision, intent)
        order = Order(
            client_order_id=coid, instrument_key=intent.instrument_key,
            side=intent.side, order_type=intent.order_type, quantity=quantity,
            created_at=self.clock.now(), status=OrderStatus.PENDING_NEW,
            limit_price=intent.limit_price,
            stop_price=self._stop_price(intent),
            time_in_force=intent.time_in_force, intent_id=intent.intent_id,
            decision_id=decision.policy_hash, strategy_id=intent.strategy_id,
            mode=mode)

        stored, created = self.orders.get_or_create(order)
        if not created:
            # A repeat of an authorisation we already acted on. Returning the
            # existing order is the whole point of the deterministic id: the
            # caller's retry becomes a lookup instead of a second trade.
            self._record(EventType.ORDER_SUBMITTED,
                         {**stored.to_dict(), "duplicate_suppressed": True},
                         intent)
            return stored

        self._record(EventType.ORDER_SUBMITTED,
                     {**stored.to_dict(), "shadow": mode is Mode.SHADOW},
                     intent)

        if mode is Mode.SHADOW:
            # Record the counterfactual; submit nothing. The order is closed
            # immediately so it can never be mistaken for live exposure by the
            # reconciler or by `cancel_stale_orders`.
            shadowed = self.orders.transition(
                coid, OrderStatus.CANCELED,
                reject_reason="shadow mode: not submitted")
            self._record(EventType.BROKER_RESPONSE,
                         {**shadowed.to_dict(), "shadow": True}, intent)
            return shadowed

        return self._submit_with_retries(stored, intent)

    # -- verification ------------------------------------------------------

    def _verify_authorisation(self, intent: TradeIntent,
                              decision: RiskDecision) -> None:
        """The gate. Everything above this line is a proposal, not an order."""
        if decision is None:
            raise self._refuse(intent, "NO_RISK_DECISION",
                               "execution requires a risk decision")
        if decision.intent_id != intent.intent_id:
            raise self._refuse(
                intent, "DECISION_INTENT_MISMATCH",
                "the decision does not authorise this intent",
                decision_intent=decision.intent_id)
        if decision.verdict is Verdict.REJECTED or not decision.approved:
            raise self._refuse(intent, "RISK_REJECTED",
                               "the risk engine rejected this intent",
                               reasons=list(decision.reason_codes))
        age = (self.clock.now() - decision.decided_at).total_seconds()
        if age > self.config.max_decision_age_s:
            raise self._refuse(
                intent, "DECISION_STALE",
                "the risk decision is stale; re-authorise before executing",
                age_s=age, limit_s=self.config.max_decision_age_s)
        if age < -1.0:
            # Tolerating a second of skew but not a decision dated into the
            # future: that is either a broken clock or a replayed record, and
            # both are reasons to stop rather than to trade.
            raise self._refuse(intent, "DECISION_IN_FUTURE",
                               "the risk decision is dated in the future",
                               age_s=age)

    def _verify_mode_permits_orders(self, mode: Mode) -> None:
        if not mode.is_live:
            return
        if self.mode_controller is None:
            raise ModeNotPermitted(
                "live execution requires a mode controller that has passed its "
                "deployment gates", mode=mode.value)
        if not self.mode_controller.permits_live_orders():
            raise ModeNotPermitted("live trading is not enabled", mode=mode.value)

    def _verify_price_collar(self, intent: TradeIntent, mode: Mode,
                             reference_price: Any) -> None:
        """Refuse to send an order into a market that has already moved.

        A collar rather than a limit price: the point is that the order is not
        sent *at all*, because an intent priced off a level the market has left
        is an intent whose reasoning no longer applies.
        """
        if reference_price is None:
            if mode.is_live and self.config.require_reference_price_when_live:
                raise self._refuse(
                    intent, "NO_REFERENCE_PRICE",
                    "live execution requires a reference price so the collar "
                    "can be evaluated")
            return
        if intent.intended_entry is None:
            return
        ref = to_decimal(reference_price, field_name="reference_price")
        entry = to_decimal(intent.intended_entry, field_name="intended_entry")
        if entry <= 0:
            return
        deviation = abs((ref - entry) / entry) * Decimal("100")
        if float(deviation) > self.config.max_price_deviation_pct:
            raise self._refuse(
                intent, "PRICE_COLLAR",
                "price collar: the market has moved too far from the intended entry",
                deviation_pct=float(deviation),
                limit_pct=self.config.max_price_deviation_pct)

    def _stop_price(self, intent: TradeIntent) -> Decimal | None:
        """The trigger price for a STOP/STOP_LIMIT order.

        Deliberately *not* `intent.stop_loss`. That field is the level at which
        the position this intent opens is considered invalidated — it sits on
        the far side of the entry from the trigger, and using it would arm the
        order to fire in the wrong direction. A stop *entry* carries its trigger
        in `metadata["stop_price"]`; if the strategy did not supply one there is
        no safe default to invent.
        """
        if intent.order_type not in (OrderType.STOP, OrderType.STOP_LIMIT):
            return None
        raw = intent.metadata.get("stop_price")
        if raw is None:
            raise self._refuse(
                intent, "NO_STOP_PRICE",
                f"a {intent.order_type.value} intent must carry its trigger in "
                "metadata['stop_price']")
        return to_decimal(raw, field_name="stop_price")

    def _effective_mode(self) -> Mode:
        """The mode actually in force.

        A controller that cannot answer is a refusal, not a fallback. The
        previous behaviour here — swallow the error and assume PAPER — is the
        classic fail-open bug: with a live adapter wired in, "assume paper"
        still submits a real order, merely mislabelled in the audit trail.
        """
        if self.mode_controller is None:
            return self.mode
        try:
            return Mode(self.mode_controller.current())
        except Exception as exc:                         # noqa: BLE001
            raise ModeNotPermitted(
                "the operating mode could not be determined; refusing to trade "
                "rather than guessing it", cause=str(exc)) from exc

    # -- submission --------------------------------------------------------

    def _submit_with_retries(self, order: Order, intent: TradeIntent) -> Order:
        attempts = max(1, int(self.config.max_submit_attempts))
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                acknowledged = self.broker.submit_order(order)
            except DuplicateOrderError as exc:
                # The venue holds a *different* order under our id. Adopting it
                # would execute terms nobody authorised, so this one is fatal:
                # our order never landed and never will under this id.
                self._settle_rejected(order, f"duplicate at venue: {exc}")
                self._record(EventType.ERROR,
                             {"stage": "submit", "code": exc.code,
                              "client_order_id": order.client_order_id,
                              "error": str(exc)}, intent)
                raise
            except (BrokerUnavailable, BrokerError) as exc:
                last_error = exc
                queried, adopted = self._query_and_adopt(order)
                if adopted is not None:
                    self._record(EventType.BROKER_RESPONSE,
                                 {**adopted.to_dict(), "adopted_after_error": exc.code},
                                 intent)
                    return adopted
                if not queried:
                    # Unknown outcome. Leave the order PENDING_NEW so recover()
                    # settles it against the venue later; anything else invents
                    # a fact we do not have.
                    self._record(EventType.ERROR,
                                 {"stage": "submit", "code": exc.code,
                                  "client_order_id": order.client_order_id,
                                  "outcome": "unknown", "error": str(exc)}, intent)
                    raise ExecutionError(
                        "submission outcome is unknown: the venue could not be "
                        "queried, so the order is left in flight for recovery",
                        client_order_id=order.client_order_id,
                        cause=str(exc)) from exc
                # Proven absent at the venue: retrying is safe.
                if isinstance(exc, BrokerError):
                    # A venue that answered and refused will refuse again;
                    # retrying only burns rate limit.
                    break
                if attempt < attempts:
                    self._wait(self._backoff(attempt))
                continue
            else:
                updated = self.orders.transition(
                    order.client_order_id, acknowledged.status,
                    **self._link(acknowledged),
                    reject_reason=acknowledged.reject_reason)
                self._record(EventType.BROKER_RESPONSE, updated.to_dict(), intent)
                return updated

        self._settle_rejected(order, f"submit failed: {last_error}")
        self._record(EventType.ERROR,
                     {"stage": "submit", "client_order_id": order.client_order_id,
                      "attempts": attempts, "error": str(last_error)}, intent)
        raise ExecutionError("order submission failed",
                             client_order_id=order.client_order_id,
                             attempts=attempts, cause=str(last_error))

    def _backoff(self, attempt: int) -> float:
        delay = self.config.retry_backoff_s * (
            self.config.retry_backoff_multiplier ** (attempt - 1))
        return min(delay, self.config.max_retry_backoff_s)

    def _wait(self, seconds: float) -> None:
        """Back off without ever reading the wall clock directly.

        Under a `FixedClock` — every backtest and every test — the wait is
        *simulated*: time advances and nothing blocks, so a retry path stays
        deterministic and instant instead of adding real seconds to a suite.
        """
        if seconds <= 0:
            return
        if self._sleeper is not None:
            self._sleeper(seconds)
            return
        if isinstance(self.clock, FixedClock):
            self.clock.advance(timedelta(seconds=seconds))
            return
        import time
        time.sleep(seconds)

    def _query(self, client_order_id: str) -> tuple[bool, Order | None]:
        """Ask the venue for an order. Returns (query_succeeded, order).

        The two failure shapes are kept apart on purpose. `(False, None)` means
        "we do not know"; `(True, None)` means "the venue says it does not have
        it". Collapsing them into a bare `None` — which is what the earlier
        version of this method did — makes an unreachable venue look exactly
        like a venue that never received the order, and that mistake is how a
        duplicate gets sent.
        """
        try:
            return True, self.broker.get_order(client_order_id)
        except Exception:                                # noqa: BLE001
            return False, None

    def _query_and_adopt(self, order: Order) -> tuple[bool, Order | None]:
        """Query, and adopt whatever the venue has. See `_query` for the tuple."""
        queried, remote = self._query(order.client_order_id)
        if not queried or remote is None:
            return queried, None
        try:
            adopted = self.orders.transition(
                order.client_order_id, remote.status, **self._link(remote),
                reject_reason=remote.reject_reason)
        except TradingError:
            # The local record has already moved somewhere the venue's status
            # cannot legally follow. Surface the current record rather than
            # forcing it: reconciliation is where that disagreement belongs.
            return True, self.orders.get(order.client_order_id)
        return True, adopted

    @staticmethod
    def _link(remote: Order) -> dict:
        """Only carry the venue id forward when there is one.

        Passing `broker_order_id=None` through `evolve` would erase an id that
        arrived on an earlier message, which is exactly the join key the
        reconciler needs.
        """
        return ({"broker_order_id": remote.broker_order_id}
                if remote.broker_order_id else {})

    def _settle_rejected(self, order: Order, reason: str) -> None:
        try:
            self.orders.transition(order.client_order_id, OrderStatus.REJECTED,
                                   reject_reason=reason)
        except TradingError:                             # already terminal
            pass

    # -- lifecycle ---------------------------------------------------------

    def poll(self, since: datetime | None = None) -> list[Fill]:
        """Pull fills and statuses from the venue and apply them locally.

        Idempotent at two levels, and it needs to be at both: the OMS refuses a
        `fill_id` it has already stored, and the portfolio refuses one it has
        already booked. The check here is a third — it is what stops the
        *return value* from claiming a redelivered fill was newly applied, which
        a caller updating a UI or a metric would believe.
        """
        applied: list[Fill] = []
        try:
            fills = self.broker.get_fills(since=since)
        except Exception as exc:                         # noqa: BLE001
            self._record(EventType.ERROR, {"stage": "poll", "error": str(exc)},
                         None)
            fills = []

        for fill in fills:
            if self.orders.fill(fill.fill_id) is not None:
                continue                                 # redelivery
            try:
                self.orders.record_fill(fill)
            except TradingError as exc:
                # Unknown order, wrong instrument, or an over-fill. All three
                # mean local and remote disagree about something structural,
                # which is reconciliation's problem, not the poller's.
                self._record(EventType.ERROR,
                             {"stage": "poll", "fill_id": fill.fill_id,
                              "code": exc.code, "error": str(exc)}, None)
                continue
            if self.portfolio is not None:
                self.portfolio.apply_fill(fill)
            applied.append(fill)
            self._record(EventType.FILL, fill.to_dict(), None,
                         subject=fill.instrument_key)

        self._sync_open_statuses()
        return applied

    def _sync_open_statuses(self) -> None:
        """Adopt venue statuses for orders we still believe are open.

        Runs *after* fills are booked, and only for statuses that do not imply
        a quantity (see `_ADOPTABLE_FROM_STATUS`). A `FILLED` arriving here
        would otherwise race the fill that justifies it and leave the order
        filled-with-nothing.
        """
        for order in self.orders.open_orders():
            queried, remote = self._query(order.client_order_id)
            if not queried or remote is None:
                continue
            if remote.status is order.status:
                continue
            if remote.status not in _ADOPTABLE_FROM_STATUS:
                continue
            try:
                updated = self.orders.transition(
                    order.client_order_id, remote.status, **self._link(remote),
                    reject_reason=remote.reject_reason)
            except TradingError:
                continue
            self._record(EventType.BROKER_RESPONSE, updated.to_dict(), None,
                         subject=updated.instrument_key)

    def recover(self) -> list[Order]:
        """Startup reconciliation of in-flight orders.

        A `PENDING_NEW` order after a crash is the ambiguous case: we may or may
        not have submitted it. Querying by the deterministic id resolves it
        without risking a duplicate — and an order the venue *does* know about
        is adopted rather than re-sent, which is the entire point.

        An order the venue reports as filled is recovered through its fills, not
        by forcing the status: the position has to come from somewhere.
        """
        recovered: list[Order] = []
        for order in self.orders.open_orders():
            queried, remote = self._query(order.client_order_id)
            if not queried:
                self._record(EventType.ERROR,
                             {"stage": "recover", "outcome": "unreachable",
                              "client_order_id": order.client_order_id}, None)
                continue
            if remote is None:
                # The venue does not have it. We leave it alone rather than
                # cancelling it locally: "not found" from a venue that is only
                # eventually consistent is not proof of absence, and a local
                # cancel would free the risk budget for a position we may
                # actually hold.
                self._record(EventType.ERROR,
                             {"stage": "recover", "outcome": "unknown_at_venue",
                              "client_order_id": order.client_order_id,
                              "status": order.status.value}, None)
                continue
            if remote.status in (OrderStatus.FILLED,
                                 OrderStatus.PARTIALLY_FILLED):
                self._adopt_fills_for(order.client_order_id)
            elif remote.status is not order.status:
                try:
                    self.orders.transition(order.client_order_id, remote.status,
                                           **self._link(remote),
                                           reject_reason=remote.reject_reason)
                except TradingError:
                    continue
            elif remote.broker_order_id and not order.broker_order_id:
                try:
                    self.orders.link_broker_id(order.client_order_id,
                                               remote.broker_order_id)
                except TradingError:
                    continue
            current = self.orders.get(order.client_order_id)
            if current is not None and current != order:
                recovered.append(current)
                self._record(EventType.BROKER_RESPONSE,
                             {**current.to_dict(), "recovered": True}, None,
                             subject=current.instrument_key)
        return recovered

    def _adopt_fills_for(self, client_order_id: str) -> None:
        try:
            fills = self.broker.get_fills()
        except Exception:                                # noqa: BLE001
            return
        for fill in fills:
            if fill.client_order_id != client_order_id:
                continue
            if self.orders.fill(fill.fill_id) is not None:
                continue
            try:
                self.orders.record_fill(fill)
            except TradingError:
                continue
            if self.portfolio is not None:
                self.portfolio.apply_fill(fill)

    def cancel_stale_orders(self, max_age: timedelta) -> list[Order]:
        """Cancel open orders older than `max_age`.

        A resting order outlives the analysis that produced it. Left alone it
        becomes a standing instruction to trade on a view nobody holds any more,
        which is how a strategy that has been switched off still loses money.
        """
        cutoff = self.clock.now() - max_age
        cancelled: list[Order] = []
        for order in self.orders.open_orders():
            if order.created_at > cutoff:
                continue
            try:
                remote = self.broker.cancel_order(order.client_order_id)
            except Exception as exc:                     # noqa: BLE001
                self._record(EventType.ERROR,
                             {"stage": "cancel_stale",
                              "client_order_id": order.client_order_id,
                              "error": str(exc)}, None)
                continue
            try:
                updated = self.orders.transition(order.client_order_id,
                                                 remote.status,
                                                 **self._link(remote))
            except TradingError:
                continue
            cancelled.append(updated)
            self._record(EventType.BROKER_RESPONSE,
                         {**updated.to_dict(), "cancelled_stale": True}, None,
                         subject=updated.instrument_key)
        return cancelled

    # -- audit -------------------------------------------------------------

    def _refuse(self, intent: TradeIntent, code: str, message: str,
                **details) -> ExecutionError:
        """Build a refusal, recording it first.

        Refusals are recorded, not just raised: "why did we not trade" is only
        answerable later if the *absence* of an order left a trace.
        """
        self._record(EventType.ERROR,
                     {"stage": "authorise", "reason_code": code,
                      "message": message, "intent_id": intent.intent_id,
                      **{k: _plain(v) for k, v in details.items()}}, intent)
        return ExecutionError(message, intent_id=intent.intent_id,
                              reason_code=code, **details)

    def _record(self, event: Any, payload: Mapping[str, Any],
                intent: TradeIntent | None, *, subject: str = "") -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(
                event, dict(payload), actor="execution",
                subject=(intent.instrument_key if intent else subject),
                correlation_id=(intent.intent_id if intent else ""))
        except Exception:                                # noqa: BLE001
            # An audit backend that is down must not stop an order that is
            # already at the venue from being recorded locally.
            pass


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value


__all__ = ["ExecutionEngine", "ExecutionConfig"]
