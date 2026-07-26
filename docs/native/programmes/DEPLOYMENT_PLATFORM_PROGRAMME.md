# Programme — Deployment Platform

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. The problem

**Nothing has ever been deployed.** A Dockerfile, a production Compose file with
Caddy, and a fail-closed staging profile all exist and are schema-validated. No
image has been built and no container has been started, because the authoring
environment has no Docker daemon.

The consequence is not cosmetic: **`staging` is the maturity ceiling for the
entire platform** until this lands, and every rollback threshold in the
readiness report references a baseline that cannot exist without it.

## 2. Separation of concerns

**Deployment tooling** (this programme) is what any operator uses to run Olympus
on their own infrastructure. **Hosting as a commercial product** is a different
business decision with different requirements — multi-tenancy, uptime
commitments, support. This programme deliberately does not assume it.

## 3. Scope by milestone

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | **Deploy the existing staging profile to a real host** | image builds; container starts; `/readyz` green; volume ownership correct under a non-root UID; SIGTERM drains an in-flight request within the grace period; a backup restores into a fresh volume and the instance starts against it |
| M2 | Upgrade, rollback, migration | a version upgrade with no data loss; a rollback to the prior image; forward-only migrations with a documented irreversibility boundary |
| M3 | Kubernetes + Helm | the same acceptance as M1 on a cluster; PDBs, probes and resource requests derived from measured limits (16 concurrent provider calls/host) |
| M4 | Zero-downtime deploy, autoscaling, DR | connection draining verified under load; autoscaling bounded by the ledger ceiling until P4 lifts it; a DR drill restoring into a different region |
| M5 | Release channels | stable/beta/edge with a documented promotion gate |

## 4. Configuration and secrets

The staging profile already fails closed on missing configuration and reports
every problem at once. Extend that discipline: no implicit production defaults,
secrets from the environment or a secret manager, never from source control,
and startup validation that names the missing variable.

## 5. Security · Privacy · Cost · Operational

**Security:** M1 is the first real network exposure. The controls already exist
(fail-closed boot, per-key principals, loopback-only without a credential) but
have never faced a real network.
**Privacy:** M1 is also the first real data at rest — the retention policy must
be set with or before it.
**Cost:** hosting. **Operational:** creates the first thing that can page
someone; P1 Observability should follow immediately.

## 6. Risks and rollback

**Risk:** a deployed instance with no retention policy. *Mitigation:* Data
Governance is a co-requisite, not a follow-up.
**Risk:** volume ownership under a non-root UID — documented, never verified.
*Mitigation:* it is M1 acceptance criteria.
**Rollback:** stop the stack; the volume persists; restore is drilled.

## 7. Non-goals

Multi-region at M1. Managed hosting. Edge deployment before the single-node
contracts are stable.
