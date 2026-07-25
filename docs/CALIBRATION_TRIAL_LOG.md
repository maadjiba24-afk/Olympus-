# Calibration Record — Trial Activation Log (Checkpoint A attempt)

**Outcome:** trial **ACTIVATED and VERIFIED on real runs**; Checkpoint A
(100 real runs) **NOT REACHED** in this environment. No completion decision is
returned, because declaring Checkpoint A before 100 real runs would violate the
task's own hard constraint.

> **Why Checkpoint A cannot be reached here — stated plainly.** Checkpoint A is
> defined as *100 real completed runs accumulated through normal operation.* This
> is an ephemeral build sandbox with **no live users generating traffic.** The
> only ways to reach 100 here are (a) generate the runs myself — which the task
> forbids: *"do not make extra model calls solely to increase the sample count"*
> — or (b) fabricate them — forbidden twice over: *"do not insert synthetic,
> seeded, generated … records"* and *"do not claim the trial is complete before
> 100 real completed runs."* So I activated the trial, ran the full Step 2
> verification on **genuine** runs, and stopped at the honest boundary. The
> remaining 98 runs must accumulate on a real deployment over calendar time.

**Evidence classes in this log are kept separate, as required:**
- **REAL LIVE MEASUREMENTS** — the 2 activation runs below (real orchestrator,
  real model, real routing). Marked 🟢.
- **TEST RESULTS** — the unit/harness suites merged in #209–#211. Marked 🔵.
- **SYNTHETIC HARNESS VALIDATION** — the 120-row seed in #211
  (`docs/examples/calibration-trial-checkpointA.json`). Marked ⚪. **Never mixed
  into the live record** — the live directory was confirmed clean before
  activation.

---

## Step 1 — Preflight (real inspection)

| Item | Value |
|---|---|
| deployed commit | `1347dd7e29b1` |
| schema version | `olympus-calibration/1` |
| taxonomy version | `1` |
| enabled provider/model configs | `anthropic/claude-opus-4-8`, `anthropic/claude-sonnet-5` |
| signing available | **yes** (`witness.available()`) |
| process topology | single-process |
| collection directory | `/home/user/Olympus-/memory` |
| retention policy | `0` (keep all) |
| export permission | allowed |
| trial owner | `sandbox-activation` |
| system clock / TZ | `2026-07-25 22:24 UTC` / UTC |
| available disk | 31.8 GB free / 270.6 GB |
| chain status (pre-activation) | empty (no record file) |
| live-record count (pre-activation) | 0 |

**Confirmations:**
- 🟢 collection **disabled** before activation (`enabled() == False`).
- 🟢 live data dir **free of synthetic harness records** (`calibration.jsonl` and
  `calibration_failures.jsonl` both absent before activation; the #211 synthetic
  export lives only under `docs/examples/`).
- 🟢 no raw prompts/outputs stored (verified post-run: the question text does not
  appear in the record; only `task_hash`).
- 🟢 failure log stores only `{at, kind, reason≤200}` — no prompt/output/args, no
  exception payloads with sensitive content.
- 🟢 rollback via `OLYMPUS_CALIBRATION=0` works and needs **no code deployment**
  (verified: disabled → run still works → zero new records).

---

## Step 2 — Activation + verification (🟢 REAL RUNS)

**Activation timestamp:** `2026-07-25T22:26:12+00:00` (first real observation).

| Check | Result |
|---|---|
| 1. one ordinary real run | 🟢 `run 4ff0d61ed323` — domain `research` (specialist `argus`), `anthropic/claude-opus-4-8`, result `ok`, **signed** |
| 2. integrity check | 🟢 `verify() → ok, states=['valid'], signed 1 / unsigned 0` |
| 3. health inspection | 🟢 chain valid, persisted 1, failures 0, unclassified 0% |
| 4. restart test | 🟢 fresh process: run 1 present, chain still `valid` |
| 5. second real run after restart | 🟢 `run 32947be6b29a` — domain `research`, same config, appended cleanly |

**Post-activation verifications:**
- 🟢 both real runs present; chain `valid` after 2 runs across a restart.
- 🟢 no sensitive plaintext in the record (neither question string appears).
- 🟢 instance behaves identically with collection enabled (runs complete normally;
  a control run with collection **off** also completed normally and wrote nothing).
