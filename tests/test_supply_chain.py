"""Supply-chain integrity (Task 4 of the moat plan).

What these prove:
1. The pre-release detector agrees with `packaging` (the canonical PEP 440
   implementation) and the regex fallback catches the same cases.
2. The guard PASSES on the real, committed lockfile and FAILS on an injected
   pre-release pin (alpha/dev) — the supply-chain ban actually bites.
3. `requirements.lock` exists, is hash-pinned, and covers the runtime deps.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Load the standalone script as a module (it lives in scripts/, not the package).
_spec = importlib.util.spec_from_file_location(
    "check_no_prerelease", ROOT / "scripts" / "check_no_prerelease.py")
cnp = importlib.util.module_from_spec(_spec)
sys.modules["check_no_prerelease"] = cnp
_spec.loader.exec_module(cnp)


# --- 1. the detector is correct ------------------------------------------

@pytest.mark.parametrize("version,expected", [
    ("1.0.0", False),
    ("2026.6.17", False),
    ("0.92.0", False),
    ("1.0.post1", False),       # post-release is NOT a pre-release
    ("1.0a1", True),
    ("1.0.0b2", True),
    ("1.0rc1", True),
    ("1.0c1", True),            # PEP 440 'c' == 'rc'
    ("2.1.dev3", True),
    ("1.0a1.dev2", True),
])
def test_is_prerelease(version, expected):
    assert cnp.is_prerelease(version) is expected


def test_detector_agrees_with_packaging():
    from packaging.version import Version
    for v in ["1.0.0", "2.3.4", "1.0a1", "1.0b2", "1.0rc1", "2.1.dev3",
              "1.0.post1", "2026.6.17", "0.111.0"]:
        assert cnp.is_prerelease(v) == (Version(v).is_prerelease
                                        or Version(v).is_devrelease)


# --- 2. the guard bites --------------------------------------------------

def test_scan_finds_injected_prereleases():
    text = ("anthropic==0.111.0 \\\n    --hash=sha256:abc\n"
            "evil-pkg==1.0.0a1\n"
            "another-bad==2.3.dev5\n")
    offenders = cnp.scan(text)
    assert ("evil-pkg", "1.0.0a1") in offenders
    assert ("another-bad", "2.3.dev5") in offenders
    assert all(name != "anthropic" for name, _ in offenders)


def test_main_passes_on_clean_lock(tmp_path, capsys):
    lock = tmp_path / "clean.lock"
    lock.write_text("anthropic==0.111.0\ncryptography==49.0.0\n")
    assert cnp.main([str(lock)]) == 0


def test_main_fails_on_prerelease_lock(tmp_path, capsys):
    lock = tmp_path / "bad.lock"
    lock.write_text("anthropic==0.111.0\nshady==9.9.9rc1\n")
    assert cnp.main([str(lock)]) == 1
    err = capsys.readouterr().err
    assert "shady==9.9.9rc1" in err


def test_main_errors_when_lock_missing(tmp_path):
    assert cnp.main([str(tmp_path / "nope.lock")]) == 2


# --- 3. the real lockfile is sound ---------------------------------------

def test_real_lock_has_no_prereleases():
    lock = ROOT / "requirements.lock"
    assert lock.exists(), "requirements.lock must be committed"
    assert cnp.scan(lock.read_text(encoding="utf-8")) == []


def test_real_lock_is_hash_pinned_and_covers_runtime_deps():
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert "--hash=sha256:" in text
    for dep in ("anthropic==", "cryptography==", "youtube-transcript-api=="):
        assert dep in text
    # the transitive deps that make the crypto backend actually load
    assert "cffi==" in text and "pycparser==" in text
