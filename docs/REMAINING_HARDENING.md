# Remaining Olympus hardening: master acceptance register

Reconciled 2026-09-12 against the actual PR #311 merge commit
`da69df5bdbd52eb52d2c14d09bcfa9ada56eba84`, tree
`5061f63f41b7902c6b6637e2b653c420de8d2f68`, sole parent
`9c47a984d24a8c7d8b81acfa47639b5e3428590c`.

**Overall status: OPEN.** The operator requested completion across batches,
including implementation prerequisites and real evidence. A refusal that avoids
unsafe behavior is necessary protection while a prerequisite is unfinished; it
does not complete that prerequisite. No row below becomes delivered from a unit
test, a helper existing, or a written runbook alone.

PR #312/M01 was subsequently protected-merged and synchronized at
`62b7d6dd565671220e106418052c926eefff11e0`, tree
`18e9fd1e104cc107ad2a9256215af5bb19341777`. M02 starts from this completed baseline;
the original E1-E31 baseline inventory remains a historical review inventory.

## Verified continuation baseline (2026-09-14)

PR #313/M02 is delivered at `7a96b42131707303ce5090577537e55fdfe70a6b`,
tree `81ecea4ee2200cff302125dd39ad81b15ad823b3`, sole parent
`62b7d6dd565671220e106418052c926eefff11e0`. Windows main, origin/main and
actual protected remote main matched after fast-forward synchronization. The
tracked worktree and index were clean; all other branches/tags and 86 unrelated
files were preserved. Source was obtained again in an isolated checkout using
the authentic GitHub commit object; its full Git hash and tree matched.

| M02 evidence | Actual result |
| --- | --- |
| Native Windows | Regression 592 passed / 26 skipped; full suite 11984 passed / 273 skipped. |
| Native-filesystem POSIX | Regression 617 passed / 1 skipped; full suite 12036 passed / 221 skipped / 10 reviewed warnings. |
| Owned PostgreSQL 16 fixture | Five lifecycle checks and twelve adversarial checks passed; database contention used separate process state directories. Fixture stopped and preserved. Lost-acknowledgement exception was injected after an actual commit, not measured as a network partition. |
| Protected delivery | PR #313: all 24 feature-head and all 24 actual merged-commit checks passed. Three isolation notices were reviewed and matched. Tested patch SHA256 `e74887aad45c4e7866c994d22659746f57c74c92c41d510ef50b82da004c09c0`. |
| Synchronization receipt | `local-sync-05snvar1/sync-result.json`, SHA256 `b4b6066292c1d22a4a8d1be88eca76b74cec19dfacb94dd5e82ecc34e2a4b1f4`, under the preserved Windows M02 package; recorded 2026-09-14T17:49:33.542962+00:00. |

## Verified continuation after PR #314 (2026-09-16)

M04 is delivered at `e47993f2831f76b7091fe4c897366de00ec4bb9d`, tree
`87ce4780b82ee72de14cdea8fb73b7918b4ac688`, sole parent
`7a96b42131707303ce5090577537e55fdfe70a6b`. The actual merged diff SHA256 is
`6330f9cb9ef50a0632f9845d53fd851c0b83e03bf7bc00c07997675a59c6861a`.

| M04 delivery evidence | Verified result |
| --- | --- |
| Windows | Regression 851 passed / 20 skipped; full suite 12083 passed / 278 skipped. All required native Windows path cases passed. |
| Native-filesystem POSIX | Regression 870 passed / 1 Windows-only skip; full suite 12139 passed / 222 skipped / 10 reviewed existing warnings. |
| Protected delivery | PR #314 protected squash merge; all 24 feature-head and all 24 actual merged-commit checks passed; actual parent, full tree, diff and 35-file set verified. |
| Synchronization | Windows HEAD/main/origin-main and actual protected remote main matched the merge; tracked worktree/index clean; other refs and all 86 unrelated files preserved. |
| Receipt | `local-sync-j8l62gid/sync-result.json`, SHA256 `650fdcb3c55f71a39e87aef841b9930edd91e1578180ecf6f34042a380302aee`, under `Olympus-m04full-20260915-184839-bb9ac3d1`; recorded 2026-09-16T08:02:48.828781+00:00. |

The authentic signed Git commit and full source were obtained again in a new
isolated checkout. Commit hash, tree, parent and canonical diff matched; GitHub
main independently matched. Earlier worktrees, failed attempts and artifacts
remain preserved. Historical pre-validation text in M04 is superseded by these
actual receipts, not rewritten as evidence for new code.

