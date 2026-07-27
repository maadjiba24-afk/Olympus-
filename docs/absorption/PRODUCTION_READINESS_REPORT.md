# Olympus — Production Readiness Report

**Scope:** the Colibri absorption programme (Waves 1–3) and the full-system
validation that follows it.
**Branch:** `claude/colibri-deep-analysis-gpit35` · **Date:** 2026-07-26
**Suite at time of writing:** `5002 passed, 29 skipped, 0 failures`
**CI gates:** compileall ✅ · capabilities drift ✅ · threat model (130 tools) ✅ ·
non-interference (exit 0) ✅ · experiments registry ✅ · env-docs ✅

---

# DECISION: **CONDITIONAL GO**

Olympus is **not** approved for production traffic, and **not** approved for a
canary, on the evidence available here. It **is** approved to proceed to
staging and shadow traffic, subject to the four conditions in §8.

## Why not a higher decision

The decision vocabulary this programme committed to has four rungs. Three of
them are unreachable from this environment, and saying so is the finding — not
a caveat on a better answer.

**PRODUCTION APPROVED is unreachable.** It requires a completed canary, and a
canary requires deployed traffic. Neither exists.

**LIMITED CANARY APPROVED is unreachable.** The programme's own sequence is
build → verify → attack → benchmark → **stage → shadow** → canary → production.
Stages 1–4 are complete and evidenced below. Stages 5 and 6 have not been
attempted, because this environment has no deployment target, no provider
credentials, and no traffic. A canary approval granted without a shadow phase
would be exactly the "use real users as the test suite" failure the programme
forbids.

**NO-GO is not supported by the evidence either.** No unresolved critical or
high-severity defect remains. Every blocker category enumerated for Phase 4 was
explicitly tested, every defect found was either fixed with a regression test or
recorded with its measurement and an operating bound. Declaring NO-GO would
misreport a system whose failures were found, fixed, and pinned.

**CONDITIONAL GO is the honest rung.** The conditions are enumerated in §8, and
every one of them is a *measurement that must be taken*, not a judgement that
must be made.

---

## 1. What "conditional" is conditional on

| # | Condition | Why it cannot be discharged here | Discharged by |
|---|---|---|---|
| **G1** | Shadow traffic ≥ 200 runs per task cell, distributionally reported | No provider credentials, no traffic | Phase 5 |
| **G2** | Real-client verification of the Anthropic-compatible surface | No third-party client can drive a socket here | Phase 5 |
| **G3** | Cross-principal isolation re-verified on a live multi-key deployment | Only the derivation is testable offline | Phase 5 |
| **G4** | Error budgets sized from observed traffic, replacing the drafts | Every SLO below is a PROPOSAL, none is measured | Phase 5 |

Until G1–G4 are discharged, four of the five Wave-3 candidates and every
service objective in §6 remain unqualified. That is a *sequencing* fact, not a
quality one.

---

## 2. Program status by wave

| Wave | Verdict | Evidence |
|---|---|---|
| **Wave 1** — 8 capabilities | **COMPLETE, independently audited** | `WAVE1_COMPLETION_REPORT.md`, `WAVE1_INDEPENDENT_AUDIT.md`. Four independent auditors, forbidden from patching source, wrote +151 adversarial tests. 2 blockers + 1 false claim found; all three resolved. |
| **Wave 2** — 10 capabilities | **COMPLETE, one named gap** | `WAVE2_COMPLETION_REPORT.md`. First verdict was NOT COMPLETE (built but unwired) and is retained as the record. 17/17 gates pass after the integration wave. Named gap A3 below. |
| **Wave 3** — 5 candidates | **COMPLETE AS SCOPED — 1 built, 4 deferred** | `WAVE3_EVIDENCE_REVIEW.md`, `WAVE3_COMPLETION_REPORT.md`. No floor was lowered to admit a candidate; no candidate was partially built "to be ready". |
| **Phase 4** — Stages A–E | **COMPLETE** | This report, §4–§7. |
| **Phase 5** — staging + shadow | **NOT ATTEMPTED** | Not executable in this environment. |
| **Phase 6** — canary | **NOT ATTEMPTED** | Blocked on Phase 5. |

