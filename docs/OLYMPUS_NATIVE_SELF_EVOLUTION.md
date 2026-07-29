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

So the worker is a **separate process**, in its own network namespace, **inside
a cgroup that bounds the whole process tree**, with a rebuilt environment,
behind a seccomp filter, on a **sized tmpfs** that is unmounted and destroyed
afterwards.

### Why a cgroup and not rlimits

Generated code can `fork()`. Every `RLIMIT_*` is **per process**, so a worker
that forks ten children gets ten times its memory allowance and ten times its
CPU allowance. `RLIMIT_NPROC` — the one limit whose name sounds like the answer
— counts processes **per real UID across the whole system**, not per process
tree, and is bypassed entirely for a privileged process.

`tests/test_trading_native_isolation_adversarial.py::test_rlimit_nproc_is_
measured_and_found_insufficient` measures which of the two applies on the host
it runs on rather than assuming either. On this container, running as uid 0,
`RLIMIT_NPROC(2)` does not stop a fork at all.

`pids.max`, `memory.max` and `cpu.max` in a cgroup bound the **tree**, and a
process cannot leave the cgroup it was put into. Termination is the same story:
`killpg` misses anything that called `setsid()`, whereas freezing a cgroup and
killing its members reaches every descendant and can be **proven** to have done
so, because the member list afterwards is empty. `native/cgroups.py` supports
both hierarchies and reports which controls are writable rather than averaging.

### The fifteen controls, and where the evidence comes from

Every one must hold before generated code runs. `run_isolated` refuses
otherwise, with a typed `IsolationUnavailable` naming the shortfall.

Reproduce with `python -c "from olympus.trading.native.isolation import
isolation_report; import json; print(json.dumps(isolation_report(), indent=2))"`.

| Mechanism | Applied here | Established by |
|---|---|---|
| `separate_process` | yes | the pid `Popen` returned |
| `scrubbed_environment` | yes | the environment the parent built |
| `no_production_secrets` | yes | that environment, plus the worker's own scan |
| `ephemeral_workdir` | yes | the parent's own path, and its removal |
| `read_only_inputs` | yes | the worker's write probe **and** the parent re-digesting its copies |
| `network_namespace` | yes | the parent's `readlink /proc/<pid>/ns/net` |
| `cpu_limit` | yes | `cpu.max`, plus membership read from `/proc/<pid>/cgroup` |
| `memory_limit` | yes | `memory.max`, plus the same membership |
| `process_limit` | yes | `pids.max`, plus the same membership |
| `disk_quota` | yes | `statvfs` on the work directory, run by the parent |
| `wall_clock_timeout` | yes | asserted — the parent's own `wait(timeout=)` |
| `descendant_termination` | yes | the cgroup member list, empty after freeze-then-kill |
| `worker_destruction` | yes | the tmpfs unmounted and the directory gone |
| `signed_inputs` | yes | the signature verifies |
| `signed_results` | yes | the parent signed the result digest **and verified it** |

Four more are applied and deliberately **not** required: `seccomp_filter`,
`no_new_privs`, `blocked_imports` and `file_size_limit`. They are defence in
depth, worth having, and not what stands between a generated experiment and the
host.

### The layers, in order of how much they are worth

1. **The cgroup.** A bound on the tree that cannot be escaped by forking,
   detaching or ignoring signals.
2. **No network.** `unshare(CLONE_NEWNET)` leaves the worker with a loopback
   interface and nothing else. An order that cannot leave the machine is not an
   order, whatever code was generated.
3. **No credentials.** The environment is rebuilt from an **allowlist**, not
   filtered by a denylist — a new secret-bearing variable is excluded by default
   rather than by remembering to add it to a pattern list.
4. **Blocked syscalls.** A seccomp-BPF filter returns `EPERM` for `socket`,
   `connect`, `ptrace` and `execve`. Redundant with (2) on purpose.
5. **Blocked imports.** A meta-path hook refusing `execution`, `brokers`, `oms`,
   `modes`, `vault`, `killswitch` and `risk`. **This is the weakest layer and is
   listed last on purpose.** Generated code can defeat an import hook; it cannot
   defeat an empty network namespace.

