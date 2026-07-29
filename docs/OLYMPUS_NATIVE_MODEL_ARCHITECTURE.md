# The Olympus Multi-Task Market Model

**Architecture, tasks, abstention, forecast contract, training pipeline and
evaluation — and what each of them is not.**

- **Last updated:** 2026-07-28
- **Modules:** `native/{tasks,model,abstain,result,pipeline,serve,evaluation,originality,modelcard}.py`
- **Tests:** `tests/test_trading_native_model.py` (57),
  `tests/test_trading_native_pipeline.py` (68)
- **Companions:** `docs/OLYMPUS_MARKET_STATE_SCHEMA.md` (inputs),
  `docs/OLYMPUS_NATIVE_REPRESENTATIONS.md` (encoders, baselines),
  `docs/OLYMPUS_NATIVE_MODEL_STATUS.md` (the ledger)

> **No weights in this repository have been trained on real market data.** Every
> number here comes from series this repository generates. Blocker B1 is why,
> and `modelcard.assert_no_real_data_claim` enforces it rather than trusting a
> template.

---

## 1. Why this architecture

```
 bars ─► representation ─► identity ─► causal core ─► regime head
          (replaceable)                     │             │
                                            ▼             ▼
                                     cross-asset ──► regime experts
                                      attention          │
                                            │            ▼
                                            └───► timeframe fusion
                                                         │
                                                         ▼
                                                  modular task heads
```

Every block below carries the reason it was chosen and the alternative it was
chosen over. **Nothing here is claimed as novel in isolation** — patch encoders,
dilated causal convolutions, mixtures of experts, attention and quantile heads
are published, general techniques, and `model.architecture_report()`
`external_methods` names all six. What is Olympus's is the combination and the
constraints it was built under.

### 1.1 Causal convolutions, not attention

Causality is **structural**: the blocks pad on the left only and slice the
right-hand tail, so a position *cannot* read a later one. An attention mask is a
line of code that can be wrong, has been wrong in published implementations, and
whose being wrong produces excellent validation numbers. Cost is linear rather
than quadratic, which on four cores with no CUDA (B5) is the difference between
trainable and not. The receptive field is arithmetic — `1 + 2(k−1)(2^L − 1)`
positions — and `ModelConfig.covers_lookback` reports when the core does *not*
see its whole window rather than leaving that silent.

`test_the_causal_core_cannot_read_a_later_position` verifies it by perturbing
the last patch and checking earlier positions do not move.

### 1.2 The mixture of experts is routed by the regime head

This is the decision most worth arguing about. A conventional MoE learns its own
gate and needs load-balancing losses to stop it collapsing onto one expert. Here
**the gate is the regime classifier's softmax** — a head separately supervised
by `regime.RegimeClassifier`, which Olympus owns end to end.

Three consequences, and the third is a real cost:

1. Routing is interpretable: "which expert handled this window" answers "the
   trending-up one", auditable against a label a human can check.
2. There is no collapse mode and no balancing loss — the gate's distribution is
   pinned by its own supervision.
3. **The experts can only specialise along an axis the regime classifier already
   sees.** A free router might discover a better partition. That is given up
   deliberately, and `ModelConfig.regime_routing=False` exists so the comparison
   can be run. **It has not been run**, and the model card lists that as a
   limitation.

The mixture is *soft*, not top-k. Top-k saves compute at scale; at this scale it
saves nothing and introduces a discrete decision whose gradient must be
estimated. Six small matmuls means every expert receives gradient on every batch,
which is what stops the rare regimes' experts never training.

### 1.3 The rest

| Block | Choice | Why, and the cost |
|---|---|---|
| Representation | any `interfaces.MarketEncoder` | The seven candidates in `representations.py` are swappable without touching `model.py`. Default is the continuous patch encoder, on parsimony rather than a measured win |
| Identity | optional additive embedding | Per-instrument idiosyncrasy is real; memorisation is the risk. `evaluation` reports seen-versus-unseen separately. Disabled by default |
| Cross-asset | multi-head attention over aligned reference summaries | An absent reference contributes nothing, and a row with *every* reference absent passes through unchanged — softmax over an all-masked row is undefined and NaNs in most implementations |
| Timeframe fusion | fixed order at construction | Dict iteration order that is stable in one process and different in another is the silent corruption this package is arranged to prevent |
| Heads | one per enabled task | A disabled task has **no parameters at all**, so a checkpoint's head bank is a fact about the checkpoint rather than a runtime switch |
| Quantile head | base + softplus increments | A crossed quantile is arithmetically impossible rather than penalised. A p90 below a p10 is not a wide interval; it is a nonsense one that flows into a stop-loss |

