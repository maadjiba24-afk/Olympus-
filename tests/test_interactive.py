"""The interactive layer that makes the operator feel smooth for non-engineers:
out-of-band secure credential capture, plain-English approval, browser
auto-launch helpers, and the advanced-mode toggle. See docs/DESIGN_OPERATOR_UX.md.
"""

import pytest

from olympus import (actions, approvals, browser, config, interactive, memory,
                     operator, securecapture, tools)
from olympus.specialists import SPECIALISTS


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    memory.set_user("shared")
    yield
    actions._REGISTRY.pop("_t_demo", None)    # don't pollute the capability count


class FakeIO:
    def __init__(self, lines=None, secrets=None):
        self.lines = list(lines or [])
        self.secrets = list(secrets or [])
        self.outs = []

    def line(self, prompt):
        return self.lines.pop(0) if self.lines else ""

    def secret(self, prompt):
        return self.secrets.pop(0) if self.secrets else ""

    def out(self, text):
        self.outs.append(text)


# --- secure credential capture (password never near the model) ------------

def test_securecapture_request_pending_clear():
    securecapture.request("shared", "Bank.com")
    assert securecapture.pending("shared") == "bank.com"
    securecapture.clear("shared")
    assert securecapture.pending("shared") is None


def test_after_turn_captures_credentials_out_of_band(monkeypatch):
    from olympus import vault
    stored = {}
    monkeypatch.setattr(vault, "available", lambda: True)
    monkeypatch.setattr(vault, "put",
                        lambda u, n, v: stored.__setitem__((u, n), v))
    securecapture.request("shared", "bank.com")
    io = FakeIO(lines=["alice"], secrets=["s3cret"])
    events = interactive.after_turn("shared", io)
    assert "saved-login:bank.com" in events
    assert stored[("shared", "site:bank.com")] == {"username": "alice",
                                                    "password": "s3cret"}
    assert securecapture.pending("shared") is None            # cleared
    assert operator.login_mode("shared", "bank.com") == "remember"
    # the password is never echoed back into the visible output
    assert all("s3cret" not in o for o in io.outs)


def test_after_turn_skips_capture_when_blank(monkeypatch):
    securecapture.request("shared", "bank.com")
    events = interactive.after_turn("shared", FakeIO(lines=[""], secrets=[""]))
    assert events == [] and securecapture.pending("shared") is None


# --- plain-English approval ----------------------------------------------

def _demo_action():
    actions.register(actions.ActionType(
        name="_t_demo", risk_class=actions.IRREVERSIBLE, scope="",
        preview=lambda p: "About to do the thing", execute=lambda p: {"ok": 1}))


def test_approvals_lists_only_prepared():
    _demo_action()
    a = actions.prepare("shared", "_t_demo", {})
    assert [x.id for x in approvals.pending("shared")] == [a.id]


def test_after_turn_yes_approves_and_executes():
    _demo_action()
    a = actions.prepare("shared", "_t_demo", {})
    events = interactive.after_turn("shared", FakeIO(lines=["yes"]))
    assert f"approved:{a.id}" in events
    assert actions.get("shared", a.id).status == actions.EXECUTED
    assert approvals.pending("shared") == []


def test_after_turn_no_declines():
    _demo_action()
    a = actions.prepare("shared", "_t_demo", {})
    events = interactive.after_turn("shared", FakeIO(lines=["nah"]))
    assert f"declined:{a.id}" in events
    assert actions.get("shared", a.id).status == actions.REJECTED


def test_is_yes():
    assert interactive.is_yes("yes") and interactive.is_yes(" Sure ")
    assert not interactive.is_yes("no") and not interactive.is_yes("")


# --- browser auto-launch helpers (pure parts) ----------------------------

def test_chrome_args_headed_vs_headless():
    headed = browser._chrome_args("/bin/chrome", 9222, "/tmp/p", headless=False)
    assert "--remote-debugging-port=9222" in headed
    assert "--remote-allow-origins=*" in headed
    assert "--user-data-dir=/tmp/p" in headed
    assert "--headless=new" not in headed                     # visible for sign-in
    assert "--headless=new" in browser._chrome_args(
        "/bin/chrome", 9222, "/tmp/p", headless=True)


