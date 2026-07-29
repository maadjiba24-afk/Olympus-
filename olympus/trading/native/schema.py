"""The Olympus market-state channel schema.

A candle is five numbers and a timestamp. A market state is everything that was
observable at an instant, and most of it is not in the candle: the book, the
funding rate, what the portfolio was already holding, whether an economic
release was twenty minutes away. This module is the registry of those channels
and — more importantly — of what is *true about each one*, because the metadata
is what stops a channel being misused.

Why metadata, not just names
----------------------------
Every leakage defect the teardown found was a metadata defect wearing a
modelling costume. A field was consumed as though it were knowable at the bar
close when it was announced three hours later; a rate was normalised with
statistics computed over the test period; an absent value became a zero that the
model then learned from. None of those are modelling errors. They are errors
about *what a number means*, and a schema that records only names cannot catch
any of them.

So a `ChannelSpec` records eleven things, and the ones that carry weight are:

**Timestamp semantics.** `BAR_CLOSE` and `ANNOUNCED` are not interchangeable.
A funding rate stamped with a bar it was announced *during* is a look-ahead of
up to one bar; a revised economic figure stamped with its original release is a
look-ahead of days. `TimestampSemantics.REVISED` exists so that a channel whose
value changes after publication can never be silently treated as final.

**Missing-value policy.** There is deliberately **no zero-fill policy**. A
zero-filled bid-ask spread is a real, plausible number describing a
perfectly-liquid market, and a model will learn from it happily; the resulting
error is invisible in every metric. The options are: the observation is invalid
(`REQUIRED`), the value is absent and a mask bit says so (`MASK`), or the last
known value is carried forward *with its staleness recorded and bounded*
(`LAST_KNOWN`). A categorical channel gets an explicit unknown level.

**Normalisation policy.** `TRAIN_ROBUST` means "statistics fitted on the
training split only". Recording that in the schema is what lets the dataset
builder refuse to fit it anywhere else, rather than relying on every caller
remembering.

**Availability.** Most of these channels are not reachable from this
environment. Saying so per-channel, in the schema, is the difference between a
design that is honest about its inputs and a design that quietly assumes them.
`schema.availability_report()` prints the split.

Nothing here imports torch, numpy, or any model. A schema is data about data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..errors import ConfigurationError, DataValidationError

#: Bumped when the *meaning* of any channel changes — not when one is added.
#: A checkpoint records the fingerprint, which catches an addition too; the
#: version is for humans reading a changelog.
SCHEMA_VERSION = 1

#: Sentinel for "every instrument class" / "every timeframe". A channel that
#: applies universally should say so rather than enumerate a list that goes
#: stale the first time an asset class is added.
ANY: tuple[str, ...] = ("*",)


class ChannelType(str, Enum):
    """What kind of quantity this is. Drives which normalisations are legal."""

    PRICE = "price"                 # a level in the quote currency
    VOLUME = "volume"               # a non-negative traded quantity
    COUNT = "count"                 # a non-negative integer-valued tally
    RATE = "rate"                   # a per-interval rate, may be signed
    RATIO = "ratio"                 # a dimensionless ratio
    SPREAD = "spread"               # a non-negative price difference
    DEPTH = "depth"                 # resting size at or near the touch
    IMBALANCE = "imbalance"         # a signed, bounded asymmetry measure
    VOLATILITY = "volatility"       # a non-negative dispersion estimate
    MONETARY = "monetary"           # a signed amount in the quote currency
    CATEGORICAL = "categorical"     # one of a fixed set of levels
    CYCLICAL = "cyclical"           # an angle on a circle (time of day, weekday)
    BOOLEAN = "boolean"             # a flag
    DURATION = "duration"           # elapsed or remaining time


class Unit(str, Enum):
    """The unit the raw value is in, before any normalisation."""

    QUOTE_CURRENCY = "quote_currency"
    BASE_UNITS = "base_units"
    QUOTE_UNITS = "quote_units"
    CONTRACTS = "contracts"
    COUNT = "count"
    FRACTION = "fraction"                   # in [0, 1] or [-1, 1]
    BASIS_POINTS = "basis_points"
    PER_INTERVAL = "per_interval"           # e.g. funding per 8h
    LOG_RATIO = "log_ratio"
    SECONDS = "seconds"
    BARS = "bars"
    CATEGORY = "category"
    BOOLEAN = "boolean"
    DIMENSIONLESS = "dimensionless"


class TimestampSemantics(str, Enum):
    """When the value became knowable, relative to the bar it is stamped with.

    This is the single most load-bearing field in the spec. Two channels with
    identical names, types and units but different semantics have completely
    different leakage profiles.
    """

    #: Aggregated over the bar's interval; knowable at `ts_close` and not before.
    BAR_CLOSE = "bar_close"
    #: An instantaneous reading taken at `ts_close`.
    SNAPSHOT_AT_CLOSE = "snapshot_at_close"
    #: Summed or counted over the bar's interval; knowable at `ts_close`.
    INTERVAL_AGGREGATE = "interval_aggregate"
    #: Derivable from the calendar alone, so knowable arbitrarily far ahead.
    #: The only category that may legitimately reference a future instant.
    KNOWN_IN_ADVANCE = "known_in_advance"
    #: Published at a specific instant that need not align to a bar boundary.
    #: Consuming it requires the announcement time, not the bar stamp.
    ANNOUNCED = "announced"
    #: Published, then *revised* later. The revised value was never knowable at
    #: the original stamp. A channel marked this way may not be consumed
    #: without a vintage, and `dataset.leakage_report` says so.
    REVISED = "revised"
    #: Computed by Olympus from bars at or before the stamp. Causality is then
    #: Olympus's own responsibility and `features.assert_causal` is the check.
    DERIVED_CAUSAL = "derived_causal"


class MissingPolicy(str, Enum):
    """What happens when the value is not there.

    There is no zero-fill member and there will not be one. See the module
    docstring.
    """

    #: Absence invalidates the whole observation.
    REQUIRED = "required"
    #: Absence is recorded in the mask; the value is undefined, not defaulted.
    MASK = "mask"
    #: Carry the last known value forward, bounded by `max_staleness_bars`, and
    #: record how stale it is so the model can condition on that.
    LAST_KNOWN = "last_known"
    #: Categorical channels get an explicit unknown level rather than a mask,
    #: because "unknown regime" is itself a meaningful state.
    UNKNOWN_LEVEL = "unknown_level"


class Normalisation(str, Enum):
    """How the raw value is made comparable across instruments and epochs."""

    NONE = "none"
    #: log(x_t / x_{t-1}) — scale-free, the default for prices.
    LOG_RATIO_PREVIOUS = "log_ratio_previous"
    #: log(x_t / close_t) — for prices that should be read relative to the bar.
    LOG_RATIO_CLOSE = "log_ratio_close"
    #: (x - mean) / std over the *input window only*. Causal by construction
    #: because `data.Window` guarantees targets close after inputs.
    INSTANCE_ZSCORE = "instance_zscore"
    #: Median / IQR fitted on the **training split only**. The dataset builder
    #: enforces that; see `dataset.ScalerPolicy`.
    TRAIN_ROBUST = "train_robust"
    #: sign(x) · log1p(|x|) — for heavy-tailed signed quantities.
    SIGNED_LOG1P = "signed_log1p"
    #: Already bounded; passed through with a range assertion.
    BOUNDED_UNIT = "bounded_unit"
    #: (sin, cos) pair. Expands to two model inputs from one channel.
    CYCLICAL = "cyclical"
    #: Expands to `len(levels)` indicator inputs.
    ONE_HOT = "one_hot"


class Availability(str, Enum):
    """Whether Olympus can actually obtain this, here, today."""

    #: Present in the store now.
    AVAILABLE = "available"
    #: Computable from what is present, by code Olympus owns.
    DERIVABLE = "derivable"
    #: The upstream field exists; Olympus does not fetch or store it.
    NOT_INGESTED = "not_ingested"
    #: No provider is reachable from this environment. Blocker B1.
    UNREACHABLE = "unreachable"


#: Which normalisations make sense for which types. A funding rate one-hot
#: encoded, or a categorical z-scored, is a configuration error rather than an
#: eccentric choice, and it is worth failing at construction.
_LEGAL_NORMALISATION: Mapping[ChannelType, frozenset[Normalisation]] = {
    ChannelType.PRICE: frozenset({Normalisation.LOG_RATIO_PREVIOUS,
                                  Normalisation.LOG_RATIO_CLOSE,
                                  Normalisation.INSTANCE_ZSCORE}),
    ChannelType.VOLUME: frozenset({Normalisation.LOG_RATIO_PREVIOUS,
                                   Normalisation.INSTANCE_ZSCORE,
                                   Normalisation.SIGNED_LOG1P,
                                   Normalisation.TRAIN_ROBUST}),
    ChannelType.COUNT: frozenset({Normalisation.LOG_RATIO_PREVIOUS,
                                  Normalisation.INSTANCE_ZSCORE,
                                  Normalisation.SIGNED_LOG1P,
                                  Normalisation.TRAIN_ROBUST}),
    ChannelType.RATE: frozenset({Normalisation.SIGNED_LOG1P,
                                 Normalisation.TRAIN_ROBUST,
                                 Normalisation.INSTANCE_ZSCORE,
                                 Normalisation.NONE}),
    ChannelType.RATIO: frozenset({Normalisation.INSTANCE_ZSCORE,
                                  Normalisation.TRAIN_ROBUST,
                                  Normalisation.BOUNDED_UNIT,
                                  Normalisation.NONE}),
    ChannelType.SPREAD: frozenset({Normalisation.SIGNED_LOG1P,
                                   Normalisation.TRAIN_ROBUST,
                                   Normalisation.INSTANCE_ZSCORE}),
    ChannelType.DEPTH: frozenset({Normalisation.SIGNED_LOG1P,
                                  Normalisation.TRAIN_ROBUST,
                                  Normalisation.INSTANCE_ZSCORE}),
    ChannelType.IMBALANCE: frozenset({Normalisation.BOUNDED_UNIT,
                                      Normalisation.INSTANCE_ZSCORE}),
    ChannelType.VOLATILITY: frozenset({Normalisation.SIGNED_LOG1P,
                                       Normalisation.TRAIN_ROBUST,
                                       Normalisation.INSTANCE_ZSCORE}),
    ChannelType.MONETARY: frozenset({Normalisation.SIGNED_LOG1P,
                                     Normalisation.TRAIN_ROBUST,
                                     Normalisation.BOUNDED_UNIT}),
    ChannelType.CATEGORICAL: frozenset({Normalisation.ONE_HOT}),
    ChannelType.CYCLICAL: frozenset({Normalisation.CYCLICAL}),
    ChannelType.BOOLEAN: frozenset({Normalisation.NONE,
                                    Normalisation.BOUNDED_UNIT}),
    ChannelType.DURATION: frozenset({Normalisation.SIGNED_LOG1P,
                                     Normalisation.TRAIN_ROBUST,
                                     Normalisation.BOUNDED_UNIT}),
}


@dataclass(frozen=True)
class ChannelSpec:
    """One observable channel and everything true about it.

    Frozen, hashable and JSON-serialisable, because the whole schema's
    fingerprint goes into every training manifest: a model trained against one
    set of channel meanings and served against another is silently wrong in a
    way no metric reports.
    """

    name: str
    kind: ChannelType
    unit: Unit
    #: Where the number comes from. A provider name, `"candle"`, or an Olympus
    #: module path for a derived channel.
    source: str
    timestamp: TimestampSemantics
    missing: MissingPolicy
    normalisation: Normalisation
    #: Instrument classes this applies to, or `ANY`. A funding rate on an
    #: equity is not missing data, it is a category error.
    instruments: tuple[str, ...] = ANY
    timeframes: tuple[str, ...] = ANY
    availability: Availability = Availability.NOT_INGESTED
    description: str = ""
    group: str = "other"
    #: Only for `LAST_KNOWN`: how stale the carried value may become before the
    #: channel is treated as absent instead.
    max_staleness_bars: int | None = None
    #: Only for categorical channels.
    levels: tuple[str, ...] = ()
    #: Inclusive bounds the raw value must respect, when it has any.
    bounds: tuple[float | None, float | None] = (None, None)
    #: Channels this one is computed from. Empty for a directly observed one.
    derives_from: tuple[str, ...] = ()

    def __post_init__(self):
        name = str(self.name).strip()
        if not name:
            raise ConfigurationError("a channel must be named")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "kind", ChannelType(self.kind))
        object.__setattr__(self, "unit", Unit(self.unit))
        object.__setattr__(self, "timestamp", TimestampSemantics(self.timestamp))
        object.__setattr__(self, "missing", MissingPolicy(self.missing))
        object.__setattr__(self, "normalisation", Normalisation(self.normalisation))
        object.__setattr__(self, "availability", Availability(self.availability))
        for attr in ("instruments", "timeframes", "levels", "derives_from"):
            object.__setattr__(self, attr, tuple(getattr(self, attr) or ()))
        object.__setattr__(self, "bounds", tuple(self.bounds))
        if not str(self.source).strip():
            raise ConfigurationError("a channel must name its source",
                                     channel=name)
        if not self.instruments:
            raise ConfigurationError(
                "a channel must say which instrument classes it applies to; "
                "an empty tuple means 'none', which cannot be intended",
                channel=name)
        if not self.timeframes:
            raise ConfigurationError("a channel must say which timeframes it "
                                     "applies to", channel=name)

        legal = _LEGAL_NORMALISATION[self.kind]
        if self.normalisation not in legal:
            raise ConfigurationError(
                "normalisation is not meaningful for this channel type",
                channel=name, kind=self.kind.value,
                normalisation=self.normalisation.value,
                legal=sorted(n.value for n in legal))

        if self.missing is MissingPolicy.LAST_KNOWN:
            if self.max_staleness_bars is None or self.max_staleness_bars < 1:
                raise ConfigurationError(
                    "LAST_KNOWN needs a positive max_staleness_bars; an "
                    "unbounded carry-forward turns a value that was true last "
                    "quarter into one the model reads as current",
                    channel=name, max_staleness_bars=self.max_staleness_bars)
        elif self.max_staleness_bars is not None:
            raise ConfigurationError(
                "max_staleness_bars is only meaningful for LAST_KNOWN",
                channel=name, missing=self.missing.value)

        categorical = self.kind is ChannelType.CATEGORICAL
        if categorical and not self.levels:
            raise ConfigurationError("a categorical channel must list its levels",
                                     channel=name)
        if not categorical and self.levels:
            raise ConfigurationError("levels are only meaningful for a "
                                     "categorical channel", channel=name)
        if categorical and self.missing is not MissingPolicy.UNKNOWN_LEVEL:
            raise ConfigurationError(
                "a categorical channel must use UNKNOWN_LEVEL: masking a "
                "category discards the fact that it was unknown, which is "
                "itself a state worth conditioning on",
                channel=name, missing=self.missing.value)
        if categorical and "unknown" not in {level.lower() for level in self.levels}:
            raise ConfigurationError(
                "a categorical channel's levels must include an explicit "
                "unknown level", channel=name, levels=list(self.levels))

        # The contradiction worth catching: a channel nothing can supply, which
        # every observation is nonetheless invalid without.
        if (self.missing is MissingPolicy.REQUIRED
                and self.availability in (Availability.NOT_INGESTED,
                                          Availability.UNREACHABLE)):
            raise ConfigurationError(
                "a REQUIRED channel that cannot be obtained makes every "
                "observation invalid; mark it MASK or fix the availability",
                channel=name, availability=self.availability.value)

        low, high = self.bounds
        if low is not None and high is not None and float(low) > float(high):
            raise ConfigurationError("bounds are inverted", channel=name,
                                     bounds=list(self.bounds))
        if self.derives_from and self.timestamp is TimestampSemantics.ANNOUNCED:
            raise ConfigurationError(
                "a derived channel cannot be ANNOUNCED; its knowability is "
                "that of its inputs", channel=name)

    # -- derived properties ------------------------------------------------

    @property
    def obtainable(self) -> bool:
        """True when Olympus can actually produce this channel today."""
        return self.availability in (Availability.AVAILABLE,
                                     Availability.DERIVABLE)

    @property
    def width(self) -> int:
        """How many model inputs this channel expands to after normalisation.

        A cyclical channel is two (sin, cos); a one-hot is one per level. Every
        other channel is one. Shape errors that would otherwise surface as a
        matrix-multiply failure deep in a forward pass surface here instead.
        """
        if self.normalisation is Normalisation.CYCLICAL:
            return 2
        if self.normalisation is Normalisation.ONE_HOT:
            return len(self.levels)
        return 1

    @property
    def needs_train_statistics(self) -> bool:
        """True when this channel's normalisation must be fitted on train only."""
        return self.normalisation is Normalisation.TRAIN_ROBUST

    @property
    def needs_vintage(self) -> bool:
        """True when consuming this channel without a vintage is a look-ahead."""
        return self.timestamp is TimestampSemantics.REVISED

    def applies_to(self, *, asset_class: str = "", timeframe: str = "") -> bool:
        def matches(allowed: tuple[str, ...], value: str) -> bool:
            if allowed == ANY or not value:
                return True
            return value in allowed
        return (matches(self.instruments, asset_class)
                and matches(self.timeframes, timeframe))

    def check_value(self, value: float) -> float:
        """Range-check a raw value. Raises rather than clipping.

        Clipping would turn an ingestion bug into a plausible observation at the
        boundary, which is exactly the failure mode this layer exists to catch.
        """
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            raise DataValidationError("channel value must be finite",
                                      channel=self.name, value=repr(value))
        low, high = self.bounds
        if low is not None and number < float(low):
            raise DataValidationError("channel value below its declared bound",
                                      channel=self.name, value=number, bound=low)
        if high is not None and number > float(high):
            raise DataValidationError("channel value above its declared bound",
                                      channel=self.name, value=number, bound=high)
        return number

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind.value,
                "unit": self.unit.value, "source": self.source,
                "timestamp": self.timestamp.value, "missing": self.missing.value,
                "normalisation": self.normalisation.value,
                "instruments": list(self.instruments),
                "timeframes": list(self.timeframes),
                "availability": self.availability.value,
                "description": self.description, "group": self.group,
                "max_staleness_bars": self.max_staleness_bars,
                "levels": list(self.levels), "bounds": list(self.bounds),
                "derives_from": list(self.derives_from), "width": self.width}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChannelSpec":
        bounds = tuple(data.get("bounds") or (None, None))
        return cls(name=str(data["name"]), kind=ChannelType(data["kind"]),
                   unit=Unit(data["unit"]), source=str(data["source"]),
                   timestamp=TimestampSemantics(data["timestamp"]),
                   missing=MissingPolicy(data["missing"]),
                   normalisation=Normalisation(data["normalisation"]),
                   instruments=tuple(data.get("instruments") or ANY),
                   timeframes=tuple(data.get("timeframes") or ANY),
                   availability=Availability(data.get("availability",
                                                      Availability.NOT_INGESTED.value)),
                   description=str(data.get("description", "")),
                   group=str(data.get("group", "other")),
                   max_staleness_bars=data.get("max_staleness_bars"),
                   levels=tuple(data.get("levels") or ()),
                   bounds=(bounds + (None, None))[:2],
                   derives_from=tuple(data.get("derives_from") or ()))


