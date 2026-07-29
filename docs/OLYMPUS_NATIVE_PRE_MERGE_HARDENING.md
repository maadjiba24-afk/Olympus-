# Pre-Merge Hardening — Olympus Native Market Intelligence

**The completion report for the four mandatory blockers.**

- **Base commit:** `9110f86a1853f732556aac57ac6c0c478571b88c` (the end of the
  native market-intelligence work: Phases 1–5 plus the final audit)
- **Branch:** `claude/native-pre-merge-hardening`
- **Merged with:** `origin/main` at `da4527b`
- **PR #215 is untouched.** It is merged and does not contain these commits.
  Nothing here reopens or modifies it.
- **Nothing was merged during this work.** The result is opened as a **draft**
  pull request.

Every claim below is reported under one of the nine required categories, and
the categories are used strictly. "Tested in GitHub CI" means a job ran on
GitHub's runners; "tested locally" means it ran in this container and nowhere
else. They are not the same evidence and are not reported as if they were.

---

## 1. Summary against the six merge-readiness conditions

| # | Condition | Status |
|---|---|---|
| 1 | All four blockers closed | **yes** — sections 2–5 |
| 2 | The branch includes current main | **yes** — merged at `da4527b` |
| 3 | GitHub CI green on the exact head commit | **pending** — see §7 |
| 4 | Generated experiments cannot escape or exhaust the host | **yes on a capable Linux host**, and refused on any other — §2 |
| 5 | Positive promotion cannot succeed without durable audit evidence | **yes** — §4 |
| 6 | The unvalidated model is structurally incapable of reaching production | **yes** — §5 |

**This is not merge-ready until condition 3 is satisfied on the pull request's
head commit.** Local test output is not a substitute and is not offered as one.

---

## 2. Blocker 1 — generated-code isolation

### What was wrong

`olympus/trading/native/isolation.py` reported `process_limit` as
`applied=False` with a comment explaining that `RLIMIT_NPROC` counts per real
UID and could not be used. That was an honest statement of a real gap, and the
gap was load-bearing: **generated code can `fork()`, and every `RLIMIT_*` is
per-process.** A worker allowed 512 MB that forks ten children has 5 GB. A
`killpg` at timeout misses anything that called `setsid()`.

### What was built

**`olympus/trading/native/cgroups.py`** (new, 512 lines). Linux-only, imported
lazily. Probes both hierarchies — v2 unified and v1 legacy — and reports which
of `pids.max`, `memory.max`, `cpu.max` and the freezer are *writable*, tested by
writing each control's own value back rather than by reading its mode bits.
`CgroupScope.create()` raises `IsolationUnavailable` on any shortfall rather
than continuing without a controller it was asked for.

Termination is **freeze-then-kill**. Reading a live cgroup's member list and
signalling it is a race a fork bomb wins; a frozen cgroup cannot create
anything. v2's atomic `cgroup.kill` is used when present.

**`isolation.py`** now enforces fifteen controls and refuses to run generated
code unless every one holds:

| Control | Mechanism | Evidence |
|---|---|---|
| separate process | `Popen` | the pid the parent was handed |
| scrubbed environment | allowlist rebuild | the environment the parent built |
| no production secrets | that environment + the worker's own scan | both |
| ephemeral workdir | `mkdtemp` | the parent's path, and its removal |
| read-only datasets | read-only bind mount | the worker's write probe **and** the parent re-digesting its copies |
| network namespace | `unshare(CLONE_NEWNET)` | the parent's `readlink /proc/<pid>/ns/net` |
| CPU limit | `cpu.max` over the tree | membership read from `/proc/<pid>/cgroup` |
| memory limit | `memory.max` over the tree | the same membership |
| process-count limit | `pids.max` over the tree | the same membership |
| disk quota | tmpfs `size=` | `statvfs`, run by the parent |
| wall-clock timeout | `wait(timeout=)` | asserted — it is the parent's own clock |
| descendant termination | freeze-then-kill | the cgroup member list, empty afterwards |
| work-directory destruction | unmount + `rmtree` | the directory is gone |
| signed inputs | parent signature | it verifies |
| signed results | parent signature | **produced and then verified** |

`SIGNED_RESULTS` is no longer hard-coded. The manifest is built, signed over its
own digest, and that signature is verified; whichever way that comes out is what
the mechanism state records. The digest covers the outcome, the inputs, the
result **and the confinement**, so a stored manifest cannot have its mechanism
states edited without breaking the signature. `SIGNED_RESULTS` is the one
mechanism excluded from what the signature covers, for the structural reason
that it is established *by* producing that signature.

