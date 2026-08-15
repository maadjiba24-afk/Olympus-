"""W1-2: retention and journal compaction actually run in production.

Both routines existed, were tested, and had NO production caller — so
`OLYMPUS_CONVERSATION_RETAIN_DAYS` enforced nothing and journals grew to the
ceiling, at which point appends *and crash recovery* stop for that
conversation.

The dangerous half is that either can destroy user data if wired carelessly, so
these tests drive the real `heartbeat.tick` rather than the routines directly.
A test that mocks the sweep and asserts it was called proves nothing about the
two defaults that matter (`retain_days=None`, `dry_run=True`).
"""

from __future__ import annotations

import json
import time

import pytest

from olympus import config, heartbeat, memory, retention, sessionlog


# --- helpers ---------------------------------------------------------------

def _old_conversation(cid: str, *, age_days: float) -> "object":
    """A conversation snapshot on disk with an mtime `age_days` in the past."""
    import os
    memory.save_conversation(cid, [{"role": "user", "content": f"hi {cid}"}])
    path = memory._conversation_path(cid)
    old = time.time() - age_days * 86400
    os.utime(path, (old, old))
    return path


def _tick(now: float = 10_000_000.0):
    """Run one real heartbeat tick with the maintenance cadence due.

    `state={}` means every cadence last ran at 0.0, and `now` well past
    `MAINTENANCE_EVERY` (86400) makes maintenance due. Note that forcing it by
    setting the cadence to 0 would do the OPPOSITE: `_due` treats a
    non-positive interval as "this cycle is OFF", so the job would never run
    and every assertion below would pass vacuously.

    LLM-dependent cadences self-skip on a keyless test install
    (`firstrun.configured()` is False), which is why other suites drive `tick`
    the same way."""
    return heartbeat.tick({}, now=now)


# =========================================================================
# ACCEPTANCE 1 — policy UNSET deletes nothing. The most important test here.
# =========================================================================

def test_unset_policy_deletes_nothing_through_the_heartbeat(monkeypatch):
    """TRAP 1. `config.RETAIN_DAYS` (30, always set) sits in scope right above
    the call and looks like the obvious argument. Passing it would invent a
    30-day conversation-deletion policy on every deployment that never chose
    one. Unset must remain a no-op — with real files on disk, checked."""
    monkeypatch.delenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", raising=False)
    paths = [_old_conversation(f"old-{i}", age_days=900) for i in range(3)]
    assert all(p.exists() for p in paths)
    assert retention.conversation_retain_days() is None, "precondition: unset"

    _tick()

    still = [p for p in paths if p.exists()]
    assert len(still) == 3, (
        "an UNSET conversation-retention policy deleted user conversations — "
        f"{3 - len(still)} of 3 gone. retention.py refuses to guess a policy; "
        "the scheduler must not become that guess.")


def test_unset_policy_reports_the_deployment_block(monkeypatch):
    """The no-op is a reported refusal, not silence."""
    monkeypatch.delenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", raising=False)
    res = retention.sweep_conversations(dry_run=False)
    assert res["removed"] == 0
    assert "OLYMPUS_CONVERSATION_RETAIN_DAYS is unset" in res["skipped_reason"]


# =========================================================================
# ACCEPTANCE 2 — policy SET actually deletes, through the heartbeat.
# =========================================================================

def test_set_policy_deletes_through_the_heartbeat(monkeypatch):
    """TRAP 2. `dry_run` defaults to True, so a scheduler that omits it runs
    forever, logs plausible output and deletes nothing. Files must be present
    before and GONE after — driven through `tick`, not by calling the sweep."""
    monkeypatch.setenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", "30")
    monkeypatch.delenv("OLYMPUS_LEGAL_HOLD", raising=False)
    stale = _old_conversation("stale-one", age_days=400)
    fresh = _old_conversation("fresh-one", age_days=1)
    assert stale.exists() and fresh.exists()

    _tick()

    assert not stale.exists(), (
        "the scheduled sweep did not delete an over-age conversation — "
        "dry_run=False almost certainly did not reach sweep_conversations()")
    assert fresh.exists(), "the sweep deleted a conversation inside the policy"


