# Final Audit — Does Olympus Own an Independent Advanced Market-Intelligence System?

**Audited from source, not from documentation.** Every claim below is backed by
a command in §2 that was run against this commit. Where a document disagreed
with what the code does, the document was corrected (§13).

- **Audit date:** 2026-07-28
- **Branch:** `claude/kronos-technical-teardown-54pjna`
- **Scope:** `olympus/trading/` (48 modules) + `olympus/trading/native/`
  (41 modules, 24,170 lines, 698 tests)

---

## 1. Executive verdict

### Ownership: **(3) Olympus-native challenger exists but is unvalidated**

Both halves of that sentence are load-bearing.

**The challenger exists and is genuinely Olympus's.** 41 native modules, 24,170
lines, 698 tests. It builds market states, splits data without leakage, trains a
gradient-descent multi-task model from a seed, writes provenanced checkpoints,
serves forecasts, decides when to decline, evaluates itself over eight strata,
proposes its own successors, runs them in an OS-isolated worker, and refuses to
promote them. A source-level comparison against the genuine Kronos repository
(§3) finds **zero shared class names, zero shared comments, zero identical AST
structures, and one shared line of code** — `from datetime import datetime,
timedelta`.

**It is unvalidated, and on the only data available it is bad.** No weights have
ever seen a market. Under a matched contract on generated data it loses to
persistence, to an order-3 autoregression and to gradient-boosted trees on every
metric tested — nine usable comparisons, nine significant losses, zero wins — and
its mean absolute error is 0.0256 against 0.0066 for the trees, of which
**0.0250 is a constant location bias**.

### Why not verdict 7

Verdict 7 ("fully Kronos-independent") is barred while runtime operation
requires Kronos code or weights. **It does not** — §3.2 blocks every Kronos
module at import and all 89 trading modules still load, and a forecast still
runs. So the stated bar is cleared.

**Verdict 7 is still wrong**, and it is the tempting answer, so the reason
matters: this list is a maturity ladder, not an independence checkbox. Selecting
7 would assert that Olympus has an independent market-intelligence *system*,
when what it has is an independent, unvalidated *challenger* that currently
loses to a decision tree. Independence of the code is necessary for 7 and is not
sufficient. Verdict 3 states exactly what the evidence supports.

### Reality: three of six maturity levels reached

| Level | Reached | Evidence |
|---|---|---|
| Research framework | **yes** | 41 native modules, 698 tests, isolated experiment runner, hypothesis and challenger machinery |
| Backtesting system | **yes** | `backtest.py` — `BacktestEngine`, `rolling_windows`, `anchored_windows`, `WalkForwardResult` |
| Simulated autonomous trader | **yes** | `tests/test_trading_end_to_end.py` (18 tests) runs data → signal → risk → order → fill → reconciliation against `PaperBroker`, a deterministic simulated venue driven by candles |
| Real broker paper-trading system | **no** | `testnet.binance.vision` → `http=000`. No broker has ever been reached |
| Bounded autonomous live trader | **no** | 9 live-mode gates, `broker_connectivity` and `paper_trading_history` unpassable |
| Demonstrated profitable autonomous trader | **no** | No real trade has ever been placed. Zero profitability evidence of any kind |

**Olympus is a research framework and a backtesting system with a simulated
autonomous trading loop.** It is not a paper trader against a real venue, and it
is nowhere near a live one.

---

## 2. Evidence — reproduce every claim

