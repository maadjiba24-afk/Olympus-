"""Structured error taxonomy for the Olympus trading domain.

Every failure in this domain is a *typed* error carrying a stable machine
readable `code`, never a bare string. Three reasons:

1. **Audit.** Every rejection is written to the immutable trail (`audit.py`);
   a stable code is what makes "why did we not trade on 2026-03-14" answerable
   a year later without re-reading source.
2. **Determinism.** The risk engine's decisions are asserted in tests against
   codes, not prose. Prose can be reworded; codes cannot drift silently.
3. **Never silently corrupt.** The Kronos teardown (docs/KRONOS_TEARDOWN.md
   §12.2) found the upstream tokenizer *silently* producing wrong numbers on an
   unsupported configuration. The adapter here raises `ConfigurationError`
   instead. That is the single most important rule in this package: an
   unsupported or unsafe condition MUST raise or abstain, never return a
   plausible-looking wrong answer.

Codes are `UPPER_SNAKE` and permanently stable once shipped.
"""

from __future__ import annotations


class TradingError(Exception):
    """Base of every error raised by `olympus.trading`.

    Carries a stable `code` plus an arbitrary `details` mapping that is safe to
    serialise into the audit trail (values must be JSON-encodable).
    """

    code = "TRADING_ERROR"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "details": dict(self.details)}

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.details:
            extra = ", ".join(f"{k}={v!r}" for k, v in sorted(self.details.items()))
            return f"[{self.code}] {self.message} ({extra})"
        return f"[{self.code}] {self.message}"


# --- configuration / wiring -------------------------------------------------

class ConfigurationError(TradingError):
    """A configuration is unsupported, contradictory, or unsafe.

    Raised eagerly at construction time rather than tolerated. This is what
    turns the upstream Kronos `s1_bits != s2_bits` silent-corruption bug into a
    loud, testable failure.
    """
    code = "CONFIGURATION_INVALID"


class DependencyMissing(TradingError):
    """An optional backend (torch, numpy, a broker SDK) is not installed.

    The trading domain's *core* — contracts, validation, risk, OMS, paper
    broker, backtester — is pure stdlib and must import and run without any of
    these. Only the Kronos adapter needs torch.
    """
    code = "DEPENDENCY_MISSING"


# --- market data ------------------------------------------------------------

class MarketDataError(TradingError):
    code = "MARKET_DATA_ERROR"


class DataValidationError(MarketDataError):
    """Incoming market data failed a structural or semantic check."""
    code = "DATA_VALIDATION_FAILED"


class StaleDataError(MarketDataError):
    """The freshest datum is older than the configured tolerance.

    Trading on stale data is one of the fastest ways to lose money in a fast
    market, so this is a hard stop, not a warning.
    """
    code = "DATA_STALE"


class InsufficientDataError(MarketDataError):
    """Fewer usable bars than the caller requires (e.g. shorter than context)."""
    code = "DATA_INSUFFICIENT"


# --- forecasting ------------------------------------------------------------

class ForecastError(TradingError):
    code = "FORECAST_ERROR"


class ModelNotAvailable(ForecastError):
    """No usable checkpoint: not downloaded, not pinned, or failed verification."""
    code = "MODEL_UNAVAILABLE"


class CheckpointVerificationError(ForecastError):
    """A checkpoint's identity did not match the pinned expectation.

    Loading an unpinned or mismatched checkpoint means forecasts are not
    reproducible and the audit trail's `model_version` is a lie, so this is
    fatal rather than a warning.
    """
    code = "CHECKPOINT_VERIFICATION_FAILED"


class IncompatibleModelError(ForecastError):
    """Tokenizer and predictor disagree on vocabulary or feature geometry."""
    code = "MODEL_INCOMPATIBLE"


class ForecastAbstained(ForecastError):
    """The forecaster declined to produce a forecast.

    Not a bug — the designed response to unreliable input or an unreliable
    output. Callers receive a `ForecastResult` with `abstained=True` in the
    normal path; this exception exists for callers that opt into strict mode.
    """
    code = "FORECAST_ABSTAINED"


# --- risk -------------------------------------------------------------------

class RiskError(TradingError):
    code = "RISK_ERROR"


