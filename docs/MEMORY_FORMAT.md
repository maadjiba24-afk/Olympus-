# File-memory ownership, format and recovery

This is the file-note contract. It is not a complete platform backup or
principal-erasure guarantee. Backend snapshots, derived journals, credentials,
legal holds and retained recovery copies require the M09/M11/M12 work tracked in
`REMAINING_HARDENING.md`.

## Layout and ownership

| Path under `MEMORY_DIR` | Meaning |
| --- | --- |
| `lessons/`, `corrections/`, `feedback/` | Explicitly shared installation notes |
| `reports/`, `upgrades/`, `prompt_backups/`, `evals/` | Shared system notes |
| `owners/<owner-key>/lessons/`, `corrections/`, `feedback/` | Private notes for the exact owner |
| `owners/<owner-key>/job_reports/` | Private scheduled-job answers; existing contract retained |
| `owners/<owner-key>/action_notes/` | Exact-owner, action-bound notes |
| `users/<safe-id>/…`, `notes/<safe-id>/` | Ambiguous legacy notes; preserved and never automatically attributed |
| `note-transactions-v2/` | Protected before/after bytes, active pointer and terminal recovery receipts |
| `note-undo-v2/`, `note-mirror-v2/` | Owner-bound undo receipts and optional-mirror status |
| `conversations/<id>.json` | Conversation snapshots; independent attribution contract |

`memory.owner_key` includes the complete SHA-256 digest of the canonical exact
principal. Case, punctuation and Unicode differences are retained. Blank/missing
owners retain the established `shared` default; they do not create another
identity. `current_user()` remains a lossy compatibility namespace and must not
authorize private operations. Use `current_owner()` and token-restoring
`user_context(exact_owner)` at request/background boundaries.

Generic save/recent/title/count/prune APIs now resolve exact owners for the
three user-scoped categories. Search combines that owner's private notes with
intentional installation-shared notes. It never reads another owner's notes.
The model-facing recall tool has no owner selector. Journey uses the actual
request owner and references containing a hash of both the path and inspected
bytes; stale references cannot delete revised content. Shared journey deletion
requires the installation operator.

`job_reports` retains the explicit `save_for` / `recent_for` / `count_for` /
`prune_for` contract: generic category operations refuse it, and `*_for`
category operations refuse non-private categories. `search_for` is the trusted
explicit-owner search adapter. Existing v0/v1 job reports remain readable because
their directory already used the full owner digest.

A private legacy store makes a not-yet-initialized exact namespace unavailable,
not successfully empty. An operator may explicitly start a NEW empty namespace:

```bash
olympus memory notes-status --user 'exact-owner'
olympus memory notes-initialize --user 'exact-owner' --category lessons --acknowledge-unclaimed-legacy
```

Initialization preserves every legacy byte, never migrates or assigns it, and
is idempotent without clearing newer notes. Each category requires its own
choice. Genuine ownership migration remains M09. Do not run this on operator
data as part of test validation.

Optional vault mirrors use `olympus-notes-v2/<owner-key>/<category>/<filename>`.
Old flat mirrors stay untouched and unclaimed. `job_reports` remains completely
excluded. The canonical note and optional mirror have separate outcomes:
`save_with_status` and the lesson tool return both, `notes-status` lists failed
mirror receipts, and `notes-retry-mirror FILENAME --user OWNER --category CATEGORY`
retries the existing derived copy without creating another canonical note.
The Path-returning `save` compatibility API also emits a warning on mirror failure.

### The action store is owner-keyed too

```
MEMORY_DIR/
  actions/owners/<owner-key>/<id>.json      prepared/executed actions
  actions/owners/<owner-key>/audit.jsonl    auxiliary transition log (not immutable)
  actions/<safe-id>/                        PRE-v4 layout, read fail-closed
```

`Action.user` and the trusted `_user` payload field hold the **exact** durable
principal. They used to hold `safe_id(user)`, and records used to live in
`actions/<safe_id>/`, so colliding principals shared one store: `pending()`
listed each other's actions and `get(user, id)` returned them, which made an
action ID a cross-owner authorization credential.

`_owned_actions` re-checks every record's stored owner against the caller's
exact principal, so a directory is a lookup **hint** and never the
authorization — a mis-filed record (a restored backup, a half-finished
migration) is not claimable by whoever's directory it landed in.

