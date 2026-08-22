"""Contracts for the content-addressed PyPA publisher descriptor."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_ACTION_DIR = _ROOT / ".github" / "actions" / "pypi-publish"
_ACTION_PATH = _ACTION_DIR / "action.yml"
_PROVENANCE_PATH = _ACTION_DIR / "UPSTREAM.md"
_LICENSE_PATH = _ACTION_DIR / "LICENSE.md"
_HELPER_PATH = _ROOT / "scripts" / "release_pipeline.py"

_UPSTREAM_COMMIT = "dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
_DIGEST = ("sha256:a68d05519f6d7e47372aeaddab80b851b69afa89be179ec4"
           "1775c72c4e3ab2d5")
_IMAGE = f"docker://ghcr.io/pypa/gh-action-pypi-publish@{_DIGEST}"
_DIGEST_IMAGE_RE = re.compile(
    r"\Adocker://ghcr\.io/pypa/gh-action-pypi-publish@"
    r"sha256:[0-9a-f]{64}\Z")


def _action():
    value = yaml.safe_load(_ACTION_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_vendored_directory_is_metadata_only():
    assert sorted(path.name for path in _ACTION_DIR.iterdir()) == [
        "LICENSE.md",
        "UPSTREAM.md",
        "action.yml",
    ]


def test_publisher_executes_only_the_audited_manifest_digest():
    runs = _action().get("runs")
    assert runs == {"using": "docker", "image": _IMAGE}
    assert _DIGEST_IMAGE_RE.fullmatch(runs["image"])
    assert ":dc37677" not in runs["image"]
    assert ":latest" not in runs["image"]


def test_descriptor_exposes_only_the_normalized_upstream_inputs():
    inputs = _action().get("inputs")
    assert set(inputs) == {
        "user",
        "password",
        "repository-url",
        "packages-dir",
        "verify-metadata",
        "skip-existing",
        "verbose",
        "print-hash",
        "attestations",
    }
    assert "repository_url" not in inputs
    assert "packages_dir" not in inputs
    assert "verify_metadata" not in inputs
    assert "skip_existing" not in inputs
    assert "print_hash" not in inputs


def test_effective_defaults_match_upstream_v1_14_2():
    inputs = _action()["inputs"]
    defaults = {name: spec.get("default") for name, spec in inputs.items()}
    assert defaults == {
        "user": "__token__",
        "password": "",
        "repository-url": "https://upload.pypi.org/legacy/",
        "packages-dir": "dist",
        "verify-metadata": "true",
        "skip-existing": "false",
        "verbose": "true",
        "print-hash": "true",
        "attestations": "true",
    }
    assert all(spec.get("required") is False for spec in inputs.values())


def test_descriptor_cannot_override_entrypoint_or_pass_custom_arguments():
    runs = _action()["runs"]
    for forbidden in ("entrypoint", "args", "env", "pre-entrypoint",
                      "post-entrypoint"):
        assert forbidden not in runs


def test_helper_and_descriptor_pin_the_same_digest():
    source = _HELPER_PATH.read_text(encoding="utf-8")
    joined = re.sub(r'"\s*\n\s*"', "", source)
    assert _DIGEST in joined
    assert _ACTION_PATH.read_text(encoding="utf-8").count(_DIGEST) == 1


def test_provenance_records_exact_upstream_identity_hashes_and_delta():
    provenance = _PROVENANCE_PATH.read_text(encoding="utf-8")
    for required in (
        "pypa/gh-action-pypi-publish",
        "v1.14.2",
        _UPSTREAM_COMMIT,
        _DIGEST,
        "4833fe3c15180e27fc7af77018b1d8c670abf1f17958d45d5e0feb4f9acc5d3d",
        "28924f16b01d3a63e0a9ef5733d7d261329efcff255d8004a96d27d373d33e8c",
        "sole execution change",
        "publishing is disabled",
    ):
        assert required in provenance


def test_upstream_license_is_retained():
    license_text = _LICENSE_PATH.read_text(encoding="utf-8")
    assert "Copyright © 2019, Sviatoslav Sydorenko" in license_text
    assert "Redistribution and use in source and binary forms" in license_text
    assert 'PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"' in \
        license_text


def test_descriptor_and_provenance_contain_no_secret_or_token_value():
    combined = "\n".join(path.read_text(encoding="utf-8")
                           for path in (_ACTION_PATH, _PROVENANCE_PATH))
    for forbidden in ("secrets.", "pypi-AgEI", "OLYMPUS_SIGNING_SEED"):
        assert forbidden not in combined