def test_find_chrome_uses_bundled(monkeypatch, tmp_path):
    import shutil
    import sys

    monkeypatch.delenv("OLYMPUS_BROWSER_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda n: None)
    monkeypatch.setattr(sys, "platform", "linux")

    fake = tmp_path / "chromium-9/chrome-linux/chrome"
    fake.parent.mkdir(parents=True)
    fake.write_text("")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))

    assert browser._find_chrome() == str(fake)


def test_find_chrome_uses_windows_edge(monkeypatch, tmp_path):
    import ntpath
    import os
    import shutil
    import sys

    monkeypatch.delenv("OLYMPUS_BROWSER_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "missing"))
    monkeypatch.setenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Test\AppData\Local")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os.path, "join", ntpath.join)

    expected = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    monkeypatch.setattr(os.path, "isfile", lambda path: path == expected)

    assert browser._find_chrome() == expected


def test_autolaunch_off_by_default(monkeypatch):
    monkeypatch.delenv("OLYMPUS_BROWSER_AUTOLAUNCH", raising=False)
    assert browser._autolaunch_enabled() is False


# --- the new tools --------------------------------------------------------

def test_remember_login_tool_sets_pending_not_password():
    out = tools._operator_remember_login("Shop.com")
    assert "private prompt" in out and "vault" in out
    assert securecapture.pending("shared") == "shop.com"
    assert operator.login_mode("shared", "shop.com") == "remember"


def test_set_advanced_mode_toggles():
    assert "on" in tools._set_advanced_mode(True).lower()
    assert operator.advanced("shared") is True
    assert "off" in tools._set_advanced_mode(False).lower()
    assert operator.advanced("shared") is False


def test_new_tools_threat_modeled():
    from olympus import threatmodel
    documented = threatmodel.documented_tools(
        threatmodel.doc_path().read_text(encoding="utf-8"))
    for name in ("operator_remember_login", "set_advanced_mode"):
        assert name in tools.HANDLERS and name in documented
    assert threatmodel.check_repo() == []


def test_hermes_has_interactive_tools():
    names = {d.get("name") for d in SPECIALISTS["hermes"].tool_defs("anthropic")}
    assert {"operator_remember_login", "set_advanced_mode"} <= names


# --- @file / @url context injection (Hermes v0.4) --------------------------

def test_expand_file_reference(tmp_path, monkeypatch):
    from olympus import tui
    f = tmp_path / "report.md"
    f.write_text("Q3 revenue was up 12%", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out, notes = tui.expand_references("summarize @report.md please")
    assert "summarize report.md please" in out
    assert "[context from file report.md]" in out
    assert "Q3 revenue was up 12%" in out
    assert notes and "injected report.md" in notes[0]


def test_expand_url_reference_wraps_untrusted():
    from olympus import tui
    out, notes = tui.expand_references(
        "what does @https://example.com/page say?",
        fetch=lambda url: "PAGE BODY")
    assert "PAGE BODY" in out
    assert "untrusted" in out.lower()      # wrapped as untrusted content
    assert "[context from https://example.com/page]" in out


def test_non_file_handles_pass_through():
    from olympus import tui
    out, notes = tui.expand_references("ping @teknium1 about this")
    assert out == "ping @teknium1 about this"
    assert notes == []


def test_failed_fetch_is_reported_not_fatal():
    from olympus import tui
    def boom(url):
        raise ValueError("blocked: internal host")
    out, notes = tui.expand_references("check @https://169.254.169.254/x",
                                       fetch=boom)
    assert "@https://169.254.169.254/x" in out    # token left in place
    assert notes and "could not fetch" in notes[0]


def test_email_addresses_are_not_references():
    from olympus import tui
    out, notes = tui.expand_references("mail bob@example.com the doc")
    assert out == "mail bob@example.com the doc"
    assert notes == []
