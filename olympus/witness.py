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
(`OLYMPUS_SIGNING_SEED`). For a real release the verifier checks the manifest's
key against a **pinned** public key (`OLYMPUS_PINNED_PUBKEY` or a committed
`witness_pubkey.txt`) — so a manifest re-signed with any other key is rejected,
not merely checked for internal consistency. With no secret seed set, the key is
the PUBLIC built-in default: such manifests are marked `"dev": true` and are
accepted only with `--allow-dev` (local integrity, never release authenticity).
See docs/SIGNING.md for key custody. `sign` refuses to write a release under the
default seed unless `--dev` is passed.
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

def _configured_seed() -> str | None:
    """The operator-configured signing seed, or None when custody is not
    configured (callers fall back to the PUBLIC default seed, posture 'dev').

    The single acquisition point used by BOTH `_seed_bytes()` and
    `is_default_seed()`, so posture can never diverge from key derivation.
    Read fresh on every call — never cached — so posture always reflects the
    live environment (tests monkeypatch it; injected credentials can rotate).

    Custody sources, strictest first:
    - `OLYMPUS_SIGNING_SEED_FILE`: read the file (systemd `LoadCredential`,
      Docker/K8s secrets). A configured-but-broken file is a hard error —
      never a silent downgrade to the forgeable default seed.
    - `OLYMPUS_SIGNING_SEED`: the seed itself in the environment.
    - Both set → error (ambiguous custody; refusing to guess).
    """
    env_seed = (os.environ.get("OLYMPUS_SIGNING_SEED") or "").strip()
    path = (os.environ.get("OLYMPUS_SIGNING_SEED_FILE") or "").strip()
    if env_seed and path:
        raise WitnessError(
            "both OLYMPUS_SIGNING_SEED and OLYMPUS_SIGNING_SEED_FILE are set — "
            "ambiguous key custody; refusing to guess which key is "
            "authoritative. Unset one of them.")
    if path:
        p = Path(path)
        # POSIX only: a seed readable by group/other is not custody. Windows
        # ACLs don't surface through st_mode's group/other bits, so the check
        # would be meaningless noise there — skipped by design.
        if os.name == "posix":
            try:
                mode = os.stat(p).st_mode & 0o777
            except OSError as err:
                raise WitnessError(
                    f"OLYMPUS_SIGNING_SEED_FILE points at {path!r} but it "
                    f"cannot be read ({err.__class__.__name__}: {err}) — "
                    "refusing to fall back to the public default seed."
                ) from err
            if mode & 0o077:
                raise WitnessError(
                    f"seed file {path!r} has mode {mode:03o} — readable by "
                    f"group/other. Fix: chmod 600 {path}")
        try:
            seed = p.read_text(encoding="utf-8").strip()
        except OSError as err:
            raise WitnessError(
                f"OLYMPUS_SIGNING_SEED_FILE points at {path!r} but it cannot "
                f"be read ({err.__class__.__name__}: {err}) — refusing to "
                "fall back to the public default seed.") from err
        if not seed:
            raise WitnessError(
                f"seed file {path!r} is empty (after stripping whitespace) — "
                "refusing to fall back to the public default seed.")
        return seed
    return env_seed or None


def _seed_bytes() -> bytes:
    seed = _configured_seed() or _DEFAULT_SEED
    return hashlib.sha256(seed.encode("utf-8")).digest()


def is_default_seed() -> bool:
    """True when no secret signing seed is configured — i.e. the key is the
    PUBLIC default and anyone could forge a signature. Such manifests are 'dev'
    only: integrity for local use, never authenticity for a release.
    Raises WitnessError when custody is configured but broken (missing/empty/
    world-readable seed file, or both env and file set)."""
    return _configured_seed() is None


def pinned_pubkey() -> str | None:
    """The canonical public key a verifier trusts, if pinned out-of-band:
    OLYMPUS_PINNED_PUBKEY (hex), else a committed `witness_pubkey.txt`. When set,
    a manifest must be signed by exactly this key — internal consistency with the
    local seed is not enough."""
    env = (os.environ.get("OLYMPUS_PINNED_PUBKEY") or "").strip()
    if env:
        return env.lower()
    f = _package_dir() / "witness_pubkey.txt"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line.lower()
    return None


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


def default_public_key_hex() -> str:
    """The public key derived from the PUBLIC default seed. Lets a verifier
    recognize a run/manifest signed by the forgeable default key regardless of
    the seed currently configured."""
    return pubkey_for_seed(_DEFAULT_SEED)


