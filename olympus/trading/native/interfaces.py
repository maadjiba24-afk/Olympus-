"""Olympus-owned encoder contracts.

The rest of Olympus depends on these, never on a concrete encoder. That is the
same bargain `forecast.Forecaster` makes one level up, and it is what lets a
representation be replaced by deleting a registry entry rather than by editing
the consumers.

What is stable here
-------------------
Shapes, identity and the mask. Not the mathematics. An encoder may be a linear
patch projection, a codebook, or something not yet written; what every consumer
is allowed to assume is:

* it declares a `LatentSpec` before it is called, so a downstream trunk can be
  built without running a forward pass;
* it declares which channels it consumes, against a schema fingerprint, so a
  model served on a different channel set fails loudly at load rather than
  quietly at inference;
* absent inputs arrive as an explicit mask and leave as an explicit mask. An
  encoder is not permitted to invent a value for a channel that was not
  observed — it may *learn* a representation of absence, which is a different
  thing and is declared by `handles_missing`.

Why the tensor type is `Any`
----------------------------
These contracts are pure stdlib and importable with torch absent, because
`tests/test_deps_claim.py` requires the trading core to be, and because a
consumer that only needs to read a `LatentSpec` should not need a deep-learning
framework to do it. The arrays flowing through `encode` are whatever the
implementation's backend uses; the contract constrains their *shape*, which is
declared in data, not their type.

Version identity
----------------
`EncoderMetadata.identity()` is what goes into a checkpoint and a cache key. It
covers the encoder name, its version, its parameter hash and the schema
fingerprint — four things that must all match for two encoders to be
interchangeable, and any one of which changing silently is a corruption.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from ..errors import ConfigurationError

#: Bumped when a contract's *meaning* changes. A consumer may refuse a
#: representation built against an older contract rather than duck-typing it.
CONTRACT_VERSION = 1


class MaskPolicy(str, Enum):
    """What an encoder does with a channel that was not observed.

    There is no `ZERO` member, for the reason `schema.MissingPolicy` has no
    zero-fill member: a zero is a real observation to a model.
    """

    #: The encoder refuses to run. Correct for a channel the model cannot work
    #: without, and honest about it.
    REFUSE = "refuse"
    #: A learned vector stands in for "this channel was absent". The model can
    #: therefore distinguish absent from any observed value, which zero-filling
    #: makes impossible.
    LEARNED_ABSENT = "learned_absent"
    #: The mask is concatenated as its own input channel and the value position
    #: is left at the encoder's own neutral element. Cheaper than a learned
    #: embedding and still recoverable, because the mask bit is present.
    MASK_CHANNEL = "mask_channel"
    #: The channel is dropped from this encoder's input surface entirely.
    #: Changes the latent shape, so it is a configuration decision, not a
    #: runtime one.
    EXCLUDE = "exclude"


@dataclass(frozen=True)
class LatentSpec:
    """The shape an encoder promises to emit, declared before it is called.

    `positions` is the sequence length the trunk will see; `d_model` its width.
    A non-sequential encoder declares `positions=1`, which keeps a single
    downstream code path rather than two.
    """

    positions: int
    d_model: int
    #: True when consecutive positions are ordered in time, which is what makes
    #: a causal trunk's masking meaningful. False for a set-valued latent.
    sequential: bool = True
    #: Present when the encoder also emits a per-position validity mask.
    emits_mask: bool = False

    def __post_init__(self):
        for name in ("positions", "d_model"):
            if int(getattr(self, name)) < 1:
                raise ConfigurationError(f"{name} must be >= 1",
                                         **{name: getattr(self, name)})
        object.__setattr__(self, "positions", int(self.positions))
        object.__setattr__(self, "d_model", int(self.d_model))

    @property
    def shape(self) -> tuple[int, int]:
        """`(positions, d_model)`, excluding the batch dimension."""
        return (self.positions, self.d_model)

    def compatible_with(self, other: "LatentSpec") -> bool:
        """Can a trunk built for `other` consume this?

        Width and ordering must match; position count need not, because a
        convolutional or attention trunk over a different sequence length is
        still the same trunk. A trunk that *does* require a fixed length says so
        itself — this is the necessary condition, not the sufficient one.
        """
        return (self.d_model == other.d_model
                and self.sequential == other.sequential)

    def to_dict(self) -> dict:
        return {"positions": self.positions, "d_model": self.d_model,
                "sequential": self.sequential, "emits_mask": self.emits_mask}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LatentSpec":
        return cls(positions=int(data["positions"]),
                   d_model=int(data["d_model"]),
                   sequential=bool(data.get("sequential", True)),
                   emits_mask=bool(data.get("emits_mask", False)))


@dataclass(frozen=True)
class EncoderMetadata:
    """Who this encoder is. Written into every checkpoint and cache key."""

    name: str
    version: str
    latent: LatentSpec
    #: Channels consumed, in the order consumed. Order is part of the identity:
    #: an encoder trained on one order and served on another is silently wrong.
    channels: tuple[str, ...]
    #: Fingerprint of the `MarketStateSchema` those channels were read from.
    schema_fingerprint: str
    mask_policy: MaskPolicy = MaskPolicy.MASK_CHANNEL
    #: Count of *trainable* scalars. Zero means the encoder learns nothing —
    #: which is a claim about this encoder as a whole, not about any one of its
    #: sub-steps.
    trainable_parameters: int = 0
    #: Free-form configuration that changes the numbers. Enters the identity.
    params: Mapping[str, Any] = field(default_factory=dict)
    #: Whether this encoder can also reconstruct its input.
    reconstructs: bool = False
    #: Whether it emits discrete tokens as well as (or instead of) a latent.
    tokenises: bool = False

    def __post_init__(self):
        if not str(self.name).strip():
            raise ConfigurationError("an encoder must be named")
        if not str(self.version).strip():
            raise ConfigurationError("an encoder must declare a version",
                                     name=self.name)
        object.__setattr__(self, "channels", tuple(self.channels))
        if not self.channels:
            raise ConfigurationError("an encoder must declare the channels it "
                                     "consumes", name=self.name)
        if len(set(self.channels)) != len(self.channels):
            raise ConfigurationError("duplicate channel in an encoder's input "
                                     "surface", name=self.name)
        object.__setattr__(self, "mask_policy", MaskPolicy(self.mask_policy))
        object.__setattr__(self, "params", dict(self.params))
        if int(self.trainable_parameters) < 0:
            raise ConfigurationError("trainable_parameters cannot be negative",
                                     name=self.name)
        object.__setattr__(self, "trainable_parameters",
                           int(self.trainable_parameters))

    @property
    def handles_missing(self) -> bool:
        """True when this encoder can run with a channel absent."""
        return self.mask_policy is not MaskPolicy.REFUSE

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version,
                "latent": self.latent.to_dict(), "channels": list(self.channels),
                "schema_fingerprint": self.schema_fingerprint,
                "mask_policy": self.mask_policy.value,
                "trainable_parameters": self.trainable_parameters,
                "params": dict(self.params), "reconstructs": self.reconstructs,
                "tokenises": self.tokenises,
                "contract_version": CONTRACT_VERSION}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EncoderMetadata":
        return cls(name=str(data["name"]), version=str(data["version"]),
                   latent=LatentSpec.from_dict(data["latent"]),
                   channels=tuple(data.get("channels") or ()),
                   schema_fingerprint=str(data.get("schema_fingerprint", "")),
                   mask_policy=MaskPolicy(data.get("mask_policy",
                                                   MaskPolicy.MASK_CHANNEL.value)),
                   trainable_parameters=int(data.get("trainable_parameters", 0)),
                   params=dict(data.get("params") or {}),
                   reconstructs=bool(data.get("reconstructs", False)),
                   tokenises=bool(data.get("tokenises", False)))

    def identity(self) -> str:
        """The hash two encoders must share to be interchangeable."""
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]

    def __repr__(self) -> str:                           # pragma: no cover
        return (f"EncoderMetadata({self.name} v{self.version}, "
                f"{self.latent.shape}, {self.trainable_parameters} params)")


@dataclass(frozen=True)
class EncodedBatch:
    """An encoder's output: the latent, and what was valid in it.

    `valid` is per-position, not per-channel, because by this point the channels
    have been mixed. A consumer that needs per-channel validity must read it
    from the `ChannelObservation` upstream — and the fact that it cannot be
    recovered here is a reason to keep the observation, not to fabricate it.
    """

    latent: Any
    spec: LatentSpec
    metadata: EncoderMetadata
    #: Per-position validity, when the encoder emits one. `None` when every
    #: position is valid by construction.
    valid: Any = None
    #: Discrete codes, when the encoder tokenises. Shape is the encoder's own.
    codes: Any = None
    #: Anything the encoder wants carried forward for reconstruction —
    #: normalisation statistics, residuals. Opaque to consumers.
    side_channel: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "side_channel", dict(self.side_channel))
        if self.metadata.tokenises and self.codes is None:
            raise ConfigurationError(
                "an encoder declaring that it tokenises returned no codes",
                encoder=self.metadata.name)
        if self.spec.emits_mask and self.valid is None:
            raise ConfigurationError(
                "an encoder declaring emits_mask returned no validity mask",
                encoder=self.metadata.name)

    def describe(self) -> dict:
        return {"encoder": self.metadata.name,
                "identity": self.metadata.identity(),
                "spec": self.spec.to_dict(),
                "has_codes": self.codes is not None,
                "has_valid": self.valid is not None,
                "side_channel": sorted(self.side_channel)}


class MarketEncoder(ABC):
    """Market observations in, latent out. The one interface consumers see."""

    @property
    @abstractmethod
    def metadata(self) -> EncoderMetadata:
        """Identity and shape. Available before any forward pass."""

    @property
    def latent_spec(self) -> LatentSpec:
        return self.metadata.latent

    @abstractmethod
    def encode(self, batch: Any, *, mask: Any = None) -> EncodedBatch:
        """Encode a batch. `mask` is per-channel presence where supported.

        Implementations raise `ConfigurationError` for a shape they cannot
        consume and `DependencyMissing` for an absent optional backend. They do
        not silently reshape: a batch of the wrong width is a caller error that
        must not be repaired into a plausible number.
        """

    def parameters_dict(self) -> Mapping[str, Any]:
        """Serialisable weights, for a checkpoint. Empty when there are none."""
        return {}

    def load_parameters(self, values: Mapping[str, Any]) -> None:
        """Restore weights saved by `parameters_dict`."""
        if values:
            raise ConfigurationError(
                "this encoder has no parameters to load",
                encoder=self.metadata.name, got=sorted(values))

    def __repr__(self) -> str:                           # pragma: no cover
        return f"{type(self).__name__}({self.metadata.name})"


class Tokeniser(ABC):
    """Optional: a discrete code layer over a continuous latent.

    Separate from `MarketEncoder` because tokenisation is a choice, not a
    requirement, and the continuous encoders must not have to implement a
    vocabulary they do not have.
    """

    @property
    @abstractmethod
    def vocabulary_size(self) -> int:
        """Total distinct codes. The error floor is a function of this."""

    @property
    def levels(self) -> int:
        """How many quantisation stages. One unless the scheme is residual."""
        return 1

    @abstractmethod
    def tokenise(self, latent: Any) -> Any:
        """Continuous latent → integer codes."""

    @abstractmethod
    def detokenise(self, codes: Any) -> Any:
        """Integer codes → the latent they stand for. Lossy by construction."""

    def utilisation(self, codes: Any) -> float | None:
        """Fraction of the vocabulary actually used. `None` when unmeasured.

        Worth having as a contract method: a codebook where 12 of 512 entries
        carry every observation is a collapsed codebook, and the training loss
        does not say so.
        """
        return None


class Reconstructor(ABC):
    """Optional: latent → an approximation of the encoder's input.

    Its purpose is evidence. A representation that cannot reconstruct its input
    has discarded something, and the question is whether what it discarded
    mattered — which is answerable only if reconstruction is implemented.
    """

    @abstractmethod
    def reconstruct(self, encoded: EncodedBatch) -> Any:
        """Latent → input-shaped tensor."""

    @abstractmethod
    def reconstruction_error(self, encoded: EncodedBatch, original: Any,
                             *, mask: Any = None) -> float:
        """Scalar error against the original. Masked positions are excluded.

        Excluded rather than counted as zero error: scoring a reconstruction on
        positions that were never observed rewards an encoder for agreeing with
        a value nobody had.
        """


class TimeframeFusion(ABC):
    """Combine per-timeframe latents into one.

    A contract rather than a function because the choice — concatenate, attend,
    gate — is exactly the sort of thing that should be swappable behind an
    interface and measured, not settled by whoever wrote it first.
    """

    @property
    @abstractmethod
    def output_spec(self) -> LatentSpec:
        ...

    @abstractmethod
    def fuse(self, per_timeframe: Mapping[str, EncodedBatch]) -> EncodedBatch:
        """Fuse, in a declared timeframe order.

        Implementations must fix the timeframe order at construction, not read
        it from mapping iteration order at call time. Dict ordering that happens
        to be stable in one process and different in another is precisely the
        silent corruption this whole module is arranged to prevent.
        """


class CrossAssetContext(ABC):
    """Condition a target instrument's latent on reference instruments'.

    The causal constraint is not this interface's to enforce — `dataset.
    align_cross_asset` has already done it, and a reference latent that reaches
    here has been paired on `ts_close <= ts_close`. What this interface owns is
    that a missing reference stays missing.
    """

    @property
    @abstractmethod
    def output_spec(self) -> LatentSpec:
        ...

    @abstractmethod
    def contextualise(self, target: EncodedBatch,
                      references: Mapping[str, EncodedBatch]) -> EncodedBatch:
        ...


__all__ = ["CONTRACT_VERSION", "MaskPolicy", "LatentSpec", "EncoderMetadata",
           "EncodedBatch", "MarketEncoder", "Tokeniser", "Reconstructor",
           "TimeframeFusion", "CrossAssetContext"]
