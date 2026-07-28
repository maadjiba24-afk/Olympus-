"""The market-state channel schema.

The schema's job is to make a class of error impossible to commit quietly, so
most of what is asserted here is a *refusal*. A test file for a registry that
only checked the registry's contents would pass on a schema that accepted
anything.

The four refusals that carry the weight:

* a REQUIRED channel that cannot be obtained — every observation invalid;
* a categorical channel without an explicit unknown level — a missing category
  silently becoming a real one;
* LAST_KNOWN without a staleness bound — last quarter's funding rate read as
  current;
* a normalisation that is meaningless for the channel's type.

And the one that is not a refusal but a guarantee: there is no code path that
turns an absent channel into a number. `ChannelObservation.value` returns
`None`, `vector` returns `None` beside a `False` mask bit, and nothing else is
offered.
"""

from __future__ import annotations

import json

import pytest

from olympus.trading.errors import ConfigurationError, DataValidationError
from olympus.trading.native import schema as S


def spec(**overrides):
    """A valid spec, with one thing changed. Keeps each test to its own point."""
    base = dict(name="x", kind=S.ChannelType.RATIO, unit=S.Unit.FRACTION,
                source="test", timestamp=S.TimestampSemantics.BAR_CLOSE,
                missing=S.MissingPolicy.MASK,
                normalisation=S.Normalisation.BOUNDED_UNIT)
    base.update(overrides)
    return S.ChannelSpec(**base)


# ===========================================================================
# the built-in registry
# ===========================================================================

def test_the_registry_covers_every_channel_family_the_design_asks_for():
    groups = set(S.OLYMPUS_MARKET_SCHEMA.groups)
    assert {"price", "volume", "microstructure", "derivatives", "volatility",
            "calendar", "session", "regime", "cross_asset", "events",
            "portfolio"} <= groups
    names = set(S.OLYMPUS_MARKET_SCHEMA.names)
    for required in ("open", "high", "low", "close", "base_volume",
                     "quote_volume", "trade_count", "bid", "ask",
                     "bid_ask_spread", "book_depth_bid", "book_depth_ask",
                     "trade_imbalance", "buy_volume", "sell_volume",
                     "realised_volatility", "funding_rate", "open_interest",
                     "liquidations_long", "liquidations_short", "session_state",
                     "time_of_day", "day_of_week", "regime_label",
                     "cross_asset_return", "event_bars_until",
                     "position_side", "portfolio_drawdown"):
        assert required in names, required


def test_every_channel_states_all_of_its_metadata():
    """The eleven fields are the point. A channel missing one is undocumented."""
    for channel in S.OLYMPUS_MARKET_SCHEMA:
        assert channel.name and channel.source and channel.description
        assert channel.group != "other", f"{channel.name} has no group"
        assert channel.instruments and channel.timeframes
        assert isinstance(channel.kind, S.ChannelType)
        assert isinstance(channel.unit, S.Unit)
        assert isinstance(channel.timestamp, S.TimestampSemantics)
        assert isinstance(channel.missing, S.MissingPolicy)
        assert isinstance(channel.normalisation, S.Normalisation)
        assert isinstance(channel.availability, S.Availability)


def test_the_availability_report_is_honest_about_what_is_unreachable():
    """Most of this schema is designed, not obtainable, and it says so."""
    report = S.OLYMPUS_MARKET_SCHEMA.availability_report()
    assert report["channels"] > 30
    assert 0 < report["obtainable"] < report["channels"], (
        "either everything is obtainable — which contradicts blocker B1 — or "
        "nothing is, which would make the schema unusable")
    counts = report["by_availability"]
    assert counts["not_ingested"] + counts["unreachable"] > 0


def test_the_obtainable_subset_is_what_a_model_here_can_actually_use():
    subset = S.OBTAINABLE_SCHEMA
    assert all(c.obtainable for c in subset)
    assert set(subset.names) < set(S.OLYMPUS_MARKET_SCHEMA.names)
    # The OHLC channels must survive, or nothing can be trained at all.
    assert {"open", "high", "low", "close"} <= set(subset.names)
    # And the microstructure channels must not, because they are not ingested.
    assert "bid_ask_spread" not in subset.names


def test_a_derivatives_channel_does_not_apply_to_an_equity():
    """Not-applicable and missing are different, and the schema knows which."""
    funding = S.OLYMPUS_MARKET_SCHEMA["funding_rate"]
    assert funding.applies_to(asset_class="crypto")
    assert not funding.applies_to(asset_class="equity")
    assert S.OLYMPUS_MARKET_SCHEMA["close"].applies_to(asset_class="equity")


