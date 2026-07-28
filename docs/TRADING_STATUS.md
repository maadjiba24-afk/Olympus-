# Olympus Trading Domain — Implementation Status

**The honest ledger.** `docs/TRADING_ARCHITECTURE.md` describes the *target*;
this file records what actually exists, what is tested, and what is unsafe or
missing. When the two disagree, this file is right.

- **Last updated:** 2026-07-28
- **Branch:** `claude/kronos-technical-teardown-54pjna`
- **Scale:** ~55,700 lines across 88 modules (48 top-level + 40 in `native/`); 68 test files; **3187 trading tests passing**
- **Whole repository:** 8042 passed, 30 skipped, **zero regressions**
- **Operating mode:** `PAPER` (the default; live is disabled)
- **Live trading:** ❌ **DISABLED AND NOT DEMONSTRABLE HERE** — see §4

> **Read §3 and §4 before trusting any of this.** Several completion-standard
> items cannot be demonstrated in this environment, and saying so plainly is
> part of the deliverable. `docs/TRADING_EXTERNAL_VALIDATION.md` scores the
> twelve external-validation gates and measures the blocker host by host;
> `docs/SELF_EVOLUTION.md` covers the thirteen self-evolution gates;
> `docs/OLYMPUS_NATIVE_MODEL_STATUS.md` covers the Olympus-native forecasting
> work: decoupling from Kronos is **done and enforced by test**, and the native
> package now trains a real network end to end and scores it against nine
> baselines under one harness — but **Olympus owns no trained market model**,
> having fitted only synthetic series.
> `docs/OLYMPUS_MARKET_STATE_SCHEMA.md` documents the 38 observable channels and
> the dataset manifest format; `docs/OLYMPUS_NATIVE_REPRESENTATIONS.md` documents
> the encoder contracts, the seven representation candidates, the nine baselines
> and the benchmark record; `docs/OLYMPUS_NATIVE_MODEL_ARCHITECTURE.md` documents
> the multi-task model, its abstention policy, its training pipeline and its
> evaluation.

---

## 1. Status legend

| Mark | Meaning |
|---|---|
| ✅ | Implemented with tests that exercise the adversarial cases, passing offline |
| 🟡 | Implemented; happy path tested, adversarial surface thin |
| 🔵 | Designed in the architecture doc; **no code** |
| ⛔ | Cannot be built or demonstrated here; reason stated |

---

## 2. Module status

### Foundation & composition

| Module | Status | Evidence |
|---|---|---|
| `contracts.py` | ✅ | Invariants at construction: OHLC coherence, UTC-only, `REJECTED ⟹ quantity 0`, stop on the protective side |
| `errors.py` | ✅ | Typed codes; unsupported configs raise rather than silently corrupt |
| `clock.py` | ✅ | Injectable; `FixedClock` refuses to move backwards |
| `pipeline.py` | ✅ | 20 tests: no order without an approving decision; fails closed on throwing risk engine, unreadable kill-switch registry, broken mode controller |

### Market analysis

| Module | Status | Evidence |
|---|---|---|
| `instruments.py` | ✅ | Sessions verified across the 2026-03-08 US DST transition; epoch-anchored timeframe grid; sealable registry |
| `storage.py` | ✅ | Idempotent dedupe, ordering, exact float round-trip, cross-process persistence |
| `validate.py` | ✅ | Gaps detected without fabrication; `assert_no_lookahead` keyed on `ts_close` |
| `candles.py` | ✅ | Tick aggregation, resampling, trailing partial bucket withheld |
| `ta.py` | ✅ | Indicators aligned to input length with exact warm-up regions |
| `features.py` | ✅ | `assert_causal` independently verified: rejects a full-sample z-score (the §12.9 bug in miniature), accepts an expanding window |
| `regime.py` | ✅ | Rule-based with hysteresis; `UNKNOWN` on insufficient history |
| `volatility.py` | ✅ | Multiple estimators; timeframe-derived annualisation |

### Forecasting

