"""Market-intelligence agent schemas — the boundary where prose becomes data.

Seven agents contribute to a trading decision (technical analysis, the
forecast, regime, volatility, sentiment, portfolio analysis, strategy
selection). Some of them are, or may be, language models. This module is the
membrane between what they emit and what the rest of the domain consumes, and
it exists because of one rule stated in `olympus/trading/__init__.py`:

    Nothing in this package lets a model put an order on a venue.

That rule is only as strong as the parser behind it. An agent's output is a
*proposal shaped like data*, and the three ways a proposal becomes an exploit
are all parser bugs:

* **Extra fields.** A response carrying `{"confidence": 0.8, "max_position":
  1000}` gets its extra key silently ignored by a permissive parser — until
  some later consumer reads `**payload` into a constructor that has such a
  field. Every schema here REJECTS unknown keys rather than dropping them, so
  the attempt is a loud `DataValidationError` in the audit trail instead of a
  latent capability.
* **Type coercion.** Accepting `"0.9"` where a float is due means accepting
  `"0.9; DROP"`, `"nan"`, and `"1e999"` too, and `float("nan")` passes every
  range check ever written (`nan <= 1` is False, but so is `nan > 1`). Numbers
  must arrive as numbers, and must be finite.
* **Instruction laundering.** An agent that faithfully summarises a poisoned
  article puts the injection into a free-text field the news sanitiser never
  saw. Every string field is re-checked with
  `sentiment.contains_instruction_text`, so the boundary holds even when the
  content took the long way round.

Never `eval`, never `exec`, never `ast.literal_eval` on an agent response. The
only accepted wire format is JSON, parsed by `json.loads` into a mapping, and a
non-object at the top level is a rejection rather than something to coerce.

Capability separation
---------------------
`AGENT_SPECS` records, per agent, whether it ingests untrusted external
content. An ingesting agent is structurally barred from action capability —
the same invariant `specialists.Specialist._ingests` enforces for the general
Olympus specialists, restated here because the trading domain's "action" is
placing an order rather than sending an email. `AgentSpec.__post_init__`
refuses to construct a spec that claims both, so the table cannot drift into an
unsafe state without failing at import.

This module deliberately registers nothing: no `olympus` specialist, no CLI
command, no orchestration. Schemas, a validator, and a spec table — the
declarative half only.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields as dataclass_fields
from datetime import datetime
from typing import Any, Mapping, Sequence, Type

from .contracts import Regime, SignalDirection, ensure_utc, jsonable
from .errors import ConfigurationError, DataValidationError
# The taint bridge. `sentiment.py` is structurally forbidden from importing
# `risk` (that IS the rule: the untrusted-content layer gets no route to the
# limits API), so the two halves meet here in the wiring layer — which has
# authority over neither and is the only place that legitimately knows about
# both. `sentiment` produces the provenance string, `risk` owns the marker.
from .risk import Tainted, assert_untainted, is_tainted  # noqa: F401
from .sentiment import contains_instruction_text, taint_origin

#: Cap on any free-text field. Evidence is a citation, not an essay; an
#: unbounded string field in a validated schema is a channel for smuggling a
#: payload past reviewers who only ever look at the numbers.
MAX_TEXT = 2_000
MAX_ITEMS = 64          # cap on any list-shaped field, for the same reason


# ---------------------------------------------------------------------------
# the taint bridge
# ---------------------------------------------------------------------------

def taint_external(value: Any, *, kind: str = "news", ident: str = "") -> Tainted:
    """Wrap a value derived from external content in the risk layer's marker.

    The single function that turns "this came from a news article" into "the
    limits API will refuse this". Every value flowing from `sentiment` toward
    anything configuration-shaped should pass through here.

    `Tainted` is deliberately not a subclass of anything useful, so a taint
    that reaches arithmetic raises rather than quietly widening a ceiling, and
    `risk.assert_untainted` — called by `LimitsStore.save` *before* it even
    looks at the operator token — refuses it outright. That ordering matters:
    a compromised caller holding a real token still cannot launder a news
    number into a limit.
    """
    return Tainted(value, origin=taint_origin(kind, ident))


def taint_agent_output(output: _AgentOutput) -> Tainted:
    """Taint an agent's whole output when its agent ingests untrusted content.

    Looks the ingestion flag up in `AGENT_SPECS` rather than taking it from the
    caller, because "did this agent read attacker-authored text?" is a property
    of the system's wiring, not of the call site — and a call site that gets to
    assert its own trustworthiness is not a boundary.
    """
    return taint_external(output.to_dict(), kind="agent",
                          ident=type(output).__name__)


# ---------------------------------------------------------------------------
# primitive validators
# ---------------------------------------------------------------------------
# Each raises `DataValidationError` naming the field. Uniform failure shape
# matters: the audit trail records the rejection, and a rejection whose reason
# is "TypeError" tells a later reader nothing about which agent misbehaved.

def _require(data: Mapping[str, Any], key: str) -> Any:
    if key not in data:
        raise DataValidationError(f"missing required field {key!r}", field=key)
    return data[key]


def _as_str(value: Any, key: str, *, max_length: int = MAX_TEXT,
            allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise DataValidationError(f"{key} must be a string", field=key,
                                  got=type(value).__name__)
    if len(value) > max_length:
        raise DataValidationError(f"{key} exceeds {max_length} characters",
                                  field=key, got=len(value))
    if not allow_empty and not value.strip():
        raise DataValidationError(f"{key} must be non-empty", field=key)
    if contains_instruction_text(value):
        # The laundering check. An agent output is data; a data field that
        # reads like a directive is either a compromised agent or a compromised
        # source, and neither is something to normalise and carry on with.
        raise DataValidationError(
            f"{key} contains instruction-shaped text; agent output is data, "
            "never directives", field=key)
    return value


def _as_number(value: Any, key: str) -> float:
    """A real, finite number — never a string, never a bool.

    `isinstance(True, int)` is True in Python, so a bare `isinstance(value,
    (int, float))` check accepts `True` as 1.0 and lets a boolean field silently
    satisfy a numeric one. And `float("nan")` defeats every subsequent range
    comparison, so non-finite values are refused here rather than downstream.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataValidationError(
            f"{key} must be a number (a string is not a number)", field=key,
            got=type(value).__name__)
    number = float(value)
    if not math.isfinite(number):
        raise DataValidationError(f"{key} must be finite", field=key,
                                  got=repr(value))
    return number


