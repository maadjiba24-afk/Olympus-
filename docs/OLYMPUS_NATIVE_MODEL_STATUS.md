# Olympus Native Model — Implementation Status

**The honest ledger for the native forecasting work.**
`docs/OLYMPUS_NATIVE_MARKET_INTELLIGENCE.md` describes the *target*; this file
records what actually exists. When they disagree, this file is right.

- **Last updated:** 2026-07-28
- **Branch:** `claude/kronos-technical-teardown-54pjna`
- **Commit surveyed:** `41d8c03`; **P1 (decouple), P2 (skeleton), P3 (learning
  on synthetic data), Phase 1 (representation, dataset and baseline
  foundations), Phase 2 (the multi-task model), Phase 3 (capabilities beyond
  the reference model) and Phase 4 (controlled self-evolution) complete**
- **Companions:** `docs/OLYMPUS_MARKET_STATE_SCHEMA.md` (channels and dataset
  format), `docs/OLYMPUS_NATIVE_REPRESENTATIONS.md` (encoders, baselines,
  benchmark record), `docs/OLYMPUS_NATIVE_MODEL_ARCHITECTURE.md` (the multi-task
  model, abstention, pipeline, evaluation),
  `docs/OLYMPUS_NATIVE_CAPABILITIES.md` (the nine capabilities and their
  eight-fact reports),
  `docs/OLYMPUS_NATIVE_SELF_EVOLUTION.md` (the learning loop, research
  isolation and the twelve-stage gate)

---

## The one-line summary

> **Olympus owns no trained market model.** The native package exists and
> works: it builds market states, splits data without leakage, trains a
> gradient-descent quantile network from a seed, writes provenanced
> checkpoints, and serves forecasts through the standard interface. It has been
> fitted **only to synthetic series in tests**, because no market data is
> reachable (B1). It is plumbing that works, not a model that knows anything.

P3 changed the second clause and nothing else: there is now a real learner in
the pipeline rather than a lookup table. It was trained on series this
repository generates, so it demonstrates that the pipeline can recover a
structure that is definitely present. **It says nothing about markets.**

Phase 2 built the multi-task model itself: fifteen registered tasks of which
seven can be trained here, a causal-convolution core with a mixture of experts
routed by a separately supervised regime head, nine structural abstention
reasons, a 22-field forecast contract, a training pipeline whose
reproducibility is computed rather than asserted, a stratified evaluation over
eight cuts and fourteen metrics, and six automated originality checks that the
model card must pass before it will render. **It did not change the one-line
summary**: the model still loses to persistence on the proper scoring rule, and
its intervals are three times wider than the realised dispersion warrants while
still covering only 68% of validation observations against a nominal 80% — too
wide *and* mis-centred.

Phase 4 connected the native models to the self-evolution framework: a forecast
evidence journal carrying all fourteen required fields, ten weakness detectors,
ten kinds of challenger proposal with eleven required fields each, a twelve-stage
promotion gate whose last two stages no autonomous actor can reach, twelve
improvement metrics with thirteen volume counters explicitly refused, and
**OS-level research isolation** — a separate process in its own network
namespace, under rlimits, behind a seccomp filter, with a read-only bind-mounted
dataset, signed inputs and results, and a destroyed worker. The end-to-end
demonstration ends in a **rejection**, on the merits: the challenger converged on
persistence and could not be distinguished from it while carrying more
parameters. **It did not change the one-line summary either.**

Phase 3 built nine capabilities the reference model does not have — multi-scale
bar-state tracking, a time-versioned cross-asset graph, an order-book
tradability gate, an event boundary, read-only portfolio conditioning, a
recorded specialist router, probabilistic scenarios, closed-set explanation
codes, and a thirteen-condition adversarial suite. **Every one of them is
research-usable and none is production-eligible**, because production
eligibility needs seven facts and two of them — real-data and paper-trading
evaluation — are unobtainable here. The register computes that verdict rather
than asserting it: `CapabilityStatus.production_eligible` has no setter, and
`registry.enable(..., mode="production")` refuses with the missing facts named.
**It did not change the one-line summary either.**

Phase 1 added the foundations that make a claim about a model checkable at all —
a 38-channel market-state schema with full per-channel metadata, a dataset and
provenance system with five named leakage defences, stable encoder contracts,
seven implemented representation candidates, and nine baselines behind one
scoring harness. The first thing that harness reported is that **the native
network loses to a 19-parameter autoregressive fit** on a linear synthetic
process, and that its prediction intervals are badly calibrated (coverage 1.000
against a nominal 0.80). That is the benchmark working, not failing.

This line should not change until §3's gates start passing, and the phrase
"Olympus owns a Kronos-class model" must not appear anywhere until **G7, G8,
G11 and G13** have all passed on real data.

---

## 1. Status legend

Six evidence classes, kept distinct because collapsing them is how a synthetic
result becomes a market claim.

| Mark | Class | Meaning |
|---|---|---|
| **I** | Implemented | Code exists and runs |
| **U** | Unit tested | Tests exercise the adversarial cases, not just the happy path |
| **S** | Evaluated on synthetic data | Measured on series this repository generated |
| **R** | Evaluated on real data | **Nothing carries this mark. Blocker B1.** |
| **B** | Blocked | Cannot be built or demonstrated here; reason stated |
| **D** | Designed only | Written down; no code |

The shorthand marks below combine them:

| Mark | Meaning |
|---|---|
| ✅ | I + U — built, and the tests exercise the adversarial cases |
| ✅**S** | I + U + S — additionally measured on synthetic data |
| 🟡 | I, happy path tested, adversarial surface thin |
| 🔵 | D — designed; **no code** |
| ⛔ | B — reason stated |

**No component in this repository has ever been evaluated on real market data.**
That row of the ledger is empty and will stay empty until B1 lifts.

---

## 2. Component status

### The native system

