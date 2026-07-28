# Olympus Native Model — Implementation Status

**The honest ledger for the native forecasting work.**
`docs/OLYMPUS_NATIVE_MARKET_INTELLIGENCE.md` describes the *target*; this file
records what actually exists. When they disagree, this file is right.

- **Last updated:** 2026-07-28
- **Branch:** `claude/kronos-technical-teardown-54pjna`
- **Commit surveyed:** `e8380c6`

---

## The one-line summary

> **Olympus owns no trained market model.** No native module exists, no
> checkpoint exists, no training run has been performed, and no evaluation
> against any model has been attempted. What exists is a design and a
> dependency map.

This line should not change until §3's gates start passing, and the phrase
"Olympus owns a Kronos-class model" must not appear anywhere until **G7, G8,
G11 and G13** have all passed on real data.

---

## 1. Status legend

| Mark | Meaning |
|---|---|
| ✅ | Built, tested, and the tests exercise the adversarial cases |
| 🟡 | Built; happy path tested, adversarial surface thin |
| 🔵 | Designed; **no code** |
| ⛔ | Cannot be built or demonstrated here; reason stated |

---

## 2. Component status

### The native system

| Component | Module (planned) | Status | Notes |
|---|---|---|---|
| Market-state representation | `native/state.py` | 🔵 | Designed. Reuses `features`, `regime`, `volatility` — all of which exist and are tested |
| Encoder | `native/encoder.py` | 🔵 | Continuous multi-scale, no codebook |
| Temporal architecture | `native/trunk.py` | 🔵 | Non-autoregressive |
| Quantile head | `native/quantile.py` | 🔵 | Pinball loss; `evaluate.pinball_loss` already exists |
| Multi-timeframe | `native/encoder.py` | 🔵 | `instruments.py` owns the alignment grid |
| Cross-asset | `native/trunk.py` | 🔵 | Blocked in practice by B1 — needs a multi-instrument corpus |
| Regime head | `native/regime.py` | 🔵 | Weak supervision from `regime.RegimeClassifier` (Olympus-owned labels) |
| Volatility head | `native/vol.py` | 🔵 | Supervised against six existing estimators |
| Conformal calibration | `native/conformal.py` | 🔵 | Distribution-free coverage |
| Liquidity / execution cost | `native/liquidity.py` | 🔵 | Best data situation: `outcomes.py` already records fills, fees, slippage |
| Event awareness | `native/events.py` | 🔵 | Event *timing* only; claims stay untrusted per `knowledge.py` |
| Portfolio-aware evaluation | `native/portfolio_eval.py` | 🔵 | Produces evidence, never sizing |
| Abstention / OOD | `native/ood.py` | 🔵 | Feeds the existing `ForecastResult.abstained` |
| Dataset / windowing | `native/data.py` | 🔵 | Strict temporal split; scalers train-only |
| Trainer | `native/train.py` | 🔵 | Deterministic; writes a manifest |
| Checkpoint format | `native/checkpoint.py` | 🔵 | Manifest-bearing; no foreign-weight init path |
| Evaluation driver | `native/eval.py` | 🔵 | Drives existing `evaluate.py` + `WalkForward` |
| `Forecaster` implementation | `native/forecaster.py` | 🔵 | The plug point into `ForecastService` |

**Native modules built: 0 of 18.**

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

**The genuinely missing thing is machine learning.** `torch` and `numpy` are
imported in exactly one file in the repository — `kronos_runtime.py`. There is
no dataset, no training loop, no optimiser, no checkpoint format, and no trained
weight of any kind.

---

## 3. Completion gates — current state

Gate definitions in `docs/OLYMPUS_NATIVE_MARKET_INTELLIGENCE.md` §6.

| # | Gate | State |
|---|---|---|
| G1 | No Olympus module outside the Kronos files references Kronos | ❌ **Not met.** 6 modules carry Kronos-named symbols (dependency map §3) |
| G2 | No native module imports Kronos or a Kronos constant | ➖ Vacuous — no native module exists |
| G3 | Native weights never initialised from foreign weights | ➖ Vacuous — no weights exist |
| G4 | Design note per component | ✅ **Met** — all twelve in architecture doc §3 |
| G5 | Deleting the Kronos modules breaks no Olympus module | ❌ **Not met** — `signals`, `strategy`, `agents`, `evaluate` carry Kronos-named symbols |
| G6 | No look-ahead in the training pipeline | ➖ No pipeline. `features.assert_causal` exists and is tested |
| G7 | Training reproducible from a seed | ➖ No trainer |
| G8 | Every checkpoint carries a complete manifest | ➖ No checkpoints |
| G9 | Abstains outside the training manifold | ➖ No model |
| G10 | Uncertainty calibrated within ±5 coverage points | ➖ No model |
| G11 | Beats persistence / drift / seasonal-naive out of sample | ⛔ **Blocked — B1.** No real data |
| G12 | Beats the same strategy without it | ⛔ **Blocked — B1** |
| G13 | Native vs Kronos under one matched harness | ⛔ **Blocked — B1, B2.** Kronos weights unreachable |
| G14 | Complexity earns its place (parsimony) | ⛔ Depends on G11 |
| G15 | Cannot promote itself | ✅ **Met by construction** — `capabilities.promote()` refuses an autonomous actor today |
| G16 | Safety kernel unreachable from `native/` | ➖ Vacuous until `native/` exists; the mechanism (`kernel.audit_evolution_modules`) is built and tested |
| G17 | Deterioration detected and acted on | ✅ **Mechanism met** — `drift.DeteriorationMonitor` demotes autonomously today; unexercised on a native model |

**Score: 3 met, 2 not met, 4 blocked, 8 vacuous.**

The three met gates are all *governance* gates that were already true before
this work started. **No value gate has been attempted.**

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
| **P1 — Decouple from Kronos** | No | High. Independence from Kronos is achievable *today* and is worth having whether or not a native model ever trains. Closes G1 and G5 |
| **P2 — Native skeleton, no learning** | No | Moderate. Establishes `MarketState`, the dataset windowing, the checkpoint manifest and the `Forecaster` plug point using a deterministic statistical model. Closes G8's mechanism and G16 |
| **P3 — Learning on synthetic data** | No | Low-moderate. Proves the training pipeline recovers known structure. Closes G6, G7. **Proves nothing about markets** and must not be reported as if it did |
| **P4–P7** | ⛔ Yes | — |
| **P8 — Continuous learning wiring** | Partly | The governance wiring can be built and tested; the learning it governs cannot run |

**Recommendation.** Do P1 first and completely. It removes a real dependency, is
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
# no ML anywhere except the Kronos backend
grep -rln "import torch\|import numpy" --include='*.py' olympus/ tests/

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
