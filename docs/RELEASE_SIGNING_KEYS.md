# Release signing keys — append-only event ledger

This file is the durable record of **which Ed25519 public key signed Olympus
releases, and when**. It exists because release-key rotation is a *flag day*:
the active trust set holds exactly one key, so a retired key disappears from
`olympus/witness_pubkey.txt` the moment its successor lands. Without this
ledger, the fact that a historical release was legitimately signed under a
now-absent key would be unrecoverable.

Retiring a key does **not** invalidate anything it already signed. A published
wheel carries its own `witness_pubkey.txt`, so `pip install olympus-council==X`
followed by `olympus verify` keeps working forever, against the key that was
active when that wheel was built. This ledger is how you check a historical
manifest *out of band* — by pinning a retired key explicitly
(`OLYMPUS_PINNED_PUBKEY=<retired key>`), never by returning it to the active
set.

## Scope

**Release signing only.** Runtime/instance signing (decision logs, per-run
manifests) belongs to a **separate deployment trust domain**, which *may* use
an instance-specific key and *may* trust several keys at once during an
overlap — see [docs/SIGNING.md](SIGNING.md). It is not necessarily a different
key from the release key; whether it is depends on how a deployment is
configured. Nothing in this file authorises two simultaneously active
*release* keys.

## The rules this ledger obeys

1. **Append only.** A new activation or retirement is a **new event row**.
   Historical rows are never edited, reordered, or deleted — not to fix a
   typo in an evidence link, not to tidy formatting. A mistake is corrected by
   appending a `CORRECTION` event that references the event number it amends.
   Event numbers run sequentially from 1, and dates never go backwards.
2. **Never more than one active key, at any point in the replay.** Replaying
   every event in order must leave exactly one key active, and must never
   pass through a state with two. `ACTIVATED` adds; `RETIRED` removes. A
   rotation appends **both** events, `RETIRED` first: the outgoing key leaves
   the active set before its replacement joins it, so the anchor is never
   momentarily ambiguous even on paper.
3. **`RETIRED` removes the key that is currently active.** Retiring a key that
   was never activated, or that another event already retired, is a malformed
   history and fails the build.
4. **A retired key is never reactivated.** The keys this ledger retires are
   retired *because* they are no longer trustworthy for signing — over-shared,
   or replaced for cause. Rotation is forward-only: an unwanted rotation is
   resolved by rotating forward again, never by re-activating a retired key.
5. **The active key is the one in `olympus/witness_pubkey.txt`.** The replay
   result and that file must agree; `tests/test_release_signing_keys.py`
   fails the build if they drift.
6. **Evidence, not assertion.** Every row cites a commit SHA, and where one
   exists, the first release tag that shipped the key. Dates are the committer
   dates of those commits in UTC — not the date the row was written. A
   `CORRECTION` amends provenance only and never alters replay state.
7. **Malformed rows are errors, not noise.** A row inside the event table that
   does not parse — wrong cell count, unrecognised event name, malformed key
   or SHA — fails the build rather than being skipped. A silently-ignored row
   is how a rewritten history would hide.

## Parse contract

There is **exactly one** event table in this file, and its rows are
**contiguous**. The table is: a header row, its `---` separator, then every
event row with no blank line, prose, or heading interrupting them. The table
**ends at the first line that is not an event row**, and nothing after that
point may look like one — a pipe-delimited event-like row appearing later in
the document is a parse **error**, not a comment and not an extra event.

A data row has exactly six pipe-delimited cells: `#`, `Date (UTC)`, `Event`,
`Public key`, `Commit`, `Evidence`.

- `#` — sequential integer from 1, never reused or renumbered.
- `Date (UTC)` — `YYYY-MM-DD`, a **real calendar date**, never earlier than
  the row above it.
- `Event` — one of `ACTIVATED`, `RETIRED`, `CORRECTION`.
- `Public key` — 64 lowercase hex characters, or `—` for a `CORRECTION`.
- `Commit` — the full 40-character SHA the event is evidenced by.
- `Evidence` — non-empty prose. For a `CORRECTION`, it must name the earlier
  event number it amends (for example, "amends event 2").

**Where new events go:** immediately after the last existing event row, before
the next heading or paragraph. Never above an existing row, and never in a
second table.