`ResultManifest.verdict` and `.trustworthy` are computed properties. A run that
completed under failed confinement reports `CONFINEMENT_FAILED` whatever the
constructor was told. `trustworthy` additionally requires that the signature
verifies *now*, that the work directory is gone, and that no descendant
survived.

### RLIMIT_NPROC: measured, not assumed

The instruction was not to count it as sufficient without proving its semantics.
`test_rlimit_nproc_is_measured_and_found_insufficient` sets it to 2 in a forked
child and forks again, and asserts whichever of the two real behaviours occurs:

- as an unprivileged uid with other processes running, the fork fails
  immediately — the limit was already spent by unrelated processes;
- as uid 0, the fork **succeeds** — the check is bypassed entirely.

On this container it is the second. Either way it is not a per-experiment bound,
and the test also asserts that `isolation.py` does not set it.

### The forgery that this found

The first version of the runner probed its confinement and then `exec`'d the
generated code in the same interpreter. An adversarial test broke it in four
lines: write a `result.json` claiming a Sharpe of 99 under a perfect probe, then
`os._exit(0)` so nothing overwrites it. The parent signed the forgery and
reported it trustworthy.

Three changes fixed it, and the guarantee now rests on none of the worker's
claims:

- the experiment runs in a **forked child** of the runner, which writes the
  report after `waitpid` and cannot be `os._exit`ed by the child;
- the report goes down a **pipe to the parent**, and the child closes that
  descriptor before any generated code runs;
- the report carries a **nonce** the runner read and unlinked before forking.

On top of that the parent reads `/proc/<pid>/cgroup` and `/proc/<pid>/ns/net`
itself, before releasing the worker through a handshake so the read cannot lose
a race. Every load-bearing mechanism above is something the parent measured.

What the experiment still controls is its own return value.
`test_the_manifest_attests_confinement_and_never_correctness` states that
explicitly, because a trustworthy manifest saying *this ran under these
controls* is easy to misread as *this number is right*.

### The adversarial suite

`tests/test_trading_native_isolation_adversarial.py` — 21 tests. Generated code
attempts to:

| Attempt | Outcome |
|---|---|
| fork repeatedly | capped at 3 children under `pids.max=5`; peak 4 |
| call `setsid()` | detached child terminated; `killpg` would have missed it |
| daemonise by double fork, reparented to init | terminated |
| spawn subprocesses (`/bin/sh`, `python3`) | `execve` denied — `PermissionError` |
| open sockets (inet, inet6, unix, raw) and connect | all denied |
| import vault, brokers, execution, oms, killswitch, risk, modes | all `ImportError` |
| modify a read-only dataset (append, truncate, chmod-then-write, unlink, rename, rmtree) | all `OSError`; source file byte-identical |
| rewrite `payload.json`, `runner.py`, `experiment.py`, `nonce` | changes nothing the parent believes; the nonce is unreadable |
| forge a report, sprayed at every fd 0–40 and to `result.json` | discarded; result `{}` |
| claim success and then `SIGKILL` itself | not reported as completed |
| exhaust CPU | `LIMIT_EXCEEDED` at 3s of a 30s wall clock |
| exhaust memory | stopped at 32 MB of a 64 MB budget |
| exhaust disk (1000 × 1 MiB files, each legal under `RLIMIT_FSIZE`) | `ENOSPC` at 7 MiB of an 8 MiB tmpfs |
| ignore SIGTERM/SIGINT/SIGHUP/SIGQUIT, `setsid`, seed four children, sleep forever | `TIMED_OUT` at 4s; all killed |

After **every** test, three independent checks prove nothing survived: the
cgroup's member list is empty, every pid the experiment reported is gone
(zombies distinguished from running processes), and no process anywhere on the
host still holds a research work directory open. An autouse fixture fails any
test that leaks regardless of what it asserted.

### Status

| Category | Item |
|---|---|
| **Fixed** | process-count limit, disk quota, descendant termination, `SIGNED_RESULTS`, computed verdict, report forgery, exit-status agreement, pipe-EOF masking a descendant leak |
| **Tested locally** | all 21 adversarial tests, on cgroup **v1** in this container |
| **Tested in GitHub CI** | pending — the `isolation (adversarial)` job runs it unprivileged and privileged, and cannot pass having run neither |
| **Linux-only** | the entire mechanism |
| **Unsupported by platform** | Windows and macOS: generated code is refused |
| **Still unsafe** | a uid-0 worker could read the runner's memory through `/proc`; this is why no load-bearing mechanism depends on the worker's report |