# =========================================================================
# ACCEPTANCE 3 — RETAIN_DAYS is not passed
# =========================================================================

def test_heartbeat_does_not_pass_retain_days(monkeypatch):
    """Pin trap 1 against the next refactor: the scheduled call must take its
    policy from the conversation knob, never from `config.RETAIN_DAYS`."""
    monkeypatch.delenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", raising=False)
    seen = {}
    real = retention.sweep_conversations

    def spy(retain_days=None, *, dry_run=True):
        seen["retain_days"] = retain_days
        seen["dry_run"] = dry_run
        return real(retain_days, dry_run=dry_run)

    monkeypatch.setattr(retention, "sweep_conversations", spy)
    _tick()

    assert seen, "the maintenance job never called sweep_conversations"
    assert seen["retain_days"] is None, (
        f"the heartbeat passed retain_days={seen['retain_days']!r} — "
        "config.RETAIN_DAYS must NOT become the conversation policy")
    assert seen["dry_run"] is False, (
        "the heartbeat left dry_run at its True default: the sweep would "
        "log plausible output and delete nothing, forever")


# =========================================================================
# ACCEPTANCE 4 — legal hold outranks the scheduled sweep
# =========================================================================

def test_legal_hold_survives_the_scheduled_sweep(monkeypatch):
    """A hold is not negotiable against a cleanup job, on the scheduled path
    exactly as on the CLI one — and it is COUNTED, not silently skipped."""
    monkeypatch.setenv("OLYMPUS_CONVERSATION_RETAIN_DAYS", "30")
    monkeypatch.setenv("OLYMPUS_LEGAL_HOLD", "held-one")
    held = _old_conversation("held-one", age_days=400)
    doomed = _old_conversation("doomed-one", age_days=400)

    _tick()

    assert held.exists(), "a principal under OLYMPUS_LEGAL_HOLD was deleted"
    assert not doomed.exists(), "the sweep did not run at all"

    res = retention.sweep_conversations(dry_run=True)
    assert "held-one" in res["held"], "the hold was not counted"


# =========================================================================
# ACCEPTANCE 5 — compaction preserves recover_history EXACTLY
# =========================================================================

def _journal_with_reset(cid: str) -> list[dict]:
    """Build a journal whose history was REWRITTEN, so `sync` seals a
    `reset` + full-history pair and everything before it is dead."""
    memory.save_conversation(cid, [{"role": "user", "content": "one"}])
    memory.save_conversation(cid, [{"role": "user", "content": "one"},
                                   {"role": "assistant", "content": "two"}])
    # A rewrite (not an extension) — this is what /clear and ABC compaction do.
    rewritten = [{"role": "user", "content": "fresh start"},
                 {"role": "assistant", "content": "ok"}]
    memory.save_conversation(cid, rewritten)
    return rewritten


def test_compaction_preserves_recover_history_identically(monkeypatch):
    """The invariant. Compaction physically deletes records; if the bound is
    too high it destroys exactly the crash-recovery data the journal exists
    for, and does so silently because nothing reads it until a crash."""
    monkeypatch.setenv("OLYMPUS_SESSION_JOURNAL", "on")
    cid = "compact-me"
    expected = _journal_with_reset(cid)
    before = sessionlog.recover_history(cid)
    assert before == expected, f"precondition failed: {before!r}"

    res = sessionlog.compact_due(threshold_bytes=1)   # force: every journal
    assert any(c["sid"] == memory.safe_id(cid) for c in res["compacted"]), (
        f"the journal was not compacted: {res}")

    after = sessionlog.recover_history(cid)
    assert after == expected, (
        "compaction changed the recoverable history — the FULL list must be "
        f"identical.\n  before: {expected!r}\n  after:  {after!r}")


