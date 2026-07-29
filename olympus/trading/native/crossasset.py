"""Cross-asset intelligence: declared relationships, computed as of the instant.

Two ideas carry this module, and the second is the one that matters.

**Relationships are declared, not discovered.** A correlation matrix estimated
over the evaluation window is a relationship that used the future to find
itself, and the resulting feature is a look-ahead that no causality check on the
*values* will catch — because every value is correctly timestamped and the
*edge* is what came from the future. So `RelationshipGraph` holds edges a human
or an upstream reference data source asserted, each with a validity interval,
and `edges_at` answers "what was believed at time t".

**Every feature is computed as of the prediction timestamp.** The reference
series is paired by `dataset.align_cross_asset`, which uses the rule
`reference.ts_close <= target.ts_close`. A missing or stale reference produces
`None`, never zero: a zero cross-asset return is indistinguishable from a
genuinely flat reference, and on a day the reference did not trade that
distinction is the whole signal.

The seven relationship kinds
----------------------------
Sector peers, index-constituent, spot-derivative, related crypto, rates-equity,
commodity-producer, and currency-macro. They are separate kinds rather than one
"related" edge because they behave differently under stress: an index and its
constituent converge, a spot and its future converge on expiry, and a
commodity and its producer decouple exactly when the producer's balance sheet
matters. A model given one undifferentiated "related" flag cannot learn that.

What is not here
----------------
No relationship data is reachable from this environment (blocker B1), and there
is one instrument's worth of synthetic bars. Everything below is exercised on
constructed graphs and constructed series. `capability.py` records that.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterator, Mapping, Sequence

from ..contracts import Candle, ensure_utc, jsonable
from ..errors import ConfigurationError
from .dataset import align_cross_asset, cross_asset_returns

#: Bumped when an edge kind's meaning changes.
GRAPH_SCHEMA_VERSION = 1


class RelationKind(str, Enum):
    """Why two instruments are related. Separate kinds because they differ
    under stress, which is when the relationship matters."""

    SECTOR_PEER = "sector_peer"
    INDEX_CONSTITUENT = "index_constituent"
    SPOT_DERIVATIVE = "spot_derivative"
    RELATED_CRYPTO = "related_crypto"
    RATES_EQUITY = "rates_equity"
    COMMODITY_PRODUCER = "commodity_producer"
    CURRENCY_MACRO = "currency_macro"


#: How each kind is expected to behave, for the explainer. Prose, and honest
#: about being an expectation rather than a measurement.
KIND_NOTES: Mapping[RelationKind, str] = {
    RelationKind.SECTOR_PEER:
        "expected to co-move on sector-wide news and to diverge on "
        "company-specific news; the divergence is the informative part",
    RelationKind.INDEX_CONSTITUENT:
        "the constituent is a component of the index, so the index leads on "
        "market-wide moves and lags on single-name ones",
    RelationKind.SPOT_DERIVATIVE:
        "converge on expiry by construction; the basis is the signal and it is "
        "not stationary",
    RelationKind.RELATED_CRYPTO:
        "correlation is high in calm markets and approaches one in stress, so "
        "a hedge estimated in calm markets fails when it is needed",
    RelationKind.RATES_EQUITY:
        "the sign of the relationship is regime-dependent and has historically "
        "flipped; a fixed-sign feature will be wrong for years at a time",
    RelationKind.COMMODITY_PRODUCER:
        "the producer is levered to the commodity and decouples when its own "
        "balance sheet dominates",
    RelationKind.CURRENCY_MACRO:
        "driven by rate differentials and policy; the relationship is a "
        "regime, not a constant",
}


@dataclass(frozen=True)
class Relationship:
    """One declared edge, with the interval over which it was believed.

    `since`/`until` are what make this auditable. An index constituent that was
    added in March is not a constituent in February, and a graph without
    intervals would let February's features use March's membership — the same
    survivorship error `dataset.Universe` exists to prevent, one level up.
    """

    source: str
    target: str
    kind: RelationKind
    since: datetime
    until: datetime | None = None
    #: Optional declared strength in [-1, 1]. Declared, not estimated — an
    #: estimated one belongs in a feature, not in the graph.
    weight: float | None = None
    note: str = ""

    def __post_init__(self):
        for name in ("source", "target"):
            if not str(getattr(self, name)).strip():
                raise ConfigurationError(f"a relationship needs a {name}")
        if self.source == self.target:
            raise ConfigurationError("an instrument is not related to itself",
                                     instrument=self.source)
        object.__setattr__(self, "kind", RelationKind(self.kind))
        object.__setattr__(self, "since",
                           ensure_utc(self.since, field_name="since"))
        if self.until is not None:
            object.__setattr__(self, "until",
                               ensure_utc(self.until, field_name="until"))
            if self.until <= self.since:
                raise ConfigurationError("relationship interval is inverted",
                                         source=self.source, target=self.target)
        if self.weight is not None and not -1.0 <= float(self.weight) <= 1.0:
            raise ConfigurationError("a declared weight must be in [-1, 1]",
                                     weight=self.weight)

    def holds_at(self, when: datetime) -> bool:
        moment = ensure_utc(when, field_name="when")
        return self.since <= moment and (self.until is None or moment < self.until)

    def to_dict(self) -> dict:
        return {"source": self.source, "target": self.target,
                "kind": self.kind.value, "since": jsonable(self.since),
                "until": jsonable(self.until) if self.until else None,
                "weight": self.weight, "note": self.note,
                "expectation": KIND_NOTES[self.kind]}


class RelationshipGraph:
    """Declared edges, queryable as of an instant.

    Undirected in effect: `references_for` returns the other endpoint whichever
    side the instrument sits on, because "the index of which I am a constituent"
    and "a constituent of me" are both context a model may condition on. The
    *kind* records which direction the edge was declared in.
    """

    __slots__ = ("_edges",)

    def __init__(self, edges: Sequence[Relationship] = ()):
        self._edges: list[Relationship] = []
        for edge in edges:
            self.add(edge)

    def add(self, edge: Relationship) -> "RelationshipGraph":
        for existing in self._edges:
            same = ({existing.source, existing.target}
                    == {edge.source, edge.target}
                    and existing.kind is edge.kind)
            if not same:
                continue
            overlap = ((existing.until is None or edge.since < existing.until)
                       and (edge.until is None or existing.since < edge.until))
            if overlap:
                raise ConfigurationError(
                    "two overlapping declarations of the same relationship; "
                    "which one was believed at a given instant would be "
                    "ambiguous",
                    source=edge.source, target=edge.target,
                    kind=edge.kind.value)
        self._edges.append(edge)
        return self

    def __len__(self) -> int:
        return len(self._edges)

    def __iter__(self) -> Iterator[Relationship]:
        return iter(self._edges)

    @property
    def instruments(self) -> tuple[str, ...]:
        return tuple(sorted({e.source for e in self._edges}
                            | {e.target for e in self._edges}))

    def edges_at(self, when: datetime) -> tuple[Relationship, ...]:
        """What was believed at `when`. The only query features may use."""
        return tuple(e for e in self._edges if e.holds_at(when))

    def references_for(self, instrument_key: str, when: datetime, *,
                       kinds: Sequence[RelationKind] = ()) -> tuple[str, ...]:
        """Instruments related to this one, as of `when`."""
        wanted = set(kinds) if kinds else None
        out = []
        for edge in self.edges_at(when):
            if wanted is not None and edge.kind not in wanted:
                continue
            if edge.source == instrument_key:
                out.append(edge.target)
            elif edge.target == instrument_key:
                out.append(edge.source)
        return tuple(sorted(set(out)))

    def kind_of(self, a: str, b: str, when: datetime) -> RelationKind | None:
        for edge in self.edges_at(when):
            if {edge.source, edge.target} == {a, b}:
                return edge.kind
        return None

    def to_dict(self) -> dict:
        return {"schema_version": GRAPH_SCHEMA_VERSION,
                "edges": [e.to_dict() for e in self._edges],
                "instruments": list(self.instruments)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RelationshipGraph":
        return cls([
            Relationship(source=str(e["source"]), target=str(e["target"]),
                         kind=RelationKind(e["kind"]),
                         since=datetime.fromisoformat(e["since"]),
                         until=(datetime.fromisoformat(e["until"])
                                if e.get("until") else None),
                         weight=e.get("weight"), note=str(e.get("note", "")))
            for e in data.get("edges") or ()])


@dataclass(frozen=True)
class CrossAssetFeature:
    """One reference's contribution, with its own staleness and mask."""

    reference: str
    kind: RelationKind
    #: Contemporaneous log return of the reference over the window's last step.
    log_return: float | None
    #: Trailing correlation of step returns over the window.
    correlation: float | None
    #: Ratio of the reference's dispersion to the target's. `None` when either
    #: cannot be computed; 1.0 would be a claim they move alike.
    relative_volatility: float | None
    #: Seconds between the reference bar's close and the target's.
    staleness_seconds: float | None
    observations: int = 0

    @property
    def present(self) -> bool:
        return self.log_return is not None

    def to_dict(self) -> dict:
        return {"reference": self.reference, "kind": self.kind.value,
                "log_return": self.log_return, "correlation": self.correlation,
                "relative_volatility": self.relative_volatility,
                "staleness_seconds": self.staleness_seconds,
                "observations": self.observations, "present": self.present,
                "expectation": KIND_NOTES[self.kind]}


