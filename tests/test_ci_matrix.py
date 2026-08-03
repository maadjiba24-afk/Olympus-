"""CI must actually TEST every Python version the package claims to support.

The 2026-07-15 truth-state audit flagged that CI ran a single Python (3.12)
while pyproject declared 3.10–3.13 — a coverage gap where a 3.10-only or
3.13-only regression would ship undetected. These tests pin the fix: the
`test` job's version matrix must cover exactly the range pyproject advertises,
so the two can never silently drift apart again.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_PYPROJECT = _ROOT / "pyproject.toml"


def _declared_versions() -> set[str]:
    """The minor versions pyproject advertises via its classifiers."""
    text = _PYPROJECT.read_text(encoding="utf-8")
    return set(re.findall(r"Programming Language :: Python :: (3\.\d+)", text))


def _ci_matrix_versions() -> set[str]:
    """The versions the CI `test` job's matrix runs."""
    text = _CI.read_text(encoding="utf-8")
    m = re.search(r"python-version:\s*\[([^\]]*)\]", text)
    assert m, "ci.yml test job has no `python-version: [...]` matrix"
    return set(re.findall(r"3\.\d+", m.group(1)))


def test_ci_matrix_covers_every_declared_version():
    declared = _declared_versions()
    assert declared, "pyproject declares no Python classifiers"
    matrix = _ci_matrix_versions()
    missing = declared - matrix
    assert not missing, f"CI does not test declared Python version(s): {missing}"


def test_ci_matrix_has_no_undeclared_versions():
    # The matrix shouldn't test a version the package doesn't claim to support.
    extra = _ci_matrix_versions() - _declared_versions()
    assert not extra, f"CI tests undeclared Python version(s): {extra}"


def test_requires_python_floor_is_in_the_matrix():
    text = _PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'requires-python\s*=\s*"[>=]*\s*(3\.\d+)"', text)
    assert m, "pyproject has no requires-python floor"
    floor = m.group(1)
    assert floor in _ci_matrix_versions(), (
        f"requires-python floor {floor} is not exercised by CI")


def test_ci_still_hash_pins_dependencies():
    # The matrix must not have traded supply-chain integrity for convenience:
    # every leg still installs from the hash-locked file.
    text = _CI.read_text(encoding="utf-8")
    assert "--require-hashes -r requirements.lock" in text, (
        "CI must install with --require-hashes (no unpinned installs)")



def test_ci_runs_real_search_provider_probe():
    """CI must exercise at least the keyless provider against the real network."""
    text = _CI.read_text(encoding="utf-8")

    match = re.search(
        r"(?ms)^  search-live:\s*\n"
        r"(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\s*\n|\Z)",
        text,
    )
    assert match, "ci.yml has no dedicated `search-live` job"

    body = match.group("body")
    assert 'OLYMPUS_SEARCH_LIVE: "1"' in body, (
        "search-live must explicitly enable real provider requests")
    assert "tests/test_websearch_live.py" in body, (
        "search-live must run the live provider end-to-end tests")
    assert "pytest" in body, (
        "search-live must execute the test through pytest")



def test_ci_runs_self_hosted_searxng_probe():
    """CI must verify Olympus against a real, locally hosted SearXNG server."""
    text = _CI.read_text(encoding="utf-8")

    match = re.search(
        r"(?ms)^  search-live:\s*\n"
        r"(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\s*\n|\Z)",
        text,
    )
    assert match, "ci.yml has no dedicated `search-live` job"

    body = match.group("body")
    assert "searxng/searxng:" in body, (
        "search-live must launch the official SearXNG container")
    assert "OLYMPUS_SEARXNG_URL: http://127.0.0.1:8888" in body, (
        "search-live must point Olympus at the local SearXNG instance")
    assert ".github/searxng/settings.yml" in body, (
        "search-live must mount the committed SearXNG configuration")
    assert "format=json" in body and "curl" in body, (
        "search-live must wait for the JSON search API before testing")

    settings_path = _ROOT / ".github" / "searxng" / "settings.yml"
    assert settings_path.exists(), (
        "the SearXNG CI settings file is missing")

    settings = settings_path.read_text(encoding="utf-8")
    assert "use_default_settings: true" in settings
    assert "formats:" in settings
    assert re.search(r"(?m)^\s*-\s*json\s*$", settings), (
        "SearXNG JSON output must be explicitly enabled")
