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


def _declared_deps() -> tuple[list[str], list[str]]:
    """Runtime dependencies, split into unconditional and marker-scoped.

    A dependency behind an environment marker (`; sys_platform == "win32"`) is
    not installed everywhere, so folding it into one number would either
    overstate the footprint on Linux or understate it on Windows. The README
    states both and this returns both.
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    always, conditional = [], []
    for entry in data["project"]["dependencies"]:
        (conditional if ";" in entry else always).append(entry)
    return always, conditional


def _declared_dep_count() -> int:
    return len(_declared_deps()[0])


def _readme_dep_count() -> int:
    text = (ROOT / "README.md").read_text()
    matches = re.findall(r"(\d+)\s+runtime deps", text)
    assert matches, "README does not state a 'N runtime deps' claim"
    return int(matches[0])


def _readme_conditional_count() -> int:
    text = (ROOT / "README.md").read_text()
    matches = re.findall(r"runtime deps \(\+(\d+) on ", text)
    return int(matches[0]) if matches else 0


def test_readme_runtime_dep_count_matches_pyproject():
    assert _readme_dep_count() == _declared_dep_count()


def test_readme_states_any_platform_scoped_runtime_dependency():
    """A marker-scoped dependency is still a dependency somewhere.

    Leaving it out of the count would make "3 runtime deps" true on Linux and
    quietly false on Windows, which is the kind of claim this file exists to
    stop drifting.
    """
    _, conditional = _declared_deps()
    assert _readme_conditional_count() == len(conditional), (
        f"pyproject declares {len(conditional)} platform-scoped runtime "
        f"dependencies ({[e.split(';')[0].strip() for e in conditional]}) and "
        f"the README states {_readme_conditional_count()}")


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


# Import name -> distribution (pyproject) name, for the few that differ.
_IMPORT_TO_DIST = {
    "yaml": "pyyaml",
    "docx": "python-docx",
    "youtube_transcript_api": "youtube-transcript-api",
}
# Stdlib backports that need no declaration: `tomli` is only imported as a
# Python-3.10 fallback for stdlib `tomllib`, and even then a pure-regex fallback
# exists (codegraph_manifest.py) — so it is not a required install.
_STDLIB_BACKPORTS = {"tomli"}


def _third_party_imports() -> set[str]:
    """Every top-level third-party module olympus/ imports anywhere — including
    inside functions/try-blocks — via an AST walk (NOT a hand-maintained list)."""
    import ast
    import sys

    stdlib = set(sys.stdlib_module_names)
    found: set[str] = set()
    for path in (ROOT / "olympus").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return {m for m in found if m not in stdlib and m != "olympus"}


def _required_packages() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    out: set[str] = set()
    for spec in data["project"]["dependencies"]:
        name = re.split(r"[<>=!~ \[]", spec, 1)[0].strip().lower()
        if name:
            out.add(name)
    return out


def test_no_undeclared_third_party_imports():
    # The load-bearing "small, auditable dependency surface" claim, ENFORCED: the
    # code may not import any third-party module that isn't a declared required
    # dependency or optional extra. This scans the code (AST), so a new lazy
    # import (e.g. pypdf for parse_document) FAILS CI until it is declared —
    # unlike the hardcoded check above, which could not have caught it.
    declared = _required_packages() | _declared_optional_packages()
    undeclared = []
    for mod in sorted(_third_party_imports()):
        if mod in _STDLIB_BACKPORTS:
            continue
        dist = _IMPORT_TO_DIST.get(mod, mod).lower()
        if dist not in declared:
            undeclared.append(f"{mod} (-> {dist})")
    assert not undeclared, (
        "olympus imports third-party modules not declared in pyproject "
        f"[project.dependencies] or [optional-dependencies]: {undeclared}")
