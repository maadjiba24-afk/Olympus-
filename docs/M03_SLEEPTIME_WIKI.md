# M03: exact-owner consolidation and recoverable publication

Reconciled 2026-09-16 against authentic merged PR #314 commit
`e47993f2831f76b7091fe4c897366de00ec4bb9d`, tree
`87ce4780b82ee72de14cdea8fb73b7918b4ac688`, parent
`7a96b42131707303ce5090577537e55fdfe70a6b`.

**Status: integrated implementation and focused adversarial regression prepared;
Windows/POSIX full suites, owned PostgreSQL evidence and delivery OPEN.** This register
does not resolve a defect by describing it. M01, M02 and M04 are delivered;
M05-M19 and the master C4-C9/E1-E31 register remain active.

### Windows full-suite correction (2026-09-16)

The first native Windows attempt passed all guards and the focused regression
(692 passed, 21 skipped), then stopped at three full-suite failures
(12,141 passed, 283 skipped). That failed run remains evidence, not a passed gate.
The strict delta reader now retains the corruption diagnostic for malformed JSON,
UTF-8, duplicate fields, invalid schema/identity and incomplete tails, while
unreadable files remain explicitly unavailable. Verify, restore and append
preserve damaged bytes. The original fail-closed assertions remain unchanged.

The corrected validator no longer injects five nonexistent control names. It
keeps the actual activation flags off in child processes, strips credentials,
and documents the real consolidation, RL, evaluation and anchor settings.
The liveness purity test imports HTTP/SSL clients before replacing their socket
class so the same no-network assertions work in isolation as well as the suite.
The PowerShell wrapper captures the child process exit code directly. A guarded
upgrade accepts only the exact previously applied M03 tree; fresh environments,
Windows/POSIX full suites, owned PostgreSQL and protected delivery are still
required for the corrected result. No operational setting is enabled.

## Reconciled baseline

M02 already changed `sleeptime.run` and `supervise.run_supervised_cycle` to use
`usermem.owners()`, which reads validated envelope principals. Those callers no
longer enumerate raw backend keys; retain that completed fix. M04 already binds
wiki note material to the requested exact owner and propagates unavailable note
evidence. Remaining wiki directory enumeration and typed-material error defaults
are separate defects. Preserve M02's typed-memory transactions and M04's note
journal, action integration, owner contexts and logical Windows directory paths.

## Finite acceptance checklist

Every delivery row is OPEN. A row closes only with integrated callers, meaningful tests,
applicable platform/backend evidence and the protected delivery contract.

