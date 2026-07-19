"""Milestone 0.4 — single (gated) prompt-write path.

The public `update_prompt` tool now ROUTES through `gate_prompt`: it measures a
before/after benchmark and rolls back on regression, and it REFUSES an agent
with no benchmark coverage instead of writing blind. The raw writer
`_apply_prompt` is internal — reachable only from inside `gate_prompt`. There is
no ungated public path that writes a prompt file.
"""

import pytest

from olympus import config, evals, orchestrator, tools

_S = config.Settings(provider="anthropic", model="x", api_key="k")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "plutus.md").write_text("OLD PROMPT\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROMPTS_DIR", d)
    self_dir = {"d": d}
    yield self_dir


def _prompt(tmp_path):
    return (config.PROMPTS_DIR / "plutus.md").read_text(encoding="utf-8").strip()


def test_update_prompt_tool_reverts_on_regression(tmp_path, monkeypatch):
    monkeypatch.setattr(evals, "ids_for", lambda specs: ["plutus-budget"])
    scores = iter([8.0, 6.0])                       # before, after — regression
    monkeypatch.setattr(evals, "run", lambda *a, **k: {"avg": next(scores)})
    out = tools._update_prompt("plutus", "NEW PROMPT", "risky")
    assert "revert" in out.lower()
    assert _prompt(tmp_path) == "OLD PROMPT"        # rolled back, not left regressed


def test_update_prompt_tool_keeps_on_improvement(tmp_path, monkeypatch):
    monkeypatch.setattr(evals, "ids_for", lambda specs: ["plutus-budget"])
    scores = iter([7.0, 8.0])                       # before, after — improvement
    monkeypatch.setattr(evals, "run", lambda *a, **k: {"avg": next(scores)})
    out = tools._update_prompt("plutus", "NEW PROMPT", "sharper")
    assert "kept" in out.lower()
    assert _prompt(tmp_path) == "NEW PROMPT"


def test_update_prompt_refuses_without_coverage_and_does_not_write(monkeypatch):
    # THE ungated path is gone: no coverage → refuse, and the file is untouched.
    monkeypatch.setattr(evals, "ids_for", lambda specs: [])            # no coverage
    ran = {"n": 0}
    monkeypatch.setattr(evals, "run",
                        lambda *a, **k: (ran.__setitem__("n", ran["n"] + 1),
                                         {"avg": 5.0})[1])
    out = tools._update_prompt("plutus", "SNEAKY UNMEASURED PROMPT", "bypass")
    assert "generate_benchmark" in out or "cannot benchmark-gate" in out.lower()
    assert _prompt(None) == "OLD PROMPT"            # never written
    assert ran["n"] == 0                            # never even measured


def test_update_prompt_routes_through_the_gate(monkeypatch):
    seen = {}
    def fake_gate(agent, new_prompt, reason, settings=None):
        seen.update(agent=agent, new_prompt=new_prompt, reason=reason)
        return "gated"
    monkeypatch.setattr(orchestrator, "gate_prompt", fake_gate)
    assert tools._update_prompt("plutus", "NEW", "why") == "gated"
    assert seen == {"agent": "plutus", "new_prompt": "NEW", "reason": "why"}


def test_apply_prompt_is_internal_not_a_public_tool():
    # The raw writer is not exposed as a callable tool; only update_prompt (gated)
    # and gate_prompt are. So no tool invocation reaches an unmeasured write.
    assert "update_prompt" in tools.HANDLERS
    assert "gate_prompt" in tools.HANDLERS
    assert "_apply_prompt" not in tools.HANDLERS
    assert "apply_prompt" not in tools.HANDLERS
    assert tools.HANDLERS["update_prompt"] is tools._update_prompt
