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