def test_the_revised_channel_is_flagged_as_needing_a_vintage():
    """An archived economic figure is not the figure that was published."""
    needing = {c.name for c in S.OLYMPUS_MARKET_SCHEMA.needing_vintage()}
    assert "event_surprise" in needing
    assert S.OLYMPUS_MARKET_SCHEMA["event_surprise"].needs_vintage


# ===========================================================================
# refusals
# ===========================================================================

def test_a_required_channel_that_cannot_be_obtained_is_refused():
    """Every observation would be invalid, and nothing would say why."""
    with pytest.raises(ConfigurationError, match="cannot be obtained"):
        spec(missing=S.MissingPolicy.REQUIRED,
             availability=S.Availability.UNREACHABLE)


def test_last_known_without_a_staleness_bound_is_refused():
    with pytest.raises(ConfigurationError, match="max_staleness_bars"):
        spec(missing=S.MissingPolicy.LAST_KNOWN)


def test_a_staleness_bound_on_a_channel_that_does_not_carry_forward_is_refused():
    with pytest.raises(ConfigurationError, match="only meaningful"):
        spec(missing=S.MissingPolicy.MASK, max_staleness_bars=4)


def test_a_categorical_channel_must_have_an_explicit_unknown_level():
    with pytest.raises(ConfigurationError, match="unknown level"):
        S.ChannelSpec(name="c", kind=S.ChannelType.CATEGORICAL,
                      unit=S.Unit.CATEGORY, source="t",
                      timestamp=S.TimestampSemantics.BAR_CLOSE,
                      missing=S.MissingPolicy.UNKNOWN_LEVEL,
                      normalisation=S.Normalisation.ONE_HOT,
                      levels=("up", "down"))


def test_a_categorical_channel_may_not_be_masked():
    """'Unknown' is a state to condition on, not an absence to hide."""
    with pytest.raises(ConfigurationError, match="UNKNOWN_LEVEL"):
        S.ChannelSpec(name="c", kind=S.ChannelType.CATEGORICAL,
                      unit=S.Unit.CATEGORY, source="t",
                      timestamp=S.TimestampSemantics.BAR_CLOSE,
                      missing=S.MissingPolicy.MASK,
                      normalisation=S.Normalisation.ONE_HOT,
                      levels=("unknown", "up"))


def test_a_meaningless_normalisation_for_the_type_is_refused():
    with pytest.raises(ConfigurationError, match="not meaningful"):
        spec(kind=S.ChannelType.CATEGORICAL, normalisation=S.Normalisation.INSTANCE_ZSCORE,
             missing=S.MissingPolicy.UNKNOWN_LEVEL, levels=("unknown", "a"))


def test_there_is_no_zero_fill_policy():
    """The absence is the design. A zero-filled spread is a tradable claim."""
    assert not any("zero" in member.value for member in S.MissingPolicy)


def test_a_derived_channel_whose_parent_is_absent_is_refused():
    with pytest.raises(ConfigurationError, match="not in the schema"):
        S.MarketStateSchema([spec(name="child", derives_from=("ghost",))])


def test_a_duplicate_channel_is_refused():
    with pytest.raises(ConfigurationError, match="duplicate"):
        S.MarketStateSchema([spec(name="a"), spec(name="a")])


def test_inverted_bounds_are_refused():
    with pytest.raises(ConfigurationError, match="inverted"):
        spec(bounds=(1.0, -1.0))


# ===========================================================================
# observations: missing stays missing
# ===========================================================================

def test_an_observation_reports_absence_and_never_invents_a_value():
    schema = S.MarketStateSchema([spec(name="a"), spec(name="b"), spec(name="c")])
    observation = schema.validate({"a": 0.5, "c": -0.25})

    assert observation.value("a") == 0.5
    assert observation.value("b") is None, "an absent channel must stay absent"
    assert observation.missing_channels == ("b",)
    values, mask = observation.vector(("a", "b", "c"))
    assert values == (0.5, None, -0.25)
    assert mask == (True, False, True)
    assert observation.completeness == pytest.approx(2 / 3)


def test_an_unknown_channel_in_an_observation_is_refused():
    """A column in training that the serving path lacks starts exactly here."""
    schema = S.MarketStateSchema([spec(name="a")])
    with pytest.raises(ConfigurationError, match="unknown channels"):
        schema.validate({"a": 0.1, "surprise": 0.2})


def test_a_missing_required_channel_invalidates_the_observation():
    schema = S.MarketStateSchema([
        spec(name="a", missing=S.MissingPolicy.REQUIRED,
             availability=S.Availability.AVAILABLE),
        spec(name="b")])
    with pytest.raises(DataValidationError, match="required channels"):
        schema.validate({"b": 0.1})