**Module budget (Synthesis B1, hard cap 14):** 11 new modules admitted —
`sessionlog`, `ctxbudget`, `modelgate`, `coupling` (Wave 1); `modelgrade`,
`ctxheat`, `routesub`, `experiments`, `ingestgate`, `watchdog`, `streamguard`
(Wave 2). **3 of 14 unspent.** Three planned modules (`localtier`,
`draftverify`, `coalesce`) were never created because their Wave-3 candidates
failed their evidence gates — the cap was not consumed by speculative code.

### The four deferrals, and why they are not failures

| Candidate | Gate | Measured | Verdict |
|---|---|---|---|
| Lossless council speculation | a qualified draft **and** verify model per cell | `modelgrade` cards = 0 | DEFERRED |
| Coupling-driven local pre-work | recall@2 ≥ 0.6, CI ≤ 0.1, **n ≥ 200** | `n_runs = 0`, `insufficient_data` | DEFERRED |
| Local inference qualification | ≥ 1 qualification card | 0 cards, no local runtime | DEFERRED |
| Provider mirror routing | a measured provider-unavailability rate | `decisions = 0` | DEFERRED |

**All four gates need operational data that only Phase-5 shadow traffic
produces.** This surfaces a structural ordering dependency in the programme as
written: the declared order runs Phase 3 (Wave 3) before Phase 5 (shadow), but
four of five Wave-3 gates cannot be evaluated until after Phase 5. The gates
were **not** relaxed to resolve the ordering. The deferrals stand, and Wave 3
must be re-reviewed after shadow traffic exists.

---

## 3. Blocker categories: every one explicitly tested

The programme forbids writing "zero blockers" unless every blocker category was
explicitly tested. Each row below names the artifact that tested it. **No
category is asserted clean without a test behind it.**

| Category | Tested by | Result |
|---|---|---|
| End-to-end integration | `test_val_integration.py` (43) | 3 defects → all fixed |
| Fuzz / parser properties | `test_val_security.py` checks 5–6, 500 seeded iterations each | 1 crash site → fixed |
| Prompt injection | check 7 | no bypass |
| Tool injection (I-T1) | check 8 | no bypass |
| Cross-user isolation | check 9 | 1 HIGH → fixed |
| Cache poisoning | check 10 | no bypass |
| Evidence poisoning | check 11 | no bypass |
| Replay attacks | check 12 | no bypass |
| Rollback attacks | check 13 | 1 residual, registered |
| Malicious plugin | check 14 | contained |
| Provider compromise | check 15 | 1 MEDIUM → fixed |
| Denial-of-wallet | check 16 | refuses, never downgrades |
| Privilege boundary | check 17 | 1 LOW → fixed |
| Fault injection / recovery | `test_val_reliability.py` (62), 16 fault classes | 3 defects → all fixed |
| Performance / cost | `test_val_performance.py` (21) + `scripts/perf_validation.py` | 1 defect → fixed; 1 finding → bounded |
| Privacy / retention | `test_val_privacy.py` (6) | 1 finding → fixed; 1 pre-existing gap open |
| Concurrency / contention | `bench_usage_contention`, `bench_admission` | ceiling measured, bound published |
| **Deployment / canary** | — | **NOT TESTED — no target exists (§8)** |
| **Real-client compatibility** | — | **NOT TESTED — claim bounded instead (§5)** |
| **Live traffic behaviour** | — | **NOT TESTED — this is what Phase 5 is for** |

The last three rows are the reason this report does not say "zero blockers".

---

## 4. Defects found in Phase 4, and their disposition

Twelve defects were found by the Stage A–E validators. **Ten were fixed with a
regression test written against the fixed behaviour** (not the symptom, so it
survives refactors). Two were re-characterised or accepted with a measured
bound. None was left silently open.

### Fixed

