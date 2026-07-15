"""Anti-rot guard for the README's runtime-dependency claim.

The comparison-table "Footprint" cell advertises the number of runtime
dependencies (e.g. "3 runtime deps"). That number is a load-bearing
differentiator ("small, auditable"), so it must stay in sync with the real
`[project].dependencies` list in `pyproject.toml`. This test parses both and
asserts they agree, so the claim can never silently drift.

`tomllib` is stdlib only on Python 3.11+; on 3.10 the test skips cleanly.
"""

import re
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")

ROOT = Path(__file__).resolve().parent.parent


def _declared_dep_count() -> int:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return len(data["project"]["dependencies"])


def _readme_dep_count() -> int:
    text = (ROOT / "README.md").read_text()
    matches = re.findall(r"(\d+)\s+runtime deps", text)
    assert matches, "README does not state a 'N runtime deps' claim"
    return int(matches[0])


def test_readme_runtime_dep_count_matches_pyproject():
    assert _readme_dep_count() == _declared_dep_count()


def _declared_optional_packages() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    extras = data["project"].get("optional-dependencies", {})
    out: set[str] = set()
    for group in extras.values():
        for spec in group:
            name = re.split(r"[<>=!~ \[]", spec, 1)[0].strip().lower()
            if name:
                out.add(name)
    return out


def test_soft_dependencies_are_declared_as_optional_extras():
    # Every third-party package the code imports LAZILY (a soft dependency) must
    # be declared as an optional extra, so nothing the code imports is
    # undeclared — the dependency surface stays fully truthful (M0.5).
    declared = _declared_optional_packages()
    for soft in ("psycopg", "websockets", "pyyaml"):
        assert soft in declared, (
            f"{soft!r} is imported in olympus/ but is not declared in "
            "[project.optional-dependencies]")
