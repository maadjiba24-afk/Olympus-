"""The persistent-artifact ingestion gate (Wave-2 capability W2-C5).

One authoritative trust boundary for every external artifact that will *persist
or execute*. The doctrine, stated once so it cannot drift
(docs/absorption/10-security-integrity.md rubric 1):

    **Durable ingest is reject-never-repair; ephemeral context is
    sanitize-and-continue.**

`check()` is the durable half: an artifact either passes every stage unchanged
(modulo the deterministic canonicalization defined below) or it is REFUSED with
retained, signed evidence. It never defaults a missing field, never coerces a
wrong type, never truncates an oversize blob, never drops an unparsable member
to salvage the rest. `sanitize_ephemeral()` is the ephemeral half: an
explicitly enumerated, closed set of safe rules for provider responses that are
consumed once and never written — and its output **must still pass the
authoritative schema (`assert_schema`) before execution**.

Invariants (spec §W2-C5.6):

* **W2-I5.1** an *unregistered* kind is REFUSED, not passed. `should_wrap`'s
  fail-closed inversion applied to ingestion: a new import path that forgets to
  register its artifact kind produces a loud refusal, never a silent bypass.
* **W2-I5.2** a persistent artifact is never repaired.
* **W2-I5.3** every refusal produces retained evidence, signed best-effort.
* **W2-I5.4** malformed input cannot bypass validation — proven by the seeded
  mutation sweeps in tests/test_ingestgate.py plus the permanent malformed
  corpus in tests/fixtures/ingest_corpus/.

Canonicalization is the ONLY transformation `check()` is permitted to make, and
it is deterministic, total and idempotent:

    bytes → utf-8 text (strict) → NFC → JSON object

Nothing else. "Repair" means anything beyond that — and the byte-identity
invariant `canonical_bytes(check(k, p)) == canonical_bytes(canonicalize(k, p))`
is what makes the absence of repair *testable* rather than aspirational.

**Enforcement flag.** `OLYMPUS_INGESTGATE` (default OFF). While off, `check()`
is a pass-through that records nothing and refuses nothing, so call sites
can be wired in a separate PR before enforcement is switched on. This PR
delivers the gate, the corpus and the tests only — no call site is wired yet.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import config

# --- data classes (reuse egress's C0/C1/C2 vocabulary, one source of truth) --

try:                                            # pragma: no cover - trivial
    from .egress import DataClass as _EgressDataClass
    C0 = _EgressDataClass.PUBLIC.value          # "C0"
    C1 = _EgressDataClass.OPERATIONAL.value     # "C1"
    C2 = _EgressDataClass.SENSITIVE.value       # "C2"
except Exception:                               # pragma: no cover - fallback
    C0, C1, C2 = "C0", "C1", "C2"

DATA_CLASSES = (C0, C1, C2)


# --- structural bounds (bounds BEFORE parse — rubric 2's CKR discipline) ----
#
# These are global structural caps, deliberately not per-kind: a depth bomb
# or a key bomb is an allocation attack regardless of which kind it claims to
# be, and one number is one thing to audit. Per-kind caps are byte caps only.

MAX_JSON_DEPTH = 24          # nesting deeper than this is a bomb, not a schema
MAX_TOTAL_KEYS = 2000        # object keys across the whole artifact
MAX_TOTAL_NODES = 200_000    # also terminates a self-referential dict payload

# Control characters that must never appear in a durable artifact. TAB, LF
# and CR are legitimate in embedded source and prose and are allowed; NUL is
# called out separately: it is the classic truncation/smuggling primitive.
_CONTROL = re.compile(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]")

# A source string that looks like it is trying to escape its root. Plain
# absolute paths and URLs are legitimate; traversal, home expansion and
# percent-encoded traversal are not.
_TRAVERSAL = re.compile(r"(?:^|[/\\])\.\.(?:[/\\]|$)|%2e%2e|^~", re.IGNORECASE)


# --- the kind registry ------------------------------------------------------
#
# Per kind:
#   max_bytes           hard cap on the RAW payload, enforced before parsing.
#   schema              light structural spec: {"required": {key: type-spec},
#                       "optional": {key: type-spec}}. A type-spec is a Python
#                       type or a tuple of them; `bool` never satisfies `int`.
#   persistent          True  => durable/executable: reject-never-repair, and
#                                sanitize_ephemeral() is ILLEGAL for it.
#                       False => a provider response consumed once and never
#                                written (the C8/tool-ladder boundary and
#                                rubric 2's "advisory data"); still gated, but
#                                eligible for explicit ephemeral sanitation.
#   provenance_required True => a {source, sha256} provenance record must be
#                       supplied and its sha256 must match the payload bytes.
#                       Set for every kind that is durable *code*, durable
#                       *belief*, or a durable *capability grant*.
#   version_key         the field carrying the artifact's version.
#   known_majors        MAJOR versions this build understands. An unknown MAJOR
#                       is REFUSED (never best-effort parsed). [Addition to the
#                       five fields named in the spec: "unknown MAJOR refused"
#                       is unimplementable without declaring the known set.]

_V = "version"
_MAJ1 = frozenset({1})

KINDS: dict[str, dict] = {
    "plugin": {
        "max_bytes": 512 * 1024,        # mirrors pluginstore._MAX_PLUGIN_BYTES
        "schema": {
            "required": {"name": str, "version": str, "code": str},
            "optional": {"description": str, "entrypoint": str,
                         "sha256": str, "size": int},
        },
        "persistent": True, "provenance_required": True,
        "version_key": _V, "known_majors": _MAJ1,
    },
    "skill": {
        "max_bytes": 100 * 1024,          # mirrors skillpack._MAX_SKILL
        "schema": {
            "required": {"name": str, "version": str, "instructions": str},
            "optional": {"description": str, "tags": list, "sha256": str,
                         "size": int},
        },
        "persistent": True, "provenance_required": False,
        "version_key": _V, "known_majors": _MAJ1,
    },
    "mcp_payload": {
        "max_bytes": 1024 * 1024,
        "schema": {
            "required": {"version": str, "result": (dict, list, str)},
            "optional": {"server": str, "tool": str, "is_error": bool},
        },
        "persistent": False, "provenance_required": False,
        "version_key": _V, "known_majors": _MAJ1,
    },
    "memory_import": {
        "max_bytes": 8 * 1024 * 1024,
        "schema": {
            "required": {"version": str, "entries": list},
            "optional": {"user": str, "origin": str},
        },
        "persistent": True, "provenance_required": True,
        "version_key": _V, "known_majors": _MAJ1,
    },
    "session_import": {
        "max_bytes": 8 * 1024 * 1024,
        "schema": {
            "required": {"version": str, "session_id": str, "messages": list},
            "optional": {"user": str, "started": str},
        },
        "persistent": True, "provenance_required": True,
        "version_key": _V, "known_majors": _MAJ1,
    },
    "model_endpoint": {
        "max_bytes": 64 * 1024,
        "schema": {
            "required": {"version": str, "name": str, "base_url": str,
                         "model": str},
            "optional": {"provider": str, "api_key_ref": str,
                         "headers": dict},
        },
        "persistent": True, "provenance_required": False,
        "version_key": _V, "known_majors": _MAJ1,
    },
    "config_bundle": {
        "max_bytes": 1024 * 1024,
        "schema": {
            "required": {"version": str, "settings": dict},
            "optional": {"name": str, "description": str},
        },
        "persistent": True, "provenance_required": True,
        "version_key": _V, "known_majors": _MAJ1,
    },
    "replay_fixture": {
        "max_bytes": 2 * 1024 * 1024,
        "schema": {
            "required": {"version": str, "run_id": str, "files": list},
            "optional": {"model": str, "created": str},
        },
        "persistent": True, "provenance_required": True,
        "version_key": _V, "known_majors": _MAJ1,
    },
    "eval_corpus": {
        "max_bytes": 8 * 1024 * 1024,
        "schema": {
            "required": {"version": str, "name": str, "cases": list},
            "optional": {"description": str},
        },
        "persistent": True, "provenance_required": False,
        "version_key": _V, "known_majors": _MAJ1,
    },
    "policy_file": {
        "max_bytes": 512 * 1024,
        "schema": {
            "required": {"version": str, "name": str, "rules": list},
            "optional": {"description": str, "default": str},
        },
        "persistent": True, "provenance_required": True,
        "version_key": _V, "known_majors": _MAJ1,
    },
    "tool_schema": {
        "max_bytes": 256 * 1024,
        "schema": {
            "required": {"version": str, "name": str, "input_schema": dict},
            "optional": {"description": str},
        },
        "persistent": True, "provenance_required": False,
        "version_key": _V, "known_majors": _MAJ1,
    },
    "provider_metadata": {
        # Advisory provider catalog/pricing (rubric 2: "tolerating malformed
        # *advisory* data with a logged downgrade") — fetched at runtime, never
        # durable, so it is the second legal ephemeral kind.
        "max_bytes": 2 * 1024 * 1024,
        "schema": {
            "required": {"version": str, "models": (list, dict)},
            "optional": {"provider": str, "pricing": dict, "fetched": str},
        },
        "persistent": False, "provenance_required": False,
        "version_key": _V, "known_majors": _MAJ1,
    },
}

# Fields whose bytes a declared hash/size in the artifact refers to (the
# "frontmatter-declared kind must match parsed structure exactly" analogue of
# Colibri's numel×esize cross-check).
_CONTENT_FIELDS = ("code", "content", "instructions", "body")


def registered_kinds() -> tuple[str, ...]:
    """Every kind the gate knows, sorted. Anything else is refused."""
    return tuple(sorted(KINDS))


def is_registered(kind: str) -> bool:
    return kind in KINDS


def kind_policy(kind: str) -> dict:
    """The registered policy for `kind`. Raises KeyError for an unknown kind —
    callers that want a verdict should use check()."""
    return KINDS[kind]


def is_persistent(kind: str) -> bool:
    """True for durable/executable kinds (reject-never-repair). Fail-closed:
    an unregistered kind is treated as persistent."""
    return bool(KINDS.get(kind, {"persistent": True})["persistent"])


def enabled() -> bool:
    """Whether the gate ENFORCES. OFF BY DEFAULT (OLYMPUS_INGESTGATE=1) so call
    sites can be wired ahead of enforcement without changing any behaviour —
    while off, check() is a pass-through that records nothing."""
    return os.environ.get("OLYMPUS_INGESTGATE", "").strip().lower() in (
        "1", "true", "yes", "on")


# --- the refusal ------------------------------------------------------------


class IngestRefused(Exception):
    """A persistent artifact failed the gate. Per-artifact and recoverable —
    deliberately NOT process-fatal (Colibri's `exit(1)` would let one hostile
    skill import take down the whole council)."""

    def __init__(self, kind: str, reasons, evidence_ref: str = "", *,
                 source: str = ""):
        self.kind = str(kind)
        self.reasons: list[str] = [str(r) for r in (reasons or [])]
        self.evidence_ref = str(evidence_ref)
        self.source = str(source)
        detail = "; ".join(self.reasons) or "unspecified"
        ref = f" [evidence {self.evidence_ref}]" if self.evidence_ref else ""
        super().__init__(f"ingest refused ({self.kind}): {detail}{ref}")


@dataclass(frozen=True)
class Accepted:
    """The full verdict for an accepted artifact. `artifact` is exactly what
    check() returns; the rest is the metadata the pipeline computed."""
    kind: str
    artifact: dict
    canonical: bytes
    sha256: str
    data_class: str
    persistent: bool
    source: str = ""


# --- canonicalization -------------------------------------------------------


def canonical_bytes(obj) -> bytes:
    """Deterministic bytes for a JSON value: keys sorted, no insignificant
    whitespace, unicode preserved. The comparison basis for W2-I5.4."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _raw_bytes(payload) -> bytes:
    """The exact bytes an artifact is hashed as. bytes pass through; text is
    utf-8 encoded; an already-parsed value is serialized canonically (that is
    the artifact the caller handed us, so that is what provenance covers)."""
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8", "surrogatepass")
    return canonical_bytes(payload)