## Verified continuation after PR #315 (2026-09-17)

M03 and the live-quality authorization correction are delivered at
`ffd1a0b2aa3c926e4133023602190930cc54090b`, tree
`6635d459e5241152f3bc46de010294fc1a677786`, sole parent
`e47993f2831f76b7091fe4c897366de00ec4bb9d`. Combined canonical diff SHA256:
`e5158749040851d57add06da683e9a3d339c7131103d44eebe7785721bf49624`.

Windows final regression: 859 passed / 21 skipped; full: 12204 / 283.
Native POSIX regression: 873 / 7; full: 12260 / 227, with 10 reviewed warnings.
Four owned PostgreSQL 16 tests were retained only for the 18 byte-identical
storage-contract files; no new PostgreSQL run on the correction was claimed.
All 25 exact feature checks and all 25 actual merged-commit checks passed.
Protected squash, actual parent/tree/diff and synchronization were verified;
the feature branch and all 86 unrelated files remain preserved.

Synchronization receipt `local-sync-t3stsyx4/sync-result.json` SHA256:
`ce3df2fea0eb0422d34b788400fe70a9c35657fa817dce07580f77c32198f406`.
The subsequent read-only continuation inspection passed: all 85 refs, the
tracked worktree/index, all 86 unrelated file fingerprints, complete protection
object and actual remote main matched. This supersedes pre-delivery PR315 prose;
it does not turn historical test counts into evidence for later source.

**Active: M05 comparison recovery candidate, native validation/delivery open.**
See `M05_COMPARISON_RECOVERY.md` for CMP1–CMP10, storage bounds, provenance,
actual consumers, focused evidence and remaining gates. M06–M19 remain open.
The first native Windows regression exposed existing long-path preference and
backup omissions. The bounded correction and retained failed evidence are
recorded there; deep-path native revalidation is required. This does not close
M14's whole-tree recovery, consistency or actual host/backup evidence gates.
The subsequent native full suite completed with 24 failures after regression
passed and all previous failures recovered. CMP11 records the reviewed correction:
authorization and fixture long paths, backup command arguments, and bounded
calibration rename retry. The shared `atomicio` change invalidates the previous
byte-identical PostgreSQL carry-forward; fresh owned PostgreSQL evidence is now
required before M05 delivery. Existing full-suite and failed-attempt evidence
remains preserved. Broader M07/M15 calibration read/quarantine work stays open.
The cancelled Moonshot run's provider-account usage receipt is still missing;
no call-count/spend estimate or rerun replaces it. Deployment, publishing,
collection, learned routing, autonomy, broker and live-verifier restrictions
remain unchanged. M19 still includes the 299-handler inventory and wider live
workflow/evaluation callers. No operational activation is part of this batch.

## Source and evidence reconciliation

The authentic merged Git object and complete tracked source were obtained in a
new isolated checkout, preserving earlier implementation and validation trees.
Its initial worktree was clean and its commit, tree and parent matched the pins
above. GitHub main was independently read and still matched PR #311. The
operator's subsequent read-only inspection exited 0: current Windows main,
HEAD, origin/main and actual protected remote main matched PR #311, the tracked
worktree was clean, the prior feature branch was preserved, and all 86 unrelated
files matched their previous content hashes. No repository mutation occurred.

Read together with the historical documents:

- `POST_PR309_AUDIT.md`: E1-E31 and the original store inventory.
- `POST_PR309_CLOSURE.md`: delivered C1-C3 scope and remaining C4-C9.
- `POST_PR309_OPERATIONS.md`: host, backup, rollback and activation gates.
- `POST_PR309_COMPARISON.md`: pinned sources and the F1-F6 protocol.
- `POST_PR310_OWNER_OUTCOME.md`: PR #311's bounded O1-O9 implementation.
- The completed PR #310 and PR #311 operator handoffs: actual platform/CI,
  protected-merge and synchronization receipts.

Those documents are historical records and are not rewritten to suggest that
their tests covered new source. In particular, historical E24 and the old
documents/todos/outcomes/search namespace rows are superseded by delivered
PR #311 behavior. PR #311 does not close the independent namespaces below.

This register binds the scope and acceptance requirements. Detailed review of
each remaining implementation is still work, including every individually
indexed E20 handler; inventory is not a claim that all defects have been found
or fixed. A newly confirmed defect in a listed component is recorded beneath its
existing work item before that item can close.

## Status and completion rules

