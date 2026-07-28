# Native Models under Controlled Self-Evolution

**How the native forecasting system improves, what it may do by itself, and
what it is structurally prevented from doing.**

- **Last updated:** 2026-07-28
- **Modules:** `native/{evidence,challengers,isolation,promotion,improvement}.py`
- **Tests:** `tests/test_trading_native_evolution.py` (81),
  `tests/test_trading_native_isolation.py` (49)
- **Demonstration:** `python scripts/native_evolution_demo.py`
- **Companions:** `docs/SELF_EVOLUTION.md` (the general framework this plugs
  into), `docs/OLYMPUS_NATIVE_CAPABILITIES.md`,
  `docs/OLYMPUS_NATIVE_MODEL_STATUS.md` (the ledger)

> **Nothing here has learned anything about a real market.** Every forecast in
> every journal below was made on series this repository generates, and no
> experiment has been run against real data, a real broker or real money. What
> is demonstrated is that the loop records, detects, proposes, isolates,
> evaluates, rejects, restricts and restores correctly — not that any model got
> better.

---

## 1. The shape

```
   ┌──────────────────── autonomous ────────────────────┐   ┌─ human only ─┐

  forecasts ─► evidence ─► weakness ─► challenger ─► isolated ─► gate 1..10 ─┐
  (serve.py)  (14 fields) (10 kinds)  (11 fields)    worker                  │
                   │                                    │                    │
                   └──────────► improvement ◄───────────┘        gate 11..12 ┘
                                (12 metrics)                   review, restricted
                                                                  promotion
        drift ─► restrict ─► rollback ──► previously approved version
```

Everything left of the divide happens without asking. Everything crossing it
needs a named operator carrying a token, and the check is
`governance.authorise`, which raises rather than returning `False` — an
autonomous caller that receives a value can ignore it.

This is the same asymmetry `docs/SELF_EVOLUTION.md` §3 already argues for:
**stopping is autonomous, starting is not.** Phase 4 adds nothing to that
argument; it connects the native models to the machinery that enforces it.

---

## 2. The learning loop — `native/evidence.py`

Every matured forecast leaves a `ForecastEvidence` carrying all fourteen fields
Phase 4 names, plus the provenance needed to re-examine it: dataset version,
feature schema, model version, checkpoint hash, prediction, quantiles,
uncertainty, dispersion, abstention decision and reasons, realised outcome,
error, coverage, regime, instrument, timeframe, strategy usage, modelled and
realised costs, and the trade outcome where there was one.

Four decisions shape it, and each closes a way the loop could quietly lie.

### An abstention is evidence, not an absence of it

A declined forecast produces a full record — the reasons, and the realised move
that followed. Without that, only *insufficient* abstention is measurable, and
a model that declined every window would have an unblemished error record and
would look like the best model in the system.

`test_unnecessary_abstention_is_measurable_only_because_declines_are_kept`
constructs exactly that case.

### Error is `None` when nothing was predicted, never zero

The same rule `NativeForecast.value()` follows. A zero error on a declined
forecast is a claim of perfect accuracy, and averaging it in is how declining
becomes a strategy for winning benchmarks.

### Maturity is a fact about the clock

`PendingForecast.mature()` refuses before `matures_at`, and there is **no
`force`** — `test_there_is_no_argument_that_forces_an_early_maturation`
inspects the signature. A keyword that permitted it would eventually be passed
by a backfill script.

### A single forecast has no calibration error

Coverage is a property of a *set*. One observation is inside its interval or
outside it, and 0% or 100% is not a calibration measurement. So the record
carries `interval_covered` and `calibration_error()` is computed over a
collection with its sample size attached. This is narrower than the field list
Phase 4 gives, and it is narrower because the per-record version would be a
number with no meaning that people would average.

### The ten weaknesses

| Kind | How it is found |
|---|---|
| `model_deterioration` | recent half's MAE against the earlier half's, same model version |
| `regime_weakness` | a regime's MAE against the whole set's |
| `poor_calibration` | realised minus nominal coverage, in points |
| `excess_confidence` | coverage in the narrowest interval tercile against the widest |
| `unnecessary_abstention` | declines followed by a move at or below the median |
| `insufficient_abstention` | answered rows exceeding 3× the median error, carrying no reason |
| `instrument_failure` | an instrument's MAE against the whole set's |
| `timeframe_failure` | a timeframe's MAE against the whole set's |
| `data_quality_failure` | MAE on degraded inputs against MAE on clean ones |
| `execution_model_error` | realised cost above modelled, and wanted trades that did not fill |

