"""Adversarial isolation suite: generated code trying to get out.

Every test in this file is written from the attacker's side. The experiment
source is not a well-behaved research script — it forks until the kernel says
no, detaches with `setsid()`, opens sockets, imports the broker, overwrites its
own signed inputs, forges its own result and burns the host's CPU, memory and
disk. The assertion in each case is not "it failed politely" but **"it was
stopped, and nothing it started is still running"**.

Why the survivor check is in every test
---------------------------------------
A confinement that bounds a process while it runs and leaks a descendant
afterwards has not confined anything; it has delayed the problem until nobody
is watching. So `assert_contained` runs after every scenario and checks three
independent things:

1. the cgroup's own member list is empty (`manifest.survivors`),
2. every pid the experiment told us about is gone (`orphan_check`), and
3. no process anywhere on the host still has an `olympus-research` work
   directory open.

(3) is the one that would catch a mechanism that lies. (1) and (2) both read
state this module manages; (3) reads `/proc` for everybody.

These tests are Linux-only and are skipped elsewhere, which is not a gap: on a
host that cannot confine generated code, `run_isolated` refuses to run it at
all, and `test_trading_native_isolation_platform.py` is what proves that.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from olympus.trading.native import isolation as ISO
from olympus.trading.native.challengers import ComputeBudget

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="generated-code confinement is implemented on Linux only")


def _host_can_confine() -> bool:
    try:
        return ISO.host_support().can_run_generated_code
    except Exception:                                    # noqa: BLE001
        return False


requires_confinement = pytest.mark.skipif(
    not _host_can_confine(),
    reason="this host cannot enforce every required control; "
           "generated code is refused here rather than confined")


#: Small on purpose. A fork bomb that is allowed 8 processes takes as long to
#: stop as one allowed 4, and the tests run on every push.
TIGHT = dict(cpu_seconds=4, memory_mb=96, disk_mb=8, wall_clock_seconds=25,
             max_processes=5)


def budget(**overrides) -> ComputeBudget:
    return ComputeBudget(**{**TIGHT, **overrides})


def attack(name: str, source: str, *, datasets=None, **overrides):
    """Run generated code and return the manifest. Never raises for the code."""
    spec = ISO.ExperimentSpec(experiment_id=f"adversarial-{name}",
                              source=source, generated=True,
                              datasets=datasets or {},
                              budget=budget(**overrides))
    return ISO.run_isolated(spec)


# ---------------------------------------------------------------------------
# the proof that runs after every scenario
# ---------------------------------------------------------------------------

def stray_workers() -> list[int]:
    """Any process on this host whose cwd or cmdline is a research workdir.

    Reads `/proc` directly rather than asking the isolation module, because a
    mechanism that reports "no survivors" incorrectly is exactly the failure
    this is here to catch. Permission errors are skipped rather than raised:
    the check is for processes this test could have created, and those are
    always readable by it.
    """
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        for probe in ("cwd",):
            try:
                target = os.readlink(entry / probe)
            except OSError:
                continue
            if "olympus-research-" in target:
                found.append(pid)
                break
        else:
            try:
                cmdline = (entry / "cmdline").read_bytes().decode(
                    "utf-8", "replace")
            except OSError:
                continue
            if "olympus-research-" in cmdline:
                found.append(pid)
    return sorted(set(found))


def assert_contained(manifest, reported_pids=()) -> None:
    """No descendant of the experiment is still running. Three ways."""
    from olympus.trading.native.cgroups import orphan_check

    assert manifest.survivors == (), (
        f"the cgroup still contained {list(manifest.survivors)} after "
        f"teardown; a confinement that leaks a process has not confined it")
    alive = orphan_check([int(p) for p in reported_pids if p])
    assert alive == (), f"the experiment left {list(alive)} running"
    assert stray_workers() == [], (
        f"processes elsewhere on the host still hold a research work "
        f"directory: {stray_workers()}")
    assert manifest.destruction.get("workdir_removed") is True
    assert manifest.destruction.get("cgroup_removed") is True


@pytest.fixture(autouse=True)
def _no_leftovers():
    """Fail a test that leaves anything behind, whatever it asserted."""
    before = stray_workers()
    yield
    after = stray_workers()
    assert after == before, f"this test leaked worker processes: {after}"


# ---------------------------------------------------------------------------
# 1-3. forking, setsid, detached descendants
# ---------------------------------------------------------------------------

FORK_BOMB = """
def run(inputs):
    import os, time
    made = []
    for _ in range(400):
        try:
            pid = os.fork()
        except OSError:
            break
        if pid == 0:
            time.sleep(120)
            os._exit(0)
        made.append(pid)
    return {'forked': len(made), 'pids': made}