| ID | Confirmed remaining defect and stores | Required behavior and acceptance |
| --- | --- | --- |
| S1 | `sleeptime.proposals`, `sleeptime.quarantine`, `sleeptime.snapshots` use `safe_id`; `_load` maps bad JSON to absence | Exact-owner envelopes and bounded strict records, explicit missing/unavailable/unclaimed-legacy state. No inferred ownership or destructive automatic migration. Reject owner transplants, duplicate IDs, invalid probabilities/times/statuses and nonregular input. Connect status and explicit initialization/recovery, preserving all legacy bytes. |
| S2 | `_commit` publishes snapshot, new memory, individual supersessions, snapshot linkage and proposal acknowledgement separately; `revert` changes rows independently and ignores stale source contents | One atomic backend transaction or durable recoverable operation covers the whole rewrite and its evidence. Bind source versions, proposal owner, verifier decision and resulting memory. Preserve the complete before-state, refuse intervening edits and cross-owner substitution, make approve/revert retries idempotent, and recover interruption without rerunning generation or verification. Test concurrent proposals, all publication phases and PostgreSQL rollback/lost acknowledgement. |
| S3 | Global `sleeptime/state` is loose unlocked RMW; supervision advances its streak before writing the signed scoreboard; proposal slicing by list length loses attribution at a cap or under concurrency | Strict cycle identity and exact persisted proposal IDs; no clean graduation from missing/corrupt/unavailable or incomplete evidence, forged truthy fields, empty work or ambiguous retry. Couple qualification to verified durable scoreboard evidence; distinguish cycle execution from recording failure and prevent duplicate qualification. Supervision remains hard-off for apply; activation flags are never changed by the harness. |
| S4 | Wiki pages and `.dream_state.json` live under normalized `users/<owner>/wiki` or legacy shared `wiki`; reads create directories, parse defaults hide corruption, direct writes/deletes lack durable serialization | Exact-owner wiki authority with strict bounded page/checkpoint schemas. Preserve unclaimed legacy pages including old shared data. Connect CRUD, listing, lint and retrieval; reject symlinks/reparse/FIFO/damaged UTF-8 and malformed freshness metadata. Prevent title/slug collisions from silently overwriting unrelated pages and avoid destructive capacity eviction. |
| S5 | `wiki.dream` publishes pages one at a time and then checkpoints; malformed rows are skipped, result flags are coerced; `_active_memory_ids` and material gathering can conceal source failures | Validate the entire proposed batch and all source evidence before mutation. Bind source versions and publish pages/retirements/checkpoint atomically or with tested recovery. Failed or partial publication never advances a successful dream checkpoint; stale generation cannot overwrite intervening operator edits. Include equal-time additions, retries, capacity and interrupted resume/rollback. |
| S6 | `wiki.users_with_material` treats legacy directory spellings as owners and swallows enumeration errors; `supervise` and `reflect` leave shared context set | Enumerate validated exact principals from typed memory, private notes and wiki state, never normalized directory labels. Preserve caller context on success, exceptions and background dispatch; bind injected and default model runners to the intended owner. Preserve explicit installation-wide system work and prevent partial enumeration from qualifying a clean cycle. |
| S7 | Discovery acquisition is deliberately refused pending wiki qualification; dormant implementation writes a wiki page separately from gap acknowledgement; feature proposal publication also has a separate acknowledgement | Remove the temporary refusal only after S4-S6 and real consumer tests pass. Persist an owner/gap/content-bound publication operation with conflict-preserving retry and recovery, so a completed research result or feature publication is not repeated after acknowledgement failure. Validate gap identity/status before provider work. Maintain replay inertness and opt-in acquisition; exercise owned mock research, never live collection. |
| S8 | `reflect.run_cycle` gates a prompt then records its snapshot separately; prompt apply, benchmark, restore and backup consumption can be interrupted or overlap; mined unreadable traces become an empty success | Preserve the single benchmark-gated public writer and coverage refusal. Add bounded typed gate outcomes and durable operation identity binding original/new prompt, exact backup, benchmark identity/results and signed evidence. Restore the specific operation's original bytes on failure, refuse stale rollback, and expose interruption/unconfirmed completion without claiming success from a string. Shared prompt scope stays explicit. No model/verifier calls during validation. |
| S9 | `deltas` uses a thread-only lock, reusable temp path and no fsync for relevant snapshots; unsigned fallback can be recorded; `rlscaffold._scoreboard_health` turns unavailable evidence into zero counts | Make the durable evidence used by S2/S3/S8 trustworthy on supported topology: bounded reads, validated chain/target/signature, unique durable publication, process-safe serialization and explicit attestation/anchor failure. Qualification must not consume unsigned or damaged history. Retain optional external anchoring restrictions and distinguish local persistence from external attestation. Connect report/export consumers; missing and unavailable counts differ. |
| S10 | CLI/gateway wiki, orchestrator enrichment and reflection scheduler need matching failure/recovery semantics; retention has historical namespace inventory | CLI status/approve/revert/recovery and actual gateway/tool/heartbeat/orchestrator consumers use the repaired APIs. Report unavailable optional wiki enrichment explicitly without turning it into empty evidence; preserve user task behavior. Register new retained sensitive data for M09 without claiming principal erasure. Validate original compatibility tests, new adversarial cases, Windows/POSIX full suites, changed-backend contracts, exact CI, protected merge and synchronization. |

## Store and caller inventory

- Owner KV: the three sleeptime namespaces above and delivered
  `usermem.state.v3` (events/memories/candidates) participating in a rewrite.
  The implementation must choose one coherent transaction/recovery boundary;
  nesting two existing single-document transactions is not atomic publication.
