"""Research isolation, and the nine things self-evolution must not be able to do.

> A Python-level import boundary alone is not adequate isolation for generated
> code.

So this file does not test the import hook. It tests the things an import hook
cannot give you, by **running generated code that tries** and asserting it
fails:

* a socket cannot be created — network namespace *and* seccomp, checked
  separately so one masking the other would show up
* a broker module cannot be imported
* the credential vault cannot be reached, and the environment it would read is
  not there to read
* a CPU limit, a memory limit and a wall clock each fire
* an approved dataset cannot be written, **including as uid 0** — which mode
  bits alone do not achieve, and the first version of this module got wrong
* the worker directory is gone afterwards
* inputs and results are signed, and a tampered payload is caught inside the run

The nine prohibitions are then checked three ways, because one way is a
coincidence: structurally by parsing the source, behaviourally by trying it in
a worker, and through `governance.authorise`, which is the call every
privileged path in the system goes through.

Every worker here is a real process, so this file is slower than the rest of
the suite. That is the cost of testing isolation rather than asserting it.

Hosts that cannot confine
-------------------------
Most of this file needs a host that can actually enforce the fifteen controls —
a cgroup with delegated `pids`/`memory`/`cpu` controllers and `mount(2)` for a
sized tmpfs. An unprivileged container has neither, and on such a host
`run_isolated` **refuses** rather than running weakened.

Those tests are therefore gated on `requires_confinement`, and the gate is not
a way of going quiet: `test_a_host_that_cannot_confine_refuses_rather_than_
running_weakened` runs *only* on such a host and asserts the refusal, so
whichever kind of machine this is, something real is checked. CI runs the file
both ways — see `.github/workflows/native-market-intelligence.yml`.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from olympus.trading.errors import ConfigurationError
from olympus.trading.native import isolation as ISO
from olympus.trading.native.challengers import ComputeBudget

SMALL = ComputeBudget(cpu_seconds=10, wall_clock_seconds=25, memory_mb=384,
                      disk_mb=8)


def _host_can_confine() -> bool:
    try:
        return ISO.host_support().can_run_generated_code
    except Exception:                                    # noqa: BLE001
        return False


CAN_CONFINE = _host_can_confine()

requires_confinement = pytest.mark.skipif(
    not CAN_CONFINE,
    reason="this host cannot enforce every required control; generated code is "
           "refused here rather than confined, which "
           "test_a_host_that_cannot_confine_refuses_rather_than_running_weakened "
           "asserts instead")


def run_source(source: str, *, name: str = "t", budget: ComputeBudget = SMALL,
               datasets=None, generated: bool = True) -> ISO.ResultManifest:
    spec = ISO.ExperimentSpec(experiment_id=name, source=source,
                              budget=budget, datasets=datasets or {},
                              generated=generated)
    return ISO.run_isolated(spec)


@pytest.fixture(scope="module")
def confinement() -> ISO.Confinement:
    """One probe, reused. Each worker is a real process."""
    if not CAN_CONFINE:
        pytest.skip("this host cannot confine generated code")
    return ISO.probe_confinement(budget=SMALL)


@pytest.fixture(scope="module")
def dataset() -> str:
    path = Path(tempfile.mkdtemp()) / "prices.json"
    path.write_text(json.dumps([100.0, 101.0, 99.5]))
    return str(path)


# ===========================================================================
# 1. the confinement itself
# ===========================================================================

@requires_confinement
def test_this_host_can_confine_generated_code(confinement):
    """If this fails the rest of the file is measuring nothing, so it is the
    first assertion and it names what is missing."""
    assert confinement.adequate_for_generated_code, (
        f"missing: {confinement.shortfall}")


@pytest.mark.skipif(CAN_CONFINE,
                    reason="this host can confine, so the refusal path is not "
                           "the one under test here")
def test_a_host_that_cannot_confine_refuses_rather_than_running_weakened():
    """The other half of the file, and the half that runs on a laptop.

    A host without cgroup delegation or `mount(2)` cannot bound a process tree.
    The correct behaviour there is not "run it and mark the result
    untrustworthy" — that would still have run it — but to refuse before the
    worker starts, naming every missing control.
    """
    support = ISO.host_support()
    assert support.can_run_generated_code is False
    assert support.missing, "an incapable host must name what it is missing"

    spec = ISO.ExperimentSpec(
        experiment_id="refusal-on-an-incapable-host",
        source="def run(inputs):\n    return {'ran': True}\n", generated=True)
    with pytest.raises(ISO.IsolationUnavailable) as raised:
        ISO.IsolatedWorker().run(spec)
    assert raised.value.details.get("missing")

    manifest = ISO.run_isolated(spec)
    assert manifest.outcome is ISO.Verdict.REFUSED
    assert manifest.verdict is ISO.Verdict.REFUSED
    assert manifest.trustworthy is False
    assert manifest.result == {}
    assert manifest.destruction["workdir"] == "(never created)"
    assert "confine" in manifest.stderr.lower()


@requires_confinement
def test_every_mechanism_reports_how_it_was_established(confinement):
    for state in confinement.states:
        assert state.basis in ("observed", "asserted")
        assert state.detail.strip()
    # The ones that matter most are observed from inside the worker, not
    # asserted by the parent that asked for them.
    for mechanism in (ISO.Mechanism.NETWORK_NAMESPACE, ISO.Mechanism.CPU_LIMIT,
                      ISO.Mechanism.MEMORY_LIMIT,
                      ISO.Mechanism.SEPARATE_PROCESS):
        assert confinement[mechanism].basis == "observed", mechanism


@requires_confinement
def test_the_worker_is_a_different_process_in_a_different_namespace(confinement):
    process = confinement[ISO.Mechanism.SEPARATE_PROCESS]
    assert process.applied and str(os.getpid()) in process.detail
    network = confinement[ISO.Mechanism.NETWORK_NAMESPACE]
    assert network.applied
    assert "net:[" in network.detail


@requires_confinement
def test_the_network_namespace_is_judged_without_the_seccomp_filter(confinement):
    """Two mechanisms that are meant to fail independently must not share one
    observation. A silently failed `unshare` reading as success because
    seccomp also blocked the socket is exactly that failure."""
    detail = confinement[ISO.Mechanism.NETWORK_NAMESPACE].detail
    assert "interfaces" in detail
    assert "['lo']" in detail or "[]" in detail


@requires_confinement
def test_a_generated_experiment_cannot_open_a_socket():
    manifest = run_source("""