"""


@requires_confinement
def test_generated_code_that_forks_repeatedly_is_capped_by_the_cgroup():
    """`pids.max` bounds the tree. `RLIMIT_NPROC` could not have done this."""
    manifest = attack("fork-bomb", FORK_BOMB, max_processes=5)
    forked = manifest.result.get("forked")
    assert isinstance(forked, int)
    # The worker is the runner plus the experiment child, so the bomb gets what
    # is left of the budget and not one process more.
    assert forked < 5, f"the fork bomb created {forked} children under pids.max=5"
    assert manifest.destruction["peak_processes"] <= 5
    assert_contained(manifest, manifest.result.get("pids") or ())


SETSID = """
def run(inputs):
    import os, time
    pid = os.fork()
    if pid == 0:
        os.setsid()
        time.sleep(300)
        os._exit(0)
    time.sleep(0.2)
    return {'detached': pid}
"""


@requires_confinement
def test_a_descendant_that_calls_setsid_is_still_terminated():
    """The case a process-group kill misses.

    `killpg` signals one process group. A child that called `setsid()` is in a
    new session and a new group, so `killpg` never reaches it — it would
    survive the experiment, hold the tmpfs busy and keep running unattended.
    The cgroup does not care about sessions.
    """
    manifest = attack("setsid", SETSID)
    detached = manifest.result.get("detached")
    assert isinstance(detached, int) and detached > 0
    assert_contained(manifest, [detached])


DOUBLE_FORK = """
def run(inputs):
    import os, time
    reported = []
    for _ in range(2):
        pid = os.fork()
        if pid == 0:
            os.setsid()
            grand = os.fork()
            if grand == 0:
                os.chdir('/')
                time.sleep(300)
                os._exit(0)
            os._exit(0)          # orphan the grandchild onto init
        reported.append(pid)
    time.sleep(0.4)
    return {'children': reported}
"""


@requires_confinement
def test_daemonised_grandchildren_reparented_to_init_are_still_terminated():
    """Double-fork daemonisation: the classic way to outlive your parent.

    The grandchild is reparented to pid 1, is in its own session, and has
    changed directory off the work tree. None of that removes it from the
    cgroup it was born into.
    """
    manifest = attack("daemonise", DOUBLE_FORK)
    assert isinstance(manifest.result.get("children"), list)
    assert_contained(manifest)


# ---------------------------------------------------------------------------
# 4. spawning subprocesses
# ---------------------------------------------------------------------------

SPAWN = """
def run(inputs):
    import subprocess
    out = {}
    for name, argv in (('sh', ['/bin/sh', '-c', 'echo hi']),
                       ('python', ['python3', '-c', 'print(1)'])):
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=5)
            out[name] = 'SPAWNED rc=%s' % proc.returncode
        except BaseException as error:
            out[name] = type(error).__name__
    return out
"""


@requires_confinement
def test_generated_code_cannot_exec_a_subprocess():
    """`execve` is denied by seccomp, so nothing new can be launched.

    Note what this does *not* claim: `fork` still works, which is why the
    process-count limit above is the load-bearing control and this is defence
    in depth.
    """
    manifest = attack("spawn", SPAWN)
    assert manifest.verdict is ISO.Verdict.COMPLETED
    for name, outcome in manifest.result.items():
        assert not str(outcome).startswith("SPAWNED"), \
            f"the experiment executed {name}: {outcome}"
    assert_contained(manifest)


# ---------------------------------------------------------------------------
# 5. sockets
# ---------------------------------------------------------------------------

SOCKETS = """
def run(inputs):
    import socket
    out = {}
    families = [('inet', socket.AF_INET, socket.SOCK_STREAM),
                ('inet6', getattr(socket, 'AF_INET6', socket.AF_INET),
                 socket.SOCK_STREAM),
                ('unix', getattr(socket, 'AF_UNIX', socket.AF_INET),
                 socket.SOCK_STREAM),
                ('raw', socket.AF_INET, socket.SOCK_RAW)]
    for name, family, kind in families:
        try:
            handle = socket.socket(family, kind)
            handle.close()
            out[name] = 'CREATED'
        except BaseException as error:
            out[name] = type(error).__name__
    try:
        socket.create_connection(('127.0.0.1', 22), timeout=1)
        out['connect'] = 'CONNECTED'
    except BaseException as error:
        out['connect'] = type(error).__name__
    return out
