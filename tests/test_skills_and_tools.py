from olympus import config, skills, tools
from olympus.specialists import SPECIALISTS


def test_skill_create_read_index():
    assert skills.index() == "(no skills built yet)"
    msg = skills.create("Evaluate a SaaS niche",
                        "Use when judging a market opportunity",
                        "1. Size the demand...\n2. Check competition...")
    assert "created" in msg
    assert "Evaluate a SaaS niche" in skills.index()
    assert "Size the demand" in skills.read("Evaluate a SaaS niche")
    # same name updates instead of duplicating
    skills.create("Evaluate a SaaS niche", "v2", "Better method.")
    assert skills.count() == 1
    assert "No skill named" in skills.read("nonexistent")


def test_prompt_update_and_restore(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROMPTS_DIR", tmp_path / "prompts")
    config.PROMPTS_DIR.mkdir()
    (config.PROMPTS_DIR / "argus.md").write_text("# Argus\n\nOriginal mind.\n")

    tools._update_prompt("argus", "# Argus\n\nNew mind.", "testing")
    assert "New mind" in (config.PROMPTS_DIR / "argus.md").read_text()

    msg = tools._restore_prompt("argus")
    assert "restored" in msg
    restored = (config.PROMPTS_DIR / "argus.md").read_text()
    assert "Original mind" in restored
    assert "update reason" not in restored


def test_email_disabled_without_config(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert "not configured" in tools._send_email("a@b.c", "s", "b")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.delenv("OLYMPUS_EMAIL_ALLOWLIST", raising=False)
    assert "allowlist" in tools._send_email("a@b.c", "s", "b").lower()
    monkeypatch.setenv("OLYMPUS_EMAIL_ALLOWLIST", "x@y.z")
    assert "not in the recipient allowlist" in tools._send_email("a@b.c", "s", "b")


def test_webhook_requires_configured_name(monkeypatch):
    monkeypatch.delenv("OLYMPUS_WEBHOOKS", raising=False)
    assert "no webhook named" in tools._call_webhook("poster", {"x": 1})
    monkeypatch.setenv("OLYMPUS_WEBHOOKS", "poster=https://example.com/hook")
    assert tools._parse_webhooks() == {"poster": "https://example.com/hook"}


def test_specialist_loadouts():
    heph = SPECIALISTS["hephaestus"]
    names = [d.get("name") for d in heph.tool_defs("anthropic")]
    assert "code_execution" in names          # sandbox on Claude
    assert "read_skill" in names              # every specialist sees skills
    names_oa = [d.get("name") for d in heph.tool_defs("openai")]
    assert "code_execution" not in names_oa   # not on other providers

    metis = SPECIALISTS["metis"]
    assert "create_skill" in [d.get("name") for d in metis.tool_defs()]

    prom = SPECIALISTS["prometheus"]
    prom_names = [d.get("name") for d in prom.tool_defs()]
    assert {"run_benchmark", "restore_prompt"} <= set(prom_names)


def test_skill_index_injected_into_system_prompt():
    skills.create("Test skill", "for testing", "Do the thing.")
    assert "Test skill" in SPECIALISTS["plutus"].system_prompt()
