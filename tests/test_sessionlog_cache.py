"""The `sync` hot-path cache (Phase-4 Stage-D defect D1).

`sessionlog.sync` runs on every turn and used to re-read, re-verify and
re-replay the whole journal each time, so a conversation's per-turn journaling
cost grew with its own depth. `_cache_get` now reuses the previous turn's
already-verified replay and chain tail when the file is provably untouched.

This file is about the CORRECTNESS of that shortcut, not its speed (the timing
A/B lives in tests/test_val_performance.py). The property under test: the cache
may only ever save work — it must never change what ends up on disk, and it
must never let an unverified byte through.
"""

import json
import os

import pytest

from olympus import config, memory, sessionlog

MSG_Q = {"role": "user", "content": "q"}
MSG_A = {"role": "assistant", "content": "a"}


@pytest.fixture(autouse=True)
def _clear_cache():
    sessionlog._CACHE.clear()
    yield
    sessionlog._CACHE.clear()


def _path(cid):
    return sessionlog._journal_path(sessionlog._sid(cid))


def _grow(cid, history, turns=6):
    for i in range(turns):
        history += [dict(MSG_Q, content=f"q{i}"), dict(MSG_A, content=f"a{i}")]
        sessionlog.sync(cid, list(history))
    return history


# ── the shortcut produces a byte-identical journal ─────────────────────────

def test_cached_and_uncached_sync_write_the_same_journal(monkeypatch):
    """The strongest statement available: run the same turn sequence with the
    cache live and with it forced to miss, and require the resulting records to
    agree on everything the cache could possibly have got wrong.

    Excluded from the comparison, because they differ by construction rather
    than by behaviour: `ts` (wall clock), `conversation_id` (the two runs use
    different ids), and the two fields that seal them — `sha` and therefore the
    successor's `prev`. Those are not taken on trust: `read_verified` below
    re-derives every seal and every link from the bytes on disk."""
    def shape(path):
        out = []
        for line in path.read_bytes().decode("utf-8").splitlines():
            rec = json.loads(line)
            out.append({k: rec[k] for k in ("v", "seq", "kind", "payload")})
        return out

    _grow("cached", [], turns=8)
    with_cache = shape(_path("cached"))

    monkeypatch.setattr(sessionlog, "_cache_get", lambda sid, path: None)
    _grow("uncached", [], turns=8)
    without = shape(_path("uncached"))

    assert with_cache == without
    assert len(with_cache) == 8             # one turn record per sync
    # and both chains verify end to end from the bytes alone
    for cid in ("cached", "uncached"):
        records, status = sessionlog.read_verified(cid)
        assert status == "ok" and len(records) == 8


def test_cache_does_not_change_the_recovered_history():
    history = _grow("recov", [], turns=10)
    assert sessionlog.recover_history("recov") == history
    records, status = sessionlog.read_verified("recov")
    assert status == "ok" and [r["seq"] for r in records] == list(range(1, 11))


def test_seqs_stay_dense_across_many_cached_syncs():
    _grow("dense", [], turns=25)
    seqs = [json.loads(l)["seq"]
            for l in _path("dense").read_bytes().decode().splitlines()]
    assert seqs == list(range(1, 26))


# ── every way the cache must refuse to be believed ─────────────────────────

def test_an_external_append_invalidates_the_entry():
    """Another process appending changes the size — the entry must not be used,
    or the next append would fork the chain at a stale seq."""
    history = _grow("ext", [], turns=3)
    entry = sessionlog._cache_get("ext", _path("ext"))
    assert entry is not None, "the cache never armed — the test proves nothing"

    sid = sessionlog._sid("ext")
    with sessionlog._locked(sid):           # simulate the other writer
        records, _ = sessionlog._scan(sid)
        sessionlog._append_records(
            sid, [("turn", {"messages": [dict(MSG_Q, content="outside")]})],
            (records[-1]["seq"], records[-1]["sha"]))

    assert sessionlog._cache_get("ext", _path("ext")) is None

    history.append(dict(MSG_Q, content="after"))
    sessionlog.sync("ext", list(history))
    records, status = sessionlog.read_verified("ext")
    assert status == "ok"
    assert [r["seq"] for r in records] == list(range(1, len(records) + 1))


