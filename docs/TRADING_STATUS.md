# Olympus Trading Domain — Implementation Status

**The honest ledger.** `docs/TRADING_ARCHITECTURE.md` describes the *target*;
this file records what actually exists, what is only designed, what is tested,
and what is unsafe or incomplete. When the two disagree, this file is right.

- **Last updated:** 2026-07-27
- **Branch:** `claude/kronos-technical-teardown-54pjna`
- **Operating mode:** `PAPER` (default; live trading is disabled)
- **Live trading:** ❌ **DISABLED AND NOT DEMONSTRABLE** — see §4

> **Read §4 before using any of this.** Several completion-standard items
> cannot be demonstrated in this environment, and saying so is part of the
> deliverable.

---

## 1. Status legend

| Mark | Meaning |
|---|---|
| ✅ **Built + tested** | Implemented, with tests that actually exercise the behaviour, passing offline |
| 🟡 **Built, thinly tested** | Implemented; tests cover the happy path but not the adversarial surface |
| 🔵 **Designed only** | Specified in the architecture doc; no code |
| ⛔ **Blocked** | Cannot be built or demonstrated in this environment; reason stated |

---

## 2. Module status

*(Updated as the build lands. Rows marked 🔵 have a written specification in
`docs/TRADING_ARCHITECTURE.md` but no implementation yet.)*

### Foundation

| Module | Status | Evidence |
|---|---|---|
| `contracts.py` | ✅ | Frozen dataclasses; invariants enforced at construction (OHLC coherence, UTC-only, `REJECTED ⟹ quantity 0`, stop on the protective side). Verified by probe + `test_trading_boundaries.py` |
| `errors.py` | ✅ | Stable typed codes; unsupported configs raise rather than silently corrupt |
| `clock.py` | ✅ | Injectable time; `FixedClock` refuses to move backwards |
| `__init__.py` | ✅ | Lazy submodule loading; subprocess test proves `import olympus.trading` pulls in no torch/numpy/pandas/scipy |

### Market analysis

| Module | Status | Evidence |
|---|---|---|
| `instruments.py` | ✅ | Sessions verified across the 2026-03-08 US DST transition; epoch-anchored timeframe grid; sealable (immutable) registry |
| `storage.py` | ✅ | Probed: idempotent dedupe, ascending order, exact float round-trip, cross-instance persistence |
| `validate.py` | ✅ | Probed: gaps detected without fabricating bars, dedupe keeps last, `assert_no_lookahead` keyed on `ts_close` (not `ts_open`) and catches a future bar |
| `candles.py` | ✅ | Probed: 1m→5m resample with correct OHLC aggregation and the trailing partial bucket withheld |

### Composition

| Module | Status | Evidence |
|---|---|---|
| `pipeline.py` | ✅ | 20 tests in `test_trading_pipeline.py` prove the central claim: no order exists without an approving `RiskDecision`. Fails closed on a throwing risk engine, an unreadable kill-switch registry, and a broken mode controller (falls back to `PAPER`, never live). Execution sizes from `decision.approved_quantity`, never the intent's. Strategy context asserted to expose no `broker`/`oms`/`execution`/`limits`/`vault`. **Caveat:** proven against fakes that enforce the same preconditions the real components must; the real `risk.py`/`execution.py` need their own behavioural tests. Written against the authored specs, so integration reconciliation is expected. |

### Remaining modules

Status for `ta`, `features`, `regime`, `volatility`, `kronos_runtime`,
`kronos_adapter`, `forecast`, `signals`, `risk`, `killswitch`, `portfolio`,
`oms`, `brokers/`, `execution`, `reconcile`, `audit`, `modes`, `backtest`,
`perf`, `strategy`, `evaluate`, `registry`, `sentiment`, `monitor`, `agents`
is recorded here as each lands. Until a row says ✅, treat the capability as
**not present**, regardless of what the architecture document describes.

---

## 3. Structural guarantees currently enforced by tests

| Guarantee | Enforced by | State |
|---|---|---|
| `strategy.py` has no import path to a broker, the OMS, or the execution engine | `test_trading_boundaries.py::test_strategy_module_has_no_execution_surface` | ✅ harness live (asserts once `strategy.py` exists) |
| Layer boundary graph (analysis ⊥ execution, forecaster ⊥ broker, risk ⊥ LLM) | `test_trading_boundaries.py::test_layer_boundaries_are_respected` | ✅ harness live, 29 module rules |
| No heavy dependency at module scope anywhere in the domain | `test_no_heavy_imports_at_module_scope` | ✅ passing |
| `import olympus.trading` does not pull in torch | subprocess probe | ✅ passing |
| Every lazily-imported third party is declared in `pyproject` | `tests/test_deps_claim.py` (pre-existing CI guard) | ✅ passing with the new `kronos` extra |

---

## 4. What is **not** demonstrable in this environment

Stated explicitly, per the task's stop-and-report rule.

| Completion-standard item | State | Reason |
|---|---|---|
| **Paper trade through a real broker sandbox or paper account** | ⛔ **Blocked** | No broker credentials and no outbound access to any venue. What is built instead is a `BrokerAdapter` ABC plus a fully functional `PaperBroker` (fills, fees, slippage, partial fills, rejections, outages, forced desync). That is a real simulated venue and a real contract for adapters — it is **not** a demonstration against a broker's sandbox, and must not be reported as one. |
| **Run Kronos forecasts against the real published checkpoints** | ⛔ **Blocked** | `huggingface.co` returns HTTP 403 through this environment's proxy (verified during the teardown), and torch is not installed. The adapter is therefore tested against a deterministic fake backend. Its *logic* — abstention, normalisation, path preservation, OHLC repair, config rejection — is tested; its *numerical agreement with upstream Kronos* is **not**. |
| **Demonstrate measurable Kronos value** | ⛔ **Blocked** | Requires both real checkpoints and real market data. `evaluate.kronos_is_valuable()` exists so the question is decided by measurement; with no data it returns `False`, which is the correct default. Public third-party evidence is currently negative (teardown §16; upstream issues #354/#355). |
| **Live trading** | ⛔ **Correctly disabled** | Not a limitation — the designed state. Nine deployment gates plus an operator token are required, and at least G2 (account verified), G4 (reconciliation), G7 (broker connectivity) and G8 (paper-trading history) cannot pass without a real venue. |

---

## 5. Known issues and technical debt

| # | Issue | Severity | Plan |
|---|---|---|---|
| 1 | `validate.py` re-implements timeframe parsing that `instruments.py` owns, delegating opportunistically. Its delegate path returns early without the positivity re-check — harmless today because `instruments` rejects `0m`, but it is duplicated logic with two possible answers. | Low | Collapse to a single owner (`instruments`) in the consolidation pass |
| 2 | The default `min_completeness` of 0.95 marks a window with one missing bar in five as `UNUSABLE`. Correct-but-strict; may be too strict for illiquid instruments. | Low | Document; make per-instrument configurable |

---

## 6. How to verify these claims yourself

```bash
# the whole trading suite, offline, no torch required
python -m pytest tests/test_trading_*.py -q

# the structural guarantees specifically
python -m pytest tests/test_trading_boundaries.py -q

# the pre-existing repo guards this integration must not break
python -m pytest tests/test_deps_claim.py -q
python -m olympus capabilities --check
```

Every claim marked ✅ above corresponds to a test or a reproducible probe. If a
command here fails, this document is wrong and should be corrected rather than
explained away.
