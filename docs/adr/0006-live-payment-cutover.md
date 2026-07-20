# ADR 0006 — Live payment path: built in full, shipped inert

**Status:** accepted
**Date:** 2026-07-17
**Relates to:** ADR 0002 (AP2 mandate co-signature), `olympus/payrail.py`
(sandbox rail), `olympus/paylive.py` (this decision's implementation)

## Context

The payment rail shipped **sandbox-only** (Parked-4): every safety rail —
mandate gating, hard caps, signed-ledger idempotency — proven against a
deterministic fake processor, with the live branch a hard `PaymentLiveError`.
The remaining question was the live-money decision itself. The user's explicit
choice: **build the live path completely, but leave it switched off** — real
money must only ever be enabled by a human, out of band, never by the agent.

## Decision

The full live-cutover path exists in `olympus/paylive.py`, and it ships
**inert**. Going live requires two separate human acts that this repository
deliberately does not and cannot perform:

1. **The operator flag** — `OLYMPUS_PAYMENT_LIVE` set in the environment.
   The agent never sets it (pinned by test).
2. **A registered live adapter** — the default `DisabledLiveAdapter` refuses
   every charge and no real adapter exists in the repository. An operator
   writes one out of band (credentials never enter this codebase — there is no
   network code in `paylive` at all) and installs it with
   `register_live_adapter`.

Even after both acts, **every** live charge still requires all of:

- a mandate with valid **system signature and user co-signature** —
  co-signature enforcement is *forced* on the live path (the sandbox
  `require_cosignature=False` convenience does not exist there);
- the user co-signing key **pinned out-of-band**
  (`OLYMPUS_MANDATE_USER_PUBKEY`) — vault-local default trust is refused for
  real money, so a vault compromise alone cannot authorize a live charge;
- the **registered adapter passed by identity** — a live processor object from
  any other source is refused, killing object-smuggling into the live path;
- amounts within the **live caps** — separate, *lower* ceilings than sandbox
  ($100 per transaction, $250 daily; env may only lower), tallied from the
  witness-signed ledger, alongside the global sandbox ceilings;
- the **`payment.live` ABC contract** passing at the last gate before the
  adapter (defense in depth over the imperative checks);
- a **signed attempt record before anything else** — for real money the audit
  fails closed: an attempt that cannot be recorded is refused.

`olympus pay-live` prints the cutover checklist *derived from these gates*
(flag / adapter / pinned key / caps), so the go-live runbook can never drift
from what the code actually requires. `--reconcile` audits signed live charges
against the adapter's own records (`LiveAdapter.lookup`).

## What the agent will never do

Set `OLYMPUS_PAYMENT_LIVE`; register (or write) a live adapter; accept, store,
or transmit real payment credentials; or weaken any gate above. A cutover is a
human runbook executed by a human.

## Consequences

- The cutover is a **configuration act, not a code change**: nothing in the
  executor needs touching to go live, so the reviewed, tested path is exactly
  the path that will run.
- The repository remains incapable of moving real money as shipped — verified
  by tests that exercise the live branch end-to-end against a fake adapter and
  pin the inert shipped state.
- Live limits start deliberately small; raising a ceiling is a code change
  (this file and `paylive.py`), not an env tweak.
