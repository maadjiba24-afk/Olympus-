#!/usr/bin/env python3
"""Fail if any locked dependency is a pre-release (alpha/beta/rc/dev).

Pre-release dependencies are a supply-chain risk: they move, get yanked, and
ship unfinished code. Olympus's dependency tree is pre-release-free today, and
this guard keeps it that way — CI runs it against `requirements.lock`.

Usage:
    python scripts/check_no_prerelease.py [LOCKFILE ...]   # default: requirements.lock

Exit code 0 = clean, 1 = at least one pre-release pin found (named on stderr).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# A pinned line in a pip/uv lockfile: `name==version` (ignore extras, markers,
# and the trailing `\` that precedes --hash continuations).
_PIN_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*==\s*([A-Za-z0-9._!+-]+)")

# Fallback PEP 440 pre-release/dev detector: a release marker that follows a
# digit, e.g. 1.0a1, 1.0.0rc2, 2.1.dev3, 1.0c1. Post-releases (1.0.post1) and
# normal releases (2026.6.17) are intentionally NOT matched.
_PRE_RE = re.compile(r"\d[._-]?(?:a|b|c|rc|alpha|beta|pre|preview|dev)\d*",
                     re.IGNORECASE)


def is_prerelease(version: str) -> bool:
    """True if `version` is a PEP 440 pre-release or dev release. Uses
    `packaging` when available (canonical), else a regex fallback so the guard
    can run even before dependencies are installed."""
    try:
        from packaging.version import InvalidVersion, Version
        try:
            v = Version(version)
            return bool(v.is_prerelease or v.is_devrelease)
        except InvalidVersion:
            return bool(_PRE_RE.search(version))
    except ImportError:
        return bool(_PRE_RE.search(version))


def scan(text: str) -> list[tuple[str, str]]:
    """Return [(name, version), ...] for every pre-release pin in lockfile text."""
    offenders = []
    for line in text.splitlines():
        m = _PIN_RE.match(line)
        if m and is_prerelease(m.group(2)):
            offenders.append((m.group(1), m.group(2)))
    return offenders


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    paths = [Path(p) for p in argv] or [Path("requirements.lock")]
    total = 0
    for path in paths:
        if not path.exists():
            print(f"error: lockfile not found: {path}", file=sys.stderr)
            return 2
        offenders = scan(path.read_text(encoding="utf-8"))
        if offenders:
            total += len(offenders)
            print(f"✗ {path}: {len(offenders)} pre-release dependency(ies):",
                  file=sys.stderr)
            for name, version in offenders:
                print(f"    {name}=={version}", file=sys.stderr)
        else:
            print(f"✓ {path}: no pre-release dependencies.")
    if total:
        print(f"\nRefusing pre-release dependencies ({total} found). Pin a "
              "stable release instead.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
