"""The sovereign memory contract (Task 2 of the moat plan).

What these prove — the data-sovereignty guarantees Olympus's file memory makes:

1. Notes carry a versioned frontmatter; frontmatter-less notes still read as v0,
   and `migrate` upgrades them in place without touching the body.
2. Memory is portable and lossless: export -> delete -> import restores the
   files **byte-for-byte**.
3. Import is safe: it REFUSES an archive whose schema_version it doesn't know
   (no silent best-effort import), and refuses a non-export tarball.
4. Delete is real and targeted: it removes exactly the named files and they are
   gone from disk afterwards.
5. Encryption reuses vault.py's key — no new crypto dependency — and round-trips.
"""

import io
import json
import tarfile
from pathlib import Path

import pytest

from olympus import config, memory


# --- helpers -------------------------------------------------------------

def _seed(user: str) -> dict[str, bytes]:
    """Create a few notes for `user` and return {relpath: bytes} as on disk."""
    memory.set_user(user)
    memory.save("lessons", "First lesson", "remember the moat")
    memory.save("corrections", "A fix", "do it this way")
    memory.set_user("shared")
    return _snapshot(memory._memory_roots(user))


def _snapshot(roots) -> dict[str, bytes]:
    snap = {}
    for r in roots:
        if not r.exists():
            continue
        for p in sorted(r.rglob("*")):
            if p.is_file():
                snap[p.relative_to(config.MEMORY_DIR).as_posix()] = p.read_bytes()
    return snap


