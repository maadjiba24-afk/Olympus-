# Phase 5 — Staging Report (Step 3)

**Unit:** P5-2 · **Status: PROFILE AUTHORED AND VALIDATED. NOT DEPLOYED.**

---

## 1. The headline, first

A canonical staging profile now exists, fails closed on incomplete
configuration, exposes a readiness probe distinct from liveness, drains on
SIGTERM, and reports its own build. **It has never been run.** `docker info`
fails in this environment — the compose CLI is present (v5.1.1) but there is no
daemon — so the compose file is schema-validated with `docker compose config`
and nothing more. No image was built. No container was started. Every claim
below is about code and configuration that was exercised **in-process**, not
about a running deployment.

## 2. What was built

| Requirement (Step 3) | Status | Where |
|---|---|---|
| explicit staging configuration | ✅ | `OLYMPUS_ENV=staging`, normalised in `config.deployment_env()` |
| no implicit production defaults | ✅ | production-only settings refused; see §3 |
| persistent paths for durable stores | ✅ | `OLYMPUS_MEMORY_DIR` required; writability probed by writing |
| documented volume ownership | ✅ | `docker-compose.staging.yml`, inline |
| secrets outside source control | ✅ | `env_file`; `.env.staging` git-ignored; only `.example` ships |
| authenticated non-loopback exposure | ✅ | boot refuses an off-loopback bind with no credential |
| readiness check | ✅ | `GET /readyz` |
| liveness check | ✅ | `GET /healthz` (pre-existing, kept cheap) |
| graceful shutdown | ✅ | SIGTERM/SIGINT → drain → `server_close()` |
| restart-safe recovery | ✅ | proved by test, not by restart — see §4 |
| resource limits | ✅ | compose `deploy.resources`, `ulimits` |
| request-size limits | ✅ | pre-existing 1 MB `_MAX_BODY`; verified over HTTP in the client campaign |
| timeout limits | ✅ | client-side, verified in the client campaign |
| concurrency limits | ✅ | `OLYMPUS_MAX_CONCURRENT_CALLS`, documented at the measured ceiling of 16 |
| spend caps | ✅ | positive `OLYMPUS_DAILY_BUDGET` required at boot |
| provider-call limits | ✅ | admission + budget, unchanged |
| retention maintenance | ✅ | finite `RETAIN_DAYS` required at boot |
| backup hooks | ✅ | pre-existing; production destination refused in staging |
| restore verification | ✅ | drilled — see `PHASE5_BACKUP_RECOVERY_REPORT.md` |
| version and commit reporting | ✅ | `config.build_info()` on `/readyz` and `/api/metrics` |
| configuration validation at startup | ✅ | `require_staging_config()` in the CLI boot invariant and `serve()` |
| **fail closed when config is absent** | ✅ | `StagingConfigError` lists every problem at once |

## 3. Structural differences from production

Not configuration differences — structural, so the profile cannot be turned
into production by swapping an env file.

| | Production compose | Staging profile |
|---|---|---|
| heartbeat (spends tokens unattended) | present, on by default | **absent** |
| Caddy, TLS, ports 80/443 | present | **absent** |
| host port publishing | `ports:` | `expose:` only |
| volume | `olympus-memory` | `olympus-staging-memory` |
| `OLYMPUS_ENV` | unset | `staging`, set in the compose file itself |
| healthcheck | none | `/readyz` |

Asserted by parsing the YAML as data, plus a test that the production compose
is untouched.

## 4. Evidence, and its exact limits

**Proved in-process (real code, real sockets):** boot refuses each incomplete
profile and names every problem; a complete profile boots; dev and production
boots are byte-identically unaffected; `/readyz` returns 503 with reasons while
`/healthz` stays 200; `/readyz` carries no secret or user data; SIGTERM
installation degrades safely off the main thread; the compose file has the
required shape.

**NOT proved:** that the container builds; that it starts; that the volume
mounts with the right ownership; that the healthcheck passes in Docker; that
`stop_grace_period` is honoured; that resource limits bind. All of these need a
daemon.

**Restart recovery** (P5-A4) is proved at the data layer — caches dropped,
stores re-read, torn tails truncated, corrupt records quarantined, corrupt
snapshots falling back to the journal — not by stopping and starting a
container.

## 5. Open

| # | Gap | Needs |
|---|---|---|
| S1 | the image has never been built or run | a Docker daemon |
| S2 | volume ownership under a non-root UID is documented, not verified | a daemon |
| S3 | no reverse proxy is exercised (the profile deliberately has none) | a deployment |
| S4 | `OLYMPUS_BUILD_COMMIT` stamping at build time is documented, not executed | a build |
