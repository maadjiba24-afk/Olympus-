"""OLYMPUS — a self-improving, self-recurring multi-agent AI system.

Pipeline:  Zeus (main agent) -> Athena (supervisor) -> Aletheia (hallucination
controller) -> specialists.  Persistent memory and a heartbeat loop make the
system self-sufficient: it scans the world for opportunities, learns from
YouTube videos, and audits/upgrades itself.
"""

# The installed distribution's version is the single source of truth (it comes
# from pyproject.toml at build time). Fall back to a literal for source checkouts
# that were never installed.
try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        __version__ = _pkg_version("olympus-council")
    except PackageNotFoundError:
        __version__ = "0.24.0"
except Exception:  # pragma: no cover - importlib.metadata always present on 3.10+
    __version__ = "0.24.0"
