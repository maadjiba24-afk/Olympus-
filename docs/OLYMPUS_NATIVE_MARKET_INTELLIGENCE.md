# Olympus Native Market Intelligence — Architecture and Plan

The design for an Olympus-owned forecasting system that does not depend on
Kronos, and the measurable gates it must pass before anyone may say it works.

- **Status:** design + **P1 (decouple) complete**. No native model code exists.
- **Companion documents:** `docs/OLYMPUS_KRONOS_DEPENDENCY_MAP.md` (what couples
  us to Kronos today), `docs/OLYMPUS_NATIVE_MODEL_STATUS.md` (the honest ledger)
- **Surveyed at:** `e8380c6`

> **The hard part is not the architecture.** Any competent design will do; the
> binding constraints are *data* and *compute*, and this environment has neither.
> §7 states both as hard blockers rather than burying them, because a plan that
> pretends otherwise would produce a model trained on nothing — which is worse
> than no model, since it would look like one.

---

## 1. Current-state architecture map

```
 ingest ─► validate ─► storage ─────┐
   │          │                     │
   │          ▼                     ▼
   │      features ──────────► FORECASTING LAYER ────► signals ─► strategy
   │      regime                     │                              │
   │      volatility                 │                              ▼
   │                                 │                       ┌─── risk.py ───┐
   │                                 │                       │ deterministic │
   │                                 │                       └───────┬───────┘
   │                                 │                               ▼
   └──────────────────────────► audit ledger ◄──────── oms ─► execution ─► broker

 FORECASTING LAYER, expanded:

   forecast.Forecaster  (ABC — the plug point)
      ├── PersistenceForecaster      Olympus, stdlib
      ├── DriftForecaster            Olympus, stdlib
      ├── SeasonalNaiveForecaster    Olympus, stdlib
      └── KronosForecaster           ── kronos_adapter.py ──► ModelBackend ABC
                                                                   │
                                                        kronos_runtime.py
                                                        torch · numpy · HF hub
                                                        (the ONLY torch in the repo)
```

**Where the intelligence actually is today.** Three trivial statistical
baselines and one adapter to somebody else's weights. Everything else —
validation, features, regime, volatility, signals, strategy, risk, execution,
governance, self-evolution — is Olympus-owned and model-agnostic, and none of it
imports torch.

That is the accurate summary of the situation: **Olympus owns a great deal of
trading infrastructure and no market model at all.**

---

## 2. Target-state architecture map

```
                    ┌──────────── olympus/trading/native/ ────────────┐
                    │                                                 │
 storage ──────────►│  state.py      MarketState — typed, causal,      │
 features           │                multi-scale observation record    │
 regime  ──────────►│      │                                           │
 volatility         │      ▼                                           │
                    │  encoder.py    Continuous multi-scale encoder     │
                    │      │         (no discrete vocabulary)           │
                    │      ▼                                           │
                    │  trunk.py      Temporal mixer + cross-sectional   │
                    │      │         attention over the universe        │
                    │      ├──────────────┬──────────┬──────────┐      │
                    │      ▼              ▼          ▼          ▼      │
                    │  quantile.py    regime.py   vol.py    liquidity.py│
                    │  (multi-horizon) (latent)  (σ + calib) (spread,   │
                    │      │                                  slippage) │
                    │      ▼                                           │
                    │  conformal.py   Distribution-free coverage        │
                    │      │                                           │
                    │  paths.py       Optional residual-bootstrap paths │
                    │      │                                           │
                    │  ood.py         Abstain outside the training      │
                    │      │          manifold                          │
                    │      ▼                                           │
                    │  forecaster.py  implements forecast.Forecaster    │
                    └───────────────────────┬─────────────────────────┘
                                            │
                    ┌───────────────────────┴─────────────────────────┐
                    │  data.py · train.py · checkpoint.py · eval.py    │
                    │  Olympus-owned training pipeline                 │
                    └─────────────────────────────────────────────────┘

 forecast.ForecastService registers, side by side and with no default:
   persistence · drift · seasonal-naive · olympus-native · kronos (benchmark)
```

Everything to the right of `forecast.Forecaster` is unchanged. The native system
is a peer of the baselines, judged by the same evaluator, gated by the same
capability ladder.

### Package naming

`olympus/trading/native/` with functional module names — `encoder`, `trunk`,
`quantile`, not a new mythological brand. A distinct brand name would be the
first thing a reader suspected of being a rename. Descriptive names make the
independence claim checkable rather than rhetorical.

---

## 3. The twelve components, and why each is designed the way it is

