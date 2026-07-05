"""The REAL `read_source_file` path guard (tools._read_source_file).

Until now the only test naming this tool swapped in a fake handler
(test_codegraph_gate.py), so the escape guard itself had zero coverage. These
tests call the real function against a real on-disk tree — no handler faking.
The guard uses path-component containment (resolve + parents), so it must
reject traversal, absolute paths, the `<root>-backup` sibling its own code
comment warns about, and symlinks that resolve outside the root.
"""

import os

import pytest

from olympus import config, tools


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """A real project root on disk, with an inside file, a `<root>-backup`
    sibling directory, and a file outside the tree entirely."""
    r = tmp_path / "proj"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "mod.py").write_text("VALUE = 42\n")
    sibling = tmp_path / (r.name + "-backup")     # extends the root's name
    sibling.mkdir()
    (sibling / "leak.py").write_text("STOLEN = True\n")
    (tmp_path / "outside.py").write_text("OUTSIDE = True\n")
    monkeypatch.setattr(config, "PROJECT_ROOT", r)
    return r


def _read(path):
    return tools._read_source_file(path)


def test_happy_path_reads_file_inside_root(root):
    assert _read("pkg/mod.py") == "VALUE = 42\n"


def test_dotdot_traversal_is_rejected(root):
    out = _read("../../etc/passwd")
    assert out.startswith("Error:") and "escapes" in out


def test_absolute_path_is_rejected(root):
    # (PROJECT_ROOT / "/etc/passwd") resolves to the absolute path itself —
    # the containment check must still refuse it.
    out = _read("/etc/passwd")
    assert out.startswith("Error:") and "escapes" in out


def test_root_backup_sibling_is_rejected(root):
    # The failure mode the code comment names: a bare startswith() prefix
    # check would accept `<root>-backup/…` because its NAME extends the root
    # string. Component containment must reject it.
    out = _read(f"../{root.name}-backup/leak.py")
    assert out.startswith("Error:") and "escapes" in out


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics: POSIX only")
def test_symlink_escaping_root_is_rejected(root, tmp_path):
    # A path INSIDE the root that resolves OUTSIDE it via a symlink.
    (root / "innocent.py").symlink_to(tmp_path / "outside.py")
    out = _read("innocent.py")
    assert out.startswith("Error:") and "escapes" in out