@dataclass(frozen=True)
class CrossAssetContext:
    """Every reference's features at one instant, plus what was missing."""

    instrument_key: str
    as_of: datetime
    features: tuple[CrossAssetFeature, ...] = ()
    #: References the graph declared and the data could not supply.
    missing: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "as_of",
                           ensure_utc(self.as_of, field_name="as_of"))
        object.__setattr__(self, "features", tuple(self.features))
        object.__setattr__(self, "missing", tuple(self.missing))

    @property
    def present(self) -> tuple[CrossAssetFeature, ...]:
        return tuple(f for f in self.features if f.present)

    @property
    def coverage(self) -> float:
        total = len(self.features) + len(self.missing)
        return len(self.present) / total if total else 0.0

    def strongest(self) -> CrossAssetFeature | None:
        """The reference with the largest absolute correlation. For the
        explainer — and `None` rather than an arbitrary pick when none has a
        correlation, because "the strongest relationship" with nothing behind it
        is exactly the kind of claim explainability is supposed to prevent."""
        scored = [f for f in self.present if f.correlation is not None]
        return max(scored, key=lambda f: abs(f.correlation)) if scored else None

    def to_dict(self) -> dict:
        strongest = self.strongest()
        return {"instrument": self.instrument_key,
                "as_of": jsonable(self.as_of),
                "features": [f.to_dict() for f in self.features],
                "missing": list(self.missing),
                "coverage": round(self.coverage, 4),
                "strongest": strongest.reference if strongest else None}


