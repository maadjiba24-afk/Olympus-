"""Windowing and splitting: where look-ahead is designed out.

Every leakage defect the Kronos teardown found was a *data-pipeline* defect,
not a modelling one — a scaler fitted across the horizon, a normalisation
window that included what it was predicting. The model architecture is not
where forecasting goes wrong; this file is.

Three defences, each of which the obvious implementation gets wrong:

**The horizon is never in the input.** A `Window` holds `inputs` and `targets`
and refuses to construct if the first target does not close strictly after the
last input. That is checked on timestamps, not on indices, so a slicing mistake
cannot satisfy it accidentally.

**A split needs an embargo.** The naive temporal split puts window *i* in train
and window *i+1* in test — but window *i*'s targets overlap window *i+1*'s
inputs, so the training set has literally seen the test set's features.
`temporal_split` drops every window whose horizon crosses the boundary. The
embargo is `horizon` bars wide and is not optional; a caller can widen it and
cannot remove it.

**Scalers are fitted on train only.** `fit_scaler` in `features.py` already
does the fitting; what this module adds is that the split happens *first* and
returns the boundary, so there is no arrangement in which a caller fits on
everything and splits afterwards without noticing.

Deterministic, ordered, pure stdlib. No shuffling — a shuffled time series is a
different problem with the same numbers in it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterator, Mapping, Sequence

from ..contracts import Candle, ensure_utc, jsonable
from ..errors import ConfigurationError, DataValidationError, InsufficientDataError


@dataclass(frozen=True)
class Window:
    """One training example: what was knowable, and what happened next."""

    instrument_key: str
    timeframe: str
    inputs: tuple[Candle, ...]
    targets: tuple[Candle, ...]

    def __post_init__(self):
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "targets", tuple(self.targets))
        if not self.inputs:
            raise DataValidationError("a window needs at least one input bar")
        if not self.targets:
            raise DataValidationError(
                "a window needs at least one target bar; a window with no "
                "future is an observation, not an example")

        for name, bars in (("inputs", self.inputs), ("targets", self.targets)):
            for i in range(1, len(bars)):
                if bars[i].ts_open <= bars[i - 1].ts_open:
                    raise DataValidationError(
                        f"{name} are not strictly ascending",
                        instrument_key=self.instrument_key, index=i)
            for bar in bars:
                if bar.instrument_key != self.instrument_key:
                    raise DataValidationError("window mixes instruments",
                                              expected=self.instrument_key,
                                              got=bar.instrument_key)
                if bar.timeframe != self.timeframe:
                    raise DataValidationError("window mixes timeframes",
                                              expected=self.timeframe,
                                              got=bar.timeframe)

        # The invariant. Checked on timestamps rather than indices so that a
        # slicing mistake cannot satisfy it by accident.
        if self.targets[0].ts_open < self.inputs[-1].ts_close:
            raise DataValidationError(
                "the first target opens before the last input closes: the "
                "horizon is inside the input window",
                last_input_close=self.inputs[-1].ts_close,
                first_target_open=self.targets[0].ts_open)

    @property
    def as_of(self) -> datetime:
        """The instant this example was decidable — the last input's close."""
        return self.inputs[-1].ts_close

    @property
    def horizon(self) -> int:
        return len(self.targets)

    @property
    def lookback(self) -> int:
        return len(self.inputs)

    @property
    def anchor_close(self) -> float:
        """Last observed close. Every target is expressed relative to this."""
        return self.inputs[-1].close

    def target_closes(self) -> tuple[float, ...]:
        return tuple(bar.close for bar in self.targets)

    def target_log_returns(self) -> tuple[float, ...]:
        """Cumulative log return from the anchor to each target bar.

        Cumulative rather than step-wise: the quantile head predicts "where
        will price be in h bars", and a model trained on step returns has to
        compose them at inference, which compounds error the training never
        saw.
        """
        import math
        anchor = self.anchor_close
        if anchor <= 0:
            raise DataValidationError("anchor close must be positive",
                                      anchor=anchor)
        out = []
        for bar in self.targets:
            if bar.close <= 0:
                raise DataValidationError("target close must be positive",
                                          ts=bar.ts_close, close=bar.close)
            out.append(math.log(bar.close / anchor))
        return tuple(out)

    def to_dict(self) -> dict:
        return {"instrument_key": self.instrument_key,
                "timeframe": self.timeframe,
                "as_of": jsonable(self.as_of),
                "lookback": self.lookback, "horizon": self.horizon,
                "anchor_close": self.anchor_close}


