# Programme — Billing and Usage Platform

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. What exists is accounting, not billing

Today: a per-day JSON ledger under a cross-process lock, cost estimation from a
price table, a daily cap, and a spend-rate window. Measured ceiling ~2000
records/s, p99 123 ms at 16 concurrent threads.

Missing: an immutable event stream, invoices, credits, subscriptions,
reconciliation, refunds, taxes, disputes, fraud controls — and multi-writer
capability, which is why this programme also blocks horizontal scaling.

## 2. The non-negotiable design rule

> **The billing ledger must not rely only on provider-reported totals.**

Providers report usage after the fact, per call, in their own units, sometimes
inconsistently, and occasionally wrongly. A ledger that simply echoes them
cannot detect a discrepancy, cannot bill during a provider outage, and cannot
support a dispute.

Olympus records **its own** usage event at the moment of the call — model,
tokens in/out, cache split, tool invocations, storage delta, wall time,
principal, tenant — and **reconciles** against provider statements as a separate,
auditable process. Divergence beyond a tolerance is an alert, not a silent
correction.

## 3. Usage ledger v2

| Property | Why |
|---|---|
| Append-only event stream | an immutable record is the basis of any dispute |
| Per-writer shards, summed on read | removes the single-writer lock — **this is what unblocks distributed execution** |
| Durable before the next call | the spend cap is a safety property; batching would break it |
| Provenance on every event | synthetic, staging and real usage must never aggregate |
| Idempotency key per event | a retried call must not double-bill |
| Tenant attribution at write time | never inferred later |

## 4. Scope

Usage events; immutable ledger; token, tool, storage, specialist and compute
usage; subscriptions; credits; quotas; budgets; invoices; taxes; refunds;
disputes; metering reconciliation; provider-cost reconciliation; fraud
prevention; spending alerts; enterprise contracts.

## 5. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | Ledger v2 (append-only, sharded, idempotent) | no lost increments under 64 concurrent writers; **the 16-call ceiling is lifted and re-measured** |
| M2 | Reconciliation against provider statements | divergence within a stated tolerance; a seeded discrepancy is detected |
| M3 | Quotas, budgets, alerts per tenant | quota enforced before work starts; alerts fire before the cap, not at it |
| M4 | Invoices, credits, subscriptions | an invoice reproduces exactly from the event stream |
| M5 | Refunds, disputes, taxes | every adjustment is an event, never an edit |

## 6. Fraud and abuse

Free-tier abuse, credential sharing, and runaway agents. The existing spend-rate
window and admission refusal are the primitives; per-tenant quotas make them
enforceable. **Refusal, never silent downgrade**, applies here too.

## 7. Security · Privacy · Cost · Operational

**Security:** billing is a HIGH-risk surface — the autonomous optimiser is
structurally forbidden from touching it. **Privacy:** usage events are
per-principal and subject to retention; they must not embed content.
**Cost:** this programme is how cost becomes attributable at all.
**Operational:** reconciliation is a recurring job with a real failure mode
(provider statement unavailable) that must degrade to "unreconciled", not to
"reconciled".
