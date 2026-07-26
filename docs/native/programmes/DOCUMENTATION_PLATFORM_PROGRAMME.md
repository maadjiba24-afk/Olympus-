# Programme — Documentation Platform

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. Documentation as a product

The repository has substantial documentation — architecture, threat model,
operator guide, ADRs, absorption archive. It is written for *the people who
built it*. A documentation platform is written for people who did not, and that
is a different product with its own acceptance criteria.

## 2. Scope and ownership

| Type | Audience | Owner | Freshness check |
|---|---|---|---|
| Quickstarts | first-time user | DevRel | **executed in CI** — a broken quickstart is a broken product |
| Conceptual guides | evaluator | Architecture | reviewed per release |
| Tutorials | new integrator | DevRel | executed in CI |
| API reference | integrator | generated from the server | fails CI on drift |
| SDK references | integrator | generated per SDK | fails CI on drift |
| Architecture guides | platform team | Architecture | reviewed per release |
| Deployment guides | operator | Deployment | validated against the shipped compose/Helm |
| Security guides | security reviewer | Security | reviewed per release + on any threat-model change |
| Operations guides | on-call | Ops | reviewed after every incident |
| Troubleshooting | everyone | Support | grown from real support tickets |
| Migration guides | upgrader | whoever ships the break | required before a breaking change merges |
| Changelog | everyone | release automation | generated |

## 3. Executable examples and documentation tests

Every quickstart, tutorial and code sample runs in CI against a real instance.
This is the single highest-value discipline in the programme: documentation rots
silently, and the only reliable detector is execution.

Olympus already has the pattern — the capabilities drift gate fails when the
README's numbers disagree with the code. Generalise it.

## 4. Versioned docs and search

Docs are versioned with the product; the current release is default and prior
majors stay reachable. Search must cover concepts and reference together —
users do not know which one holds their answer.

## 5. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | Quickstart + conceptual core, executed in CI | a new user reaches a first successful call unaided |
| M2 | Generated API + SDK references | drift fails CI |
| M3 | Deployment, security, operations guides | an operator deploys from the docs alone |
| M4 | Versioning, search, troubleshooting from real tickets | — |

## 6. Non-goals

A docs site before there is an SDK to document. Marketing copy in reference
material.
