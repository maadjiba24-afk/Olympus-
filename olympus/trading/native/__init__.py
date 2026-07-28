"""Olympus-native market intelligence.

An Olympus-owned forecasting system: independently designed components, an
Olympus-controlled training pipeline, and checkpoints trained from Olympus
datasets. Kronos is a benchmark and a replaceable provider, not an ancestor —
`tests/test_trading_independence.py` proves nothing here reaches it.

What exists (phases P2–P3)
--------------------------
The pipeline, and two estimators to run through it:

* `state`      — `MarketState`: typed, causal, multi-scale observation
* `data`       — windowing and the embargoed temporal split
* `quantile`   — conditional quantiles by lookup; the cheap baseline
* `encoder`    — continuous patch projection; no codebook, no vocabulary
* `trunk`      — dilated **causal** convolutions, non-autoregressive
* `neural`     — the learned monotone quantile ladder, trained by pinball loss
* `torchutil`  — the torch boundary: lazy import, seeded determinism
* `checkpoint` — Olympus checkpoints and the manifest that makes them claimable
* `train`      — the pipeline, in the one order that is safe
* `forecaster` — `forecast.Forecaster` implementation; the plug point

What does not exist
-------------------
Cross-asset and multi-timeframe modelling, the regime, volatility, liquidity and
event heads, conformal calibration, and the full out-of-distribution detector.
`docs/OLYMPUS_NATIVE_MODEL_STATUS.md` is the ledger; when it and this docstring
disagree, the ledger is right.

**Olympus does not own a trained market model.** The estimators here have been
fitted only to synthetic series in tests, because no market data is reachable
from this environment (`docs/TRADING_EXTERNAL_VALIDATION.md` §1). P3 showed the
network recovers a structure that is genuinely there and correctly fails to beat
persistence on white noise — evidence about the pipeline, not about markets.

Import discipline
-----------------
Everything except `encoder`, `trunk`, `neural` and `torchutil` is pure stdlib,
like the rest of the trading core. Those four import torch *inside functions*,
behind the `native` extra, and raise `errors.DependencyMissing` when it is
absent — so the package imports, and its stdlib half runs, on an installation
that has never seen torch. That is why `tests/test_deps_claim.py` stays green.
"""

from __future__ import annotations

_LAZY = {
    "checkpoint": ".checkpoint",
    "data": ".data",
    "encoder": ".encoder",
    "forecaster": ".forecaster",
    "neural": ".neural",
    "quantile": ".quantile",
    "state": ".state",
    "torchutil": ".torchutil",
    "train": ".train",
    "trunk": ".trunk",
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
