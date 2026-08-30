"""Per-conversation capability profiles: tool scoping + autonomy caps
layered on the security gate."""

import json

import pytest

from olympus import (actions, capprofile, config, gateway, memory, prefs,
                     specialists)


def _names(defs):
    return {d.get("name") for d in defs if isinstance(d, dict)}


def test_full_profile_changes_nothing():
    defs = [{"name": "send_email"}, {"name": "web_search"}]
    assert capprofile.filter_tools(defs, "cli-user") == defs


def test_reader_strips_acting_tools():
    capprofile.assign("tg-42", "reader")
    defs = [{"name": "send_email"}, {"name": "prepare_action"},
            {"name": "web_search"}, {"name": "recall_memory"},
            {"type": "code_execution_20250522"}]
    kept = capprofile.filter_tools(defs, "tg-42")
    assert _names(kept) == {"web_search", "recall_memory"}
    assert not any("code_execution" in str(d.get("type", "")) for d in kept)


def test_guest_also_strips_local_reads_and_memory_writes():
    capprofile.assign("tg-43", "guest")
    defs = [{"name": "read_file"}, {"name": "save_lesson"},
            {"name": "spawn_subagent"}, {"name": "web_search"}]
    assert _names(capprofile.filter_tools(defs, "tg-43")) == {"web_search"}


def test_allowlist_profile_is_a_whitelist(monkeypatch):
    path = config.MEMORY_DIR / "capability_profiles.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"kiosk": {"allow": ["web_search"], "max_autonomy": 0}}),
        encoding="utf-8")
    capprofile.assign("tg-44", "kiosk")
    defs = [{"name": "web_search"}, {"name": "recall_memory"}]
    assert _names(capprofile.filter_tools(defs, "tg-44")) == {"web_search"}


@pytest.mark.parametrize("replacement", [
    None,
    "{not valid json",
    "[]",
    json.dumps({"kiosk": {"deny": 5, "max_autonomy": 4}}),
    json.dumps({"kiosk": {"deny": [], "max_autonomy": "high"}}),
])
def test_assigned_custom_profile_loss_fails_closed(replacement):
    path = config.MEMORY_DIR / "capability_profiles.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"kiosk": {"allow": ["web_search"], "max_autonomy": 0}}),
        encoding="utf-8")
    capprofile.assign("tg-policy-loss", "kiosk")

    if replacement is None:
        path.unlink()
    else:
        path.write_text(replacement, encoding="utf-8")

    assert capprofile.of_user("tg-policy-loss") == "guest"
    assert capprofile.autonomy_cap("tg-policy-loss") == 0
    defs = [{"name": "send_email"}, {"name": "web_search"}]
    assert _names(capprofile.filter_tools(defs, "tg-policy-loss")) == {
        "web_search"}


@pytest.mark.parametrize("stored", ["missing", "", [], {}, 7])
def test_malformed_explicit_assignment_fails_closed(stored):
    prefs.set("local-restricted", "capability_profile", stored)
    assert capprofile.of_user("local-restricted") == "guest"
    assert capprofile.autonomy_cap("local-restricted") == 0


def test_invalid_channel_default_fails_closed(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CHANNEL_PROFILE", "missing-profile")
    assert capprofile.of_user("tg-invalid-default") == "guest"
    assert capprofile.autonomy_cap("tg-invalid-default") == 0
    # The channel default does not apply to a local owner.
    assert capprofile.of_user("local-owner") == "full"


def test_custom_file_cannot_shadow_builtin_guest():
    path = config.MEMORY_DIR / "capability_profiles.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "guest": {"deny": [], "allow": [], "max_autonomy": 4},
    }), encoding="utf-8")
    capprofile.assign("tg-shadow", "guest")

    assert capprofile.autonomy_cap("tg-shadow") == 0
    defs = [{"name": "send_email"}, {"name": "web_search"}]
    assert _names(capprofile.filter_tools(defs, "tg-shadow")) == {"web_search"}


def test_channel_default_profile_via_env(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CHANNEL_PROFILE", "reader")
    assert capprofile.of_user("tg-999") == "reader"      # channel uid
    assert capprofile.of_user("some-local-user") == "full"  # not a channel
    capprofile.assign("tg-999", "full")                  # explicit wins
    assert capprofile.of_user("tg-999") == "full"


def test_autonomy_is_capped_by_profile():
    actions.set_autonomy("tg-45", 4)
    assert actions.autonomy_level("tg-45") == 4
    capprofile.assign("tg-45", "guest")
    assert actions.autonomy_level("tg-45") == 0
    capprofile.clear("tg-45")
    assert actions.autonomy_level("tg-45") == 4


def test_unknown_profile_rejected():
    out = capprofile.assign("u1", "nonsense")
    assert "No profile" in out
    assert capprofile.of_user("u1") == "full"


def test_specialist_loadout_respects_active_conversation():
    plutus = specialists.SPECIALISTS["plutus"]
    memory.set_user("tg-46")
    try:
        capprofile.assign("tg-46", "guest")
        restricted = _names(plutus.tool_defs())
        capprofile.clear("tg-46")
        full = _names(plutus.tool_defs())
    finally:
        memory.set_user("shared")
    assert "save_lesson" in full
    assert "save_lesson" not in restricted
    assert restricted < full


def test_chat_profile_command_is_view_only():
    capprofile.assign(gateway.principal_id("u1"), "reader")
    out = gateway.reply_for({}, "u1", "/profile")
    joined = "\n".join(out)
    assert "reader" in joined and "autonomy cap: L1" in joined
