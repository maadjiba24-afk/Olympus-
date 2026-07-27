# Phase 5 — Controlled Staging and Shadow Validation: Implementation Specification

**Status:** BINDING. Authority order for any conflict: production code →
`00-SYNTHESIS.md` → `PRODUCTION_READINESS_REPORT.md` → wave specs → this
document.
**Branch:** `claude/colibri-deep-analysis-gpit35`
**Baseline commit:** `0f70596`

---

## 1. Current verified state

| Fact | Evidence |
|---|---|
| Waves 1–2 complete; Wave 3 complete as scoped (1 of 5 built) | `WAVE{1,2,3}_COMPLETION_REPORT.md` |
| Four adaptive candidates deferred on unmet evidence floors | `WAVE3_EVIDENCE_REVIEW.md` |
| Phase 4 offline validation complete; verdict **CONDITIONAL GO** | `PRODUCTION_READINESS_REPORT.md` |
| Suite `5002 passed, 29 skipped, 0 failures` (221 s) | Step-0 baseline, §32 |
| All CI gates green | Step-0 baseline, §32 |
| Approved for staging + shadow. **Not** approved for production, public exposure, canary, or autonomous external action | Phase-4 decision |

**Step-0 finding (recorded, not hidden).** The first baseline run was **RED**:
`test_sync_cost_no_longer_grows_with_session_depth` failed in-suite while
passing six times in isolation. Root cause was a noise-dominated instrument
*and* an overstated claim — the D1 fix reduces journal depth-scaling 15.8×
(26.84 → 1.70 µs/turn), it does not eliminate it. Instrument replaced, claim
corrected in four places, baseline re-verified green (`0f70596`). Phase 5 work
begins from that commit.

---

## 2. Phase 5 objective

Produce a **trustworthy answer** to one question:

> Does Olympus have enough real operational evidence and deployment validation
> to begin a tightly controlled canary without endangering users, data,
> infrastructure, or budget?

Not: ship more capability. The deliverable is *evidence and safety
infrastructure*, and an honest verdict — including "no" if that is the truth.

---

## 3. In scope

1. A canonical **staging profile** that fails closed on missing configuration.
2. **Shadow mode** as a first-class, named execution mode — not a flag soup.
3. A **single side-effect boundary** with adversarial proof of containment.
4. **Evaluation suites** with independent oracles.
5. A **provider qualification campaign runner** (execution gated on credentials).
6. An **external-client compatibility harness** driving real SDKs over real sockets.
7. **Traffic generation** with mandatory provenance labelling.
8. **Operational baselines** with sample counts, distributions and provenance.
9. A **retention policy surface** (mechanism, not an invented legal period).
10. A **legacy `api-v1` namespace migration procedure**.
11. A **backup/restore drill** that actually restores.
12. **Restart, failure and recovery validation.**
13. **Re-running the four deferred Wave-3 gates**, floors untouched.
14. The eight Phase-5 reports.

## 4. Explicitly out of scope

- Implementing any deferred Wave-3 capability. A passed gate authorizes a
  *proposal*, never an enablement.
- Enabling speculative execution, predictive prefetch, local-model routing,
  provider mirroring, automatic routing substitution, or autonomous external
  tool execution. All remain off or shadow-only (rule 5).
- Any public network exposure from this environment.
- Any real deployment, DNS, TLS issuance, or cloud resource creation.
- Lowering any evidence threshold (rule 6).
- New adaptive capability of any kind.

---

## 5. Existing seams to extend (never duplicate)

Recon confirmed a usable substrate. **No parallel infrastructure will be
created** where one of these can be extended.

| Need | Existing seam | Action |
|---|---|---|
| Container | `Dockerfile` (py3.12-slim, hash-pinned lock, `VOLUME /app/memory`) | reuse unchanged |
| Orchestration | `deploy/docker-compose.yml` | **add a staging profile**; production compose untouched |
| HTTP server | `web.serve()` → `ThreadingHTTPServer` | add lifecycle, do not replace |
| Liveness | `GET /healthz` (exists, unauthenticated) | keep; add readiness beside it |
| Metrics | `GET /api/metrics` → `metrics.snapshot()` | extend with mode + build info |
| Tracing | `olympus/otel.py` (OTLP exporter) | reuse |
| Retention | `memory.sweep_dated_files` + `sweep_evidence`, driven by heartbeat | extend |
| Backup/restore | `olympus/backup.py` (`restore()` exists) | drill it, don't rewrite |
| Migrations | `olympus/migrate.py` | extend for legacy namespace |
| Actions/approval | `olympus/actions.py` — 4 risk classes, 5 autonomy levels, `_execute` | make shadow-aware |
| Egress policy | `olympus/egress.py` — `classify` / `guard` | reuse for the boundary |
| Tool dispatch | **`tools.resolve_handler`** — the single chokepoint both loops use | wrap in shadow mode |
| Replay | `olympus/replaystore.py` | reuse as a traffic source |
| Evidence stores | 5 append-only ledgers + watchdog forensics | add provenance |
| Cost control | `usage.check_budget`, `usage.slot`, `DAILY_BUDGET` | reuse; never weaken |
| Quarantine | `olympus/experiments.py` + `experiments.json` | record gate outcomes |

