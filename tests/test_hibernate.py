"""Serverless hibernation: next-due computation and single-tick run."""

from olympus import config, hibernate, scheduler


def test_next_due_in_uses_cadences(monkeypatch):
    # only one cadence enabled, everything else disabled, to make math clean
    monkeypatch.setattr(config, "OPPORTUNITY_SCAN_EVERY", 3600)
    monkeypatch.setattr(config, "WATCHLIST_EVERY", 0)
    monkeypatch.setattr(config, "MAINTENANCE_EVERY", 0)
    monkeypatch.setattr(config, "DAILY_LEARNING_EVERY", 0)
    monkeypatch.setattr(config, "TRAIN_EVERY", 0)
    monkeypatch.setattr(config, "EVOLUTION_AUDIT_EVERY", 0)
    monkeypatch.setattr(config, "REPLAY_GATE_EVERY", 0)
    monkeypatch.setattr(config, "BACKUP_EVERY", 0)
    state = {"opportunity_scan": 1000.0}
    wait = hibernate.next_due_in(state, now=1000.0 + 600)   # 600s elapsed
    assert abs(wait - 3000) < 2                              # 3600 - 600


def test_next_due_in_accounts_for_scheduler(monkeypatch):
    for attr in ("OPPORTUNITY_SCAN_EVERY", "WATCHLIST_EVERY", "MAINTENANCE_EVERY",
                 "DAILY_LEARNING_EVERY", "TRAIN_EVERY", "EVOLUTION_AUDIT_EVERY",
                 "REPLAY_GATE_EVERY", "BACKUP_EVERY"):
        monkeypatch.setattr(config, attr, 0)
    scheduler.add("soon", "every 30m", "do x", now=0.0)
    wait = hibernate.next_due_in({}, now=0.0)
    assert abs(wait - 1800) < 2


def test_due_now_is_zero(monkeypatch):
    monkeypatch.setattr(config, "OPPORTUNITY_SCAN_EVERY", 3600)
    for attr in ("WATCHLIST_EVERY", "MAINTENANCE_EVERY", "DAILY_LEARNING_EVERY",
                 "TRAIN_EVERY", "EVOLUTION_AUDIT_EVERY", "REPLAY_GATE_EVERY",
                 "BACKUP_EVERY"):
        monkeypatch.setattr(config, attr, 0)
    # never ran → due immediately
    assert hibernate.next_due_in({}, now=99999.0) == 0.0


def test_run_once_returns_log_and_next_wake(monkeypatch):
    from olympus import heartbeat
    monkeypatch.setattr(heartbeat, "tick", lambda state, now=None: ["did work"])
    monkeypatch.setattr(hibernate, "next_due_in", lambda state=None, now=None: 1234.5)
    result = hibernate.run_once()
    assert result["log"] == ["did work"]
    assert result["next_wake_secs"] == 1234.5
