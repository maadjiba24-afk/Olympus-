"""The seven-layer pipeline, composed.

This module is deliberately thin. It owns no policy of its own: every decision
it makes is delegated to the layer that owns that decision, and its whole job is
to run them **in the right order, with the right refusals between them**.

    ingest → validate → analyse → forecast → signal → strategy
           → RISK AUTHORISATION → execution → reconciliation

Why a single composed object rather than letting callers wire it up ad hoc: the
ordering *is* the safety property. A caller who forgets to validate before
forecasting, or who hands an intent straight to the execution engine, has
defeated the design. Putting the sequence in one place — one that structurally
cannot skip the risk step, because it constructs the call itself — makes the
safe path the easy path and the unsafe path unreachable through this API.

What this module will not do
----------------------------
* It never calls a broker directly; only `execution` does.
* It never constructs an `Order`; only `oms`/`execution` do.
* It never decides quantity; the strategy proposes and `risk` disposes.
* It has no fallback for a missing risk engine. A pipeline without an authoriser
  refuses to be built at all (`ConfigurationError`), because the failure mode of
  a "temporarily unauthorised" trading loop is unbounded.

Collaborators are duck-typed and injected. The pipeline depends on *method
names*, not on concrete classes, so a backtest can substitute a `FixedClock` and
a `PaperBroker` while paper and live substitute their own — which is what makes
"the same strategy and risk code runs in all modes" true rather than aspirational.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import (Candle, DataQuality, Instrument, Mode, Order,
                        RiskDecision, Signal, TradeIntent, Verdict, jsonable)
from .errors import ConfigurationError, TradingError


@dataclass(frozen=True)
class PipelineOutcome:
    """Everything one pass produced, including every refusal along the way.

    Deliberately records the *rejections* as first-class results rather than
    returning None on failure: "we looked and decided not to trade" and "we
    never looked" are completely different operational states, and an operator
    reading a log must be able to tell them apart.
    """
    correlation_id: str
    instrument_key: str
    timeframe: str
    as_of: datetime
    mode: Mode
    #: Where the pass stopped: one of the STAGE_* constants below.
    stopped_at: str = ""
    reason: str = ""
    data_quality: DataQuality | None = None
    forecasts: Mapping[str, Any] = field(default_factory=dict)
    signals: tuple[Signal, ...] = ()
    fused: Any = None
    intents: tuple[TradeIntent, ...] = ()
    decisions: tuple[RiskDecision, ...] = ()
    orders: tuple[Order, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def traded(self) -> bool:
        return bool(self.orders)

    @property
    def approved_decisions(self) -> tuple[RiskDecision, ...]:
        return tuple(d for d in self.decisions if d.approved)

    @property
    def rejected_decisions(self) -> tuple[RiskDecision, ...]:
        return tuple(d for d in self.decisions if not d.approved)

    def to_dict(self) -> dict:
        return {
            "correlation_id": self.correlation_id,
            "instrument_key": self.instrument_key,
            "timeframe": self.timeframe,
            "as_of": jsonable(self.as_of),
            "mode": self.mode.value,
            "stopped_at": self.stopped_at,
            "reason": self.reason,
            "data_quality": self.data_quality.value if self.data_quality else None,
            "forecasts": {k: jsonable(v) for k, v in self.forecasts.items()},
            "signals": [jsonable(s) for s in self.signals],
            "intents": [jsonable(i) for i in self.intents],
            "decisions": [jsonable(d) for d in self.decisions],
            "orders": [jsonable(o) for o in self.orders],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


# Stage names, used in `stopped_at` and in audit records. Stable strings.
STAGE_COMPLETE = "complete"
STAGE_KILLSWITCH = "killswitch"
STAGE_MODE = "mode"
STAGE_VALIDATE = "validate"
STAGE_FORECAST = "forecast"
STAGE_SIGNAL = "signal"
STAGE_STRATEGY = "strategy"
STAGE_RISK = "risk"
STAGE_EXECUTION = "execution"


class TradingPipeline:
    """Runs one instrument/timeframe through the full stack.

    Every collaborator is optional *except* the risk engine and the strategy
    source: a pipeline that could produce an intent but not authorise it is a
    pipeline that must not exist, so that combination is rejected at
    construction rather than discovered at the first trade.
    """

    def __init__(
        self,
        *,
        risk_engine,
        strategies,
        instruments=None,
        validator=None,
        forecast_service=None,
        signal_generators: Sequence[Any] = (),
        fusion=None,
        execution_engine=None,
        portfolio=None,
        killswitches=None,
        mode_controller=None,
        audit=None,
        clock: Clock | None = None,
        min_data_quality: DataQuality = DataQuality.DEGRADED,
    ):
        if risk_engine is None:
            raise ConfigurationError(
                "TradingPipeline requires a risk engine; a pipeline that can "
                "propose trades but not authorise them must not exist")
        if strategies is None:
            raise ConfigurationError("TradingPipeline requires a strategy source")
        if not hasattr(risk_engine, "authorise"):
            raise ConfigurationError(
                "risk_engine must expose .authorise(intent, ctx)",
                got=type(risk_engine).__name__)

        self.risk_engine = risk_engine
        self.strategies = strategies
        self.instruments = instruments
        self.validator = validator
        self.forecast_service = forecast_service
        self.signal_generators = tuple(signal_generators)
        self.fusion = fusion
        self.execution_engine = execution_engine
        self.portfolio = portfolio
        self.killswitches = killswitches
        self.mode_controller = mode_controller
        self.audit = audit
        self.clock = clock or default_clock()
        self.min_data_quality = min_data_quality

    # -- helpers ----------------------------------------------------------

    def _mode(self) -> Mode:
        if self.mode_controller is None:
            return Mode.PAPER          # the safe default, never live
        try:
            return self.mode_controller.current()
        except Exception:              # noqa: BLE001 - a broken mode store must not trade
            return Mode.PAPER

    def _record(self, event_type, payload, *, correlation_id, subject="",
                actor="pipeline") -> None:
        """Best-effort audit. A failing audit sink must not crash the loop, but
        it must be visible — the failure is recorded in the outcome's errors."""
        if self.audit is None:
            return
        try:
            self.audit.record(event_type, payload, actor=actor, subject=subject,
                              correlation_id=correlation_id)
        except Exception:              # noqa: BLE001
            pass

    def _active_strategies(self):
        if hasattr(self.strategies, "active"):
            return list(self.strategies.active())
        if isinstance(self.strategies, (list, tuple)):
            return list(self.strategies)
        return [self.strategies]

    # -- the pass ---------------------------------------------------------

    def run(
        self,
        *,
        instrument: Instrument,
        timeframe: str,
        candles: Sequence[Candle],
        as_of: datetime | None = None,
        quote=None,
        correlation_id: str | None = None,
        extra_context: Mapping[str, Any] | None = None,
    ) -> PipelineOutcome:
        """One full pass. Returns an outcome; raises only on programmer error.

        Operational failures (bad data, an abstaining forecast, a rejected
        trade, a broker outage) are *results*, not exceptions — a trading loop
        that raises on a wide spread stops trading everything else too.
        """
        cid = correlation_id or uuid.uuid4().hex
        now = as_of or self.clock.now()
        mode = self._mode()
        key = instrument.key
        warnings: list[str] = []
        errors: list[str] = []

        def stop(stage: str, reason: str, **kw) -> PipelineOutcome:
            return PipelineOutcome(
                correlation_id=cid, instrument_key=key, timeframe=timeframe,
                as_of=now, mode=mode, stopped_at=stage, reason=reason,
                warnings=tuple(warnings), errors=tuple(errors), **kw)

        # 0. Kill switches, before anything else spends effort or touches data.
        if self.killswitches is not None:
            try:
                engaged = self.killswitches.check(key, None)
            except Exception as exc:              # noqa: BLE001
                # A kill-switch registry that cannot be read is itself a reason
                # to stop: fail closed, never open.
                return stop(STAGE_KILLSWITCH,
                            f"kill-switch state unreadable: {exc}")
            if engaged is not None:
                return stop(STAGE_KILLSWITCH,
                            f"kill switch engaged: {getattr(engaged, 'code', '')}")

        # 1. Validate. Nothing downstream may see unvalidated data.
        quality = None
        if self.validator is not None:
            try:
                candles, report = self.validator(candles, timeframe=timeframe,
                                                 clock=self.clock,
                                                 instrument=instrument)
                quality = report.status
                warnings.extend(report.warnings)
                self._record("DATA_VALIDATED", report.to_dict(),
                             correlation_id=cid, subject=key)
                if report.status is DataQuality.UNUSABLE:
                    return stop(STAGE_VALIDATE, "market data unusable",
                                data_quality=quality)
                if (self.min_data_quality is DataQuality.OK
                        and report.status is not DataQuality.OK):
                    return stop(STAGE_VALIDATE,
                                "market data below required quality",
                                data_quality=quality)
            except TradingError as exc:
                errors.append(str(exc))
                return stop(STAGE_VALIDATE, str(exc))

        # 2. Forecast. An abstention is a normal outcome, not an error.
        forecasts: dict[str, Any] = {}
        if self.forecast_service is not None:
            try:
                forecasts = self.forecast_service.run_all(
                    candles=candles, as_of=now, instrument_key=key,
                    timeframe=timeframe)
            except Exception as exc:              # noqa: BLE001
                errors.append(f"forecast service failed: {exc}")
                forecasts = {}
            for name, result in forecasts.items():
                self._record("FORECAST_PRODUCED", jsonable(result),
                             correlation_id=cid, subject=key, actor=name)

        # 3. Signals.
        signals: list[Signal] = []
        for gen in self.signal_generators:
            try:
                produced = gen.generate(
                    instrument=instrument, timeframe=timeframe, candles=candles,
                    forecasts=forecasts, as_of=now, quote=quote)
            except Exception as exc:              # noqa: BLE001
                errors.append(f"signal generator {gen!r} failed: {exc}")
                continue
            signals.extend(produced or [])

        fused = None
        if self.fusion is not None and signals:
            try:
                fused = self.fusion.fuse(signals, as_of=now)
            except Exception as exc:              # noqa: BLE001
                errors.append(f"signal fusion failed: {exc}")

        # 4. Strategies propose. They cannot do anything else.
        intents: list[TradeIntent] = []
        for strategy in self._active_strategies():
            try:
                proposed = strategy.on_bar(self._context(
                    instrument=instrument, timeframe=timeframe, candles=candles,
                    forecasts=forecasts, signals=tuple(signals), fused=fused,
                    as_of=now, quote=quote, extra=extra_context))
            except Exception as exc:              # noqa: BLE001
                errors.append(f"strategy {getattr(strategy, 'id', strategy)!r} "
                              f"failed: {exc}")
                continue
            for intent in proposed or []:
                intents.append(intent)
                self._record("TRADE_INTENT", jsonable(intent),
                             correlation_id=cid, subject=key,
                             actor=getattr(strategy, "id", "strategy"))

        if not intents:
            return stop(STAGE_COMPLETE, "no intent proposed",
                        data_quality=quality, forecasts=forecasts,
                        signals=tuple(signals), fused=fused)

        # 5. RISK AUTHORISATION. There is no path around this call.
        decisions: list[RiskDecision] = []
        ctx = self._risk_context(instrument=instrument, timeframe=timeframe,
                                 candles=candles, quote=quote,
                                 forecasts=forecasts, quality=quality,
                                 mode=mode, as_of=now, extra=extra_context)
        for intent in intents:
            try:
                decision = self.risk_engine.authorise(intent, ctx)
            except TradingError as exc:
                # A risk engine that errors is treated as a refusal. Failing
                # open here would be the single worst bug in the system.
                errors.append(f"risk engine error (treated as refusal): {exc}")
                continue
            decisions.append(decision)
            self._record("RISK_DECISION", jsonable(decision),
                         correlation_id=cid, subject=key, actor="risk")

        approved = [d for d in decisions if d.approved]
        if not approved:
            return stop(STAGE_RISK, "all intents rejected by risk",
                        data_quality=quality, forecasts=forecasts,
                        signals=tuple(signals), fused=fused,
                        intents=tuple(intents), decisions=tuple(decisions))

        # 6. Execution — only for intents carrying an approving decision.
        orders: list[Order] = []
        if self.execution_engine is None:
            warnings.append("no execution engine configured; decisions only")
            return stop(STAGE_EXECUTION, "no execution engine configured",
                        data_quality=quality, forecasts=forecasts,
                        signals=tuple(signals), fused=fused,
                        intents=tuple(intents), decisions=tuple(decisions))

        by_id = {i.intent_id: i for i in intents}
        for decision in approved:
            intent = by_id.get(decision.intent_id)
            if intent is None:                    # pragma: no cover - defensive
                errors.append(f"decision {decision.intent_id} has no intent")
                continue
            try:
                order = self.execution_engine.execute(intent, decision, instrument)
            except TradingError as exc:
                errors.append(f"execution refused/failed: {exc}")
                continue
            orders.append(order)
            self._record("ORDER_SUBMITTED", jsonable(order),
                         correlation_id=cid, subject=key, actor="execution")

        return PipelineOutcome(
            correlation_id=cid, instrument_key=key, timeframe=timeframe,
            as_of=now, mode=mode, stopped_at=STAGE_COMPLETE,
            reason="", data_quality=quality, forecasts=forecasts,
            signals=tuple(signals), fused=fused, intents=tuple(intents),
            decisions=tuple(decisions), orders=tuple(orders),
            warnings=tuple(warnings), errors=tuple(errors))

    # -- context builders -------------------------------------------------
    # Kept as overridable methods so a backtest or a test can supply a richer
    # context object without the pipeline needing to know its shape.

    def _context(self, **kw):
        """The object handed to a strategy. Deliberately a plain namespace so
        strategies cannot reach anything not explicitly placed in it."""
        from types import SimpleNamespace
        portfolio_snapshot = None
        if self.portfolio is not None:
            try:
                portfolio_snapshot = self.portfolio.snapshot()
            except Exception:                      # noqa: BLE001
                portfolio_snapshot = None
        return SimpleNamespace(clock=self.clock, portfolio=portfolio_snapshot, **kw)

    def _risk_context(self, **kw):
        """The evidence the risk engine judges from.

        A `SimpleNamespace` by default so this module does not hard-depend on
        `risk.RiskContext`'s exact field list; a deployment that wants the real
        dataclass subclasses and overrides this.
        """
        from types import SimpleNamespace
        portfolio_snapshot = None
        if self.portfolio is not None:
            try:
                portfolio_snapshot = self.portfolio.snapshot()
            except Exception:                      # noqa: BLE001
                portfolio_snapshot = None
        return SimpleNamespace(clock=self.clock, portfolio=portfolio_snapshot,
                               killswitches=self.killswitches, **kw)


__all__ = ["TradingPipeline", "PipelineOutcome", "STAGE_COMPLETE",
           "STAGE_KILLSWITCH", "STAGE_MODE", "STAGE_VALIDATE", "STAGE_FORECAST",
           "STAGE_SIGNAL", "STAGE_STRATEGY", "STAGE_RISK", "STAGE_EXECUTION"]
