# External Validation — Authoritative Completion Report

Scope: closing the gap between the Olympus trading laboratory and a real,
externally connected paper-trading system.

- **Baseline commit:** `005dadd` — 7102 passed / 30 skipped
- **This phase:** `b5217c2` → `53fc595`
- **Date:** 2026-07-27

> **The autonomous trading objective is NOT complete.** Six of the twelve
> completion gates are blocked by a single external cause: this environment's
> egress policy refuses every market-data provider, every broker, and
> `huggingface.co`. No real market data has been ingested, no broker has been
> connected, and the genuine Kronos checkpoint has never been executed.

---

## 1. The blocker, measured

Every candidate external system was probed directly. All are refused by the
organisation's egress policy at the CONNECT stage:

| Host | Purpose | Result |
|---|---|---|
| `huggingface.co`, `cdn-lfs.huggingface.co` | Kronos checkpoint + tokenizer | **403** |
| `api.binance.com`, `testnet.binance.vision`, `data-api.binance.vision` | crypto data + sandbox trading | **403** |
| `paper-api.alpaca.markets`, `data.alpaca.markets` | US equities paper | **403** |
| `api.exchange.coinbase.com`, `api-public.sandbox.exchange.coinbase.com` | crypto | **403** |
| `api.kraken.com`, `finnhub.io`, `api.polygon.io` | crypto / market data | **403** |
| `query1.finance.yahoo.com`, `stooq.com` | free historical data | **403** |

Reachable for comparison: `github.com`, `api.github.com`,
`raw.githubusercontent.com`, `pypi.org`, `files.pythonhosted.org`.

The proxy records the denial as
`connect_rejected | gateway answered 403 to CONNECT (policy denial)`. Its own
documentation states: *"Do not retry or route around it — report the blocked
host."* Accordingly **no mirror, proxy, or alternative source was sought** for
the Kronos weights or for market data; doing so would be circumventing an
organisational control.

This is a *policy* boundary, not a bug. Everything below is written so that the
moment egress is granted, the work resumes without redesign.

---

## 2. Completion gates

| # | Gate | Status |
|---|---|---|
| 1 | Real market data ingested and validated | ⛔ **Blocked** — no provider reachable |
| 2 | Real paper broker connected | ⛔ **Blocked** — no broker reachable |
| 3 | Genuine Kronos checkpoint runs | ⛔ **Blocked** — `huggingface.co` 403 |
| 4 | Olympus output compared with upstream | ⛔ **Blocked** — depends on gate 3 |
| 5 | Concrete strategy implemented | ✅ **Met** |
| 6 | Strategy tested out of sample | 🟡 **Partial** — walk-forward runs, but only on synthetic series |
| 7 | Kronos incremental value measured | ⛔ **Blocked** — depends on gates 1 and 3 |
| 8 | Real paper order submitted, filled, reconciled | ⛔ **Blocked** — depends on gate 2 |
| 9 | Restart and failure scenarios demonstrated | ✅ **Met** — against the simulated venue |
| 10 | Audit traceability complete | ✅ **Met** |
| 11 | All existing tests green | ✅ **Met** — 7102 → see §6 |
| 12 | Live trading remains disabled | ✅ **Met** |

**5 met, 1 partial, 6 blocked.**

---

## 3. Evidence classification

### Tested against real external systems

| Item | Evidence |
|---|---|
| Egress-policy denial is real and correctly surfaced | `BinanceSpotProvider.health()` and `.backfill()` were executed against `api.binance.com`. Both failed as intended, returning/raising a typed `MarketDataError` carrying `<urlopen error Tunnel connection failed: 403 Forbidden>` rather than a bare exception. |

That is the **only** claim in this document supported by contact with a real
external system, and it is a claim about the *blocker*, not about trading.

### Integration tested (within Olympus, real components, simulated venue)

| Item | Evidence |
|---|---|
| Ingestion pipeline end to end | `IngestionService` + `ReplayProvider` + `CandleStore`: gaps counted and never filled, duplicates dropped, late bars refused, reconnect-and-backfill recovers all six bars of a simulated mid-stream disconnect, state survives restart |
| Full trade lifecycle | `tests/test_trading_end_to_end.py` — real risk engine, OMS, portfolio, audit, execution, reconciler against `PaperBroker` |
| Restart and failure scenarios | duplicate submit, partial fill, rejection, outage, forced desync, restart adoption |
| Audit traceability | market data → forecast → intent → decision → order, retrievable by `client_order_id`; forgery and deletion both detected |

