"""W1-1b: the remaining durable publish sites.

Same contract and same tracing helpers as `test_durable_fsync.py`, applied to
the 19 sites W1-1 did not cover. Split into a second file only because the two
PRs are reviewed separately; the shape is identical.

Two directions are asserted here, and the split is the point of W1-1b:

* **Group C (16 sites)** — fsync before replace, like every W1-1 site.
* **Group B (3 sites)** — atomic publish and NO fsync, because each sits on a
  per-model-call / per-tool-call locked read-modify-write. W1-1 shipped a
  per-call fsync on exactly that shape (`usage.record`) and broke the
  observability wall-clock contract on windows-py3.12. These tests are the
  inverse guard: they fail if someone later "fixes" the missing fsync.

Group A — the five `os.replace(existing, dest)` quarantine/restore moves — is
deliberately untested here because it was deliberately unconverted: those move
a file that already exists and have no data to publish.
"""

from __future__ import annotations

import json
import os

import pytest

from olympus import (agentbeat, backup, connectors, ctxbudget, ctxheat, facts,
                     modelgate, modelgrade, pairing, pluginstore, scheduler,
                     skills, toolcall_repair, watchdog, webmonitor,
                     webproposals, webreflect)
from test_durable_fsync import assert_data_synced_before_replace, trace


def assert_published_without_fsync(events: list, *, label: str) -> None:
    """The Group B contract: atomic, and deliberately NOT durable.

    Atomicity is still required — a reader in another process must never see a
    torn file. Only the sync is given up, and only where the write sits on a
    per-call locked path.
    """
    kinds = [e[0] for e in events]
    assert "replace" in kinds, f"{label}: never published atomically"
    first_replace = kinds.index("replace")
    synced = [e for e in events[:first_replace] if e[0] == "fsync"]
    assert not synced, (
        f"{label}: fsynced before replace, but this site is on a per-call "
        f"locked read-modify-write and must NOT sync — that is the W1-1a "
        f"regression. events={events}")


# =========================================================================
# Group B — hot paths. Atomic, no fsync, no knob.
# =========================================================================

def test_ctxbudget_save_does_not_fsync(monkeypatch):
    """`observe()` reaches `_save` on EVERY model call (llm.py:389/483,
    openai_compat.py:187) under a lock. It is the `ctx` component of the very
    benchmark W1-1 broke."""
    events = trace(monkeypatch)
    ctxbudget._save({"ratios": {}})
    assert_published_without_fsync(events, label="ctxbudget._save")


def test_toolcall_repair_record_does_not_fsync(monkeypatch):
    """Runs per repaired tool call inside `proclock.lock("repair-stats")`.
    `usage.py` groups this with `ctxbudget.observe` as one contract."""
    events = trace(monkeypatch)
    toolcall_repair.record_repair("anthropic", "claude-opus-5", "web_fetch",
                                  1, "recovered")
    assert_published_without_fsync(events, label="record_repair")


def test_ctxheat_write_does_not_fsync(monkeypatch, tmp_path):
    """Reached from `record()` -> `_apply()` (locked RMW), and
    `recall._heat_record` calls that once per RETRIEVED MEMORY on the per-turn
    `recall.context_block` path — more writes per turn than `usage.record`."""
    events = trace(monkeypatch)
    ctxheat._atomic_write_json(tmp_path / "heat.json", {"a": 1})
    assert_published_without_fsync(events, label="ctxheat._atomic_write_json")


def test_group_b_still_publishes_atomically(monkeypatch, tmp_path):
    """Giving up the sync must not give up atomicity: the file still lands
    whole, via a replace, never a partial in-place write."""
    target = tmp_path / "heat.json"
    ctxheat._atomic_write_json(target, {"k": "v"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"k": "v"}
    ctxbudget._save({"ratios": {"x": 1}})
    assert json.loads(ctxbudget._cal_path().read_text(encoding="utf-8")) \
        == {"ratios": {"x": 1}}


# =========================================================================
# Group C — everything else. fsync before replace.
# =========================================================================

def test_agentbeat_save_syncs(monkeypatch):
    """Per agent-beat mutation (add/prune/tick) — heartbeat cadence."""
    events = trace(monkeypatch)
    agentbeat._save([])
    assert_data_synced_before_replace(events, label="agentbeat._save")


def test_connectors_write_syncs(monkeypatch, tmp_path):
    """Per operator action (`add_mcp_server`)."""
    events = trace(monkeypatch)
    connectors._atomic_write(tmp_path / "connectors.json", '{"servers": []}')
    assert_data_synced_before_replace(events, label="connectors._atomic_write")


def test_facts_trim_syncs(monkeypatch):
    """Amortised compaction: fires only when the append log passes
    2 x MAX_FACTS (5000), i.e. ~1 write per 5000 appends."""
    path = facts._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps({"claim": str(i), "verdict": "y", "source": "",
                    "ts": 0}) + "\n" for i in range(5)), encoding="utf-8")
    events = trace(monkeypatch)
    facts._trim(path)
    assert_data_synced_before_replace(events, label="facts._trim")


def test_modelgate_write_baseline_syncs(monkeypatch):
    """Per gate run (heartbeat cadence / operator)."""
    events = trace(monkeypatch)
    modelgate._write_baseline("anthropic/claude-opus-5", {"score": 8.0})
    assert_data_synced_before_replace(events, label="modelgate._write_baseline")