class MarketStateSchema:
    """An ordered, fingerprinted registry of channels.

    Ordered because a model trained on one channel order and served on another
    is totally and silently wrong — the same argument `MarketState.feature_vector`
    makes about feature ordering, one level up.
    """

    __slots__ = ("_channels", "version", "name")

    def __init__(self, channels: Sequence[ChannelSpec], *,
                 version: int = SCHEMA_VERSION, name: str = "olympus"):
        ordered: dict[str, ChannelSpec] = {}
        for spec in channels:
            if spec.name in ordered:
                raise ConfigurationError("duplicate channel", channel=spec.name)
            ordered[spec.name] = spec
        # Referential integrity: a derived channel whose inputs are not in the
        # schema is a channel nothing can build.
        for spec in ordered.values():
            for parent in spec.derives_from:
                if parent not in ordered:
                    raise ConfigurationError(
                        "channel derives from one that is not in the schema",
                        channel=spec.name, missing_parent=parent)
        self._channels = ordered
        self.version = int(version)
        self.name = str(name)

    # -- access ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._channels)

    def __iter__(self) -> Iterator[ChannelSpec]:
        return iter(self._channels.values())

    def __contains__(self, name: object) -> bool:
        return name in self._channels

    def __getitem__(self, name: str) -> ChannelSpec:
        try:
            return self._channels[name]
        except KeyError:
            raise ConfigurationError("unknown channel", channel=name,
                                     known=len(self._channels)) from None

    def get(self, name: str) -> ChannelSpec | None:
        return self._channels.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._channels)

    @property
    def groups(self) -> tuple[str, ...]:
        return tuple(sorted({spec.group for spec in self}))

    @property
    def width(self) -> int:
        """Total model-input width once every channel is expanded."""
        return sum(spec.width for spec in self)

    def required(self) -> tuple[ChannelSpec, ...]:
        return tuple(s for s in self if s.missing is MissingPolicy.REQUIRED)

    def obtainable(self) -> tuple[ChannelSpec, ...]:
        return tuple(s for s in self if s.obtainable)

    def by_group(self, group: str) -> tuple[ChannelSpec, ...]:
        return tuple(s for s in self if s.group == group)

    def by_availability(self, availability: Availability) -> tuple[ChannelSpec, ...]:
        return tuple(s for s in self if s.availability is availability)

    def needing_train_statistics(self) -> tuple[ChannelSpec, ...]:
        return tuple(s for s in self if s.needs_train_statistics)

    def needing_vintage(self) -> tuple[ChannelSpec, ...]:
        return tuple(s for s in self if s.needs_vintage)

    def applicable(self, *, asset_class: str = "", timeframe: str = ""
                   ) -> tuple[ChannelSpec, ...]:
        return tuple(s for s in self
                     if s.applies_to(asset_class=asset_class, timeframe=timeframe))

    def subset(self, names: Iterable[str], *, name: str = "") -> "MarketStateSchema":
        """A narrower schema over the same specs, order preserved.

        Used to declare exactly which channels a given model consumes, so the
        manifest records a model's real input surface rather than the whole
        registry.
        """
        wanted = list(names)
        unknown = [n for n in wanted if n not in self._channels]
        if unknown:
            raise ConfigurationError("subset names unknown channels",
                                     unknown=unknown)
        keep = set(wanted)
        # Preserve registry order rather than the caller's, so two callers
        # requesting the same set get the same schema and the same fingerprint.
        return MarketStateSchema([s for s in self if s.name in keep],
                                 version=self.version,
                                 name=name or f"{self.name}:subset")

    def obtainable_subset(self, *, name: str = "") -> "MarketStateSchema":
        """Just the channels Olympus can actually produce today.

        This is what a model in this environment can be trained on, and it is
        much smaller than the full registry. See `availability_report`.
        """
        return self.subset([s.name for s in self.obtainable()],
                           name=name or f"{self.name}:obtainable")

    # -- validation --------------------------------------------------------

    def validate(self, values: Mapping[str, float], *,
                 asset_class: str = "", timeframe: str = ""
                 ) -> "ChannelObservation":
        """Turn a raw mapping into a checked, mask-aware observation.

        Unknown channels raise. A model fed a channel the schema has never heard
        of is a model whose input surface nobody knows, and accepting it quietly
        is how a training set acquires a column that its serving path lacks.
        """
        unknown = [k for k in values if k not in self._channels]
        if unknown:
            raise ConfigurationError("observation carries unknown channels",
                                     unknown=sorted(unknown))

        applicable = {s.name for s in self.applicable(asset_class=asset_class,
                                                      timeframe=timeframe)}
        checked: dict[str, float] = {}
        for name, raw in values.items():
            spec = self._channels[name]
            if name not in applicable:
                raise DataValidationError(
                    "channel does not apply to this instrument or timeframe; a "
                    "funding rate on an equity is a category error, not a "
                    "missing value",
                    channel=name, asset_class=asset_class, timeframe=timeframe)
            checked[name] = spec.check_value(raw)

        missing_required = [s.name for s in self.required()
                            if s.name in applicable and s.name not in checked]
        if missing_required:
            raise DataValidationError("observation is missing required channels",
                                      missing=sorted(missing_required))
        return ChannelObservation(schema=self, values=checked,
                                  applicable=tuple(sorted(applicable)))

    # -- identity ----------------------------------------------------------

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version,
                "channels": [s.to_dict() for s in self]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MarketStateSchema":
        return cls([ChannelSpec.from_dict(c) for c in data.get("channels") or ()],
                   version=int(data.get("version", SCHEMA_VERSION)),
                   name=str(data.get("name", "olympus")))

    def fingerprint(self) -> str:
        """Content hash over every channel's full metadata.

        Over the metadata, not just the names: a channel whose timestamp
        semantics silently changed from BAR_CLOSE to ANNOUNCED is a different
        schema, and a checkpoint trained under the old meaning must not load
        cleanly under the new one.
        """
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]

    def availability_report(self) -> dict:
        """What is obtainable, what is not, and why. The honest summary."""
        counts = {a.value: len(self.by_availability(a)) for a in Availability}
        return {"schema": self.name, "version": self.version,
                "fingerprint": self.fingerprint(),
                "channels": len(self), "model_input_width": self.width,
                "obtainable": len(self.obtainable()),
                "by_availability": counts,
                "by_group": {g: len(self.by_group(g)) for g in self.groups},
                "needing_train_statistics":
                    [s.name for s in self.needing_train_statistics()],
                "needing_vintage": [s.name for s in self.needing_vintage()]}

    def __repr__(self) -> str:                           # pragma: no cover
        return (f"MarketStateSchema({self.name} v{self.version}, "
                f"{len(self)} channels, {len(self.obtainable())} obtainable)")


