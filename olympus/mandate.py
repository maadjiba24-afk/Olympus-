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

_LABEL = "mandate/v1"                # the SYSTEM signature subkey
_USER_LABEL = "mandate-user/v1"      # the USER co-signature subkey (distinct key)
# The capability scope a payment mandate is exercised under (M1.2 grants). A
# bound token must grant this scope up to the FINANCIAL_LEGAL risk ceiling.
PAYMENT_SCOPE = "payment.charge"
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
    capability_jti: str = ""       # the M1.2 capability token this is bound to

    def payload(self) -> dict:
        d = {"kind": "cart", **{k: (list(v) if isinstance(v, tuple) else v)
                                for k, v in asdict(self).items()}}
        if not self.capability_jti:
            # Omit the binding key entirely when unbound, so an unbound cart's
            # canonical payload (and signature) is byte-identical to before —
            # capability binding is purely additive and backward compatible.
            d.pop("capability_jti", None)
        return d


@dataclass(frozen=True)
class SignedMandate:
    kind: str                  # "intent" | "cart"
    payload: dict
    public_key: str            # SYSTEM signing subkey
    signature: str             # SYSTEM signature over the canonical payload
    user_public_key: str = ""  # USER co-signing subkey (empty ⇒ single-signed)
    user_signature: str = ""   # USER co-signature over the SAME canonical payload

    @property
    def cosigned(self) -> bool:
        return bool(self.user_signature)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SignedMandate":
        if not isinstance(d, dict) or "payload" not in d:
            raise MandateError("not a signed mandate")
        return cls(kind=str(d.get("kind", "")), payload=dict(d["payload"]),
                   public_key=str(d.get("public_key", "")),
                   signature=str(d.get("signature", "")),
                   user_public_key=str(d.get("user_public_key", "")),
                   user_signature=str(d.get("user_signature", "")))


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
                capability_jti: str = "", now: float | None = None) -> CartMandate:
    """Build a CartMandate referencing an IntentMandate. Structural validation
    only; CONTAINMENT within the intent is checked at verify()/sign() time.
    `capability_jti` (optional) binds the cart to a M1.2 capability token, so its
    authority can be checked against — and revoked via — that grant."""
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
        created_at=now, capability_jti=str(capability_jti or ""))


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


# --- user co-signature (dual-signature, M4) ------------------------------

# --- user co-signer custody (on-device / external key holder) -------------
#
# The user co-signature closes the gap where a compromised SYSTEM key alone
# could mint a "valid" mandate. But if the user key lives in the SAME vault as
# the system key, one vault compromise forges both halves (ADR 0002's residual).
# The fix is custody: the user's PRIVATE key need not be reachable by the agent.
#
#   * A pluggable SIGNER produces the co-signature. The default is vault-local
#     (backward compatible), but `command_signer` delegates to an external
#     process/device (HSM, phone, hardware token) that holds the key and returns
#     only a signature — the agent never sees the private key.
#   * An out-of-band PIN (OLYMPUS_MANDATE_USER_PUBKEY) binds verification to the
#     expected user public key(s). With a pin set, a co-signature minted under
#     ANY other key — including a vault-forged one — is refused. So a vault
#     compromise no longer forges the user half unless the attacker ALSO holds
#     the pinned user key.

# A signer: canonical bytes -> (public_key_hex, signature_hex).
_user_signer = None


def _default_user_signer(canonical: bytes) -> tuple[str, str]:
    """Vault-local co-signer (the backward-compatible default)."""
    return (witness.sub_public_key_hex(_USER_LABEL),
            witness.sign_with(_USER_LABEL, canonical))


def register_user_signer(fn) -> None:
    """Install the user co-signer — an external/on-device key holder. `fn`
    takes the canonical payload bytes and returns (public_key_hex, signature_hex);
    the agent host never handles the private key."""
    global _user_signer
    _user_signer = fn


def reset_user_signer() -> None:
    global _user_signer
    _user_signer = None


