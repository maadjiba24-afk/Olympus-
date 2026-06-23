"""Validate the reliability-gate harness logic against a mocked model.

The *live* gate (real Claude calls on 3 prompts) is operator-run and needs a
key; this proves the script's record→replay→report wiring and its exit codes
without spending anything.
"""

import importlib.util
import sys
from pathlib import Path

from olympus import replaygate, usage

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "reliability_gate", ROOT / "scripts" / "reliability_gate.py")
rg = importlib.util.module_from_spec(_spec)
sys.modules["reliability_gate"] = rg
_spec.loader.exec_module(rg)


def _result(run_id, ok=True, skipped=False):
    return {"prompt": "p", "completed": ok, "replayable": ok,
            "decisions": 2 if ok else 0, "run_id": run_id,
            "detail": "replayed byte-identically" if ok else "x",
            "skipped": skipped}


def test_gate_met_when_all_pass(monkeypatch, capsys):
    monkeypatch.setattr(rg, "_DEFAULT_CAP", "5.00")
    monkeypatch.setattr(usage, "today_spend", lambda: 1.23)
    monkeypatch.setattr(replaygate, "run_exit_check",
                        lambda prompts=None: (True, [_result("a"), _result("b"), _result("c")]))
    rc = rg.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RELIABILITY GATE MET" in out
    assert "run a" in out and "run b" in out and "run c" in out


def test_gate_not_met_on_genuine_failure(monkeypatch, capsys):
    monkeypatch.setattr(usage, "today_spend", lambda: 0.0)
    monkeypatch.setattr(replaygate, "run_exit_check",
                        lambda prompts=None: (False, [_result("a"), _result("b", ok=False), _result("c")]))
    rc = rg.main([])
    assert rc == 1
    assert "NOT MET" in capsys.readouterr().out


def test_provider_skip_is_inconclusive_not_failure(monkeypatch, capsys):
    monkeypatch.setattr(usage, "today_spend", lambda: 0.0)
    skips = [_result("a", ok=False, skipped=True)] * 3
    monkeypatch.setattr(replaygate, "run_exit_check",
                        lambda prompts=None: (False, skips))
    rc = rg.main([])
    assert rc == 0                                  # inconclusive, not a failure
    assert "INCONCLUSIVE" in capsys.readouterr().out


def test_sets_a_spend_cap(monkeypatch):
    monkeypatch.delenv("OLYMPUS_DAILY_BUDGET", raising=False)
    monkeypatch.setattr(usage, "today_spend", lambda: 0.0)
    seen = {}
    def fake_run(prompts=None):
        import os
        seen["cap"] = os.environ.get("OLYMPUS_DAILY_BUDGET")
        return (True, [_result("a"), _result("b"), _result("c")])
    monkeypatch.setattr(replaygate, "run_exit_check", fake_run)
    rg.main([])
    assert seen["cap"] == "5.00"                     # a cap is enforced by default
