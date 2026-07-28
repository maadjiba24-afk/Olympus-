# Olympus ↔ Kronos Dependency Map

Every place Kronos touches Olympus, what kind of dependency it is, and what has
to happen for it to become optional.

- **Surveyed at:** `e8380c6` on `claude/kronos-technical-teardown-54pjna`
- **Method:** case-insensitive search across `*.py`, `*.toml`, `*.md`; then
  read of every hit. At `e8380c6`, **40 files** mention Kronos: 15 Olympus
  modules, 19 test files, 5 documents, `pyproject.toml`. (This document and its
  two companions add three more.) Counts below are measured, not estimated.
- **Kronos licence:** MIT, © 2025 ShiYu (`docs/KRONOS_TEARDOWN.md` line 10)

> **The licence is not the constraint.** MIT permits copying Kronos source into
> Olympus with attribution. This project forbids it anyway. Every rule in this
> document is stricter than the licence requires, because the goal is
> independence of *design*, not compliance.

---

## 1. Summary

Those 40 files fall into four classes, and only the first two represent actual
coupling:

| Class | Files | What it means | Removable? |
|---|---|---|---|
| **A — Kronos implementation** | 2 modules, 3 test files | Code that exists only to drive Kronos | Yes: delete the module, delete the registry entry |
| **B — Kronos-shaped Olympus code** | 5 files | Olympus code whose *names or defaults* assume Kronos | Yes: generalise the name, keep the behaviour |
| **C — Comparison scaffolding** | 2 modules | Machinery for judging Kronos, useful for judging anything | Should stay; generalise the vocabulary |
| **D — Citations** | 5 docs + 8 modules' comments | References to teardown findings that justify an Olympus design choice | **Should stay.** These are provenance, not coupling |

Class D is the largest by count and the least important. `features.py` cites
`KRONOS_TEARDOWN §12.9` five times (verified) because that defect is *why*
`causal_window_normalise` exists. Removing the citation would remove the reason,
not the dependency.

---

## 2. Class A — Kronos implementation

Code that has no purpose without Kronos. This is the whole of the real coupling.

| File | Lines | Contents |
|---|---|---|
| `olympus/trading/kronos_runtime.py` | 1,090 | Checkpoint pins, `ModelBackend` ABC, torch/numpy loaders, HF hub access, device selection, tokenizer↔predictor compatibility, calendar-stamp construction |
| `olympus/trading/kronos_adapter.py` | 892 | `KronosConfig`, `KronosForecaster`, instance normalisation, bar repair, sampling parameters |
| `tests/test_trading_kronos_runtime.py` | 558 | — |
| `tests/test_trading_kronos_adapter.py` | 840 | — |
| `tests/test_trading_kronos_defects.py` | 343 | Named regression per teardown defect |

### Kronos-imposed constants that must not leak into a native model

These exist because the pretrained weights require them. A native model that
inherited any of them would be a Kronos derivative wearing Olympus names:

| Constant | Where | Why it is Kronos's, not ours |
|---|---|---|
| `KRONOS_FEATURES` — the 6-column OHLCV order | `kronos_runtime.py:94` | Fixed by `d_in == 6` in the pretrained weights. Not a design choice we made |
| `TEMPORAL_FEATURES` — 5 calendar features | `kronos_runtime.py:99` | The exact set `TemporalEmbedding` expects |
| `s1_bits` / `s2_bits` symmetry rule | `kronos_runtime.py:265` | A property of BSQ's hierarchical vocabulary |
| `_STD_EPSILON = 1e-5` | `kronos_adapter.py:123` | Deliberately identical to upstream `model/kronos.py:546` — *"a different epsilon is a different input distribution"* |
| `max_context` per checkpoint | `kronos_runtime.py:386-420` | Architecture of the released weights |

**Rule for the native work:** none of these five may appear in
`olympus/trading/native/`. A native encoder that happens to use six OHLCV
columns must arrive there from its own argument, recorded in its own design
note, and must be free to use a different set.

### The boundary that already exists

`kronos_runtime.ModelBackend` (line 758) is an ABC exposing exactly one
operation — return *n* plausible futures in normalised space. Its docstring is
explicit that tokens are deliberately not exposed:

> *"Exposing tokens across this boundary would put Kronos's internal vocabulary
> into Olympus's forecasting layer."*

This is the single most useful thing in the current design for the native work:
Kronos's vocabulary already does not reach Olympus. But note the ABC lives in
`kronos_runtime.py` and is named for a *sampling* backend. The native model does
not sample paths as its primary output, so it should implement
`forecast.Forecaster` directly rather than `ModelBackend`. See §5.

---

## 3. Class B — Kronos-shaped Olympus code

Olympus-owned logic whose identifiers assume Kronos is *the* model. Each is a
rename plus a default change; none requires new behaviour.