def command_signer(cmd: list[str]):
    """A co-signer that shells out to an EXTERNAL device/holder: `cmd` receives
    the canonical payload on stdin and must print JSON {public_key, signature}
    (hex). The private key stays on that device — this host only relays bytes."""
    def sign(canonical: bytes) -> tuple[str, str]:
        import subprocess
        try:
            proc = subprocess.run(cmd, input=canonical, capture_output=True,
                                  timeout=30)
        except (OSError, subprocess.SubprocessError) as err:
            raise MandateError(f"external co-signer failed to run: {err}") from err
        if proc.returncode != 0:
            raise MandateError(
                f"external co-signer exited {proc.returncode}: "
                f"{proc.stderr.decode('utf-8', 'replace')[:200]}")
        try:
            out = json.loads(proc.stdout.decode("utf-8"))
            return str(out["public_key"]), str(out["signature"])
        except (ValueError, KeyError, TypeError) as err:
            raise MandateError(
                f"external co-signer returned malformed output: {err}") from err
    return sign


def user_pinned_pubkeys() -> list[str]:
    """The out-of-band pinned user co-signing key(s) (OLYMPUS_MANDATE_USER_PUBKEY,
    comma-separated), lowercased. Empty ⇒ no pin (vault-local default trust)."""
    import os
    raw = (os.environ.get("OLYMPUS_MANDATE_USER_PUBKEY") or "").strip()
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def co_sign(signed: SignedMandate, *, signer=None) -> SignedMandate:
    """Add the USER co-signature over the SAME canonical payload the system
    signed. `signer` (or the registered one, or the vault default) holds the
    user key — in an on-device deployment it is an external process and the
    private key never reaches this host (see ADR 0002, "On-device user-key
    custody")."""
    if not isinstance(signed, SignedMandate):
        raise MandateError("not a signed mandate")
    sign = signer or _user_signer or _default_user_signer
    pubkey, sig = sign(_canonical(signed.payload))
    return SignedMandate(
        kind=signed.kind, payload=signed.payload, public_key=signed.public_key,
        signature=signed.signature,
        user_public_key=str(pubkey), user_signature=str(sig))


def user_signature_ok(signed: SignedMandate) -> bool:
    """The USER co-signature validates over the payload, and its key is TRUSTED:
    if a pin is configured (OLYMPUS_MANDATE_USER_PUBKEY) the co-signing key must
    be one of the pinned keys — so a vault-forged co-signature under any other
    key is refused; with no pin, the vault-local user subkey is accepted
    (backward compatible). Any field edit invalidates the signature, so a
    co-signature can't be lifted onto a different transaction."""
    if not signed.user_signature:
        return False
    pubkey = (signed.user_public_key or "").lower()
    if not pubkey:
        return False
    pins = user_pinned_pubkeys()
    if pins:
        if pubkey not in pins:
            return False                # not the out-of-band-expected user key
    else:
        try:
            if pubkey != witness.sub_public_key_hex(_USER_LABEL).lower():
                return False            # default trust: only the vault subkey
        except witness.WitnessError:
            return False
    return witness.verify_signature(pubkey, _canonical(signed.payload),
                                    signed.user_signature)


# --- capability-token binding (M1.2 grants, M4) --------------------------

def capability_ok(signed: SignedMandate, capability, *,
                  now: float | None = None,
                  seen_nonces: set[str] | None = None,
                  revoked: set[str] | None = None) -> bool:
    """The mandate's transaction scope is within the capability token it is bound
    to: the token's `jti` matches the payload binding, and `identity.verify_grant`
    accepts it for the PAYMENT scope at FINANCIAL_LEGAL risk (so a forged,
    expired, revoked, or scope-escalating token is refused). A capability grant is
    a REUSABLE, revocable, expiry-bounded authority — it may back more than one
    mandate in its lifetime; per-transaction single-use is the MANDATE nonce's
    job, so this check does not consume the grant's nonce (`seen_nonces` is only
    consulted if a caller opts in)."""
    jti = str(signed.payload.get("capability_jti", ""))
    if not jti or not isinstance(capability, dict):
        return False
    if str((capability.get("payload") or {}).get("jti", "")) != jti:
        return False        # the presented token is not the one bound
    from . import identity
    v = identity.verify_grant(capability, requested_scope=PAYMENT_SCOPE,
                              requested_risk=FINANCIAL_LEGAL, now=now,
                              seen_nonces=seen_nonces, revoked=revoked)
    return bool(v.ok)


# --- human-legible transaction scope -------------------------------------