The requirement is an *independently designed* system. Cosmetic difference is
not independence, so each choice below carries the engineering reason it was
made, and several are deliberately opposite to Kronos's choice.

### 3.1 Market-state representation — `state.py`

A typed `MarketState` record: OHLCV at several timeframes, a causal feature
vector from `features.py`, a regime label from `regime.py`, volatility
estimates from `volatility.py`, session/calendar context from `instruments.py`,
and a data-quality stamp.

**Why not Kronos's shape.** Kronos consumes a fixed 6-column OHLCV matrix plus
five calendar integers, because `d_in == 6` is baked into its weights. Olympus
has a richer, already-tested feature layer; a representation that discarded it
to match somebody else's input arity would be a worse representation chosen for
compatibility.

**Invariant:** every field is computable from bars closing at or before
`as_of`. Enforced by `features.assert_causal`, which already exists and already
catches the teardown's §12.9 leakage in miniature.

### 3.2 Encoder — `encoder.py`

A **continuous** multi-scale patch encoder. Non-overlapping windows of bars are
projected to a latent vector per scale; no codebook, no vocabulary, no
quantisation.

**Why continuous rather than tokenised.** Three reasons, in order of weight:

1. **Quantisation imposes an error floor.** A discrete codebook cannot represent
   a price move finer than its cell. For a forecast whose downstream consumer is
   a risk engine reasoning about basis points, that floor is a design cost with
   no matching benefit — Olympus is not doing text-style generation.
2. **It removes the class of defect the teardown found.** §12.2 is a silent
   corruption caused by asymmetric bit-splits in a hierarchical vocabulary. A
   design with no vocabulary cannot have that defect.
3. **It is trainable on the compute we have.** A codebook needs commitment
   losses, entropy penalties and careful balancing; a linear patch projection
   does not.

This is the single most important architectural divergence, and it is a
divergence *in kind*.

### 3.3 Temporal architecture — `trunk.py` + `quantile.py`

**Direct multi-horizon quantile regression**, not autoregressive sampling. One
forward pass emits a set of quantiles for every step of the horizon.

**Why direct rather than autoregressive.** Kronos samples *N* paths
autoregressively and the adapter derives quantiles from the ensemble. That has
three costs: sampling error compounds with horizon, uncertainty is only as good
as the sample count, and inference is *N*× the work. Olympus's consumers —
`risk.py`, `signals.py`, `strategy.py` — read `quantiles`, `uncertainty` and
`expected_return`, and never a single path. Producing exactly what is consumed,
in one pass, is both cheaper and better calibrated.

Trained with **pinball loss**, which `evaluate.pinball_loss` already implements
and tests. The evaluation metric and the training objective are the same
function, which is worth more than it sounds: it makes the reported number the
thing that was optimised.

### 3.4 Multi-timeframe modelling

The encoder consumes aligned windows at several timeframes simultaneously —
e.g. 1m/1h/1d — with a learned per-scale weighting. `instruments.py` already
owns the epoch-anchored timeframe grid that makes the alignment exact.

Kronos is single-series. This is a capability it does not have, not a
reimplementation of one it does.

### 3.5 Cross-asset modelling

A shared trunk with per-instrument embeddings and cross-sectional attention over
the instrument universe at each timestamp, so BTC's move can inform the ETH
forecast.

**Constraint:** the universe must be defined by point-in-time membership.
`backtest.py` already discloses that a static universe cannot have survivorship
bias removed by the engine — a cross-sectional model trained on today's universe
would bake that bias into the weights, which is worse than disclosing it.

### 3.6 Market-regime modelling — `regime.py` head

A discrete latent regime head, **weakly supervised by
`regime.RegimeClassifier`'s rule-based labels**. Those labels are Olympus-owned,
deterministic and already tested, so there is no licensing question and no
teacher dependency.

Rationale for a head rather than a separate model: regime is the conditioning
variable for everything else, and a jointly-trained head shares the trunk's
representation instead of re-deriving it.

### 3.7 Volatility and uncertainty — `vol.py` + `conformal.py`

Two separate things, deliberately separated:

- **Volatility head** — predicts realised volatility over the horizon,
  supervised against `volatility.py`'s estimators (Yang-Zhang, Garman-Klass,
  Parkinson, Rogers-Satchell, realised, EWMA — all six already implemented).
- **Conformal calibration** — a distribution-free wrapper giving a *provable*
  marginal coverage guarantee from a held-out calibration set.

