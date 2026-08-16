"""W1-5: the setuptools version is pinned in THREE places and they must agree.

`requirements-publish.lock` and `_SETUPTOOLS_METADATA_POLICY_VERSION` were
already cross-checked. The third — `[project.optional-dependencies].test` in
pyproject.toml — drifted silently and surfaced only as a confusing
wheel-metadata failure, which is how it presented in #245 and cost most of a
debugging cycle.

Nothing in the suite covered this helper at all before now.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

release_pipeline = pytest.importorskip(
    "release_pipeline",
    reason="release_pipeline needs tomllib (3.11+) or the tomli backport")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway copy of the three pinned files, so a mutation cannot touch
    the real tree even if an assertion fails mid-test."""
    for name in ("requirements-publish.lock", "pyproject.toml"):
        shutil.copy2(REPO / name, tmp_path / name)
    return tmp_path


def _set_lock(root: Path, version: str) -> None:
    p = root / "requirements-publish.lock"
    p.write_text(
        re.sub(r"(?m)^setuptools==[0-9.]+", f"setuptools=={version}",
               p.read_text(encoding="utf-8")), encoding="utf-8")


def _set_extra(root: Path, version: str) -> None:
    p = root / "pyproject.toml"
    p.write_text(
        p.read_text(encoding="utf-8").replace(
            '"setuptools==84.0.0"', f'"setuptools=={version}"'),
        encoding="utf-8")


# --- the real repository must be consistent --------------------------------

def test_the_repo_itself_has_all_three_pins_in_sync():
    """The check that would have caught #245, run against the real tree."""
    assert release_pipeline._pinned_setuptools_version(REPO) \
        == release_pipeline._SETUPTOOLS_METADATA_POLICY_VERSION


def test_all_three_locations_are_read_independently():
    assert release_pipeline._lock_setuptools_version(REPO) \
        == release_pipeline._SETUPTOOLS_METADATA_POLICY_VERSION
    assert release_pipeline._test_extra_setuptools_version(REPO) \
        == release_pipeline._SETUPTOOLS_METADATA_POLICY_VERSION


# --- drift in each location, independently ---------------------------------
#
# A check that only catches drift in one direction is half a check.

def test_lock_drift_is_caught(repo):
    _set_lock(repo, "84.0.1")
    with pytest.raises(release_pipeline.ReleaseCheckError) as err:
        release_pipeline._pinned_setuptools_version(repo)
    msg = str(err.value)
    assert "DRIFTED" in msg
    assert "requirements-publish.lock: 84.0.1" in msg, msg
    assert "84.0.0" in msg, "the message must show what the others say"


def test_policy_constant_drift_is_caught(repo, monkeypatch):
    monkeypatch.setattr(release_pipeline,
                        "_SETUPTOOLS_METADATA_POLICY_VERSION", "84.0.2")
    with pytest.raises(release_pipeline.ReleaseCheckError) as err:
        release_pipeline._pinned_setuptools_version(repo)
    msg = str(err.value)
    assert "_SETUPTOOLS_METADATA_POLICY_VERSION): 84.0.2" in msg, msg


def test_test_extra_drift_is_caught(repo):
    """The pin that was NOT checked before this change."""
    _set_extra(repo, "84.0.3")
    with pytest.raises(release_pipeline.ReleaseCheckError) as err:
        release_pipeline._pinned_setuptools_version(repo)
    msg = str(err.value)
    assert "optional-dependencies].test): 84.0.3" in msg, msg


def test_failure_names_every_location_and_value(repo):
    """The point of the message: a reader must see which one drifted, rather
    than having to work out which one is supposed to be authoritative."""
    _set_extra(repo, "84.0.3")
    with pytest.raises(release_pipeline.ReleaseCheckError) as err:
        release_pipeline._pinned_setuptools_version(repo)
    msg = str(err.value)
    for location in ("requirements-publish.lock",
                     "_SETUPTOOLS_METADATA_POLICY_VERSION",
                     "optional-dependencies].test"):
        assert location in msg, f"{location} missing from:\n{msg}"


def test_message_is_ascii_only(repo):
    """This lands in CI logs whose encoding is not ours to choose; an em-dash
    here decoded as mojibake the first time it was captured."""
    _set_extra(repo, "84.0.3")
    with pytest.raises(release_pipeline.ReleaseCheckError) as err:
        release_pipeline._pinned_setuptools_version(repo)
    str(err.value).encode("ascii")        # raises if a non-ASCII char crept in


# --- malformed rather than merely drifted ----------------------------------

def test_missing_test_extra_pin_is_caught(repo):
    p = repo / "pyproject.toml"
    p.write_text(p.read_text(encoding="utf-8").replace(
        '"setuptools==84.0.0",', ""), encoding="utf-8")
    with pytest.raises(release_pipeline.ReleaseCheckError) as err:
        release_pipeline._pinned_setuptools_version(repo)
    assert "exactly once" in str(err.value)


def test_malformed_test_extra_pin_is_caught(repo):
    _set_extra(repo, "84.0")               # not X.Y.Z
    with pytest.raises(release_pipeline.ReleaseCheckError) as err:
        release_pipeline._pinned_setuptools_version(repo)
    assert "malformed" in str(err.value)


def test_build_system_pin_is_not_mistaken_for_the_test_extra(repo):
    """pyproject names setuptools seven times, including
    `[build-system] requires = ["setuptools>=77"]` — a DIFFERENT pin, on
    purpose. Reading the TOML structurally cannot confuse the two; a regex over
    the file would have had to."""
    text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires = ["setuptools>=77"]' in text, "fixture assumption changed"
    assert release_pipeline._test_extra_setuptools_version(repo) == "84.0.0"
