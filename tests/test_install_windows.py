"""The Windows installer ships and exposes the documented one-liner + steps."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PS1 = ROOT / "install.ps1"


def test_install_ps1_exists():
    assert PS1.is_file(), "install.ps1 must exist for the Windows one-liner"


def test_install_ps1_core_steps():
    text = PS1.read_text(encoding="utf-8")
    assert "install.ps1 | iex" in text            # the advertised one-liner
    assert "venv" in text                          # private environment
    assert "git+" in text                          # installs the package
    assert "olympus.cmd" in text                   # PATH shim
    assert "3.10" in text                          # version guard


def test_readme_documents_windows_install():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "install.ps1" in readme
