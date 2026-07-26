# Phase 5 — Shadow Evidence Report (Steps 4, 5, 9, 10)

**Status: SUBSTRATE COMPLETE AND PROVEN. NO OPERATIONAL EVIDENCE COLLECTED.**

---

## 1. The distinction this report exists to preserve

Shadow mode is **built, wired and adversarially proven**. It has **not been
fed**. Running it requires a deployed instance receiving traffic; this
environment has neither.

So there are no shadow baselines, and the rollback thresholds in
`PRODUCTION_READINESS_REPORT.md` — every one expressed against a
"shadow-measured baseline" — still have no baseline. That gap is unchanged by
Phase 5 and is the single largest reason the verdict cannot reach canary.

## 2. What shadow mode does (Step 4)

The **real** decision path runs: routing, planning, the dependency graph,
verification, context budgeting, admission, the recovery ladder, journaling,
tracing, replay recording. Only what leaves the machine is intercepted. A
shadow run that skipped the decision path would measure nothing.

Recorded per run, per Step 4's list: routing decisions, provider choice,
latency, token usage, cache use, cost, context-budget decisions, tool
proposals, recovery-ladder activations, verification outcomes, replay
identifiers, failures and cancellations — through the existing trace and
evidence stores, plus the shadow sink for intercepted intents.

**Marking:** every `/v1` response carries `X-Olympus-Mode: shadow`, verified
over a real socket by a real SDK. `/readyz` reports the mode. Every sink record
carries provenance.

## 3. The side-effect boundary (Step 5)

One enforcement point — `tools.resolve_handler` — covers both API dialects plus
plugin and MCP handlers, because recon established that both council tool loops
resolve through it. Defence in depth: `actions._execute` (the approval spine,
reachable without any tool call) and `guard_egress()` (background jobs).

Five bands; **all 130 shipped tools classified**; **default deny** for anything
unrecognised.

| Band | Shadow behaviour |
|---|---|
| `READ` | runs for real |
| `STAGING_WRITE` | runs for real |
| `RECORDED` | intent recorded, unmistakably-marked synthetic result returned |
| `APPROVAL` | refused |
| `PROHIBITED` | refused |

Network *reads* stay `READ` deliberately: they egress but do not mutate, and
blocking them would make every research task fail — distorting the very
evidence shadow mode exists to collect.

### Containment, proven route by route (P5-A5)

Direct execution · 8 consecutive retries · malformed tool calls · plugin
handlers · MCP handlers · a plugin `pre_tool` hook rewriting params · both API
dialects end-to-end through the real `openai_compat` loop with a
provider-generated tool call · the approval spine · a 6-thread cancellation
race · a failing sink.

Each uses a tripwire standing in for a real external effect; if it is ever
called the test fails **naming the route that got through**. All closed.

Three coverage tests keep the table honest: every shipped tool has a band; no
entry names a tool that does not exist (a stale entry would make the coverage
test pass while the real tool went unclassified); every band is a known band.
The first caught `operator_trust` during development.

## 4. Provenance (Steps 9–10, P5-A6/A7)

Six closed values. An unknown or missing one is a hard `ProvenanceError`, never
a default: a record whose origin is unknown cannot later be told from real
traffic.

| Value | Produced in Phase 5? |
|---|---|
| `synthetic` | ✅ tests, fuzzers, benchmarks |
| `replay-derived` | ✅ available; replay fixtures exist |
| `shadow-provider` | ✅ the default when no credential is present |
| `real-provider-staging` | ❌ no credentials |
| `real-client-staging` | ✅ **the client-compatibility campaign, 25 records** |
| `real-user` | ❌ **never** — forbidden in Phase 5, asserted by test |

**Synthetic and operational evidence are never aggregated unlabelled.** Any
aggregate reports its provenance mix or is invalid.

## 5. Traffic sources (Step 9)

| # | Source | Status |
|---|---|---|
| 1 | deterministic replay fixtures | ✅ available (`replaystore`) |
| 2 | synthetic adversarial workloads | ✅ 500-iteration seeded fuzz sweeps |
| 3 | benchmark / evaluation tasks | ⚠️ partial — grading needs an independent oracle, and quality grading needs a provider |
| 4 | historical sanitized traces | ❌ none exist |
| 5 | staging external-client traffic | ✅ **25 real-SDK requests over real HTTP** |
| 6 | shadow-provider traffic | ⚠️ substrate ready, not run at volume |
| 7 | approved real traffic | ❌ forbidden in Phase 5 |

## 6. Baselines (Step 10) — what exists and what does not

**Measured, provenance `synthetic`** (offline harness, no provider):
journal `sync` p50/p90/p99 and its depth-scaling coefficient; journal recovery
per record; observability overhead; context-budget call costs; streamguard
per-delta cost; watchdog call costs; admission overhead contended and
uncontended; the usage-ledger contention curve at 1/2/4/8/16 threads; storage
per turn; concurrency capacity.

**Measured, provenance `real-client-staging`**: per-case HTTP latency for 25
client-compatibility cases — with a stubbed provider, so these are
Olympus-overhead numbers, not end-to-end latencies.

**NOT measured — no traffic and no provider:** end-to-end latency, provider
latency, queue wait, admission rejection rate, cancellation success rate,
provider failure rate, replay divergence rate in the field, context estimation
error on real prompts, truncation rate, cache read/creation rates, tool-schema
failure rate, tool-repair rate, recovery-rung distribution, cost per task
class, memory-retrieval quality, routing-decision distribution.

Every item in that second list is a rollback trigger input or an SLO input.
**None of them has a number, and none has been given one.**
