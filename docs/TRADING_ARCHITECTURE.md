# Olympus Trading Domain — Architecture & Design

Design record for the `olympus.trading` domain: a modular market-intelligence
and algorithmic-trading platform in which **Kronos is one forecasting component
among several**, not a strategy, not a risk system, and not an execution engine.

This document covers the fourteen design deliverables required before
implementation: current-state map, proposed architecture, gap analysis, threat
model, data contracts, module boundaries, storage schema, event definitions,
broker interface, risk policy, backtesting design, phased plan, test plan, and
deployment gates.

**Companion documents**
- `docs/KRONOS_TEARDOWN.md` — the verified source-level teardown the Kronos
  adapter is built against. Every defect repaired below cites a section there.
- `docs/TRADING_STATUS.md` — the honest, continuously-updated statement of what
  is implemented, what is only designed, what is tested, and what is unsafe.
  **Read that before trusting anything here to be built.**

> **Standing rule.** This document describes the *target*. It is not evidence
> that any of it works. `docs/TRADING_STATUS.md` is the evidence.

---

## 1. Current-state architecture map

Olympus today (v0.26.0, 209 modules, ~85k LOC, 322 test files) is a
controlled-autonomy multi-agent assistant. It has no trading capability
whatsoever — but it already ships most of the *safety* machinery a trading
system needs, which is why this integration is mostly composition rather than
invention.

### 1.1 What exists

| Layer | Modules | What it provides |
|---|---|---|
| **Agent core** | `orchestrator.py` (3151), `agent.py`, `specialists.py`, `llm.py`, `moa.py`, `consensus.py` | Zeus/council orchestration, 13 specialists, hallucination controller |
| **Tool surface** | `tools.py` (4653), `toolselect.py`, `connectors.py`, `mcp_client.py` | 130 named tools, per-specialist loadouts, dynamic selection |
| **Security** | `security.py` (813), `cmdguard.py`, `capprofile.py`, `egress.py`, `sandbox.py` | Capability separation, untrusted-content envelopes, injection sanitising, egress pinning |
| **Action spine** | `actions.py`, `approvals.py`, `builtin_actions.py` | Risk classes (`TRIVIAL`→`FINANCIAL_LEGAL`), autonomy levels, prepare→approve→execute→undo, scopes, daily limits |
| **Authorisation** | `mandate.py` (590), `mandate_store.py` | AP2-style signed intent/cart mandates, amount caps, user co-signature, containment checks |
| **Audit** | `ledger.py` (445), `attest.py`, `sessionlog.py`, `trace.py` | **Hash-chained, Ed25519-signed, verifiable append-only ledger** with replay hashing and tail healing |
| **Secrets** | `vault.py`, `secretref.py` | Fernet-encrypted secret storage keyed by user |
| **Persistence** | `store.py` | `(namespace, key) → bytes`; FileStore default, PostgresStore via `OLYMPUS_DATABASE_URL` |
| **Concurrency** | `proclock.py` | Cross-process file locks for read-modify-write |
| **Ops** | `health.py`, `watchdog.py`, `metrics.py`, `otel.py`, `scheduler.py`, `heartbeat.py` | Health probes, supervision, metrics, scheduling |
| **Governance** | `capabilities.py`, `modelpin.py`, `modelgate.py` | CI-enforced capability manifest; model pinning/gating |

### 1.2 The three properties that constrain every design decision here

1. **Three required dependencies.** `anthropic`, `youtube-transcript-api`,
   `cryptography`. Everything else is a lazily-imported, guarded extra. A
   trading domain that made numpy/pandas/torch mandatory would destroy the
   single most distinctive property of the product.
2. **CI is strict.** `requirements.lock` is a universal hash lock installed with
   `--require-hashes`; `python -m olympus capabilities --check` fails the build
   if a published count drifts; `scripts/check_threat_model.py` fails if the
   tool surface and its threat model diverge. New capabilities must be
   registered, not smuggled in.
3. **Deny-first is the house style.** Sensitive actions are *prepared*, never
   performed, until a human approves. The trading domain must not be the one
   place that breaks that pattern — it is the place where it matters most.

### 1.3 What Olympus does **not** have

No market data of any kind. No instrument model, no sessions/calendars, no
candles, no positions, no orders, no broker connectivity, no P&L, no
backtester, no financial risk model. `actions.FINANCIAL_LEGAL` and
`mandate.PAYMENT_SCOPE` govern *payments*, not *trading*: a payment is a single
bounded transfer with a cap, whereas a position is an open-ended, mark-to-market
exposure that can lose money after the authorised action completes. That
difference is why trading needs its own deterministic risk engine rather than
reusing the payment mandate.

