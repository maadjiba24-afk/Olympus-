"""Attested Human Handoff — the moat core.

Olympus never solves or bypasses a human-verification check (CAPTCHA / 2FA /
step-up). Instead, when one is detected, it hands the live session to the human,
and when the human clears it, records a CRYPTOGRAPHICALLY SIGNED attestation:

    "a human cleared a <kind> verification on <domain> at <time>"

signed with the same Ed25519 root-of-trust that signs the decision log
(`olympus.witness`). Every credentialed action taken afterward can reference the
attestation id, so the audit trail carries a PROVABLE chain back to a real human
clearing exactly the checks that require a human.

This is a capability a bypass-first agent structurally cannot produce: having
defeated the human, it has nothing to attest. Respecting the control *is* the
differentiator.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os

from . import config, security, witness

SCHEMA = "olympus-attest/1"
KINDS = ("captcha", "otp", "step_up")


class AttestError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat()


def _core(kind: str, domain: str, at: str) -> dict:
    """The signed body: only the facts, so structurally-equal attestations sign
    identically (via witness.canonical_json)."""
    return {"schema": SCHEMA, "kind": kind, "domain": domain, "at": at}


def _id(core: dict) -> str:
    return "att_" + hashlib.sha256(
        witness.canonical_json(core)).hexdigest()[:16]


def attest(kind: str, domain: str, *, at: str | None = None) -> dict:
    """Build and SIGN a human-verification attestation. Requires the crypto
    backend; raises AttestError without it (we must not mint an unsigned proof)."""
    if not witness.available():
        raise AttestError("cannot attest without the cryptography backend")
    kind = kind if kind in KINDS else "step_up"
    domain = (domain or "").strip().lower()[:200]
    at = at or _now_iso()
    core = _core(kind, domain, at)
    payload = witness.canonical_json(core)
    record = dict(core)
    record["id"] = _id(core)
    record["integrity"] = {
        "publicKey": witness.public_key_hex(),
        "signature": witness.sign(payload),
        "seedDerivation": witness.SEED_DERIVATION,
    }
    return record


def verify_attestation(record: dict, *, pin: str | None = None) -> bool:
    """True iff `record` is a well-formed attestation validly signed by a TRUSTED
    key. Any tampering with kind/domain/at breaks the signature. With an explicit
    `pin` (or OLYMPUS_ATTEST_PIN), the signer MUST be that key — the pinning path
    for a third-party verifier holding the expected public key out-of-band; with
    no pin, the instance verifies its own signing key. Fails closed without
    crypto."""
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        return False
    if not witness.available():
        return False
    integ = record.get("integrity") or {}
    pub = (integ.get("publicKey") or "").strip()
    if not pub or record.get("kind") not in KINDS:
        return False
    core = _core(record.get("kind", ""), record.get("domain", ""),
                 record.get("at", ""))
    payload = witness.canonical_json(core)
    if not witness.verify_signature(pub, payload, integ.get("signature", "")):
        return False
    if pin is None:
        pin = (os.environ.get("OLYMPUS_ATTEST_PIN") or "").strip() or None
    if pin:
        return pub.lower() == pin.lower()
    return pub == witness.public_key_hex()


_RECEIPT_HEADER = "-----OLYMPUS ATTESTATION-----"
_RECEIPT_FOOTER = "-----END OLYMPUS ATTESTATION-----"


def export_receipt(record: dict) -> str:
    """Render a signed attestation as a portable, human-readable RECEIPT a third
    party can verify out of band — the moat made outward-facing. It carries the
    facts, the signer's public key, and the signature; nothing secret. Pair it
    with the expected pinned key (published once, out of band) to verify."""
    core = _core(record.get("kind", ""), record.get("domain", ""),
                 record.get("at", ""))
    integ = record.get("integrity") or {}
    body = {
        "schema": record.get("schema", SCHEMA),
        "id": record.get("id", _id(core)),
        **core,
        "publicKey": integ.get("publicKey", ""),
        "signature": integ.get("signature", ""),
    }
    return (_RECEIPT_HEADER + "\n"
            + json.dumps(body, indent=2, sort_keys=True) + "\n"
            + _RECEIPT_FOOTER)


def verify_receipt(text: str, *, pin: str | None = None) -> dict:
    """Verify an exported receipt (the inverse of export_receipt). Returns
    {ok, kind, domain, at, signer, trusted, problems}. `ok` means the signature
    is valid over the facts; `trusted` additionally means it was signed by the
    expected pinned key (`pin` or OLYMPUS_ATTEST_PIN) — the check a third-party
    verifier runs holding the key out of band. Fails closed without crypto."""
    body = _parse_receipt(text)
    if body is None:
        return {"ok": False, "trusted": False,
                "problems": ["not a well-formed Olympus attestation receipt"]}
    record = {
        "schema": body.get("schema"), "kind": body.get("kind"),
        "domain": body.get("domain"), "at": body.get("at"),
        "id": body.get("id"),
        "integrity": {"publicKey": body.get("publicKey"),
                      "signature": body.get("signature")},
    }
    signer = (body.get("publicKey") or "").strip()
    ok = verify_attestation(record)                 # signature valid at all?
    out = {"ok": bool(ok), "kind": body.get("kind"), "domain": body.get("domain"),
           "at": body.get("at"), "signer": signer, "trusted": False,
           "problems": []}
    if not ok:
        out["problems"].append("signature INVALID — the receipt was altered or "
                               "not signed by a real key")
        return out
    if pin is None:
        pin = (os.environ.get("OLYMPUS_ATTEST_PIN") or "").strip() or None
    out["trusted"] = bool(pin) and signer.lower() == pin.lower()
    if pin and not out["trusted"]:
        out["problems"].append("valid signature, but NOT by the expected pinned "
                               "key (untrusted signer)")
    return out


def _parse_receipt(text: str) -> dict | None:
    if not isinstance(text, str) or _RECEIPT_HEADER not in text:
        return None
    try:
        inner = text.split(_RECEIPT_HEADER, 1)[1].split(_RECEIPT_FOOTER, 1)[0]
        body = json.loads(inner)
    except (IndexError, json.JSONDecodeError, TypeError):
        return None
    return body if isinstance(body, dict) else None


def _path():
    return config.MEMORY_DIR / "attestations.jsonl"


def record_attestation(record: dict) -> dict:
    """Append a signed attestation to the durable ledger (append-only JSONL)."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def attest_and_record(kind: str, domain: str) -> dict:
    """Sign a human-cleared-a-check attestation and persist it. Returns the
    record (with its `id`)."""
    return record_attestation(attest(kind, domain))