- Global qualification: `sleeptime/state`, signed `sleeptime:scoreboard`,
  owner quarantine targets, their delta files and any new operation receipts.
- Wiki: pages, freshness metadata, dream checkpoint/seen IDs, legacy private and
  shared trees; exact private note and typed-memory source snapshots.
- Discovery: exact-owner `discovery/gaps.json`, wiki destinations and explicitly
  shared upgrade notes. A shared upgrade proposal is a retained product boundary,
  not authority to expose private wiki/typed-memory content indiscriminately.
- Prompt reflection: decision traces (source), prompt files (shared target),
  prompt backups, benchmark evidence, `prompt:<agent>` delta history, gate notes
  and recovery receipts. Broader skills gating remains in M19 unless required
  by a shared primitive changed here.
- Entry points: `sleeptime`, `supervise`, `wiki`, `discovery`, `reflection`,
  `reflect`, `heartbeat`, `orchestrator._wiki_block` / `gate_prompt` /
  `evolution_audit`, `tools._apply_prompt` / `_restore_prompt` / `_gate_prompt` /
  `_update_prompt`, CLI sleeptime/supervision/wiki/discovery, gateway `/wiki`,
  and `rlscaffold` scoreboard reports. Preserve actual route behavior, not just
  helper tests. Audit transitive consumers of any changed shared primitive.

## E20 reconciliation within this batch

The historical inventory stays pinned to PR #311. Baseline E20-295/296 (wiki
metadata parsing), E20-297 (typed material) and E20-299 (wiki enumeration) are
implemented in M03 with strict wiki snapshot/material/enumeration reads;
platform validation and delivery remain open. E20-298's private-note material suppression and E20-203's
prompt-backup unlink suppression were removed by delivered M04; preserve those
fixes. Their wider wiki/prompt transaction requirements remain S5/S8. Returning
empty evidence in other handlers is in scope even when a handler was not an
exact `except: pass` entry. Do not mark other inventory IDs complete merely
because this batch changes their file.

## Implemented boundaries and recovery

| Rows | Integrated implementation | Evidence still required |
| --- | --- | --- |
| S1-S2 | `sleeptime_evidence` is an optional validated collection within the existing `usermem.state.v3` snapshot, so proposal acknowledgement, complete before/after rows and supersession commit together. Exact-owner manual approval/revert refuse stale edits. Quarantine uses a durable outbox and exact-owner signed target. | Windows/POSIX full suites, new owned PostgreSQL commit/rollback/lost-acknowledgement and three-process database-lock proof. |
| S3/S9 | The signed `sleeptime:scoreboard:v2` record is the counter authority. A durable cycle draft precedes signing. Supervised and scheduled preparation share a bounded cycle lock. Stable IDs retry recording/anchoring without model replay; conflicting ID reuse is refused. Empty work, missing confidence, unavailable/unsigned/contradictory evidence never graduates. Delta publication is bounded and process-serialized on POSIX. | Full platform legs; external anchoring remains optional and needs its configured sink to acknowledge when enabled. |
| S4-S6 | Exact-owner wiki pages and content-version checkpoints share one validated snapshot, published through the delivered note journal. Whole batches are checked before mutation. Complete source versions are rechecked under typed-memory and note locks. Large notes are chunked; unread chunks are not checkpointed. Owners come only from validated envelopes/notes/initialization markers. | Windows extended-path case and POSIX recovery/locking full leg; native Windows remains one process per state directory (M13). |
| S7 | Discovery persists the full result and owner/gap/destination binding before publication. Wiki or shared upgrade note, gap acknowledgement and completion receipt share one journal. Retry/recovery does not research again after preparation. Actual callers replace the temporary wiki refusal. | Platform integration and protected delivery. Live acquisition stays disabled in this workflow. |
| S8 | All prompt writers use the typed benchmark gate. Exact original/candidate bytes, benchmark manifest/results and provenance are bound to one prepared operation. Candidate evaluation is context-local; ordinary readers see the original until publication, signed evidence and report acknowledgements finish. Recovery uses that operation's backup, with stale-edit refusal. | Platform full suites, unchanged tool inventory and CI. Benchmarks here use owned fixtures; they are not real model-quality measurements. |
| S10 | CLI status/initialization/apply/revert/retry, wiki/gateway/context, supervision, reflection and heartbeat consumers use the repaired APIs. CLI unavailable/dirty outcomes exit nonzero. New sensitive recovery stores are inventoried for M09; erasure remains refused pending complete attribution. | Reviewed skips/warnings, exact 24 PR and post-merge checks, protected squash parent/tree/diff and local synchronization. |

