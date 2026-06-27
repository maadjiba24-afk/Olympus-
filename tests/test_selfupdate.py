"""Self-update planning and the version command."""

import sys

from olympus import selfupdate


def test_plan_force_git():
    argv = selfupdate.plan(force_git=True)
    assert argv[:4] == [sys.executable, "-m", "pip", "install"]
    assert any(a.startswith("git+") for a in argv)


def test_plan_pipx(monkeypatch):
    monkeypatch.setattr(selfupdate, "_is_pipx", lambda: True)
    assert selfupdate.plan() == ["pipx", "upgrade", "olympus-council"]


def test_plan_pypi(monkeypatch):
    monkeypatch.setattr(selfupdate, "_is_pipx", lambda: False)
    monkeypatch.setattr(selfupdate, "_installed_from_git", lambda: False)
    argv = selfupdate.plan()
    assert argv[-1] == "olympus-council" and "--upgrade" in argv


def test_plan_git_install(monkeypatch):
    monkeypatch.setattr(selfupdate, "_is_pipx", lambda: False)
    monkeypatch.setattr(selfupdate, "_installed_from_git", lambda: True)
    assert any(a.startswith("git+") for a in selfupdate.plan())


def test_run_invokes_subprocess(monkeypatch):
    called = {}
    monkeypatch.setattr(selfupdate, "plan", lambda force_git=False: ["echo", "hi"])

    def fake_call(argv):
        called["argv"] = argv
        return 0

    monkeypatch.setattr(selfupdate.subprocess, "call", fake_call)
    assert selfupdate.run() == 0
    assert called["argv"] == ["echo", "hi"]


def test_version_is_a_string():
    from olympus import __version__
    assert isinstance(__version__, str) and __version__[0].isdigit()