class RiskRejection(RiskError):
    """A trade intent failed one or more deterministic risk checks."""
    code = "RISK_REJECTED"


class KillSwitchActive(RiskError):
    """A kill switch is engaged; no new risk may be taken."""
    code = "KILL_SWITCH_ACTIVE"


class LimitsImmutableError(RiskError):
    """Something tried to mutate risk limits through a forbidden path.

    Risk limits are operator-owned. Analysis agents, strategies, and anything
    derived from external content are structurally barred from changing them;
    an attempt is a security event, not a validation error.
    """
    code = "LIMITS_IMMUTABLE"


# --- execution --------------------------------------------------------------

class ExecutionError(TradingError):
    code = "EXECUTION_ERROR"


class BrokerError(ExecutionError):
    """The broker rejected, errored, or behaved unexpectedly."""
    code = "BROKER_ERROR"


class BrokerUnavailable(ExecutionError):
    """The broker is unreachable or unauthenticated."""
    code = "BROKER_UNAVAILABLE"


class DuplicateOrderError(ExecutionError):
    """An order with this idempotency key already exists.

    Surfaced rather than swallowed so the caller can decide between "this is a
    safe retry, give me the existing order" and "this is a bug".
    """
    code = "ORDER_DUPLICATE"


class ReconciliationError(ExecutionError):
    """Local state and broker state disagree beyond tolerance."""
    code = "RECONCILIATION_FAILED"


# --- modes / gating ---------------------------------------------------------

class ModeError(TradingError):
    code = "MODE_ERROR"


class ModeNotPermitted(ModeError):
    """The requested operating mode is not enabled or its gates did not pass."""
    code = "MODE_NOT_PERMITTED"


class GateFailure(ModeError):
    """A deployment gate failed; live trading stays disabled."""
    code = "GATE_FAILED"


# --- knowledge / self-evolution ---------------------------------------------

class KnowledgeError(TradingError):
    """A knowledge record is malformed, or was used outside its warrant."""
    code = "KNOWLEDGE_ERROR"


class EvolutionError(TradingError):
    """Base of the controlled-self-evolution failures."""
    code = "EVOLUTION_ERROR"


class SafetyKernelViolation(EvolutionError):
    """Something in the evolution machinery reached for the safety kernel.

    The kernel — risk policy, kill switches, the vault, live-mode gates, the
    audit ledger, the permission system — is the set of components Olympus may
    *recommend* changes to and may never *apply* changes to. An attempt is a
    security event, not a validation error, so it raises rather than returning
    a rejection an autonomous caller could ignore.
    """
    code = "SAFETY_KERNEL_VIOLATION"


class GovernanceViolation(EvolutionError):
    """An autonomous actor attempted an action reserved for a human operator.

    Distinct from `SafetyKernelViolation`: the kernel violation is about
    *what* was touched, this is about *who* touched it. Promoting a capability
    is a legitimate operation — performed by an operator. Performed by the
    evolution engine itself it is a governance breach.
    """
    code = "GOVERNANCE_VIOLATION"


class PromotionDenied(EvolutionError):
    """A capability, model or strategy did not earn the state it asked for.

    Carries the specific unmet evidence requirements, because "denied" without
    the missing evidence is an argument rather than a decision.
    """
    code = "PROMOTION_DENIED"


class ExperimentError(EvolutionError):
    """A research experiment could not be run, or tried to escape the sandbox."""
    code = "EXPERIMENT_ERROR"


__all__ = [
    "TradingError", "ConfigurationError", "DependencyMissing",
    "MarketDataError", "DataValidationError", "StaleDataError",
    "InsufficientDataError", "ForecastError", "ModelNotAvailable",
    "CheckpointVerificationError", "IncompatibleModelError",
    "ForecastAbstained", "RiskError", "RiskRejection", "KillSwitchActive",
    "LimitsImmutableError", "ExecutionError", "BrokerError",
    "BrokerUnavailable", "DuplicateOrderError", "ReconciliationError",
    "ModeError", "ModeNotPermitted", "GateFailure",
    "KnowledgeError", "EvolutionError", "SafetyKernelViolation",
    "GovernanceViolation", "PromotionDenied", "ExperimentError",
]