| Symbol | Location | Problem | Target |
|---|---|---|---|
| `KronosSignalGenerator` | `signals.py:256` | Reads whatever forecaster is registered, but is named for one | `ForecastSignalGenerator(forecast_name=…)` |
| `forecast_name: str = "kronos"` | `signals.py:286` | Default binds the signal layer to a model name | No default; the caller names the forecaster |
| `KronosMomentumStrategy` | `strategy.py:639` | The logic is "trade a forecast's directional view" — nothing Kronos-specific in the body | `ForecastMomentumStrategy` |
| `id = "kronos-momentum"` | `strategy.py:662` | Strategy id, which is persisted in `StrategyRecord` and the audit trail | New id. **Not a free rename** — see §6 |
| `source="kronos"` | `strategy.py:709` | Signal provenance string | The forecaster's registered name |
| `reason_codes=("KRONOS_FORECAST_DIRECTIONAL",)` | `strategy.py:725` | Stable reason code in the audit trail | `FORECAST_DIRECTIONAL`. **Not a free rename** — see §6 |
| `KronosForecastOutput` | `agents.py:392` | Agent output schema; fields are generic (direction, confidence, horizon) | `ForecastAgentOutput` |
| `key="kronos_forecast"` | `agents.py:751` | Agent registry key | `forecast` |
| `kronos = [torch, numpy, einops, huggingface_hub, safetensors, tqdm]` | `pyproject.toml:57` | The extra is named for the model | Keep as-is; add a separate `native` extra (§7) |
| `"kronos_adapter"`, `"kronos_runtime"` | `__init__.py:64-65` | Lazy-import registry entries | Keep while Kronos is a benchmark |

---

## 4. Class C — comparison scaffolding

Machinery built to answer *"does Kronos earn its place?"*. The question
generalises; the vocabulary does not.

| Symbol | Location | Assessment |
|---|---|---|
| `StrategyComparison.kronos` | `evaluate.py:830` | The field is "the arm under test". Rename to `candidate` |
| `run_strategy_comparison(kronos_strategy=…)` | `evaluate.py:949` | Rename the parameter to `candidate_strategy` |
| `kronos_verdict()` | `evaluate.py:1009` | Nine criteria, none Kronos-specific. Rename to `value_verdict()` |
| `kronos_is_valuable()` | `evaluate.py:1096` | Rename to `model_is_valuable()`; **keep the `False` default** |
| `hypotheses.kronos_conditional_value()` | `hypotheses.py:511` | A standing research hypothesis *about Kronos*. Correctly Kronos-specific — **keep**, and add a mirror hypothesis about the native model |
| `STANDING_HYPOTHESES` | `hypotheses.py:571` | Add the native-model hypothesis alongside |

The nine criteria in `kronos_verdict` — matched costs, matched limits, both arms
traded, out-of-sample, minimum paired observations, significance — are exactly
what a native-vs-Kronos comparison needs. **This code is an asset for the native
work, not an obstacle to it.** Generalising it is the first implementation step,
because a native model must be judged by the same function that judges Kronos,
or the comparison proves nothing.

---

## 5. What Olympus already owns, model-agnostically

Verified by inspection: **`torch` and `numpy` are imported in exactly one file
in the entire repository** (`kronos_runtime.py`). Everything below is pure
stdlib and has no idea Kronos exists.

| Layer | Modules | Reusable by a native model? |
|---|---|---|
| Contracts | `contracts.py` — `Candle`, `ForecastResult`, `ForecastPath`, `Signal`, `TradeIntent`, `RiskDecision` | **Directly.** `ForecastResult` already carries `model_version`, `model_identity`, `inference_params`, `quantiles`, `uncertainty`, `abstained` — it was designed for any model |
| Forecaster interface | `forecast.Forecaster` ABC (line 154) + `BaselineForecaster` / `Persistence` / `Drift` / `SeasonalNaive` | **Directly.** This is the plug point. The baselines are the native model's first opponents |
| Service | `forecast.ForecastService`, `ForecastCache` | Directly |
| Features | `features.py` — 15 causal features, `FeatureBuilder`, `ScalerStats`, `fit_scaler`, `split_by_time`, `assert_causal`, `causal_window_normalise` | **Directly, and this is the leakage defence.** A native training pipeline that fits a scaler on the full sample would reproduce teardown §12.9; `assert_causal` is the test that catches it |
| Market analysis | `regime.py`, `volatility.py` (6 estimators), `ta.py`, `candles.py`, `instruments.py` | Directly. `regime.RegimeClassifier` can act as a **weak-supervision teacher** for a native regime head — Olympus-owned labels, no licensing question |
| Data | `storage.CandleStore`, `validate.py`, `ingest.py` | Directly. `CandleStore` is the training corpus store |
| Evaluation | `evaluate.py` metrics (MAE/RMSE/MAPE/sMAPE/directional/pinball/CRPS/coverage), `paired_bootstrap`, `sign_test`, `compare_to_baseline` | Directly. Pinball loss and coverage are exactly the metrics a quantile model needs |
| Backtesting | `backtest.py`, `WalkForward`, `perf.py` | Directly |
| Governance | `registry.py`, `capabilities.py`, `champion.py`, `governance.py`, `kernel.py`, `lab.py`, `drift.py`, `outcomes.py`, `rollback.py`, `evolution.py` | **Directly, and non-negotiably.** A native checkpoint is a capability like any other |
| Safety | `risk.py`, `killswitch.py`, `oms.py`, `execution.py`, `modes.py`, `audit.py` | Untouched. The native model is upstream of all of it |

