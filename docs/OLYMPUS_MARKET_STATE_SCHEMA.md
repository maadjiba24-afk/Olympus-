# Olympus Market-State Schema and Dataset Format

**What a native model may observe, what each observation means, and what makes a
dataset built from it defensible.**

- **Last updated:** 2026-07-28
- **Modules:** `olympus/trading/native/schema.py`, `olympus/trading/native/dataset.py`
- **Tests:** `tests/test_trading_native_schema.py` (31),
  `tests/test_trading_native_dataset.py` (38)
- **Companions:** `docs/OLYMPUS_NATIVE_MARKET_INTELLIGENCE.md` (architecture),
  `docs/OLYMPUS_NATIVE_REPRESENTATIONS.md` (encoders and baselines),
  `docs/OLYMPUS_NATIVE_MODEL_STATUS.md` (the ledger)

> **Most of this schema is not obtainable here.** 21 of 38 channels can be
> produced from this environment; the rest are designed, documented and blocked
> on B1. `OLYMPUS_MARKET_SCHEMA.availability_report()` is the authority, and it
> is printed in §3.

---

## 1. Why the metadata is the schema

A candle is five numbers. A market state is everything observable at an instant,
and the difficulty is not collecting it — it is knowing *what each number means*
well enough to avoid using it wrongly.

Every leakage defect this repository has found was a metadata defect wearing a
modelling costume:

| The defect | The metadata that would have caught it |
|---|---|
| A funding rate stamped with the bar it was announced *during* | `TimestampSemantics.ANNOUNCED` |
| A backfilled economic figure that was revised after publication | `TimestampSemantics.REVISED` |
| An absent bid-ask spread that became `0.0` | `MissingPolicy` — which has no zero-fill member |
| A scaler fitted over the test period | `Normalisation.TRAIN_ROBUST` + `dataset.ScalerPolicy` |
| A funding rate "missing" on an equity | `ChannelSpec.instruments` — it is a category error, not a gap |
| A stale open-interest print read as current | `MissingPolicy.LAST_KNOWN` + `max_staleness_bars` |

So a `ChannelSpec` records eleven things, and all eleven are required:

```
name · type · unit · source · timestamp semantics · missing-value policy ·
normalisation policy · supported instruments · supported timeframes ·
availability · description
```

plus, where they apply: bounds, categorical levels, staleness limit, and the
channels it derives from. `tests/test_trading_native_schema.py::
test_every_channel_states_all_of_its_metadata` asserts none is blank.

---

## 2. The five vocabularies

### 2.1 Timestamp semantics — the load-bearing field

Two channels with identical names, types and units but different semantics have
completely different leakage profiles.

| Member | Knowable at | The failure it prevents |
|---|---|---|
| `BAR_CLOSE` | `ts_close` | — (the default) |
| `SNAPSHOT_AT_CLOSE` | `ts_close`, instantaneously | Treating a touch reading as though it held over the whole bar |
| `INTERVAL_AGGREGATE` | `ts_close` | — |
| `KNOWN_IN_ADVANCE` | arbitrarily early | The only category that may legitimately reference a future instant |
| `ANNOUNCED` | its own announcement instant | Stamping a mid-bar release with the containing bar: up to one bar of look-ahead |
| `REVISED` | never, without a vintage | A backfilled series is not the series that was published. Days of look-ahead |
| `DERIVED_CAUSAL` | at the stamp, by Olympus's own code | Causality becomes ours to prove — `features.assert_causal` is the proof |

`schema.needing_vintage()` lists the `REVISED` channels. `dataset.leakage_report`
flags them **only when they are obtainable**: a channel Olympus cannot fetch is
not in the data and cannot leak from it, and a finding that is always present is
a finding nobody reads.

### 2.2 Missing-value policy — there is no zero-fill

| Member | Behaviour |
|---|---|
| `REQUIRED` | Absence invalidates the whole observation |
| `MASK` | Absence is recorded in the mask; the value is **undefined**, not defaulted |
| `LAST_KNOWN` | Carry forward, bounded by `max_staleness_bars`, staleness recorded |
| `UNKNOWN_LEVEL` | Categorical channels get an explicit unknown level |

There is no zero-fill member and there will not be one. A zero-filled bid-ask
spread is a real, plausible number describing a perfectly liquid market; a model
learns from it happily and the resulting error is invisible in every metric.

Two structural refusals follow, both enforced at construction:

- A `REQUIRED` channel that is `NOT_INGESTED` or `UNREACHABLE` — every
  observation would be invalid and nothing would say why.
- A categorical channel using `MASK` — "unknown regime" is itself a state worth
  conditioning on, and masking it discards that.

### 2.3 Normalisation policy

