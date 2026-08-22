"""The one pinned root (ADR 0005 / DEFERRED #8).

A workspace file/command action is PREVIEWED on the specialist worker thread but
EXECUTED later from the web/CLI approval handler. To guarantee it runs where it
was previewed, `prepare()` pins the workspace root it resolved at preview time
into the action payload (`_pinned_root`), and the executors confine to THAT root
rather than re-deriving `workdir()` from whatever context runs them. This is the
safe alternative to the rejected context-sensitive `workdir()`: the root travels
*with* the action.
"""
import json
from pathlib import Path

import pytest

from olympus import actions, builtin_actions, sandbox


@pytest.fixture(autouse=True)
def _builtins():
    builtin_actions.register_builtins()
    yield


# --- the pin is captured at prepare time --------------------------------

def test_prepare_pins_current_workdir():
    a = actions.prepare("u", "write_file", {"path": "note.txt", "content": "hi"})
    assert a.payload["_pinned_root"] == str(sandbox.workdir())


def test_only_workspace_actions_pin_root():
    reg = actions.registered()
    assert reg["write_file"].pins_root
    assert reg["run_command"].pins_root
    assert reg["edit_file"].pins_root
    # write_document uses the documents store (not the sandbox root) → no pin.
    assert not reg["write_document"].pins_root
    b = actions.prepare("u", "write_document", {"name": "d", "content": "c"})
    assert "_pinned_root" not in b.payload


# --- the core guarantee: preview root == execution root -----------------

def test_write_file_executes_in_pinned_root_after_workdir_changes(monkeypatch,
                                                                  tmp_path):
    # prepared on "the worker thread" — pins root A (the live workdir)
    a = actions.prepare("u", "write_file",
                        {"path": "out.txt", "content": "PINNED"})
    root_a = Path(a.payload["_pinned_root"])
    # the world changes before approval runs: workdir() now resolves elsewhere
    root_b = tmp_path / "other_workspace"
    root_b.mkdir()
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(root_b))
    assert sandbox.workdir() == root_b.resolve()          # ambient really moved
    # executed "from the approval handler"
    res = builtin_actions._write_file_execute(a.payload)
    # it landed where it was PREVIEWED (root A), not the new ambient root B
    assert (root_a / "out.txt").read_text() == "PINNED"
    assert not (root_b / "out.txt").exists()
    assert Path(res["path"]) == (root_a / "out.txt").resolve()


def test_run_command_executes_in_pinned_root_after_workdir_changes(monkeypatch,
                                                                   tmp_path):
    a = actions.prepare("u", "run_command",
                        {"command": "printf hi > marker.txt"})
    root_a = Path(a.payload["_pinned_root"])
    root_b = tmp_path / "other_ws"
    root_b.mkdir()
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(root_b))
    builtin_actions._run_command_execute(a.payload)
    assert (root_a / "marker.txt").is_file()              # ran in the pinned root
    assert not (root_b / "marker.txt").exists()


def test_edit_file_executes_in_pinned_root_after_workdir_changes(monkeypatch,
                                                                 tmp_path):
    root_a = sandbox.workdir()
    (root_a / "doc.txt").write_text("alpha beta")
    a = actions.prepare("u", "edit_file",
                        {"path": "doc.txt", "old_string": "beta",
                         "new_string": "gamma"})
    root_b = tmp_path / "elsewhere"
    root_b.mkdir()
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(root_b))
    builtin_actions._edit_file_execute(a.payload)
    assert (root_a / "doc.txt").read_text() == "alpha gamma"


# --- the pin can never be abused to escape or redirect ------------------

def test_tampered_pin_to_system_dir_falls_back_to_workdir():
    wd = sandbox.workdir()
    res = sandbox.write_file("t.txt", "x", root="/etc")   # system dir → refused
    assert Path(res["path"]) == (wd / "t.txt").resolve()  # fell back to workdir
    assert not Path("/etc/t.txt").exists()


def test_pin_to_filesystem_root_falls_back():
    wd = sandbox.workdir()
    res = sandbox.write_file("r.txt", "x", root="/")
    assert Path(res["path"]) == (wd / "r.txt").resolve()


def test_nonexistent_pin_falls_back_to_workdir(tmp_path):
    wd = sandbox.workdir()
    res = sandbox.write_file("n.txt", "x", root=str(tmp_path / "does_not_exist"))
    assert Path(res["path"]) == (wd / "n.txt").resolve()


def test_pinned_root_still_confines(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(ValueError):
        sandbox.write_file("../escape.txt", "x", root=str(root))


def test_pinned_root_is_not_user_editable():
    a = actions.prepare("u", "write_file", {"path": "f.txt", "content": "c"})
    orig = a.payload["_pinned_root"]
    actions.edit("u", a.id, {"_pinned_root": "/tmp/evil", "content": "c2"})
    a2 = actions.get("u", a.id)
    assert a2.payload["_pinned_root"] == orig      # internal field untouched
    assert a2.payload["content"] == "c2"           # normal field still editable


def test_initial_payload_cannot_choose_a_different_existing_root(monkeypatch,
                                                                  tmp_path):
    """A valid non-system directory is untrusted when the caller chose it."""
    trusted = tmp_path / "trusted"
    attacker = tmp_path / "attacker"
    trusted.mkdir()
    attacker.mkdir()
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(trusted))
    actions.grant_scope("u", "exec")

    a = actions.prepare(
        "u", "write_file",
        {"path": "outside.txt", "content": "must stay trusted",
         "_pinned_root": str(attacker)})
    assert a.payload["_pinned_root"] == str(trusted.resolve())

    done = actions.approve("u", a.id)
    assert done.status == actions.EXECUTED
    assert (trusted / "outside.txt").read_text() == "must stay trusted"
    assert not (attacker / "outside.txt").exists()


def test_root_capture_failure_blocks_preparation(monkeypatch):
    def broken_root():
        raise OSError("workspace unavailable")

    monkeypatch.setattr(sandbox, "current_root", broken_root)
    with pytest.raises(
            RuntimeError, match="could not capture the workspace root"):
        actions.prepare("u", "write_file", {"path": "x", "content": "y"})


def test_prepare_tool_reports_root_capture_failure(monkeypatch):
    from olympus import memory, tools

    def broken_root():
        raise OSError("workspace unavailable")

    monkeypatch.setattr(sandbox, "current_root", broken_root)
    memory.set_user("u")
    result = tools._prepare_action(
        "write_file", {"path": "x", "content": "y"})

    assert result == (
        "Error: could not capture the workspace root for this action.")
    assert actions.pending("u") == []


def test_legacy_pending_action_cannot_execute_a_stored_root(monkeypatch,
                                                            tmp_path):
    trusted = tmp_path / "trusted"
    attacker = tmp_path / "attacker"
    trusted.mkdir()
    attacker.mkdir()
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(trusted))
    actions.grant_scope("u", "exec")

    a = actions.prepare(
        "u", "write_file", {"path": "legacy.txt", "content": "blocked"})
    path = actions._path("u", a.id)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored.pop("boundary_version")  # exact shape of a pre-upgrade record
    stored["payload"]["_pinned_root"] = str(attacker)
    path.write_text(json.dumps(stored), encoding="utf-8")

    done = actions.approve("u", a.id)
    assert done.status == actions.FAILED
    assert "legacy trust-boundary format" in done.error
    assert not (trusted / "legacy.txt").exists()
    assert not (attacker / "legacy.txt").exists()