Use the exact principal with `--user`. `olympus sleeptime state-status --user OWNER`
and `olympus wiki state-status --user OWNER` report missing, valid or unavailable
state and fingerprint unclaimed legacy data. Explicit `initialize-empty
--acknowledge-legacy` creates new empty exact-owner state; it never assigns old
normalized bytes to an owner. `sleeptime cycles-initialize --acknowledge-legacy`
starts the separate qualification authority; it does not carry an old streak.

Review `sleeptime proposals --user OWNER` before `sleeptime apply PROPOSAL_ID
--user OWNER`. `sleeptime revert PROPOSAL_ID --user OWNER` restores the recorded
rows only if neither source nor rewrite has changed. `sleeptime retry-quarantine
--user OWNER` retries pending signing/anchor acknowledgements. `sleeptime
retry-cycle CYCLE_ID` retries a durable cycle draft without models. Supervision
also accepts `sleeptime-supervise --cycle-id CYCLE_ID`; apply remains hard-off.
The existing enable and auto-apply flags, non-default signing seed and graduation
checks still govern unattended mutation; this batch does not set them.

`olympus prompt-status AGENT` identifies an interrupted operation.
`olympus prompt-recover AGENT OPERATION --decision finish` completes its prepared
decision without benchmarking again; `--decision rollback` restores its exact
original, provided there is no intervening edit. An incomplete benchmark cannot
be finished; rollback remains available. If the note journal is interrupted,
first inspect `olympus memory notes-status`, then use its exact transaction with
`memory notes-recover TRANSACTION --decision resume|rollback`. This same journal
recovers wiki batches and discovery publications. Stale targets are never
overwritten to force recovery; retain the operation and reconcile the competing
operator change. A historical completed publication may report that its target
has since changed, without restoring or repeating it.

Bounds are explicit refusals, not eviction: 500 proposals/snapshots per owner,
200 wiki pages, 8,000 characters per page, 1 MiB wiki snapshots and cycle reports,
1,000 prompt operation histories, and a 200-record retained delta window. Cycle
drafts/receipts retain retry identity outside that rolling window. Discovery
retains bounded prepared research up to 64,000 characters; a result exceeding
the 8,000-character wiki page bound stays prepared for operator review rather
than being silently truncated or regenerated. Capacity/stale/schema refusals
preserve original bytes. M09 owns evidence-aware archival, migration and erasure.

Signed local history has the documented custody limitation: the local seed is
not an independent trust root. With anchoring enabled, qualification requires
the configured sink's exact current head. No independent attestation, genuine
calibration checkpoint or comparative superiority follows from these tests.

## Validation and boundaries

Use actual entry points with owned storage and injected models/research/alerts.
Exercise punctuation, case, Unicode and long owners, malicious paths, schema
confusion, unavailable stores, malformed evidence, stale approval/revert,
lost acknowledgements, concurrent writers, cap boundaries and interruption at
each durable phase. File and supported PostgreSQL paths both need evidence;
Windows full-suite success cannot substitute for POSIX flock/fsync. No broad
unconfined cloud full suite; keep the established isolated focused route.

M13 still owns native Windows shared-state process support/launcher enforcement
and host topology; do not imply that thread tests close it. M09 owns complete
principal migration/erasure/legal holds, including snapshots and recovery bytes.
Production, genuine-data calibration/routing and matched comparisons remain
external gates after their code prerequisites. Shared installation prompts and
operator reports remain intentional scope. No deployment, publishing,
collection, learned routing, autonomy, broker or live-verifier activation is
authorized by this implementation batch.
