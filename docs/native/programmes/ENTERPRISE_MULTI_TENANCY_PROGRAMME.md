# Programme — Enterprise Multi-Tenancy

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. The honest starting position

Olympus is **single-tenant with multiple principals**. Principal isolation is
real and verified — under concurrency, across a restore, and over a real socket.
But there is no container above a principal: no organisation, no workspace, no
project, no per-tenant quota, no per-tenant retention.

## 2. Tenancy invariants (must hold at every milestone)

1. **No cross-tenant read is possible by construction**, not by check. If a code
   path can name another tenant's resource, the model is wrong.
2. **Every resource has exactly one owning tenant**, assigned at creation and
   never inferred.
3. **A tenant deletion removes every derived store** for every principal in it,
   verified from the filesystem — the existing deletion machinery generalised.
4. **Quotas are enforced before work starts**, not after spend.
5. **Isolation survives restore.** A backup/restore that merged tenants would
   silently undo the current isolation guarantee.
6. **Legal hold outranks every retention and deletion path**, at every level.

## 3. Hierarchy

```
Organisation  (billing owner, policy root, retention root, audit root)
  └─ Workspace   (team boundary; quota allocation)
       └─ Project   (resource grouping; environment separation)
            └─ Principal  (user, service account, or API key — EXISTS TODAY)
```

Policy, quota and retention **inherit downward** with explicit override points.
Ownership **resolves upward** to exactly one billing account.

## 4. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | Organisation + workspace; resource ownership | every resource resolves to one org; a cross-org read is impossible by construction |
| M2 | Per-tenant quotas and retention | quota enforced before work starts; per-tenant retention overrides the global default; legal hold still outranks both |
| M3 | Projects, environments, per-tenant audit | audit scoped and complete |
| M4 | Data residency, customer-managed keys | residency enforced at write time; CMK rotation without data loss |

## 5. Encryption and residency

Today: a Fernet vault for credentials; everything else is plaintext on a local
filesystem. Customer-managed keys and regional residency are M4 and require an
explicit encryption boundary — which does not exist yet and must be designed
before it is promised.

## 6. Security · Privacy · Cost · Operational

**Security:** the isolation guarantee is the product for enterprise buyers; a
regression here is existential.
**Privacy:** per-tenant retention, exports and deletion are the compliance
surface. **Cost:** quota enforcement is what makes per-tenant billing possible.
**Operational:** tenant-scoped health becomes a first-class dashboard.

## 7. Non-goals

Multi-region before residency is designed. A policy DSL before RBAC exists.
