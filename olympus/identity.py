"""Signed identity + capability grants (Plane 1.2) — artifact types 2 and 3 on
the ONE authority evaluator.

Reuses the witness root of trust with a DOMAIN-SEPARATED subkey per artifact type
(exactly as `mandate.py` does), so a signature minted for one artifact kind can
never be replayed as another, and the M1.1 signed-artifact discipline (any field
edit invalidates the signature). Grants are EVALUATED by the ABC predicates
(`capability_grant` contract) — this module mints and checks artifacts; it is not
a second policy engine.

  * Agent card  — a signed assertion of an agent's identity, time-bounded.
  * Capability token — a signed GRANT: a subject may exercise a set of scopes up
    to a risk ceiling, until an expiry, once per nonce, unless revoked.

Every failure mode is rejected: forged (bad/foreign signature), expired,
revoked, replayed (nonce reuse), and scope-escalating (a scope or risk beyond
the grant's bound).
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field

from . import actions, config, witness

CARD_SCHEMA = "olympus-agent-card/1"
GRANT_SCHEMA = "olympus-capability/1"
CARD_LABEL = "agent-card/v1"        # witness subkey (domain separation)
GRANT_LABEL = "capability/v1"

_MAX_TTL = 30 * 24 * 3600.0         # a grant may not outlive 30 days
_RISK_RANK = {rc: i for i, rc in enumerate(actions.RISK_CLASSES)}


class IdentityError(ValueError):
    """An identity/grant artifact could not be minted or is malformed."""


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _nonce() -> str:
    return secrets.token_hex(16)


def _seal(schema: str, label: str, payload: dict) -> dict:
    """Sign a payload under a domain-separated witness subkey."""
    return {"schema": schema, "payload": payload,
            "publicKey": witness.sub_public_key_hex(label),
            "signature": witness.sign_with(label, _canonical(payload))}


def _signature_ok(artifact: dict, schema: str, label: str) -> bool:
    """The cryptographic check: the artifact carries our own subkey for THIS
    artifact type and the signature validates the payload (fail closed)."""
    if not isinstance(artifact, dict) or artifact.get("schema") != schema:
        return False
    payload = artifact.get("payload")
    if not isinstance(payload, dict):
        return False
    try:
        expected = witness.sub_public_key_hex(label)
    except witness.WitnessError:
        return False
    if str(artifact.get("publicKey", "")).lower() != expected.lower():
        return False        # no foreign-key / cross-artifact acceptance
    return witness.verify_signature(expected, _canonical(payload),
                                    str(artifact.get("signature", "")))


# --- agent cards ----------------------------------------------------------

def issue_card(agent_id: str, *, ttl: float = 3600.0) -> dict:
    agent_id = (agent_id or "").strip()
    if not agent_id:
        raise IdentityError("agent_id is required")
    if not (0 < float(ttl) <= _MAX_TTL):
        raise IdentityError(f"ttl must be in (0, {_MAX_TTL}] seconds")
    now = time.time()
    return _seal(CARD_SCHEMA, CARD_LABEL, {
        "agent_id": agent_id, "issued_at": now, "expires_at": now + float(ttl),
        "nonce": _nonce()})


def verify_card(card: dict, *, now: float | None = None,
                seen_nonces: set | None = None) -> tuple[bool, tuple[str, ...]]:
    now = time.time() if now is None else now
    reasons: list[str] = []
    if not _signature_ok(card, CARD_SCHEMA, CARD_LABEL):
        return False, ("invalid or foreign signature",)
    p = card["payload"]
    nonce = str(p.get("nonce", ""))
    if not nonce:
        reasons.append("missing nonce")
    elif seen_nonces is not None and nonce in seen_nonces:
        reasons.append("nonce already used (replay)")
    if now >= float(p.get("expires_at", 0) or 0):
        reasons.append("card has expired")
    ok = not reasons
    if ok and seen_nonces is not None and nonce:
        seen_nonces.add(nonce)
    return ok, tuple(reasons)


# --- capability grants (tokens) -------------------------------------------

def grant(subject: str, *, scopes, risk_ceiling: str, ttl: float = 3600.0,
          jti: str | None = None) -> dict:
    subject = (subject or "").strip()
    if not subject:
        raise IdentityError("subject is required")
    if risk_ceiling not in _RISK_RANK:
        raise IdentityError(f"unknown risk ceiling {risk_ceiling!r}")
    if not (0 < float(ttl) <= _MAX_TTL):
        raise IdentityError(f"ttl must be in (0, {_MAX_TTL}] seconds")
    scopes = sorted({str(s).strip() for s in (scopes or []) if str(s).strip()})
    now = time.time()
    return _seal(GRANT_SCHEMA, GRANT_LABEL, {
        "subject": subject, "scopes": scopes, "risk_ceiling": risk_ceiling,
        "issued_at": now, "expires_at": now + float(ttl),
        "nonce": _nonce(), "jti": jti or ("cap_" + _nonce()[:12])})


@dataclass(frozen=True)
class GrantVerdict:
    ok: bool
    signature_valid: bool = False
    not_expired: bool = False
    not_revoked: bool = False
    fresh_nonce: bool = False
    scope_within_bound: bool = False
    reasons: tuple[str, ...] = ()

    def context(self) -> dict:
        """The ABC evaluation context (booleans the capability_grant contract's
        predicates read) for `behavioral_contracts.check('capability.exercise')`."""
        return {"grant_signature_valid": self.signature_valid,
                "grant_not_expired": self.not_expired,
                "grant_not_revoked": self.not_revoked,
                "grant_fresh_nonce": self.fresh_nonce,
                "grant_scope_within_bound": self.scope_within_bound}


def verify_grant(token: dict, *, requested_scope: str | None = None,
                 requested_risk: str | None = None, now: float | None = None,
                 seen_nonces: set | None = None,
                 revoked: set | None = None) -> GrantVerdict:
    """Verify a capability token against a request. Rejects (any of): forged
    signature, expiry, revocation, nonce replay, and scope/risk escalation."""
    now = time.time() if now is None else now
    reasons: list[str] = []

    sig_ok = _signature_ok(token, GRANT_SCHEMA, GRANT_LABEL)
    if not sig_ok:
        # A forged token yields nothing else trustworthy — stop here, fail closed.
        return GrantVerdict(ok=False, reasons=("invalid or foreign signature",))
    p = token["payload"]

    not_expired = now < float(p.get("expires_at", 0) or 0)
    if not not_expired:
        reasons.append("token has expired")

    jti = str(p.get("jti", ""))
    revoked_set = _revoked() if revoked is None else revoked
    not_revoked = bool(jti) and jti not in revoked_set
    if not not_revoked:
        reasons.append("token revoked" if jti else "token missing id (jti)")

    nonce = str(p.get("nonce", ""))
    fresh = bool(nonce) and (seen_nonces is None or nonce not in seen_nonces)
    if not fresh:
        reasons.append("nonce already used (replay)" if nonce else "missing nonce")

    scope_ok = True
    granted_scopes = set(p.get("scopes", []) or [])
    ceiling = str(p.get("risk_ceiling", ""))
    if requested_scope is not None and requested_scope not in granted_scopes:
        scope_ok = False
        reasons.append(f"scope {requested_scope!r} not granted by this token")
    if requested_risk is not None:
        if requested_risk not in _RISK_RANK:
            scope_ok = False
            reasons.append(f"unknown requested risk {requested_risk!r}")
        elif _RISK_RANK[requested_risk] > _RISK_RANK.get(ceiling, -1):
            scope_ok = False
            reasons.append(f"risk {requested_risk!r} exceeds token ceiling "
                           f"{ceiling!r} (escalation)")

    ok = not reasons
    if ok and seen_nonces is not None and nonce:
        seen_nonces.add(nonce)          # consume the nonce only on full success
    return GrantVerdict(ok=ok, signature_valid=sig_ok, not_expired=not_expired,
                        not_revoked=not_revoked, fresh_nonce=fresh,
                        scope_within_bound=scope_ok, reasons=tuple(reasons))


# --- revocation (persisted set of jti) ------------------------------------

def _revocation_path():
    return config.MEMORY_DIR / "capability_revocations.json"


def _revoked() -> set:
    try:
        return set(json.loads(_revocation_path().read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return set()


def revoke(jti: str) -> None:
    """Revoke a capability token by its id — a tighten-only, append-only set.

    Held under `proclock` for the whole read-modify-write. This set is a
    SECURITY control: losing an update here does not merely drop data, it
    silently UN-revokes a capability, because `is_revoked` then answers False
    for a token an operator believes they killed. Two revocations racing (an
    operator in the CLI while the web process revokes on logout, say) is exactly
    when revocation matters most — measured at 11 of 80 revocations lost without
    this lock. The temp file is pid-unique for the same reason: a fixed
    `.json.tmp` is itself a cross-process collision."""
    jti = (jti or "").strip()
    if not jti:
        return
    from . import proclock
    with proclock.lock("capability-revocations"):
        r = _revoked()
        r.add(jti)
        path = _revocation_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(sorted(r)), encoding="utf-8")
        os.replace(tmp, path)


def is_revoked(jti: str) -> bool:
    return (jti or "").strip() in _revoked()