Every finding carries its measurement, the threshold it crossed, its sample
size and the forecast ids behind it. A finding below `MIN_SAMPLE` (30) is
marked **provisional** and reported rather than suppressed: "we saw this three
times" is worth recording and is not worth acting on, and those are different
next actions.

`test_a_healthy_journal_produces_no_actionable_weakness` is the counter-case. A
detector that fires on everything is not a detector.

---

## 3. Challenger generation — `native/challengers.py`

Ten kinds — representation, architecture, task head, feature, horizon, training
objective, calibration, regime specialist, data source, simpler model — and
eleven required fields, all checked at construction. A proposal missing one does
not exist as an object, which is the difference between a schema and a checklist
somebody fills in later.

Three of the eleven do work beyond documentation.

**Contradicting evidence may not be empty.** `NO_CONTRADICTION` is a sentinel a
proposer has to type, and typing it is a claim that can be wrong in review. A
field that defaulted to empty would be empty on every proposal. When the
originating finding was provisional, its small sample becomes the contradicting
evidence automatically — a thin sample *is* evidence against acting.

**The compute budget is enforced.** `ComputeBudget` converts directly into the
rlimits `isolation.py` applies. A proposal claiming 60 CPU-seconds gets 60
CPU-seconds and is killed at 61.

**The rollback plan must name a restore target** the deployment ledger can
resolve — not "revert the change".

### Simplification is never a second-class challenger

`generate()` pairs every complexity-adding challenger with a `SIMPLER_MODEL`
counterpart over the same weakness. `champion.compare()` already breaks a
statistical tie toward the simpler arm, which only helps if a simpler arm was
entered — and left to itself, a system generating proposals from failures
generates additions, because a failure looks like something missing.

Phase 1's benchmark is the argument: a 19-parameter AR(3) beat the native
network on a linear synthetic process.

`to_research_proposal()` converts a challenger into the framework's existing
`ResearchProposal`, so a native challenger enters the same backlog, the same
falsifiability checks and the same status lifecycle as everything else. The four
native-specific fields travel in `notes` rather than being dropped.

---

## 4. Research isolation — `native/isolation.py`

> A Python-level import boundary alone is not adequate isolation for generated
> code.

`lab.ResearchSandbox` enforces by *absence* — it holds no reference to
credentials or order submission, so `request("live_broker_credentials")` has
nothing to return. That is a real guarantee against an experiment that asks
politely. It is not a guarantee against generated code that imports what it
likes, opens a socket and posts an order, because all of that happens inside the
same interpreter with the same file descriptors and the same network.

So the worker is a **separate process**, in its own network namespace, under
rlimits, with a rebuilt environment, behind a seccomp filter, in an ephemeral
directory that is destroyed afterwards.

### Confinement is verified from inside, not asserted from outside

`preexec_fn` applies the confinement and cannot report what it managed to apply.
So the worker **probes its own confinement** as its first act: it tries to
create a socket, reads its network namespace inode and `/proc/self/net/dev`,
reads back its own rlimits, and tries to write to its input files. The parent
compares that report against what it asked for.

If the network namespace silently did not apply, the probe sees a working socket
and the run is rejected as `CONFINEMENT_FAILED`. The result is thrown away
rather than trusted. **Fail closed, on observation rather than intent.**

### What this host actually enforces

Reproduce with `python -c "from olympus.trading.native.isolation import
isolation_report; import json; print(json.dumps(isolation_report(), indent=2))"`.

| Mechanism | Applied | Established by |
|---|---|---|
| `separate_process` | yes | observed |
| `scrubbed_environment` | yes | observed |
| `ephemeral_workdir` | yes | observed |
| `network_namespace` | yes | observed |
| `seccomp_filter` | yes | observed |
| `no_new_privs` | yes | asserted |
| `blocked_imports` | yes | observed |
| `cpu_limit` | yes | observed |
| `memory_limit` | yes | observed |
| `file_size_limit` | yes | observed |
| `process_limit` | **no** | asserted |
| `wall_clock_timeout` | yes | asserted |
| `read_only_inputs` | yes | observed |
| `signed_inputs` | yes | observed |
| `signed_results` | yes | asserted |
| `worker_destruction` | yes | asserted |

### The four layers, in order of how much they are worth

1. **No network.** `unshare(CLONE_NEWNET)` leaves the worker with a loopback
   interface and nothing else. An order that cannot leave the machine is not an
   order, whatever code was generated.