@dataclass(frozen=True)
class ChannelObservation:
    """Channel values at one instant, with an explicit mask.

    The mask is the point. `values` holds what was observed; `mask` says, for
    every applicable channel in schema order, whether it was. There is no
    arrangement in which a consumer reads a number that was not measured — the
    closest it can get is `LAST_KNOWN`, which reports its own staleness.
    """

    schema: MarketStateSchema
    values: Mapping[str, float]
    #: Channels that apply to this instrument and timeframe. Channels outside
    #: this set are *not applicable* rather than *missing*, and the difference
    #: matters: an equity has no funding rate to be missing.
    applicable: tuple[str, ...] = ()
    #: `channel -> bars since the value was actually observed`, for LAST_KNOWN.
    staleness: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "values", dict(self.values))
        object.__setattr__(self, "staleness", dict(self.staleness))
        if not self.applicable:
            object.__setattr__(self, "applicable", self.schema.names)
        for name, stale in self.staleness.items():
            spec = self.schema[name]
            if spec.missing is not MissingPolicy.LAST_KNOWN:
                raise ConfigurationError(
                    "staleness recorded for a channel that does not carry "
                    "values forward", channel=name)
            if spec.max_staleness_bars is not None and stale > spec.max_staleness_bars:
                raise DataValidationError(
                    "carried value is staler than the channel permits; it "
                    "should have been dropped to absent rather than aged past "
                    "its own limit",
                    channel=name, staleness=stale,
                    max_staleness_bars=spec.max_staleness_bars)

    def present(self, name: str) -> bool:
        return name in self.values

    def value(self, name: str) -> float | None:
        """The observed value, or `None`. Never a fabricated default."""
        return self.values.get(name)

    def mask(self, names: Sequence[str] | None = None) -> tuple[bool, ...]:
        """Presence bits in the caller's order, or schema order by default."""
        wanted = tuple(names) if names is not None else self.applicable
        return tuple(n in self.values for n in wanted)

    def vector(self, names: Sequence[str] | None = None
               ) -> tuple[tuple[float | None, ...], tuple[bool, ...]]:
        """`(values, mask)` in order, with `None` where the mask is False.

        Returned as a pair rather than as a pre-filled array so that the caller
        must decide what an absent channel means for its own arithmetic. An
        encoder substitutes a *learned* absent-embedding; a report prints a
        dash. Neither is the other's default.
        """
        wanted = tuple(names) if names is not None else self.applicable
        return (tuple(self.values.get(n) for n in wanted),
                tuple(n in self.values for n in wanted))

    @property
    def missing_channels(self) -> tuple[str, ...]:
        return tuple(n for n in self.applicable if n not in self.values)

    @property
    def completeness(self) -> float:
        """Fraction of applicable channels actually observed."""
        if not self.applicable:
            return 0.0
        return len(set(self.values) & set(self.applicable)) / len(self.applicable)

    def to_dict(self) -> dict:
        return {"schema_fingerprint": self.schema.fingerprint(),
                "values": dict(sorted(self.values.items())),
                "applicable": list(self.applicable),
                "staleness": dict(sorted(self.staleness.items())),
                "missing": list(self.missing_channels),
                "completeness": round(self.completeness, 6)}