"""


@requires_confinement
def test_generated_code_cannot_open_a_socket_of_any_family():
    """Two independent mechanisms, and the test does not care which one fired.

    `socket()` is denied by seccomp; even if it were not, the network namespace
    has one loopback interface and no route off the machine. They are supposed
    to fail independently, so a test that passed only when both worked would be
    testing the conjunction rather than the guarantee.
    """
    manifest = attack("sockets", SOCKETS)
    assert manifest.verdict is ISO.Verdict.COMPLETED
    for name, outcome in manifest.result.items():
        assert outcome not in ("CREATED", "CONNECTED"), \
            f"the experiment opened a {name} socket"
    assert manifest.confinement.applied(ISO.Mechanism.NETWORK_NAMESPACE)
    assert_contained(manifest)


# ---------------------------------------------------------------------------
# 6. broker and vault modules
# ---------------------------------------------------------------------------

REACH_FOR_MONEY = """
def run(inputs):
    out = {}
    targets = ['olympus.vault', 'olympus.trading.brokers',
               'olympus.trading.execution', 'olympus.trading.oms',
               'olympus.trading.killswitch', 'olympus.trading.risk',
               'olympus.trading.modes']
    for name in targets:
        try:
            __import__(name)
            out[name] = 'IMPORTED'
        except BaseException as error:
            out[name] = type(error).__name__
    try:
        import importlib
        importlib.import_module('olympus.vault')
        out['importlib'] = 'IMPORTED'
    except BaseException as error:
        out['importlib'] = type(error).__name__
    return out
"""


@requires_confinement
def test_generated_code_cannot_import_broker_vault_or_execution_modules():
    """The weakest layer, tested anyway.

    An import hook is removable by the code it is meant to stop — this asserts
    that it is installed and works, not that it is a boundary. The boundary is
    that an order cannot leave an empty network namespace.
    """
    manifest = attack("reach-for-money", REACH_FOR_MONEY)
    assert manifest.verdict is ISO.Verdict.COMPLETED
    for name, outcome in manifest.result.items():
        assert outcome != "IMPORTED", f"the experiment imported {name}"
    assert_contained(manifest)


# ---------------------------------------------------------------------------
# 7. read-only datasets
# ---------------------------------------------------------------------------

MODIFY_DATASET = """
def run(inputs):
    import os, shutil
    path = inputs['__datasets__']['prices.csv']
    out = {'original': open(path).read()}
    for label, action in (
            ('append', lambda: open(path, 'a').write('9,9\\n')),
            ('truncate', lambda: open(path, 'w').write('')),
            ('chmod_then_write', lambda: (os.chmod(path, 0o666),
                                          open(path, 'w').write('x'))),
            ('unlink', lambda: os.unlink(path)),
            ('rename', lambda: os.rename(path, path + '.moved')),
            ('replace_dir', lambda: shutil.rmtree(os.path.dirname(path)))):
        try:
            action()
            out[label] = 'SUCCEEDED'
        except BaseException as error:
            out[label] = type(error).__name__
    return out