2. **No credentials.** The environment is rebuilt from an **allowlist**, not
   filtered by a denylist — a new secret-bearing variable is excluded by default
   rather than by remembering to add it to a pattern list.
3. **Blocked syscalls.** A seccomp-BPF filter returns `EPERM` for `socket`,
   `connect`, `ptrace` and `execve`. Redundant with (1) on purpose.
4. **Blocked imports.** A meta-path hook refusing `execution`, `brokers`, `oms`,
   `modes`, `vault`, `killswitch` and `risk`. **This is the weakest layer and is
   listed last on purpose.** Generated code can defeat an import hook; it cannot
   defeat an empty network namespace.

`test_the_network_namespace_is_judged_without_the_seccomp_filter` exists because
the first version let (3) mask a failed (1): both stop a socket being created,
and sharing one observation would mean a silently failed `unshare` read as a
working namespace. Two mechanisms meant to fail independently must be judged
independently.

### Three things this got wrong first, and now does not

- **`chmod 0444` does not bind uid 0.** Root bypasses the DAC check, and the
  first version shipped a dataset the worker could overwrite while the manifest
  said read-only. The mechanism is now a **read-only bind mount** in a private
  mount namespace, enforced by the VFS regardless of uid. The mode bits remain
  as defence in depth.
- **The seccomp filter cannot be installed before `execve`.** It denies
  `execve`, so installing it in `preexec_fn` means the worker never starts. It
  is installed by the runner as its first act, after exec — which is also when
  it starts mattering.
- **A worker killed by its own CPU limit is not a confinement failure.** The
  first version reported it as one, because a killed worker writes no probe and
  a missing probe looks like missing confinement. `Verdict.LIMIT_EXCEEDED` is
  now checked before the confinement verdict.

### Signing, and who does it

The **parent** signs the input payload and the result manifest, because a worker
that could sign could sign a result it invented. The **worker** verifies the
input *digest*, so a payload altered between write and read is caught inside the
run. Integrity is checked in both directions; authenticity is only ever claimed
by the parent.

`ResultManifest.trustworthy` is computed from three facts — the verdict, the
confinement and the destruction — and none of them is settable.

### Known limitations, because they are real

- **`RLIMIT_NPROC` is not applied.** It counts processes per real UID across the
  whole system, not per process tree, so setting it to 8 in a container where
  the same UID already runs more would stop the worker starting at all. The
  wall clock and the process-group kill bound a runaway. `process_limit` reports
  `applied=False` with this reason rather than pretending.
- **`RLIMIT_FSIZE` caps the largest single file, not total disk.**
- **This is not a defence against a hostile human with a shell.** It is a
  defence against code Olympus generated, running unattended.

---

## 5. The twelve-stage gate — `native/promotion.py`

| # | Stage | Who | What it establishes |
|---|---|---|---|
| 1 | `static_validation` | olympus | parses, no forbidden import, no foreign checkpoint reference |
| 2 | `unit_tests` | olympus | its own tests pass, adversarial ones included |
| 3 | `leakage_tests` | olympus | an independent audit finds no future information |
| 4 | `historical_evaluation` | olympus | held-out period, same dataset, costs and metrics as the incumbent |
| 5 | `walk_forward_evaluation` | olympus | rolling refits, because one split rewards a lucky period |
| 6 | `baseline_comparison` | olympus | beats persistence and the statistical baselines after costs |
| 7 | `robustness_tests` | olympus | degrades explicitly under the thirteen adversarial conditions |
| 8 | `calibration_tests` | olympus | coverage within tolerance, by regime and not only overall |
| 9 | `paper_trading` | olympus | traded with no real money, so execution assumptions meet a book |
| 10 | `shadow_mode` | olympus | alongside the incumbent on live inputs, deciding nothing |
| 11 | `human_review` | **operator** | a named person has read the evidence and signed |
| 12 | `restricted_promotion` | **operator** | live on a named subset, under a cap, with an expiry |

`advance()` accepts only the next stage. Not because somebody would skip one
deliberately, but because a pipeline that permits skipping will skip under time
pressure and nobody will notice which stage was dropped.

**A missing check fails the stage**, with the reason recorded. The alternative
is that a missing check reads as a pass, which is how a gate becomes a
formality.

**Restricted promotion is a promotion with a shape.** `Restriction` carries
instruments, a maximum notional, an expiry, a review date and the criteria that
would justify widening it — all required. `ChallengerRun.active_at()` is
computed from the clock, so an approval that outlives its expiry is not a
deployment.

