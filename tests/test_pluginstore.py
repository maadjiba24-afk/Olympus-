"""Hardened plugin installer: deny-by-default, pinned provenance, tamper
detection, source policy, and optional loader enforcement."""

import hashlib

import pytest

from olympus import pluginstore


@pytest.fixture(autouse=True)
def _dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_PLUGINS_DIR", str(tmp_path / "plugins"))
    monkeypatch.delenv("OLYMPUS_PLUGIN_ALLOW", raising=False)
    monkeypatch.delenv("OLYMPUS_PLUGIN_ENFORCE", raising=False)


def _plugin_file(tmp_path, body="# a plugin\n"):
    f = tmp_path / "myplugin.py"
    f.write_text(body, encoding="utf-8")
    return f, hashlib.sha256(body.encode()).hexdigest()


# --- provenance ------------------------------------------------------------

def test_install_requires_pinned_hash(tmp_path):
    f, _ = _plugin_file(tmp_path)
    with pytest.raises(ValueError, match="no --sha256 pinned"):
        pluginstore.install(str(f))


def test_install_with_correct_hash_succeeds(tmp_path):
    f, digest = _plugin_file(tmp_path)
    res = pluginstore.install(str(f), sha256=digest)
    assert res["sha256"] == digest
    assert (pluginstore.plugins_dir() / f"{res['name']}.py").exists()
    assert pluginstore.list_installed()[0]["source"] == str(f)


def test_install_rejects_hash_mismatch(tmp_path):
    f, _ = _plugin_file(tmp_path)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        pluginstore.install(str(f), sha256="deadbeef")
    assert pluginstore.list_installed() == []      # nothing installed


def test_allow_unpinned_local_file(tmp_path):
    f, _ = _plugin_file(tmp_path)
    res = pluginstore.install(str(f), allow_unpinned=True)
    assert res["name"]


# --- source policy ---------------------------------------------------------

def test_remote_denied_without_allowlist(monkeypatch):
    with pytest.raises(ValueError, match="not in OLYMPUS_PLUGIN_ALLOW"):
        pluginstore.install("https://evil.example/x.py", sha256="ab")


def test_remote_allowed_by_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("OLYMPUS_PLUGIN_ALLOW", "https://trusted.example/")
    body = b"# ok\n"
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(pluginstore, "_fetch", lambda src: body)
    res = pluginstore.install("https://trusted.example/p.py", sha256=digest)
    assert res["sha256"] == digest


def test_remote_outside_policy_denied(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PLUGIN_ALLOW", "https://trusted.example/")
    with pytest.raises(ValueError, match="not in OLYMPUS_PLUGIN_ALLOW"):
        pluginstore.install("https://other.example/p.py", sha256="ab")


# --- tamper detection ------------------------------------------------------

def test_verify_detects_modification(tmp_path):
    f, digest = _plugin_file(tmp_path)
    res = pluginstore.install(str(f), sha256=digest)
    # tamper with the installed copy
    (pluginstore.plugins_dir() / f"{res['name']}.py").write_text(
        "# tampered\n", encoding="utf-8")
    rows = pluginstore.verify()
    assert rows[0]["status"] == "MODIFIED"


def test_verify_all_ok_after_clean_install(tmp_path):
    f, digest = _plugin_file(tmp_path)
    pluginstore.install(str(f), sha256=digest)
    assert all(r["status"] == "ok" for r in pluginstore.verify())


def test_remove(tmp_path):
    f, digest = _plugin_file(tmp_path)
    res = pluginstore.install(str(f), sha256=digest)
    assert pluginstore.remove(res["name"]) is True
    assert pluginstore.list_installed() == []
    assert pluginstore.remove("nope") is False


# --- loader enforcement ----------------------------------------------------

def test_verified_names_off_by_default():
    assert pluginstore.verified_names() is None


def test_verified_names_lists_only_matching(monkeypatch, tmp_path):
    f, digest = _plugin_file(tmp_path)
    res = pluginstore.install(str(f), sha256=digest)
    monkeypatch.setenv("OLYMPUS_PLUGIN_ENFORCE", "1")
    assert pluginstore.verified_names() == {res["name"]}
    # tamper → dropped from the verified set
    (pluginstore.plugins_dir() / f"{res['name']}.py").write_text("x",
                                                                 encoding="utf-8")
    assert pluginstore.verified_names() == set()


def test_oversize_plugin_rejected(monkeypatch, tmp_path):
    big = b"x" * (pluginstore._MAX_PLUGIN_BYTES + 1)
    monkeypatch.setattr(pluginstore, "_fetch", lambda src: big)
    monkeypatch.setenv("OLYMPUS_PLUGIN_ALLOW", "https://t/")
    with pytest.raises(ValueError, match="exceeds"):
        pluginstore.install("https://t/big.py",
                            sha256=hashlib.sha256(big).hexdigest())
