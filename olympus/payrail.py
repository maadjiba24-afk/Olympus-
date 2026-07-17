"""Payment rail — SANDBOX/TEST MODE ONLY. Moves no real money.

A mandate (ADR 0002) proves a bounded, co-signed, capability-scoped human
authorization; it authorizes nothing to *actually* charge. This is the executor
that would sit behind it — built here in **sandbox mode only**, with every
safety rail in place, so the primitive can be exercised and reviewed before any
live rail is ever contemplated.

What this DOES:
  * gate every charge through `mandate.enforce_commit` — a charge is refused
    unless the mandate has a valid system signature, a valid USER co-signature
    (on-device-pinnable), a capability token within bound, intent containment,
    freshness, and FINANCIAL_LEGAL risk (which never auto-executes);
  * enforce hard caps (per-transaction + daily) fail-closed;
  * be idempotent by the mandate nonce — a retried charge returns the prior
    result, never double-charges;
  * append a witness-SIGNED ledger record of every attempt (charged / blocked /
    duplicate / refused), externally anchored for free by the head anchor.

What this DELIBERATELY does NOT do:
  * move real money. The default processor is a deterministic FAKE (no network).
  * implement any LIVE processor. `charge()` fails closed on any processor
    marked live unless `OLYMPUS_PAYMENT_LIVE` is set — and no live processor
    exists in this module. Wiring real money is a separate, human-gated step
    (see the HALT on the milestone). The agent never sets `OLYMPUS_PAYMENT_LIVE`.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, asdict

from . import deltas, mandate

# Hard ceilings (minor units, e.g. cents). Env may only LOWER them below these.
_MAX_TX_CEIL = 100_000          # $1,000.00 absolute per-transaction ceiling
_MAX_DAILY_CEIL = 500_000       # $5,000.00 absolute daily ceiling


class PaymentError(RuntimeError):
    """A charge could not be attempted (the caller passed something invalid)."""


class PaymentLiveError(RuntimeError):
    """A LIVE (real-money) charge was attempted. Fail closed — this module
    ships sandbox-only; live money is a separate, human-gated step."""


# --- processors -------------------------------------------------------------

class Processor:
    """A payment processor. `live` MUST be False for anything in this module —
    a live processor is out of scope pending an explicit human go-ahead."""
    name = "base"
    live = False

    def charge(self, *, amount: int, currency: str, merchant: str,
               idempotency_key: str) -> dict:
        raise NotImplementedError


class SandboxProcessor(Processor):
    """A deterministic FAKE processor — no network, no money. Returns a stable
    sandbox charge id so tests and dry-runs are reproducible."""
    name = "sandbox"
    live = False

    def charge(self, *, amount, currency, merchant, idempotency_key) -> dict:
        import hashlib
        cid = "sbx_" + hashlib.sha256(
            f"{idempotency_key}|{amount}|{currency}|{merchant}".encode()
        ).hexdigest()[:20]
        return {"ok": True, "charge_id": cid, "sandbox": True,
                "amount": amount, "currency": currency}


def live_enabled() -> bool:
    """Operator-only switch. The agent never sets this."""
    return os.environ.get("OLYMPUS_PAYMENT_LIVE", "").strip().lower() in (
        "1", "on", "true", "yes")


# --- caps -------------------------------------------------------------------

def _env_int(name: str, ceil: int) -> int:
    try:
        v = int(os.environ.get(name, ceil))
    except (TypeError, ValueError):
        return ceil
    return max(0, min(v, ceil))          # env may only LOWER the hard ceiling


def max_tx() -> int:
    return _env_int("OLYMPUS_PAYMENT_MAX_TX", _MAX_TX_CEIL)


def max_daily() -> int:
    return _env_int("OLYMPUS_PAYMENT_MAX_DAILY", _MAX_DAILY_CEIL)


# --- idempotency + daily tally: derived from the SIGNED ledger --------------
#
# Idempotency and the daily cap are SECURITY rails, so the state they read must
# be trustworthy. Rather than a plain (forgeable, pre-seedable) JSON file, both
# are derived from the witness-SIGNED, append-only payment ledger (deltas) —
# which `verify_history` proves untampered. A charge dedupes against, and the
# daily total sums, only records that verify. Fail closed: if the ledger is
# present but does not verify, we refuse to charge on an untrusted history.

def _day(now: float) -> str:
    return time.strftime("%Y%m%d", time.gmtime(now))


def _verified_records(user: str):
    """Signed payment records for `user`, or None if the ledger is present but
    TAMPERED (fail closed — never trust an unverifiable payment history)."""
    recs = deltas.snapshots(f"payment:{user}")
    if recs and not deltas.verify_history(f"payment:{user}")["ok"]:
        return None
    return recs


def _prior_charge(recs: list, nonce: str) -> dict | None:
    for s in recs:
        st = s.get("state", {})
        if st.get("event") == "charged" and st.get("nonce") == nonce:
            return st
    return None


def _daily_spent(recs: list, day: str) -> int:
    total = 0
    for s in recs:
        st = s.get("state", {})
        if st.get("event") == "charged" and st.get("day") == day:
            total += int(st.get("amount", 0) or 0)
    return total


# --- result -----------------------------------------------------------------

@dataclass
class ChargeResult:
    ok: bool
    status: str                 # charged | duplicate | blocked | refused
    reason: str = ""
    charge_id: str = ""
    amount: int = 0
    currency: str = ""
    mode: str = "sandbox"
    ledger_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _record(user: str, event: str, payload: dict, *, critical: bool = False) -> str:
    """Append a witness-signed record of a payment attempt. `critical=True`
    (the 'charged' record — the durable source of truth for idempotency and the
    daily tally) PROPAGATES a failure so a charge that can't be recorded fails
    closed; audit events (blocked/refused/duplicate) are best-effort."""
    try:
        snap = deltas.record_snapshot(
            f"payment:{user}", kind="payment",
            state={"event": event, **payload},
            provenance=deltas.Provenance(source="payrail", trust="operator",
                                         detail=event))
        return snap["snapshot_hash"]
    except Exception:
        if critical:
            raise
        from . import errors
        errors.capture("payrail.record", RuntimeError(f"unrecorded {event}"))
        return ""


# --- the executor -----------------------------------------------------------

def charge(signed_cart: "mandate.SignedMandate", intent: "mandate.IntentMandate",
           *, capability=None, processor: Processor | None = None,
           user: str = "shared", require_cosignature: bool = True,
           now: float | None = None, revoked=None) -> ChargeResult:
    """Attempt a charge for a fully-verified mandate — SANDBOX by default.

    Order: idempotency check → LIVE guard (fail closed) → mandate gate
    (`enforce_commit`, raises on any defect) → hard caps → processor → signed
    ledger record. Returns a ChargeResult; a blocked/refused attempt is recorded
    too, so nothing about a payment attempt is silent."""
    now = time.time() if now is None else now
    processor = processor or SandboxProcessor()
    mode = "live" if getattr(processor, "live", False) else "sandbox"

    key = str(signed_cart.payload.get("nonce", "")) if isinstance(
        signed_cart, mandate.SignedMandate) else ""
    if not key:
        raise PaymentError("mandate has no nonce to key idempotency on")

    # Source of truth = the SIGNED payment ledger. Fail closed on a tampered one.
    recs = _verified_records(user)
    if recs is None:
        raise PaymentError(
            "payment ledger failed verification — refusing to charge on an "
            "untrusted history")

    # 1. Idempotency — a prior SIGNED 'charged' record for this nonce means the
    # charge already happened; return it, never re-charge.
    prior = _prior_charge(recs, key)
    if prior:
        _record(user, "duplicate", {"nonce": key,
                                    "charge_id": prior.get("charge_id", ""),
                                    "amount": prior.get("amount", 0)})
        return ChargeResult(True, "duplicate", charge_id=prior.get("charge_id", ""),
                            amount=int(prior.get("amount", 0) or 0),
                            currency=str(prior.get("currency", "")), mode=mode)

    # 2. LIVE guard — fail closed. This module has no live processor; a live
    # attempt is refused whether or not the operator flag is set (no live path
    # exists here — that is the post-HALT step).
    if mode == "live":
        _record(user, "refused-live", {"nonce": key})
        if not live_enabled():
            raise PaymentLiveError(
                "live payments are disabled (OLYMPUS_PAYMENT_LIVE unset) — "
                "sandbox-only build")
        raise PaymentLiveError(
            "no live payment processor is implemented in this build — live "
            "money is a separate, human-gated step (see ADR / milestone HALT)")

    # 3. Mandate gate — raises on any defect (signature, co-signature,
    # capability, containment, freshness).
    try:
        mandate.enforce_commit(signed_cart, intent, capability=capability,
                               require_cosignature=require_cosignature, now=now,
                               seen_nonces=None, revoked=revoked)
    except Exception as err:
        _record(user, "refused", {"nonce": key, "reason": str(err)[:200]})
        return ChargeResult(False, "refused", f"mandate refused: {err}",
                            mode=mode)

    scope = mandate.transaction_scope(signed_cart)
    try:
        amount = int(scope.get("amount", 0) or 0)
    except (TypeError, ValueError):
        _record(user, "refused", {"nonce": key, "reason": "non-numeric amount"})
        return ChargeResult(False, "refused", "invalid amount in mandate",
                            mode=mode)
    currency = str(scope.get("currency", ""))
    merchant = str(scope.get("merchant", ""))

    # 4. Hard caps — fail closed; every rejection is recorded.
    if amount <= 0:
        _record(user, "refused", {"nonce": key, "reason": "non-positive amount"})
        return ChargeResult(False, "refused", "non-positive amount", mode=mode)
    if amount > max_tx():
        _record(user, "blocked", {"nonce": key, "amount": amount,
                                  "reason": "per-transaction cap"})
        return ChargeResult(False, "blocked",
                            f"amount {amount} exceeds per-transaction cap "
                            f"{max_tx()}", amount=amount, currency=currency,
                            mode=mode)
    day = _day(now)
    spent = _daily_spent(recs, day)
    if spent + amount > max_daily():
        _record(user, "blocked", {"nonce": key, "amount": amount,
                                  "reason": "daily cap"})
        return ChargeResult(False, "blocked",
                            f"daily cap {max_daily()} would be exceeded "
                            f"({spent}+{amount})", amount=amount,
                            currency=currency, mode=mode)

    # 5. Execute against the (sandbox) processor.
    try:
        out = processor.charge(amount=amount, currency=currency,
                               merchant=merchant, idempotency_key=key)
    except Exception as err:
        _record(user, "processor-error", {"nonce": key, "amount": amount,
                                          "reason": str(err)[:200]})
        return ChargeResult(False, "refused", f"processor error: {err}",
                            amount=amount, currency=currency, mode=mode)
    if not out.get("ok"):
        _record(user, "declined", {"nonce": key, "amount": amount})
        return ChargeResult(False, "refused", "processor declined",
                            amount=amount, currency=currency, mode=mode)

    # 6. The SIGNED 'charged' record IS the durable commit — idempotency and the
    # daily tally derive from it. If it can't be recorded, the charge fails
    # closed (nothing is trusted as charged without a signed record).
    charge_id = str(out.get("charge_id", ""))
    try:
        ledger_hash = _record(user, "charged",
                              {"nonce": key, "charge_id": charge_id,
                               "amount": amount, "currency": currency,
                               "day": day}, critical=True)
    except Exception as err:
        return ChargeResult(False, "refused",
                            f"charge could not be recorded: {err}",
                            amount=amount, currency=currency, mode=mode)
    return ChargeResult(True, "charged", charge_id=charge_id, amount=amount,
                        currency=currency, mode=mode, ledger_hash=ledger_hash)


def attempts(user: str) -> list[dict]:
    """Every recorded payment attempt (signed, verifiable via deltas)."""
    return deltas.snapshots(f"payment:{user}")
