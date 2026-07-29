"""The continuous patch encoder — architecture doc §3.2.

Bars become channels, channels become non-overlapping patches, patches become
latent vectors by linear projection. **No codebook, no vocabulary, no
quantisation.**

Why continuous
--------------
Three reasons, in the order they carry weight:

1. **Quantisation imposes an error floor.** A discrete codebook cannot resolve
   a price move finer than its cell. The downstream consumer here is a risk
   engine reasoning in basis points, so that floor is a cost with no matching
   benefit — Olympus is not doing text-style generation, where a vocabulary buys
   a tractable softmax over something genuinely discrete.
2. **It removes a class of defect.** A hierarchical codebook has bit-splits,
   and bit-splits can be misconfigured into silent corruption. A design with no
   vocabulary cannot have that defect at all.
3. **It trains on the compute available.** A codebook needs commitment losses,
   entropy penalties and balance monitoring. A linear projection needs none.

Channels
--------
Four, all scale-free and all computable from the input window alone:

    log return · log(high/close) · log(low/close) · log(volume ratio)

Prices are never fed raw. A model trained on absolute prices learns the price
level, which is the one thing guaranteed not to generalise across instruments
or across time — and it is why the first channel is a *return*, not a close.

Normalisation
-------------
Instance normalisation over the input window's own statistics. This is causal
because a `Window` guarantees its targets close strictly after its inputs, so
the mean and standard deviation here cannot have seen the horizon. That
guarantee is enforced in `data.Window`, on timestamps, not assumed here — the
distinction between "normalised over the past" and "normalised over everything
including what we are predicting" is the whole of teardown §12.9, and it lives
in one place rather than being re-argued at every call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..errors import ConfigurationError
from .torchutil import cached_class, require_torch

#: Channel order. Fixed here, recorded in every manifest, and consumed in this
#: order by the encoder — a model trained on one order and served on another is
#: silently, totally wrong, and the order is not a thing to leave implicit.
CHANNELS: tuple[str, ...] = ("log_return", "log_high_ratio", "log_low_ratio",
                             "log_volume_ratio")

#: Guard against a near-constant channel producing an enormous normalised value.
#: A window of identical closes has zero return variance, and dividing by it
#: would send an infinity into the first linear layer.
_STD_FLOOR = 1e-8


@dataclass(frozen=True)
class EncoderConfig:
    """Encoder shape. Frozen and recorded in the manifest."""

    lookback: int
    patch_len: int
    d_model: int = 32

    def __post_init__(self):
        for name in ("lookback", "patch_len", "d_model"):
            if int(getattr(self, name)) < 1:
                raise ConfigurationError(f"{name} must be >= 1",
                                         **{name: getattr(self, name)})
        if self.lookback % self.patch_len != 0:
            raise ConfigurationError(
                "lookback must be a whole number of patches; a ragged final "
                "patch would be padded, and padding the *oldest* bars with "
                "zeros teaches the model that history began with a flat market",
                lookback=self.lookback, patch_len=self.patch_len)
        object.__setattr__(self, "lookback", int(self.lookback))
        object.__setattr__(self, "patch_len", int(self.patch_len))
        object.__setattr__(self, "d_model", int(self.d_model))

    @property
    def n_patches(self) -> int:
        return self.lookback // self.patch_len

    @property
    def n_channels(self) -> int:
        return len(CHANNELS)

    def to_dict(self) -> dict:
        return {"lookback": self.lookback, "patch_len": self.patch_len,
                "d_model": self.d_model, "n_patches": self.n_patches,
                "channels": list(CHANNELS)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EncoderConfig":
        return cls(lookback=int(data["lookback"]),
                   patch_len=int(data["patch_len"]),
                   d_model=int(data.get("d_model", 32)))


def bars_to_channels(bars: Sequence[Any]) -> list[list[float]]:
    """`[n_channels][n_bars-1]` of scale-free per-bar values.

    One shorter than the input, because the first bar has no previous close to
    return against. Returning a shorter series rather than padding a zero at the
    front is deliberate: a padded zero is a real observation of "no move" that
    the model will condition on.
    """
    import math
    if len(bars) < 2:
        raise ConfigurationError("need at least two bars to form a return",
                                 got=len(bars))
    log_returns, high_ratio, low_ratio, volume_ratio = [], [], [], []
    for i in range(1, len(bars)):
        previous, current = bars[i - 1], bars[i]
        if previous.close <= 0 or current.close <= 0:
            raise ConfigurationError("non-positive close in the input window",
                                     ts=current.ts_close)
        log_returns.append(math.log(current.close / previous.close))
        high_ratio.append(math.log(max(current.high, 1e-12) / current.close))
        low_ratio.append(math.log(max(current.low, 1e-12) / current.close))
        # Volume is compared with the previous bar rather than normalised
        # globally: a per-instrument volume level is not comparable across
        # instruments, and the ratio is.
        base = previous.volume if previous.volume > 0 else 1.0
        volume_ratio.append(math.log(max(current.volume, 1e-9) / base))
    return [log_returns, high_ratio, low_ratio, volume_ratio]


def normalise_window(channels: Sequence[Sequence[float]]
                     ) -> tuple[list[list[float]], list[tuple[float, float]]]:
    """Instance-normalise each channel over the window. Returns values + stats.

    The statistics are returned rather than discarded because they are part of
    what the model saw, and a forecast that cannot be reproduced from its
    recorded inputs is not reproducible.
    """
    import statistics
    out, stats = [], []
    for column in channels:
        values = list(column)
        mean = statistics.fmean(values) if values else 0.0
        spread = statistics.pstdev(values) if len(values) > 1 else 0.0
        spread = max(spread, _STD_FLOOR)
        out.append([(v - mean) / spread for v in values])
        stats.append((mean, spread))
    return out, stats


def build_encoder(config: EncoderConfig):
    """A `PatchEncoder` module. Torch is imported here, never at module scope."""
    torch = require_torch()
    nn = torch.nn

    def factory():
        class PatchEncoder(nn.Module):
            """Non-overlapping patches → one latent vector per patch.

            A single linear layer per patch, shared across patches. Shared
            because a per-patch projection would let the model learn *where in
            the window* a pattern sat, which is position memorisation rather
            than shape recognition, and it multiplies the parameter count by
            `n_patches` for the privilege.
            """

            def __init__(self, n_channels: int, patch_len: int, d_model: int):
                super().__init__()
                self.n_channels = n_channels
                self.patch_len = patch_len
                self.d_model = d_model
                self.project = nn.Linear(n_channels * patch_len, d_model)
                self.norm = nn.LayerNorm(d_model)

            def forward(self, x):
                # x: [batch, channels, time]
                batch, channels, time = x.shape
                if time % self.patch_len != 0:
                    raise ValueError(
                        f"time {time} is not a whole number of "
                        f"{self.patch_len}-bar patches")
                patches = time // self.patch_len
                # [batch, patches, channels * patch_len]
                folded = (x.reshape(batch, channels, patches, self.patch_len)
                           .permute(0, 2, 1, 3)
                           .reshape(batch, patches, channels * self.patch_len))
                return self.norm(self.project(folded))

        return PatchEncoder

    cls = cached_class("PatchEncoder", factory)
    return cls(config.n_channels, config.patch_len, config.d_model)


__all__ = ["CHANNELS", "EncoderConfig", "bars_to_channels", "normalise_window",
           "build_encoder"]
