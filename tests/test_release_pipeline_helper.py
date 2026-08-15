"""Adversarial unit tests for the executable release validators (v6).

`scripts/release_pipeline.py` holds every nontrivial gate the publish
pipeline depends on, and now also SIGNS the release. On a real release it
runs once, on a runner, with the signing seed or the publishing credential
nearby — so its failure modes must be proven HERE, deterministically, with
no network and no production key.

Every test drives a hostile input: a prerelease tag, a moved main, a tag
that does not peel to the release commit, a dirty or untracked tree,
tracked bytecode, symlinks, a forged or wrong-key manifest, unsigned files
smuggled into a distribution, corrupted RECORD entries, archive links and
devices, and paths that collide under case folding or Unicode
normalization. A validator that merely crashes is not fail-closed, so each
case asserts the typed `ReleaseCheckError`, never an incidental TypeError.

NO CRYPTOGRAPHIC TEST USES `importorskip`. `cryptography` is a hard
requirement of the signing lock; a silently skipped signature test is
indistinguishable from a passing one, which is exactly the false-green the
v2 audit found.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import importlib.metadata
import inspect
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import unicodedata
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

import release_pipeline as rp  # noqa: E402

_VERSION = "9.9.9"
_TAG = f"v{_VERSION}"
_STEM = "olympus_council"
_SEED = "step1l-v3-disposable-test-seed"


def _pubkey_for(seed: str) -> str:
    key = ed25519.Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(seed.encode("utf-8")).digest())
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _run(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_rootless_workflow_gate_clis_execute(capsys):
    assert rp.main([
        "check-toolchain", "--python-version", platform.python_version(),
        "--pip-version", importlib.metadata.version("pip")]) == 0
    assert rp.main([
        "check-dispatch-ref", "refs/heads/main", "branch"]) == 0
    assert "verified" in capsys.readouterr().out


def _git_repo(tmp_path: Path, *, key: str | None = None) -> Path:
    """A miniature, buildable Olympus checkout with one reviewed commit."""
    root = tmp_path / "repo"
    (root / "olympus").mkdir(parents=True)
    (root / "olympus" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (root / "olympus" / "cli.py").write_text(
        "def main():\n    return 0\n", encoding="utf-8")
    (root / "olympus" / "core.py").write_text("y = 2\n", encoding="utf-8")
    (root / "olympus" / "prompt.md").write_text("# p\n", encoding="utf-8")
    (root / "olympus" / "data.json").write_text('{"a": 1}\n',
                                                encoding="utf-8")
    (root / "olympus" / "policy.yaml").write_text(
        "enabled: true\n", encoding="utf-8")
    (root / "olympus" / "accelerator.so").write_bytes(b"signed-so\n")
    (root / "olympus" / "adapter.dll").write_bytes(b"signed-dll\n")
    (root / "olympus" / "NOTICE").write_text(
        "signed extensionless data\n", encoding="utf-8")
    (root / "olympus" / "witness_pubkey.txt").write_text(
        f"# pinned\n{key or _pubkey_for(_SEED)}\n", encoding="utf-8")
    (root / "README.md").write_text("# Test Olympus\n", encoding="utf-8")
    (root / "LICENSE").write_text("test license\n", encoding="utf-8")
    (root / "requirements-publish.lock").write_text(
        "setuptools==84.0.0\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[build-system]\n'
        'requires = ["setuptools>=77"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        '[project]\n'
        'name = "olympus-council"\n'
        f'version = "{_VERSION}"\n'
        'description = "Test Olympus package"\n'
        'readme = "README.md"\n'
        'requires-python = ">=3.10"\n'
        'license = "MIT"\n'
        'license-files = ["LICENSE"]\n'
        'authors = [{ name = "Test Author" }]\n'
        'keywords = ["test", "olympus"]\n'
        'classifiers = [\n'
        '    "Development Status :: 4 - Beta",\n'
        '    "Programming Language :: Python :: 3",\n'
        ']\n\n'
        '[project.urls]\n'
        'Homepage = "https://example.invalid/olympus"\n'
        'Issues = "https://example.invalid/olympus/issues"\n\n'
        '[project.scripts]\n'
        'olympus = "olympus.cli:main"\n\n'
        '[tool.setuptools.packages.find]\n'
        'include = ["olympus*"]\n\n'
        '[tool.setuptools.package-data]\n'
        'olympus = ["*.md", "*.json", "*.yaml", "*.txt", "*.so", '
        '"*.dll", "NOTICE"]\n',
        encoding="utf-8")
    for args in (("init", "-b", "main"), ("config", "user.email", "t@e.com"),
                 ("config", "user.name", "t"), ("add", "-A"),
                 ("commit", "-m", "init")):
        _run(root, *args)
    return root


def _head(path: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


def _sign(root: Path) -> Path:
    parameters = inspect.signature(rp.sign_manifest).parameters
    if "expected_commit" in parameters:
        return rp.sign_manifest(root, expected_commit=_head(root))
    return rp.sign_manifest(root)              # v3 red-baseline compatibility


def _verify(root: Path, manifest: dict, *, key: str | None = None):
    kwargs = {"expected_key": key or _pubkey_for(_SEED)}
    parameters = inspect.signature(rp.verify_manifest_native).parameters
    if "expected_commit" in parameters:
        kwargs.update(expected_commit=_head(root), source_root=root)
    return rp.verify_manifest_native(manifest, root / "olympus", **kwargs)


def _check_dists(root: Path, dist: Path, manifest: dict,
                 *, version: str = _VERSION, key: str | None = None):
    kwargs = {
        "manifest": manifest,
        "expected_key": key or _pubkey_for(_SEED),
    }
    parameters = inspect.signature(rp.check_dists).parameters
    if "expected_commit" in parameters:
        kwargs.update(expected_commit=_head(root), source_root=root)
    return rp.check_dists(dist, version, **kwargs)


def _resign(manifest: dict, seed: str = _SEED) -> None:
    core = {k: v for k, v in manifest.items() if k != "integrity"}
    core["summary"] = {**core["summary"], "verified": 0, "failed": 0}
    payload = rp.canonical_json(core)
    key = ed25519.Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(seed.encode("utf-8")).digest())
    manifest["integrity"] = {
        "manifestHash": hashlib.sha256(payload).hexdigest(),
        "publicKey": _pubkey_for(seed),
        "signature": key.sign(payload).hex(),
        "seedDerivation": "ed25519(sha256(seed))",
    }


# =============================================================================
# Tag syntax and version identity
# =============================================================================

@pytest.mark.parametrize("tag", ["v1.2.3", "v0.27.3", "v10.0.1"])
def test_stable_tags_are_accepted(tag):
    assert rp.version_from_tag(tag) == tag[1:]


@pytest.mark.parametrize("tag, why", [
    ("1.2.3", "no v prefix"),
    ("v1.2", "not three components"),
    ("v1.2.3.4", "four components"),
    ("v1.2.3rc1", "a prerelease tag must never publish"),
    ("v1.2.3-rc.1", "a hyphenated prerelease"),
    ("v1.2.3a1", "an alpha tag"),
    ("v1.2.3.dev0", "a dev tag"),
    ("v1.2.3+build.5", "local build metadata"),
    ("V1.2.3", "wrong case"),
    ("v1.2.3\n", "a trailing newline ($ would accept this)"),
    ("release-1.2.3", "an unrelated tag name"),
    ("", "an empty tag"),
    (" v1.2.3", "leading whitespace"),
])
def test_nonstable_and_malformed_tags_fail_closed(tag, why):
    with pytest.raises(rp.ReleaseCheckError):
        rp.version_from_tag(tag)


@pytest.mark.parametrize("tag", [None, 1.23, b"v1.2.3", ["v1.2.3"]])
def test_a_non_string_tag_fails_closed_rather_than_crashing(tag):
    with pytest.raises(rp.ReleaseCheckError):
        rp.version_from_tag(tag)


def _write_pyproject(root: Path, version: str) -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "olympus-council"\nversion = "{version}"\n',
        encoding="utf-8")


def test_check_tag_accepts_a_matching_source_version(tmp_path):
    _write_pyproject(tmp_path, _VERSION)
    assert rp.check_tag(_TAG, tmp_path, check_runtime=False) == _VERSION


def test_check_tag_rejects_a_source_version_mismatch(tmp_path):
    _write_pyproject(tmp_path, "9.9.8")
    with pytest.raises(rp.ReleaseCheckError, match="different versions"):
        rp.check_tag(_TAG, tmp_path, check_runtime=False)


def test_check_tag_rejects_an_ambiguous_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        f'version = "{_VERSION}"\nversion = "1.0.0"\n', encoding="utf-8")
    with pytest.raises(rp.ReleaseCheckError, match="version lines"):
        rp.check_tag(_TAG, tmp_path, check_runtime=False)


def test_check_tag_rejects_a_runtime_version_mismatch(tmp_path, monkeypatch):
    _write_pyproject(tmp_path, _VERSION)
    monkeypatch.setattr(rp, "runtime_version", lambda: "0.0.1")
    with pytest.raises(rp.ReleaseCheckError, match="runtime"):
        rp.check_tag(_TAG, tmp_path, check_runtime=True)


def test_a_missing_pyproject_fails_closed_not_crashes(tmp_path):
    """AUDIT (medium): malformed inputs reached the caller as untyped
    crashes, contradicting the module's fail-closed claim."""
    with pytest.raises(rp.ReleaseCheckError, match="unreadable"):
        rp.pyproject_version(tmp_path)