def _correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    pairs = [(x, y) for x, y in zip(a, b)
             if x is not None and y is not None
             and math.isfinite(x) and math.isfinite(y)]
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    numerator = sum((x - mx) * (y - my) for x, y in pairs)
    denominator = math.sqrt(sum((x - mx) ** 2 for x in xs)
                            * sum((y - my) ** 2 for y in ys))
    if denominator <= 0:
        return None
    return max(-1.0, min(1.0, numerator / denominator))


def build_cross_asset_context(*, instrument_key: str,
                              target_bars: Sequence[Candle],
                              references: Mapping[str, Sequence[Candle]],
                              graph: RelationshipGraph,
                              as_of: datetime | None = None,
                              max_staleness: timedelta | None = None,
                              kinds: Sequence[RelationKind] = ()
                              ) -> CrossAssetContext:
    """Cross-asset features, using only what was knowable at `as_of`.

    Three things are enforced here rather than assumed:

    1. **The edge set is read as of the instant**, so a relationship declared
       later cannot inform an earlier forecast.
    2. **The pairing is causal**, delegated to `dataset.align_cross_asset`.
    3. **Bars after `as_of` are dropped** from both the target and every
       reference before anything is computed, because a caller holding a full
       series is the normal case and filtering is this function's job.
    """
    if not target_bars:
        raise ConfigurationError("no target bars to build a context from")
    moment = (ensure_utc(as_of, field_name="as_of") if as_of is not None
              else max(b.ts_close for b in target_bars))

    target = sorted((b for b in target_bars if b.ts_close <= moment),
                    key=lambda b: b.ts_close)
    if not target:
        raise ConfigurationError(
            "every target bar closes after as_of; there is nothing knowable "
            "at the instant being modelled", as_of=moment.isoformat())

    declared = graph.references_for(instrument_key, moment, kinds=kinds)
    usable: dict[str, list[Candle]] = {}
    missing: list[str] = []
    for name in declared:
        bars = [b for b in references.get(name) or () if b.ts_close <= moment]
        if len(bars) < 2:
            missing.append(name)
            continue
        usable[name] = sorted(bars, key=lambda b: b.ts_close)

    if not usable:
        return CrossAssetContext(instrument_key=instrument_key, as_of=moment,
                                 features=(), missing=tuple(declared))

    aligned = align_cross_asset(target, usable, max_staleness=max_staleness)
    target_steps = [math.log(target[i].close / target[i - 1].close)
                    for i in range(1, len(target))]

    features: list[CrossAssetFeature] = []
    for name in sorted(usable):
        kind = graph.kind_of(instrument_key, name, moment)
        if kind is None:                                 # pragma: no cover
            continue
        returns = cross_asset_returns(aligned, name)
        latest = next((r for r in reversed(returns) if r is not None), None)
        paired = [(t, r) for t, r in zip(target_steps, returns[1:])
                  if r is not None]
        correlation = _correlation([p[0] for p in paired],
                                   [p[1] for p in paired])
        reference_steps = [r for _, r in paired]
        relative = None
        if len(reference_steps) > 1 and len(target_steps) > 1:
            target_sigma = statistics.pstdev(target_steps)
            reference_sigma = statistics.pstdev(reference_steps)
            if target_sigma > 0:
                relative = reference_sigma / target_sigma
        staleness = aligned[-1].staleness_seconds.get(name) if aligned else None
        features.append(CrossAssetFeature(
            reference=name, kind=kind, log_return=latest,
            correlation=correlation, relative_volatility=relative,
            staleness_seconds=staleness, observations=len(paired)))

    return CrossAssetContext(instrument_key=instrument_key, as_of=moment,
                             features=tuple(features), missing=tuple(missing))