Implemented and **not trainable here**: cross-asset attention and timeframe
fusion are built and shape-tested with constructed inputs. Their tasks are
blocked. What is missing is a corpus, not code.

---

## 2. The fifteen tasks

`tasks.TASKS` is the registry, and `TaskConfig` **refuses to enable a task whose
supervision does not exist**. That refusal is the module's main job: a checkpoint
advertising a spread head trained on nothing would pass every shape test and
report a confident number for a quantity it has never seen.

| # | Task | Class | Enabled by default | Note |
|---|---|---|---|---|
| 1 | `return_distribution` | supervised | ✅ | The primary head. Required — every derived output depends on it |
| 2 | `direction` | supervised | ✅ | A separate head, not a threshold on the median: a skewed distribution can have a negative median and a >50% chance of a positive move |
| 3 | `price_quantiles` | **derived** | (automatic) | `anchor·exp(q)` is exact. A second head could disagree, and a price band contradicting its own return band is worse than one band |
| 4 | `volatility` | supervised | ✅ | Predicted in log space, so a negative volatility is unrepresentable. Target is the **horizon's** realised sigma, not the input's |
| 5 | `regime` | supervised | ✅ | Weak supervision Olympus owns end to end. Also the MoE router, which is why it is on by default |
| 6 | `drawdown` | supervised | ✅ | A probability against a threshold, not a magnitude regression: the magnitude's distribution is skewed and MSE collapses to the mean, useless in the tail the risk engine cares about |
| 7 | `liquidity` | **blocked** | ❌ | Book depth is `NOT_INGESTED` (B1). Volume is a poor proxy — a thin book can trade large volume at terrible prices, which is the case the head exists to catch |
| 8 | `spread` | **blocked** | ❌ | Bid and ask are `NOT_INGESTED` (B1) |
| 9 | `execution_cost` | **blocked** | ❌ | Needs book state as input. Its *targets* exist — `outcomes.py` records real fills — so it is the one blocked task whose supervision is closer than its features |
| 10 | `anomaly` | self-supervised | ✅ | Target is the input. Doubles as the density proxy `ood` reads |
| 11 | `ood` | **derived** | (automatic) | Computed *about* the model. A head predicting its own OOD-ness would be trained only on in-distribution data — the one regime where the question does not arise |
| 12 | `calibration` | **derived** | (automatic) | Fitted on a held-out split *after* training. `CalibrationInfo` refuses `fitted_on="train"` |
| 13 | `abstention` | supervised | ✅ | The borderline case: a real head whose target is the model's own error, trained on a **detached** signal so the model cannot reduce this loss by making its forecasts predictably worse |
| 14 | `cross_asset` | **blocked** | ❌ | Needs a multi-instrument corpus. The alignment exists; the data does not |
| 15 | `multi_timeframe` | **blocked** | ❌ | Needs a multi-scale corpus |

**Seven of fifteen are trainable here.** Loss weighting is explicit per task and
recorded in the manifest; there is no automatic uncertainty-weighting scheme,
because those make "why did this head stop learning" unanswerable from the config
alone, which on a four-core budget with no room for ablations is the wrong trade.

`RunRecord.dead_heads()` reads the per-task loss trajectory and names any head
whose loss never moved — a total loss hides that entirely.

---

## 3. Abstention

Abstention is an **output**, not an error path. A model that always answers is
not more useful than one that sometimes declines; it is a model whose failures
arrive as confident numbers.

| Reason | Fires when |
|---|---|
| `unverified_checkpoint` | the content hash does not recompute |
| `incompatible_version` | architecture or task-schema version mismatch |
| `unsupported_horizon` | the requested horizon is not the trained one |
| `missing_channels` | a channel the checkpoint consumes is absent |
| `stale_input` | the last bar closed too long before `as_of` |
| `instrument_out_of_distribution` | unseen instrument **and** the model carries an instrument embedding |
| `unsupported_regime` | this regime had too few training windows |
| `poor_calibration` | recorded coverage error exceeds tolerance |
| `excessive_dispersion` | the interval is far wider than this instrument's returns actually spread |

Every reason is a check that runs, produces a measured value, compares against a
stated threshold, and records **both** — including on the passing path, so a
reader can later ask whether the limit was right.

Two orderings and one correction are worth stating.

