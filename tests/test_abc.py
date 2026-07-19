"""Agent Behavioral Contracts (ABC): C=(Preconditions, Invariants, Governance,
Recovery) as runtime-enforced YAML wrapping the approval spine, autonomy dial,
and Aletheia. Proves every existing invariant is a contract, and that violations
block execution and trigger Recovery."""

import pytest

from olympus import actions, behavioral_contracts as abc
from olympus import builtin_actions  # noqa: F401


# --- YAML ↔ embedded fallback stay in sync -------------------------------

def test_yaml_matches_embedded_fallback():
    # PyYAML is an OPTIONAL parser (ABC falls back to the embedded dict without
    # it — and the canonical CI/prod install has no yaml), so this drift guard
    # only runs where a parser is present. The embedded dict is authoritative at
    # runtime; this proves the human-readable YAML mirror hasn't diverged.
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(abc._yaml_path().read_text(encoding="utf-8"))
    assert data["contracts"] == abc._DEFAULT_CONTRACTS


def test_all_default_contracts_load_and_are_valid():
    reg = abc.load()
    assert {"action_execution", "specialist_output", "tool_loadout"} <= set(reg)
    for c in reg.values():
        assert c.recovery in abc.RECOVERIES
        for kind in abc.CLAUSES:
            for pname in c.clause(kind):
                assert pname in abc._PREDICATES     # no vacuous clause


def test_load_rejects_unknown_predicate():
    with pytest.raises(ValueError):
        abc._coerce("bad", {"operation": "x", "invariants": ["nope_missing"]})


def test_load_rejects_unknown_recovery():
    with pytest.raises(ValueError):
        abc._coerce("bad", {"operation": "x", "recovery": "explode"})


# --- the existing invariants ARE contracts -------------------------------

def test_every_governance_operation_has_a_contract():
    reg = abc.load()
    for op in ("action.execute", "specialist.output", "run.loadout"):
        assert abc.contracts_for(op, reg), f"no contract for {op}"


# --- predicate behavior ---------------------------------------------------

def test_irreversible_requires_real_approval():
    p = abc._PREDICATES["approval_present_if_irreversible"]
    # trivial: always fine
    ok, _ = p({"risk_class": actions.TRIVIAL})
    assert ok
    # irreversible without a real approval timestamp: blocked
    ok, why = p({"risk_class": actions.IRREVERSIBLE,
                 "status": actions.APPROVED, "approved_at": None})
    assert not ok and "approval" in why
    # irreversible WITH a genuine approval: fine
    ok, _ = p({"risk_class": actions.IRREVERSIBLE,
               "status": actions.APPROVED, "approved_at": 123.0})
    assert ok


def test_autonomy_sufficient_and_cap():
    suff = abc._PREDICATES["autonomy_sufficient"]
    assert suff({"approved_at": 1.0})[0]                       # approval covers
    assert suff({"can_auto": True})[0]
    assert not suff({"approved_at": None, "can_auto": False,
                     "eff_level": 1, "min_level": 3, "risk_class": "x"})[0]
    cap = abc._PREDICATES["within_autonomy_cap"]
    assert cap({"eff_level": 2, "autonomy_cap": 4})[0]
    assert not cap({"eff_level": 4, "autonomy_cap": 1})[0]


def test_aletheia_verified_blocks_rejection():
    p = abc._PREDICATES["aletheia_verified"]
    assert p({})[0]                              # no verdict → permissive
    assert p({"verify_verdict": "ok"})[0]
    assert not p({"verify_verdict": "reject"})[0]
    assert not p({"verify_verdict": "hallucination"})[0]


def test_output_contract_invariant():
    from olympus import contracts
    p = abc._PREDICATES["output_contract_satisfied"]
    good = contracts.ContractResult(ok=True)
    bad = contracts.ContractResult(ok=False, violations=("too long",))
    assert p({"contract_result": good})[0]
    ok, why = p({"contract_result": bad})
    assert not ok and "too long" in why