def run(inputs):
    import socket
    s = socket.socket()
    return {'created': True}
""", name="socket")
    assert manifest.verdict is ISO.Verdict.FAILED
    assert manifest.result == {}


@requires_confinement
def test_a_generated_experiment_cannot_reach_the_network():
    manifest = run_source("""
def run(inputs):
    import urllib.request
    return {'body': urllib.request.urlopen('http://1.1.1.1', timeout=3).read(16)}
""", name="http")
    assert manifest.verdict is ISO.Verdict.FAILED


@requires_confinement
def test_the_environment_is_rebuilt_from_an_allowlist_not_filtered():
    """A denylist forgets the variable somebody adds next month."""
    manifest = run_source("""
import os
def run(inputs):
    return {'env': sorted(os.environ)}
""", name="env")
    assert manifest.verdict is ISO.Verdict.COMPLETED
    leaked = (set(manifest.result["env"]) - set(ISO.ENV_ALLOWLIST)
              - set(ISO.ENV_SET_BY_PARENT) - {"LC_CTYPE"})
    assert not leaked, f"the worker inherited {sorted(leaked)}"
    assert not any("TOKEN" in name or "SECRET" in name or "KEY" in name
                   for name in manifest.result["env"])


@requires_confinement
def test_an_approved_dataset_cannot_be_written_even_as_uid_zero(dataset):
    """Mode bits do not bind uid 0 — root bypasses the DAC check. The
    guarantee is a read-only bind mount, and this asserts the guarantee rather
    than the mode."""
    manifest = run_source("""
import json, pathlib
def run(inputs):
    path = pathlib.Path(inputs['__datasets__']['prices.json'])
    contents = json.loads(path.read_text(encoding="utf-8"))
    try:
        path.write_text('[]')
        writable = True
    except OSError as error:
        writable = str(error)
    return {'n': len(contents), 'writable': writable}
""", name="ro", datasets={"prices.json": dataset})
    assert manifest.verdict is ISO.Verdict.COMPLETED
    assert manifest.result["n"] == 3
    assert manifest.result["writable"] is not True
    assert manifest.confinement.applied(ISO.Mechanism.READ_ONLY_INPUTS)


@requires_confinement
def test_a_cpu_limit_fires_and_is_reported_as_a_limit_not_a_failure():
    manifest = run_source("""
