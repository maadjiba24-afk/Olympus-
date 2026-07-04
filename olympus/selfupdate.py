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


def pending_work() -> dict:
    """A snapshot of in-flight work that a restart/upgrade would interrupt:
    chat requests in the gateway journal and scheduled jobs mid-run. Purely
    informational — each subsystem already knows how to resume its own."""
    snapshot: dict = {"inflight": [], "jobs_running": []}
    try:
        import json as _json
        from . import config
        d = config.MEMORY_DIR / "inflight"
        if d.exists():
            for path in d.glob("*.json"):
                try:
                    entry = _json.loads(path.read_text(encoding="utf-8"))
                    snapshot["inflight"].append(str(entry.get("uid", path.stem)))
                except Exception:
                    snapshot["inflight"].append(path.stem)
    except Exception:
        pass
    try:
        from . import scheduler
        snapshot["jobs_running"] = [
            j.name for j in scheduler.jobs()
            if j.enabled and j.started_at > j.last_run]
    except Exception:
        pass
    return snapshot


def write_handoff() -> dict:
    """Record the durable handoff before an upgrade replaces the code, so the
    next process can tell the user what carried across and resume it."""
    import json as _json
    import time as _time
    from . import __version__, config
    handoff = {"ts": _time.time(), "from_version": __version__,
               "pending": pending_work()}
    try:
        path = config.MEMORY_DIR / "handoff.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(handoff, indent=1), encoding="utf-8")
    except OSError:
        pass
    return handoff


def take_handoff() -> dict | None:
    """Consume the handoff record left by the pre-upgrade process (None when
    there wasn't one). Called once by the heartbeat on its first tick."""
    import json as _json
    from . import config
    path = config.MEMORY_DIR / "handoff.json"
    if not path.exists():
        return None
    try:
        handoff = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        handoff = None
    path.unlink(missing_ok=True)
    return handoff


def run(force_git: bool = False) -> int:
    """Run the upgrade. Returns the subprocess exit code (0 = success)."""
    argv = plan(force_git)
    handoff = write_handoff()
    pending = handoff["pending"]
    carried = len(pending["inflight"]) + len(pending["jobs_running"])
    if carried:
        print(f"⚡ {carried} in-flight task(s) journaled — they resume after "
              "the upgrade.")
    print("⚡ Upgrading Olympus:  " + " ".join(argv))
    try:
        return subprocess.call(argv)
    except FileNotFoundError as err:
        print(f"Could not run the upgrade ({err}). Try manually:\n  "
              + " ".join(argv))
        return 1