# ===========================================================================
# the built-in registry
# ===========================================================================
#
# Availability is stated per channel and is deliberately pessimistic. In this
# environment no market-data provider is reachable (blocker B1), so anything
# beyond what `contracts.Candle` carries — plus what Olympus can compute from
# it, from the calendar, or from its own portfolio — is NOT_INGESTED or
# UNREACHABLE. `OLYMPUS_MARKET_SCHEMA.availability_report()` is the summary.

def _price(name: str, description: str, *, normalisation=Normalisation.LOG_RATIO_PREVIOUS,
           availability=Availability.AVAILABLE, source="candle",
           timestamp=TimestampSemantics.BAR_CLOSE,
           missing=MissingPolicy.REQUIRED) -> ChannelSpec:
    return ChannelSpec(name=name, kind=ChannelType.PRICE,
                       unit=Unit.QUOTE_CURRENCY, source=source,
                       timestamp=timestamp, missing=missing,
                       normalisation=normalisation, availability=availability,
                       description=description, group="price",
                       bounds=(0.0, None))


_OHLC = [
    _price("open", "First trade price in the bar."),
    _price("high", "Highest trade price in the bar.",
           normalisation=Normalisation.LOG_RATIO_CLOSE),
    _price("low", "Lowest trade price in the bar.",
           normalisation=Normalisation.LOG_RATIO_CLOSE),
    _price("close", "Last trade price in the bar. The forecast anchor."),
]