```bash
# no checkpoints and no datasets are tracked in this repository
git ls-files | grep -Ei '\.(pt|pth|ckpt|safetensors|bin|onnx|npz)$'   # empty
git ls-files | grep -Ei '\.(csv|parquet|feather|arrow)$'              # empty

# every trading module imports with all Kronos modules blocked
python - <<'EOF'
import sys, importlib, importlib.abc, pkgutil
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if "kronos" in name.lower(): raise ImportError("BLOCKED " + name)
import olympus.trading as T, olympus.trading.native as N
sys.meta_path.insert(0, Block())
bad = []
for pkg, mods in ((T, pkgutil.iter_modules(T.__path__)),
                  (N, pkgutil.iter_modules(N.__path__))):
    for m in mods:
        if "kronos" in m.name: continue
        try: importlib.import_module(pkg.__name__ + "." + m.name)
        except Exception as e: bad.append((m.name, e))
print("FAILED:", bad or "none")
EOF

# source similarity against the genuine Kronos repository
git clone --depth 1 https://github.com/shiyu-coder/Kronos /tmp/kronos
python scripts/independence_similarity.py /tmp/kronos olympus/trading/native

# the automated originality audit, as data
python -c "import json;from olympus.trading.native import originality as o;\
print(json.dumps(o.audit_report(), indent=2))"

# nothing in the evolution surface reaches the safety kernel
python -c "from olympus.trading import kernel;print(kernel.audit_evolution_modules())"

# the capability register and its readiness verdicts
python -c "from olympus.trading.native.capability import native_capabilities;\
print(native_capabilities().table())"

# the matched evaluation and its verdict
python scripts/matched_evaluation.py --bars 2000

# broker and market data, measured
curl -sS -o /dev/null -w "%{http_code}\n" https://testnet.binance.vision/api/v3/ping
curl -sS -o /dev/null -w "%{http_code}\n" https://api.binance.com/api/v3/ping
```

### What those commands returned at audit time

| Check | Result |
|---|---|
| tracked checkpoints | **none** |
| tracked datasets | **none** |
| weight files anywhere on disk | **none** |
| trading modules failing with Kronos blocked | **none** (89 of 89 import) |
| `kernel.audit_evolution_modules()` | `[]` — 41 native modules audited |
| `originality.audit_report()["clean"]` | `True`, 8 checks |
| production-eligible capabilities | **0 of 9** |
| matched-evaluation verdict | `INSUFFICIENT_EVIDENCE` |
| `testnet.binance.vision` | `http=000` |
| `api.binance.com` | `http=000` |
| full test suite | 8,389 passed, 30 skipped |

---

## 3. Independence audit

### 3.1 What Olympus owns

| Asset | Owned | Evidence |
|---|---|---|
| Original model source code | **yes** | `native/{model,trunk,neural,encoder,quantile}.py`. Zero shared class names or AST structures with Kronos |
| Original representation layer | **yes** | `native/representations.py` — seven independently implemented candidates, each with a machine-readable design record |
| Original training pipeline | **yes** | `native/{pipeline,train}.py`. Seeded, reproducible, resumable; `RunRecord.reproducible` computed from five facts |
| Original evaluation pipeline | **yes** | `native/{evaluation,benchmark,matched}.py`. Kronos ships **no** evaluation code at all, so nothing could have been copied |
| Independently trained checkpoints | **yes, and worthless** | Every weight came from `torch.manual_seed` inside `Trainer.fit`. **None is committed and none has seen a market** |
| Versioned datasets / legal manifests | **manifests yes, datasets no** | `native/dataset.py` produces two-hash manifests with provenance, membership intervals and leakage audits. **No dataset is committed and none is real** |
| Model-serving infrastructure | **yes** | `native/serve.py` — verifies before loading, one forward pass per head, latency and memory measured |
| Self-evolution infrastructure | **yes** | `native/{evidence,challengers,isolation,promotion,improvement}.py` on top of `trading/{hypotheses,lab,capabilities,champion,rollback,evolution,governance,kernel}.py` |
| Deployment and rollback mechanisms | **yes** | `rollback.py` — signed deployments, nine triggers, autonomous rollback that can only retreat to human-approved state |

**The honest summary of that table:** Olympus owns all the *machinery* and none
of the *evidence*. Every asset that could be built without market access exists;
every asset that requires market access does not.

### 3.2 Runtime dependency test

Blocking every module whose name contains "kronos" at import time and then
importing all 48 top-level trading modules and all 41 native modules:

```
MODULES FAILING WITHOUT KRONOS: none
kronos in sys.modules: []
```

A forecast still runs. `ForecastService.register(name, forecaster)` registers
nothing by default — **there is no wired default forecaster at all**, Kronos
included.