| Module | Status | Evidence |
|---|---|---|
| `kronos_runtime.py` | ✅ | Checkpoint pinning; unpinned refused; `ModelBackend` boundary keeps tokens out of the forecasting layer. **Kronos-owned, not Olympus-owned** — see `docs/OLYMPUS_KRONOS_DEPENDENCY_MAP.md` |
| `kronos_adapter.py` | ✅ | 97 tests incl. a named regression per teardown defect (§3) |
| `forecast.py` | ✅ | Service layer; an exploding forecaster becomes an abstention, never an exception into a strategy. The `Forecaster` ABC is the model-neutral plug point a native model will implement |
| independence | ✅ | 30 tests (`test_trading_independence.py`): no Olympus module imports, names or embeds a runtime string naming Kronos; blocking both Kronos modules at import breaks nothing else; `native/` additionally carries no Kronos-imposed constant, no module-scope torch, no Kronos import at any depth, and no reference to an external weight file or codebook |
| `native/` | 🟡 | 40 modules, 22,930 lines, 659 tests. A 38-channel market-state schema, dataset provenance with five leakage defences, stable encoder contracts, seven representation candidates, a multi-task model (fifteen registered tasks, seven trainable), nine structural abstention reasons, a reproducible training pipeline, a stratified evaluation over eight strata, nine capabilities behind a register whose readiness verdict is computed, and a self-evolution loop with OS-level research isolation — separate process, network namespace, seccomp filter, rlimits, signed inputs and results. **Fitted only to synthetic series, where it loses to persistence and its intervals are 3.1–3.4× too wide; zero of nine capabilities are production-eligible; the end-to-end evolution demonstration ends in a rejection** — see `docs/OLYMPUS_NATIVE_MODEL_STATUS.md`, `docs/OLYMPUS_NATIVE_CAPABILITIES.md` and `docs/OLYMPUS_NATIVE_SELF_EVOLUTION.md` |
| `signals.py` | ✅ | Generation + fusion; abstained forecast produces **no** signal, not a flat one |

### Decision, safety, execution

| Module | Status | Evidence |
|---|---|---|
| `risk.py` | ✅ | 56 tests, boundary-tested per limit with stable reason codes; fails closed on missing measurements; projected (post-fill) exposure; reduce-never-increase; taint barrier |
| `killswitch.py` | ✅ | Survives restart; auto-trips need operator override; unreadable registry raises |
| `portfolio.py` | ✅ | 19 tests; zero-crossing cost basis correct **both** directions; exact Decimal cash; idempotent fills; restart recovery |
| `oms.py` | ✅ | 18 tests; deterministic idempotency key from the decision; over-fill raised not clamped; terminal states terminal |
| `brokers/base.py` | ✅ | Credentials are vault *references*; `repr`/`str` cannot leak material (tested) |
| `brokers/paper.py` | ✅ | 19 tests; fills at next bar's open, self-enforced via staging timestamps; fees, adverse slippage, partial fills, rejections, outages, `force_desync` |
| `execution.py` | ✅ | Requires an approving decision; query-then-adopt retry; price collar; shadow mode reaches no venue |
| `reconcile.py` | ✅ | Adopts orders/fills; **never** auto-repairs a position break; trips the desync switch |
| `audit.py` | ✅ | 14 tests; forgery and deletion both detected by actually rewriting history on disk |
| `modes.py` | ✅ | 29 tests; default PAPER; live needs all 9 gates + token + named operator + audit event |
| `backtest.py` | ✅ | 20 tests; drives the real risk engine/OMS/portfolio/broker; `assert_no_lookahead` on every window; `latency_bars >= 1` enforced; survivorship and intrabar limits disclosed in warnings; deterministic (regression-tested after a real bug) |
| `perf.py` | ✅ | Every required metric; `None` for undefined ratios so a flat curve cannot post a Sharpe; gross/net are *required* constructor fields so a single-cost-basis report cannot be built; per-instrument/regime/period breakdowns whose trade counts sum back to the whole |
| `strategy.py` | ✅ | Concrete strategies with the sizing/exit code shared between the forecast and forecast-free arms, so the signal source is the only difference. Model-agnostic since P1: `ForecastMomentumStrategy` names no model |
| `registry.py` | ✅ | Model registry; approval requires an operator and an unpinned revision can never be approved |
| `sentiment.py` | ✅ | Injection payloads neutralised (verified by probe: instruction text and fenced system blocks stripped, benign headlines untouched); nothing derived from a `NewsItem` can reach the limits API |
| `monitor.py` | ✅ | Health probes and auto-trip evaluation; a failing probe reports DEGRADED rather than taking the safety system down with it |
| `agents.py` | ✅ | Validated output schemas for the seven market-intelligence agents; malformed/out-of-range/instruction-bearing outputs rejected |
| `evaluate.py` | ✅ | Forecast metrics (MAE/RMSE/MAPE/sMAPE/directional accuracy/pinball/CRPS/coverage) plus paired-bootstrap and sign-test significance. `model_is_valuable()` returns **False** with no evidence, and a mean-zero-noise "improvement" over 200 paired observations does not pass (p≈0.84) while a genuine effect does (p<0.001) — verified by probe |