# =============================================================================
# Commit eligibility and tag peeling
# =============================================================================

def _origin_clone(tmp_path: Path):
    origin = tmp_path / "origin"
    origin.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "t@e.com"),
                 ("config", "user.name", "t")):
        _run(origin, *args)
    (origin / "f.txt").write_text("one", encoding="utf-8")
    _run(origin, "add", "-A")
    _run(origin, "commit", "-m", "one")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True,
                   capture_output=True)
    _run(clone, "config", "user.email", "t@e.com")
    _run(clone, "config", "user.name", "t")
    return origin, clone


def test_the_current_protected_tip_is_accepted(tmp_path):
    origin, clone = _origin_clone(tmp_path)
    tip = _head(origin)
    assert rp.check_commit_is_protected_main_tip(tip, clone) == tip


def test_an_ancestor_of_main_is_rejected(tmp_path):
    """An ancestor-only rule would let a superseded commit publish."""
    origin, clone = _origin_clone(tmp_path)
    old = _head(origin)
    (origin / "f.txt").write_text("two", encoding="utf-8")
    _run(origin, "commit", "-am", "two")
    with pytest.raises(rp.ReleaseCheckError, match="tip"):
        rp.check_commit_is_protected_main_tip(old, clone)


def test_a_commit_that_is_not_on_main_at_all_is_rejected(tmp_path):
    _origin, clone = _origin_clone(tmp_path)
    (clone / "side.txt").write_text("side", encoding="utf-8")
    _run(clone, "add", "-A")
    _run(clone, "commit", "-m", "side")
    with pytest.raises(rp.ReleaseCheckError, match="tip"):
        rp.check_commit_is_protected_main_tip(_head(clone), clone)


def test_a_main_that_moved_after_dispatch_is_rejected(tmp_path):
    origin, clone = _origin_clone(tmp_path)
    dispatched = _head(origin)
    (origin / "f.txt").write_text("moved", encoding="utf-8")
    _run(origin, "commit", "-am", "moved")
    with pytest.raises(rp.ReleaseCheckError, match="tip"):
        rp.check_commit_is_protected_main_tip(dispatched, clone)


@pytest.mark.parametrize("sha", ["", "deadbeef", "z" * 40, None, 12345])
def test_a_malformed_sha_fails_closed(sha, tmp_path):
    _origin, clone = _origin_clone(tmp_path)
    with pytest.raises(rp.ReleaseCheckError, match="40-hex"):
        rp.check_commit_is_protected_main_tip(sha, clone)


def test_tag_must_peel_to_the_dispatch_sha(tmp_path):
    root = _git_repo(tmp_path)
    sha = _head(root)
    _run(root, "tag", _TAG)
    assert rp.check_tag_points_at(_TAG, sha, root) == sha


def test_an_annotated_tag_peels_to_its_commit(tmp_path):
    root = _git_repo(tmp_path)
    sha = _head(root)
    _run(root, "tag", "-a", _TAG, "-m", "rel")
    assert rp.check_tag_points_at(_TAG, sha, root) == sha


def test_a_tag_that_does_not_exist_is_rejected(tmp_path):
    root = _git_repo(tmp_path)
    with pytest.raises(rp.ReleaseCheckError, match="does not exist"):
        rp.check_tag_points_at("v0.0.1", _head(root), root)


def test_a_tag_pointing_elsewhere_is_rejected(tmp_path):
    """The dispatched commit must be the tagged one — an old tag must not
    execute today's workflow against yesterday's code."""
    root = _git_repo(tmp_path)
    _run(root, "tag", _TAG)
    (root / "olympus" / "core.py").write_text("y = 3\n", encoding="utf-8")
    _run(root, "commit", "-am", "next")
    with pytest.raises(rp.ReleaseCheckError, match="does not point"):
        rp.check_tag_points_at(_TAG, _head(root), root)


def test_a_prerelease_tag_is_rejected_before_any_git_call(tmp_path):
    root = _git_repo(tmp_path)
    with pytest.raises(rp.ReleaseCheckError, match="stable release tag"):
        rp.check_tag_points_at("v1.2.3rc1", _head(root), root)


# =============================================================================
# Path safety
# =============================================================================

@pytest.mark.parametrize("path, why", [
    ("../evil.py", "parent traversal"),
    ("a/../../evil.py", "embedded traversal"),
    ("/etc/passwd", "absolute posix"),
    ("C:/Windows/evil.py", "drive-qualified"),
    ("C:\\Windows\\evil.py", "drive plus backslash"),
    ("a\\b.py", "backslash separator"),
    ("a\x00b.py", "NUL byte"),
    ("", "empty path"),
    (".", "bare dot"),
    ("./a.py", "dot-prefixed"),
    ("a/./b.py", "embedded dot"),
    (None, "not a string"),
    (7, "an int"),
])
def test_unsafe_paths_are_rejected(path, why):
    with pytest.raises(rp.ReleaseCheckError):
        rp.ensure_safe_relpath(path)


@pytest.mark.parametrize("path", ["core.py", "sub/mod.py", "a/b/c/data.json"])
def test_safe_paths_are_accepted(path):
    assert rp.ensure_safe_relpath(path) == path


def test_case_colliding_paths_are_rejected():
    """A case-insensitive filesystem would let Core.py shadow core.py."""
    with pytest.raises(rp.ReleaseCheckError, match="collide"):
        rp.ensure_no_path_collisions(["core.py", "Core.py"])


def test_unicode_normalization_colliding_paths_are_rejected():
    composed = "caf\u00e9.py"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    with pytest.raises(rp.ReleaseCheckError, match="collide"):
        rp.ensure_no_path_collisions([composed, decomposed])


def test_duplicate_paths_are_rejected():
    with pytest.raises(rp.ReleaseCheckError, match="collide"):
        rp.ensure_no_path_collisions(["core.py", "core.py"])


def test_distinct_paths_are_accepted():
    rp.ensure_no_path_collisions(["a.py", "b/c.py", "d.json"])


# =============================================================================
# The narrow signer: stdlib + cryptography only
# =============================================================================

def test_the_release_helper_never_imports_olympus():
    """AUDIT (critical, functional): `python -m olympus sign` pulls the whole
    CLI graph (`cli -> ... -> llm -> anthropic`) and cannot run in the
    minimal signing environment. The helper must be self-contained."""
    source = (_REPO / "scripts" / "release_pipeline.py").read_text(
        encoding="utf-8")
    offenders = [line for line in source.splitlines()
                 if line.lstrip().startswith(("import olympus",
                                              "from olympus"))]
    assert not offenders, f"olympus import would break signing: {offenders}"


