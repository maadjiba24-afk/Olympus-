# Programme — Autonomous Optimisation

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. The highest-risk programme in the roadmap

A system that changes itself based on measured outcomes is the most valuable and
most dangerous capability here. This specification is written defensively.

## 2. Forbidden self-modifications (absolute)

The system may **never** autonomously modify:

1. **Security controls** — authentication, authorisation, isolation, the
   side-effect boundary, the approval spine.
2. **Billing and metering** — the usage ledger, cost attribution, quotas.
3. **Identity** — principals, roles, keys, sessions.
4. **Approval boundaries** — risk classes, autonomy levels, the irreversible/
   financial exclusion.
5. **The evidence machinery itself** — provenance, floors, gate thresholds, the
   experiments registry.
6. **Its own forbidden list.**

This is enforced structurally, not by policy text: the optimiser's write surface
is an allowlist of tunable parameters, and anything outside it is unreachable.
A test asserts the allowlist excludes every module above.

## 3. Legitimate optimisation targets

Prompt and context composition; retrieval parameters; specialist selection
weights; routing policy parameters (within Adaptive Routing's floors); cache
key strategy; concurrency and batching within measured ceilings; retry and
timeout policy.

## 4. The pipeline

```
propose → offline evaluate → shadow evaluate → APPROVE (human) →
bounded experiment → attribute → keep or roll back
```

**Approval is human at M1–M3.** Auto-approval for a narrow, proven class is M4
at the earliest, requires its own evidence, and never extends to anything in §2.

## 5. Anti-reward-hacking

The measurable target is never the real goal, and a sufficiently motivated
optimiser will find the gap.

1. **Held-out evaluation** — the optimiser never sees the set it is graded on.
2. **Multi-metric gates** — quality *and* cost *and* latency *and* refusal
   rate. Improving one while degrading another is a rejection.
3. **Independent oracle** — the grader is not the thing being optimised.
4. **Regression floors** — no cell may regress, however good the average.
5. **Suspicion of large wins** — an improvement beyond a threshold triggers
   review rather than adoption. Real gains here are incremental; a 40% jump is
   usually a measurement bug.

## 6. Guardrails and ceilings

Every experiment carries a token budget, a wall-clock bound, a blast-radius
bound (a fraction of traffic), an automatic rollback trigger, and an owner.
Cumulative optimisation spend has its own ceiling separate from operational
spend.

## 7. Change attribution and override

Every change records: proposal, evidence, approver, activation time, metrics
before and after, and the rollback trigger. A human can revert any change at any
time without a code deploy, and the revert path is tested — not assumed.

## 8. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | Proposal generation + offline evaluation | proposals are reproducible; the write allowlist provably excludes §2 |
| M2 | Shadow evaluation | shadow results predict live results within a stated error |
| M3 | Human-approved bounded experiments | every experiment rolls back cleanly; attribution is complete |
| M4 | Narrow auto-approval (only if justified) | a written case, plus a measured false-positive rate on M3 approvals |

**Prerequisite:** Observability (P1) and Adaptive Routing (D2). An optimiser
without observability is an agent changing things nobody can see.