### Connectivity & operations

| Module | Status | Evidence |
|---|---|---|
| `ingest.py` | 🟡 | 28 tests. `IngestionService` is the single write path: duplicates dropped, gaps **counted and never filled**, late bars refused, three timestamps (venue open/close + local `received_at`), restart recovery, reconnect-then-backfill. Adversarially tested against `ReplayProvider`; `BinanceSpotProvider`'s REST/websocket parsing is tested against Binance's documented wire formats but **not** against Binance — the host is unreachable here (§4). |
| `brokers/binance_testnet.py` | 🟡 | 27 tests. Testnet enforced by host allow-list (pointing it at `api.binance.com` raises at construction); spot-only, so the futures testnet host is refused; uncovered SELL rejected before any request; HMAC signing, `-2010` duplicate → adopt, `-2013` unknown → `None`, unmapped status raises rather than reading as open; signed URLs deliberately excluded from exception text. **Never contacted the venue** (§4). |
| `cli.py` | ✅ | 24 tests, the load-bearing ones negative: an AST scan asserts the module cannot import or construct `LimitsStore`, `ModeController` or `ExecutionEngine`, and the parser offers no `mode`/`submit`/`set-limit`/`disengage` command. Renders status/portfolio/orders/strategies/models/limits; the only write actions are `kill` and read-only `reconcile`/`verify-audit`. Survives a subsystem that throws. |

### Controlled self-evolution (`docs/SELF_EVOLUTION.md`)