| ID | Sev | Defect | Fix |
|---|---|---|---|
| A-D1 | MED | `replay_run` reported a divergence on **every** ordinary verified run — `synthesis_check` is emitted after `_pipeline`, which replay does not re-execute. `replaygate` turns a divergence into a GitHub issue, so this was a steady drip of false alarms. | Only what replay re-executes is compared. |
| A-D2 | MED | A cancelled run's forensics said `"ok"` — the preserved record contradicted the refusal the user was shown. | The lease closes with its real outcome. |
| A-D3 | MED | A dropped history slice could leave **no durable evidence**: the truncation was a trace event only, but `_maybe_compact` runs from `_finish` where `trace.current()` is `None` and the trace is already flushed. | Unconditional `errors.capture` before the flag-gated path. |
| B-F1 | MED | `json.loads` accepts `Infinity`; `int(float("inf"))` raises `OverflowError`, which is neither `TypeError` nor `ValueError` — it escaped the handler and dropped the connection. | Guard widened; the infinities take the same in-band refusal as every other bad shape. |
| B-F2 | **HIGH** | Every `/v1` API key shared **one** memory namespace (`user="api-v1"`). Key holder B could read what holder A saved through `recall_memory`, `search_sessions`, `read_document`, `list_documents`, `list_todos`. | `_v1_principal()` derives a domain-separated SHA-256 prefix per key: one-way, stable across restarts, no aliasing. |
| B-F3 | MED | The openai-compat parse dereferenced `choices[0]["message"]` and `call["function"]["name"]` outside any guard. Contained one layer up by a broad `except Exception` — by luck, not by design. | `_message_of` raises a typed provider-failure `RuntimeError`; `_named_tool_calls` drops unnameable calls; an all-malformed response degrades to text. |
| B-F4 | LOW | `/api/chat` was the one surface with no loopback fallback: with neither credential set, any peer reaching the port got an anonymous namespace and a funded council run. | All three HTTP surfaces now share the posture; peer decided from the kernel socket, never a header. |
| C-D1 | **HIGH** | An `ENOSPC` on the usage ledger escaped into `openai_compat`'s retry handler, which treats `OSError` as a **provider** fault: one logical call became **four billed HTTP POSTs**. | Accounting never escapes; a wedged lock and a full disk are captured with distinct contexts. |
| C-D2 | MED | `trace.flush()` runs from `ask()`'s `finally:` — an `OSError` there replaced an already-computed answer with a traceback. | Guarded, plus a nested guard on the overflow write. |
| C-D3 | MED | Two threads of one process shared `.{name}.{pid}.tmp`: one snapshot write was silently clobbered, the loser raised `FileNotFoundError` into its reply. | Writer-unique temp name. |
| D-D1 | MED | `sessionlog.sync` re-scanned, re-verified and re-replayed the **whole journal every turn**, so per-turn cost grew with session depth: 26.84 µs/turn of depth-scaling, reaching 82.2 ms/turn at 3000 turns. Wave 1's published "p50 3.0 ms, inside the 5 ms gate" held only for a shallow journal. | Verified replay + chain tail memoized, honoured only when the stat tuple matches **and** the tail seal still matches, both under the session lock. Coefficient drops to **1.70 µs/turn — a 15.8× reduction, not an elimination** (§5). |
| E-F1 | MED | Retention covered `traces/` and `usage/` only — all five absorption evidence ledgers plus watchdog forensics grew without bound. | `memory.sweep_evidence`, wired into the existing heartbeat job. |

### Accepted with a measured bound, not fixed

**D-F1 — observability overhead.** First reported as "+72% to +90% of pipeline
wall time", crossing the >20% threshold. **Re-characterised.** The percentage is
an artifact of the harness: the denominator is a fake pipeline with ~0 ms of
provider time, so any fixed cost looks enormous. The absolute cost is +1.5 to
+2.1 ms/run, which against a real model call is <0.1%. The >20% threshold does
not meaningfully apply to a zero-latency denominator.

The concurrency concern underneath it is real, and is now **measured rather
than asserted**. `usage.record` takes a cross-process lock around a
read-modify-write of the day ledger after every provider call:

| Threads | p50 | p99 | max | throughput |
|---|---|---|---|---|
| 1 | 0.28 ms | 0.4 ms | 0.4 ms | 3176 /s |
| 2 | 0.26 ms | 21.0 ms | 21.0 ms | 690 /s |
| 4 | 0.25 ms | 41.9 ms | 41.9 ms | 495 /s |
| 8 | 0.29 ms | 102.1 ms | 142.9 ms | 435 /s |
| 16 | 0.26 ms | 101.7 ms | 122.9 ms | 842 /s |

