"""AP2-style payment mandates — creation + verification ONLY (no live rail).

A mandate is a signed, constraint-bound, tamper-evident record that a *human*
authorized a *specific, bounded* financial action. It replaces the status-quo
"approved: true" flag with something a third party could verify. See
`docs/adr/0001-ap2-payment-mandates.md` and `docs/AP2_THREAT_MODEL.md`.

Two kinds (AP2):
  * IntentMandate — the user authorizes an action within CONSTRAINTS (amount
    cap + currency, merchant allowlist, item, expiry). Its constraints may come
    ONLY from the trusted user channel (`trusted=True`); a mandate built from
    untrusted/ingested content is refused (construction-injection defense).
  * CartMandate — the concrete cart the agent assembled; it must fall WITHIN its
    IntentMandate (amount ≤ cap, merchant ∈ allowlist, currency match, not
    expired) before it can be signed.

Hard boundaries of this phase (enforced by the absence of any such code here):
  * No live payment rail, no card/VC issuance, no PSP/merchant network calls.
  * A payment mandate is FINANCIAL_LEGAL risk → it can NEVER auto-execute at any
    autonomy level (`actions._min_level_to_auto` pins it at 99). The mandate is
    the artifact the human produces AT the approval step, never a way around it.

Signing reuses the Ed25519 root of trust (`witness.py`) via a domain-separated
subkey (label `mandate/v1`) — a key distinct from the release/decision-log key,
same custody. No new dependency.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, asdict

from . import witness

_LABEL = "mandate/v1"
_MAX_ITEM = 200
_MAX_MERCHANTS = 50
_MAX_ITEMS = 50
_CURRENCIES = frozenset({
    "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "INR", "BRL"})
FINANCIAL_LEGAL = "irreversible_financial_legal"   # mirrors actions.FINANCIAL_LEGAL


class MandateError(ValueError):
    """A mandate could not be created, signed, or verified."""


# --- data model -----------------------------------------------------------

@dataclass(frozen=True)
class IntentMandate:
    id: str
    user: str
    amount_cap: int            # minor units (e.g. cents); the ceiling
    currency: str
    merchants: tuple[str, ...]  # allowlist (lowercased)
    item: str
    nonce: str
    expires_at: float
    trusted: bool              # constraints came from the trusted user channel
    created_at: float

    def payload(self) -> dict:
        return {"kind": "intent", **{k: (list(v) if isinstance(v, tuple) else v)
                                     for k, v in asdict(self).items()}}


@dataclass(frozen=True)
class CartMandate:
    id: str
    intent_id: str
    user: str
    amount: int
    currency: str
    merchant: str
    items: tuple[str, ...]
    nonce: str
    created_at: float

    def payload(self) -> dict:
        return {"kind": "cart", **{k: (list(v) if isinstance(v, tuple) else v)
                                   for k, v in asdict(self).items()}}


@dataclass(frozen=True)
class SignedMandate:
    kind: str                  # "intent" | "cart"
    payload: dict
    public_key: str
    signature: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SignedMandate":
        if not isinstance(d, dict) or "payload" not in d:
            raise MandateError("not a signed mandate")
        return cls(kind=str(d.get("kind", "")), payload=dict(d["payload"]),
                   public_key=str(d.get("public_key", "")),
                   signature=str(d.get("signature", "")))


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reasons: tuple[str, ...] = ()


# --- canonical serialization ---------------------------------------------

def _canonical(payload: dict) -> bytes:
    """Deterministic bytes over the payload — sorted keys, tight separators — so
    any field edit (or reordering/whitespace) invalidates the signature."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _nonce() -> str:
    return secrets.token_hex(16)


# --- creation -------------------------------------------------------------

