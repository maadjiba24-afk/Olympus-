"""Representation candidates, implemented and comparable.

Phase P3 shipped one encoder — non-overlapping continuous patches — and the
argument for it was written before any of the alternatives existed. That is a
justification, not evidence. This module implements seven candidates against the
one `interfaces.MarketEncoder` contract so the choice can be *measured*: same
input, same reconstruction objective, same harness, parameter counts reported.

The seven
---------
============================  ==========================================
`continuous_patch`            linear projection of non-overlapping patches
`multi_resolution_patch`      the same at several patch lengths at once
`learned_tokens`              a single vector-quantised codebook
`residual_vq`                 a codebook, then a codebook on the residual
`grouped_embeddings`          a separate projection per channel group
`mask_aware`                  values, presence mask and a learned absent vector
`hybrid`                      continuous and quantised latents, fused
============================  ==========================================

Every candidate carries a `RepresentationCandidate` record with its rationale,
formulation, shapes, complexity, reconstruction objective, limitations,
relationship to prior work, and how its independence is evidenced. Those records
are data, not prose, so `tests/test_trading_native_repr.py` can assert that no
candidate ships undocumented.

On "parameter-free"
-------------------
Some sub-steps here are genuinely parameter-free and are described that way
about themselves only. Patch *folding* — the reshape that groups `patch_len`
consecutive timesteps — has no parameters; the projection applied to the folded
result has `C·P·d + d`. Instance normalisation as implemented in `encoder.py`
subtracts a mean and divides by a standard deviation, with no learned affine, so
that sub-step is parameter-free; the encoder containing it is not. Whenever a
count is quoted below it is the count for the whole candidate, and
`metadata.trainable_parameters` is measured from the built module rather than
predicted, so a mismatch between claim and reality is a test failure.

On prior work
-------------
Patch-based tokenisation of time series, vector quantisation and residual vector
quantisation are published, general techniques with a substantial literature.
Olympus claims no novelty in any of them; the record for each candidate names
the family it belongs to. What is claimed is that these are *independent
implementations from the published idea*, written against Olympus's own
contracts and channel schema — the `independence` field on each record states
what that rests on, and `tests/test_trading_independence.py` is the check.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..errors import ConfigurationError
from .encoder import CHANNELS, bars_to_channels, normalise_window
from .interfaces import (EncodedBatch, EncoderMetadata, LatentSpec, MarketEncoder,
                         MaskPolicy, Reconstructor, Tokeniser)
from .schema import OLYMPUS_MARKET_SCHEMA
from .torchutil import cached_class, require_torch

#: Bumped when a candidate's mathematics changes, so a checkpoint built under
#: the old formulation does not load silently under the new one.
REPRESENTATION_VERSION = "1"

#: Default channel grouping for `grouped_embeddings`, over `encoder.CHANNELS`.
#: Liquidity and event groups are declared and empty here: the channels that
#: would populate them are `NOT_INGESTED` (see `schema.availability_report`),
#: and an empty declared group is more honest than a group silently absent.
DEFAULT_GROUPS: Mapping[str, tuple[str, ...]] = {
    "price": ("log_return", "log_high_ratio", "log_low_ratio"),
    "volume": ("log_volume_ratio",),
    "liquidity": (),
    "event": (),
}


@dataclass(frozen=True)
class RepresentationCandidate:
    """Everything that must be true and stated about a candidate.

    Every field is required to be non-empty. A candidate that ships without a
    stated limitation has not been thought about, and the test suite treats a
    blank `limitations` as a defect rather than as an absence of problems.
    """

    key: str
    title: str
    rationale: str
    #: The mathematics, as it would be written down. Plain text, not LaTeX,
    #: because it has to be readable in a terminal diff.
    formulation: str
    trainable_components: tuple[str, ...]
    input_shape: str
    output_shape: str
    complexity: str
    reconstruction_objective: str
    forecasting_compatibility: str
    limitations: tuple[str, ...]
    prior_art: str
    independence: str
    implemented: bool = True
    tokenises: bool = False
    mask_policy: MaskPolicy = MaskPolicy.MASK_CHANNEL

    def __post_init__(self):
        object.__setattr__(self, "trainable_components",
                           tuple(self.trainable_components))
        object.__setattr__(self, "limitations", tuple(self.limitations))
        object.__setattr__(self, "mask_policy", MaskPolicy(self.mask_policy))
        for name in ("key", "title", "rationale", "formulation", "input_shape",
                     "output_shape", "complexity", "reconstruction_objective",
                     "forecasting_compatibility", "prior_art", "independence"):
            if not str(getattr(self, name)).strip():
                raise ConfigurationError(
                    f"candidate {self.key!r} has no {name}; an undocumented "
                    f"candidate cannot be compared against another",
                    candidate=self.key, field=name)
        if not self.limitations:
            raise ConfigurationError(
                "a candidate must state at least one limitation; 'none known' "
                "is a statement and should be written as one",
                candidate=self.key)
        if not self.trainable_components:
            raise ConfigurationError(
                "a candidate must list its trainable components, or state "
                "explicitly that it has none", candidate=self.key)

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title,
                "rationale": self.rationale, "formulation": self.formulation,
                "trainable_components": list(self.trainable_components),
                "input_shape": self.input_shape,
                "output_shape": self.output_shape,
                "complexity": self.complexity,
                "reconstruction_objective": self.reconstruction_objective,
                "forecasting_compatibility": self.forecasting_compatibility,
                "limitations": list(self.limitations),
                "prior_art": self.prior_art, "independence": self.independence,
                "implemented": self.implemented, "tokenises": self.tokenises,
                "mask_policy": self.mask_policy.value}


CANDIDATES: Mapping[str, RepresentationCandidate] = {
    c.key: c for c in (
        RepresentationCandidate(
            key="continuous_patch",
            title="Continuous non-overlapping patch projection",
            rationale=(
                "The cheapest representation that is not the identity. Groups "
                "consecutive bars so the trunk sees O(T/P) positions instead of "
                "O(T), which is what makes a small model affordable on four "
                "cores, and imposes no error floor because nothing is "
                "quantised."),
            formulation=(
                "x in R^{C x T}, normalised per channel over the window.\n"
                "Fold:  X[p] = vec(x[:, pP:(p+1)P]) in R^{CP},  p = 0..T/P-1.\n"
                "Project: z[p] = LayerNorm(W X[p] + b),  W in R^{d x CP}.\n"
                "W and b are shared across p."),
            trainable_components=("projection W (d x CP)", "bias b (d)",
                                  "LayerNorm gain and bias (2d)"),
            input_shape="[batch, C, T] with T divisible by P",
            output_shape="[batch, T/P, d]",
            complexity=("O(batch * (T/P) * CP * d) multiply-accumulates; "
                        "parameters CPd + 3d. Folding is parameter-free; the "
                        "projection and the LayerNorm that follow are not."),
            reconstruction_objective=(
                "Linear decoder d -> CP per position, mean squared error "
                "against the normalised input patch."),
            forecasting_compatibility=(
                "Direct. Emits a time-ordered sequence, which is what the "
                "causal-convolution trunk consumes."),
            limitations=(
                "A hard patch boundary cannot represent a pattern straddling "
                "it; a stride-P view is blind to phase.",
                "Sharing W across positions is deliberate but costs the ability "
                "to treat the oldest patch differently from the newest.",
                "No mechanism for an absent channel beyond whatever the caller "
                "put in the tensor."),
            prior_art=("The patch-then-project family used widely for time "
                       "series and images. Olympus claims no novelty in the "
                       "idea."),
            independence=(
                "Written against `interfaces.MarketEncoder` and "
                "`encoder.CHANNELS`; no third-party checkpoint, weight file or "
                "vocabulary is read. Enforced by "
                "`tests/test_trading_independence.py`."),
            mask_policy=MaskPolicy.REFUSE),

        RepresentationCandidate(
            key="multi_resolution_patch",
            title="Multi-resolution patch projection",
            rationale=(
                "One patch length picks one timescale. Markets do not have one. "
                "Running several patch lengths in parallel and concatenating "
                "the positions lets the trunk attend to a two-bar reversal and "
                "a sixteen-bar trend without choosing between them in advance."),
            formulation=(
                "For each P_k in {P_1..P_K}: fold and project as in "
                "continuous_patch with its own W_k, then add a learned "
                "resolution embedding r_k in R^d.\n"
                "z = concat_k [ z^{(k)}[0..T/P_k-1] + r_k ]  along positions.\n"
                "Total positions = sum_k T/P_k."),
            trainable_components=("one projection W_k and bias per resolution",
                                  "one resolution embedding r_k per resolution",
                                  "shared output LayerNorm"),
            input_shape="[batch, C, T] with T divisible by every P_k",
            output_shape="[batch, sum_k T/P_k, d]",
            complexity=("O(batch * d * C * T * K) — each resolution touches "
                        "every timestep exactly once, so cost is linear in the "
                        "number of resolutions, not in their lengths. "
                        "Parameters sum_k (C P_k d) + Kd + 3d."),
            reconstruction_objective=(
                "Per-resolution linear decoders, each reconstructing its own "
                "patch grid; the reported error is the mean over resolutions, "
                "so a candidate cannot look good by reconstructing only its "
                "coarsest view."),
            forecasting_compatibility=(
                "Positions are ordered *within* a resolution but the "
                "concatenation across resolutions is not a single time order. "
                "A causal convolutional trunk therefore may not be applied "
                "across the whole sequence — it must be applied per resolution "
                "and fused, or replaced by a permutation-invariant trunk. This "
                "is a real cost and is the main argument against the candidate."),
            limitations=(
                "Breaks the single-time-order assumption a causal trunk relies "
                "on; see forecasting_compatibility.",
                "Position count grows with the harmonic sum of the patch "
                "lengths, so a fine resolution is expensive downstream.",
                "Requires T divisible by every patch length, which constrains "
                "the lookback to a common multiple."),
            prior_art=("Multi-scale and pyramid representations are long "
                       "established in signal processing and vision. No novelty "
                       "is claimed."),
            independence=(
                "Composed from this repository's own patch encoder; no external "
                "architecture is transcribed."),
            mask_policy=MaskPolicy.REFUSE),

        RepresentationCandidate(
            key="learned_tokens",
            title="Vector-quantised market tokens",
            rationale=(
                "The candidate that makes the discrete option real rather than "
                "rhetorical. A codebook gives a finite alphabet, which buys "
                "interpretable cluster identity and a compact code — and costs "
                "an error floor. Implemented so the cost can be measured "
                "instead of asserted."),
            formulation=(
                "Encode as continuous_patch to z[p] in R^d.\n"
                "Codebook E in R^{K x d}.  k(p) = argmin_k ||z[p] - E_k||^2.\n"
                "q[p] = E_{k(p)}.  Straight-through: z_out = z + (q - z).detach()\n"
                "Losses: codebook ||sg[z] - q||^2, commitment beta*||z - sg[q]||^2."),
            trainable_components=("the patch projection", "the codebook E (K x d)",
                                  "LayerNorm gain and bias"),
            input_shape="[batch, C, T]",
            output_shape="[batch, T/P, d] plus codes [batch, T/P] of ints in [0, K)",
            complexity=("Encoding as continuous_patch plus O(batch * (T/P) * K * d) "
                        "for the nearest-code search. Parameters CPd + Kd + 3d."),
            reconstruction_objective=(
                "Decode from the quantised latent, not the continuous one. "
                "Decoding from z would measure the projection and hide the "
                "quantiser, which is the component under test."),
            forecasting_compatibility=(
                "Direct, and additionally supports a categorical head over "
                "codes. The straight-through estimator makes the encoder "
                "trainable end to end with a forecasting loss."),
            limitations=(
                "Imposes a hard error floor: no move finer than a codebook "
                "cell is representable, and a risk engine reasoning in basis "
                "points is downstream.",
                "Codebook collapse is a real failure mode and is invisible in "
                "the loss; `utilisation()` is provided because of it.",
                "Adds two loss terms and a commitment coefficient to tune, on "
                "an environment with no compute budget for tuning."),
            prior_art=("Vector quantisation with a straight-through estimator, "
                       "as used in VQ-VAE-family models. A standard published "
                       "technique; no novelty claimed."),
            independence=(
                "The codebook is initialised from a seeded Olympus RNG in "
                "`torchutil.configure_determinism` and trained only on Olympus "
                "datasets. No external vocabulary or checkpoint is loaded — "
                "`checkpoint.assert_olympus_origin` refuses one by shape."),
            tokenises=True, mask_policy=MaskPolicy.REFUSE),

        RepresentationCandidate(
            key="residual_vq",
            title="Residual vector quantisation",
            rationale=(
                "The direct answer to the error floor that `learned_tokens` "
                "imposes: quantise the residual too. S stages of K codes give "
                "K^S effective cells for S*K*d parameters instead of K^S*d, "
                "which is the only version of a large alphabet that fits in "
                "this compute budget."),
            formulation=(
                "r_0 = z.  For s = 1..S:  k_s = argmin_k ||r_{s-1} - E^s_k||^2,\n"
                "q_s = E^s_{k_s},  r_s = r_{s-1} - q_s.\n"
                "q = sum_s q_s.  Straight-through as in learned_tokens.\n"
                "Codes are an S-tuple per position."),
            trainable_components=("the patch projection",
                                  "S codebooks E^s (K x d each)",
                                  "LayerNorm gain and bias"),
            input_shape="[batch, C, T]",
            output_shape="[batch, T/P, d] plus codes [batch, T/P, S]",
            complexity=("O(batch * (T/P) * S * K * d) for the search. "
                        "Parameters CPd + S*Kd + 3d. Effective vocabulary K^S."),
            reconstruction_objective=(
                "Decode from the summed quantised latent. Reported alongside "
                "the single-stage error so the residual stage's contribution is "
                "visible rather than assumed."),
            forecasting_compatibility=(
                "Direct. A categorical head would need S heads, one per stage, "
                "which is a cost worth stating."),
            limitations=(
                "Later stages see progressively smaller residuals and can "
                "collapse to a single code while the loss still improves.",
                "Stage order is fixed and not learned, so a badly scaled first "
                "stage constrains everything after it.",
                "S times the search cost of a single codebook."),
            prior_art=("Residual vector quantisation, long established in "
                       "speech and audio coding and used in recent neural "
                       "codecs. No novelty claimed."),
            independence=(
                "Same as `learned_tokens`: seeded initialisation, Olympus-only "
                "training data, no external codebook."),
            tokenises=True, mask_policy=MaskPolicy.REFUSE),

        RepresentationCandidate(
            key="grouped_embeddings",
            title="Separate embeddings per channel group",
            rationale=(
                "Price, volume, liquidity and event channels have different "
                "units, different missingness and different availability. One "
                "shared projection forces them through the same weights and "
                "makes an absent liquidity group indistinguishable from a quiet "
                "one. Separate projections keep the groups addressable, which "
                "is what lets a group be dropped when it is not obtainable."),
            formulation=(
                "Partition channels into groups G_1..G_M.\n"
                "For each group: fold its own channels and project with W_m to "
                "d_m, where sum_m d_m = d.\n"
                "z[p] = LayerNorm(concat_m z^{(m)}[p])."),
            trainable_components=("one projection W_m and bias per non-empty group",
                                  "output LayerNorm gain and bias"),
            input_shape="[batch, C, T] with channels ordered by group",
            output_shape="[batch, T/P, d]",
            complexity=("O(batch * (T/P) * P * d * sum_m |G_m|) = the same "
                        "multiply-accumulate count as continuous_patch, since "
                        "the partition is exact. Parameters sum_m (|G_m| P d_m) "
                        "+ sum_m d_m + 2d, which is *fewer* than "
                        "continuous_patch: the block-diagonal structure removes "
                        "the cross-group weights."),
            reconstruction_objective=(
                "Per-group linear decoders reconstructing only their own "
                "channels; total error is the channel-weighted mean, so a group "
                "with one channel cannot dominate."),
            forecasting_compatibility=(
                "Direct, and the only candidate that degrades gracefully when a "
                "whole group is unobtainable — the group's width is dropped and "
                "the rest is unaffected."),
            limitations=(
                "Cannot represent an interaction between a price channel and a "
                "volume channel within a patch; the block-diagonal structure "
                "forbids it. Any such interaction must be learned in the trunk.",
                "In this environment two of the four declared groups are empty, "
                "because their channels are NOT_INGESTED (blocker B1), so the "
                "candidate is being evaluated at a fraction of its intended "
                "width.",
                "Requires a channel-to-group map that must stay in step with "
                "the schema."),
            prior_art=("Field-wise or modality-wise embeddings, standard in "
                       "tabular and multimodal models. No novelty claimed."),
            independence=("Groups are defined over Olympus's own channel "
                          "schema; the partition is this repository's."),
            mask_policy=MaskPolicy.EXCLUDE),

        RepresentationCandidate(
            key="mask_aware",
            title="Mask-aware continuous encoder",
            rationale=(
                "The candidate that takes `schema.MissingPolicy` seriously. "
                "Twenty-one of thirty-eight declared channels are obtainable "
                "here and the rest are not; a representation that cannot say "
                "'this channel was absent' has to be fed a fabricated value, "
                "which is the failure the whole schema exists to prevent."),
            formulation=(
                "Inputs x in R^{C x T} and m in {0,1}^{C x T}.\n"
                "Neutralise: x~ = x * m  (absent positions take the encoder's "
                "neutral element, which is recoverable because m travels with "
                "it).\n"
                "Concatenate: u = [x~ ; m] in R^{2C x T}, fold and project.\n"
                "Add absence: z[p] += sum_c (1 - mbar_c[p]) * a_c, where "
                "mbar_c[p] = min over the patch and a_c in R^d is learned.\n"
                "z[p] = LayerNorm(z[p])."),
            trainable_components=("projection over 2C channels",
                                  "per-channel absent embedding A (C x d)",
                                  "LayerNorm gain and bias"),
            input_shape="[batch, C, T] values and [batch, C, T] mask",
            output_shape="[batch, T/P, d] plus a per-position validity mask",
            complexity=("Twice continuous_patch's projection cost, because the "
                        "channel count doubles: O(batch * (T/P) * 2CP * d). "
                        "Parameters 2CPd + Cd + 3d."),
            reconstruction_objective=(
                "Masked mean squared error: positions where m = 0 are excluded "
                "from the loss entirely rather than scored against zero. "
                "Scoring them would reward the decoder for agreeing with a "
                "value nobody observed."),
            forecasting_compatibility=(
                "Direct, and the only candidate that can be served when an "
                "input channel drops out at inference — which is the realistic "
                "failure, not a hypothetical one."),
            limitations=(
                "Doubles the input width for a benefit that is zero when "
                "nothing is missing, so on a fully-observed dataset it is "
                "strictly more expensive than continuous_patch for no gain.",
                "The absent embedding is per channel, not per (channel, "
                "duration), so a channel absent for one bar and for fifty is "
                "represented identically.",
                "Patch-level absence uses the minimum over the patch, so one "
                "absent bar marks the whole patch absent — deliberately "
                "conservative, and it discards the partially-observed case."),
            prior_art=("Masking and missingness indicators are standard "
                       "practice; learned absent-embeddings appear across the "
                       "missing-data literature. No novelty claimed."),
            independence=("Built on Olympus's own `ChannelObservation` mask "
                          "semantics, which no external model shares."),
            mask_policy=MaskPolicy.LEARNED_ABSENT),

        RepresentationCandidate(
            key="hybrid",
            title="Hybrid continuous and discrete",
            rationale=(
                "If the discrete path's alphabet is genuinely informative and "
                "its error floor is genuinely costly, keeping both and letting "
                "a projection weigh them is the arrangement that pays for the "
                "alphabet without paying the floor. Worth implementing "
                "precisely because it is the one that sounds like a compromise "
                "and may be strictly better or strictly worse."),
            formulation=(
                "z_c = continuous patch latent.  z_q = quantised latent from a "
                "codebook over z_c.\n"
                "z = LayerNorm(W_f [z_c ; z_q] + b_f),  W_f in R^{d x 2d}."),
            trainable_components=("the patch projection", "the codebook",
                                  "the fusion projection W_f and bias",
                                  "LayerNorm gain and bias"),
            input_shape="[batch, C, T]",
            output_shape="[batch, T/P, d] plus codes [batch, T/P]",
            complexity=("learned_tokens plus O(batch * (T/P) * 2d * d) for "
                        "fusion. Parameters CPd + Kd + 2d^2 + 4d."),
            reconstruction_objective=(
                "Decode from the fused latent. Reported beside both parents' "
                "errors, because a hybrid that beats neither parent is a "
                "negative result worth having."),
            forecasting_compatibility=(
                "Direct, with codes available for a categorical auxiliary head."),
            limitations=(
                "Most parameters of any candidate here, on the tightest compute "
                "budget.",
                "The fusion can learn to ignore the discrete branch entirely, "
                "in which case it is continuous_patch with extra cost — and "
                "nothing in the loss reports that it has happened.",
                "Inherits codebook collapse from its discrete parent."),
            prior_art=("Hybrid continuous/discrete latents appear in several "
                       "published model families. No novelty claimed."),
            independence=("Composition of two candidates in this file; nothing "
                          "external is transcribed."),
            tokenises=True, mask_policy=MaskPolicy.REFUSE),
    )
}


@dataclass(frozen=True)
class RepresentationConfig:
    """Shape and hyper-parameters, shared across candidates.

    Shared on purpose: a comparison in which each candidate chose its own
    lookback would compare lookbacks.
    """

    lookback: int = 16
    patch_len: int = 4
    d_model: int = 32
    channels: tuple[str, ...] = CHANNELS
    #: `multi_resolution_patch` only.
    patch_lens: tuple[int, ...] = (2, 4, 8)
    #: `learned_tokens`, `residual_vq`, `hybrid`.
    codebook_size: int = 32
    #: `residual_vq` only.
    residual_stages: int = 2
    #: Commitment coefficient for the quantised candidates.
    commitment: float = 0.25
    #: `grouped_embeddings` only.
    groups: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_GROUPS))

    def __post_init__(self):
        for name in ("lookback", "patch_len", "d_model", "codebook_size",
                     "residual_stages"):
            if int(getattr(self, name)) < 1:
                raise ConfigurationError(f"{name} must be >= 1",
                                         **{name: getattr(self, name)})
        object.__setattr__(self, "channels", tuple(self.channels))
        object.__setattr__(self, "patch_lens", tuple(sorted(set(self.patch_lens))))
        object.__setattr__(self, "groups",
                           {k: tuple(v) for k, v in self.groups.items()})
        if not self.channels:
            raise ConfigurationError("a representation needs channels")
        if self.lookback % self.patch_len != 0:
            raise ConfigurationError(
                "lookback must be a whole number of patches",
                lookback=self.lookback, patch_len=self.patch_len)
        for length in self.patch_lens:
            if length < 1 or self.lookback % length != 0:
                raise ConfigurationError(
                    "every resolution's patch length must divide the lookback",
                    lookback=self.lookback, patch_lens=list(self.patch_lens))
        if not 0.0 <= self.commitment <= 10.0:
            raise ConfigurationError("commitment must be in [0, 10]",
                                     commitment=self.commitment)
        grouped = [c for names in self.groups.values() for c in names]
        if len(grouped) != len(set(grouped)):
            raise ConfigurationError("a channel appears in two groups",
                                     groups={k: list(v) for k, v in self.groups.items()})
        unknown = [c for c in grouped if c not in self.channels]
        if unknown:
            raise ConfigurationError("a group names a channel this config does "
                                     "not carry", unknown=unknown)

    @property
    def n_channels(self) -> int:
        return len(self.channels)

    @property
    def n_patches(self) -> int:
        return self.lookback // self.patch_len

    def to_dict(self) -> dict:
        return {"lookback": self.lookback, "patch_len": self.patch_len,
                "d_model": self.d_model, "channels": list(self.channels),
                "patch_lens": list(self.patch_lens),
                "codebook_size": self.codebook_size,
                "residual_stages": self.residual_stages,
                "commitment": self.commitment,
                "groups": {k: list(v) for k, v in sorted(self.groups.items())}}


# ===========================================================================
# the torch modules
# ===========================================================================

def _module_factories() -> Mapping[str, Callable[[RepresentationConfig], Any]]:
    """Build every candidate's module class once, with torch imported here.

    One function holding all seven so torch is imported exactly once per
    process and the classes are created inside a `cached_class` — defining an
    `nn.Module` subclass at module scope would import torch at module scope,
    which `tests/test_trading_independence.py` forbids.
    """
    torch = require_torch()
    nn = torch.nn

    def _make_classes():
        class _Base(nn.Module):
            """Shared plumbing: shape checks and the reconstruction decoder.

            `forward` returns `(latent, codes, aux)` for every candidate, so the
            wrapper below has one code path rather than seven.
            """

            positions: int

            def check(self, x):
                if x.dim() != 3:
                    raise ValueError(f"expected [batch, channels, time], got "
                                     f"{tuple(x.shape)}")
                return x

            def forward(self, x, mask=None):             # pragma: no cover
                raise NotImplementedError

            def decode(self, z):                         # pragma: no cover
                raise NotImplementedError

        class PatchProjection(nn.Module):
            """Fold `patch_len` timesteps and project. The shared primitive.

            The fold itself is parameter-free; `self.project` and `self.norm`
            below hold this module's parameters.
            """

            def __init__(self, n_channels: int, patch_len: int, d_model: int):
                super().__init__()
                self.n_channels = n_channels
                self.patch_len = patch_len
                self.project = nn.Linear(n_channels * patch_len, d_model)
                self.norm = nn.LayerNorm(d_model)

            def fold(self, x):
                batch, channels, time = x.shape
                if time % self.patch_len:
                    raise ValueError(f"time {time} is not a whole number of "
                                     f"{self.patch_len}-bar patches")
                patches = time // self.patch_len
                return (x.reshape(batch, channels, patches, self.patch_len)
                         .permute(0, 2, 1, 3)
                         .reshape(batch, patches, channels * self.patch_len))

            def forward(self, x):
                return self.norm(self.project(self.fold(x)))

        class ContinuousPatch(_Base):
            def __init__(self, config: RepresentationConfig):
                super().__init__()
                self.encode_ = PatchProjection(config.n_channels,
                                               config.patch_len, config.d_model)
                self.decode_ = nn.Linear(config.d_model,
                                         config.n_channels * config.patch_len)
                self.patch_len = config.patch_len
                self.n_channels = config.n_channels
                self.positions = config.n_patches

            def forward(self, x, mask=None):
                return self.encode_(self.check(x)), None, {}

            def decode(self, z):
                flat = self.decode_(z)                   # [B, P, C*L]
                batch, patches, _ = flat.shape
                return (flat.reshape(batch, patches, self.n_channels, self.patch_len)
                            .permute(0, 2, 1, 3)
                            .reshape(batch, self.n_channels,
                                     patches * self.patch_len))

        class MultiResolutionPatch(_Base):
            def __init__(self, config: RepresentationConfig):
                super().__init__()
                self.patch_lens = list(config.patch_lens)
                self.n_channels = config.n_channels
                self.encoders = nn.ModuleList(
                    [PatchProjection(config.n_channels, length, config.d_model)
                     for length in self.patch_lens])
                self.decoders = nn.ModuleList(
                    [nn.Linear(config.d_model, config.n_channels * length)
                     for length in self.patch_lens])
                self.resolution = nn.Parameter(
                    torch.zeros(len(self.patch_lens), config.d_model))
                self.norm = nn.LayerNorm(config.d_model)
                self.counts = [config.lookback // length
                               for length in self.patch_lens]
                self.positions = sum(self.counts)

            def forward(self, x, mask=None):
                self.check(x)
                pieces = [encoder(x) + self.resolution[i]
                          for i, encoder in enumerate(self.encoders)]
                return self.norm(torch.cat(pieces, dim=1)), None, {}

            def decode(self, z):
                # Reconstruct each resolution from its own slice and average.
                # Averaging rather than taking the finest: a candidate that
                # reconstructs only its coarsest view should score as one that
                # reconstructs only its coarsest view.
                out, start = [], 0
                for i, length in enumerate(self.patch_lens):
                    count = self.counts[i]
                    piece = z[:, start:start + count, :]
                    start += count
                    flat = self.decoders[i](piece)
                    batch = flat.shape[0]
                    out.append(flat.reshape(batch, count, self.n_channels, length)
                                   .permute(0, 2, 1, 3)
                                   .reshape(batch, self.n_channels, count * length))
                return torch.stack(out, dim=0).mean(dim=0)

        class VectorQuantiser(nn.Module):
            """One codebook, nearest-neighbour lookup, straight-through."""

            def __init__(self, size: int, d_model: int, commitment: float):
                super().__init__()
                self.codebook = nn.Parameter(torch.randn(size, d_model) * 0.1)
                self.size = size
                self.commitment = commitment

            def forward(self, z):
                flat = z.reshape(-1, z.shape[-1])
                # ||z - e||^2 expanded so the cross term is one matmul.
                distances = ((flat ** 2).sum(1, keepdim=True)
                             - 2.0 * flat @ self.codebook.t()
                             + (self.codebook ** 2).sum(1))
                codes = distances.argmin(dim=1)
                quantised = self.codebook[codes].reshape(z.shape)
                codebook_loss = ((quantised - z.detach()) ** 2).mean()
                commit_loss = ((z - quantised.detach()) ** 2).mean()
                # Straight-through: forward uses q, backward passes to z.
                passthrough = z + (quantised - z).detach()
                return (passthrough, codes.reshape(z.shape[:-1]),
                        codebook_loss + self.commitment * commit_loss)

        class LearnedTokens(_Base):
            def __init__(self, config: RepresentationConfig):
                super().__init__()
                self.encode_ = PatchProjection(config.n_channels,
                                               config.patch_len, config.d_model)
                self.quantiser = VectorQuantiser(config.codebook_size,
                                                 config.d_model,
                                                 config.commitment)
                self.decode_ = nn.Linear(config.d_model,
                                         config.n_channels * config.patch_len)
                self.patch_len = config.patch_len
                self.n_channels = config.n_channels
                self.positions = config.n_patches

            def forward(self, x, mask=None):
                z = self.encode_(self.check(x))
                quantised, codes, loss = self.quantiser(z)
                return quantised, codes, {"quantisation_loss": loss}

            def decode(self, z):
                flat = self.decode_(z)
                batch, patches, _ = flat.shape
                return (flat.reshape(batch, patches, self.n_channels, self.patch_len)
                            .permute(0, 2, 1, 3)
                            .reshape(batch, self.n_channels,
                                     patches * self.patch_len))

        class ResidualVQ(_Base):
            def __init__(self, config: RepresentationConfig):
                super().__init__()
                self.encode_ = PatchProjection(config.n_channels,
                                               config.patch_len, config.d_model)
                self.stages = nn.ModuleList(
                    [VectorQuantiser(config.codebook_size, config.d_model,
                                     config.commitment)
                     for _ in range(config.residual_stages)])
                self.decode_ = nn.Linear(config.d_model,
                                         config.n_channels * config.patch_len)
                self.patch_len = config.patch_len
                self.n_channels = config.n_channels
                self.positions = config.n_patches

            def forward(self, x, mask=None):
                z = self.encode_(self.check(x))
                residual, total, codes = z, None, []
                loss = z.new_zeros(())
                for stage in self.stages:
                    quantised, code, stage_loss = stage(residual)
                    total = quantised if total is None else total + quantised
                    residual = residual - quantised
                    codes.append(code)
                    loss = loss + stage_loss
                return total, torch.stack(codes, dim=-1), {"quantisation_loss": loss}

            def decode(self, z):
                flat = self.decode_(z)
                batch, patches, _ = flat.shape
                return (flat.reshape(batch, patches, self.n_channels, self.patch_len)
                            .permute(0, 2, 1, 3)
                            .reshape(batch, self.n_channels,
                                     patches * self.patch_len))

        class GroupedEmbeddings(_Base):
            def __init__(self, config: RepresentationConfig):
                super().__init__()
                index = {name: i for i, name in enumerate(config.channels)}
                self.group_names, self.indices, widths = [], [], []
                populated = [(name, members)
                             for name, members in sorted(config.groups.items())
                             if members]
                if not populated:
                    raise ConfigurationError(
                        "every declared channel group is empty; there is "
                        "nothing to embed", groups=sorted(config.groups))
                # Width is split in proportion to channel count, so a group with
                # three channels is not squeezed into the same width as one with
                # one. The remainder goes to the last group rather than being
                # dropped, because a d_model the caller asked for must be the
                # d_model they get.
                total_channels = sum(len(m) for _, m in populated)
                for i, (name, members) in enumerate(populated):
                    self.group_names.append(name)
                    self.indices.append([index[c] for c in members])
                    if i == len(populated) - 1:
                        widths.append(config.d_model - sum(widths))
                    else:
                        widths.append(max(1, round(config.d_model
                                                   * len(members) / total_channels)))
                if any(w < 1 for w in widths):
                    raise ConfigurationError(
                        "d_model is too small to give every group a width",
                        d_model=config.d_model, groups=self.group_names)
                self.widths = widths
                self.encoders = nn.ModuleList(
                    [PatchProjection(len(idx), config.patch_len, width)
                     for idx, width in zip(self.indices, widths)])
                self.decoders = nn.ModuleList(
                    [nn.Linear(width, len(idx) * config.patch_len)
                     for idx, width in zip(self.indices, widths)])
                self.norm = nn.LayerNorm(config.d_model)
                self.patch_len = config.patch_len
                self.n_channels = config.n_channels
                self.positions = config.n_patches

            def forward(self, x, mask=None):
                self.check(x)
                pieces = []
                for idx, encoder in zip(self.indices, self.encoders):
                    columns = x[:, idx, :]
                    pieces.append(encoder(columns))
                return self.norm(torch.cat(pieces, dim=-1)), None, {}

            def decode(self, z):
                batch, patches, _ = z.shape
                out = z.new_zeros(batch, self.n_channels,
                                  patches * self.patch_len)
                start = 0
                for idx, width, decoder in zip(self.indices, self.widths,
                                               self.decoders):
                    piece = z[:, :, start:start + width]
                    start += width
                    flat = decoder(piece)
                    columns = (flat.reshape(batch, patches, len(idx), self.patch_len)
                                   .permute(0, 2, 1, 3)
                                   .reshape(batch, len(idx),
                                            patches * self.patch_len))
                    out[:, idx, :] = columns
                return out

        class MaskAware(_Base):
            def __init__(self, config: RepresentationConfig):
                super().__init__()
                # 2C channels: the values (neutralised where absent) and the
                # mask itself, so absence is recoverable from the input.
                self.encode_ = PatchProjection(2 * config.n_channels,
                                               config.patch_len, config.d_model)
                self.absent = nn.Parameter(
                    torch.zeros(config.n_channels, config.d_model))
                self.norm = nn.LayerNorm(config.d_model)
                self.decode_ = nn.Linear(config.d_model,
                                         config.n_channels * config.patch_len)
                self.patch_len = config.patch_len
                self.n_channels = config.n_channels
                self.positions = config.n_patches

            def forward(self, x, mask=None):
                self.check(x)
                if mask is None:
                    mask = torch.ones_like(x)
                if mask.shape != x.shape:
                    raise ValueError(f"mask shape {tuple(mask.shape)} does not "
                                     f"match values {tuple(x.shape)}")
                mask = mask.to(x.dtype)
                # Absent positions take the neutral element. This is not a
                # fabricated observation: the mask travels alongside in the very
                # next C channels, and the learned absent vector is added below.
                neutralised = x * mask
                z = self.encode_(torch.cat([neutralised, mask], dim=1))
                batch, channels, time = mask.shape
                patches = time // self.patch_len
                # Conservative: one absent bar marks the whole patch absent.
                patch_mask = (mask.reshape(batch, channels, patches, self.patch_len)
                                  .min(dim=-1).values)              # [B, C, P]
                absence = (1.0 - patch_mask).permute(0, 2, 1) @ self.absent
                latent = self.norm(z + absence)
                valid = patch_mask.min(dim=1).values                # [B, P]
                return latent, None, {"valid": valid, "patch_mask": patch_mask}

            def decode(self, z):
                flat = self.decode_(z)
                batch, patches, _ = flat.shape
                return (flat.reshape(batch, patches, self.n_channels, self.patch_len)
                            .permute(0, 2, 1, 3)
                            .reshape(batch, self.n_channels,
                                     patches * self.patch_len))

        class Hybrid(_Base):
            def __init__(self, config: RepresentationConfig):
                super().__init__()
                self.encode_ = PatchProjection(config.n_channels,
                                               config.patch_len, config.d_model)
                self.quantiser = VectorQuantiser(config.codebook_size,
                                                 config.d_model,
                                                 config.commitment)
                self.fuse = nn.Linear(2 * config.d_model, config.d_model)
                self.norm = nn.LayerNorm(config.d_model)
                self.decode_ = nn.Linear(config.d_model,
                                         config.n_channels * config.patch_len)
                self.patch_len = config.patch_len
                self.n_channels = config.n_channels
                self.positions = config.n_patches

            def forward(self, x, mask=None):
                z = self.encode_(self.check(x))
                quantised, codes, loss = self.quantiser(z)
                fused = self.norm(self.fuse(torch.cat([z, quantised], dim=-1)))
                return fused, codes, {"quantisation_loss": loss}

            def decode(self, z):
                flat = self.decode_(z)
                batch, patches, _ = flat.shape
                return (flat.reshape(batch, patches, self.n_channels, self.patch_len)
                            .permute(0, 2, 1, 3)
                            .reshape(batch, self.n_channels,
                                     patches * self.patch_len))

        return {"continuous_patch": ContinuousPatch,
                "multi_resolution_patch": MultiResolutionPatch,
                "learned_tokens": LearnedTokens,
                "residual_vq": ResidualVQ,
                "grouped_embeddings": GroupedEmbeddings,
                "mask_aware": MaskAware,
                "hybrid": Hybrid}

    return cached_class("representation_modules", _make_classes)


# ===========================================================================
# the wrapper: a torch module behind the stdlib contract
# ===========================================================================

class TorchRepresentation(MarketEncoder, Reconstructor):
    """A built candidate, behind `interfaces.MarketEncoder`.

    Holds the torch module rather than being one, so that everything a consumer
    touches — metadata, latent spec, identity — is reachable with torch absent
    right up to the moment a forward pass is requested.
    """

    def __init__(self, key: str, module: Any, config: RepresentationConfig, *,
                 schema_fingerprint: str = ""):
        self.key = key
        self.module = module
        self.config = config
        self.candidate = CANDIDATES[key]
        trainable = sum(p.numel() for p in module.parameters()
                        if p.requires_grad)
        spec = LatentSpec(
            positions=module.positions, d_model=config.d_model,
            # Multi-resolution concatenates several time orders, so its
            # positions are not one sequence. Declaring that here is what stops
            # a causal trunk being applied across the join.
            sequential=(key != "multi_resolution_patch"),
            emits_mask=(key == "mask_aware"))
        self._metadata = EncoderMetadata(
            name=f"olympus.native.repr.{key}",
            version=REPRESENTATION_VERSION, latent=spec,
            channels=config.channels,
            schema_fingerprint=schema_fingerprint or OLYMPUS_MARKET_SCHEMA.fingerprint(),
            mask_policy=self.candidate.mask_policy,
            trainable_parameters=trainable, params=config.to_dict(),
            reconstructs=True, tokenises=self.candidate.tokenises)

    @property
    def metadata(self) -> EncoderMetadata:
        return self._metadata

    #: Policies whose encoder actually reads a runtime mask. `EXCLUDE` is not
    #: among them: it drops the channel at configuration time, so a mask handed
    #: to it at call time has nothing to act on.
    _CONSUMES_MASK = (MaskPolicy.LEARNED_ABSENT, MaskPolicy.MASK_CHANNEL)

    def encode(self, batch: Any, *, mask: Any = None) -> EncodedBatch:
        if mask is not None and self.candidate.mask_policy not in self._CONSUMES_MASK:
            raise ConfigurationError(
                "this representation does not consume a mask; accepting one "
                "would silently discard it and the caller would believe "
                "missingness had been handled",
                representation=self.key,
                mask_policy=self.candidate.mask_policy.value)
        latent, codes, aux = self.module(batch, mask)
        return EncodedBatch(latent=latent, spec=self._metadata.latent,
                            metadata=self._metadata,
                            valid=aux.get("valid"), codes=codes,
                            side_channel={k: v for k, v in aux.items()
                                          if k != "valid"})

    # -- Reconstructor -----------------------------------------------------

    def reconstruct(self, encoded: EncodedBatch) -> Any:
        return self.module.decode(encoded.latent)

    def reconstruction_error(self, encoded: EncodedBatch, original: Any, *,
                             mask: Any = None) -> float:
        rebuilt = self.reconstruct(encoded)
        if rebuilt.shape != original.shape:
            raise ConfigurationError(
                "reconstruction shape does not match the input",
                got=tuple(rebuilt.shape), expected=tuple(original.shape),
                representation=self.key)
        squared = (rebuilt - original) ** 2
        if mask is None:
            return float(squared.mean().item())
        weights = mask.to(squared.dtype)
        denominator = weights.sum()
        if float(denominator.item()) <= 0:
            raise ConfigurationError(
                "every position is masked; there is nothing to score",
                representation=self.key)
        # Masked positions are excluded, not scored as zero error. Scoring them
        # would reward the decoder for agreeing with a value nobody observed.
        return float(((squared * weights).sum() / denominator).item())

    def parameters_dict(self) -> Mapping[str, Any]:
        from .torchutil import state_dict_to_json
        return state_dict_to_json(self.module)

    def load_parameters(self, values: Mapping[str, Any]) -> None:
        from .torchutil import json_to_state_dict
        json_to_state_dict(values, self.module)

    def __repr__(self) -> str:                           # pragma: no cover
        return (f"TorchRepresentation({self.key}, "
                f"{self._metadata.latent.shape}, "
                f"{self._metadata.trainable_parameters} params)")


class QuantisedRepresentation(TorchRepresentation, Tokeniser):
    """A candidate that also emits codes. Adds the `Tokeniser` contract."""

    @property
    def vocabulary_size(self) -> int:
        if self.key == "residual_vq":
            return self.config.codebook_size ** self.config.residual_stages
        return self.config.codebook_size

    @property
    def levels(self) -> int:
        return self.config.residual_stages if self.key == "residual_vq" else 1

    def tokenise(self, latent: Any) -> Any:
        quantiser = getattr(self.module, "quantiser", None)
        if quantiser is not None:
            return quantiser(latent)[1]
        codes = []
        residual = latent
        for stage in self.module.stages:
            quantised, code, _ = stage(residual)
            residual = residual - quantised
            codes.append(code)
        torch = require_torch()
        return torch.stack(codes, dim=-1)

    def detokenise(self, codes: Any) -> Any:
        quantiser = getattr(self.module, "quantiser", None)
        if quantiser is not None:
            return quantiser.codebook[codes]
        total = None
        for i, stage in enumerate(self.module.stages):
            piece = stage.codebook[codes[..., i]]
            total = piece if total is None else total + piece
        return total

    def utilisation(self, codes: Any) -> float | None:
        """Fraction of the codebook actually used. Reported per stage, minimum.

        The minimum rather than the mean: a residual scheme whose second stage
        has collapsed to one code has collapsed, and averaging that against a
        healthy first stage would hide it.
        """
        size = self.config.codebook_size
        if self.levels > 1:
            # `[..., stages]` — score each stage separately and take the worst.
            per_stage = codes.reshape(-1, codes.shape[-1])
            return min(len(set(per_stage[:, i].tolist())) / size
                       for i in range(per_stage.shape[-1]))
        return len(set(codes.reshape(-1).tolist())) / size


def build_representation(key: str, config: RepresentationConfig | None = None, *,
                         schema_fingerprint: str = "") -> TorchRepresentation:
    """Build one candidate. Torch is required here and nowhere above."""
    if key not in CANDIDATES:
        raise ConfigurationError("unknown representation candidate",
                                 candidate=key, known=sorted(CANDIDATES))
    settings = config or RepresentationConfig()
    classes = _module_factories()
    module = classes[key](settings)
    wrapper = (QuantisedRepresentation if CANDIDATES[key].tokenises
               else TorchRepresentation)
    return wrapper(key, module, settings, schema_fingerprint=schema_fingerprint)


# ===========================================================================
# the comparison
# ===========================================================================

@dataclass(frozen=True)
class RepresentationResult:
    """One candidate's measured behaviour. Comparable across candidates."""

    key: str
    parameters: int
    positions: int
    #: Reconstruction MSE on held-out windows, after training.
    reconstruction_error: float
    #: The same before training. A candidate whose error does not fall has not
    #: learned, and the trained number alone cannot show that.
    initial_error: float
    epochs: int
    seconds: float
    codebook_utilisation: float | None = None
    notes: str = ""

    @property
    def improvement(self) -> float:
        return self.initial_error - self.reconstruction_error

    @property
    def learned(self) -> bool:
        return self.improvement > 0.0

    @property
    def error_per_parameter(self) -> float:
        """Reconstruction error scaled by parameter count.

        A parsimony measure, not a headline: a candidate that reconstructs
        marginally better for four times the parameters has not obviously won,
        and this is the number that says so.
        """
        return self.reconstruction_error * max(1, self.parameters)

    def to_dict(self) -> dict:
        return {"key": self.key, "parameters": self.parameters,
                "positions": self.positions,
                "reconstruction_error": self.reconstruction_error,
                "initial_error": self.initial_error,
                "improvement": self.improvement, "learned": self.learned,
                "epochs": self.epochs, "seconds": round(self.seconds, 3),
                "codebook_utilisation": self.codebook_utilisation,
                "error_per_parameter": self.error_per_parameter,
                "notes": self.notes}


