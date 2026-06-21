"""Signed releases + signed decision log — one Ed25519 root of trust.

This mirrors Ruflo's witness/verify pattern in Python, using the `cryptography`
dependency Olympus already ships (Ed25519 — no new dependency):

- A **release manifest** lists every tracked package file with its SHA-256, plus
  git commit/branch, and is signed with an Ed25519 key. `olympus verify`
  recomputes each hash, re-derives the public key from the signing seed, checks
  the signature, and exits non-zero naming any drifted file.
- The **same key signs the Task-1 decision log** (`trace.canonical_log`), so the
  audit trail is tamper-evident too — the thing Ruflo signs for trajectories,
  Olympus signs for the whole decision log.

Root of trust: an Ed25519 key derived deterministically from a seed
(`OLYMPUS_SIGNING_SEED`, or a built-in default for local/dev use). The public
key is embedded in what it signs *and* re-derived from the seed at verify time,
so re-signing tampered content with a different key is detected. Keep the seed
secret in production (set `OLYMPUS_SIGNING_SEED`); the default seed is public and
provides integrity, not authenticity.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from . import config

SCHEMA = "olympus-witness/1"

# Public, deterministic default — fine for local integrity checks. Production
# sets OLYMPUS_SIGNING_SEED to a secret value and pins the derived public key.
_DEFAULT_SEED = "olympus-witness-root-of-trust/v1"
SEED_DERIVATION = "ed25519(sha256(seed))"

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    serialization, ed25519  # noqa: B018  (touch so linters see the use)
    _HAVE_CRYPTO = True
except BaseException:                      # missing or broken native backend
    _HAVE_CRYPTO = False
    InvalidSignature = Exception


class WitnessError(RuntimeError):
    pass


# --- canonical encoding (Ruflo's canonicalJSON discipline) ----------------

def canonical_json(obj) -> bytes:
    """Deterministic bytes: keys sorted recursively, None/undefined dropped, so
    structurally-equal payloads encode — and therefore sign — identically."""
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in sorted(o.items()) if v is not None}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        return o
    return json.dumps(clean(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


# --- the key -------------------------------------------------------------

def _seed_bytes() -> bytes:
    seed = os.environ.get("OLYMPUS_SIGNING_SEED") or _DEFAULT_SEED
    return hashlib.sha256(seed.encode("utf-8")).digest()


def _require_crypto() -> None:
    if not _HAVE_CRYPTO:
        raise WitnessError(
            "the 'cryptography' package is required for signing/verification — "
            "`pip install cryptography`.")


def signing_key() -> "ed25519.Ed25519PrivateKey":
    _require_crypto()
    return ed25519.Ed25519PrivateKey.from_private_bytes(_seed_bytes())


def public_key_hex() -> str:
    pk = signing_key().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return pk.hex()


def available() -> bool:
    """True if signing/verification can actually run (crypto backend present)."""
    return _HAVE_CRYPTO


def sign(data: bytes) -> str:
    return signing_key().sign(data).hex()


def verify_signature(public_key_hex: str, data: bytes, signature_hex: str) -> bool:
    _require_crypto()
    try:
        pk = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pk.verify(bytes.fromhex(signature_hex), data)
        return True
    except (InvalidSignature, ValueError):
        return False


# --- release manifest ----------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def tracked_files(root: Path | None = None) -> list[Path]:
    """The package files a release vouches for: all .py, prompt .md, and .json
    under the package, except the manifest itself (it can't sign its own hash)."""
    root = root or _package_dir()
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.name == "verification.json":
            continue
        if p.suffix in (".py", ".md", ".json"):
            out.append(p)
    return out


def _git_info(root: Path) -> dict:
    def run(*args):
        try:
            return subprocess.check_output(
                ["git", *args], cwd=root,
                stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            return None
    return {"gitCommit": run("rev-parse", "HEAD"),
            "branch": run("rev-parse", "--abbrev-ref", "HEAD")}


def build_manifest(root: Path | None = None) -> dict:
    """Build and sign a release manifest over the tracked package files."""
    pkg = _package_dir()
    root = root or pkg.parent
    files = [{"path": p.relative_to(pkg).as_posix(), "sha256": _sha256_file(p)}
             for p in tracked_files(pkg)]
    core = {
        "schema": SCHEMA,
        **_git_info(root),
        "files": files,
        "summary": {"total": len(files), "verified": 0, "failed": 0},
    }
    payload = canonical_json(core)
    core["integrity"] = {
        "manifestHash": hashlib.sha256(payload).hexdigest(),
        "publicKey": public_key_hex(),
        "signature": sign(payload),
        "seedDerivation": SEED_DERIVATION,
    }
    return core


def write_manifest(path: Path | None = None) -> Path:
    path = path or (_package_dir() / "verification.json")
    path.write_text(json.dumps(build_manifest(), indent=2) + "\n", encoding="utf-8")
    return path


def verify_manifest(manifest: dict, *, base: Path | None = None) -> dict:
    """Recompute every file hash and check the signature. Returns
    {ok, drifted, missing, signature_ok, pubkey_trusted, problems}."""
    base = base or _package_dir()
    integrity = manifest.get("integrity") or {}
    core = {k: v for k, v in manifest.items() if k != "integrity"}
    # The signature covers the manifest exactly as it was at build time, when
    # summary.verified/failed were 0; normalize before re-checking.
    core_for_sig = json.loads(json.dumps(core))
    core_for_sig["summary"] = {**core_for_sig.get("summary", {}),
                               "verified": 0, "failed": 0}
    payload = canonical_json(core_for_sig)

    drifted, missing = [], []
    for entry in manifest.get("files", []):
        fp = base / entry["path"]
        if not fp.exists():
            missing.append(entry["path"])
        elif _sha256_file(fp) != entry["sha256"]:
            drifted.append(entry["path"])

    pub = integrity.get("publicKey", "")
    signature_ok = bool(pub) and verify_signature(pub, payload,
                                                  integrity.get("signature", ""))
    # Re-derive the expected key from the seed: a manifest re-signed with a
    # different key is caught here even if its own signature is internally valid.
    pubkey_trusted = (pub == public_key_hex()) if _HAVE_CRYPTO else False

    problems = []
    for p in drifted:
        problems.append(f"drifted (hash mismatch): {p}")
    for p in missing:
        problems.append(f"missing tracked file: {p}")
    if not signature_ok:
        problems.append("manifest signature is INVALID (content was altered).")
    if signature_ok and not pubkey_trusted:
        problems.append("manifest is signed by an UNTRUSTED key "
                        "(does not match the signing seed).")
    return {
        "ok": not problems,
        "drifted": drifted, "missing": missing,
        "signature_ok": signature_ok, "pubkey_trusted": pubkey_trusted,
        "problems": problems,
    }


def verify_release(manifest_path: Path | None = None) -> dict:
    path = manifest_path or (_package_dir() / "verification.json")
    if not path.exists():
        return {"ok": False, "problems": [f"no manifest at {path} — run "
                                          "`olympus sign` to create one."],
                "drifted": [], "missing": [], "signature_ok": False,
                "pubkey_trusted": False}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return verify_manifest(manifest)


# --- signed decision log (Task 1's audit trail) --------------------------

def sign_log(decisions: list[dict]) -> dict:
    """Sign a run's decision path. Returns {publicKey, signature, seedDerivation}
    over `trace.canonical_log` (the replay-invariant decision cores)."""
    from . import trace
    payload = trace.canonical_log(decisions).encode("utf-8")
    return {"publicKey": public_key_hex(), "signature": sign(payload),
            "seedDerivation": SEED_DERIVATION}


def verify_log(decisions: list[dict], log_signature: dict) -> bool:
    """True if `log_signature` is a valid signature over these decisions by the
    trusted key. Any tampering with the decision path breaks this."""
    from . import trace
    pub = log_signature.get("publicKey", "")
    if not pub or (_HAVE_CRYPTO and pub != public_key_hex()):
        return False
    payload = trace.canonical_log(decisions).encode("utf-8")
    return verify_signature(pub, payload, log_signature.get("signature", ""))


def verify_run(run_id: str) -> dict:
    """Verify the decision-log signature of a recorded run. Returns
    {ok, found, signed, problems}."""
    from . import trace
    run = trace.load_run(run_id)
    if not run:
        return {"ok": False, "found": False, "signed": False,
                "problems": [f"no recorded run '{run_id}'"]}
    sig = run.get("log_signature")
    if not sig:
        return {"ok": False, "found": True, "signed": False,
                "problems": ["run has no decision-log signature "
                             "(recorded before signing, or crypto unavailable)"]}
    ok = verify_log(run.get("decisions", []), sig)
    return {"ok": ok, "found": True, "signed": True,
            "problems": [] if ok else
            ["decision-log signature INVALID — the recorded decisions were "
             "altered since the run."]}
