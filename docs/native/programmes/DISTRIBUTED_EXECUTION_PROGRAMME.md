# Programme — Distributed Execution

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. Hard precondition

> **Do not begin implementation until the single-node execution contracts are
> formally documented and stable.**

They are not, today. What must be written first: the exact ordering guarantees
of the council pipeline; the journal's consistency model under concurrent
writers; the idempotency semantics of a retried tool call; the cancellation
contract across a network boundary; and the evidence-consistency model when
ledgers are written from multiple hosts.

## 2. The real blocker is the usage ledger, not the scheduler

Measured today: the ledger is a **single-writer, cross-process-locked JSON
file**, at ~2000 records/s with p99 123 ms at 16 concurrent threads. Every
provider call writes it. Distributed execution multiplies writers.

**Batching is forbidden** — it would make spend non-durable between flushes, and
the cap is a safety property, not a metric. So the ledger must become an
append-only event stream with per-writer shards summed on read, which is
**Billing & Usage (P4)**. Distributed execution is sequenced behind it.

## 3. Scope

Scheduler; worker pool; queues with priority and fairness; **leases** (the
existing progress-lease design generalised); idempotency keys; cancellation
propagation across hosts; retry with attribution; journal consistency; evidence
consistency; node failure; network partition; task affinity (session stickiness
for warm context); specialist placement; local-model placement (a worker with a
GPU is not interchangeable); cost-aware placement; multi-region; disaster
recovery.

## 4. Consistency decisions (must be stated before code)

| Question | Position to justify |
|---|---|
| Journal under concurrent writers | one writer per session, enforced by lease — not a distributed log |
| Evidence ledgers | append-only per-writer shards, summed on read; no cross-host lock |
| A partitioned worker holding a lease | lease expiry with a fencing token; the work is re-run, never dual-run |
| Retried tool call | idempotency key required for any non-READ band |
| Cancellation across a partition | best-effort with a bounded window; a cancelled-but-completed action must be *recorded*, not hidden |

## 5. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M0 | **Document the single-node contracts** | a written spec reviewed and stable — this is the gate, not a formality |
| M1 | Scheduler + workers + leases, single region | a killed worker's task is re-run exactly once; no dual-run |
| M2 | Idempotency + cancellation across hosts | a retried side effect happens once |
| M3 | Affinity and cost-aware placement | measured improvement over random placement |
| M4 | Multi-region + DR | a region loss is survivable with a stated RPO/RTO |

## 6. Risks

The highest is **dual execution of a side effect** after a partition — which is
why idempotency is M2 and not later, and why the side-effect boundary's bands
map directly onto idempotency requirements.