**Why conformal.** A neural quantile head's nominal 90% interval is 90% only if
the model is well specified. Conformal prediction gives the guarantee without
that assumption, at the cost of some interval width. Since Olympus's risk engine
sizes positions from `uncertainty`, an interval that is honestly wide beats one
that is optimistically narrow — and `drift.detect_calibration_drift` already
exists to notice when it stops holding.

### 3.8 Liquidity and execution-cost prediction — `liquidity.py`

A small separate model predicting spread, expected slippage and fill probability
at a given size. **Trained on `outcomes.py` records** — Olympus already stores
realised fills, fees, slippage and execution quality for every decision.

This is the component with the best data situation and the least glamour, and it
is likely the first to produce measurable value: `evaluate` and `outcomes` both
show that costs, not direction, are what most often turn a positive gross edge
into a negative net one.

### 3.9 Event-aware forecasting

An event-context channel (scheduled economic releases, exchange notices, halts)
plus a **hard abstention rule** in a configurable window around known events.

The event data itself arrives through `knowledge.py`, which already treats
external content as untrusted until validated and forbids anything derived from
it reaching risk configuration. The model may condition on an event's *presence
and time*; it may not read an article's *claims* as features.

### 3.10 Portfolio-aware signal evaluation

Scores a candidate signal by its marginal contribution to portfolio risk, using
`portfolio.py`'s live positions and the cross-asset correlation structure from
§3.5 — so a fourth correlated long is scored differently from the first.

**Boundary:** this produces *evidence attached to a signal*. It does not size
the position. Sizing stays in `strategy.py` and authorisation stays in
`risk.py`, whose concentration and correlated-exposure limits are unchanged and
unaware of this component.

### 3.11 Abstention and OOD detection — `ood.py`

Two mechanisms:

- **Nonconformity-based OOD** — the conformal calibration set gives a natural
  score; an input whose score exceeds the calibration distribution's tail is
  out of distribution.
- **Coverage monitoring** — when realised coverage departs from nominal,
  abstain rather than widen silently.

Feeds `ForecastResult.abstained`, which already exists and which `signals.py`
already honours by producing **no** signal rather than a flat one.

**Design position:** abstention must be cheap and frequent. `outcomes.py` scores
every decision against the counterfactual of doing nothing, so a model that
abstains too often will be visible in the record — the risk of over-abstention is
measurable, and the risk of over-confidence is money.

### 3.12 Training and evaluation pipeline — `data.py`, `train.py`, `checkpoint.py`, `eval.py`

- **`data.py`** — windowed datasets from `CandleStore`, with a **strict temporal
  split** and scalers fitted on train only (`features.fit_scaler` +
  `split_by_time` already do this). The single most important property: no
  statistic computed on any bar after the split boundary may touch a training
  input.
- **`train.py`** — the loop. Deterministic given a seed. Writes a **training
  manifest**: data hash, split boundaries, seed, config, code commit, dependency
  versions, and every metric at every epoch.
- **`checkpoint.py`** — an Olympus checkpoint format carrying its manifest.
  Registered in `registry.py` like any other model. **The trainer accepts random
  initialisation or an Olympus checkpoint, and nothing else** — there is no code
  path that initialises from foreign weights, and that is a test, not a policy.
- **`eval.py`** — drives `evaluate.py`'s existing metrics and
  `backtest.WalkForward`. Adds nothing statistical; the metrics already exist and
  are already tested.

---

## 4. Safety boundary — unchanged

The native system sits in exactly the place Kronos sits, and inherits every
constraint that applies there.

| Guarantee | How it survives |
|---|---|
| Deterministic risk engine | `native/` never imports `risk.py`'s mutation surface. Enforceable by the same AST scan `kernel.py` runs |
| Kill switches | Unchanged. A native forecast is upstream of authorisation |
| Order authorisation | Every order still cites an approving `RiskDecision` |
| Broker separation | `native/` never imports `brokers` or `execution` — both are in `kernel.KERNEL_MODULES` |
| Capability boundaries | A native checkpoint enters `capabilities.py` at `proposed` and climbs one rung at a time on recorded evidence |
| Credential protection | `native/` never imports `olympus.vault` |
| Audit ledger | Every forecast, training run and promotion is recorded |
| Live-mode gates | Unchanged; nine gates, operator token, named operator |
| Human promotion | `capabilities.promote()` requires an operator. A model cannot promote itself, and `champion.install()` refuses a challenger that never won a contest |
| Self-evolution governance | The native model is a `Contender` and a `Capability`, subject to `drift.py`'s deterioration ladder like anything else |

**What the native system may do:** produce forecasts, evidence and structured
`TradeIntent`s.

**What it may not do:** submit orders, change risk limits, read broker
credentials, enable live trading, or promote itself.