def test_signing_produces_a_manifest_that_verifies(tmp_path, monkeypatch):
    root = _git_repo(tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", _SEED)
    path = _sign(root)
    assert path == root / "olympus" / "verification.json"

    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["integrity"]["publicKey"] == _pubkey_for(_SEED)
    signed_package = {entry["path"] for entry in manifest["files"]}
    assert signed_package == {
        "NOTICE", "__init__.py", "accelerator.so", "adapter.dll", "cli.py",
        "core.py", "data.json", "policy.yaml", "prompt.md",
        "witness_pubkey.txt",
    }
    tracked = set(subprocess.run(
        ["git", "ls-files"], cwd=root, check=True, capture_output=True,
        text=True).stdout.splitlines())
    assert {entry["path"] for entry in manifest["sourceFiles"]} == tracked
    report = _verify(root, manifest)
    assert report["signature_ok"] is True
    assert report["files"] == len(signed_package)


def test_the_signed_manifest_is_byte_compatible_with_witness_format(
        tmp_path, monkeypatch):
    """The format must stay verifiable by `olympus verify` for end users."""
    root = _git_repo(tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", _SEED)
    manifest = json.loads(
        _sign(root).read_text(encoding="utf-8"))
    assert manifest["schema"] == "olympus-witness/1"
    assert manifest["integrity"]["seedDerivation"] == "ed25519(sha256(seed))"
    core = {k: v for k, v in manifest.items() if k != "integrity"}
    payload = rp.canonical_json(core)
    key = ed25519.Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(manifest["integrity"]["publicKey"]))
    key.verify(bytes.fromhex(manifest["integrity"]["signature"]), payload)


def test_signing_requires_the_pinned_key(tmp_path, monkeypatch):
    """AUDIT (high/critical): signer identity was never enforced."""
    root = _git_repo(tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "a-completely-different-seed")
    with pytest.raises(rp.ReleaseCheckError, match="pinned release key"):
        _sign(root)


def test_signing_without_a_seed_refuses(tmp_path, monkeypatch):
    root = _git_repo(tmp_path)
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    with pytest.raises(rp.ReleaseCheckError, match="not set"):
        _sign(root)


def test_signing_refuses_a_dirty_tree(tmp_path, monkeypatch):
    root = _git_repo(tmp_path)
    (root / "olympus" / "core.py").write_text("y = 666\n", encoding="utf-8")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", _SEED)
    with pytest.raises(rp.ReleaseCheckError, match="not clean"):
        _sign(root)


def test_signing_refuses_untracked_release_inputs(tmp_path, monkeypatch):
    root = _git_repo(tmp_path)
    (root / "olympus" / "sneaky.py").write_text("evil = 1\n",
                                                encoding="utf-8")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", _SEED)
    with pytest.raises(rp.ReleaseCheckError, match="not clean"):
        _sign(root)


def test_signing_refuses_tracked_bytecode(tmp_path, monkeypatch):
    """AUDIT (high): *.pyc / __pycache__ are executable and were invisible
    to every integrity check."""
    root = _git_repo(tmp_path)
    cache = root / "olympus" / "__pycache__"
    cache.mkdir()
    (cache / "core.cpython-312.pyc").write_bytes(b"\x00\x01evil")
    _run(root, "add", "-Af")
    _run(root, "commit", "-m", "pyc")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", _SEED)
    with pytest.raises(rp.ReleaseCheckError, match="bytecode"):
        _sign(root)


@pytest.mark.skipif(os.name == "nt",
                    reason="symlink creation requires privilege on Windows")
def test_signing_refuses_symlinked_package_files(tmp_path, monkeypatch):
    """AUDIT (medium): the scan could not see beneath symlinks."""
    root = _git_repo(tmp_path)
    (root / "olympus" / "link.py").symlink_to(root / "pyproject.toml")
    _run(root, "add", "-A")
    _run(root, "commit", "-m", "link")
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", _SEED)
    with pytest.raises(rp.ReleaseCheckError, match="symlink"):
        _sign(root)


def test_a_minimal_package_still_signs_its_pinned_key(tmp_path, monkeypatch):
    root = tmp_path / "empty"
    (root / "olympus").mkdir(parents=True)
    (root / "olympus" / "witness_pubkey.txt").write_text(
        f"{_pubkey_for(_SEED)}\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f'version = "{_VERSION}"\n', encoding="utf-8")
    for args in (("init", "-b", "main"), ("config", "user.email", "t@e.com"),
                 ("config", "user.name", "t"), ("add", "-A"),
                 ("commit", "-m", "init")):
        _run(root, *args)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", _SEED)
    manifest = json.loads(_sign(root).read_text(encoding="utf-8"))
    assert [entry["path"] for entry in manifest["files"]] == [
        "witness_pubkey.txt"]


def test_signing_writes_no_bytecode(tmp_path, monkeypatch):
    root = _git_repo(tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", _SEED)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    _sign(root)
    assert not list(root.rglob("__pycache__"))


def test_the_pinned_key_file_must_hold_exactly_one_key(tmp_path):
    root = _git_repo(tmp_path)
    (root / "olympus" / "witness_pubkey.txt").write_text(
        f"{_pubkey_for(_SEED)}\n{_pubkey_for('other')}\n", encoding="utf-8")
    with pytest.raises(rp.ReleaseCheckError, match="exactly 1"):
        rp.pinned_key(root)


def test_the_pinned_key_must_be_hex(tmp_path):
    root = _git_repo(tmp_path)
    (root / "olympus" / "witness_pubkey.txt").write_text(
        "not-a-key\n", encoding="utf-8")
    with pytest.raises(rp.ReleaseCheckError, match="hex key"):
        rp.pinned_key(root)


# =============================================================================
# Manifest verification
# =============================================================================

def _signed(tmp_path, monkeypatch):
    root = _git_repo(tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", _SEED)
    _sign(root)
    manifest = json.loads(
        (root / "olympus" / "verification.json").read_text(encoding="utf-8"))
    return root, manifest


def test_a_tampered_file_breaks_verification(tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    (root / "olympus" / "core.py").write_text("y = 666\n", encoding="utf-8")
    with pytest.raises(rp.ReleaseCheckError, match="does not match the tree"):
        _verify(root, manifest)


def test_a_deleted_file_breaks_verification(tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    (root / "olympus" / "core.py").unlink()
    with pytest.raises(rp.ReleaseCheckError, match="does not match the tree"):
        _verify(root, manifest)


def test_a_forged_signature_is_rejected(tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    manifest["integrity"]["signature"] = "0" * len(
        manifest["integrity"]["signature"])
    with pytest.raises(rp.ReleaseCheckError, match="signature"):
        _verify(root, manifest)


def test_a_manifest_signed_by_another_key_is_rejected(tmp_path, monkeypatch):
    """Internally consistent, but not the pinned release key."""
    attacker = "an-attackers-seed"
    root = _git_repo(tmp_path, key=_pubkey_for(attacker))
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", attacker)
    _sign(root)
    manifest = json.loads(
        (root / "olympus" / "verification.json").read_text(encoding="utf-8"))
    # It verifies against the attacker key...
    _verify(root, manifest, key=_pubkey_for(attacker))
    # ...but never against the real release key.
    with pytest.raises(rp.ReleaseCheckError, match="untrusted key"):
        _verify(root, manifest)


def test_a_mutated_manifest_body_breaks_the_hash(tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    manifest["files"][0]["sha256"] = "0" * 64
    with pytest.raises(rp.ReleaseCheckError, match="hash|signature"):
        _verify(root, manifest)


def test_verification_requires_an_expected_key(tmp_path, monkeypatch):
    """The v2 defect: `expected_key=None` silently skipped identity."""
    root, manifest = _signed(tmp_path, monkeypatch)
    with pytest.raises(TypeError):
        rp.verify_manifest_native(
            manifest, root / "olympus", expected_commit=_head(root),
            source_root=root)
    for bad in (None, "", "zz", 7):
        with pytest.raises(rp.ReleaseCheckError, match="hex key"):
            rp.verify_manifest_native(manifest, root / "olympus",
                                      expected_key=bad,
                                      expected_commit=_head(root),
                                      source_root=root)


def test_expected_commit_is_required_by_every_authentication_entry_point():
    required = {
        rp.sign_manifest: ("expected_commit",),
        rp.verify_manifest_native: ("expected_key", "expected_commit"),
        rp.check_dists: ("manifest", "expected_key", "expected_commit",
                         "source_root"),
    }
    for function, names in required.items():
        parameters = inspect.signature(function).parameters
        for name in names:
            parameter = parameters.get(name)
            assert parameter is not None, (
                f"{function.__name__} has no required {name} trust input")
            assert parameter.default is inspect.Parameter.empty, (
                f"{function.__name__}.{name} must not be optional")
    assert "source_root" in inspect.signature(
        rp.verify_manifest_native).parameters


def test_signing_rejects_an_expected_commit_other_than_head(tmp_path,
                                                            monkeypatch):
    root = _git_repo(tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", _SEED)
    wrong = "f" * 40 if _head(root) != "f" * 40 else "e" * 40
    with pytest.raises(rp.ReleaseCheckError, match="commit|HEAD"):
        rp.sign_manifest(root, expected_commit=wrong)


def test_a_correctly_signed_manifest_for_the_wrong_commit_is_rejected(
        tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    manifest["gitCommit"] = (
        "f" * 40 if _head(root) != "f" * 40 else "e" * 40)
    _resign(manifest)
    with pytest.raises(rp.ReleaseCheckError, match="commit"):
        _verify(root, manifest)


def test_a_stale_manifest_is_rejected_even_when_source_bytes_are_identical(
        tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    old_commit = manifest["gitCommit"]
    _run(root, "commit", "--allow-empty", "-m", "identical source commit")
    assert _head(root) != old_commit
    with pytest.raises(rp.ReleaseCheckError, match="commit"):
        _verify(root, manifest)


def _good_manifest() -> dict:
    return {
        "schema": rp.MANIFEST_SCHEMA,
        "gitCommit": "a" * 40,
        "branch": "main",
        "files": [{"path": "core.py", "sha256": "b" * 64}],
        "sourceFiles": [
            {"path": "olympus/core.py", "sha256": "b" * 64},
            {"path": "pyproject.toml", "sha256": "f" * 64},
        ],
        "summary": {"total": 1, "verified": 0, "failed": 0},
        "integrity": {
            "manifestHash": "c" * 64,
            "publicKey": "d" * 64,
            "signature": "e" * 128,
            "seedDerivation": "ed25519(sha256(seed))",
        },
    }


def test_a_well_formed_manifest_passes_schema_validation():
    rp.check_manifest_schema(_good_manifest())


@pytest.mark.parametrize("mutate, why", [
    (lambda m: m.update(schema="other/1"), "a foreign schema"),
    (lambda m: m.update(dev=True), "a dev manifest is forgeable"),
    (lambda m: m.pop("gitCommit"), "no commit identity"),
    (lambda m: m.update(gitCommit="A" * 40), "an uppercase commit"),
    (lambda m: m.update(gitCommit="a" * 39), "a short commit"),
    (lambda m: m.update(gitCommit=7), "a non-string commit"),
    (lambda m: m.update(files=[]), "no files"),
    (lambda m: m.update(files="all of them"), "files is not a list"),
    (lambda m: m.update(files=[{"path": "a.py"}]), "an entry with no digest"),
    (lambda m: m.update(files=[{"path": "a.py", "sha256": "short"}]),
     "a malformed digest"),
    (lambda m: m.update(files=[{"path": 1, "sha256": "b" * 64}]),
     "a non-string path"),
    (lambda m: m.update(files=[{"path": "../evil.py", "sha256": "b" * 64}]),
     "a path-traversing entry"),
    (lambda m: m.update(files=[{"path": "/etc/passwd", "sha256": "b" * 64}]),
     "an absolute path"),
    (lambda m: m.update(files=[{"path": "a\\b.py", "sha256": "b" * 64}]),
     "a windows-separator path"),
    (lambda m: m.update(files=[{"path": "__pycache__/a.pyc",
                                "sha256": "b" * 64}]),
     "bytecode in the manifest"),
    (lambda m: m.update(files=[{"path": "a.py", "sha256": "b" * 64},
                               {"path": "A.py", "sha256": "c" * 64}],
                         summary={"total": 2}), "case-colliding entries"),
    (lambda m: m.pop("sourceFiles"), "no repository source map"),
    (lambda m: m.update(sourceFiles=[
        {"path": "pyproject.toml", "sha256": "f" * 64}]),
     "a source map omitting package content"),
    (lambda m: m["sourceFiles"][0].update(sha256="short"),
     "a malformed source digest"),
    (lambda m: m.pop("integrity"), "no integrity block"),
    (lambda m: m["integrity"].pop("signature"), "no signature"),
    (lambda m: m["integrity"].update(publicKey="zz"), "a malformed key"),
    (lambda m: m["integrity"].update(signature="nothex!"),
     "a non-hex signature"),
    (lambda m: m["integrity"].update(seedDerivation="rot13"),
     "an unexpected derivation"),
    (lambda m: m.update(summary={"total": 99}), "a summary that disagrees"),
])
def test_malformed_manifests_fail_schema_validation(mutate, why):
    manifest = _good_manifest()
    mutate(manifest)
    with pytest.raises(rp.ReleaseCheckError):
        rp.check_manifest_schema(manifest)


@pytest.mark.parametrize("manifest", [None, [], "manifest", 7])
def test_a_non_mapping_manifest_fails_closed(manifest):
    with pytest.raises(rp.ReleaseCheckError, match="mapping"):
        rp.check_manifest_schema(manifest)


# =============================================================================
# Distribution binding
# =============================================================================

def _wheel_record(entries: dict[str, bytes], version: str) -> bytes:
    lines = []
    for name, blob in entries.items():
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(blob).digest()).rstrip(b"=").decode()
        lines.append(f"{name},sha256={digest},{len(blob)}")
    lines.append(f"{_STEM}-{version}.dist-info/RECORD,,")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _fixture_core_metadata(root: Path, version: str, *,
                           project_version: str | None = None,
                           extra_headers: bytes = b"",
                           body: bytes | None = None) -> bytes:
    headers = (
        "Metadata-Version: 2.4\n"
        "Name: olympus-council\n"
        f"Version: {project_version or version}\n"
        "Summary: Test Olympus package\n"
        "Author: Test Author\n"
        "License-Expression: MIT\n"
        "Project-URL: Homepage, https://example.invalid/olympus\n"
        "Project-URL: Issues, https://example.invalid/olympus/issues\n"
        "Keywords: test,olympus\n"
        "Classifier: Development Status :: 4 - Beta\n"
        "Classifier: Programming Language :: Python :: 3\n"
        "Requires-Python: >=3.10\n"
        "Description-Content-Type: text/markdown\n"
        "License-File: LICENSE\n"
        "Dynamic: license-file\n"
    ).encode("utf-8")
    if extra_headers:
        headers += extra_headers
        if not headers.endswith(b"\n"):
            headers += b"\n"
    return headers + b"\n" + (
        (root / "README.md").read_bytes() if body is None else body)


def _append_core_header(blob: bytes, header: str, value: str) -> bytes:
    headers, body = blob.split(b"\n\n", 1)
    return headers + f"\n{header}: {value}\n\n".encode("utf-8") + body


def _replace_core_header(blob: bytes, header: str, value: str) -> bytes:
    headers_blob, separator, body_blob = blob.partition(b"\n\n")
    headers = headers_blob.decode("utf-8")
    prefix = f"{header}: "
    lines = headers.splitlines()
    matches = [index for index, line in enumerate(lines)
               if line.startswith(prefix)]
    assert matches
    lines[matches[0]] = prefix + value
    rewritten = ("\n".join(lines) + "\n").encode("utf-8")
    return rewritten + (b"\n" + body_blob if separator else b"")


def _fixture_wheel_metadata(*, extra_headers: bytes = b"") -> bytes:
    blob = (
        b"Wheel-Version: 1.0\n"
        b"Generator: setuptools (84.0.0)\n"
        b"Root-Is-Purelib: true\n"
        b"Tag: py3-none-any\n"
    )
    if extra_headers:
        blob += extra_headers
        if not blob.endswith(b"\n"):
            blob += b"\n"
    return blob


def _check_wheel_file(blob: bytes, source_root: Path) -> None:
    parameters = inspect.signature(rp._check_wheel_file).parameters
    if "source_root" in parameters:
        rp._check_wheel_file(blob, source_root)
    else:                              # frozen-v4 red-baseline compatibility
        rp._check_wheel_file(blob)


def _build_dists(dist: Path, root: Path, *, version: str = _VERSION,
                 extra: dict[str, bytes] | None = None,
                 wheel_extra: dict[str, bytes] | None = None,
                 sdist_extra: dict[str, bytes] | None = None,
                 sdist_mode: str = "w:gz",
                 omit: str | None = None,
                 wheel_omit: str | None = None,
                 sdist_omit: str | None = None,
                 corrupt_record: bool = False,
                 skip_record_entry: bool = False,
                 metadata_version: str | None = None,
                 wheel_metadata_extra: bytes = b"",
                 wheel_entry_points: bytes | None = None) -> None:
    """Build the exact closed wheel/sdist surface required by the release gate.

    ``extra`` remains a convenient package-member override applied to both
    distributions.  The format-specific hooks model a hostile backend that
    changes one output without changing the other.
    """
    dist.mkdir(parents=True, exist_ok=True)
    entries: dict[str, bytes] = {}
    for path in sorted((root / "olympus").rglob("*")):
        if path.is_file():
            entries[f"olympus/{path.relative_to(root / 'olympus').as_posix()}"] \
                = path.read_bytes()
    dist_info = f"{_STEM}-{version}.dist-info"
    metadata = _fixture_core_metadata(
        root, version, project_version=metadata_version)
    wheel_metadata = _fixture_core_metadata(
        root, version, project_version=metadata_version,
        extra_headers=wheel_metadata_extra)
    entry_points = (wheel_entry_points or
                    b"[console_scripts]\nolympus = olympus.cli:main\n")
    entries.update({
        f"{dist_info}/METADATA": wheel_metadata,
        f"{dist_info}/WHEEL": _fixture_wheel_metadata(),
        f"{dist_info}/entry_points.txt": entry_points,
        f"{dist_info}/top_level.txt": b"olympus\n",
        f"{dist_info}/licenses/LICENSE":
            (root / "LICENSE").read_bytes(),
    })
    if extra:
        entries.update(extra)
    if wheel_extra:
        entries.update(wheel_extra)
    if wheel_omit or omit:
        entries.pop(wheel_omit or omit, None)

    record_source = dict(entries)
    if skip_record_entry and (wheel_extra or extra):
        for name in (wheel_extra or extra or {}):
            record_source.pop(name, None)
    record = _wheel_record(record_source, version)
    if corrupt_record:
        record = record.replace(b"sha256=", b"sha256=zz", 1)
    entries[f"{dist_info}/RECORD"] = record

    wheel = dist / f"{_STEM}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, blob in sorted(entries.items()):
            archive.writestr(name, blob)

    sroot = f"{_STEM}-{version}"
    source_blobs: dict[str, bytes] = {}
    listing = subprocess.run(
        ["git", "ls-files"], cwd=root, check=True, capture_output=True,
        text=True).stdout.splitlines()
    for rel in listing:
        path = root / rel
        if path.is_file():
            source_blobs[Path(rel).as_posix()] = path.read_bytes()
    # The signed manifest is the sole generated source member. It cannot be
    # Git-tracked because the signature is produced after checkout.
    source_blobs["olympus/verification.json"] = (
        root / "olympus" / "verification.json").read_bytes()
    if extra:
        source_blobs.update({name: blob for name, blob in extra.items()
                             if name.startswith("olympus/")})
    if sdist_omit or omit:
        source_blobs.pop(sdist_omit or omit, None)

    egg = f"{_STEM}.egg-info"
    generated = {
        f"{egg}/PKG-INFO": metadata,
        f"{egg}/SOURCES.txt": b"",       # filled after the member set closes
        f"{egg}/dependency_links.txt": b"",
        f"{egg}/entry_points.txt":
            b"[console_scripts]\nolympus = olympus.cli:main\n",
        f"{egg}/top_level.txt": b"olympus\n",
    }
    source_names = sorted(set(source_blobs) | set(generated))
    generated[f"{egg}/SOURCES.txt"] = (
        "\n".join(source_names) + "\n").encode("utf-8")
    sdist_blobs = {
        **source_blobs,
        **generated,
        "PKG-INFO": metadata,
        "setup.cfg": b"[egg_info]\ntag_build =\ntag_date = 0\n",
    }
    if sdist_extra:
        sdist_blobs.update(sdist_extra)

    with tarfile.open(dist / f"{_STEM}-{version}.tar.gz",
                      sdist_mode) as archive:
        def add(name: str, blob: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            archive.addfile(info, io.BytesIO(blob))

        for name, blob in sorted(sdist_blobs.items()):
            add(f"{sroot}/{name}", blob)


def test_bound_distributions_verify(tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root, sdist_mode="w:gz")
    sdist = dist / f"{_STEM}-{_VERSION}.tar.gz"
    assert sdist.read_bytes().startswith(b"\x1f\x8b")
    report = _check_dists(root, dist, manifest)
    assert report["version"] == _VERSION


@pytest.mark.parametrize("sdist_mode", ["w", "w:bz2", "w:xz"],
                         ids=["uncompressed", "bzip2", "xz"])
def test_check_dists_rejects_a_non_gzip_sdist_named_tar_gz(
        sdist_mode, tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root, sdist_mode=sdist_mode)
    with pytest.raises(rp.ReleaseCheckError, match="gzip"):
        _check_dists(root, dist, manifest)


def test_check_dists_rejects_an_sdist_missing_the_signed_readme(
        tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root, sdist_omit="README.md")
    with pytest.raises(rp.ReleaseCheckError, match="README|readme"):
        _check_dists(root, dist, manifest)


@pytest.mark.parametrize("license_file", ["LICENSE"])
def test_check_dists_rejects_an_sdist_missing_each_declared_license_file(
        license_file, tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root, sdist_omit=license_file)
    with pytest.raises(rp.ReleaseCheckError, match="license"):
        _check_dists(root, dist, manifest)


def test_real_setuptools_build_is_accepted(tmp_path, monkeypatch):
    """The closed metadata model must match the backend used in production."""
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-B", "-m", "build", "--no-isolation",
         "--outdir", str(dist)],
        cwd=root, check=True, capture_output=True, text=True)
    sdist = dist / f"{_STEM}-{_VERSION}.tar.gz"
    assert sdist.read_bytes().startswith(b"\x1f\x8b")
    with tarfile.open(sdist, "r:gz") as archive:
        names = {member.name for member in archive.getmembers()}
    sroot = f"{_STEM}-{_VERSION}"
    assert {f"{sroot}/README.md", f"{sroot}/LICENSE"} <= names
    report = _check_dists(root, dist, manifest)
    assert report["version"] == _VERSION


def test_post_build_inspection_reverifies_the_manifest_signature(
        tmp_path, monkeypatch):
    """A backend may rewrite both the manifest and matching distributions.

    Keeping the original signer strings is not authentication: inspection
    must verify the signature again after the untrusted build has finished.
    """
    root, manifest = _signed(tmp_path, monkeypatch)
    _verify(root, manifest)                 # the pre-build verification passed

    hostile = b"y = 666\n"
    digest = hashlib.sha256(hostile).hexdigest()
    next(entry for entry in manifest["files"]
         if entry["path"] == "core.py")["sha256"] = digest
    if "sourceFiles" in manifest:          # v4 binds the sdist copy as well
        next(entry for entry in manifest["sourceFiles"]
             if entry["path"] == "olympus/core.py")["sha256"] = digest
    old_signature = manifest["integrity"]["signature"]
    (root / "olympus" / "verification.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    assert manifest["integrity"]["signature"] == old_signature

    dist = tmp_path / "dist"
    _build_dists(dist, root, extra={"olympus/core.py": hostile})
    with pytest.raises(rp.ReleaseCheckError, match="manifest hash|signature"):
        _check_dists(root, dist, manifest)


def test_witness_pubkey_cannot_ship_outside_the_package_signed_map(
        tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    covered = [entry for entry in manifest["files"]
               if entry["path"] != "witness_pubkey.txt"]
    if len(covered) != len(manifest["files"]):
        manifest["files"] = covered
        manifest["summary"]["total"] = len(covered)
        _resign(manifest)
        (root / "olympus" / "verification.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    dist = tmp_path / "dist"
    _build_dists(dist, root)
    with pytest.raises(rp.ReleaseCheckError, match="not covered|unsigned"):
        _check_dists(root, dist, manifest)


@pytest.mark.parametrize("member", [
    "unsigned.txt",
    "unsigned.yaml",
    "unsigned.so",
    "unsigned.dll",
    "UNSIGNED",
    "payload.arbitrary-suffix",
])
def test_every_unsigned_package_member_is_rejected(member, tmp_path,
                                                    monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root, extra={f"olympus/{member}": b"unsigned\n"})
    with pytest.raises(rp.ReleaseCheckError, match="not covered|unsigned"):
        _check_dists(root, dist, manifest)


@pytest.mark.parametrize("distribution", ["wheel", "sdist"])
def test_a_mutated_witness_pubkey_in_either_distribution_is_rejected(
        distribution, tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    kwargs = ({"wheel_extra": {"olympus/witness_pubkey.txt": b"0" * 64}}
              if distribution == "wheel" else
              {"sdist_extra": {"olympus/witness_pubkey.txt": b"0" * 64}})
    dist = tmp_path / "dist"
    _build_dists(dist, root, **kwargs)
    with pytest.raises(rp.ReleaseCheckError, match="hash|match|modified"):
        _check_dists(root, dist, manifest)


@pytest.mark.parametrize("member,payload", [
    ("pyproject.toml", b"[build-system]\nbuild-backend='evil.backend'\n"),
    ("setup.py", b"raise SystemExit('executed')\n"),
    ("build.py", b"raise SystemExit('executed')\n"),
])
def test_modified_or_injected_sdist_build_inputs_are_rejected(
        member, payload, tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root, sdist_extra={member: payload})
    with pytest.raises(rp.ReleaseCheckError, match="source|signed|unsigned|hash"):
        _check_dists(root, dist, manifest)


def test_an_unexpected_executable_top_level_sdist_member_is_rejected(
        tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root,
                 sdist_extra={"release-hook": b"#!/bin/sh\necho owned\n"})
    with pytest.raises(rp.ReleaseCheckError, match="source|unsigned"):
        _check_dists(root, dist, manifest)


def test_forged_wheel_entry_points_are_rejected(tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(
        dist, root,
        wheel_entry_points=(
            b"[console_scripts]\nolympus = attacker.module:main\n"))
    with pytest.raises(rp.ReleaseCheckError, match="entry|console"):
        _check_dists(root, dist, manifest)


def test_an_unsigned_python_file_in_the_wheel_is_rejected(tmp_path,
                                                          monkeypatch):
    """AUDIT (high): distributions were never bound to the signed manifest."""
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root, extra={"olympus/backdoor.py": b"evil = 1\n"})
    with pytest.raises(rp.ReleaseCheckError, match="not covered"):
        _check_dists(root, dist, manifest)


def test_a_mutated_signed_file_in_the_wheel_is_rejected(tmp_path,
                                                        monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root, extra={"olympus/core.py": b"y = 666\n"})
    with pytest.raises(rp.ReleaseCheckError, match="signed hash"):
        _check_dists(root, dist, manifest)


def test_a_missing_signed_file_in_the_wheel_is_rejected(tmp_path,
                                                        monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root, omit="olympus/core.py")
    with pytest.raises(rp.ReleaseCheckError, match="missing"):
        _check_dists(root, dist, manifest)


def test_bytecode_in_a_distribution_is_rejected(tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root,
                 extra={"olympus/__pycache__/core.pyc": b"\x00evil"})
    with pytest.raises(rp.ReleaseCheckError, match="bytecode"):
        _check_dists(root, dist, manifest)


def test_a_corrupt_wheel_record_is_rejected(tmp_path, monkeypatch):
    """AUDIT: RECORD hashes were never validated."""
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root, corrupt_record=True)
    with pytest.raises(rp.ReleaseCheckError, match="RECORD"):
        _check_dists(root, dist, manifest)


def test_a_wheel_member_absent_from_record_is_rejected(tmp_path,
                                                       monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root, extra={"olympus/extra.json": b"{}\n"},
                 skip_record_entry=True)
    with pytest.raises(rp.ReleaseCheckError, match="RECORD"):
        _check_dists(root, dist, manifest)


def test_a_stray_file_in_dist_is_rejected(tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root)
    (dist / "notes.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(rp.ReleaseCheckError, match="unexpected file"):
        _check_dists(root, dist, manifest)


def test_a_duplicate_distribution_is_rejected(tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root)
    (dist / f"{_STEM}-{_VERSION}-py3-none-manylinux1.whl").write_bytes(
        (dist / f"{_STEM}-{_VERSION}-py3-none-any.whl").read_bytes())
    with pytest.raises(rp.ReleaseCheckError, match="exactly one"):
        _check_dists(root, dist, manifest)


def test_forged_wheel_metadata_is_rejected(tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root, metadata_version="0.0.1")
    with pytest.raises(rp.ReleaseCheckError, match="METADATA Version"):
        _check_dists(root, dist, manifest)


def test_forged_wheel_dependencies_are_rejected(tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root,
                 wheel_metadata_extra=b"Requires-Dist: attacker-package\n")
    with pytest.raises(rp.ReleaseCheckError, match="requirements"):
        _check_dists(root, dist, manifest)


@pytest.mark.parametrize("header,value", [
    ("Name", "attacker-shadow-name"),
    ("Version", "0.0.0"),
    ("Requires-Python", ">=99"),
])
def test_duplicate_core_metadata_singletons_are_rejected(
        header, value, tmp_path):
    root = _git_repo(tmp_path)
    blob = _append_core_header(
        _fixture_core_metadata(root, _VERSION), header, value)
    with pytest.raises(rp.ReleaseCheckError, match="singleton|exactly once"):
        rp._check_project_metadata(blob, _VERSION, "test METADATA", root)


@pytest.mark.parametrize("header,value", [
    ("Metadata-Version", "1.0"),
    ("Summary", "attacker-controlled summary"),
    ("Author", "Attacker Person"),
    ("Keywords", "attacker,shadow"),
    ("Description-Content-Type", "text/x-rst"),
    ("License-Expression", "Apache-2.0"),
    ("Dynamic", "attacker-field"),
    ("Project-URL", "Homepage, https://attacker.invalid"),
    ("Classifier", "Private :: Attacker Controlled"),
    ("License-File", "ATTACKER-LICENSE"),
])
def test_forged_core_metadata_is_rejected(header, value, tmp_path):
    root = _git_repo(tmp_path)
    blob = _replace_core_header(
        _fixture_core_metadata(root, _VERSION), header, value)
    with pytest.raises(rp.ReleaseCheckError, match="signed source|policy"):
        rp._check_project_metadata(blob, _VERSION, "test METADATA", root)


def test_forged_core_metadata_readme_body_is_rejected(tmp_path):
    root = _git_repo(tmp_path)
    blob = _fixture_core_metadata(
        root, _VERSION, body=b"# Attacker-controlled description\n")
    with pytest.raises(rp.ReleaseCheckError, match="description|README|signed"):
        rp._check_project_metadata(blob, _VERSION, "test METADATA", root)


@pytest.mark.parametrize("header,value", [
    ("Classifier", "Private :: Attacker Controlled"),
    ("Project-URL", "Backdoor, https://attacker.invalid"),
    ("License-File", "ATTACKER-LICENSE"),
    ("Requires-Dist", "attacker-package"),
    ("Provides-Extra", "attacker"),
])
def test_repeated_core_metadata_collections_are_exactly_bound(
        header, value, tmp_path):
    root = _git_repo(tmp_path)
    blob = _append_core_header(
        _fixture_core_metadata(root, _VERSION), header, value)
    with pytest.raises(rp.ReleaseCheckError, match="collection|signed source"):
        rp._check_project_metadata(blob, _VERSION, "test METADATA", root)


def test_unexpected_core_metadata_header_is_rejected(tmp_path):
    root = _git_repo(tmp_path)
    blob = _append_core_header(
        _fixture_core_metadata(root, _VERSION), "X-Attacker", "shadow")
    with pytest.raises(rp.ReleaseCheckError, match="unexpected|policy"):
        rp._check_project_metadata(blob, _VERSION, "test METADATA", root)


@pytest.mark.parametrize("header,value", [
    ("Wheel-Version", "9.9"),
    ("Root-Is-Purelib", "false"),
])
def test_duplicate_wheel_singletons_are_rejected(header, value, tmp_path):
    root = _git_repo(tmp_path)
    blob = _fixture_wheel_metadata(
        extra_headers=f"{header}: {value}\n".encode("utf-8"))
    with pytest.raises(rp.ReleaseCheckError, match="singleton|exactly once"):
        _check_wheel_file(blob, root)


@pytest.mark.parametrize("header,value", [
    ("Generator", "attacker-backend (9.9)"),
    ("Build", "attacker-build"),
])
def test_forged_or_unexpected_wheel_headers_are_rejected(
        header, value, tmp_path):
    root = _git_repo(tmp_path)
    blob = (_replace_core_header(_fixture_wheel_metadata(), header, value)
            if header == "Generator" else
            _fixture_wheel_metadata(
                extra_headers=f"{header}: {value}\n".encode("utf-8")))
    with pytest.raises(rp.ReleaseCheckError, match="WHEEL|unexpected|policy"):
        _check_wheel_file(blob, root)


def test_unexpected_wheel_metadata_body_is_rejected(tmp_path):
    root = _git_repo(tmp_path)
    with pytest.raises(rp.ReleaseCheckError, match="WHEEL|body|policy"):
        _check_wheel_file(
            _fixture_wheel_metadata() + b"\nattacker body\n", root)


def test_marker_only_setuptools_requirements_section_is_semantically_bound():
    blob = b'[:sys_platform == "win32"]\ntzdata>=2024.1\n'
    flattened = rp._normalise_requirement_lines(blob, "test requirements")
    assert rp._canonical_requirements(flattened, "test requirements") == \
        rp._canonical_requirements(
            ['tzdata>=2024.1; sys_platform == "win32"'],
            "expected requirements")


def test_an_empty_dist_directory_is_rejected(tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    dist.mkdir()
    with pytest.raises(rp.ReleaseCheckError, match="exactly one"):
        _check_dists(root, dist, manifest)


def test_a_shipped_manifest_that_is_not_the_verified_one_is_rejected(
        tmp_path, monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    dist = tmp_path / "dist"
    _build_dists(dist, root)
    other = json.loads(json.dumps(manifest))
    other["integrity"]["signature"] = "f" * 128
    with pytest.raises(rp.ReleaseCheckError, match="not the one"):
        _check_dists(root, dist, other)


@pytest.mark.parametrize("version", ["9.9", "9.9.9rc1", "", None, 9])
def test_check_dists_rejects_a_malformed_version(version, tmp_path,
                                                 monkeypatch):
    root, manifest = _signed(tmp_path, monkeypatch)
    with pytest.raises(rp.ReleaseCheckError, match="stable X.Y.Z"):
        _check_dists(root, tmp_path, manifest, version=version)


# =============================================================================
# Archive member safety
# =============================================================================

def _mk(typeflag, name, linkname=""):
    info = tarfile.TarInfo(name)
    info.type = typeflag
    info.linkname = linkname
    info.size = 0
    return info


def _tar_with(tmp_path: Path, *members: tarfile.TarInfo) -> Path:
    path = tmp_path / f"{_STEM}-{_VERSION}.tar.gz"
    root = f"{_STEM}-{_VERSION}"
    with tarfile.open(path, "w:gz") as archive:
        blob = (f"Metadata-Version: 2.1\nName: olympus-council\n"
                f"Version: {_VERSION}\n").encode()
        info = tarfile.TarInfo(f"{root}/PKG-INFO")
        info.size = len(blob)
        archive.addfile(info, io.BytesIO(blob))
        for member in members:
            archive.addfile(member)
    return path


def _raw_tar_with(name: str, blob: bytes) -> tuple[bytes, int]:
    """Return deterministic tar bytes and the end of the final file record."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(blob)
        archive.addfile(info, io.BytesIO(blob))
        member_records_end = archive.offset
    return buffer.getvalue(), member_records_end


@pytest.mark.parametrize("kind, typeflag, linkname", [
    ("symlink", tarfile.SYMTYPE, "/etc/passwd"),
    ("hardlink", tarfile.LNKTYPE, f"{_STEM}-{_VERSION}/PKG-INFO"),
    ("chardev", tarfile.CHRTYPE, ""),
    ("blockdev", tarfile.BLKTYPE, ""),
    ("fifo", tarfile.FIFOTYPE, ""),
])
def test_non_regular_tar_members_are_rejected(kind, typeflag, linkname,
                                              tmp_path):
    """AUDIT (medium): traversal checks were name-only and link-blind."""
    root = f"{_STEM}-{_VERSION}"
    path = _tar_with(tmp_path, _mk(typeflag, f"{root}/olympus/x", linkname))
    with pytest.raises(rp.ReleaseCheckError,
                       match="symlink|hardlink|device|FIFO|non-regular"):
        rp.check_sdist_members(path, _VERSION)


def test_a_traversing_tar_member_is_rejected(tmp_path):
    root = f"{_STEM}-{_VERSION}"
    path = _tar_with(tmp_path, _mk(tarfile.REGTYPE, f"{root}/../evil.py"))
    with pytest.raises(rp.ReleaseCheckError):
        rp.check_sdist_members(path, _VERSION)


def test_a_tar_member_outside_the_root_is_rejected(tmp_path):
    path = _tar_with(tmp_path, _mk(tarfile.REGTYPE, "elsewhere/evil.py"))
    with pytest.raises(rp.ReleaseCheckError, match="outside its root"):
        rp.check_sdist_members(path, _VERSION)


def test_a_tar_directory_outside_the_root_is_rejected(tmp_path):
    path = _tar_with(tmp_path, _mk(tarfile.DIRTYPE, "elsewhere"))
    with pytest.raises(rp.ReleaseCheckError, match="outside its root"):
        rp.check_sdist_members(path, _VERSION)


def test_an_unused_tar_directory_inside_the_root_is_rejected(tmp_path):
    root = f"{_STEM}-{_VERSION}"
    path = _tar_with(tmp_path, _mk(tarfile.DIRTYPE, f"{root}/unused"))
    with pytest.raises(rp.ReleaseCheckError, match="unused directory"):
        rp.check_sdist_members(path, _VERSION)


@pytest.mark.parametrize("parents_first", [True, False])
def test_legitimate_tar_parent_directories_are_accepted(parents_first,
                                                         tmp_path):
    root = f"{_STEM}-{_VERSION}"
    parents = [
        _mk(tarfile.DIRTYPE, root),
        _mk(tarfile.DIRTYPE, f"{root}/olympus"),
    ]
    child = _mk(tarfile.REGTYPE, f"{root}/olympus/__init__.py")
    members = [*parents, child] if parents_first else [child, *parents]
    blobs = rp.check_sdist_members(_tar_with(tmp_path, *members), _VERSION)
    assert f"{root}/olympus/__init__.py" in blobs


def test_a_zip_symlink_member_is_rejected(tmp_path):
    wheel = tmp_path / f"{_STEM}-{_VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        info = zipfile.ZipInfo("olympus/link.py")
        info.external_attr = (0o120777 << 16) | 0o20
        archive.writestr(info, "/etc/passwd")
    with pytest.raises(rp.ReleaseCheckError, match="symlink"):
        rp.check_wheel_members(wheel, _VERSION)


@pytest.mark.parametrize("payload", [b"", b"not a zip", b"PK\x03\x04garbage"])
def test_a_malformed_wheel_fails_closed_not_crashes(payload, tmp_path):
    """AUDIT (medium): malformed archives crashed with untyped exceptions."""
    wheel = tmp_path / f"{_STEM}-{_VERSION}-py3-none-any.whl"
    wheel.write_bytes(payload)
    with pytest.raises(rp.ReleaseCheckError):
        rp.check_wheel_members(wheel, _VERSION)


def test_a_corrupt_deflate_member_fails_closed_not_crashes(tmp_path):
    wheel = tmp_path / f"{_STEM}-{_VERSION}-py3-none-any.whl"
    member = "olympus/__init__.py"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, b"signed package bytes\n" * 64)
    with zipfile.ZipFile(wheel) as archive:
        info = archive.getinfo(member)

    raw = bytearray(wheel.read_bytes())
    name_length = int.from_bytes(raw[info.header_offset + 26:
                                     info.header_offset + 28], "little")
    extra_length = int.from_bytes(raw[info.header_offset + 28:
                                      info.header_offset + 30], "little")
    compressed_offset = info.header_offset + 30 + name_length + extra_length
    # DEFLATE block type 0b11 is reserved and makes zlib reject the stream.
    raw[compressed_offset] = (raw[compressed_offset] & 0b11111000) | 0b111
    wheel.write_bytes(raw)

    with pytest.raises(rp.ReleaseCheckError, match="could not be read"):
        rp.check_wheel_members(wheel, _VERSION)


@pytest.mark.parametrize("payload", [b"", b"not a tarball"])
def test_a_malformed_sdist_fails_closed_not_crashes(payload, tmp_path):
    sdist = tmp_path / f"{_STEM}-{_VERSION}.tar.gz"
    sdist.write_bytes(payload)
    with pytest.raises(rp.ReleaseCheckError):
        rp.check_sdist_members(sdist, _VERSION)


def test_a_closed_gzip_tar_is_accepted(tmp_path):
    root = f"{_STEM}-{_VERSION}"
    name = f"{root}/PKG-INFO"
    blob = b"valid metadata\n"
    raw, _ = _raw_tar_with(name, blob)
    sdist = tmp_path / f"{_STEM}-{_VERSION}.tar.gz"
    sdist.write_bytes(gzip.compress(raw, mtime=0))

    assert rp.check_sdist_members(sdist, _VERSION) == {name: blob}


def test_a_gzip_tar_with_nonzero_bytes_after_its_end_marker_is_rejected(
        tmp_path):
    root = f"{_STEM}-{_VERSION}"
    raw, _ = _raw_tar_with(f"{root}/PKG-INFO", b"valid metadata\n")
    poison = b"ignored_inner_tar_tail"
    sdist = tmp_path / f"{_STEM}-{_VERSION}.tar.gz"
    sdist.write_bytes(gzip.compress(raw + poison, mtime=0))

    with pytest.raises(rp.ReleaseCheckError,
                       match="end|trailing|archive") as caught:
        rp.check_sdist_members(sdist, _VERSION)
    assert poison.decode("ascii") not in str(caught.value)


def test_concatenated_gzip_cannot_hide_a_second_tar_after_the_first(
        tmp_path):
    root = f"{_STEM}-{_VERSION}"
    first, _ = _raw_tar_with(f"{root}/PKG-INFO", b"valid metadata\n")
    poison = "ignored_second_tar"
    second, _ = _raw_tar_with(f"elsewhere/{poison}", b"attacker data\n")
    sdist = tmp_path / f"{_STEM}-{_VERSION}.tar.gz"
    sdist.write_bytes(
        gzip.compress(first, mtime=0) + gzip.compress(second, mtime=0))

    with pytest.raises(rp.ReleaseCheckError,
                       match="end|trailing|archive") as caught:
        rp.check_sdist_members(sdist, _VERSION)
    assert poison not in str(caught.value)


def test_a_gzip_tar_missing_both_end_of_archive_blocks_is_rejected(tmp_path):
    root = f"{_STEM}-{_VERSION}"
    raw, member_records_end = _raw_tar_with(
        f"{root}/PKG-INFO", b"valid metadata\n")
    sdist = tmp_path / f"{_STEM}-{_VERSION}.tar.gz"
    sdist.write_bytes(gzip.compress(raw[:member_records_end], mtime=0))

    with pytest.raises(rp.ReleaseCheckError, match="end|zero|archive"):
        rp.check_sdist_members(sdist, _VERSION)


def test_a_late_corrupt_gzip_stream_fails_closed_not_crashes(tmp_path):
    """A compressor error reached the CLI as an unsanitized traceback."""
    root = f"{_STEM}-{_VERSION}"
    raw_tar = io.BytesIO()
    with tarfile.open(fileobj=raw_tar, mode="w") as archive:
        for name in (f"{root}/first", f"{root}/second"):
            blob = (name.encode("utf-8") + b"\n") * 128
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            archive.addfile(info, io.BytesIO(blob))

    raw = raw_tar.getvalue()
    split = len(raw) // 2
    first = gzip.compress(raw[:split], mtime=0)
    second = bytearray(gzip.compress(raw[split:], mtime=0))
    # The first DEFLATE block starts after gzip's fixed ten-byte header.
    # BTYPE 0b11 is reserved, so the second concatenated member fails only
    # after tarfile has successfully opened and consumed the first member.
    second[10] = (second[10] & 0b11111000) | 0b111
    sdist = tmp_path / f"{_STEM}-{_VERSION}.tar.gz"
    sdist.write_bytes(first + second)

    with pytest.raises(rp.ReleaseCheckError,
                       match="member table|could not be read"):
        rp.check_sdist_members(sdist, _VERSION)


def test_a_wheel_without_a_record_is_rejected(tmp_path):
    wheel = tmp_path / f"{_STEM}-{_VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("olympus/__init__.py", "x = 1\n")
    with pytest.raises(rp.ReleaseCheckError, match="RECORD"):
        rp.check_wheel_members(wheel, _VERSION)


# =============================================================================
# Sanitized, closed failure messages
# =============================================================================

def test_rejected_paths_are_never_echoed():
    """AUDIT (medium): the module claimed sanitized messages it did not
    enforce. A rejected path is attacker-influenced text in a CI log."""
    poison = "SEED_sk_live_deadbeef"
    with pytest.raises(rp.ReleaseCheckError) as caught:
        rp.ensure_safe_relpath(f"../{poison}.py")
    assert poison not in str(caught.value)


def test_manifest_failures_do_not_echo_manifest_contents():
    poison = "SEED_sk_live_cafebabe"
    with pytest.raises(rp.ReleaseCheckError) as caught:
        rp.check_manifest_schema({"schema": poison})
    assert poison not in str(caught.value)


def test_signing_failures_never_echo_the_seed(tmp_path, monkeypatch):
    poison = "sk_live_super_secret_seed_value"
    root = _git_repo(tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", poison)
    with pytest.raises(rp.ReleaseCheckError) as caught:
        _sign(root)
    rendered = f"{caught.value!r} {caught.value}"
    assert poison not in rendered, "the signing seed leaked into the error"


def test_the_cli_reports_failure_without_leaking_the_environment(
        tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "super-secret-seed-value")
    code = rp.main(["check-tag", "v1.2.3rc1", "--root", str(tmp_path),
                    "--no-runtime"])
    assert code == 1
    captured = capsys.readouterr()
    assert "RELEASE CHECK FAILED" in captured.err
    assert "super-secret-seed-value" not in captured.err + captured.out


def test_the_cli_succeeds_on_a_valid_tag(tmp_path, capsys):
    _write_pyproject(tmp_path, _VERSION)
    code = rp.main(["check-tag", _TAG, "--root", str(tmp_path),
                    "--no-runtime"])
    assert code == 0
    assert "verified" in capsys.readouterr().out


def test_the_cli_signs_and_verifies_end_to_end(tmp_path, capsys,
                                               monkeypatch):
    root = _git_repo(tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", _SEED)
    commit = _head(root)
    assert rp.main(["sign-manifest", "--root", str(root),
                    "--expected-commit", commit]) == 0
    assert rp.main(["verify-manifest", "--root", str(root),
                    "--expected-commit", commit]) == 0
    out = capsys.readouterr().out
    assert "signed release manifest written" in out
    assert "signed manifest verified" in out