| Component | Module (planned) | Status | Notes |
|---|---|---|---|
| Market-state **schema** | `native/schema.py` | ✅ | 38 channels, each with all eleven metadata fields. No zero-fill policy exists; a REQUIRED channel that cannot be obtained is refused at construction. **21 of 38 obtainable here** — `docs/OLYMPUS_MARKET_STATE_SCHEMA.md` §3 |
| Market-state representation | `native/state.py` | ✅ | 336 lines. Typed, causal (a scale closing after `as_of` raises), multi-scale, missing-is-missing. Reuses `features`, `regime`, `volatility` |
| Encoder | `native/encoder.py` | ✅ | 200 lines. Continuous patch projection over four scale-free channels — **no codebook, no vocabulary**. Instance-normalised over the input window only; a ragged patch grid is refused at construction |
| Temporal architecture | `native/trunk.py` | ✅ | 185 lines. Dilated **causal** convolutions, non-autoregressive, direct multi-horizon. Causality is structural (left-padding only) and is verified by perturbing the last patch and checking earlier positions do not move |
| Quantile head — table | `native/quantile.py` | 🟡 | 493 lines. Direct multi-horizon conditional quantiles by lookup. Kept as the cheap baseline and the abstention reference. Declines on a thin cell, an out-of-range input or a missing feature |
| Quantile head — learned | `native/neural.py` | ✅ | 439 lines. Monotone quantile ladder (softplus increments — quantiles cannot cross), trained by pinball loss, the same function `evaluate.py` scores with. 1,679 parameters at the tested config |
| Torch boundary | `native/torchutil.py` | ✅ | 164 lines. Lazy import behind the `native` extra, `DependencyMissing` when absent, seeded determinism, state-dict ⇄ JSON with shape checking |
| Multi-timeframe | `native/timeframes.py`, `native/state.py`, `native/dataset.py` | ✅**S** | Phase 3 added the eight-rung ladder with a `BarState` on every observation. `last_close` is gated on the state, not on the bar's existence, because a partial bar *has* a close and returning it is the leak; `assert_no_partial_leak` re-derives the states independently. **No model consumes more than the base scale yet** — the ladder produces features and `MultiTaskModel` does not read them |
| Cross-asset | `native/crossasset.py`, `native/dataset.py` | ✅**S** | Phase 3 added the relationship graph: seven edge kinds, every edge carrying `since`/`until` so a relationship declared in March cannot inform a February forecast. Features are causal by delegation to `align_cross_asset`, and `assert_no_future_reference` re-checks the raw series. Modelling remains blocked by B1: there is one instrument's worth of reachable data and it is synthetic |
| Regime head | `native/regime.py` | 🔵 | Weak supervision from `regime.RegimeClassifier` (Olympus-owned labels) |
| Volatility head | `native/vol.py` | 🔵 | Supervised against six existing estimators |
| Conformal calibration | `native/conformal.py` | 🔵 | Distribution-free coverage |
| Liquidity / execution cost | `native/liquidity.py` | 🔵 | Best data situation: `outcomes.py` already records fills, fees, slippage |
| Event awareness | `native/events.py` | ✅**S** | Seven kinds, split by whether their timing is knowable ahead. **Timing and declared importance only — no content field and no numeric surprise field.** External text is sanitised at construction and only the sanitised form is stored; `assert_event_boundary` refuses source naming a forbidden control, with one enumerated exemption and a staleness test |
| Portfolio-aware forecasting | `native/portfolio_context.py` | ✅**S** | Evidence, never sizing — and structurally so: the view copies values out and retains no manager, every type is frozen, and `assert_read_only` runs over every native module. The one capability whose inputs Olympus owns |
| Abstention / OOD | `native/quantile.py` | 🟡 | Range-based OOD and thin-cell abstention, feeding the existing `ForecastResult.abstained`. **Not** the conformal detector §3.11 describes |
| Dataset / windowing | `native/data.py` | ✅ | 324 lines. Horizon-inside-input refused on timestamps; embargoed split that cannot be set below the horizon |
| Dataset **provenance and alignment** | `native/dataset.py` | ✅ | Universe membership through time, corporate actions applied only as of a stated instant, causal multi-timeframe and cross-asset alignment, gap and duplicate detection, three-way and walk-forward splits, an **independent** leakage audit, and a two-hash manifest |
| Encoder **contracts** | `native/interfaces.py` | ✅ | `LatentSpec`, `EncoderMetadata`, `EncodedBatch`, `MarketEncoder`, `Tokeniser`, `Reconstructor`, `TimeframeFusion`, `CrossAssetContext`. Pure stdlib — importable with torch absent |
| Representation **candidates** | `native/representations.py` | ✅**S** | Seven implemented and compared on reconstruction. Each ships a machine-readable record with rationale, formulation, complexity, limitations and prior-art relationship; a candidate with a blank field is refused at construction |
| **Baselines** | `native/baselines.py` | ✅**S** | Nine, on one interface, emitting the same `QuantilePrediction` the native model does. Seven pure stdlib; two behind the `native` extra |
| **Benchmark harness** | `native/benchmark.py` | ✅**S** | One split, one cost model, one metric set for every model. Paired bootstrap on the intersection of scored windows. Decides nothing |
| **Task registry** | `native/tasks.py` | ✅ | Fifteen tasks: 7 trainable here, 5 blocked with stated reasons, 3 derived. A task whose supervision does not exist is refused at config construction |
| **Multi-task model** | `native/model.py` | ✅**S** | Causal core, regime-routed mixture of experts, optional identity/cross-asset/fusion blocks, one head per enabled task. 7,561 parameters at the tested config |
| **Abstention policy** | `native/abstain.py` | ✅ | Nine reasons, each a measured check against a stated threshold, recorded on the passing path too. Fails closed and checks integrity first |
| **Forecast contract** | `native/result.py` | ✅ | All 22 required fields. Provenance mandatory; an uncomputed field is `None`; an abstaining forecast carries no task values |
| **Training pipeline** | `native/pipeline.py` | 🟡 | Seeds, accumulation, checkpointing, resume, early stopping, tracking, pinning, per-task monitoring, signing, audit — all exercised. **Mixed precision and distributed are implemented and have never run** (B5), and the run record names them |
| **Inference** | `native/serve.py` | ✅**S** | Verifies before loading, one forward pass for every head, derives what is derived, decides, assembles. Latency and memory measured |
| **Evaluation pipeline** | `native/evaluation.py` | ✅**S** | Fourteen metrics over eight strata, decisions after costs as the headline, thin strata marked, abstained rows excluded from accuracy |
| **Originality checks** | `native/originality.py` | ✅ | Six automated checks; two enumerated exemptions, each with a staleness test that has already removed two entries |
| **Model card** | `native/modelcard.py` | ✅ | Generated from the artefacts, audits itself before rendering, refuses a real-data claim outright |
| Trainer | `native/train.py` | ✅ | 340 lines. `train` and `train_neural`, both in the one safe order; splits before any statistic is computed |
| Checkpoint format | `native/checkpoint.py` | ✅ | 407 lines. Manifest required at construction; foreign origins refused by shape; append-only store with content-hash verification |
| Evaluation driver | `native/train.evaluate_on_split` | 🟡 | Drives `evaluate.py`'s metrics and reports abstentions separately. `WalkForward` integration not yet wired |
| `Forecaster` implementation | `native/forecaster.py` | ✅ | 372 lines. Estimator registry — table or network, chosen by the checkpoint's kind. Registers beside the baselines; nothing downstream learns it exists |
| **Capability register** | `native/capability.py` | ✅ | The eight facts per capability, with `production_eligible` computed from seven of them and no setter. The rule is published as data in `ELIGIBILITY_RULE`; `enable()` refuses and names what is missing; `force=True` demands a written reason and records it |
| **Order book / liquidity** | `native/microstructure.py` | ✅**S** | Snapshots, spread, depth with its band, imbalance, fill probability, slippage, square-root impact, deterioration against the book's own median — and the six-condition tradability gate. **Nothing is calibrated**; every number pairs with `Estimate.calibrated=False` |
| **Regime specialists** | `native/specialists.py` | 🟡 | Eight specialists plus an always-registered generalist, four distinct fallback reasons, every decision recorded, and a `degenerate` check because a router that always picks the same destination is not routing. **No specialist has been trained** — they are registered slots |
| **Scenario generation** | `native/scenarios.py` | ✅**S** | Six scenarios summing to exactly one. The unconditional two take their probabilities from the model's own quantile asymmetry; the conditional three are declared, because a return distribution contains no information about whether the exchange will halt. A scenario with no falsifier is refused |
| **Evidence journal** | `native/evidence.py` | ✅**S** | The fourteen fields per matured forecast and ten weakness detectors. An abstention is evidence, error is `None` rather than zero when nothing was predicted, maturity is a fact about the clock with no `force`, and a finding below thirty observations is provisional rather than suppressed |
| **Challenger proposals** | `native/challengers.py` | ✅ | Ten kinds, eleven required fields, contradicting evidence mandatory, compute budget enforced by `isolation.py` rather than declared. Every complexity-adding proposal is paired with a simplification, so the parsimony tie-break has something to break toward |
| **Research isolation** | `native/isolation.py` | ✅**S** | Separate process, network namespace, seccomp-BPF filter, rlimits, allowlisted environment, read-only bind-mounted inputs, signed inputs and results, destroyed worker. Confinement is **observed by the worker**, not asserted by the parent, and a run whose confinement did not hold is discarded |
| **Promotion gate** | `native/promotion.py` | 🟡 | Twelve stages in order; a missing check fails rather than passes; rejection is terminal; `promote()` calls `governance.authorise` first and has no `force`. **Stages 9 and 10 have never actually run** — no paper broker on real quotes (B3), no live stream to shadow |
| **Improvement metrics** | `native/improvement.py` | 🟡 | The twelve Phase 4 names, with thirteen volume counters that raise rather than being ignored. **Seven of the twelve are unmeasured here** and are listed as such |
| **Explainability** | `native/explain.py` | ✅**S** | Twenty-six reason codes in a closed set, each with a measurement. `evidence_only` is what a machine reads; `narrative()` is assembled from the codes and cannot carry a claim they do not. Rules over context, **not attribution over the model's computation** |

