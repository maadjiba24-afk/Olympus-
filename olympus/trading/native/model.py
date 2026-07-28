"""The Olympus multi-task market model.

    bars ─► representation ─► identity ─► causal core ─► regime router
                (replaceable)                 │              │
                                              ▼              ▼
                                       cross-asset ───► regime experts
                                        attention           │
                                              │             ▼
                                              └────► timeframe fusion
                                                            │
                                                            ▼
                                                     modular task heads

Why this architecture
=====================
The requirement is an *independently designed* system, so every block below
carries the reason it was chosen and — where it matters — the alternative it
was chosen over. Nothing here is claimed as novel in isolation; patch encoders,
dilated causal convolutions, mixtures of experts, attention and quantile heads
are all published, general techniques. What is Olympus's is the specific
combination and the constraints it was built under, and `modelcard.py` states
that in the form a reader can check.

**Causal convolutions, not attention, for the backbone.** Causality here is
structural: the blocks pad on the left only, so a position *cannot* read a
later one. An attention mask is a line of code that can be wrong, has been
wrong in published implementations, and whose being wrong produces excellent
validation numbers. Cost is also linear rather than quadratic, which on four
cores with no CUDA (blocker B5) is the difference between trainable and not.
The receptive field is an arithmetic fact — `1 + 2(k−1)(2^L − 1)` positions —
rather than something to discover empirically.

**The mixture of experts is routed by the regime head, not by a learned
router.** This is the design decision most worth arguing about. A conventional
MoE learns its own gate and needs load-balancing losses to stop it collapsing
onto one expert. Here the gate *is* the regime classifier's softmax — a head
that is separately supervised by `regime.RegimeClassifier`, which Olympus owns.
Three consequences follow, and the third is a real cost:

1. Routing is interpretable. "Which expert handled this window" has the answer
   "the trending-up one", auditable against a label a human can check.
2. There is no collapse mode and no balancing loss, because the gate's
   distribution is pinned by its own supervision.
3. **The experts can only specialise along an axis the regime classifier
   already sees.** A conventional router might discover a better partition.
   That is given up deliberately, and if a later ablation shows a free router
   wins, the honest response is to switch — `ModelConfig.regime_routing=False`
   exists so the comparison can be run.

**Instrument identity is an additive embedding, and is optional.** A model that
memorises which instrument it is looking at generalises worse to an unseen one,
which is the case `evaluation.py` reports separately. The embedding is there
because per-instrument idiosyncrasy is real; it is small, and
`seen_versus_unseen` is how anyone finds out whether it helped or just
memorised.

**The representation layer is replaceable.** The core consumes anything
implementing `interfaces.MarketEncoder`, so the seven candidates in
`representations.py` are swappable without touching this file. The default is
the continuous patch encoder, on a parsimony argument rather than a measured
win — `docs/OLYMPUS_NATIVE_REPRESENTATIONS.md` §3 is the comparison, and it did
not produce a winner worth adopting.

**Heads are modular and per-task.** Each enabled task in `tasks.TaskConfig`
gets its own head; a disabled task has no parameters at all. A checkpoint's
head bank is therefore a fact about the checkpoint rather than a runtime
switch, which is what stops a model reporting a spread forecast from a head
that was never trained.

What is implemented but cannot be trained here
----------------------------------------------
Cross-asset attention and multi-timeframe fusion are built, shape-tested and
exercised with constructed inputs. Their *tasks* are blocked (`tasks.py`), and
their blocks are inert unless a caller supplies references or extra scales.
What is missing is a corpus, not code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..errors import ConfigurationError
from .interfaces import LatentSpec
from .representations import RepresentationConfig, build_representation
from .tasks import REGIME_CLASSES, TaskConfig, TaskKind
from .torchutil import cached_class, require_torch

#: Bumped when the forward pass changes in a way that alters numbers. A
#: checkpoint records it, and `abstain.py` refuses a mismatch rather than
#: loading weights into a different function.
ARCHITECTURE_VERSION = "1"


@dataclass(frozen=True)
class ModelConfig:
    """Everything that changes the numbers. Recorded verbatim in the manifest."""

    lookback: int = 33
    horizon: int = 3
    quantiles: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9)
    #: Which `representations.CANDIDATES` entry supplies the latent.
    representation: str = "continuous_patch"
    patch_len: int = 4
    d_model: int = 32
    #: Causal core depth. Receptive field is `1 + 2(k-1)(2^L - 1)` positions.
    n_layers: int = 3
    kernel_size: int = 2
    dropout: float = 0.0
    #: Experts per regime class. `regime_routing=False` replaces the supervised
    #: gate with a free learned router, so the comparison can be run.
    n_experts: int = len(REGIME_CLASSES)
    regime_routing: bool = True
    expert_hidden: int = 32
    #: Optional additive instrument embedding. Zero disables it entirely.
    n_instruments: int = 0
    #: Cross-asset attention heads. Zero omits the block.
    cross_asset_heads: int = 0
    #: Timeframes fused, in a fixed order. One entry means no fusion block.
    timeframes: tuple[str, ...] = ()
    tasks: TaskConfig = field(default_factory=TaskConfig)
    architecture_version: str = ARCHITECTURE_VERSION

    def __post_init__(self):
        for name in ("lookback", "horizon", "patch_len", "d_model", "n_layers",
                     "kernel_size", "n_experts", "expert_hidden"):
            if int(getattr(self, name)) < 1:
                raise ConfigurationError(f"{name} must be >= 1",
                                         **{name: getattr(self, name)})
        for name in ("n_instruments", "cross_asset_heads"):
            if int(getattr(self, name)) < 0:
                raise ConfigurationError(f"{name} must be >= 0",
                                         **{name: getattr(self, name)})
        levels = tuple(sorted(float(q) for q in self.quantiles))
        if len(levels) < 2:
            raise ConfigurationError(
                "at least two quantiles are needed; one quantile is a point "
                "forecast wearing a distribution's name", quantiles=list(levels))
        if any(not 0.0 < q < 1.0 for q in levels):
            raise ConfigurationError("quantiles must be in (0, 1)",
                                     quantiles=list(levels))
        object.__setattr__(self, "quantiles", levels)
        object.__setattr__(self, "timeframes", tuple(self.timeframes))
        if not 0.0 <= self.dropout < 1.0:
            raise ConfigurationError("dropout must be in [0, 1)",
                                     dropout=self.dropout)
        if self.cross_asset_heads and self.d_model % self.cross_asset_heads:
            raise ConfigurationError("d_model must divide by cross_asset_heads",
                                     d_model=self.d_model,
                                     cross_asset_heads=self.cross_asset_heads)
        if self.regime_routing:
            if self.n_experts != len(REGIME_CLASSES):
                raise ConfigurationError(
                    "regime routing needs exactly one expert per regime class; "
                    "the gate is the regime head's softmax and its width is "
                    "fixed by the label set",
                    n_experts=self.n_experts, classes=len(REGIME_CLASSES))
            if not self.tasks.is_enabled(TaskKind.REGIME):
                raise ConfigurationError(
                    "regime routing needs the regime head enabled: the head's "
                    "logits *are* the router, so disabling it removes the gate",
                    enabled=[k.value for k in self.tasks.enabled])
        # The representation consumes `lookback - 1` timesteps, because the
        # first bar has no previous close to return against.
        if (self.lookback - 1) % self.patch_len:
            raise ConfigurationError(
                "lookback - 1 must be a whole number of patches; a ragged "
                "final patch would be zero-padded and the model would learn "
                "that history began with a flat market",
                lookback=self.lookback, patch_len=self.patch_len)

    @property
    def n_quantiles(self) -> int:
        return len(self.quantiles)

    @property
    def median_index(self) -> int:
        return min(range(self.n_quantiles),
                   key=lambda i: abs(self.quantiles[i] - 0.5))

    @property
    def n_positions(self) -> int:
        return (self.lookback - 1) // self.patch_len

    @property
    def receptive_field(self) -> int:
        """Positions of history the causal core can reach. Arithmetic, not
        empirical."""
        return 1 + 2 * (self.kernel_size - 1) * (2 ** self.n_layers - 1)

    @property
    def covers_lookback(self) -> bool:
        """Does the core actually see the whole window it is given?

        Reported rather than enforced: a receptive field shorter than the
        window is a legitimate choice, and a *silent* one is not.
        """
        return self.receptive_field >= self.n_positions

    @property
    def representation_config(self) -> RepresentationConfig:
        return RepresentationConfig(lookback=self.lookback - 1,
                                    patch_len=self.patch_len,
                                    d_model=self.d_model)

    def to_dict(self) -> dict:
        return {"lookback": self.lookback, "horizon": self.horizon,
                "quantiles": list(self.quantiles),
                "representation": self.representation,
                "patch_len": self.patch_len, "d_model": self.d_model,
                "n_layers": self.n_layers, "kernel_size": self.kernel_size,
                "dropout": self.dropout, "n_experts": self.n_experts,
                "regime_routing": self.regime_routing,
                "expert_hidden": self.expert_hidden,
                "n_instruments": self.n_instruments,
                "cross_asset_heads": self.cross_asset_heads,
                "timeframes": list(self.timeframes),
                "tasks": self.tasks.to_dict(),
                "architecture_version": self.architecture_version,
                "n_positions": self.n_positions,
                "receptive_field": self.receptive_field,
                "covers_lookback": self.covers_lookback}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelConfig":
        return cls(lookback=int(data.get("lookback", 33)),
                   horizon=int(data.get("horizon", 3)),
                   quantiles=tuple(data.get("quantiles") or (0.1, 0.25, 0.5, 0.75, 0.9)),
                   representation=str(data.get("representation", "continuous_patch")),
                   patch_len=int(data.get("patch_len", 4)),
                   d_model=int(data.get("d_model", 32)),
                   n_layers=int(data.get("n_layers", 3)),
                   kernel_size=int(data.get("kernel_size", 2)),
                   dropout=float(data.get("dropout", 0.0)),
                   n_experts=int(data.get("n_experts", len(REGIME_CLASSES))),
                   regime_routing=bool(data.get("regime_routing", True)),
                   expert_hidden=int(data.get("expert_hidden", 32)),
                   n_instruments=int(data.get("n_instruments", 0)),
                   cross_asset_heads=int(data.get("cross_asset_heads", 0)),
                   timeframes=tuple(data.get("timeframes") or ()),
                   tasks=TaskConfig.from_dict(data.get("tasks") or {}),
                   architecture_version=str(data.get("architecture_version",
                                                     ARCHITECTURE_VERSION)))


def _classes():
    """Every torch module in this file, built once with torch imported here."""
    torch = require_torch()
    nn = torch.nn

    def factory():
        class CausalBlock(nn.Module):
            """Dilated causal convolution with a gated residual.

            Left-padding only, and the right-hand tail is sliced off after the
            convolution. That slice is what makes causality structural: there
            is no arrangement of the weights under which position *t* reads
            position *t+1*, because the values were never in the receptive
            window.

            The gate is a sigmoid over a second projection — cheap, and it lets
            a block pass a position through unchanged, which a plain residual
            stack cannot do without cancelling its own weights.
            """

            def __init__(self, d_model: int, kernel_size: int, dilation: int,
                         dropout: float):
                super().__init__()
                self.pad = (kernel_size - 1) * dilation
                self.conv = nn.Conv1d(d_model, 2 * d_model, kernel_size,
                                      dilation=dilation)
                self.norm = nn.LayerNorm(d_model)
                self.drop = nn.Dropout(dropout) if dropout else nn.Identity()

            def forward(self, x):
                # x: [batch, d_model, positions]
                padded = nn.functional.pad(x, (self.pad, 0))
                projected = self.conv(padded)
                value, gate = projected.chunk(2, dim=1)
                mixed = torch.tanh(value) * torch.sigmoid(gate)
                mixed = self.drop(mixed)
                out = x + mixed
                return self.norm(out.transpose(1, 2)).transpose(1, 2)

        class CausalCore(nn.Module):
            """The backbone. Exponentially dilated causal blocks."""

            def __init__(self, cfg: ModelConfig):
                super().__init__()
                self.blocks = nn.ModuleList([
                    CausalBlock(cfg.d_model, cfg.kernel_size, 2 ** i, cfg.dropout)
                    for i in range(cfg.n_layers)])

            def forward(self, latent):
                # latent: [batch, positions, d_model]
                h = latent.transpose(1, 2)
                for block in self.blocks:
                    h = block(h)
                return h.transpose(1, 2)

        class RegimeExperts(nn.Module):
            """One small feed-forward expert per regime, mixed by the gate.

            A *soft* mixture rather than top-k. Top-k routing saves compute at
            scale; at this scale it saves nothing and introduces a discrete
            decision whose gradient has to be estimated. A soft mixture over
            six experts is six small matmuls, and every expert receives
            gradient on every batch, which is what stops the rare regimes'
            experts from never training.
            """

            def __init__(self, cfg: ModelConfig):
                super().__init__()
                self.experts = nn.ModuleList([
                    nn.Sequential(nn.Linear(cfg.d_model, cfg.expert_hidden),
                                  nn.GELU(),
                                  nn.Linear(cfg.expert_hidden, cfg.d_model))
                    for _ in range(cfg.n_experts)])
                self.norm = nn.LayerNorm(cfg.d_model)
                self.free_router = (None if cfg.regime_routing
                                    else nn.Linear(cfg.d_model, cfg.n_experts))

            def forward(self, summary, gate_logits):
                """`summary`: [batch, d_model]. `gate_logits`: [batch, experts]."""
                if self.free_router is not None:
                    gate_logits = self.free_router(summary)
                weights = torch.softmax(gate_logits, dim=-1)     # [B, E]
                stacked = torch.stack([e(summary) for e in self.experts], dim=1)
                mixed = (weights.unsqueeze(-1) * stacked).sum(dim=1)
                return self.norm(summary + mixed), weights

        class CrossAssetAttention(nn.Module):
            """Attend from the target's summary over reference summaries.

            Causality is not this block's to enforce — `dataset.align_cross_asset`
            has already paired references on `ts_close <= ts_close`. What this
            owns is that an absent reference stays absent: a masked reference
            contributes nothing, and a batch row with no references at all
            passes its summary through unchanged rather than attending to
            padding.
            """

            def __init__(self, cfg: ModelConfig):
                super().__init__()
                self.attention = nn.MultiheadAttention(
                    cfg.d_model, cfg.cross_asset_heads, batch_first=True)
                self.norm = nn.LayerNorm(cfg.d_model)

            def forward(self, summary, references, mask=None):
                """`references`: [batch, n_refs, d_model]; `mask`: [batch, n_refs]
                with True where the reference is *absent*."""
                query = summary.unsqueeze(1)
                if mask is not None and bool(mask.all(dim=1).any()):
                    # A row whose every reference is absent would attend over
                    # nothing; softmax over an all-masked row is undefined and
                    # produces NaN in most implementations. Pass those rows
                    # through untouched instead.
                    usable = ~mask.all(dim=1)
                    out = summary.clone()
                    if bool(usable.any()):
                        attended, _ = self.attention(
                            query[usable], references[usable], references[usable],
                            key_padding_mask=mask[usable], need_weights=False)
                        out[usable] = self.norm(
                            summary[usable] + attended.squeeze(1))
                    return out
                attended, _ = self.attention(query, references, references,
                                             key_padding_mask=mask,
                                             need_weights=False)
                return self.norm(summary + attended.squeeze(1))

        class TimeframeFusionBlock(nn.Module):
            """Fuse one summary per timeframe, in a **fixed** order.

            Fixed at construction, never read from mapping iteration order at
            call time: dict ordering that happens to be stable in one process
            and different in another is exactly the silent corruption the whole
            native package is arranged to prevent.
            """

            def __init__(self, cfg: ModelConfig):
                super().__init__()
                self.order = tuple(cfg.timeframes)
                self.scale = nn.Parameter(
                    torch.zeros(len(self.order), cfg.d_model))
                self.project = nn.Linear(len(self.order) * cfg.d_model,
                                         cfg.d_model)
                self.norm = nn.LayerNorm(cfg.d_model)

            def forward(self, per_timeframe: Mapping[str, Any]):
                missing = [tf for tf in self.order if tf not in per_timeframe]
                if missing:
                    raise ValueError(f"fusion is missing timeframes {missing}")
                pieces = [per_timeframe[tf] + self.scale[i]
                          for i, tf in enumerate(self.order)]
                return self.norm(self.project(torch.cat(pieces, dim=-1)))

        class QuantileHead(nn.Module):
            """Monotone quantile ladder: a base plus softplus increments.

            A crossed quantile — p90 below p10 — is not a wide interval, it is
            a nonsense one that flows into a stop-loss. Independent outputs
            cross; this construction makes crossing arithmetically impossible
            rather than penalised.
            """

            def __init__(self, d_model: int, horizon: int, n_quantiles: int):
                super().__init__()
                self.horizon = horizon
                self.n_quantiles = n_quantiles
                self.base = nn.Linear(d_model, horizon)
                self.increments = nn.Linear(d_model, horizon * (n_quantiles - 1))

            def forward(self, summary):
                base = self.base(summary).unsqueeze(-1)          # [B, H, 1]
                steps = nn.functional.softplus(
                    self.increments(summary)).reshape(
                        -1, self.horizon, self.n_quantiles - 1)
                ladder = torch.cumsum(steps, dim=-1)
                return torch.cat([base, base + ladder], dim=-1)  # [B, H, Q]

        class MultiTaskModel(nn.Module):
            """The whole model. Heads exist only for enabled tasks."""

            def __init__(self, cfg: ModelConfig, representation):
                super().__init__()
                self.cfg = cfg
                self.representation = representation
                self.encoder = representation.module
                self.core = CausalCore(cfg)
                self.experts = RegimeExperts(cfg)
                self.identity = (nn.Embedding(cfg.n_instruments, cfg.d_model)
                                 if cfg.n_instruments else None)
                self.cross_asset = (CrossAssetAttention(cfg)
                                    if cfg.cross_asset_heads else None)
                self.fusion = (TimeframeFusionBlock(cfg)
                               if len(cfg.timeframes) > 1 else None)

                # The regime head is built before the experts are consulted,
                # because in the default configuration its logits are the gate.
                self.regime_head = nn.Linear(cfg.d_model, len(REGIME_CLASSES))

                heads = {}
                for kind in cfg.tasks.heads:
                    if kind is TaskKind.RETURN_DISTRIBUTION:
                        heads[kind.value] = QuantileHead(
                            cfg.d_model, cfg.horizon, cfg.n_quantiles)
                    elif kind is TaskKind.REGIME:
                        continue                    # already built above
                    elif kind is TaskKind.ANOMALY:
                        continue                    # uses the decoder, no head
                    else:
                        heads[kind.value] = nn.Linear(cfg.d_model, 1)
                self.heads = nn.ModuleDict(heads)

            # -- pieces ----------------------------------------------------

            def summarise(self, x, *, instrument_ids=None, mask=None):
                """Bars → one `[batch, d_model]` summary per row.

                The summary is the **last** core position, not a mean over
                positions. A mean would let an old position dilute the most
                recent one, and the most recent position is the only one whose
                information is entirely fresh.
                """
                encoded = (self.encoder(x, mask)
                           if mask is not None else self.encoder(x, None))
                latent, _, aux = encoded
                if self.identity is not None:
                    if instrument_ids is None:
                        raise ValueError(
                            "this model carries an instrument embedding and "
                            "was given no instrument ids")
                    latent = latent + self.identity(instrument_ids).unsqueeze(1)
                core = self.core(latent)
                return core[:, -1, :], aux

            def forward(self, x, *, instrument_ids=None, mask=None,
                        references=None, reference_mask=None,
                        extra_timeframes=None):
                summary, aux = self.summarise(x, instrument_ids=instrument_ids,
                                              mask=mask)

                if self.fusion is not None:
                    if not extra_timeframes:
                        raise ValueError(
                            "this model was configured to fuse timeframes and "
                            "was given none")
                    merged = dict(extra_timeframes)
                    merged.setdefault(self.cfg.timeframes[0], summary)
                    summary = self.fusion(merged)

                if self.cross_asset is not None and references is not None:
                    summary = self.cross_asset(summary, references,
                                               reference_mask)

                regime_logits = self.regime_head(summary)
                routed, gate = self.experts(summary, regime_logits)

                out: dict[str, Any] = {
                    "summary": routed,
                    "gate": gate,
                    TaskKind.REGIME.value: regime_logits,
                }
                for name, head in self.heads.items():
                    out[name] = head(routed)
                if TaskKind.ANOMALY.value in {k.value for k in self.cfg.tasks.enabled}:
                    out[TaskKind.ANOMALY.value] = self._reconstruct(x)
                out.update({f"aux_{k}": v for k, v in aux.items()})
                return out

            def _reconstruct(self, x):
                latent, _, _ = self.encoder(x, None)
                return self.encoder.decode(latent)

        return {"CausalBlock": CausalBlock, "CausalCore": CausalCore,
                "RegimeExperts": RegimeExperts,
                "CrossAssetAttention": CrossAssetAttention,
                "TimeframeFusionBlock": TimeframeFusionBlock,
                "QuantileHead": QuantileHead, "MultiTaskModel": MultiTaskModel}

    return cached_class("multitask_model_classes", factory)


def build_model(config: ModelConfig | None = None):
    """Build the model. Torch is required here and nowhere at module scope."""
    cfg = config or ModelConfig()
    representation = build_representation(cfg.representation,
                                          cfg.representation_config)
    latent = representation.latent_spec
    if not latent.sequential:
        raise ConfigurationError(
            "this representation does not emit a single time order, so a "
            "causal core cannot be applied across its positions; see the "
            "candidate's forecasting_compatibility note",
            representation=cfg.representation)
    if latent.d_model != cfg.d_model:
        raise ConfigurationError(               # pragma: no cover - guarded above
            "representation width does not match the core",
            representation=latent.d_model, core=cfg.d_model)
    return _classes()["MultiTaskModel"](cfg, representation)


def model_parameters(model) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def latent_spec_of(config: ModelConfig) -> LatentSpec:
    """The latent shape without building anything. For a consumer sizing a
    downstream component before a checkpoint exists."""
    return LatentSpec(positions=config.n_positions, d_model=config.d_model,
                      sequential=True)


def architecture_report(config: ModelConfig | None = None) -> dict:
    """What this architecture is, as data. Feeds the model card."""
    cfg = config or ModelConfig()
    return {
        "architecture_version": ARCHITECTURE_VERSION,
        "config": cfg.to_dict(),
        "blocks": {
            "representation": {
                "kind": cfg.representation, "replaceable": True,
                "why": "any interfaces.MarketEncoder; the seven candidates in "
                       "representations.py are swappable without touching "
                       "model.py"},
            "causal_core": {
                "kind": "dilated causal convolutions with gated residuals",
                "receptive_field": cfg.receptive_field,
                "covers_lookback": cfg.covers_lookback,
                "why": "causality is structural (left padding only) rather "
                       "than enforced by a mask that can be wrong; cost is "
                       "linear, not quadratic"},
            "experts": {
                "kind": "soft mixture over one expert per regime class",
                "routed_by": ("regime head softmax" if cfg.regime_routing
                              else "free learned router"),
                "n_experts": cfg.n_experts,
                "why": "a supervised gate is interpretable and cannot collapse; "
                       "the cost is that experts can only specialise along an "
                       "axis the regime classifier already sees"},
            "identity": {
                "enabled": bool(cfg.n_instruments),
                "why": "per-instrument idiosyncrasy is real and memorisation "
                       "is the risk; evaluation reports seen versus unseen "
                       "instruments separately"},
            "cross_asset": {
                "enabled": bool(cfg.cross_asset_heads),
                "why": "attention over causally-aligned reference summaries; "
                       "an absent reference contributes nothing"},
            "timeframe_fusion": {
                "enabled": len(cfg.timeframes) > 1,
                "order": list(cfg.timeframes),
                "why": "order fixed at construction, never read from mapping "
                       "iteration order"},
            "heads": {
                "enabled": [k.value for k in cfg.tasks.heads],
                "why": "a disabled task has no parameters, so a checkpoint's "
                       "head bank is a fact about the checkpoint"},
        },
        "external_methods": [
            "patch-based encoding of time series (published, general)",
            "dilated causal convolutions (published, general)",
            "mixture of experts (published, general)",
            "multi-head attention (published, general)",
            "monotone quantile regression via non-negative increments "
            "(published, general)",
            "pinball loss for quantile regression (published, general)",
        ],
        "claimed_original": [
            "the specific combination and its constraints",
            "routing the expert mixture by a separately-supervised regime "
            "head's logits rather than by a free learned gate",
            "the task registry's refusal to enable a head whose supervision "
            "does not exist",
        ],
        "not_claimed": [
            "novelty in any individual technique listed under external_methods",
            "superiority to any third-party model — no matched evaluation on "
            "real data has been run",
        ],
    }


__all__ = ["ARCHITECTURE_VERSION", "ModelConfig", "build_model",
           "model_parameters", "latent_spec_of", "architecture_report"]