`olympus/trading/native/` should be added to `kernel.EVOLUTION_MODULES` so the
existing structural audit covers it from the first commit — before there is any
model to be tempted by.

---

## 5. Phased implementation plan

Each phase has an exit condition. A phase whose exit condition is unmet does not
advance, and phases 4–7 are all blocked on external access (§7).

| Phase | Work | Exit condition |
|---|---|---|
| **P0 — Documentation** | These three documents | *(this deliverable)* |
| **P1 — Decouple** ✅ | Class B + C renames per dependency-map §7 R1. `tests/test_trading_independence.py` asserting no Olympus module outside `kronos_*` references Kronos | ✅ **Done.** 14 independence tests pass; G1 and G5 closed; 2444 trading tests green; no native code written |
| **P2 — Native skeleton, no learning** | `native/` package: `MarketState`, dataset windowing, checkpoint format + manifest, a `Forecaster` implementation that is a *deterministic statistical* model (no torch) | Registered in `ForecastService`, produces valid `ForecastResult`s, evaluated against the three baselines. Establishes the plumbing before the modelling |
| **P3 — Learning, offline** | `torch` behind a `native` extra. Encoder + trunk + quantile head. Trainer with manifest, seeding, temporal split | Trains to convergence on **synthetic** series with known structure and recovers that structure. This validates the pipeline, not the market |
| **P4 — Real data** ⛔ | Ingest real bars, build the corpus, train | **BLOCKED — B1.** No provider reachable |
| **P5 — Extended heads** | Regime, volatility, conformal, liquidity, event, OOD | Each head measurably beats the corresponding baseline out of sample |
| **P6 — Champion/challenger** ⛔ | Native vs Kronos under one `EvaluationHarness` | **BLOCKED — B2, B4.** Kronos weights unreachable |
| **P7 — Paper trading** ⛔ | Shadow, then paper | **BLOCKED — B3.** No broker reachable |
| **P8 — Continuous learning** | Wire the native model into `drift.py`, `outcomes.py`, `evolution.py`; scheduled retraining proposals | Retraining is *proposed* autonomously and *approved* by a human — the existing governance split, unchanged |

**P1 and P2 are unblocked and can begin immediately** once this design is
accepted. P3 is unblocked but of limited value: a model that fits synthetic data
proves the pipeline works and nothing about markets.

---

## 6. Completion gates

Measurable, and several are designed to be *failable*. Numbered for citation.

### Independence gates

| # | Gate | Measurement |
|---|---|---|
| **G1** | No Olympus module outside the Kronos files references Kronos | AST scan: no import of `kronos_*`, no `"kronos"` string constant. Test, run in CI |
| **G2** | No native module imports Kronos, torch-via-Kronos, or a Kronos constant | AST scan of `native/` for the five constants in dependency-map §2 |
| **G3** | Native weights have never been initialised from foreign weights | The trainer accepts random init or an Olympus manifest only; a foreign path raises. Test |
| **G4** | Every native component has a written design note stating its mechanism and why it was chosen | Present for all twelve components in §3, reviewable against the Kronos teardown |
| **G5** | Deleting `kronos_adapter.py` and `kronos_runtime.py` breaks no Olympus module | Run the suite with both files removed; only their own tests fail |

### Correctness gates

| # | Gate | Measurement |
|---|---|---|
| **G6** | No look-ahead in the training pipeline | `features.assert_causal` on every feature; a leakage regression test that fails a full-sample scaler |
| **G7** | Training is reproducible | Two runs, same seed and data hash → identical weights hash |
| **G8** | Every checkpoint carries a complete manifest | Data hash, split boundaries, seed, config, code commit, dependency versions. A checkpoint without one cannot be registered |
| **G9** | The model abstains outside its training manifold | OOD test on a deliberately shifted series; abstention rate rises measurably |
| **G10** | Uncertainty is calibrated | Realised coverage of the nominal 90% interval within ±5 points on held-out data |

### Value gates — the ones that can fail

| # | Gate | Measurement |
|---|---|---|
| **G11** | Native beats persistence, drift and seasonal-naive out of sample | Pinball loss and directional accuracy, paired bootstrap p < 0.05 via `evaluate.paired_bootstrap` |
| **G12** | Native beats the same strategy without it | `run_strategy_comparison` on net return, matched costs and limits |
| **G13** | Native vs Kronos is decided under one matched harness | `champion.compare()` — no mismatched-harness exception. **A verdict of "indistinguishable" or "Kronos wins" is an acceptable and publishable result** |
| **G14** | The added complexity earns its place | `champion.compare()`'s parsimony rule: if the native model is not significantly better than a baseline, the *simpler* model wins and the native model is not promoted |