- **Delivered**: integrated source plus reviewed applicable platform reports,
  exact CI checks, protected merge and synchronization exist.
- **Implemented; validation pending**: the source behavior is connected and
  focused checks passed, but the delivery contract is incomplete.
- **Open code**: a required implementation, integration or recovery path remains.
- **External evidence pending**: code alone cannot establish the missing fact.
- **Intentional boundary**: a documented product/trust decision retained by the
  user's safeguards. It is not a claim of stronger behavior.

For every implementation batch, completion requires:

1. Name every affected store, direct reader/writer, background enumerator,
   CLI/API/tool surface, recovery/export/import/delete path and related guard.
2. Separate exact owners without treating a normalized legacy spelling as
   authority. Preserve ambiguous bytes until explicit attribution. Distinguish
   absence, unavailable data, denied access and a completed action whose evidence
   could not be durably recorded.
3. Validate before mutation; bound inputs/reads; prevent partial publication and
   lost updates under the supported backend/topology; surface failed durability
   barriers. Include retries, stale receipts, interrupted recovery and concurrent
   users. Multi-file changes need a transaction or a tested recovery protocol.
4. Exercise real callers with owned/mock fixtures, not only a new utility.
   Preserve meaningful existing tests and verify compatibility. Test malformed,
   oversized, nonregular/reparse/symlink inputs, punctuation/case/Unicode/long
   owner collisions, tampering, replay, timeout, crash and recovery as applicable.
5. Run applicable Windows and native-filesystem POSIX legs. Review skips and
   warnings against the required cases. Recheck base, patch, tree and changed
   paths; commit normally; push the feature; discover the actual PR; require
   all 24 exact standard checks and every applicable additional check at that head; protected squash merge; verify actual
   parent/tree/diff; require post-merge checks and fast-forward synchronization.

No force/reset/clean/prune/admin override, automatic branch deletion, secret
dump, old-patch reapplication or invocation of an old application helper on
merged main is part of this workflow. Preserve all existing evidence and
unrelated files. No previously rejected unconfined cloud full-suite rerun.

## Complete C1-C9 disposition

| ID | Current disposition | What closes the remaining requirement |
| --- | --- | --- |
| C1 | Delivered through PR #310; retained by #311 | Preserve strict assessment finding evidence, repair and consumer contracts. Two-file learning reconciliation remains the documented limit; assess it with M19 before claiming transactional learning. |
| C2 | Delivered through PR #310; retained by #311 | Preserve exact-owner knowledge and unavailable-evidence behavior. Do not reinterpret duplicate retries as a recovery mechanism for interrupted learning. |
| C3 | Delivered through PR #310; retained by #311 | Preserve corrupt advisory caches and explicit optional-feed coverage. No optional-feed activation is authorized. |
| C4 | O1-O9 delivered; repository-wide work OPEN | M02-M12, M16 and M19 as applicable: remaining ownership, consumers, evidence integrity, migration and verified erasure. E1-E31 are reconciled individually below. |
| C5 | OPEN code and host evidence | M13: genuine native Windows process locking/caps or enforced supported topology across every launcher; no claimed shared-state support from thread tests. Actual intended-host process/state inventory must match the implemented contract. |
| C6 | OPEN prerequisites and external evidence | M11-M14: identified intended host, state custody/mount/permissions, replacement/reboot receipts, backup delivery and restore/rollback drill, approved monitoring destination and observed alert. |
| C7 | OPEN evidence/integration review | M10/M15: current genuine records, integrity/qualification code, explicit activation decision, real 100/250/500 checkpoint decisions. No historic smoke count substitution. |
| C8 | OPEN code and real-data gate | M16: verified independent source qualification, at least 300 labeled genuine outcomes across 3 task types and 2 qualified sources, 25 per eligible cell including incumbent, explicit opt-in. Distinct strings are insufficient. |
| C9 | OPEN implementation and measurements | M17/M18: executable frozen-fixture protocol and native serving prerequisites; verified Manus/Odysseus identities; actual approved matched measurements and uncertainty. No superiority from source inspection. |

## Finite implementation and evidence work list

M01-M19 are the complete work groups for this continuation. Dependencies keep
later items open; finishing M01 is not a stopping point for the objective.

