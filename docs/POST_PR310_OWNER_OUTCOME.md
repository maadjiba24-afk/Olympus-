# Post-PR310 owner namespaces and outcome evidence

Baseline: actual PR310 merge `9c47a984d24a8c7d8b81acfa47639b5e3428590c`,
tree `b71051d1c4c2eaefdb30e57168bc79b0e7cd67f0`. The operator's read-only
inspection confirmed unchanged local/remote main, no tracked/staged changes,
86 preserved untracked paths and no repository/ancestor AGENTS.md files.
A separate implementation checkout preserves the previous review checkout.

## Finite implementation scope

| ID | Store family | Required consumers / acceptance |
| --- | --- | --- |
| O1 | Documents and document backups | Exact-owner workspace; bounded regular-file reads; serialized preserve-before-replace writes/deletes; undo binds owner and recorded path and refuses stale rollback. CLI/tools/action handlers carry exact identity. |
| O2 | Document retrieval and ANN caches | Exact-owner chunk/cache keys; strict bounded evidence; damaged caches preserved; no silent successful rebuild over corruption; explicit unavailable context; lexical/ANN ownership and publication failure tests. |
| O3 | Todos, notes and reminders in the todo store | Exact-owner path/lock, strict schema, missing vs unavailable, complete locked read/modify/write; agenda/tool/CLI refusal and due-time validation. |
| O4 | Playbooks | Exact-owner backend keys and validated owner envelope; serialized version/status/use updates; proposal cannot race an active playbook into unapproved replacement; prompt and CLI surface unavailability. |
| O5 | Email style | Exact-owner profile and consumers; validate before provider access and again before publication; bounded guide/count/time; unavailable prompt context without exposing damaged fragments. No real email/provider access in validation. |
| O6 | Discovery gap ledger | Exact-owner default context/path/lock; strict bounded gap evidence and serialized dedup/status updates; preserve opt-in/replay exclusions. Wiki publication remains separately owner-qualified before cross-store use. |
| O7 | Action outcomes | Versioned exact-owner backend records; strict enums/counts/times; bounded serialized append; expose recording failures without claiming a completed external action failed or encouraging duplicate execution. |
| O8 | Routing outcomes | Exact-owner key/row attribution; strict bounded records; serialize append/feedback; no silent partial global aggregation; unavailable gate/selector/report/export behavior; preserve synthetic/replay exclusions and all activation thresholds. |
| O9 | Conversation search | Exact-owner search namespace and durable ownership binding; leave ambiguous legacy bindings/index intact and unclaimed; transactional index rebuild; explicit evidence errors; retention never deletes another owner's or unattributed legacy evidence. |

Across O1-O9: punctuation/case/Unicode/truncation/default-owner adversarial cases,
legacy byte preservation, invalid JSON/schema/nonfinite/oversize/nonregular
evidence, interrupted publication, transaction races and consumer behavior must
be checked. Native Windows retains one process per state directory. POSIX
cross-process/locking and Windows file-I/O coverage require both local full-suite
legs for the final source. The prior PR310 test counts do not validate this patch.

No automatic legacy attribution, live migration, repair, collection, routing,
deployment, publishing, autonomy or verifier fan-out is enabled. New exact-owner
namespaces may start empty; historical normalized bytes do not belong to whoever
happens to have the normalized spelling. Invalid existing new-format evidence is
unavailable and must not be overwritten by ordinary updates.

## Audit discoveries outside this finite patch

The repository-wide safe_id inventory also found normalized namespaces in
usermem, relgraph, sleeptime, compare, notes/builtin_actions and journey, gallery,
wiki, ctxheat, usage and some memory compatibility/retention paths. Some are
legacy inspection paths or identifiers that are not owner authority. They require
individual classification, not a mechanical safe_id replacement. Direct consumers
of O1-O9 must be fixed or refuse unsafe cross-store use in this patch; the remaining
independent stores stay explicit C4 prerequisites. This is not a claim of global
tenant isolation or completed repository-wide owner migration.

## Layout and compatibility

Documents, backups, todos, email style, document chunks and discovery gaps live
under `owners/<owner_key(exact)>/workspace-v2/`. Playbooks, action outcomes,
routing outcomes and document ANN graphs use backend namespaces `playbooks.v2`,
`outcomes.v2`, `routing_outcomes.v2` and `docrag.ann.v2`, keyed by
`storage_key(exact)`. JSON publications contain the exact owner, version 2 and
validated data. ANN graphs and their corpus signatures publish in one envelope.
The graph's node identities and embeddings must match its indicated corpus.

Search uses `search_index-v2.db`; snapshot ownership uses write-once
`conversations/<conversation-id>.owner-v2` JSON metadata. It binds both the exact
owner and exact conversation identifier. The snapshot's existing filename
normalization is retained; a conflicting exact identifier is refused. Search
rebuild validates included snapshots before replacing rows in one SQLite
transaction. Failed rebuilds leave the previous index intact.

