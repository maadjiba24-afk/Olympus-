# Programme — Observability Platform

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. Why this converts a verdict

Every rollback trigger in the readiness report is expressed against a
**shadow-measured baseline that does not exist**. A canary cannot be *monitored*
without one, let alone rolled back on a threshold. This programme is what turns
"CONDITIONAL GO" into a decision that can actually be made.

## 2. What exists

Decision log; OTLP export; `/healthz`, `/readyz`, `/api/metrics` with build
info; liveness verdicts; config-skew diagnostics; replay; five evidence ledgers.
**And a CI gate proving instrumentation cannot alter a run** — that property
must survive everything below.

## 3. Scope

Metrics, logs, traces; request timelines; model calls; routing decisions; tool
calls; approval waits; replay links; cost; cache; context; memory; provider
health; specialist health; tenant health; SLOs; alerts; dashboards;
privacy-preserving debugging.

## 4. Data visibility — who sees what

| Audience | May see | May never see |
|---|---|---|
| **End user** | their own request timeline, approval waits, refusal reasons | other principals' anything; internal model identities if the operator hides them |
| **Organisation admin** | aggregate usage, cost, health, audit for their tenant | request *content* of members, unless policy grants it explicitly |
| **Developer (self-host)** | everything in their own instance | — |
| **Platform operator** | aggregate health, error classes, provider health, per-tenant resource use | **tenant request content by default** — access requires break-glass with audit |

**Privacy-preserving debugging** is the hard part: an operator diagnosing a
failure needs enough to act and not the user's prompt. Approach: content is
fingerprinted and structurally described (length, class, schema) by default;
content access is a break-glass path that is time-boxed and always audited.

## 5. SLOs — every one currently a PROPOSAL

Availability, chat latency p95, verified-answer rate, admission refusal rate.
**None is measured.** M2 replaces each with a number derived from observed
traffic; until then they must be labelled PROPOSAL wherever they appear.

## 6. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | Dashboards + alerts on existing signals | an operator can answer "is it healthy, is it expensive, is it degrading" in one view |
| M2 | **Baselines from observed traffic** | every rollback trigger has n, a distribution and provenance |
| M3 | Per-tenant health and SLOs | a noisy tenant is visible without reading anyone's content |
| M4 | Privacy-preserving debug + break-glass | content access is impossible without an audited break-glass |

## 7. The invariant that must not regress

Instrumentation must remain provably non-interfering. Measured cost today:
+1.5–2.1 ms/run, dominated by two locked read-modify-writes. Any new telemetry
must be measured against that gate, and the gate stays in CI.