def test_modelgate_write_freeze_syncs(monkeypatch):
    """A FREEZE is a safety control: a lost write silently un-freezes a member,
    exactly like `identity.revoke`. Unconditional fsync."""
    events = trace(monkeypatch)
    modelgate._write_freeze("aletheia", "high", "drift detected", "ref-1")
    assert_data_synced_before_replace(events, label="modelgate._write_freeze")


def test_modelgate_unfreeze_syncs(monkeypatch):
    """Operator action; the same safety-control argument in reverse."""
    modelgate._write_freeze("aletheia", "high", "drift", "ref-1")
    events = trace(monkeypatch)
    assert modelgate.unfreeze("aletheia") is True
    assert_data_synced_before_replace(events, label="modelgate.unfreeze")


def test_modelgrade_rebuild_syncs(monkeypatch):
    """Only on a missing / stale-schema / corrupt cards.json — a cache repair,
    not a steady-state write."""
    monkeypatch.setenv("OLYMPUS_MODELGRADE", "on")
    events = trace(monkeypatch)
    modelgrade.rebuild()
    assert_data_synced_before_replace(events, label="modelgrade.rebuild")


def test_pairing_save_syncs(monkeypatch):
    """Per pairing action (issue / claim / expire) — operator-paced."""
    events = trace(monkeypatch)
    pairing._save({"codes": {}})
    assert_data_synced_before_replace(events, label="pairing._save")


def test_pluginstore_save_manifest_syncs(monkeypatch):
    """Per plugin install / uninstall."""
    events = trace(monkeypatch)
    pluginstore._save_manifest({"plugins": {}})
    assert_data_synced_before_replace(events, label="pluginstore._save_manifest")


def test_scheduler_save_syncs(monkeypatch):
    """Per scheduled-job mutation (add / remove / run)."""
    events = trace(monkeypatch)
    scheduler._save([])
    assert_data_synced_before_replace(events, label="scheduler._save")


def test_skills_save_emb_cache_syncs(monkeypatch):
    """Only on an embedding cache MISS — a skill's text or the embedding model
    changed. A warm cache returns before this write."""
    events = trace(monkeypatch)
    skills._save_emb_cache({"slug": {"hash": "h", "model": "m", "vec": [0.1]}})
    assert_data_synced_before_replace(events, label="skills._save_emb_cache")


def test_webmonitor_save_syncs(monkeypatch):
    """Per monitor mutation — operator-paced."""
    events = trace(monkeypatch)
    webmonitor._save([])
    assert_data_synced_before_replace(events, label="webmonitor._save")


def test_webproposals_save_syncs(monkeypatch):
    """Per discovery cycle (`draft_from_discoveries`) — heartbeat cadence."""
    events = trace(monkeypatch)
    webproposals._save({})
    assert_data_synced_before_replace(events, label="webproposals._save")


def test_webreflect_save_state_syncs(monkeypatch):
    """Per reflection cycle (`run_due`) — heartbeat cadence."""
    events = trace(monkeypatch)
    webreflect._save_state({"seen": [], "last_run": 0.0})
    assert_data_synced_before_replace(events, label="webreflect._save_state")


def test_watchdog_write_forensics_syncs(monkeypatch):
    """Per watchdog anomaly (observe / cancel). Rare by construction, and the
    one write whose whole purpose is surviving whatever just went wrong."""
    state = watchdog.LeaseState(lease_id="L1", now=100.0, created=0.0)
    verdict = watchdog.Verdict(kind="stall", reason="no beat for 412s",
                               operator_action="check the worker")
    events = trace(monkeypatch)
    written = watchdog.write_forensics(state, verdict, outcome="observed")
    assert written is not None, "forensics did not write"
    assert_data_synced_before_replace(events, label="watchdog.write_forensics")


def test_backup_create_syncs(monkeypatch):
    """Per backup — operator action or scheduled cadence."""
    events = trace(monkeypatch)
    res = backup.create()
    assert res.get("ok") or res.get("path"), f"backup did not publish: {res}"
    assert_data_synced_before_replace(events, label="backup.create")


# =========================================================================
# Group A — deliberately unconverted. Pinned so the exemption stays visible.
# =========================================================================

GROUP_A = [
    ("olympus/ctxbudget.py", "_quarantine a corrupt calibration file"),
    ("olympus/ctxheat.py", "_quarantine a corrupt heat ledger"),
    ("olympus/modelgrade.py", "quarantine corrupt promotion evidence"),
    ("olympus/toolcall_repair.py", "move corrupt repair telemetry aside"),
    ("olympus/backup.py", "restore: commit verified staged files into place"),
]


def test_group_a_sites_remain_plain_os_replace():
    """These are `os.replace(existing, dest)` — a MOVE of a file that already
    exists, with no data to write. `atomicio.publish(tmp, path, data)` has no
    meaning for them and would corrupt them, so they keep the bare call.

    Pinned as a test so the exemption is a recorded decision rather than an
    oversight the next sweep silently "fixes".
    """
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    for rel, _why in GROUP_A:
        text = (repo / rel).read_text(encoding="utf-8")
        assert "os.replace(" in text, (
            f"{rel}: expected a surviving bare os.replace (Group A move)")
