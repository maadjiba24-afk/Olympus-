"""Supply-chain integrity (Task 4 of the moat plan).

What these prove:
1. The pre-release detector agrees with `packaging` (the canonical PEP 440
   implementation) and the regex fallback catches the same cases.
2. The guard PASSES on the real, committed lockfile and FAILS on an injected
   pre-release pin (alpha/dev) — the supply-chain ban actually bites.
3. `requirements.lock` exists, is hash-pinned, and covers the runtime deps.
"""

import importlib.util
import os
import subprocess
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


def _run_cli_with_cp1252(lock: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_no_prerelease.py"),
         str(lock)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="cp1252",
        errors="strict",
        check=False,
    )


def _assert_ascii_output(result: subprocess.CompletedProcess[str]) -> None:
    (result.stdout + result.stderr).encode("ascii")


def test_cli_cp1252_clean_lock_is_ascii_safe(tmp_path):
    lock = tmp_path / "clean-cp1252.lock"
    lock.write_text("anthropic==0.111.0\ncryptography==49.0.0\n",
                    encoding="utf-8")

    result = _run_cli_with_cp1252(lock)

    assert result.returncode == 0, result.stderr
    assert "[OK]" in result.stdout
    assert result.stderr == ""
    _assert_ascii_output(result)


def test_cli_cp1252_prerelease_lock_is_ascii_safe(tmp_path):
    lock = tmp_path / "bad-cp1252.lock"
    lock.write_text("anthropic==0.111.0\nshady==9.9.9rc1\n",
                    encoding="utf-8")

    result = _run_cli_with_cp1252(lock)

    assert result.returncode == 1
    assert "[FAIL]" in result.stderr
    assert "shady==9.9.9rc1" in result.stderr
    _assert_ascii_output(result)


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


# --- 4. the shipped release-signing pinned key is well-formed -------------

def test_shipped_witness_pubkey_is_valid_hex():
    """The committed pinned key must be a 64-char Ed25519 public key (hex), and
    must be packaged so it ships in the wheel (verify trust-pins to it)."""
    f = ROOT / "olympus" / "witness_pubkey.txt"
    assert f.is_file(), "olympus/witness_pubkey.txt must be committed"
    keys = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    assert len(keys) == 1, "exactly one pinned key expected"
    key = keys[0].lower()
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    # packaged into the wheel
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "witness_pubkey.txt" in pyproject