**Native modules built: 40 files, 22,930 lines, 659 tests.** Of the 46 components
in the table above, **36 are built and adversarially tested (18 of them also
measured on synthetic data), 7 are partial, and 3 are untouched**: the standalone
conformal, regime and volatility heads. Phase 2 absorbed the regime and
volatility heads into the multi-task model and Phase 3 built the liquidity and
event components, so what remains untouched is the conformal layer — which
Phase 2 gave a much sharper reason to build.

All 40 are registered in `kernel.EVOLUTION_MODULES` and
`audit_evolution_modules()` returns zero findings.

### Infrastructure the native work will reuse — already built

Not native-model work, but load-bearing for it. Verified present and tested at
`e8380c6`:

| Layer | Status | Why it matters here |
|---|---|---|
| `forecast.Forecaster` ABC + 3 baselines | ✅ | The plug point exists and has opponents ready |
| `contracts.ForecastResult` | ✅ | Already model-neutral: quantiles, uncertainty, identity, abstention |
| `features.py` — causal features, scalers, `assert_causal` | ✅ | The leakage defence. Directly satisfies gate G6 |
| `regime.py`, `volatility.py` | ✅ | Weak-supervision labels for two heads |
| `storage.CandleStore` | ✅ | The training corpus store — **currently holds no real bars** |
| `evaluate.py` — pinball, CRPS, coverage, paired bootstrap | ✅ | Training objective and evaluation metric are the same function |
| `backtest.py` + `WalkForward` | ✅ | Out-of-sample harness |
| `registry.py` | ✅ | Checkpoint registration and approval |
| `champion.py` | ✅ | Matched comparison; raises on a mismatched harness |
| `capabilities.py`, `governance.py`, `kernel.py` | ✅ | Promotion gating, unchanged |
| `drift.py`, `outcomes.py`, `evolution.py` | ✅ | Deterioration detection and the learning loop |