def create_intent(user: str, *, amount_cap: int, currency: str,
                  merchants: list[str], item: str, expires_in: float = 3600.0,
                  trusted: bool, nonce: str | None = None,
                  now: float | None = None) -> IntentMandate:
    """Build an IntentMandate. Validates every field. `trusted` MUST be True for
    the mandate to be signable — it asserts the constraints came from the
    trusted user channel, not from ingested/untrusted content."""
    now = time.time() if now is None else now
    if not isinstance(user, str) or not user.strip():
        raise MandateError("user is required")
    if not isinstance(amount_cap, int) or isinstance(amount_cap, bool) \
            or amount_cap <= 0:
        raise MandateError("amount_cap must be a positive integer (minor units)")
    cur = str(currency).upper()
    if cur not in _CURRENCIES:
        raise MandateError(f"unsupported currency {currency!r}")
    ms = [str(m).strip().lower() for m in (merchants or []) if str(m).strip()]
    if not ms:
        raise MandateError("at least one allowed merchant is required")
    if len(ms) > _MAX_MERCHANTS:
        raise MandateError("too many merchants in the allowlist")
    itm = str(item or "").strip()
    if not itm:
        raise MandateError("item description is required")
    try:
        ttl = float(expires_in)
    except (TypeError, ValueError):
        raise MandateError("expires_in must be a number of seconds")
    if ttl <= 0:
        raise MandateError("expires_in must be positive")
    return IntentMandate(
        id="im_" + _nonce()[:12], user=user.strip(), amount_cap=amount_cap,
        currency=cur, merchants=tuple(dict.fromkeys(ms)), item=itm[:_MAX_ITEM],
        nonce=nonce or _nonce(), expires_at=now + ttl, trusted=bool(trusted),
        created_at=now)


def create_cart(intent: IntentMandate, *, amount: int, currency: str,
                merchant: str, items: list[str], nonce: str | None = None,
                now: float | None = None) -> CartMandate:
    """Build a CartMandate referencing an IntentMandate. Structural validation
    only; CONTAINMENT within the intent is checked at verify()/sign() time."""
    now = time.time() if now is None else now
    if not isinstance(intent, IntentMandate):
        raise MandateError("an IntentMandate is required")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise MandateError("amount must be a positive integer (minor units)")
    its = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not its:
        raise MandateError("a cart needs at least one item")
    if len(its) > _MAX_ITEMS:
        raise MandateError("too many items in the cart")
    return CartMandate(
        id="cm_" + _nonce()[:12], intent_id=intent.id, user=intent.user,
        amount=amount, currency=str(currency).upper(),
        merchant=str(merchant or "").strip().lower(),
        items=tuple(i[:_MAX_ITEM] for i in its), nonce=nonce or _nonce(),
        created_at=now)


# --- containment (pure) ---------------------------------------------------

def contained(cart: CartMandate, intent: IntentMandate,
              now: float | None = None) -> VerifyResult:
    """Is `cart` within `intent`? Amount ≤ cap, currency match, merchant in the
    allowlist, same user, intent not expired. Pure."""
    now = time.time() if now is None else now
    reasons: list[str] = []
    if cart.intent_id != intent.id:
        reasons.append("cart does not reference this intent")
    if cart.user != intent.user:
        reasons.append("cart/intent user mismatch")
    if cart.currency != intent.currency:
        reasons.append("currency mismatch")
    if cart.amount > intent.amount_cap:
        reasons.append(f"amount {cart.amount} exceeds cap {intent.amount_cap}")
    if cart.merchant not in intent.merchants:
        reasons.append(f"merchant {cart.merchant!r} not in allowlist")
    if now >= intent.expires_at:
        reasons.append("intent has expired")
    return VerifyResult(not reasons, tuple(reasons))


# --- signing + verification ----------------------------------------------

def sign(mandate: IntentMandate | CartMandate) -> SignedMandate:
    """Sign a mandate with the domain-separated `mandate/v1` subkey. An
    IntentMandate MUST be trusted-constructed to be signable."""
    if isinstance(mandate, IntentMandate) and not mandate.trusted:
        raise MandateError(
            "refusing to sign an intent whose constraints are not from the "
            "trusted user channel (construction-injection defense)")
    payload = mandate.payload()
    sig = witness.sign_with(_LABEL, _canonical(payload))
    return SignedMandate(kind=payload["kind"], payload=payload,
                         public_key=witness.sub_public_key_hex(_LABEL),
                         signature=sig)


