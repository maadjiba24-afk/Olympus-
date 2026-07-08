"""Phase 1 (Hermes) — built-in profile catalog + the operator review surface
(history / status). These are the usability layer over the already-governed
operator: no live browser or credentials involved.
"""

import json
import types

import pytest

from olympus import browser, config, operator


# --- built-in profile catalog ----------------------------------------------

@pytest.fixture()
def seeded(monkeypatch, tmp_path):
    d = tmp_path / "profiles"
    d.mkdir()
    (d / "acme.json").write_text(json.dumps({
        "domain": "acme.com",
        "login_url": "https://acme.com/login",
        "username_selector": "#u", "password_selector": "#p",
        "submit_selector": "#go", "success_selector": "#home",
        "templates": {"reorder": {"risk": "irreversible",
                                  "steps": [{"op": "click", "selector": "#b"}],
                                  "success_selector": "#done"}},
    }))
    monkeypatch.setattr(browser, "_builtin_profiles_dir", lambda: d)
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")   # user store
    return d


def test_builtin_profiles_load_and_are_tagged(seeded):
    profs = browser.builtin_profiles()
    assert len(profs) == 1
    assert profs[0].domain == "acme.com" and profs[0].source == "builtin"
    assert "reorder" in profs[0].templates


def test_builtin_profile_is_visible_via_get_and_list(seeded):
    assert browser.get_profile("acme.com").domain == "acme.com"
    assert any(p.domain == "acme.com" for p in browser.list_profiles())


def test_user_recording_overrides_builtin(seeded):
    # a user records their own acme.com profile — it must shadow the seed
    browser.record_profile("acme.com", login_url="https://acme.com/other",
                           source="agent")
    p = browser.get_profile("acme.com")
    assert p.source == "agent" and p.login_url == "https://acme.com/other"
    # only one acme.com entry in the merged list (no duplicate)
    assert sum(1 for x in browser.list_profiles() if x.domain == "acme.com") == 1


def test_malformed_seed_is_skipped(monkeypatch, tmp_path):
    d = tmp_path / "profiles"
    d.mkdir()
    (d / "bad.json").write_text("{ not json")
    (d / "ok.json").write_text(json.dumps({"domain": "ok.com"}))
    monkeypatch.setattr(browser, "_builtin_profiles_dir", lambda: d)
    domains = {p.domain for p in browser.builtin_profiles()}
    assert domains == {"ok.com"}


def test_no_profiles_dir_is_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "_builtin_profiles_dir",
                        lambda: tmp_path / "does-not-exist")
    assert browser.builtin_profiles() == []


# --- authorize / forget / status (prefs-backed) ----------------------------

