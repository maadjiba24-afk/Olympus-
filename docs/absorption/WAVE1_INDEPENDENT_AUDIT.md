# Wave 1 — Independent Adversarial Audit

**Purpose.** Phase-1 gate before Wave 2: independently re-verify the Wave-1
claims (not re-run the authors' own tests), with fresh adversarial probes, honest
distributional statistics, and blockers fixed before proceeding. Per the
governing operating rule, "code exists + tests pass" is *not* completion.

**Method.** Four independent auditors, each writing new adversarial probe tests
against a distinct capability cluster, forbidden from patching source (auditor
reports; it does not fix). Findings were then triaged and every **blocker** and
**false claim** was fixed in a follow-up pass, with the audit tests flipped to
assert the corrected behavior.

**Audit test artifacts (new):**
`tests/test_audit_sessionlog.py` (37), `tests/test_audit_replay_noninterference.py`,
`tests/test_audit_ctxbudget_usage.py` (39), `tests/test_audit_modelgate_toolcall.py` (39).

**Suite after audit + fixes:** `3999 passed, 26 skipped, 0 failures` (Wave-1 tip
was 3848; +151 adversarial tests). CI gates green: capabilities, threat-model
(130 tools), non-interference (exit 0), compileall, env-docs.

---

## 1. Verdict summary

| Capability | Areas probed | Verdict | Blockers found → status |
|---|---|---|---|
| C1 session journal | durability, hash-chain, compaction, key-rotation, schema-migration, concurrency, retention/tombstone, isolation | **CONFIRMED** | none |
| C2 replay fixtures | secret screening, determinism modes, tamper, path-escape | **CONFIRMED after fix** | 1 blocker (secret screen) → **FIXED** |
| C3 non-interference | hostile observe-plugin (incl. nested), gate robustness, concurrency-order | **CONFIRMED** | none (concurrency-order = documented limitation) |
| C4 context budget | estimator generalisation, I-C2 property, flag-off identity, poisoning, corrupt-file | **CONFIRMED; one claim re-declared** | I-C4 "0.1%" false → **RE-DECLARED**; OverflowError → **FIXED** |
| C5 cache telemetry | I-U1/U2/U3, verdicts, fingerprint, migration | **CONFIRMED** | none |
| C6 drift tripwire | severity ladder, reproduce-before-believe, **budget cap** | **CONFIRMED after fix** | 1 blocker (budget overrun) → **FIXED**; missing-domain → limitation |
| C7 predictability | read-only, no-prefetch, floors | **CONFIRMED** | none |
| C8 tool-call ladder | I-T1 execution precondition, coercion/truncation/salvage safety, telemetry | **CONFIRMED** | none |
| Config docs | every Wave-1 knob in .env.example | **CONFIRMED after fix** | `OLYMPUS_RUN_BUDGET_USD` undocumented → **FIXED** |

**Net: 2 blockers + 1 false claim found; all resolved. No critical Wave-1
invariant remains unverified → Wave 2 is unblocked.**

---

## 2. Blockers found and fixed

### B1 — Replay secret screen missed modern credential formats (C2, I-R2)
**Finding.** The v1 screen `sk-[A-Za-z0-9]{8,}` never engaged on a modern OpenAI
key `sk-proj-…` (only 4 alnum chars follow `sk-` before the dash), so a live
`sk-proj-` key planted in a frozen response **exported into a committable
fixture** with the manifest reporting a clean scrub. Also missed:
space-before-colon auth headers, spaced api-key values, and all other-provider
credentials (AWS/GitHub/Slack/Google).

**Fix (`olympus/replaystore.py`, screen `v2`).** Broadened `sk-[A-Za-z0-9-]{8,}`
(covers proj/svcacct/admin), whitespace-tolerant `authorization\s*:\s*bearer`
and `api[_-]?key\s*[=:]\s*\S`, a contiguous keyed-secret pattern, and explicit
AWS/GitHub/Slack/Google shapes. Verified: all 7 audited vectors now **caught**;
export **refuses** the smuggled-key fixture (`FixtureSecretsError`).
**Documented residual:** base64-encoded and split-across-fields secrets still slip
(need entropy/structural detection — logged as accepted debt, tests retained as
`*_DOCUMENTED_RESIDUAL`).

### B2 — Drift-gate budget cap overrun by a cost spike (C6, I-M1)
**Finding (denial-of-wallet).** `_run_corpus` projected next-item cost as the
**max of past** per-item costs — blind to a spike. Costs `[0.1, 0.1, 5.0]` under
a $1.00 cap spent **$5.20** (the spike item ran because the trailing max was only
0.1); and "first item always runs" let a single item exceed the cap unboundedly.
I-M1 ("gate never exceeds its dollar cap") was **false**.

**Fix (`olympus/modelgate.py`).** Replaced the trailing-max projection with a
**pre-flight worst-case estimate**: an item is only started if
`total + worst_case(item) ≤ cap`. `_worst_case_cost` prices `input +
_DRIFT_MAX_OUTPUT (1024) output × _CALLS_PER_ITEM (3)` with the same `usage.PRICES`
table — a true upper bound for the short-answer drift corpus, realistic enough to
let a full 14-item corpus complete under $1 (the naive `MAX_TOKENS`=16k bound
would block half the corpus). The estimator is injectable (`cost_estimator`) so
tests assert never-exceed with perfect foresight. Verified: `[0.1,0.1,5.0]` under
$1 now stops before the spike (2 items, $0.20); a single over-cap item runs 0
items, $0.
**Documented residual (honest I-M1 restatement).** The cap is a true never-exceed
bound *provided per-item output stays within `_DRIFT_MAX_OUTPUT`*; a provider
returning more than that bound could overrun by a single item's excess — bounded
and documented, versus the old unbounded overrun.

---

## 3. False claim re-declared (C4, I-C4)

**Finding.** The shipped "estimator error 29.4% → 0.1%" headline is **circular**:
the benchmark calibrated and measured against the *same* injected chars-per-token
constant, so error collapsed to rounding. It measured memorisation, not accuracy.

**Honest measurement** (train n=200 → held-out n=200 per class, per-sample cpt
drawn from a realistic within-class distribution; bootstrap 95% CI, B=2000):

```
HELD-OUT calibrated-estimator ABSOLUTE RELATIVE ERROR by content class
class     tgt_cpt cal_cpt |  mean          95%CI     med    p90    p95    p99    max | flat//4 mean
--------------------------------------------------------------------------------------------------
english      4.10    4.03 |  6.9% [6.2,7.6]        5.9%  13.7%  15.7%  21.2%  24.0% |  7.2%
code         3.10    3.08 | 15.7% [14.2,17.4]     13.6%  30.7%  37.9%  46.6%  60.9% | 23.7%
cjk          1.70    1.71 | 11.5% [10.2,12.9]      9.2%  25.4%  30.6%  41.3%  41.4% | 58.0%
cyrillic     2.60    2.59 |  9.0% [8.1,10.0]       8.0%  19.1%  23.4%  29.6%  36.2% | 34.8%
json         3.40    3.21 | 12.7% [11.4,14.0]     10.7%  25.0%  31.2%  36.8%  50.3% | 15.5%
mixed        2.90    2.74 | 43.9% [42.6,45.2]     43.2%  55.7%  61.2%  65.9%  72.6% | 29.2%
```

**Honest read.**
- The 0.1% claim does not survive genuine variance: held-out **mean 7–16%** for
  unimodal classes; **every class's p90+ tail exceeds the declared ±15%**.
- Calibration's real, legitimate win is capturing the **class mean** — it beats
  naive `chars//4` decisively on CJK (11.5% vs 58%), Cyrillic (9% vs 35%), code
  (15.7% vs 23.7%); on English (near the 4.0 prior) it's a wash.
- **Structural limit:** a single scalar cpt per (provider,model) cannot serve
  heterogeneous content — the bimodal **"mixed" class is 43.9%, worse than naive
  `chars//4` (29.2%)**.

**Disposition.** I-C4 / acceptance-gate A7 are **re-declared distributionally**:
the honest bound is "calibrated mean beats naive `chars//4` on non-English
unimodal content; ±15% is a *mean* target for unimodal cells, not a tail
guarantee; mixed/bimodal content is a known failure mode." **C4 remains
default-off** (`OLYMPUS_CTX_BUDGET`; I-C1 flag-off byte-identity independently
re-confirmed against 1000 histories even with a poisoned calibration file on
disk), so no production path relies on the calibrated estimator yet. Turning C4
*on* to drive refusals is gated (Wave 2) on either a per-content-class estimator
or scoping to unimodal cells. The circular shipped benchmark should be relabelled
as a "calibration-converges" test, not an accuracy claim.

---

## 4. Confirmed invariants (highlights of the adversarial evidence)

- **C1 durability**: fsync **syscall counts** measured — `always` = per-record,
  `auto` = once/turn-end; a committed record survives reopen at the byte level.
- **C1 hash-chain**: a record with a **valid own seal but broken `prev`** is
  caught at the exact boundary and quarantined (isolates prev-link from seal).
- **C1 concurrency**: with `proclock` monkeypatched to a no-op, concurrent
  appends corrupt the journal (`seqs=[1,1,1,1]`, quarantined) — the lock's
  necessity demonstrated, not assumed.
- **C1 retention**: a **middle-range** tombstone's bytes are physically gone from
  the raw file only after compaction *through* it; no quarantine copy retains the
  secret (GDPR-style deletion works).
- **C1 isolation**: 9 hostile conversation ids (`../../etc/passwd`, embedded NUL,
  backslashes, URL-encoded) all collapse to a direct child of `sessions/`.
- **C3 non-interference**: a hostile observe-only plugin mutating `pre_llm_call`
  params — including **nested list mutation** — cannot reach the caller (deepcopy,
  not shallow); the gate exits 1 on an injected decision diff and 2 on a trivial
  (<3 decision-type) fixture.
- **C4 I-C2**: 2000-iter seeded sweep — `plan()` is "fit" **iff**
  `total+overhead+reserve ≤ window`; bool blocks raise rather than summing as 1.
- **C4 poisoning**: out-of-bound cpts, NaN/inf/str/None/bool all discarded;
  flood-bounded EMA stays within `[CPT_MIN, CPT_MAX]`; per-key isolation holds.
- **C5**: `in + cache_read + cache_creation == provider total` across 8 usage
  shapes; positional `record()` byte-identical; fingerprint stable/sensitive; no
  TTL constants; old (pre-cache) day files load and accept new records.
- **C6**: every severity boundary (info/warn_pending/warn/freeze/block/quarantine)
  verified incl. the inclusive tolerance edge and quarantine short-circuit;
  reproduce-before-believe holds (an unconfirmed single-domain regression never
  hardens to warn).
- **C7**: `predictability_report()` writes **nothing** (fs-snapshot before/after
  identical); **no `OLYMPUS_PREFETCH` consumer exists anywhere** in `olympus/`;
  floors abstain below n and require n≥200.
- **C8**: no repaired/coerced call reaches a handler without passing rung-2
  validation against the authoritative `input_schema`; a JSON `true` coerced for
  an int param stays rejected (coercion cannot launder a semantically wrong
  value); unknown tool names always refused; salvage cannot map onto the wrong
  param.

---

## 5. Accepted debt & documented limitations (carried forward, not blocking)

| Item | Class | Rationale |
|---|---|---|
| Seal key rotation / signing absent (C1) | accepted-debt | spec §C1.15; SHA-256 integrity only. `witness.sign_log` is detached → layers on in Wave 2 with no journal-format change. |
| Committed-suffix truncation (rollback) undetectable in-file (C1) | limitation | spec §C1.8/§C1.15; needs external anchoring (Wave 2 witness signing). |
| `safe_id` intentionally lossy (C1) | limitation | it is a sanitiser, not a unique key; isolation holds. |
| base64 / split-across-fields secrets slip the screen (C2) | limitation | needs entropy/structural detection; retained as `*_DOCUMENTED_RESIDUAL` tests. |
| Non-interference under **parallel dispatch** not fully exercised (C3) | limitation | the committed mini fixture is single-threaded; `canonicalize_parallel_since` covers ordering but a multi-worker replay fixture is not yet built (Wave-2 integration testing). |
| I-C4 tail error / mixed-content worse-than-naive (C4) | re-declared | scalar cpt structural limit; C4 default-off; §3 above. |
| `observe()` huge-int overflow (C4) | fixed | now discard-and-count; was unreachable with real data. |
| `classify()` ignores fully-vanished (`missing`) domains (C6) | limitation | narrow — a vanished domain usually pushes error_rate over quarantine first; noted for Wave-2 severity refinement. |
| I-M1 bounded-by-`_DRIFT_MAX_OUTPUT` residual (C6) | documented | §2 B2; true never-exceed under the short-answer output bound. |
| `OLYMPUS_TOOL_VALIDATE=off` legacy path less safe (C8) | accepted-debt | intended one-release escape hatch. |

---

## 6. Gate decision

**PASS — Wave 2 approved to start.** Both blockers (secret screen, drift-budget
overrun) are fixed with adversarial tests flipped to assert the corrected
behavior; the one false claim (I-C4) is honestly re-declared and its capability
remains default-off so no production path depends on it; all remaining items are
documented limitations or accepted debt with named Wave-2 owners. Full suite and
all CI gates are green. No critical Wave-1 invariant remains unverified.

*Fixes in this pass:* `olympus/replaystore.py` (screen v2), `olympus/modelgate.py`
(pre-flight budget estimate), `olympus/ctxbudget.py` (overflow guard),
`.env.example` (`OLYMPUS_RUN_BUDGET_USD`), plus the four audit test files and the
two author tests updated to the corrected contracts.