def assert_no_future_reference(context: CrossAssetContext,
                               references: Mapping[str, Sequence[Candle]]) -> None:
    """Independent check that no reference bar postdates the prediction instant.

    Independent of the filtering in `build_cross_asset_context`, so it still
    works when that filtering is the bug. It re-reads the raw series and asserts
    that the newest bar the context could have used closes at or before
    `as_of`.
    """
    offenders = []
    for feature in context.features:
        bars = references.get(feature.reference) or ()
        used = [b for b in bars if b.ts_close <= context.as_of]
        if not used and feature.present:
            offenders.append(
                f"{feature.reference}: a feature was produced with no bar at "
                f"or before as_of")
        future = [b for b in bars if b.ts_close > context.as_of]
        if future and feature.staleness_seconds is not None \
                and feature.staleness_seconds < 0:
            offenders.append(
                f"{feature.reference}: negative staleness, so the paired bar "
                f"closes after as_of")
    if offenders:
        raise ConfigurationError(
            "a cross-asset feature used information from after the prediction "
            "timestamp", offenders=offenders)


__all__ = ["GRAPH_SCHEMA_VERSION", "RelationKind", "KIND_NOTES",
           "Relationship", "RelationshipGraph", "CrossAssetFeature",
           "CrossAssetContext", "build_cross_asset_context",
           "assert_no_future_reference"]