def transaction_scope(signed: SignedMandate) -> dict:
    """The bounded authorization in plain terms — rendered from the EXACT signed
    payload, so the human-visible summary can never diverge from what is
    co-signed (display/sign parity; construction-injection defense)."""
    p = signed.payload
    if signed.kind == "cart":
        return {"action": "payment", "amount": p.get("amount"),
                "currency": p.get("currency"), "merchant": p.get("merchant"),
                "items": list(p.get("items") or []),
                "capability_jti": p.get("capability_jti", ""),
                "risk_class": FINANCIAL_LEGAL, "auto_executable": False}
    return {"action": "intent", "amount_cap": p.get("amount_cap"),
            "currency": p.get("currency"), "merchants": list(p.get("merchants") or []),
            "item": p.get("item"), "expires_at": p.get("expires_at"),
            "risk_class": FINANCIAL_LEGAL, "auto_executable": False}


def verify(signed: SignedMandate, *, intent: IntentMandate | None = None,
           now: float | None = None, require_cosignature: bool = False,
           seen_nonces: set[str] | None = None) -> VerifyResult:
    """Full verification. Always: valid signature, not expired, fresh nonce. For
    a cart: an `intent` must be supplied and the cart must be contained within
    it. With `require_cosignature=True` the USER co-signature must also validate
    (a system-only mandate is refused, fail closed). `seen_nonces` (if given) is
    consulted for replay AND updated on success."""
    now = time.time() if now is None else now
    reasons: list[str] = []
    if not isinstance(signed, SignedMandate):
        return VerifyResult(False, ("not a signed mandate",))

    if not signature_ok(signed):
        reasons.append("invalid signature")
    if require_cosignature and not user_signature_ok(signed):
        reasons.append("missing or invalid user co-signature")

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
        created_at=float(payload.get("created_at", 0) or 0),
        capability_jti=str(payload.get("capability_jti", "")))


# --- autonomy-dial mapping + ABC governance ------------------------------

def risk_class() -> str:
    """A payment mandate is always FINANCIAL_LEGAL — never auto-runnable."""
    return FINANCIAL_LEGAL


def can_auto_execute() -> bool:
    """Structural guarantee: a payment mandate never auto-executes at any
    autonomy level. Always False — there is no argument that changes it."""
    return False


def enforce_commit(signed_cart: SignedMandate, intent: IntentMandate, *,
                   capability=None, require_cosignature: bool = True,
                   now: float | None = None,
                   seen_nonces: set[str] | None = None,
                   revoked: set[str] | None = None):
    """Gate a cart mandate through the `payment.mandate` ABC contract before it
    could ever back a payment. Builds the verification context and raises
    `behavioral_contracts.ContractViolation` (recovery=block) on any failure.

    Milestone 4 makes the gate two-party and capability-bound: it requires a
    valid USER co-signature and — when the cart is capability-bound — that its
    scope is within the presented token. Returns the VerifyResult on success."""
    from . import behavioral_contracts as abc
    res = verify(signed_cart, intent=intent, now=now,
                 require_cosignature=require_cosignature, seen_nonces=None)
    fresh = True
    nonce = str(signed_cart.payload.get("nonce", ""))
    if seen_nonces is not None:
        fresh = bool(nonce) and nonce not in seen_nonces
    bound_jti = str(signed_cart.payload.get("capability_jti", ""))
    # An unbound cart passes the capability clause vacuously (binding is opt-in);
    # a bound cart must present the matching, in-bound token.
    cap_ok = (True if not bound_jti
              else capability_ok(signed_cart, capability, now=now,
                                 seen_nonces=None, revoked=revoked))
    ctx = {
        "mandate_signature_valid": signature_ok(signed_cart),
        "mandate_cosignature_valid": user_signature_ok(signed_cart),
        "mandate_not_expired": "expired" not in " ".join(res.reasons),
        "mandate_fresh_nonce": fresh,
        "mandate_intent_contained": contained(
            _cart_from_payload(signed_cart.payload), intent, now=now).ok,
        "mandate_trusted_construction": bool(intent.trusted),
        "mandate_capability_within_bound": cap_ok,
    }
    abc.enforce("payment.mandate", ctx)
    if seen_nonces is not None and nonce:
        seen_nonces.add(nonce)
    return res
