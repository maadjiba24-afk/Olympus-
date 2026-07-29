"""Datasets, provenance, alignment and leakage.

This file is the one that matters most for anything trained afterwards. A model
built on a leaking dataset produces excellent numbers and no information, and
the failure is invisible in every metric a model reports about itself — so the
checks have to live here, on the data, before a model exists to be flattered.

Each of the five failures `dataset.py` exists to prevent has a test that
*constructs* it rather than describing it: a universe whose member joined late,
a split applied after a corporate action, a daily bar paired with the hour
inside it, a gap, and a scaler asked to fit across the boundary. A test that
only checked the happy path would pass against a module that had none of these
defences.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.contracts import Candle
from olympus.trading.errors import (ConfigurationError, DataValidationError,
                                    InsufficientDataError)
from olympus.trading.native import dataset as D
from olympus.trading.native.data import DatasetSpec, make_windows

START = datetime(2027, 3, 1, tzinfo=timezone.utc)
KEY = "SIM:D"


def bars(n: int, *, key: str = KEY, timeframe: str = "1h", start: int = 0,
         step: timedelta = timedelta(hours=1), base: float = 100.0,
         drift: float = 0.05) -> list[Candle]:
    out = []
    for i in range(n):
        price = base + drift * (start + i)
        opened = START + step * (start + i)
        out.append(Candle(instrument_key=key, timeframe=timeframe,
                          ts_open=opened, ts_close=opened + step,
                          open=price, high=price * 1.004, low=price * 0.996,
                          close=price, volume=1000.0 + i))
    return out


SPEC = DatasetSpec(instrument_keys=(KEY,), timeframe="1h", lookback=10, horizon=3)


def source_for(series) -> D.SourceRecord:
    return D.SourceRecord(provider="sim", endpoint="memory://",
                          instrument_keys=(KEY,), timeframe="1h",
                          first_ts=series[0].ts_open, last_ts=series[-1].ts_close,
                          bars=len(series), licence="internal", adjusted="false")


def universe_for(*keys) -> D.Universe:
    return D.Universe("u", [D.MembershipInterval(instrument_key=k, since=START)
                            for k in (keys or (KEY,))])


# ===========================================================================
# provenance
# ===========================================================================

def test_a_source_distinguishes_unknown_adjustment_from_unadjusted():
    """A boolean here would collapse 'the provider says no' and 'nobody asked'."""
    series = bars(5)
    assert source_for(series).adjusted == "false"
    with pytest.raises(ConfigurationError, match="'unknown'"):
        D.SourceRecord(provider="p", endpoint="", instrument_keys=(KEY,),
                       timeframe="1h", first_ts=START, last_ts=START,
                       bars=0, adjusted="maybe")


def test_a_source_with_an_inverted_interval_is_refused():
    with pytest.raises(ConfigurationError, match="inverted"):
        D.SourceRecord(provider="p", endpoint="", instrument_keys=(KEY,),
                       timeframe="1h", first_ts=START + timedelta(days=1),
                       last_ts=START, bars=1)


def test_a_manifest_reports_what_it_cannot_tell_you():
    """Missing provenance is a field, not an absence of one."""
    series = bars(200)
    built = D.build_dataset(series, SPEC, dataset_id="d", created_at=START)
    assert "no source records: provenance is unknown" in built.manifest.gaps
    assert "no universe: survivorship cannot be ruled out" in built.manifest.gaps
    assert not built.manifest.clean

    complete = D.build_dataset(series, SPEC, dataset_id="d", created_at=START,
                               sources=[source_for(series)],
                               universe=universe_for())
    assert complete.manifest.gaps == ()
    assert complete.manifest.clean


def test_an_unknown_licence_is_reported_as_a_gap():
    series = bars(200)
    unlicensed = D.SourceRecord(provider="sim", endpoint="", instrument_keys=(KEY,),
                                timeframe="1h", first_ts=series[0].ts_open,
                                last_ts=series[-1].ts_close, bars=len(series),
                                adjusted="false")
    built = D.build_dataset(series, SPEC, dataset_id="d", created_at=START,
                            sources=[unlicensed], universe=universe_for())
    assert any("licence" in gap for gap in built.manifest.gaps)


# ===========================================================================
# survivorship
# ===========================================================================

def test_a_bar_from_before_an_instrument_joined_the_universe_is_dropped():
    """The mechanical survivorship defence, on a constructed violation."""
    early = bars(50)
    joined_late = D.Universe("u", [D.MembershipInterval(
        instrument_key=KEY, since=START + timedelta(hours=20))])
    kept = joined_late.filter_bars(early)
    # Membership is judged on the bar's *close* — the instant it was knowable —
    # so the bar closing exactly at the join date is the first member.
    assert len(kept) == 31, "bars before the join date must not survive"
    assert all(b.ts_close >= START + timedelta(hours=20) for b in kept)
    assert min(b.ts_close for b in kept) == START + timedelta(hours=20)


def test_membership_is_a_function_of_time_not_of_today():
    universe = D.Universe("u", [
        D.MembershipInterval(instrument_key="A", since=START,
                             until=START + timedelta(days=10)),
        D.MembershipInterval(instrument_key="B",
                             since=START + timedelta(days=5))])
    assert universe.members_at(START + timedelta(days=1)) == ("A",)
    assert universe.members_at(START + timedelta(days=7)) == ("A", "B")
    assert universe.members_at(START + timedelta(days=20)) == ("B",)
    # The attribute that would produce the survivorship bug is named to be
    # unattractive, and it still exists — for reporting, not for building.
    assert universe.all_members == ("A", "B")


def test_overlapping_membership_intervals_are_refused():
    universe = D.Universe("u", [D.MembershipInterval(instrument_key="A",
                                                     since=START)])
    with pytest.raises(ConfigurationError, match="[Oo]verlapping"):
        universe.add(D.MembershipInterval(instrument_key="A",
                                          since=START + timedelta(days=1)))


def test_a_universe_that_excludes_everything_fails_loudly():
    series = bars(200)
    future = D.Universe("u", [D.MembershipInterval(
        instrument_key=KEY, since=START + timedelta(days=400))])
    with pytest.raises(InsufficientDataError, match="universe filter"):
        D.build_dataset(series, SPEC, dataset_id="d", created_at=START,
                        universe=future)


# ===========================================================================
# corporate actions
# ===========================================================================

def test_an_action_that_has_not_happened_yet_does_not_restate_history():
    """The whole difference between a backtest and a restated one."""
    series = bars(40)
    split = D.CorporateAction(instrument_key=KEY, action=D.ActionType.SPLIT,
                              ex_date=START + timedelta(hours=30), value=2.0)

    before = D.adjust_for_actions(series, [split],
                                  as_of=START + timedelta(hours=20))
    assert [b.close for b in before] == [b.close for b in series], (
        "an action with a future ex-date must leave the series untouched")

    after = D.adjust_for_actions(series, [split],
                                 as_of=START + timedelta(hours=35))
    assert after[0].close == pytest.approx(series[0].close / 2.0)
    assert after[0].volume == pytest.approx(series[0].volume * 2.0)
    # Bars on or after the ex-date are already in the new units.
    assert after[35].close == pytest.approx(series[35].close)


def test_actions_without_an_adjustment_instant_are_refused():
    series = bars(200)
    split = D.CorporateAction(instrument_key=KEY, action=D.ActionType.SPLIT,
                              ex_date=START + timedelta(hours=30), value=2.0)
    with pytest.raises(ConfigurationError, match="adjustment_as_of"):
        D.build_dataset(series, SPEC, dataset_id="d", created_at=START,
                        actions=[split])


def test_an_action_announced_after_its_ex_date_is_refused():
    with pytest.raises(ConfigurationError, match="announced after"):
        D.CorporateAction(instrument_key=KEY, action=D.ActionType.SPLIT,
                          ex_date=START, value=2.0,
                          announced_at=START + timedelta(days=1))


def test_an_adjustment_that_would_produce_a_non_positive_price_raises():
    series = bars(10, base=1.0, drift=0.0)
    dividend = D.CorporateAction(instrument_key=KEY,
                                 action=D.ActionType.CASH_DIVIDEND,
                                 ex_date=START + timedelta(hours=5), value=5.0)
    with pytest.raises(DataValidationError, match="non-positive price"):
        D.adjust_for_actions(series, [dividend],
                             as_of=START + timedelta(hours=6))


# ===========================================================================
# quality: gaps, duplicates, ordering
# ===========================================================================

def test_a_gap_is_detected_and_never_filled():
    series = bars(10) + bars(10, start=15)
    gaps = D.detect_gaps(series)
    assert len(gaps) == 1
    assert gaps[0].missing_bars == 5
    # Nothing was inserted. A forward-filled bar carries a real close price.
    assert len(series) == 20


def test_conflicting_duplicates_are_distinguished_from_repeated_rows():
    series = bars(5)
    repeated = series + [series[0]]
    assert D.detect_duplicates(repeated)[0].conflicting is False

    disagreeing = series + [Candle(
        instrument_key=KEY, timeframe="1h", ts_open=series[0].ts_open,
        ts_close=series[0].ts_close, open=1.0, high=1.1, low=0.9, close=1.05,
        volume=1.0)]
    assert D.detect_duplicates(disagreeing)[0].conflicting is True


def test_a_dataset_with_conflicting_duplicates_is_refused_but_gaps_are_tolerated():
    """A market with a weekend has gaps and is still a market."""
    gapped = bars(120) + bars(120, start=130)
    built = D.build_dataset(gapped, SPEC, dataset_id="d", created_at=START,
                            sources=[source_for(gapped)], universe=universe_for())
    assert built.manifest.quality.missing_bars == 10
    assert built.manifest.quality.usable

    conflicted = bars(200)
    conflicted.append(Candle(instrument_key=KEY, timeframe="1h",
                             ts_open=conflicted[0].ts_open,
                             ts_close=conflicted[0].ts_close, open=1.0,
                             high=1.1, low=0.9, close=1.05, volume=1.0))
    with pytest.raises(DataValidationError, match="not usable"):
        D.build_dataset(conflicted, SPEC, dataset_id="d", created_at=START)


def test_a_non_final_bar_makes_a_dataset_unusable():
    series = bars(200)
    forming = series[-1]
    series[-1] = Candle(
        instrument_key=forming.instrument_key, timeframe=forming.timeframe,
        ts_open=forming.ts_open, ts_close=forming.ts_close, open=forming.open,
        high=forming.high, low=forming.low, close=forming.close,
        volume=forming.volume, is_final=False)
    report = D.quality_report(series)
    assert not report.usable
    assert any("non-final" in reason for reason in report.blocking_reasons)


def test_timestamps_are_checked_for_utc_independently_of_the_contract():
    """Two checks, because a naive datetime mis-orders silently against an
    aware one."""
    D.assert_utc(bars(3))


# ===========================================================================
# alignment
# ===========================================================================

def test_a_higher_timeframe_bar_is_paired_only_once_it_has_closed():
    """The multi-timeframe bug: pairing an hour with the day containing it."""
    hourly = bars(60)
    daily = bars(5, timeframe="1d", step=timedelta(days=1), base=50.0, drift=1.0)
    aligned = D.align_timeframes(hourly, {"1d": daily})

    # The first daily bar closes 24h in; nothing is old enough before that.
    assert aligned[0].context["1d"] is None
    assert aligned[0].missing() == ("1d",)
    assert aligned[0].complete is False

    # An hour after the first daily close, it is paired — with the bar that
    # *closed*, never the one still forming.
    paired = aligned[25]
    assert paired.context["1d"] is not None
    assert paired.context["1d"].ts_close <= paired.base.ts_close
    assert paired.staleness_seconds["1d"] > 0


def test_a_context_series_finer_than_the_base_is_refused():
    """Almost always a transposed argument, and it discards most of the data."""
    hourly = bars(30)
    daily = bars(5, timeframe="1d", step=timedelta(days=1))
    with pytest.raises(ConfigurationError, match="finer than"):
        D.align_timeframes(daily, {"1h": hourly})


def test_an_over_aged_context_bar_becomes_absent_rather_than_stale():
    hourly = bars(60)
    daily = bars(5, timeframe="1d", step=timedelta(days=1), base=50.0, drift=1.0)
    aligned = D.align_timeframes(hourly, {"1d": daily},
                                 max_staleness=timedelta(hours=2))
    stale = [row for row in aligned if row.context["1d"] is None]
    assert len(stale) > 20, "a bound that drops nothing is not a bound"
    fresh = [row for row in aligned if row.context["1d"] is not None]
    assert all(row.staleness_seconds["1d"] <= 7200 for row in fresh)


def test_cross_asset_alignment_uses_the_same_causal_rule():
    base = bars(40)
    reference = bars(40, key="SIM:REF", base=200.0, drift=0.1)
    aligned = D.align_cross_asset(base, {"SIM:REF": reference})
    for row in aligned:
        partner = row.context["SIM:REF"]
        if partner is not None:
            assert partner.ts_close <= row.base.ts_close


def test_a_stale_cross_asset_reference_yields_no_return_rather_than_zero():
    """A zero return from a stale reference is indistinguishable from a flat one."""
    base = bars(30)
    sparse = bars(3, key="SIM:REF", step=timedelta(hours=10), base=200.0, drift=1.0)
    aligned = D.align_cross_asset(base, {"SIM:REF": sparse})
    returns = D.cross_asset_returns(aligned, "SIM:REF")
    assert len(returns) == len(base)
    assert returns.count(None) > 0
    assert 0.0 not in [r for r in returns if r is not None], (
        "a repeated reference bar must produce None, not a zero return")


# ===========================================================================
# splits and leakage
# ===========================================================================

def test_the_three_way_split_embargoes_both_boundaries():
    windows = make_windows(bars(400), lookback=10, horizon=3)
    split = D.three_way_split(windows, train_fraction=0.6,
                              validation_fraction=0.2)
    train, validation, test = split.sizes
    assert train and validation and test
    assert split.embargoed > 0, "an embargo that drops nothing is not an embargo"

    last_train = max(w.targets[-1].ts_close for w in split.train)
    first_validation = min(w.inputs[0].ts_open for w in split.validation)
    assert first_validation >= last_train

    last_validation = max(w.targets[-1].ts_close for w in split.validation)
    first_test = min(w.inputs[0].ts_open for w in split.test)
    assert first_test >= last_validation


def test_fractions_that_leave_no_test_set_are_refused():
    windows = make_windows(bars(300), lookback=10, horizon=3)
    with pytest.raises(ConfigurationError, match="no test set"):
        D.three_way_split(windows, train_fraction=0.8, validation_fraction=0.3)


def test_walk_forward_folds_expand_and_each_is_embargoed():
    windows = make_windows(bars(500), lookback=10, horizon=3)
    folds = D.walk_forward_folds(windows, folds=4, min_train=60, anchored=True)
    assert len(folds) == 4
    assert [len(f.train) for f in folds] == sorted(len(f.train) for f in folds), (
        "an anchored walk-forward must not shrink its training set")
    for fold in folds:
        assert fold.embargoed > 0
        last_train = max(w.targets[-1].ts_close for w in fold.train)
        first_test = min(w.inputs[0].ts_open for w in fold.test)
        assert first_test >= last_train, f"fold {fold.index} leaks"


def test_a_rolling_walk_forward_keeps_its_training_set_the_same_size():
    windows = make_windows(bars(500), lookback=10, horizon=3)
    folds = D.walk_forward_folds(windows, folds=3, min_train=80, anchored=False)
    sizes = {len(f.train) for f in folds}
    assert len(sizes) == 1, f"a rolling window should be fixed-size, got {sizes}"


def test_the_leakage_audit_catches_a_split_the_splitter_would_have_allowed():
    """Independent of `temporal_split`, so it still works when that is the bug."""
    windows = make_windows(bars(200), lookback=10, horizon=3)
    # A deliberately naive split: adjacent windows, no embargo.
    findings = D.leakage_report(train=windows[:100], test=windows[100:])
    assert any(f.check == "embargo_violation" for f in findings)


def test_a_window_appearing_in_two_parts_is_caught():
    windows = make_windows(bars(200), lookback=10, horizon=3)
    findings = D.leakage_report(train=windows[:60], test=windows[50:])
    assert any(f.check == "window_overlap" for f in findings)


def test_a_correctly_embargoed_split_produces_no_findings():
    windows = make_windows(bars(400), lookback=10, horizon=3)
    split = D.three_way_split(windows)
    assert D.leakage_report(train=split.train, validation=split.validation,
                            test=split.test) == []
    D.assert_no_leakage(train=split.train, validation=split.validation,
                        test=split.test)


def test_assert_no_leakage_raises_on_a_leaking_split():
    windows = make_windows(bars(200), lookback=10, horizon=3)
    with pytest.raises(DataValidationError, match="leakage findings"):
        D.assert_no_leakage(train=windows[:100], test=windows[100:])


def test_a_revised_channel_is_only_flagged_when_it_is_obtainable():
    """A finding that is always present is a finding nobody reads."""
    from olympus.trading.native import schema as S
    windows = make_windows(bars(400), lookback=10, horizon=3)
    split = D.three_way_split(windows)
    findings = D.leakage_report(train=split.train, validation=split.validation,
                                test=split.test, schema=S.OLYMPUS_MARKET_SCHEMA)
    assert findings == [], "event_surprise is unreachable and cannot leak"


# ===========================================================================
# scaler policy
# ===========================================================================

def test_a_scaler_cannot_be_fitted_across_the_split_boundary():
    boundary = START + timedelta(hours=100)
    policy = D.ScalerPolicy(boundary=boundary)
    rows = [(START + timedelta(hours=i), {"a": float(i)}) for i in range(200)]

    assert len(policy.fitting_rows(rows)) == 101
    with pytest.raises(DataValidationError, match="after the training boundary"):
        policy.check(rows)
    policy.check(rows[:100])


# ===========================================================================
# manifests: determinism, hashing, round-trip
# ===========================================================================

def test_building_the_same_dataset_twice_produces_the_same_hashes():
    series = bars(300)
    first = D.build_dataset(series, SPEC, dataset_id="d", created_at=START,
                            sources=[source_for(series)], universe=universe_for())
    second = D.build_dataset(bars(300), SPEC, dataset_id="d", created_at=START,
                             sources=[source_for(series)], universe=universe_for())
    assert first.manifest.content_hash == second.manifest.content_hash
    assert first.manifest.manifest_hash() == second.manifest.manifest_hash()
    assert first.split.sizes == second.split.sizes


def test_changing_one_bar_changes_the_content_hash():
    series = bars(300)
    altered = list(series)
    altered[7] = Candle(instrument_key=KEY, timeframe="1h",
                        ts_open=series[7].ts_open, ts_close=series[7].ts_close,
                        open=series[7].open, high=series[7].high * 1.01,
                        low=series[7].low, close=series[7].close,
                        volume=series[7].volume)
    first = D.build_dataset(series, SPEC, dataset_id="d", created_at=START)
    second = D.build_dataset(altered, SPEC, dataset_id="d", created_at=START)
    assert first.manifest.content_hash != second.manifest.content_hash


def test_the_same_bars_under_a_different_universe_are_a_different_dataset():
    """The content hash agrees; the manifest hash must not."""
    series = bars(300)
    one = D.build_dataset(series, SPEC, dataset_id="d", created_at=START,
                          universe=universe_for())
    other_universe = D.Universe("v", [D.MembershipInterval(
        instrument_key=KEY, since=START)])
    two = D.build_dataset(series, SPEC, dataset_id="d", created_at=START,
                          universe=other_universe)
    assert one.manifest.content_hash == two.manifest.content_hash
    assert one.manifest.manifest_hash() != two.manifest.manifest_hash()


def test_a_manifest_round_trips_through_json():
    series = bars(300)
    built = D.build_dataset(series, SPEC, dataset_id="d", created_at=START,
                            sources=[source_for(series)], universe=universe_for(),
                            notes="round trip")
    text = json.dumps(built.manifest.to_dict())
    rebuilt = D.DatasetManifest.from_dict(json.loads(text))
    assert rebuilt.manifest_hash() == built.manifest.manifest_hash()
    assert rebuilt.quality.bars == built.manifest.quality.bars
    assert rebuilt.quality.missing_bars == built.manifest.quality.missing_bars
    assert rebuilt.sources[0].licence == "internal"
    assert rebuilt.notes == "round trip"


def test_a_manifest_without_a_content_hash_is_refused():
    with pytest.raises(ConfigurationError, match="content hash"):
        D.DatasetManifest(dataset_id="d", spec=SPEC, content_hash="",
                          schema_fingerprint="x", schema_name="s",
                          created_at=START)


def test_the_built_dataset_records_the_schema_it_was_built_against():
    from olympus.trading.native import schema as S
    series = bars(300)
    built = D.build_dataset(series, SPEC, dataset_id="d", created_at=START,
                            schema=S.OBTAINABLE_SCHEMA)
    assert built.manifest.schema_fingerprint == S.OBTAINABLE_SCHEMA.fingerprint()
    assert built.manifest.channels == S.OBTAINABLE_SCHEMA.names