---

## 2. Proposed trading architecture

### 2.1 The seven layers

The core architectural rule is strict separation. Each layer consumes only the
layer above's output contract and can be tested — and replaced — alone.

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. MARKET ANALYSIS      ingest → validate → store → candles → features │
│                         ta · regime · volatility · sentiment           │
│                         (no forecasts, no opinions about trades)       │
├────────────────────────────────────────────────────────────────────────┤
│ 2. FORECAST GENERATION  forecast.ForecastService                       │
│                         ├── kronos_adapter.KronosForecaster            │
│                         ├── PersistenceForecaster   ← the baseline     │
│                         └── Drift / SeasonalNaive                      │
│                         OUTPUT: ForecastResult (may ABSTAIN)           │
├────────────────────────────────────────────────────────────────────────┤
│ 3. SIGNAL GENERATION    signals.*Generator → Signal                    │
│                         signals.SignalFusion → FusedSignal             │
├────────────────────────────────────────────────────────────────────────┤
│ 4. STRATEGY DECISIONS   strategy.Strategy.on_bar(ctx) → [TradeIntent]  │
│                         ── PROPOSALS ONLY. No broker reference. ──     │
├────────────────────────────────────────────────────────────────────────┤
│ 5. RISK AUTHORISATION   risk.RiskEngine.authorise(intent, ctx)         │
│                         → RiskDecision {APPROVED|REDUCED|REJECTED}     │
│                         DETERMINISTIC · NO LLM · NO NETWORK            │
│                         killswitch.KillSwitchRegistry checked FIRST    │
├────────────────────────────────────────────────────────────────────────┤
│ 6. ORDER EXECUTION      execution.ExecutionEngine (needs an APPROVING  │
│                         decision to do anything at all)                │
│                         oms.OrderStore · brokers.BrokerAdapter         │
├────────────────────────────────────────────────────────────────────────┤
│ 7. RECONCILIATION       reconcile.Reconciler · portfolio.PortfolioMgr  │
│                         monitor.TradingMonitor → auto kill-switch trips│
└────────────────────────────────────────────────────────────────────────┘
        every step emits → audit.AuditTrail → olympus.ledger (signed)