**Nothing here can hide a failure.** A rejection is terminal under the same
challenger id, `StageResult` has no mutator, and `concealment_check()` re-reads
the stored objects independently — so a ledger reconstructed from tampered data
is caught even though `ChallengerRun.__post_init__` would have refused to build
it in this process.

### What Olympus may and may not do

| Autonomous | Human-only |
|---|---|
| generate proposals | promote a model |
| run isolated experiments | deploy a model |
| reject weak challengers | enable live trading |
| restrict deteriorating models | change risk limits |
| demote models | expand permissions |
| engage safety shutdowns | clear a kill switch |
| roll back to a human-approved version | access broker credentials |
| | modify the safety kernel |

Prohibited for **every** actor, operator included: concealing a result and
rewriting audit history. An operator who genuinely needs to correct the record
does it out of band, leaving their own trace; what must not exist is an
in-system path that makes concealment routine.

---

## 6. Improvement metrics — `native/improvement.py`

Twelve metrics, each with a declared direction and a definition written out
because several have a tempting wrong one.

> Do not measure progress by code volume, number of proposals or number of
> capabilities alone.

Enforced rather than noted. `FORBIDDEN_PROGRESS_METRICS` names thirteen
quantities — lines of code, modules added, proposals generated, capabilities
added, experiments run, commits, tests added and the rest — and `measure()`
**raises** when one appears in its input. Not ignores: a metric silently dropped
is a metric somebody will keep computing and quoting.

Three are checked hardest because they are easiest to fake:

- **Abstention quality is not the abstention rate.** It is selectivity — the
  median realised move on declined windows over the median on answered ones. A
  model that declines everything has no answered rows to compare against and
  scores `None`, not a high number.
- **Parsimony is a metric, not a tie-break.** `simpler_at_equal_performance`
  exists so that "improvement" is not always an addition.
- **Unseen instruments and unseen regimes are separate.** The framework's
  existing `unseen_regime_performance` covers half of it, and generalising to a
  new regime on a known instrument is a different ability from generalising to a
  new instrument. `unseen_generalisation` returns the ratio *and* both halves,
  with a note that a ratio above 1.0 more often means the unseen set was easier.

**The verdict starts at `UNPROVEN`** — not `NOT_IMPROVING`, which reads as a
finding, and not `IMPROVING`, which is a claim. `IMPROVING` needs three usable
metrics, a majority improved, and **nothing regressed**. The last condition is
strict on purpose: a change that improves four things and breaks one is a change
with a finding in it, and calling that "improving" is how the finding stops
being discussed.

`to_framework_metrics()` translates into `evolution.IMPROVEMENT_METRICS` names
so a native result lands in the same cycle record as everything else; metrics
with no counterpart are listed under `native_only` rather than lost.

---

## 7. The two demonstrations

`python scripts/native_evolution_demo.py` runs both and prints the trail. Both
are also tests, and `test_the_demonstration_script_runs_both_end_to_end` runs
the script itself — a script that drifted from the tests would stop being
evidence.

### A. evidence → weakness → hypothesis → isolation → training → evaluation → rejection

```
[1] 240 matured forecasts, 225 answered, MAE 0.002876, coverage error -4.9 pts
[2] ACTIONABLE  regime_weakness  regime=ranging  n=120  MAE 1.57x overall
[3] proposal ch-3991f4cc5971 (regime_specialist), 11 fields, falsifiable,
    paired with 1 simplification, entered the backlog as regime_conditional
[4] input digest signed and verifying
[5] worker: 15 of 16 mechanisms applied, verdict completed, trustworthy,
    result signed, workdir gone
[6] gate: 5 stages passed, baseline_comparison FAILED
[7] outcome rejected, terminal, no concealment findings
```

The rejection is on the merits. The challenger — an AR(3) fit — was trained on
a **random walk**, its coefficients shrank toward zero, and it converged on
persistence: a paired bootstrap over 160 observations put the mean MAE gain at
+4.7e-05 with p = 0.35. Statistically indistinguishable, while carrying three
parameters against persistence's zero. **A tie goes to the simpler arm.**

Two things went wrong while building this demonstration, and both are recorded
in the script because they are the failure mode it exists to illustrate:

1. The first version's "persistence" baseline predicted *the last return again*.
   On a random walk that is √2 worse than predicting zero, so the AR arm beat it
   by 30% on pure noise. Persistence on a price series predicts the price does
   not change — a return of zero. **A mis-specified baseline is the most common
   way research produces an improvement that does not exist.**
2. The second version compared two means with `<`. A 1.5% MAE difference on 160
   observations passed. The gate now runs a seeded paired bootstrap and applies
   the parsimony rule to a tie.