_VOLUME = [
    ChannelSpec(name="base_volume", kind=ChannelType.VOLUME, unit=Unit.BASE_UNITS,
                source="candle", timestamp=TimestampSemantics.INTERVAL_AGGREGATE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.LOG_RATIO_PREVIOUS,
                availability=Availability.AVAILABLE, group="volume",
                bounds=(0.0, None),
                description="Traded quantity in base units. `Candle.volume`."),
    ChannelSpec(name="quote_volume", kind=ChannelType.VOLUME, unit=Unit.QUOTE_UNITS,
                source="candle", timestamp=TimestampSemantics.INTERVAL_AGGREGATE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.LOG_RATIO_PREVIOUS,
                availability=Availability.AVAILABLE, group="volume",
                bounds=(0.0, None),
                description="Traded notional in the quote currency. "
                            "`Candle.amount`; zero when the venue omits it."),
    ChannelSpec(name="trade_count", kind=ChannelType.COUNT, unit=Unit.COUNT,
                source="venue.trades", timestamp=TimestampSemantics.INTERVAL_AGGREGATE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.NOT_INGESTED, group="volume",
                bounds=(0.0, None),
                description="Number of trades in the bar. Separates one large "
                            "print from many small ones, which the volume "
                            "total cannot."),
    ChannelSpec(name="buy_volume", kind=ChannelType.VOLUME, unit=Unit.BASE_UNITS,
                source="venue.trades", timestamp=TimestampSemantics.INTERVAL_AGGREGATE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.NOT_INGESTED, group="microstructure",
                bounds=(0.0, None),
                description="Volume executed against the ask (aggressor buys)."),
    ChannelSpec(name="sell_volume", kind=ChannelType.VOLUME, unit=Unit.BASE_UNITS,
                source="venue.trades", timestamp=TimestampSemantics.INTERVAL_AGGREGATE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.NOT_INGESTED, group="microstructure",
                bounds=(0.0, None),
                description="Volume executed against the bid (aggressor sells)."),
]

