# M05 comparison records, decisions and recovery

Continuation baseline: delivered PR #315, commit
`ffd1a0b2aa3c926e4133023602190930cc54090b`, tree
`6635d459e5241152f3bc46de010294fc1a677786`. The operator's 2026-09-17
read-only reconciliation passed: tracked worktree/index clean, all 85 refs and
all 86 unrelated files matched their pinned receipts, and actual protected
remote main still matched. No operator repository mutation was part of that
reconciliation. Earlier branches, environments, packages and failed logs stay.

**Status: integrated candidate; native validation and protected delivery OPEN.**
M06–M19 and the genuine/operational evidence gates remain open. This document is
not a provider-execution authorization, a deployment receipt or a quality claim.

## Finite acceptance and connected callers

| ID | Contract | Actual consumer / evidence |
| --- | --- | --- |
| CMP1 | Exact nonempty owner; missing versus unavailable versus unclaimed legacy; bounded strict JSON, types, identities, finite numbers, regular paths | `compare_state`, every public comparison operation; CLI `--owner`; web cookie/account principal; collision and damaged/nonregular evidence cases |
| CMP2 | Strict pool/input refusal before provider work; stable request id; replay of an existing id never executes models; conflicting request refuses | `compare.run`, CLI prints id before work; browser sends a generated id; malformed HTTP body/fields are rejected without coercion |
| CMP3 | Durable preparation and started markers precede each model call; bounded answers and private provenance | `backend.complete_text_once`, provider receipt hooks; Anthropic, OpenAI-compatible, Bedrock and Claude Code owned adapter cases |
| CMP4 | Composite execution identifies each child; no cross-model fallback inside comparisons; no shared MoA trace publication | Comparison-only MoA path; parent/child run ids and actual response receipts; ordinary MoA path remains covered by existing fixtures |
| CMP5 | First reveal fixes the decision, including a reveal without a vote; one atomic snapshot commits decision, derived tally and outbox | CLI/web reveal, failed-answer refusal, conflicting/same-choice retries, threads and actual POSIX competing processes |
| CMP6 | Recovery never calls models; saved answers survive; unpersisted/never-started work is explicitly indeterminate | CLI `--show`/`--recover`; API `get`/`recover`; browser recent/id/recovery controls; faults before and after each run/reveal publication and POSIX directory fsync |
| CMP7 | Qualified local linkage is idempotent and acknowledged; disabled means no calibration I/O; unavailable chain is not replaced | `compare_calibration` publishes observations plus comparison under the existing calibration lock; before/after chain publication, missing owner acknowledgement, toggles, corruption and replay fixtures |
| CMP8 | Expiring detailed answers cannot erase votes or release an id for repeated execution; capacity refuses before model calls | Up to 50 details and 10,000 lifetime comparison ids; compact vote/id receipts; pending execution/calibration is never silently pruned |
| CMP9 | Consumer errors are visible and safe; evidence claims stay scoped | CLI exits 1 on refusal, 2 on pending recovery; HTTP 400/401/404/409/410/503 or 202 pending; blind exceptions contain no model or credential text; owned DOM/fetch recovery contract |
| CMP10 | Windows validation discoveries cannot be concealed by shorter paths or missing archive members | Extended-path preference I/O and backup inventory/archive/restore; unavailable inventory/custody refuses publication; actual native long-path round trips and junction refusal are required |
| CMP11 | Full-suite failures retain their evidence and receive scoped corrections without weakening shared publication contracts | Assessment authorization long paths; complete fixture byte snapshots; unchanged backup command arguments; bounded opt-in Windows rename retries; fresh owned PostgreSQL validation after the shared publisher change |

The focused cloud run is 276 passed / 2 native-Windows-only skips under a
kernel seccomp policy which denied IP socket creation, connect/send operations
and io_uring setup. It used owned model responses and in-memory HTTP dispatch.
It included the existing comparison, fallback/MoA, Bedrock, Claude Code,
calibration and live-quality-authorization contracts. The owned JavaScript
DOM/fetch contract executed in Node; it is not a real browser measurement.
Failed preliminary attempts and their actual diagnostics remain preserved.
The package manifest pins the final source and the actual log/XML bytes.