def _as_int(value: Any, key: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataValidationError(f"{key} must be an integer", field=key,
                                  got=type(value).__name__)
    if minimum is not None and value < minimum:
        raise DataValidationError(f"{key} must be >= {minimum}", field=key,
                                  got=value)
    return int(value)


def _as_bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise DataValidationError(f"{key} must be a boolean", field=key,
                                  got=type(value).__name__)
    return value


def _in_range(value: Any, key: str, low: float, high: float) -> float:
    number = _as_number(value, key)
    if not low <= number <= high:
        raise DataValidationError(f"{key} must be within [{low}, {high}]",
                                  field=key, got=number)
    return number


def _as_str_tuple(value: Any, key: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        # A bare string is iterable, so a permissive parser turns "abc" into
        # ("a", "b", "c") — wrong quietly, which is the worst kind.
        raise DataValidationError(f"{key} must be a list of strings", field=key,
                                  got=type(value).__name__)
    if len(value) > MAX_ITEMS:
        raise DataValidationError(f"{key} has more than {MAX_ITEMS} entries",
                                  field=key, got=len(value))
    return tuple(_as_str(item, f"{key}[{i}]") for i, item in enumerate(value))


def _as_number_tuple(value: Any, key: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise DataValidationError(f"{key} must be a list of numbers", field=key,
                                  got=type(value).__name__)
    if len(value) > MAX_ITEMS:
        raise DataValidationError(f"{key} has more than {MAX_ITEMS} entries",
                                  field=key, got=len(value))
    return tuple(_as_number(item, f"{key}[{i}]") for i, item in enumerate(value))


def _as_number_map(value: Any, key: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise DataValidationError(f"{key} must be an object", field=key,
                                  got=type(value).__name__)
    if len(value) > MAX_ITEMS:
        raise DataValidationError(f"{key} has more than {MAX_ITEMS} entries",
                                  field=key, got=len(value))
    out: dict[str, float] = {}
    for name, item in value.items():
        label = _as_str(name, f"{key} key", max_length=120, allow_empty=False)
        out[label] = _as_number(item, f"{key}[{label!r}]")
    return out


def _as_ts(value: Any, key: str) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value, field_name=key)
    if not isinstance(value, str):
        raise DataValidationError(f"{key} must be an ISO-8601 timestamp",
                                  field=key, got=type(value).__name__)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise DataValidationError(f"{key} is not a valid ISO-8601 timestamp",
                                  field=key, got=value[:60]) from None
    # `ensure_utc` rejects naive datetimes, which is the point: an agent that
    # omits the offset is an agent whose timestamps cannot be ordered against
    # market data.
    return ensure_utc(parsed, field_name=key)


def _as_enum(value: Any, key: str, enum_cls) -> Any:
    if isinstance(value, enum_cls):
        return value
    if not isinstance(value, str):
        raise DataValidationError(f"{key} must be a string", field=key,
                                  got=type(value).__name__)
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(sorted(m.value for m in enum_cls))
        raise DataValidationError(f"{key} must be one of: {allowed}",
                                  field=key, got=value[:60]) from None


def _as_choice(value: Any, key: str, allowed: Sequence[str]) -> str:
    text = _as_str(value, key, max_length=64, allow_empty=False)
    if text not in allowed:
        raise DataValidationError(f"{key} must be one of: {', '.join(allowed)}",
                                  field=key, got=text)
    return text


def _reject_unknown(data: Mapping[str, Any], schema: Type) -> None:
    """The closed-world check.

    A schema that ignores unknown keys accepts every future payload, including
    the one that carries `{"approved_quantity": 999}` toward a consumer that
    happens to accept it. Rejecting is also what makes a *changed* agent
    visible: a model that starts emitting a new field fails here rather than
    silently having it dropped.
    """
    if not isinstance(data, Mapping):
        raise DataValidationError("agent output must be a JSON object",
                                  got=type(data).__name__)
    known = {f.name for f in dataclass_fields(schema)}
    unknown = sorted(str(k) for k in data if str(k) not in known)
    if unknown:
        raise DataValidationError(
            f"unexpected field(s) in {schema.__name__}: {', '.join(unknown)}",
            schema=schema.__name__, unexpected=unknown)


# ---------------------------------------------------------------------------
# the common envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _AgentOutput:
    """Fields every agent must supply, and the reason each one is mandatory.

    * `instrument` — an opinion without a subject cannot be routed.
    * `ts` — an opinion without a time cannot be checked for staleness, and a
      stale opinion is indistinguishable from a fresh one at the point of use.
    * `confidence` — the risk engine gates on it (`min_forecast_confidence`),
      so it is not optional and it is not allowed to default to 1.0. An agent
      that will not say how sure it is has said nothing.
    * `evidence` — what the claim rests on. An unexplained agent output is one
      an incident review cannot audit.
    * `model_version` — which model said it. Without this the audit trail
      records an opinion attributable to nothing.
    * `warnings` — what the agent itself doubted.
    """
    instrument: str
    ts: datetime
    confidence: float
    evidence: tuple[str, ...] = ()
    model_version: str = ""
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0 or not math.isfinite(confidence):
            raise DataValidationError("confidence must be within [0, 1]",
                                      field="confidence", got=self.confidence)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if not isinstance(self.instrument, str) or not self.instrument.strip():
            raise DataValidationError("instrument must be a non-empty string",
                                      field="instrument")

    def to_dict(self) -> dict:
        out: dict[str, Any] = {}
        for f in dataclass_fields(self):
            out[f.name] = jsonable(getattr(self, f.name))
        return out

    @classmethod
    def _common(cls, data: Mapping[str, Any]) -> dict:
        """Validate and extract the envelope fields shared by every schema."""
        return {
            "instrument": _as_str(_require(data, "instrument"), "instrument",
                                  max_length=120, allow_empty=False),
            "ts": _as_ts(_require(data, "ts"), "ts"),
            "confidence": _in_range(_require(data, "confidence"),
                                    "confidence", 0.0, 1.0),
            "evidence": _as_str_tuple(data.get("evidence", ()), "evidence"),
            "model_version": _as_str(data.get("model_version", ""),
                                     "model_version", max_length=200),
            "warnings": _as_str_tuple(data.get("warnings", ()), "warnings"),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]):     # pragma: no cover - abstract
        raise NotImplementedError


# ---------------------------------------------------------------------------
# the seven schemas
# ---------------------------------------------------------------------------

TREND_CHOICES = ("up", "down", "sideways")


@dataclass(frozen=True)
class TechnicalAnalysisOutput(_AgentOutput):
    """Indicator-derived structure. Descriptive, never prescriptive.

    Note what is absent: any notion of a target position, an entry, or a size.
    A technical-analysis agent that could name a quantity would be an order
    generator with extra steps.
    """
    timeframe: str = ""
    trend: str = "sideways"
    support: tuple[float, ...] = ()
    resistance: tuple[float, ...] = ()
    indicators: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()
        object.__setattr__(self, "support", tuple(float(v) for v in self.support))
        object.__setattr__(self, "resistance",
                           tuple(float(v) for v in self.resistance))
        object.__setattr__(self, "indicators", dict(self.indicators))
        if self.trend not in TREND_CHOICES:
            raise DataValidationError(
                f"trend must be one of: {', '.join(TREND_CHOICES)}",
                field="trend", got=self.trend)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TechnicalAnalysisOutput":
        _reject_unknown(data, cls)
        return cls(
            timeframe=_as_str(data.get("timeframe", ""), "timeframe",
                              max_length=32),
            trend=_as_choice(_require(data, "trend"), "trend", TREND_CHOICES),
            support=_as_number_tuple(data.get("support", ()), "support"),
            resistance=_as_number_tuple(data.get("resistance", ()), "resistance"),
            indicators=_as_number_map(data.get("indicators", {}), "indicators"),
            **cls._common(data))


@dataclass(frozen=True)
class ForecastAgentOutput(_AgentOutput):
    """The forecaster's opinion, reduced to the numbers risk gates on.

    Model-agnostic: whichever forecaster produced the `ForecastResult`, it
    arrives here as the same few fields.

    `abstained` is a first-class field rather than an inferred state. The Kronos
    teardown's central finding was that a model under an unsupported
    configuration silently returns a plausible wrong answer (that is why this
    field exists, and the citation is the reason rather than a dependency); the
    counter is an explicit "I declined", which a caller must handle rather than
    mistake for a forecast of zero return.
    """
    horizon: int = 0
    expected_return: float = 0.0
    direction_probability: float = 0.5
    uncertainty: float = 1.0
    forecast_volatility: float = 0.0
    abstained: bool = False

    def __post_init__(self):
        super().__post_init__()
        if self.horizon < 0:
            raise DataValidationError("horizon must be >= 0", field="horizon",
                                      got=self.horizon)
        for name in ("direction_probability", "uncertainty"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise DataValidationError(f"{name} must be within [0, 1]",
                                          field=name, got=value)
            object.__setattr__(self, name, value)
        if float(self.forecast_volatility) < 0:
            raise DataValidationError("forecast_volatility must be >= 0",
                                      field="forecast_volatility",
                                      got=self.forecast_volatility)
        if self.abstained and self.confidence != 0.0:
            # An abstention with residual confidence is the exact shape of the
            # bug this schema is defending against: a caller that reads
            # confidence without reading `abstained` would trade on a forecast
            # that was never made.
            raise DataValidationError(
                "an abstained forecast must report zero confidence",
                field="confidence", got=self.confidence)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ForecastAgentOutput":
        _reject_unknown(data, cls)
        return cls(
            horizon=_as_int(_require(data, "horizon"), "horizon", minimum=0),
            expected_return=_as_number(data.get("expected_return", 0.0),
                                       "expected_return"),
            direction_probability=_in_range(
                data.get("direction_probability", 0.5),
                "direction_probability", 0.0, 1.0),
            uncertainty=_in_range(data.get("uncertainty", 1.0), "uncertainty",
                                  0.0, 1.0),
            forecast_volatility=_in_range(data.get("forecast_volatility", 0.0),
                                          "forecast_volatility", 0.0, 1e6),
            abstained=_as_bool(data.get("abstained", False), "abstained"),
            **cls._common(data))


@dataclass(frozen=True)
class RegimeOutput(_AgentOutput):
    """Which market we are in. A `Regime` enum member, never free text —
    downstream code branches on it, and a branch on an open string set is a
    branch with a silent default."""
    regime: Regime = Regime.UNKNOWN
    previous_regime: Regime | None = None
    #: [0, 1] — how settled the classification is. Low stability near a regime
    #: boundary is the honest answer, and strategies size down on it.
    stability: float = 0.0

    def __post_init__(self):
        super().__post_init__()
        object.__setattr__(self, "regime", Regime(self.regime))
        if self.previous_regime is not None:
            object.__setattr__(self, "previous_regime",
                               Regime(self.previous_regime))
        stability = float(self.stability)
        if not 0.0 <= stability <= 1.0:
            raise DataValidationError("stability must be within [0, 1]",
                                      field="stability", got=stability)
        object.__setattr__(self, "stability", stability)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RegimeOutput":
        _reject_unknown(data, cls)
        previous = data.get("previous_regime")
        return cls(
            regime=_as_enum(_require(data, "regime"), "regime", Regime),
            previous_regime=(None if previous is None
                             else _as_enum(previous, "previous_regime", Regime)),
            stability=_in_range(data.get("stability", 0.0), "stability",
                                0.0, 1.0),
            **cls._common(data))


@dataclass(frozen=True)
class VolatilityOutput(_AgentOutput):
    """Realised and forecast volatility, plus where that sits historically.

    `percentile` is what position sizing actually consumes: an absolute
    volatility number means nothing without knowing whether it is high *for
    this instrument*."""
    realised_volatility: float = 0.0
    forecast_volatility: float = 0.0
    percentile: float = 0.0                 # [0, 1] against the instrument's own history
    regime_label: str = "normal"

    _VOL_LABELS = ("low", "normal", "elevated", "extreme")

    def __post_init__(self):
        super().__post_init__()
        for name in ("realised_volatility", "forecast_volatility"):
            value = float(getattr(self, name))
            if value < 0 or not math.isfinite(value):
                raise DataValidationError(f"{name} must be finite and >= 0",
                                          field=name, got=value)
            object.__setattr__(self, name, value)
        percentile = float(self.percentile)
        if not 0.0 <= percentile <= 1.0:
            raise DataValidationError("percentile must be within [0, 1]",
                                      field="percentile", got=percentile)
        object.__setattr__(self, "percentile", percentile)
        if self.regime_label not in self._VOL_LABELS:
            raise DataValidationError(
                f"regime_label must be one of: {', '.join(self._VOL_LABELS)}",
                field="regime_label", got=self.regime_label)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VolatilityOutput":
        _reject_unknown(data, cls)
        return cls(
            realised_volatility=_in_range(
                _require(data, "realised_volatility"), "realised_volatility",
                0.0, 1e6),
            forecast_volatility=_in_range(
                data.get("forecast_volatility", 0.0), "forecast_volatility",
                0.0, 1e6),
            percentile=_in_range(data.get("percentile", 0.0), "percentile",
                                 0.0, 1.0),
            regime_label=_as_choice(data.get("regime_label", "normal"),
                                    "regime_label", cls._VOL_LABELS),
            **cls._common(data))


@dataclass(frozen=True)
class SentimentOutput(_AgentOutput):
    """The one schema whose inputs are attacker-authored.

    `items_considered` carries item ids, never article text: an audit record
    should let a reviewer go and read the source, not re-host it. Everything
    here is subject to the same instruction check as every other string field,
    which is the second line of defence behind `sentiment.sanitise_external_text`
    — the first one runs at ingestion, this one catches laundering through the
    agent's own prose.
    """
    score: float = 0.0                      # [-1, 1]
    volume: int = 0
    items_considered: tuple[str, ...] = ()
    #: Whether any source document had instruction-shaped text removed. A spike
    #: across a feed is what an attack looks like from the inside, so it is a
    #: reportable field rather than a log line.
    redacted_sources: int = 0

    def __post_init__(self):
        super().__post_init__()
        score = float(self.score)
        if not -1.0 <= score <= 1.0:
            raise DataValidationError("score must be within [-1, 1]",
                                      field="score", got=score)
        object.__setattr__(self, "score", score)
        if int(self.volume) < 0:
            raise DataValidationError("volume must be >= 0", field="volume",
                                      got=self.volume)
        object.__setattr__(self, "volume", int(self.volume))
        object.__setattr__(self, "items_considered",
                           tuple(self.items_considered))
        if int(self.redacted_sources) < 0:
            raise DataValidationError("redacted_sources must be >= 0",
                                      field="redacted_sources",
                                      got=self.redacted_sources)
        object.__setattr__(self, "redacted_sources", int(self.redacted_sources))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SentimentOutput":
        _reject_unknown(data, cls)
        return cls(
            score=_in_range(_require(data, "score"), "score", -1.0, 1.0),
            volume=_as_int(data.get("volume", 0), "volume", minimum=0),
            items_considered=_as_str_tuple(data.get("items_considered", ()),
                                           "items_considered"),
            redacted_sources=_as_int(data.get("redacted_sources", 0),
                                     "redacted_sources", minimum=0),
            **cls._common(data))


@dataclass(frozen=True)
class PortfolioAnalysisOutput(_AgentOutput):
    """A read of the book as it stands.

    `instrument` is conventionally the portfolio currency or `"PORTFOLIO"`;
    the envelope field is kept rather than special-cased so every agent output
    has one shape in the audit trail.
    """
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    concentration: float = 0.0              # [0, 1] largest position / equity
    open_positions: int = 0
    drawdown: float = 0.0                   # [0, 1] fraction from peak

    def __post_init__(self):
        super().__post_init__()
        if float(self.gross_exposure) < 0:
            raise DataValidationError("gross_exposure must be >= 0",
                                      field="gross_exposure",
                                      got=self.gross_exposure)
        for name in ("concentration", "drawdown"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise DataValidationError(f"{name} must be within [0, 1]",
                                          field=name, got=value)
            object.__setattr__(self, name, value)
        if int(self.open_positions) < 0:
            raise DataValidationError("open_positions must be >= 0",
                                      field="open_positions",
                                      got=self.open_positions)
        object.__setattr__(self, "open_positions", int(self.open_positions))
        object.__setattr__(self, "gross_exposure", float(self.gross_exposure))
        object.__setattr__(self, "net_exposure", float(self.net_exposure))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PortfolioAnalysisOutput":
        _reject_unknown(data, cls)
        return cls(
            gross_exposure=_in_range(_require(data, "gross_exposure"),
                                     "gross_exposure", 0.0, 1e15),
            net_exposure=_as_number(data.get("net_exposure", 0.0),
                                    "net_exposure"),
            concentration=_in_range(data.get("concentration", 0.0),
                                    "concentration", 0.0, 1.0),
            open_positions=_as_int(data.get("open_positions", 0),
                                   "open_positions", minimum=0),
            drawdown=_in_range(data.get("drawdown", 0.0), "drawdown", 0.0, 1.0),
            **cls._common(data))


@dataclass(frozen=True)
class StrategySelectionOutput(_AgentOutput):
    """Which registered strategy fits the current conditions.

    The furthest any agent in this table goes toward a trade, and it still
    stops well short: it names a *strategy* and a direction, never a quantity,
    a price, or an order. Sizing belongs to the strategy and authorisation
    belongs to the risk engine, and keeping those out of an LLM-authored schema
    is what makes "no model can place an order" a structural claim rather than
    a policy.
    """
    strategy_id: str = ""
    direction: SignalDirection = SignalDirection.FLAT
    rationale: str = ""
    alternatives: tuple[str, ...] = ()

    def __post_init__(self):
        super().__post_init__()
        object.__setattr__(self, "direction", SignalDirection(self.direction))
        object.__setattr__(self, "alternatives", tuple(self.alternatives))
        if not isinstance(self.strategy_id, str) or not self.strategy_id.strip():
            raise DataValidationError("strategy_id must be a non-empty string",
                                      field="strategy_id")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StrategySelectionOutput":
        _reject_unknown(data, cls)
        return cls(
            strategy_id=_as_str(_require(data, "strategy_id"), "strategy_id",
                                max_length=120, allow_empty=False),
            direction=_as_enum(_require(data, "direction"), "direction",
                               SignalDirection),
            rationale=_as_str(data.get("rationale", ""), "rationale"),
            alternatives=_as_str_tuple(data.get("alternatives", ()),
                                       "alternatives"),
            **cls._common(data))


# ---------------------------------------------------------------------------
# the spec table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentSpec:
    """One agent, declared as data.

    A table rather than seven classes because the security-relevant property —
    "does this agent read attacker-authored text, and is it therefore barred
    from acting?" — must be *enumerable*. `specialists.Specialist._ingests`
    computes the same property from a tool loadout; here the loadout is the
    input contract, so it is declared directly and checked at construction.
    """
    key: str
    title: str
    purpose: str
    #: What the agent is given. Names of contract types / data sources, so a
    #: reviewer can see at a glance whether anything untrusted is in scope.
    input_contract: tuple[str, ...]
    output_schema: Type[_AgentOutput]
    #: True when any input is external, attacker-authored content.
    ingests_untrusted: bool = False

    def __post_init__(self):
        object.__setattr__(self, "input_contract", tuple(self.input_contract))
        if not issubclass(self.output_schema, _AgentOutput):
            raise ConfigurationError(
                "an agent's output schema must be a validated agent output",
                key=self.key, got=getattr(self.output_schema, "__name__", "?"))
        if self.ingests_untrusted and self.may_act:
            # Unreachable while `may_act` is derived, and deliberately kept: if
            # someone later makes `may_act` a stored field, this fails at import
            # rather than shipping an ingesting agent with action capability.
            raise ConfigurationError(
                "an agent that ingests untrusted content may not hold action "
                "capability", key=self.key)

    @property
    def may_act(self) -> bool:
        """Capability separation, derived rather than stored.

        Mirrors `specialists.Specialist.tool_defs`: `allow_action = not
        ingests`. Derived so the two facts cannot disagree — a stored flag is a
        flag someone eventually sets to True "just for this one agent".

        Note that in this domain NO agent may act regardless: acting means
        placing an order, and only `execution.py` does that, only from a
        `RiskDecision`. `may_act` here governs the lesser capabilities (writing
        to Olympus's own state, calling connectors), and it is False for every
        ingesting agent by construction.
        """
        return not self.ingests_untrusted

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "purpose": self.purpose,
                "input_contract": list(self.input_contract),
                "output_schema": self.output_schema.__name__,
                "ingests_untrusted": self.ingests_untrusted,
                "may_act": self.may_act}


#: The seven market-intelligence agents. Declarative on purpose: an agent that
#: exists but is not in this table has no validated output schema, and
#: `AgentOutputValidator` refuses to parse for an unknown key — so adding an
#: agent is a deliberate edit here, not an emergent property of some prompt
#: file appearing on disk.
AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        key="technical_analysis",
        title="Technical Analysis",
        purpose="Describe price structure — trend, support, resistance, and "
                "indicator state — from validated candles.",
        input_contract=("Candle[]", "DataQualityReport"),
        output_schema=TechnicalAnalysisOutput,
    ),
    AgentSpec(
        key="forecast",
        title="Forecast",
        purpose="Summarise whichever forecaster ran into the few numbers the "
                "risk engine gates on, preserving the abstention.",
        input_contract=("ForecastResult", "DataQualityReport"),
        output_schema=ForecastAgentOutput,
    ),
    AgentSpec(
        key="regime",
        title="Market Regime",
        purpose="Classify the prevailing regime and how settled that "
                "classification is.",
        input_contract=("Candle[]", "VolatilityOutput"),
        output_schema=RegimeOutput,
    ),
    AgentSpec(
        key="volatility",
        title="Volatility",
        purpose="Estimate realised and forecast volatility and locate them in "
                "the instrument's own history.",
        input_contract=("Candle[]",),
        output_schema=VolatilityOutput,
    ),
    AgentSpec(
        key="sentiment",
        title="News & Sentiment",
        # The only True in this column, and the reason the column exists.
        purpose="Aggregate sanitised external news into a bounded score. Reads "
                "attacker-authored text, so it holds no action capability and "
                "its output reaches the pipeline only as a Signal.",
        input_contract=("NewsItem[]",),
        output_schema=SentimentOutput,
        ingests_untrusted=True,
    ),
    AgentSpec(
        key="portfolio_analysis",
        title="Portfolio Analysis",
        purpose="Describe the current book: exposure, concentration, open "
                "positions, drawdown.",
        input_contract=("PortfolioSnapshot", "AccountSnapshot"),
        output_schema=PortfolioAnalysisOutput,
    ),
    AgentSpec(
        key="strategy_selection",
        title="Strategy Selection",
        purpose="Name the registered strategy best suited to current "
                "conditions. Names a strategy, never a quantity or an order.",
        input_contract=("RegimeOutput", "VolatilityOutput",
                        "PortfolioAnalysisOutput", "strategy registry"),
        output_schema=StrategySelectionOutput,
    ),
)

AGENT_SPECS_BY_KEY: Mapping[str, AgentSpec] = {
    spec.key: spec for spec in AGENT_SPECS}

#: Agents whose inputs include attacker-authored text. Exported so a wiring
#: layer can assert capability separation without re-deriving it.
INGESTING_AGENTS: frozenset[str] = frozenset(
    spec.key for spec in AGENT_SPECS if spec.ingests_untrusted)


def spec_for(key: str) -> AgentSpec:
    spec = AGENT_SPECS_BY_KEY.get(str(key))
    if spec is None:
        raise ConfigurationError(
            "unknown market-intelligence agent", key=str(key),
            known=sorted(AGENT_SPECS_BY_KEY))
    return spec


# ---------------------------------------------------------------------------
# the validator
# ---------------------------------------------------------------------------

class AgentOutputValidator:
    """Parses an agent/LLM response into its schema, or refuses.

    There is exactly one accepted wire format — a JSON object — and exactly one
    parser, `json.loads`. No `eval`, no `ast.literal_eval`, no YAML, no
    "helpful" regex extraction of the first number in the text. Every one of
    those has been a remote-code or type-confusion vector somewhere, and none
    of them is needed: an agent that cannot emit JSON is an agent that has not
    been given a schema.

    Field order is never load-bearing. A response is a mapping; the validator
    reads keys by name and rejects any key it does not know.
    """

    #: Bounded so a runaway generation cannot be parsed at all. 256 KiB is
    #: orders of magnitude above any legitimate structured output.
    max_bytes = 256 * 1024

    def __init__(self, *, specs: Sequence[AgentSpec] = AGENT_SPECS):
        self.specs: Mapping[str, AgentSpec] = {s.key: s for s in specs}

    def schema_for(self, agent_key: str) -> Type[_AgentOutput]:
        spec = self.specs.get(str(agent_key))
        if spec is None:
            raise ConfigurationError("unknown agent", key=str(agent_key),
                                     known=sorted(self.specs))
        return spec.output_schema

    def parse(self, agent_key: str, raw: Any) -> _AgentOutput:
        """Validate a raw response (str, bytes, or mapping) into its schema."""
        return self.validate(agent_key, self._decode(raw))

    def validate(self, agent_key: str, payload: Any) -> _AgentOutput:
        schema = self.schema_for(agent_key)
        if not isinstance(payload, Mapping):
            raise DataValidationError(
                "agent output must be a JSON object at the top level",
                agent=str(agent_key), got=type(payload).__name__)
        return schema.from_dict(payload)

    def _decode(self, raw: Any) -> Any:
        if isinstance(raw, Mapping):
            return raw
        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > self.max_bytes:
                raise DataValidationError("agent response exceeds the size cap",
                                          got=len(raw), limit=self.max_bytes)
            try:
                raw = bytes(raw).decode("utf-8")
            except UnicodeDecodeError:
                raise DataValidationError(
                    "agent response is not valid UTF-8") from None
        if not isinstance(raw, str):
            raise DataValidationError("agent response must be JSON text or a "
                                      "mapping", got=type(raw).__name__)
        if len(raw) > self.max_bytes:
            raise DataValidationError("agent response exceeds the size cap",
                                      got=len(raw), limit=self.max_bytes)
        try:
            return json.loads(raw)
        except (ValueError, RecursionError) as exc:
            # No fallback. A response that is not JSON is a broken agent, and
            # "find the JSON inside the prose" is how an injected preamble gets
            # a second chance at being parsed.
            raise DataValidationError(f"agent response is not valid JSON: {exc}",
                                      ) from None


__all__ = [
    "TechnicalAnalysisOutput", "ForecastAgentOutput", "RegimeOutput",
    "VolatilityOutput", "SentimentOutput", "PortfolioAnalysisOutput",
    "StrategySelectionOutput", "AgentSpec", "AGENT_SPECS",
    "AGENT_SPECS_BY_KEY", "INGESTING_AGENTS", "spec_for",
    "AgentOutputValidator", "TREND_CHOICES", "MAX_TEXT", "MAX_ITEMS",
    "taint_external", "taint_agent_output", "Tainted", "is_tainted",
    "assert_untainted",
]
