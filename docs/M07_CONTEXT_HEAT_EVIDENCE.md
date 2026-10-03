# M07 context heat, source attribution and recoverable pins

This candidate starts from delivered M06 main
`baf7b912aea31efd3b6bc5286678306fdb4e708f`, tree
`5daed33a9f551426cf01e45585291087effa40c2`. Implementation and owned tests do
not authorize live promotion. Delivery requires exact-source Windows and native
POSIX full suites, independent review, all applicable feature/main checks, a
protected merge and verified synchronization. Receipts must name their actual
source tree; older failures and validation scopes remain distinct.

## Acceptance matrix

| ID | Contract | Required evidence and actual consumers |
| --- | --- | --- |
| H01 | Exact, bounded owner identity | Punctuation, case, Unicode, long-prefix and traversal-shaped owners; copied envelopes; ambient exact owner; explicit blank rejection. `record`, proposal/gate/apply/rollback, status, CLI and recall use the same captured principal. |
| H02 | Missing, unclaimed and unavailable remain distinct | Flat shared and normalized-user legacy artifacts preserved; duplicate keys, nonfinite/deep/oversized JSON, missing initialized state, denial and wrong schema/owner refuse without quarantine or empty replacement. Status and doctor perform no repair. |
| H03 | Confined filesystem authority | Held-directory relative operations; regular single-link files; parent/leaf link, junction/reparse, attribute-change race, sharing denial and deep native Windows paths; native POSIX process serialization. |
| H04 | Finite bounded policy and telemetry | Closed item/provenance vocabularies, strict booleans, bounded counts/timestamps/savings, nonzero token estimates, unchanged default eligibility/budget/hysteresis/decay/churn protections. Capacity refusal preserves history. |
| H05 | Actual source attribution | Exact M02 memory source API, active status and versioned content digest. Touch does not change content identity; editing, deletion and stale already-selected recall rows cannot inherit authority. Other item kinds have no production qualification adapter. |
| H06 | Non-replaying verifier receipt ingestion | Completed owned source/run/event/owner/item/revision/verdict receipt, strict verdict, duplicate retry and conflicting replay; historical receipt recovery after source change. Recall never fabricates verifier evidence. |
| H07 | Gate eligibility cannot be supplied by callers | Recompute authoritative proposal, measured size, eligibility, budget and incumbent decisions; bind owner, initialization, source revisions, ledger, pins, policy and request digest. Strict `True` verdict; absent/unavailable/unqualified evidence never passes. |
| H08 | Serialized recoverable application | Source-before-heat lock order; durable callback reservation; callback outside locks; source/policy/state CAS; interruption and lost acknowledgement never rerun the callback. One authority publication includes pins, shadow and operation result. |
| H09 | Rollback and historical results | Durable empty-pin receipt, no unlink; rollback works with unavailable source/registry; status and CLI still expose its receipt independently of qualification. Current pins must match the latest pin-changing operation. Historical retry returns its own result and separately identifies whether it remains active. |
| H10 | Real consumers and operator surfaces | Recall preserves its static result on heat failure; actual candidate revision checked before reordering and recording. Doctor never labels self-reported reuse as activation. `ctxheat status/propose/gate/apply/rollback/receipt` uses typed results and exact owner. |
| H11 | Native validation and protected delivery | Updated policy/recall regressions, adversarial state tests and actual native filesystem tests; full standard collection on Windows and ext4 WSL, mandatory native IDs and carry-forward gallery/browser cases, explicit skip/warning review and exact-tree protected delivery. |

## Authority and finite limits

`MEMORY_DIR/owners/<owner_key(exact-owner)>/ctxheat-v2/` contains an immutable
`initialized.json`, atomic `state.json`, `store.lock` and retained `pending-*.json`
publication evidence. Heat entries, source bindings, retired revision history,
verifier receipts, pins, gate/rollback operations and bounded shadow rows share
the state document. Diagnostic `ledger_path`, `pins_path` and `shadow_log_path`
refer to that one authority.