| ID | Scope and source finding at PR #311 | Consumers and complete acceptance | Status / dependency |
| --- | --- | --- | --- |
| M01 | E19 memory-card age; E31 liveness | `metrics.uptime_seconds`, both web health routes, operational metrics compatibility, `usermem.render_card` and the memory-card CLI. Creation age survives reinforcement; invalid/missing age is unavailable, not zero; liveness never consults storage, configuration or spend. Readiness must still reject unwritable state. | Delivered in PR #312: Windows 11945 passed/272 skipped; POSIX 11996 passed/221 skipped; all 24 PR and 24 merged-commit CI checks passed. Main synchronized at `62b7d6dd565671220e106418052c926eefff11e0`, tree `18e9fd1e104cc107ad2a9256215af5bb19341777`. |
| M02 | `usermem.events`, `.memories`, `.candidates`; `relgraph.nodes`, `.edges` still key on `safe_id`; invalid JSON becomes empty | Exact-owner validated envelopes and backend transaction boundaries; all memory operations, graph operations, retrieval/ingest, candidate approval, supersession, companion, episodic memory, web memory panel, tools and CLI. Candidate consumption must not lose a candidate if publishing its accepted memory fails; multi-document graph changes need atomicity/recovery. | Delivered in PR #313 at the verified continuation baseline above: Windows/POSIX full suites, owned PostgreSQL lifecycle/adversarial evidence, all 24 PR and 24 merged-commit checks, protected squash merge and synchronization verified. M03/M09/M12/M13 dependencies remain open; neither legacy migration nor general multi-host/Windows topology support is claimed. |
| M03 | Sleeptime proposal/quarantine/snapshot keys and wiki paths are normalized; background jobs use backend keys as principals | `sleeptime`, `supervise`, `reflection`, `wiki.dream`, `wiki.dream_all`, heartbeat, orchestrator wiki context, gateway/CLI wiki and discovery publication. Exact owner enumeration and context propagation; strict proposals/snapshots; preserve-before-rewrite; atomic/recoverable approval and revert; no graduation from unavailable evidence. Restore discovery acquisition only after real ownership and consumer tests pass; keep acquisition opt-in. | Delivered in PR #315 with the scoped platform, PostgreSQL carry-forward, all 25 feature and 25 post-merge CI, protected delivery and synchronization evidence above. S1-S10 in `M03_SLEEPTIME_WIKI.md` connect proposal/wiki/cycle/prompt/discovery recovery and actual callers. The temporary wiki publication refusal is replaced by the qualified path; acquisition stays opt-in. |
| M04 | Normalized `memory.USER_SCOPED` lessons/corrections/feedback, separate `notes/<safe_id>` action store; journey uses normalized readers | `memory` save/read/search/recent/count/export/import/delete/mirroring, `builtin_actions` note execute/undo, journey list/show/remove, CLI/tools, wiki material ingestion. Exact private ownership alongside explicitly shared system categories. Unique bounded atomic note writes; undo binds owner, path and content and refuses stale/arbitrary-path deletion. User export/delete cannot claim normalized legacy bytes. | Delivered in PR #314 with Windows/POSIX full suites, all 24 PR and 24 merged-commit checks, protected merge and synchronization verified above. N1-N9 limits remain in `M04_PRIVATE_NOTES.md`; full erasure, native Windows process topology and external evidence remain open. Shared installation notes remain intentionally shared; this is not permission to share private notes. |
| M05 | `compare` response records and vote tally use normalized paths, loose reads and separate updates | CLI compare/reveal, web compare/reveal/tally, calibration comparison linkage. Exact identities for owner, comparison, actual executed models and runs; bounded data; reveal retry cannot double vote; durable records/tally/calibration failure recoverable without rerunning model calls. | Integrated M05 candidate; focused owned tests passed under kernel IP denial. Native full suites and protected delivery remain open. CMP1-CMP9 in `M05_COMPARISON_RECOVERY.md`; real model execution remains restricted. |
| M06 | Gallery owner uses normalized ambient identity; legacy visibility infers ownership from login being disabled | `gallery`, `media.generate/edit`, web gallery read/delete, CLI and explicit legacy claim. Exact write/read ownership; no ambiguous automatic legacy exposure; qualified claim with preserved source and conflict handling; safe image bounds/type/path, atomic publication and stale-delete protection. | Open code. Shared sandbox file-tool workspace is an explicit separate trust boundary; do not claim tenant confinement for it. |
| M07 | `ctxheat` labels/paths normalize user identity; heat, pins, shadow and gate evidence need a common contract | `recall` retrieval instrumentation and policy selection, ctxheat CLI/status/gate/apply/rollback. Exact owner throughout; strict finite/bounded heat and pins; unavailable evidence cannot qualify promotion or reset history; serialized/recoverable updates and complete source attribution. | Open code. Keep default-off promotion and existing evidence thresholds. |
| M08 | `usage` fairness/session/spend owner keys normalize/truncate; E4 pricing tables and updates disagree | `usage._resolve_key`, session totals, admission, budget checks, `modelgate`, assessment cost reporting, metrics/CLI, config pool pricing and `providers.fetch_pricing`. Exact identity distinct from display labels; one validated versioned input/output price source, explicit stale/unknown estimates, deterministic matching, preserved historical costs and consistent budgeting/routing. Refresh only through an approved provider path; malformed/negative/nonfinite prices must not weaken caps. | Open code. Include concurrency, unavailable ledger and cost reporting in M19. |
| M09 | Principal erasure and retention are currently blocked; legal-hold and legacy/export/delete attribution remains incomplete | Complete manifest covering every file store, KV namespace, search/cache, conversation/session/ACE record, artifact and derived journal. Exact legal holds; read-only dry-run matches real effect; explicit reviewed legacy ownership; conflict-preserving migration; recoverable deletion journal; verify by rereading all targets/backends, never certify on errors. CLI/API/sweeps must use the same authority. | Open code. Requires M02-M08, M11-M13 and classification of already-delivered stores. Refusal alone does not close erasure. |
| M10 | E3 `modelgrade.observe` has no production caller; its append path lacks an explicit fsync | Connect independently established verifier/evaluation results to exact model/revision/config/task cell and source/run identities. No grading from a model's own claim, no duplicate retry observations, no fake success from completion. Durable append/repair and consumers must agree on missing/unavailable evidence, freeze and qualification; all-off behavior remains inert. | Open code. Requires M15/M16 evidence semantics. Real observation count is external. |
| M11 | E17 vault uses unsalted SHA-256 passphrase derivation and unlocked whole-value RMW | Versioned salted password-hard encryption format; bounded authenticated parsing and resource limits; old ciphertext readable without silent migration; explicit verified migration/key rotation with preserved recovery copies; atomic serialized put/delete; all vault/OAuth/cookie/config-secret and backup encrypt/decrypt/restore callers covered. Do not silently change signing identities. | Open code. Crypto format/KDF requires primary documentation and compatibility fixtures; operator secrets never needed for implementation. |
| M12 | E23 Postgres has no executed live contract proof; local proclock does not serialize multiple hosts | Backend-native transaction/locking API connected to every required RMW caller, unique atomic file writes, deletion durability and bounded reads. Owned disposable Postgres tests for namespace bytes, missing/read/write/delete/enumeration, contention, rollback, restart and failure. Backend errors must reach consumers. CI leg must execute, not merely collect skipped tests. | Open code and owned real database validation. Do not claim that KV transactions move filesystem journals or web session state into Postgres. |
| M13 | C5 POSIX flock versus Windows thread fallback; process-local web quota/session/metrics state | Native Windows lock/slot implementation or complete single-process lease enforcement on every state-sharing CLI/web/heartbeat/gateway/MCP entrypoint; state-root-aware lock identities, reentrancy, timeout, process-death release and spawn tests. Preserve POSIX flock/fsync guarantees. Enforce the supported topology at launch; test refusal/recovery. | Open code and actual host inventory. E11 HA is not silently enabled; supporting concurrent store writers does not provide distributed sessions/rate limits. |
| M14 | C6/E15/E22 production receipts/config exist but actual operational proof is absent | Review/finish bounded validated lifecycle receipts, identity binding, permissions on supported hosts, backup delivery/restore/rollback, monitor exporter/alert path, and operator CLI diagnostics. Named readiness must distinguish missing/stale/unavailable receipts. Do not claim Windows mode bits prove ACL custody. Verify intended-host topology and external approved alert delivery. | Open prerequisite review, then real host evidence. Host, state mount, backup destination and monitoring destination/authorization missing. |
| M15 | C7 calibration chain/checkpoint code exists; current genuine trial not established | Review durable attempt versus persisted counts, incomplete-tail/corruption recovery, replay/synthetic/duplicate exclusion, model/source/config binding and feedback hierarchy across orchestrator/compare/CLI/API. Checkpoint decisions require valid current dataset plus thresholds and pause rules, not a displayed target count. Explicit opt-in, disable/rollback and no sensitive plaintext remain tested. | Open integration review; actual dataset and 100/250/500 human decisions missing. Two historical smoke records are not a current count. |
| M16 | C8 currently equates distinct `user` strings with distinct real sources | Explicit independently qualified source identities with operator evidence, bounded/versioned bindings and revocation; selector, gates, aggregation, export/status and feedback use that qualification. Aliases/self/synthetic/replayed/stale or damaged qualification cannot mint eligible sources/cells. Retain 300/3/2/25 and incumbent requirements and explicit opt-in. | Open code. Genuine outcomes/source qualification and activation authorization remain external after tests pass. |
| M17 | E9 `NativeForecaster` construction remains confined to tests; E10 synthetic results do not qualify quality | Configured checkpoint resolver connected to ForecastService/native serving, exact artifact/schema/task identity, verified promotion state/restrictions/expiry and human authorization. Abstain on unavailable/unqualified artifacts; preserve baselines, rollback and kill-switch restrictions. Owned offline fixture must reach the actual serving entrypoint. | Open integration. Real held-out data, baseline comparison, promotion receipt and any deployment authorization missing. No broker/live-trading activation. |
| M18 | C9 F1-F6 comparison protocol is written; actual matched execution and two product identities absent | Runnable owned fixtures and product adapters for forms/browser state, artifacts, replacement/recovery, injected/unavailable/owner-collision evidence, procedure holdouts and memory attribution. Freeze source/models/prompts/resources/OS; record every attempt, timeout, cost and artifact; paired analysis and uncertainty; at least 20 paired trials per agreed product/fixture when authorized. No silently skipped failures or inferred superiority. | Open harness/adaptor review/implementation; Manus/Odysseus exact identities, approved runtime access/budget and actual measurements missing. |
| M19 | E20 silent error handling plus multi-file evidence recovery across the listed scope | Review every indexed baseline handler in `REMAINING_E20_INVENTORY.json` and every unavailable/defaulting path in M02-M18. Classify each as an intentional cleanup/optional boundary or a defect with connected error/recovery behavior and a test. Positive authorization/evidence cannot be silently fabricated; failed telemetry cannot repeat a completed action. Document accepted cleanup limits individually. | Open review: 299 exact `except: pass`-body handlers in 125 files, not 299 confirmed defects. No row is pre-approved by the count. |

