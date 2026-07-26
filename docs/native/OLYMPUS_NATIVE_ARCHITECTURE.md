# Olympus Native Architecture

**Status:** ARCHITECTURE OF RECORD.
**Maturity labelling is load-bearing.** Every component below is marked
**[CURRENT]** (implemented and tested today), **[NEAR-TERM]** (designed, not
built), or **[TARGET]** (long-horizon intent). Nothing marked NEAR-TERM or
TARGET exists.

---

## 1. Planes

Olympus is organised into thirteen planes. Today most of them are *functions
inside one process*; the architecture names them because the boundaries are
real even when the deployment is not.

### 1.1 Control plane — **[NEAR-TERM]**
Decides what runs, where, and under whose policy. **Today there is no control
plane**: `orchestrator.Olympus` is instantiated per request and decides
everything in-process. Near-term it becomes a distinct component owning
admission, placement, policy resolution and quota enforcement.

### 1.2 Execution plane — **[CURRENT]**
`orchestrator.py` — Zeus routes, Athena plans a dependency graph, specialists
execute in a bounded worker pool, Aletheia verifies. Cancellation propagates
end-to-end; a progress lease detects spend without progress.

### 1.3 Data plane — **[CURRENT]**
`MEMORY_DIR` on a local filesystem: conversation snapshots, sealed journals,
per-principal memory, documents, evidence ledgers, the vault (Fernet).
Concurrency is `flock` + thread locks. **Single-host only** — this is the
hardest constraint in the current architecture.

### 1.4 Evidence plane — **[CURRENT]**
Five append-only ledgers plus watchdog forensics, the decision log, replay
fixtures and the experiments registry. Append-only, provenance-stamped,
retention-swept, corrupt-line-skipping.

### 1.5 Identity plane — **[CURRENT: principals] / [NEAR-TERM: identity]**
Today: per-API-key derived principals (domain-separated SHA-256), a local
account store with PBKDF2, and an access token. **No** organisations, roles,
service accounts, SSO, or MFA. Near-term this becomes the foundation everything
else hangs from.

### 1.6 Policy plane — **[CURRENT, partial]**
Risk classes, autonomy levels, side-effect bands, data classes, sovereign mode,
egress allowlists. Policy is **per-principal**, evaluated in-process. There is
no policy *document*, no inheritance, and no org-level override.

### 1.7 Provider plane — **[CURRENT]**
Adapters per provider, a model pool, drift fingerprinting, typed provider
failures, streaming pathology detection, prompt-cache telemetry, and a
qualification layer (`modelgrade`) that can gate role eligibility.

### 1.8 Specialist plane — **[CURRENT]**
13 first-party specialists with output contracts and tool scoping, plus an agent
registry that can add specialists without shadowing built-ins.

### 1.9 Plugin plane — **[CURRENT, minimal]**
Plugin handlers and MCP clients resolve through the same tool chokepoint, so the
side-effect boundary covers them. **There is no manifest, no declared
permission set, no sandbox, no signing and no revocation.** This is the gap that
makes a marketplace impossible today.

### 1.10 Observability plane — **[CURRENT]**
Decision log, OTLP export, `/healthz`, `/readyz`, `/api/metrics`, liveness
verdicts, config-skew diagnostics — and a CI gate proving instrumentation
cannot alter a run.

### 1.11 Billing plane — **[CURRENT: accounting] / [NEAR-TERM: billing]**
A per-day usage ledger under a cross-process lock, cost estimation, a daily cap.
It is **accounting, not billing**: no immutable event stream, no invoices, no
credits, no reconciliation against provider statements, and a measured
throughput ceiling of ~2000 records/s.

### 1.12 Deployment plane — **[CURRENT: single node] / [NEAR-TERM: staging]**
Dockerfile, production Compose (with Caddy), and a fail-closed staging profile
with readiness and drain. **Never deployed** — validated by schema only.

### 1.13 SDK and API plane — **[CURRENT: API] / [NEAR-TERM: SDK]**
Two dialects through one generation path, verified with real vendor SDKs over
real HTTP. **No official Olympus SDK exists in any language.**

---

## 2. Component boundaries

```
                    ┌─────────────────────────────────────────┐
   client ─────────▶│  API surface  /v1/messages  /v1/chat…   │  [CURRENT]
   (vendor SDK      │  auth → principal derivation → admission │
    or HTTP)        └────────────────┬────────────────────────┘
                                     │
                    ┌────────────────▼────────────────────────┐
                    │  Execution plane (orchestrator)         │  [CURRENT]
                    │  route → plan → dispatch → verify       │
                    └───┬──────────────┬──────────────┬───────┘
                        │              │              │
              ┌─────────▼───┐  ┌───────▼──────┐  ┌────▼─────────┐
              │ Provider    │  │ Tool chokept │  │ Evidence     │
              │ plane       │  │ (THE side-   │  │ plane        │
              │             │  │  effect      │  │              │
              │ [CURRENT]   │  │  boundary)   │  │ [CURRENT]    │
              └─────────────┘  └───────┬──────┘  └──────────────┘
                                       │
                          ┌────────────▼────────────┐
                          │ Approval spine          │  [CURRENT]
                          │ risk class × autonomy   │
                          └────────────┬────────────┘
                                       │
                                  external world
```