**What is still genuinely missing is data.** After P3 there is an encoder, a
trunk, an optimiser and learned weights — `torch` is imported in exactly two
places, `kronos_runtime.py` and `native/torchutil.py`, both inside functions,
both behind their own optional extra (`kronos`, `native`). Nothing imports it at
module scope, `tests/test_deps_claim.py` stays green, and the whole native
package still imports and its non-neural half still runs with torch absent.

The gap is no longer machinery. It is that every series the machinery has ever
seen was generated by `tests/test_trading_native_neural.py`.

### P3 — what the synthetic run actually showed

Generating process: AR(1) on log steps, φ = 0.97, σ = 0.001, 1,400 bars. This
process was chosen because its conditional expectation is available in closed
form — `E[3-step cumulative log return | last step s] = s·(φ + φ² + φ³)` — so
"recovers the structure" can be scored against the **truth** rather than against
realised prices. A model can track realised prices by luck; it cannot track the
true conditional mean by luck.

| Measurement | AR(1), structure present | Random walk, **negative control** |
|---|---|---|
| Parameters | 1,679 | 1,679 |
| Train rows / test windows | 966 / 396 | 966 / 396 |
| Pinball loss, first → last epoch | 0.1532 → 0.0037 | 0.1487 → 0.0021 |
| `converged` | True | True |
| corr(predicted median, **true** conditional mean) | **0.516** | −0.528 |
| MAE, model vs persistence | **0.006671 vs 0.008827** | 0.012387 vs 0.003912 |
| Paired-bootstrap verdict | significantly **better** | significantly **worse** |
| Wall clock (4 cores, no CUDA) | 6.6 s | 4.6 s |

The right-hand column is the load-bearing one. The loss falls just as smoothly
on white noise — a training curve is not evidence — and the model is then
significantly *worse* than doing nothing. An evaluation that reported an edge
there would be measuring something other than skill, and the suite asserts it
does not.

**What this licenses.** One claim: the pipeline can learn a structure that is
genuinely present, and the harness can tell that case from the case where none
is. **What it does not licence:** any statement about market performance, any
comparison with Kronos, and any use of the word "trained" without the qualifier
"on synthetic data".


### Phase 1 — what the benchmark actually reported

Full tables in `docs/OLYMPUS_NATIVE_REPRESENTATIONS.md` §6. The two results that
change what anyone should believe:

**The native network is currently worse than an autoregressive fit.** On a
synthetic AR(1) process, ranked by pinball loss:

| Rank | Model | Params | Pinball | Coverage (nominal 0.80) |
|---|---|---|---|---|
| 1 | `autoregression` (AR 3) | 19 | 0.000934 | 0.840 |
| 2 | `linear_regression` | 54 | 0.000936 | 0.845 |
| 3 | `gradient_boosted_trees` | 855 | 0.001022 | 0.785 |
| 5 | **`olympus_native`** | **1,679** | **0.001744** | **1.000** |
| 9 | `persistence` (reference) | 15 | 0.002715 | 0.873 |

The process is linear by construction, so a linear model *should* win — this is
the benchmark behaving correctly, not the model failing unexpectedly. But two
things follow that were not known before Phase 1: the native model is beaten by
88× fewer parameters on a task it should be able to do, and **its intervals are
badly calibrated** — coverage 1.000 means every observation fell inside its
10–90 band, so its pinball score is being carried by an over-cautious spread
rather than a sharp median. Neither is visible in its training loss.

**On a random walk, nothing beat persistence.** `winners: (none)`, and the three
largest models were significantly *worse*. This is the control that makes the
first table worth reading: a harness reporting an edge on white noise would be
measuring something other than skill.

**The representation comparison found no winner worth adopting.**
`multi_resolution_patch` has the lowest reconstruction error and 3.5× the latent
positions of everything else, and it breaks the single time order the causal
trunk requires. The quantised candidates pay exactly the error floor they
document — `learned_tokens` plateaus at 256× the continuous error, `residual_vq`
cuts that by 2.4×. **No selection has been made**; choosing on synthetic
reconstruction would be choosing on the fiction.


### Phase 2 — what the multi-task model measured

Full tables in `docs/OLYMPUS_NATIVE_MODEL_ARCHITECTURE.md` §8. Reproduce with
`python scripts/native_train_and_evaluate.py`.

**The headline is a defect, not a result.** The model's prediction intervals are
**3.1–3.4× wider** than a Gaussian at the realised dispersion would need, and
they still cover only **0.68** of validation observations against a nominal
**0.80**. Widening a well-centred interval raises coverage; this one does not,
which means the *location* is wrong as well as the scale. A scale correction —
which is what `fit_calibration` produces — cannot fix that, and saying so is
more useful than shipping the correction.

| Measurement | Value |
|---|---|
| Parameters | 7,561 |
| Inference latency | p50 1.18 ms, p95 1.47 ms, max 7.9 ms |
| Peak resident memory | ~700 MB, whole process, `getrusage` |
| Directional accuracy | 0.580 |
| Coverage error (signed, points) | **+13.4** overall; 0.0 in the high-volatility stratum, +20.0 in the low |
| Quantile loss vs persistence | **−0.0089, significant (p ≈ 0) — worse** |
| Net return after costs | +0.00047 overall; **negative** in the low-liquidity and high-volatility strata |
| Dead heads | none — all seven heads' losses moved |

Two secondary findings worth keeping:

- **The stratification earns its place immediately.** Overall net return is
  positive; the low-liquidity-proxy stratum is −0.0036 and the high-volatility
  stratum is −0.0018. An average would have hidden both, and they are the strata
  a live system meets first.