## E1-E31 reconciliation

| ID | Current source / disposition | Remaining acceptance or retained boundary |
| --- | --- | --- |
| E1 | Bounded rejection rework and post-synthesis UNVERIFIED are implemented; ADR 0005 intentional boundary | Preserve conspicuous degradation and test it. The verdict is not guaranteed factual truth. Genuine reliability is M15/M18; no live verifier fan-out. |
| E2 | Router exemptions are explicitly ledgered by `_record_verify_exempt`; intentional boundary | Preserve exemption visibility and independent interactive verification. No claim that every answer is independently verified; M19 reviews evidence-recording failures. |
| E3 | OPEN: `modelgrade.observe` has no production caller | M10: connect qualified independent outcomes and durable semantics, then obtain genuine observations. |
| E4 | OPEN: split static/live pricing and inconsistent model matching | M08: coherent versioned price semantics and runtime refresh with explicit uncertainty and preserved historical costs. |
| E5 | Ingest checks are wired but default-off by existing rollout policy | Preserve defenses/tests; M14/M15 must record actual opt-in/soak before an enabled-deployment claim. Default-on activation is not authorized now. |
| E6 | Scope owner/corruption/mutation controls delivered; host state administrator remains trusted | Unsigned authorization JSON is not cryptographic proof against a malicious state administrator. Preserve exact-owner and scope enforcement; do not claim otherwise or add a new root-of-trust promise by renaming a file. |
| E7 | Propose-only code evolution and offline preference scaffold are intentional product boundaries | No autonomous code application, live model-weight training, payment rail or distributed consensus is being smuggled into hardening. Preserve ADR 0003/NORTH_STAR_REVIEW cuts. |
| E8 | Project-selective codegraph build/update/watch are intentional opt-ins | A graph-dependent claim requires actual selected-project identity, successful build/freshness and explicit unavailable context. Review those consumers in M19; no universal automatic index claim. |
| E9 | OPEN: native adapter exists, configured construction/serving route unproven | M17, with genuine artifact/promotion receipts separate from fixture integration. |
| E10 | OPEN real-data quality gate | M17/M18: leakage-resistant held-out real data and matched baselines. A synthetic smoke score cannot close this row. |
| E11 | Single-instance process-local sessions/metrics/quota are an intentional supported-topology boundary | M13/M14 must enforce and prove the actual supported topology. Postgres KV or native process locks alone do not confer HA. Multi-replica availability is not claimed. |
| E12 | Local shell backend explicitly lacks OS isolation; confinement backends are separate | Preserve explicit confinement and approval gates. M14 validates the selected deployment backend. Do not represent application-level egress checks as kernel confinement. |
| E13 | Delivered: absent WhatsApp secret refuses signatures | Preserve signature tests in affected channel batches. No live webhook required. |
| E14 | Delivered: webhook loopback/credential/owner startup guards | Preserve gateway owner and off-loopback tests. No exposed listener is authorized. |
| E15 | Delivered code for explicit durable-state configuration; external gate OPEN | M14 actual intended-host state/mount/permissions and survival evidence. |
| E16 | Auxiliary `actions._audit` JSONL is intentionally distinct from the signed decision ledger | M19 reviews failures and claims; no immutable/signed guarantee for this auxiliary file. The signed approval/evidence path remains mandatory where required. |
| E17 | OPEN: weak passphrase KDF and migration prerequisite | M11 includes existing ciphertext/backups and all secret consumers, not only an encryption utility. |
| E18 | Separate mandate subkeys may share a custody root; intentional trust distinction | Independent human-custody claims require independently managed keys and validation in M14. Two derived keys do not prove two independent parties. |
| E19 | Delivered in M01 / PR #312 | Preserve real stored creation age, unavailable invalid age, reinforcement independence and CLI coverage; actual platform/CI/merge/synchronization evidence is recorded in M01 above. |
| E20 | OPEN: historical count replaced by a pinned per-handler inventory | M19 and error/recovery cases in every store batch. Broad logging alone is not the fix for invalid positive evidence or lost updates. |
| E21 | Delivered: finite default budget and safe malformed-input behavior | Preserve explicit zero-as-unlimited semantics, never describe zero as disabled. M08 must not weaken admission/cost caps. |
| E22 | Delivered: production boot/config checks exist | M14 actual deployment proof remains OPEN; a passing config unit test is insufficient. |
| E23 | M02 owned PostgreSQL lifecycle/adversarial validation delivered; broader backend gate OPEN | M12 still requires remaining RMW callers, general backend failure/recovery and an executing PostgreSQL CI contract. M02 evidence is scoped to typed memory/graphs, not every store. |
| E24 | Delivered by PR #311 O7/O8; older audit row superseded | Strict exact-owner outcomes and explicit unavailable evidence, including completed-action warnings, must remain. Do not reapply the previous patch. |
| E25 | Delivered: replay/reliability distinguishes unavailable evidence from regression | Preserve skip/inconclusive semantics and no-model refusal; live verifier execution remains separately restricted. |
| E26 | Delivered: signed/shipped package-data surface aligned | Preserve package/release verification tests. No publishing authorized by this phase. |
| E27 | Delivered: independent anonymous-IP allowance ceiling | Preserve counter semantics. Distributed enforcement is not established by this single-instance fix. |
| E28 | Delivered: `/v1` rate-limit path | Preserve guards and verify actual proxy/peer assumptions in M14 before deployment claims. |
| E29 | Delivered: validation precedes quota consumption | Preserve rejected-request and malformed-settings cases. |
| E30 | Delivered: atomic single-instance daily check/consume | Preserve concurrency cases. Supported topology remains M13/M14. |
| E31 | Delivered in M01 / PR #312 | Preserve storage-independent HTTP liveness; readiness still returns 503 for unwritable state and spend reporting remains on the operational endpoint. |