---

## 3. Blocker 2 — platform-safe imports

`resource`, `ctypes` and `signal` moved from module scope into the functions
that use them. `cgroups.py` is imported lazily, and only
`IsolationUnavailable` is re-exported at module scope so callers on every
platform have something to catch.

`tests/test_trading_native_isolation_platform.py`:

- walks **every** module in `olympus/` and fails on a module-scope import of
  any of `resource`, `fcntl`, `pwd`, `grp`, `termios`, `posix`, `tty`, `crypt`,
  `syslog`, `nis`, `spwd`, `msvcrt`, `winreg`, `winsound`, `_winapi`. One
  exemption is enumerated with its reason (`proclock.py`, guarded by
  `try/except ImportError`), and a companion test fails if that exemption goes
  stale;
- imports the whole package in a **subprocess** with those modules made
  genuinely unimportable, and a further test proves the blocker itself works so
  the first cannot pass vacuously;
- asserts `IsolationUnavailable` — a `TradingError` subclass, not `ImportError`
  and not `OSError` — for `win32`, `darwin`, `cygwin`, `aix` and `sunos5`;
- asserts that `isolation_report()` is answerable on an unsupported host and
  still states what *would* be required, so a reader can tell an unsupported
  host from an unmeasured one;
- asserts that `trusted_reason` does not exempt a hand-written experiment from
  the platform check. It says "this code was not generated", which is a
  statement about the code, not a licence to launch a worker where `preexec_fn`
  does not exist.

### Status

| Category | Item |
|---|---|
| **Fixed** | unconditional `resource` import; the refusal is now typed |
| **Tested locally** | the AST guard, the simulated-Windows subprocess import, every platform refusal |
| **Tested in GitHub CI** | pending — `import smoke (windows-latest)` and `import smoke (macos-latest)` |

---

## 4. Blocker 3 — promotion fails closed

### What was wrong

`GateLedger._log` wrapped its audit write in `except Exception: pass`, with the
comment *"an audit sink that fails must not stop the gate"*. In the direction of
**stopping** something that reasoning is right. In the direction of
**promoting** it meant a challenger could go live with nothing on record saying
who approved it — and afterwards that state is indistinguishable from a
promotion nobody authorised.

### What was built

**`olympus/trading/native/durable.py`** (new). A hash-chained append-only audit
log and an atomically-replaced state store. Every commit `fsync`s the file *and*
the containing directory, then re-reads and compares digests. `AuditEvent.digest`
is computed from the content and never stored-and-trusted.

`GateLedger.promote` and `advance` into a human stage both commit through
`_commit_positive`, in this order:

1. verify the named operator and their token (`governance.authorise`);
2. validate every required piece of promotion evidence and the restriction;
3. write and verify the immutable audit event;
4. write and verify the durable promotion state;
5. update the in-memory ledger only after both durable records succeeded.

A gate constructed with no durable paths **refuses to promote at all** unless
`allow_volatile_promotion=True` is passed, so a production ledger cannot
promote into RAM by omission.

The audit event is written before the state on purpose. The two writes cannot be
atomic together, so the order is chosen so the surviving half is the safe one: a
crash between them leaves an attempt on record and no permission granted, and
`reconstruct()` reports it.

Negative actions — reject, restrict, demote, roll back, shut down — still
proceed when the sink fails, and the missing record now appears in
`unrecorded_safety_actions` instead of vanishing.

### The nine failure modes

`tests/test_trading_native_promotion_durability.py`, 23 tests. Each breaks one
part and asserts the challenger is **not promoted**:

audit sink unavailable · chain does not verify · disk full (`ENOSPC`) · short
write that succeeds · partial final line · corruption mid-log · partial state
write · crash between the two writes · duplicate request · restart and
reconstruction · state naming an audit event that is gone · tampered state
digest · a promotion inserted into the state with no audit event.

A structural test asserts that neither `promote` nor the human-stage branch of
`advance` reaches the best-effort logger, so the guarantee cannot be undone by a
future edit that still passes the behavioural tests.

### Status

| Category | Item |
|---|---|
| **Fixed** | catch-and-ignore on positive promotions; volatile-by-default promotion; unvalidated evidence |
| **Tested locally** | all 23 tests |
| **Tested in GitHub CI** | pending — part of the `linux (py3.x)` matrix |
| **Deferred** | the durable stores are file-backed; a networked audit service would need its own availability contract, and none is wired here |

---

## 5. Blocker 4 — the unvalidated model is quarantined

