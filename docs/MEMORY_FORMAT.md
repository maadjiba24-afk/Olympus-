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
  conversations/<id>.json                persisted chat histories
```

Per-user categories keep one person's `lessons`, `corrections`, and `feedback`
out of everyone else's sessions.

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