## Legacy data, recovery and intentional boundaries

Migration is a real requirement (M09), not a reason to automatically attribute
ambiguous data. It must bind an operator's explicit source-to-owner decision,
source hash and destination identity; preserve source until verified publication;
refuse conflicts; report interruption precisely; and allow safe resume without
duplication. Normal reads must never silently perform it. The same protocol must
cover affected exports and deletion inventory, including legacy conversation
bindings and previously isolated vault/preferences/action stores as applicable.

Installation-wide shared notes/skills, explicit shared sandbox workspace,
propose-only code evolution, offline preference training, explicit visible
verification degradation and a trusted state administrator remain documented
boundaries. They must not hide defects in private stores or claims of absent
protections. Single-instance scope does not excuse failure to enforce that
scope. Default-off evidence gates do not excuse missing production callers.

## Historical M01 pre-validation evidence

The following is the preserved pre-delivery record, not current gate status.
M01 was subsequently delivered in PR #312 and M02 in PR #313 as recorded above.

M01 modified `olympus/metrics.py`, `olympus/web.py`, `olympus/usermem.py` and
added `tests/test_remaining_closure_foundations.py`, plus this register,
the pinned E20 inventory and the changelog entry.

The final 2026-09-12 isolated focused run used Python 3.12.14, kernel-denied
network, owned state and a separate environment populated from preserved
installed test dependencies. It ran the new tests plus
`test_usermem.py`, `test_hardening_round3b.py`, `test_companion_evidence.py` and
`test_phase5_recovery.py` and the three metrics collector tests: **98 passed**,
no skips, in 3.39 seconds. Dependency consistency, prerelease, compile, import,
capability and threat-model guards all exited 0. The new HTTP
tests exercise request parsing/dispatch/serialization through an owned in-memory
connection, not a mock of the health response. They detect attempted ledger
reads even if an exception is swallowed. The CLI test reads a real fixture memory.

