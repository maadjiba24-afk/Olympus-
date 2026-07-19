"""LIVE payment path — fully built, shipped INERT. The switch stays OFF.

This is the post-HALT half of the payment rail (`payrail` is the sandbox half).
The user's decision was "build it, leave it off": the complete live-cutover code
path exists here — every gate, cap, audit record and reconciliation hook — but
NOTHING in this repository can move real money, because going live requires TWO
separate human acts that this codebase deliberately does not perform:

  1. **The operator flag.** `OLYMPUS_PAYMENT_LIVE` must be set in the
     environment. The agent never sets it (pinned by test: this module never
     writes the environment).
  2. **A registered live adapter.** The default is `DisabledLiveAdapter`, which
     refuses every charge. No real adapter exists in this repository — writing
     one (with its credentials, held outside this codebase) and calling
     `register_live_adapter` is the operator's deliberate act. There is no
     network code here at all.

And even after both acts, every single live charge still requires:

  * a mandate with a valid SYSTEM signature **and** a valid USER co-signature —
    co-signature enforcement is FORCED in live mode (the sandbox convenience of
    waiving it does not exist here);
  * the user co-signing key to be **pinned out-of-band**
    (`OLYMPUS_MANDATE_USER_PUBKEY`) — vault-local default trust is not enough
    for real money, so a vault compromise alone cannot authorize a live charge;
  * the amount to clear the LIVE caps — separate, LOWER ceilings than sandbox
    (env may only lower them further), tallied from the witness-SIGNED ledger;
  * the `payment.live` ABC contract to pass at the last gate before the
    adapter (defense in depth over the imperative checks);
  * a witness-signed attempt record BEFORE anything else — for real money the
    audit fails closed: if the attempt can't be recorded, the charge is refused.

The caller must also pass the registered adapter itself to `payrail.charge` —
a live processor object smuggled in from anywhere else is refused by identity,
so the only executable live path is the one a human explicitly installed.
"""

from __future__ import annotations

import logging
import time

from . import mandate, payrail

_log = logging.getLogger("olympus.paylive")

# LIVE hard ceilings (minor units). Deliberately LOWER than the sandbox
# ceilings: real money starts small. Env may only lower them further.
_LIVE_MAX_TX_CEIL = 10_000        # $100.00 absolute per-transaction ceiling
_LIVE_MAX_DAILY_CEIL = 25_000     # $250.00 absolute daily ceiling


def live_max_tx() -> int:
    return payrail._env_int("OLYMPUS_PAYMENT_LIVE_MAX_TX", _LIVE_MAX_TX_CEIL)


def live_max_daily() -> int:
    return payrail._env_int("OLYMPUS_PAYMENT_LIVE_MAX_DAILY",
                            _LIVE_MAX_DAILY_CEIL)


# --- adapters ----------------------------------------------------------------

class LiveAdapter(payrail.Processor):
    """A real-money processor adapter. NONE ships in this repository — an
    operator writes one out of band (credentials never live in this codebase)
    and installs it with `register_live_adapter`. `lookup` is the
    reconciliation hook: fetch the processor's own record of a charge."""
    name = "live-base"
    live = True

    def charge(self, *, amount: int, currency: str, merchant: str,
               idempotency_key: str) -> dict:
        raise NotImplementedError

    def lookup(self, charge_id: str) -> dict:
        raise NotImplementedError


class DisabledLiveAdapter(LiveAdapter):
    """The default: refuses everything, fail closed."""
    name = "disabled"

    def _refuse(self, *a, **k):
        raise payrail.PaymentLiveError(
            "no live payment adapter is registered — this build ships INERT. "
            "Wiring real money requires an operator to install an adapter "
            "(register_live_adapter) AND set OLYMPUS_PAYMENT_LIVE; the agent "
            "does neither.")

    charge = lookup = _refuse


_adapter: LiveAdapter = DisabledLiveAdapter()


