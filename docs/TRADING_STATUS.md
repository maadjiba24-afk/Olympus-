# Olympus Trading Domain — Implementation Status

**The honest ledger.** `docs/TRADING_ARCHITECTURE.md` describes the *target*;
this file records what actually exists, what is tested, and what is unsafe or
missing. When the two disagree, this file is right.

- **Last updated:** 2026-07-27
- **Branch:** `claude/kronos-technical-teardown-54pjna`
- **Scale:** ~15,100 lines across 22 modules; 22 test files; **956 trading tests passing**
- **Existing Olympus suite:** no regressions
- **Operating mode:** `PAPER` (the default; live is disabled)
- **Live trading:** ❌ **DISABLED AND NOT DEMONSTRABLE HERE** — see §4

> **Read §3 and §4 before trusting any of this.** Several completion-standard
> items cannot be demonstrated in this environment, and saying so plainly is
> part of the deliverable.

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
| `kronos_runtime.py` | ✅ | Checkpoint pinning; unpinned refused; `ModelBackend` boundary keeps tokens out of the forecasting layer |
| `kronos_adapter.py` | ✅ | 97 tests incl. a named regression per teardown defect (§3) |
| `forecast.py` | ✅ | Service layer; an exploding forecaster becomes an abstention, never an exception into a strategy |
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

### Not built

| Module | Status | Consequence |
|---|---|---|
| `backtest.py`, `perf.py` | 🔵 | **No event-driven backtester or performance metrics.** The anti-leakage *primitives* are built and tested (`assert_no_lookahead`, `assert_causal`, next-bar fills, fit/transform split), but there is no engine composing them into a run, and none of the required metrics (Sharpe, Sortino, Calmar, profit factor, …) are computed anywhere. |
| `strategy.py` | 🔵 | No concrete strategies. The pipeline accepts any object with `.on_bar(ctx) -> [TradeIntent]`; none ships. |
| `evaluate.py` | 🔵 | **`kronos_is_valuable()` does not exist.** The Kronos-vs-baseline comparison is designed, not implemented. |
| `registry.py` | 🔵 | Model registry absent; `ModelApprovedGate` passes trivially when no models are declared. |
| `sentiment.py` | 🔵 | No news ingestion. The taint barrier it would feed (`risk.Tainted` / `assert_untainted`) **is** built and tested. |
| `monitor.py` | 🔵 | No monitor loop. `killswitch.evaluate_auto_trips()` is built and tested; nothing calls it on a schedule. |
| `agents.py` | 🔵 | No market-intelligence agent schemas. |
| `ingest.py` | 🔵 | No live market-data source. Data enters via `storage`/`validate` from the caller. |

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
| **Paper trade through a real broker sandbox** | ⛔ **Blocked** | No broker credentials, no venue reachable. What exists is a fully functional `PaperBroker` and the ABC a real adapter must satisfy. That is a real simulated venue — it is **not** a demonstration against a broker's sandbox and must not be reported as one. |
| **Run Kronos against the published checkpoints** | ⛔ **Blocked** | `huggingface.co` returns HTTP 403 through this environment's proxy (verified during the teardown). The adapter is tested against a deterministic fake backend: its *logic* is tested, its *numerical agreement with upstream Kronos* is not. |
| **Demonstrate Kronos adds value** | ⛔ **Blocked, and unimplemented** | Needs real checkpoints, real data, **and** `evaluate.py`/`backtest.py`, which do not exist. No claim about Kronos's usefulness is made or supported. Public third-party evidence is currently negative (teardown §16; upstream issues #354/#355). |
| **Backtest without known data leakage** | 🟡 **Partial** | The primitives are built and tested; the engine that composes them is not. |
| **Live trading** | ⛔ **Correctly disabled** | The designed state. At least gates G2 (account), G4 (reconciliation), G7 (connectivity), G8 (paper history) cannot pass without a real venue. |

---

## 5. Known issues and technical debt

| # | Issue | Severity |
|---|---|---|
| 1 | `validate.py` re-implements timeframe parsing that `instruments.py` owns, delegating opportunistically; its delegate path returns early without the positivity re-check. Harmless today (`instruments` rejects `0m`) but two code paths can answer the same question. | Low |
| 2 | Default `min_completeness` of 0.95 marks one missing bar in five as `UNUSABLE`. Correct-but-strict; may be wrong for illiquid instruments. | Low |
| 3 | `risk.py`/`killswitch.py`/`portfolio.py`/`oms.py`/`brokers`/`execution.py`/`reconcile.py`/`audit.py`/`modes.py` were authored directly rather than by the parallel build, so a duplicate-cluster overwrite is possible; all are committed, so any clobber is recoverable by `git checkout`. | Low |
| 4 | No CLI surface (`olympus trading …`). All use is via the Python API. | Medium |

---

## 6. Verify these claims yourself

```bash
python -m pytest tests/test_trading_*.py -q      # 956 passing, offline, no torch needed
python -m pytest tests/test_trading_end_to_end.py -q   # the completion standard
python -m pytest tests/test_trading_kronos_defects.py -q  # teardown regressions
python -m pytest tests/test_trading_boundaries.py -q   # structural guarantees
python -m olympus capabilities --check           # Olympus's own CI guard
python -m pytest tests/test_deps_claim.py -q     # dependency truthfulness
```

Every ✅ above corresponds to a test. If a command here fails, this document is
wrong and should be corrected rather than explained away.