**The median does not degrade. The tail and the ceiling do.**

Not fixed, deliberately. Batching or deferring ledger writes would make spend
non-durable between flushes, and the spend cap is a **governance safety
property** — trading a correct cap for tail latency is the wrong trade. The
alternative (per-writer shards summed on read) changes the on-disk format every
reader and CLI report depends on; that is a designed change, not Phase-4 triage.

> **OPERATING BOUND (carried into canary).** Council fan-out at or below **16
> concurrent provider calls per host**, where the accounting tail is ~123 ms
> against multi-second provider calls. Above that the ledger lock, not the
> provider, becomes the limiter. Pinned by
> `test_val_performance.py::test_usage_ledger_tail_under_concurrency`.

---

## 5. Claims that are deliberately bounded

The programme's honesty rule is that a claim must not exceed its evidence.
These are stated at their real strength, not their convenient one.

**Anthropic-compatible surface: SDK-type-verified, NOT real-client-verified.**
The wire contract deserializes into the *real* `anthropic` SDK models — the
SDK's own schema judging the output, not hand-written fixtures agreeing with
themselves. But no third-party client drove the endpoint over a socket, and
nothing here can. The broader compatibility claim is **not made**. (Gate G2.)

**Context-budget estimator: re-declared distributionally.** The original
"29.4% → 0.1%" headline was **circular** — it calibrated and measured against
the same injected constant, so it measured memorisation. Honest held-out
measurement (train n=200 → held-out n=200 per class, bootstrap 95% CI,
B=2000): english 6.9% [6.2, 7.6], code 15.7% [14.2, 17.4], cjk 11.5%
[10.2, 12.9], cyrillic 9.0% [8.1, 10.0], json 12.7% [11.4, 14.0], **mixed
43.9% [42.6, 45.2] — worse than naive `chars//4` at 29.2%**. Every class's p90
exceeds the declared ±15%. `OLYMPUS_CTX_BUDGET` stays **default off**; no
production path depends on it. Registered as `ctxbudget-calibrated-estimator`,
status `accepted_debt`, with an explicit activation condition.

**Wave-2 named gap A3.** `ctxheat` is wired into `recall.retrieve` and its
gate/rollback mechanism is live, but retrieval runs *before the answer exists*,
so the trusted verifier-acceptance signal cannot be observed at that seam. It
was **not faked**. The consequence is asserted as a test: recall-only heat can
never be promoted into the prompt. Heat accumulates and changes nothing until a
verification-path wire lands.

**Wave-1 A6 self-correction.** Phase 4 disproved my own Wave-1 claim that the
compaction record was flag-independent. It was gated behind
`OLYMPUS_CTX_BUDGET` in practice (A-D3). The claim was wrong; the fix and this
sentence are the correction.

**The D-D1 fix reduces depth scaling by 15.8×; it does not eliminate it.**
An earlier version of this report said the path was "flat in depth". That was
wrong, and the measurement that disproved it is recorded rather than the claim
quietly softened. Medians of syncs taken *at* depth, both arms on one machine:

| depth | cached | uncached |
|---|---|---|
| 100 | 1.68 ms | 4.36 ms |
| 500 | 1.73 ms | 14.56 ms |
| 1500 | 2.53 ms | 38.31 ms |
| 3000 | 6.61 ms | 82.20 ms |

The dominant O(journal bytes) parse-and-sha256 term is gone. An O(history
length) term remains and is **inherent to the contract**: `sync` must compare
the caller's history against the replayed prefix to tell an extension from a
rewrite, and `_cache_put` takes a defensive copy so a caller mutating a message
in place cannot silently de-sync the cache. The term is invisible below ~1000
turns. The journal-append SLO in §6 is therefore **depth-qualified to ~3000
turns** and must be re-baselined beyond that.

The original test for this asserted flatness by fitting a slope across the
deciles of one growing run. At 150 turns that fit was noise-dominated, and it
**flaked in a full-suite run** — caught by the Phase-5 Step-0 baseline, which is
what a baseline gate is for. It has been replaced by a direct two-depth
measurement of the coefficient.

