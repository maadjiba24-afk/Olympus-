# Programme — Identity and Access Management

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. Why this is foundational, not a later UI feature

Olympus today has **principals, not identities**. Measured from the code: zero
occurrences of SAML, MFA/TOTP, or service accounts; RBAC vocabulary appears but
is not enforced as a permission system. Authentication is a shared access token,
a local PBKDF2 account store, and per-API-key derived principals.

Every enterprise capability — organisations, workspaces, quotas, audit,
per-tenant retention, plugin permissions — is *about* identity. Building any of
them first means modelling ownership before there is anything to own.

## 2. Scope

**Authentication:** local accounts (harden the existing PBKDF2 store), API keys
with a real lifecycle, service accounts, OAuth 2.1 / OIDC (M3), SAML (M3), MFA
(TOTP first, WebAuthn later), device/session management.

**Authorisation:** RBAC as the M2 baseline (roles: owner, admin, member,
service, auditor); ABAC/policy-based as M4 only if RBAC proves insufficient —
policy engines are easy to add and hard to reason about, so the burden of proof
is on adding one.

**Key management:** rotation without downtime, expiry, revocation effective
within one request, compromised-credential response, break-glass with
mandatory audit and time-boxing.

**Audit:** one unified, tamper-evident trail. Today each capability logs its
own; that is not an audit trail, it is several.

## 3. The migration invariant

Existing per-API-key principals are derived as a domain-separated SHA-256 prefix
of the credential. That derivation closed a HIGH-severity cross-principal
leak. **The new identity model must not merge namespaces**, and the migration
must be verified by the same isolation tests that currently pass — under
concurrency, across a restore, and over a real socket.

Legacy `api-v1` data stays commingled and is never auto-assigned.

## 4. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | Principal model + service accounts + key lifecycle | a revoked key fails within one request; rotation causes no downtime; existing isolation tests still pass |
| M2 | RBAC enforcement + MFA + unified audit | a permission-matrix test per role × resource; MFA enrolment and recovery; audit entries are tamper-evident |
| M3 | OIDC + SAML SSO, break-glass | IdP integration tests against a real IdP; break-glass is time-boxed and always audited |
| M4 | Policy-based access (only if justified) | a written case that RBAC is insufficient |

## 5. Security · Privacy · Cost · Operational

**Security:** the highest-leverage programme in the roadmap; also the highest
blast radius if wrong. Every change ships behind a flag with the current path
intact until parity is proven.
**Privacy:** identity records are personal data, subject to the retention policy
and to deletion. An identity deletion must remove derived sessions and audit
*references* while preserving the audit trail's integrity — these conflict, and
the resolution must be explicit (pseudonymise, do not delete).
**Cost:** engineering only. **Operational:** introduces credential rotation and
IdP outage as new failure modes.

## 6. Non-goals

Building an IdP. Custom crypto. A policy DSL before RBAC is proven insufficient.
