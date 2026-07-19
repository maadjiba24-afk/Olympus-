"""Three levers that strengthen the 13 specialists: sharpened prompts (aligned
to the benchmark), per-specialist model role + effort (data-driven tiering), and
output contracts. See docs / CHANGELOG.
"""

import pytest

from olympus import config, contracts
from olympus.specialists import SPECIALISTS


# --- lever 1: prompts sharpened toward the benchmark behaviors -------------

# (specialist, a stable phrase the sharpening added) — anchors, not full prose.
_ANCHORS = {
    "plutus": "break-even",
    "aegis": "incident response",
    "chronos": "rotation",
    "chiron": "smallest next action",
    "peitho": "paste-ready",
    "argus": "kill-criterion",
    "mnemosyne": "untrusted",
}


@pytest.mark.parametrize("key,phrase", _ANCHORS.items())
def test_prompts_have_benchmark_aligned_guidance(key, phrase):
    assert phrase in SPECIALISTS[key].system_prompt().lower()


# --- lever 2: per-specialist model role + effort (data-driven tiering) -----

def test_role_and_effort_fields_default_safely():
    for key, spec in SPECIALISTS.items():
        assert spec.role in ("reasoning", "coding", "verify")
        assert spec.effort  # non-empty; default "high" preserves behavior


def test_run_defaults_to_specialist_configured_effort(monkeypatch):
    # run()/run_counted() without an explicit effort must use the specialist's
    # own .effort, not a hard-coded 'high' — else one-shot routines (scan/audit/
    # learn) ignore a specialist tuned to a cheaper tier.
    from olympus import backend, specialists
    captured = {}
    monkeypatch.setattr(backend, "run_agent_counted",
                        lambda s, sys, task, tools, mcp_servers=None, effort=None:
                        (captured.__setitem__("effort", effort), ("out", 0))[1])
    spec = specialists.Specialist(key="plutus", name="P", title="t",
                                  description="d", effort="low")
    spec.run("do it", settings=config.Settings(provider="anthropic",
                                                model="m", api_key="k"))
    assert captured["effort"] == "low"                     # used self.effort
    spec.run("again", settings=config.Settings(provider="anthropic",
                                               model="m", api_key="k"), effort="high")
    assert captured["effort"] == "high"                    # explicit override wins


def test_hephaestus_routes_to_coding():
    assert SPECIALISTS["hephaestus"].role == "coding"
    assert config.specialist_role("hephaestus") == "coding"
    assert SPECIALISTS["plutus"].role == "reasoning"


def test_specialist_role_is_registry_driven():
    assert config.specialist_role("hephaestus") == "coding"
    assert config.specialist_role("does-not-exist") == "reasoning"  # safe fallback


def test_single_model_pool_routes_everyone_to_primary():
    # No multi-model pool → tiering is a no-op, every specialist gets the one model.
    s = config.Settings(provider="anthropic", model="claude-x", api_key="k")
    pool = config.ModelPool.of(s)
    for key in SPECIALISTS:
        assert pool.for_specialist(key) is pool.primary()


# --- lever 3: output contracts attached (enforced by default) --------------

def test_contracts_are_on_by_default(monkeypatch):
    """ADR 0005 hardening: enforcement never ships dormant. The kill switch
    still works."""
    monkeypatch.delenv("OLYMPUS_CONTRACTS", raising=False)
    assert config.contracts_enabled() is True      # default: enforcing
    monkeypatch.setenv("OLYMPUS_CONTRACTS", "off")
    assert config.contracts_enabled() is False


def test_iris_length_contract():
    c = SPECIALISTS["iris"].contract
    assert c is not None and c.max_chars == 8000
    assert contracts.check("ok", c).ok
    assert not contracts.check("x" * 9000, c).ok


def test_argus_tool_call_contract():
    c = SPECIALISTS["argus"].contract
    assert c is not None and c.max_tool_calls == 24
    assert contracts.check("done", c, tool_calls=5).ok
    assert not contracts.check("done", c, tool_calls=30).ok