def run(inputs):
    while True:
        pass
""", name="cpu", budget=ComputeBudget(cpu_seconds=2, wall_clock_seconds=30,
                                      memory_mb=256))
    assert manifest.verdict is ISO.Verdict.LIMIT_EXCEEDED, (
        "a worker killed by its own limit was reported as a confinement "
        "failure, which reads as the limit being broken")
    assert manifest.wall_seconds < 25


@requires_confinement
def test_a_wall_clock_timeout_fires_when_the_process_sleeps():
    """CPU time does not advance while sleeping, so only the wall clock can
    end this — which is why both limits exist."""
    manifest = run_source("""
import time
def run(inputs):
    time.sleep(600)
    return {}
""", name="sleep", budget=ComputeBudget(cpu_seconds=3, wall_clock_seconds=4,
                                        memory_mb=256))
    assert manifest.verdict is ISO.Verdict.TIMED_OUT
    assert manifest.wall_seconds < 30


@requires_confinement
def test_a_memory_limit_bounds_an_allocation():
    manifest = run_source("""
def run(inputs):
    return {'n': len(bytearray(600 * 1024 * 1024))}
""", name="mem", budget=ComputeBudget(cpu_seconds=8, wall_clock_seconds=25,
                                      memory_mb=128))
    assert manifest.verdict is not ISO.Verdict.COMPLETED
    assert manifest.result == {}


@requires_confinement
def test_the_worker_directory_is_destroyed_afterwards():
    manifest = run_source("def run(inputs):\n    return {'ok': True}\n",
                          name="destroy")
    assert manifest.destruction["workdir_removed"] is True
    assert not Path(manifest.destruction["workdir"]).exists()
    assert manifest.destruction["error"] == ""


def test_a_worker_runs_once():
    """Single-use, whether or not the first attempt was allowed to start.

    A worker that could run twice would let a second experiment inherit
    whatever the first left behind — and that is true of a worker whose first
    run was *refused* too, so the flag is set before the host is consulted and
    this test does not need a host that can confine.
    """
    spec = ISO.ExperimentSpec(experiment_id="reuse", budget=SMALL,
                              source="def run(inputs):\n    return {}\n")
    worker = ISO.IsolatedWorker()
    try:
        worker.run(spec)
    except ISO.IsolationUnavailable:
        pass                     # this host refuses generated code; still used
    with pytest.raises(ConfigurationError):
        worker.run(spec)


# ===========================================================================
# 2. signing
# ===========================================================================

def test_inputs_are_signed_and_the_signature_covers_the_dataset(dataset):
    spec = ISO.ExperimentSpec(experiment_id="sign", budget=SMALL,
                              source="def run(inputs):\n    return {}\n",
                              datasets={"prices.json": dataset})
    signed = ISO.sign_inputs(spec)
    assert signed.signed and signed.verify()
    # Changing the dataset changes the digest, so the old signature no longer
    # describes what would run.
    Path(dataset).write_text(json.dumps([1.0, 2.0]))
    assert ISO.digest_of(spec.payload()) != signed.digest
    Path(dataset).write_text(json.dumps([100.0, 101.0, 99.5]))


@requires_confinement
def test_a_result_manifest_is_signed_by_the_parent_not_the_worker():
    manifest = run_source("def run(inputs):\n    return {'v': 1}\n", name="sig")
    assert manifest.signed and manifest.verify()
    # A worker holding a signing key could sign a result it invented, so the
    # variables witness reads for one are not on the allowlist. `PYTHONHASHSEED`
    # is, and is not a secret — which is why this checks the actual names
    # rather than matching on the substring "SEED".
    assert not any(name.startswith("OLYMPUS_") for name in ISO.ENV_ALLOWLIST)
    for secret in ("OLYMPUS_SIGNING_SEED", "OLYMPUS_SIGNING_SEED_FILE",
                   "OLYMPUS_PINNED_PUBKEY"):
        assert secret not in ISO.ENV_ALLOWLIST


@requires_confinement
def test_a_tampered_payload_is_caught_inside_the_run():
    spec = ISO.ExperimentSpec(experiment_id="tamper", budget=SMALL,
                              source="def run(inputs):\n    return {}\n")
    signed = ISO.sign_inputs(spec)
    forged = ISO.SignedInputs(payload={**signed.payload, "entrypoint": "evil:go"},
                              digest=signed.digest,
                              signature=signed.signature)
    assert forged.verify() is False
    with pytest.raises(ConfigurationError):
        ISO.IsolatedWorker().run(spec, signed=forged)


@requires_confinement
def test_a_result_produced_under_failed_confinement_is_not_trustworthy():
    """`trustworthy` and `verdict` are computed, and neither is settable."""
    manifest = run_source("def run(inputs):\n    return {'v': 1}\n", name="trust")
    assert manifest.trustworthy
    broken = ISO.ResultManifest(
        experiment_id="x", outcome=ISO.Verdict.COMPLETED,
        started_at=manifest.started_at, finished_at=manifest.finished_at,
        confinement=ISO.Confinement(), input_digest="d",
        result={"looks": "fine"}, destruction={"workdir_removed": True})
    # The caller asked for COMPLETED and got CONFINEMENT_FAILED, because the
    # verdict is derived from the confinement rather than accepted from the
    # constructor.
    assert broken.outcome is ISO.Verdict.COMPLETED
    assert broken.verdict is ISO.Verdict.CONFINEMENT_FAILED
    assert broken.trustworthy is False
    assert broken.confinement.shortfall
    assert "verdict is confinement_failed" in broken.untrustworthy_because


def test_a_spec_supplies_exactly_one_of_source_and_entrypoint():
    with pytest.raises(ConfigurationError):
        ISO.ExperimentSpec(experiment_id="both", source="x", entrypoint="m:f")
    with pytest.raises(ConfigurationError):
        ISO.ExperimentSpec(experiment_id="neither")


def test_a_declared_dataset_that_does_not_exist_is_refused():
    with pytest.raises(ConfigurationError):
        ISO.ExperimentSpec(experiment_id="ghost",
                           source="def run(inputs):\n    return {}\n",
                           datasets={"missing.json": "/nowhere/missing.json"})


# ===========================================================================
# 3. the nine prohibitions — structurally
# ===========================================================================

NATIVE_EVOLUTION_MODULES = (
    "olympus.trading.native.evidence",
    "olympus.trading.native.challengers",
    "olympus.trading.native.isolation",
    "olympus.trading.native.promotion",
    "olympus.trading.native.improvement",
)


def test_no_native_evolution_module_reaches_the_safety_kernel():
    from olympus.trading import kernel
    findings = kernel.audit_evolution_modules(list(NATIVE_EVOLUTION_MODULES))
    assert findings == [], [f.to_dict() if hasattr(f, "to_dict") else f
                            for f in findings]


def test_the_whole_evolution_surface_is_registered_and_clean():
    from olympus.trading import kernel
    registered = set(kernel.EVOLUTION_MODULES)
    for module in NATIVE_EVOLUTION_MODULES:
        assert module in registered, f"{module} is not audited"
    assert kernel.audit_evolution_modules() == []


@pytest.mark.parametrize("module", NATIVE_EVOLUTION_MODULES)
def test_no_native_evolution_module_imports_a_forbidden_module(module):
    import ast
    import importlib

    source = Path(importlib.import_module(module).__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"olympus.trading.execution", "olympus.trading.brokers",
                 "olympus.trading.oms", "olympus.vault",
                 "olympus.trading.modes", "olympus.trading.killswitch"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.ImportFrom) and node.level:
            base = "olympus.trading.native" if node.level == 1 else "olympus.trading"
            imported.add(f"{base}.{node.module}" if node.module else base)
    assert not (imported & forbidden), sorted(imported & forbidden)


# ===========================================================================
# 4. the nine prohibitions — behaviourally, in a worker
# ===========================================================================

PROHIBITED_ATTEMPTS = {
    "import_broker_execution": """