def register_live_adapter(adapter: LiveAdapter) -> None:
    """Install the live adapter — the operator's deliberate act (human act #2).
    Only a real LiveAdapter is accepted; nothing in this repo calls this with
    anything but the disabled default.

    Installing a real-money adapter is the single most consequential act in the
    system, so it is never silent: it emits a loud WARNING to the operational
    log. (The DisabledLiveAdapter — the inert default, used to reset — is not a
    real adapter and is installed quietly.)"""
    global _adapter
    if not isinstance(adapter, LiveAdapter) or adapter.live is not True:
        raise payrail.PaymentError(
            "a live adapter must be a LiveAdapter with live=True")
    if not isinstance(adapter, DisabledLiveAdapter):
        _log.warning(
            "LIVE PAYMENT ADAPTER INSTALLED (%r): real money can now move once "
            "OLYMPUS_PAYMENT_LIVE is also set and a co-signed, capability-scoped "
            "mandate clears every gate. This is an operator act; the agent never "
            "performs it.", getattr(adapter, "name", type(adapter).__name__))
    _adapter = adapter


def reset_live_adapter() -> None:
    global _adapter
    _adapter = DisabledLiveAdapter()


def adapter_ready() -> bool:
    return not isinstance(_adapter, DisabledLiveAdapter)


def is_inert() -> bool:
    """True in the SHIPPED state: real money cannot move. Inert unless BOTH
    operator acts are done — the flag is set AND a real adapter is registered.
    A single, machine-checkable safety predicate (doctor/monitoring can assert
    it) that can never drift from the gates, because it reads them directly.
    Note: even when this returns False, every individual charge still has to
    clear the co-signed mandate, key-pin, caps, and contract gates."""
    return not (payrail.live_enabled() and adapter_ready())


# --- cutover status: the go-live checklist, derived from code ----------------

def cutover_status() -> dict:
    """What is (and is not) in place for live money — the checklist an operator
    reads before a cutover, derived from the live gates themselves so it can
    never drift from what the code actually requires. Read-only."""
    missing = []
    if not payrail.live_enabled():
        missing.append("operator flag: set OLYMPUS_PAYMENT_LIVE (human act #1)")
    if not adapter_ready():
        missing.append("live adapter: none registered — write one out of band "
                       "and call register_live_adapter (human act #2)")
    if not mandate.user_pinned_pubkeys():
        missing.append("user key custody: pin the user co-signing key "
                       "(OLYMPUS_MANDATE_USER_PUBKEY) — vault-local trust is "
                       "not accepted for live money")
    return {
        "live_flag": payrail.live_enabled(),
        "adapter": {"registered": adapter_ready(), "name": _adapter.name},
        "user_key_pinned": bool(mandate.user_pinned_pubkeys()),
        "caps": {"live_max_tx": live_max_tx(),
                 "live_max_daily": live_max_daily(),
                 "sandbox_max_tx": payrail.max_tx(),
                 "sandbox_max_daily": payrail.max_daily()},
        "ready": not missing,
        "missing": missing,
        "note": "every live charge additionally requires a co-signed, "
                "capability-scoped mandate; the agent never performs the "
                "missing acts",
    }


# --- the live tally (signed-ledger-derived, live records only) ---------------

def _live_daily_spent(recs: list, day: str) -> int:
    total = 0
    for s in recs:
        st = s.get("state", {})
        if st.get("event") == "charged" and st.get("day") == day \
                and st.get("live") is True:
            total += int(st.get("amount", 0) or 0)
    return total


# --- the live executor -------------------------------------------------------