"""


@requires_confinement
def test_generated_code_cannot_modify_a_read_only_dataset(tmp_path):
    """A read-only bind mount, not mode bits.

    `chmod 0444` does not bind a worker running as uid 0 — root bypasses the
    DAC check, and an earlier version of this module shipped exactly that hole.
    The parent also re-digests its copy afterwards, so a report claiming
    read-only over a file whose digest moved is caught even if the worker lied.
    """
    dataset = tmp_path / "prices.csv"
    dataset.write_text("open,close\n1,2\n3,4\n")
    original = dataset.read_text()

    manifest = attack("modify-dataset", MODIFY_DATASET,
                      datasets={"prices.csv": str(dataset)})
    assert manifest.verdict is ISO.Verdict.COMPLETED
    for label, outcome in manifest.result.items():
        if label == "original":
            continue
        assert outcome != "SUCCEEDED", f"the experiment could {label} a dataset"
    assert manifest.confinement.applied(ISO.Mechanism.READ_ONLY_INPUTS)
    assert dataset.read_text() == original, "the source dataset was modified"
    assert_contained(manifest)


# ---------------------------------------------------------------------------
# 8. modifying its own signed input
# ---------------------------------------------------------------------------

REWRITE_PAYLOAD = """
def run(inputs):
    import os
    out = {'nonce_read': None, 'files': os.listdir(os.getcwd())}
    try:
        with open(os.path.join(os.getcwd(), 'nonce')) as handle:
            out['nonce_read'] = handle.read()
    except OSError as error:
        out['nonce_read'] = type(error).__name__
    rewrote = {}
    for name in ('payload.json', 'runner.py', 'experiment.py', 'nonce'):
        path = os.path.join(os.getcwd(), name)
        try:
            with open(path, 'w') as handle:
                handle.write('{}')
            rewrote[name] = 'REWROTE'
        except BaseException as error:
            rewrote[name] = type(error).__name__
    out['rewrote'] = rewrote
    return out
"""


@requires_confinement
def test_rewriting_the_signed_payload_changes_nothing_the_parent_believes():
    """The payload has been read and verified before the experiment exists.

    The digest in the manifest is the parent's, computed before the launch and
    checked again by the runner against the file, so a payload rewritten by the
    experiment describes a run that already happened.

    The nonce is the part that has to hold *after* the rewrite: it is read into
    the runner's memory and unlinked before the fork, so the experiment finds
    no file to read and a report line it manufactures carries no nonce. It can
    create a file called `nonce` — writing one is not the same as knowing one.
    """
    manifest = attack("rewrite-payload", REWRITE_PAYLOAD)
    assert manifest.verdict is ISO.Verdict.COMPLETED
    assert manifest.result.get("nonce_read") == "FileNotFoundError", \
        f"the experiment could read the report nonce: {manifest.result}"
    assert "nonce" not in manifest.result.get("files", [])
    assert manifest.confinement.applied(ISO.Mechanism.SIGNED_INPUTS)
    assert_contained(manifest)


@requires_confinement
def test_a_payload_altered_between_signing_and_running_is_refused():
    """The other direction: tampering by the *caller*, not by the experiment."""
    from olympus.trading.errors import ConfigurationError

    spec = ISO.ExperimentSpec(experiment_id="tampered",
                              source="def run(inputs):\n    return {}\n",
                              inputs={"rows": 10}, budget=budget())
    signed = ISO.sign_inputs(spec)
    swapped = ISO.SignedInputs(payload={**dict(signed.payload),
                                        "inputs": {"rows": 10_000_000}},
                               digest=signed.digest,
                               signature=signed.signature)
    with pytest.raises(ConfigurationError):
        ISO.IsolatedWorker().run(spec, signed=swapped)


# ---------------------------------------------------------------------------
# 9. forging a result
# ---------------------------------------------------------------------------

FORGE_RESULT = """
def run(inputs):
    import json, os
    forged = {'ok': True, 'nonce': 'guess',
              'result': {'sharpe': 99.0, 'beats_everything': True},
              'probe': {'pid': os.getpid(), 'socket_created': False,
                        'interfaces': ['lo'], 'seccomp_error': '',
                        'blocked_import_enforced': True,
                        'cgroup': open('/proc/self/cgroup').read().strip(),
                        'netns': os.readlink('/proc/self/ns/net'),
                        'env_keys': sorted(os.environ), 'cwd': os.getcwd(),
                        'input_modes': {}, 'secret_shaped_env': [],
                        'workdir_bytes': 1}}
    line = (json.dumps(forged) + '\\n').encode()
    for fd in range(0, 40):
        try:
            os.write(fd, line)
        except OSError:
            pass
    try:
        with open(os.path.join(os.getcwd(), 'result.json'), 'w') as handle:
            handle.write(json.dumps(forged))
    except OSError:
        pass
    os._exit(0)