@pytest.fixture()
def user(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    return "optest"


def test_authorize_and_status(user):
    assert not operator.enabled(user)
    operator.authorize_site(user, "Amazon.com", "manual")
    assert operator.enabled(user)
    assert "amazon.com" in operator.sites(user)
    s = operator.status_summary(user)
    assert "ON" in s and "amazon.com" in s and "manual" in s


def test_forget_site(user):
    operator.authorize_site(user, "acme.com", "manual")
    assert operator.forget_site(user, "acme.com") is True
    assert "acme.com" not in operator.sites(user)


def test_status_when_off(user):
    s = operator.status_summary(user)
    assert "off" in s and "none yet" in s


# --- history / review surface ----------------------------------------------

def _action(atype, status, domain, template, created_at):
    return types.SimpleNamespace(
        type=atype, status=status, created_at=created_at,
        payload={"domain": domain, "template": template})


def test_history_filters_to_operator_actions(user, monkeypatch):
    records = [
        _action("browser_operate", "executed", "acme.com", "reorder", 100),
        _action("send_email", "executed", None, None, 90),          # not operator
        _action("browser_operate_financial", "prepared", "bank.com", "pay", 80),
    ]
    monkeypatch.setattr(operator.actions, "history", lambda u, limit=0: records)
    h = operator.history(user)
    assert [a.type for a in h] == ["browser_operate", "browser_operate_financial"]


def test_render_history_is_plain_english(user, monkeypatch):
    records = [_action("browser_operate", "executed", "acme.com", "reorder", 100)]
    monkeypatch.setattr(operator.actions, "history", lambda u, limit=0: records)
    out = operator.render_history(user)
    assert "acme.com" in out and "reorder" in out and "executed" in out


def test_render_history_empty(user, monkeypatch):
    monkeypatch.setattr(operator.actions, "history", lambda u, limit=0: [])
    assert operator.render_history(user) == "No operator actions yet."


def test_status_surfaces_pending_operator_approvals(user, monkeypatch):
    monkeypatch.setattr(operator.actions, "history", lambda u, limit=0: [])
    monkeypatch.setattr(operator.actions, "pending", lambda u: [
        _action("browser_operate_irreversible", "prepared", "x.com", "t", 1)])
    operator.authorize_site(user, "x.com", "manual")
    s = operator.status_summary(user)
    assert "Awaiting your approval: 1" in s


# --- olympus operator CLI group --------------------------------------------

def _run_cli(monkeypatch, tmp_path, *argv):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    from olympus import cli
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(list(argv))
    return rc, buf.getvalue()


def test_cli_operator_authorize_status_forget(monkeypatch, tmp_path):
    rc, out = _run_cli(monkeypatch, tmp_path, "operator", "authorize", "Acme.com")
    assert rc in (0, None) and "acme.com" in out and "manual" in out
    _, out = _run_cli(monkeypatch, tmp_path, "operator", "status")
    assert "ON" in out and "acme.com" in out
    _, out = _run_cli(monkeypatch, tmp_path, "operator", "forget", "acme.com")
    assert "Forgot acme.com" in out


def test_cli_operator_enable_disable(monkeypatch, tmp_path):
    _, out = _run_cli(monkeypatch, tmp_path, "operator", "enable")
    assert "enabled" in out.lower()
    _, out = _run_cli(monkeypatch, tmp_path, "operator", "status")
    assert "ON" in out
    _, out = _run_cli(monkeypatch, tmp_path, "operator", "disable")
    assert "disabled" in out.lower()


def test_cli_operator_authorize_requires_domain(monkeypatch, tmp_path):
    rc, out = _run_cli(monkeypatch, tmp_path, "operator", "authorize")
    assert rc == 1 and "Usage" in out


def test_cli_operator_default_is_status(monkeypatch, tmp_path):
    _, out = _run_cli(monkeypatch, tmp_path, "operator")
    assert "Operator:" in out


# --- chat-facing review tools ----------------------------------------------

def test_operator_history_tool_registered_and_on_hermes():
    from olympus import tools, specialists
    assert "operator_history" in tools.EXTRA_TOOLS
    assert "operator_history" in tools.HANDLERS
    assert "operator_history" in specialists.SPECIALISTS["hermes"].extra_tools


def test_operator_status_tool_uses_rich_summary(user, monkeypatch):
    from olympus import tools, memory
    monkeypatch.setattr(memory, "current_user", lambda: user)
    monkeypatch.setattr(operator.actions, "history", lambda u, limit=0: [])
    operator.authorize_site(user, "acme.com", "manual")
    out = tools.HANDLERS["operator_status"]()
    assert "Operator: ON" in out and "acme.com" in out


def test_operator_history_tool_clamps_and_renders(user, monkeypatch):
    from olympus import tools, memory
    monkeypatch.setattr(memory, "current_user", lambda: user)
    rec = [_action("browser_operate", "executed", "acme.com", "reorder", 100)]
    monkeypatch.setattr(operator.actions, "history", lambda u, limit=0: rec)
    assert "acme.com" in tools.HANDLERS["operator_history"](999)