def test_a_same_size_rewrite_is_caught_by_the_tail_seal():
    """The stat tuple can be forged (same size, and mtime_ns restored). The
    tail seal cannot: it is the recorded sha of the last committed record."""
    _grow("forge", [], turns=4)
    path = _path("forge")
    entry = sessionlog._cache_get("forge", path)
    assert entry is not None

    st = path.stat()
    lines = path.read_bytes().splitlines()
    # swap the final record for a different (still same-length) sealed record
    victim = json.loads(lines[-1])
    forged = dict(victim, payload={"messages": [dict(MSG_Q, content="X")]})
    forged["sha"] = ""
    forged["sha"] = sessionlog._seal(forged)
    body = b"\n".join(lines[:-1] + [sessionlog._canonical(forged).encode()])
    body += b"\n"
    body = body.ljust(st.st_size, b" ")[:st.st_size]   # preserve the size
    path.write_bytes(body)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # preserve the mtime

    assert path.stat().st_size == st.st_size
    assert sessionlog._cache_get("forge", path) is None, (
        "a rewritten journal was served from cache")


def test_a_deleted_journal_invalidates_the_entry():
    _grow("gone", [], turns=3)
    assert sessionlog._cache_get("gone", _path("gone")) is not None
    _path("gone").unlink()
    assert sessionlog._cache_get("gone", _path("gone")) is None


def test_a_torn_tail_is_never_served_from_cache():
    """`_tail_sha` returns "" for a file not ending in "\\n", so a torn write
    forces the full `_scan` — which is what truncates it back (I-J6)."""
    _grow("torn", [], turns=3)
    path = _path("torn")
    with open(path, "ab") as f:
        f.write(b'{"seq": 4, "partial"')
    assert sessionlog._cache_get("torn", path) is None
    records, status = sessionlog.read_verified("torn")
    assert status == "torn_tail_truncated"
    assert [r["seq"] for r in records] == [1, 2, 3]


def test_compaction_drops_the_entry():
    _grow("comp", [], turns=6)
    assert sessionlog._cache_get("comp", _path("comp")) is not None
    sessionlog.compact("comp", 3)
    assert sessionlog._cache_get("comp", _path("comp")) is None
    records, status = sessionlog.read_verified("comp")
    assert status == "ok"
    assert min(r["seq"] for r in records) > 3


def test_a_failed_append_drops_the_entry(monkeypatch):
    """A cache entry describes a journal we successfully wrote. If the write
    failed we do not know the on-disk tail, so extending from the cached one
    would fork the chain."""
    history = _grow("fail", [], turns=3)
    assert sessionlog._cache_get("fail", _path("fail")) is not None

    def boom(path):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sessionlog, "_open_append", boom)
    history.append(dict(MSG_Q, content="doomed"))
    assert sessionlog.sync("fail", list(history)) == 0      # captured, not raised
    assert sessionlog._cache_get("fail", _path("fail")) is None


def test_a_tombstone_drops_the_entry():
    """`_replay` skips tombstoned turns, so the cached history is stale the
    moment a tombstone lands."""
    history = _grow("tomb", [], turns=5)
    assert sessionlog._cache_get("tomb", _path("tomb")) is not None
    sessionlog.append_tombstone("tomb", 2, 3)
    assert sessionlog._cache_get("tomb", _path("tomb")) is None
    # and the next sync re-derives from the journal rather than the stale copy
    history.append(dict(MSG_Q, content="post-tombstone"))
    sessionlog.sync("tomb", list(history))
    records, status = sessionlog.read_verified("tomb")
    assert status == "ok"
    assert [r["seq"] for r in records] == list(range(1, len(records) + 1))


# ── the cache is bounded and does not alias ────────────────────────────────

def test_cache_is_bounded():
    for i in range(sessionlog._CACHE_MAX + 12):
        sessionlog.sync(f"many-{i}", [dict(MSG_Q, content=str(i))])
    assert len(sessionlog._CACHE) <= sessionlog._CACHE_MAX


def test_same_session_id_in_two_memory_dirs_does_not_alias(tmp_path,
                                                           monkeypatch):
    """Keyed on the journal path, not the bare session id — otherwise a
    relocated MEMORY_DIR (or the next test) could be served another journal's
    tail."""
    _grow("dup", [], turns=3)
    first = _path("dup")
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "other")
    memory.set_user("shared")
    second = _path("dup")
    assert first != second
    assert sessionlog._cache_get("dup", second) is None
    sessionlog.sync("dup", [dict(MSG_Q, content="fresh")])
    records, status = sessionlog.read_verified("dup")
    assert status == "ok" and [r["seq"] for r in records] == [1]


# ── read paths are untouched ───────────────────────────────────────────────

def test_read_paths_still_verify_unconditionally():
    """The integrity guarantee is consumed on read. The write-path shortcut
    must not have leaked into any of them."""
    import inspect
    for fn in (sessionlog.read_verified, sessionlog.recover_history,
               sessionlog.journal_status, sessionlog.compact):
        src = inspect.getsource(fn)
        assert "_cache_get" not in src, (
            f"{fn.__name__} takes the cached shortcut — reads must always "
            f"verify the full chain")
        assert "_scan(" in src, f"{fn.__name__} no longer scans"
