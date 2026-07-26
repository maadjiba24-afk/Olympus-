# Olympus Capability Tree

**Maturity vocabulary** (honest labels, per the native principles):
`designed` · `prototyped` · `implemented` · `tested` · `qualified` · `staging` ·
`canary` · `production`.

**Nothing in this repository is above `staging`.** No capability has been
deployed, run against real traffic, or qualified with operational evidence.

**Risk classification:** `LOW` (bounded, reversible) · `MED` (affects cost,
quality or availability) · `HIGH` (affects security, isolation, money or data
durability).

---

## Layer 1 — Core execution

| Capability | Current | Target | Dependencies | Evidence required | Risk | Owner programme | Milestone |
|---|---|---|---|---|---|---|---|
| Request lifecycle | tested | production | deployment | live traffic baseline | HIGH | Deployment | M1 |
| Planning (dependency graph) | tested | production | — | task-class quality baseline | MED | Adaptive Routing | M2 |
| Model execution | tested | production | provider creds | provider qualification | HIGH | Local Model Qual. | M2 |
| Tool execution | tested | production | — | tool-call validity rate | HIGH | Core | M1 |
| State (journal + snapshot) | tested | production | — | depth beyond 3000 turns | HIGH | Distributed Exec. | M2 |
| Replay | tested | production | — | field divergence rate | MED | Observability | M1 |
| Verification | tested | production | provider creds | verified-answer rate | HIGH | Adaptive Routing | M2 |
| Recovery ladder | tested | production | — | rung distribution | MED | Core | M1 |
| Cancellation | tested | production | — | propagation under load | MED | Core | M1 |

## Layer 2 — Intelligence

| Capability | Current | Target | Dependencies | Evidence required | Risk | Owner programme | Milestone |
|---|---|---|---|---|---|---|---|
| Model qualification | implemented (0 cards) | qualified | provider creds | executed campaign, n ≥ MIN_N/cell | HIGH | Local Model Qual. | M2 |
| Adaptive routing | implemented, **off** | qualified | qualification | per-cell A/B vs. heuristic | HIGH | Adaptive Routing | M3 |
| Specialist selection | tested | production | telemetry | selection-vs-outcome data | MED | Specialist Platform | M3 |
| Confidence calibration | designed | implemented | verification data | calibration curve | MED | Adaptive Routing | M4 |
| Memory retrieval | tested | production | — | retrieval-quality baseline | MED | Adaptive Routing | M2 |
| Context heat | implemented, **shadow** | qualified | verifier signal (A3 gap) | promotion-signal source | MED | Adaptive Routing | M4 |
| Autonomous optimisation | designed | implemented | observability + evidence | offline + shadow eval | **HIGH** | Autonomous Opt. | M5 |
| Speculation | designed, **unbuilt** | implemented | qualified draft + verifier | per-cell A/B | HIGH | Adaptive Routing | M5 |
| Predictive prefetch | designed, **unbuilt** | implemented | n ≥ 200 real runs | recall@2 ≥ 0.6, CI ≤ 0.1 | MED | Adaptive Routing | M5 |
| Provider mirroring | designed, **unbuilt** | implemented | unavailability rate | measured rate worth mitigating | MED | Adaptive Routing | M5 |
| Local inference tier | designed, **unbuilt** | qualified | local runtime | qualification cards | MED | Local Model Qual. | M4 |

## Layer 3 — Platform

| Capability | Current | Target | Dependencies | Evidence required | Risk | Owner programme | Milestone |
|---|---|---|---|---|---|---|---|
| API gateway | tested (real SDKs over HTTP) | production | deployment | live error/latency rates | HIGH | Deployment | M1 |
| SDKs | **none** | production | stable API contract | client integration tests | MED | SDK Ecosystem | M2 |
| Plugins | implemented, minimal | production | permissions + sandbox | isolation proof | **HIGH** | Plugin Ecosystem | M4 |
| Connectors | tested | production | identity | per-connector auth audit | HIGH | Identity | M2 |
| Deployment | staging (never run) | production | a host | successful deploy + restore drill | HIGH | Deployment | M1 |
| Observability | tested | production | deployment | dashboard + alert coverage | MED | Observability | M1 |
| Billing | accounting only | production | usage ledger v2 | reconciliation vs. provider | **HIGH** | Billing & Usage | M3 |

## Layer 4 — Enterprise

| Capability | Current | Target | Dependencies | Evidence required | Risk | Owner programme | Milestone |
|---|---|---|---|---|---|---|---|
| Organisations | **none** | production | identity | tenancy isolation proof | **HIGH** | Org Management | M2 |
| Workspaces | **none** | production | organisations | — | HIGH | Org Management | M2 |
| Projects | **none** | production | workspaces | — | MED | Org Management | M3 |
| Users | local accounts (PBKDF2) | production | identity | auth audit | **HIGH** | Identity | M1 |
| Service accounts | **none** | production | identity | key lifecycle audit | **HIGH** | Identity | M1 |
| RBAC | **none** (roles referenced, not enforced) | production | identity + orgs | permission matrix tests | **HIGH** | Identity | M2 |
| SSO (OIDC/SAML) | **none** | production | identity | IdP integration tests | HIGH | Identity | M3 |
| MFA | **none** | production | identity | enrolment + recovery tests | HIGH | Identity | M2 |
| Audit | per-capability logs | unified | identity | tamper-evidence | HIGH | Identity | M2 |
| Policies | per-principal, in-code | declarative | orgs | policy-eval tests | HIGH | Enterprise Tenancy | M3 |
| Quotas | global caps | per-tenant | tenancy + ledger | enforcement under load | HIGH | Enterprise Tenancy | M3 |
| Data governance | retention + deletion, **policy unset** | production | operator policy | policy set + expiry enforced | **HIGH** | Enterprise Tenancy | M1 |

## Layer 5 — Ecosystem

| Capability | Current | Target | Dependencies | Evidence required | Risk | Owner programme | Milestone |
|---|---|---|---|---|---|---|---|
| Specialist registry | implemented (internal) | production | qualification | promotion/demotion evidence | MED | Specialist Platform | M3 |
| Plugin marketplace | **none** | production | permissions, sandbox, signing, revocation, review | governance + incident drill | **HIGH** | Plugin Ecosystem | M6 |
| Specialist marketplace | **none** | production | specialist platform + billing | — | **HIGH** | Specialist Platform | M6 |
| Templates | partial (playbooks, site templates) | production | — | reuse metrics | LOW | Documentation | M4 |
| Evaluations | harnesses exist | production | provider creds + oracles | independent-oracle proof | MED | Observability | M3 |
| Third-party integrations | connectors + MCP | production | plugin permissions | per-integration audit | HIGH | Plugin Ecosystem | M5 |
| Developer portal | **none** | production | SDKs + docs | — | LOW | Documentation | M5 |

---

## The five capabilities that gate everything else

1. **Identity** — every enterprise capability, every tenant boundary and every
   audit trail hangs off it. Nothing above Layer 3 can start first.
2. **Deployment** — no capability can exceed `staging` maturity until something
   is actually deployed. This is the ceiling on the entire tree.
3. **Usage ledger v2** — billing, per-tenant quotas and multi-process scaling
   are all blocked by the current single-writer design.
4. **Provider credentials** — every `qualified` state in Layer 2 needs them.
5. **A retention policy** — one operator decision blocks all regulated use.