_MICROSTRUCTURE = [
    ChannelSpec(name="bid", kind=ChannelType.PRICE, unit=Unit.QUOTE_CURRENCY,
                source="venue.book", timestamp=TimestampSemantics.SNAPSHOT_AT_CLOSE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.LOG_RATIO_CLOSE,
                availability=Availability.NOT_INGESTED, group="microstructure",
                bounds=(0.0, None),
                description="Best bid at the bar close. A snapshot, not an "
                            "aggregate — it describes one instant, and using it "
                            "as though it held over the bar overstates what was "
                            "tradable."),
    ChannelSpec(name="ask", kind=ChannelType.PRICE, unit=Unit.QUOTE_CURRENCY,
                source="venue.book", timestamp=TimestampSemantics.SNAPSHOT_AT_CLOSE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.LOG_RATIO_CLOSE,
                availability=Availability.NOT_INGESTED, group="microstructure",
                bounds=(0.0, None), description="Best ask at the bar close."),
    ChannelSpec(name="bid_ask_spread", kind=ChannelType.SPREAD,
                unit=Unit.BASIS_POINTS, source="olympus.trading.native.schema",
                timestamp=TimestampSemantics.SNAPSHOT_AT_CLOSE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.NOT_INGESTED, group="microstructure",
                bounds=(0.0, None), derives_from=("bid", "ask"),
                description="(ask - bid) / mid, in basis points. Derivable the "
                            "moment bid and ask are ingested, and not before."),
    ChannelSpec(name="book_depth_bid", kind=ChannelType.DEPTH, unit=Unit.BASE_UNITS,
                source="venue.book", timestamp=TimestampSemantics.SNAPSHOT_AT_CLOSE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.NOT_INGESTED, group="microstructure",
                bounds=(0.0, None),
                description="Resting size within a fixed band below the mid. "
                            "The execution-cost head's main input."),
    ChannelSpec(name="book_depth_ask", kind=ChannelType.DEPTH, unit=Unit.BASE_UNITS,
                source="venue.book", timestamp=TimestampSemantics.SNAPSHOT_AT_CLOSE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.NOT_INGESTED, group="microstructure",
                bounds=(0.0, None),
                description="Resting size within a fixed band above the mid."),
    ChannelSpec(name="book_imbalance", kind=ChannelType.IMBALANCE, unit=Unit.FRACTION,
                source="olympus.trading.native.schema",
                timestamp=TimestampSemantics.SNAPSHOT_AT_CLOSE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.BOUNDED_UNIT,
                availability=Availability.NOT_INGESTED, group="microstructure",
                bounds=(-1.0, 1.0),
                derives_from=("book_depth_bid", "book_depth_ask"),
                description="(bid_depth - ask_depth) / (bid_depth + ask_depth)."),
    ChannelSpec(name="trade_imbalance", kind=ChannelType.IMBALANCE, unit=Unit.FRACTION,
                source="olympus.trading.native.schema",
                timestamp=TimestampSemantics.INTERVAL_AGGREGATE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.BOUNDED_UNIT,
                availability=Availability.NOT_INGESTED, group="microstructure",
                bounds=(-1.0, 1.0), derives_from=("buy_volume", "sell_volume"),
                description="(buy - sell) / (buy + sell) over the bar. An "
                            "aggregate, unlike book_imbalance, which is a "
                            "snapshot — they are not substitutes."),
]