### 3.3 Source-similarity audit against the genuine Kronos repository

Cloned `github.com/shiyu-coder/Kronos` (MIT, 11,189 lines of Python) and
compared it against `olympus/trading/native/`:

| Probe | Kronos | Olympus native | Overlap |
|---|---|---|---|
| Class names | 30 | 223 | **0** |
| Function names | 182 | 622 | 9 — all universal: `__init__`, `__len__`, `__getitem__`, `forward`, `encode`, `decode`, `predict`, `generate`, `get` |
| Comment / docstring fragments (>40 chars) | 310 | 2,096 | **0** |
| Distinct code lines (>40 chars) | 2,353 | 7,168 | **1** — `from datetime import datetime, timedelta` |
| **AST shapes** (node-type sequences, ≥25 nodes) | 262 | 1,075 | **0** |
| Best file-pair similarity | — | — | **0.019** (`serve.py` vs `kronos.py`) |

The AST-shape probe is the one that matters for **cosmetic renaming**: it
discards every identifier, constant and string, so a renamed copy would still
collide. Zero collisions across 262 × 1,075 shapes.

The same probe against `kronos_adapter.py` + `kronos_runtime.py` — the two files
that legitimately wrap Kronos — also returns **0 identical structures**. Even the
adapter is not a copy.

**Test copying:** Kronos's `tests/` against Olympus's 69 trading test files —
0 identical AST shapes, 0 identical lines out of 63 and 12,043 respectively.

**Hidden weight conversion:** `from_pretrained`, `hf_hub_download`,
`snapshot_download` and `torch.hub` appear in exactly two places — inside
`kronos_runtime.py`, where they belong, and inside `originality.py` as the
strings it *searches for*. The two `load_state_dict` calls in `native/` are
`pipeline.py:737` (restoring its own best epoch) and `torchutil.py:159` (loading
its own JSON state dict). `checkpoint.assert_olympus_origin` refuses any
`initialised_from` that looks like a weight file, URL or hub id.

### 3.4 Every remaining Kronos dependency, classified

| # | Dependency | Location | Class | Note |
|---|---|---|---|---|
| 1 | `kronos_adapter.py` (473 lines) | `olympus/trading/` | **Benchmark-only** | Wraps an external forecaster behind the `Forecaster` ABC. Nothing registers it by default. Would be the comparison arm if weights were reachable |
| 2 | `kronos_runtime.py` (~600 lines) | `olympus/trading/` | **Benchmark-only** | Checkpoint pins, device selection, compatibility verification. The only file in the repo that can fetch foreign weights |
| 3 | `kronos` optional extra | `pyproject.toml` | **Optional** | `torch`, `numpy`, `einops`, `huggingface_hub`. Deliberately not implied by the `native` extra |
| 4 | Lazy-import entries | `trading/__init__.py:64-65` | **Removable** | Two lines in a lazy table. `test_trading_independence.py` exempts them with a staleness check |
| 5 | Teardown citations in docstrings | `contracts.py`, `errors.py`, `features.py`, `evaluate.py`, `outcomes.py`, `proposals.py`, `registry.py`, `agents.py`, `native/data.py` | **Historical** | ~20 comments citing `KRONOS_TEARDOWN.md` for *why a defence exists*. They explain design decisions; deleting them would delete the rationale |
| 6 | Forbidden-name lists | `native/originality.py` | **Removable** | The checker must name what it forbids. Enumerated exemption with a staleness test |
| 7 | `matched_reference_label()` | `kronos_adapter.py` | **Benchmark-only** | Supplies the incumbent's display name so `native/matched.py` need not know it |
| 8 | Retired reason/strategy codes | `strategy.py:75` | **Historical** | Comment pointing at `RETIRED_REASON_CODES` |
| 9 | Kronos test files (3, 163 tests) | `tests/` | **Benchmark-only** | Test the adapter's contract handling with an injected loader. Pass without any checkpoint |

**Required: none. Fallback: none. Undisclosed: none.**