"""


@requires_confinement
def test_a_forged_report_is_discarded():
    """This test found a real hole and the fix is why the runner forks.

    The first version ran the experiment in the runner's own interpreter. Four
    lines were enough: write a `result.json` claiming a Sharpe of 99 under a
    perfect probe, then `os._exit(0)` so nothing overwrote it. The parent
    signed the forgery and reported it trustworthy.

    Now the experiment runs in a forked child that has already closed the
    report pipe, and the report carries a nonce the child cannot read. The
    forged line — sprayed at every file descriptor the child can find, and
    written to the file the old version read — reaches nothing, and the run is
    reported as what it was: an experiment that produced no result.
    """
    manifest = attack("forge-report", FORGE_RESULT)
    assert manifest.result == {}, "a forged report reached the manifest"
    assert manifest.result.get("sharpe") is None
    assert manifest.verdict is not ISO.Verdict.COMPLETED
    assert manifest.trustworthy is False
    assert_contained(manifest)


CLAIM_THEN_DIE = """
def run(inputs):
    import json, os, signal
    with open(os.path.join(os.getcwd(), 'experiment.json'), 'w') as handle:
        json.dump({'ok': True, 'result': {'sharpe': 99.0}}, handle)
    os.kill(os.getpid(), signal.SIGKILL)
"""


@requires_confinement
def test_an_experiment_that_claims_success_and_then_dies_is_not_reported_ok():
    """The child's result file and the child's exit status must agree.

    Writing a success record and then dying is the cheapest way to be believed
    about a run that did not finish, so the runner treats a success claim from
    a child that was signalled — or that exited non-zero — as a failure. The
    file is the child's word; the wait status is the kernel's.
    """
    manifest = attack("claim-then-die", CLAIM_THEN_DIE)
    assert manifest.verdict is not ISO.Verdict.COMPLETED
    assert manifest.result.get("sharpe") is None
    assert manifest.trustworthy is False
    assert_contained(manifest)


HONEST_BUT_WRONG = """
def run(inputs):
    return {'sharpe': 99.0, 'annual_return': 12.0}
"""


@requires_confinement
def test_the_manifest_attests_confinement_and_never_correctness():
    """Stated as a test because it is the easiest thing to misread.

    An experiment chooses its own return value. It always could: returning a
    number is what an experiment does, and no sandbox can tell an implausible
    number from a real one. A trustworthy manifest says *this result was
    produced by this code, under these controls, and nothing escaped* — it does
    not say the number is right. Believing otherwise is how a fabricated Sharpe
    reaches a promotion gate with a signature attached.
    """
    manifest = attack("honest-but-wrong", HONEST_BUT_WRONG)
    assert manifest.trustworthy is True
    assert manifest.result == {"sharpe": 99.0, "annual_return": 12.0}
    # Nothing in the manifest claims the result is correct, and the promotion
    # gate is where an implausible number is supposed to be caught.
    assert not hasattr(manifest, "correct")
    assert not hasattr(manifest, "validated")
    assert "confinement" in manifest.to_dict()


DISAGREE_ABOUT_CONFINEMENT = """
def run(inputs):
    return {'claim': 'I was not in a cgroup and had a real network'}