def list_attestations(domain: str = "") -> list[dict]:
    """Signed attestations, newest last, optionally filtered to one domain.
    Malformed lines are skipped (tolerant of a hand-edited ledger)."""
    path = _path()
    if not path.exists():
        return []
    d = (domain or "").strip().lower()
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(rec, dict) or rec.get("schema") != SCHEMA:
            continue
        if d and rec.get("domain") != d:
            continue
        out.append(rec)
    return out


def latest_attestation(domain: str) -> dict | None:
    """The most recent VALID attestation for a domain, or None. Used to bind a
    credentialed action to the human-check that preceded it."""
    for rec in reversed(list_attestations(domain)):
        if verify_attestation(rec):
            return rec
    return None


def burden_by_domain() -> dict[str, int]:
    """How many human-checks each site has required (from the attestation ledger)
    — the metric the moat drives toward zero as saved sessions cut re-checks."""
    counts: dict[str, int] = {}
    for r in list_attestations():
        d = r.get("domain", "")
        counts[d] = counts.get(d, 0) + 1
    return counts


def evolution_report() -> str:
    """METIS-facing: where the human-check burden sits, so the operator can cut
    it — a saved session (browser_save_auth) + 'remember this device' means the
    human clears a check ONCE and the agent reuses it, so the rate should fall
    over time. Highlights the heaviest sites to target next."""
    counts = burden_by_domain()
    if not counts:
        return "No human-verification checkpoints have needed a human yet."
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    lines = ["Human-check burden (drive toward zero via saved sessions):"]
    for domain, n in ranked[:10]:
        hint = " — save this session to stop re-clearing it" if n >= 3 else ""
        lines.append(f"- {domain}: {n} human-check(s){hint}")
    return "\n".join(lines)


def summary(domain: str = "") -> str:
    """A short, human-facing rundown of the signed human-attestations — the audit
    trail that proves a human cleared each verification."""
    recs = list_attestations(domain)
    if not recs:
        scope = f" for {domain}" if domain else ""
        return f"No human-verification attestations recorded{scope} yet."
    lines = []
    for r in recs[-20:]:
        ok = "valid" if verify_attestation(r) else "UNVERIFIED"
        lines.append(f"- {r.get('at')} · {r.get('domain')} · {r.get('kind')} "
                     f"({r.get('id')}, {ok})")
    return security.sanitize_for_memory("\n".join(lines))