Native Windows extended-path/junction cases MUST execute on Windows. Native
POSIX process contention and directory durability MUST execute on its native
filesystem. Both full suites, skip/warning review, source guards, all applicable
CI checks, protected merge, actual parent/tree/diff checks, post-merge CI and
operator synchronization remain required. No prior PR315 result validates M05.

### Native Windows failure and bounded correction

The first native run, `local-windows-b2dd96d4016c`, stopped at regression:
1,167 passed, 16 failed, 29 skipped; the full suite did not run. All five required
Windows path/junction cases passed. Its receipt SHA256 is
`cc2d7bc916dfc43eccf4e0a003db9df9765f15fd89e3f5ff8894388f984d327a`.
Read-only diagnostics verified all 55 saved artifacts, all 86 refs and all 86
unrelated file contents. The failed run and its test-state files stay preserved.

Twelve recovery assertions used ordinary `Path` reads of intact 271/272-character
note paths. They now use the same extended-path adapter as production note I/O,
without changing asserted bytes or recovery decisions. The validator's
`OLYMPUS_HOME`/`OLYMPUS_SIGNING_SEED` overrides caused the clean-environment skew
failure; the next run must omit those overrides while retaining a private profile
and memory directory. No signing identity or real configuration is changed.

The other three failures exposed existing production defects: preference
publication failed at a 269-character temporary path, and each of two backup
archives omitted both original owner notes (269/271-character paths). Each
archive matched its own manifest; neither restored the omitted notes. That is
not a verified complete backup. Both originals remain readable and preserved.

Preference I/O now uses the Windows extended API for exact and legacy reads,
writes and explicit repair/migration. Layout, full owner digests and existing
repair collision checks remain intact. Backup inventory uses explicit
`scandir`/`stat`, refuses unreadable/nonregular/linked included entries and
unresolvable custody paths, and uses the extended API through archive creation,
verification and restore. Existing explicit cache/key/temp exclusions remain.
The affected production modules were unchanged from PR315 before this correction.

The correction's owned focused run passed 224 tests with four native-Windows-only
skips under kernel network denial. A preceding launcher attempt did not start
Python; its log is preserved. The comparison/provider integration rerun passed
276 tests with two Windows-only skips. Its earlier attempt had two subprocess
dependency failures; a new isolated runtime restored access to the retained
dependencies without changing the old environment. Those logs also remain.
New native Windows tests must exercise long-path
backup/restore, preference policy/repair/legacy claims and backup junction refusal.
Keep the deep native validation paths that exposed the omissions. Fresh native
suites and protected delivery remain open.

This bounded M14/M19 prerequisite correction does not establish a consistent
snapshot during concurrent writers, crash-atomic whole-tree restore, independent
backup custody, intended-host restore/rollback or delivery receipts. Those remain
in M14, with full migration/erasure in M09. Prior incomplete archives are neither
rewritten nor relabelled complete. At that correction, the 18 carried-forward
PostgreSQL contract files were unchanged. The subsequent shared publisher change
below requires fresh applicable PostgreSQL evidence.

### Full-suite correction (2026-09-18)

The next regression exposed a test fixture using `save_for` for the user-scoped
`lessons` category. The fixture now uses `user_context` plus `save`; a POSIX
exercise also checks the same round trip. The subsequent native Windows run,
`local-windows-33e14b2686a0`, passed regression (1,221 passed / 32 skipped) and
completed the full suite (12,278 passed / 24 failed / 292 skipped). Both suites
passed all eight required Windows cases and all 17 previously failed cases.
Receipt SHA256: `c6de78c9193efadae83563a643cbfc3f3dcebfc4e96b625b36973fee486d31f0`.
The verified export preserves complete XML/log traces; its SHA256 is
`62c0f4cc2792e0a8c3c7aa51d47b99bc42e43152703a8c7f76d602e623bf0bc2`.
All 60 current artifacts and 189 earlier evidence files were verified.

