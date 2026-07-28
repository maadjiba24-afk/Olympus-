"""Olympus-native market intelligence.

An Olympus-owned forecasting system: independently designed components, an
Olympus-controlled training pipeline, and checkpoints trained from Olympus
datasets. Kronos is a benchmark and a replaceable provider, not an ancestor —
`tests/test_trading_independence.py` proves nothing here reaches it.

What exists (phase P2)
----------------------
The plumbing, and a real but simple estimator to run through it:

* `state`      — `MarketState`: typed, causal, multi-scale observation
* `data`       — windowing and the embargoed temporal split
* `quantile`   — direct multi-horizon conditional quantile estimation
* `checkpoint` — Olympus checkpoints and the manifest that makes them claimable
* `train`      — the pipeline, in the one order that is safe
* `forecaster` — `forecast.Forecaster` implementation; the plug point

What does not exist
-------------------
The neural encoder and trunk (`docs/OLYMPUS_NATIVE_MARKET_INTELLIGENCE.md`
§3.2–3.3), the cross-asset and multi-timeframe modelling, the regime,
volatility, liquidity and event heads, conformal calibration, and the
full out-of-distribution detector. `docs/OLYMPUS_NATIVE_MODEL_STATUS.md` is the
ledger; when it and this docstring disagree, the ledger is right.

**Olympus does not own a trained market model.** The estimator here has been
fitted only to synthetic series in tests, because no market data is reachable
from this environment (`docs/TRADING_EXTERNAL_VALIDATION.md` §1). It is
plumbing that works, not a model that knows anything.

Import discipline
-----------------
Pure stdlib, like the rest of the trading core. The neural work in a later phase
imports torch lazily behind a `native` extra and raises
`errors.DependencyMissing` when it is absent; nothing in this package does so
today, which is why `tests/test_deps_claim.py` stays green.
"""

from __future__ import annotations

_LAZY = {
    "checkpoint": ".checkpoint",
    "data": ".data",
    "forecaster": ".forecaster",
    "quantile": ".quantile",
    "state": ".state",
    "train": ".train",
}


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    module = importlib.import_module(target, __name__)
    globals()[name] = module
    return module


def __dir__():                                           # pragma: no cover
    return sorted(set(globals()) | set(_LAZY))


__all__ = sorted(_LAZY)