def _make_archive(path: Path, manifest: dict, files=()) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, data in files:
            info = tarfile.TarInfo(f"data/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        md = json.dumps(manifest).encode("utf-8")
        mi = tarfile.TarInfo("manifest.json")
        mi.size = len(md)
        tar.addfile(mi, io.BytesIO(md))
    path.write_bytes(buf.getvalue())


# --- 1. versioned notes + frontmatter ------------------------------------

def test_save_writes_versioned_frontmatter():
    memory.set_user("shared")
    path = memory.save("lessons", "Titled", "the body")
    text = path.read_text(encoding="utf-8")
    assert memory.note_schema_version(text) == memory.NOTE_SCHEMA_VERSION
    meta, body = memory.parse_note(text)
    assert meta["schema_version"] == str(memory.NOTE_SCHEMA_VERSION)
    assert body.startswith("# Titled")
    assert "the body" in body
    assert memory.note_title(text) == "Titled"


def test_frontmatterless_note_reads_as_v0():
    meta, body = memory.parse_note("# Old\n\nplain body\n")
    assert meta == {} and memory.note_schema_version("# Old\n\nx") == 0
    assert body == "# Old\n\nplain body\n"
    assert memory.note_title("# Old\n\nplain body\n") == "Old"


def test_migrate_upgrades_v0_in_place_and_is_idempotent():
    memory.set_user("shared")
    d = config.MEMORY_DIR / "lessons"
    d.mkdir(parents=True, exist_ok=True)
    legacy = d / "20240101-000000-legacy.md"
    legacy.write_text("# Legacy\n\nold wisdom\n", encoding="utf-8")

    first = memory.migrate_notes()
    assert first["migrated"] == 1
    text = legacy.read_text(encoding="utf-8")
    assert memory.note_schema_version(text) == memory.NOTE_SCHEMA_VERSION
    # body preserved verbatim under the new frontmatter, original date kept
    _, body = memory.parse_note(text)
    assert body == "# Legacy\n\nold wisdom\n"
    assert "created: 20240101-000000" in text

    second = memory.migrate_notes()        # nothing left to do
    assert second["migrated"] == 0


# --- 2. export -> delete -> import is byte-identical ----------------------

def test_export_delete_import_is_byte_identical(tmp_path):
    before = _seed("alice")
    assert before, "fixture should have created files"
    archive = tmp_path / "alice.tar.gz"

    manifest = memory.export_memory(archive, user="alice")
    assert manifest["schema_version"] == memory.ARCHIVE_SCHEMA_VERSION
    assert len(manifest["files"]) == len(before)

    removed = memory.delete_memory("alice")
    assert sorted(removed) == sorted(before)
    assert _snapshot(memory._memory_roots("alice")) == {}   # truly gone

    result = memory.import_memory(archive)
    assert result["count"] == len(before)
    assert result["verified"] == len(before)               # checksums matched

    after = _snapshot(memory._memory_roots("alice"))
    assert after == before                                  # byte-for-byte


# --- 3. import refuses what it cannot vouch for ---------------------------

def test_import_refuses_unknown_schema_version(tmp_path):
    archive = tmp_path / "future.tar.gz"
    _make_archive(archive, {"schema_version": 999, "files": []})
    with pytest.raises(ValueError, match="unknown schema_version"):
        memory.import_memory(archive)


def test_import_rejects_path_traversal(tmp_path):
    # A crafted archive whose manifest path escapes MEMORY_DIR must not write
    # outside it — importing is a trust boundary for archives made elsewhere.
    archive = tmp_path / "evil.tar.gz"
    _make_archive(archive,
                  {"schema_version": memory.ARCHIVE_SCHEMA_VERSION,
                   "files": [{"path": "../escape.txt", "sha256": ""}]},
                  files=[("../escape.txt", b"pwned")])
    result = memory.import_memory(archive)
    assert result["count"] == 0                                # entry skipped
    assert not (config.MEMORY_DIR.parent / "escape.txt").exists()


def test_import_refuses_non_export_tarball(tmp_path):
    archive = tmp_path / "random.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"hello"
        info = tarfile.TarInfo("readme.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    archive.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="no manifest.json"):
        memory.import_memory(archive)


# --- 4. targeted delete removes exactly what's named ---------------------

def test_targeted_delete_removes_only_named_files():
    memory.set_user("bob")
    keep = memory.save("lessons", "Keep me", "stays")
    memory.set_user("bob")
    drop = memory.save("corrections", "Drop me", "goes")
    memory.set_user("shared")

    removed = memory.delete_memory("bob", category="corrections")
    rel_drop = drop.relative_to(config.MEMORY_DIR).as_posix()
    rel_keep = keep.relative_to(config.MEMORY_DIR).as_posix()

    assert removed == [rel_drop]          # exactly the one named category
    assert not drop.exists()              # gone from disk
    assert keep.exists()                  # the other category untouched
    assert rel_keep not in removed


def test_delete_by_note_id_removes_single_file():
    memory.set_user("carol")
    a = memory.save("lessons", "Alpha", "one")
    memory.set_user("carol")
    b = memory.save("lessons", "Beta", "two")
    memory.set_user("shared")

    removed = memory.delete_memory("carol", category="lessons", note_id=a.stem)
    assert removed == [a.relative_to(config.MEMORY_DIR).as_posix()]
    assert not a.exists() and b.exists()


def test_delete_unknown_category_raises():
    with pytest.raises(ValueError, match="unknown memory category"):
        memory.delete_memory("dave", category="nonsense")


# --- 5. optional at-rest encryption reuses vault.py ----------------------

@pytest.mark.requires_crypto
def test_encrypted_export_roundtrips_with_vault_key(tmp_path, monkeypatch):
    from olympus import vault  # noqa: F401  (module presence asserted by the marker)
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "a-strong-stable-secret")

    before = _seed("erin")
    archive = tmp_path / "erin.enc"
    memory.export_memory(archive, user="erin", encrypt=True)
    # the file is genuinely encrypted: not a gzip stream
    assert archive.read_bytes()[:2] != b"\x1f\x8b"

    memory.delete_memory("erin")
    assert _snapshot(memory._memory_roots("erin")) == {}

    result = memory.import_memory(archive)
    assert result["count"] == len(before)
    assert _snapshot(memory._memory_roots("erin")) == before


@pytest.mark.requires_crypto
def test_encrypted_export_fails_clearly_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "key-one")
    _seed("frank")
    archive = tmp_path / "frank.enc"
    memory.export_memory(archive, user="frank", encrypt=True)

    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "wrong-key")
    with pytest.raises(ValueError, match="could not be decrypted"):
        memory.import_memory(archive)


# --- docs ----------------------------------------------------------------

def test_memory_format_doc_exists():
    doc = Path(__file__).resolve().parent.parent / "docs" / "MEMORY_FORMAT.md"
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    assert "schema_version" in text and "export" in text and "import" in text
