# Decision — The First Olympus-Native Implementation Programme

**Status:** RECOMMENDATION AWAITING APPROVAL.
**Nothing has been implemented.** Implementation begins only if the user
explicitly asks for it after reviewing this decision.

---

## 1. Recommendation

> ## Deployment Platform (F1), milestone M1 only — with Data Governance (F2) as a co-requisite.

**Deploy the staging profile that already exists to one real host, verify it,
and set a retention policy.** Nothing more.

This is deliberately the least novel candidate on the list. It is recommended
because it is the only one that removes a *ceiling* rather than adding a
*feature*.

---

## 2. Candidates considered

All fourteen native programmes were eligible. Six were credible first choices;
the rest have hard prerequisites that disqualify them from being first.

| Candidate | Why it was considered |
|---|---|
| **F1 Deployment Platform** | nothing has ever been deployed |
| **F3 Identity & Access** | blocks every enterprise capability |
| **D1 Model Qualification** | runner exists; unblocks four deferred capabilities |
| **P1 Observability** | converts CONDITIONAL GO into a canary decision |
| **P5 SDK Ecosystem** | most visible external value |
| **P4 Billing / ledger v2** | hidden blocker on horizontal scaling |

---

## 3. Scoring method

Nine criteria from the brief, each 1–5, **unweighted** — weighting invites
fitting the weights to a preferred answer. For *risk* criteria, 5 means **low**
risk. "Time to validated outcome" scores 5 for weeks, 1 for quarters.

| Criterion | F1 Deploy | F3 Identity | D1 Qualif. | P1 Observ. | P5 SDK | P4 Billing |
|---|---|---|---|---|---|---|
| Customer value | 3 | 4 | 3 | 3 | 4 | 2 |
| Architectural dependency (unblocks others) | **5** | **5** | 3 | 3 | 1 | 4 |
| Readiness (how much exists) | **5** | 2 | **5** | 4 | 1 | 2 |
| Available evidence | **5** | 3 | 2 | 3 | 3 | 3 |
| Implementation risk (5 = low) | **5** | 2 | 4 | 4 | 3 | 2 |
| Security risk (5 = low) | 3 | 1 | 4 | 4 | 4 | 2 |
| Operational requirements met | 4 | 3 | 2 | 3 | 4 | 3 |
| Time to validated outcome | **5** | 2 | 3 | 3 | 2 | 2 |
| Unlocks later programmes | **5** | **5** | 4 | 4 | 2 | 4 |
| **Total (max 45)** | **40** | 27 | 30 | 31 | 24 | 24 |

### Why the scores fall this way

**F1 scores 5 on readiness and evidence** because the artifact already exists:
a fail-closed staging profile, `/readyz`, SIGTERM drain, build reporting, a
compose file that passes schema validation, and a backup/restore drill executed
against real archives. What is missing is a host, not code.

**F1 scores 5 on time-to-outcome** because success is measurable in days: the
container builds, `/readyz` returns green, SIGTERM drains an in-flight request,
and a backup restores into a fresh volume.

**F1 scores only 3 on customer value** — honestly. No user gets a new feature.
The value is that every *other* programme becomes able to claim more than
`staging` maturity.

**F3 Identity scores highest on unblocking but 2 on readiness and 1 on security
risk.** Measured from the code: zero SAML, zero MFA, zero service accounts, RBAC
vocabulary without enforcement. It is a large greenfield build touching the
highest-blast-radius surface, and it would be built *without a deployed
environment to test it against*.

**D1 Qualification scores 5 on readiness but 2 on available evidence** — the
runner exists and is fixture-validated, but the campaign needs provider
credentials the project does not have. It is the strongest parallel candidate
the moment credentials appear.

---

## 4. Why the winner is the boring one

**Every maturity label in the capability tree is capped at `staging` until
something is deployed.** That is not a documentation problem; it is a real
epistemic limit. Today Olympus cannot state a single operational fact about
itself: no latency, no error rate, no cost per task, no cache hit rate. Every
SLO is a PROPOSAL and every rollback threshold references a baseline that cannot
exist.

Three consequences follow:

1. **Every other programme would be built blind.** Identity built against no
   real environment, observability with nothing to observe, routing with no
   traffic to route.
2. **The four deferred capabilities stay deferred regardless.** They need
   operational data; operational data needs a deployment.
3. **The programme that is cheapest to do is also the one that unlocks the
   most.** That is unusual and worth acting on.

The counter-argument — *"deployment is not a product programme"* — is right and
does not change the answer. The recommendation is explicitly **M1 only**, not
the whole Deployment Platform. Kubernetes, Helm, autoscaling and multi-region
are out of scope precisely because they are the parts that would turn this into
an infrastructure project.