Nothing is classified Required, and that is checked rather than asserted (§3.2).
Nothing is classified Fallback: no code path falls back to Kronos, because
nothing registers it. Nothing is Undisclosed: the search in §3.3 covered
imports, identifiers, runtime strings, comments, class structures, tests and
weight-conversion paths, and every hit is in the table above.

**One caveat on completeness.** This audit can prove the absence of *textual and
structural* copying. It cannot prove the absence of conceptual influence — the
native model uses causal convolutions, quantile heads and a tokenised
representation, and so does Kronos, because those are the standard tools for
this problem. `native/modelcard.py` discloses external methods and inspirations
by name, which is the appropriate remedy; it is a disclosure, not a proof.

---

## 4. Original Olympus capabilities

Nine registered capabilities. **Zero are production-eligible**, and that verdict
is computed from seven facts by `CapabilityStatus.production_eligible`, which
has no setter.

| Capability | What it does | Where | Original | Inputs → Outputs | Tests | Real data | Deployment |
|---|---|---|---|---|---|---|---|
| **multi_timeframe** | Eight declared scales, each bar classified closed/partial/delayed/absent; `last_close` gated on state | `native/timeframes.py` | yes | bars per scale → per-scale features + mask | adversarial | **blocked** | not deployed |
| **cross_asset** | Relationship graph, seven edge kinds, every edge with `since`/`until` | `native/crossasset.py` | yes | target + reference bars + graph → causal features | adversarial | **blocked** | not deployed |
| **order_book_liquidity** | Spread, depth, imbalance, fill probability, slippage, square-root impact, six-condition tradability gate | `native/microstructure.py` | yes | book snapshots + size → cost estimates + gate | adversarial | **blocked** | not deployed |
| **event_awareness** | Seven event kinds; timing and declared importance only, no content | `native/events.py` | yes | calendar + instant → four numbers | adversarial | **blocked** | not deployed |
| **portfolio_aware** | Exposure, concentration, correlation, hedges, drawdown contribution — evidence only | `native/portfolio_context.py` | yes | positions + marks → features + cautions | adversarial | none | not deployed |
| **regime_specialists** | Eight specialists plus a generalist, four fallback reasons, degeneracy check | `native/specialists.py` | yes | routing features → recorded decision | adversarial | **blocked** | not deployed |
| **scenario_generation** | Six probabilistic scenarios summing to one, each with falsifiers | `native/scenarios.py` | yes | quantiles → scenario set | adversarial | **blocked** | not deployed |
| **explainability** | 26 closed reason codes with measurements; prose assembled from codes | `native/explain.py` | yes | context → codes + falsifiers | adversarial | none | not deployed |
| **robustness** | Thirteen adversarial conditions exercised end to end | `tests/test_trading_native_robustness.py` | yes | damaged inputs → explicit degradation | adversarial | **blocked** | not deployed |

### Known limitations that materially change how these read

1. **Nothing in `microstructure.py` is calibrated.** Fill probability, slippage
   and impact run on declared defaults and report `calibrated=False`. The impact
   coefficient *could* be fitted from `outcomes.py`'s recorded fills — the one
   calibration reachable today, and it has not been done.
2. **No specialist has been trained.** The router, the fallbacks and the record
   are complete; the specialists are registered slots.
3. **Regime accuracy measured 0.000** in the matched evaluation. The regime head
   never agreed with the label.
4. **The multi-timeframe ladder produces features no model reads.** The
   multi-task model consumes the base scale only.
5. **The derived channels do not reach the point forecast.** The candles-only and
   derived-feature arms produced *bit-identical* predictions — measured gain
   exactly 0.000000.
6. **The robustness suite establishes safe degradation, not good degradation.**
   Safe degradation is refusing to answer, and a system that refused everything
   would also pass it.

---

## 5. Evaluation results

### Matched evaluation, 682 prediction instants, two instruments