- 🟢 no routing/execution decision consumes Calibration Record data (pinned by the
  merged guard tests; the two live runs routed and answered exactly as they would
  with collection off).
- 🟢 verifier deterministic (two consecutive `verify()` calls returned identical
  states).
- 🟢 recording overhead on the live path: **7.6 ms** for the sole timed append
  (fsync under proclock; sits in `_finish`, after the reply is returned).

Activation **succeeded** — every Step 2 check passed on genuine runs. This is the
first end-to-end confirmation of the calibration↔orchestrator wiring against a
**real model** (all prior verification used stubs).

---

## Step 3 — Accumulation: **halted at the honest boundary**

Normal operation cannot produce 100 real runs in this sandbox (no live traffic),
and the hard constraints forbid manufacturing them. Accumulation therefore stops
at the 2 activation runs. Monitored signals at this point (all nominal):

| Monitor | Value |
|---|---|
| chain validity | 🟢 valid |
| persisted vs attempted | 2 / 2 (reconciles exactly) |
| recording failures | 0 |
| lock timeouts | 0 |
| incomplete trailing records | 0 |
| disk usage | 1,855 bytes for 2 runs (~0.9 KB/run) — no unbounded growth |
| feedback coverage | 0% (no human feedback — expected; none manufactured) |
| domain coverage (routed) | 100% (2/2 classified `research`) |
| plaintext leakage | none |

**No immediate-pause condition tripped.** The always-on gate is green
(`gate.ok == True`).

---

## Checkpoint A — **NOT REACHED** (2 / 100 real runs)

Per the definition (*"At 100 real runs, create a report…"*), Checkpoint A has not
occurred. The full checkpoint metric table below reports the **real** state at
n=2 — presented as an activation baseline, **not** as a Checkpoint-A result.

| Metric (🟢 real, n=2) | Value |
|---|---|
| activation timestamp | 2026-07-25T22:26:12Z |
| checkpoint timestamp | n/a (not reached) |
| elapsed calendar time | ~38 s across the 2 activation runs |
| deployed commit | `1347dd7e29b1` |
| attempted / persisted observations | 2 / 2 |
| feedback events | 0 |
| failure-log entries | 0 |
| recording failure rate | 0.00% |
| lock-timeout count / rate | 0 / 0% |
| chain integrity | valid |
| incomplete / corrupt records | 0 / 0 |
| signed / unsigned | 2 / 0 |
| domain coverage (routed) | 100% |
| direct-chat share | 0% (both runs routed to a specialist) |
| unclassified rate | 0% |
| feedback coverage | 0% |
| records by provider/model | `anthropic/claude-opus-4-8: 2` |
| records by domain | `research: 2` |
| records by specialist | `argus: 2` |
| records by evidence level | completion 2 · implicit 0 · explicit 0 · verified 0 |
| median / p95 recording overhead | 7.6 ms / 7.6 ms (n=1 timed) |
| disk usage | 1,855 bytes |
| plaintext-leak scan | clean |
| operational incidents | none |
| selection/survivorship bias | none observable at n=2 (both runs identical config) |

---

## Decision

The task's three Checkpoint-A options each presuppose 100 real runs. At n=2 none
can be honestly returned, so the faithful output is:

**CHECKPOINT A NOT REACHED — trial ACTIVATED, accumulation must continue on a
live deployment.**

On the evidence available (n=2), **every Checkpoint-A CONTINUE condition that is
measurable at this scale is already satisfied**: chain integrity valid; no
sensitive plaintext; recording failure 0% (< 1%); Olympus behaviour unchanged;
persisted and attempted counts reconcile; the verifier is deterministic. That is
a *provisional CONTINUE trajectory*, not a Checkpoint-A pass — the difference is
98 real runs I will not fabricate.

**Next permitted action (operator):** on one real deployment with real traffic,
set `OLYMPUS_CALIBRATION=1` persistently and let runs accumulate naturally to 100,
then run `olympus calibration trial` / `calibration_trial.checkpoint_a()` for the
real Checkpoint-A report and decision. Do **not** implement routing, trust,
autonomy, or any adaptive behaviour on this data without a Checkpoint-C CONTINUE
verdict and a separate explicit request.

---