### B. deterioration → restriction → restore → audit trail

```
[1] dep-1 native-mt-1, dep-2 native-mt-2 — both deployed by a named operator
[2] metric regression: severity critical; distribution drift: severity major
    → component state shadow
[3] restricted to disabled by "drift-monitor" — no operator involved
[4] autonomous reinstatement REFUSED (GovernanceViolation)
[5] triggers: excessive_drawdown 0.22 > 0.15, forecast_degradation 2.5 > 0.5
    7 triggers unmeasured, and the decision knows it was partial
    rolled back to native-mt-1, which alice deployed
[6] audit chain: 12 entries, 12 verified, no problems
```

The rollback is autonomous because its destination is a version a human already
approved: it can only retreat to previously approved state, never advance.

---

## 8. The nine required prohibitions

Each is checked three ways, because one way is a coincidence.

| Prohibition | Structural | Behavioural | Governance |
|---|---|---|---|
| import broker execution modules | AST parse of all five modules | a worker tries and fails | — |
| read the credential vault | `kernel.audit_evolution_modules()` | a worker tries and fails | — |
| change live mode | AST parse | a worker tries and fails | `enable_live_trading` refused |
| modify hard risk limits | AST parse | a worker tries and fails | `modify_risk_limits` refused |
| clear kill switches | AST parse | a worker tries and fails | `clear_kill_switch` refused |
| edit the audit ledger | — | — | `rewrite_audit_history` refused **for every actor** |
| promote its own capability | `promote()` calls `authorise` first | — | `promote_capability` refused |
| deploy generated code | no deploy path exists in `native/` | a worker cannot reach the network | `deploy_code` refused |
| conceal negative results | rejection terminal, no mutator, no delete | `concealment_check()` | `conceal_result` refused **for every actor** |

Plus: `kernel.propose_kernel_change()` produces a document and there is no
`apply_kernel_change`, which
`test_no_module_in_the_package_can_apply_a_kernel_change` asserts.

---

## 9. Status

Phase 4 is machinery. Applying the honest evidence classes from
`docs/OLYMPUS_NATIVE_MODEL_STATUS.md` §1:

| Component | Implemented | Unit tested | Synthetic | Real data |
|---|---|---|---|---|
| evidence journal and the ten weaknesses | yes | adversarial | yes | **blocked — B1** |
| challenger generation | yes | adversarial | yes | blocked — B1 |
| research isolation | yes | adversarial | yes | n/a — it isolates, it does not forecast |
| twelve-stage gate | yes | adversarial | stages 1–8 exercised on synthetic evidence | **stages 9–10 blocked — B3** |
| improvement metrics | yes | adversarial | 5 of 12 measurable from evidence alone | blocked — B1 |

**Seven of the twelve improvement metrics are unmeasured here** and are reported
as such rather than defaulted: robustness score, failure rate, drift-detection
time, rollback time, drawdown, risk-adjusted return and
`simpler_at_equal_performance`. Four of them need something outside a forecast
journal — the robustness suite, the serving layer, the drift monitor and the
deployment ledger — and three need trades that happened.

**Stages 9 and 10 of the gate have never been run**, because there is no paper
broker fed by real quotes (B3) and no live input stream to shadow. A challenger
reaching `awaiting_review` in this environment has passed eight real stages and
two that were recorded from constructed evidence, and the gate does not
distinguish those — which is a limitation worth knowing before reading a
`PROMOTED` outcome as meaningful.

Nothing has been promoted. `GateLedger.promoted` is empty, and the only
`PROMOTED` outcome anywhere is in a test that supplies its own operator token.

---

## 10. Verify these claims yourself

```bash
# what this host can actually enforce on generated code
python -c "import json; from olympus.trading.native.isolation import \
isolation_report; print(json.dumps(isolation_report()['confinement'], indent=2))"

# both demonstrations, end to end
python scripts/native_evolution_demo.py

# the learning loop, the gate, the metrics and the demonstrations
python -m pytest tests/test_trading_native_evolution.py -q

# the isolation guarantees and the nine prohibitions
python -m pytest tests/test_trading_native_isolation.py -q

# nothing in the evolution surface reaches the safety kernel
python -c "from olympus.trading import kernel; \
print(kernel.audit_evolution_modules())"

# the twelve-stage gate, as a document
python -c "from olympus.trading.native.promotion import describe_gate; \
print(describe_gate())"
```

If a command here contradicts this document, the document is wrong and should be
corrected rather than explained away.