**G13 and G14 exist to be losable.** A native model that ties a persistence
baseline should not be deployed, and the framework already enforces that: the
parsimony tie-break in `champion.compare()` prefers the simpler contender when
the difference is not statistically distinguishable.

### Governance gates

| # | Gate | Measurement |
|---|---|---|
| **G15** | The native model cannot promote itself | `capabilities.promote()` with an autonomous actor raises `GovernanceViolation`. Already true; assert it for the native record |
| **G16** | The safety kernel is unreachable from `native/` | `kernel.audit_evolution_modules()` with `native/` added to `EVOLUTION_MODULES` |
| **G17** | Deterioration is detected and acted on | A native checkpoint whose forecast error rises is demoted by `drift.DeteriorationMonitor` without human action |

---

## 7. Blockers

Stated plainly. Four are hard; two are design constraints.

### B1 — No training data ⛔ **HARD**

Every market-data provider is refused by this environment's egress policy with
403 at CONNECT: Binance, Alpaca, Coinbase, Kraken, Finnhub, Polygon, Yahoo,
Stooq (`docs/TRADING_EXTERNAL_VALIDATION.md` §1). `CandleStore` is empty of real
bars.

A forecasting model is mostly its data. Without this, phases P4 onward cannot
start, and no claim about market performance is possible.

*What unblocks it:* egress to one provider, or a bar corpus supplied out of band
with a licence permitting model training. **The licence matters** — many market
data terms forbid derivative products, and a model trained on such data is a
derivative product.

### B2 — No Kronos checkpoint ⛔ **HARD**

`huggingface.co` is 403. The genuine Kronos weights have never been executed
here. G13 cannot be attempted, so "outperforms Kronos" is not claimable however
good the native model turns out to be.

### B3 — No broker ⛔ **HARD**

No sandbox venue reachable. P7 cannot start.

### B4 — Kronos weight licence unknown ⛔ **HARD, and possibly permanent**

The repository is MIT; the weights are distributed separately under terms this
environment cannot read. **Until confirmed, distillation from Kronos is
prohibited.** See dependency-map §9 — the engineering answer may be that we do
not want a teacher whose only public evidence is negative.

### B5 — Compute 🟡 **SEVERE CONSTRAINT**

Measured in this environment:

```
4 CPU cores · 15 GB RAM · no CUDA device · torch 2.13.0 (CPU only)
23 GB free disk · ephemeral container
```

Feasible: a model of order 10⁵–10⁶ parameters on 10⁵–10⁶ bars, hours per run.
Not feasible: anything at Kronos's scale, cross-asset attention over a large
universe, or extensive hyperparameter search.

This shapes the architecture rather than blocking it — §3.2 and §3.3 choose a
continuous encoder and a direct quantile head partly *because* they are cheap.
But it does mean the first native model will be small, and calling a small model
"Kronos-class" would be false.

### B6 — Dependency policy 🟡 **DESIGN CONSTRAINT**

Olympus ships three required dependencies and a CI guard
(`tests/test_deps_claim.py`) that AST-scans every third-party import. The
trading core is pure stdlib and that is a property worth keeping.

`torch` must therefore go in a **`native` extra**, separate from `kronos`, and
`native/` must import it lazily inside functions, raising
`errors.DependencyMissing` when absent — exactly as `kronos_runtime.py` does.
The rest of `native/` (state, dataset windowing, checkpoint format, evaluation)
must remain stdlib and testable without torch.

### B7 — Checkpoint storage 🟡 **DESIGN CONSTRAINT**

The container is ephemeral and multi-gigabyte weights do not belong in git.
Needs a decision before P3 produces its first checkpoint: an artefact store, Git
LFS, or a policy that only small checkpoints are committed. The **manifest** is
small and must be committed regardless — a checkpoint without its manifest
cannot be registered (G8).

---

## 8. What this document does not claim

- **No native code exists.** This is a design. `docs/OLYMPUS_NATIVE_MODEL_STATUS.md`
  is the ledger and currently records nothing built.
- **No claim that the native model will beat Kronos.** G13 is unattempted and
  blocked, and losing it is an acceptable outcome.
- **No claim that this architecture is better than Kronos's.** It is different,
  for stated reasons. Whether the differences help is an empirical question that
  B1 and B5 currently prevent anyone from answering.
- **Olympus does not own a Kronos-class model.** It owns no trained weights at
  all. That sentence should stay in this document until G7, G8, G11 and G13 have
  all been passed with real data.
