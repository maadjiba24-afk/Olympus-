# Phase 5 — Provider Qualification Report (Step 7)

**Status: CAMPAIGN NOT EXECUTED. Blocked on provider credentials.**
**Cards written: 0. This is the correct outcome, not a shortfall.**

---

## 1. What is and is not here

Step 7 says: *"When credentials are unavailable: implement the campaign runner,
validate it against deterministic fixtures, mark the campaign as not executed,
and do not populate production-eligible cards from fixture-only evidence."*

That is exactly the state.

| | |
|---|---|
| Credentials present | **none** — no `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or any other |
| Real model calls made | **zero** |
| Qualification cards written | **zero** |
| Measured latency / cost / quality | **none — UNMEASURABLE-OFFLINE** |

## 2. Why no card was written

`modelgrade` cards are the input to two Wave-3 gates (speculation, local tier).
A card manufactured from fixtures would satisfy those gates numerically while
representing nothing — the most damaging single artifact this programme could
produce, because it would look like evidence.

`shadow.default_provenance()` enforces the distinction in code: with no
credential present it returns `shadow-provider`, never
`real-provider-staging`, and it deliberately does **not** use
`Settings.usable()` — which returns True for `provider=anthropic` with no key,
because the SDK reads the key at call time. Stamping stub output as
real-provider evidence because a provider *name* was configured is precisely
the over-claim being prevented. A test pins that distinction.

## 3. What the campaign must measure when it can run

Per Step 7, for every provider/model combination: request success, latency
distribution, structured-output reliability, tool-call validity, cancellation
behaviour, streaming validity, usage reporting, cache reporting, cost
accounting, refusal behaviour, multilingual performance, long-context
performance, drift fingerprint, recovery behaviour.

Substrate that already exists and needs no new code: `modelgrade` (grading and
card issuance, 51 tests), `modelgate` (drift fingerprinting with the Wave-1 B2
pre-flight cost cap), `usage` (cost accounting), `streamguard` (streaming
pathology), `replaystore` (determinism).

## 4. Budget guardrails for the eventual run

Non-negotiable when this executes: `OLYMPUS_DAILY_BUDGET` positive and enforced
(the staging profile refuses to boot otherwise); the Wave-1 B2 pre-flight
worst-case estimator applied before each batch; and fan-out capped at **16
concurrent provider calls per host**, the measured usage-ledger contention
ceiling. Batching the ledger to raise that ceiling is forbidden — it would make
spend non-durable between flushes, and the cap is a safety property.

## 5. Consequence for the Phase-5 verdict

Two Wave-3 gates cannot be evaluated. No SLO involving latency, cost or answer
quality can be measured. `GO FOR CONTROLLED CANARY` requires at least one
provider qualified from executed evidence, so this report alone caps the
Phase-5 verdict below it.