The pre-v4 directory is **not read by any per-owner API**. An earlier revision
read it when `safe_id(exact) == exact`, on the reasoning that such a caller
"cannot be a collision victim" — but a `safe_id` value is one that several
principals map to, so equalling it is exactly what the COLLIDER does.
`ACTION_BOUNDARY_VERSION` is `4`; refusing to EXECUTE a v3 record is not
sufficient on its own, because an action's title, preview and payload are
already private. Legacy records stay on disk for `pending_all()`,
`legacy_actions()` and `discard_legacy_actions()` — operator-wide inspection —
so an administrator can see what has to be prepared again.

### Other owner-keyed stores

```
MEMORY_DIR/
  store/vault/<owner-key>                   encrypted per-tenant secrets
  store/vault/shared, store/vault/operator   reserved installation namespaces
  prefs.json                                 installation-wide preferences
  prefs/system/<name>/prefs.json             other reserved namespaces
  prefs/owners/<owner-key>/prefs.json        per-tenant preferences
  users/<safe-id>/prefs.json                 PRE-v2, quarantined
  schedule.json                              jobs; owner_version stamps each
```

`memory.storage_key(owner)` is the single rule both use: a reserved
installation namespace (`memory.SYSTEM_OWNERS` = `shared`, `operator`) keeps its
literal name, and every tenant is keyed by `owner_key`. There is **no fallback**
from an owner-key to the old `safe_id` key — that would hand one principal
another's credentials, which is the merge the key exists to remove.

Tenant preferences sit under `prefs/`, deliberately **outside** `users/` and
`owners/` — the two trees `memory._memory_roots` sweeps — so autonomy levels,
granted scopes and authorized sites neither ride along in a memory export nor
get wiped by a whole-scope memory delete.

### Quarantine

`prefs.is_quarantined(owner)` is the SINGLE detection API — no other module
reconstructs the legacy path for itself, and a test enforces that. It is true
while `users/<safe_id(owner)>/prefs.json` still exists, for **every** principal
in that collision group, because the file names only the normalized value and
any of them could be its author.

While it is true, `prefs.get` returns `prefs.QUARANTINE_POSTURE` for the
security keys rather than the stored value or the ordinary default:

| Key | Quarantine value | Why not the default |
| --- | --- | --- |
| `capability_profile` | `guest` | The default `full` WIDENS a legacy `guest` |
| `autonomy` | `0` (L0) | The default L1 widens a legacy L0 |
| `scopes` | `[]` | — |
| `action_limits` | `{}`, and `actions.daily_limit` returns `QUARANTINE_DAILY_LIMIT` | A class default of `0` means *unlimited* |
| `operator` | no sites, not advanced, not enabled | A legacy authorized site must not survive unattributed |
| `earned_autonomy` | `False` | — |
| `pending_secure_login` | `None` | No password prompt may be pending |

`actions.can_auto_execute` also refuses outright, so nothing unattended or
standing runs for an unresolved identity even when a caller supplies its own
effective level. Writing a new exact preference file stores the value but does
not clear the quarantine; only `prefs.migrate_legacy` / `prefs.discard_legacy`
do. Reserved installation namespaces are never quarantined.

A quarantined credential vault is refused by every credential path but is still
scanned by the outbound secret floor for the whole collision group — see
`vault.legacy_scan`, which compares in-process and returns only a generic
reason so no secret, label, entry name or ciphertext escapes.

Pre-migration records are quarantined rather than attributed:
`vault.legacy_tenants()`, `prefs.legacy_owners()`, `scheduler.quarantined()`
and `actions.legacy_actions()` list them for an operator, with matching
`migrate_legacy` / `discard_*` calls. Nothing adopts them automatically,
because the record does not contain the information needed to do so.

### `current_user()` vs `current_owner()`

`orchestrator.Olympus` holds the EXACT principal in `self.user`: it is the
value bound during ask calls, each stream resumption, and DAG worker execution,
and the one handed to prefs, vault, actions, operator and trust. Normalizing it
there defeats all of those at once, whatever the stores below do.

| Use | Read | Why |
| --- | --- | --- |
| Building a path in a `safe_id`-keyed store | `current_user()` | The store normalizes anyway; the value IS the path segment |
| Authorization, credentials, durable record ownership | `current_owner()` | `safe_id` collapses punctuation and truncates at 64 chars, merging distinct principals |
| Scoped request or background binding | `user_context(<exact input>)` | Sets both contexts and restores the caller, including errors |
| Background work with no request context | `*_for(owner, …)` | Takes the durable principal as an argument |

### Gateway transport principals

