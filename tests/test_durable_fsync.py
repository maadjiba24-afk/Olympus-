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


def test_usage_record_syncs_before_replace(monkeypatch):
    """The spend ledger. Default is fsync=always; see the knob test below."""
    events = trace(monkeypatch)
    usage.record("claude-opus-5", 100, 50)
    assert_data_synced_before_replace(events, label="usage.record")


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
    """`OLYMPUS_USAGE_FSYNC=auto` restores the pre-W1-1 atomic-only write.

    The knob exists because the sync is a measured 3.1x on this call (730 ->
    2269 us/call); `always` is the default because a lost ledger disables the
    budget cap. Publishing must still be atomic either way.
    """
    monkeypatch.setenv("OLYMPUS_USAGE_FSYNC", "auto")
    events = trace(monkeypatch)
    usage.record("claude-opus-5", 10, 5)
    kinds = [e[0] for e in events]
    assert "replace" in kinds, "auto must still publish atomically"
    first_replace = kinds.index("replace")
    assert not [e for e in events[:first_replace] if e[0] == "fsync"], \
        "auto must not fsync"


def test_usage_fsync_knob_defaults_to_always(monkeypatch):
    monkeypatch.delenv("OLYMPUS_USAGE_FSYNC", raising=False)
    assert usage._fsync_ledger() is True
    monkeypatch.setenv("OLYMPUS_USAGE_FSYNC", "always")
    assert usage._fsync_ledger() is True
    monkeypatch.setenv("OLYMPUS_USAGE_FSYNC", "AUTO")     # case-insensitive
    assert usage._fsync_ledger() is False


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