- **Calibration is regime-dependent.** Coverage error is 0.0 in high volatility
  and +20.0 in low — the model is correctly sized when markets move and far too
  cautious when they do not. That is a specific, actionable defect rather than a
  general "miscalibrated".

**No comparison against any third-party model was run and none can be.** No
official checkpoint is reachable (B2) and its weight licence is unverified (B4).
The arm is carried in the report as *missing with a reason* rather than dropped.
No claim of superiority is made.

### Phase 3 — what the capability register reports

Full detail in `docs/OLYMPUS_NATIVE_CAPABILITIES.md`. Print the register with:

```bash
python -c "from olympus.trading.native.capability import native_capabilities;\
print(native_capabilities().table())"
```

**Nine capabilities. Zero production-eligible. Nine research-usable.**

| Capability | Impl | Data | Tests | Historical | Real | Paper | Production |
|---|---|---|---|---|---|---|---|
| multi_timeframe | implemented | not ingested | adversarial | synthetic | blocked | none | **NO** |
| cross_asset | implemented | unreachable | adversarial | synthetic | blocked | none | **NO** |
| order_book_liquidity | implemented | not ingested | adversarial | synthetic | blocked | none | **NO** |
| event_awareness | implemented | not ingested | adversarial | synthetic | blocked | none | **NO** |
| portfolio_aware | implemented | internal | adversarial | synthetic | none | none | **NO** |
| regime_specialists | implemented | derivable | adversarial | synthetic | blocked | none | **NO** |
| scenario_generation | implemented | derivable | adversarial | synthetic | blocked | none | **NO** |
| explainability | implemented | derivable | adversarial | synthetic | none | none | **NO** |
| robustness | implemented | derivable | adversarial | synthetic | blocked | none | **NO** |

Phase 3 measured no new forecasting number, and that is the correct outcome:
the capabilities are inputs to and constraints on a forecast, not a forecast.
What it did establish is a set of structural results, each of which is a test
rather than a claim:

- **A partial higher-timeframe bar cannot leak its close.** The attribute
  exists on the candle; the gate is on the state, and a second, independent
  check re-derives every state from the bars and the clock.
- **A cross-asset edge declared later cannot inform an earlier instant.**
  Verified by declaring one after the fact and asserting the context comes back
  empty.
- **A positive expected return is not a tradable forecast.** Six conditions,
  each broken alone in a test, each alone blocking the trade. An unknown book
  is treated as *not fitting*, never as an infinite one.
- **Event text reaches nothing.** Not because the sanitiser is complete — it is
  a denylist and the suite feeds it a payload it misses — but because
  `EventContext` exposes four numbers and no content field at all.
- **A forecaster cannot mutate the portfolio.** The view copies out and retains
  no manager; `assert_read_only` runs over all 35 native modules.
- **The router falls back for four distinct reasons**, and `RoutingLog`
  reports when one destination has taken 95% of decisions — which the
  individual decisions never show.
- **Scenario probabilities sum to one or the set is refused**, and the
  bullish/bearish split is derived from the model's own quantile asymmetry
  rather than declared.

Three limitations are worth carrying into this document rather than leaving in
the capability file:

1. **Nothing in the microstructure module is calibrated.** Fill probability,
   slippage and impact all run on declared defaults. The impact coefficient
   *could* be fitted from `outcomes.py`'s recorded fills — that is the one
   calibration this system could perform today, and it has not been done.
2. **No specialist has been trained.** The router, the fallbacks and the record
   are complete; the specialists are registered slots. At the lookbacks used
   here the regime classifier reports `UNKNOWN` and the generalist answers
   everything, which is correct behaviour and means the specialisation has
   never been exercised.
3. **The robustness suite establishes safe degradation, not good degradation.**
   Safe degradation is refusing to answer, and a system that refused everything
   would also pass it. That is why abstention rates sit beside every score in
   the Phase 2 evaluation rather than "it declined" being counted as a success.

### Phase 4 — what self-evolution established

Full detail in `docs/OLYMPUS_NATIVE_SELF_EVOLUTION.md`. Both demonstrations run
with `python scripts/native_evolution_demo.py`.

**Phase 4 measured no forecasting number either**, and that is again the correct
outcome: it built the loop that will measure them. What it did establish is a
set of structural facts, each a test:

- **Generated research code cannot reach a broker.** Not by import discipline —
  by an empty network namespace, a seccomp filter denying `socket`/`connect`,
  and an environment rebuilt from an allowlist. Fifteen of sixteen mechanisms
  apply on this host and the sixteenth reports why it does not.
- **The worker verifies its own confinement.** If the network namespace did not
  apply, the probe sees a working socket and the result is discarded rather
  than trusted.
- **A challenger cannot skip the leakage test.** Stages are accepted only in
  order, and a stage with no check *fails*.
- **Olympus cannot promote.** `promote()` calls `governance.authorise` first,
  there is no `force`, and an autonomous actor has no route to becoming an
  operator.
- **A failure cannot be lost.** Rejection is terminal, there is no mutator and
  no delete, and `concealment_check()` re-reads the stored objects
  independently of the constructor that would have refused them.
- **Progress cannot be claimed by volume.** Thirteen counters — lines of code,
  proposals generated, capabilities added and the rest — raise when passed to
  `measure()` rather than being quietly dropped.

Three findings from building it, kept because they are the point:

1. **`chmod 0444` does not bind a uid-0 worker.** The first version shipped a
   dataset the worker could overwrite while the manifest said read-only. It is
   now a read-only bind mount in a private mount namespace, which the VFS
   enforces regardless of uid.
2. **A mis-specified baseline manufactures an improvement.** The demonstration's
   first "persistence" arm predicted the last return again; on a random walk
   that is √2 worse than predicting zero, so the challenger beat it by 30% on
   pure noise. The second version compared two means with `<` and passed a 1.5%
   difference on 160 observations. Only the third — a seeded paired bootstrap
   plus the parsimony tie-break — rejected it.
