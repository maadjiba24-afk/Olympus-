"""The temporal trunk and the monotone quantile head — architecture doc §3.3.

A dilated **causal** convolution stack over patch embeddings, then a head that
emits every horizon step's quantiles in one forward pass.

Why causal convolutions rather than attention
---------------------------------------------
Three reasons, and the first is not about accuracy:

1. **Causality is structural.** A causal convolution with left-only padding
   cannot see a future patch — not "is trained not to", *cannot*. Attention
   needs a mask, and a mask is a line of code that can be wrong, that has been
   wrong in published implementations, and whose being wrong produces excellent
   validation numbers. Making the leak impossible beats testing for it.
2. **Cost is linear, not quadratic.** On four CPU cores with no GPU (blocker
   B5), an O(n²) attention over a long context is not a design that can be
   trained here.
3. **The receptive field is a stated number.** Exponential dilation gives
   `1 + 2·(k−1)·(2^L − 1)` patches of history, so "how far back can this model
   see" has an arithmetic answer rather than an empirical one.

Why a non-autoregressive head
-----------------------------
One pass emits `horizon × n_quantiles`. Sampling paths autoregressively
compounds error with horizon and makes uncertainty a function of how many
samples you could afford. Olympus's consumers — `risk.py`, `signals.py`,
`strategy.py` — read quantiles, uncertainty and expected return, never a path.
Producing exactly what is consumed is both cheaper and better calibrated.

Why the head is monotone by construction
----------------------------------------
The statistical estimator got quantile ordering for free by sorting. A neural
head does not: independent outputs cross, and a p95 below a p05 is not a wide
interval, it is a nonsense one that flows into a stop-loss. So the head emits
the lowest quantile plus **softplus increments**, and a crossed quantile is
arithmetically impossible rather than penalised.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..errors import ConfigurationError
from .torchutil import cached_class, require_torch


@dataclass(frozen=True)
class TrunkConfig:
    """Trunk and head shape."""

    d_model: int = 32
    kernel_size: int = 2
    n_layers: int = 3
    horizon: int = 3
    n_quantiles: int = 5
    dropout: float = 0.0

    def __post_init__(self):
        for name in ("d_model", "kernel_size", "n_layers", "horizon",
                     "n_quantiles"):
            if int(getattr(self, name)) < 1:
                raise ConfigurationError(f"{name} must be >= 1",
                                         **{name: getattr(self, name)})
        if not 0.0 <= float(self.dropout) < 1.0:
            raise ConfigurationError("dropout must be in [0, 1)",
                                     dropout=self.dropout)
        for name in ("d_model", "kernel_size", "n_layers", "horizon",
                     "n_quantiles"):
            object.__setattr__(self, name, int(getattr(self, name)))
        object.__setattr__(self, "dropout", float(self.dropout))

    @property
    def receptive_field(self) -> int:
        """Patches of history the last output can see. Arithmetic, not measured."""
        return 1 + 2 * (self.kernel_size - 1) * (2 ** self.n_layers - 1)

    def to_dict(self) -> dict:
        return {"d_model": self.d_model, "kernel_size": self.kernel_size,
                "n_layers": self.n_layers, "horizon": self.horizon,
                "n_quantiles": self.n_quantiles, "dropout": self.dropout,
                "receptive_field_patches": self.receptive_field}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TrunkConfig":
        return cls(d_model=int(data.get("d_model", 32)),
                   kernel_size=int(data.get("kernel_size", 2)),
                   n_layers=int(data.get("n_layers", 3)),
                   horizon=int(data["horizon"]),
                   n_quantiles=int(data["n_quantiles"]),
                   dropout=float(data.get("dropout", 0.0)))


def build_trunk(config: TrunkConfig):
    """A `QuantileTrunk` module: causal trunk plus monotone head."""
    torch = require_torch()
    nn = torch.nn

    def factory():
        class CausalBlock(nn.Module):
            """One dilated causal convolution with a residual connection.

            Padding is applied on the left and the right tail is trimmed, which
            is what makes the convolution causal. Symmetric padding here would
            let every output position read `dilation × (kernel−1)` patches of
            its own future — a leak that trains beautifully and generalises to
            nothing.
            """

            def __init__(self, d_model: int, kernel_size: int, dilation: int,
                         dropout: float):
                super().__init__()
                self.pad = (kernel_size - 1) * dilation
                self.conv = nn.Conv1d(d_model, d_model, kernel_size,
                                      dilation=dilation)
                self.norm = nn.LayerNorm(d_model)
                self.activation = nn.GELU()
                self.dropout = nn.Dropout(dropout) if dropout > 0 else nn.Identity()

            def forward(self, x):
                # x: [batch, d_model, patches]
                padded = nn.functional.pad(x, (self.pad, 0))
                out = self.conv(padded)
                out = self.dropout(self.activation(out))
                out = self.norm((x + out).transpose(1, 2)).transpose(1, 2)
                return out

        class QuantileTrunk(nn.Module):
            """Patch embeddings → per-step, per-quantile cumulative log return."""

            def __init__(self, cfg: TrunkConfig):
                super().__init__()
                self.horizon = cfg.horizon
                self.n_quantiles = cfg.n_quantiles
                self.blocks = nn.ModuleList([
                    CausalBlock(cfg.d_model, cfg.kernel_size, 2 ** i,
                                cfg.dropout)
                    for i in range(cfg.n_layers)])
                # The lowest quantile, per horizon step.
                self.base = nn.Linear(cfg.d_model, cfg.horizon)
                # Raw increments; softplus makes them non-negative, so the
                # quantile ladder cannot cross.
                self.increments = nn.Linear(cfg.d_model,
                                            cfg.horizon * (cfg.n_quantiles - 1))

            def forward(self, patches):
                # patches: [batch, n_patches, d_model]
                x = patches.transpose(1, 2)               # [batch, d, patches]
                for block in self.blocks:
                    x = block(x)
                # The last position is the only one that has seen the whole
                # window, and it is the instant the forecast is made from.
                last = x[:, :, -1]                        # [batch, d]

                base = self.base(last).unsqueeze(-1)      # [batch, h, 1]
                raw = self.increments(last).reshape(
                    -1, self.horizon, self.n_quantiles - 1)
                steps = nn.functional.softplus(raw)
                ladder = torch.cumsum(steps, dim=-1)
                return torch.cat([base, base + ladder], dim=-1)

        return QuantileTrunk

    cls = cached_class("QuantileTrunk", factory)
    return cls(config)


def pinball_loss(predicted, actual, quantiles):
    """Mean pinball (quantile) loss.

    The same objective `evaluate.pinball_loss` scores with, so the number
    reported is the number that was optimised. That equivalence is worth more
    than it sounds: a model trained on MSE and reported on pinball is being
    judged on something it never tried to do.

    `predicted`: [batch, horizon, n_quantiles]
    `actual`:    [batch, horizon]
    """
    torch = require_torch()
    levels = torch.tensor(list(quantiles), dtype=predicted.dtype).reshape(1, 1, -1)
    errors = actual.unsqueeze(-1) - predicted
    return torch.maximum(levels * errors, (levels - 1.0) * errors).mean()


__all__ = ["TrunkConfig", "build_trunk", "pinball_loss"]
