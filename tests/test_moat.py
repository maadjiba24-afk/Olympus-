"""Moat status board — the unified view of the self-evolving capabilities."""

import json

from olympus import cli, evolve, moat, store


def test_status_covers_every_capability():
    caps = moat.status()["capabilities"]
    assert set(caps) == {"vector_recall", "swarm", "consensus",
                         "bandit_routing", "file_agents", "federation"}
    for c in caps.values():
        assert "enabled" in c and "flag" in c


def test_render_is_readable():
    out = moat.render()
    assert "Olympus moat" in out
    for label in ("vector recall", "swarm", "consensus", "bandit routing",
                  "file agents", "federation"):
        assert label in out


def test_reflects_enabled_flags(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CONSENSUS", "1")
    monkeypatch.setenv("OLYMPUS_SWARM", "1")
    caps = moat.status()["capabilities"]
    assert caps["consensus"]["enabled"] is True
    assert caps["swarm"]["enabled"] is True
    assert caps["federation"]["enabled"] is False


def test_shows_self_tuned_consensus_panel(monkeypatch):
    monkeypatch.delenv("OLYMPUS_CONSENSUS_VERIFIERS", raising=False)
    store.reset()
    evolve._set_param("consensus", "verifiers", 5)
    caps = moat.status()["capabilities"]
    assert caps["consensus"]["verifiers"] == 5
    assert caps["consensus"]["tuned"].get("current") == 5
    store.reset()


def test_health_flag_no_data_vs_ok():
    assert "no data" in moat._health_flag({"samples": 0})
    assert "ok" in moat._health_flag({"samples": 10, "ok_rate": 0.9})
    assert "DEGRADED" in moat._health_flag({"samples": 10, "ok_rate": 0.4})


# --- CLI ------------------------------------------------------------------

def test_moat_is_a_registered_command():
    assert "moat" in cli.command_names()


def test_cli_moat_prints_board(capsys):
    rc = cli.main(["moat"])
    out = capsys.readouterr().out
    assert (rc or 0) == 0
    assert "Olympus moat" in out and "consensus" in out


def test_cli_moat_json(capsys):
    rc = cli.main(["moat", "--json"])
    out = capsys.readouterr().out
    assert (rc or 0) == 0
    data = json.loads(out)
    assert "capabilities" in data and "consensus" in data["capabilities"]