`test_the_network_namespace_is_judged_without_the_seccomp_filter` exists because
the first version let (4) mask a failed (2): both stop a socket being created,
and sharing one observation would mean a silently failed `unshare` read as a
working namespace. Two mechanisms meant to fail independently must be judged
independently.

### Confinement is established by the parent, not reported by the worker

The first version had the worker probe itself and believed the report. An
adversarial test broke it in four lines: the experiment wrote its own
`result.json` claiming a Sharpe of 99 under a perfect probe, then called
`os._exit(0)` so nothing overwrote it. The parent signed the forgery and
reported it trustworthy.

Three changes, and the guarantee now rests on none of the worker's claims:

- The experiment runs in a **forked child** of the runner. The child cannot
  `os._exit` the runner, and the runner writes the report after `waitpid`.
- The report goes down a **pipe to the parent**, not to a file, and the child
  closes that descriptor before any generated code runs. Nothing that outlives
  the runner can append to it.
- The report carries a **nonce** the parent generated and the runner read and
  unlinked before forking. A line the child manufactures carries no nonce and is
  discarded.

On top of that, the parent reads `/proc/<pid>/cgroup` and `/proc/<pid>/ns/net`
itself, before releasing the worker through a handshake so the read cannot lose
a race to a fast experiment. Every load-bearing mechanism in the table above is
something the parent measured.

What the experiment still controls is its own **return value** — and that is not
a weakness, it is what an experiment is for. A trustworthy manifest says *this
result was produced by this code, under these controls, and nothing escaped*. It
does not say the number is right. `test_the_manifest_attests_confinement_and_
never_correctness` states that so it cannot be misread.

### Things this got wrong first, and now does not

- **`chmod 0444` does not bind uid 0.** Root bypasses the DAC check, and the
  first version shipped a dataset the worker could overwrite while the manifest
  said read-only. The mechanism is now a **read-only bind mount** in a private
  mount namespace, enforced by the VFS regardless of uid, and the parent
  re-digests its copies afterwards.
- **The seccomp filter cannot be installed before `execve`.** It denies
  `execve`, so installing it in `preexec_fn` means the worker never starts. It
  is installed by the runner as its first act, after exec.
- **A worker killed by its own CPU limit is not a confinement failure.** The
  first version reported it as one, because a killed worker writes no probe and
  a missing probe looks like missing confinement. `Verdict.LIMIT_EXCEEDED` is
  checked first — and because the experiment now runs in a child, the runner
  survives to report which signal killed it.
- **Output through pipes hid descendant leaks.** `communicate()` returns at EOF,
  and a detached grandchild holding the write end keeps the pipes open, so a
  leaked descendant surfaced as a timeout instead of as a leak. Output goes to
  files and the parent waits on the *process*.
- **`RLIMIT_NPROC` is not a process-count limit.** See above.

### Signing, and who does it

The **parent** signs the input payload and the result manifest, because a worker
that could sign could sign a result it invented. The **worker** verifies the
input *digest*, so a payload altered between write and read is caught inside the
run. Integrity is checked in both directions; authenticity is only ever claimed
by the parent.

`signed_results` is **determined, not asserted**: the manifest is signed over
its own digest and that signature is verified, and whichever way that comes out
is what the mechanism records. The digest covers the outcome, the inputs, the
result and the confinement, so a stored manifest cannot have its mechanism
states edited without breaking the signature.

`ResultManifest.verdict` and `ResultManifest.trustworthy` are **computed
properties**. A run that completed under failed confinement reports
`CONFINEMENT_FAILED`, and there is no constructor argument that says otherwise.
`trustworthy` additionally requires that the signature verifies now, that the
work directory is gone, and that no descendant survived.

### The adversarial suite