The state limit is 8 MiB, with the reused strict JSON depth limit of 32. There
are at most 2,000 current entries, 2,000 retired entries, 10,000 verifier events,
2,000 operations and 500 shadow rows. Receipt/event identities are never pruned
to admit a replay. Shadow telemetry retains its last 500 revision-ordered rows;
it is not eligibility evidence. Counters are bounded at `2**31-1`, timestamps at
253402300799, item references at 128 characters, and source digests use full
SHA-256. Accepted receipts exactly reconcile verifier counts; rejected receipts
impose a correction floor, while additional self-reported corrections remain
valid. Publication serials bind receipt ingestion and retired snapshot cutoffs,
so A-to-B-to-A history preserves both positive and negative evidence.
The existing per-claim cost/latency limits also cap accumulated savings.
At most 64 state-directory entries and 32 MiB of pending publication bytes are
admitted for further mutation. Capacity exhaustion refuses; it does not clean.

Owner identity is an exact nonblank UTF-8 string, at most 8,192 characters and
32,768 bytes. An omitted owner uses the already captured ambient exact owner.
`shared` is explicitly the installation principal, not a normalization fallback
for invalid explicit input. The old shared flat files and normalized user files
remain unclaimed. There is no automatic claim, migration or reset in M07.

## Source, verification and qualification boundaries

The sole production placement consumer remains recall. The qualified source
adapter reads active memory through the M02 exact-owner API and hashes a versioned
projection of owner, namespace, item ID, memory type and actual content. The heat
authority stores its digest and measured token size, never memory text. A memory
touch/counter/embedding change does not alter this prompt-content revision.
Recall also compares the digest of the actual row it selected, preventing a
later reread from authorizing stale content or crediting a different revision.

Other declared kinds retain nonauthoritative telemetry and the pure selection
policy but have no production source adapter. They cannot qualify promotion.
Owned policy tests use explicit deterministic mock source adapters; real memory
and recall tests exercise the actual M02 path. Those mocks are not live evidence.

`record_verifier_outcome` ingests an already-completed receipt from trusted
in-process verifier code. A trusted source label alone no longer counts. Owner,
item, kind, source revision, source label, run ID, event ID, strict boolean verdict
and observation timestamp are retained and digested. This is integrity and
attribution, not cryptographic proof that a caller is an independent verifier.
M15/M16 broader verification/benchmark qualification remains open. There is no
new production verifier producer, model call or collection activation here.

The shipped `PROVISIONAL` value remains true; the committed registry remains
proposed and uncalibrated. Promotion requires calibrated reviewed code, a valid
current committed ACTIVE entry and a passing bound benchmark. An unreviewed
mutable retest ledger cannot activate this path. Defaults and evidence
thresholds are preserved; malformed knobs cannot lower the minimum below one
verified event. Shadow produces telemetry only. Off produces no heat writes.

## Operations and recovery

Gate/apply callers supply a stable operation ID. The reservation is published
before invoking a callback; an evaluating receipt after interruption is
unconfirmed and must not rerun the callback. A passing callback qualifies a
specific snapshot; application rechecks sources, policy and authority revision
before one publication. Rejected/failed/stale outcomes remain recorded.

Mutation and explicit retry paths confirm file/directory barriers before
acknowledging visible results. Status reads remain pure and report visible
historical receipts without claiming a new durability confirmation. A
post-replace failure returns `publication_unconfirmed`; the same operation ID
can recover its recorded result. Retry results preserve their original pins,
with a separate active flag so a later application or rollback is not relabeled.

Rollback publishes an empty pin set and its receipt without needing current
source or registry availability. It preserves prior entries, operations and
pins in historical receipts. It does not delete the authority. Pending staging
files and interrupted initialization remain preserved; missing initialized
state cannot initialize again. No recovery invokes an external provider.

POSIX uses local filesystem flock and directory fsync. Native Windows supports
one process per state root, with held-handle relative lookup/publication and
file flush. Windows process locking and power-loss directory durability are not
claimed; M12/M13 remain separate. Filesystem administrators are trusted: this
format does not provide hardware-backed anti-rollback against replacement of
the entire authority and initialization history.

## CLI and inventory

`olympus ctxheat status --owner <exact-owner>` is read-only. `propose` reports
attributed proposals. `gate` explicitly refuses when the owned benchmark adapter
is unavailable; there is no `--passed` flag or arbitrary receipt import.
`apply --operation-id <id>` consumes an existing qualified local operation;
`rollback --operation-id <id>` records unpinning; `receipt` reports that ID.
The CLI does not add live provider, collection, routing or autonomy authority.

Retention reports the exact context-heat workspace and marker/state/lock/pending
inventory. Existing exact-owner erasure refusal remains: M09 must account for
all live, retired, shadow, receipt, staging and ambiguous legacy bytes. No new
deletion or migration claim is made by this batch.
