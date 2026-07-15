"""Milestone 0.3 — untrusted-content envelope is fail-closed.

`security.should_wrap` now WRAPS by default; only an explicitly trusted source
skips — a first-party builtin on TRUSTED_TOOLS, or a registered connector ACTION
plugin. The point of the inversion: a new ingesting tool that nobody classified
still arrives wrapped, instead of silently bypassing the envelope.
"""

import pytest

from olympus import connectors, security, tools


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    # Isolate the global plugin registry so test registrations don't leak into
    # other modules' capability-separation / loadout assertions.
    monkeypatch.setattr(connectors, "_PLUGINS", {})
    monkeypatch.setattr(connectors, "_PLUGINS_LOADED", True)


def test_unregistered_tool_wraps_by_default():
    # The bypass this milestone closes: a brand-new, unclassified tool name.
    assert security.should_wrap("brand_new_ingesting_tool_nobody_registered") is True
    assert security.should_wrap("") is True


def test_new_ingesting_builtin_not_on_allowlist_wraps():
    # Simulate a builtin someone added but forgot to classify: not in
    # TRUSTED_TOOLS and not an action plugin → wrapped.
    name = "read_pastebin"                                  # hypothetical new ingester
    assert name not in security.TRUSTED_TOOLS
    assert name not in connectors.plugin_action_names()
    assert security.should_wrap(name) is True


def test_trusted_builtins_skip_the_envelope():
    for name in ("current_time", "recall_memory", "read_skill", "browser_act",
                 "browser_exists", "operator_status", "query_codegraph"):
        assert security.should_wrap(name) is False


def test_ingestion_builtins_wrap():
    for name in ("web_search", "web_fetch", "browser_open", "browser_read",
                 "browser_screenshot", "read_email", "analyze_image",
                 "trigger_research"):
        assert security.should_wrap(name) is True


def test_registered_action_plugin_skips_data_plugin_wraps():
    @connectors.plugin("m03_read_feed", "read a feed", action=False)
    def _f() -> str:
        return "x"

    @connectors.plugin("m03_do_thing", "do a thing", action=True)
    def _a() -> str:
        return "done"

    assert security.should_wrap("m03_read_feed") is True    # data → wrapped
    assert security.should_wrap("m03_do_thing") is False    # action → not wrapped


def test_classification_is_complete_and_disjoint():
    # Every current builtin tool is classified exactly once: trusted OR ingestion,
    # never both, never neither. A newly-added builtin left unclassified fails
    # this test loudly (and, until fixed, wraps at runtime — fail-closed).
    handlers = set(tools.HANDLERS)
    trusted = set(security.TRUSTED_TOOLS)
    ingestion = set(security.INGESTION_TOOLS)
    assert trusted.isdisjoint(ingestion), trusted & ingestion
    unclassified = handlers - trusted - ingestion
    assert not unclassified, f"unclassified builtins (would wrap): {sorted(unclassified)}"
    # TRUSTED_TOOLS must not name a tool that no longer exists.
    stale = trusted - handlers
    assert not stale, f"TRUSTED_TOOLS names non-existent tools: {sorted(stale)}"