def test_capability_separation_predicate():
    from olympus import security
    p = abc._PREDICATES["no_action_tool_in_ingesting_run"]
    assert p({"ingests_external": False})[0]
    action_tool = sorted(security.ACTION_TOOLS)[0]
    ok, why = p({"ingests_external": True,
                 "tool_names": [action_tool, "web_search"]})
    assert not ok and action_tool in why


# --- evaluate / check / enforce ------------------------------------------

def test_evaluate_first_failing_clause_wins():
    c = abc.Contract(name="c", operation="op", recovery=abc.BLOCK,
                     preconditions=("scope_granted",),
                     invariants=("risk_class_known",))
    v = abc.evaluate(c, {"scope": "x", "granted_scopes": set(),
                         "risk_class": actions.TRIVIAL})
    assert not v.ok and v.clause == "preconditions"


def test_check_fails_open_on_engine_error(monkeypatch):
    def boom(ctx):
        raise RuntimeError("predicate blew up")
    monkeypatch.setitem(abc._PREDICATES, "risk_class_known", boom)
    v = abc.check("action.execute", {"risk_class": "whatever"})
    assert v.ok                                  # engine error → fail OPEN

def test_enforce_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ABC", "off")
    # A clearly-violating context does not raise when ABC is disabled.
    v = abc.enforce("action.execute",
                    {"risk_class": actions.IRREVERSIBLE,
                     "status": actions.APPROVED, "approved_at": None,
                     "scope": "", "granted_scopes": set(),
                     "eff_level": 1, "autonomy_cap": 4, "min_level": 99,
                     "can_auto": False})
    assert v.ok


def test_enforce_raises_on_blocking_violation(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ABC", "on")
    with pytest.raises(abc.ContractViolation) as ei:
        abc.enforce("action.execute",
                    {"risk_class": actions.IRREVERSIBLE,
                     "status": actions.APPROVED, "approved_at": None,
                     "scope": "", "granted_scopes": set(),
                     "eff_level": 1, "autonomy_cap": 4, "min_level": 99,
                     "can_auto": False})
    assert ei.value.recovery == abc.HOLD
    assert ei.value.contract == "action_execution"


# --- wired enforcement at the action execution chokepoint ----------------

@pytest.fixture()
def reg(monkeypatch):
    monkeypatch.setenv("OLYMPUS_ABC", "on")
    monkeypatch.setattr(actions, "_REGISTRY", {})
    executed = {"count": 0}
    actions.register(actions.ActionType(
        name="t_send", risk_class=actions.IRREVERSIBLE, scope="email",
        preview=lambda p: "send",
        execute=lambda p: (executed.__setitem__("count", executed["count"] + 1)
                           or {"sent": True})))
    return executed


def test_contract_blocks_irreversible_bypass_and_recovers(reg):
    """A code path that reaches _execute with an APPROVED-but-never-really-
    approved irreversible action is blocked by the contract and HELD."""
    actions.grant_scope("u", "email")
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    a.status = actions.APPROVED       # forged status, no genuine approval
    a.approved_at = None
    done = actions._execute(a)
    assert reg["count"] == 0                       # did NOT run
    assert done.status == actions.PREPARED         # Recovery: HOLD → await human
    assert "behavioral contract" in done.error


def test_genuine_approval_passes_the_contract(reg):
    actions.grant_scope("u", "email")
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    done = actions.approve("u", a.id)              # real approval sets approved_at
    assert done.status == actions.EXECUTED
    assert reg["count"] == 1


def test_kill_switch_lets_bypass_through(reg, monkeypatch):
    """With ABC off, the contract layer is inert (the primary spine still ran
    approve()); proves the enforcement — not some other guard — is what blocked."""
    monkeypatch.setenv("OLYMPUS_ABC", "off")
    actions.grant_scope("u", "email")
    a = actions.prepare("u", "t_send", {"to": "x@y.z"})
    a.status = actions.APPROVED
    a.approved_at = None
    done = actions._execute(a)
    assert done.status == actions.EXECUTED         # no ABC → bypass runs
    assert reg["count"] == 1