| arm | MAE | quantile loss | dir acc | coverage (nom. 0.80) | net return | Sharpe |
|---|---|---|---|---|---|---|
| persistence | 0.008711 | 0.003215 | 0.4457 | 0.833 | +0.000 | – |
| autoregression | 0.007038 | 0.002470 | 0.6466 | 0.768 | +0.904 | +20.0 |
| **gradient-boosted trees** | **0.006589** | **0.002334** | **0.6613** | 0.773 | **+1.052** | **+23.5** |
| olympus-native | 0.025636 | 0.008821 | 0.4477 | 0.471 | −0.362 | −7.4 |
| kronos (official checkpoint) | *did not run* | | | | | |
| olympus + kronos ensemble | *did not run* | | | | | |

**Nine usable comparisons against the three simple arms: nine significant
losses, zero wins**, Holm-corrected over a 63-test family. No regime, no period
and no instrument in which the native model wins.

**The single most diagnostic number:** MAE 0.025636, bias +0.025038. Essentially
the entire error is a **location** offset of ~2.7 realised standard deviations,
with approximately correct spread. It explains the coverage, the sub-coin-toss
directional accuracy, and why the only stratum where the model is net positive
is trending-up — an upward bias pays there.

### Earlier measurements, consistent with this

- **Phase 1:** the native network lost to a 19-parameter AR(3) fit on a linear
  synthetic process; interval coverage 1.000 against a nominal 0.80.
- **Phase 2:** lost to persistence on quantile loss (p ≈ 0); intervals 3.1–3.4×
  too wide *and* mis-centred; coverage error +13.4 overall, +20.0 in low
  volatility, 0.0 in high.
- **Phase 3:** nine capabilities, zero production-eligible.
- **Phase 4:** the end-to-end evolution demonstration ends in a **rejection** —
  the challenger converged on persistence and could not be distinguished from it
  while carrying more parameters.

**Four independent evaluations, four losses.** That is a consistent finding, not
a run of bad luck.

### Caveat that cuts both ways

Every number above is on series this repository generated. They are evidence
about the harness and about a reproducible defect in the model. They are **not**
evidence about markets, and a future real-data result could differ in either
direction.

---

## 6. Security assessment

| Control | State | Evidence |
|---|---|---|
| Safety kernel | 11 components, 14 modules, 3 independent mechanisms | `kernel.SAFETY_KERNEL`; `seal()` / `verify_seal()` |
| Evolution isolation from kernel | **enforced** | `audit_evolution_modules()` → `[]` over 41 native + 13 evolution modules |
| Research isolation | **OS-level, verified from inside** | Separate process, network namespace, seccomp-BPF, rlimits, read-only bind-mounted inputs, destroyed worker. 15 of 16 mechanisms apply; the 16th reports why not |
| Credential reach from research | **none** | Environment rebuilt from an allowlist; `olympus.vault` import blocked; no network |
| Order submission from research | **structurally impossible** | Empty network namespace + seccomp denying `socket`/`connect` + blocked imports |
| Kernel modification | **no code path exists** | `propose_kernel_change()` returns a document; there is no `apply_kernel_change` |
| Concealment | **prohibited for every actor** | `conceal_result` and `rewrite_audit_history` refused for operators too |
| Audit trail | hash-chained, verified | 12/12 entries verified in the Phase 4 demonstration |

**Three defects were found and fixed during construction**, and they are worth
recording because each shipped first:

1. `chmod 0444` does not bind a uid-0 worker — root bypasses the DAC check. Now
   a read-only bind mount in a private mount namespace.
2. The seccomp filter cannot be installed before `execve`, because it denies
   `execve`. It is installed by the runner after exec.
3. Seccomp was masking a failed `unshare`, so a dead network namespace read as a
   working one. The two are now judged independently.

**What is not claimed:** this defends against Olympus's own machinery, not
against a human with a shell. Anyone who can edit the source can edit
`kernel.py`.

---

## 7. Self-evolution assessment

