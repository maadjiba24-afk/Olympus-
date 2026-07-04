"""Per-turn dynamic tool selection: big loadouts get trimmed to what the task
needs; security-stripped tools can never come back (selection only drops);
essentials and small loadouts are untouched; results are deterministic.
"""

import pytest

from olympus import specialists, toolselect, tools


def _def(name, desc=""):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": {}}}


def _big_loadout():
    """A loadout whose droppable tail exceeds the default cap of 16."""
    defs = [_def(n, d) for n, d in [
        ("send_email", "send an email message"),
        ("read_inbox", "list email messages in the inbox"),
        ("call_webhook", "post a payload to a webhook"),
        ("generate_image", "generate an image from a prompt"),
        ("text_to_speech", "synthesize spoken audio"),
        ("transcribe_audio", "transcribe an audio recording"),
        ("browse_page", "fetch and read a web page"),
        ("analyze_image", "describe an image"),
        ("browser_open", "open a page in the browser harness"),
        ("browser_read", "read the current browser page"),
        ("browser_act", "click or type in the browser"),
        ("site_profiles", "list recorded site profiles"),
        ("watch_youtube", "fetch a youtube transcript"),
        ("schedule_task", "schedule a recurring task"),
        ("spawn_subagent", "delegate a task to a specialist"),
        ("read_file", "read a file from the workspace"),
        ("grep_files", "search file contents by regex"),
        ("glob_files", "list files matching a pattern"),
        ("edit_file", "edit a file by string replacement"),
        ("list_dir", "list a workspace directory"),
    ]]
    defs.append(_def("recall_memory", "search saved memory"))   # essential
    defs.append(_def("prepare_action", "prepare a gated action"))
    defs.append({"type": "web_search_20250305", "name": "web_search"})
    return defs


def test_small_loadout_is_untouched():
    defs = [_def("a"), _def("b"), _def("c")]
    assert toolselect.select("anything at all", defs) == defs


def test_selection_is_a_subset_preserving_order():
    defs = _big_loadout()
    out = toolselect.select("fix the bug in the email sender", defs)
    assert len(out) < len(defs)
    ids = [id(d) for d in defs]
    assert [id(d) for d in out] == sorted((id(d) for d in out),
                                          key=ids.index)
    assert all(any(d is e for e in defs) for d in out)


def test_relevant_tools_survive_irrelevant_drop():
    defs = _big_loadout()
    out = toolselect.select(
        "search the workspace files for the failing regex and edit the file",
        defs)
    names = {d.get("name") for d in out}
    assert {"grep_files", "edit_file", "read_file"} <= names


def test_essentials_and_server_side_always_kept(monkeypatch):
    monkeypatch.setenv("OLYMPUS_TOOL_SELECT_MAX", "2")   # brutal cap
    out = toolselect.select("write a poem", _big_loadout())
    names = {d.get("name") for d in out}
    assert {"recall_memory", "prepare_action", "web_search"} <= names


def test_deterministic():
    defs = _big_loadout()
    a = toolselect.select("summarize my email inbox", defs)
    b = toolselect.select("summarize my email inbox", defs)
    assert [d.get("name") for d in a] == [d.get("name") for d in b]


def test_env_kill_switch(monkeypatch):
    monkeypatch.setenv("OLYMPUS_TOOL_SELECT", "0")
    defs = _big_loadout()
    assert toolselect.select("email", defs) == defs


def test_cap_env_is_respected(monkeypatch):
    monkeypatch.setenv("OLYMPUS_TOOL_SELECT_MAX", "5")
    out = toolselect.select("summarize my email inbox", _big_loadout())
    droppable = [d for d in out if not toolselect._protected(d)]
    assert len(droppable) == 5


# --- integration with the specialist registry -------------------------------

def test_tool_defs_with_task_is_subset_of_full():
    spec = specialists.SPECIALISTS["hephaestus"]
    full = {d.get("name") for d in spec.tool_defs("anthropic")}
    trimmed = {d.get("name")
               for d in spec.tool_defs("anthropic", task="hi")}
    assert trimmed <= full


def test_tool_defs_without_task_unchanged():
    spec = specialists.SPECIALISTS["hephaestus"]
    assert spec.tool_defs("anthropic") == spec.tool_defs("anthropic")


def test_selection_cannot_readd_stripped_tools(monkeypatch):
    """browser_act is stripped from Argus (ingesting) by capability
    separation; a task screaming for it must not bring it back."""
    spec = specialists.SPECIALISTS["argus"]
    out = spec.tool_defs("anthropic",
                         task="browser act: click and type on the page")
    assert "browser_act" not in {d.get("name") for d in out}


def test_base_tools_survive_for_every_specialist():
    base = {d["name"] for d in tools.BASE_TOOLS}
    for spec in specialists.SPECIALISTS.values():
        out = spec.tool_defs("anthropic", task="completely unrelated words")
        assert base <= {d.get("name") for d in out}, spec.key