## 3. Trust boundaries

| Boundary | Enforcement | Maturity |
|---|---|---|
| Untrusted client → API | credential compare, principal derivation, admission, 1 MB body cap | [CURRENT] verified over real HTTP |
| Untrusted model output → tool execution | execution precondition validates before any handler runs | [CURRENT] |
| Untrusted provider → parser | typed provider-failure class; unnameable calls dropped | [CURRENT] |
| Untrusted artifact → durable store | `ingestgate`; reject-never-repair | [CURRENT] 2,080 seeded mutations |
| Agent → external world | side-effect band + approval spine | [CURRENT] 10 bypass routes proven closed |
| Principal → principal | derived namespaces | [CURRENT] verified under concurrency, across restore, over the wire |
| **Tenant → tenant** | — | **[NEAR-TERM] does not exist** |
| **Plugin → host** | tool chokepoint only | **[NEAR-TERM] no sandbox, no permissions** |

## 4. Data flows

**Request:** client → auth → principal → admission slot → orchestrator →
route (pin > bandit > qualification guard > substitution > heuristic) → plan →
dispatch (each specialist call: budget check → provider → usage record →
streamguard) → tools through the chokepoint → verification → reply.
**Side flows, every request:** decision log, sealed journal append, usage
ledger, optional OTLP.

**Deletion:** request → dry run (predicts exactly what will go) → legal-hold
check → tombstone the journal → unlink snapshot, journal, per-user tree, heat
ledger, documents → verify from the filesystem → audit.

## 5. Failure domains

| Domain | Blast radius today | Containment |
|---|---|---|
| One provider fails | one specialist call | typed failure, failover, recorded |
| Provider returns garbage | one parse | sanitise-and-continue |
| Disk full | writes fail | captured, never escalated into a billed retry |
| Journal corrupt | one session | quarantine; reads stop at the boundary |
| Wedged lock | accounting for one call | bounded timeout, captured |
| **Host lost** | **everything** | **[NEAR-TERM] backup + verified restore is the only answer** |
| One tenant's load | **all principals** | **[NEAR-TERM] no per-tenant quota** |

## 6. Scaling model

**[CURRENT]** Single process, `ThreadingHTTPServer`, thread-per-request.
Measured ceilings: `MAX_CONCURRENT_CALLS` (default 6), and a hard **16
concurrent provider calls per host** — above that the usage-ledger lock, not the
provider, is the limiter (p99 123 ms at 16 threads, throughput ~2000/s).

**[NEAR-TERM]** Multiple stateless API processes behind a load balancer,
sharing a data plane. Blocked on the usage ledger's single-writer design.

**[TARGET]** Control plane + scheduler + worker pool with leases, idempotency
and cost-aware placement.

## 7. Tenancy model

**[CURRENT] Single-tenant with multiple principals.** A principal is derived
from an API key or a local account. Isolation is verified. There is no
container above a principal.

**[NEAR-TERM] Organisation → workspace → project → principal**, with resource
ownership, quotas, audit and retention attaching at the organisation level.

**Invariant to preserve at every step:** no platform feature may weaken
principal isolation. A tenancy model that merged namespaces would silently undo
a HIGH-severity fix already shipped.

## 8. Extension model

**[CURRENT]** Specialists (registry, no shadowing of built-ins), plugins
(handler + lifecycle hooks), MCP clients, custom actions. All external effects
funnel through the tool chokepoint.

**[NEAR-TERM]** Manifest, declared permissions, sandbox, signing, revocation —
in that order. **A marketplace is [TARGET] and must not precede them.**

## 9. Security invariants (must hold through every future change)

1. An unclassified tool is denied.
2. Irreversible and financial actions never auto-run.
3. Tool arguments are validated before any handler runs.
4. A durable artifact is never silently repaired.
5. Instrumentation cannot alter a run — proven per commit.
6. Accounting failures never escalate into billed retries.
7. Distinct credentials derive distinct namespaces.
8. Refusal, never silent downgrade.
9. Spend is capped and estimated before the fact.
10. A deletion removes every derived store and is verified.

## 10. Approval and evidence boundaries

**Approval:** risk class decides the floor; autonomy level decides the ceiling;
irreversible and financial sit above every ceiling. The agent never raises its
own autonomy.

**Evidence:** every record carries provenance from a closed set. Floors are
constants in the gate runner with no mechanism to lower them. Activation is a
human decision even after a gate passes — a passed gate authorises a *proposal*.
