"""Lazy torch access, and the determinism setup a reproducible fit needs.

Every torch import in this package goes through here, inside a function. That
is not style: `tests/test_trading_independence.py` fails the build on a
module-scope tensor-library import, because the trading core is pure stdlib and
that is a property worth keeping — `contracts`, `risk`, the OMS, the paper
broker and the backtester all run on a base install, and a native model that
made torch mandatory would take that away from everyone who never asked for a
neural forecaster.

`nn.Module` subclasses are therefore defined *inside* builder functions and
cached, because a class statement referencing `nn.Module` cannot exist at module
scope without importing torch there.

Determinism
-----------
`configure_determinism` does the four things a reproducible torch fit needs, and
the fourth is the one people forget:

1. seed torch's global RNG;
2. turn on deterministic algorithm selection, so a kernel is not chosen by
   whichever happened to benchmark faster this run;
3. pin the thread count, because a different reduction order across a different
   number of threads changes the last bits of a sum, and those bits compound
   over an epoch;
4. return a *seeded generator* rather than relying on the global RNG for
   batching — global-RNG batching makes a fit reproducible only if nothing else
   in the process ever draws a random number.

The cost is real: one thread is slower. Gate G7 says a fit must be reproducible,
and a fast fit that produces different weights each run cannot be compared with
anything, including itself.
"""

from __future__ import annotations

from typing import Any

from ..errors import DependencyMissing

#: Built classes, keyed by name. Torch classes cannot live at module scope, so
#: they are constructed on first use and reused after.
_CLASS_CACHE: dict[str, Any] = {}


def require_torch():
    """Import torch or raise `DependencyMissing` — never a bare `ImportError`.

    The distinction matters at a call site: `ImportError` reads as a broken
    installation, and this is a *feature that was not installed*, which is a
    different conversation with the operator.
    """
    try:
        import torch                                     # noqa: PLC0415
    except ImportError as exc:
        raise DependencyMissing(
            "the Olympus-native neural model needs torch, which is an optional "
            "extra: install `olympus[native]`. The statistical estimator in "
            "`native.quantile` runs without it.",
            package="torch", extra="native") from exc
    return torch


def torch_available() -> bool:
    """Non-raising probe, for a caller that wants to degrade rather than fail."""
    try:
        require_torch()
    except DependencyMissing:
        return False
    return True


def torch_version() -> str:
    """Recorded in every neural checkpoint's manifest.

    The same code on a different tensor library is different code, and a
    numerical change with no version recorded is a change nobody can attribute.
    """
    return str(require_torch().__version__)


def configure_determinism(seed: int, *, threads: int = 1):
    """Seed everything and pin the thread count. Returns a seeded generator."""
    torch = require_torch()
    torch.manual_seed(int(seed))
    try:
        torch.use_deterministic_algorithms(True)
    except Exception:                                    # noqa: BLE001
        # Older builds lack the toggle. The seed and thread pin still hold, and
        # a checkpoint's manifest records the torch version, so a divergence
        # between two builds is attributable rather than mysterious.
        pass
    torch.set_num_threads(max(1, int(threads)))
    generator = torch.Generator()
    generator.manual_seed(int(seed))
    return generator


def cached_class(name: str, factory):
    """Build a torch class once and reuse it.

    Defining an `nn.Module` subclass inside a function is the price of keeping
    torch out of module scope; rebuilding it on every call would also mean two
    instances of the "same" model failing an `isinstance` check.
    """
    if name not in _CLASS_CACHE:
        _CLASS_CACHE[name] = factory()
    return _CLASS_CACHE[name]


def tensor_to_lists(tensor) -> Any:
    """A tensor as nested Python lists, for a JSON checkpoint."""
    return tensor.detach().cpu().tolist()


def state_dict_to_json(module) -> dict:
    """Weights as JSON: `{name: {"shape": [...], "values": [...]}}`.

    Readable on purpose while the models are small. It keeps the whole
    checkpoint design — one content hash over parameters *and* manifest, an
    append-only store, no separate blob — and it is honestly a stopgap: at the
    scale a real encoder reaches this becomes a manifest beside a binary, which
    is blocker B7 in the architecture doc. The manifest, which is what any claim
    rests on, does not change when that happens.
    """
    return {name: {"shape": list(tensor.shape),
                   "values": tensor.detach().cpu().flatten().tolist()}
            for name, tensor in module.state_dict().items()}


def json_to_state_dict(payload, module) -> None:
    """Load JSON weights back into `module`, checking every shape.

    A silent shape mismatch is a model that produces plausible numbers from the
    wrong weights, so a disagreement raises here rather than at inference.
    """
    from ..errors import ConfigurationError
    torch = require_torch()
    target = module.state_dict()

    missing = sorted(set(target) - set(payload))
    unexpected = sorted(set(payload) - set(target))
    if missing or unexpected:
        raise ConfigurationError(
            "checkpoint weights do not match the model definition; loading "
            "them would produce plausible numbers from the wrong parameters",
            missing=missing, unexpected=unexpected)

    loaded = {}
    for name, entry in payload.items():
        shape = tuple(int(s) for s in entry["shape"])
        expected = tuple(target[name].shape)
        if shape != expected:
            raise ConfigurationError(
                "checkpoint weight has the wrong shape", parameter=name,
                expected=list(expected), got=list(shape))
        loaded[name] = torch.tensor(entry["values"],
                                    dtype=target[name].dtype).reshape(shape)
    module.load_state_dict(loaded)


__all__ = ["require_torch", "torch_available", "torch_version",
           "configure_determinism", "cached_class", "tensor_to_lists",
           "state_dict_to_json", "json_to_state_dict"]
