# ADR 0001 — AP2-style payment mandates

- **Status:** Proposed (awaiting operator approval before implementation)
- **Date:** 2026-07-14
- **Deciders:** Olympus maintainers
- **Related:** `docs/AP2_THREAT_MODEL.md`, `docs/THREAT_MODEL.md`,
  the action spine (`olympus/actions.py`), the autonomy dial (L0–L4),
  Agent Behavioral Contracts (`olympus/behavioral_contracts.py`),
  the Ed25519 root of trust (`olympus/witness.py`).

> This is the **precondition artifact** for Component 5 of the 2026-landscape
> build loop. Per the loop scope, no mandate code is written until this ADR and
> the accompanying threat model are approved.

## Context

Google's **Agent Payments Protocol (AP2)** introduces a verifiable-authorization
primitive for agent-initiated commerce: cryptographically-signed **Mandates**
that prove a *user* authorized a *specific* agent action, so a merchant (or a
downstream payment rail) can verify the authorization without trusting the agent
itself. Two mandate kinds matter:

- **Intent Mandate** — the user authorizes the agent to act within *constraints*
  ("buy running shoes, ≤ $150, from an approved merchant, before Friday").
- **Cart Mandate** — the concrete cart the agent assembled, presented back and
  signed before any payment, and checked to fall **within** the Intent Mandate.

Olympus already has the governance scaffolding a payment primitive needs — a
risk-tiered approval spine, an L0–L4 autonomy dial, capability separation for
untrusted content, a vault, and an Ed25519 root of trust. What it lacks is a
**verifiable, tamper-evident record that a human authorized a financial action,
bounded by explicit constraints** — today an approval is a status flag, not a
signed artifact a third party could check.

The deep-research landscape scan flagged AP2 as **high moat, medium fit**: few
agent systems have safe agent-initiated payment authorization, and it maps
cleanly onto our existing spine — but it is **not a solved security problem**
(published red-team work documents mandate-spoofing and prompt-injection during
mandate construction, e.g. arXiv 2510.25819). That risk is why this ADR exists
and why the initial scope is deliberately narrow.

## Decision

Implement **mandate creation + verification only**, as a signed artifact layered
on the existing approval spine and autonomy dial. Explicitly **out of scope for
this loop**: any live payment rail, card/VC issuance, merchant integration, or
network egress to a payment processor. A mandate in this phase is an
*internal, verifiable authorization record* — it authorizes nothing to actually
move money.

Concretely, when approved:

1. **`olympus/mandate.py`** — a native module (no new dependency; reuse the
   Ed25519 primitives in `witness.py` and the vault for key custody):
   - `IntentMandate` — user, scope constraints (amount cap + currency, merchant
     allowlist, item description, expiry, nonce), created only from a **trusted**
     user channel, wrapped-content forbidden.
   - `CartMandate` — references its Intent Mandate id, carries the concrete cart,
     and is **verified to fall within** the intent (amount ≤ cap, merchant ∈
     allowlist, not expired) before it can be signed.
   - `create_intent(...)`, `create_cart(...)`, `sign(mandate)`, `verify(mandate)`
     — verification checks signature, expiry, nonce/replay, subject binding
     (user + session), and intent-containment.
2. **Autonomy-dial mapping.** A payment mandate is `FINANCIAL_LEGAL` risk, which
   `actions._min_level_to_auto` already pins at *always require explicit
   approval* (level 99). A mandate can therefore **never auto-execute** at any
   autonomy level; the signed mandate is the artifact the human produces at the
   approval step, not a way around it.
3. **ABC governance.** A new `payment.mandate` behavioral contract
   (preconditions: intent containment + non-expiry + fresh nonce; governance:
   valid signature + trusted-construction provenance; recovery: `block`) so a
   spoofed, replayed, expired, or injection-constructed mandate is refused at the
   contract layer, not just by ad-hoc checks.

## Options considered

1. **Full AP2 with VCs and a payment rail.** Rejected for this loop: pulls in a
   VC/DID stack (new dependencies — a hard stop) and live money movement, which
   is far past the risk envelope the scan recommended for a first step.
2. **A plain "approved: true" flag (status quo).** Rejected: not tamper-evident,
   not constraint-bound, not independently verifiable — exactly the gap.
3. **Mandate creation + verification only, Ed25519-signed, no rails (chosen).**
   Delivers the verifiable-authorization primitive and its safety properties
   with zero new dependencies and zero ability to move money, so it can be
   hardened and reviewed before any rail is ever considered.

## Consequences

**Positive**
- A tamper-evident, constraint-bound, independently-verifiable record that a
  human authorized a financial action — reusable by any future rail.
- No new dependency; reuses the Ed25519 root of trust and the vault.
- Fits the existing spine/dial/ABC; adds no new autonomy escalation path.

**Negative / risks**
- Mandate-spoofing and construction-injection are real and only *mitigated*, not
  eliminated (see the threat model). This ADR commits to the mitigations and to
  documenting residual risk, not to a proof of safety.
- Signing keys become a higher-value target; custody stays in the vault, and the
  mandate signing key SHOULD be distinct from the release/witness key
  (key-separation; decided in implementation).
- A mandate is inert without a rail; users must not mistake "signed mandate" for
  "payment made." Naming and UX must make this explicit.

## Non-goals (this loop)

Live payments, card/VC issuance, merchant/PSP network calls, refunds/chargebacks,
multi-party settlement, and any autonomy level at which a payment could run
without an explicit human approval.
