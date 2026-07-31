"""Every file a release SIGNS must be a file the wheel SHIPS.

`witness.tracked_files()` vouches for every `.py`/`.md`/`.json` under the
package, and `olympus verify` re-checks each one against the signed manifest. So
a tracked file that `[tool.setuptools.package-data]` does not ship makes verify
fail on *every installed copy*:

    [verify] missing tracked file: profiles/README.md

That is not hypothetical — 0.25.0, 0.26.0 and 0.27.0 all shipped that way, so
`pip install olympus-council && olympus verify` (the exact command RELEASING.md
hands an auditor) returned a failure on every published release. Nothing was
tampered with; the manifest simply described a file the wheel omitted. The
integrity story is only worth as much as this invariant.

This guard is deliberately parsed with a regex rather than `tomllib`: tomllib is
stdlib only on 3.11+, and a packaging guard that silently skips on the oldest
supported Python is a guard with a hole in exactly the configuration least
likely to be noticed. (Same reason `test_ci_matrix.py` regex-parses.)
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from olympus import witness

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "olympus"


def _declared_patterns() -> list[str]:
    """The `olympus = [...]` globs from `[tool.setuptools.package-data]`."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = re.search(
        r"^\[tool\.setuptools\.package-data\](.*?)(?=^\[|\Z)",
        text, re.S | re.M)
    assert section, "pyproject has no [tool.setuptools.package-data] section"
    entry = re.search(r"^\s*olympus\s*=\s*\[(.*?)\]", section.group(1), re.S | re.M)
    assert entry, "package-data declares no `olympus` entry"
    return re.findall(r'"([^"]+)"', entry.group(1))


def _shipped(rel: str, patterns: list[str]) -> bool:
    """Whether `rel` (package-relative, posix) is shipped.

    A `.py` file ships as a module when its directory is an importable package;
    anything else needs a package-data glob. `fnmatch` treats `*` as matching
    `/` too, so `**/x` and `*/x` both cover nested paths here — the assertion is
    about coverage, and over-matching would only make this test *stricter*."""
    return any(fnmatch.fnmatch(rel, pat) for pat in patterns)


def test_package_data_declares_a_pattern_for_every_tracked_file():
    """The invariant: signed ⊆ shipped."""
    patterns = _declared_patterns()
    unshipped = []
    for path in witness.tracked_files(PKG):
        rel = path.relative_to(PKG).as_posix()
        if path.suffix == ".py":
            if path.parent == PKG or (path.parent / "__init__.py").exists():
                continue                    # ships as a module
            unshipped.append(f"{rel} (.py outside an importable package)")
            continue
        if not _shipped(rel, patterns):
            unshipped.append(f"{rel} (no package-data pattern)")
    assert not unshipped, (
        "these files are signed by the release manifest but would NOT ship in "
        "the wheel, so `olympus verify` fails on every install:\n  "
        + "\n  ".join(unshipped)
        + f"\ndeclared patterns: {patterns}")


def test_the_regression_that_shipped_is_covered():
    """`profiles/README.md` specifically — the file that broke three releases."""
    assert (PKG / "profiles" / "README.md").exists(), "fixture file vanished"
    assert _shipped("profiles/README.md", _declared_patterns())


def test_tracked_files_still_means_py_md_json():
    """This guard is only meaningful while `tracked_files` keeps that rule. If
    it starts vouching for another suffix, the package-data globs must grow to
    match — fail here rather than ship an unverifiable wheel."""
    suffixes = {p.suffix for p in witness.tracked_files(PKG)}
    assert suffixes <= {".py", ".md", ".json"}, (
        f"tracked_files now vouches for {suffixes - {'.py', '.md', '.json'}}; "
        "extend [tool.setuptools.package-data] to cover it")


def test_patterns_are_not_silently_empty():
    """A typo'd section name would make every other assertion vacuous."""
    patterns = _declared_patterns()
    assert patterns, "package-data patterns parsed as empty"
    assert any(p.endswith(".md") for p in patterns)
    assert any(p.endswith(".json") for p in patterns)