| Element | State |
|---|---|
| Evidence journal | 14 fields per matured forecast; abstentions recorded as evidence; error `None` not zero; maturity a fact about the clock with no `force` |
| Weakness detection | 10 kinds, each with measurement, threshold, sample size; provisional below n=30 |
| Challenger generation | 10 kinds, 11 required fields; contradicting evidence mandatory; compute budget enforced as rlimits; every complexity-adding proposal paired with a simplification |
| Isolated experiments | OS-level; a run whose confinement did not hold is **discarded** |
| Promotion gate | 12 stages in order; a missing check **fails**; stages 11–12 require an operator token; no `force` |
| Improvement metrics | 12 named; 13 volume counters **raise** rather than being ignored; verdict starts `UNPROVEN` |
| Autonomous powers exercised | reject, restrict, demote, roll back, shut down — all demonstrated |
| Human-only powers | promote, deploy, enable live trading, change limits, expand permissions, clear kill switch — none reachable autonomously |

**What it has never done:** improved anything. The loop is complete and has
produced exactly one end-to-end outcome — a **rejection**. Seven of the twelve
improvement metrics are unmeasured here. Gate stages 9 (paper trading) and 10
(shadow mode) **have never run**, because both need a live venue; a challenger
reaching `awaiting_review` has passed eight real stages and two recorded from
constructed evidence, and the gate does not distinguish them.

---

## 8. Real-market validation status

**None. Zero. No component has ever seen a market.**

- No market-data provider is reachable: `api.binance.com` → `http=000`.
- No dataset is committed; no dataset is real.
- No checkpoint has been trained on anything but generated series.
- 21 of 38 schema channels are obtainable in principle; **0 are ingested**.

Every "evaluated" mark anywhere in this repository means *evaluated on synthetic
data*, and the status ledger's evidence classes (`I`/`U`/`S`/`R`) keep that
distinction. **Nothing carries `R`.**

## 9. Broker-validation status

**None.** `testnet.binance.vision` → `http=000`. `BinanceTestnetBroker` exists
and has never connected. `PaperBroker` is a deterministic simulated venue driven
by candles this repository generated — it is a simulator, not a broker.

Of the nine live-mode gates, `broker_connectivity` and `paper_trading_history`
cannot pass, and `models_approved` has nothing to approve.

## 10. Profitability evidence

**None of any kind.**

No real order has been placed. No real money has been risked. The only positive
returns anywhere in this repository come from arms trading a synthetic series
built with strong, exploitable autocorrelation — and the native model is
**negative** even there (−0.362, Sharpe −7.4).

Any statement that Olympus is or might be profitable is unsupported by anything
in this repository.

---

## 11. Unresolved blockers

| # | Blocker | Severity | Blocks |
|---|---|---|---|
| **B1** | No market-data provider reachable | ⛔ Hard | All real-data validation; G10–G14; 7 of 9 capabilities' real-data marks |
| **B2** | Kronos checkpoint unreachable (`huggingface.co` 403 at CONNECT, gateway policy) | ⛔ Hard | The entire Phase 5 question; G13 |
| **B3** | No broker sandbox reachable | ⛔ Hard | Paper trading, gate stages 9–10, live-mode gates |
| **B4** | Kronos **weight** licence unverifiable — **narrowed** | ⛔ Hard | Distillation. The *source* licence is now known to be MIT (verified by reading the cloned `LICENSE`); only the weight terms remain unread |
| **B5** | 4 CPU cores, 15 GB RAM, no CUDA, ephemeral | 🟡 Severe | Caps model size at ~10⁵–10⁶ parameters |
| **B6** | Three required dependencies with a CI guard | 🟡 Constraint | `torch` stays an optional extra |
| **B7** | Ephemeral container, no artefact store | 🟡 Constraint | Checkpoint storage undecided |
| **B8** | **The location bias** — new, and the only one solvable here | 🔴 **Blocking value** | Bias +0.0250 ≈ 2.7σ with correct spread. Until it is fixed, no comparison involving the native model measures its architecture |

**B8 is the one to act on.** It needs no market data, no Kronos, no broker and no
new hardware.

---

## 12. Exact next actions

Ordered by value per unit of unblocked effort.