An authenticated transport key is not itself a safe filename, and `safe_id`
must never turn it into an identity. `gateway.principal_id(key, prefix)` hashes
a domain-separated, length-framed copy of the complete exact key and encodes
the complete SHA-256 digest as URL-safe Base64:

```
email-v2-<43-character full-digest encoding>
```

The bounded prefix, version marker and digest stay below 64 characters and
contain only path-safe characters, so the conversation snapshot layer
preserves the principal verbatim. Punctuation and 64-character-prefix
collisions therefore produce different bots, conversations and exact owners.
Trusted transports may retain an old explicit id only when it is
prefix-matched, path-safe and bounded; Telegram's negative group ids are the
motivating compatibility case.

Pre-change gateway ids of the form `<prefix>-<safe_id(raw key)>` are ambiguous.
They are not automatically assigned to a new hashed principal. An operator who
can establish ownership out of band may migrate the relevant quarantined
stores explicitly; otherwise they should remain quarantined or be discarded.
Until that mapping occurs, legacy-derived owners cannot load or write vault
credentials, receive permissive security preferences, or run unattended
scheduler jobs, goals, heartbeats, web monitors or operator jobs. The records
stay on disk for operator inspection; quarantine changes authority, not
evidence.
In-flight journal v1 records are an exception only in disposition: because
they expire within 24 hours, they are dropped without replay instead of being
adopted. v2 records are stored under `owner_key(uid)` and verify that their
exact embedded uid matches the filename before replay.

PR #311 repaired its documented owner workspace/outcome stores, and PR #313
repaired typed-memory and relationship-graph snapshots. Remaining normalized
stores, including gallery and usage, are unfinished M05-M08 work; normalization
is not an accepted private-ownership boundary. See the complete store inventory
and E1-E31 dispositions in `REMAINING_HARDENING.md`.

**Equalling a `safe_id` value never proves ownership.** It is one that several
principals map to, so the principal whose exact identity matches it is as
likely to be the collider as the owner. Any store migrating to owner keys must
quarantine its legacy records for explicit operator resolution rather than
handing them to whoever matches.

## Note format and bounds

Comparison records use the independent exact-owner envelope at
`owners/<owner_key(exact-owner)>/compare-v2/state.json`. It contains bounded
answers and execution receipts, the first reveal decision, pending calibration
linkage and compact expired-comparison id/vote receipts. Tallies derive from
those decisions; there is no new `_tally.json`. The snapshot has a 64 MiB byte
bound, at most 50 detailed comparisons and 10,000 total reserved ids. Corrupt,
nonregular, owner-mismatched or unsupported state is unavailable, never empty.

Old `compares/` and `users/<safe_id>/compares/` records/tallies remain preserved
and unclaimed. Explicit empty initialization does not import them or repair a
damaged exact-owner file. M09's complete export/migration/erasure manifest must
include this snapshot, compact receipts, pending outbox, locks, legacy directories
and owner-qualified references in the global calibration chain. See
`M05_COMPARISON_RECOVERY.md`; M05 does not close complete erasure or M15.

New notes use schema version 2: strict UTF-8 Markdown with the exact fields
`schema_version`, `created`, JSON-encoded `owner_json`, `category`, full
`body_sha256` and `operation`. The digest covers the complete title/body after
normalizing CRLF to LF. Duplicate/missing fields, unknown versions, malformed
dates, nonregular/reparse paths, mismatched owners/categories and changed bodies
are unavailable evidence. Hashes detect damage; a state administrator remains
trusted and they are not signatures.

Existing shared v0/v1 notes remain readable. Unattributed notes in a NEW private
user-scoped directory are refused; old normalized notes remain unclaimed.
`memory migrate` uses preserved before/after bytes and does not invent an owner
for ambiguous legacy data. Existing private job-report v1 notes are retained
without inferring an owner from a readable path label.

Bounds: 8,192 owner characters, 512 title characters, 500,000 body characters,
1 MiB per note/file, 10,000 inventory entries, 64 MiB per side of a batch, and
192 MiB per serialized recovery plan. Archive decoding has both compressed and
expanded bounds. Refusal preserves the original evidence. Hidden staging is
bounded and not read as a note; unexplained hidden entries are unavailable.

## Recoverable publication and actions

File mutations use unique exclusive staging, file fsync, atomic replacement,
and strict POSIX directory fsync. Native Windows uses extended-length paths
without changing registry policy. It retains the documented single-process per
state-directory restriction; thread serialization is supported. M13 remains
open for native process locking/launcher enforcement and real host topology.