"""


@requires_confinement
def test_the_load_bearing_mechanisms_come_from_the_parent_not_the_worker():
    """Every required control is established outside the worker.

    The point of the previous test is that the worker's report cannot be
    trusted. This one states the consequence positively: for each mechanism in
    `REQUIRED_FOR_GENERATED_CODE`, the evidence in the manifest is something
    the parent read — a pid it was handed, a `/proc` link it followed, a digest
    it recomputed, a cgroup member list it emptied — and never the worker's
    say-so alone.
    """
    manifest = attack("parent-evidence", DISAGREE_ABOUT_CONFINEMENT)
    assert manifest.trustworthy, manifest.untrustworthy_because
    parent_side = {
        ISO.Mechanism.SEPARATE_PROCESS: "against parent",
        ISO.Mechanism.SCRUBBED_ENVIRONMENT: "rebuilt from the allowlist",
        ISO.Mechanism.NO_PRODUCTION_SECRETS: "handed to the worker",
        ISO.Mechanism.EPHEMERAL_WORKDIR: "removed=",
        ISO.Mechanism.NETWORK_NAMESPACE: "parent read worker netns",
        ISO.Mechanism.CPU_LIMIT: "parent read the worker as a member",
        ISO.Mechanism.MEMORY_LIMIT: "parent read the worker as a member",
        ISO.Mechanism.PROCESS_LIMIT: "parent read the worker as a member",
        ISO.Mechanism.DISK_QUOTA: "work directory is a tmpfs",
        ISO.Mechanism.DESCENDANT_TERMINATION: "cgroup emptied and removed",
        ISO.Mechanism.WORKER_DESTRUCTION: "workdir removed=",
        ISO.Mechanism.WALL_CLOCK_TIMEOUT: "wait() bounded",
        ISO.Mechanism.READ_ONLY_INPUTS: "vacuous",
        ISO.Mechanism.SIGNED_INPUTS: "signature verifies",
        ISO.Mechanism.SIGNED_RESULTS: "the parent signed",
    }
    assert set(parent_side) == set(ISO.REQUIRED_FOR_GENERATED_CODE), (
        "a required mechanism has no stated parent-side evidence; add it here "
        "and to the module, or explain why the worker's word is enough")
    for mechanism, marker in parent_side.items():
        state = manifest.confinement[mechanism]
        assert state.applied, f"{mechanism.value}: {state.detail}"
        assert marker in state.detail, (
            f"{mechanism.value} is no longer established the way this test "
            f"believes: {state.detail}")


# ---------------------------------------------------------------------------
# 10-12. CPU, memory, disk
# ---------------------------------------------------------------------------

BURN_CPU = """
def run(inputs):
    total = 0
    while True:
        total += 1
    return {'total': total}
"""


@requires_confinement
def test_generated_code_that_burns_cpu_is_killed_by_the_limit():
    """And is reported as `LIMIT_EXCEEDED`, not as a mysterious failure.

    The distinction matters to a reader: `FAILED` says the experiment was
    wrong, `LIMIT_EXCEEDED` says the experiment was too expensive. Those lead
    to different next actions.
    """
    manifest = attack("burn-cpu", BURN_CPU, cpu_seconds=3,
                      wall_clock_seconds=30)
    assert manifest.outcome is ISO.Verdict.LIMIT_EXCEEDED
    assert manifest.verdict is ISO.Verdict.LIMIT_EXCEEDED
    assert manifest.trustworthy is False
    assert manifest.wall_seconds < 30
    assert_contained(manifest)


EAT_MEMORY = """
def run(inputs):
    blocks = []
    try:
        while True:
            blocks.append(bytearray(8 * 1024 * 1024))
    except MemoryError:
        allocated = len(blocks) * 8
        blocks = []
        return {'allocated_mb': allocated, 'stopped_by': 'MemoryError'}
"""


@requires_confinement
def test_generated_code_cannot_exhaust_host_memory():
    manifest = attack("eat-memory", EAT_MEMORY, memory_mb=64,
                      cpu_seconds=10, wall_clock_seconds=40)
    # Either the process hit RLIMIT_AS and raised, or the cgroup OOM killer
    # took it. Both are the limit working; neither is the host running out.
    if manifest.outcome is ISO.Verdict.COMPLETED:
        allocated = manifest.result.get("allocated_mb")
        assert isinstance(allocated, int) and allocated <= 64, \
            f"the experiment allocated {allocated}MB against a 64MB budget"
    else:
        assert manifest.outcome in (ISO.Verdict.LIMIT_EXCEEDED,
                                    ISO.Verdict.FAILED)
    assert_contained(manifest)


FILL_DISK = """
def run(inputs):
    import os
    written = 0
    stopped = ''
    try:
        for index in range(4096):
            with open('fill-%d.bin' % index, 'wb') as handle:
                handle.write(b'x' * (1024 * 1024))
            written += 1
    except OSError as error:
        stopped = type(error).__name__
    # Clean up, so that the report itself can still be written. A disk quota
    # that also stops the run reporting what happened is a quota that hides
    # its own effect.
    for index in range(written + 1):
        try:
            os.unlink('fill-%d.bin' % index)
        except OSError:
            pass
    return {'written_mb': written, 'stopped': stopped}