### 5.1 The tool-dispatch chokepoint (load-bearing)

Recon established that **exactly two** sites execute a council tool:

- `olympus/agent.py:152` — Anthropic-native loop
- `olympus/openai_compat.py:721` — OpenAI-compat loop

**Both** obtain the handler from `tools.resolve_handler(name)`. That single
function is therefore the enforcement point for the shadow boundary. Wrapping
it covers both dialects, plugin handlers and MCP handlers at once, with one
testable seam — rather than retrofitting dry-run adapters into the 40 modules
that can perform egress.

Defence in depth adds two further points: `actions._execute` (the approval
spine) and a network-egress choke.

---

## 6. Staging architecture

```
        ┌────────────────────────── staging host ──────────────────────────┐
        │                                                                  │
  ops → │  :8484  olympus-staging      OLYMPUS_ENV=staging                 │
  only  │         (authenticated,      OLYMPUS_SHADOW_MODE=1               │
        │          non-loopback bind)  OLYMPUS_API_KEYS=<required>         │
        │              │                                                   │
        │              ├── /healthz    liveness   (no auth)                │
        │              ├── /readyz     readiness  (no auth, no data)       │
        │              ├── /api/metrics + mode + build                     │
        │              └── /v1/*       both dialects, key-gated            │
        │                                                                  │
        │  volume: olympus-staging-memory → /app/memory  (all durable state)│
        │  NO heartbeat service. NO Caddy. NO public port.                  │
        └──────────────────────────────────────────────────────────────────┘
```

**Differences from the production profile, all deliberate:**

| Aspect | Production compose | Staging profile |
|---|---|---|
| Heartbeat (spends tokens autonomously) | on by default | **absent** |
| Caddy / TLS / public 80+443 | present | **absent** |
| Volume | `olympus-memory` | `olympus-staging-memory` (never shared) |
| Shadow mode | n/a | **required**, fails closed if unset |
| Env | unset | `OLYMPUS_ENV=staging`, validated |
| Ports | published | `expose` only — reachable on the compose network, never the host |

---

## 7. Shadow-execution architecture

Shadow mode is a **named mode**, resolved once per process from
`OLYMPUS_SHADOW_MODE`, exposed through one predicate and one context object.

**What still runs, unchanged:** routing, planning, the dependency graph,
verification, context budgeting, admission, the recovery ladder, journaling,
tracing, replay recording. Shadow mode must exercise the *real* decision path
or it measures nothing.

**What changes:** every external side effect is intercepted at the boundary
(§8) and turned into a **recorded intent** instead of an action.

**What is recorded per shadow run** (Step 4 list, all of it): routing decisions,
provider choice, latency, token usage, cache use, cost, context-budget
decisions, tool proposals, recovery-ladder activations, verification outcomes,
replay identifiers, failures and cancellations.

**Marking.** Every shadow run carries a provenance stamp in the trace, in the
evidence record, and in an `X-Olympus-Mode: shadow` response header. A shadow
request must never be mistakable for a production request — asserted by test.

---

## 8. Side-effect isolation model

One boundary module. Every external action is classified into exactly one of
five bands:

| Band | Meaning | Shadow behaviour |
|---|---|---|
| `READ` | pure read, no external mutation | **allowed** |
| `STAGING_WRITE` | reversible write inside Olympus's own staging stores | **allowed** |
| `RECORDED` | safe-to-simulate external effect | **intercepted**, intent recorded to the shadow sink, synthetic result returned |
| `APPROVAL` | needs an explicit human approval boundary | **refused** in shadow |
| `PROHIBITED` | irreversible or financial | **refused** in shadow, always |

Coverage required by Step 5: email, calendar, file mutation, outbound webhooks,
repository changes, non-Olympus database writes, connector actions, shell
commands, financial actions, user notifications.