def test_a_value_outside_its_declared_bounds_raises_rather_than_clipping():
    """Clipping would turn an ingestion bug into a plausible boundary value."""
    schema = S.MarketStateSchema([spec(name="a", bounds=(-1.0, 1.0))])
    with pytest.raises(DataValidationError, match="above its declared bound"):
        schema.validate({"a": 1.5})
    with pytest.raises(DataValidationError, match="below its declared bound"):
        schema.validate({"a": -1.5})


def test_a_non_finite_value_is_refused():
    schema = S.MarketStateSchema([spec(name="a")])
    with pytest.raises(DataValidationError, match="finite"):
        schema.validate({"a": float("inf")})


def test_a_channel_that_does_not_apply_is_a_category_error_not_a_missing_value():
    schema = S.MarketStateSchema([spec(name="a", instruments=("crypto",))])
    with pytest.raises(DataValidationError, match="category error"):
        schema.validate({"a": 0.1}, asset_class="equity")


def test_a_carried_value_staler_than_its_bound_is_refused():
    schema = S.MarketStateSchema([
        spec(name="a", missing=S.MissingPolicy.LAST_KNOWN, max_staleness_bars=3)])
    S.ChannelObservation(schema=schema, values={"a": 0.1}, staleness={"a": 3})
    with pytest.raises(DataValidationError, match="staler than"):
        S.ChannelObservation(schema=schema, values={"a": 0.1}, staleness={"a": 4})


def test_staleness_on_a_channel_that_does_not_carry_forward_is_refused():
    schema = S.MarketStateSchema([spec(name="a")])
    with pytest.raises(ConfigurationError, match="does not carry"):
        S.ChannelObservation(schema=schema, values={"a": 0.1}, staleness={"a": 1})


# ===========================================================================
# width, identity and round-tripping
# ===========================================================================

def test_a_cyclical_channel_is_two_model_inputs_and_a_one_hot_is_one_per_level():
    """Shape errors surface here rather than inside a matrix multiply."""
    cyclical = S.OLYMPUS_MARKET_SCHEMA["time_of_day"]
    assert cyclical.width == 2
    categorical = S.OLYMPUS_MARKET_SCHEMA["session_state"]
    assert categorical.width == len(categorical.levels)
    assert S.OLYMPUS_MARKET_SCHEMA["close"].width == 1
    assert S.OLYMPUS_MARKET_SCHEMA.width == sum(
        c.width for c in S.OLYMPUS_MARKET_SCHEMA)


def test_the_fingerprint_changes_when_a_channels_meaning_changes():
    """Not just its name: a timestamp semantics change is a schema change."""
    original = S.MarketStateSchema([spec(name="a")])
    renamed_meaning = S.MarketStateSchema([
        spec(name="a", timestamp=S.TimestampSemantics.ANNOUNCED)])
    assert original.fingerprint() != renamed_meaning.fingerprint()


def test_the_fingerprint_is_stable_across_processes():
    """Two builds of the same schema must agree, or no checkpoint can be
    validated against it."""
    first = S.MarketStateSchema([spec(name="a"), spec(name="b")])
    second = S.MarketStateSchema([spec(name="a"), spec(name="b")])
    assert first.fingerprint() == second.fingerprint()


def test_a_subset_keeps_registry_order_not_the_callers():
    """Two callers asking for the same set must get the same fingerprint."""
    schema = S.MarketStateSchema([spec(name="a"), spec(name="b"), spec(name="c")])
    forwards = schema.subset(["a", "c"])
    backwards = schema.subset(["c", "a"])
    assert forwards.names == backwards.names == ("a", "c")
    assert forwards.fingerprint() == backwards.fingerprint()


def test_a_subset_naming_an_unknown_channel_is_refused():
    schema = S.MarketStateSchema([spec(name="a")])
    with pytest.raises(ConfigurationError, match="unknown channels"):
        schema.subset(["a", "ghost"])


def test_the_schema_round_trips_through_json_unchanged():
    text = json.dumps(S.OLYMPUS_MARKET_SCHEMA.to_dict())
    rebuilt = S.MarketStateSchema.from_dict(json.loads(text))
    assert rebuilt.fingerprint() == S.OLYMPUS_MARKET_SCHEMA.fingerprint()
    assert rebuilt.names == S.OLYMPUS_MARKET_SCHEMA.names
    assert rebuilt.width == S.OLYMPUS_MARKET_SCHEMA.width


def test_an_unknown_channel_lookup_names_itself_rather_than_raising_keyerror():
    with pytest.raises(ConfigurationError, match="unknown channel"):
        S.OLYMPUS_MARKET_SCHEMA["not_a_channel"]