Eleven failures were authorization publication/quarantine paths exceeding the
ordinary Windows path limit. Assessment I/O now uses extended paths for directory
creation, bounded reads, publication, status and repair; owner keys, logical paths,
strict schemas, scope checks and repair bytes remain intact. Eleven other failures
were fixture reads/enumeration of long document, note and mirror paths. Those
fixtures now use native I/O paths and keep their byte, count and owner-isolation
assertions. Seed snapshots explicitly require every seeded note. Backup delivery's
remaining path mismatch is corrected by preserving the supplied command argument
while using native paths for existence checks. No uploader runs in these tests.

The remaining failure was Windows error 5 during calibration replacement. The
saved old and pending files were both readable afterward, with different hashes;
the error does not identify which process or condition denied the rename.
Calibration still uses `atomicio.publish` without fsync on its established hot
path. It now creates an exclusive unique staging file and opts into at most six
rename attempts, sleeping at most 310 ms total for Windows errors 5, 32 or 33.
Only the rename repeats: bytes, observation count and model execution do not.
Permanent denial raises and preserves old/pending bytes; no unlink or permission
change is used to recover. Other atomic-writer callers keep one attempt by default.
Real Windows handle fixtures require both successful retry after release and
preservation after persistent denial. Microsoft documents the relevant
[file sharing and rename access contract](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew).

The correction passed 427 focused owned tests with eight platform skips under
kernel IP denial. Its earlier attempt exposed the existing shared-writer guard;
the guard remains unchanged and passes after keeping publication centralized.
Comparison, provider, owner-workspace, note, wiki and backup recovery integration
passed 559 focused checks with five native-Windows-only skips. An earlier confined
attempt also selected eight real HTTP server tests, which the kernel refused at
socket creation; their native Windows/WSL execution remains required. Both failed
attempts are retained. Dependency, capability, threat-model and noninterference
guards passed. Native Windows/WSL suites and protected delivery
remain pending. `atomicio.py` is now changed from the prior PostgreSQL-tested
contract: the old four-test result is historical only; fresh owned PostgreSQL
contention, transaction, restart and lost-acknowledgement evidence is required.
M07/M15 still include calibration schema, unavailable reads and quarantine review;
this rename correction does not close those broader items.

## Storage and identity

The only comparison store is
`MEMORY_DIR/owners/<owner_key(exact-owner)>/compare-v2/state.json`. Its version-2
envelope embeds the exact owner. It contains detailed records and compact
archived id/vote receipts. A tally is derived from their validated first decisions;
there is no independently incremented counter file. The complete read/modify/
write transaction holds the owner lock and uses unique private staging, file
fsync, atomic replace and a strict POSIX directory barrier. Read operations do
not create a comparison directory or repair bytes; the lock infrastructure may
create its own lock files.