**Default is deny.** An unclassified tool is treated as `PROHIBITED` in shadow
mode — a new tool cannot leak by being forgotten. This is the reject-never-
repair principle applied to the action surface.

### 8.1 Adversarial cases that must be proven contained

Direct execution · retries · recovery ladders · malformed tool calls · plugin
hooks (`emit_pre_tool` rewrite) · replay · provider-generated tool calls ·
alternate API dialect (both `/v1` surfaces) · cancellation races.

---

## 9. Data classification

Unchanged from `PRIVACY_RETENTION_REVIEW.md` §2. Staging adds:

| Store | Sensitivity | Staging rule |
|---|---|---|
| `shadow/intents.jsonl` | MEDIUM — contains tool arguments | subject to `RETAIN_DAYS`; never leaves the volume |
| `eval/results.jsonl` | LOW — task ids, scores, timings | retained |
| `provider_qual/cards.jsonl` | LOW | retained |

No real user data may enter staging. Enforced procedurally (§28), not by code —
stated plainly rather than claimed as a control.

---

## 10. Evidence-source classification (rule 7)

Every evidence record **must** carry a `provenance` field. Missing provenance
is a hard validation failure, not a default.

| Value | Meaning |
|---|---|
| `synthetic` | generated by Olympus's own fixtures/fuzzers |
| `replay-derived` | from a frozen replay fixture |
| `shadow-provider` | a fake/stub provider in shadow mode |
| `real-provider-staging` | a real provider API call from staging |
| `real-client-staging` | a real external SDK client over a real socket |
| `real-user` | production traffic — **never produced in Phase 5** |

**Synthetic and operational evidence must never be aggregated into one
unlabelled metric.** Any aggregate reports its provenance mix or is invalid.

---

## 11. Persistent-storage model

All durable state under `MEMORY_DIR` (`OLYMPUS_MEMORY_DIR`, default
`/app/memory` in-container), on a named volume. Owned by the container user;
the process must fail closed at startup if the path is absent or unwritable
rather than silently degrading to a container-local path that dies with the
container.

## 12. Secrets model

Loaded from the environment via `env_file`, never from source control. The repo
ships `.env.example` only. Startup validation reports *which* secrets are
present by name and never their values. Vault contents stay Fernet-encrypted.

## 13. Network exposure model

Staging uses `expose`, never `ports`. There is **no** public listener and no
reverse proxy in the staging profile. Non-loopback binding requires
`OLYMPUS_API_KEYS` (or `OLYMPUS_ACCESS_TOKEN` / `OLYMPUS_REQUIRE_LOGIN`) — the
Phase-4 F4 fix already enforces this and staging validation asserts it up front
instead of at first request.

## 14. Authentication and principal isolation

Per-key principals via `web.Handler._v1_principal` (Phase-4 F2). Staging
validation requires ≥1 API key and asserts distinct keys derive distinct
namespaces before serving. Isolation must survive concurrency and restore
(P5-A14).

## 15. Observability model

`/healthz` liveness · `/readyz` readiness (config valid, stores writable, mode
resolved) · `/api/metrics` extended with mode, build commit and version · OTLP
via `otel.py` · structured evidence ledgers. Instrumentation must stay
non-interfering (P5-A18) — the existing gate re-runs unchanged.

## 16. Retention and deletion model

Per Step 11, mechanism only. Global default, per-principal override, legal
hold, session deletion, derived-memory deletion, replay deletion, evidence
deletion, backup-expiry documentation, tombstone propagation, compaction,
deletion verification, dry-run report. **No legal period is invented.** Absent
an operator policy the deployment stays blocked for regulated or multi-user
personal-data use, and the report says so.

## 17. Backup and restore model

`backup.create` → integrity check → restore into a *clean* tree → verify
journals, replay store, evidence stores, principal isolation, tombstones,
config compatibility, and that the app starts. **A backup that has not been
restored is not a verified backup** (P5-A10).

## 18. Provider qualification plan

Campaign runner measures the Step-7 list per provider/model. Cards are written
**only** from executed measurements. With no credentials: runner implemented,
validated against deterministic fixtures, campaign marked NOT EXECUTED, and
**no production-eligible card is written** (P5-A9).

## 19. External-client compatibility plan

Recon **confirmed this is executable here**: `anthropic==0.120.0` and
`openai==2.48.0` are installed, and a feasibility probe drove the real
`web.Handler` over a real TCP socket with both SDKs, non-streaming and
streaming, successfully. The campaign therefore runs for real.

