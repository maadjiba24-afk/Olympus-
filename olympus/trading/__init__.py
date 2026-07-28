"""Olympus trading domain — market intelligence and algorithmic trading.

Kronos is *one forecasting component* inside this domain. It is not a strategy,
not a risk system, and not an execution engine. Nothing in this package lets a
model — language, forecasting, or otherwise — put an order on a venue: every
order is authorised by the deterministic, non-LLM risk engine in `risk.py`
first, and the path from market data to a filled order runs through seven
separable layers:

    ingest → validate → features/forecast → signals → strategy
           → risk authorisation → execution → reconciliation

Import discipline
-----------------
The core of this package is **pure stdlib**. Olympus ships with three required
dependencies and that is a property worth keeping, so contracts, validation,
technical analysis, risk, the OMS, the paper broker, and the backtester all
import and run with nothing extra installed.

Heavy or optional backends are imported lazily *inside* the functions that need
them and raise `errors.DependencyMissing` when absent:

    * `kronos_adapter` / `kronos_runtime` — torch, numpy (extra: ``kronos``)
    * broker SDKs — per adapter

Accordingly this module exposes contracts and errors eagerly and everything
else through `__getattr__`, so `import olympus.trading` stays cheap and never
drags in torch.
"""

from __future__ import annotations

from . import contracts, errors
from .clock import Clock, FixedClock, default_clock
from .contracts import (  # noqa: F401  (re-exported for callers)
    SCHEMA_VERSION, AccountSnapshot, Candle, DataQuality, DataQualityReport,
    Fill, ForecastPath, ForecastResult, Instrument, Mode, Order, OrderStatus,
    OrderType, PortfolioSnapshot, Position, Quote, Regime, RiskCheck,
    RiskDecision, Side, Signal, SignalDirection, TimeInForce, TradeIntent,
    Verdict,
)
from .errors import TradingError  # noqa: F401

_LAZY = {
    "audit": ".audit",
    "backtest": ".backtest",
    "brokers": ".brokers",
    "candles": ".candles",
    "capabilities": ".capabilities",
    "champion": ".champion",
    "drift": ".drift",
    "evaluate": ".evaluate",
    "evolution": ".evolution",
    "execution": ".execution",
    "features": ".features",
    "forecast": ".forecast",
    "governance": ".governance",
    "hypotheses": ".hypotheses",
    "ingest": ".ingest",
    "instruments": ".instruments",
    "kernel": ".kernel",
    "killswitch": ".killswitch",
    "knowledge": ".knowledge",
    "kronos_adapter": ".kronos_adapter",
    "kronos_runtime": ".kronos_runtime",
    "lab": ".lab",
    "modes": ".modes",
    "monitor": ".monitor",
    "oms": ".oms",
    "outcomes": ".outcomes",
    "perf": ".perf",
    "portfolio": ".portfolio",
    "proposals": ".proposals",
    "reconcile": ".reconcile",
    "regime": ".regime",
    "registry": ".registry",
    "risk": ".risk",
    "rollback": ".rollback",
    "sentiment": ".sentiment",
    "signals": ".signals",
    "storage": ".storage",
    "strategy": ".strategy",
    "ta": ".ta",
    "validate": ".validate",
    "volatility": ".volatility",
}


def __getattr__(name: str):
    """Lazily import submodules so `import olympus.trading` stays cheap."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    module = importlib.import_module(target, __name__)
    globals()[name] = module
    return module


def __dir__():  # pragma: no cover - introspection helper
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    "contracts", "errors", "Clock", "FixedClock", "default_clock",
    "TradingError", "SCHEMA_VERSION",
    "Instrument", "Candle", "Quote", "DataQualityReport", "DataQuality",
    "ForecastResult", "ForecastPath", "Signal", "SignalDirection",
    "TradeIntent", "RiskCheck", "RiskDecision", "Verdict", "Order",
    "OrderStatus", "OrderType", "Side", "TimeInForce", "Fill", "Position",
    "AccountSnapshot", "PortfolioSnapshot", "Mode", "Regime",
    *sorted(_LAZY),
]