### Unit tested

`ingest` (28), `binance_testnet` (27), `cli` (24), plus the pre-existing 1939.
**2018 trading tests total.**

### Implemented but NOT verified against the real system

| Item | What is verified | What is not |
|---|---|---|
| `BinanceSpotProvider` | REST kline and websocket parsing against Binance's documented wire formats; `closeTime` normalisation; forming-bar rejection; typed failure on unreachable host | that Binance sends those bytes; auth; rate limits; real latency |
| `BinanceTestnetBroker` | testnet-host guard, long-only guard, HMAC signing path, response parsing, `-2010` duplicate → adopt, `-2013` unknown → None, unmapped status → raise | that the venue accepts any request; real fills; real fees; clock skew |
| Streaming | reconnect/backoff/gap-backfill via `ReplayProvider` | a real websocket, real disconnects |

### Designed but unimplemented

- Phase 5 checkpoint-validation harness (hash pinning, upstream-vs-Olympus
  numerical comparison, latency/memory measurement). Not written, because a
  harness whose subject cannot be obtained cannot be tested and would be
  untested code claiming a validated model.
- Phase 7 was **not run for a verdict.** `evaluate.kronos_is_valuable()` exists
  and is tested, but running it without the real checkpoint and real data would
  produce a number about a fake backend and synthetic prices. That number would
  be worse than no number.

---

## 4. What was built this phase

| Module | Lines | Purpose |
|---|---|---|
| `olympus/trading/ingest.py` | ~640 | Provider contract, `IngestionService`, `ReplayProvider`, `BinanceSpotProvider` |
| `olympus/trading/brokers/binance_testnet.py` | ~470 | Binance Spot Testnet behind `BrokerAdapter` |
| `olympus/trading/cli.py` | ~330 | Operator console |

**Market selected: crypto spot, Binance.** Its market-data endpoints require no
credentials, so the only barrier on the data path is network reachability rather
than key provisioning; spot crypto trades 24/7, removing session-calendar edge
cases; BTC/ETH are the most liquid instruments available; and Binance Spot
Testnet is a genuine exchange test environment. No leverage, no derivatives, no
options, no futures, no unrestricted shorting — the futures testnet host is
explicitly refused, and an uncovered SELL is rejected before any request.

### Safety properties added

- **Testnet enforced by host allow-list.** Pointing the broker at
  `api.binance.com` raises at construction. The one mistake that must be
  impossible to make quietly.
- **Never fabricate.** Ingestion fills no gap, interpolates no bar, carries no
  price forward. A hole stays a hole; the pipeline abstains.
- **Three timestamps.** Venue open/close plus local `received_at`. Their
  difference is the only way to distinguish a quiet market from a dead feed.
- **Credentials never leave the adapter.** Signed URLs are deliberately excluded
  from exceptions — a signed URL carries the HMAC.
- **Console cannot start anything.** No route to live mode, limit edits, order
  submission, or clearing an auto-trip. Enforced by an AST test.

### Defect found and fixed

`BrokerUnavailable` is a *sibling* of `BrokerError`, not a subclass. The
adapter's `health()` and `get_quote()` caught only `BrokerError`, so the
unreachable case — precisely what a health probe exists to report — escaped and
would have crashed the monitor watching for outages.

---

## 5. What an operator must do to close the blocked gates

1. Allow egress to `testnet.binance.vision` and `api.binance.com` (data), and to
   `huggingface.co` + `cdn-lfs.huggingface.co` (checkpoint).
2. Create Binance Spot Testnet API keys; store them via `olympus.vault` and pass
   a `BrokerCredentials` reference. Never place them in config or environment.
3. Install the `kronos` extra (`pip install -e '.[kronos]'`).
4. Re-run the probes in §1 and confirm 200s.
5. Then, in order: backfill → stream → checkpoint load and hash-pin → upstream
   comparison → walk-forward with and without Kronos → paper order.

Until step 1 succeeds, gates 1–4, 7 and 8 cannot be attempted, and no claim
about Kronos's real behaviour or value is supportable.

---

## 6. Verification

```bash
python -m pytest tests/test_trading_*.py -q        # 2018 passed
python -m pytest -q                                # whole repo
python -m olympus capabilities --check
```
