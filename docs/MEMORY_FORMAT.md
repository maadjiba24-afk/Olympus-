# The Memory Format — a data-sovereignty contract

Olympus's memory lives in plain files under `MEMORY_DIR`, not an opaque binary
store. That is a deliberate promise: you can **version** it, **carry it out**
whole, **grep** it, and **delete exactly what you name** — and prove each of
those, byte for byte. This document is that contract.

Contrast with a churning binary store (e.g. Ruflo's `agentdb.rvf`, a moving
alpha target): their format *is* versioned (`rvf_version`), which is the right
instinct — so Olympus versions too, but over files you can read and own.

## Layout

```
MEMORY_DIR/
  lessons/ corrections/ feedback/        shared (system-generated) notes
  reports/ upgrades/ prompt_backups/ evals/   always-shared system notes
  users/<user-id>/lessons|corrections|feedback/    per-user namespaces
  owners/<owner-key>/job_reports/        PRIVATE per-exact-owner notes
  conversations/<id>.json                persisted chat histories
```

Per-user categories keep one person's `lessons`, `corrections`, and `feedback`
out of everyone else's sessions.

### Private categories and `owner_key`

`job_reports` (a scheduled job's answer) is a **private** category. It is never
part of a shared sweep and is not in the per-user `users/<user-id>/` tree.

An ordinary `memory.search()` reads exactly **one** private directory: the
current owner's own, resolved from `current_owner()`. So the owner of a job
report finds it through the normal `recall_memory` tool and nobody else does.
`search()` takes no `owner` argument — the principal comes from the trusted
request binding, never from the caller, so the model cannot name a namespace.

The generic `save` / `recent` / `recent_titles` / `prune` / `category_count`
resolve the *normalized* ambient namespace, so they **refuse** a private
category rather than silently operating on the wrong owner. The owner-bound
`save_for` / `search_for` / `recent_for` / `count_for` / `prune_for` take the
principal as an argument; they are the trusted background/admin path for code
that has a durable owner but no request context (the heartbeat, export,
retention), and they refuse non-private categories symmetrically.

Two reasons it is not simply another `USER_SCOPED` category:

* `users/<user-id>/` is keyed by the ambient namespace, which passes through
  `safe_id` — that collapses every run of non-`[A-Za-z0-9_-]` to a single `-`
  and truncates at 64 characters, so `tg-a.b`, `tg-a@b`, `tg-a b` and `tg-a-b`
  become one directory, as do any two ids sharing a 64-character sanitized
  prefix. Fine for a path; wrong for an identity.
* `memory.owner_key(owner)` therefore keys on the **exact** principal: a
  bounded readable label for a human browsing the store, plus the complete
  SHA-256 hex digest of the exact canonical string. The label may collide; the
  digest is collision-resistant, so the pair is too.
* A missing/blank owner canonicalizes to `shared`, the legacy default used
  everywhere else, rather than minting a blank identity nobody can authenticate
  as.
* Authorization for a private read comes from `current_owner()` — an
  exact-owner ContextVar set alongside the `safe_id` path namespace by
  `memory.set_user`. `current_user()` is normalized and must never authorize
  one. The generic `save`/`recent`/`recent_titles`/`prune`/`category_count`
  refuse private categories rather than resolving them against the normalized
  namespace.
* `owners/` is a SIBLING of `users/`, not a subdirectory of a category, so any
  export/retention sweep must list it explicitly — `_memory_roots` does.

`job_reports` is deliberately **not** mirrored into `OLYMPUS_VAULT_DIR`: the
mirror is one flat folder per category with no owner dimension, so mirroring it
would pool every owner's private output back into one browsable directory.

`reports/` stays installation-global on purpose — `opportunity_scan`,
`evolution_audit`, skill curation, feature evolution and the replay gate all
write genuinely shared system notes there, and every owner is meant to find
them.

### The action store is owner-keyed too

```
MEMORY_DIR/
  actions/owners/<owner-key>/<id>.json      prepared/executed actions
  actions/owners/<owner-key>/audit.jsonl    immutable state-transition log
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
value bound by every `memory.set_user` in the ask paths and DAG worker threads,
and the one handed to prefs, vault, actions, operator and trust. Normalizing it
there defeats all of those at once, whatever the stores below do.

| Use | Read | Why |
| --- | --- | --- |
| Building a path in a `safe_id`-keyed store | `current_user()` | The store normalizes anyway; the value IS the path segment |
| Authorization, credentials, durable record ownership | `current_owner()` | `safe_id` collapses punctuation and truncates at 64 chars, merging distinct principals |
| A request boundary binding an identity from outside | `set_user(<exact input>)` | Sets both contexts, canonicalized once |
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

Several stores — `documents`, `docrag`, `todos`, `playbooks`, `emailstyle`, the
conversation search index, `assess`, `discovery` — still key on `safe_id`
internally, so handing them an exact principal changes nothing until each is
migrated the way `vault` and `prefs` were. `usage` and `gallery` are normalized
by design: they are accounting and display, not authorization.

**Equalling a `safe_id` value never proves ownership.** It is one that several
principals map to, so the principal whose exact identity matches it is as
likely to be the collider as the owner. Any store migrating to owner keys must
quarantine its legacy records for explicit operator resolution rather than
handing them to whoever matches.

## Note format and `schema_version`

Each markdown note carries a small frontmatter block, then the title and body:

```
---
schema_version: 1
created: 20260621-143000
---
# A short title

The body of the note.
```

- **`schema_version`** (`NOTE_SCHEMA_VERSION`, currently `1`) versions the note
  format itself.
- **Frontmatter-less notes are valid v0.** Every reader tolerates a note with
  no frontmatter (`parse_note` returns `({}, text)`), so memory written by older
  Olympus still loads unchanged.
- **`olympus memory migrate`** upgrades v0 notes to the current schema in place,
  preserving the body verbatim and keeping the note's original date. It is
  idempotent — already-current notes are left untouched.

`parse_note(text) -> (meta, body)` is the single split point; `note_title` and
`note_schema_version` build on it. Readers (`search`, `recent`, prompt-backup
restore, eval-score parsing) all read the *body*, never the frontmatter.

## Export / import — portable and lossless

```bash
olympus memory export --user alice --out alice.tar.gz     # one namespace
olympus memory export --all --out everything.tar.gz       # every user + shared
olympus memory export --user alice --out alice.enc --encrypt
olympus memory import alice.tar.gz
```

An export is a `tar.gz` containing:

- **`manifest.json`** — `{schema_version, created, scope, files:[{path, sha256,
  bytes}]}`. The `schema_version` here is `ARCHIVE_SCHEMA_VERSION`.
- **`data/<relpath>`** — every file, byte for byte, at its path relative to
  `MEMORY_DIR`.

Import is **safe by refusal**: it validates `schema_version` against
`SUPPORTED_ARCHIVE_VERSIONS` and raises rather than best-effort importing an
archive it doesn't understand, and rejects any tarball with no `manifest.json`.
On success each file's SHA-256 is checked against the manifest. **export →
delete → import restores the files byte-for-byte** (proven in
`tests/test_memory_contract.py`).

### Optional at-rest encryption

`--encrypt` wraps the whole archive with Fernet using the **same key
`vault.py` already derives from `OLYMPUS_SECRET_KEY`** — no new crypto
dependency. Import auto-detects an encrypted export (a plain export starts with
the gzip magic `1f 8b`; anything else is decrypted first) and fails with a clear
message if the key is wrong or missing.

## Targeted, verifiable delete

```bash
olympus memory delete --user alice                    # the whole namespace
olympus memory delete --user alice --category lessons # one category
olympus memory delete --user alice --category lessons --id 20260621-143000-foo
```

`delete_memory` **hard-deletes** (unlinks — not tombstones) and returns exactly
the relative paths it removed; the CLI lists them and, unless `--yes` is given,
requires typing `delete` to confirm. When it returns, the named files are gone
from disk — nothing else is touched.

## Retention vs. sovereignty

`prune()` (newest-N per category) and `sweep_dated_files()` (age-based) bound
*automatic* growth. The contract here is the *manual*, user-driven side: your
right to take your memory with you, inspect it, and erase it on demand.