Bounds: 64 MiB serialized snapshot; 50 detailed records; 10,000 total ids;
2–26 members; 32 KiB UTF-8 each for prompt, system and answer; 8,192 characters
for the exact owner; 16 captured dispatches/responses per member (including a
composite parent's dispatch). The comparison MoA path accepts at most six
reference members. Before any call, capacity is reserved for worst-case JSON
escaping and all bounded results. Over-bound/empty/non-string provider answers
become ineligible failures, rather than truncated answers presented as complete.
Receipt exhaustion refuses new work; it never clears history. M09 must supply
reviewed retention/migration/erasure before this finite budget can be retired.

Configured provider/model, endpoint-selection digest and requested effort are
recorded separately from provider-reported model, revision, response id and
endpoint digest. Missing served model/revision is **unavailable**, not inferred
from an alias. Response ids and parent/child run ids do not fragment the tally;
the complete execution/configuration identity does. `tally()` / API `tally` now
key by the full identity digest; `tally_rows` supplies display names and counts.
`chosen_model` and label mapping remain display values; `chosen_identity` is
the exact tally key. API ids are 32 lowercase hex characters. Old 12-character
ids belong to unclaimed legacy data and are not implicitly adopted.

These records are local evidence, not remotely attested identities. A provider
may misreport its served model and a state administrator can modify unsigned
state. Answer text may itself mention a model; blindness removes server-side
mapping and failure diagnostics, not semantic content from a model answer.

## Recovery semantics

A run writes preparation, then a started marker for each call, then that call's
result. A process death or failed result write cannot prove what happened at a
remote provider. Recovery preserves every persisted answer and closes any
remaining slot as **indeterminate**. It does not retry that slot, switch providers
or reconstruct an answer it never saved. A repeated run with the same id and
request returns saved status; a different request with that id conflicts. When
preparation itself never published, no model call was performed.

The first successful reveal publication is the vote commit point. It can accept
an eligible answer only if at least two answers are durable, or reveal without a
vote. Subsequent reads and identical picks preserve it. A different pick, or any
pick after a no-vote reveal, conflicts because it would no longer be blind.
A lost acknowledgement after replace can leave the new state visible. Explicit
recovery republishes the validated snapshot to re-establish the directory barrier
before continuing. No exception text is presented as a blind answer.

The detailed snapshot may expire old completed comparisons. A compact receipt
retains their id and vote; subsequent access returns 410, rather than generating
new answers or counting another vote. Running and calibration-pending records
are preserved. If they fill capacity, recover them before new work.

## Calibration boundary

Collection remains controlled by the existing default-off setting. Starting with
collection disabled never creates a deferred collection obligation. A comparison
started with it enabled records its decision outbox first; publication also
checks that collection is still enabled. Disabling it leaves a pending outbox
without touching the chain. Replay, absent provider receipts or overflow are
excluded from the linkage. A blind preference is never verified correctness:
all linked bodies explicitly carry `verified: false` and
`eligible_for_promotion: false`; observation evidence is completion level only.

The bridge validates bounded chain envelopes, exact sequence, hashes, existing
signatures and event-key uniqueness before appending. It preserves the existing
byte prefix and publishes all new run/comparison events atomically under the
same `calibration` lock used by the existing writer. Event keys include the full
owner digest and comparison id. Existing exact events are acknowledged after a
directory barrier; conflicting/tombstoned events, malformed/truncated chains,
I/O errors and capacity failures leave the owner outbox pending. A failed owner
acknowledgement retries the same events, without appending duplicates or calling
models. Linked events contain no prompt, system text, answer or raw owner.

`recorded` means this publication was durably acknowledged, with its entry hash
retained in the owner snapshot; it is not a perpetual chain-health certificate.
Unsigned-but-valid chains remain explicitly unsigned in calibration verification.
M15 still owns general calibration writer/reader, body validation, custody,
retention, telemetry counters and genuine checkpoint gates. This scoped bridge
is not a claim that every calibration writer or every backend is hardened.

## Legacy, exports, deletion and topology

Legacy `compares/` and `users/<safe_id>/compares/` records and `_tally.json` remain
untouched and unclaimed, including a normalized spelling matching the exact
login. Their presence blocks implicit empty first use. The explicit CLI
`--initialize-empty --acknowledge-unclaimed-legacy` and the corresponding web
button create only the new exact-owner store; they do not assign legacy records,
import old votes or repair a damaged version-2 snapshot. Qualified legacy
attribution/import and user-facing export/erasure remain M09 work. The trusted
Python `compare.export(owner)` returns a validated complete snapshot for an
operator; it is not exposed to the model or web client and reveals private
mapping data. Administrative inspection cannot be evidence of a blind trial.

M09's inventory must include the complete new snapshot (answers, mappings,
child/provider receipts, compact archived ids/votes and outbox), its lock files,
legacy comparison directories, and owner-qualified references in the global
calibration chain. Erasing the owner file alone cannot certify complete erasure.
A calibration link's digest is not encryption or an independent custody proof.

Storage remains filesystem-backed even when the installation's KV backend is
PostgreSQL. Locks serialize processes on one supported POSIX state directory;
Windows retains the documented **single process** boundary of `proclock`.
Directory-fsync guarantees are POSIX-specific. This work does not enable Windows
multi-process writers, distributed locks, shared web sessions/quotas or HA.
M12/M13/M14 remain open; new PostgreSQL or host evidence is not claimed.