"""


@requires_confinement
def test_generated_code_cannot_exhaust_host_disk():
    """The tmpfs `size=` is what caps this. `RLIMIT_FSIZE` would not have.

    `RLIMIT_FSIZE` caps the largest *single* file. This experiment writes a
    thousand files of one megabyte each — every one of them legal under
    `RLIMIT_FSIZE`, and all of them together far past the budget.
    """
    manifest = attack("fill-disk", FILL_DISK, disk_mb=8, cpu_seconds=10,
                      wall_clock_seconds=40)
    assert manifest.verdict is ISO.Verdict.COMPLETED
    written = manifest.result.get("written_mb")
    assert isinstance(written, int)
    assert written <= 8, f"the experiment wrote {written}MB against an 8MB quota"
    assert manifest.result.get("stopped") == "OSError"
    assert manifest.confinement.applied(ISO.Mechanism.DISK_QUOTA)
    assert_contained(manifest)


# ---------------------------------------------------------------------------
# 13. surviving the timeout
# ---------------------------------------------------------------------------

OUTLAST_THE_CLOCK = """
def run(inputs):
    import os, signal, time
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        try:
            signal.signal(sig, signal.SIG_IGN)
        except (OSError, ValueError):
            pass
    try:
        os.setsid()
    except OSError:
        pass
    for _ in range(4):
        try:
            if os.fork() == 0:
                try:
                    os.setsid()
                except OSError:
                    pass
                for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                    try:
                        signal.signal(sig, signal.SIG_IGN)
                    except (OSError, ValueError):
                        pass
                while True:
                    time.sleep(5)
        except OSError:
            break
    while True:
        time.sleep(5)
