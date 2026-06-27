#!/usr/bin/env python3
"""Auto version-bump helper — make cutting a release a one-liner.

Releasing Olympus means three coordinated edits (pyproject version, the
`__version__` fallback, and the CHANGELOG), then a tag. Doing them by hand is
error-prone — a mismatched version is exactly the kind of drift the rest of the
project works hard to prevent. This script does all the edits atomically and
tells you the one manual step left (creating the tag/release).

Usage:
    python scripts/bump_version.py patch          # 0.17.0 -> 0.17.1
    python scripts/bump_version.py minor          # 0.17.0 -> 0.18.0
    python scripts/bump_version.py major          # 0.17.0 -> 1.0.0
    python scripts/bump_version.py --set 1.2.3    # explicit
    python scripts/bump_version.py minor --dry-run # show, change nothing

What it does:
  * bumps `version` in pyproject.toml,
  * updates the `__version__` fallback literal in olympus/__init__.py,
  * in CHANGELOG.md, turns `## [Unreleased]` into a dated `## [X.Y.Z]` section
    and adds a fresh empty `[Unreleased]`, and fixes the compare links,
  * prints the git commands to commit and tag.

The version math (`next_version`) is a pure function so it's unit-tested.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "olympus" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
REPO_URL = "https://github.com/maadjiba24-afk/Olympus-"

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def next_version(current: str, part: str) -> str:
    """Compute the next semantic version. `part` ∈ {major, minor, patch}."""
    m = _SEMVER.match(current.strip())
    if not m:
        raise ValueError(f"current version is not X.Y.Z: {current!r}")
    major, minor, patch = (int(x) for x in m.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump part: {part!r} (use major/minor/patch)")


def read_current_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit("Could not find version in pyproject.toml")
    return m.group(1)


def _bump_pyproject(old: str, new: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    text = re.sub(r'(?m)^(version\s*=\s*")' + re.escape(old) + r'(")',
                  rf"\g<1>{new}\g<2>", text, count=1)
    PYPROJECT.write_text(text, encoding="utf-8")


def _bump_init(old: str, new: str) -> None:
    text = INIT.read_text(encoding="utf-8")
    # Replace the fallback literal(s) only (e.g. __version__ = "0.17.0").
    text = text.replace(f'"{old}"', f'"{new}"')
    INIT.write_text(text, encoding="utf-8")


def _bump_changelog(old: str, new: str, date: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    if f"## [{new}]" in text:
        return  # already cut
    # 1) Promote the Unreleased section to a dated release, leaving a fresh one.
    text = text.replace(
        "## [Unreleased]\n",
        f"## [Unreleased]\n\n## [{new}] — {date}\n", 1)
    # 2) Fix the compare links at the bottom.
    text = text.replace(
        f"[Unreleased]: {REPO_URL}/compare/v{old}...HEAD",
        f"[Unreleased]: {REPO_URL}/compare/v{new}...HEAD\n"
        f"[{new}]: {REPO_URL}/compare/v{old}...v{new}", 1)
    CHANGELOG.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Bump the Olympus version.")
    p.add_argument("part", nargs="?", default="patch",
                   choices=["major", "minor", "patch"])
    p.add_argument("--set", dest="explicit", help="set an explicit X.Y.Z")
    p.add_argument("--dry-run", action="store_true",
                   help="print the change without writing files")
    args = p.parse_args(argv)

    current = read_current_version()
    new = args.explicit or next_version(current, args.part)
    if not _SEMVER.match(new):
        raise SystemExit(f"--set value must be X.Y.Z: {new!r}")
    if new == current:
        raise SystemExit(f"new version equals current ({current}); nothing to do")

    date = datetime.date.today().isoformat()
    print(f"Bumping {current} -> {new}  ({date})")
    if args.dry_run:
        print("(dry run — no files changed)")
        return 0

    _bump_pyproject(current, new)
    _bump_init(current, new)
    _bump_changelog(current, new, date)
    print(f"""
Updated:
  pyproject.toml      version -> {new}
  olympus/__init__.py __version__ fallback -> {new}
  CHANGELOG.md        cut [{new}] section + fresh [Unreleased]

Next:
  1. Edit CHANGELOG.md if the [{new}] notes need polishing.
  2. python -m olympus capabilities --check     # numbers still match
  3. Commit:   git add -A && git commit -m "Release {new}"
  4. After it merges to main, create a GitHub Release with tag v{new}
     (or: git tag v{new} && git push origin v{new}) — that publishes to PyPI.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