def execute_live(signed_cart, intent, *, capability=None,
                 processor: payrail.Processor, user: str | None = None,
                 nonce: str, now: float | None = None,
                 revoked=None) -> "payrail.ChargeResult":
    """The one live-charge path, normally reached via `payrail.charge`. Fail
    closed at every step; every attempt — including a refused one — is a
    signed record.

    It TRUSTS NOTHING from the caller about payment history: the verified
    ledger is re-derived and the nonce re-checked HERE, so even a direct call
    that bypasses `payrail.charge` cannot double-charge or charge against a
    tampered history.

    Shipped state: no flag, no adapter → PaymentLiveError before anything else
    could matter. Nothing in this repository can make this function move money.
    """
    now = time.time() if now is None else now

    # 0a. Ledger namespace = the MANDATE's authorizing user, never the
    # caller's choice (idempotency and daily caps must not be namespaceable).
    mandate_user = str(getattr(signed_cart, "payload", {}).get("user", ""))
    if not mandate_user:
        raise payrail.PaymentError(
            "mandate has no user to key the payment ledger on")
    if user is None:
        user = mandate_user
    elif user != mandate_user:
        raise payrail.PaymentError(
            f"caller user {user!r} does not match the mandate's authorizing "
            f"user {mandate_user!r}")

    # 0. Audit first, fail closed: for real money, an attempt that cannot be
    # signed into the ledger is refused outright (critical=True propagates).
    payrail._record(user, "live-attempt", {"nonce": nonce}, critical=True)

    # 0b. Re-derive the SIGNED history independently (never trust the caller):
    # fail closed on a tampered ledger, and dedupe the nonce here too.
    recs = payrail._verified_records(user)
    if recs is None:
        raise payrail.PaymentError(
            "payment ledger failed verification — refusing a live charge on "
            "an untrusted history")
    prior = payrail._prior_charge(recs, nonce)
    if prior:
        payrail._record(user, "duplicate", {"nonce": nonce,
                                            "charge_id": prior.get("charge_id", ""),
                                            "amount": prior.get("amount", 0)})
        return payrail.ChargeResult(
            True, "duplicate", charge_id=str(prior.get("charge_id", "")),
            amount=int(prior.get("amount", 0) or 0),
            currency=str(prior.get("currency", "")), mode="live")

    # 1. Human act #1 — the operator flag. The agent never sets it.
    if not payrail.live_enabled():
        raise payrail.PaymentLiveError(
            "live payments are disabled (OLYMPUS_PAYMENT_LIVE unset) — "
            "this build ships inert; only an operator may flip the switch")

    # 2. Human act #2 — a registered live adapter, passed BY IDENTITY. A live
    # processor object from anywhere else (including the disabled default) is
    # refused: the only executable live path is the one a human installed.
    if not adapter_ready():
        raise payrail.PaymentLiveError(
            "no live payment adapter is registered — this build ships inert "
            "(the default adapter refuses everything)")
    if processor is not _adapter:
        payrail._record(user, "refused", {"nonce": nonce,
                                          "reason": "unregistered live processor"})
        raise payrail.PaymentLiveError(
            "live charges execute only through the REGISTERED live adapter — "
            "a live processor passed from anywhere else is refused")

    # 3. On-device key custody — the user co-signing key must be pinned
    # out-of-band. Vault-local default trust is not enough for real money.
    if not mandate.user_pinned_pubkeys():
        payrail._record(user, "refused", {"nonce": nonce,
                                          "reason": "user key not pinned"})
        return payrail.ChargeResult(
            False, "refused",
            "live charges require a pinned user co-signing key "
            "(OLYMPUS_MANDATE_USER_PUBKEY) — on-device custody, not vault trust",
            mode="live")

    # 4. The mandate gate, with co-signature enforcement FORCED — the sandbox
    # convenience of waiving it does not exist on the live path.
    try:
        mandate.enforce_commit(signed_cart, intent, capability=capability,
                               require_cosignature=True, now=now,
                               seen_nonces=None, revoked=revoked)
    except Exception as err:
        payrail._record(user, "refused", {"nonce": nonce,
                                          "reason": str(err)[:200]})
        return payrail.ChargeResult(False, "refused",
                                    f"mandate refused: {err}", mode="live")

    scope = mandate.transaction_scope(signed_cart)
    try:
        amount = int(scope.get("amount", 0) or 0)
    except (TypeError, ValueError):
        payrail._record(user, "refused", {"nonce": nonce,
                                          "reason": "non-numeric amount"})
        return payrail.ChargeResult(False, "refused",
                                    "invalid amount in mandate", mode="live")
    currency = str(scope.get("currency", ""))
    merchant = str(scope.get("merchant", ""))
    day = payrail._day(now)

    # 5. Caps — the LIVE ceilings AND the global sandbox ceilings both apply
    # (live is a subset of allowed, never an expansion). Fail closed.
    if amount <= 0:
        payrail._record(user, "refused", {"nonce": nonce,
                                          "reason": "non-positive amount"})
        return payrail.ChargeResult(False, "refused", "non-positive amount",
                                    mode="live")
    tx_cap = min(live_max_tx(), payrail.max_tx())
    if amount > tx_cap:
        payrail._record(user, "blocked", {"nonce": nonce, "amount": amount,
                                          "reason": "live per-transaction cap"})
        return payrail.ChargeResult(
            False, "blocked", f"amount {amount} exceeds the live "
            f"per-transaction cap {tx_cap}", amount=amount, currency=currency,
            mode="live")
    live_spent = _live_daily_spent(recs, day)
    all_spent = payrail._daily_spent(recs, day)
    if live_spent + amount > live_max_daily() \
            or all_spent + amount > payrail.max_daily():
        payrail._record(user, "blocked", {"nonce": nonce, "amount": amount,
                                          "reason": "live daily cap"})
        return payrail.ChargeResult(
            False, "blocked", f"live daily cap {live_max_daily()} would be "
            f"exceeded ({live_spent}+{amount})", amount=amount,
            currency=currency, mode="live")

    # 6. The payment.live ABC contract — defense in depth at the LAST gate
    # before the adapter. Every flag it re-checks was established above; if any
    # is somehow false here, the imperative flow was bypassed — refuse loudly.
    from . import behavioral_contracts as abc
    ctx = {
        "payment_live_enabled": payrail.live_enabled(),
        "payment_live_adapter_ready": adapter_ready(),
        "payment_user_key_pinned": bool(mandate.user_pinned_pubkeys()),
        "payment_live_within_caps": True,     # established by step 5 above
        # This flow calls enforce_commit with require_cosignature=True, always —
        # the literal above is the ONLY live call site. A refactor that waives
        # co-signature must falsify this flag and fail the contract.
        "payment_live_cosign_enforced": True,
    }
    try:
        abc.enforce("payment.live", ctx)
    except abc.ContractViolation as v:
        payrail._record(user, "blocked", {"nonce": nonce,
                                          "reason": str(v)[:200]})
        raise

    # 7. The adapter — the only line that could ever touch a real processor,
    # and only through the operator-installed adapter.
    try:
        out = _adapter.charge(amount=amount, currency=currency,
                              merchant=merchant, idempotency_key=nonce)
    except Exception as err:
        payrail._record(user, "processor-error", {"nonce": nonce,
                                                  "amount": amount,
                                                  "reason": str(err)[:200]})
        return payrail.ChargeResult(False, "refused",
                                    f"live adapter error: {err}",
                                    amount=amount, currency=currency,
                                    mode="live")
    if not out.get("ok"):
        payrail._record(user, "declined", {"nonce": nonce, "amount": amount})
        return payrail.ChargeResult(False, "refused", "live adapter declined",
                                    amount=amount, currency=currency,
                                    mode="live")

    # 8. The SIGNED 'charged' record IS the commit (idempotency + both daily
    # tallies derive from it). If it can't be recorded, fail closed.
    charge_id = str(out.get("charge_id", ""))
    try:
        ledger_hash = payrail._record(
            user, "charged", {"nonce": nonce, "charge_id": charge_id,
                              "amount": amount, "currency": currency,
                              "day": day, "live": True,
                              "adapter": _adapter.name}, critical=True)
    except Exception as err:
        return payrail.ChargeResult(False, "refused",
                                    f"live charge could not be recorded: {err}",
                                    amount=amount, currency=currency,
                                    mode="live")
    return payrail.ChargeResult(True, "charged", charge_id=charge_id,
                                amount=amount, currency=currency, mode="live",
                                ledger_hash=ledger_hash)