_DERIVATIVES = [
    ChannelSpec(name="funding_rate", kind=ChannelType.RATE, unit=Unit.PER_INTERVAL,
                source="venue.derivatives", timestamp=TimestampSemantics.ANNOUNCED,
                missing=MissingPolicy.LAST_KNOWN, max_staleness_bars=8,
                normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.NOT_INGESTED, group="derivatives",
                instruments=("crypto", "future"),
                description="Perpetual funding, announced on the venue's own "
                            "schedule rather than on bar boundaries. ANNOUNCED, "
                            "so it needs its announcement time — stamping it "
                            "with the containing bar leaks up to one interval."),
    ChannelSpec(name="open_interest", kind=ChannelType.VOLUME, unit=Unit.CONTRACTS,
                source="venue.derivatives", timestamp=TimestampSemantics.SNAPSHOT_AT_CLOSE,
                missing=MissingPolicy.LAST_KNOWN, max_staleness_bars=4,
                normalisation=Normalisation.LOG_RATIO_PREVIOUS,
                availability=Availability.NOT_INGESTED, group="derivatives",
                instruments=("crypto", "future"), bounds=(0.0, None),
                description="Outstanding contracts. Often published on a slower "
                            "cadence than the bar, hence LAST_KNOWN."),
    ChannelSpec(name="liquidations_long", kind=ChannelType.MONETARY,
                unit=Unit.QUOTE_UNITS, source="venue.derivatives",
                timestamp=TimestampSemantics.INTERVAL_AGGREGATE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.NOT_INGESTED, group="derivatives",
                instruments=("crypto", "future"), bounds=(0.0, None),
                description="Forced closes of long positions over the bar."),
    ChannelSpec(name="liquidations_short", kind=ChannelType.MONETARY,
                unit=Unit.QUOTE_UNITS, source="venue.derivatives",
                timestamp=TimestampSemantics.INTERVAL_AGGREGATE,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.NOT_INGESTED, group="derivatives",
                instruments=("crypto", "future"), bounds=(0.0, None),
                description="Forced closes of short positions over the bar."),
]

_VOLATILITY = [
    ChannelSpec(name="realised_volatility", kind=ChannelType.VOLATILITY,
                unit=Unit.DIMENSIONLESS, source="olympus.trading.volatility",
                timestamp=TimestampSemantics.DERIVED_CAUSAL,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.DERIVABLE, group="volatility",
                bounds=(0.0, None), derives_from=("close",),
                description="Per-bar close-to-close sigma over a trailing "
                            "window. Absent during warm-up rather than zero — a "
                            "zero volatility is a tradable claim, not a gap."),
    ChannelSpec(name="parkinson_volatility", kind=ChannelType.VOLATILITY,
                unit=Unit.DIMENSIONLESS, source="olympus.trading.volatility",
                timestamp=TimestampSemantics.DERIVED_CAUSAL,
                missing=MissingPolicy.MASK,
                normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.DERIVABLE, group="volatility",
                bounds=(0.0, None), derives_from=("high", "low"),
                description="High-low range estimator. More efficient than "
                            "close-to-close and blind to overnight gaps, which "
                            "is why both are carried."),
]

_CALENDAR = [
    ChannelSpec(name="time_of_day", kind=ChannelType.CYCLICAL, unit=Unit.FRACTION,
                source="calendar", timestamp=TimestampSemantics.KNOWN_IN_ADVANCE,
                missing=MissingPolicy.MASK, normalisation=Normalisation.CYCLICAL,
                availability=Availability.AVAILABLE, group="calendar",
                bounds=(0.0, 1.0),
                description="Fraction of the day elapsed at the bar close, "
                            "encoded as (sin, cos) so 23:59 and 00:01 are near."),
    ChannelSpec(name="day_of_week", kind=ChannelType.CYCLICAL, unit=Unit.FRACTION,
                source="calendar", timestamp=TimestampSemantics.KNOWN_IN_ADVANCE,
                missing=MissingPolicy.MASK, normalisation=Normalisation.CYCLICAL,
                availability=Availability.AVAILABLE, group="calendar",
                bounds=(0.0, 1.0), description="Weekday as a circular position."),
    ChannelSpec(name="is_month_end", kind=ChannelType.BOOLEAN, unit=Unit.BOOLEAN,
                source="calendar", timestamp=TimestampSemantics.KNOWN_IN_ADVANCE,
                missing=MissingPolicy.MASK, normalisation=Normalisation.NONE,
                availability=Availability.AVAILABLE, group="calendar",
                bounds=(0.0, 1.0),
                description="Rebalancing flows cluster here."),
]

_SESSION = [
    ChannelSpec(name="session_state", kind=ChannelType.CATEGORICAL, unit=Unit.CATEGORY,
                source="olympus.trading.instruments",
                timestamp=TimestampSemantics.KNOWN_IN_ADVANCE,
                missing=MissingPolicy.UNKNOWN_LEVEL, normalisation=Normalisation.ONE_HOT,
                availability=Availability.AVAILABLE, group="session",
                levels=("unknown", "closed", "pre_open", "open", "post_close",
                        "continuous"),
                description="Where the bar sits in the venue's trading day. "
                            "`MarketSession` supplies it; continuous venues "
                            "report `continuous` rather than pretending to have "
                            "an open."),
    ChannelSpec(name="minutes_since_open", kind=ChannelType.DURATION, unit=Unit.SECONDS,
                source="olympus.trading.instruments",
                timestamp=TimestampSemantics.KNOWN_IN_ADVANCE,
                missing=MissingPolicy.MASK, normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.AVAILABLE, group="session",
                bounds=(0.0, None),
                description="Absent, not zero, on a continuous venue."),
    ChannelSpec(name="minutes_to_close", kind=ChannelType.DURATION, unit=Unit.SECONDS,
                source="olympus.trading.instruments",
                timestamp=TimestampSemantics.KNOWN_IN_ADVANCE,
                missing=MissingPolicy.MASK, normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.AVAILABLE, group="session",
                bounds=(0.0, None),
                description="Closing-auction proximity."),
]

_REGIME = [
    ChannelSpec(name="regime_label", kind=ChannelType.CATEGORICAL, unit=Unit.CATEGORY,
                source="olympus.trading.regime",
                timestamp=TimestampSemantics.DERIVED_CAUSAL,
                missing=MissingPolicy.UNKNOWN_LEVEL, normalisation=Normalisation.ONE_HOT,
                availability=Availability.DERIVABLE, group="regime",
                levels=("unknown", "trending_up", "trending_down", "ranging",
                        "volatile", "illiquid"),
                description="Olympus's own classifier output — a weak label the "
                            "system owns end to end, which is what makes it "
                            "usable as supervision without a licence question."),
    ChannelSpec(name="regime_confidence", kind=ChannelType.RATIO, unit=Unit.FRACTION,
                source="olympus.trading.regime",
                timestamp=TimestampSemantics.DERIVED_CAUSAL,
                missing=MissingPolicy.MASK, normalisation=Normalisation.BOUNDED_UNIT,
                availability=Availability.DERIVABLE, group="regime",
                bounds=(0.0, 1.0), derives_from=("regime_label",),
                description="How near the classifier was to a boundary. A "
                            "hysteresis-held label with low confidence is not "
                            "the same observation as a decisive one."),
]

