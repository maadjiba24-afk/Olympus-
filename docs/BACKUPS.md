# Data backups & disaster recovery

A public Olympus instance keeps everything irreplaceable on one machine: per-user
memory, accounts, the **encrypted OAuth tokens** that reach people's Gmail and
Calendar, and the signed decision log. If that machine dies, it is gone. This is
how to make a recoverable copy and get it **off the machine**.

## What a backup contains

`olympus backup` archives `OLYMPUS_MEMORY_DIR` into a single `.tar.gz`, with a
manifest recording every file's SHA-256. Included by default:

- `accounts/` — usernames + hashed passwords
- `usermem/`, `profile/`, `playbooks/`, `relgraph/` — per-user memory
- `vault`/credential store — OAuth tokens (already encrypted)
- `traces/` — the signed decision log
- `reports/`, `errors/`, `usage/` — operational records

> **Backend caveat.** `olympus backup` archives the `OLYMPUS_MEMORY_DIR`
> filesystem tree. When `OLYMPUS_DATABASE_URL` is set, the vault and per-user
> memory are stored in Postgres (`store.backend()`), **not** under the memory
> dir — so they are **not** in the archive. Back the database up separately.

Excluded by default (large and **reproducible** — losing them only forfeits
replay of *old* runs, never user data): the replay caches `responses/`,
`tool_results/`, `context/`. Pass `--full` to include them.

## How it's hardened

- **Encrypted before delivery.** With `OLYMPUS_SECRET_KEY` set, the archive is
  encrypted (Fernet / AES-128) before it is delivered off the machine — the
  off-machine copy is not plaintext PII or tokens. (The plaintext `.tar.gz` is
  written to a temp file under the backups dir and encrypted from there, so it
  briefly touches local disk before delivery.) Delivering an *unencrypted*
  archive off-droplet is **refused** unless you opt in with
  `OLYMPUS_BACKUP_ALLOW_PLAINTEXT=1`.
- **Tamper-evident.** The archive's SHA-256 is signed with the same Ed25519
  witness key Olympus uses for releases (`<archive>.sig.json`). Restore verifies
  the signature and refuses an altered archive **when the signature is present**.
  A Fernet-encrypted archive is additionally HMAC-authenticated, so tampering
  fails regardless. For a plaintext archive whose `.sig.json` sidecar is missing,
  restore proceeds on manifest hashes alone — keep the sidecar to retain the
  authenticity guarantee.
- **Integrity-checked on restore.** Every file's hash is re-checked against the
  manifest; a corrupt archive fails loudly instead of restoring garbage.
- **Safe extraction.** Path-traversal (`../`), absolute paths, and links in an
  archive are rejected, so a crafted archive can't write outside the target.
- **Atomic + bounded.** Archives are written atomically (no half-file is ever
  visible), the backups dir is never backed up into itself, and old local
  archives are pruned to `OLYMPUS_BACKUP_KEEP` (default 7).
- **Fail-safe & alerting.** A failed scheduled backup is captured for the
  operator (`olympus errors`, Telegram) and never crashes the heartbeat.
- **Evidence-backed readiness.** A completed attempt records whether that exact
  archive was encrypted and signed and whether both the archive and Ed25519
  signature sidecar were accepted by the off-machine delivery command.
  Local-only success remains useful, but cannot satisfy deployment readiness.

## Set it up (off-droplet delivery)

A backup that stays on the droplet protects against data corruption but **not**
against losing the droplet. To get it off the machine, bring any uploader and
point `OLYMPUS_BACKUP_CMD` at it — `{path}` is replaced with the archive path:

```bash
# in deploy/.env
OLYMPUS_SECRET_KEY=<a strong, STABLE secret>          # also encrypts the vault
OLYMPUS_BACKUP_CMD=rclone copy {path} spaces:olympus-backups/
# OLYMPUS_BACKUP_CMD=aws s3 cp {path} s3://my-bucket/olympus/
OLYMPUS_BACKUP_EVERY=86400        # daily
OLYMPUS_BACKUP_KEEP=7
```

Olympus never sees your storage credentials — they live in the uploader's own
config. `OLYMPUS_SECRET_KEY` **must stay constant**: it's the key to both the
vault and the backups; lose it and old encrypted backups can't be restored.

### Running it on a schedule

The backup makes **no model calls**, so you don't need the (token-spending)
heartbeat. `deploy/docker-compose.yml` has a dedicated, commented-out `backup`
service that just loops `olympus backup` — uncomment it, or run it from host
cron:

```bash
0 3 * * *  docker compose -f /path/deploy/docker-compose.yml exec -T web \
             python -m olympus backup
```

If you already run the full `heartbeat` service, scheduled backups happen there
automatically on `OLYMPUS_BACKUP_EVERY`.

## Verify and restore

```bash
olympus backup            # make one now (and deliver if OLYMPUS_BACKUP_CMD is set)
olympus backup --list     # local archives
olympus backup --drill    # restore newest archive into an isolated temp dir
# olympus backup --drill /path/to/a/specific/archive.enc

# Restore into a fresh instance (refuses to clobber a non-empty dir):
olympus restore olympus-backup-YYYYMMDDThhmmssZ.tar.gz.enc --into /app/memory
```

Restore needs the **same `OLYMPUS_SECRET_KEY`** that created the archive (to
decrypt) and verifies the signature against the witness key. To restore over an
existing instance, stop it and pass `--force`. `--insecure` bypasses the
signature/hash checks — only for a last-resort recovery of a known-good archive
whose signing key you no longer have.

### Drill it

A backup you have never restored is a hope, not a backup. `backup --drill` uses
the normal strict restore implementation (signature, safe extraction, manifest
hashes), targets an automatically deleted temporary directory, and records a
receipt bound to the archive SHA-256. It never touches live `MEMORY_DIR`.

For the complete container-replacement, host-reboot, disk, permission, backup,
and recovery gate, see [Deployment durability readiness](DEPLOYMENT_READINESS.md).