def pubkey_for_seed(seed: str) -> str:
    """The Ed25519 public key (hex) a given seed derives to — computed from the
    argument alone, never the configured environment. `olympus keygen` uses it
    to print the pin for a seed that was just written to disk."""
    _require_crypto()
    sk = ed25519.Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(seed.encode("utf-8")).digest())
    return sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def write_seed_file(path: Path, *, force: bool = False) -> str:
    """Generate a fresh 256-bit signing seed and write it to `path`, created
    0600 via os.open flags (no write-then-chmod race). Refuses to overwrite an
    existing file unless `force`. Returns the seed so the caller can derive
    its public key in-process — callers must NEVER print or log it."""
    import secrets
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not force:
            raise WitnessError(
                f"{path} already exists — refusing to overwrite a signing "
                "seed (pass --force to replace it; the old key becomes "
                "unrecoverable).")
        path.unlink()
    seed = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (seed + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return seed


def posture() -> str:
    """The signing posture of THIS instance: 'production' when a secret
    OLYMPUS_SIGNING_SEED is configured, else 'dev' (the public default key —
    proves integrity, never authenticity)."""
    return "dev" if is_default_seed() else "production"


def log_signed_by_default(run: dict) -> bool:
    """Whether a recorded run's decision-log signature was produced by the
    PUBLIC default seed — i.e. the run is dev/unverified no matter what seed the
    verifier has configured."""
    sig = (run or {}).get("log_signature") or {}
    pub = (sig.get("publicKey") or "").lower()
    if not pub or not _HAVE_CRYPTO:
        return False
    try:
        return pub == default_public_key_hex()
    except WitnessError:
        return False


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
    if is_default_seed():
        # Mark (inside the signed core) that this was signed by the public
        # default seed, so a verifier can refuse it for anything but local use.
        core["dev"] = True
    payload = canonical_json(core)
    core["integrity"] = {
        "manifestHash": hashlib.sha256(payload).hexdigest(),
        "publicKey": public_key_hex(),
        "signature": sign(payload),
        "seedDerivation": SEED_DERIVATION,
    }
    return core


def write_manifest(path: Path | None = None, *, dev: bool = False) -> Path:
    """Write a signed release manifest. Refuses to sign under the public default
    seed unless `dev=True` (a clearly-marked local manifest), so a forgeable
    release can't be produced by accident."""
    if is_default_seed() and not dev:
        raise WitnessError(
            "refusing to sign a release manifest under the PUBLIC default seed — "
            "anyone could forge it. Set OLYMPUS_SIGNING_SEED to a secret value "
            "(see `olympus witness-pubkey` and docs/SIGNING.md), or pass --dev to "
            "write a clearly-marked local dev manifest.")
    path = path or (_package_dir() / "verification.json")
    path.write_text(json.dumps(build_manifest(), indent=2) + "\n", encoding="utf-8")
    return path


def verify_manifest(manifest: dict, *, base: Path | None = None,
                    pin: str | None = None, allow_dev: bool = False) -> dict:
    """Recompute every file hash and establish trust in the signer. Returns
    {ok, drifted, missing, added, signature_ok, pubkey_trusted, is_dev,
    problems}. `added` lists tracked files present on disk but absent from the
    signed manifest (injected-file detection).

    Trust model (authenticity, not just internal consistency):
    - A **pinned** key (arg, or `pinned_pubkey()`): the manifest MUST be signed
      by exactly that key — a manifest re-signed with any other key fails even
      if its own signature checks out.
    - No pin, **dev** manifest (signed by the public default seed): accepted only
      with `allow_dev=True`, and only as local integrity (never authenticity).
    - No pin, non-dev manifest: rejected — there's no trusted key to check against.
    """
    base = base or _package_dir()
    integrity = manifest.get("integrity") or {}
    core = {k: v for k, v in manifest.items() if k != "integrity"}
    # The signature covers the manifest as built, when summary.verified/failed
    # were 0; normalize before re-checking.
    core_for_sig = json.loads(json.dumps(core))
    core_for_sig["summary"] = {**core_for_sig.get("summary", {}),
                               "verified": 0, "failed": 0}
    payload = canonical_json(core_for_sig)

    drifted, missing = [], []
    manifest_paths = set()
    for entry in manifest.get("files", []):
        manifest_paths.add(entry["path"])
        fp = base / entry["path"]
        if not fp.exists():
            missing.append(entry["path"])
        elif _sha256_file(fp) != entry["sha256"]:
            drifted.append(entry["path"])
    # Detect files ADDED since signing: a tracked file on disk that the signed
    # manifest doesn't cover (e.g. an injected olympus/backdoor.py). Checking
    # only manifest["files"] would let such a file pass verification.
    added = sorted(
        str(p.relative_to(base)) for p in tracked_files(base)
        if str(p.relative_to(base)) not in manifest_paths)

    pub = (integrity.get("publicKey") or "").lower()
    signature_ok = bool(pub) and verify_signature(pub, payload,
                                                  integrity.get("signature", ""))
    is_dev = bool(manifest.get("dev"))
    pin = (pin if pin is not None else pinned_pubkey())
    pin = pin.lower() if pin else None

    problems = []
    for p in drifted:
        problems.append(f"drifted (hash mismatch): {p}")
    for p in missing:
        problems.append(f"missing tracked file: {p}")
    for p in added:
        problems.append(f"untracked file NOT covered by the signed manifest: {p}")

    pubkey_trusted = False
    if not signature_ok:
        problems.append("manifest signature is INVALID (content was altered).")
    elif pin:
        if pub == pin:
            pubkey_trusted = True
        else:
            problems.append("manifest is signed by an UNTRUSTED key "
                            "(does not match the pinned public key).")
    elif is_dev:
        if allow_dev:
            pubkey_trusted = True
        else:
            problems.append("this is a DEV manifest signed by the PUBLIC default "
                            "seed — not trustworthy for a release. Pass "
                            "--allow-dev to accept it for local use.")
    else:
        problems.append("no pinned key configured — cannot establish trust. Set "
                        "OLYMPUS_PINNED_PUBKEY or commit witness_pubkey.txt "
                        "(or use --allow-dev for a dev manifest).")

    return {
        "ok": not problems,
        "drifted": drifted, "missing": missing, "added": added,
        "signature_ok": signature_ok, "pubkey_trusted": pubkey_trusted,
        "is_dev": is_dev, "problems": problems,
    }


def verify_release(manifest_path: Path | None = None, *,
                   allow_dev: bool = False) -> dict:
    path = manifest_path or (_package_dir() / "verification.json")
    if not path.exists():
        return {"ok": False, "problems": [f"no manifest at {path} — run "
                                          "`olympus sign` to create one."],
                "drifted": [], "missing": [], "added": [], "signature_ok": False,
                "pubkey_trusted": False, "is_dev": False}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return verify_manifest(manifest, allow_dev=allow_dev)


# --- signed decision log (Task 1's audit trail) --------------------------

def sign_log(decisions: list[dict]) -> dict:
    """Sign a run's decision path. Returns {publicKey, signature, seedDerivation}
    over `trace.canonical_log` (the replay-invariant decision cores)."""
    from . import trace
    payload = trace.canonical_log(decisions).encode("utf-8")
    return {"publicKey": public_key_hex(), "signature": sign(payload),
            "seedDerivation": SEED_DERIVATION}


def verify_log(decisions: list[dict], log_signature: dict,
               *, pin: str | None = None) -> bool:
    """True if `log_signature` is a valid signature over these decisions by a
    TRUSTED key. Any tampering with the decision path breaks the signature.

    Trust model (note: decision logs are signed at RUNTIME by the running
    instance's key, a different trust domain from the CI-signed *release*
    manifest — so this deliberately does NOT auto-consult the release
    `pinned_pubkey()`, which would reject every default-seed log on a normal
    install):

    - With an explicit **pin**: the log MUST be signed by exactly that key — a
      log re-signed under any other seed is rejected even if its own signature
      checks out. This is the pinning path for a third-party verifier who holds
      the expected public key out-of-band (e.g. `OLYMPUS_LOG_PIN`).
    - With no pin: bind to the locally-configured signing key
      (`public_key_hex()`), i.e. the instance verifies its own logs.
    """
    from . import trace
    # Fail CLOSED without a crypto backend: we cannot verify a signature, so we
    # must not vouch for it. (verify_signature would raise WitnessError here, so
    # this both honors the bool contract and avoids an unreachable fallback.)
    if not _HAVE_CRYPTO:
        return False
    pub = (log_signature.get("publicKey") or "").strip()
    if not pub:
        return False
    payload = trace.canonical_log(decisions).encode("utf-8")
    if not verify_signature(pub, payload, log_signature.get("signature", "")):
        return False
    if pin is None:
        pin = (os.environ.get("OLYMPUS_LOG_PIN") or "").strip() or None
    if pin:
        return pub.lower() == pin.lower()
    return pub == public_key_hex()


def verify_run(run_id: str, *, pin: str | None = None) -> dict:
    """Verify the decision-log signature of a recorded run. Returns
    {ok, found, signed, problems}. Pass `pin` (or set OLYMPUS_LOG_PIN) to bind
    verification to an expected public key held out-of-band, so a log re-signed
    under a different seed is rejected even though its own signature is
    self-consistent; with no pin the instance verifies its own signing key."""
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
    decisions = run.get("decisions", [])
    if verify_log(decisions, sig, pin=pin):
        return {"ok": True, "found": True, "signed": True, "problems": []}
    if not _HAVE_CRYPTO:
        return {"ok": False, "found": True, "signed": True,
                "problems": ["cannot verify the decision-log signature — the "
                             "cryptography backend is unavailable on this host."]}
    # Distinguish a tampered log from a valid-but-untrusted signer for the report.
    pub = (sig.get("publicKey") or "").strip()
    payload = trace.canonical_log(decisions).encode("utf-8")
    sig_ok = bool(pub) and verify_signature(pub, payload, sig.get("signature", ""))
    if not sig_ok:
        problem = ("decision-log signature INVALID — the recorded decisions were "
                   "altered since the run.")
    else:
        problem = ("decision-log signed by an UNTRUSTED key (does not match the "
                   "pinned public key).")
    return {"ok": False, "found": True, "signed": True, "problems": [problem]}