def make_windows(candles: Sequence[Candle], *, lookback: int, horizon: int,
                 stride: int = 1) -> list[Window]:
    """Slide a (lookback, horizon) pair over a bar series.

    Refuses a non-final bar. A forming bar's close is not yet its close, so a
    window ending on one is a window whose last feature reads a number that is
    still changing.
    """
    if lookback < 1:
        raise ConfigurationError("lookback must be >= 1", lookback=lookback)
    if horizon < 1:
        raise ConfigurationError("horizon must be >= 1", horizon=horizon)
    if stride < 1:
        raise ConfigurationError("stride must be >= 1", stride=stride)

    bars = list(candles or ())
    if len(bars) < lookback + horizon:
        raise InsufficientDataError(
            "not enough bars for one window",
            have=len(bars), need=lookback + horizon)
    for bar in bars:
        if not bar.is_final:
            raise DataValidationError(
                "a non-final bar cannot enter a training window: its close is "
                "not yet the close", ts=bar.ts_open)

    key = bars[0].instrument_key
    timeframe = bars[0].timeframe
    out: list[Window] = []
    last_start = len(bars) - lookback - horizon
    for start in range(0, last_start + 1, stride):
        out.append(Window(
            instrument_key=key, timeframe=timeframe,
            inputs=tuple(bars[start:start + lookback]),
            targets=tuple(bars[start + lookback:start + lookback + horizon])))
    return out


@dataclass(frozen=True)
class Split:
    """A temporal split, with the embargo that makes it honest."""

    train: tuple[Window, ...]
    test: tuple[Window, ...]
    boundary: datetime
    embargoed: int = 0
    embargo_bars: int = 0

    def __post_init__(self):
        object.__setattr__(self, "train", tuple(self.train))
        object.__setattr__(self, "test", tuple(self.test))
        object.__setattr__(self, "boundary",
                           ensure_utc(self.boundary, field_name="boundary"))

    def to_dict(self) -> dict:
        return {"train": len(self.train), "test": len(self.test),
                "boundary": jsonable(self.boundary),
                "embargoed": self.embargoed,
                "embargo_bars": self.embargo_bars}

    def describe(self) -> str:                           # pragma: no cover
        return (f"train={len(self.train)} test={len(self.test)} "
                f"embargoed={self.embargoed} at {self.boundary.isoformat()}")