---

## 5. Rejected alternatives, and why

| Rejected | Reason |
|---|---|
| **F3 Identity first** | Highest blast radius, lowest readiness, and it would be built against no real environment. Do it second, against something running. |
| **D1 Qualification first** | Blocked on credentials, which are not in the project's control. Start it the moment they arrive — it parallelises well with F1. |
| **P1 Observability first** | Nothing to observe. Its M2 acceptance ("baselines from observed traffic") is literally unreachable without F1. |
| **P5 SDK first** | Most visible, but it would bake today's credential scheme into a client library, guaranteeing a breaking change once Identity lands. |
| **P4 Billing first** | Correct that it is the hidden scaling blocker, but it needs a tenant model to bill and traffic to reconcile against. |
| **Any deferred capability** | All four are NO-GO on unchanged floors. Starting one now would mean lowering a floor, which is forbidden. |

---

## 6. Prerequisites (all operator-provided; none is engineering work)

1. **A host with a Docker daemon** — a VM or equivalent. This environment has
   none: `docker info` fails; the compose CLI is present without a daemon.
2. **A conversation-retention decision** — a number of days, or the literal
   `forever`. Engineering cannot make this; it is a legal and product position.
3. **A staging credential set** — `OLYMPUS_API_KEYS` and
   `OLYMPUS_ACCESS_TOKEN`, distinct from any production value.
4. *(Optional, enables the parallel D1 track)* provider API credentials with a
   spend cap.

Without (1) and (2), this programme cannot start. That is not a scoping
weakness — it is the finding.

---

## 7. First milestone

**"One deployed staging instance, verified."**

| Task | Acceptance |
|---|---|
| Build the image on the host | build succeeds from the pinned lock |
| Start via the staging compose profile | container reaches running |
| Boot validation fires correctly | an incomplete profile refuses and names every problem; a complete one starts |
| Readiness | `/readyz` returns 200 with `env=staging`, correct commit, `memory_dir_writable=true` |
| Liveness distinct from readiness | a config-incomplete instance returns 503 on `/readyz` and 200 on `/healthz` |
| Volume ownership | durable state persists across a container restart; verified under the shipped UID |
| Graceful shutdown | `docker stop` drains an in-flight request within the grace period; no torn journal tail |
| **Restore drill on the host** | a backup restores into a fresh volume and the instance starts against it, isolation intact |
| Retention policy set | `olympus retention status` exits 0 |
| Legacy namespace handled | `api-v1` inspected and quarantined or deleted, never auto-assigned |
| Real client over a real network | a vendor SDK call succeeds from another machine over TLS |

**Explicitly out of scope for M1:** Kubernetes, Helm, autoscaling, multi-region,
zero-downtime deploys, release channels, public exposure, and any canary traffic.

---

## 8. Acceptance gates

| Gate | Requirement |
|---|---|
| N-A1 | The image builds from `requirements.lock` with no floating dependency |
| N-A2 | Staging boot fails closed on each missing variable, reporting all at once |
| N-A3 | `/readyz` and `/healthz` behave differently under a config fault |
| N-A4 | Durable state survives a container restart |
| N-A5 | SIGTERM drains; the journal shows no torn tail after a stop |
| N-A6 | A backup taken on the host restores into a clean volume and the instance starts |
| N-A7 | Principal isolation holds after that restore |
| N-A8 | A retention policy is set and `retention status` exits 0 |
| N-A9 | The legacy `api-v1` namespace is dispositioned by an explicit operator action |
| N-A10 | A real vendor SDK reaches the instance over a real network with TLS |
| N-A11 | The full suite and all CI gates still pass |
| N-A12 | Every operational claim in the resulting report is backed by a command that was actually run on the host |

**N-A12 is the honesty gate.** The failure mode for a deployment report is
describing what the configuration *should* do rather than what was observed.

---

## 9. Explicit non-goals

Not production. Not a canary. Not public exposure. No new capability, adaptive
or otherwise. No deferred capability activated. No evidence floor lowered. No
hosting product. Not the rest of the Deployment Platform programme.

---

## 10. What happens after M1

F1-M1 completing does not authorise a canary. It produces the first real
operational surface, which makes three things possible in parallel:

- **P1 Observability M1** — dashboards and alerts on signals that finally exist;
- **D1 Qualification** — if provider credentials arrive;
- **F3 Identity M1** — now testable against something running.

The canary decision itself waits on **P1 M2**: baselines for every rollback
trigger. That remains the gate, and it has not moved.