**The result is not hidden and not cosmetically improved.** It is recorded as
data, in `olympus/trading/native/quarantine.py`, and enforced:

> **B8** — the model's error is almost entirely a constant offset. On the
> held-out synthetic split, MAE is 0.025636 with a mean signed error of
> +0.025038, roughly 2.7σ of location, with the predicted spread approximately
> correct. A model whose whole error is an offset has not learned the
> conditional mean it was trained for.
>
> **B9** — the native arm lost to a gradient-boosted tree, to a linear fit and
> to persistence, on identical data, costs, splits and metrics. The matched
> evaluation returned INSUFFICIENT EVIDENCE, which is not a tie.
>
> **B1** — no market data has ever been read. **B3** — no broker has ever been
> reached.

B8's resolution criteria explicitly **exclude the fix that would make the number
go away without fixing the model**: "the mean signed error is within one
standard error of zero, *without a post-hoc offset term*", on a second split the
model was not selected on. No further tuning against synthetic data was done.

The eight structural properties, each with a test:

| # | Property | Enforced by |
|---|---|---|
| 1 | experimental | `experimental()` computed from open blockers; `BLOCKERS` is a frozen tuple of frozen dataclasses |
| 2 | not the default forecaster | `forecast.py` does not mention it; an AST sweep checks every module-level `DEFAULT_*` in `olympus/trading/` |
| 3 | not automatically registered | a subprocess wraps `ModelRegistry.register`, imports the package and resolves every lazy attribute, and asserts nothing was registered |
| 4 | cannot participate in live trading | `Purpose.LIVE/PAPER/SHADOW` raise at construction; `assert_not_live(mode)` is a second, independent refusal |
| 5 | cannot become production eligible | computed from the blockers; registering **and approving** it in the registry changes nothing |
| 6 | cannot pass the promotion gate | the gate consults `assert_promotable`, which scans every string in the evidence *and* the challenger id — renaming it does not get it through |
| 7 | not autonomously selectable | a constant `False`, not derived from the blockers, because moving a model into use is a permission expansion |
| 8 | disabled by default | `enabled()` requires the flag **and** zero open blockers |

Property 8 is the one worth reading twice. `OLYMPUS_NATIVE_MODEL_ENABLED=1` does
not enable the model. It records an operator's intent, and the blockers still
say no — `test_setting_the_flag_expresses_intent_and_changes_nothing` asserts
exactly that for five spellings of "true". The quarantine is not one environment
variable deep.

Closing a blocker is an edit to `BLOCKERS` in a commit a reviewer reads, not an
argument anyone can pass.

### Status

| Category | Item |
|---|---|
| **Experimental** | the entire native model, and it now says so in its own identity record, so the label reaches every audit entry and model card |
| **Fixed** | the model had no structural barrier to being registered, selected or promoted |
| **Tested locally** | 50 tests |
| **Tested in GitHub CI** | pending — `linux`, `import smoke`, and `base install` all run the quarantine suite |
| **Blocked on external systems** | B1 (no reachable market data), B3 (no reachable broker) — see `docs/TRADING_EXTERNAL_VALIDATION.md` |
| **Still unsafe** | nothing: the model cannot reach production. It is also not *useful* yet, which is a different sentence and the honest one |

---

## 5b. What CI found that local runs did not

Four defects surfaced on GitHub's runners and nowhere else. They are listed
because they are the argument for the CI matrix existing.

**`import olympus` fails on Windows.** Not because of anything in this branch:
`olympus/trading/instruments.py` builds its market sessions at module scope,
and `zoneinfo.ZoneInfo("UTC")` raises on Windows because CPython ships no IANA
time-zone database there. The whole trading package was unimportable on
Windows. Fixed by declaring `tzdata` as a `sys_platform == "win32"` dependency,
and the error message now distinguishes "this zone does not exist" from "this
machine has no zone database". The README's runtime-dependency claim and its
anti-rot guard were updated to state the platform-scoped dependency rather than
fold it into the count.

**`pipeline.capabilities()` raised when torch was absent.** A function whose
docstring reads *"detected, never assumed"* called `torch_version()` in its
return literal, before checking availability. Every no-torch runner hit it.

**`MatchedReport.baseline_verdict` returned two different shapes.** Two keys
when the Olympus arm did not run and seven when it did, so
`scripts/matched_evaluation.py` raised `KeyError: 'n_comparisons'` on exactly
the machine where the arm cannot run. One key set now, always.

