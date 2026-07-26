# Olympus Native Roadmap

**Sequencing rule:** by dependency and product value, not by novelty. Fourteen
programmes exist; **at most two run concurrently**, and the organisation this is
written for is one to a few engineers. A roadmap that develops every platform
simultaneously is a wish list.

**Levels:** `FOUNDATIONAL` · `PLATFORM-ENABLING` · `PRODUCT-DIFFERENTIATING` ·
`ECOSYSTEM` · `LONG-HORIZON`.

---

## FOUNDATIONAL

### F1 — Deployment Platform
- **Problem:** nothing has ever been deployed. Every capability is capped at
  `staging` maturity, and every rollback threshold references a baseline that
  does not exist.
- **Outcome:** a running staging instance with verified restore.
- **Prerequisites:** a host with a Docker daemon. *(Operator-provided.)*
- **Scope:** deploy the existing staging profile; verify image build, volume
  ownership, healthcheck, drain, restore-from-backup; stamp build commit.
- **Non-scope:** Kubernetes, Helm, autoscaling, multi-region.
- **Acceptance:** container builds and starts; `/readyz` green; SIGTERM drains
  an in-flight request; a backup restores into a fresh volume and the instance
  starts against it.
- **Security:** first real network exposure — the F4 fail-closed boot and
  per-key principals are the controls. **Privacy:** first real data at rest;
  requires the retention policy (F2). **Cost:** hosting only. **Ops:** creates
  the first thing that can page someone.
- **Risks:** a deployed instance with no retention policy is a compliance
  liability — F2 must land with or before it.
- **Rollback:** stop the stack; the volume persists.

### F2 — Data Governance Activation
- **Problem:** `OLYMPUS_CONVERSATION_RETAIN_DAYS` is unset, so the deployment is
  blocked for regulated or multi-user personal data.
- **Outcome:** a stated, enforced retention position.
- **Prerequisites:** an operator/legal decision. *(Cannot be made by engineering.)*
- **Scope:** set the policy; enforce backup expiry at the storage destination;
  run the legacy `api-v1` procedure; document the deletion SLA.
- **Non-scope:** per-principal retention periods (Enterprise Tenancy).
- **Acceptance:** `olympus retention status` exits 0; a dry-run report matches a
  real sweep; a deletion is verified and survives a restore taken after it.
- **Cost:** none. **Ops:** none beyond the existing heartbeat.
- **Rollback:** unset the policy — the deployment re-blocks, loudly.

### F3 — Identity and Access Management
- **Problem:** Olympus has principals, not identities. No service accounts, no
  roles enforced, no MFA, no SSO. Every enterprise capability is blocked.
- **Outcome:** a first-class identity plane.
- **Prerequisites:** F1 (something to authenticate against).
- **Scope:** principal model, service accounts, API-key lifecycle (rotation,
  expiry, revocation), RBAC enforcement, MFA, session hardening, unified audit.
- **Non-scope:** SSO federation (M3), break-glass (M3).
- **Acceptance:** a permission matrix test per role; a revoked key fails within
  one request; the audit trail is tamper-evident; **existing per-key principal
  isolation still holds** under the new model.
- **Security:** the highest-leverage programme — it is the substrate for every
  later boundary. **Privacy:** identity is personal data; subject to F2.
- **Risks:** the migration path from today's keys must not merge namespaces.
- **Rollback:** feature-flagged; the current credential path stays until parity.

## PLATFORM-ENABLING

### P1 — Observability Platform
Live dashboards, alerts, SLOs sized from observed traffic, per-tenant health.
**Prereq:** F1. **Acceptance:** every rollback trigger has a measured baseline.
**This is what converts "CONDITIONAL GO" into a canary decision.**

### P2 — Enterprise Multi-Tenancy · P3 — Organisation Management
Organisation → workspace → project, resource ownership, quotas, per-tenant
retention and audit. **Prereq:** F3. **Acceptance:** a cross-tenant read is
impossible by construction, proven under concurrency and across restore.

### P4 — Billing and Usage Platform
Usage ledger v2 (immutable event stream, multi-writer), reconciliation against
provider statements, quotas, budgets, invoices. **Prereq:** P2.
**Acceptance:** the ledger reconciles to provider cost within a stated
tolerance; **and it removes the 16-concurrent-call ceiling**, which currently
caps horizontal scaling.

### P5 — SDK Ecosystem · P6 — Documentation Platform
Official clients and docs-as-product. **Prereq:** F3 (auth model must be stable
before it is baked into five languages).

## PRODUCT-DIFFERENTIATING

### D1 — Model and Specialist Qualification
Execute the campaign runner that already exists. **Prereq:** provider
credentials + F1. **Acceptance:** ≥1 provider qualified from executed evidence.
**Unlocks four deferred capabilities.**

### D2 — Adaptive Routing
Route on measured evidence. **Prereq:** D1 + P1. **First milestone must use
existing qualification and telemetry — not a new model.**

### D3 — Local Model Qualification
**Prereq:** D1. **Non-goal:** treating successful model loading as qualification.

### D4 — Autonomous Optimisation
**Prereq:** D2 + P1. **Highest risk in the roadmap** — see its programme spec
for the forbidden self-modification list.

## ECOSYSTEM

### E1 — Plugin Ecosystem
Manifest → permissions → sandbox → signing → revocation → review → *then*
marketplace. **Prereq:** F3. **The marketplace is the last milestone, not the first.**

### E2 — Specialist Platform
Internal platform first (qualification, versioning, promotion, demotion,
retirement); public marketplace last. **Prereq:** D1 + P4.

## LONG-HORIZON

### L1 — Distributed Execution
**Prereq:** single-node execution contracts formally documented and stable,
**plus P4** (the usage ledger is the actual blocker). **Do not begin
implementation before the contracts are written.**

### L2 — Public marketplaces
**Prereq:** E1 + E2 complete, including revocation and incident response.

---

## Sequence

| Wave | Programmes | Gate to exit |
|---|---|---|
| **W1** | F1 Deployment · F2 Data Governance | a running staging instance with a stated retention policy and a verified restore |
| **W2** | F3 Identity · P1 Observability | roles enforced; every rollback trigger has a measured baseline |
| **W3** | P2/P3 Tenancy · D1 Qualification | tenant isolation proven; ≥1 provider qualified |
| **W4** | P4 Billing · P5/P6 SDK+Docs | ledger reconciles; one SDK shipped |
| **W5** | D2 Adaptive Routing · E1 Plugin permissions | routing beats heuristic on measured cells; plugins sandboxed |
| **W6** | D3 Local · D4 Autonomous Opt. · E2 Specialist platform | — |
| **W7** | L1 Distributed · L2 Marketplaces | — |

**W1 and W2 are the only waves with a hard external prerequisite** (a host, a
policy decision, credentials). Everything else is engineering.