3. **The seccomp filter cannot precede `execve`.** It denies `execve`, so
   installing it in `preexec_fn` means the worker never starts.

Two limitations belong in this document rather than only in the capability file:

- **Gate stages 9 and 10 have never run.** Paper trading needs a broker fed by
  real quotes (B3) and shadow mode needs a live input stream. A challenger that
  reaches `awaiting_review` here has passed eight real stages and two recorded
  from constructed evidence, and the gate does not distinguish them.
- **Seven of the twelve improvement metrics are unmeasured.** Robustness score,
  failure rate, drift-detection time, rollback time, drawdown, risk-adjusted
  return and `simpler_at_equal_performance`. Four need something outside a
  forecast journal; three need trades that happened.

---

## 3. Completion gates — current state

Gate definitions in `docs/OLYMPUS_NATIVE_MARKET_INTELLIGENCE.md` §6.

| # | Gate | State |
|---|---|---|
| G1 | No Olympus module outside the Kronos files references Kronos | ✅ **Met.** `tests/test_trading_independence.py`: no import, no identifier, no runtime string. One enumerated exemption (`__init__.py`'s lazy-import table) |
| G2 | No native module imports Kronos or a Kronos constant | ✅ **Met.** No import, identifier or runtime string; no Kronos-imposed constant; prose mentions confined to module docstrings; no module-scope torch. Phase 1 added two named structural tests: one walks *every* import at any depth (a lazy import inside a function is how a dependency returns), and one refuses any reference to an external weight file, codebook or hub identifier — which matters now that two candidates carry codebooks |
| G3 | Native weights never initialised from foreign weights | ✅ **Met.** `assert_olympus_origin` refuses a URL, a path, a weights file or a hub id by shape, and an unknown id when the store is consulted. Every neural weight in existence came from `torch.manual_seed` inside `train_neural` |
| G4 | Design note per component | ✅ **Met** — all twelve in architecture doc §3 |
| G5 | Deleting the Kronos modules breaks no Olympus module | ✅ **Met, executed.** A subprocess blocks both modules at import and every other trading module still imports |
| G6 | No look-ahead in the training pipeline | ✅ **Met, and now audited independently.** Horizon-inside-input refused on timestamps; embargo cannot be set below the horizon; split before any statistic; instance normalisation reads the input window only; the trunk is left-padded, tested by perturbation. Phase 1 added `dataset.leakage_report`, which re-derives the checks *without sharing code with the splitter*, plus survivorship filtering, as-of corporate-action adjustment, causal multi-timeframe alignment and a `ScalerPolicy` that refuses to fit across the boundary |
| G7 | Training reproducible from a seed | ✅ **Met, and now for a gradient-descent learner.** Same seed → byte-identical weights; different seed → different weights (a fit that ignored its seed would pass the first assertion alone). `configure_determinism` sets `manual_seed`, `use_deterministic_algorithms(True)`, single-threaded execution and a seeded batching `Generator` |
| G8 | Every checkpoint carries a complete manifest | ✅ **Met.** Required at construction; missing reproducibility fields reported in `manifest.gaps` rather than hidden |
| G9 | Abstains outside the training manifold | ✅ **Met.** Phase 2 built the full policy: nine structural reasons, each a measured check against a stated threshold, recorded on the passing path too, integrity checked first and failing closed. Every reason is individually constructed in `tests/test_trading_native_model.py`. Abstention cannot be gamed — accuracy metrics score answered rows only, and the rate sits beside every score. The conformal *detector* remains unbuilt; the range-based one plus the eight structural checks is what "met" rests on |
| G10 | Uncertainty calibrated within ±5 coverage points | ⛔ **Blocked — B1** for the gate itself. The measurement exists, is run every evaluation, and the model **fails it on synthetic data**: +13.4 coverage points overall, +20.0 in low volatility, 0.0 in high. Phase 2 also found the intervals are 3.1–3.4× too wide *and* mis-centred, which a scale correction cannot fix. Not a G10 result — G10 is about real data — but a specific defect with a specific location |
| G11 | Beats persistence / drift / seasonal-naive out of sample | ⛔ **Blocked — B1.** No real data. The harness is built, has nine opponents rather than three, applies costs, stratifies over eight cuts, and has now reported a loss twice: the multi-task model loses to persistence on quantile loss (p ≈ 0) and Phase 1's network lost to a 19-parameter AR(3) fit |
| G12 | Beats the same strategy without it | ⛔ **Blocked — B1** |
| G13 | Native vs Kronos under one matched harness | ⛔ **Blocked — B1, B2, B4.** The harness now has a first-class *unavailable arm*: the comparison is carried in every report as missing-with-a-reason rather than dropped, and the arm's identity comes from `kronos_adapter` so the native evaluation module contains no competitor's name. Weights unreachable, licence unverified, no claim made |
| G14 | Complexity earns its place (parsimony) | ⛔ Depends on G11. Now measurable: `ForecastScore.parameters` is reported beside every result and `RepresentationResult.error_per_parameter` scales reconstruction error by size, so the largest model cannot win by being largest |
| G15 | Cannot promote itself | ✅ **Met by construction** — `capabilities.promote()` refuses an autonomous actor today, and `evaluation.run` / `benchmark.run_benchmark` return numbers rather than decisions |
| G16 | Safety kernel unreachable from `native/` | ✅ **Met, and now on three axes.** All **thirty-five** native modules are in `kernel.EVOLUTION_MODULES` and `audit_evolution_modules()` returns zero findings. Phase 3 added two further source-level boundaries over the same set: `events.assert_event_boundary` (no event-handling code names a risk limit, credential, permission, live-mode flag, safety control or deployment gate) and `portfolio_context.assert_read_only` (no forecasting module names a portfolio or order mutator). Each has exactly one enumerated exemption — the module that declares the forbidden names — with a companion staleness test |
| G17 | Deterioration detected and acted on | ✅ **Mechanism met, and now exercised on a native model.** Phase 4's demonstration B runs the whole path: a measured metric regression flags the component, the monitor restricts it to `DISABLED` with no operator, an autonomous reinstatement is refused, two rollback triggers fire, the deployment ledger restores the version a named operator deployed, and a twelve-entry hash-chained audit trail verifies. On synthetic evidence, so the state does not change |

**Score: 12 met, 1 partial, 4 blocked, 0 vacuous.** Phase 2 closed G9.
**Phase 4 closed none.** G15 (cannot promote itself) and G16 (kernel
unreachable) were already met and are now met on a wider surface — five more
modules audited, and a promotion path that exists and refuses. G17
(deterioration detected and acted on) moves from *mechanism met, unexercised on
a native model* to **exercised end to end on a native model**, but on synthetic
evidence, so it does not change state.
**Phase 3 closed none**, and it would be wrong to claim otherwise: nine
capabilities that cannot be evaluated on real data cannot close a gate that is
about real data. What Phase 3 changed is G16's evidence — the audit now covers
35 native modules rather than 26, and two further structural boundaries
(`assert_event_boundary`, `assert_read_only`) are enforced over every one of
them — and it added a register that refuses to call any of the new work ready.

P1 closed G1 and G5; P2 closed G2, G3, G6, G7, G8 and G16, and moved G9 to
partial and G10 from vacuous to blocked. Every remaining gate is now either
*measured* or *blocked on data* — none is vacuous, which is the useful thing
P2 changed.

**P3 closed no new gate**, and the earlier version of this document was wrong to
say it would close G6 and G7 — P2 had already closed both. What P3 actually did
is re-establish them for a learner that can violate them: a lookup table has no
optimiser to be non-deterministic and no receptive field to leak through, so
those gates were cheap to hold. They now hold for a network. That is a
strengthening of existing evidence, not a new gate, and it is recorded as such.

**Phase 2 closed G9** — abstention outside the training manifold — on the
strength of nine structural checks, each individually constructed in a test, and
a harness in which declining cannot be gamed. It closed nothing else. Every value
gate is still blocked on B1, and Phase 2 did not lift it. What it did instead is
find a defect: the intervals are too wide *and* mis-centred, and the coverage
error is regime-dependent. A phase that finds a defect in its own output has done
its job.

**Phase 1 also closed no new gate**, and it would be wrong to claim otherwise.
Every value gate is blocked on B1 and Phase 1 did not lift it. What Phase 1
changed is that four gates went from *asserted* to *measurable*: G6 gained an
audit independent of the code it audits, G9 gained an abstention rate that
cannot be gamed, G10 gained a coverage measurement that the native model
immediately failed on synthetic data, and G14 gained a parsimony column. A gate
you can fail is worth more than a gate you can only pass.

**No value gate has been attempted.** G11–G14 are the ones that decide whether
any of this is worth having, and all four need B1 to lift.

---

## 4. Blockers

Full statements in the architecture doc §7.

| # | Blocker | Severity | Effect |
|---|---|---|---|
| **B1** | No training data — every provider 403 at CONNECT | ⛔ Hard | Phases P4+ cannot start. No claim about market performance is possible |
| **B2** | Kronos checkpoint unreachable (`huggingface.co` 403) | ⛔ Hard | G13 unattemptable. "Outperforms Kronos" not claimable |
| **B3** | No broker sandbox reachable | ⛔ Hard | P7 cannot start |
| **B4** | Kronos **weight** licence unverifiable | ⛔ Hard | Distillation prohibited until read. May be permanent |
| **B5** | 4 CPU cores, 15 GB RAM, no CUDA, ephemeral | 🟡 Severe | Caps the model at ~10⁵–10⁶ parameters. Shapes the architecture; does not block it |
| **B6** | Olympus ships 3 required deps with a CI guard | 🟡 Constraint | `torch` goes in a `native` extra, imported lazily |
| **B7** | Ephemeral container, no artefact store | 🟡 Constraint | Checkpoint storage undecided. The manifest is small and must be committed regardless |

**B1 is the one that matters.** The other six are solvable by decisions; B1 is
solvable only by access. A native architecture with no data is a plan, and this
document should keep saying so.

---

## 5. What can proceed now

| Phase | Blocked? | Value |
|---|---|---|
| **P1 — Decouple from Kronos** | ✅ **Done** | Closed G1 and G5. 28 independence tests; no native code written |
| **P2 — Native skeleton** | ✅ **Done** | Closed G2, G3, G6, G7, G8, G16. The estimator is real, not a stub — it can beat a baseline or fail to, which is what makes the comparison worth running |
| **P3 — Learning on synthetic data** | ✅ **Done** | Closed no new gate; strengthened G6 and G7 for a gradient-descent learner. Encoder, trunk, monotone quantile head, trainer, 30 tests. Converges on a known structure, recovers it (r = 0.516 against the closed-form conditional mean), and correctly fails to beat persistence on white noise. **Proves nothing about markets** and must not be reported as if it did |
| **Phase 1 — representation and dataset foundations** | ✅ **Done** | Closed no gate; moved G6, G9, G10 and G14 from asserted to measurable. 38-channel schema, dataset provenance and leakage audit, encoder contracts, 7 representation candidates, 9 baselines, 1 harness, 196 new tests. **First result: the native model loses to AR(3) and its intervals are 20 coverage points too wide** |
| **Phase 2 — the multi-task model** | ✅ **Done** | Closed G9. Fifteen tasks (7 trainable), regime-routed mixture of experts, nine abstention reasons, 22-field contract, reproducibility computed not asserted, 14 metrics over 8 strata, 6 originality checks, self-auditing model card. 125 new tests. **First result: the model loses to persistence and its intervals are 3.1–3.4× too wide while covering only 0.68 against a nominal 0.80** |
| **Phase 3 — capabilities beyond the reference model** | ✅ **Done** | Closed no gate; strengthened G16 on two new axes. Nine capabilities, a register whose readiness verdict is computed, thirteen adversarial conditions, 178 new tests. **First result: nine of nine are research-usable and zero are production-eligible**, which is what the evidence supports and not a placeholder |
| **Phase 4 — controlled self-evolution** | ✅ **Done** | Closed no gate; widened G15/G16 and exercised G17 on a native model. Evidence journal, ten weakness detectors, ten challenger kinds, OS-level research isolation, a twelve-stage gate and twelve improvement metrics. 130 new tests. **First result: the end-to-end demonstration ends in a rejection**, because the challenger converged on persistence and could not be distinguished from it while carrying more parameters |
| **P4–P7** | ⛔ Yes | — |
| **P8 — Continuous learning wiring** | Partly | The governance wiring can be built and tested; the learning it governs cannot run |

**Recommendation.** Two pieces of unblocked work are now worth more than any
further architecture.

1. **Fix the interval defect, which Phase 2 localised.** The intervals are
   3.1–3.4× wider than the realised dispersion warrants *and* still cover only
   0.68 against a nominal 0.80, with the error concentrated in low volatility
   (+20.0 points) and absent in high (0.0). Too wide and mis-centred is not what
   a conformal scale correction fixes — `fit_calibration` would narrow a band
   that is already missing. The location error has to be found first, and the
   regime-dependence is the clue: the model is correctly sized when markets move
   and far too cautious when they do not. This does not need market data, which
   makes it the one substantial piece of unblocked modelling work left.
2. **Calibrate the impact model from recorded fills.** Phase 3 built
   `ImpactModel.calibrate_from_fills`, which needs `size`, `depth`,
   `spread_bps` and `realised_bps` per fill — and `outcomes.py` already records
   real fills, fees and slippage from the paper broker. It is the only estimator
   in the system whose calibration is reachable today, and until it is run every
   slippage and tradability number reports `calibrated=False` and means it. The
   liquidity / execution-cost *head* (§3.8) is the larger version of the same
   observation: it remains the only model component whose training data was not
   invented for a test.

Everything else waits on B1. Further architecture — multi-timeframe,
cross-asset, regime and volatility heads — would be tuned against series this
repository invents, which optimises for the fiction. Phase 1's benchmark is the
argument: a bigger model already lost to a smaller one on data we control.

**Original recommendation, kept for the record:** do P1 first and completely. It removes a real dependency, is
independently verifiable by an AST test, and its value does not depend on the
native model succeeding. Doing P3 before P1 would produce an unproven model
inside a system still coupled to Kronos — the worst of both.

---

## 6. Reporting rules for this work

Carried forward from the trading-domain phases, where the same discipline was
applied:

1. **Separate the evidence classes.** Implemented / unit tested / integration
   tested / **tested against real data** / blocked / designed but unimplemented.
   A synthetic-data result is never reported as a market result.
2. **Do not use "complete"** for the native-model objective while any of G7,
   G8, G11 or G13 is unproven.
3. **Do not claim independence** on the basis of an adapter, a rename, or a new
   brand name. G1, G2, G3 and G5 are the claim; each is a test.
4. **Do not claim superiority** without `champion.compare()` returning a verdict
   on a matched harness. An "indistinguishable" verdict is a real result and
   should be published as one.
5. **A failed value gate is an acceptable outcome.** If the native model does not
   beat persistence, the correct action is to say so and keep the baseline —
   `champion.compare()`'s parsimony rule already encodes that preference.

---

## 7. Verify these claims yourself

```bash
# what the schema can and cannot obtain here
python -c "import json;from olympus.trading.native import schema as s;\
print(json.dumps(s.OLYMPUS_MARKET_SCHEMA.availability_report(), indent=2))"

# torch appears inside functions only, behind an extra
grep -rn "import torch\|import numpy" --include='*.py' olympus/

# the synthetic-data results, including both negative controls
python -m pytest tests/test_trading_native_neural.py \
                tests/test_trading_native_baselines.py -q

# the schema, dataset and representation guarantees
python -m pytest tests/test_trading_native_schema.py \
                tests/test_trading_native_dataset.py \
                tests/test_trading_native_repr.py -q

# nothing native reaches Kronos, imports torch at module scope, or reads a
# foreign weight file
python -m pytest tests/test_trading_independence.py -q

# reproduce the published benchmark tables, including the negative control
python scripts/native_benchmark.py

# train the multi-task model, evaluate it, and emit its model card
python scripts/native_train_and_evaluate.py

# what this host can enforce on generated research code
python -c "import json; from olympus.trading.native.isolation import \
isolation_report; print(json.dumps(isolation_report()['confinement'], indent=2))"

# both self-evolution demonstrations, end to end
python scripts/native_evolution_demo.py

# the learning loop, the gate, the metrics, and the nine prohibitions
python -m pytest tests/test_trading_native_evolution.py \
                tests/test_trading_native_isolation.py -q

# the nine capabilities and their readiness verdicts
python -c "from olympus.trading.native.capability import native_capabilities;\
print(native_capabilities().table())"

# the capabilities and the thirteen adversarial conditions
python -m pytest tests/test_trading_native_capabilities.py \
                tests/test_trading_native_robustness.py -q

# the six originality checks, as data
python -c "import json;from olympus.trading.native import originality as o;\
print(json.dumps(o.audit_report(), indent=2))"

# every Kronos reference in the repo
grep -ril kronos --include='*.py' --include='*.toml' .

# the infrastructure the native work will reuse
python -m pytest tests/test_trading_forecast.py tests/test_trading_features.py \
                 tests/test_trading_evaluate.py tests/test_trading_champion.py -q

# the governance gates that are already met
python -m pytest tests/test_trading_capabilities.py tests/test_trading_kernel.py -q
```

If a command here contradicts this document, the document is wrong and should be
corrected rather than explained away.
