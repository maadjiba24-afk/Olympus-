#!/usr/bin/env python3
"""Executable release-pipeline validation and signing (Step 1L v6).

WHY THIS IS A SCRIPT, NOT INLINE YAML
-------------------------------------
Release gating logic embedded in workflow `run:` blocks cannot be unit
tested: it only executes on a release run, on a runner, with the signing
seed and the publishing credential already in reach. Every nontrivial check
therefore lives here, where the adversarial cases are exercised
deterministically by `tests/test_release_pipeline_helper.py`.

WHY IT IMPORTS NO OLYMPUS CODE
------------------------------
`python -m olympus sign` pulls the entire CLI import graph
(`cli -> heartbeat -> gateway -> orchestrator -> agent -> llm -> anthropic`),
so it cannot run in the minimal, hash-locked environment the signing job
deliberately installs. This module therefore implements signing and
verification directly on the standard library plus `cryptography`, in a
format byte-compatible with `olympus.witness`:

    seed_bytes = sha256(seed.encode()).digest()
    key        = Ed25519PrivateKey.from_private_bytes(seed_bytes)
    payload    = canonical_json(core)      # sorted keys, None dropped
    signature  = key.sign(payload).hex()

TRUST MODEL
-----------
Nothing is trusted because it arrived in an artifact. Every source-processing
job checks out the exact commit itself; the only thing that crosses a job
boundary is `verification.json` (signed) and the built distributions (bound
to that signature). The signer must derive EXACTLY the pinned public key in
`olympus/witness_pubkey.txt`, so a leaked-but-different seed cannot produce a
release that verifies.

FAILURE REPORTING
-----------------
Every check fails closed and raises `ReleaseCheckError`. Messages name the
KIND of problem and a COUNT — never a rejected path, archive member, digest,
manifest value, seed, or environment value, because these strings reach CI
logs that a release engineer reads and an attacker may influence.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import configparser
import csv
import email.errors
import email.headerregistry
import email.parser
import email.policy
import glob
import gzip
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import tomllib
import unicodedata
import zipfile
import zlib
from pathlib import Path, PurePosixPath

# A release tag is STABLE ONLY: vX.Y.Z. `\A`/`\Z`, never `$`, because `$`
# also matches immediately before a trailing newline.
TAG_RE = re.compile(r"\Av(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\Z")
_VERSION_RE = re.compile(r'(?m)^version = "(?P<version>[^"]+)"$')
_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_HEXKEY_RE = re.compile(r"\A[0-9a-f]{64}\Z")

_PROJECT = "olympus-council"
_DIST_STEM = "olympus_council"
PACKAGE = "olympus"
MANIFEST_NAME = "verification.json"
MANIFEST_SCHEMA = "olympus-witness/1"
SEED_DERIVATION = "ed25519(sha256(seed))"
PUBKEY_FILE = "witness_pubkey.txt"

# Generated interpreter artefacts must never be tracked, signed, or shipped.
# Native modules are intentionally NOT on this list: a reviewed, Git-tracked
# .pyd/.so/.dll is release content and is protected by the same signed hash as
# every other package file.
BYTECODE_SUFFIXES = (".pyc", ".pyo")
CACHE_DIRS = ("__pycache__",)

_WHEEL_DIST_INFO_FILES = frozenset({
    "METADATA",
    "WHEEL",
    "entry_points.txt",
    "top_level.txt",
    "licenses/LICENSE",
    "RECORD",
})
_SDIST_EGG_INFO_FILES = frozenset({
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "top_level.txt",
})
_SETUPTOOLS_METADATA_POLICY_VERSION = "84.0.0"
_CORE_SINGLETON_HEADERS = frozenset({
    "metadata-version",
    "name",
    "version",
    "summary",
    "author",
    "author-email",
    "maintainer",
    "maintainer-email",
    "license-expression",
    "license",
    "keywords",
    "requires-python",
    "description-content-type",
})


class ReleaseCheckError(Exception):
    """A release invariant did not hold. Messages are closed and sanitized."""


def _fail(message: str) -> "ReleaseCheckError":
    return ReleaseCheckError(message)


# --- path safety --------------------------------------------------------------

def ensure_safe_relpath(path) -> str:
    """A relative, POSIX, traversal-free path, or fail closed.

    The rejected value is NEVER echoed: a manifest or archive member is
    attacker-influenced text and this message reaches a CI log.
    """
    if type(path) is not str or not path:
        raise _fail("an entry has a missing or non-string path "
                    "(value withheld)")
    if "\x00" in path:
        raise _fail("an entry path contains a NUL byte (value withheld)")
    if "\\" in path:
        raise _fail("an entry path uses a backslash separator "
                    "(value withheld)")
    if path.startswith("/") or re.match(r"\A[A-Za-z]:", path):
        raise _fail("an entry path is absolute or drive-qualified "
                    "(value withheld)")
    # RAW segments, not `PurePosixPath.parts`: pathlib normalizes `./a.py`
    # to `a.py` and `a/./b.py` to `a/b.py`, so a normalized inspection
    # cannot see the dot segments at all and would accept both.
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise _fail("an entry path is not a clean relative path "
                    "(value withheld)")
    return path


def _collision_key(path: str) -> str:
    """One key per path that a case-insensitive or Unicode-normalizing
    filesystem would treat as the same file."""
    return unicodedata.normalize("NFC", path).casefold()


def ensure_no_path_collisions(paths) -> None:
    seen: set[str] = set()
    for path in paths:
        ensure_safe_relpath(path)
        key = _collision_key(path)
        if key in seen:
            raise _fail("two entries collide under case folding or Unicode "
                        "normalization (values withheld)")
        seen.add(key)


def _is_bytecode(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return path.endswith(BYTECODE_SUFFIXES) or any(
        part in CACHE_DIRS for part in parts)


# --- crypto (stdlib + cryptography only) --------------------------------------

def _require_crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except Exception as err:                                    # noqa: BLE001
        raise _fail(f"the cryptography package is unavailable "
                    f"({type(err).__name__})")
    return ed25519, serialization


def canonical_json(obj) -> bytes:
    """Byte-identical to `olympus.witness.canonical_json`."""
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in sorted(o.items())
                    if v is not None}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        return o
    return json.dumps(clean(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _seed_bytes(seed: str) -> bytes:
    return hashlib.sha256(seed.encode("utf-8")).digest()


def public_key_for_seed(seed: str) -> str:
    ed25519, serialization = _require_crypto()
    key = ed25519.Ed25519PrivateKey.from_private_bytes(_seed_bytes(seed))
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _configured_seed() -> str:
    seed = (os.environ.get("OLYMPUS_SIGNING_SEED") or "").strip()
    if not seed:
        raise _fail("OLYMPUS_SIGNING_SEED is not set — refusing to sign "
                    "under the public default seed")
    return seed


def pinned_key(root: Path) -> str:
    """The single trusted release key committed to the repository."""
    path = root / PACKAGE / PUBKEY_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise _fail(f"the pinned public key file is unreadable "
                    f"({type(err).__name__})")
    keys = [line.strip().lower() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]
    if len(keys) != 1:
        raise _fail(f"the pinned key file lists {len(keys)} keys, expected "
                    f"exactly 1 for a release")
    if not _HEXKEY_RE.match(keys[0]):
        raise _fail("the pinned key is not a 32-byte hex key "
                    "(value withheld)")
    return keys[0]


# --- git-derived source of truth ----------------------------------------------

def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True)
    except OSError as err:
        raise _fail(f"git could not be executed ({type(err).__name__})")
    if proc.returncode != 0:
        raise _fail(f"git {args[0]} failed with exit {proc.returncode}")
    return proc.stdout


def ensure_clean_worktree(root: Path) -> None:
    """No modifications, no staged changes, no untracked files.

    A release is cut from exactly what review saw. An untracked file is not
    merely untidy: it is an unreviewed input that a build would happily
    package.
    """
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    entries = [line for line in status.splitlines() if line.strip()]
    if entries:
        raise _fail(f"the working tree is not clean: {len(entries)} "
                    f"modified/untracked path(s) (names withheld)")


def _tracked_regular_files(root: Path, pathspec: str | None = None) -> list[str]:
    """Every Git-tracked regular file in *pathspec*, repository-relative.

    Git index modes, rather than a filesystem walk, are the source of truth.
    Submodules, symlinks and other non-regular entries are release-fatal.
    """
    args = ["ls-files", "-s"]
    if pathspec is not None:
        args.extend(("--", pathspec))
    listing = _git(root, *args)
    out: list[str] = []
    for line in listing.splitlines():
        if not line.strip():
            continue
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) < 3 or not path:
            raise _fail("git returned a malformed tracked-file entry")
        mode = fields[0]
        rel_repo = ensure_safe_relpath(path)
        if mode == "120000":
            raise _fail("a tracked release input is a symlink; release inputs "
                        "must be regular files (name withheld)")
        if mode != "100644" and mode != "100755":
            raise _fail("a tracked release input is not a regular file "
                        "(name withheld)")
        if _is_bytecode(rel_repo):
            raise _fail("tracked bytecode or __pycache__ content found in "
                        "the release source — generated code must never be signed "
                        "or shipped (name withheld)")
        if rel_repo == f"{PACKAGE}/{MANIFEST_NAME}":
            continue                         # a manifest cannot sign itself
        out.append(rel_repo)
    ensure_no_path_collisions(out)
    if not out:
        raise _fail("no tracked regular release files to sign — an empty "
                    "manifest would vouch for nothing")
    return sorted(out)


def tracked_release_files(root: Path) -> list[str]:
    """Every Git-tracked regular package file, package-relative."""
    prefix = PACKAGE + "/"
    files = _tracked_regular_files(root, PACKAGE)
    out = [path[len(prefix):] for path in files if path.startswith(prefix)]
    ensure_no_path_collisions(out)
    if not out:
        raise _fail("no tracked package files to sign — an empty manifest "
                    "would vouch for nothing")
    return out


def tracked_source_files(root: Path) -> list[str]:
    """Every Git-tracked regular repository file, repository-relative."""
    return _tracked_regular_files(root)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_entries(root: Path, paths: list[str]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for rel in paths:
        path = root / rel
        if path.is_symlink() or not path.is_file():
            raise _fail("a tracked release file is missing or non-regular "
                        "in the worktree (name withheld)")
        entries.append({"path": rel, "sha256": _sha256_file(path)})
    return entries


def _require_commit(value, *, label: str) -> str:
    if type(value) is not str or not _SHA_RE.fullmatch(value):
        raise _fail(f"{label} is not exactly 40 lowercase hex characters")
    return value


# --- tag / version / commit identity ------------------------------------------

def version_from_tag(tag) -> str:
    if type(tag) is not str:
        raise _fail("tag is not a string")
    match = TAG_RE.match(tag)
    if not match:
        raise _fail("the supplied tag is not a stable release tag of the "
                    "form vX.Y.Z (value withheld)")
    return match.group("version")


def pyproject_version(root: Path) -> str:
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError as err:
        raise _fail(f"pyproject.toml is unreadable ({type(err).__name__})")
    matches = _VERSION_RE.findall(text)
    if len(matches) != 1:
        raise _fail(f"pyproject.toml declares {len(matches)} top-level "
                    f"version lines, expected exactly 1")
    return matches[0]


def runtime_version() -> str:
    import importlib.metadata

    try:
        return importlib.metadata.version(_PROJECT)
    except Exception as err:                                    # noqa: BLE001
        raise _fail(f"the installed runtime version is unavailable "
                    f"({type(err).__name__})")


def check_toolchain(python_version: str, pip_version: str) -> None:
    """Require the exact interpreter and installer used to prove the locks."""
    import importlib.metadata

    if platform.python_version() != python_version:
        raise _fail("the runner Python version is not the approved exact "
                    "version")
    try:
        installed_pip = importlib.metadata.version("pip")
    except Exception as err:                                    # noqa: BLE001
        raise _fail(f"the runner pip version is unavailable "
                    f"({type(err).__name__})")
    if installed_pip != pip_version:
        raise _fail("the runner pip version is not the approved exact version")


def check_tag(tag, root: Path, *, check_runtime: bool = True) -> str:
    version = version_from_tag(tag)
    packaged = pyproject_version(root)
    if packaged != version:
        raise _fail("the tag and pyproject.toml name different versions")
    if check_runtime and runtime_version() != version:
        raise _fail("the tag and the installed runtime name different "
                    "versions")
    return version


def check_commit_is_protected_main_tip(sha, root: Path, *,
                                       remote: str = "origin",
                                       branch: str = "main") -> str:
    """The release commit must EQUAL the freshly fetched protected tip.

    Ancestry is not enough: an ancestor-only rule lets a tag point at a
    commit that a later fix superseded.
    """
    if type(sha) is not str or not _SHA_RE.match(sha):
        raise _fail("the release commit SHA is not a 40-hex string")
    _git(root, "fetch", "--no-tags", remote, branch)
    tip = _git(root, "rev-parse", f"{remote}/{branch}").strip()
    if sha != tip:
        raise _fail("the release commit is not the protected branch tip — "
                    "only the current reviewed tip may be released")
    return tip


def check_tag_points_at(tag, sha, root: Path) -> str:
    """The tag must already exist and peel exactly to the release commit.

    Peeling (`^{commit}`) resolves annotated tags to their commit, so both
    lightweight and annotated tags are handled without accepting a tag that
    points at some other object.
    """
    version_from_tag(tag)
    if type(sha) is not str or not _SHA_RE.match(sha):
        raise _fail("the release commit SHA is not a 40-hex string")
    listing = _git(root, "tag", "--list", tag).strip()
    if not listing:
        raise _fail("the supplied tag does not exist in this repository")
    peeled = _git(root, "rev-list", "-n", "1", tag).strip()
    if not _SHA_RE.match(peeled):
        raise _fail("the supplied tag does not peel to a commit")
    if peeled != sha:
        raise _fail("the supplied tag does not point at the release commit")
    return peeled


def check_dispatch_ref(ref, ref_type) -> None:
    """A manual release may be dispatched only from protected ``main``."""
    if type(ref) is not str or type(ref_type) is not str or \
            ref != "refs/heads/main" or ref_type != "branch":
        raise _fail("the release workflow was not dispatched from the "
                    "protected main branch")


# --- the signed manifest -------------------------------------------------------

def build_manifest(root: Path, *, expected_commit: str) -> dict:
    """A release manifest over git-tracked package files, format-compatible
    with `olympus.witness.build_manifest`, plus a repository source map."""
    expected_commit = _require_commit(expected_commit,
                                      label="the expected release commit")
    package = root / PACKAGE
    files = _file_entries(package, tracked_release_files(root))
    source_files = _file_entries(root, tracked_source_files(root))
    commit = _git(root, "rev-parse", "HEAD").strip()
    if commit != expected_commit:
        raise _fail("the checked-out commit is not the expected release "
                    "commit")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() or None
    return {
        "schema": MANIFEST_SCHEMA,
        "gitCommit": commit,
        "branch": branch,
        "files": files,
        "sourceFiles": source_files,
        "summary": {"total": len(files), "verified": 0, "failed": 0},
    }


def sign_manifest(root: Path, *, expected_commit: str,
                  expected_key: str | None = None,
                  output_path: Path | None = None) -> Path:
    """Sign the release manifest with the configured seed.

    The derived public key must equal the pinned trusted key EXACTLY: a
    signature from any other seed is not a release, and discovering that at
    verification time would be too late.
    """
    ed25519, _serialization = _require_crypto()
    expected = (expected_key or pinned_key(root)).lower()
    if not _HEXKEY_RE.match(expected):
        raise _fail("the expected public key is not a 32-byte hex key")

    ensure_clean_worktree(root)
    seed = _configured_seed()
    derived = public_key_for_seed(seed)
    if derived != expected:
        raise _fail("the configured signing seed does not derive the pinned "
                    "release key — refusing to sign (keys withheld)")

    core = build_manifest(root, expected_commit=expected_commit)
    payload = canonical_json(core)
    key = ed25519.Ed25519PrivateKey.from_private_bytes(_seed_bytes(seed))
    core["integrity"] = {
        "manifestHash": hashlib.sha256(payload).hexdigest(),
        "publicKey": derived,
        "signature": key.sign(payload).hex(),
        "seedDerivation": SEED_DERIVATION,
    }
    default_out = (root / PACKAGE / MANIFEST_NAME).resolve()
    out = (Path(output_path) if output_path is not None else default_out).resolve()
    if out.name != MANIFEST_NAME:
        raise _fail("the manifest output must use the exact verification.json "
                    "filename")
    if out.is_relative_to(root.resolve()) and out != default_out:
        raise _fail("the manifest output cannot overwrite release source")
    try:
        out.write_text(json.dumps(core, indent=2) + "\n", encoding="utf-8")
    except OSError as err:
        raise _fail(f"the manifest output is unavailable "
                    f"({type(err).__name__})")
    return out


def check_manifest_schema(manifest, *, _check_maps: bool = True) -> None:
    """Exact schema, validated before any signature check so a malformed
    document can never reach the verifier as a crash."""
    if not isinstance(manifest, dict):
        raise _fail("manifest is not a mapping")
    allowed_top = {"schema", "gitCommit", "branch", "files", "sourceFiles",
                   "summary", "integrity"}
    if set(manifest) != allowed_top:
        raise _fail("manifest has missing or unexpected top-level fields")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise _fail("manifest schema is not the expected release schema "
                    "(value withheld)")
    if manifest.get("dev") is not None:
        raise _fail("manifest is marked dev — it was signed with the public "
                    "default seed and is forgeable")
    _require_commit(manifest.get("gitCommit"),
                    label="manifest gitCommit")
    files = manifest.get("files")
    source_files = manifest.get("sourceFiles")
    if not isinstance(files, list) or not files:
        raise _fail("manifest files is not a non-empty list")
    if not isinstance(source_files, list) or not source_files:
        raise _fail("manifest sourceFiles is not a non-empty list")

    def check_entries(entries, label: str) -> list[str]:
        paths = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
                raise _fail(f"manifest holds a malformed {label} entry")
            path, digest = entry["path"], entry["sha256"]
            ensure_safe_relpath(path)
            if type(digest) is not str or not _SHA256_RE.fullmatch(digest):
                raise _fail(f"a manifest {label} entry has a malformed digest "
                            "(value withheld)")
            if _is_bytecode(path):
                raise _fail(f"a manifest {label} entry names bytecode or "
                            "cache content")
            paths.append(path)
        ensure_no_path_collisions(paths)
        return paths

    package_paths = check_entries(files, "package-file")
    source_paths = check_entries(source_files, "source-file")
    if MANIFEST_NAME in package_paths or \
            f"{PACKAGE}/{MANIFEST_NAME}" in source_paths:
        raise _fail("the manifest cannot include itself in a signed file map")
    source_set = set(source_paths)
    missing_sources = [path for path in package_paths
                       if f"{PACKAGE}/{path}" not in source_set]
    if missing_sources:
        raise _fail("the repository source map omits signed package content")
    if _check_maps:
        _check_manifest_map_consistency(manifest)
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict):
        raise _fail("manifest has no integrity block")
    if set(integrity) != {"manifestHash", "publicKey", "signature",
                          "seedDerivation"}:
        raise _fail("manifest integrity has missing or unexpected fields")
    for field in ("manifestHash", "publicKey", "signature"):
        value = integrity.get(field)
        if type(value) is not str or not value:
            raise _fail(f"manifest integrity.{field} is missing or malformed")
    if not _SHA256_RE.fullmatch(integrity["manifestHash"]):
        raise _fail("manifest integrity.manifestHash is not a SHA-256 digest")
    if not _HEXKEY_RE.fullmatch(integrity["publicKey"]):
        raise _fail("manifest integrity.publicKey is not a 32-byte hex key")
    if not re.fullmatch(r"[0-9a-f]{128}", integrity["signature"]):
        raise _fail("manifest integrity.signature is not a 64-byte hex value")
    if integrity.get("seedDerivation") != SEED_DERIVATION:
        raise _fail("manifest integrity.seedDerivation is unexpected")
    summary = manifest.get("summary")
    if not isinstance(summary, dict) or set(summary) != {
            "total", "verified", "failed"} or \
            summary.get("total") != len(files) or \
            summary.get("verified") != 0 or summary.get("failed") != 0:
        raise _fail("manifest summary.total disagrees with its file list")


def _check_manifest_map_consistency(manifest) -> None:
    source_digests = {entry["path"]: entry["sha256"]
                      for entry in manifest["sourceFiles"]}
    if any(source_digests[f"{PACKAGE}/{entry['path']}"] != entry["sha256"]
           for entry in manifest["files"]):
        raise _fail("the signed package and repository hash maps disagree")


def authenticate_manifest(manifest, *, expected_key: str,
                          expected_commit: str) -> dict:
    """Authenticate schema, commit, signer, manifest hash and Ed25519 signature.

    Deliberately does no filesystem I/O, so the exact same primitive can be
    applied independently to the downloaded manifest and each archive copy.
    """
    ed25519, _serialization = _require_crypto()
    # Cross-map digest agreement is checked only after authenticity. That way
    # a backend that edits one map but preserves the old integrity block is
    # rejected specifically by the cryptographic post-build check.
    check_manifest_schema(manifest, _check_maps=False)
    expected_commit = _require_commit(expected_commit,
                                      label="the expected release commit")
    if manifest["gitCommit"] != expected_commit:
        raise _fail("manifest gitCommit is not the expected release commit")
    if type(expected_key) is not str or not _HEXKEY_RE.fullmatch(
            expected_key.lower()):
        raise _fail("the expected public key is not a 32-byte hex key")
    expected = expected_key.lower()
    integrity = manifest["integrity"]
    if integrity["publicKey"].lower() != expected:
        raise _fail("the manifest was signed by an untrusted key")

    core = {k: v for k, v in manifest.items() if k != "integrity"}
    core["summary"] = {**core.get("summary", {}), "verified": 0, "failed": 0}
    payload = canonical_json(core)
    if hashlib.sha256(payload).hexdigest() != integrity["manifestHash"]:
        raise _fail("the manifest hash does not match its own content")
    try:
        key = ed25519.Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(expected))
        key.verify(bytes.fromhex(integrity["signature"]), payload)
    except Exception as err:                                    # noqa: BLE001
        raise _fail(f"the manifest signature does not verify "
                    f"({type(err).__name__})")
    _check_manifest_map_consistency(manifest)
    return {"signature_ok": True,
            "files": len(manifest["files"]),
            "source_files": len(manifest["sourceFiles"])}


def verify_manifest_native(manifest, package_root: Path, *,
                           expected_commit: str,
                           source_root: Path | None = None,
                           expected_key: str) -> dict:
    """Schema, signature, signer identity, then file hashes — in that order.

    Implemented natively so the build job needs no olympus import and so the
    signer-identity check can never be skipped by omitting an argument:
    `expected_key` is REQUIRED.
    """
    report = authenticate_manifest(manifest, expected_key=expected_key,
                                   expected_commit=expected_commit)
    package_entries = {entry["path"]: entry["sha256"]
                       for entry in manifest["files"]}
    if source_root is not None:
        tracked_package = set(tracked_release_files(source_root))
        if set(package_entries) != tracked_package:
            raise _fail("a tracked package file is not covered by the signed "
                        "package map, or the map contains unsigned content")
        tracked_source = set(tracked_source_files(source_root))
        manifest_source = {entry["path"]
                           for entry in manifest["sourceFiles"]}
        if manifest_source != tracked_source:
            raise _fail("the signed repository source map does not exactly "
                        "cover the tracked regular files")
    drifted = missing = 0
    for entry in manifest["files"]:
        path = package_root / entry["path"]
        if path.is_symlink() or not path.is_file():
            missing += 1
        elif _sha256_file(path) != entry["sha256"]:
            drifted += 1
    if missing or drifted:
        raise _fail(f"the signed manifest does not match the tree: "
                    f"{missing} missing, {drifted} modified (names withheld)")
    if source_root is not None:
        source_missing = source_drifted = 0
        for entry in manifest["sourceFiles"]:
            path = source_root / entry["path"]
            if path.is_symlink() or not path.is_file():
                source_missing += 1
            elif _sha256_file(path) != entry["sha256"]:
                source_drifted += 1
        if source_missing or source_drifted:
            raise _fail("the signed manifest does not match the repository "
                        f"source: {source_missing} missing, "
                        f"{source_drifted} modified (names withheld)")
    return report


# --- archive member safety -----------------------------------------------------

def check_wheel_members(wheel: Path, version: str) -> dict[str, bytes]:
    """Every wheel member, proven safe, with RECORD validated.

    Returns the member map so callers do not reopen the archive.
    """
    try:
        archive = zipfile.ZipFile(wheel)
    except (zipfile.BadZipFile, OSError) as err:
        raise _fail(f"the wheel is not a readable zip archive "
                    f"({type(err).__name__})")
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        ensure_no_path_collisions(names)
        for info in infos:
            if info.filename.endswith("/"):
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise _fail("the wheel holds a symlink member — a wheel must "
                            "contain regular files only (name withheld)")
            if mode and mode != 0o100000:
                raise _fail("the wheel holds a non-regular member "
                            "(name withheld)")
        try:
            blobs = {name: archive.read(name) for name in names
                     if not name.endswith("/")}
        except (zipfile.BadZipFile, OSError, RuntimeError, zlib.error) as err:
            raise _fail(f"a wheel member could not be read "
                        f"({type(err).__name__})")

    record_name = f"{_DIST_STEM}-{version}.dist-info/RECORD"
    if record_name not in blobs:
        raise _fail("the wheel has no RECORD for this version")
    recorded: dict[str, tuple[str, str]] = {}
    try:
        rows = list(csv.reader(
            blobs[record_name].decode("utf-8").splitlines()))
    except (UnicodeDecodeError, csv.Error) as err:
        raise _fail(f"the wheel RECORD is unreadable ({type(err).__name__})")
    for row in rows:
        if not row:
            continue
        if len(row) != 3:
            raise _fail("the wheel RECORD has a malformed row")
        name = ensure_safe_relpath(row[0])
        if name in recorded:
            raise _fail("the wheel RECORD contains a duplicate path")
        recorded[name] = (row[1], row[2])
    ensure_no_path_collisions(recorded)
    if record_name not in recorded or recorded[record_name] != ("", ""):
        raise _fail("the wheel RECORD does not carry its required empty "
                    "self-entry")

    for name, blob in blobs.items():
        if name == record_name:
            continue
        if name not in recorded:
            raise _fail("a wheel member is absent from RECORD "
                        "(name withheld)")
        digest, size = recorded[name]
        if not digest.startswith("sha256="):
            raise _fail("a RECORD entry has no sha256 digest "
                        "(name withheld)")
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(blob).digest()).rstrip(b"=").decode()
        if digest[len("sha256="):] != expected:
            raise _fail("a wheel member does not match its RECORD digest "
                        "(name withheld)")
        if size and str(len(blob)) != size:
            raise _fail("a wheel member does not match its RECORD size "
                        "(name withheld)")
    for name in recorded:
        if name != record_name and name not in blobs:
            raise _fail("RECORD lists a member the wheel does not contain "
                        "(name withheld)")
    return blobs


def check_sdist_members(sdist: Path, version: str) -> dict[str, bytes]:
    """Every sdist member, proven safe. Links and devices are rejected."""
    root = f"{_DIST_STEM}-{version}"
    try:
        compressed = sdist.read_bytes()
        # Validate the complete immutable gzip snapshot before tar parsing.
        # tarfile stops at the tar end marker, which can otherwise leave a
        # corrupt gzip trailer unread.  Parsing the same snapshot below also
        # avoids a check/use race on the artifact path.
        raw_tar = gzip.decompress(compressed)
    except (OSError, EOFError, zlib.error) as err:
        raise _fail(f"the sdist gzip stream could not be read completely "
                    f"({type(err).__name__})")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(compressed), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError, zlib.error) as err:
        raise _fail(f"the sdist is not a readable gzip tar archive "
                    f"({type(err).__name__})")
    blobs: dict[str, bytes] = {}
    directories: set[str] = set()
    with archive:
        try:
            members = archive.getmembers()
        except (tarfile.TarError, OSError, EOFError, zlib.error) as err:
            raise _fail(f"the sdist member table is unreadable "
                        f"({type(err).__name__})")
        tail = raw_tar[archive.offset:]
        if len(tail) < 2 * tarfile.BLOCKSIZE or \
                len(tail) % tarfile.BLOCKSIZE or any(tail):
            raise _fail("the sdist tar archive end marker or zero padding "
                        "is invalid")
        names = [m.name for m in members]
        ensure_no_path_collisions(names)
        for member in members:
            ensure_safe_relpath(member.name)
            if member.issym() or member.islnk():
                raise _fail("the sdist holds a symlink or hardlink member — "
                            "an archive link can redirect extraction "
                            "(name withheld)")
            if member.ischr() or member.isblk() or member.isfifo() \
                    or member.isdev():
                raise _fail("the sdist holds a device or FIFO member "
                            "(name withheld)")
            if member.isdir():
                if member.name != root and not member.name.startswith(root + "/"):
                    raise _fail("the sdist holds a member outside its root "
                                "directory (name withheld)")
                directories.add(member.name)
                continue
            if not member.isreg():
                raise _fail("the sdist holds a non-regular member "
                            "(name withheld)")
            if not member.name.startswith(root + "/"):
                raise _fail("the sdist holds a member outside its root "
                            "directory (name withheld)")
            try:
                handle = archive.extractfile(member)
                blobs[member.name] = handle.read() if handle else b""
            except (tarfile.TarError, OSError, EOFError, zlib.error) as err:
                raise _fail(f"an sdist member could not be read "
                            f"({type(err).__name__})")
    if not blobs:
        raise _fail("the sdist contains no regular files")
    legitimate_directories: set[str] = set()
    for name in blobs:
        parent = PurePosixPath(name).parent
        while parent != PurePosixPath("."):
            legitimate_directories.add(parent.as_posix())
            parent = parent.parent
    if not directories <= legitimate_directories:
        raise _fail("the sdist contains an unused directory member "
                    "(name withheld)")
    return blobs


# --- built distributions -------------------------------------------------------

def _parse_metadata(blob: bytes, label: str) -> tuple[dict[str, list[str]], str]:
    """Parse RFC-style packaging metadata without tolerating parser defects."""
    policy = email.policy.default.clone(raise_on_defect=True, utf8=True)
    try:
        metadata = email.parser.Parser(policy=policy).parsestr(
            _decode(blob, label))
    except (email.errors.MessageError, email.errors.MessageDefect, TypeError,
            ValueError) as err:
        raise _fail(f"{label} is malformed ({type(err).__name__})")
    if metadata.defects or metadata.is_multipart():
        raise _fail(f"{label} is malformed (metadata structure)")
    payload = metadata.get_payload()
    if type(payload) is not str:
        raise _fail(f"{label} has a non-text metadata body")
    fields: dict[str, list[str]] = {}
    for name, value in metadata.items():
        fields.setdefault(name.casefold(), []).append(str(value))
    return fields, payload


def _decode(blob: bytes, label: str) -> str:
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError as err:
        raise _fail(f"{label} is not valid UTF-8 ({type(err).__name__})")


def _required_project_string(project: dict, key: str) -> str:
    value = project.get(key)
    if type(value) is not str or not value:
        raise _fail("the signed pyproject metadata is malformed")
    return value


def _expected_people_headers(project: dict, key: str,
                             header: str) -> dict[str, list[str]]:
    people = project.get(key, [])
    if not isinstance(people, list):
        raise _fail("the signed pyproject people metadata is malformed")
    names: list[str] = []
    addresses: list[str] = []
    for person in people:
        if not isinstance(person, dict) or not person or \
                not set(person) <= {"name", "email"}:
            raise _fail("the signed pyproject people metadata is malformed")
        name = person.get("name")
        address = person.get("email")
        if name is not None and (type(name) is not str or not name):
            raise _fail("the signed pyproject people metadata is malformed")
        if address is not None and (type(address) is not str or not address):
            raise _fail("the signed pyproject people metadata is malformed")
        if name is None and address is None:
            raise _fail("the signed pyproject people metadata is malformed")
        if name is not None and address is None:
            names.append(name)
        elif name is None:
            addresses.append(address)
        else:
            try:
                addresses.append(str(email.headerregistry.Address(
                    display_name=name, addr_spec=address)))
            except (TypeError, ValueError) as err:
                raise _fail("the signed pyproject people metadata is malformed "
                            f"({type(err).__name__})")
    expected: dict[str, list[str]] = {}
    if names:
        expected[header] = [", ".join(names)]
    if addresses:
        expected[f"{header}-email"] = [", ".join(addresses)]
    return expected


def _expected_readme(project: dict, source_root: Path) -> tuple[str, str]:
    value = project.get("readme")
    content_type: str | None = None
    if type(value) is str:
        path = ensure_safe_relpath(value)
        suffix_types = {
            ".md": "text/markdown",
            ".rst": "text/x-rst",
            ".txt": "text/plain",
        }
        content_type = suffix_types.get(PurePosixPath(path).suffix.lower())
        if content_type is None:
            raise _fail("the signed pyproject readme type is unsupported")
        try:
            body = (source_root / Path(*PurePosixPath(path).parts)).read_bytes(
                ).decode("utf-8")
        except (OSError, UnicodeDecodeError) as err:
            raise _fail(f"the signed project readme is unreadable "
                        f"({type(err).__name__})")
        return content_type, body
    if not isinstance(value, dict) or set(value) not in (
            {"file", "content-type"}, {"text", "content-type"}):
        raise _fail("the signed pyproject readme metadata is malformed")
    content_type = value.get("content-type")
    if type(content_type) is not str or not content_type:
        raise _fail("the signed pyproject readme metadata is malformed")
    if "text" in value:
        body = value["text"]
        if type(body) is not str:
            raise _fail("the signed pyproject readme metadata is malformed")
        return content_type, body
    path = ensure_safe_relpath(value["file"])
    try:
        return content_type, (source_root / Path(
            *PurePosixPath(path).parts)).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise _fail(f"the signed project readme is unreadable "
                    f"({type(err).__name__})")


def _expected_license_files(project: dict, source_root: Path) -> list[str]:
    patterns = project.get("license-files")
    if not isinstance(patterns, list) or not patterns:
        raise _fail("the signed pyproject license-files policy is malformed")
    matched: set[str] = set()
    for pattern in patterns:
        if type(pattern) is not str or not pattern or "\\" in pattern or \
                pattern.startswith("/") or re.match(r"\A[A-Za-z]:", pattern) or \
                any(part in ("", ".", "..") for part in pattern.split("/")):
            raise _fail("the signed pyproject license-files policy is malformed")
        paths = [path for path in source_root.glob(pattern) if path.is_file()]
        if not paths:
            raise _fail("a signed pyproject license-files pattern is unmatched")
        matched.update(path.relative_to(source_root).as_posix()
                       for path in paths)
    ensure_no_path_collisions(matched)
    return sorted(matched)


def _file_backed_readme_path(project: dict) -> str | None:
    """Return the signed PEP 621 README path, or None for inline text."""
    value = project.get("readme")
    if type(value) is str:
        return ensure_safe_relpath(value)
    if not isinstance(value, dict):
        raise _fail("the signed pyproject readme metadata is malformed")
    if set(value) == {"text", "content-type"}:
        return None
    if set(value) != {"file", "content-type"} or \
            type(value.get("file")) is not str or not value["file"]:
        raise _fail("the signed pyproject readme metadata is malformed")
    return ensure_safe_relpath(value["file"])


def _required_sdist_source_groups(source_root: Path) \
        -> dict[str, set[str]]:
    """Build-critical sdist members derived only from signed PEP 621 input."""
    project = _project_metadata(source_root)
    # This independently validates and reads the signed README definition;
    # only file-backed forms become archive members.
    _expected_readme(project, source_root)
    readme_path = _file_backed_readme_path(project)
    groups = {
        "pyproject build input": {"pyproject.toml"},
        "declared license file": set(_expected_license_files(
            project, source_root)),
    }
    if readme_path is not None:
        groups["project README"] = {readme_path}
    return groups


def _pinned_setuptools_version(source_root: Path) -> str:
    try:
        lines = (source_root / "requirements-publish.lock").read_text(
            encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as err:
        raise _fail(f"the signed publish lock is unreadable "
                    f"({type(err).__name__})")
    versions: list[str] = []
    for line in lines:
        if not line.startswith("setuptools=="):
            continue
        match = re.fullmatch(
            r"setuptools==([0-9]+\.[0-9]+\.[0-9]+)(?:[ \t]+\\)?", line)
        if match is None:
            raise _fail("the signed publish lock has a malformed setuptools pin")
        versions.append(match.group(1))
    if versions != [_SETUPTOOLS_METADATA_POLICY_VERSION]:
        raise _fail("the signed publish lock and metadata policy disagree")
    return versions[0]


def _expected_core_metadata(source_root: Path, version: str) \
        -> tuple[dict[str, list[str]], str]:
    project = _project_metadata(source_root)
    if _required_project_string(project, "version") != version:
        raise _fail("the signed pyproject version disagrees with the release")
    _pinned_setuptools_version(source_root)
    readme_type, readme_body = _expected_readme(project, source_root)
    keywords = project.get("keywords")
    classifiers = project.get("classifiers")
    urls = project.get("urls")
    if not isinstance(keywords, list) or not keywords or \
            not all(type(value) is str and value for value in keywords) or \
            not isinstance(classifiers, list) or not classifiers or \
            not all(type(value) is str and value for value in classifiers) or \
            not isinstance(urls, dict) or not urls or \
            not all(type(key) is str and key and type(value) is str and value
                    for key, value in urls.items()):
        raise _fail("the signed pyproject descriptive metadata is malformed")
    license_expression = project.get("license")
    if type(license_expression) is not str or not license_expression:
        raise _fail("the signed pyproject license metadata is malformed")
    license_files = _expected_license_files(project, source_root)
    requirements = _expected_requirements(source_root)
    optional = project.get("optional-dependencies") or {}
    if not isinstance(optional, dict):
        raise _fail("the signed pyproject optional dependencies are malformed")
    extras = list(optional)
    expected = {
        "metadata-version": ["2.4"],
        "name": [_required_project_string(project, "name")],
        "version": [version],
        "summary": [_required_project_string(project, "description")],
        "license-expression": [license_expression],
        "project-url": [f"{name}, {url}" for name, url in urls.items()],
        "keywords": [",".join(keywords)],
        "classifier": list(classifiers),
        "requires-python": [_required_project_string(
            project, "requires-python")],
        "description-content-type": [readme_type],
        "license-file": license_files,
        "dynamic": ["license-file"],
    }
    expected.update(_expected_people_headers(project, "authors", "author"))
    expected.update(_expected_people_headers(
        project, "maintainers", "maintainer"))
    if requirements:
        expected["requires-dist"] = requirements
    if extras:
        expected["provides-extra"] = extras
    return expected, readme_body


def _canonical_extras(values, label: str) -> frozenset[str]:
    try:
        from packaging.utils import canonicalize_name
        extras = [canonicalize_name(value) for value in values
                  if type(value) is str and value]
    except Exception as err:                                    # noqa: BLE001
        raise _fail(f"the packaging extras parser is unavailable "
                    f"({type(err).__name__})")
    if len(extras) != len(values) or len(extras) != len(set(extras)):
        raise _fail(f"{label} contains a duplicate or malformed extra")
    return frozenset(extras)


def _check_project_metadata(blob: bytes, version: str, label: str,
                            source_root: Path):
    fields, body = _parse_metadata(blob, label)
    expected, expected_body = _expected_core_metadata(source_root, version)
    for header in _CORE_SINGLETON_HEADERS:
        expected_count = 1 if header in expected else 0
        actual_count = len(fields.get(header, []))
        if expected_count and actual_count != 1:
            raise _fail(f"{label} singleton headers must appear exactly once")
        if not expected_count and actual_count:
            raise _fail(f"{label} has an unexpected header under the policy")
    actual_requirements = _canonical_requirements(
        fields.get("requires-dist", []), f"{label} requirements")
    expected_requirements = _canonical_requirements(
        expected.get("requires-dist", []), "signed pyproject requirements")
    if actual_requirements != expected_requirements:
        raise _fail(f"{label} requirements disagree with signed source")
    actual_extras = _canonical_extras(
        fields.get("provides-extra", []), f"{label} extras")
    expected_extras = _canonical_extras(
        expected.get("provides-extra", []), "signed pyproject extras")
    if actual_extras != expected_extras:
        raise _fail(f"{label} extras disagree with signed source")
    if set(fields) != set(expected):
        raise _fail(f"{label} has missing or unexpected headers under the "
                    "closed metadata policy")
    if fields["version"][0] != version:
        raise _fail(f"{label} Version disagrees with the release")
    if fields["name"] != expected["name"]:
        raise _fail(f"{label} Name disagrees with signed source")
    semantic = {"requires-dist", "provides-extra", "name", "version"}
    for header in set(expected) - semantic:
        if Counter(fields[header]) != Counter(expected[header]):
            raise _fail(f"{label} header collection disagrees with signed "
                        "source metadata policy")
    if body != expected_body:
        raise _fail(f"{label} description body disagrees with the signed "
                    "project README")
    return blob


def _config(blob: bytes, label: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(_decode(blob, label))
    except configparser.Error as err:
        raise _fail(f"{label} is malformed ({type(err).__name__})")
    return parser


def _check_entry_points(blob: bytes, label: str) -> None:
    parser = _config(blob, label)
    if parser.sections() != ["console_scripts"] or \
            dict(parser.items("console_scripts")) != {
                "olympus": "olympus.cli:main"}:
        raise _fail(f"{label} does not declare the exact release entry point")


def _check_top_level(blob: bytes, label: str) -> None:
    names = [line.strip() for line in _decode(blob, label).splitlines()
             if line.strip()]
    if names != [PACKAGE]:
        raise _fail(f"{label} does not declare the exact top-level package")


def _check_wheel_file(blob: bytes, source_root: Path) -> None:
    fields, body = _parse_metadata(blob, "wheel WHEEL metadata")
    expected = {
        "wheel-version": ["1.0"],
        "generator": [f"setuptools ({_pinned_setuptools_version(source_root)})"],
        "root-is-purelib": ["true"],
        "tag": ["py3-none-any"],
    }
    for header in expected:
        if len(fields.get(header, [])) != 1:
            raise _fail("wheel WHEEL singleton headers must appear exactly once")
    if set(fields) != set(expected) or any(
            fields[header] != values for header, values in expected.items()):
        raise _fail("wheel WHEEL metadata violates the exact allowed policy")
    if body:
        raise _fail("wheel WHEEL metadata has an unexpected body under the "
                    "closed policy")


def _project_metadata(source_root: Path) -> dict:
    try:
        project = tomllib.loads(
            (source_root / "pyproject.toml").read_text(encoding="utf-8"))[
                "project"]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError,
            TypeError) as err:
        raise _fail(f"the signed pyproject metadata is unreadable "
                    f"({type(err).__name__})")
    if not isinstance(project, dict):
        raise _fail("the signed pyproject project table is malformed")
    return project


def _add_requirement_marker(requirement: str, marker: str,
                            label: str) -> str:
    try:
        from packaging.markers import Marker
        from packaging.requirements import Requirement
        parsed = Requirement(requirement)
        combined = marker if parsed.marker is None else \
            f"({parsed.marker}) and ({marker})"
        parsed.marker = Marker(combined)
    except Exception as err:                                    # noqa: BLE001
        raise _fail(f"{label} contains a malformed requirement "
                    f"({type(err).__name__})")
    return str(parsed)


def _expected_requirements(source_root: Path) -> list[str]:
    project = _project_metadata(source_root)
    requirements = list(project.get("dependencies") or [])
    for extra, values in sorted(
            (project.get("optional-dependencies") or {}).items()):
        requirements.extend(_add_requirement_marker(
            value, f'extra == "{extra}"', "signed pyproject requirements")
                            for value in values)
    return requirements


def _canonical_requirements(requirements, label: str) -> frozenset:
    try:
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
        parsed = [Requirement(value) for value in requirements]
    except Exception as err:                                    # noqa: BLE001
        raise _fail(f"{label} contains a malformed requirement "
                    f"({type(err).__name__})")
    keys = [
        (canonicalize_name(req.name),
         tuple(sorted(canonicalize_name(extra) for extra in req.extras)),
         str(req.specifier), req.url or "", str(req.marker or ""))
        for req in parsed]
    if len(keys) != len(set(keys)):
        raise _fail(f"{label} contains a duplicate requirement")
    return frozenset(keys)


def _normalise_requirement_lines(blob: bytes, label: str) -> list[str]:
    """Flatten setuptools' requirements.txt-style extras sections."""
    requirements: list[str] = []
    marker: str | None = None
    for raw in _decode(blob, label).splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            extra, sep, condition = section.partition(":")
            if (extra and any(c not in "abcdefghijklmnopqrstuvwxyz"
                              "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                              for c in extra)) or (not extra and not condition):
                raise _fail(f"{label} contains a malformed extras section")
            marker = f'extra == "{extra}"' if extra else condition
            if sep and condition and extra:
                marker = f"({condition}) and ({marker})"
            continue
        requirements.append(line if marker is None else
                            _add_requirement_marker(line, marker, label))
    return requirements


def _check_generated_setup_cfg(blob: bytes) -> None:
    parser = _config(blob, "sdist generated setup.cfg")
    if parser.sections() != ["egg_info"] or \
            set(parser.options("egg_info")) != {"tag_build", "tag_date"} or \
            parser.get("egg_info", "tag_build").strip() or \
            parser.get("egg_info", "tag_date").strip() != "0":
        raise _fail("sdist generated setup.cfg has unexpected semantics")


def _check_wheel_metadata(wheel_blobs: dict[str, bytes], version: str,
                          source_map: dict[str, str],
                          source_root: Path) -> None:
    dist_info = f"{_DIST_STEM}-{version}.dist-info"
    actual = {name[len(dist_info) + 1:] for name in wheel_blobs
              if name.startswith(dist_info + "/")}
    if actual != _WHEEL_DIST_INFO_FILES:
        raise _fail("wheel dist-info is not the closed expected metadata set")
    metadata_blob = wheel_blobs[f"{dist_info}/METADATA"]
    _check_project_metadata(metadata_blob, version, "wheel METADATA",
                            source_root)
    _check_wheel_file(wheel_blobs[f"{dist_info}/WHEEL"], source_root)
    _check_entry_points(wheel_blobs[f"{dist_info}/entry_points.txt"],
                        "wheel entry_points metadata")
    _check_top_level(wheel_blobs[f"{dist_info}/top_level.txt"],
                     "wheel top_level metadata")
    license_blob = wheel_blobs[f"{dist_info}/licenses/LICENSE"]
    if "LICENSE" not in source_map or \
            hashlib.sha256(license_blob).hexdigest() != source_map["LICENSE"]:
        raise _fail("wheel license metadata does not match signed source")
    return metadata_blob


def _check_sdist_metadata(sdist_rel: dict[str, bytes], version: str,
                          source_map: dict[str, str],
                          source_root: Path) -> None:
    egg = f"{_DIST_STEM}.egg-info"
    expected_requirements = sorted(_expected_requirements(source_root))
    egg_info_files = set(_SDIST_EGG_INFO_FILES)
    # Pinned setuptools omits requires.txt entirely when the signed project has
    # no dependencies.  When requirements exist, the file is mandatory and is
    # checked semantically below; accepting an empty or injected optional file
    # would make the supposedly closed generated surface backend-dependent.
    if expected_requirements:
        egg_info_files.add("requires.txt")
    generated = {"PKG-INFO", "setup.cfg"} | {
        f"{egg}/{name}" for name in egg_info_files}
    actual_generated = set(sdist_rel) - set(source_map) - {
        f"{PACKAGE}/{MANIFEST_NAME}"}
    if actual_generated != generated:
        raise _fail("sdist contains unsigned source or generated metadata "
                    "outside the closed expected set")
    root_info = sdist_rel["PKG-INFO"]
    egg_info = sdist_rel[f"{egg}/PKG-INFO"]
    _check_project_metadata(root_info, version, "sdist PKG-INFO", source_root)
    _check_project_metadata(egg_info, version, "sdist egg-info PKG-INFO",
                            source_root)
    if root_info != egg_info:
        raise _fail("sdist generated PKG-INFO copies disagree")
    _check_generated_setup_cfg(sdist_rel["setup.cfg"])
    _check_entry_points(sdist_rel[f"{egg}/entry_points.txt"],
                        "sdist entry_points metadata")
    _check_top_level(sdist_rel[f"{egg}/top_level.txt"],
                     "sdist top_level metadata")
    if _decode(sdist_rel[f"{egg}/dependency_links.txt"],
               "sdist dependency links").strip():
        raise _fail("sdist dependency links metadata must be empty")

    if expected_requirements:
        actual_requirements = sorted(_normalise_requirement_lines(
            sdist_rel[f"{egg}/requires.txt"],
            "sdist requirements metadata"))
        if actual_requirements != expected_requirements:
            raise _fail("sdist generated requirements disagree with signed "
                        "source")

    sources = [line.strip() for line in _decode(
        sdist_rel[f"{egg}/SOURCES.txt"], "sdist source inventory").splitlines()
               if line.strip()]
    for path in sources:
        ensure_safe_relpath(path)
    ensure_no_path_collisions(sources)
    # setuptools writes SOURCES.txt before it adds the root PKG-INFO and the
    # generated setup.cfg.  Its ordering groups project metadata ahead of
    # package paths rather than using bytewise path order, so order carries no
    # authority; collision checking above still proves every path is unique.
    expected_sources = set(sdist_rel) - {"PKG-INFO", "setup.cfg"}
    if set(sources) != expected_sources:
        raise _fail("sdist SOURCES inventory disagrees with archive members")
    return root_info


def _manifest_from_blob(blob: bytes, label: str):
    try:
        return json.loads(_decode(blob, label))
    except (ValueError, TypeError) as err:
        raise _fail(f"{label} is not valid JSON ({type(err).__name__})")


def _read_manifest(path: Path):
    try:
        return _manifest_from_blob(path.read_bytes(), "signed manifest")
    except OSError as err:
        raise _fail(f"the signed manifest is unreadable "
                    f"({type(err).__name__})")


def check_dists(dist_dir: Path, version: str, *, manifest,
                expected_key: str, expected_commit: str,
                source_root: Path) -> dict:
    """Authenticate the manifest afresh, then authorize every archive member."""
    if type(version) is not str or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise _fail("the release version is not a stable X.Y.Z version")
    if not isinstance(source_root, Path):
        source_root = Path(source_root)
    try:
        authenticate_manifest(manifest, expected_key=expected_key,
                              expected_commit=expected_commit)
    except ReleaseCheckError:
        raise _fail("the supplied manifest is not the one authenticated for "
                    "release: its manifest hash, signature, signer, or commit "
                    "check failed") from None
    # Inspection runs from a fresh checkout. Prove the signed source map is
    # complete and matches it before using that map to authorize an sdist.
    verify_manifest_native(manifest, source_root / PACKAGE,
                           expected_key=expected_key,
                           expected_commit=expected_commit,
                           source_root=source_root)
    files = sorted(Path(p) for p in glob.glob(str(dist_dir / "*")))
    wheels = [p for p in files if p.name.endswith(".whl")]
    sdists = [p for p in files if p.name.endswith(".tar.gz")]
    extras = [p for p in files if p not in wheels and p not in sdists]
    if extras:
        raise _fail(f"{len(extras)} unexpected file(s) in dist "
                    f"(names withheld)")
    if len(wheels) != 1 or len(sdists) != 1:
        raise _fail(f"expected exactly one wheel and one sdist, found "
                    f"{len(wheels)} and {len(sdists)}")
    wheel, sdist = wheels[0], sdists[0]
    if wheel.name != f"{_DIST_STEM}-{version}-py3-none-any.whl":
        raise _fail("the wheel filename is not the expected release name")
    if sdist.name != f"{_DIST_STEM}-{version}.tar.gz":
        raise _fail("the sdist filename is not the expected release name")

    wheel_blobs = check_wheel_members(wheel, version)
    sdist_blobs = check_sdist_members(sdist, version)

    sroot = f"{_DIST_STEM}-{version}"
    wheel_pkg = {name[len(PACKAGE) + 1:]: blob
                 for name, blob in wheel_blobs.items()
                 if name.startswith(PACKAGE + "/")}
    sdist_rel = {name[len(sroot) + 1:]: blob
                 for name, blob in sdist_blobs.items()}
    sdist_pkg = {name[len(PACKAGE) + 1:]: blob
                 for name, blob in sdist_rel.items()
                 if name.startswith(PACKAGE + "/")}
    for path in set(wheel_pkg) | set(sdist_pkg):
        if _is_bytecode(path):
            raise _fail("a distribution ships bytecode or __pycache__ "
                        "content (name withheld)")

    manifest_member = MANIFEST_NAME
    if manifest_member not in wheel_pkg:
        raise _fail("the wheel does not carry the signed manifest")
    if manifest_member not in sdist_pkg:
        raise _fail("the sdist does not carry the signed manifest")
    wheel_manifest = _manifest_from_blob(
        wheel_pkg[manifest_member], "wheel manifest")
    sdist_manifest = _manifest_from_blob(
        sdist_pkg[manifest_member], "sdist manifest")
    for shipped_manifest in (wheel_manifest, sdist_manifest):
        authenticate_manifest(shipped_manifest, expected_key=expected_key,
                              expected_commit=expected_commit)
    expected_bytes = canonical_json(manifest)
    if canonical_json(wheel_manifest) != expected_bytes or \
            canonical_json(sdist_manifest) != expected_bytes:
        raise _fail("a shipped manifest is not exactly the independently "
                    "authenticated manifest")

    signed = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    allowed_package = set(signed) | {manifest_member}
    for label, shipped in (("wheel", wheel_pkg), ("sdist", sdist_pkg)):
        actual = set(shipped)
        if actual != allowed_package:
            missing = len(allowed_package - actual)
            extra = len(actual - allowed_package)
            raise _fail(f"the {label} package member set is not covered "
                        f"exactly: {missing} missing, {extra} unsigned "
                        f"(names withheld)")
        mismatched = sum(
            hashlib.sha256(shipped[path]).hexdigest() != digest
            for path, digest in signed.items())
        if mismatched:
            raise _fail(f"{mismatched} {label} file(s) do not match their "
                        "signed hash (names withheld)")

    dist_info = f"{_DIST_STEM}-{version}.dist-info"
    expected_wheel_members = {
        f"{PACKAGE}/{path}" for path in allowed_package} | {
        f"{dist_info}/{path}" for path in _WHEEL_DIST_INFO_FILES}
    if set(wheel_blobs) != expected_wheel_members:
        raise _fail("the wheel contains an unsigned member outside its closed "
                    "package and metadata sets")

    source_map = {entry["path"]: entry["sha256"]
                  for entry in manifest["sourceFiles"]}
    required_groups = _required_sdist_source_groups(source_root)
    for label, required in required_groups.items():
        if not required <= set(source_map):
            raise _fail("the signed repository source map omits a required "
                        f"sdist {label}")
        if not required <= set(sdist_rel):
            raise _fail(f"the sdist omits a required signed {label}")
    # verification.json is generated after signing and is the one explicit
    # exception to sourceFiles. Everything else in the source archive is either
    # signed source or the narrowly validated setuptools metadata set.
    for path in set(sdist_rel) & set(source_map):
        if hashlib.sha256(sdist_rel[path]).hexdigest() != source_map[path]:
            raise _fail("the sdist repository source does not match its signed "
                        "hash (name withheld)")
    wheel_metadata = _check_wheel_metadata(
        wheel_blobs, version, source_map, source_root)
    sdist_metadata = _check_sdist_metadata(
        sdist_rel, version, source_map, source_root)
    if wheel_metadata != sdist_metadata:
        raise _fail("wheel and sdist project metadata disagree")
    return {"wheel": wheel.name, "sdist": sdist.name, "version": version}


# --- CLI ------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check-tag")
    p.add_argument("tag")
    p.add_argument("--root", default=".")
    p.add_argument("--no-runtime", action="store_true")

    p = sub.add_parser("check-commit")
    p.add_argument("sha")
    p.add_argument("--root", default=".")

    p = sub.add_parser("check-tag-points-at")
    p.add_argument("tag")
    p.add_argument("sha")
    p.add_argument("--root", default=".")

    p = sub.add_parser("check-dispatch-ref")
    p.add_argument("ref")
    p.add_argument("ref_type")

    p = sub.add_parser("check-toolchain")
    p.add_argument("--python-version", required=True)
    p.add_argument("--pip-version", required=True)

    p = sub.add_parser("sign-manifest")
    p.add_argument("--root", default=".")
    p.add_argument("--manifest")
    p.add_argument("--expected-commit", required=True)

    p = sub.add_parser("verify-manifest")
    p.add_argument("--root", default=".")
    p.add_argument("--manifest")
    p.add_argument("--expected-commit", required=True)

    p = sub.add_parser("check-dists")
    p.add_argument("--root", default=".")
    p.add_argument("--dist", default="dist")
    p.add_argument("--version", required=True)
    p.add_argument("--manifest")
    p.add_argument("--expected-commit", required=True)

    args = parser.parse_args(argv)
    root = Path(getattr(args, "root", ".")).resolve()
    try:
        if args.command == "check-tag":
            version = check_tag(args.tag, root,
                                check_runtime=not args.no_runtime)
            print(f"tag identity verified: {version}")
        elif args.command == "check-commit":
            check_commit_is_protected_main_tip(args.sha, root)
            print("release commit is the protected main tip")
        elif args.command == "check-tag-points-at":
            check_tag_points_at(args.tag, args.sha, root)
            print("tag peels to the release commit")
        elif args.command == "check-dispatch-ref":
            check_dispatch_ref(args.ref, args.ref_type)
            print("dispatch ref is the protected main branch")
        elif args.command == "check-toolchain":
            check_toolchain(args.python_version, args.pip_version)
            print("exact release toolchain verified")
        elif args.command == "sign-manifest":
            output_path = Path(args.manifest) if args.manifest else None
            if output_path is not None and not output_path.is_absolute():
                output_path = root / output_path
            out = sign_manifest(
                root, expected_commit=args.expected_commit,
                output_path=output_path)
            print(f"signed release manifest written: {out.name}")
        elif args.command == "verify-manifest":
            manifest_path = (Path(args.manifest) if args.manifest else
                             root / PACKAGE / MANIFEST_NAME)
            if not manifest_path.is_absolute():
                manifest_path = root / manifest_path
            manifest = _read_manifest(manifest_path)
            ensure_clean_worktree_excluding_manifest(root)
            report = verify_manifest_native(manifest, root / PACKAGE,
                                             expected_key=pinned_key(root),
                                             expected_commit=args.expected_commit,
                                             source_root=root)
            print(f"signed manifest verified: {report['files']} files")
        elif args.command == "check-dists":
            manifest_path = (Path(args.manifest) if args.manifest else
                             root / PACKAGE / MANIFEST_NAME)
            if not manifest_path.is_absolute():
                manifest_path = root / manifest_path
            dist_path = Path(args.dist)
            if not dist_path.is_absolute():
                dist_path = root / dist_path
            manifest = _read_manifest(manifest_path)
            check_dists(dist_path, args.version, manifest=manifest,
                        expected_key=pinned_key(root),
                        expected_commit=args.expected_commit,
                        source_root=root)
            print("distributions verified and bound to the signed manifest")
    except ReleaseCheckError as err:
        print(f"RELEASE CHECK FAILED: {err}", file=sys.stderr)
        return 1
    return 0


def ensure_clean_worktree_excluding_manifest(root: Path) -> None:
    """Clean except for the signed manifest, which the build job places.

    The manifest is the ONE file a later job is allowed to introduce, and it
    is itself signed — so it is verified rather than trusted.
    """
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    offenders = []
    allowed = f"{PACKAGE}/{MANIFEST_NAME}"
    for line in status.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip().strip('"')
        if path != allowed:
            offenders.append(path)
    if offenders:
        raise _fail(f"the working tree holds {len(offenders)} unexpected "
                    f"modified/untracked path(s) (names withheld)")


if __name__ == "__main__":
    raise SystemExit(main())
