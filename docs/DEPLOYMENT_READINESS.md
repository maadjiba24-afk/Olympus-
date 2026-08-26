# Deployment durability readiness (P2A)

P2A is the gate between “the process starts” and “this installation is ready to
carry durable user state.” It does **not** enable calibration, adaptive routing,
autonomous expansion, or any live collection campaign.

`/healthz` remains liveness. For `OLYMPUS_ENV=staging` or `production`,
`/readyz` is fail-closed until `olympus deployment status` proves every item:

1. `OLYMPUS_MEMORY_DIR` explicitly names a real mount point.
2. The mount is owned by the running UID and has no group/other permission bits.
3. A write + file fsync + atomic rename + directory fsync succeeds.
4. Free space exceeds `OLYMPUS_MIN_FREE_BYTES`.
5. `OLYMPUS_BUILD_COMMIT` is an exact 40-character lowercase Git SHA.
6. The evidence stored on that mount survives a container replacement.
7. The evidence stored on that mount survives a host reboot.
8. A recent backup was encrypted and signed, and both archive and signature
   sidecar were accepted by the configured off-machine delivery command.
9. That exact archive was restored through the strict restore path into a
   throwaway directory.

Evidence lives under `MEMORY_DIR/deployment/`. The directory is mode `0700` and
receipts are mode `0600` on POSIX systems. Writes are atomic and fsynced.

## Before starting

Set the exact image revision and configure an uploader in `deploy/.env`:

```bash
cd Olympus-
export OLYMPUS_BUILD_COMMIT=$(git rev-parse HEAD)
cd deploy

# edit .env:
# OLYMPUS_BUILD_COMMIT=<the exact value above>
# OLYMPUS_SECRET_KEY=<stable strong secret>
# OLYMPUS_BACKUP_CMD=rclone copy {path} <off-machine destination>/
# OLYMPUS_BACKUP_EVERY=86400
```

Production compose pins `OLYMPUS_ENV=production` and
`OLYMPUS_MEMORY_DIR=/app/memory` structurally. An env-file swap therefore cannot
silently downgrade the service to dev or move durable state inside the image.

For staging, use a **staging-only** key and destination and set
`OLYMPUS_STAGING_ALLOW_BACKUP_CMD=1`. Never point staging at production storage.
Run the same sequence with
`docker compose -f docker-compose.staging.yml` and service name
`olympus-staging`; staging has no Caddy or heartbeat dependents.

## First bring-up: expected not-ready state

```bash
docker compose up -d --build olympus
docker compose exec olympus python -m olympus deployment status
```

The container should be alive but not ready. Caddy remains gated until the
durability receipts are complete. The unattended heartbeat is behind the
separate `autonomy` profile and remains disabled for P2A. `docker compose exec`
still works against the running app container.

## Prove container replacement

```bash
docker compose exec olympus \
  python -m olympus deployment challenge container

docker compose up -d --force-recreate --no-deps olympus

docker compose exec olympus \
  python -m olympus deployment verify container
```

Verification refuses a mere process restart because the container identity must
change. The challenge must still be present on the mounted volume after the old
container is gone.

## Prove host reboot

```bash
docker compose exec olympus \
  python -m olympus deployment challenge host

sudo reboot
```

After the host returns and Docker restarts the service:

```bash
cd /path/to/Olympus-/deploy
docker compose exec olympus \
  python -m olympus deployment verify host
```

Verification compares Linux boot IDs, so stopping and starting only the
container cannot satisfy the host test.

## Prove backup delivery and recovery

```bash
docker compose exec olympus python -m olympus backup
docker compose exec olympus python -m olympus backup --drill
```

The backup receipt records what actually happened. A local-only, plaintext,
unsigned, missing-sidecar, or failed-delivery result remains not-ready. The
uploader is invoked once for the archive and once for `<archive>.sig.json`. The
drill selects the newest archive unless a path is supplied, restores it into an
automatically deleted temporary directory, verifies the signature and every
manifest hash, and records the archive SHA-256. It never writes into live
`MEMORY_DIR`.

## Final gate

```bash
docker compose exec olympus python -m olympus deployment status
curl -fsS https://caelarion.com/readyz
```

Do not route traffic or start unattended services until the status is `READY`.
Receipts are build-bound and expire; a new image or stale evidence deliberately
returns the deployment to not-ready until the relevant proof is repeated.

## PostgreSQL is deliberately blocked

The current Olympus archive covers the `MEMORY_DIR` filesystem. When
`OLYMPUS_DATABASE_URL` is set, private memory and vault data live in PostgreSQL
and are outside that archive. P2A therefore fails `database_coverage` rather than
claiming a recoverable deployment. Add an independently verified database
backup/restore receipt before allowing this gate to pass for that backend.

## What the receipts do not prove

- A successful uploader exit does not prove the remote provider will retain the
  object forever. Provider-side retention/versioning must be configured and
  monitored separately.
- A host administrator who can write `MEMORY_DIR` can forge receipts. That
  administrator is already inside the filesystem trust boundary.
- Local/mock tests do not prove a real droplet, public DNS, TLS issuance, or an
  actual reboot. Those proofs must be collected on the authorized deployment.
- P2A does not authorize live-network probes, real-user collection, calibration,
  routing changes, or autonomy.

## Thresholds

| Variable | Default | Meaning |
| --- | ---: | --- |
| `OLYMPUS_MIN_FREE_BYTES` | `1073741824` | Minimum free bytes on state mount |
| `OLYMPUS_BACKUP_MAX_AGE` | `172800` | Maximum backup/drill age (48 h) |
| `OLYMPUS_DURABILITY_EVIDENCE_MAX_AGE` | `2592000` | Maximum lifecycle receipt age (30 d) |
| `OLYMPUS_DURABILITY_CHALLENGE_MAX_AGE` | `86400` | Maximum challenge-to-verification time (24 h) |

Malformed or non-positive values fail closed.
