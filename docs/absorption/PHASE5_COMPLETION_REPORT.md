# Phase 5 — Completion Report: Controlled Staging and Shadow Validation

**Branch:** `claude/colibri-deep-analysis-gpit35` · **Date:** 2026-07-26
**Suite:** `5141 passed, 30 skipped, 0 failures`
**Gates:** compileall ✅ · capabilities drift ✅ · threat model (130 tools) ✅ ·
non-interference (exit 0) ✅ · no-prerelease ✅ · env-docs ✅ ·
experiments registry ✅

---

# DECISION: **CONDITIONAL GO FOR CONTINUED STAGING**

Not `GO FOR CONTROLLED CANARY`. Not `NO-GO`.

## Why this rung, in one paragraph

Phase 5 built and adversarially proved the safety substrate a canary needs, and
executed one campaign that Phase 4 had recorded as impossible. It did **not**
produce operational evidence, because producing it requires a deployed instance,
provider credentials and traffic — none of which exist here. A canary approval
requires shadow-measured baselines for every rollback trigger and at least one
provider qualified from executed evidence. Both are absent. That is a
sequencing fact, and it was stated in the specification **before** the work
began, so no result has been bent toward a verdict that was never available.

---

## 1. The one honest sentence about each phase objective

| Objective | Outcome |
|---|---|
| Create a production-like staging environment | **Authored and validated. Never deployed** — no Docker daemon, no host. |
| Run Olympus safely in shadow mode | **Built, wired, adversarially proven. Never fed** — no traffic. |
| Accumulate trustworthy operational evidence | **Not accumulated.** 25 real-client records; everything else synthetic. |
| Establish measurable baselines | **Offline baselines only.** No end-to-end, provider, or cost baseline exists. |
| Determine whether the four Wave-3 capabilities may be reconsidered | **Determined: no.** 4 NO-GO on unchanged floors. |

---

## 2. What was actually completed

| Unit | Deliverable | Status |
|---|---|---|
| P5-0 | Clean baseline | ✅ — and it was **red** first (§4) |
| P5-1 | `PHASE5_STAGING_SHADOW_SPEC.md`, 32 sections | ✅ |
| P5-2 | Staging profile: fail-closed boot, `/readyz`, SIGTERM drain, build info, compose profile | ✅ authored, **not deployed** |
| P5-3 | Shadow mode as a named mode | ✅ |
| P5-4 | Single side-effect boundary + adversarial suite | ✅ |
| P5-8 | External-client compatibility campaign | ✅ **EXECUTED, 25/25** |
| P5-10 | Retention policy surface | ✅ mechanism; **policy deliberately unset** |
| P5-11 | Legacy `api-v1` procedure | ✅ |
| P5-12 | Backup/restore drill | ✅ **EXECUTED** |
| P5-13 | Restart/failure/recovery validation | ✅ **EXECUTED** |
| P5-14 | Wave-3 gate re-run | ✅ **4 NO-GO, floors untouched** |
| P5-15 | Eight reports | ✅ |
| P5-6/7 | Eval suites, provider qualification | ⚠️ **runner-only** — see §5 |
| P5-9 | Traffic generation at volume | ⚠️ substrate only |

New tests this phase: **+139** (5002 → 5141).
New modules: **2 of 3 remaining slots** (`shadow`, `retention`); 1 held in
reserve. `sideeffects` was folded into `shadow` — recorded as a deviation from
my own spec, with reasoning, in the module docstring rather than discovered
later.

---

## 3. The one thing Phase 5 proved that Phase 4 said was impossible

`PRODUCTION_READINESS_REPORT.md` recorded gate **G2** (real-client
verification) as blocked: *"no third-party client can drive a socket here."*

**Recon proved that wrong.** `anthropic==0.120.0` and `openai==2.48.0` are
installed and loopback sockets work. The campaign ran: **25/25 cases**, both
dialects, real SDKs, real TCP, real HTTP, real SSE, against the real
`web.Handler`. Three Phase-4 fixes were re-proved over the wire — B-F1
(`Infinity` → 400 with the connection intact), B-F2 (two keys → two distinct
principals), W3-A2 (`top_k` refused loudly).

**The claim ceiling is enforced, not merely stated.** A CI test scans every
document in `docs/absorption/` and fails on any unhedged assertion of
production-client verification, distinguishing asserting from mentioning; a
second test proves that detection fires on a bare claim rather than passing
vacuously. G2 is **substantially, not fully** discharged: the upstream provider
is stubbed, and nothing crossed a real network.

---

## 4. Findings Phase 5 produced about Phase 4

Recorded because a phase that only confirms the previous phase is not auditing
it.

