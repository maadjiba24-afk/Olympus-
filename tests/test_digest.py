"""The 'what Olympus learned on its own' readout — CLI `learned`, `/learned`,
and the `recent_learning` tool. Read-only over heartbeat state + memory.
"""

import time

import pytest

from olympus import cli, config, digest, memory, tui, tools
from olympus.specialists import SPECIALISTS


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    memory.set_user("shared")
    yield


def test_ago_buckets():
    now = 1_000_000.0
    assert digest._ago(None, now) == "not yet"
    assert digest._ago(now - 10, now) == "just now"
    assert digest._ago(now - 600, now) == "10m ago"
    assert digest._ago(now - 7200, now) == "2h ago"
    assert digest._ago(now - 2 * 86400, now) == "2d ago"


def test_empty_state_explains_how_to_start():
    out = digest.learned_recently()
    assert "hasn't run yet" in out and "heartbeat" in out
    assert "0 learned skill" in out


def test_summarizes_cycles_and_recent_memory(monkeypatch):
    now = time.time()
    monkeypatch.setattr(memory, "load_state", lambda: {
        "opportunity_scan": now - 3600,      # 1h ago
        "evolution_audit": now - 3 * 86400,  # 3d ago
    })
    memory.save("reports", "Opportunity scan: AI dev tools", "…body…")
    memory.save("lessons", "Lesson: verify before claiming", "…")
    out = digest.learned_recently(now=now)
    assert "World scan for opportunities (Argus): last ran 1h ago" in out
    assert "Self-audit & self-upgrade (Prometheus): last ran 3d ago" in out
    assert "AI dev tools" in out               # recent report title surfaced
    assert "verify before claiming" in out     # recent lesson title surfaced


def test_disabled_cycle_shows_disabled_not_not_yet(monkeypatch):
    now = time.time()
    monkeypatch.setattr(config, "TRAIN_EVERY", 0)     # training turned off
    monkeypatch.setattr(memory, "load_state", lambda: {"opportunity_scan": now})
    out = digest.learned_recently(now=now)
    assert "Specialist training: disabled" in out
    assert "Specialist training: last ran not yet" not in out


# --- surfaces: CLI command, TUI command, agent tool ----------------------

def test_recent_titles_preserve_leading_hash():
    # Regression: lstrip("# ") mangled titles like "#1 priority" -> "1 priority".
    memory.save("reports", "#1 priority: ship it", "body")
    memory.save("reports", "Normal Title", "body")
    titles = memory.recent_titles("reports", 5)
    assert "#1 priority: ship it" in titles and "Normal Title" in titles


def test_learned_is_a_cli_command():
    assert "learned" in cli.command_names()


def test_tui_slash_learned(monkeypatch):
    assert "/learned" in tui.COMMANDS
    handled, output, exit_ = tui.dispatch_command(object(), "/learned")
    assert handled and not exit_ and "Olympus" in output


def test_recent_learning_tool():
    out = tools._recent_learning()
    assert "Olympus" in out and "Skill library" in out


def test_metis_has_recent_learning():
    names = {d.get("name") for d in SPECIALISTS["metis"].tool_defs("anthropic")}
    assert "recent_learning" in names


def test_recent_learning_threat_modeled():
    from olympus import threatmodel
    documented = threatmodel.documented_tools(
        threatmodel.doc_path().read_text(encoding="utf-8"))
    assert "recent_learning" in tools.HANDLERS and "recent_learning" in documented
    assert threatmodel.check_repo() == []
