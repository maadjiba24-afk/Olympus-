# ADR 0005: Enforced answer verification, cross-process safety, and a deterministic difficulty scorer

Status: accepted
Date: 2026-07-18

## Context

A code audit of four capability claims found three gaps:

1. **Aletheia is advisory on the interactive path.** `_verify`
   (orchestrator.py) returns annotated prose with no verdict; the one ABC
   predicate that could block on a hallucination verdict
   (`aletheia_verified`, behavioral_contracts.py:170-181) is never fed a
   `verify_verdict` in production (the live call site passes only
   `{"contract_result": ...}`) and is evaluated at specialist-dispatch time —
   one stage *before* Aletheia runs. A confident falsehood ships flagged, not
   stopped, and a verify-stage crash fails silently open.
2. **Lost updates across processes.** The heartbeat runs as a separate OS
   process sharing the on-disk store with the web/CLI process. Every existing
   lock is a `threading.Lock` (single-process). Unprotected read-modify-write
   races: the usage ledger (usage.py `record`), `memory.save()` (second-
   granularity filename → same-title overwrite), `watchlist_pop()` (fully
   unlocked RMW), and `goals._save`.
3. **Reasoning depth is static.** Every `effort=` is a hardcoded literal per
   call site; the only escalation (teacher) is failure-reactive and no-ops on
   single-model pools (`teacher.py` guard: `len(pool.members) < 2`).

This ADR locks the three design decisions before any code is written.

## Decision (a): Aletheia verdict schema and failure semantics

`_verify` will emit, alongside the corrected/annotated content, a structured
verdict:

```json
{
  "status": "pass" | "warn" | "reject",
  "unsupported_claims": ["<claim>", ...],
  "confidence": 0.0-1.0
}
```

Semantics, enforced at a new `answer.verify` ABC chokepoint that runs AFTER
`_verify` and BEFORE `_synthesize`, feeding `verify_verdict` to the existing
`aletheia_verified` predicate:

- **`reject` fails CLOSED**: exactly one forced rework of the implicated
  specialists (reusing the existing rework path). If the reworked output is
  rejected again, the reply ships **hard-downgraded**: an explicit
  `⚠️ UNVERIFIED` banner is prepended, the unsupported claims are listed, and
  the event is recorded in the signed decision log. Never an infinite loop —
  the rework counter is capped at one.
- **`warn`/`pass` proceed** (warn keeps Aletheia's inline annotations, as
  today).
- **Aletheia infrastructure error** (exception, timeout, malformed verdict)
  fails OPEN **but degraded and visible**: the reply carries the UNVERIFIED
  banner and the failure is logged as a decision — never a silent fall-through
  to raw findings. (Today's behavior appends a note buried in the content;
  the banner becomes a structural, non-optional prefix.)

Rationale: fail-closed on an *affirmative* machine verdict of fabrication is
safe (the verdict is explicit, not an absence); fail-closed on infrastructure
error would let a transient API failure take down every answer, so visibility
— not blocking — is the right degradation there.

## Decision (b): cross-process locking primitive — `fcntl.flock` lockfile wrapper

Two candidates were considered:

1. A small `fcntl.flock` lockfile wrapper used around each RMW section.
2. Routing RMW through the store backend's "CAS" upsert
   (store.py `PostgresStore.put`: `ON CONFLICT (ns,k) DO UPDATE SET
   v=EXCLUDED.v`).

**Option 2 is rejected on correctness, not just size.** The audit prompt
called that path a CAS; verification against the code shows it is not:
`ON CONFLICT ... SET v=EXCLUDED.v` is a blind row-level overwrite —
last-writer-wins with no compare step — so two processes that both read
`spend=X` and write `X+1` still lose an update. Making it a real CAS would
require a version column + retry loop, *and* it would only cover Postgres
deployments; the **default backend is `FileStore`** (plain
`path.write_bytes`), so the default deployment would gain nothing.

