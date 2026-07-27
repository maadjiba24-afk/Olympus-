# Programme — Organisation Management

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. Relationship to identity, tenancy and billing

These four are routinely conflated. Olympus separates them:

| Concept | Owns | Programme |
|---|---|---|
| **Identity** | who you are, and what you may do | Identity & Access |
| **Organisation** | the human/commercial container: members, roles, invitations | this programme |
| **Tenant** | the isolation boundary for data and resources | Enterprise Multi-Tenancy |
| **Billing account** | who pays | Billing & Usage |

Default mapping: **one organisation = one tenant = one billing account.** They
are modelled separately because enterprises break the mapping — a parent org
with several billing accounts, or one billing account across several tenants for
data-residency reasons. Collapsing them early is the mistake that forces a
rewrite later.

## 2. Scope

Organisation lifecycle (create, rename, suspend, delete); invitations with
expiry and revocation; teams; workspaces and projects (surfaced here, owned by
Tenancy); ownership and transfer; member lifecycle including offboarding;
role assignment (enforced by Identity); policies and quotas (set here, enforced
by Tenancy); billing ownership; audit; exports; deletion.

## 3. The two operations that go wrong

**Ownership transfer.** Must be atomic, audited, and reversible within a window.
An organisation with no owner is unrecoverable without support intervention.

**Member offboarding.** Removing a member must revoke sessions and keys
immediately, reassign or explicitly orphan their owned resources, and preserve
their audit history. Deleting a member's audit trail to satisfy a deletion
request destroys the record of what they did — the resolution is to
pseudonymise, and it must be stated in policy, not decided at implementation
time.

## 4. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | Org lifecycle, invitations, membership | an org always has ≥1 owner; invitations expire; revocation is immediate |
| M2 | Teams, transfer, offboarding | transfer is atomic and audited; offboarding revokes within one request |
| M3 | Policies, quotas, billing ownership | org policy inherits to workspaces with explicit overrides |
| M4 | Exports and org deletion | export is complete and machine-readable; deletion removes every tenant resource, verified |

## 5. Security · Privacy · Cost · Operational

**Security:** invitation flows are a classic escalation vector — bind to an
email, expire, single-use, and never auto-accept.
**Privacy:** member records are personal data; offboarding is a deletion event
with an audit exception. **Cost:** none directly. **Operational:** support
tooling for the unrecoverable cases (lost owner) is part of M2, not an afterthought.