def run(inputs):
    from olympus.trading import execution
    return {'reached': 'execution'}
""",
    "import_broker_package": """
def run(inputs):
    from olympus.trading import brokers
    return {'reached': 'brokers'}
""",
    "read_credential_vault": """
def run(inputs):
    from olympus import vault
    return {'reached': 'vault'}
""",
    "change_live_mode": """
def run(inputs):
    from olympus.trading import modes
    return {'reached': 'modes'}
""",
    "modify_risk_limits": """
def run(inputs):
    from olympus.trading import risk
    return {'reached': 'risk'}
""",
    "clear_kill_switch": """
def run(inputs):
    from olympus.trading import killswitch
    return {'reached': 'killswitch'}
""",
    "submit_an_order_over_the_network": """
def run(inputs):
    import socket
    s = socket.create_connection(('127.0.0.1', 9), timeout=1)
    s.sendall(b'BUY 100')
    return {'sent': True}
""",
}


@requires_confinement
@pytest.mark.parametrize("name", sorted(PROHIBITED_ATTEMPTS))
def test_generated_code_attempting_a_prohibited_action_does_not_succeed(name):
    manifest = run_source(PROHIBITED_ATTEMPTS[name], name=name)
    assert manifest.verdict is not ISO.Verdict.COMPLETED, (
        f"a worker succeeded at {name}")
    assert manifest.result == {}


@requires_confinement
def test_the_blocked_import_layer_is_the_weakest_and_is_documented_as_such():
    """It is defence in depth and the module says so. Asserted because the
    ordering of the four layers is a claim about which one is load-bearing."""
    doc = ISO.__doc__ or ""
    assert "weakest layer" in doc
    assert "empty network namespace" in doc
    # And the check still fires, because a documented-as-weak layer that did
    # not work would be no layer at all.
    manifest = run_source(PROHIBITED_ATTEMPTS["import_broker_execution"],
                          name="weak")
    assert manifest.verdict is ISO.Verdict.FAILED


# ===========================================================================
# 5. the nine prohibitions — through governance
# ===========================================================================

AUTONOMOUS_MUST_BE_REFUSED = (
    "promote_to_live", "promote_capability", "deploy_code", "merge_code",
    "enable_live_trading", "clear_kill_switch", "modify_risk_limits",
    "modify_safety_kernel", "expand_permissions", "disable_audit",
    "rotate_secrets",
)


@pytest.mark.parametrize("action", AUTONOMOUS_MUST_BE_REFUSED)
def test_an_autonomous_actor_is_refused_every_human_only_action(action):
    from olympus.trading.errors import GovernanceViolation
    from olympus.trading.governance import Action, Actor, authorise

    with pytest.raises(GovernanceViolation):
        authorise(Action(action), Actor.autonomous("evolution-engine"),
                  subject="native-forecaster")


@pytest.mark.parametrize("action", ("conceal_result", "rewrite_audit_history"))
def test_nobody_may_conceal_a_result_or_rewrite_history(action):
    """Prohibited for *every* actor kind, operator included. An in-system path
    that makes concealment routine is what must not exist."""
    from olympus.trading.errors import GovernanceViolation
    from olympus.trading.governance import Action, Actor, authorise

    for actor in (Actor.autonomous("engine"), Actor.system("cron"),
                  Actor.operator("alice", token="tok")):
        with pytest.raises(GovernanceViolation):
            authorise(Action(action), actor, subject="experiment-1")


def test_no_module_in_the_package_can_apply_a_kernel_change():
    from olympus.trading import kernel
    assert not hasattr(kernel, "apply_kernel_change")
    recommendation = kernel.propose_kernel_change(
        component="risk_policy_enforcement",
        summary="widen the concentration limit",
        rationale="a challenger performs better with a larger position",
        proposed_by="evolution-engine")
    # A document, not an action. Nothing on it applies anything.
    assert not any(name.startswith("apply") or name.startswith("commit")
                   for name in dir(recommendation))


@requires_confinement
def test_the_isolation_report_states_what_this_host_cannot_do():
    report = ISO.isolation_report()
    assert report["blocked_syscalls"] == list(ISO.BLOCKED_SYSCALLS)
    assert set(report["required_for_generated_code"]) == {
        m.value for m in ISO.REQUIRED_FOR_GENERATED_CODE}
    shortfall = report["confinement"]["shortfall"]
    assert shortfall == [], f"this host cannot confine generated code: {shortfall}"
    assert report["can_run_generated_code"] is True
    # The process limit is a cgroup fact, and the detail says so — including
    # why the obvious-looking alternative is not one.
    detail = next(s["detail"] for s in report["confinement"]["states"]
                  if s["mechanism"] == "process_limit")
    assert "pids.max" in detail
    assert "per real UID" in detail        # why RLIMIT_NPROC is not counted
    assert "member of cgroup" in detail    # and that membership was observed