`NONE · LOG_RATIO_PREVIOUS · LOG_RATIO_CLOSE · INSTANCE_ZSCORE · TRAIN_ROBUST ·
SIGNED_LOG1P · BOUNDED_UNIT · CYCLICAL · ONE_HOT`

Two of these carry obligations the schema records so the pipeline can enforce
them rather than relying on a caller remembering:

- **`TRAIN_ROBUST`** means statistics fitted on the training split *only*.
  `schema.needing_train_statistics()` enumerates them and
  `dataset.ScalerPolicy` refuses to fit across the boundary.
- **`INSTANCE_ZSCORE`** means over the input window only. Causal because
  `data.Window` guarantees its targets close strictly after its inputs — a
  guarantee enforced on timestamps, not assumed.

A normalisation meaningless for the channel's type is refused at construction: a
one-hot encoded funding rate is a configuration error, not an eccentric choice.

### 2.4 Availability — the honest field

| Member | Meaning here |
|---|---|
| `AVAILABLE` | Present in the store now |
| `DERIVABLE` | Computable from what is present, by code Olympus owns |
| `NOT_INGESTED` | The upstream field exists; Olympus does not fetch it |
| `UNREACHABLE` | No provider is reachable from this environment (**B1**) |

### 2.5 Width

A channel expands to a known number of model inputs: `CYCLICAL` → 2 (sin, cos),
`ONE_HOT` → one per level, everything else → 1. `schema.width` is the total, so a
shape error surfaces at configuration time rather than inside a matrix multiply.

---

## 3. The registry — 38 channels, 21 obtainable

Produced by `OLYMPUS_MARKET_SCHEMA.availability_report()`.

| Group | Channels | Obtainable | Notes |
|---|---|---|---|
| price | 4 | 4 | `open`, `high`, `low`, `close` |
| volume | 3 | 2 | `trade_count` not ingested |
| microstructure | 9 | 0 | bid, ask, spread, depth ×2, imbalance ×2, buy/sell volume |
| derivatives | 4 | 0 | funding, open interest, liquidations ×2 |
| volatility | 2 | 2 | realised, Parkinson — derivable from bars |
| calendar | 3 | 3 | time of day, weekday, month end |
| session | 3 | 3 | state, minutes since open, minutes to close |
| regime | 2 | 2 | Olympus's own classifier — no licence question |
| cross_asset | 2 | 2 | reference return and trailing correlation |
| events | 3 | 0 | timing not ingested; `event_surprise` **unreachable** |
| portfolio | 3 | 3 | side, exposure, drawdown — Olympus owns these |
| **total** | **38** | **21** | model input width 57 full / 36 obtainable |

**What "obtainable" buys and what it costs.** The 21 reachable channels are
OHLCV, two derived volatility estimators, the calendar, the session, Olympus's
own regime labels, cross-asset context, and Olympus's own portfolio state.
Everything that would tell a model about *liquidity* — the book, the trade
imbalance, the spread — is absent. So is everything about *positioning* —
funding, open interest, liquidations. A forecasting model trained here is
trained on price history and Olympus's own internal state, and the
execution-cost head described in the architecture doc §3.8 has no inputs at all.

`OBTAINABLE_SCHEMA` is the subset, and it is what a model in this environment
may declare as its input surface.

---

## 4. Observations: missing stays missing

`ChannelObservation` holds what was observed and a mask saying, per applicable
channel, whether it was. The API offers exactly three ways to read a value and
none of them fabricates one:

```python
observation.value("bid")          # -> float | None
observation.mask(names)           # -> (True, False, ...)
observation.vector(names)         # -> ((0.5, None, -0.25), (True, False, True))
```

`vector` returns a pair rather than a pre-filled array so the caller must decide
what absence means for its own arithmetic. An encoder substitutes a *learned*
absent-embedding; a report prints a dash. Neither is the other's default.

Three states are kept distinct, and the distinction is the point:

- **observed** — in `values`, mask bit true;
- **missing** — applicable but absent, mask bit false, `value()` returns `None`;
- **not applicable** — a funding rate on an equity. Supplying it raises
  `DataValidationError` with "category error" rather than being recorded as a
  gap.

---

## 5. Dataset and provenance

### 5.1 The five failures `dataset.py` prevents

| Failure | Mechanism | Test |
|---|---|---|
| **Survivorship** | `Universe` stores membership as intervals; `filter_bars` drops a bar for an instrument that was not a member when it closed | `test_a_bar_from_before_an_instrument_joined_the_universe_is_dropped` |
| **Actions applied from the future** | `adjust_for_actions(..., as_of=)` applies only actions whose ex-date has arrived; `build_dataset` refuses actions without an `as_of` | `test_an_action_that_has_not_happened_yet_does_not_restate_history` |
| **Misaligned timeframes** | `align_causally` pairs each base bar with the newest context bar that closed **at or before** it, and reports staleness | `test_a_higher_timeframe_bar_is_paired_only_once_it_has_closed` |
| **Gaps read as flat markets** | `detect_gaps` finds them; nothing fills them | `test_a_gap_is_detected_and_never_filled` |
| **Statistics fitted across the split** | `ScalerPolicy` carries the boundary and refuses rows past it | `test_a_scaler_cannot_be_fitted_across_the_split_boundary` |