**F5-1 — the Phase-4 baseline was red.** The first full-suite run failed
`test_sync_cost_no_longer_grows_with_session_depth`, which passed six times in
isolation. The instrument was noise-dominated *and* the claim it defended was
overstated: the D1 journal fix reduces depth-scaling **15.8×** (26.84 → 1.70
µs/turn), it does not eliminate it. "Flat in depth" appeared in the test name,
the perf harness, an SLO note and the readiness report; all four were
corrected, and the residual — an O(history) term inherent to `sync`'s contract
— is now documented with a measured table. The journal-append SLO is recorded
as **depth-qualified to ~3000 turns**.

**F5-2 — a published reproduction command silently stopped measuring what it
claimed.** `WAVE3_EVIDENCE_REVIEW.md` publishes
`print(len(modelgrade.cards()), …)`, expecting `0`. It now prints `4` — because
`cards()` returns the whole document and `len()` counts its four keys. The real
card count is `counts["cards"]`, still `0`. Verdict unchanged; the defect class
is not — a reviewer running this repo's own published evidence would have read
it as *"the floor is now four-fifths met"*. The re-run harness measures the
right field with the explanation inline, so the correct measurement is
executable rather than prose that can drift again.

**F5-3 — this phase's own unit caused a 100-test regression, and it is on the
record.** The client-compatibility harness installed a stub on
`web.orchestrator.Olympus` — a module attribute — and never restored it, so
every later test in the session ran against the stub. Caught by the full suite,
fixed with a `restore()` closure, and the incident is recorded in the
function's docstring so the next editor does not repeat it.

**F5-4 — two CI gates caught real drift from Phase-5 work.** The env-docs gate
caught `OLYMPUS_BIND_HOST`, a knob introduced in the same commit. The ctxheat
wiring guard caught `retention.py` naming ctxheat; rather than widening the
allowlist I paired the allowance with an AST check that `retention.py` never
*imports* ctxheat — the guard is now stricter than before.

---

## 5. What is NOT done, and exactly why

Rule 3 requires these to be separated. They are.

| Category | State |
|---|---|
| **Code implemented** | staging profile, shadow mode, side-effect boundary, retention surface, legacy procedure, campaign runners, gate re-runner |
| **Locally simulated** | restart, disk-full, read-only, torn tail, corrupt record, corrupt snapshot, interrupted sweep, concurrency |
| **Offline validated** | 5141 tests, 7 CI gates, the whole Stage-D perf harness |
| **Deployed to staging** | ❌ **NOTHING.** No daemon, no host. `docker compose config` only. |
| **Exercised with real providers** | ❌ **NOTHING.** No credentials. Zero model calls. |
| **Exercised with real external clients** | ✅ **25 cases**, real SDKs over real HTTP, stubbed upstream |
| **Exercised with shadow traffic** | ❌ **NOTHING.** No traffic source. |
| **Exercised with real users** | ❌ **NOTHING**, and forbidden. |

### Blocked items, with the exact missing dependency

| # | Item | Missing |
|---|---|---|
| B1 | Staging actually deployed | a Docker daemon and a host |
| B2 | Provider qualification campaign | provider API credentials |
| B3 | Shadow traffic baselines | a deployment + credentials + traffic |
| B4 | Rollback thresholds calibrated | B3 |
| B5 | SLOs sized from observed traffic | B3 |
| B6 | Tool-use round-trip via a real client | B2 |
| B7 | Real-network behaviour (TLS, proxy, NAT) | B1 |
| B8 | Evaluation suites with quality grading | B2 + an independent oracle |
| B9 | Local inference tier qualification | a local model runtime |
| B10 | **A conversation-retention policy** | **an operator/legal decision — not a technical blocker** |

B10 is the only one a human can clear today, with one environment variable.

---

## 6. Acceptance matrix

| Gate | Verdict | Evidence |
|---|---|---|
| P5-A1 clean baseline | ✅ | red first, diagnosed, fixed, re-verified (F5-1) |
| P5-A2 staging config fails closed | ✅ | `test_phase5_staging.py`, every problem reported at once |
| P5-A3 durable stores persistent | ✅ | boot refuses an unset/unwritable memory dir |
| P5-A4 restart recovery | ✅ | data-layer restart + 6 fault classes |
| P5-A5 shadow blocks prohibited effects | ✅ | 10 bypass routes, tripwire-proven |
| P5-A6 provenance mandatory | ✅ | closed set; unknown → hard error |
| P5-A7 synthetic vs operational distinguishable | ✅ | never aggregated unlabelled |
| P5-A8 real-client HTTP harness | ✅ **exceeded** — it exists *and ran*, 25/25 |
| P5-A9 cards require executed evidence | ✅ **as a rule**; campaign ❌ not executed |
| P5-A10 backup restored | ✅ | into a clean tree, read back, tamper refused |
| P5-A11 retention dry run accurate | ✅ | plan set == deletion set |
| P5-A12 deletion removes derived data | ✅ | history unrecoverable after delete |
| P5-A13 legacy not auto-assigned | ✅ | verbatim ack + module-wide AST scan |
| P5-A14 isolation survives concurrency and restore | ✅ | 3-principal race + post-restore + over the wire |
| P5-A15 spend caps hold | ✅ | 90/90 increments under 6 threads |
| P5-A16 cancellation propagates | ✅ | disconnect mid-stream, server healthy |
| P5-A17 replay deterministic | ✅ | non-interference gate exit 0; journal replays identically post-restore |
| P5-A18 instrumentation non-interfering | ✅ | gate exit 0; shadow-off is object identity |
| P5-A19 journal hot path in bound | ✅ | **depth-qualified to ~3000 turns** (F5-1) |
| P5-A20 usage contention respects the limit | ✅ | curve measured; ≤16 published; batching forbidden |
| P5-A21 env vars documented | ✅ | derived scan; caught its own new knob |
| P5-A22 full suite passes | ✅ | 5141 / 0 failures |
| P5-A23 CI and security gates pass | ✅ | 7 gates |
| P5-A24 Wave-3 gates re-run, floors unchanged | ✅ | 4 NO-GO; `floors_modified: false` |
| P5-A25 untestable claims labelled | ✅ | §5, and every report's limits section |

