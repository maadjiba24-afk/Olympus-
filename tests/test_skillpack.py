"""agentskills.io interop: parse, import, export, round-trip."""

from olympus import skillpack, skills


SAMPLE = """---
name: Cold Email Outreach
description: Write high-conversion cold emails
specialist: peitho
---
# Cold Email Outreach

Steps:
1. Lead with the prospect's problem.
2. One clear ask.
"""


def test_parse_skill_md():
    p = skillpack.parse_skill_md(SAMPLE)
    assert p["name"] == "Cold Email Outreach"
    assert p["description"].startswith("Write high-conversion")
    assert p["specialist"] == "peitho"
    assert "One clear ask" in p["instructions"]


def test_parse_without_frontmatter_falls_back_to_heading():
    p = skillpack.parse_skill_md("# Just A Heading\n\nbody text here")
    assert p["name"] == "Just A Heading"


def test_import_file_creates_skill(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text(SAMPLE, encoding="utf-8")
    msg = skillpack.import_file(str(f))
    assert "Cold Email Outreach" in msg
    assert "One clear ask" in skills.read("Cold Email Outreach")
    # imported as permanent (not provisional) by default
    assert "<!-- provisional -->" not in skills.read("Cold Email Outreach")


def test_import_provisional(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text(SAMPLE, encoding="utf-8")
    skillpack.import_file(str(f), provisional=True)
    assert "<!-- provisional -->" in skills.read("Cold Email Outreach")


def test_import_dir_scans_tree(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "SKILL.md").write_text(SAMPLE, encoding="utf-8")
    msgs = skillpack.import_dir(str(tmp_path))
    assert len(msgs) == 1


def test_export_roundtrip(tmp_path):
    skills.create("Pricing Anchor", "Anchor on value",
                  "Tier at 19/49/99.", specialist="plutus")
    out = skillpack.export("Pricing Anchor", str(tmp_path))
    text = open(out, encoding="utf-8").read()
    assert text.startswith("---")
    parsed = skillpack.parse_skill_md(text)
    assert parsed["name"] == "Pricing Anchor"
    assert parsed["specialist"] == "plutus"
    assert "Tier at 19/49/99." in parsed["instructions"]


def test_export_unknown_skill_raises():
    import pytest
    with pytest.raises(ValueError):
        skillpack.to_skill_md("does-not-exist")
