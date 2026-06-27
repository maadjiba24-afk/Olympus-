"""Migration importer: memories, profile, skills, and secret-safety."""

from olympus import migrate, profile, skills, memory


def _make_agent(tmp_path):
    (tmp_path / "MEMORY.md").write_text(
        "# Preferences\nUser likes concise answers.\n\n"
        "# Context\nRuns a bakery called Crumb.\n", encoding="utf-8")
    (tmp_path / "USER.md").write_text(
        "Founder of Crumb, a neighborhood bakery.", encoding="utf-8")
    sk = tmp_path / "skills" / "pricing"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: Pricing\ndescription: how to price\nspecialist: plutus\n---\n"
        "Anchor on value.\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-secret\nUNRELATED=foo\n", encoding="utf-8")
    return tmp_path


def test_import_memories_and_profile(tmp_path):
    root = _make_agent(tmp_path)
    summary = migrate.import_dir(str(root), user="cli")
    assert summary["memories"] >= 2
    assert summary["profile"] is True
    assert "Crumb" in profile.card("cli")


def test_import_skills(tmp_path):
    root = _make_agent(tmp_path)
    summary = migrate.import_dir(str(root), user="cli")
    assert summary["skills"] >= 1
    assert "Anchor on value" in skills.read("Pricing")


def test_keys_detected_but_not_imported_by_default(tmp_path):
    root = _make_agent(tmp_path)
    summary = migrate.import_dir(str(root), user="cli")
    assert "ANTHROPIC_API_KEY" in summary["keys_found"]
    assert summary["keys_imported"] == []
    assert any("NOT imported" in n for n in summary["notes"])


def test_keys_imported_on_request(tmp_path, monkeypatch):
    root = _make_agent(tmp_path)
    saved = {}
    monkeypatch.setattr("olympus.firstrun.save_env_value",
                        lambda k, v: saved.update({k: v}))
    summary = migrate.import_dir(str(root), user="cli", with_keys=True)
    assert "ANTHROPIC_API_KEY" in summary["keys_imported"]
    assert saved["ANTHROPIC_API_KEY"] == "sk-ant-secret"


def test_missing_path():
    summary = migrate.import_dir("/no/such/dir")
    assert any("not found" in n for n in summary["notes"])


def test_render():
    out = migrate.render({"memories": 3, "skills": 1, "profile": True,
                          "keys_found": ["X"], "keys_imported": [], "notes": ["hi"]})
    assert "memories imported : 3" in out and "note: hi" in out