**Option 1 is adopted**: a new tiny module (`olympus/proclock.py`) exposing
`with proclock.lock(name):` — an `fcntl.flock(LOCK_EX)` on
`MEMORY_DIR/locks/<name>.lock`, reentrant per process via a thread-local
depth counter, no new dependencies (`fcntl` is stdlib; the target race —
heartbeat process vs web process — is same-machine, same-filesystem, which is
exactly flock's domain). It composes with the existing `threading.Lock`s
(threads within a process still serialize; flock serializes across
processes). Applied at the four RMW sites: usage `record` (and
`today_spend`'s read stays lock-free — reads of a torn file are already
prevented by `os.replace`), `memory.save` filename allocation,
`watchlist_pop`, and `goals._save`/load-modify-save cycles.

`memory.save`'s same-second collision is fixed primarily by **unique
filenames** (microsecond timestamp + `os.getpid()` + an `O_EXCL`-style
create-or-retry), with flock only guarding the rare retry loop — uniqueness,
not serialization, is the semantic fix for an append-style note store.

**FileStore blind-put last-writer-wins is ACCEPTED and documented**: a KV
`put` is *defined* as replace; callers that need read-modify-write must hold
`proclock.lock`. The alternative (versioned CAS in FileStore) buys nothing
for blind puts and complicates every reader. Known limitation: `flock` does
not span machines — multi-host deployments sharing storage must use the
Postgres backend, where the same `proclock` call sites still serialize
per-host writers and the DB serializes the row write itself.

Windows portability: `fcntl` is POSIX-only; on Windows `proclock` degrades to
the existing single-process `threading` behavior with a logged warning (the
heartbeat-vs-web split is documented as unsupported there today).

## Decision (c): difficulty scorer inputs — deterministic only

The pre-scorer is a pure function with **zero LLM calls** and zero I/O:

```
score(risk_class,        # actions.RISK_CLASSES member for the pending action(s)
      prompt_chars,      # len of the task/brief
      tool_count,        # tools in the loadout for this run
      retry_index,       # 0 first pass, 1+ rework
      needs_verification # router flag already emitted today
) -> "low" | "medium" | "high"
```

Mapping (monotonic, clamped): any of {risk_class in
(IRREVERSIBLE, FINANCIAL_LEGAL), retry_index >= 1} forces `high`;
`needs_verification` or prompt_chars/tool_count above thresholds raises one
tier; small, tool-light, no-verification tasks stay `low`. The
per-specialist `effort` field becomes a **floor**: the scorer may raise a
run's effort above it, never lower below it. Thresholds are plain constants
(candidates for `evolve` tunables later — but the *inputs* stay
deterministic forever; an LLM-assessed difficulty signal is explicitly out of
scope).

Teacher escalation on single-model pools: instead of the current no-op, a
rework on a 1-member pool retries the SAME model at the top effort tier
("same-model, more-compute" escalation), so escalation exists on the default
single-key deployment.

## Amendment (post-implementation adversarial review of decision b)

A 22-agent adversarial review of the Phase 2 diff confirmed 18 findings; all
were fixed in the same phase except one design that was **rejected and
reverted**:

- **Per-worker scratch re-rooting is REJECTED.** Making `sandbox.workdir()`
  context-sensitive (workspace/agents/<specialist>) broke a critical
  invariant: the approval spine prepares/previews a held file action on the
  specialist worker thread (scratch set) but executes it from the web/CLI
  approval handler (no scratch) — an approved action ran in a different root
  than the user previewed. It also broke inter-specialist file handoff,
  gallery visibility of generated images, and pre-existing workspaces.
  Concurrent same-path writes by two specialists are ACCEPTED as a residual;
  the safe design — pinning ONE root into the prepared action so preview and
  execution share it — is future work on the actions spine.
- **Lock-scope hygiene:** an in-process mutex must never be held across a
  flock wait (a wedged peer process would freeze every thread queueing on
  the mutex — found on the usage ledger's reply path). Rule: take the flock
  first or split the scopes; hot best-effort paths (evolve telemetry) use a
  bounded `timeout=` acquire and drop the write instead of hanging.
- **Reentrancy identity:** the depth table keys on the SANITIZED lock name —
  raw-name keying self-deadlocks when two spellings map to one lock file.
- **Atomic publish everywhere a reader maps torn→empty:** goals.json,
  scheduler jobs, FileStore.put, prefs, the watchlist rewrite, and note
  bodies (published via `os.link` after a full tmp write) — plus proclock
  coverage extended to evolve's telemetry/tunables blob, scheduler's jobs
  file, prefs, and the conversation counter, and the goals completion write
  re-checks `status == "active"` under the lock so a concurrent drop/close
  is never overwritten by a stale verdict.

## Amendment 2 (post-implementation adversarial review of decision c)

A 10-agent adversarial review of the Phase 3 diff confirmed 5 distinct
findings; resolutions:

- **Replay recordings are version-bound (ACCEPTED).** `effort` is part of
  the hashed replay request, so a pre-Phase-3 recording whose plan step now
  scores differently raises `ReplayDivergence` instead of replaying. This is
  the re-executable-replay design working as intended — any
  behavior-changing release (including Phase 1's prompt changes) invalidates
  old recordings, and the divergence is the tripwire, not a bug. Recordings
  verify the code that produced them.
- **The scorer must be reachable, not cosmetic.** Three findings showed the
  wiring was a production no-op: routing effort was provably constant, every
  shipped specialist floored at "high" (so the floor semantics and the
  single-pool escalation changed no real call), and counting the 7 shared
  BASE tools put most of the roster near the tool threshold. Fixed by: a
  second prompt-length tier (VERY_LONG → "high" reachable from length
  alone), counting only EXTRA tools (threshold 10), and lowering three
  genuinely light specialists (iris, mnemosyne, chiron) to a "medium" floor
  — the cheap path now exists in production, backstopped by the enforcing
  answer.verify gate, Athena review, and the retry→high rule.
- **risk_class is wired, not declared.** A specialist whose extra tools
  include an IRREVERSIBLE/FINANCIAL_LEGAL action (from the static action
  registry) always scores "high" — deterministic, no I/O.
- **Escalation is only traced when real.** `teacher.effort_escalated` is
  emitted solely when the retry bump actually changes the call parameters
  (floor below "high"); an event describing a no-op would mislead trace
  readers.

## Amendment 3 (second-ring sweep after the acceptance re-audit)

The acceptance re-audit swept the whole tree for the cross-process RMW class
and found a second ring of shared files the first pass never named; all are
now under the same regime:

- **agentbeat `heartbeats.json`** (worst offender): `run_due` used to load
  the list, spend minutes running LLM beats, then blind-save the stale list
  — silently deleting any beat added from the chat process mid-run. Now the
  due beats are marked+saved under `proclock.lock("agentbeat")` BEFORE
  running, and the long LLM phase holds no lock; add/remove serialize and
  the save is atomic.
- **operator `operator_jobs.json`**: same mark-first restructure for
  `run_due` (`proclock.lock("operator-jobs")`), `schedule` serialized.
- **todos**: per-user lock + atomic save around every load-modify-save.
- **facts `verified_facts.jsonl`**: append+trim under
  `proclock.lock("facts", timeout=2.0)` — a `_trim` compaction in one
  process can no longer drop an append landing in the other; bounded wait
  because this sits on the verify agent's tool path (a dropped cache write
  under contention beats a hang, and the caller is told).
- **heartbeat state / conversations / conversation-counter reset**: atomic
  publishes (readers map torn files to empty state), and the counter reset
  now takes the same cross-process lock as the bump.
- `usage.today_spend`'s comment now states the true invariant: reads are
  deliberately flock-free; correctness rests on the atomic replace.

Still deliberately out of scope: the remaining plain-write per-user JSON
state with low cross-process exposure (ace playbook, companion, connectors,
docrag, compare, capabilities snapshots), the per-process (not global)
concurrent-call cap, and any cross-machine story beyond the Postgres
backend.

## Amendment 4 (synthesis faithfulness — the last unverified hop)

The acceptance audit noted that Aletheia verifies specialist findings
BEFORE synthesis: nothing checked what Zeus composed, so a synthesis-stage
hallucination passed through. Closed with a stage-4.5 check:

- **What it checks**: faithfulness, not world facts. The findings were
  already fact-checked; the residual risk is the composer ADDING claims
  beyond them (or contradicting them). One no-tools JSON call on the
  verify-role model — `{faithful, unsupported_additions[], confidence}` —
  fed through the `answer.synthesis` contract (same `aletheia_verified`
  predicate).
- **Blocking path**: unfaithful → exactly one recompose with the additions
  named; still unfaithful → structural `⚠️ UNVERIFIED ADDITIONS` banner.
- **Streaming path**: delivered tokens can't be retracted, so an unfaithful
  composition gets a trailing correction note listing the unsupported
  claims — the check still runs and is recorded either way.
- **Checker infrastructure failure**: traced and operator-reported but NOT
  user-bannered — the findings themselves were verified, so a missing
  double-check is not unverified content (unlike the primary verify stage,
  where absence of verification IS the banner condition).
- **Scope**: runs only when the primary verification ran and the answer is
  not already downgraded; skipped in fast mode; `OLYMPUS_SYNTH_CHECK=off`
  kill switch. Cost: one medium-effort JSON call per delegated verified
  turn.

Remaining accepted edges after this: Zeus's direct replies and clarify
turns are still unverified (they make no factual delegation), and the
router's `needs_verification=False` opt-out still bypasses the whole chain.

## Amendment 5 (hardening addendum — Phase 1)

Standing rule adopted: **no dormant code ships** — every enforcement
mechanism is ON by default (kill switches allowed, default-off forbidden).
That is how the vacuous-true bug happened, and structural output contracts
were the last default-off enforcement: `contracts_enabled()` now defaults
ON (`OLYMPUS_CONTRACTS=off` is the kill switch; the shipped contracts
encode already-true output invariants, so enabling changes no happy-path
behavior).

Gate hardening, each with a pinning test:

- **Un-bypassable**: fast mode skips the Athena review only — never the
  answer.verify chokepoint. Proven by test.
- **Verify wall-clock cap**: the verify stage runs under
  `OLYMPUS_VERIFY_TIMEOUT` (default 600 s, always on; `0` disables). A hung
  verifier routes to the existing visible infra-error path instead of
  stalling the reply; the orphaned worker is discarded.
- **Errored rework ships degraded immediately**: an EXCEPTION during either
  rework dispatch (as opposed to a failed verification) never retries — the
  Aletheia-forced rework banners and ships the first verified text; the
  Athena quality rework keeps the first-pass outputs and proceeds.
- **Verdict parsing is total**: schema-validated status enum, coerced claim
  lists, clamped confidence; any garbage — wrong types, nulls, pathological
  nesting — yields either a coerced valid verdict or None (the visible
  degraded path). Never a crash, never a silent pass of an invalid status.

## Amendment 6 (hardening addendum — Phase 2)

- **Bounded lock acquisition by default.** flock cannot go stale (the
  kernel releases it when the holder dies — proven by a kill -9 test: the
  survivor acquires promptly and atomically-published state is old-or-new,
  never torn). The only unbounded wait is a live-but-wedged holder, so
  `proclock.lock` now defaults to a 60 s timeout (`timeout=None` is the
  explicit block-forever opt-in). Caller audit: the heartbeat catches per
  job; a tool call surfaces the TimeoutError honestly; the three reply-path
  callers handle it explicitly — `usage.record` skips the ledger write and
  captures (one lost increment under a wedged peer beats a broken reply),
  `bump_conversation_count` returns 0 (skips the audit trigger),
  `watchlist_pop` returns None (the entry stays queued).
- **The signed audit trail is multiprocess-safe.** `trace.flush` appends to
  a SHARED daily file and the heartbeat's goal cycles run the full pipeline
  — two processes' large-record appends could interleave and corrupt both
  lines, which readers silently skip (a signed run would vanish). Appends
  now serialize under `proclock.lock("traces", timeout=30)`; a wedged peer
  diverts the record to a uniquely-named `overflow-<id>.jsonl` (still found
  by `load_run`/`find_record`, which glob `*.jsonl`) — the audit record is
  never lost and a reply never stalls on the lock. Integrity is pinned by a
  two-process concurrent-flush test (every line parseable, every run
  present) and an overflow test.

## Amendment 7 (hardening addendum — Phase 3)

- **The spend guard outranks the difficulty dial.** When a daily budget is
  set and less than 10% of it remains (`usage.budget_headroom_low`,
  deterministic, error-tolerant), a scored RAISE above the specialist's
  floor is capped back to the floor and the denial is traced
  (`effort.budget_capped`). The run itself — reworks included — always
  still happens: the cap denies extra compute, never the work. With no
  budget set (the default), nothing changes.
- **The scorer is a total function.** `score`/`at_least` coerce any input
  (None, strings, negatives, NaN/inf, arbitrary objects) to harmless
  defaults — never an exception, always a valid tier, always
  deterministic. Pinned by a seeded 300-case property test over randomized
  junk inputs.

## Consequences

- The interactive path gains its first enforcing verification gate; the
  dormant `aletheia_verified` predicate becomes live with a real verdict at
  the correct stage.
- Ledger totals, notes, watchlist entries, and goal writes survive
  heartbeat-vs-web concurrency; a race test with two real processes becomes
  part of the suite.
- "Thinks harder on hard calls" becomes true on single-key deployments, with
  a deterministic, auditable trigger.
- No new dependencies. No security guard becomes tunable. The UNVERIFIED
  banner is user-visible by design — degradation is never silent.