**`unverified_checkpoint` is first and fails closed.** If the hash does not
recompute, nothing about the checkpoint can be trusted — including the thresholds
the other eight checks would use.

**`instrument_out_of_distribution` only applies to a model with an instrument
embedding.** An instrument-agnostic model applied to a new symbol is doing
exactly what it was built for.

**`excessive_dispersion` was wrong and was fixed by measurement.** The first
implementation compared the predicted interval against the window's per-bar sigma
scaled by `√h`. That fired on *every* forecast of a model whose intervals were in
fact covering 86% against a nominal 80% — because the square-root rule assumes
independent increments, and a market with independent increments would not be
worth forecasting. The reference is now `ServingContext.typical_terminal_spread`:
the empirical p10–p90 spread of *actual* h-step returns in training, rescaled by
how this window's volatility compares with the training median.

---

## 4. The forecast contract

`result.NativeForecast` carries all 22 required fields. Two rules govern it.

**A field that was not computed is `None`, never a default.** A drawdown
probability of `0.0` from a checkpoint with no drawdown head is a claim that the
instrument will not fall, and a consumer cannot tell it from a real prediction of
safety. `tasks_present` enumerates what was produced; `value(task)` returns
`None` for anything else. An **abstaining forecast carries no task values at
all** — filling them with zeros would let a caller that ignored `abstained` read
a claim of safety, which is the exact failure abstention exists to prevent.

**Provenance is required at construction.** `model_version`, `checkpoint_hash`,
`dataset_version` and `feature_schema_version` are mandatory. The chain is
dataset hash → training manifest → checkpoint hash → forecast, and any link
being absent breaks it silently unless something refuses.

`to_forecast_result()` narrows to the serving contract the rest of Olympus
consumes. **One direction only** — a narrow record cannot be widened back, and
offering a function that appeared to would invite reconstructing fields that were
never measured.

---

## 5. The training pipeline

> No experiment is reported as reproducible unless the dataset, code,
> configuration, environment and seed are all recorded.

`RunRecord.reproducible` is a conjunction of those five facts. **There is no
argument, field or method that sets it to `True`** — it is computed, and `gaps`
names what is missing. `test_a_run_missing_any_of_the_five_is_not_reproducible`
removes them one at a time.

| Feature | State here |
|---|---|
| Seed control | ✅ exercised — same seed → identical weights, different seed → different weights |
| Gradient accumulation | ✅ exercised — effective batch = `batch_size × accumulate`, with the trailing partial group still stepped |
| Checkpointing + hashing | ✅ exercised |
| Resume | ✅ exercised — epoch numbering continues |
| Early stopping | ✅ exercised — `selection_split` records which split chose the weights |
| Experiment tracking | ✅ `RunRecord`, one per run |
| Dataset + dependency pinning | ✅ recorded in the manifest |
| Per-task loss monitoring | ✅ `dead_heads()` |
| Calibration evaluation | ✅ on a held-out split; `fitted_on="train"` is refused |
| Artifact signing | ✅ ed25519 via `witness`; degrades to unsigned **with a stated reason** rather than aborting |
| Training-event audit | ✅ into the same trail as every order; a broken sink does not abort a run |
| **Mixed precision** | ⚠️ **implemented, never executed** — no CUDA (B5) |
| **Distributed** | ⚠️ **implemented, never executed** — one process (B5) |

The last two matter. `capabilities()` detects what is available, the run record
states what was *used*, and `RunRecord.untested_paths` names the compiled-but-
unrun paths with their reasons. An autocast context that has only ever been a
no-op is a no-op that has been tested, not a mixed-precision trainer.

**Early stopping selects on validation, which makes that split part of
training.** That is the reason there are three splits: selection happens on
validation, `test` is touched exactly once by `evaluation.py` after everything is
frozen, and `selection_split` records it so nobody has to reconstruct it.

---

## 6. Evaluation

**Do not optimise exclusively for price error.** `StratifiedReport.verdict`
requires a significant gain on the proper scoring rule **and** no loss on
decisions after costs — a model that scores better and loses money has not
improved anything anyone cares about.

Fourteen metrics: MAE, RMSE, MAPE (returns `None` rather than dividing by a
near-zero — on log returns the actual routinely *is* near zero), directional
accuracy, Brier score, log loss (clipped, because one infinity makes every
comparison meaningless), quantile loss, signed calibration error, volatility
error, drawdown quality (**lift, not accuracy** — a 5%-base-rate event is 95%
"accurately" predicted by a head that always says no), abstention quality,
latency, memory, failure rate, and decision value after costs.

