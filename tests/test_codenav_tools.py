"""Code-navigation tools (grep/glob/ranged read) and the edit_file action.

The read-side tools are free (side-effect-free, path-confined, deny-listed);
the write side (edit_file) rides the approval spine with a unified diff as its
preview. The sensitive-file deny-list is matched case-insensitively — the
case-sensitive version of this list was a real vulnerability upstream
(Odysseus #5097).
"""

import pytest

from olympus import actions, builtin_actions, sandbox, specialists, tools  # noqa: F401


@pytest.fixture()
def ws(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(root))
    root.mkdir()
    (root / "app.py").write_text("def main():\n    return 1\n# marker\n")
    (root / "lib").mkdir()
    (root / "lib" / "util.py").write_text("VALUE = 42\n")
    (root / "notes.md").write_text("marker in docs\n")
    return root


# --- sensitive-path deny-list ---------------------------------------------

@pytest.mark.parametrize("name", [
    ".env", ".ENV", ".env.local", "server.pem", "SERVER.PEM", "id_rsa",
    "ID_RSA.bak", "credentials.json", ".netrc", "secrets.yaml", "api.key",
])
def test_sensitive_names_are_denied(name):
    assert sandbox.is_sensitive_path(name)


@pytest.mark.parametrize("part", [".ssh", ".SSH", ".aws", ".gnupg"])
def test_sensitive_dirs_are_denied_anywhere_in_path(part):
    assert sandbox.is_sensitive_path(f"home/{part}/config")


def test_ordinary_files_are_not_denied():
    assert not sandbox.is_sensitive_path("src/environment.py")
    assert not sandbox.is_sensitive_path("keyboard.rs")


def test_read_file_refuses_credential_file(ws):
    (ws / ".env").write_text("TOKEN=x\n")
    assert sandbox.read_file(".env").startswith("Error")


# --- ranged read -----------------------------------------------------------

def test_read_file_line_range(ws):
    assert sandbox.read_file("app.py", 2, 2) == "    return 1\n"
    # open-ended range reads to EOF
    assert sandbox.read_file("app.py", 3) == "# marker\n"


def test_read_file_range_past_eof_is_an_error(ws):
    assert sandbox.read_file("app.py", 99).startswith("Error")


def test_read_file_whole_file_unchanged(ws):
    assert sandbox.read_file("app.py").startswith("def main")


# --- grep / glob -----------------------------------------------------------

def test_grep_finds_matches_with_location(ws):
    out = sandbox.grep_files("marker")
    assert "app.py:3" in out and "notes.md:1" in out


def test_grep_glob_filter(ws):
    out = sandbox.grep_files("marker", glob="*.py")
    assert "app.py" in out and "notes.md" not in out


def test_grep_skips_sensitive_and_binary(ws):
    (ws / ".env").write_text("marker TOKEN\n")
    (ws / "blob.bin").write_bytes(b"marker\x00\x01")
    out = sandbox.grep_files("marker")
    assert ".env" not in out and "blob.bin" not in out


def test_grep_bad_regex_is_reported(ws):
    assert sandbox.grep_files("[unclosed").startswith("Error")


def test_grep_confined(ws):
    assert sandbox.grep_files("x", path="../..").startswith("Error")


def test_glob_matches_nested(ws):
    out = sandbox.glob_files("**/*.py")
    assert "app.py" in out and "lib/util.py" in out


def test_glob_skips_sensitive(ws):
    (ws / "id_rsa").write_text("key\n")
    assert "id_rsa" not in sandbox.glob_files("*")


# --- edit_file: executor, diff preview, action wiring ----------------------

def test_edit_file_applies_and_undoes(ws):
    res = sandbox.edit_file("app.py", "return 1", "return 2")
    assert (ws / "app.py").read_text().count("return 2") == 1
    assert res["replacements"] == 1
    sandbox.undo_write(res)
    assert "return 1" in (ws / "app.py").read_text()


def test_edit_file_requires_unique_match(ws):
    (ws / "app.py").write_text("x = 1\nx = 1\n")
    with pytest.raises(ValueError, match="occurs 2 times"):
        sandbox.edit_file("app.py", "x = 1", "x = 2")
    res = sandbox.edit_file("app.py", "x = 1", "x = 2", replace_all=True)
    assert res["replacements"] == 2
    assert (ws / "app.py").read_text() == "x = 2\nx = 2\n"


def test_edit_file_refuses_missing_old_string(ws):
    with pytest.raises(ValueError, match="not found"):
        sandbox.edit_file("app.py", "nope", "x")


def test_edit_file_refuses_credential_file(ws):
    (ws / ".env").write_text("TOKEN=a\n")
    with pytest.raises(ValueError, match="credential"):
        sandbox.edit_file(".env", "TOKEN=a", "TOKEN=b")


def test_edit_file_diff_is_a_unified_diff(ws):
    diff = sandbox.edit_file_diff("app.py", "return 1", "return 2")
    assert diff.startswith("--- a/app.py")
    assert "-    return 1" in diff and "+    return 2" in diff
    # and the file was NOT touched by the preview
    assert "return 1" in (ws / "app.py").read_text()


def test_edit_file_diff_reports_unappliable(ws):
    assert "cannot apply" in sandbox.edit_file_diff("app.py", "nope", "x")


def test_edit_file_action_registered_with_diff_preview(ws):
    at = actions.registered()["edit_file"]
    assert at.risk_class == actions.NOTABLE and at.reversible
    preview = at.preview({"path": "app.py", "old_string": "return 1",
                          "new_string": "return 2"})
    assert "+    return 2" in preview


# --- exposure --------------------------------------------------------------

def test_tools_are_registered():
    for name in ("grep_files", "glob_files", "edit_file"):
        assert name in tools.EXTRA_TOOLS and name in tools.HANDLERS


def test_hephaestus_loadout_gains_code_nav():
    extra = specialists.SPECIALISTS["hephaestus"].extra_tools
    for name in ("grep_files", "glob_files", "edit_file"):
        assert name in extra


def test_edit_file_tool_routes_through_spine(ws, monkeypatch):
    """The free tool must PREPARE an action, never write directly."""
    captured = {}

    def fake_spine(type_name, payload, title, noun):
        captured["type"] = type_name
        captured["payload"] = payload
        return "prepared"

    monkeypatch.setattr(tools, "_spine_action", fake_spine)
    out = tools.HANDLERS["edit_file"]("app.py", "return 1", "return 2")
    assert out == "prepared"
    assert captured["type"] == "edit_file"
    assert captured["payload"]["old_string"] == "return 1"
    assert "return 1" in (ws / "app.py").read_text()   # untouched