_CROSS_ASSET = [
    ChannelSpec(name="cross_asset_return", kind=ChannelType.RATIO, unit=Unit.LOG_RATIO,
                source="olympus.trading.native.dataset",
                timestamp=TimestampSemantics.DERIVED_CAUSAL,
                missing=MissingPolicy.MASK, normalisation=Normalisation.INSTANCE_ZSCORE,
                availability=Availability.DERIVABLE, group="cross_asset",
                description="Contemporaneous log return of a declared reference "
                            "instrument, aligned on bar close. Requires a "
                            "reference series in the same dataset — absent, not "
                            "zero, when the reference has a gap."),
    ChannelSpec(name="cross_asset_correlation", kind=ChannelType.RATIO,
                unit=Unit.FRACTION, source="olympus.trading.native.dataset",
                timestamp=TimestampSemantics.DERIVED_CAUSAL,
                missing=MissingPolicy.MASK, normalisation=Normalisation.BOUNDED_UNIT,
                availability=Availability.DERIVABLE, group="cross_asset",
                bounds=(-1.0, 1.0), derives_from=("cross_asset_return",),
                description="Trailing correlation with the reference. Computed "
                            "on the input window only."),
]

_EVENTS = [
    ChannelSpec(name="event_bars_until", kind=ChannelType.DURATION, unit=Unit.BARS,
                source="calendar.economic",
                timestamp=TimestampSemantics.KNOWN_IN_ADVANCE,
                missing=MissingPolicy.MASK, normalisation=Normalisation.SIGNED_LOG1P,
                availability=Availability.NOT_INGESTED, group="events",
                bounds=(0.0, None),
                description="Bars until the next *scheduled* release. Timing is "
                            "KNOWN_IN_ADVANCE; the release's content is not, and "
                            "is deliberately not a channel here."),
    ChannelSpec(name="event_importance", kind=ChannelType.CATEGORICAL,
                unit=Unit.CATEGORY, source="calendar.economic",
                timestamp=TimestampSemantics.KNOWN_IN_ADVANCE,
                missing=MissingPolicy.UNKNOWN_LEVEL, normalisation=Normalisation.ONE_HOT,
                availability=Availability.NOT_INGESTED, group="events",
                levels=("unknown", "none", "low", "medium", "high"),
                description="Declared importance of the upcoming release."),
    ChannelSpec(name="event_surprise", kind=ChannelType.RATIO, unit=Unit.DIMENSIONLESS,
                source="calendar.economic", timestamp=TimestampSemantics.REVISED,
                missing=MissingPolicy.MASK, normalisation=Normalisation.TRAIN_ROBUST,
                availability=Availability.UNREACHABLE, group="events",
                description="(actual - consensus) / historical dispersion. "
                            "REVISED: the figure printed on the day is not the "
                            "figure in the archive, so a backfilled series is a "
                            "look-ahead unless it carries vintages. Marked "
                            "UNREACHABLE rather than merely un-ingested — a "
                            "vintaged source is what would be needed."),
]

_PORTFOLIO = [
    ChannelSpec(name="position_side", kind=ChannelType.CATEGORICAL, unit=Unit.CATEGORY,
                source="olympus.trading.portfolio",
                timestamp=TimestampSemantics.SNAPSHOT_AT_CLOSE,
                missing=MissingPolicy.UNKNOWN_LEVEL, normalisation=Normalisation.ONE_HOT,
                availability=Availability.AVAILABLE, group="portfolio",
                levels=("unknown", "flat", "long", "short"),
                description="What Olympus already holds. A forecast conditioned "
                            "on this is *evidence*; it is never sizing, which "
                            "stays with the risk engine."),
    ChannelSpec(name="position_exposure", kind=ChannelType.RATIO, unit=Unit.FRACTION,
                source="olympus.trading.portfolio",
                timestamp=TimestampSemantics.SNAPSHOT_AT_CLOSE,
                missing=MissingPolicy.MASK, normalisation=Normalisation.BOUNDED_UNIT,
                availability=Availability.AVAILABLE, group="portfolio",
                bounds=(-1.0, 1.0),
                description="Signed position notional as a fraction of equity."),
    ChannelSpec(name="portfolio_drawdown", kind=ChannelType.RATIO, unit=Unit.FRACTION,
                source="olympus.trading.portfolio",
                timestamp=TimestampSemantics.SNAPSHOT_AT_CLOSE,
                missing=MissingPolicy.MASK, normalisation=Normalisation.BOUNDED_UNIT,
                availability=Availability.AVAILABLE, group="portfolio",
                bounds=(0.0, 1.0),
                description="Current drawdown from peak equity."),
]


OLYMPUS_MARKET_SCHEMA = MarketStateSchema(
    [*_OHLC, *_VOLUME, *_MICROSTRUCTURE, *_DERIVATIVES, *_VOLATILITY,
     *_CALENDAR, *_SESSION, *_REGIME, *_CROSS_ASSET, *_EVENTS, *_PORTFOLIO],
    version=SCHEMA_VERSION, name="olympus.market.v1")

#: What a model in *this* environment may actually be trained on. Everything
#: else is designed, documented and unreachable — see B1.
OBTAINABLE_SCHEMA = OLYMPUS_MARKET_SCHEMA.obtainable_subset(
    name="olympus.market.v1:obtainable")


__all__ = ["SCHEMA_VERSION", "ANY", "ChannelType", "Unit", "TimestampSemantics",
           "MissingPolicy", "Normalisation", "Availability", "ChannelSpec",
           "MarketStateSchema", "ChannelObservation", "OLYMPUS_MARKET_SCHEMA",
           "OBTAINABLE_SCHEMA"]