```

### 2.2 The structural guarantee

> **No language model, forecasting model, or autonomous agent can submit an
> order.**

This is enforced by construction, not by policy text:

- `Strategy` objects are constructed with **no reference** to a broker, the OMS,
  the execution engine, or the limits store. There is no attribute to reach
  through. A strategy's only output type is `TradeIntent`, which is inert.
- `ExecutionEngine.execute(intent, decision, instrument)` **requires** a
  `RiskDecision`. It verifies `decision.intent_id == intent.intent_id`, that the
  verdict is not `REJECTED`, and that the decision is not stale. It sizes from
  `decision.approved_quantity` — never from the intent.
- `RiskEngine` imports nothing from the LLM stack and performs no I/O. Given the
  same `(intent, context, limits)` it returns the same decision, byte for byte.
- `RiskLimits` is a frozen dataclass behind a `LimitsStore` that requires an
  operator token. Anything derived from external content carries a `TAINTED`
  marker and is refused by `assert_untainted` at the limits API.

The LLM's role in this system is **advisory only**: agents produce validated,
schema-checked analysis objects that become `Signal`s. A signal can, at most,
cause a strategy to *propose* a trade that the deterministic engine then judges.

### 2.3 Where Kronos sits

Kronos is a `Forecaster` implementation behind an Olympus-owned interface. It is
one of at least four registered forecasters, and the `PersistenceForecaster`
baseline exists specifically so `evaluate.kronos_is_valuable()` can answer, from
measured out-of-sample data net of costs, whether Kronos earns its place. The
default answer is **False**.

---

## 3. Gap analysis

| # | Capability | Olympus today | Gap | Resolution |
|---|---|---|---|---|
| 1 | Market data ingestion | none | total | `ingest.py` + `storage.py` |
| 2 | Data validation | `security.sanitize_*` (text only) | no numeric/temporal validation | `validate.py` |
| 3 | Instrument/session model | none | total | `instruments.py` |
| 4 | Feature engineering | none | total | `features.py`, `ta.py` |
| 5 | Forecasting | none | total | `forecast.py`, `kronos_adapter.py` |
| 6 | Signal generation/fusion | none | total | `signals.py` |
| 7 | Strategy management | `specialists.py` (LLM agents) | wrong abstraction — agents are advisory | `strategy.py` |
| 8 | Portfolio accounting | none | total | `portfolio.py` |
| 9 | **Deterministic risk engine** | `actions.py` risk *classes*; `mandate.py` amount caps | Neither models open-ended market exposure. A mandate authorises a bounded transfer; a position keeps losing after authorisation. | `risk.py` — new, deterministic |
| 10 | Kill switches | `watchdog.py` (process supervision) | no financial trips | `killswitch.py` |
| 11 | Order management | none | total | `oms.py` |
| 12 | Broker adapters | `connectors.py` (LLM tool connectors) | wrong trust model — connectors serve agents | `brokers/` |
| 13 | Execution engine | `actions._execute` | no idempotency, partial fills, or collars | `execution.py` |
| 14 | Reconciliation | none | total | `reconcile.py` |
| 15 | Backtesting | `evals.py` (LLM quality) | unrelated | `backtest.py` |
| 16 | Performance metrics | `metrics.py` (HTTP counters) | unrelated | `perf.py` |
| 17 | **Audit trail** | `ledger.py` — hash-chained, signed, verifiable | **small gap: needs trading event types + correlation tracing** | `audit.py` *on top of* `ledger.py` |
| 18 | **Secrets** | `vault.py` — Fernet, per-user | **no gap** | reuse directly |
| 19 | **Persistence** | `store.py` — file/Postgres | **no gap** | reuse directly |
| 20 | **Cross-process locking** | `proclock.py` | **no gap** | reuse directly |
| 21 | Operating modes | `actions.autonomy_level` (L0–L3) | not trading-shaped; no gates | `modes.py` |
| 22 | Model registry | `modelpin.py`, `modelgate.py` (LLMs) | close but LLM-specific | `registry.py` |
| 23 | Monitoring | `health.py`, `watchdog.py` | no trading probes | `monitor.py` |
| 24 | Untrusted news boundary | `security.wrap_untrusted` | pattern exists; needs a taint marker reaching the limits API | `sentiment.py` + `TAINTED` |

**Reused rather than rebuilt:** `store`, `vault`, `proclock`, `ledger`. Four of
the hardest, most security-sensitive pieces already exist and are already
tested. That is the single biggest reason this integration is tractable.

---

## 4. Threat model

Follows the house format of `docs/THREAT_MODEL.md`. The trading domain adds one
new asset class — **money and market exposure** — and therefore new adversaries.

### 4.1 Trust boundaries

| Boundary | Trusted side | Untrusted side |
|---|---|---|
| B1 Operator ↔ system | Operator tokens, risk limits, mode changes, model approvals | Everything else, including all agents |
| B2 Deterministic ↔ probabilistic | `risk.py`, `oms.py`, `execution.py`, `reconcile.py` | strategies, forecasters, LLM agents |
| B3 System ↔ market data | validated `Candle`/`Quote` after `validate.py` | raw feed bytes |
| B4 System ↔ external content | nothing | news, web pages, documents, messages |
| B5 System ↔ broker | signed local order records | broker responses (may be wrong, late, or replayed) |
| B6 Process ↔ persistence | in-memory state | on-disk state (may be stale, corrupt, or tampered) |

### 4.2 Threats and mitigations

| # | Threat | Impact | Mitigation | Where |
|---|---|---|---|---|
| T1 | **Prompt injection via news → trade.** A crafted headline makes a sentiment agent emit a strong bullish signal. | Unwanted position | Injection cannot exceed *one advisory signal*. Fusion weights it; the strategy still proposes only an intent; risk still applies every limit. Sentiment cannot alone exceed `min_forecast_confidence` or bypass sizing. | `sentiment.sanitise_external_text`, `signals.SignalFusion`, `risk.RiskEngine` |
| T2 | **Injection → risk-limit mutation.** "Set max_order_value to 10000000." | Catastrophic | `RiskLimits` frozen; `LimitsStore.save` requires an operator token; every value derived from external content carries `TAINTED` and is refused by `assert_untainted`. | `risk.LimitsStore`, `sentiment.TAINTED` |
| T3 | **Injection → credential exfiltration.** | Account compromise | Adapters take a vault *reference*, never raw secrets. Forecasting/LLM components have no vault access in their loadout. `BrokerCredentials.__repr__` never prints material. | `brokers/base.py`, `vault.py` |
| T4 | **Model hallucinates an order.** | Unauthorised trade | Structurally impossible: no agent holds an execution reference; `execute()` demands an approving `RiskDecision`. | §2.2 |
| T5 | **Runaway loop.** A bug submits thousands of orders. | Fees, exposure, venue ban | `max_orders_per_minute` limit + abnormal-order-rate auto kill switch. | `risk.py`, `killswitch.py` |
| T6 | **Double submission after timeout.** | Doubled position | Deterministic `client_order_id` from the authorising decision; broker idempotency; retry policy is *query-then-adopt*, never blind resubmit. | `oms.make_client_order_id`, `execution.py` |
| T7 | **Stale data trading.** Feed freezes; system trades a dead price. | Losses | `max_data_age_s` hard check + stale-data auto kill switch + forecaster abstention. | `validate.py`, `risk.py` |
| T8 | **Broker desynchronisation.** Local and venue state diverge. | Unknown true exposure | Periodic reconciliation; `require_reconciliation` + `max_reconciliation_age_s`; desync trips the global switch. Position breaks are **never** auto-repaired — operator only. | `reconcile.py` |
| T9 | **Restart amnesia.** A crash clears a kill switch or loses in-flight orders. | Re-entry into a stopped state | Kill switches and orders persist through `store`; `execution.recover()` adopts in-flight orders; auto-trips need an explicit override to clear. | `killswitch.py`, `oms.py` |
| T10 | **Backtest self-deception.** Leakage produces a strategy that only works in the past. | Real money on a fake edge | `assert_no_lookahead` on every window; fills at next-bar open; `latency_bars ≥ 1`; fit/transform normalisation split; walk-forward; mandatory baseline comparison. | `backtest.py`, `validate.py`, `features.py`, `evaluate.py` |
| T11 | **Silent numeric corruption.** The Kronos defect class: an unsupported config returns plausible wrong numbers. | Wrong forecasts trusted | Every unsupported config raises `ConfigurationError`; OHLC coherence repaired and warned; unpinned checkpoints refused. | `kronos_adapter.py`, `kronos_runtime.py` |
| T12 | **Tampered audit trail.** | Loss of accountability | `ledger.py` hash chain + Ed25519 signatures; `AuditTrail.verify()` detects tampering. | `audit.py` |
| T13 | **Unauthorised live enablement.** | Real money without gates | Default `PAPER`; live requires *all* gates + operator token + immutable audit event. Not reachable by config file or by any agent. | `modes.py` |
| T14 | **Malicious/compromised checkpoint.** | Adversarial forecasts | Pinned `repo_id` + git revision (+ optional sha256); unpinned refused; only `approved` registry models usable live. | `kronos_runtime.py`, `registry.py` |
| T15 | **Concentration via many small approvals.** Each trade passes; the aggregate does not. | Blow-up | Every exposure check evaluates the **projected** portfolio after the fill, not the current one. | `risk.py` |

### 4.3 Explicit non-goals

Not defended against, and stated so no one assumes otherwise: a compromised host
or Python process; a malicious operator; broker-side fraud; market
manipulation by others; exchange outages beyond failing safe; latency
competition (this is not an HFT system and must never be used as one).

---

## 5. Data contracts

Implemented and frozen in `olympus/trading/contracts.py`. Full field lists live
in the source; the design decisions are here.

### 5.1 Two numeric domains, never mixed

| Domain | Type | Used for | Why |
|---|---|---|---|
| Analytics | `float` | candles, forecasts, indicators, features | market data is float; exactness is meaningless for a predicted price |
| Accounting & execution | `Decimal` | order quantity/price, fills, positions, cash, exposure, P&L | binary floats produce quantities brokers reject and P&L that will not reconcile to the cent |

Conversion happens in exactly **one** place — position sizing — always through
`Instrument.quantize_quantity` (rounds **down**, because rounding a size up can
breach the very limit that produced it) and `quantize_price` (half-even).

### 5.2 Time

Every timestamp is timezone-aware UTC; `ensure_utc` rejects naive datetimes at
construction. `Candle` carries **both** `ts_open` and `ts_close`, which removes
the most common source of off-by-one-bar look-ahead: code that reads a bar
stamped with its open time and assumes the close was knowable then. Nothing may
act on a bar until the clock has passed `ts_close` and `is_final` is true.

### 5.3 The contracts

`Instrument`, `Candle`, `Quote`, `DataQualityReport`, `ForecastPath`,
`ForecastResult`, `Signal`, `TradeIntent`, `RiskCheck`, `RiskDecision`, `Order`,
`Fill`, `Position`, `AccountSnapshot`, `PortfolioSnapshot`.

Invariants enforced at construction (each has a test):
- `Candle`: OHLC coherence, positive prices, non-negative volume, `ts_close > ts_open`.
- `Quote`: no crossed book.
- `ForecastResult`: `uncertainty ∈ [0,1]`; an abstaining result **must** carry a
  `failure_reason`; a non-abstaining result's `mean_close` length **must** equal
  `horizon`.
- `TradeIntent`: positive quantity; a limit order must carry a limit price; a
  stop must be on the protective side of the entry.
- `RiskDecision`: `REJECTED ⟹ approved_quantity == 0`; approving ⟹ `> 0`. There
  is no such thing as a zero-size approval.
- `Order`: `0 ≤ filled_quantity ≤ quantity`.

### 5.4 Forecast result contract

`ForecastResult` carries everything a consumer needs to decide *whether to trust
it*: instrument, timeframe, input window start/end, horizon, **individual
sampled paths**, mean, median, quantiles, expected return (total and per step),
forecast volatility, realised volatility, direction probabilities, uncertainty,
model version, **model identity** (repo + revision + hash), **inference
params**, data-quality status, warnings, `abstained`, `failure_reason`,
`failure_code`, and latency.

Two deliberate departures from upstream Kronos:

1. **Paths are preserved.** Upstream averages samples *inside* its inference
   function (`kronos.py:467`), destroying the dispersion a risk engine needs.
   Here the mean is a *view*, not the result.
2. **Abstention is a first-class outcome**, not an exception —
   `ForecastResult.abstain(...)`. Bad data is normal; the pipeline must record
   the refusal like any other result rather than throwing.

---

## 6. Module boundaries

| Module | May import | May **not** import | Rationale |
|---|---|---|---|
| `contracts`, `errors`, `clock` | stdlib only | anything in the domain | the spine cannot depend on its dependents |
| `instruments`, `storage` | contracts, errors, clock, `olympus.store`, `proclock` | forecast, strategy, risk, execution | reference data is inert |
| `validate`, `candles` | contracts, errors, clock, instruments | anything downstream of analysis | |
| `ta`, `features`, `regime`, `volatility` | contracts, clock, instruments, ta | forecast, strategy, risk, execution, brokers | analysis knows nothing about trading |
| `sentiment` | contracts, errors, clock, `olympus.security` | risk, execution, brokers, vault | **hard boundary**: external content must never reach limits or credentials |
| `kronos_runtime`, `kronos_adapter` | contracts, errors, clock, validate, instruments | signals, strategy, risk, execution, oms, brokers | a forecaster must not be able to trade |
| `forecast`, `signals` | contracts, clock, validate, analysis modules, audit | strategy, risk, execution | |
| `strategy` | contracts, clock, signals, features, regime, volatility, portfolio (read-only snapshot) | **brokers, oms, execution, risk limits** | the structural no-direct-trading guarantee |
| `risk`, `killswitch` | contracts, errors, clock, portfolio, `olympus.store`, audit | LLM stack, network, brokers, strategies | determinism |
| `portfolio`, `oms` | contracts, errors, clock, `olympus.store`, proclock | strategies, forecasters | |
| `brokers/*` | contracts, errors, clock, `olympus.vault` | strategy, risk, forecast | |
| `execution`, `reconcile` | contracts, errors, clock, oms, portfolio, brokers, audit, modes | strategy, forecast, signals | execution takes a decision, not an opinion |
| `backtest`, `perf`, `evaluate` | everything (it composes the whole system) | — | must use the **same** strategy/risk/OMS code as live |
| `audit`, `modes`, `registry`, `monitor` | contracts, clock, `olympus.ledger`/`store` | strategies, forecasters | |

A test asserts the forbidden-import graph so the boundaries cannot silently rot.

---

## 7. Storage schema

Persistence rides on `olympus.store` — `(namespace, key) → bytes` — so the whole
domain inherits the existing file/Postgres switch with no new database
dependency. Namespaces (all prefixed `trading.`):

| Namespace | Key | Value | Notes |
|---|---|---|---|
| `trading.candles` | `{exchange}:{symbol}:{timeframe}` | NDJSON of candles | append-only; dedupe on `ts_open`, last wins; corrupt lines skipped and counted |
| `trading.instruments` | `{exchange}:{symbol}` | JSON `Instrument` | operator-managed |
| `trading.orders` | `{client_order_id}` | JSON `Order` | write-through on every transition |
| `trading.orders.index` | `open` / `by_broker` | JSON index | rebuilt from records on load |
| `trading.fills` | `{fill_id}` | JSON `Fill` | idempotent by `fill_id` |
| `trading.portfolio` | `{account_id}` | JSON positions + cash + equity curve | `proclock` on read-modify-write |
| `trading.killswitch` | `{scope}:{target}` | JSON `KillSwitchState` | **must survive restart** |
| `trading.limits` | `active` / `v{n}` | JSON `RiskLimits` + provenance | versioned; operator token required |
| `trading.mode` | `current` | JSON mode + gates + operator | default `PAPER` when absent |
| `trading.registry` | `{model_id}` | JSON `ModelRecord` | |
| `trading.reconcile` | `last_success` / `{ts}` | JSON report | feeds the freshness risk check |
| `trading.strategy` | `{strategy_id}` | JSON status + drawdown state | |
| `trading.audit` | via `olympus.ledger` | hash-chained signed nodes | **not** a plain KV namespace |

**Why NDJSON and KV rather than SQL.** A required Postgres would break the
zero-setup property. The access patterns are append-heavy and key-lookup-heavy,
which KV serves well. Operators who want SQL set `OLYMPUS_DATABASE_URL` and the
same code persists to Postgres unchanged.

---

## 8. Event definitions

Every event is a `TradingEvent(event_id, ts, type, actor, subject,
correlation_id, payload, schema_version)` appended to the signed ledger.

| Event | Emitted by | Key payload |
|---|---|---|
| `MARKET_DATA_RECEIVED` | `ingest` | instrument, timeframe, bar count, source, span |
| `DATA_VALIDATED` | `validate` | `DataQualityReport` |
| `FORECAST_PRODUCED` | `forecast` | full `ForecastResult` incl. model identity + params |
| `AGENT_OUTPUT` | `agents` | validated schema object, agent key, taint flag |
| `TRADE_INTENT` | `strategy` | full `TradeIntent` incl. supporting signals |
| `RISK_DECISION` | `risk` | full `RiskDecision` incl. **every** check and `policy_hash` |
| `ORDER_SUBMITTED` | `execution` | `Order`, `client_order_id`, attempt, mode |
| `BROKER_RESPONSE` | `execution` | raw status, broker id, latency, error |
| `FILL` | `execution` | `Fill` |
| `POSITION_CHANGED` | `portfolio` | before/after position, realised P&L |
| `CONFIG_CHANGED` | `risk`, `modes` | old/new hash, operator, reason |
| `MODEL_CHANGED` | `registry` | model id, revision, status, operator |
| `KILL_SWITCH` | `killswitch` | scope, target, engaged, reason, auto |
| `MODE_CHANGED` | `modes` | old, new, gate results, operator |
| `RECONCILIATION` | `reconcile` | report incl. every break |
| `ERROR` | any | error code, module, context |
| `HUMAN_INTERVENTION` | CLI | operator, action, reason |

### Traceability

`correlation_id` threads one causal chain:

```
MARKET_DATA_RECEIVED → DATA_VALIDATED → FORECAST_PRODUCED → TRADE_INTENT
   → RISK_DECISION → ORDER_SUBMITTED → BROKER_RESPONSE → FILL → POSITION_CHANGED
```

`AuditTrail.trace(client_order_id)` returns that chain, which is how the
requirement "every live order must be traceable back to its source market data,
forecast, strategy version, model version, risk checks, final authorisation, and
broker response" is satisfied from the record alone.

---

## 9. Broker-interface specification

`brokers.base.BrokerAdapter` (ABC). The **only** place the system talks to a venue.

```
identity      name, venue, supports (capability dict)
lifecycle     connect(credentials) · disconnect() · is_connected() · health()
account       get_account() -> AccountSnapshot
              get_positions() -> [Position]
orders        get_open_orders() · get_order(client_order_id)
              submit_order(order) -> Order        # honours client_order_id idempotency
              cancel_order(client_order_id)
              replace_order(client_order_id, quantity=, limit_price=)
fills         get_fills(since=) -> [Fill]
market data   get_quote(instrument_key) · get_candles(instrument_key, tf, start, end)
```

**Credential rule (security-critical).** Adapters receive a
`BrokerCredentials(ref, user)` — an opaque *vault reference*, never raw secrets
— and resolve it through `olympus.vault` themselves. `__repr__`/`__str__` never
print secret material (tested). Forecasting and LLM components have no path to
credentials.

**Capability declaration.** `supports` lets the OMS refuse an unsupported order
type up front rather than discovering it at the venue.

**Implementations**
- `brokers/paper.PaperBroker` — a *fully functional* simulated venue with fee and
  slippage models, partial fills, rejections, outages, and a `force_desync()`
  hook so reconciliation failure is testable. It is both the paper-trading venue
  and the backtester's fill engine, which is what makes backtest and paper share
  one code path.
- Real adapters are **not** shipped. Writing one against an untested sandbox and
  calling it "supported" is precisely the overclaim this project refuses. The
  ABC plus the paper implementation is the contract a real adapter must satisfy.

---

## 10. Risk-policy specification

`risk.RiskEngine.authorise(intent, ctx) -> RiskDecision`. Deterministic: no
network, no LLM, no randomness, no clock read except the injected one. Same
inputs ⟹ identical decision including `policy_hash`.

### 10.1 Evaluation order

1. **Kill switches** (global → strategy → instrument). Any engaged switch
   short-circuits to `REJECTED`.
2. **Mode** — is the requested mode in `allowed_modes`?
3. **Static permissions** — instrument, exchange, order type approved?
4. **Data integrity** — freshness, quality, spread, price deviation, liquidity.
5. **Forecast quality** — `min_forecast_confidence`, `max_forecast_uncertainty`.
6. **Session** — market open, if required.
7. **Protection** — stop loss present, if required.
8. **Operational** — broker connected, reconciliation recent, order rate.
9. **Sizing limits** (may *reduce*) — order value, position size, concentration.
10. **Projected exposure** (may *reduce*) — gross, net, leverage, position count,
    correlated exposure, evaluated on the **post-fill** portfolio.
11. **Loss limits** — daily loss, strategy drawdown, portfolio drawdown.
12. **Floor** — if the surviving quantity is below `min_quantity`/`min_notional`,
    the verdict is `REJECTED`, not a zero-size approval.

Every check that runs — pass *and* fail — is recorded as a `RiskCheck`. The
audit trail must show what was evaluated, not merely what tripped.

### 10.2 Authority

The engine may **reduce** or **refuse**. It may never increase a quantity,
loosen a limit, or invent an order. `APPROVED_REDUCED` exists so a partial
authorisation is visibly distinct from a clean one.

### 10.3 Limits are operator-owned

`RiskLimits` is frozen; `LimitsStore.save(limits, operator_token, reason)`
persists a new version with provenance and an audit event. Analysis agents,
strategies, and anything carrying `TAINTED` are refused with
`LimitsImmutableError`. `policy_hash()` is a stable SHA-256 over the
canonicalised limits, written into every decision so a historical decision can
be replayed against exactly the policy that produced it.

### 10.4 Kill switches

| Switch | Scope | Trip |
|---|---|---|
| Global | everything | manual, or any auto rule below |
| Per-strategy | one strategy | manual, or strategy drawdown |
| Per-instrument | one instrument | manual, or instrument-specific anomaly |
| Daily-loss | global | realised+unrealised daily loss ≥ `max_daily_loss` |
| Drawdown | global / strategy | portfolio or strategy drawdown breach |
| Order-rate | global | orders per window ≥ `max_orders_per_minute` |
| Stale-data | global | freshest data older than `max_data_age_s` |
| Broker-desync | global | reconciliation failed or older than `max_reconciliation_age_s` |

All persist through `olympus.store` — a restart cannot silently clear one.
Automatic trips require an explicit operator override to clear; ordinary code
paths cannot.

---

## 11. Backtesting design

The backtester exists to *avoid self-deception*, so its design is organised
around the specific ways backtests lie.

**Same code, different clock.** The engine drives the *real* `Strategy`,
`RiskEngine`, `OrderStore`, and `PaperBroker`. Only the clock (`FixedClock`) and
the data source differ from paper trading. A strategy that trades in backtest
runs unchanged in paper and live.

**Event loop.** Bars merged across instruments and processed in strict
`ts_close` order:

1. advance the clock to `bar.ts_close`
2. hand the strategy only bars with `ts_close ≤ clock.now()`, enforced by
   `validate.assert_no_lookahead` on **every** window
3. strategy proposes `TradeIntent`s
4. the real risk engine authorises
5. approved orders reach `PaperBroker` and fill **no earlier than the next
   bar's open** (`latency_bars ≥ 1`)
6. portfolio marks to the current close; equity curve recorded

**Bias defences** — each with a dedicated test:

| Bias | Defence |
|---|---|
| Look-ahead | `assert_no_lookahead` on every window; bar delivered only after the clock passes `ts_close` |
| Fill-at-signal-price | market orders fill at the *next* bar's open plus slippage, never the current close |
| Target leakage | features are causal; `assert_causal()` |
| Future-data normalisation | fit/transform split — scaling statistics come from the train window only (the exact defect in Kronos `finetune_csv`, teardown §12.9) |
| Survivorship | delisting/inclusion map; the engine **warns** when a universe is static over a long window |
| Unrealistic fills | limit orders fill only if the bar traded through; partial fills; rejections |
| Ignored costs | fees, spread, and slippage always modelled; results reported gross **and** net |
| Timestamp misalignment | `ts_open`/`ts_close` both carried; UTC enforced |

**Validation protocol.** Rolling and anchored walk-forward, out-of-sample only.
Every Kronos strategy is compared against the *same strategy without Kronos*
(`BaselineMomentumStrategy` shares its sizing and exit code, so the signal
source is the only difference), plus persistence/drift forecast baselines.

**Reported metrics.** Total and annualised return, volatility, Sharpe, Sortino,
max drawdown, Calmar, profit factor, win rate, average win/loss, turnover,
exposure, fees, slippage, trade count — broken down by instrument, by market
regime, and by period, gross and net of costs.

**The verdict rule is code, not prose.** `evaluate.kronos_is_valuable()` returns
`True` only on a measurable out-of-sample improvement net of costs over the same
strategy without Kronos, with a significance check passing. It defaults to
`False`. Public evidence today (teardown §16; upstream issues #354/#355) is
negative, and this function — not marketing — is how Olympus decides.

---

## 12. Phased implementation plan

| Phase | Content | Exit criterion |
|---|---|---|
| **0. Spine** | `contracts`, `errors`, `clock` | Frozen contracts with invariant tests ✔ |
| **1. Market analysis** | `instruments`, `storage`, `validate`, `candles`, `ta`, `features`, `regime`, `volatility` | Validated candles persist and round-trip; no-lookahead assertion works |
| **2. Forecasting** | `kronos_runtime`, `kronos_adapter`, `forecast`, `signals` | Kronos runs reproducibly behind the Olympus interface; abstains correctly; every teardown defect has a regression test |
| **3. Decision** | `strategy`, `portfolio` | Strategies emit intents; portfolio accounting exact through position reversal |
| **4. Safety** | `risk`, `killswitch`, `audit`, `modes` | Every limit rejects at its boundary; kill switches persist across restart; default mode is PAPER |
| **5. Execution** | `oms`, `brokers/`, `execution`, `reconcile` | Idempotent submission; partial fills; restart recovery; reconciliation detects breaks |
| **6. Validation** | `backtest`, `perf`, `evaluate`, `registry` | Leak-free backtest; baseline comparison; Kronos verdict computed, not asserted |
| **7. Operations** | `monitor`, `sentiment`, `agents`, CLI | Auto-trips fire; untrusted content provably cannot reach limits |
| **8. Live readiness** | deployment gates | **Not attempted here.** Requires a real broker sandbox, operator credentials, and a soak period. |

---

## 13. Test plan

| Class | What it proves | Examples |
|---|---|---|
| **Contract invariants** | bad data cannot be constructed | incoherent OHLC, naive datetime, negative volume, inverted stop, zero-size approval |
| **Defect regressions** | each Kronos teardown defect is repaired | `test_teardown_12_2_asymmetric_bits_rejected`, `..._12_1_topk_topp_exclusive`, `..._12_6_horizon_bound`, `..._12_7_ohlc_repaired`, `..._12_9_context_only_normalisation` |
| **Determinism** | same inputs ⟹ same outputs | risk decision + `policy_hash`; seeded backtest run twice |
| **Boundary** | limits fire exactly at the edge | just-inside passes, just-outside rejects, for every limit |
| **Adversarial** | safety cannot be talked around | injection payloads through news; tainted value refused by limits; strategy has no broker attribute |
| **Failure injection** | the system fails safe | broker outage, timeout, partial fill, rejection, desync, corrupt store line |
| **Recovery** | restarts do not lose safety state | kill switch survives; in-flight order adopted not duplicated; portfolio reloads |
| **Leakage** | the backtest is not lying | future bar rejected; fill not at signal close; fit/transform split |
| **Property** | accounting is exact | position reversal through zero, both directions; fills sum to position |
| **Offline** | CI stays hermetic | every test passes with no network and no torch |

---

## 14. Deployment gates

Live trading is **disabled by default** and stays disabled until *every* gate
passes **and** an authorised operator explicitly enables it. The API connection
working is not a gate — it is one of nine.

| # | Gate | Passes when |
|---|---|---|
| G1 | Config valid | limits loaded, instruments registered, mode store readable |
| G2 | Account verified | broker account fetched and matches the configured account id |
| G3 | Kill switches functional | each switch is actually engaged and disengaged during the check |
| G4 | Reconciliation | a successful reconciliation within `max_reconciliation_age_s` |
| G5 | Risk limits configured | non-default limits explicitly set by an operator |
| G6 | Audit trail verified | `ledger.verify_ledger` passes over the current run |
| G7 | Broker connectivity | connected, healthy, latency within bound |
| G8 | Paper-trading history | ≥ N paper trades reconciled without a break |
| G9 | Model approved | every model in the active path is `approved` in the registry with a pinned revision |

Switching to live requires: all gates green, an operator token, a validated
configuration, and an immutable `MODE_CHANGED` audit event. It is not reachable
by config file alone and not reachable by any agent.

**Stop-and-report rule.** If reliable market data, checkpoint validation,
position reconciliation, risk enforcement, auditability, or kill-switch
functionality cannot be *demonstrated*, live trading stays off and the blockers
are reported explicitly. See `docs/TRADING_STATUS.md`.
