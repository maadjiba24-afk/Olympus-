"""Per-specialist skill-index scoping — the fix for cross-specialist
contamination: a skill tagged for one specialist must not appear in another's
prompt, while untagged / 'all' skills stay global."""

from olympus import skills
from olympus.specialists import SPECIALISTS


def _seed():
    skills.create("Plutus Pricing", "value pricing", "anchor on value",
                  specialist="plutus")
    skills.create("Aegis Hardening", "harden systems", "patch fast",
                  specialist="aegis")
    skills.create("Global Honesty", "always cite", "cite your sources")  # untagged
    skills.create("Everyone Skill", "shared", "be concise", specialist="all")


def test_default_index_shows_everything():
    _seed()
    idx = skills.index()                       # human view / `olympus skills`
    for name in ("Plutus Pricing", "Aegis Hardening", "Global Honesty",
                 "Everyone Skill"):
        assert name in idx


def test_scoped_index_includes_own_and_global_only():
    _seed()
    plutus = skills.index("plutus")
    assert "Plutus Pricing" in plutus          # own
    assert "Global Honesty" in plutus          # untagged → global
    assert "Everyone Skill" in plutus          # 'all' → global
    assert "Aegis Hardening" not in plutus      # another specialist's → hidden


def test_scoped_index_excludes_other_specialist():
    _seed()
    aegis = skills.index("aegis")
    assert "Aegis Hardening" in aegis
    assert "Plutus Pricing" not in aegis        # the contamination that's now fixed


def test_visible_to_rules():
    assert skills._visible_to(None, "plutus") is True        # untagged = global
    assert skills._visible_to("all", "plutus") is True       # 'all' = global
    assert skills._visible_to("plutus", "plutus") is True    # own
    assert skills._visible_to("aegis", "plutus") is False    # other
    assert skills._visible_to("aegis", None) is True         # human view shows all


def test_specialist_system_prompt_is_scoped():
    _seed()
    plutus_prompt = SPECIALISTS["plutus"].system_prompt()
    aegis_prompt = SPECIALISTS["aegis"].system_prompt()
    # Plutus sees its own + global, never Aegis's tagged skill — and vice versa.
    assert "Plutus Pricing" in plutus_prompt
    assert "Aegis Hardening" not in plutus_prompt
    assert "Aegis Hardening" in aegis_prompt
    assert "Plutus Pricing" not in aegis_prompt
    # Global skills reach both.
    assert "Everyone Skill" in plutus_prompt and "Everyone Skill" in aegis_prompt
