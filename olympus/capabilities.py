"""The capability manifest — generated from code, the single source of truth.

Every published capability count (agents, tools, actions, commands) is derived
here by introspecting the live registries, never typed by hand. The manifest is
committed as `olympus/capabilities.json`, and prose in the README is *bound* to
it via `<!--cap:KEY-->N<!--/cap-->` markers. CI (`olympus capabilities --check`)
regenerates the manifest and fails if the committed file or any README number
drifts from the code — so a number can never silently go stale.

This is the lesson from Ruflo: it *generates* baselines yet its README prose
still contradicts itself (100 vs 98 vs 60 agents). Generating the manifest is
not enough; the prose must be bound to it. Olympus does both.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SCHEMA_VERSION = 1

# A README claim: <!--cap:agents-->12<!--/cap--> . The middle integer is the
# number the prose asserts; the key selects which manifest section it must match.
_CLAIM_RE = re.compile(r"<!--\s*cap:(\w+)\s*-->\s*(\d+)\s*<!--\s*/cap\s*-->")


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def manifest_path() -> Path:
    return _package_dir() / "capabilities.json"


def readme_path() -> Path:
    return _package_dir().parent / "README.md"


def _agents() -> dict:
    from .specialists import SPECIALISTS
    return {"count": len(SPECIALISTS), "names": sorted(SPECIALISTS)}


def _tools() -> dict:
    # HANDLERS is the canonical client-side tool surface; the server-side web
    # tools dispatch through the same names, so the keys are the whole set.
    from . import tools
    names = sorted(tools.HANDLERS)
    return {"count": len(names), "names": names}


def _actions() -> dict:
    # Importing these registers their ActionTypes on the spine, so the count is
    # canonical (order-independent of what a test happened to import).
    from . import actions, builtin_actions, computeruse  # noqa: F401
    reg = actions.registered()
    return {"count": len(reg), "names": sorted(reg)}


def _commands() -> dict:
    from . import cli
    names = cli.command_names()
    return {"count": len(names), "names": names}


def manifest() -> dict:
    """Build the manifest fresh from the live code. Deterministic (sorted, no
    timestamps/version) so the committed file only changes when capabilities do."""
    return {
        "schema_version": SCHEMA_VERSION,
        "agents": _agents(),
        "tools": _tools(),
        "actions": _actions(),
        "commands": _commands(),
    }


def to_json(m: dict | None = None) -> str:
    return json.dumps(m or manifest(), indent=2, sort_keys=True) + "\n"


def write(path: Path | None = None) -> Path:
    path = path or manifest_path()
    path.write_text(to_json(), encoding="utf-8")
    return path


def readme_claims(text: str) -> list[tuple[str, int]]:
    """Every bound numeric claim in README prose, as (key, number)."""
    return [(k, int(n)) for k, n in _CLAIM_RE.findall(text)]


def check(committed: str | None, readme_text: str, m: dict | None = None) -> list[str]:
    """Compare the committed manifest JSON and README claims to freshly
    generated truth. Returns a list of human-readable drift problems; an empty
    list means everything is consistent.

    - `committed`: the on-disk capabilities.json text (or None if missing).
    - `readme_text`: the README content to scan for `cap:` markers."""
    m = m or manifest()
    problems: list[str] = []

    fresh = to_json(m)
    if committed is None:
        problems.append("capabilities.json is missing — run "
                        "`olympus capabilities --write`.")
    elif committed != fresh:
        problems.append("capabilities.json is stale (differs from the code) — "
                        "run `olympus capabilities --write` and commit it.")

    claims = readme_claims(readme_text)
    if not claims:
        problems.append("README has no bound capability claims "
                        "(`<!--cap:KEY-->N<!--/cap-->`) — prose is not pinned "
                        "to the manifest.")
    for key, claimed in claims:
        if key not in m:
            problems.append(f"README claims cap:{key} but the manifest has no "
                            f"'{key}' section.")
            continue
        actual = m[key]["count"]
        if claimed != actual:
            problems.append(
                f"README says {claimed} {key}, but the code has {actual} "
                f"{key}. Update the README number (or the code).")
    return problems


def check_repo() -> list[str]:
    """Run `check` against the files on disk (capabilities.json + README.md)."""
    mp, rp = manifest_path(), readme_path()
    committed = mp.read_text(encoding="utf-8") if mp.exists() else None
    readme_text = rp.read_text(encoding="utf-8") if rp.exists() else ""
    return check(committed, readme_text)
