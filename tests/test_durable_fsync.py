"""W1-1: the durable stores fsync before they publish.

Simulating power loss is not feasible in a unit test, so these assert the
CALLS: `os.fsync` must be invoked on the temp file's descriptor BEFORE
`os.replace` makes it visible. That ordering is the whole property — a sync
after the rename proves nothing, because the window being closed is exactly
"the rename reached disk and the data blocks did not".

Both syscalls are monkeypatched at the `os` module, which is where
`olympus.atomicio` looks them up.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from olympus import (atomicio, config, domainlore, goals, identity, memory,
                     prefs, store, todos, usage)


# --- tracing helpers -------------------------------------------------------

def trace(monkeypatch) -> list:
    """Record ("fsync", kind, size) and ("replace", src, dst) in call order.

    `kind` distinguishes a directory sync from a file sync via fstat, so a test
    can require that the DATA was synced and not merely the parent directory.
    """
    events: list = []
    real_fsync, real_replace = os.fsync, os.replace

    def fake_fsync(fd):
        try:
            st = os.fstat(fd)
            kind = "dir" if stat.S_ISDIR(st.st_mode) else "file"
            size = st.st_size
        except OSError:                       # pragma: no cover - defensive
            kind, size = "unknown", -1
        events.append(("fsync", kind, size))
        return real_fsync(fd)

    def fake_replace(src, dst, *a, **kw):
        events.append(("replace", str(src), str(dst)))
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "fsync", fake_fsync)
    monkeypatch.setattr(os, "replace", fake_replace)
    return events


def assert_data_synced_before_replace(events: list, *, label: str) -> None:
    """The core contract: a FILE fsync strictly precedes the first replace."""
    kinds = [e[0] for e in events]
    assert "replace" in kinds, f"{label}: never published (no os.replace)"
    first_replace = kinds.index("replace")
    before = [e for e in events[:first_replace]
              if e[0] == "fsync" and e[1] == "file"]
    assert before, (
        f"{label}: os.replace ran with no preceding file fsync — "
        f"the rename can reach disk before the data. events={events}")
    # The synced descriptor must hold the payload, not an empty file: a sync
    # issued before the write would satisfy ordering and still lose the data.
    assert any(size > 0 for _, _, size in before), (
        f"{label}: fsync happened on an empty descriptor — synced before the "
        f"write. events={events}")


# --- one case per durable store -------------------------------------------

def test_filestore_put_syncs_before_replace(monkeypatch):
    """store.FileStore.put — usermem, relgraph, docrag and routing_outcomes
    all ride this one, so it is the highest-value site."""
    events = trace(monkeypatch)
    store.FileStore().put("ns", "key", b"payload-bytes")
    assert_data_synced_before_replace(events, label="store.put")


def test_usage_record_syncs_when_opted_in(monkeypatch):
    """The spend ledger is the ONE W1-1 site that does not fsync by default
    (W1-1a — the sync costs roughly 15 ms inside a cross-process lock on CI
    storage). This proves the OPT-IN path is still correct, which is what keeps
    W1-1c honest: when the ledger becomes cheap to sync, this is the contract it
    has to meet.

    PROPERTY PRESERVED: `always` really does put the record on stable storage.
    FORMAT CHANGED: it no longer does so before an `os.replace`.

    This used to call `assert_data_synced_before_replace`, because `record()`
    republished the whole ledger with tmp + replace. W2-2 made it an APPEND, so
    there is no replace to order against — the durable-publish ordering that
    helper checks does not exist on this path any more. Asserting it would be
    asserting the absence of a write path, which is a different claim.
    """
    monkeypatch.setenv("OLYMPUS_USAGE_FSYNC", "always")
    events = trace(monkeypatch)
    usage.record("claude-opus-5", 100, 50)
    file_syncs = [e for e in events if e[0] == "fsync" and e[1] == "file"]
    assert file_syncs, f"`always` did not fsync the appended record: {events}"
    assert any(size > 0 for _, _, size in file_syncs), (
        "fsync happened on an empty descriptor — synced before the write")


def test_identity_revoke_syncs_before_replace(monkeypatch):
    """Security-relevant: a lost write here silently UN-revokes a capability."""
    events = trace(monkeypatch)
    identity.revoke("jti-abc")
    assert_data_synced_before_replace(events, label="identity.revoke")
    assert identity.is_revoked("jti-abc")


def test_prefs_set_syncs_before_replace(monkeypatch):
    events = trace(monkeypatch)
    prefs.set("shared", "daily_budget", 5)
    assert_data_synced_before_replace(events, label="prefs.set")


def test_goals_save_syncs_before_replace(monkeypatch):
    events = trace(monkeypatch)
    goals._save([])
    assert_data_synced_before_replace(events, label="goals._save")


def test_todos_save_syncs_before_replace(monkeypatch):
    events = trace(monkeypatch)
    todos._save("shared", [{"id": "1", "text": "t", "done": False,
                            "due": None, "created": 0}])
    assert_data_synced_before_replace(events, label="todos._save")


def test_domainlore_save_syncs_before_replace(monkeypatch):
    events = trace(monkeypatch)
    domainlore._save({})
    assert_data_synced_before_replace(events, label="domainlore._save")


def test_domainlore_save_staged_syncs_before_replace(monkeypatch):
    events = trace(monkeypatch)
    domainlore._save_staged([])
    assert_data_synced_before_replace(events, label="domainlore._save_staged")


def test_save_conversation_syncs_before_replace(monkeypatch):
    events = trace(monkeypatch)
    memory.save_conversation("conv-1", [{"role": "user", "content": "hi"}])
    assert_data_synced_before_replace(events, label="memory.save_conversation")


def test_save_state_syncs_before_replace(monkeypatch):
    """The heartbeat state — a lost write resets every cadence timestamp."""
    events = trace(monkeypatch)
    memory.save_state({"last_run": 123.0})
    assert_data_synced_before_replace(events, label="memory.save_state")


def test_watchlist_pop_syncs_before_replace(monkeypatch):
    from olympus import proclock
    path = memory._watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("https://a.example\nhttps://b.example\n", encoding="utf-8")
    events = trace(monkeypatch)
    assert memory._watchlist_pop_locked(proclock) == "https://a.example"
    assert_data_synced_before_replace(events, label="memory._watchlist_pop")


# --- the usage hot-path knob ----------------------------------------------

def test_usage_fsync_knob_auto_skips_the_sync(monkeypatch):
    """`OLYMPUS_USAGE_FSYNC=auto` keeps the atomic-only write.

    Publishing must still be atomic either way — `auto` gives up durability
    against power loss, never atomicity against a peer process.
    """
    monkeypatch.setenv("OLYMPUS_USAGE_FSYNC", "auto")
    events = trace(monkeypatch)
    usage.record("claude-opus-5", 10, 5)
    # FILE syncs only. `_append_usage` fsyncs the parent DIRECTORY on the day's
    # FIRST append and only then (the Q5 create-path barrier: fsyncing a file
    # does not make its directory entry durable, and the append path never goes
    # through atomicio.publish, so nothing else covers it). That is a different
    # guarantee from the per-record data barrier this knob controls, it is
    # one-per-day rather than per-call, and `auto` was never a promise to skip
    # it. Counting it here made this test red on POSIX and green on Windows,
    # where `atomicio.CAN_FSYNC_DIR` is False and the call is a no-op — a
    # verdict that depended on the platform rather than on the behaviour.
    assert not [e for e in events if e[0] == "fsync" and e[1] == "file"], (
        f"`auto` must not fsync the RECORD on the record path: {events}")
    assert len([e for e in events if e[0] == "fsync" and e[1] == "dir"]) <= 1, (
        f"the parent-directory sync is a CREATE-path barrier and must fire at "
        f"most once, not per append: {events}")
    # PROPERTY PRESERVED, mechanism changed. The old assertion was
    # `"replace" in kinds` — atomicity against a peer process came from
    # tmp + os.replace. An APPEND of one newline-terminated line gives it by
    # construction instead: a concurrent reader sees whole lines, and
    # `_replay_journal` discards a torn final line rather than mis-parsing it.
    # The guarantee survives; asserting `os.replace` would now be asserting a
    # mechanism that no longer exists on this path.
    import time as _t
    journal = config.MEMORY_DIR / "usage" / f"{_t.strftime('%Y-%m-%d')}.jsonl"
    assert journal.exists(), "the append produced no journal"
    raw = journal.read_bytes()
    assert raw.endswith(b"\n"), (
        "a record was left without its terminating newline — a concurrent "
        "reader could not tell a complete record from a torn one")
    assert len(raw.splitlines()) == 1
    # W2-2: unset resolves to `interval`, NOT `auto` -- the message here used
    # to say "auto" and would have kept saying it, passing, forever. A stale
    # assertion message on a green test is a lie that survives every run.
    monkeypatch.delenv("OLYMPUS_USAGE_FSYNC", raising=False)
    assert usage._fsync_ledger() == "interval", (
        "unset must resolve to the group-commit default")


def test_usage_record_does_not_fsync_by_default(monkeypatch):
    """REGRESSION GUARD (W1-1a). The ledger sync costs roughly 15 ms per call
    inside a cross-process lock on CI storage — ~300 ms serialized across a
    20-specialist council turn — which is what
    `test_val_performance.py::test_observability_overhead_absolute_cost_bounded`
    caught on the windows-py3.12 leg.

    If a future change re-arms the default, this fails here in milliseconds
    instead of nine minutes into the Windows leg.

    PROPERTY PRESERVED, RESTATED FOR THE NEW DEFAULT. The old assertion was
    "the default never fsyncs at all", which matched `auto`. W2-2's default is
    `interval` — group commit — which DOES sync, at most once per interval. So
    the guard cannot be "never syncs" any more without pinning `auto` forever
    and retiring group commit. What W1-1a actually protected is that the
    default must not sync ON EVERY CALL, because that is what cost ~19.5 ms
    per call on the runner. That is what is asserted now: many records, at most
    one sync.

    COUNTED PER KIND, for the same reason. The day's first append also syncs
    the parent DIRECTORY once (the Q5 create-path barrier), which is a real
    `os.fsync` on POSIX and a no-op on Windows. Lumping the two together made
    this guard count 2 on Linux against a bound of 1 while passing on Windows —
    a regression guard whose verdict came from the platform. Both are bounded
    here, separately and on their own terms.
    """
    monkeypatch.delenv("OLYMPUS_USAGE_FSYNC", raising=False)
    monkeypatch.setenv("OLYMPUS_USAGE_FSYNC_INTERVAL_MS", "60000")
    usage._LAST_FSYNC[0] = 0.0            # let exactly the first one through
    events = trace(monkeypatch)
    for _ in range(25):
        usage.record("claude-opus-5", 100, 50)
    syncs = [e for e in events if e[0] == "fsync" and e[1] == "file"]
    assert len(syncs) <= 1, (
        f"the default fsynced {len(syncs)} records across 25 calls — a "
        f"PER-CALL sync arriving as a default is the W1-1a regression "
        f"(~19.5 ms per call on the runner). See usage._fsync_ledger. "
        f"events={events}")
    dir_syncs = [e for e in events if e[0] == "fsync" and e[1] == "dir"]
    assert len(dir_syncs) <= 1, (
        f"the parent directory was synced {len(dir_syncs)} times across 25 "
        f"appends — that barrier belongs to the day's FIRST append only, and "
        f"per-append it is the same per-call device flush under another name")


def test_usage_fsync_knob_resolves_each_mode(monkeypatch):
    """The SPECIFIC mode for each input.

    `_fsync_ledger()` returned a bool until W2-2 and now returns one of
    "always" / "auto" / "interval". Rewriting these as `== "interval"` alone
    would pin today's default and retire the invariant W1-1a put here, so the
    fail-safe DIRECTION is asserted separately below rather than as a side
    effect of these equalities.
    """
    for value, expected in (("always", "always"), ("ALWAYS", "always"),
                            ("auto", "auto"), ("AUTO", "auto"),
                            ("interval", "interval"), ("  always  ", "always")):
        monkeypatch.setenv("OLYMPUS_USAGE_FSYNC", value)
        assert usage._fsync_ledger() == expected, f"{value!r} misresolved"
    monkeypatch.delenv("OLYMPUS_USAGE_FSYNC", raising=False)
    assert usage._fsync_ledger() == "interval"


def test_usage_fsync_never_resolves_to_always_by_accident(monkeypatch):
    """THE W1-1a INVARIANT, asserted on its own.

    W1-1a's regression was a per-call fsync arriving as a DEFAULT and costing
    ~19.5 ms inside a lock on the CI runner. The guard that stopped it
    returning was: neither an unset value nor an unrecognised one may ever
    resolve to `always`. That is a statement about DIRECTION, and it must
    survive any future change to what the default happens to be -- which is
    why it is not folded into the mode-equality test above.
    """
    monkeypatch.delenv("OLYMPUS_USAGE_FSYNC", raising=False)
    assert usage._fsync_ledger() != "always", (
        "unset resolved to `always` -- a per-call fsync arriving as a default "
        "is the W1-1a regression")
    for junk in ("yes", "", "1", "true", "on", "alwasy", "ALWAYS_", "  "):
        monkeypatch.setenv("OLYMPUS_USAGE_FSYNC", junk)
        assert usage._fsync_ledger() != "always", (
            f"unrecognised value {junk!r} resolved to `always` -- a typo must "
            f"never arm a per-call fsync")


def test_unrecognised_fsync_mode_is_reported_not_silent(monkeypatch):
    """A typo must not be answered by silently choosing something else.

    `OLYMPUS_USAGE_FSYNC=alwasy` resolves to `interval`: the operator asked
    for a zero-width tail and got a one-second one. Falling back is right;
    doing it silently is not.
    """
    from olympus import errors
    seen = []
    monkeypatch.setattr(errors, "capture",
                        lambda where, err, context="": seen.append(where))
    monkeypatch.setattr(usage, "_FSYNC_MODE_WARNED", False)
    monkeypatch.setenv("OLYMPUS_USAGE_FSYNC", "alwasy")
    assert usage._fsync_ledger() == "interval"
    assert "usage.fsync_mode" in seen, (
        "an unrecognised fsync mode was accepted silently")
    # Reported ONCE: this is read on the per-append path.
    seen.clear()
    usage._fsync_ledger()
    assert seen == [], "the unrecognised-mode report repeated on every call"


# --- the platform guard ----------------------------------------------------

def test_dir_fsync_skipped_cleanly_where_unavailable(monkeypatch, tmp_path):
    """Windows cannot open a directory as a descriptor, so `os.O_DIRECTORY`
    does not exist there and the rename-durability half is unavailable. It must
    degrade to a no-op, not raise — the file sync still applies."""
    monkeypatch.setattr(atomicio, "CAN_FSYNC_DIR", False)
    called: list = []
    monkeypatch.setattr(os, "fsync", lambda fd: called.append(fd))
    atomicio.fsync_dir(tmp_path)                     # must not raise
    assert called == [], "fsync_dir must not sync anything when unavailable"


def test_publish_still_works_without_dir_fsync(monkeypatch, tmp_path):
    """The whole publish path stays correct on a platform with no dir fsync."""
    monkeypatch.setattr(atomicio, "CAN_FSYNC_DIR", False)
    events = trace(monkeypatch)
    target = tmp_path / "x.json"
    atomicio.publish(tmp_path / ".x.tmp", target, json.dumps({"a": 1}))
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert_data_synced_before_replace(events, label="publish (no dir fsync)")
    assert not [e for e in events if e[0] == "fsync" and e[1] == "dir"]


def test_dir_fsync_survives_an_unopenable_directory(monkeypatch, tmp_path):
    """A directory that will not open is not a reason to fail a write whose
    data already landed and was synced."""
    monkeypatch.setattr(atomicio, "CAN_FSYNC_DIR", True)
    monkeypatch.setattr(os, "O_DIRECTORY", getattr(os, "O_DIRECTORY", 0),
                        raising=False)

    def boom(*a, **kw):
        raise OSError("cannot open directory")

    monkeypatch.setattr(os, "open", boom)
    atomicio.fsync_dir(tmp_path)                     # must not raise


@pytest.mark.skipif(not atomicio.CAN_FSYNC_DIR,
                    reason="platform cannot fsync a directory (Windows)")
def test_dir_fsync_actually_syncs_on_posix(monkeypatch, tmp_path):
    """Where the platform supports it, the parent directory IS synced after
    the replace — that is what makes the rename itself durable."""
    events = trace(monkeypatch)
    atomicio.publish(tmp_path / ".y.tmp", tmp_path / "y.json", "{}")
    kinds = [(e[0], e[1]) for e in events if e[0] == "fsync"]
    assert ("fsync", "dir") in kinds, f"no directory fsync: {events}"


# --- the bytes on disk must not have moved ---------------------------------

def test_publish_matches_write_text_bytes(tmp_path):
    """`publish` writes str through text mode with `Path.write_text`'s
    defaults. A binary write here would change newline translation on Windows
    and move every file hash with it — including the conversation snapshot
    sha that sessionlog.compact records."""
    payload = json.dumps({"a": [1, 2]}, indent=2)     # contains newlines
    ref = tmp_path / "ref.json"
    ref.write_text(payload, encoding="utf-8")
    out = tmp_path / "out.json"
    atomicio.publish(tmp_path / ".out.tmp", out, payload)
    assert out.read_bytes() == ref.read_bytes()


def test_publish_bytes_roundtrip(tmp_path):
    blob = bytes(range(256))
    out = tmp_path / "b.bin"
    atomicio.publish(tmp_path / ".b.tmp", out, blob)
    assert out.read_bytes() == blob


def test_publish_removes_the_temp_file(tmp_path):
    tmp = tmp_path / ".t.tmp"
    out = tmp_path / "t.json"
    atomicio.publish(tmp, out, "{}")
    assert not tmp.exists(), "temp file must be consumed by the replace"
    assert out.exists()


def test_publish_applies_chmod(tmp_path):
    """FileStore.put relies on this: the vault's ciphertext must land 0600."""
    out = tmp_path / "secret"
    atomicio.publish(tmp_path / ".secret.tmp", out, b"cipher", chmod=0o600)
    assert out.exists()
    if os.name == "posix":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