1. **Diagnose and fix the location bias (B8).**
   Reproduce with `python scripts/matched_evaluation.py --bars 2000` and read
   §8 of `docs/OLYMPUS_VS_KRONOS.md`. The evidence: prediction spread is
   correct (sd 0.0096 vs realised 0.0094) and the mean is displaced by +0.0377
   on one series and +0.0250 on another. Start at `native/model.QuantileHead` —
   the monotone ladder's base term — and at the pinball-loss target
   construction in `native/tasks.py`, since a median that minimises pinball
   loss should not be displaced. Success criterion: |bias| < 0.25σ on both
   generated instruments, without the spread degrading.

2. **Fix or remove the regime head.**
   Regime accuracy measured **0.000**. Either it is scored against the wrong
   label or it is not learning. Both are one afternoon's work and one of them
   is a correctness bug.

3. **Wire the derived channels into the point forecast, or delete them.**
   The candles-only and derived-feature arms produced bit-identical
   predictions. Either the multi-task heads should inform the forecast, or the
   input-advantage axis is measuring nothing and should stop being reported.

4. **Calibrate `ImpactModel` from recorded fills.**
   `ImpactModel.calibrate_from_fills` exists and needs ≥10 fills with `size`,
   `depth`, `spread_bps`, `realised_bps`. `outcomes.py` already records them
   from the paper broker. **The only calibration in the system that is
   reachable today**, and it turns three `calibrated=False` estimates into
   measured ones.

5. **Request egress for one market-data host (B1).**
   This is an environment change, not a code change: one allowlist entry.
   It unblocks G10–G14, seven capabilities' real-data marks, and the phrase
   "evaluated on real data" appearing anywhere for the first time.

6. **Request egress for `huggingface.co`, or supply the Kronos weights out of
   band, and read the weight licence (B2, B4).**
   Only this answers the Phase 5 question. Without it the ownership verdict
   cannot move past 3 on the evidence axis, whatever the model does.

7. **Do not train anything larger until 1 and 2 are fixed.**
   Four independent evaluations have reported a loss. A bigger model with the
   same location bias will report a fifth.

---

## 13. Corrections made to existing documents

Audited every document that makes a completion, ownership, independence or
superiority claim.

| Document | Finding | Action |
|---|---|---|
| `OLYMPUS_NATIVE_MODEL_STATUS.md` | Phase list said "Phase N complete" without stating that the *objective* is not | **Corrected** — ownership verdict and maturity level added to the header |
| `OLYMPUS_NATIVE_MARKET_INTELLIGENCE.md` | Status line stale at "Phase 3 complete"; Phases 4 and 5 had shipped | **Corrected** |
| `OLYMPUS_NATIVE_CAPABILITIES.md` | Accurate — "zero production-eligible" already stated in the header | none |
| `OLYMPUS_NATIVE_SELF_EVOLUTION.md` | Accurate — already states nothing has been promoted and stages 9–10 never ran | none |
| `OLYMPUS_VS_KRONOS.md` | Accurate — headline verdict is `INSUFFICIENT EVIDENCE` | none |
| `SELF_EVOLUTION.md` | Accurate — "has not yet learned anything about a real market" already in the header | none |
| `TRADING_STATUS.md` | Accurate — already carries the loss results | none |

**No document was found claiming superiority over Kronos, real-data validation,
profitability, or full independence.** The two corrections were both staleness
rather than overstatement.

---

## 14. The one-paragraph answer

Olympus owns an independent, well-engineered, thoroughly tested market-
intelligence **framework** — 41 native modules with no textual, structural or
runtime dependency on Kronos, backed by a source-level comparison finding zero
shared classes, zero shared comments and zero shared AST structures. It owns a
native forecasting **model** that trains reproducibly from a seed and serves
through a 22-field contract. It does **not** own evidence that any of it works:
no checkpoint has seen a market, no broker has been reached, no money has been
risked, and on the only data available the model loses to a gradient-boosted
tree on every metric with a reproducible +2.7σ location bias. The correct verdict
is **(3) Olympus-native challenger exists but is unvalidated**, and the correct
next action is to fix the bias — which needs nothing this environment lacks.