**Claim ceiling.** This upgrades the Phase-4 claim from *SDK-type-verified* to
**real-SDK-over-HTTP verified in staging**. It does **not** reach
*production-client verified*: the upstream provider is stubbed, and no
third-party application drove it across a real network. The claim will be
stated at exactly that strength (rule 1).

## 20. Traffic-generation plan

Seven sources per Step 9, each stamping provenance. In Phase 5 only sources
1–3 and 6 can be produced; 4 (historical sanitized traces) is unavailable, 5
requires staging deployment, 7 is forbidden.

## 21. Evaluation and benchmark plan

Versioned suites over the Step-6 dimensions. **No self-confirming tests**: the
expected outcome must come from an independent oracle — a fixed fixture, a
property, or a structural invariant — never from the same code path that
produces the observed outcome.

## 22. Baseline metrics

Per Step 10. Each metric reports **sample count, mean, median, p95, p99, max,
CI where meaningful, and provenance**. A metric without n and provenance is not
promotion evidence and must not appear in a promotion table.

## 23. Rollback triggers

The Phase-4 canary triggers stand. Every one is expressed against a
shadow-measured baseline; Phase 5 either produces that baseline or records that
it did not.

## 24. Promotion criteria

`GO FOR CONTROLLED CANARY` requires **all** of: P5-A1…A25 pass; ≥1 provider
qualified from executed evidence; the client-compatibility campaign executed;
a restored backup verified; a retention policy supplied by the operator; and
shadow baselines exist for every rollback trigger. Any miss caps the verdict at
`CONDITIONAL GO FOR CONTINUED STAGING`.

## 25. Security threats introduced by Phase 5

| Threat | Control |
|---|---|
| Shadow boundary bypassed via a new/unclassified tool | default-deny classification |
| Shadow sink used to exfiltrate arguments | sink is volume-local, retention-swept, never egresses |
| Staging profile accidentally run as production | `OLYMPUS_ENV` validated; production defaults refused |
| Test key promoted to production | staging keys are distinct principals by construction |
| Legacy `api-v1` data auto-assigned to a principal | migration is explicit, never automatic (P5-A13) |
| Restore leaking cross-principal data | isolation re-asserted after restore (P5-A14) |

## 26. Cost budgets

Shadow with a stubbed provider costs **$0**. Any real-provider campaign is
bounded by `OLYMPUS_DAILY_BUDGET` and the pre-flight worst-case estimator from
the Wave-1 B2 fix. The usage-ledger concurrency bound (≤16 concurrent provider
calls/host) is a **hard operating limit** and must not be weakened by batching
(P5-A20).

## 27. Failure semantics

Unchanged and re-asserted: refusal over silent degradation; reject-never-repair
for durable artifacts; sanitize-and-continue for ephemeral provider payloads;
accounting failures never escape into provider retry (Stage-C D1); spend caps
hold under retry and recovery (P5-A15).

## 28. Migration plan

1. Staging profile is additive — the production compose is untouched.
2. Shadow mode defaults **off**; enabling is explicit.
3. Boundary classification ships default-deny; every existing tool is
   classified in the same commit, so no tool changes behaviour outside shadow.
4. Legacy `api-v1` migration is operator-run, dry-run first, audited.
5. No on-disk format changes.

## 29. Rollback plan

| Unit | Rollback |
|---|---|
| Staging profile | delete the profile; nothing else references it |
| Shadow mode | unset `OLYMPUS_SHADOW_MODE` — every seam is a no-op when off |
| Side-effect boundary | flag-off restores the exact prior dispatch; asserted by an identity test |
| Eval / campaign / traffic harnesses | additive scripts; deleting them changes no runtime path |
| Retention surface | mechanism is opt-in; default behaviour documented and unchanged |

## 30. Acceptance matrix

**Provable locally** = provable in *this* environment with no external
dependency.