**A unit test asserted a superiority the matched evaluation had already
contradicted.** `test_the_model_beats_persistence_when_structure_exists` passed
on this container's torch build and failed on GitHub's CPU-only build at a mean
gain of **-0.0037**. The assertion was **removed, not widened**: a claim that
depends on which BLAS the tensor library was linked against is not a claim, and
B9 already records that the model loses to persistence on the matched protocol.
The measurement is still taken and printed; what went is the assertion that its
sign is positive.

That last one is the reason the isolation suite is gated on host capability
rather than assumed: a test that only passes on the machine it was written on
is a test that reports the machine.

---

## 6. What was deliberately not done

- **No further tuning of the native model.** The instruction was explicit, and
  tuning against the only data available — synthetic series the model was
  already selected on — would produce a number that means nothing.
- **No post-hoc bias correction.** Subtracting the measured offset would close
  B8 numerically and change nothing about the model. B8's resolution criteria
  rule it out by name.
- **No merge.** Nothing was merged, and PR #215 was not touched.
- **No live trading, no real money, no broker credentials.** Unchanged from
  Phases 4 and 5.

---

## 7. Continuous integration

`.github/workflows/native-market-intelligence.yml` adds, alongside the existing
`ci.yml`:

| Job | Covers |
|---|---|
| `linux (py3.10 … py3.13)` | the full native suite on the declared range |
| `import smoke (windows-latest)` | `import olympus`, every native module, the typed refusal |
| `import smoke (macos-latest)` | the same |
| `base install (no native extra)` | asserts torch is **absent**, then imports and runs the stdlib half |
| `install with [native]` | asserts torch is **present**, then runs the torch-dependent suites |
| `isolation (adversarial)` | unprivileged and privileged, via `scripts/isolation_ci_gate.py` |
| `guards` | capability counts, threat model, dependency disclosure, Kronos independence, CI-matrix/pyproject agreement |
| `native` | one required context that fails if any leg did |

The adversarial job cannot pass vacuously. `scripts/isolation_ci_gate.py` prints
the host's capability report and then branches: on a capable host it runs the
escape attempts and propagates their exit code; on an incapable one it asserts
the **fail-closed refusal** — generated code refused before it starts, the
shortfall named, no work directory created. There is no path where the job goes
green having tested neither.

The base-install and native-install jobs each begin by asserting the condition
that makes them meaningful — torch absent, torch present — so neither can
silently become a copy of the other.

**CI has not yet run on the head commit of this branch.** Until it has and is
green, condition 3 of §1 is unmet and this work is not merge-ready. Local
results are reported above as local.

---

## 8. Pull-request structure

The work is opened as **one draft pull request**, and the reason is worth
stating because the instruction preferred a split.

The preferred split was: (1) schemas, datasets, representations, baselines,
model and evaluation framework; (2) generated-code isolation, controlled
evolution and promotion machinery.

That split is not available on this branch, because **(1) is already merged**.
PR #215 carried the schemas, datasets, representations, baselines, the model and
the evaluation framework, and it is closed. What remains between `9110f86` and
current main is Phases 4 and 5 plus this hardening — which is to say, it is
almost entirely part (2) already, plus the matched evaluation that produced the
verdict the quarantine enforces.

Splitting what is left would separate the quarantine from the evaluation that
justifies it, and separate the promotion gate from the isolation it depends on.
A reviewer reading the isolation module alone could not tell whether the
promotion gate consults it; a reviewer reading the gate alone could not tell
whether B8 is real. The security-sensitive code is reviewable on its own terms —
§2 and §4 above are the reading order, and the two adversarial suites are
self-contained — but the *justification* for the constraints lives in the
evaluation, and separating them would make the constraints look arbitrary.

If a reviewer would still prefer the split, the natural cut is
`native/{isolation,cgroups,durable,promotion,quarantine}.py` plus their four
test files, against everything else. Say so and it will be cut that way.

---

## 9. Verify these claims yourself

```bash
# what this host can enforce
python -c "from olympus.trading.native.isolation import isolation_report; \
import json; print(json.dumps(isolation_report(), indent=2))"

# the quarantine, as data
python -c "from olympus.trading.native import quarantine; print(quarantine.describe())"

# every escape attempt
pytest -q tests/test_trading_native_isolation_adversarial.py

# every way a promotion can fail to be recorded
pytest -q tests/test_trading_native_promotion_durability.py

# the eight structural properties
pytest -q tests/test_trading_native_quarantine.py

# platform safety, including the simulated-Windows import
pytest -q tests/test_trading_native_isolation_platform.py

# the CI gate's own logic, on this host
python scripts/isolation_ci_gate.py
```