Before changing a batch, a private journal preserves every target's before/after
bytes. A durable active pointer makes readers/writers refuse until the mutation
completes or an operator recovers it. Recovery validates ALL targets before
changing any and refuses independently changed data. Resume/rollback has one
terminal decision; a completed decision cannot be changed or replayed over
subsequent work.

```bash
olympus memory notes-status --user 'exact-owner'
olympus memory notes-recover TRANSACTION_ID --decision resume
# Or choose rollback after inspecting the preserved targets and plan.
```

Save-note execution publishes the note and EXECUTED action record in the same
recoverable batch. Undo validates exact owner, durable action ID, constrained
canonical filename and original content hash, then publishes the deletion,
undo receipt and UNDONE action record together. An arbitrary path, another
action's result, stale content or unconfirmed absence is refused. Repeating a
confirmed undo verifies its receipt without deleting anything new.

An interruption before a note starts may leave an APPROVED record. After
journal recovery, `memory notes-retry-action ACTION_ID --user OWNER` can retry
that previously approved note through the original preview, permission, quota
and behavioral-contract gates. Terminal actions are inspected rather than
executed/count-recorded again. Auxiliary audit/outcome failure does not turn a
confirmed note into a request to repeat it. These auxiliary logs are not an
immutable signed ledger; M19 remains open for other action/evidence surfaces.

## Export and import

```bash
olympus memory export --user alice --out alice.tar.gz
olympus memory export --all --out file-memory.tar.gz
olympus memory export --user alice --out alice.enc --encrypt
olympus memory import alice.tar.gz --user alice
olympus memory import file-memory.tar.gz --all
```

Schema-2 archives contain a declared scope, complete file inventory, byte counts
and SHA-256 values, plus `data/<relative-path>` payloads. All entries are validated
before any restored target changes: schema, checksum, UTF-8 note metadata,
owner/path attribution, archive member type, duplicate/case-colliding names,
traversal, size limits and the existing optional ingestion gate. Schema-1 archives
remain supported; legacy normalized paths are restored as unclaimed evidence.
Unsigned archive metadata is not proof of genuine ownership.

The CLI requires an explicit matching owner or `--all` for import. Scoped exports
cover that owner's file-note roots; `--all` also preserves the existing
administrative file-memory roots. Neither claims complete principal backup.
Scope metadata is validated even for empty archives. Conflicts during publication
use the same preserved recovery protocol. `overwrite=False` reports existing
files skipped; success counts describe only verified restored targets.

Optional Fernet export encryption preserves the existing vault interface and
key compatibility. Password-hard KDF, key migration and rotation are M11, still
open. Wrong/missing keys refuse restoration.

## Targeted deletion and retention

The preview and effect use the same exact-owner file-note inventory and content
fingerprints. Changes after preview refuse deletion. `prune` and explicit delete
use recoverable transactions and report actual removed canonical paths; I/O
failure is not silently counted as success. Legacy ambiguous notes are untouched.

**Recovery plans retain original bytes.** These operations remove selected active
file notes; they are not whole-principal erasure, destruction of all recovery
copies, or legal-hold processing. M09 must cover the complete platform, derived
copies and backend records before those gates can close. Prompt-backup readers
accept the new unique note names and validate their bytes. M03 connects complete
prompt/benchmark/proposal recovery as described below; its delivery gates remain
separate from M04's completed platform reports.

## M03 consolidation, wiki and publication evidence

The optional `sleeptime.v1` collection in each exact-owner `usermem.state.v3`
envelope contains bounded proposals, quarantine outbox entries and full rewrite
snapshots. Existing v3 documents without the collection remain readable. The
former normalized sleeptime namespaces are unclaimed; explicit initialization
does not migrate them. Typed-memory rows and rewrite acknowledgement publish in
one file snapshot or one PostgreSQL owner transaction.

`owners/<exact-owner-key>/workspace-v2/wiki/state.json` binds pages, freshness and
content-version dream checkpoints. The note journal protects its updates, and
also joins discovery result publication with the gap acknowledgement. Prepared
discovery research is preserved under `workspace-v2/discovery/operations/`.

`sleeptime-cycles-v2/` retains stable cycle drafts/receipts; the signed v2 delta
scoreboard holds authoritative counters. `prompt-operations-v1/<agent>/` retains
exact original/candidate prompt bytes, benchmark cases/results and recovery
receipts. These files, delta histories, shared backup/report projections and
note-journal before/after bytes are sensitive retained evidence. Deleting an
active page, reverting a rewrite or restoring a prompt is not erasure. M09 must
cover every copy before whole-principal migration/erasure can be enabled.