| Gate | Requirement | Provable locally? | Blocking dependency |
|---|---|---|---|
| P5-A1 | clean baseline | ✅ **done** | — |
| P5-A2 | staging config fails closed | ✅ | — |
| P5-A3 | durable stores use persistent paths | ✅ | — |
| P5-A4 | restart recovery | ✅ | — |
| P5-A5 | shadow cannot perform prohibited side effects | ✅ | — |
| P5-A6 | evidence provenance mandatory | ✅ | — |
| P5-A7 | synthetic vs operational distinguishable | ✅ | — |
| P5-A8 | real-client HTTP harness exists **and runs** | ✅ (probe confirmed) | — |
| P5-A9 | qualification cards require executed evidence | ✅ (the *rule*) | provider credentials for the campaign |
| P5-A10 | backup restored successfully | ✅ | — |
| P5-A11 | retention dry run accurate | ✅ | — |
| P5-A12 | deletion removes/tombstones derived data | ✅ | — |
| P5-A13 | legacy namespace not auto-assigned | ✅ | — |
| P5-A14 | isolation survives concurrency and restore | ✅ | — |
| P5-A15 | spend caps hold under retry and recovery | ✅ | — |
| P5-A16 | cancellation propagates end to end | ✅ | — |
| P5-A17 | replay deterministic | ✅ | — |
| P5-A18 | instrumentation non-interfering | ✅ | — |
| P5-A19 | journal hot path within bound | ✅ | — |
| P5-A20 | usage contention respects the limit | ✅ | — |
| P5-A21 | every env var documented | ✅ | — |
| P5-A22 | full suite passes | ✅ | — |
| P5-A23 | all CI/security gates pass | ✅ | — |
| P5-A24 | Wave-3 gates re-run, floors unchanged | ✅ | — |
| P5-A25 | untestable external claims labelled untested | ✅ | — |
| — | staging actually deployed | ❌ | deployment target |
| — | real-provider qualification executed | ❌ | provider credentials |
| — | shadow traffic baselines | ❌ | deployment + credentials |

**23 of 25 gates are fully provable here. P5-A9 is provable as a rule but its
campaign cannot execute. Three non-gate objectives are hard-blocked.**

## 31. PR decomposition (dependency order)

| # | Unit | Depends on |
|---|---|---|
| P5-1 | This spec | Step-0 baseline |
| P5-2 | Staging profile: config validation, `/readyz`, graceful shutdown, build info, compose profile | P5-1 |
| P5-3 | Shadow mode: `olympus/shadow.py`, provenance, markers | P5-2 |
| P5-4 | Side-effect boundary + adversarial suite | P5-3 |
| P5-5 | Evidence provenance enforcement | P5-3 |
| P5-6 | Evaluation suites + independent oracles | P5-3 |
| P5-7 | Provider qualification runner | P5-6 |
| P5-8 | External-client compatibility campaign | P5-2 |
| P5-9 | Traffic generation + baselines | P5-4, P5-6 |
| P5-10 | Retention policy surface | P5-1 |
| P5-11 | Legacy namespace migration | P5-1 |
| P5-12 | Backup/restore drill | P5-2 |
| P5-13 | Restart/failure/recovery validation | P5-2 |
| P5-14 | Wave-3 gate re-run | P5-9 |
| P5-15 | The eight reports | all |

Each commit carries tests, migration notes, rollback notes, and security,
privacy and cost implications.

**Module budget.** Synthesis B1 caps new modules at 14; 11 are spent. Phase 5
admits at most **2**: `shadow.py` and `sideeffects.py`. Each is justified under
the admission test (distinct durable state; independent platform invariant;
harmful coupling if merged; independently testable lifecycle). Everything else
extends an existing module or lives in `scripts/`, which is not a runtime
module. **1 slot remains reserved.**

---

## 32. Exact limitations of this execution environment

Stated plainly so nothing downstream over-claims (rules 1–3).

### Available
- Full source tree, full offline suite, all CI gates.
- Real `anthropic==0.120.0` and `openai==2.48.0` SDKs.
- Real TCP sockets on loopback; real `ThreadingHTTPServer`.
- Real filesystem, real `flock`, real subprocesses.
- `docker compose` CLI v5.1.1 — schema validation only (no daemon).

### Not available
| Missing | Consequence |
|---|---|
| Provider API credentials | no real model call; no real latency/cost/quality; provider qualification **cannot execute** |
| Deployment target (host, cloud, k8s) | staging profile is **authored and validated, never deployed** |
| Externally reachable endpoint | no third-party client, no DNS, no TLS |
| Representative traffic | no operational baseline; all Phase-5 numbers are synthetic or replay-derived |
| Real users | forbidden by the programme regardless |
| Historical sanitized traces | traffic source 4 unavailable |
| Docker **daemon** (`docker info` fails; compose CLI v5.1.1 present) | compose files validated by `docker compose config` only — **never** by `docker compose up`. No image is built, no container is started, in this environment |

### The consequence for the verdict

`GO FOR CONTROLLED CANARY` requires executed provider qualification and shadow
baselines. **Both are hard-blocked here.** The reachable ceiling for
`PHASE5_COMPLETION_REPORT.md` is therefore
**`CONDITIONAL GO FOR CONTINUED STAGING`**, and that is stated now — before the
work — so no later result is bent toward a decision that was never available.

`NO-GO` remains reachable if the buildable work fails.
