"""The version-bump helper: semver math + the file edits it performs."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "bump_version", ROOT / "scripts" / "bump_version.py")
bump = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump)


def test_next_version_patch_minor_major():
    assert bump.next_version("0.17.0", "patch") == "0.17.1"
    assert bump.next_version("0.17.0", "minor") == "0.18.0"
    assert bump.next_version("0.17.3", "major") == "1.0.0"
    assert bump.next_version("1.2.9", "patch") == "1.2.10"


def test_next_version_rejects_bad_input():
    with pytest.raises(ValueError):
        bump.next_version("v0.17", "patch")
    with pytest.raises(ValueError):
        bump.next_version("0.17.0", "sideways")


def test_changelog_edit_promotes_unreleased(tmp_path, monkeypatch):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- a thing\n\n"
        "## [0.17.0] — 2026-06-27\n- old\n\n"
        "[Unreleased]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.17.0...HEAD\n"
        "[0.17.0]: https://github.com/maadjiba24-afk/Olympus-/releases/tag/v0.17.0\n",
        encoding="utf-8")
    monkeypatch.setattr(bump, "CHANGELOG", cl)
    bump._bump_changelog("0.17.0", "0.18.0", "2026-07-01")
    text = cl.read_text(encoding="utf-8")
    assert "## [0.18.0] — 2026-07-01" in text
    assert "## [Unreleased]\n\n## [0.18.0]" in text          # fresh unreleased kept
    assert "- a thing" in text                                # notes carried down
    assert "compare/v0.18.0...HEAD" in text                   # link updated
    assert "[0.18.0]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.17.0...v0.18.0" in text


def test_changelog_edit_idempotent(tmp_path, monkeypatch):
    cl = tmp_path / "CHANGELOG.md"
    base = ("## [Unreleased]\n\n## [0.18.0] — 2026-07-01\n- x\n\n"
            "[Unreleased]: https://github.com/maadjiba24-afk/Olympus-/compare/v0.18.0...HEAD\n")
    cl.write_text(base, encoding="utf-8")
    monkeypatch.setattr(bump, "CHANGELOG", cl)
    bump._bump_changelog("0.17.0", "0.18.0", "2026-07-01")   # already cut
    assert cl.read_text(encoding="utf-8") == base            # unchanged


def test_pyproject_and_init_edits(tmp_path, monkeypatch):
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "olympus-council"\nversion = "0.17.0"\n',
                  encoding="utf-8")
    init = tmp_path / "__init__.py"
    init.write_text('__version__ = "0.17.0"\n', encoding="utf-8")
    monkeypatch.setattr(bump, "PYPROJECT", py)
    monkeypatch.setattr(bump, "INIT", init)
    assert bump.read_current_version() == "0.17.0"
    bump._bump_pyproject("0.17.0", "0.18.0")
    bump._bump_init("0.17.0", "0.18.0")
    assert 'version = "0.18.0"' in py.read_text(encoding="utf-8")
    assert '__version__ = "0.18.0"' in init.read_text(encoding="utf-8")