def signature_ok(signed: SignedMandate) -> bool:
    """Just the cryptographic check: does the signature validate for the payload
    against the current `mandate/v1` public key? (Also rejects a mandate whose
    embedded public key isn't ours — no unknown-key acceptance.)"""
    try:
        expected = witness.sub_public_key_hex(_LABEL)
    except witness.WitnessError:
        return False
    if (signed.public_key or "").lower() != expected.lower():
        return False
    return witness.verify_signature(expected, _canonical(signed.payload),
                                    signed.signature)


def verify(signed: SignedMandate, *, intent: IntentMandate | None = None,
           now: float | None = None,
           seen_nonces: set[str] | None = None) -> VerifyResult:
    """Full verification. Always: valid signature, not expired, fresh nonce. For
    a cart: an `intent` must be supplied and the cart must be contained within
    it. `seen_nonces` (if given) is consulted for replay AND updated on success."""
    now = time.time() if now is None else now
    reasons: list[str] = []
    if not isinstance(signed, SignedMandate):
        return VerifyResult(False, ("not a signed mandate",))

    if not signature_ok(signed):
        reasons.append("invalid signature")

    payload = signed.payload
    nonce = str(payload.get("nonce", ""))
    if not nonce:
        reasons.append("missing nonce")
    elif seen_nonces is not None and nonce in seen_nonces:
        reasons.append("nonce already used (replay)")

    if signed.kind == "intent":
        exp = float(payload.get("expires_at", 0) or 0)
        if now >= exp:
            reasons.append("intent has expired")
        if not bool(payload.get("trusted")):
            reasons.append("intent not trusted-constructed")
    elif signed.kind == "cart":
        if intent is None:
            reasons.append("cart verification requires its intent")
        else:
            c = _cart_from_payload(payload)
            reasons.extend(contained(c, intent, now=now).reasons)
    else:
        reasons.append(f"unknown mandate kind {signed.kind!r}")

    ok = not reasons
    if ok and seen_nonces is not None and nonce:
        seen_nonces.add(nonce)          # consume the nonce on success
    return VerifyResult(ok, tuple(reasons))


def _cart_from_payload(payload: dict) -> CartMandate:
    return CartMandate(
        id=str(payload.get("id", "")), intent_id=str(payload.get("intent_id", "")),
        user=str(payload.get("user", "")), amount=int(payload.get("amount", 0) or 0),
        currency=str(payload.get("currency", "")),
        merchant=str(payload.get("merchant", "")),
        items=tuple(str(i) for i in (payload.get("items") or [])),
        nonce=str(payload.get("nonce", "")),
        created_at=float(payload.get("created_at", 0) or 0))


# --- autonomy-dial mapping + ABC governance ------------------------------

def risk_class() -> str:
    """A payment mandate is always FINANCIAL_LEGAL — never auto-runnable."""
    return FINANCIAL_LEGAL


def can_auto_execute() -> bool:
    """Structural guarantee: a payment mandate never auto-executes at any
    autonomy level. Always False — there is no argument that changes it."""
    return False


def enforce_commit(signed_cart: SignedMandate, intent: IntentMandate, *,
                   now: float | None = None,
                   seen_nonces: set[str] | None = None):
    """Gate a cart mandate through the `payment.mandate` ABC contract before it
    could ever back a payment. Builds the verification context and raises
    `behavioral_contracts.ContractViolation` (recovery=block) on any failure.
    Returns the VerifyResult on success."""
    from . import behavioral_contracts as abc
    res = verify(signed_cart, intent=intent, now=now, seen_nonces=None)
    fresh = True
    nonce = str(signed_cart.payload.get("nonce", ""))
    if seen_nonces is not None:
        fresh = bool(nonce) and nonce not in seen_nonces
    ctx = {
        "mandate_signature_valid": signature_ok(signed_cart),
        "mandate_not_expired": "expired" not in " ".join(res.reasons),
        "mandate_fresh_nonce": fresh,
        "mandate_intent_contained": contained(
            _cart_from_payload(signed_cart.payload), intent, now=now).ok,
        "mandate_trusted_construction": bool(intent.trusted),
    }
    abc.enforce("payment.mandate", ctx)
    if seen_nonces is not None and nonce:
        seen_nonces.add(nonce)
    return res