The alignment rule deserves its own line because the tempting alternative is
wrong: pairing a 1h bar with the daily bar that *contains* it uses a bar that has
not closed, whose high, low and close are still moving. `align_timeframes` also
refuses a context series *finer* than the base — almost always a transposed
argument, and it silently discards most of the finer series.

### 5.2 Splits

- **`three_way_split`** — train / validation / test, embargoed at both
  boundaries. Implemented as two applications of `temporal_split` so the embargo
  logic exists in exactly one place. Validation exists so model selection happens
  without touching test; a hyper-parameter chosen on test has consumed it.
- **`walk_forward_folds`** — anchored (default) or rolling. Every fold's boundary
  goes through the same embargo, so a fold cannot be where the defence is
  skipped.
- **`leakage_report`** — an audit *independent of the code that built the split*,
  because a check sharing a helper with the thing it checks fails together with
  it. It re-derives window overlap, part ordering and the horizon-inside-inputs
  invariant from the windows themselves.

### 5.3 Quality

`QualityReport` counts duplicates (distinguishing *conflicting* duplicates from
repeated identical rows — a different and worse problem), gaps, non-final bars,
out-of-order bars and mixed timeframes. `usable` is deliberately conservative
about the first three and deliberately tolerant of gaps: a market with a weekend
has gaps and is still a market.

---

## 6. The dataset manifest

```json
{
  "dataset_id": "...", "manifest_version": 1,
  "spec": { "instrument_keys": [...], "timeframe": "1h",
            "lookback": 10, "horizon": 3, "stride": 1,
            "train_fraction": 0.7, "embargo_bars": null },
  "content_hash": "<sha256 over every bar's OHLCV and timestamps>",
  "schema_fingerprint": "<sha256 over every channel's full metadata>",
  "schema_name": "olympus.market.v1",
  "created_at": "2027-...Z",
  "sources": [ { "provider": "...", "endpoint": "...", "instrument_keys": [...],
                 "timeframe": "1h", "first_ts": "...", "last_ts": "...",
                 "bars": 400, "retrieved_at": "...",
                 "licence": "...", "adjusted": "true|false|unknown",
                 "notes": "" } ],
  "quality": { "bars": 400, "duplicates": [...], "gaps": [...],
               "non_final": 0, "out_of_order": 0, "usable": true, ... },
  "split": { "train": 232, "validation": 72, "test": 60, "embargo_bars": 3, ... },
  "universe_name": "...", "universe_fingerprint": "...",
  "corporate_actions": 0, "adjustment_as_of": null,
  "leakage_findings": [],
  "channels": [ ... ], "notes": "",
  "gaps": [ "no source records: provenance is unknown", ... ]
}
```

**Two hashes, deliberately.** `content_hash` is over the bars, so two machines
can agree they hold the same data. `manifest_hash()` is over the whole record —
spec, schema fingerprint, sources, universe — so a dataset rebuilt from a
*different universe* with the same bars is a different dataset, which it is.
`test_the_same_bars_under_a_different_universe_are_a_different_dataset` asserts
exactly that.

**`gaps` is what the manifest cannot tell you.** Missing provenance is a field,
not the absence of one. `clean` requires `gaps` to be empty as well as the
leakage findings, because "we do not know whether this is survivorship-biased" is
a defect, and a `clean` that ignored it would let the most common real failure
pass as fine.

**`adjusted` is a three-state string, not a boolean.** `"false"` means the
provider says the prices are unadjusted; `"unknown"` means nobody asked. A
boolean would collapse them.

---

## 7. What is not here

Stated rather than left to be discovered:

- **No ingestion.** Nothing in `dataset.py` fetches. It processes bars it is
  handed. The 17 non-obtainable channels have no reader because there is no
  provider to read from (B1).
- **No vintage store.** `REVISED` channels are *flagged* and cannot be safely
  consumed. Building the point-in-time archive that would make them usable is
  not attempted.
- **No real corporate-action data.** `CorporateAction` and `adjust_for_actions`
  are implemented and tested on constructed cases. No action feed is reachable.
- **No cross-asset corpus.** `align_cross_asset` and `cross_asset_returns` work;
  there is one instrument's worth of reachable data to run them on, and it is
  synthetic.
- **`impact_bps` is zero** in the benchmark's cost model, and stated rather than
  omitted: with no book data there is nothing to calibrate it against.