## Events

| # | Date (UTC) | Event | Public key | Commit | Evidence |
|---|------------|-------|------------|--------|----------|
| 1 | 2026-06-27 | ACTIVATED | `350f970ac5159b30f6736c124a1e468cd1cc82ddd73cb24799057c5c3b0b0336` | `74ba2144cf6f0a7305181f73e7d6ac4c111cca6d` | Introduced `olympus/witness_pubkey.txt` in "Release 0.18.0: hardening + pinned signing key + strengthened modules" (committed 2026-06-27T15:25:39Z). First release tag carrying it: `v0.18.0`. The preceding tag `v0.17.0` has no `witness_pubkey.txt` at all — release pinning begins here. Key still active and unchanged at `596f07601483268dbf6b32d8976aefc878d2a9d6`, shipped through `v0.27.2`. |

### Current state

**Current state is not restated here.** It is *derived* by replaying the event
table above — `ACTIVATED` adds, `RETIRED` removes, `CORRECTION` changes
nothing — and the single key that replay leaves active must equal the one line
in `olympus/witness_pubkey.txt`.

`tests/test_release_signing_keys.py` performs exactly that replay on every CI
run and fails the build if the two disagree. A hand-maintained copy of the
active key, or a sentence counting how many rotations have happened, would go
stale the moment a rotation lands and would have to be remembered as an extra
edit — which is precisely the drift an append-only ledger exists to prevent.
Read the table; do not restate it.

## Notes on the evidence for event 1

Established from repository history rather than recollection:

- `git log --follow -- olympus/witness_pubkey.txt` returns exactly two
  commits. `74ba2144` created the file with this key;
  `b63572c916b784351227a707f58ef634945c0acf` (2026-07-05T18:29:44Z) rewrote
  only the header comment — the key line is unchanged context in that diff, so
  it is **not** a key event and gets no row here.
- The key value is byte-identical at tags `v0.18.0`, `v0.19.0`, `v0.23.0`,
  `v0.27.0` and `v0.27.2`.
- Corroborating, non-authoritative: the repository-scoped GitHub Actions
  secret `OLYMPUS_SIGNING_SEED` reports `created_at 2026-06-27T14:23:39Z`
  (metadata only — the value has never been read), about an hour before the
  commit that pinned the derived public key. Consistent with a seed generated
  and installed immediately before pinning.

## Appending a new event

Only as part of the release-key rotation procedure in
[RELEASING.md](../RELEASING.md#rotating-the-release-signing-key-flag-day).

The rotation PR carries **two commits**, because a ledger event cites the
commit that changed the pin and **a commit cannot cite its own hash**:

- **Commit A** replaces the single line in `olympus/witness_pubkey.txt`.
- **Immediately after committing A, and before writing commit B**, read its
  SHA **and its UTC committer date** — at this moment `HEAD` *is* commit A:
  ```bash
  TZ=UTC0 git show -s --format='%H %cd' --date=format-local:%Y-%m-%d HEAD
  ```
  Save both values. `TZ=UTC0` is required: the `Date (UTC)` column is UTC,
  and a local-zone rendering can differ by a day.
- **Commit B** appends two rows here — `RETIRED` for the outgoing key,
  `ACTIVATED` for its replacement — **both carrying that same SHA and that
  same UTC date**. They record one event, evidenced by one commit; using the
  date commit B happened to be written on would misdate the history.
- **After** commit B exists, `git rev-parse HEAD~1` becomes a *verification*
  that B's parent really is A. It is not how A's SHA is obtained; before B
  exists, `HEAD~1` is the commit **before** A.

**Merge with a merge commit — never squash, never rebase.** Commit A must
remain an ancestor of `main` or the SHA these rows cite becomes unresolvable.

Do not edit event 1, or any existing row. A rotation appends; it never
rewrites.

### `CORRECTION` events

A `CORRECTION` fixes **provenance only** — a mistyped commit SHA or date in an
earlier row. It names the event number it amends in its evidence cell and
carries `—` in the key column. It **must not change which key is active**, and
it is never a way to undo a rotation: an unwanted rotation is resolved by
rotating forward again, not by correcting the ledger backwards.