| Module | Status | Evidence |
|---|---|---|
| `governance.py` | ✅ | 34 tests. The autonomous / human-only / prohibited split as a call that **raises** — a returned `False` is something an autonomous caller can ignore. Every `Action` is classified; an unclassified one fails closed. An operator without a token cannot be constructed, and no serialisation carries one. |
| `kernel.py` | ✅ | 22 tests, the gate-13 proof. Eleven components / 14 modules declared; content sealing that names *which* module changed; an AST scan asserting no self-evolution module can import `execution`/`brokers`/`vault`/`modes` or call ~20 kernel mutation entry points. `propose_kernel_change()` exists; no `apply` counterpart does, asserted by parsing the whole package. |
| `knowledge.py` | ✅ | 38 tests. Repetition capped at 0.5 confidence however many sources agree; same-origin evidence counted once; external content untrusted until attributed validation; correction writes a new version and keeps the old; no record however evidenced reaches risk config, credentials, permissions or execution settings. |
| `outcomes.py` | ✅ | 32 tests. Thirteen scoring axes; an unmeasurable axis grades UNKNOWN, never GOOD; decision records frozen at decision time and unamendable; abstentions scored as well as trades; weakness detection requires n≥30. |
| `drift.py` | ✅ | 36 tests. PSI + two-sample KS in pure stdlib, both reported with their thresholds; five-rung ladder where descending is autonomous and ascending needs an operator; staleness alone flags a component whose metrics look perfect. |
| `hypotheses.py` | ✅ | 31 tests. A proposal cannot be constructed without a failure condition, nor with success/failure criteria that overlap. `REFUTED` is terminal and undeletable. The standing Kronos hypothesis carries the negative upstream evidence (#354/#355) rather than omitting it. |
| `lab.py` | ✅ | 32 tests. Sandbox isolation by *absence* — no attribute exists for a denied resource; fails closed on unlisted ones; breach attempts recorded before the exception propagates. Seven experiment kinds; parameter sweeps carry a Bonferroni correction. |
| `proposals.py` | ✅ | 38 tests. Every required field enforced at construction. A patch naming a kernel file, or whose *body* reaches into one, raises before review. No `apply`/`merge`/`deploy` function exists, asserted on the AST. |
| `capabilities.py` | ✅ | 26 tests. Ten states; promotion is operator-only, one rung at a time, and names exactly which evidence is missing — a rationale is refused identically to nothing. Suspension and deprecation are autonomous. |
| `champion.py` | ✅ | 28 tests. A mismatched harness (data, costs, or risk limits) **raises** rather than reporting; in-sample can never justify replacement; parsimony breaks statistical ties; a challenger materially worse in any regime is refused. |
| `rollback.py` | ✅ | 40 tests. Deployment refused without reproducible config, pinned dependencies, evidence and a written procedure. Rolling back is autonomous *because* it can only retreat to a human-deployed version. Nine triggers; post-rollback reconciliation is reported, never auto-repaired. |
| `evolution.py` | ✅ | 35 tests. Append-only ledger with no update or delete; `explain()` answers the seven required questions, returning `None` for "did it improve" until there is measurement; the improvement verdict starts at UNPROVEN and excludes the three governance counters. |
| `storekeys.py` | ✅ | Composite keys over `olympus.store`, whose `_safe()` rewrites separators. Fixes a defect that made five registries write records they could never list. |

### Not built

| Module | Status | Consequence |
|---|---|---|
| Checkpoint-validation harness | 🔵 | Phase-5 hash pinning, upstream-vs-Olympus numerical comparison and latency/memory measurement are designed but unwritten: a harness whose subject cannot be obtained cannot be tested, and would be untested code claiming a validated model. |
| Scheduled monitor / evolution loop | 🔵 | `monitor.py`, `killswitch.evaluate_auto_trips()`, `drift.EvaluationSchedule` and `evolution.EvolutionCycle.run()` are all built and tested; nothing runs any of them on a timer. Every one fires only when a caller invokes it. |

---

## 3. Kronos defect regressions

Each defect from `docs/KRONOS_TEARDOWN.md` has a named test in
`tests/test_trading_kronos_defects.py`:

| Defect | Repair |
|---|---|
| §12.1 `top_k`+`top_p` silently ignores `top_p` | Combination raises `ConfigurationError` |
| §12.6 opaque pandas error when `horizon ≥ context` | Refused at config **and** against the checkpoint's `max_context` |
| §12.7 paths averaged away inside inference | Every path preserved; mean is a view; quantiles from the ensemble |
| §12.7 incoherent OHLC output | Repaired per bar and warned; invariant asserted |
| §12.9 normalisation leaks the horizon | Proven context-only: backend sees identical input regardless of what follows `as_of` |
| §12.2 asymmetric `s1/s2` bits corrupt silently | Refused at pin construction |
| Unpinned checkpoints | `CheckpointVerificationError` before any network/torch work |
| §12.10 `comet_ml` hard import | Not inherited — no training code vendored |

---

## 4. What is **not** demonstrable here

| Completion-standard item | State | Reason |
|---|---|---|
| **Ingest real market data** | ⛔ **Blocked** | Every candidate feed is refused by this environment's egress policy with HTTP 403 at CONNECT — Binance, Alpaca, Coinbase, Kraken, Finnhub, Polygon, Yahoo, Stooq. Measured host by host in `docs/TRADING_EXTERNAL_VALIDATION.md` §1. `BinanceSpotProvider` was executed against `api.binance.com` and correctly surfaced the denial as a typed `MarketDataError`; that is the only real-external-system result in this work, and it is a fact about the blocker, not about trading. **No real bar has ever entered the system.** |
| **Paper trade through a real broker sandbox** | ⛔ **Blocked** | `testnet.binance.vision` is 403 like the rest, and no credentials exist. `BinanceTestnetBroker` is written and unit-tested but **has never contacted the venue**: nothing about real fills, fees, rate limits or clock skew is verified. What is demonstrated end to end is `PaperBroker`, a real simulated venue — it is **not** a broker sandbox and must not be reported as one. |
| **Run Kronos against the published checkpoints** | ⛔ **Blocked** | `huggingface.co` returns HTTP 403 through this environment's proxy (verified during the teardown). The adapter is tested against a deterministic fake backend: its *logic* is tested, its *numerical agreement with upstream Kronos* is not. |
| **Demonstrate Kronos adds value** | ⛔ **Still blocked — but now decidable** | The machinery exists (`evaluate.kronos_is_valuable`, `backtest`), so the question can be *answered* the moment real checkpoints and real data are available. It has not been answered here: no checkpoint is reachable and no market data is available, so the function returns its honest default of `False`. **No claim about Kronos's usefulness is made or supported.** Public third-party evidence is currently negative (teardown §16; upstream issues #354/#355). |
| **Backtest without known data leakage** | ✅ **Built and tested** | 20 tests, each naming the self-deception it closes. Two honest limits, disclosed in every result's `warnings`: fills are evaluated against bar OHLC rather than ticks, and a static universe cannot have survivorship bias removed by the engine. |
| **Live trading** | ⛔ **Correctly disabled** | The designed state. At least gates G2 (account), G4 (reconciliation), G7 (connectivity), G8 (paper history) cannot pass without a real venue. |

---

## 5. Known issues and technical debt

| # | Issue | Severity |
|---|---|---|
| 1 | `validate.py` re-implements timeframe parsing that `instruments.py` owns, delegating opportunistically; its delegate path returns early without the positivity re-check. Harmless today (`instruments` rejects `0m`) but two code paths can answer the same question. | Low |
| 2 | Default `min_completeness` of 0.95 marks one missing bar in five as `UNUSABLE`. Correct-but-strict; may be wrong for illiquid instruments. | Low |
| 3 | `risk.py`/`killswitch.py`/`portfolio.py`/`oms.py`/`brokers`/`execution.py`/`reconcile.py`/`audit.py`/`modes.py` were authored directly rather than by the parallel build, so a duplicate-cluster overwrite is possible; all are committed, so any clobber is recoverable by `git checkout`. | Low |
| 4 | `BrokerUnavailable` is a *sibling* of `BrokerError` (both subclass `ExecutionError`), which is easy to get wrong: `binance_testnet`'s `health()` and `get_quote()` originally caught only `BrokerError`, so the unreachable case escaped the health probe that exists to report it. Fixed and regression-tested, but the hierarchy remains a trap for the next adapter. | Medium |
| 5 | Streaming reconnect/backoff/gap-backfill is exercised only through `ReplayProvider`'s simulated disconnect. Real websocket failure modes — half-open sockets, silent stalls, out-of-order frames — are unexercised. | Medium |
| 6 | No scheduler runs `monitor`/`evaluate_auto_trips`/`EvolutionCycle`; every loop fires only when something calls it. | Medium |
| 7 | The research sandbox is a *capability* boundary, not a process boundary. An experiment cannot obtain production resources through it and `kernel.audit_evolution_modules()` proves `lab.py` has no import route to them — but arbitrary code running in the same interpreter is contained by neither. Running untrusted experiment bodies would need OS-level isolation that is not built. | Medium |
| 8 | `champion.Contender.complexity` is an integer the author types. The parsimony tie-break is only as honest as that number and nothing validates it. | Low |
| 9 | `evolution.measure_improvement` compares two periods handed to it; choosing favourable ones would produce a favourable verdict. Period selection is an operator decision and is not audited. | Medium |

---

## 6. Verify these claims yourself

```bash
python -m pytest tests/test_trading_*.py -q      # 2427 passing, offline, no torch needed
python -m pytest -q                              # whole repo
python -m pytest tests/test_trading_end_to_end.py -q   # the completion standard
python -m pytest tests/test_trading_kronos_defects.py -q  # teardown regressions
python -m pytest tests/test_trading_boundaries.py -q   # structural guarantees
python -m pytest tests/test_trading_cli.py -q    # the console's negative guarantees
python -m pytest tests/test_trading_kernel.py -q # the safety kernel is unreachable
python -m pytest tests/test_trading_self_evolution.py -q  # the 13 evolution gates
python -m olympus capabilities --check           # Olympus's own CI guard
python -m pytest tests/test_deps_claim.py -q     # dependency truthfulness
```

Every ✅ above corresponds to a test. If a command here fails, this document is
wrong and should be corrected rather than explained away.