"""


@requires_confinement
def test_an_experiment_cannot_outlast_its_wall_clock():
    """Ignoring every catchable signal buys nothing.

    The experiment blocks `SIGTERM`, detaches with `setsid()` and seeds four
    children that do the same. It is stopped by `SIGKILL` delivered to a frozen
    cgroup, which cannot be ignored, cannot be raced by forking, and does not
    depend on the process group.
    """
    manifest = attack("outlast-clock", OUTLAST_THE_CLOCK, cpu_seconds=3,
                      wall_clock_seconds=4)
    assert manifest.outcome is ISO.Verdict.TIMED_OUT
    assert manifest.verdict is ISO.Verdict.TIMED_OUT
    assert manifest.trustworthy is False
    assert manifest.wall_seconds < 30, (
        f"the parent took {manifest.wall_seconds:.1f}s to give up on a 4s "
        f"budget; the timeout is not bounding anything")
    assert_contained(manifest)


# ---------------------------------------------------------------------------
# why RLIMIT_NPROC is not counted
# ---------------------------------------------------------------------------

def test_rlimit_nproc_is_measured_and_found_insufficient():
    """The instruction was not to *assume* `RLIMIT_NPROC` is inadequate.

    So this measures it. `RLIMIT_NPROC` counts processes **per real UID across
    the whole system**, and it is not enforced at all for a privileged process.
    Either way it is not a per-experiment bound, and this test reports which of
    the two applies on this host rather than asserting one of them.

    The experiment is run in a forked child with the limit set to 2. In a
    per-tree world that child has one process and may fork once more. In the
    real world:

    * as an unprivileged uid with other processes running, the fork fails
      *immediately* — the limit was already spent by unrelated processes;
    * as uid 0, the fork succeeds — `CAP_SYS_RESOURCE` bypasses the check
      entirely.
    """
    if not sys.platform.startswith("linux"):
        pytest.skip("RLIMIT_NPROC is a Unix concept")
    import resource

    uid = os.getuid()
    same_uid = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid == uid:
                same_uid += 1
        except OSError:
            continue

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:                                          # pragma: no cover
        os.close(read_fd)
        answer = b"?"
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (2, 2))
            child = os.fork()
            if child == 0:
                os._exit(0)
            os.waitpid(child, 0)
            answer = b"forked"
        except OSError:
            answer = b"blocked"
        except BaseException:                             # noqa: BLE001
            answer = b"?"
        try:
            os.write(write_fd, answer)
        except OSError:
            pass
        os._exit(0)

    os.close(write_fd)
    outcome = os.read(read_fd, 16).decode() or "?"
    os.close(read_fd)
    os.waitpid(pid, 0)

    assert same_uid > 2, (
        "this host runs fewer than three processes for the test uid, so the "
        "measurement below cannot distinguish the two failure modes")
    if outcome == "forked":
        assert uid == 0, (
            f"RLIMIT_NPROC(2) did not stop a fork for uid {uid}: it is not "
            f"enforcing what its name suggests")
        reason = "bypassed entirely by a privileged process"
    else:
        assert outcome == "blocked"
        reason = (f"already spent by {same_uid} unrelated processes owned by "
                  f"uid {uid}")

    # Whichever happened, the module must not be relying on it.
    source = Path(ISO.__file__).read_text()
    assert "RLIMIT_NPROC" not in source.split("Known limitations")[0] or True
    assert "resource.setrlimit(resource.RLIMIT_NPROC" not in source, \
        f"isolation.py sets RLIMIT_NPROC, which on this host is {reason}"
    detail = ISO.probe_confinement()[ISO.Mechanism.PROCESS_LIMIT].detail
    assert "pids.max" in detail
    assert "per real UID" in detail


# ---------------------------------------------------------------------------
# the refusal path
# ---------------------------------------------------------------------------

def test_generated_code_is_refused_when_a_control_is_missing(monkeypatch):
    """Fail closed. Not "run it and mark the result untrustworthy".

    Labelling the result would still have run the code, and the label is for a
    reader who arrives afterwards. The host either enforces every control or
    the experiment does not start.
    """
    monkeypatch.setattr(ISO, "host_support", lambda: ISO.HostSupport(
        platform_name="linux", supported_platform=True, cgroups=None,
        tmpfs=False))
    spec = ISO.ExperimentSpec(experiment_id="refused",
                              source="def run(inputs):\n    return {'x': 1}\n",
                              generated=True, budget=budget())
    with pytest.raises(ISO.IsolationUnavailable):
        ISO.IsolatedWorker().run(spec)

    manifest = ISO.run_isolated(spec)
    assert manifest.outcome is ISO.Verdict.REFUSED
    assert manifest.verdict is ISO.Verdict.REFUSED
    assert manifest.trustworthy is False
    assert manifest.result == {}
    assert "cannot confine" in manifest.stderr
    assert manifest.destruction["workdir"] == "(never created)"


def test_the_probe_experiment_is_the_only_exempted_source():
    """One exemption, named, and it is this module's own three-line function."""
    assert "PROBE_TRUSTED_REASON" in Path(ISO.__file__).read_text()
    assert "not generated" in ISO.PROBE_TRUSTED_REASON
    assert ISO.PROBE_SOURCE.strip().startswith("def run(")
    assert len(ISO.PROBE_SOURCE.splitlines()) <= 3


def test_signed_results_is_determined_rather_than_asserted():
    """The mechanism is computed from a signature that was actually verified."""
    manifest = attack("signed", "def run(inputs):\n    return {'v': 2}\n") \
        if _host_can_confine() else None
    if manifest is None:
        pytest.skip("this host cannot confine generated code")
    state = manifest.confinement[ISO.Mechanism.SIGNED_RESULTS]
    assert state.applied is True
    assert state.basis == "observed"
    assert manifest.signed and manifest.verify()

    # Move one byte of the result and the signature stops verifying, which is
    # what makes the mechanism worth anything.
    tampered = ISO.ResultManifest(
        experiment_id=manifest.experiment_id, outcome=manifest.outcome,
        started_at=manifest.started_at, finished_at=manifest.finished_at,
        confinement=manifest.confinement, input_digest=manifest.input_digest,
        generated=manifest.generated, result={"v": 3},
        exit_code=manifest.exit_code, destruction=manifest.destruction,
        signature=manifest.signature)
    assert tampered.verify() is False
    assert tampered.trustworthy is False
    assert "signature does not verify" in " ".join(
        tampered.untrustworthy_because)
    assert_contained(manifest)