# --- reconciliation ----------------------------------------------------------

def reconcile(user: str) -> dict:
    """Audit every signed LIVE charge against the adapter's own records
    (`LiveAdapter.lookup`). With the disabled default this honestly reports
    unavailable — reconciliation is meaningful only after a human cutover."""
    if not adapter_ready():
        return {"available": False,
                "reason": "no live adapter registered (inert build)",
                "checked": 0, "matched": 0, "mismatched": [], "unavailable": []}
    # An audit is only as good as its source: verify the local ledger first.
    recs = payrail._verified_records(user)
    if recs is None:
        return {"available": False,
                "reason": "payment ledger failed verification — reconcile "
                          "refuses an untrusted local history",
                "checked": 0, "matched": 0, "mismatched": [], "unavailable": []}
    report = {"available": True, "checked": 0, "matched": 0,
              "mismatched": [], "unavailable": []}
    for s in recs:
        st = s.get("state", {})
        if st.get("event") != "charged" or st.get("live") is not True:
            continue
        report["checked"] += 1
        cid = str(st.get("charge_id", ""))
        try:
            remote = _adapter.lookup(cid)
        except Exception as err:
            report["unavailable"].append({"charge_id": cid,
                                          "reason": str(err)[:200]})
            continue
        if (int(remote.get("amount", -1)) == int(st.get("amount", 0) or 0)
                and str(remote.get("currency", "")) == str(st.get("currency"))):
            report["matched"] += 1
        else:
            report["mismatched"].append(
                {"charge_id": cid, "ledger_amount": st.get("amount"),
                 "remote_amount": remote.get("amount")})
    return report
