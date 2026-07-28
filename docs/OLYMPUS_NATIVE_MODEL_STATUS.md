# Olympus Native Model — Implementation Status

**The honest ledger for the native forecasting work.**
`docs/OLYMPUS_NATIVE_MARKET_INTELLIGENCE.md` describes the *target*; this file
records what actually exists. When they disagree, this file is right.

- **Last updated:** 2026-07-28
- **Branch:** `claude/kronos-technical-teardown-54pjna`
- **Commit surveyed:** `e8380c6`; **P1 (decouple) and P2 (skeleton) complete**

---

## The one-line summary

> **Olympus owns no trained market model.** The native package exists and
> works: it builds market states, splits data without leakage, fits a
> conditional quantile estimator, writes provenanced checkpoints, and serves
> forecasts through the standard interface. It has been fitted **only to
> synthetic series in tests**, because no market data is reachable (B1). It is
> plumbing that works, not a model that knows anything.

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
| Market-state representation | `native/state.py` | ✅ | 336 lines. Typed, causal (a scale closing after `as_of` raises), multi-scale, missing-is-missing. Reuses `features`, `regime`, `volatility` |
| Encoder | `native/encoder.py` | 🔵 | Continuous multi-scale, no codebook. **Not built** |
| Temporal architecture | `native/trunk.py` | 🔵 | Non-autoregressive. **Not built** |
| Quantile head | `native/quantile.py` | 🟡 | 493 lines. Direct multi-horizon conditional quantiles — the right *shape*, learned by a lookup table rather than a network. Declines on a thin cell, an out-of-range input or a missing feature |
| Multi-timeframe | `native/state.py` | 🟡 | The state carries one `ScaleObservation` per timeframe and single-scale is the degenerate case; **no model consumes more than the base scale yet** |
| Cross-asset | `native/trunk.py` | 🔵 | Blocked in practice by B1 — needs a multi-instrument corpus |
| Regime head | `native/regime.py` | 🔵 | Weak supervision from `regime.RegimeClassifier` (Olympus-owned labels) |
| Volatility head | `native/vol.py` | 🔵 | Supervised against six existing estimators |
| Conformal calibration | `native/conformal.py` | 🔵 | Distribution-free coverage |
| Liquidity / execution cost | `native/liquidity.py` | 🔵 | Best data situation: `outcomes.py` already records fills, fees, slippage |
| Event awareness | `native/events.py` | 🔵 | Event *timing* only; claims stay untrusted per `knowledge.py` |
| Portfolio-aware evaluation | `native/portfolio_eval.py` | 🔵 | Produces evidence, never sizing |
| Abstention / OOD | `native/quantile.py` | 🟡 | Range-based OOD and thin-cell abstention, feeding the existing `ForecastResult.abstained`. **Not** the conformal detector §3.11 describes |
| Dataset / windowing | `native/data.py` | ✅ | 324 lines. Horizon-inside-input refused on timestamps; embargoed split that cannot be set below the horizon |
| Trainer | `native/train.py` | ✅ | 217 lines. One function, in the one safe order; splits before any statistic is computed |
| Checkpoint format | `native/checkpoint.py` | ✅ | 407 lines. Manifest required at construction; foreign origins refused by shape; append-only store with content-hash verification |
| Evaluation driver | `native/train.evaluate_on_split` | 🟡 | Drives `evaluate.py`'s metrics and reports abstentions separately. `WalkForward` integration not yet wired |
| `Forecaster` implementation | `native/forecaster.py` | ✅ | 330 lines. Registers beside the baselines; nothing downstream learns it exists |

**Native modules built: 7 files, 2,173 lines, 67 tests.** Of the eighteen
components designed, **six are built, four partial, eight untouched** — every
untouched one is neural or needs data.

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

**What is still genuinely missing is the neural work.** `torch` and `numpy` are
still imported in exactly one file in the repository — `kronos_runtime.py` — and
the native package is pure stdlib, so `tests/test_deps_claim.py` stays green.
There is now a dataset, a checkpoint format and a training pipeline; there is no
optimiser, no encoder, no trunk, and no learned weight beyond a lookup table.

---

## 3. Completion gates — current state