Eight strata: instrument, timeframe, horizon, regime, volatility environment,
liquidity environment, calendar period, seen-versus-unseen instruments. A
stratum below 30 observations is marked `(thin)` rather than hidden.

Three honesty mechanisms:

- **Accuracy metrics score answered rows only.** Scoring an absent prediction as
  zero would credit the model with persistence's accuracy on every window it
  declined. `n` is offered, `n_scored` is answered, and a model that declined
  everything reports `None` for every accuracy metric.
- **Failure ≠ abstention.** A system conflating them would report its crashes as
  caution.
- **The liquidity stratum labels itself a proxy**, in its own output, because it
  is cut on volume rather than depth (B1).

The **external-benchmark arm** is generic. `evaluation.py` does not name any
third-party model — `kronos_adapter.native_benchmark_arm()` supplies the
identity, because a native evaluation module containing a competitor's name is
exactly what `tests/test_trading_independence.py` prevents. The arm is currently
*unavailable*, and is reported as missing rather than omitted.

---

## 7. Originality checks

Six claims, each an automated check returning findings with locations:

1. No native module imports a Kronos implementation module — at **any depth**, because a lazy import inside a function is how a dependency returns.
2. No native module references a weight file, hub or pretrained loader.
3. No native configuration names a foreign checkpoint identifier — walked through nested mappings and sequences.
4. Model cards disclose every external method the architecture uses.
5. Every third-party import is disclosed.
6. Ownership claims travel with what they rest on.

These live in a module, not only in tests, so that `modelcard.build_card` runs
them **before it emits a card** and refuses one that would state something false.
The disclosure and the check therefore cannot drift apart.

**Two exemptions exist and both are enumerated with reasons.**
`originality.MARKER_EXEMPT` covers the checker's own marker list; the
independence suite's `CHECKER_EXEMPT` covers the same module for the Kronos
rules. Each has a companion test that fails if the exemption outlives its reason
— and that test has already removed two entries that were no longer needed.

**Check 6 is the substantive one.** Ownership is not a property of a file's
location: a checkpoint initialised from someone else's weights is not
Olympus-owned wherever it lives, and an architecture transcribed from a paper is
Olympus-*implemented* rather than Olympus-*invented*. So a claim must travel with
the disclosure that makes it narrow.

---

## 8. What Phase 2 measured

Model: 7,561 parameters, lookback 33, horizon 3, seven heads, six experts.
Data: AR(1) φ=0.97 generated by this repository. 519 train / 155 validation /
181 test windows.

| Measurement | Value |
|---|---|
| Inference latency | p50 1.1 ms, p95 1.5 ms, max 5.9 ms |
| Peak resident memory | ~698 MB (whole process, `getrusage`) |
| Interval width vs a Gaussian at the realised sd | **3.1–3.4×** |
| Coverage, validation / test (nominal 0.80) | **0.68 / 0.84** |
| Quantile loss vs persistence | **significantly worse** (gain −0.0061, p ≈ 0) |
| Abstention rate at the default threshold | 0.47–1.00 depending on the volatility stratum |

**The intervals are simultaneously too wide and mis-centred.** Three times wider
than the realised dispersion warrants, and still covering only 68% of validation
observations against a nominal 80%. Widening a well-centred interval raises
coverage; this one does not, which means the location is off. That is a real
defect in a real component, found by measurement, and it is the top item in the
ledger's recommendations.

**The model loses to persistence on the proper scoring rule.** Consistent with
Phase 1, where it also lost to a 19-parameter AR(3) fit. Reported rather than
buried: a benchmark whose losses are not published is not a benchmark.

**No comparison against any third-party model has been run and none can be.**
No official checkpoint is reachable (B2) and the weight licence is unverified
(B4). No claim of superiority is made.

---

## 9. What is not here

- **Any real-data result.** Every number above is synthetic.
- **A tuned model.** No hyper-parameter search was run; the compute budget does
  not support one and a search on synthetic data would tune for the fiction.
- **The free-router ablation.** `regime_routing=False` exists and has not been
  compared against the supervised gate.
- **Mixed-precision or distributed training that has ever run.**
- **Five of the fifteen task heads' training.** Their supervision does not exist
  here, and `TaskConfig` refuses to enable them.
- **A calibrated model.** `fit_calibration` works and the defect it would correct
  is a mis-centred interval, which a scale correction does not fix.