def windows_to_tensor(windows: Sequence[Any], config: RepresentationConfig):
    """Windows → `[batch, channels, time]`, normalised per window.

    Uses `encoder.bars_to_channels` and `encoder.normalise_window`, so a
    representation comparison and a live forecast see the same arithmetic. Two
    implementations of "turn bars into channels" would be two chances to
    disagree, and the disagreement would look like a modelling result.
    """
    torch = require_torch()
    rows = []
    for window in windows:
        channels = bars_to_channels(window.inputs)
        normalised, _ = normalise_window(channels)
        trimmed = [column[-config.lookback:] for column in normalised]
        if any(len(column) != config.lookback for column in trimmed):
            raise ConfigurationError(
                "a window is shorter than the configured lookback",
                have=min(len(c) for c in trimmed), need=config.lookback)
        rows.append(trimmed)
    if not rows:
        raise ConfigurationError("no windows to encode")
    return torch.tensor(rows, dtype=torch.float32)


def evaluate_representation(key: str, train_tensor: Any, test_tensor: Any, *,
                            config: RepresentationConfig | None = None,
                            epochs: int = 40, batch_size: int = 32,
                            learning_rate: float = 1e-2, seed: int = 0
                            ) -> RepresentationResult:
    """Train one candidate on a reconstruction objective and score it held out.

    Reconstruction rather than forecasting, deliberately. A forecasting score
    confounds the representation with the trunk; reconstruction asks the one
    question a representation is responsible for — what did it keep?
    """
    from .torchutil import configure_determinism
    torch = require_torch()
    settings = config or RepresentationConfig()
    generator = configure_determinism(seed)
    representation = build_representation(key, settings)
    module = representation.module

    with torch.no_grad():
        encoded = representation.encode(test_tensor)
        initial = representation.reconstruction_error(encoded, test_tensor)

    optimiser = torch.optim.Adam(module.parameters(), lr=learning_rate)
    started = time.time()
    rows = train_tensor.shape[0]
    module.train()
    for _ in range(epochs):
        order = torch.randperm(rows, generator=generator)
        for start in range(0, rows, batch_size):
            batch = train_tensor[order[start:start + batch_size]]
            optimiser.zero_grad()
            latent, _, aux = module(batch, None)
            rebuilt = module.decode(latent)
            loss = ((rebuilt - batch) ** 2).mean()
            extra = aux.get("quantisation_loss")
            if extra is not None:
                loss = loss + extra
            loss.backward()
            optimiser.step()
    module.eval()
    elapsed = time.time() - started

    with torch.no_grad():
        encoded = representation.encode(test_tensor)
        final = representation.reconstruction_error(encoded, test_tensor)
        utilisation = None
        if isinstance(representation, QuantisedRepresentation) and encoded.codes is not None:
            utilisation = representation.utilisation(encoded.codes)

    return RepresentationResult(
        key=key, parameters=representation.metadata.trainable_parameters,
        positions=representation.latent_spec.positions,
        reconstruction_error=final, initial_error=initial, epochs=epochs,
        seconds=elapsed, codebook_utilisation=utilisation)


