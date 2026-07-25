# Calibration Record — Production Trial (v1)

**Status:** trial **INSTRUMENTED and ARMED**; live checkpoints **PENDING real
accumulation.** Observation-only throughout.

> **Scientific-integrity boundary (read first).** A production trial's verdict
> requires *real* runs, *real* model calls, and *real* human approve/reject
> feedback accumulating over calendar time on a live instance. That cannot be
> synthesised in a build session, and fabricating it would violate this trial's
> own closing rule — *"do not claim a moat because data accumulated."* So this
> document delivers everything that is real now — the pre-merge review, the
> missing instrumentation, the trial configuration, and a validated checkpoint
> harness — and marks the 100/250/500 checkpoints **PENDING**, to be filled by
> the operator from live data. No checkpoint metric in this file is invented; the
> only numbers shown are from a clearly-labelled **synthetic harness-validation**
> run that proves the reporting machinery, not the hypothesis.

---

## Step 1 — Pre-merge review of PR #210 (merged as `47bee2f`)

Reviewed the actual merged diff. Findings:

| Check | Verdict |
|---|---|
| Collection disabled by default | ✅ `enabled()` returns False unless `OLYMPUS_CALIBRATION` ∈ {1,true,yes,on}; `test_default_is_off` |
| No raw prompts/outputs stored | ✅ only `sha256:` refs; `test_prompt_text_is_never_stored`, `test_domain_never_inferred_from_task_text`; export leak-checked |
| No feedback event alters execution | ✅ `record_feedback` writes to the JSONL only; guard tests pin no decision module reads it, orchestrator only writes |
| Process locking preserves one valid chain | ✅ `proclock` around the RMW; `test_concurrent_appends_preserve_the_chain` (40-way, monotone unique seqs) |
| Lock timeouts bounded | ✅ `lock_timeout()` default 5 s, `proclock` raises `TimeoutError` on expiry |
| Incomplete trailing writes recoverable | ✅ `V_INCOMPLETE_TAIL`; `test_incomplete_trailing_write_is_recoverable_not_corruption` |
| Middle-chain corruption never silently repaired | ✅ `V_CORRUPTED_CHAIN`, `verify()` reports and does not rewrite; `test_middle_corruption_is_flagged_and_not_repaired` |
| Unsigned records distinguishable | ✅ `V_UNSIGNED_VALID`, `verify()` returns `signed`/`unsigned` counts |
| Schema upgrades preserve readability | ✅ `_read_raw` accepts any family version, `migrate_entry` in-memory only; `test_older_schema_entry_still_readable` |
| Capability manifest + README markers correct | ✅ regenerated 130→131 for the CLI command; `test_capabilities` green |
| No unrelated files | ✅ diff is calibration + orchestrator wiring + tests + docs only |
| Guard tests meaningful, not text-matching | ✅ they assert *behaviour* (no decision import; orchestrator writes but never reads back; disabled = zero writes), not string presence |

### The drop-on-lock-timeout decision (flagged for particular attention)

Dropping a row on lock timeout is **correct** — blocking a real user run on a
wedged peer is worse than losing one telemetry row — but the Phase 2 code
*counted* it only via `errors.capture` (a log line), with **no attempted-vs-
persisted counter and no durable failure record.** That is the gap the trial
protocol names. **Fixed before arming the trial** (this PR):

- **In-process counters** (`_STATS`): attempted / persisted / duplicate /
  dropped_timeout / dropped_error, surfaced by `counters()` and `health()`.
- **Durable failure log** (`calibration_failures.jsonl`): every drop is appended
  with a timestamp, so failures survive restart and feed the <1 % gate and the
  selection-bias scan. Append is best-effort and lock-free — under-counting on its
  own failure is fail-safe (it can never manufacture a false pass).
- **`failure_rate`** uses the **durable** numbers (`persisted_records` from the
  record + `durable_failures` from the log), so it is meaningful across restarts.

A silent-loss tripwire is now in the gate: `verify().entries` must equal
`counters().persisted_records`, else the trial pauses. **No general queue or event
bus was added** — just counting, per the protocol's explicit limit.

---

## Step 2 — Trial configuration

Captured by `calibration_trial.trial_config(owner)` and emitted at activation.
Reversible via `OLYMPUS_CALIBRATION=0` — **no code rollback**.

