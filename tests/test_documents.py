"""Phase 3 (the surveyed framework workspace) — the user document store + agent tools.
Writes ride the always-hold approval spine (reversible, never silent); reads
are free. Storage is per-user Markdown files under memory/users/<id>/documents.
"""

import io
import contextlib

import pytest

from olympus import actions, builtin_actions, config, documents  # noqa: F401


@pytest.fixture()
def user(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    return "docuser"


# --- backend ---------------------------------------------------------------

def test_save_read_list_roundtrip(user):
    documents.save(user, "My Plan", "# My Plan\n\nStep 1.")
    assert "# My Plan" in documents.read(user, "My Plan")
    docs = documents.listing(user)
    assert len(docs) == 1 and docs[0]["title"] == "My Plan"
    assert "My Plan" in documents.render_list(user)


def test_read_missing_returns_none(user):
    assert documents.read(user, "nope") is None


def test_save_is_reversible(user):
    documents.save(user, "doc", "v1")
    res = documents.save(user, "doc", "v2")
    assert documents.read(user, "doc") == "v2"
    documents.undo_save(res)
    assert documents.read(user, "doc") == "v1"


def test_undo_new_document_deletes_it(user):
    res = documents.save(user, "fresh", "hello")
    assert documents.exists(user, "fresh")
    documents.undo_save(res)
    assert not documents.exists(user, "fresh")


def test_delete_is_recoverable_backup(user):
    documents.save(user, "gone", "bye")
    assert documents.delete(user, "gone") is True
    assert not documents.exists(user, "gone")
    assert documents.delete(user, "gone") is False
    backups = list((documents._backup_dir(user)).glob("deleted-*"))
    assert backups and "bye" in backups[0].read_text()


def test_size_cap_enforced(user):
    with pytest.raises(ValueError, match="limit"):
        documents.save(user, "big", "x" * (documents._MAX_BYTES + 1))


def test_documents_are_per_user(user, monkeypatch):
    documents.save(user, "mine", "secret")
    assert documents.read("someone-else", "mine") is None


def test_name_slug_collision_is_same_doc(user):
    documents.save(user, "My Report!", "a")
    documents.save(user, "my report", "b")     # same slug
    assert len(documents.listing(user)) == 1
    assert documents.read(user, "My Report!") == "b"


# --- action spine (always-hold, reversible) --------------------------------

def test_write_document_action_registered_reversible():
    at = actions.registered()["write_document"]
    assert at.risk_class == actions.NOTABLE and at.reversible
    assert at.scope == ""                       # no scope gate; user's own content


def test_write_document_never_auto_executes_even_at_l4(user, monkeypatch):
    from olympus import tools, memory
    monkeypatch.setattr(memory, "current_user", lambda: user)
    monkeypatch.setattr(actions, "autonomy_level", lambda u: actions.L4_STANDING)
    out = tools.HANDLERS["write_document"]("Draft", "# Draft\nbody")
    # staged, not written
    assert not documents.exists(user, "Draft")
    assert "await" in out.lower() or "approv" in out.lower()
    pend = [a for a in actions.pending(user) if a.type == "write_document"]
    assert pend and pend[0].status == actions.PREPARED


def test_write_document_executes_on_approval(user, monkeypatch):
    from olympus import tools, memory
    monkeypatch.setattr(memory, "current_user", lambda: user)
    tools.HANDLERS["write_document"]("Draft", "# Draft\nbody")
    pend = [a for a in actions.pending(user) if a.type == "write_document"]
    actions.approve(user, pend[0].id)
    assert documents.read(user, "Draft") == "# Draft\nbody"


# --- read tools + registration ---------------------------------------------

def test_read_and_list_tools(user, monkeypatch):
    from olympus import tools, memory
    monkeypatch.setattr(memory, "current_user", lambda: user)
    documents.save(user, "Notes", "# Notes\nx")
    assert "# Notes" in tools.HANDLERS["read_document"]("Notes")
    assert "Notes" in tools.HANDLERS["list_documents"]()
    assert "No document" in tools.HANDLERS["read_document"]("ghost")


def test_tools_registered_and_on_peitho():
    from olympus import tools, specialists
    for name in ("list_documents", "read_document", "write_document"):
        assert name in tools.EXTRA_TOOLS and name in tools.HANDLERS
        assert name in specialists.SPECIALISTS["peitho"].extra_tools


# --- CLI -------------------------------------------------------------------

def _cli(monkeypatch, tmp_path, *argv):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    from olympus import cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(list(argv))
    return rc, buf.getvalue()


def test_cli_documents_list_and_read(monkeypatch, tmp_path):
    documents_dir_user = "cli"
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    documents.save(documents_dir_user, "Report", "# Report\nhi")
    _, out = _cli(monkeypatch, tmp_path, "documents", "list")
    assert "Report" in out
    _, out = _cli(monkeypatch, tmp_path, "documents", "read", "Report")
    assert "# Report" in out


def test_cli_documents_read_requires_name(monkeypatch, tmp_path):
    rc, out = _cli(monkeypatch, tmp_path, "documents", "read")
    assert rc == 1 and "Usage" in out
