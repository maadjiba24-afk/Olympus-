#!/usr/bin/env python3
"""Fail if the threat model and the live tool surface have drifted apart.

Every exposed tool (`tools.HANDLERS`) must have an entry in
`docs/THREAT_MODEL.md`, and the doc must not describe a tool that no longer
exists. CI runs this so a new capability can't ship un-threat-modeled.

Exit code 0 = consistent, 1 = drift (problems named on stderr).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from olympus import threatmodel  # noqa: E402


def main() -> int:
    problems = threatmodel.check_repo()
    if problems:
        print("✗ threat model is out of sync with the tool surface:",
              file=sys.stderr)
        for p in problems:
            print(f"    {p}", file=sys.stderr)
        return 1
    n = len(threatmodel.exposed_tools())
    print(f"✓ threat model covers all {n} exposed tools.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
