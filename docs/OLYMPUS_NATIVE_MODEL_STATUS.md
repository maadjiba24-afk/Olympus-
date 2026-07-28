# Olympus Native Model — Implementation Status

**The honest ledger for the native forecasting work.**
`docs/OLYMPUS_NATIVE_MARKET_INTELLIGENCE.md` describes the *target*; this file
records what actually exists. When they disagree, this file is right.

- **Last updated:** 2026-07-28
- **Branch:** `claude/kronos-technical-teardown-54pjna`
- **Commit surveyed:** `e8380c6`; **P1 (decouple), P2 (skeleton), P3 (learning
  on synthetic data) and Phase 1 (representation, dataset and baseline
  foundations) complete**
- **Companions:** `docs/OLYMPUS_MARKET_STATE_SCHEMA.md` (channels and dataset
  format), `docs/OLYMPUS_NATIVE_REPRESENTATIONS.md` (encoders, baselines,
  benchmark record)

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
| Multi-timeframe | `native/state.py`, `native/dataset.py` | 🟡 | The state carries one `ScaleObservation` per timeframe, and `align_timeframes` now pairs each base bar with the newest *closed* context bar and reports its staleness — a finer context series is refused. **No model consumes more than the base scale yet**, and `TimeframeFusion` is a contract with no implementation |
| Cross-asset | `native/dataset.py` | 🟡 | `align_cross_asset` and `cross_asset_returns` are implemented and tested — a stale reference yields `None`, never a zero return. `CrossAssetContext` is a contract with no implementation, and the modelling remains blocked by B1: there is one instrument's worth of reachable data and it is synthetic |
| Regime head | `native/regime.py` | 🔵 | Weak supervision from `regime.RegimeClassifier` (Olympus-owned labels) |
| Volatility head | `native/vol.py` | 🔵 | Supervised against six existing estimators |
| Conformal calibration | `native/conformal.py` | 🔵 | Distribution-free coverage |
| Liquidity / execution cost | `native/liquidity.py` | 🔵 | Best data situation: `outcomes.py` already records fills, fees, slippage |
| Event awareness | `native/events.py` | 🔵 | Event *timing* only; claims stay untrusted per `knowledge.py` |
| Portfolio-aware evaluation | `native/portfolio_eval.py` | 🔵 | Produces evidence, never sizing |
| Abstention / OOD | `native/quantile.py` | 🟡 | Range-based OOD and thin-cell abstention, feeding the existing `ForecastResult.abstained`. **Not** the conformal detector §3.11 describes |
| Dataset / windowing | `native/data.py` | ✅ | 324 lines. Horizon-inside-input refused on timestamps; embargoed split that cannot be set below the horizon |
| Dataset **provenance and alignment** | `native/dataset.py` | ✅ | Universe membership through time, corporate actions applied only as of a stated instant, causal multi-timeframe and cross-asset alignment, gap and duplicate detection, three-way and walk-forward splits, an **independent** leakage audit, and a two-hash manifest |
| Encoder **contracts** | `native/interfaces.py` | ✅ | `LatentSpec`, `EncoderMetadata`, `EncodedBatch`, `MarketEncoder`, `Tokeniser`, `Reconstructor`, `TimeframeFusion`, `CrossAssetContext`. Pure stdlib — importable with torch absent |
| Representation **candidates** | `native/representations.py` | ✅**S** | Seven implemented and compared on reconstruction. Each ships a machine-readable record with rationale, formulation, complexity, limitations and prior-art relationship; a candidate with a blank field is refused at construction |
| **Baselines** | `native/baselines.py` | ✅**S** | Nine, on one interface, emitting the same `QuantilePrediction` the native model does. Seven pure stdlib; two behind the `native` extra |
| **Benchmark harness** | `native/benchmark.py` | ✅**S** | One split, one cost model, one metric set for every model. Paired bootstrap on the intersection of scored windows. Decides nothing |
| Trainer | `native/train.py` | ✅ | 340 lines. `train` and `train_neural`, both in the one safe order; splits before any statistic is computed |
| Checkpoint format | `native/checkpoint.py` | ✅ | 407 lines. Manifest required at construction; foreign origins refused by shape; append-only store with content-hash verification |
| Evaluation driver | `native/train.evaluate_on_split` | 🟡 | Drives `evaluate.py`'s metrics and reports abstentions separately. `WalkForward` integration not yet wired |
| `Forecaster` implementation | `native/forecaster.py` | ✅ | 372 lines. Estimator registry — table or network, chosen by the checkpoint's kind. Registers beside the baselines; nothing downstream learns it exists |