`champion.py` is already model-neutral: `Contender`, `EvaluationHarness`,
`compare()`. It raises when two arms were measured under different data, costs
or risk limits — which is precisely the guarantee a native-vs-Kronos claim needs.

---

## 6. Renames that are not free

Three identifiers are persisted in append-only records and cannot simply change:

1. **`reason_codes=("KRONOS_FORECAST_DIRECTIONAL",)`** — reason codes are
   documented as *"permanently stable once shipped"* (`errors.py:18`). Existing
   audit entries carry it. Adding `FORECAST_DIRECTIONAL` and retiring the old
   code is correct; rewriting history is not, and `governance.Action.
   REWRITE_AUDIT_HISTORY` is prohibited to every actor.

2. **`id = "kronos-momentum"`** — persisted in `StrategyRecord` via
   `StrategyManager`. A new id is a new strategy with a fresh performance
   history, which is the honest outcome: a renamed strategy has not inherited
   the old one's track record.

3. **Model registry ids** — `registry.ModelRecord` pins revisions and approval
   state. A native checkpoint is a **new record**, never an edit of the Kronos
   one.

---

## 7. Removal map

Kronos is not to be deleted. It is to become **one registered provider among
several**, with nothing depending on it by default.

| Stage | Change | Result |
|---|---|---|
| **R1 — Generalise the vocabulary** | Class B renames + Class C renames, with the three constrained identifiers handled per §6 | No Olympus module has "kronos" in a public symbol name. Kronos modules unchanged |
| **R2 — Add the native plug point** | `olympus/trading/native/` implementing `forecast.Forecaster`. New `native` extra in `pyproject.toml`, separate from `kronos` | Two forecasters exist behind one interface |
| **R3 — Default off Kronos** | No Olympus default names `"kronos"`. `ForecastService` registers whatever the operator configures | Deleting `kronos_adapter.py` breaks no Olympus module — only its own tests |
| **R4 — Matched comparison** | Native and Kronos judged by the generalised `value_verdict()` under one `EvaluationHarness` | A supportable answer to "which is better", or an honest "indistinguishable" |
| **R5 — Reclassify** | Kronos capability record moves to `external_benchmark`; native record enters the ladder at `proposed` | Kronos is a benchmark, a challenger, or an ensemble member — never the core |

### The structural test for R3

`kernel.py` already proves a property of this shape for the self-evolution
modules. The same technique applies:

> Parse every module under `olympus/trading/` **except** `kronos_adapter.py`,
> `kronos_runtime.py` and their tests. Fail if any imports either module or
> contains the literal string `"kronos"` in a non-comment string constant.

That test is the machine-checkable definition of "Olympus does not depend on
Kronos", and it should be written **before** the native model, so the removal is
verified independently of whether the native model turns out to be any good.

---

## 8. What must never happen

Enumerated so a reviewer can check the diff against a list rather than a feeling.

| Forbidden | Detectable by |
|---|---|
| Copying Kronos source into `native/` | Kronos is MIT and public; a diff against the upstream tree at `67b630e` settles it |
| A native module importing `kronos_adapter` or `kronos_runtime` | The AST test in §7 |
| Reusing `KRONOS_FEATURES`, `TEMPORAL_FEATURES`, the bit-symmetry rule, `_STD_EPSILON`, or a `max_context` value | Constant-name and value scan of `native/` |
| Initialising native weights from a Kronos checkpoint | The trainer accepts random init or an Olympus checkpoint manifest, nothing else — enforced in code and tested |
| Presenting Kronos weights as Olympus-owned | Every checkpoint carries a training manifest naming its data, seed, config and code commit. No manifest, no claim |
| A BSQ tokenizer with different names | Design note per component stating the mechanism and why it was chosen; a discrete hierarchical codebook over OHLCV would need an independent justification that does not exist |
| Claiming superiority without matched evaluation | `champion.compare()` raises on a mismatched harness |
| Claiming independence because an adapter wraps Kronos | This document; §2 lists the coupling that an adapter does not remove |

---

## 9. Open question for the operator

**Is Kronos permitted as a teacher?** The repository is MIT, but distillation
uses the *weights*, and the weights are distributed separately on Hugging Face
under terms this environment cannot read — `huggingface.co` returns 403 at
CONNECT (`docs/TRADING_EXTERNAL_VALIDATION.md` §1).

Until someone reads the model card and confirms the weight licence permits
derivative models, **distillation from Kronos is blocked** and the native model
must be trained from data alone. This is recorded as blocker **B4** in
`docs/OLYMPUS_NATIVE_MARKET_INTELLIGENCE.md` §7.

Note that the honest engineering answer may be "we do not want a teacher
anyway": the only public evidence about Kronos's skill is negative (teardown
§16; upstream issues #354/#355 report Kronos-mini underperforming a persistence
baseline). Distilling a model that may not beat persistence would transfer its
errors along with its behaviour.