An earlier attempt could not launch the old validation environment's missing
interpreter link. Its files were preserved; the working runtime used the existing
installed dependencies read-only. The first actual focused run found a fixture
missing the metrics authentication credential (401); the fixture was corrected
to supply a real test token, without weakening the endpoint. The initial 94-test
run passed. A subsequent oversized-integer timestamp check was added and its
overflow handling corrected before the final run above. These are focused
results, not fresh hash-locked platform or full-suite receipts.

Windows/POSIX full validation, exact-source guards, new patch packaging, reviewed
commit/push/PR, 24-check CI, protected merge and synchronization are **pending**.
No source mutation has been performed on the operator's Windows machine by this
batch. No external operation or feature activation has been performed.

## Exact external dependencies and next operator actions

| Dependency | Concrete next action when its code prerequisite is ready |
| --- | --- |
| Current Windows worktree | PR #313 synchronization verified at `7a96b42131707303ce5090577537e55fdfe70a6b`, clean tracked worktree/index and 86 unrelated files preserved. Recheck before the next guarded branch/application step. |
| Windows/POSIX batch validation | Review the new pinned package, use the verified Windows Python 3.12 interpreter and a new private environment; validate the identical source in new WSL/ext4 directories. Preserve all prior runs. |
| Intended deployment host | Identify the intended host, OS, service/container topology and persistent state mount; provide read-only identity/permission evidence before any lifecycle or deployment action is proposed. A dev checkout is not that proof. |
| Backup/monitoring | Identify the independent backup destination and approved monitoring destination; review a concrete restore/rollback drill and alert test before authorization. Do not send an alert or reboot an unidentified host. |
| Genuine calibration | Supply the existing current dataset or its non-sensitive signed/hashed report for inspection after custody/gates are ready. If none exists, approve a concrete collection plan before activation; use naturally occurring authorized tasks. |
| Learned routing | Qualify two genuinely independent sources and collect the required real labeled distribution; review checkpoint/cell evidence and explicitly opt in only after all gates pass. |
| Native quality | Supply an authorized real held-out dataset and a qualified checkpoint/promotion record; owned synthetic tests cannot establish a model advantage. |
| Competitor measurements | Identify the exact Manus/Odysseus product/repository/version and available access; approve the frozen runtime/model/resource budget and owned F1-F6 fixtures before measured runs. |

The objective remains open until every required implementation row is delivered
and each applicable external gate has its actual acceptance evidence. Any
remaining intentional boundary must be stated with the narrower behavior it
supports; it must never be relabeled as a completed stronger capability.


M04 validation correction, 2026-09-15: the original native Windows attempt
stopped at 11 directory-path containment failures (667 passed, 20 skipped); its
full suite never started. The directory API compatibility defect is corrected
without changing those assertions. The latest isolated run is 696 passed with
one native-Windows skip. Corrected native Windows/POSIX full suites and protected
delivery remain open; every earlier failed run is retained.


M04 full-suite reconciliation, 2026-09-15: the path-corrected Windows regression
passed (686 passed, 20 skipped); its full suite stopped at 8 failures (12,072
passed, 278 skipped). The eight failures reproduced under kernel isolation.
Missing settings documentation is fixed, and mirror/context/legacy tests now
verify the intended exact-owner contracts without dropping assertions or hiding
errors. The expanded isolated run passed 861 tests with one native-Windows skip.
A new forward-only package preserves both earlier attempts. Corrected Windows
and POSIX full-suite gates plus protected delivery remain open. The explicit
M09 erasure/migration refusal and all other M03/M05-M19 work remain unresolved.