**Native modules built: 17 files, 9,181 lines, 293 tests.** Of the 26 components
in the table above, **15 are built and adversarially tested (3 of them also
measured on synthetic data), 5 are partial, and 6 are untouched**: the regime,
volatility, conformal, liquidity, event and portfolio-evaluation heads. Every
untouched one needs data that B1 blocks, except two — the conformal layer, which
Phase 1 gave a reason to build, and the liquidity head, whose inputs already
exist in `outcomes.py`.

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
| G9 | Abstains outside the training manifold | 🟡 **Partial.** Range-based OOD works and is tested; the conformal nonconformity detector is not built. Phase 1 made abstention measurable rather than merely possible: `benchmark.ForecastScore.abstention_rate` sits beside every score and `compare_scored` intersects window sets, so a model cannot win by declining the hard cases |
| G10 | Uncertainty calibrated within ±5 coverage points | ⛔ **Blocked — B1** for the gate itself, but the *measurement* now exists and has been run on synthetic data, where the native model **fails it badly**: coverage 1.000 against a nominal 0.800, a +20-point error. That is not a G10 result — G10 is about real data — but it is a defect found, and it was invisible before Phase 1 |
| G11 | Beats persistence / drift / seasonal-naive out of sample | ⛔ **Blocked — B1.** No real data. The harness that would decide it is built, has nine opponents rather than three, applies costs, and has been shown capable of reporting a loss — the native model currently loses to AR(3) on synthetic data |
| G12 | Beats the same strategy without it | ⛔ **Blocked — B1** |
| G13 | Native vs Kronos under one matched harness | ⛔ **Blocked — B1, B2.** Kronos weights unreachable |
| G14 | Complexity earns its place (parsimony) | ⛔ Depends on G11. Now measurable: `ForecastScore.parameters` is reported beside every result and `RepresentationResult.error_per_parameter` scales reconstruction error by size, so the largest model cannot win by being largest |
| G15 | Cannot promote itself | ✅ **Met by construction** — `capabilities.promote()` refuses an autonomous actor today |
| G16 | Safety kernel unreachable from `native/` | ✅ **Met.** All sixteen native modules are in `kernel.EVOLUTION_MODULES` and `audit_evolution_modules()` returns zero findings. The model produces forecasts and nothing else — it cannot submit an order, change a limit, reach a credential or promote itself, and `benchmark.run_benchmark` returns numbers rather than decisions |
| G17 | Deterioration detected and acted on | ✅ **Mechanism met** — `drift.DeteriorationMonitor` demotes autonomously today; unexercised on a native model |

**Score: 11 met, 2 partial, 4 blocked, 0 vacuous — unchanged by P3 and by
Phase 1.**

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
| **P4–P7** | ⛔ Yes | — |
| **P8 — Continuous learning wiring** | Partly | The governance wiring can be built and tested; the learning it governs cannot run |

**Recommendation.** Two pieces of unblocked work are now worth more than any
further architecture.

1. **Fix the calibration defect Phase 1 found.** Coverage 1.000 against a
   nominal 0.800 is a real defect in a real component, found by a real
   measurement, and it does not need market data to fix — a conformal
   calibration layer (architecture doc §3.9) is the designed answer and G9 is
   still only partial. This is the rare case where synthetic data is a
   legitimate test bed, because a badly-calibrated interval is badly calibrated
   whatever the series.
2. **The liquidity / execution-cost head** (§3.8), because `outcomes.py` already
   records real fills, fees and slippage from the paper broker. It remains the
   only component in the system whose training data was not invented for a test.

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