No normalized spelling, including literal `shared`, acquires historical bytes
merely by matching an old path. Missing qualified evidence means no v2 evidence,
not proof that historical data never existed. Ordinary updates cannot repair
damaged new-format evidence. There is no automated attribution or repair tool in
this phase. In particular, resaving a legacy conversation without a qualified
binding refuses; use a new conversation identifier until an operator-reviewed
migration exists. Earlier branches, artifacts and old state remain preserved.

## Explicit implementation limits

* Discovery gap storage is implemented. Knowledge acquisition refuses before
  research/provider access while its wiki destination lacks an exact-owner
  namespace. Gaps stay open; existing wiki pages stay intact. Complete wiki
  ownership and its dreaming/usermem dependencies remain a separate C4 task.
* Principal erasure is unavailable. The remaining normalized store inventory
  cannot prove a complete exact-owner deletion map. Both dry-run and requested
  deletion report refusal, promise no deletions, preserve journals without
  tombstoning, and never certify `verified=True`. Legal holds still outrank
  deletion. Conversation sweeps report candidates but remove none through this
  blocked path. Explicit legacy inspection/export/quarantine remain separate
  operator operations; this patch performs none of them. Restoring full erasure
  requires the remaining ownership inventory and adversarial deletion tests.
* POSIX locks serialize threads and processes sharing one state directory;
  Windows retains the established single-process requirement. Backend envelopes
  do not create distributed Postgres read/modify/write transactions. Multi-host
  concurrency and genuine Postgres durability are not established by mock tests.
* File publication can be visible even if its final durability barrier fails.
  Such outcomes are unconfirmed, not automatically rolled back or retried.
  Schema validation is not a signature or proof of truthful observations; the
  state administrator remains trusted. The aggregate is a bounded complete read
  of qualified owner blobs, not a distributed snapshot across hosts.
* Routing retries with the same retained run/specialist do not mint observations;
  feedback updates the owned row. Rolling caps remain 1,000 action events and
  2,000 routing rows per owner. Retry deduplication is limited to retained rows.
  Counts and approval signals do not prove independent genuine sources, task
  correctness or production outcomes. All activation thresholds and opt-ins stay.

## Consumer disposition

Tools, specialist prompts, action document handlers and CLI keep exact owner
identity. Stored-evidence failures produce explicit tool/CLI/prompt refusal;
HTTP requests return an unavailable-evidence response instead of empty success.
Action approval/rejection/undo preserve the actual completed action status if
recording fails, and carry a warning against repeating an action to repair
telemetry. The orchestrator reports routing-record/feedback and playbook-use
failures. Offline preference export reads owner envelopes rather than treating
hashed storage keys as identities, and refuses partial evidence. Learned routing
revalidates durable evidence for each decision; a cached positive gate cannot
survive a damaged ledger. Selector fallback remains the heuristic and status
distinguishes unavailable evidence from an empty valid ledger.

## Delivery and C4-C9 checklist

| Item | Implementation / accepted limit | Remaining gate or exact blocker |
| --- | --- | --- |
| O1-O5, O7-O8 | Implemented in this review branch with owned/mock adversarial tests. | Final-source Windows and POSIX full suites, skip reconciliation and protected delivery have not run for this phase. |
| O6 | Exact-owner gap ledger implemented; unqualified wiki publication explicitly refused. | Wiki/dreaming/usermem ownership prerequisite remains; no acquisition activation. Same delivery gates. |
| O9 | Exact-owner search and immutable binding implemented; unsafe erasure explicitly refused. | Operator-reviewed legacy migration and complete exact-owner erasure inventory remain. Same delivery gates. |
| C1-C3 | Delivered by PR310 within its documented limits. | Do not reapply its patch or closure helper. |
| C4 | E1-E31 audit retained; this phase addresses only the finite scope above. | Other normalized namespaces, wiki prerequisite and erasure completeness remain source tasks. No repository-wide isolation claim. |
| C5 | POSIX shared-directory locks; Windows thread fallback and one-process limit retained. | Actual intended-host topology and launcher enforcement evidence absent; native Windows shared-store multi-process operation unsupported. |
| C6 | Existing runbook retained. | Intended host/state permissions, replacement/reboot receipts, backup/restore and rollback drills, approved monitoring destination and alert proof absent. Deployment restricted. |
| C7 | No activation or genuine data collection. | Current genuine dataset, activation authorization and real 100/250/500 checkpoint decisions absent. Historical smoke is not a current count. |
| C8 | Thresholds unchanged; unavailable evidence cannot qualify selectors. | Verified 300 outcomes, 3 task types, 2 genuine qualified sources, 25 per eligible cell including incumbent, and explicit opt-in absent. |
| C9 | Previous pinned comparison and six-fixture protocol retained. | Verified Manus/Odysseus source identities and actual matched measurements absent. No measured superiority claim. |

The package manifest and actual validation receipts, not this document's prose,
pin the final patch/tree and test results. Delivery requires both platform reports,
source/base/artifact rechecks, all 24 exact CI checks, clean mergeable head/base,
protected squash merge, actual parent/tree/diff verification and local fast-forward
synchronization. No force reset, admin override, cleanup or branch deletion.
