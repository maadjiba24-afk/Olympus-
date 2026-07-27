# Olympus Native Evolution — Documentation Index

**This is the active documentation root for Olympus.** Work here defines Olympus
on its own terms: its users, its evidence, its architecture, its security
requirements and its operational reality.

`docs/absorption/` is a **closed archive**. See
[`../absorption/COLIBRI_ARCHIVE.md`](../absorption/COLIBRI_ARCHIVE.md).

---

## Status

| | |
|---|---|
| Programme | Olympus Native Evolution |
| Phase | **Design.** No native programme has begun implementation. |
| First programme recommended | Deployment Platform (F1) M1 — **awaiting approval** |
| Highest maturity anywhere in the platform | `staging` |

---

## Core documents

| Document | Purpose |
|---|---|
| [`OLYMPUS_NATIVE_VISION.md`](OLYMPUS_NATIVE_VISION.md) | What Olympus is, who it serves, its principles, safety and evidence philosophy |
| [`OLYMPUS_NATIVE_ARCHITECTURE.md`](OLYMPUS_NATIVE_ARCHITECTURE.md) | Thirteen planes, trust boundaries, failure domains, scaling and tenancy — each labelled CURRENT / NEAR-TERM / TARGET |
| [`OLYMPUS_CAPABILITY_TREE.md`](OLYMPUS_CAPABILITY_TREE.md) | Every capability with honest maturity, dependencies, evidence requirements and risk |
| [`OLYMPUS_NATIVE_ROADMAP.md`](OLYMPUS_NATIVE_ROADMAP.md) | Programmes by level, with a seven-wave sequence |
| [`OLYMPUS_PROGRAMME_DEPENDENCY_GRAPH.md`](OLYMPUS_PROGRAMME_DEPENDENCY_GRAPH.md) | Why the order is what it is, and the five-programme critical path |
| [`OLYMPUS_NEXT_PROGRAMME_DECISION.md`](OLYMPUS_NEXT_PROGRAMME_DECISION.md) | Scored selection of the first implementation programme |

## Programme specifications

| Programme | Level |
|---|---|
| [`Deployment Platform`](programmes/DEPLOYMENT_PLATFORM_PROGRAMME.md) | FOUNDATIONAL |
| [`Identity and Access`](programmes/IDENTITY_ACCESS_PROGRAMME.md) | FOUNDATIONAL |
| [`Enterprise Multi-Tenancy`](programmes/ENTERPRISE_MULTI_TENANCY_PROGRAMME.md) | PLATFORM-ENABLING |
| [`Organisation Management`](programmes/ORGANISATION_MANAGEMENT_PROGRAMME.md) | PLATFORM-ENABLING |
| [`Billing and Usage`](programmes/BILLING_USAGE_PROGRAMME.md) | PLATFORM-ENABLING |
| [`Observability Platform`](programmes/OBSERVABILITY_PLATFORM_PROGRAMME.md) | PLATFORM-ENABLING |
| [`SDK Ecosystem`](programmes/SDK_ECOSYSTEM_PROGRAMME.md) | PLATFORM-ENABLING |
| [`Documentation Platform`](programmes/DOCUMENTATION_PLATFORM_PROGRAMME.md) | PLATFORM-ENABLING |
| [`Adaptive Routing`](programmes/ADAPTIVE_ROUTING_PROGRAMME.md) | PRODUCT-DIFFERENTIATING |
| [`Local Model Qualification`](programmes/LOCAL_MODEL_QUALIFICATION_PROGRAMME.md) | PRODUCT-DIFFERENTIATING |
| [`Autonomous Optimisation`](programmes/AUTONOMOUS_OPTIMISATION_PROGRAMME.md) | PRODUCT-DIFFERENTIATING |
| [`Specialist Platform`](programmes/SPECIALIST_PLATFORM_PROGRAMME.md) | ECOSYSTEM |
| [`Plugin Ecosystem`](programmes/PLUGIN_ECOSYSTEM_PROGRAMME.md) | ECOSYSTEM |
| [`Distributed Execution`](programmes/DISTRIBUTED_EXECUTION_PROGRAMME.md) | LONG-HORIZON |

---

## Principles binding all native work

1. **Evidence before activation** — a capability may be implemented and refuse
   to activate. Four are in that state today.
2. **Safe failure** — bounded, observable, recoverable, non-destructive.
3. **Default deny** — tools, plugins, side effects, cross-tenant access and
   privileged actions are denied unless explicitly authorised.
4. **Approval boundaries** — irreversible and high-impact actions require
   explicit approval; no autonomy level overrides this.
5. **Reproducibility** — important decisions and failures must be replayable or
   otherwise explainable.
6. **Cost awareness** — every autonomous mechanism carries budgets, ceilings,
   attribution, alerts and rollback.
7. **Tenant isolation** — no feature may weaken principal or tenant isolation.
8. **Honest maturity labels** — `designed`, `prototyped`, `implemented`,
   `tested`, `qualified`, `staging`, `canary`, `production`. Nothing here is
   above `staging`.
9. **Reversible evolution** — routing, optimisation, specialists, plugins and
   deployment changes all support rollback.
10. **No architecture by imitation** — a design is justified by user value,
    evidence, security, reliability, cost, maintainability, scalability or
    developer experience. Never by what another project did.