def compare_representations(train_tensor: Any, test_tensor: Any, *,
                            keys: Sequence[str] = (),
                            config: RepresentationConfig | None = None,
                            **kwargs: Any) -> list[RepresentationResult]:
    """Every candidate, same data, same objective, same budget.

    Ordered by reconstruction error, but the ordering is not the conclusion:
    the candidates differ in position count and parameter count, and a
    comparison that reports only the error would let the largest model win by
    being largest. `error_per_parameter` is reported beside it for that reason.
    """
    wanted = tuple(keys) if keys else tuple(CANDIDATES)
    unknown = [k for k in wanted if k not in CANDIDATES]
    if unknown:
        raise ConfigurationError("unknown candidates", unknown=unknown)
    results = [evaluate_representation(key, train_tensor, test_tensor,
                                       config=config, **kwargs)
               for key in wanted]
    return sorted(results, key=lambda r: r.reconstruction_error)


def comparison_table(results: Sequence[RepresentationResult]) -> str:
    """A fixed-width table. For a report, not for a machine."""
    header = (f"{'candidate':<24}{'params':>8}{'pos':>6}{'initial':>11}"
              f"{'final':>11}{'gain':>11}{'util':>7}{'secs':>8}")
    lines = [header, "-" * len(header)]
    for r in results:
        util = "-" if r.codebook_utilisation is None else f"{r.codebook_utilisation:.2f}"
        lines.append(f"{r.key:<24}{r.parameters:>8}{r.positions:>6}"
                     f"{r.initial_error:>11.6f}{r.reconstruction_error:>11.6f}"
                     f"{r.improvement:>11.6f}{util:>7}{r.seconds:>8.2f}")
    return "\n".join(lines)


def candidate_report() -> dict:
    """Every candidate's documentation, as data. What the tests assert on."""
    return {"version": REPRESENTATION_VERSION,
            "candidates": {k: c.to_dict() for k, c in CANDIDATES.items()},
            "implemented": sorted(k for k, c in CANDIDATES.items() if c.implemented),
            "tokenising": sorted(k for k, c in CANDIDATES.items() if c.tokenises)}


__all__ = ["REPRESENTATION_VERSION", "DEFAULT_GROUPS", "RepresentationCandidate",
           "CANDIDATES", "RepresentationConfig", "TorchRepresentation",
           "QuantisedRepresentation", "build_representation",
           "RepresentationResult", "windows_to_tensor", "evaluate_representation",
           "compare_representations", "comparison_table", "candidate_report"]