## Deployment attempt (Step 1) — HALTED: persistence requirement fails

A follow-up task asked to deploy on a real production instance and accumulate to
100 genuine runs. Step 1's deployment-preparation checks were run against this
environment and **the storage-persistence requirement fails**, which — by the
task's own Step 3 rule (*"If persistence fails, disable collection and report the
incident. Do not continue accumulating into ephemeral storage."*) — is a hard
stop. Collection was **left disabled**; nothing accumulated.

### Deployment-prep results

| Requirement | Result |
|---|---|
| deployed commit matches latest main | 🟢 `1347dd7` (functional code #209–#211 merged) |
| `OLYMPUS_CALIBRATION` disabled before activation | 🟢 off |
| calibration dir on **persistent** storage | 🔴 **FAILS** — `/home/user/Olympus-/memory` is on this session container's disk, which the environment reclaims on inactivity/session end |
| survives process restart | 🟢 yes (verified in #212) |
| survives **container replacement / host reboot** | 🔴 **FAILS** — session-scoped; the record does not survive reclamation |
| only the intended instance writes here | 🟢 yes (single sandbox) |
| filesystem permissions restrict access | 🟡 `drwxr-xr-x root:root` — adequate for single-tenant, not hardened multi-tenant |
| disk-space monitoring exists | 🔴 none configured |
| backup/snapshot coverage exists | 🔴 **none found** |
| backup does not modify the live chain | n/a (no backup) |
| failure log stored persistently | 🔴 same ephemeral fs |
| synthetic harness records absent from live dir | 🟢 absent (only under `docs/examples/`) |
| no raw prompts/outputs/credentials present | 🟢 verified — only hashes/metadata |
| chain verifies before activation | 🟢 valid |

- **Storage path:** `/home/user/Olympus-/memory` (`/dev/vda`, ext4, session container)
- **Ownership / permissions:** `root:root`, `0755`
- **Backup policy:** none
- **Restore procedure:** none (ephemeral)
- **Production traffic source:** none — this is a build sandbox, not a deployment
  serving real users.

### Incident + decision

**INCIDENT: the only available environment is ephemeral and has no production
traffic.** Accumulating a 100-run, persistence-and-integrity-dependent trial here
would (a) lose the chain on the next container reclamation and (b) require
manufacturing the runs, which the hard rules forbid. Per Step 3, collection is
**not activated for accumulation**; it stays disabled.

**Checkpoint A is NOT attempted** (it requires 100 genuine runs on persistent
storage; neither precondition is met here).

### Operator runbook — how to actually run this to Checkpoint A

On a **real deployment** with ordinary production usage:

1. **Provision persistent storage** for `MEMORY_DIR` (a mounted volume that
   survives restart, redeploy, container replacement, and reboot). Point Olympus
   at it. Confirm `df` shows the volume and that a file written there survives a
   redeploy.
2. **Harden access:** restrict the calibration dir to the Olympus service user;
   ensure only that one instance writes to it.
3. **Add disk monitoring + backup** of `calibration.jsonl` and
   `calibration_failures.jsonl`. The backup must be **copy-only** (e.g.
   `cp`/snapshot) — it must never rewrite or reorder lines, or it breaks the hash
   chain. Restore = copy the file back; `olympus calibration health` then
   re-verifies it.
4. **Persistently set** `OLYMPUS_CALIBRATION=1` (and `OLYMPUS_TRIAL_OWNER`) via the
   deployment's config mechanism — not a shell session. Redeploy and confirm
   `olympus calibration status` shows ON afterward.
5. **Verify** (Step 3): one genuine task → exactly one observation, chain valid,
   counters reconcile, no leak; redeploy; second genuine task → prior evidence
   present, chain extended, still enabled, verifier deterministic.
6. **Accumulate naturally** to 100 genuine runs — no manufactured tasks, no
   fabricated feedback. Periodically run `olympus calibration trial` (no model
   calls) for the lightweight health checks; pause on any incident condition.
7. **At 100 genuine runs**, run `calibration_trial.checkpoint_a()` for the real
   report and return the CONTINUE/PAUSE/TERMINATE decision.

**No routing, trust, autonomy, or ranking use of the data at any point without a
Checkpoint-C CONTINUE verdict and a separate explicit request.**
