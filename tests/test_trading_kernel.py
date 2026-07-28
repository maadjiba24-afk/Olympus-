"""The safety kernel — Phase 10 completion gate 13.

The gate reads: *prove that no self-evolution mechanism can alter the safety
kernel, permissions, live-mode gates or hard risk limits.* A test asserting
that Olympus behaved itself during one run would not prove that. These tests
parse the source of every self-evolution module and assert the property
structurally, so a future edit that imports `ExecutionEngine` into the
hypothesis engine fails CI rather than shipping.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from olympus.trading import kernel as K
from olympus.trading.errors import ConfigurationError, SafetyKernelViolation


# --- gate 13: the structural proof ------------------------------------------

def test_no_evolution_module_can_reach_the_safety_kernel():
    """Gate 13. Parsed, not executed — auditing code by importing it would run
    the very code the audit exists to distrust."""
    findings = K.audit_evolution_modules()
    assert findings == [], (
        "self-evolution modules must have no import or call route into risk "
        "limits, kill-switch clearing, live-mode control, the vault or order "
        "submission:\n" + "\n".join(str(f) for f in findings))


def test_every_evolution_module_actually_exists_and_was_scanned():
    """A vacuous pass is the failure mode of a structural test: if the module
    names drift, `audit_evolution_modules` skips them and reports clean."""
    for module in K.EVOLUTION_MODULES:
        path = K._module_to_path(module)
        assert path.exists(), f"{module} is declared but absent, so it is unaudited"
        assert K.scan_module_for_kernel_access(module) == []


def test_the_scanner_catches_a_forbidden_import():
    """The detector must actually detect. Verified on synthetic source rather
    than by breaking a real module."""
    source = "from olympus.trading.execution import ExecutionEngine\n"
    findings = K.scan_module_for_kernel_access("olympus.trading.knowledge", source)
    assert findings and findings[0].kind == "import"
    assert "execution" in findings[0].detail


def test_the_scanner_catches_a_forbidden_call():
    source = "def go(sw):\n    sw.disengage(scope='global')\n"
    findings = K.scan_module_for_kernel_access("olympus.trading.drift", source)
    assert any(f.kind == "call" and "disengage" in f.detail for f in findings)


def test_the_scanner_catches_a_relative_import_of_the_vault():
    """`from ..vault import x` inside olympus.trading must resolve to olympus.vault."""
    source = "from ..vault import get\n"
    findings = K.scan_module_for_kernel_access("olympus.trading.lab", source)
    assert findings, "a relative import must not evade the scanner"


def test_assert_evolution_isolated_raises_when_a_route_exists():
    with pytest.raises(SafetyKernelViolation):
        K.assert_evolution_isolated(["olympus.trading.execution"])


# --- what the kernel covers -------------------------------------------------

def test_the_kernel_names_all_eleven_specified_components():
    expected = {"risk_policy_enforcement", "kill_switches", "credential_vault",
                "permission_system", "capability_boundaries",
                "order_authorisation", "live_mode_gates", "audit_ledger",
                "model_isolation", "human_approval", "deployment_signing"}
    assert {c.name for c in K.SAFETY_KERNEL} == expected


def test_every_kernel_component_states_why_it_is_protected():
    """A component whose rationale is empty cannot be reviewed for exemption."""
    for component in K.SAFETY_KERNEL:
        assert component.rationale.strip(), component.name
        assert component.modules


def test_kernel_membership_matches_on_a_dotted_boundary_not_a_prefix():
    assert K.is_kernel_module("olympus.trading.risk")
    assert K.is_kernel_module("olympus.trading.brokers.paper")
    assert not K.is_kernel_module("olympus.trading.riskier")
    assert not K.is_kernel_module("olympus.trading.strategy")


def test_assert_not_kernel_refuses_a_module_and_a_path_alike():
    for target in ("olympus.trading.risk", "olympus/trading/risk.py",
                   "olympus/trading/killswitch.py", "olympus/vault.py"):
        with pytest.raises(SafetyKernelViolation) as exc:
            K.assert_not_kernel(target)
        assert exc.value.details["component"]


def test_assert_not_kernel_permits_an_ordinary_module():
    assert K.assert_not_kernel("olympus/trading/strategy.py") == "olympus.trading.strategy"


# --- sealing ----------------------------------------------------------------

def test_a_seal_covers_every_declared_kernel_module():
    seal = K.seal()
    assert set(seal.modules) == set(K.KERNEL_MODULES)
    assert len(seal.digest) == 64


def test_a_seal_verifies_against_the_code_it_was_taken_from():
    assert K.verify_seal(K.seal()).intact is True


def test_verification_names_the_module_that_changed():
    """An aggregate mismatch alone would tell an operator something is wrong
    and nothing about where."""
    seal = K.seal()
    tampered = K.KernelSeal(
        digest="0" * 64,
        modules={**seal.modules, "olympus.trading.risk": "deadbeef"},
        sealed_at=seal.sealed_at)
    result = K.verify_seal(tampered)
    assert result.intact is False
    assert "olympus.trading.risk" in result.changed
    assert "olympus.trading.risk" in result.describe()


def test_verification_notices_a_kernel_module_that_disappeared():
    seal = K.seal()
    shrunk = K.KernelSeal(digest=seal.digest,
                          modules={k: v for k, v in seal.modules.items()
                                   if k != "olympus.trading.modes"})
    result = K.verify_seal(shrunk)
    assert result.intact is False
    assert "olympus.trading.modes" in result.added


def test_a_seal_round_trips_through_json():
    seal = K.seal(sealed_by="operator")
    assert K.KernelSeal.from_dict(seal.to_dict()).digest == seal.digest


# --- recommendation, never application --------------------------------------

def test_olympus_may_recommend_a_kernel_change():
    recommendation = K.propose_kernel_change(
        component="risk_policy_enforcement",
        summary="add a per-venue notional cap",
        rationale="observed concentration on one venue during the March episode",
        proposed_by="evolution-cycle",
        evidence=("outcome-journal:instrument:BINANCE:BTCUSDT",))
    assert recommendation.to_dict()["applied"] is False
    assert "not self-modifiable" in recommendation.to_dict()["note"]


def test_a_recommendation_must_name_a_real_component():
    with pytest.raises(ConfigurationError):
        K.propose_kernel_change(component="made_up", summary="x",
                                rationale="y", proposed_by="z")


def test_a_recommendation_must_name_its_proposer():
    with pytest.raises(ConfigurationError):
        K.propose_kernel_change(component="kill_switches", summary="x",
                                rationale="y", proposed_by="  ")


def test_no_module_can_apply_a_kernel_change():
    """The load-bearing absence. Adding an `apply_kernel_change` anywhere in
    the package fails here, which is the point of testing for absence rather
    than trusting a convention."""
    package = Path(K.__file__).resolve().parent
    forbidden = {"apply_kernel_change", "modify_kernel", "unseal",
                 "disable_kernel", "bypass_kernel"}
    offenders = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in forbidden:
                    offenders.append(f"{path.name}:{node.lineno} {node.name}")
    assert offenders == [], (
        "no function may exist that applies a change to the safety kernel: "
        + ", ".join(offenders))


def test_the_kernel_module_itself_is_in_the_kernel():
    """Otherwise the permission system could be edited to shorten the list."""
    assert K.is_kernel_module("olympus.trading.kernel")


def test_describe_kernel_lists_every_component():
    text = K.describe_kernel()
    for component in K.SAFETY_KERNEL:
        assert component.name in text
