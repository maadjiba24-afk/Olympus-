"""Olympus-native market intelligence.

An Olympus-owned forecasting system: independently designed components, an
Olympus-controlled training pipeline, and checkpoints trained from Olympus
datasets. Kronos is a benchmark and a replaceable provider, not an ancestor —
`tests/test_trading_independence.py` proves nothing here reaches it.

What exists
-----------
The observation, the data, the representation, the model, and the harness that
judges it:

* `schema`     — the 38 channels, and what is true about each one
* `state`      — `MarketState`: typed, causal, multi-scale observation
* `data`       — windowing and the embargoed temporal split
* `dataset`    — provenance, universe membership, alignment, leakage audit
* `interfaces` — the encoder contracts the rest of Olympus depends on
* `encoder`    — continuous patch projection; no codebook, no vocabulary
* `representations` — seven candidates, implemented and comparable
* `trunk`      — dilated **causal** convolutions, non-autoregressive
* `quantile`   — conditional quantiles by lookup; the cheap estimator
* `neural`     — the learned monotone quantile ladder, trained by pinball loss
* `baselines`  — nine opponents on one interface
* `benchmark`  — one split, one cost model, one metric set, for every model
* `torchutil`  — the torch boundary: lazy import, seeded determinism
* `checkpoint` — Olympus checkpoints and the manifest that makes them claimable
* `train`      — the pipeline, in the one order that is safe
* `forecaster` — `forecast.Forecaster` implementation; the plug point

What does not exist
-------------------
The regime, volatility, liquidity, event and portfolio-evaluation heads;
conformal calibration; the full out-of-distribution detector; and any model that
consumes more than the base timeframe or more than one instrument — the causal
alignment for both exists in `dataset`, and nothing reads it yet.
`docs/OLYMPUS_NATIVE_MODEL_STATUS.md` is the ledger; when it and this docstring
disagree, the ledger is right.

**Olympus does not own a trained market model.** Everything here has been fitted
only to synthetic series, because no market data is reachable from this
environment (`docs/TRADING_EXTERNAL_VALIDATION.md` §1). On those series the
network recovers a structure that is genuinely there, correctly fails to beat
persistence on white noise, and **loses to a nineteen-parameter autoregressive
fit** — evidence about the pipeline and the harness, not about markets.

Import discipline
-----------------
`encoder`, `trunk`, `neural`, `representations`, `torchutil` and two of the nine
`baselines` need torch; everything else is pure stdlib, like the rest of the
trading core. Those import torch *inside functions*, behind the `native` extra,
and raise `errors.DependencyMissing` when it is absent — so the package imports,
its stdlib half runs, and a benchmark still fields seven opponents on an
installation that has never seen torch. That is why `tests/test_deps_claim.py`
stays green.
"""

from __future__ import annotations

_LAZY = {
    "abstain": ".abstain",
    "capability": ".capability",
    "challengers": ".challengers",
    "crossasset": ".crossasset",
    "events": ".events",
    "evidence": ".evidence",
    "explain": ".explain",
    "improvement": ".improvement",
    "isolation": ".isolation",
    "microstructure": ".microstructure",
    "portfolio_context": ".portfolio_context",
    "promotion": ".promotion",
    "scenarios": ".scenarios",
    "specialists": ".specialists",
    "timeframes": ".timeframes",
    "baselines": ".baselines",
    "benchmark": ".benchmark",
    "checkpoint": ".checkpoint",
    "data": ".data",
    "dataset": ".dataset",
    "encoder": ".encoder",
    "evaluation": ".evaluation",
    "forecaster": ".forecaster",
    "interfaces": ".interfaces",
    "model": ".model",
    "modelcard": ".modelcard",
    "neural": ".neural",
    "originality": ".originality",
    "pipeline": ".pipeline",
    "quantile": ".quantile",
    "representations": ".representations",
    "result": ".result",
    "schema": ".schema",
    "serve": ".serve",
    "state": ".state",
    "tasks": ".tasks",
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