def payload_sha256(payload) -> str:
    """sha256 of the payload bytes — the value a publisher puts in provenance.
    Exposed so callers and tests compute it exactly the way the gate does."""
    return hashlib.sha256(_raw_bytes(payload)).hexdigest()


def _sha_best_effort(payload) -> str:
    try:
        return payload_sha256(payload)
    except Exception:
        return ""


class _DuplicateKey(ValueError):
    def __init__(self, key):
        self.key = key
        super().__init__(f"duplicate key {key!r}")


def _no_duplicates(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateKey(key)
        seen.add(key)
    return dict(pairs)


def _text_depth_exceeds(text: str, limit: int) -> bool:
    """Bracket-nesting depth of JSON *text*, scanned before parsing. json.loads
    on a depth bomb raises RecursionError (or, on some builds, segfaults the C
    scanner); bounds must come before the parse, never after."""
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{" or ch == "[":
            depth += 1
            if depth > limit:
                return True
        elif ch == "}" or ch == "]":
            depth -= 1
    return False


def _structure_reasons(obj) -> list[str]:
    """Iterative (recursion-proof, cycle-proof) structural bounds check."""
    stack = [(obj, 1)]
    keys = 0
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_TOTAL_NODES:
            return ["too_many_nodes"]
        if depth > MAX_JSON_DEPTH:
            return ["max_depth_exceeded"]
        if isinstance(node, dict):
            keys += len(node)
            if keys > MAX_TOTAL_KEYS:
                return ["too_many_keys"]
            for key, value in node.items():
                if not isinstance(key, str):
                    return ["non_string_key"]
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            for value in node:
                stack.append((value, depth + 1))
    return []


def _text_reasons(text: str) -> list[str]:
    """Character-level refusals for a single string (key or value)."""
    reasons = []
    if "\x00" in text:
        reasons.append("nul_byte")
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        reasons.append("invalid_utf8")
    if _CONTROL.search(text):
        reasons.append("control_chars")
    return reasons


def _normalize_tree(node, reasons: list[str]):
    """Recursively NFC-normalize every string and reject unrepresentable
    values. Depth is already bounded by _structure_reasons, so the recursion is
    bounded by MAX_JSON_DEPTH."""
    if isinstance(node, str):
        reasons.extend(_text_reasons(node))
        return unicodedata.normalize("NFC", node)
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            reasons.extend(_text_reasons(key))
            out[unicodedata.normalize("NFC", key)] = _normalize_tree(
                value, reasons)
        if len(out) != len(node):
            reasons.append("duplicate_keys")     # collided under NFC
        return out
    if isinstance(node, list):
        return [_normalize_tree(v, reasons) for v in node]
    if node is None or isinstance(node, bool):
        return node
    if isinstance(node, int):
        return node
    if isinstance(node, float):
        if node != node or node in (float("inf"), float("-inf")):
            reasons.append("non_finite_number")
        return node
    reasons.append("unserializable")
    return None


def canonicalize(kind: str, payload) -> dict:
    """bytes → utf-8 text → NFC → JSON object, with every structural bound
    enforced before the parse. The ONLY transformation the gate performs.
    Deterministic, total and idempotent. Raises IngestRefused (without writing
    evidence — check() owns the evidence path) when the payload cannot be
    canonicalized at all."""
    reasons = _canonicalize_reasons(payload)
    if isinstance(reasons, list):
        raise IngestRefused(kind, reasons)
    return reasons


def _canonicalize_reasons(payload):
    """Returns the canonical dict, or a list of refusal reasons."""
    if payload is None:
        return ["empty_payload"]

    if isinstance(payload, (bytes, bytearray, memoryview)):
        raw = bytes(payload)
        if not raw:
            return ["empty_payload"]
        if b"\x00" in raw:
            return ["nul_byte"]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ["invalid_utf8"]
    elif isinstance(payload, str):
        text = payload
        if not text.strip():
            return ["empty_payload"]
    else:
        text = None

    if text is not None:
        char_reasons = _text_reasons(text)
        if char_reasons:
            return char_reasons
        text = unicodedata.normalize("NFC", text)
        if _text_depth_exceeds(text, MAX_JSON_DEPTH):
            return ["max_depth_exceeded"]
        try:
            parsed = json.loads(text, object_pairs_hook=_no_duplicates)
        except _DuplicateKey:
            return ["duplicate_keys"]
        except RecursionError:
            return ["max_depth_exceeded"]
        except (ValueError, TypeError):
            return ["invalid_json"]
    else:
        parsed = payload

    if not isinstance(parsed, dict):
        return ["not_an_object"]
    if not parsed:
        return ["empty_payload"]

    structural = _structure_reasons(parsed)
    if structural:
        return structural

    norm_reasons: list[str] = []
    artifact = _normalize_tree(parsed, norm_reasons)
    if norm_reasons:
        # de-duplicate, preserving order
        return list(dict.fromkeys(norm_reasons))
    return artifact


# --- schema -----------------------------------------------------------------


def _type_ok(value, spec) -> bool:
    types = spec if isinstance(spec, tuple) else (spec,)
    # bool is a subclass of int: an artifact declaring `size: 1` must not be
    # satisfied by `size: true`.
    if isinstance(value, bool) and bool not in types:
        return False
    if int in types and float not in types and isinstance(value, float):
        return False
    return isinstance(value, types)


def schema_reasons(value, schema: dict) -> list[str]:
    """Structural violations of `schema`, empty when the value conforms."""
    if not isinstance(value, dict):
        return ["not_an_object"]
    reasons: list[str] = []
    for key, spec in (schema.get("required") or {}).items():
        if key not in value:
            reasons.append(f"missing_required:{key}")
        elif not _type_ok(value[key], spec):
            reasons.append(f"wrong_type:{key}")
    for key, spec in (schema.get("optional") or {}).items():
        if key in value and not _type_ok(value[key], spec):
            reasons.append(f"wrong_type:{key}")
    return reasons


def assert_schema(value, schema: dict, *, kind: str = "") -> dict:
    """Raise IngestRefused unless `value` satisfies `schema`; return `value`.

    THE MANDATORY SECOND GATE FOR EPHEMERAL DATA. `sanitize_ephemeral()`
    produces something *safe to look at*; it does NOT produce something safe to
    act on. Every caller that turns a sanitized provider response into an
    execution (a tool call, an endpoint choice, a capability grant) must run it
    through the authoritative schema here first — the C8/tool-ladder boundary.
    Purely structural: no I/O, no evidence, no side effects.
    """
    reasons = schema_reasons(value, schema)
    if reasons:
        raise IngestRefused(kind or "?", reasons)
    return value


# --- version policy ---------------------------------------------------------


def _major_of(version) -> int | None:
    """The MAJOR component of a version, or None when unparsable."""
    if isinstance(version, bool):
        return None
    if isinstance(version, int):
        return version if version >= 0 else None
    if not isinstance(version, str):
        return None
    head = version.strip().lstrip("vV").split(".", 1)[0].strip()
    if not head.isdigit():
        return None
    return int(head)


def _version_reasons(artifact: dict, policy: dict) -> list[str]:
    key = policy.get("version_key")
    if not key:
        return []
    major = _major_of(artifact.get(key))
    if major is None:
        return ["malformed_version"]
    if major not in policy["known_majors"]:
        return [f"unknown_major_version:{major}"]
    return []


# --- provenance / source ----------------------------------------------------


def _source_reasons(source: str) -> list[str]:
    if not source:
        return []
    if "\x00" in source or _CONTROL.search(source):
        return ["unsafe_source"]
    if _TRAVERSAL.search(source):
        return ["unsafe_source"]
    return []


def _provenance_reasons(payload, policy: dict, provenance,
                        source: str) -> list[str]:
    reasons = _source_reasons(str(source or ""))
    if provenance is not None:
        if not isinstance(provenance, dict):
            return reasons + ["provenance_malformed"]
        reasons.extend(_source_reasons(str(provenance.get("source") or "")))
    if reasons:
        return list(dict.fromkeys(reasons))

    if provenance is None:
        return ["provenance_missing"] if policy["provenance_required"] else []

    missing = [f for f in ("source", "sha256")
               if not str(provenance.get(f) or "").strip()]
    if missing:
        return [f"provenance_incomplete:{f}" for f in missing]

    declared = str(provenance["sha256"]).strip().lower()
    actual = _sha_best_effort(payload)
    if not actual:
        return ["provenance_unverifiable"]
    if declared != actual:
        return ["provenance_sha_mismatch"]
    return []


# --- integrity (declared-vs-actual cross-checks) ----------------------------


def _integrity_reasons(artifact: dict) -> list[str]:
    """Colibri's numel×esize cross-check, generalised: whatever the artifact
    declares about itself must match what it actually contains."""
    content = None
    for field in _CONTENT_FIELDS:
        value = artifact.get(field)
        if isinstance(value, str):
            content = value
            break
    if content is None:
        return []
    blob = content.encode("utf-8")
    reasons = []
    declared_hash = artifact.get("sha256")
    if isinstance(declared_hash, str) and declared_hash.strip():
        if hashlib.sha256(blob).hexdigest() != declared_hash.strip().lower():
            reasons.append("declared_hash_mismatch")
    declared_size = artifact.get("size")
    if isinstance(declared_size, int) and not isinstance(declared_size, bool):
        if declared_size != len(blob):
            reasons.append("declared_size_mismatch")
    return reasons


# --- classification ---------------------------------------------------------


def classify(artifact) -> str:
    """The artifact's data class (C0/C1/C2), reusing egress's regex classifier
    so "what looks like a secret" has exactly one definition in the codebase.
    Advisory: classification labels an accepted artifact, it never refuses."""
    try:
        from . import egress
        text = canonical_bytes(artifact).decode("utf-8", "replace")
        return egress.classify(text).value
    except Exception:
        return C1      # fail toward the more restrictive label, never C0


# --- refusal evidence (W2-I5.3) ---------------------------------------------

_EVIDENCE_LOCK = threading.Lock()
_EVIDENCE_SEQ = 0


def refusals_path() -> Path:
    d = config.MEMORY_DIR / "ingest"
    d.mkdir(parents=True, exist_ok=True)
    return d / "refusals.jsonl"


def _next_evidence_ref(sha: str) -> str:
    global _EVIDENCE_SEQ
    with _EVIDENCE_LOCK:
        _EVIDENCE_SEQ += 1
        seq = _EVIDENCE_SEQ
    return f"ing-{(sha or '0' * 12)[:12]}-{time.time_ns():x}-{seq}"


def _record_refusal(kind: str, source: str, reasons: list[str],
                    payload) -> str:
    """Append signed refusal evidence and return its evidence_ref.

    NEVER raises: a gate that can be crashed by its own audit path is a gate an
    attacker can turn off. A failure to persist the record is itself recorded
    through errors.capture() (also best-effort) and the refusal still stands.
    """
    sha = _sha_best_effort(payload)
    ref = _next_evidence_ref(sha)
    try:
        core = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "kind": str(kind)[:120],
            "source": str(source or "")[:300],
            "reasons": [str(r)[:200] for r in reasons][:40],
            "sha256": sha,
            "evidence_ref": ref,
        }
        record = dict(core)
        try:
            from . import witness
            payload_bytes = canonical_bytes(core)
            record["signature"] = {
                "publicKey": witness.public_key_hex(),
                "signature": witness.sign(payload_bytes),
                "seedDerivation": witness.SEED_DERIVATION,
            }
            record["signed"] = True
        except Exception as err:
            record["signed"] = False
            record["note"] = (
                f"unsigned: signing unavailable ({type(err).__name__}) — the "
                "refusal is retained regardless")
        with refusals_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as err:                     # pragma: no cover - defensive
        try:
            from . import errors
            errors.capture("ingestgate._record_refusal", err,
                           context=str(kind))
        except Exception:
            pass
    return ref


def refusals(n: int = 100) -> list[dict]:
    """The most recent retained refusal records, oldest-first."""
    path = config.MEMORY_DIR / "ingest" / "refusals.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[-n:]


def verify_refusal(record: dict) -> bool:
    """True if a retained refusal record still carries a valid signature over
    its core. False for an unsigned record or a tampered one."""
    sig = record.get("signature")
    if not record.get("signed") or not isinstance(sig, dict):
        return False
    core = {k: record.get(k) for k in
            ("ts", "kind", "source", "reasons", "sha256", "evidence_ref")}
    try:
        from . import witness
        return witness.verify_signature(
            sig.get("publicKey", ""), canonical_bytes(core),
            sig.get("signature", ""))
    except Exception:
        return False


def _refuse(kind: str, source: str, reasons, payload):
    reasons = list(dict.fromkeys(str(r) for r in reasons)) or ["refused"]
    ref = _record_refusal(kind, source, reasons, payload)
    raise IngestRefused(kind, reasons, ref, source=source)


# --- the gate ---------------------------------------------------------------


def check(kind: str, payload, *, source: str = "", provenance=None):
    """THE chokepoint. Returns the accepted, canonicalized artifact (a dict) or
    raises IngestRefused. Never repairs, never partially accepts.

    Stage order (spec §W2-C5.5) — each stage refuses on its own reasons, so the
    first honest failure is the one reported:

      0. enforcement flag (off => pass-through, records nothing)
      1. registered-kind lookup   -- W2-I5.1, fail-closed
      2. pre-parse byte cap       -- bounds BEFORE parse
      3. canonicalize             -- bytes→utf-8→NFC→JSON, structural bounds
      4. canonical size limit
      5. structural/schema validation
      6. version policy           -- unknown MAJOR refused
      7. provenance + source safety
      8. integrity                -- declared-vs-actual cross-checks
      9. accept

    Stage 10, data classification, is computed by `check_detailed()`: it labels
    an accepted artifact (C0/C1/C2) and never refuses, so keeping it out of
    `check()` lets check() return the artifact itself and makes the byte-
    identity invariant expressible directly on its return value.

    The pre-parse cap at stage 2 is a deliberate strengthening of the spec's
    stated order (canonicalize → size limit): a payload over its cap must die
    before it is decoded and parsed, or the cap does not bound anything. The
    declared canonical size limit is still applied at stage 4.
    """
    if not enabled():
        # Byte-identical when off (§18 ethic): no validation, no canonical-
        # ization, no evidence, no refusal — including for unregistered kinds.
        return payload

    policy = KINDS.get(kind)
    if policy is None:
        _refuse(kind, source, ["unregistered_kind"], payload)

    try:
        raw = _raw_bytes(payload)
    except Exception:
        _refuse(kind, source, ["unserializable"], None)
    if len(raw) > policy["max_bytes"]:
        _refuse(kind, source,
                [f"oversize:{len(raw)}>{policy['max_bytes']}"], payload)

    result = _canonicalize_reasons(payload)
    if isinstance(result, list):
        _refuse(kind, source, result, payload)
    artifact: dict = result

    canonical = canonical_bytes(artifact)
    if len(canonical) > policy["max_bytes"]:
        _refuse(kind, source,
                [f"oversize:{len(canonical)}>{policy['max_bytes']}"], payload)

    reasons = schema_reasons(artifact, policy["schema"])
    if reasons:
        _refuse(kind, source, reasons, payload)

    reasons = _version_reasons(artifact, policy)
    if reasons:
        _refuse(kind, source, reasons, payload)

    reasons = _provenance_reasons(payload, policy, provenance, source)
    if reasons:
        _refuse(kind, source, reasons, payload)

    if policy["persistent"]:
        reasons = _integrity_reasons(artifact)
        if reasons:
            _refuse(kind, source, reasons, payload)

    return artifact


def check_detailed(kind: str, payload, *, source: str = "",
                   provenance=None) -> Accepted:
    """check() plus the metadata the pipeline computed (canonical bytes, sha256
    and the C0/C1/C2 data class). check() returns only the artifact so that the
    byte-identity invariant is expressible directly on its return value."""
    artifact = check(kind, payload, source=source, provenance=provenance)
    if not enabled():
        # Pass-through: no verdict was computed, so report none.
        return Accepted(kind, artifact, b"", "", "", is_persistent(kind),
                        source)
    canonical = canonical_bytes(artifact)
    return Accepted(kind=kind, artifact=artifact, canonical=canonical,
                    sha256=hashlib.sha256(canonical).hexdigest(),
                    data_class=classify(artifact),
                    persistent=bool(KINDS[kind]["persistent"]), source=source)


# --- the ephemeral half -----------------------------------------------------

#: The CLOSED set of sanitation rules. A rule not on this list cannot be
#: requested — "sanitize" must never become an open-ended repair vocabulary.
SANITIZE_RULES = {
    "normalize": "unicode normalization form; only 'NFC' is permitted",
    "strip_control": "drop C0/C1 control characters except TAB/LF/CR",
    "max_len": "truncate each string to at most N characters",
    "max_items": "truncate each list to at most N elements",
    "collapse_whitespace": "collapse runs of whitespace to a single space",
}


def sanitize_ephemeral(kind: str, payload, *, rules: dict):
    """Sanitize-and-continue, for EPHEMERAL kinds only.

    A provider response that is consumed once and never written may be repaired
    within the explicitly enumerated rules in SANITIZE_RULES — and only those.
    An unknown rule name, or a rule with an out-of-contract value, is a
    ValueError: the safe-rule set is closed by construction.

    **The result is NOT validated.** Sanitation makes a value safe to *look
    at*; it does not make it safe to *act on*. Every caller must pass the
    result through `assert_schema(value, KINDS[kind]["schema"], kind=kind)`
    before it reaches execution.

    Refuses outright — regardless of the enforcement flag, and with retained
    evidence — for an unregistered kind or for any PERSISTENT kind. Applying
    ephemeral sanitation to a durable artifact is exactly the reject-never-
    repair violation this module exists to prevent, so it is a refusal, not a
    warning.
    """
    policy = KINDS.get(kind)
    if policy is None:
        _refuse(kind, "", ["unregistered_kind",
                           "ephemeral_sanitation_on_unregistered_kind"],
                payload)
    if policy["persistent"]:
        _refuse(kind, "", ["ephemeral_sanitation_on_persistent_kind"], payload)

    unknown = [r for r in rules if r not in SANITIZE_RULES]
    if unknown:
        raise ValueError(
            f"unknown sanitation rule(s): {', '.join(sorted(unknown))} — "
            f"only {', '.join(sorted(SANITIZE_RULES))} are permitted")

    form = rules.get("normalize", "NFC")
    if form not in (None, "NFC"):
        raise ValueError(f"unsupported normalization form {form!r}; only NFC")
    max_len = rules.get("max_len")
    max_items = rules.get("max_items")
    for name, value in (("max_len", max_len), ("max_items", max_items)):
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, int) or value < 0):
            raise ValueError(f"{name} must be a non-negative int")

    strip_control = bool(rules.get("strip_control", False))
    collapse = bool(rules.get("collapse_whitespace", False))

    def _string(text: str) -> str:
        text = text.replace("\x00", "")
        if strip_control:
            text = _CONTROL.sub("", text)
        if form:
            text = unicodedata.normalize(form, text)
        if collapse:
            text = re.sub(r"\s+", " ", text).strip()
        if max_len is not None:
            text = text[:max_len]
        return text

    def _walk(node, depth: int):
        if depth > MAX_JSON_DEPTH:
            return None
        if isinstance(node, str):
            return _string(node)
        if isinstance(node, dict):
            return {_string(str(k)): _walk(v, depth + 1)
                    for k, v in node.items()}
        if isinstance(node, list):
            items = node if max_items is None else node[:max_items]
            return [_walk(v, depth + 1) for v in items]
        return node

    return _walk(payload, 1)
