"""Per-worker workspace roots (M1 / closes DEFERRED #8).

Each dispatched specialist writes into its OWN root `workspace/agents/<key>/`,
so two specialists writing the same filename never clobber. Reads are a UNION
across the worker roots + the shared workspace, so inter-specialist handoff, the
gallery, and pre-existing workspaces stay visible. With no worker scope,
behaviour is exactly the single shared workspace (pre-M1).
"""
import threading
from pathlib import Path

import pytest

from olympus import actions, builtin_actions, gallery, sandbox


@pytest.fixture(autouse=True)
def _reg():
    builtin_actions.register_builtins()
    yield


# --- (a) concurrent same-name write, zero clobber -----------------------

def test_two_workers_concurrent_write_no_clobber(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    barrier = threading.Barrier(2)
    paths: dict[str, str] = {}

    def worker(name: str, content: str):
        # Each REAL thread scopes its own per-worker root, exactly as
        # orchestrator._run_one does for a dispatched specialist.
        tok = sandbox.set_worker_root(name)
        try:
            barrier.wait()                    # force the two writes to overlap
            paths[name] = sandbox.write_file("report.md", content)["path"]
        finally:
            sandbox.reset_worker_root(tok)

    t1 = threading.Thread(target=worker, args=("alpha", "A-content"))
    t2 = threading.Thread(target=worker, args=("beta", "B-content"))
    t1.start(); t2.start(); t1.join(); t2.join()

    pa, pb = Path(paths["alpha"]), Path(paths["beta"])
    assert pa != pb                                           # distinct roots
    assert pa.parent == (tmp_path / "agents" / "alpha").resolve()
    assert pb.parent == (tmp_path / "agents" / "beta").resolve()
    assert pa.read_text() == "A-content"                     # both intact —
    assert pb.read_text() == "B-content"                     # zero clobber


# --- (b) preview-root == execution-root, per worker ---------------------

def test_preview_root_equals_execution_root_per_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    # Worker 'alpha' PREVIEWS (prepares) a write on its worker thread.
    tok = sandbox.set_worker_root("alpha")
    try:
        a = actions.prepare("u", "write_file", {"path": "out.txt", "content": "X"})
    finally:
        sandbox.reset_worker_root(tok)
    assert a.payload["_pinned_root"] == str(sandbox.worker_root("alpha"))

    # EXECUTE later from a context with NO worker scope (the approval handler) —
    # it still lands in alpha's root, because the root was pinned at preview.
    res = builtin_actions._write_file_execute(a.payload)
    assert Path(res["path"]) == (sandbox.worker_root("alpha") / "out.txt").resolve()
    assert (sandbox.worker_root("alpha") / "out.txt").read_text() == "X"


# --- (c) union read: own file wins, shared/handoff still visible --------

def test_union_read_worker_then_shared(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    sandbox.write_file("shared.md", "SHARED")            # shared (no scope)
    tok = sandbox.set_worker_root("alpha")
    try:
        sandbox.write_file("report.md", "ALPHA")
        assert "ALPHA" in sandbox.read_file("report.md")  # own root wins
        assert "SHARED" in sandbox.read_file("shared.md")  # union fallback
    finally:
        sandbox.reset_worker_root(tok)
    # A later unscoped reader still finds alpha's report via the union.
    assert "ALPHA" in sandbox.read_file("report.md")


def test_each_worker_reads_its_own_same_name_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    for name, content in (("alpha", "AAA"), ("beta", "BBB")):
        tok = sandbox.set_worker_root(name)
        try:
            sandbox.write_file("note.md", content)
            assert content in sandbox.read_file("note.md")   # no cross-read
        finally:
            sandbox.reset_worker_root(tok)


# --- (d) default (no worker scope) is exactly the shared workspace ------

def test_no_worker_scope_uses_shared_root(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    assert sandbox.current_worker_root() is None
    res = sandbox.write_file("a.txt", "hi")
    assert Path(res["path"]) == (tmp_path / "a.txt").resolve()   # not agents/
    assert "hi" in sandbox.read_file("a.txt")


# --- (e) gallery unions worker-generated images -------------------------

def test_gallery_no_longer_unions_worker_images(monkeypatch, tmp_path):
    """INVERTED BY W2-1b, deliberately.

    This used to assert that the gallery UNIONS sibling worker roots, so an
    image a specialist wrote into its own root showed up in the gallery. That
    union is correct for specialists collaborating on one user's task and is
    exactly the cross-tenant leak once images belong to different people: it is
    how user B could list, read and delete user A's images.

    The gallery now resolves inside ONE principal's directory and never unions
    (`gallery._owned`). A worker-root image is no longer surfaced by it. The
    union itself is untouched for the sandbox file tools, which still share one
    workspace by design -- only the gallery stopped relying on it.
    """
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    monkeypatch.delenv("OLYMPUS_REQUIRE_LOGIN", raising=False)
    tok = sandbox.set_worker_root("iris")
    try:
        sandbox.write_file("pic.png", "png-bytes-placeholder")
    finally:
        sandbox.reset_worker_root(tok)
    assert "pic.png" not in [im["name"] for im in gallery.list_images("someone")]
    assert gallery.read_image("pic.png", "someone") is None


# --- confinement still holds inside a per-worker root -------------------

def test_worker_root_still_confines(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    tok = sandbox.set_worker_root("alpha")
    try:
        with pytest.raises(ValueError):
            sandbox.write_file("../escape.txt", "x")
    finally:
        sandbox.reset_worker_root(tok)