**Journal `append_turn` still rescans.** The D-D1 fix covers `sync`, the live
per-turn path. `append_turn` retains the full rescan per call — it has no
production caller, so it is not on any hot path, but the harness reports it
honestly rather than quietly excluding it.

---

## 6. Service objectives — every one a PROPOSAL

**No SLO below is measured. Every one needs shadow traffic to size.** They are
recorded so Phase 5 has a hypothesis to test, not so they can be cited as
achieved.

| Objective | Draft target | Status |
|---|---|---|
| Availability | 99.0% / month | **PROPOSAL** |
| Latency p95 (chat) | ≤ 30 s | **PROPOSAL** |
| Verified-answer rate | 95% | **PROPOSAL** |
| Admission refusal rate | ≤ 1% (refusal is a **safe** outcome — budgeted, not an error) | **PROPOSAL** |

Measured offline, and therefore real but environment-bound:

| Metric | Measured | Note |
|---|---|---|
| `sessionlog.sync` p50 / p99 (live per-turn path) | 1.28 ms / 2.96 ms | at shallow depth; **depth-qualified** — see §5 |
| `sessionlog.sync` depth-scaling coefficient | 1.70 µs/turn (was 26.84) | 15.8× reduction; 6.6 ms/turn at 3000 turns |
| Journal recovery | 16–17 µs/record | linear, as designed |
| Observability overhead | +1.5 to +2.1 ms/run | <0.1% of a real model call |
| `ctxbudget.plan` | 13.9 µs/call | flag default off |
| `streamguard.feed` | 20.6 µs/delta ON, 0.03 µs OFF | default off |
| Max concurrent slots | 5 refused / 6 held | matches `MAX_CONCURRENT_CALLS` |

Everything requiring a real model call — true latency, TTFT, cost per task,
answer quality — is in the **UNMEASURABLE-OFFLINE** register with a reason and
a method, and is **never given a number**.

---

## 7. Negative results preserved, capabilities quarantined

The programme requires that negative results survive and that failed
capabilities be quarantined rather than forced into production. **21 entries**
are in `olympus/experiments.json`, enforced by a CI gate:

- **8 `accepted_debt`** — measured, refuted or bounded, each with an
  activation condition and a deactivation trigger. Includes the refuted
  estimator claim, the replay secret-screen residual (base64 / split-field
  secrets), seal key-rotation, suffix rollback, the modelgate missing-domain
  limitation, and the parallel-dispatch non-interference ordering limitation.
- **8 `proposed`** — built or specified, not activated, awaiting evidence.
  All four deferred Wave-3 candidates sit here.
- **5 `active`** — shipped behind flags with live evidence.

**No entry was deleted, downgraded, or rounded up to clear this report.**

Five documented residuals are pinned by tests named `*_DOCUMENTED_RESIDUAL`,
each asserting the residual is *still only that* — they are regression nets
against silent widening, not re-litigation of accepted debt.

---

## 8. Conditions on the GO, and what must happen next

### Blocking — must be discharged before a canary is considered

1. **Run Phase 5 (staging + shadow traffic).** Nothing below can be evaluated
   without it. Shadow traffic must reach **n ≥ 200 per task cell**
   (`task_class|language|context_band|tools|structured`) and be reported
   distributionally — mean, median, p90, p95, p99, max, n, CI — by provider,
   model, language and task class. A single aggregate number does not
   discharge this.
2. **Drive the Anthropic surface with a real third-party client** over a real
   socket. Until then the compatibility claim stays bounded (G2).
3. **Re-verify cross-principal isolation on a live multi-key deployment.** The
   B-F2 derivation is proven offline; the *deployment* is not (G3).
4. **Size the error budgets from observed traffic** and replace every PROPOSAL
   in §6 with a measurement (G4).

### Migration notes an operator must action

- **B-F2 changes namespaces.** Existing `api-v1` memory is **not** rewritten or
  deleted. Keyed deployments start from an empty per-key namespace — that is
  the point; the shared pool was the defect. Copying old material under a
  chosen key is a deliberate operator act.