def test_compaction_actually_drops_records(monkeypatch):
    """A compaction that preserves history by dropping nothing would satisfy
    the test above and reclaim nothing. It must really shrink the journal."""
    monkeypatch.setenv("OLYMPUS_SESSION_JOURNAL", "on")
    cid = "shrink-me"
    _journal_with_reset(cid)
    sid = memory.safe_id(cid)
    recs_before, _ = sessionlog.read_verified(cid)
    sessionlog.compact_due(threshold_bytes=1)
    recs_after, _ = sessionlog.read_verified(cid)
    assert len(recs_after) < len(recs_before), (
        f"nothing was dropped: {len(recs_before)} -> {len(recs_after)}")


def test_append_only_journal_is_never_compacted(monkeypatch):
    """No `reset` means no dead prefix: every record still contributes to the
    replay, so the safe bound is 0 and the journal is left alone rather than
    truncated. Reported as skipped, not silently ignored."""
    monkeypatch.setenv("OLYMPUS_SESSION_JOURNAL", "on")
    cid = "append-only"
    hist = [{"role": "user", "content": "a"}]
    memory.save_conversation(cid, hist)
    hist = hist + [{"role": "assistant", "content": "b"}]
    memory.save_conversation(cid, hist)
    expected = sessionlog.recover_history(cid)

    res = sessionlog.compact_due(threshold_bytes=1)
    assert not res["compacted"], "an append-only journal was compacted"
    assert any(s["sid"] == memory.safe_id(cid) for s in res["skipped"]), (
        f"the skip was not reported: {res}")
    assert sessionlog.recover_history(cid) == expected


def test_safe_bound_is_verified_by_replay(monkeypatch):
    """`safe_compact_bound` returns 0 rather than a bound it cannot verify."""
    monkeypatch.setenv("OLYMPUS_SESSION_JOURNAL", "on")
    assert sessionlog.safe_compact_bound([]) == 0
    cid = "verified"
    _journal_with_reset(cid)
    records, _ = sessionlog.read_verified(cid)
    bound = sessionlog.safe_compact_bound(records)
    assert bound > 0
    kept = [r for r in records if r["seq"] > bound]
    assert sessionlog._replay(kept) == sessionlog._replay(records)


def test_compaction_threshold_is_well_below_the_ceiling():
    """A threshold AT the ceiling would only ever fire after appends and
    recovery had already stopped for that conversation."""
    assert 0 < sessionlog.COMPACT_AT_FRACTION <= 0.5
    assert sessionlog._max_bytes() * sessionlog.COMPACT_AT_FRACTION \
        < sessionlog._max_bytes()


def test_undersized_journals_are_left_alone(monkeypatch):
    """The default threshold must not compact an ordinary small journal."""
    monkeypatch.setenv("OLYMPUS_SESSION_JOURNAL", "on")
    cid = "small"
    _journal_with_reset(cid)
    res = sessionlog.compact_due()          # real threshold, tiny journal
    assert not res["compacted"], f"a small journal was compacted: {res}"
    assert res["scanned"] >= 1


# =========================================================================
# ACCEPTANCE 6 — both run inside the existing _watch_job ceiling
# =========================================================================

def test_both_run_inside_the_watch_job_ceiling(monkeypatch):
    """Neither routine gets its own unsupervised path: they are inside
    `_job_maintenance`, which `tick` runs through `_watch_job` and therefore
    under OLYMPUS_JOB_STALL_SECS."""
    import inspect
    src = inspect.getsource(heartbeat.tick)
    body = src[src.index('_due(state, "maintenance"'):]
    job = body[:body.index('_watch_job("maintenance"')]
    assert "sweep_conversations" in job, (
        "sweep_conversations is not inside _job_maintenance, so it does not "
        "inherit the per-job timeout")
    assert "compact_due" in job, (
        "compact_due is not inside _job_maintenance, so it does not inherit "
        "the per-job timeout")


def test_maintenance_job_survives_a_broken_journal(monkeypatch):
    """compact_due must never stop the sweep — it runs alongside five other
    maintenance routines."""
    monkeypatch.setenv("OLYMPUS_SESSION_JOURNAL", "on")
    d = config.MEMORY_DIR / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.journal.jsonl").write_text("{not json\n", encoding="utf-8")
    res = sessionlog.compact_due(threshold_bytes=1)
    assert isinstance(res, dict)        # returned rather than raised
