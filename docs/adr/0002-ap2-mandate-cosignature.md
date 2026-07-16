# ADR 0002 — AP2 mandates: user co-signature, capability binding, transaction scope

- **Status:** Proposed — **precondition artifact; HALTED for operator approval before any code**
- **Date:** 2026-07-16
- **Deciders:** Olympus maintainers (pending)
- **Supersedes/extends:** [ADR 0001](0001-ap2-payment-mandates.md) (mandate creation + verification only)
- **Related:** `docs/AP2_THREAT_MODEL.md` (with the milestone-4 addendum),
  `olympus/mandate.py`, the capability tokens in `olympus/identity.py` (M1.2),
  the autonomy dial in `olympus/actions.py` (L0–L4, risk classes),
  Agent Behavioral Contracts (`olympus/behavioral_contracts.py`, the
  `payment.mandate` contract), the Ed25519 root of trust (`olympus/witness.py`).

> This is the **precondition artifact** for the FINAL milestone of the build
> loop (AP2 = artifact type 4 on Plane 1). Per the milestone rule, no mandate
> code is written until this ADR and the threat-model addendum are approved by
> the operator. This document proposes a decision; it does not implement it.

## Context

ADR 0001 delivered — and `olympus/mandate.py` implements — AP2 mandates as a
**single-signature** artifact: `IntentMandate` / `CartMandate` signed by the
system's mandate subkey (`witness.sign_with("mandate/v1", …)`), verified for
signature, expiry, nonce/replay, subject binding, and intent-containment. A
payment mandate is `FINANCIAL_LEGAL` risk, so `actions._min_level_to_auto` pins
it at level 99 — it **never** auto-executes. No live rail exists.

That artifact proves *the system* asserted a bounded authorization. It does **not**
yet carry an independent, cryptographic proof that *the human* authorized *this
specific* transaction. Today the human approval is still the spine's status
step; the signature on the mandate is the system's, not the user's. Three gaps
remain before a mandate is a true two-party authorization:

1. **No user co-signature.** A single (system) signature means a compromised or
   injection-steered agent that can reach the signing subkey can mint a "valid"
   mandate. The user's authorization is not itself a verifiable artifact.
2. **No capability binding.** M1.2 introduced signed **capability tokens**
   (`identity.grant`) bounding a subject to scopes up to a risk ceiling. A
   mandate is not yet tied to one, so the mandate's authority is not checked
   against an independently-granted, revocable capability.
3. **Transaction scope is implicit.** The mandate's constraints (amount, merchant,
   currency, expiry) exist, but their mapping to the **autonomy dial** as an
   explicit *transaction scope* — what the human is co-signing, at what risk
   level — is not first-class.

## Decision (proposed — pending approval)

Extend the mandate to a **two-party, capability-bound, scope-explicit**
authorization, still with **no live payment rail** and still **never
auto-executing**. Concretely, when approved:

1. **User co-signature (dual-signature).** A mandate becomes exercisable only
   when it carries BOTH signatures over the *same canonical payload*:
   - the **system** signature (existing `mandate/v1` subkey), and
   - a **user co-signature** under a distinct, user-held key
     (`mandate-user/v1` subkey via `witness.sign_with`, custody in the vault; a
     real deployment would hold the user key on the user's own device — this
     loop keeps it vault-local and clearly labels that as a trust assumption).
   `verify()` gains `require_cosignature=True`: a mandate missing or failing the
   user co-signature is refused, fail closed. The co-signature binds to the
   transaction hash, so it cannot be lifted onto a different payload.
2. **Capability-token binding.** The mandate references a M1.2 capability token
   (`jti`) and its scope. `verify()` checks the mandate's transaction scope is
   **within** the token's granted scope and risk ceiling (reusing
   `identity.verify_grant`), and that the token is unexpired, unrevoked, and
   fresh. Revoking the capability revokes the mandate's authority — a kill
   switch the single-signature design lacks.
3. **Transaction scope → autonomy dial.** A mandate declares an explicit
   `TransactionScope` (amount cap + currency, merchant, action class). It maps to
   the dial as `FINANCIAL_LEGAL` — `_min_level_to_auto` = 99, **never auto** — so
   the co-signature IS the human approval artifact at the gate, not a bypass. No
   new autonomy path is introduced; the scope makes the bound the human co-signs
   explicit and human-legible.
4. **ABC governance.** Extend the `payment.mandate` contract with governance
   predicates for the new invariants (user-co-signature valid; capability within
   bound), recovery `block` — a mandate missing either is refused at the contract
   layer, not just by ad-hoc checks. Tighten-only (adds clauses; weakens none).

## Options considered

1. **Keep single-signature (status quo).** Rejected: the user's authorization is
   not a verifiable artifact, so a defeated agent boundary forges authorization.
2. **Full AP2 with VC/DID user identity + a payment rail.** Rejected for this
   loop: new dependency stack (hard stop) and live money movement — far past the
   risk envelope. Co-signature gives the two-party property with zero new deps.
3. **User co-signature + capability binding + explicit scope, no rail (chosen).**
   Delivers two-party, revocable, scope-explicit authorization on the existing
   Ed25519 root of trust and M1.2 tokens, with zero new dependencies and zero
   ability to move money.

## Consequences

**Positive**
- Two-party authorization: a mandate now proves *both* the system and the user
  authorized *this* bounded transaction — a single compromised key is not enough.
- Revocable authority via the capability token (kill switch).
- Explicit, human-legible transaction scope at the approval gate.
- Zero new dependencies; reuses `witness`, `identity` (M1.2), the vault, ABC.

**Negative / risks**
- A **user key** now exists and must be custodied. This loop keeps it vault-local
  and labels that as a trust assumption; on-device user keys are future work.
- Two keys, two custody problems; key-separation (system vs user vs release)
  becomes load-bearing.
- Co-signature does not defeat a fully compromised vault or a socially-engineered
  human; the human-visible summary (C2.4) remains the backstop. See the threat
  model addendum for the honest residual-risk statement.
- Still inert without a rail — "co-signed mandate" ≠ "payment made." Naming/UX
  must keep this explicit.

## Non-goals (this loop)

Live payments, card/VC issuance, on-device user-key custody, merchant/PSP network
calls, refunds/chargebacks, multi-party settlement, and any autonomy level at
which a payment could run without an explicit human co-signature.

## Precondition / HALT

Per the milestone rule, **implementation does not begin until this ADR and the
threat-model addendum (`docs/AP2_THREAT_MODEL.md`, "Milestone-4 addendum") are
approved by the operator.** The Phase-2 adversarial tests named in the threat
model must pass before the component is called done.