- **B-F4 changes one configuration's behaviour.** A server bound off-loopback
  with **neither** `OLYMPUS_ACCESS_TOKEN` **nor** `OLYMPUS_REQUIRE_LOGIN` now
  **refuses** the browser API instead of serving it anonymously. The 401 body
  names which to set. Documented in `README.md` and `.env.example`.

### Rollback

Every absorption capability is flag-gated and default-off or default-additive;
the flag is the rollback. The two Phase-4 changes that are *not* flag-gated:

- The D-D1 journal cache is per-process and validated per call — disabling it
  is a one-line revert to the unconditional `_scan`, with no on-disk change
  (a `cache=False` arm exists in the harness and is exercised by CI).
- The B-F4 posture is reverted by setting either credential, which is also the
  correct forward fix.

### Known-open, non-blocking

- **Conversation snapshots have no retention bound.** Pre-existing, predates
  the absorption programme, recorded in `PRIVACY_RETENTION_REVIEW.md` §5.
  Journals share the snapshot's lifetime and *do* have deletion
  (`delete_session`, tombstone + `compact`).
- **`sessionlog.compact()` has zero callers in `olympus/`.** D-D1 made journal
  *depth* cheap; it did not make the journal self-trimming. Size remains
  bounded only by the 64 MB cap, past which the journal refuses rather than
  degrading.
- **Wave-2 gap A3** (§5) — heat accumulates, promotes nothing.

### Automatic rollback triggers for the eventual canary

Pre-committed here so they are not negotiated under incident pressure. Any one
fires an immediate rollback:

| Trigger | Threshold |
|---|---|
| Verified-answer rate | below the shadow-measured baseline by >2 pp |
| Error rate | above the shadow-measured baseline by >1 pp |
| Latency p95 | >1.5× the shadow-measured baseline |
| Spend per task | >1.25× the shadow-measured baseline |
| Any untyped exception reaching a user surface | 1 occurrence |
| Journal quarantine events | 1 occurrence |
| Cross-principal isolation violation | 1 occurrence |
| Admission refusal rate | >5% sustained over 15 min |

Every threshold is expressed against a **shadow-measured baseline**, which does
not yet exist. That is G1 restated, and it is why the canary cannot start
first.

---

## 9. The decision, restated with its evidence

> **CONDITIONAL GO.**
>
> **Evidence for:** Waves 1–3 complete as scoped, independently audited, with
> four candidates deferred on measured evidence rather than shipped
> speculatively. 5002 tests pass with zero failures across six CI gates. All
> 17 Phase-4 blocker categories that are testable offline were explicitly
> tested. Twelve defects found; ten fixed with regression tests written against
> the fixed behaviour, two accepted with published measurements and an
> operating bound. Two HIGH-severity defects — cross-principal memory leakage
> and a disk fault causing quadruple billing — were found and closed. Negative
> results are preserved in an enforced registry; no floor was lowered and no
> claim was rounded up.
>
> **Evidence against a higher decision:** three blocker categories —
> deployment, real-client compatibility, and live traffic behaviour — have **no
> test behind them**, because this environment provides no deployment target,
> no credentials and no traffic. Four of five Wave-3 gates require operational
> data that only shadow traffic produces. Every service objective is an
> unmeasured PROPOSAL. A canary approval on this evidence would be using real
> users as the test suite.
>
> **Next gate:** Phase 5 (staging + shadow traffic), G1–G4. Re-review Wave 3
> and re-issue this decision once shadow baselines exist.

---

*Cross-references: `00-SYNTHESIS.md` (rulings R1–R11, budgets B1–B4) ·
`WAVE1_COMPLETION_REPORT.md` · `WAVE1_INDEPENDENT_AUDIT.md` ·
`WAVE2_COMPLETION_REPORT.md` · `WAVE3_EVIDENCE_REVIEW.md` ·
`WAVE3_COMPLETION_REPORT.md` · `PRIVACY_RETENTION_REVIEW.md` ·
`olympus/experiments.json` · `scripts/perf_validation.py` ·
`tests/test_val_{integration,security,reliability,performance,privacy,phase4_fixes}.py`*
