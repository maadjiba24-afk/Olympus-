"""Self-update planning and the version command."""

import json
import sys

from olympus import selfupdate


class _Distribution:
    def __init__(self, direct_url):
        self.direct_url = direct_url

    def read_text(self, name: str):
        assert name == "direct_url.json"
        return self.direct_url


def _direct_url(monkeypatch, value):
    monkeypatch.setattr(
        "importlib.metadata.distribution",
        lambda name: _Distribution(value),
    )


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


def test_git_detection_requires_affirmative_pep610_evidence(monkeypatch):
    _direct_url(
        monkeypatch,
        json.dumps({
            "url": "https://github.com/maadjiba24-afk/Olympus-",
            "vcs_info": {"vcs": "Git", "commit_id": "a" * 40},
        }),
    )

    assert selfupdate._installed_from_git() is True


def test_wheel_and_local_directory_default_to_release(monkeypatch):
    for value in (
        None,
        json.dumps({
            "url": "file:///workspace/Olympus-",
            "dir_info": {"editable": True},
        }),
    ):
        _direct_url(monkeypatch, value)
        assert selfupdate._installed_from_git() is False


def test_git_detection_rejects_malformed_metadata(monkeypatch):
    values = (
        "{",
        json.dumps([]),
        json.dumps({"vcs_info": "git"}),
        json.dumps({"vcs_info": {"vcs": "git"}}),
        json.dumps({"vcs_info": {"vcs": "git", "commit_id": None}}),
        json.dumps({"vcs_info": {"vcs": "git", "commit_id": False}}),
        json.dumps({"vcs_info": {"vcs": "git", "commit_id": 7}}),
    )
    for value in values:
        _direct_url(monkeypatch, value)
        assert selfupdate._installed_from_git() is False


def test_metadata_failure_keeps_implicit_upgrade_on_pypi(monkeypatch):
    def unavailable(_name):
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr("importlib.metadata.distribution", unavailable)
    monkeypatch.setattr(selfupdate, "_is_pipx", lambda: False)

    assert selfupdate._installed_from_git() is False
    assert selfupdate.plan() == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        selfupdate.PACKAGE,
    ]


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