**24 of 25 fully met. P5-A9 met as a rule; its campaign is blocked on
credentials.**

---

## 7. The four Wave-3 candidates

**4 NO-GO.** No floor was modified — the re-run harness holds them as constants
and has no mechanism to lower one.

| Candidate | n | Floor | Provenance |
|---|---|---|---|
| Lossless speculation | 0 cards | qualified draft + verifier per cell | none |
| Predictive prefetch | 1 run | n ≥ 200, recall@2 ≥ 0.6, CI ≤ 0.1 | **synthetic** |
| Local inference tier | 0 cards | qualifying cards per cell | none |
| Provider mirroring | 0 decisions | a measured unavailability rate | none |

Prefetch moved 0 → 1 run. That is **not** progress: the run came from this
repository's own test executions, provenance `synthetic`, and rule 7 is
explicit that synthetic data cannot satisfy a floor whose purpose is to
characterise real traffic. Reported NO-GO on provenance as well as on count.

---

## 8. Standing safety posture

Unchanged and re-verified: speculative execution, predictive prefetch,
local-model routing, provider mirroring, automatic routing substitution and
autonomous external tool execution are **all off**. No adaptive capability was
implemented. No threshold was lowered. All four candidates remain `proposed` in
`experiments.json`; `localtier`, `draftverify` and `coalesce` still do not
exist.

---

## 9. What must happen before a canary is reconsidered

**In order.** Nothing later is meaningful without what precedes it.

1. **Deploy the staging profile** to a real host with a Docker daemon.
   Discharges B1, B7; validates everything §4 lists as unverified-by-execution.
2. **Set a conversation-retention policy.** One variable. Until then this
   instance is not cleared for regulated or multi-user personal data.
3. **Provision provider credentials** with a hard `OLYMPUS_DAILY_BUDGET`, and
   run the qualification campaign. Discharges B2, B6.
4. **Drive shadow traffic** — replay fixtures and synthetic workloads first,
   then a real source — until every metric in
   `PHASE5_SHADOW_EVIDENCE_REPORT.md` §6's "not measured" list has n, a
   distribution, and provenance.
5. **Calibrate the rollback thresholds** against those baselines, replacing
   every PROPOSAL SLO with a measurement.
6. **Re-run the Wave-3 gates** against operational evidence.
7. **Re-issue this decision.**

---

## 10. The question this phase was asked

> *Does Olympus have enough real operational evidence and deployment validation
> to begin a tightly controlled canary without endangering users, data,
> infrastructure, or budget?*

**No — and the two halves of that answer are different.**

On **danger**, the substrate is in good shape. The side-effect boundary is
default-deny across all 130 tools and holds through ten adversarial bypass
routes. Spend caps hold under concurrency and never degrade to a cheaper model.
Principal isolation holds under concurrency, across a restore, and over a real
socket. Backups restore and are verified by reading the data back. Deletion is
complete and verified. Every destructive operation defaults to a dry run.

On **evidence**, there is essentially none. No model has been called. No
traffic has been served. Every rollback trigger is expressed against a baseline
that does not exist, so a canary could not be *monitored*, let alone rolled
back on a threshold. Starting one now would mean discovering the baselines from
production users — which is precisely what this programme forbids.

**CONDITIONAL GO FOR CONTINUED STAGING.** The gate to the next rung is
deployment and traffic, not more code.

---

*Companion reports: `PHASE5_STAGING_SHADOW_SPEC.md` ·
`PHASE5_STAGING_REPORT.md` · `PHASE5_SHADOW_EVIDENCE_REPORT.md` ·
`PHASE5_CLIENT_COMPATIBILITY_REPORT.md` ·
`PHASE5_PROVIDER_QUALIFICATION_REPORT.md` ·
`PHASE5_BACKUP_RECOVERY_REPORT.md` · `PHASE5_RETENTION_DELETION_REPORT.md` ·
`WAVE3_REVIEW_AFTER_SHADOW.md`*
