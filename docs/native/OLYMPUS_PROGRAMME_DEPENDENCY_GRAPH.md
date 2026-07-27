# Olympus Programme Dependency Graph

Not a list — an argument about order. Each edge below is a *hard* dependency:
the downstream programme cannot produce a trustworthy result without it.

---

## The graph

```
                     ┌──────────────────────┐
                     │ F1 Deployment        │◀── operator: a host
                     │ Platform             │
                     └──────┬───────────────┘
                            │ (nothing exceeds `staging` maturity without this)
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
  ┌───────────┐      ┌─────────────┐     ┌──────────────┐
  │ F2 Data   │      │ F3 Identity │     │ P1 Observ-   │
  │ Governance│      │ & Access    │     │ ability      │
  └─────┬─────┘      └──────┬──────┘     └──────┬───────┘
        │ operator:         │                   │
        │ a policy          │                   │
        │            ┌──────┴──────┐            │
        │            ▼             ▼            │
        │      ┌──────────┐  ┌──────────┐       │
        │      │ P2/P3    │  │ P5/P6    │       │
        │      │ Tenancy  │  │ SDK+Docs │       │
        │      │ + Orgs   │  └──────────┘       │
        │      └────┬─────┘                     │
        │           ▼                           │
        │      ┌──────────┐                     │
        └─────▶│ P4       │                     │
               │ Billing  │◀────────────────────┘
               └────┬─────┘
                    │  (usage ledger v2 also unblocks horizontal scaling)
                    ▼
               ┌──────────┐        ┌──────────────┐
               │ L1 Dist. │        │ D1 Model     │◀── operator: provider creds
               │ Execution│        │ Qualification│
               └──────────┘        └──────┬───────┘
                                          │
                          ┌───────────────┼───────────────┐
                          ▼               ▼               ▼
                   ┌────────────┐  ┌───────────┐  ┌────────────┐
                   │ D2 Adaptive│  │ D3 Local  │  │ E2 Special-│
                   │ Routing    │  │ Models    │  │ ist Platfm │
                   └──────┬─────┘  └───────────┘  └──────┬─────┘
                          ▼                              │
                   ┌────────────┐                        │
                   │ D4 Autono- │                        │
                   │ mous Opt.  │                        │
                   └────────────┘                        │
                                                         ▼
        ┌──────────────┐                          ┌──────────────┐
        │ E1 Plugin    │─────────────────────────▶│ L2 Public    │
        │ Ecosystem    │  (permissions, sandbox,  │ Marketplaces │
        └──────────────┘   signing, revocation)   └──────────────┘
```

## Why each edge exists

| Edge | Why it is hard, not preferred |
|---|---|
| **F1 → everything** | No capability can be labelled above `staging` until something runs. Every SLO, every rollback threshold and every operational baseline is downstream of a deployment. |
| **F2 → F1 (co-requisite)** | A deployed instance holding real conversations with no retention position is a compliance liability from day one. |
| **F3 → P2/P3** | Enterprise administration is *about* identity. Building organisations first would mean modelling ownership before there is anything to own. |
| **F3 → P5/P6** | An auth model baked into five SDKs and a docs site is expensive to change. Stabilise it once. |
| **F3 → E1** | Plugin permissions are meaningless without a principal to grant them to. |
| **P2 → P4** | Enterprise billing needs a tenant to bill. A usage ledger keyed only by principal cannot produce an invoice for an organisation. |
| **P4 → L1** | Not obvious and load-bearing: the usage ledger is a **single-writer, cross-process-locked JSON file**, measured at ~2000 records/s with p99 123 ms at 16 threads. Distributed execution multiplies writers. **Distributed execution is blocked on the ledger, not on the scheduler.** |
| **P1 → D4** | Autonomous optimisation without observability is an agent changing things nobody can see. |
| **D1 → D2** | Routing on unqualified models is routing on nothing. |
| **D1 → D3** | Local qualification uses the same card machinery. |
| **D2 → D4** | Optimisation needs a routing policy to optimise. |
| **E1 → L2** | A marketplace that can install code before permissions, signing and revocation exist is a supply-chain incident waiting to happen. |
| **Evidence → activation** | The universal edge. Four capabilities are already built and refuse to activate. |

## Critical path

```
F1 Deployment  →  F3 Identity  →  P2 Tenancy  →  P4 Billing  →  L1 Distributed
   (host)          (RBAC,           (orgs,        (ledger v2,     (scale)
                    service          quotas)       reconcile)
                    accounts)
```

**Five programmes.** Everything else branches off it and can be resequenced;
this chain cannot. Two observations:

- **F1 is the single highest-leverage item in the entire roadmap.** Not because
  it is hard, but because it is the ceiling on every maturity label. Until it
  lands, the honest label for the whole platform is `staging`.
- **P4 is the hidden blocker.** It looks like a commercial concern; it is
  actually the constraint on horizontal scaling, because the usage ledger's
  single-writer design caps the whole system at 16 concurrent provider calls per
  host.

## Off the critical path (can run in parallel with capacity)

`P1 Observability` (after F1) · `P5/P6 SDK+Docs` (after F3) ·
`D1 Qualification` (needs only credentials + F1) · `E1 Plugin permissions`
(after F3).

**D1 is the best parallel candidate:** it requires no critical-path programme
beyond F1, its runner already exists, and it unblocks four deferred capabilities
at once.