`tests/test_trading_native_isolation_adversarial.py` — twenty-one tests in which
generated code tries to fork repeatedly, call `setsid()`, daemonise by double
fork, spawn subprocesses, open sockets of four families, import the broker and
the vault, modify a read-only dataset six ways, rewrite its own signed payload,
forge its result, claim success and then die, exhaust CPU, memory and disk, and
ignore every catchable signal to outlast its wall clock.

After every one of them, three independent checks prove nothing survived: the
cgroup's member list is empty, every pid the experiment reported is gone, and no
process anywhere on the host still holds a research work directory open.

### Known limitations, because they are real

- **`RLIMIT_FSIZE` caps the largest single file, not total disk.** It is kept as
  defence in depth under its own name; the actual bound is the tmpfs `size=`.
- **The cgroup CPU quota is a rate, not a total.** `cpu.max` bounds the tree to
  one core; total CPU is bounded by the wall clock on top of it.
- **A worker running as uid 0 could read the runner's memory through `/proc`.**
  `PR_SET_DUMPABLE=0` closes that for an unprivileged worker and root ignores
  it. This is why no load-bearing mechanism depends on the worker's report.
- **Linux only.** On Windows and macOS `import olympus` succeeds and any attempt
  to run generated code raises `IsolationUnavailable` naming the platform.
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

### Positive decisions fail closed; negative ones fail safe

The pre-merge audit found `GateLedger._log` wrapping its audit write in a bare
`except Exception: pass`, under the comment *"an audit sink that fails must not
stop the gate"*. That reasoning is right in one direction and inverted in the
other, and the two are now separated.

**Expanding permission** — human review and restricted promotion — commits
through `native/durable.py` in a fixed order, and a failure at any step leaves
the challenger exactly where it was:

1. verify the named operator and the token they carry;
2. validate every piece of promotion evidence and the restriction;
3. write **and read back** the immutable audit event;
4. write **and read back** the durable promotion state;
5. return only once both durable records have succeeded.

**Reducing permission** — rejection, restriction, demotion, rollback, emergency
shutdown — still proceeds when the sink is unavailable, because refusing to stop
a deteriorating model because the audit disk is full leaves it running. The
missing record is surfaced in `GateLedger.unrecorded_safety_actions` rather than
swallowed.

### What "durable" means here, and the crash window

Three things, each checked rather than assumed:

- **Flushed to the device.** `write()` returning is not durability. Every commit
  `fsync`s the file *and* the containing directory, because a rename that has
  not reached the directory entry is a file that is not there after a crash.
- **Read back and compared.** A successful `write()` on a full filesystem can
  store fewer bytes than it was given, and the result parses as JSON right up
  until it does not. The commit re-reads and compares digests.
- **Chained.** Each audit event carries the digest of the one before it, so an
  edited, removed or reordered record breaks the chain and `AuditLog.verify()`
  names the index where it broke. `AuditEvent.digest` is computed from the
  content, never stored and trusted.

The two durable writes cannot be made atomic together. The order is therefore
chosen so the **surviving half is the safe one**: the audit event is written
first, then the state. A crash in between leaves an event saying a promotion was
attempted and no state saying it happened, and `reconstruct()` reads the state
as authoritative — the challenger comes back unpromoted, with evidence in the
audit that somebody tried. The other order would come back promoted with no
record of who approved it.

`tests/test_trading_native_promotion_durability.py` breaks each part in turn:
audit sink unavailable, chain failure, disk full, short write, partial audit
write, partial state write, crash between the writes, duplicate request, restart
and reconstruction, tampered state and tampered log. Every one asserts the same
thing — the challenger is **not promoted**.

### The native model cannot pass this gate at all

`native/quarantine.py` refuses it, before the operator is even consulted. The
matched evaluation returned insufficient evidence and blocker **B8** is open:
the model's error is almost entirely a constant offset. Section 9 has the
detail; the gate consults the quarantine so that no signature can make a model
that loses to persistence promotable.

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
[5] worker: 19 of 19 mechanisms applied (15 required + 4 defence-in-depth),
    verdict completed, trustworthy, result signed, workdir gone, cgroup
    emptied and removed, zero survivors
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
