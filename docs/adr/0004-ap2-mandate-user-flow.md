# ADR 0004 — AP2 mandate user-facing flow (no rail)

- **Status:** Accepted — implemented (action-spine flow; still no live rail)
- **Date:** 2026-07-17
- **Deciders:** Olympus maintainers
- **Supersedes nothing; extends** [ADR 0001](0001-ap2-payment-mandates.md),
  `docs/AP2_THREAT_MODEL.md`, the action spine (`olympus/actions.py`), the
  autonomy dial, and Agent Behavioral Contracts.

## Context

ADR 0001 built mandate **creation + verification** primitives
(`olympus/mandate.py`) but left them with no user-facing flow — by design. This
ADR adds the flow: how a human actually *authorizes* a bounded payment and how
that authorization is recorded, **without introducing any live payment rail**.

The boundary from ADR 0001 stands and is restated as a hard constraint here: **no
money moves.** No PSP/merchant network calls, no card/VC issuance, no rail. A
mandate authorization in this phase produces a signed, verified, recorded
authorization artifact and nothing else. The value is a tamper-evident,
constraint-bound, independently-verifiable record that *this human authorized
this bounded action* — reusable by any future rail, and safe to build and harden
first.

## Decision

Expose payment-mandate authorization as a single **action-spine action**,
reusing every existing governance mechanism rather than inventing a parallel one:

1. **`authorize_payment` action type** (`FINANCIAL_LEGAL` risk, scope
   `payment.authorize`). Because it is `FINANCIAL_LEGAL`,
   `actions._min_level_to_auto` pins it at 99 — it can **never** auto-run at any
   autonomy level. It is always *prepared* and waits for explicit human approval.
2. **The approval IS the signing event.** The agent `prepare`s the action from
   trusted user-supplied constraints (amount cap + currency, merchant allowlist,
   item, expiry) plus the concrete cart (amount, merchant, items). The human sees
   a plain-language preview of the exact bounded authorization (the
   construction-injection backstop, threat-model C2.4). On approval, `execute`:
   - builds the `IntentMandate` (trusted-constructed) and the `CartMandate`,
   - signs both with the `mandate/v1` Ed25519 subkey,
   - runs `mandate.enforce_commit` — the `payment.mandate` ABC contract
     (intent-containment, non-expiry, fresh nonce, valid signature, trusted
     construction; recovery `block`) — **before** recording anything,
   - records the verified, signed mandate to an append-only per-user store, and
   - returns a result that states, explicitly, `moved_money: false`.
3. **Persistence + replay defense.** `olympus/mandate_store.py` keeps a bounded
   history of issued mandates and a non-evicting set of consumed nonces. State
   is keyed by the exact principal and, on the supported POSIX single-host
   deployment, the read/modify/write cycle is serialized across processes.
   Missing state means first use; malformed, wrong-shaped, inconsistent, or
   unattributed legacy state raises a typed error rather than becoming an empty
   nonce set. The store publishes nonce consumption before the record, so
   interruption burns authority instead of reopening replay, and independently
   rejects an already-consumed cart nonce while holding the write lock.

## Options considered

1. **A live rail now.** Rejected — out of scope, and unsafe before the primitive
   is hardened (ADR 0001's residual-risk position).
2. **A bespoke approval UI outside the spine.** Rejected — it would duplicate the
   risk-tier/autonomy/approval logic and create a second, unaudited path to a
   financial action.
3. **One `FINANCIAL_LEGAL` action on the existing spine (chosen).** The mandate
   flow inherits deny-first, scope-gating, rate-limiting, the ABC layer, the
   signed decision log, and the "never auto-run" guarantee for free.

## Consequences

**Positive**
- A real, human-driven authorization flow with zero ability to move money.
- No new dependency; reuses the spine, the ABC contract, and the Ed25519 subkey.
- The human always sees the exact bounded authorization before signing it.
- Corrupt, cross-owner, concurrent, and partially-published replay evidence
  fails closed and remains available for operator inspection.

**Negative / residual risk (unchanged from ADR 0001)**
- Mandate-spoofing and construction-injection are *mitigated, not solved*; the
  no-rail boundary keeps the blast radius of a defeated control to an internal
  record, never a payment.
- A recorded mandate must not be mistaken for a completed payment; the result
  and preview say so explicitly.
- Refusing to evict nonce evidence means a full nonce ledger blocks new mandate
  recording. That availability failure is intentional until an explicit,
  evidence-preserving archival protocol exists.
- `proclock` degrades to process-local locking on Windows and does not span
  multiple hosts. Those topologies must not treat this store as a distributed
  payment-authorization transaction boundary; the no-live-rail constraint
  remains mandatory.

## Non-goals (unchanged)

Live payments, card/VC issuance, merchant/PSP calls, refunds/chargebacks,
settlement, and any autonomy level at which a payment authorization could run
without an explicit human approval.