Gate definitions in `docs/OLYMPUS_NATIVE_MARKET_INTELLIGENCE.md` §6.

| # | Gate | State |
|---|---|---|
| G1 | No Olympus module outside the Kronos files references Kronos | ✅ **Met.** `tests/test_trading_independence.py`: no import, no identifier, no runtime string. One enumerated exemption (`__init__.py`'s lazy-import table) |
| G2 | No native module imports Kronos or a Kronos constant | ✅ **Met.** No import, identifier or runtime string; no Kronos-imposed constant; prose mentions confined to module docstrings; no module-scope torch |
| G3 | Native weights never initialised from foreign weights | ✅ **Met.** `assert_olympus_origin` refuses a URL, a path, a weights file or a hub id by shape, and an unknown id when the store is consulted |
| G4 | Design note per component | ✅ **Met** — all twelve in architecture doc §3 |
| G5 | Deleting the Kronos modules breaks no Olympus module | ✅ **Met, executed.** A subprocess blocks both modules at import and every other trading module still imports |
| G6 | No look-ahead in the training pipeline | ✅ **Met.** Horizon-inside-input refused on timestamps; embargo cannot be set below the horizon; split happens before any statistic; state causality raises |
| G7 | Training reproducible from a seed | ✅ **Met** for this estimator: identical rows produce byte-identical parameters, asserted at both the estimator and pipeline level |
| G8 | Every checkpoint carries a complete manifest | ✅ **Met.** Required at construction; missing reproducibility fields reported in `manifest.gaps` rather than hidden |
| G9 | Abstains outside the training manifold | 🟡 **Partial.** Range-based OOD works and is tested; the conformal nonconformity detector is not built |
| G10 | Uncertainty calibrated within ±5 coverage points | ⛔ **Blocked — B1.** Uncertainty is computed and bounded; coverage cannot be measured without real out-of-sample data |
| G11 | Beats persistence / drift / seasonal-naive out of sample | ⛔ **Blocked — B1.** No real data |
| G12 | Beats the same strategy without it | ⛔ **Blocked — B1** |
| G13 | Native vs Kronos under one matched harness | ⛔ **Blocked — B1, B2.** Kronos weights unreachable |
| G14 | Complexity earns its place (parsimony) | ⛔ Depends on G11 |
| G15 | Cannot promote itself | ✅ **Met by construction** — `capabilities.promote()` refuses an autonomous actor today |
| G16 | Safety kernel unreachable from `native/` | ✅ **Met.** All seven native modules are in `kernel.EVOLUTION_MODULES` and the audit is clean |
| G17 | Deterioration detected and acted on | ✅ **Mechanism met** — `drift.DeteriorationMonitor` demotes autonomously today; unexercised on a native model |

**Score: 11 met, 2 partial, 4 blocked, 0 vacuous.**

P1 closed G1 and G5; P2 closed G2, G3, G6, G7, G8 and G16, and moved G9 to
partial and G10 from vacuous to blocked. Every remaining gate is now either
*measured* or *blocked on data* — none is vacuous, which is the useful thing
P2 changed.

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
| **P1 — Decouple from Kronos** | ✅ **Done** | Closed G1 and G5. 14 independence tests; 2444 trading tests green; no native code written |
| **P2 — Native skeleton** | ✅ **Done** | Closed G2, G3, G6, G7, G8, G16. 2,173 lines, 67 tests. The estimator is real, not a stub — it can beat a baseline or fail to, which is what makes the comparison worth running |
| **P3 — Learning on synthetic data** | No | Low-moderate. Proves the training pipeline recovers known structure. Closes G6, G7. **Proves nothing about markets** and must not be reported as if it did |
| **P4–P7** | ⛔ Yes | — |
| **P8 — Continuous learning wiring** | Partly | The governance wiring can be built and tested; the learning it governs cannot run |

**Recommendation.** P1 and P2 are done. P3 (learning on synthetic data) is
unblocked but of limited value — it would prove a *training loop* converges,
which the current pipeline already demonstrates for a model that has no loop.
The higher-value unblocked work is the liquidity/execution-cost head (§3.8),
because `outcomes.py` already records real fills, fees and slippage from the
paper broker: that is the one component with data available today.

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