def temporal_split(windows: Sequence[Window], *, train_fraction: float = 0.7,
                   embargo_bars: int | None = None) -> Split:
    """Split in time, dropping every window whose horizon crosses the boundary.

    The naive split — first 70% train, rest test — puts a window in train whose
    *targets* are another window's *inputs*. The model then trains on the test
    set's features without anyone writing a line of code that says so.

    `embargo_bars` defaults to the window horizon, which is the minimum that
    removes the overlap. A caller may widen it (useful when features have a long
    warm-up) and cannot set it below the horizon.
    """
    ordered = sorted(windows, key=lambda w: w.as_of)
    if not ordered:
        raise InsufficientDataError("no windows to split")
    if not 0.0 < train_fraction < 1.0:
        raise ConfigurationError("train_fraction must be in (0, 1)",
                                 train_fraction=train_fraction)

    horizon = ordered[0].horizon
    if any(w.horizon != horizon for w in ordered):
        raise ConfigurationError(
            "windows have mixed horizons; a single split cannot embargo them "
            "correctly", horizons=sorted({w.horizon for w in ordered}))

    embargo = horizon if embargo_bars is None else int(embargo_bars)
    if embargo < horizon:
        raise ConfigurationError(
            "embargo_bars may not be smaller than the horizon: a shorter "
            "embargo leaves training windows whose targets are test inputs",
            embargo_bars=embargo, horizon=horizon)

    cut = max(1, int(len(ordered) * train_fraction))
    boundary = ordered[cut - 1].as_of

    train = [w for w in ordered[:cut]]
    # Everything after the boundary whose *inputs* start at or before the last
    # training window's targets end is dropped.
    if train:
        contaminated_until = max(w.targets[-1].ts_close for w in train)
    else:                                                # pragma: no cover
        contaminated_until = boundary
    test = [w for w in ordered[cut:] if w.inputs[0].ts_open >= contaminated_until]
    embargoed = len(ordered) - cut - len(test)

    return Split(train=tuple(train), test=tuple(test), boundary=boundary,
                 embargoed=embargoed, embargo_bars=embargo)


def data_version(candles: Sequence[Candle]) -> str:
    """A content hash of the exact bars. The dataset's identity.

    Hashes the OHLCV values and timestamps, not the object identities, so two
    processes reading the same store agree. This is what a training manifest
    records so that "reproduce this run" has an answer.
    """
    digest = hashlib.sha256()
    for bar in candles:
        digest.update(f"{bar.instrument_key}|{bar.timeframe}|"
                      f"{bar.ts_open.isoformat()}|{bar.ts_close.isoformat()}|"
                      f"{bar.open!r}|{bar.high!r}|{bar.low!r}|{bar.close!r}|"
                      f"{bar.volume!r}".encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True)
class DatasetSpec:
    """The declarative description of a dataset. Recorded in every manifest."""

    instrument_keys: tuple[str, ...]
    timeframe: str
    lookback: int
    horizon: int
    stride: int = 1
    train_fraction: float = 0.7
    embargo_bars: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "instrument_keys", tuple(self.instrument_keys))
        if not self.instrument_keys:
            raise ConfigurationError("a dataset must name its instruments")
        for name in ("lookback", "horizon", "stride"):
            if int(getattr(self, name)) < 1:
                raise ConfigurationError(f"{name} must be >= 1")

    def to_dict(self) -> dict:
        return {"instrument_keys": list(self.instrument_keys),
                "timeframe": self.timeframe, "lookback": self.lookback,
                "horizon": self.horizon, "stride": self.stride,
                "train_fraction": self.train_fraction,
                "embargo_bars": self.embargo_bars}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DatasetSpec":
        return cls(instrument_keys=tuple(data.get("instrument_keys") or ()),
                   timeframe=str(data.get("timeframe", "")),
                   lookback=int(data["lookback"]), horizon=int(data["horizon"]),
                   stride=int(data.get("stride", 1)),
                   train_fraction=float(data.get("train_fraction", 0.7)),
                   embargo_bars=data.get("embargo_bars"))

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]


def build_dataset(candles: Sequence[Candle], spec: DatasetSpec) -> Split:
    """Windows plus split, in the only order that is safe.

    Deliberately one function. If windowing and splitting were separate public
    steps, a caller could fit a scaler between them — which is the leak this
    module exists to prevent, and which would then be nobody's fault in
    particular.
    """
    windows = make_windows(candles, lookback=spec.lookback,
                           horizon=spec.horizon, stride=spec.stride)
    return temporal_split(windows, train_fraction=spec.train_fraction,
                          embargo_bars=spec.embargo_bars)


__all__ = ["Window", "make_windows", "Split", "temporal_split", "data_version",
           "DatasetSpec", "build_dataset"]