| Field | Source |
|---|---|
| activation timestamp | `activated_at` |
| software commit | `commit` (read from `.git`) |
| schema version | `olympus-calibration/1` |
| taxonomy version | `DOMAIN_TAXONOMY_VERSION` |
| enabled providers/models | `providers_models` (from the pool; **never keys**) |
| collection directory | `collection_dir` / `record_path` |
| retention setting | `retention_days` |
| export setting | `export_allowed` |
| signing available | `signing_available` (`witness.available()`) |
| process topology | `process_topology` (single vs heartbeat+web) |
| trial owner | `trial_owner` (`OLYMPUS_TRIAL_OWNER`) |

**To arm the trial on one instance:**
```
export OLYMPUS_CALIBRATION=1
export OLYMPUS_TRIAL_OWNER="you@org"
olympus calibration status        # confirm ON + config
olympus calibration trial         # checkpoint progress (safe: no content)
```

---

## Step 3 — Checkpoints (PENDING live accumulation)

Each checkpoint is produced by the harness and is **empty until real runs exist**.
The always-on **gate** pauses the trial on: middle-chain corruption; persisted ≠
verified (silent loss); recording-failure > 1 %; any behavioural effect; sensitive
plaintext; or a non-deterministic chain state.

- **A / 100 runs — instrumentation quality.** `checkpoint_a()`: attempts,
  persisted, feedback events, failure count/rate, lock timeouts, chain integrity,
  incomplete/corrupt, signed/unsigned, domain coverage (routed), unclassified
  rate, feedback coverage, records by provider·model·domain·evidence-level, and
  median/p95 recording overhead. **No ranking at A.**
- **B / 250 runs — evidence quality + bias.** `checkpoint_b()`: approval /
  rejection / edit / retry / verified rates (kept separate), missing-feedback and
  unclassified rates, comparable cells, model-version fragmentation, % records
  useful for comparison, approval↔verified agreement, and a **bias scan**
  (unclassified-by-provider, no-feedback-by-domain/specialist, failures-by-hour)
  to detect selection/survivorship bias.
- **C / 500 runs — strategic hypothesis.** `checkpoint_c()`: per-cell completion /
  approval / rejection / edit / retry / verified / blind-win rates with Wilson
  intervals, config version, evidence time span, and per-domain rankings — which
  **still refuse** on thin / incomparable / mixed-version / cross-domain /
  overlapping evidence.

### Harness validation (SYNTHETIC — not trial data)

A 120-row synthetic seed (2 providers × 2 domains, 20 % feedback) exercises the
full pipeline. Representative machine-readable output:
`docs/examples/calibration-trial-checkpointA.json` (carries a `_DISCLAIMER`).
It confirms the machinery: chain `valid`, failure_rate `0.0`, routed-domain
coverage `1.0`, feedback coverage `0.2`, overhead median ≈ 2 ms / p95 ≈ 3 ms, no
plaintext in the export. **These numbers validate the reporting code, not the
hypothesis.**

---

## Decision criteria (unchanged from the protocol; applied at C)

Return exactly one: **CONTINUE** · **REVISE** · **KILL AS MOAT, RETAIN AS AUDIT
FEATURE** · **KILL COMPLETELY**. CONTINUE requires *all*: chain valid; failure
< 1 %; no plaintext; routed-domain coverage ≥ 85 %; feedback coverage ≥ 30 %; ≥ 2
provider/model configs with ≥ 20 comparable observations in one domain; ≥ 1
operationally useful distinction; and that distinction could realistically change
provider choice, cost, reliability, or workflow.

**A moat claim additionally requires evidence that the record improves a decision
and that the advantage compounds — accumulation alone is not a moat.**

---

## Current recommendation

**Provisional: REVISE-leaning, pending data** — but the honest status is that no
decision-grade verdict can be returned yet, because zero live runs exist. What the
armed instrumentation already lets us predict as the likely binding constraints
(to watch first): **feedback coverage ≥ 30 %** (explicit 👍/👎 is rare — this is
the most probable REVISE trigger) and **≥ 2 configs × ≥ 20 obs in one domain**
(single-operator volume with one primary model may never reach it — the most
probable KILL-AS-MOAT trigger). The trial exists precisely to measure whether
those fears are real.

**Next permitted action:** arm the trial on one instance and run to Checkpoint A
(100 runs). Do **not** proceed to adaptive routing, trust, or any behavioural use
of this data unless the Checkpoint-C verdict is CONTINUE **and** a separate
implementation request explicitly authorises it.
