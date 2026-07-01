"""Self-update — `olympus upgrade` so users get new releases the easy way.

Olympus can be installed three ways (the one-line installer's venv, pipx, or a
plain pip install). This figures out which one is in effect and runs the right
upgrade command, so a user never has to remember the incantation — they just
type ``olympus upgrade``.

Resolution order:
  * pipx install   → ``pipx upgrade olympus-council``
  * git/venv install (the install.sh path) → pip ``--upgrade`` from the repo
  * plain pip      → ``pip install --upgrade olympus-council`` (from PyPI)

``--git`` forces the from-source upgrade (latest ``main``), useful before a
release is on PyPI.
"""

from __future__ import annotations

import os
import subprocess
import sys

REPO = "https://github.com/maadjiba24-afk/Olympus-"
PACKAGE = "olympus-council"


def _is_pipx() -> bool:
    """Heuristic: pipx installs live under a 'pipx' venvs directory."""
    prefix = sys.prefix.replace("\\", "/").lower()
    return "/pipx/venvs/" in prefix or bool(os.environ.get("PIPX_HOME"))


def _installed_from_git() -> bool:
    """True when the running package has no PyPI release metadata recorded as a
    normal wheel — i.e. it was installed from the git repo (the install.sh path).
    Best-effort; defaults to False so we prefer the PyPI upgrade."""
    try:
        from importlib.metadata import metadata
        m = metadata("olympus-council")
        # A VCS install records a direct_url; PyPI installs don't. We can't read
        # direct_url portably here, so treat a missing version as "from source".
        return not m.get("Version")
    except Exception:
        return True


def plan(force_git: bool = False) -> list[str]:
    """Return the argv of the upgrade command we would run (no side effects)."""
    if force_git:
        return [sys.executable, "-m", "pip", "install", "--upgrade",
                f"git+{REPO}"]
    if _is_pipx():
        return ["pipx", "upgrade", PACKAGE]
    if _installed_from_git():
        return [sys.executable, "-m", "pip", "install", "--upgrade",
                f"git+{REPO}"]
    return [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]


def run(force_git: bool = False) -> int:
    """Run the upgrade. Returns the subprocess exit code (0 = success)."""
    argv = plan(force_git)
    print("⚡ Upgrading Olympus:  " + " ".join(argv))
    try:
        return subprocess.call(argv)
    except FileNotFoundError as err:
        print(f"Could not run the upgrade ({err}). Try manually:\n  "
              + " ".join(argv))
        return 1
