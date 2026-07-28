"""OS-level isolation for generated research code.

> A Python-level import boundary alone is not adequate isolation for generated
> code.

That sentence is the whole reason this module exists. `lab.ResearchSandbox`
enforces by *absence* — it holds no reference to credentials or order
submission, so `request("live_broker_credentials")` has nothing to return. That
is a real guarantee against an experiment that asks politely. It is not a
guarantee against generated code that imports what it likes, opens a socket and
posts an order, because all of that happens inside the same interpreter with
the same file descriptors and the same network.

So the worker here is a **separate process**, in its own network namespace,
under rlimits, with a scrubbed environment, behind a seccomp filter, in an
ephemeral directory that is destroyed afterwards.

Confinement is verified from inside, not asserted from outside
--------------------------------------------------------------
`preexec_fn` applies the confinement. It cannot report what it managed to
apply — a `preexec_fn` that raised would only tell us the launch failed, not
which limit is missing. So the worker **probes its own confinement** as its
first act: it tries to create a socket, reads `/sys/class/net`, reads back its
own rlimits, and reports what it actually found. The parent compares that
report against what it asked for.

The consequence matters: if the network namespace silently did not apply, the
probe sees a working socket and the run is rejected as `CONFINEMENT_FAILED`.
The result is thrown away rather than trusted. **Fail closed**, and fail on
observation rather than on intent.

The four layers, in order of how much they are worth
----------------------------------------------------
1. **No network.** `unshare(CLONE_NEWNET)` leaves the worker with a loopback
   interface and nothing else. An order that cannot leave the machine is not
   an order, whatever code was generated.
2. **No credentials.** The environment is rebuilt from an allowlist, not
   filtered by a denylist — a new secret-bearing variable is excluded by
   default rather than by remembering to add it to a pattern list.
3. **Blocked syscalls.** A seccomp-BPF filter returns `EPERM` for `socket`,
   `connect`, `ptrace` and `execve`. Redundant with (1) on purpose: two
   independent mechanisms fail independently.
4. **Blocked imports.** The worker installs a meta-path hook refusing
   `olympus.trading.execution`, `olympus.trading.brokers`, `olympus.vault` and
   `olympus.trading.modes`. **This is the weakest layer and is listed last on
   purpose.** Generated code can defeat an import hook; it cannot defeat an
   empty network namespace. It is here because a clear `ImportError` naming the
   boundary is more useful to a researcher than a mysterious `EPERM`, not
   because it is load-bearing.

What is signed, and by whom
---------------------------
The **parent** signs the input payload and the result manifest with Olympus's
key, because the worker must not hold a signing key — a worker that could sign
could sign a result it invented. The **worker** verifies the input *digest*, so
a payload altered between write and read is caught inside the run. Integrity is
checked in both directions; authenticity is only ever claimed by the parent.

Known limitations, stated because they are real
-----------------------------------------------
- **`RLIMIT_NPROC` is not applied.** It counts processes per real UID across
  the whole system, not per process tree, so setting it to 8 in a container
  where the same UID already runs more than 8 processes would prevent the
  worker from starting at all. The wall-clock timeout and the process-group
  kill are what bound a runaway; `Mechanism.PROCESS_LIMIT` reports `applied
  =False` with this reason rather than pretending.
- **`RLIMIT_FSIZE` caps the largest single file, not total disk.** A real disk
  quota needs a filesystem that supports one. The mechanism is labelled for
  what it does.
- **This is not a defence against a hostile human with a shell.** It is a
  defence against code Olympus generated, running unattended.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from ..errors import ConfigurationError
from .challengers import ComputeBudget

#: Bumped when the confinement contract changes.
ISOLATION_SCHEMA_VERSION = 1

#: Environment variables the worker is given. An allowlist: anything not named
#: here is absent, so a new secret-bearing variable is excluded by default
#: rather than by somebody remembering to add it to a denylist.
ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "TZ", "HOME",
                                  "TMPDIR", "PYTHONHASHSEED")

#: Modules the worker's import hook refuses. Defence in depth — see the module
#: docstring on why this is the weakest of the four layers.
BLOCKED_IMPORTS: tuple[str, ...] = (
    "olympus.trading.execution", "olympus.trading.brokers",
    "olympus.trading.oms", "olympus.trading.modes", "olympus.vault",
    "olympus.trading.killswitch", "olympus.trading.risk",
)

#: Syscalls the seccomp filter denies, by name. Numbers are per-architecture.
BLOCKED_SYSCALLS: tuple[str, ...] = ("socket", "connect", "ptrace", "execve")

_SYSCALL_NUMBERS: Mapping[str, Mapping[str, int]] = {
    "x86_64": {"socket": 41, "connect": 42, "ptrace": 101, "execve": 59},
    "aarch64": {"socket": 198, "connect": 203, "ptrace": 117, "execve": 221},
}
_AUDIT_ARCH: Mapping[str, int] = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}

_CLONE_NEWNET = 0x40000000
_CLONE_NEWNS = 0x00020000
_MS_RDONLY, _MS_REMOUNT, _MS_BIND = 1, 32, 4096
_MS_REC, _MS_PRIVATE = 16384, 1 << 18
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_EPERM = 1

_OUTPUT_CAP = 64_000


class Mechanism(str, Enum):
    """Every confinement this module attempts, named individually.

    One enum member per mechanism rather than a single `isolated: bool`,
    because "isolated" is a summary and the interesting question is always
    *which one* is missing on a given host.
    """

    SEPARATE_PROCESS = "separate_process"
    SCRUBBED_ENVIRONMENT = "scrubbed_environment"
    EPHEMERAL_WORKDIR = "ephemeral_workdir"
    READ_ONLY_INPUTS = "read_only_inputs"
    NETWORK_NAMESPACE = "network_namespace"
    SECCOMP_FILTER = "seccomp_filter"
    NO_NEW_PRIVS = "no_new_privs"
    BLOCKED_IMPORTS = "blocked_imports"
    CPU_LIMIT = "cpu_limit"
    MEMORY_LIMIT = "memory_limit"
    FILE_SIZE_LIMIT = "file_size_limit"
    PROCESS_LIMIT = "process_limit"
    WALL_CLOCK_TIMEOUT = "wall_clock_timeout"
    SIGNED_INPUTS = "signed_inputs"
    SIGNED_RESULTS = "signed_results"
    WORKER_DESTRUCTION = "worker_destruction"


#: The mechanisms that make this more than a Python import boundary. All of
#: them must be observed before generated code may run. `SECCOMP_FILTER`,
#: `NO_NEW_PRIVS` and `FILE_SIZE_LIMIT` are deliberately *not* here: they are
#: worth having and are not what stands between a generated experiment and a
#: broker.
REQUIRED_FOR_GENERATED_CODE: frozenset[Mechanism] = frozenset({
    Mechanism.SEPARATE_PROCESS, Mechanism.SCRUBBED_ENVIRONMENT,
    Mechanism.EPHEMERAL_WORKDIR, Mechanism.NETWORK_NAMESPACE,
    Mechanism.CPU_LIMIT, Mechanism.MEMORY_LIMIT,
    Mechanism.WALL_CLOCK_TIMEOUT, Mechanism.WORKER_DESTRUCTION,
})


@dataclass(frozen=True)
class MechanismState:
    """One mechanism: whether it applied, and how that was established."""

    mechanism: Mechanism
    applied: bool
    detail: str
    #: "observed" when the worker probed it; "asserted" when only the parent
    #: knows. An asserted mechanism is a weaker claim and says so.
    basis: str = "observed"

    def __post_init__(self):
        object.__setattr__(self, "mechanism", Mechanism(self.mechanism))
        if not str(self.detail).strip():
            raise ConfigurationError(
                "a mechanism state must say how it was established",
                mechanism=self.mechanism.value)

    def to_dict(self) -> dict:
        return {"mechanism": self.mechanism.value, "applied": self.applied,
                "detail": self.detail, "basis": self.basis}


@dataclass(frozen=True)
class Confinement:
    """What actually held for one run. Computed from the worker's own probe."""

    states: tuple[MechanismState, ...] = ()
    schema_version: int = ISOLATION_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "states", tuple(self.states))

    def __getitem__(self, mechanism: Mechanism | str) -> MechanismState:
        wanted = Mechanism(mechanism)
        for state in self.states:
            if state.mechanism is wanted:
                return state
        raise KeyError(wanted.value)

    def applied(self, mechanism: Mechanism | str) -> bool:
        try:
            return self[mechanism].applied
        except KeyError:
            return False

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(s.mechanism.value for s in self.states if not s.applied)

    @property
    def observed(self) -> tuple[str, ...]:
        return tuple(s.mechanism.value for s in self.states
                     if s.applied and s.basis == "observed")

    @property
    def adequate_for_generated_code(self) -> bool:
        """Computed. There is no argument that sets this.

        Every mechanism in `REQUIRED_FOR_GENERATED_CODE` must be applied, and
        the ones the worker can see must have been *observed* rather than
        merely asserted by the parent.
        """
        return all(self.applied(m) for m in REQUIRED_FOR_GENERATED_CODE)

    @property
    def shortfall(self) -> tuple[str, ...]:
        """Which required mechanisms are missing. Empty when adequate."""
        return tuple(sorted(m.value for m in REQUIRED_FOR_GENERATED_CODE
                            if not self.applied(m)))

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version,
                "states": [s.to_dict() for s in self.states],
                "missing": list(self.missing),
                "observed": list(self.observed),
                "adequate_for_generated_code": self.adequate_for_generated_code,
                "shortfall": list(self.shortfall)}


# ---------------------------------------------------------------------------
# the confinement, applied in the child before exec
# ---------------------------------------------------------------------------

def _libc():
    return ctypes.CDLL("libc.so.6", use_errno=True)


def _own_netns() -> str:
    """This process's network namespace, for comparison with the worker's."""
    try:
        return os.readlink("/proc/self/ns/net")
    except OSError:
        return ""


def _bind_read_only(libc, target: str) -> None:
    """Make `target` read-only by mount, not by mode bits.

    `chmod 0444` is not confinement for a worker running as uid 0: root
    bypasses the DAC check, and the first version of this module shipped a
    dataset the worker could overwrite while the manifest said read-only. A
    read-only bind mount is enforced by the VFS regardless of uid, which is why
    this is the mechanism and the mode bits are only defence in depth.

    Needs `CAP_SYS_ADMIN` in the new mount namespace. Without it every call
    fails and the worker's own write probe reports the input as writable, which
    is the honest outcome rather than a silent downgrade.
    """
    encoded = target.encode("utf-8")
    libc.unshare(_CLONE_NEWNS)
    libc.mount(None, b"/", None, _MS_REC | _MS_PRIVATE, None)
    libc.mount(encoded, encoded, None, _MS_BIND, None)
    libc.mount(None, encoded, None,
               _MS_BIND | _MS_REMOUNT | _MS_RDONLY, None)


def _apply_confinement(budget: ComputeBudget, *, read_only_dir: str = ""):
    """Run in the child between fork and exec. Best effort, silently.

    Silent because a `preexec_fn` that raised would abort the launch and tell
    us only that something failed. What is applied is established afterwards by
    the worker's own probe, which is a stronger claim than this function's
    return value could ever be.

    **The seccomp filter is not applied here**, and the reason is worth
    recording: this function runs *before* `execve`, and the filter denies
    `execve`. Installing it here means the worker can never start. The filter
    is therefore installed by the runner as its first act, after exec — which
    is also when it starts being useful, because everything before exec is code
    this module wrote.
    """
    def _preexec():
        try:
            os.setsid()                       # own group, so a kill reaches all
        except OSError:
            pass
        limits = (
            (resource.RLIMIT_CPU, (budget.cpu_seconds, budget.cpu_seconds + 1)),
            (resource.RLIMIT_AS, (budget.memory_mb * 1024 * 1024,) * 2),
            (resource.RLIMIT_FSIZE, (budget.disk_mb * 1024 * 1024,) * 2),
            (resource.RLIMIT_CORE, (0, 0)),
        )
        for which, values in limits:
            try:
                resource.setrlimit(which, values)
            except (ValueError, OSError):
                pass
        try:
            libc = _libc()
            libc.unshare(_CLONE_NEWNET)
            libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
            if read_only_dir:
                _bind_read_only(libc, read_only_dir)
        except Exception:                                # noqa: BLE001
            pass                                  # the probe will report it
    return _preexec


# ---------------------------------------------------------------------------
# the payload
# ---------------------------------------------------------------------------

def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def digest_of(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


@dataclass(frozen=True)
class ExperimentSpec:
    """What the worker is asked to run. Signed before it crosses the boundary.

    Exactly one of `source` and `entrypoint`. A spec carrying both would leave
    the question of which one ran to be answered by reading the runner, and the
    whole point of signing the spec is that what ran is what was signed.
    """

    experiment_id: str
    #: Generated Python source. Must define `run(inputs) -> dict`.
    source: str = ""
    #: `"module:function"` resolved inside the worker, for pre-written code.
    entrypoint: str = ""
    inputs: Mapping[str, Any] = field(default_factory=dict)
    #: Files copied in read-only, as `name -> host path`.
    datasets: Mapping[str, str] = field(default_factory=dict)
    budget: ComputeBudget = field(default_factory=ComputeBudget)
    #: True when `source` was produced by Olympus rather than written by hand.
    generated: bool = True

    def __post_init__(self):
        if not str(self.experiment_id).strip():
            raise ConfigurationError("an experiment must be identified")
        object.__setattr__(self, "inputs", dict(self.inputs))
        object.__setattr__(self, "datasets", dict(self.datasets))
        if bool(self.source.strip()) == bool(self.entrypoint.strip()):
            raise ConfigurationError(
                "an experiment supplies exactly one of source and entrypoint; "
                "with both, what ran is not what was signed",
                experiment_id=self.experiment_id)
        if self.entrypoint and ":" not in self.entrypoint:
            raise ConfigurationError("entrypoint must be 'module:function'",
                                     entrypoint=self.entrypoint)
        for name, path in self.datasets.items():
            if not Path(path).is_file():
                raise ConfigurationError(
                    "a declared dataset does not exist; an experiment that "
                    "silently ran without its input would produce a result "
                    "about nothing",
                    experiment_id=self.experiment_id, dataset=name, path=path)

    def dataset_digests(self) -> dict[str, str]:
        return {name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                for name, path in sorted(self.datasets.items())}

    def payload(self) -> dict:
        """What gets signed. Includes dataset digests, so swapping the data
        after signing invalidates the signature."""
        return {"schema_version": ISOLATION_SCHEMA_VERSION,
                "experiment_id": self.experiment_id,
                "source_sha256": hashlib.sha256(
                    self.source.encode("utf-8")).hexdigest(),
                "entrypoint": self.entrypoint,
                "inputs": dict(self.inputs),
                "dataset_digests": self.dataset_digests(),
                "budget": self.budget.to_dict(),
                "generated": self.generated}

    def to_dict(self) -> dict:
        return {**self.payload(), "digest": digest_of(self.payload())}


@dataclass(frozen=True)
class SignedInputs:
    """The payload, its digest, and the parent's signature over it."""

    payload: Mapping[str, Any]
    digest: str
    signature: Mapping[str, str] = field(default_factory=dict)

    @property
    def signed(self) -> bool:
        return bool(self.signature.get("signature"))

    def verify(self) -> bool:
        """Digest recomputes, and the signature checks when there is one."""
        if digest_of(self.payload) != self.digest:
            return False
        if not self.signed:
            return False
        from .pipeline import verify_artifact
        return verify_artifact(self.digest, self.signature)

    def to_dict(self) -> dict:
        return {"payload": dict(self.payload), "digest": self.digest,
                "signature": dict(self.signature), "signed": self.signed}


def sign_inputs(spec: ExperimentSpec) -> SignedInputs:
    from .pipeline import sign_artifact
    payload = spec.payload()
    digest = digest_of(payload)
    return SignedInputs(payload=payload, digest=digest,
                        signature=sign_artifact(digest))


class Verdict(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    #: The kernel killed the worker for exceeding a resource limit. Distinct
    #: from TIMED_OUT (the parent's wall clock) and from CONFINEMENT_FAILED
    #: (the confinement did not hold): here the confinement worked exactly as
    #: asked, which is a different thing to report.
    LIMIT_EXCEEDED = "limit_exceeded"
    #: The worker ran and its confinement did not hold. The result is discarded.
    CONFINEMENT_FAILED = "confinement_failed"
    #: The worker was never started, because confinement could not be
    #: established for generated code.
    REFUSED = "refused"


@dataclass(frozen=True)
class ResultManifest:
    """What came back, with the confinement it came back under.

    `trustworthy` is computed. A result produced under failed confinement is
    not a result — it is an observation about a process that was not isolated,
    and treating it as evidence is how an experiment that reached production
    gets its findings believed.
    """

    experiment_id: str
    verdict: Verdict
    started_at: datetime
    finished_at: datetime
    confinement: Confinement
    input_digest: str
    result: Mapping[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    wall_seconds: float = 0.0
    signature: Mapping[str, str] = field(default_factory=dict)
    destruction: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = ISOLATION_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "verdict", Verdict(self.verdict))
        object.__setattr__(self, "result", dict(self.result))
        object.__setattr__(self, "signature", dict(self.signature))
        object.__setattr__(self, "destruction", dict(self.destruction))

    @property
    def result_digest(self) -> str:
        return digest_of({"experiment_id": self.experiment_id,
                          "verdict": self.verdict.value,
                          "input_digest": self.input_digest,
                          "result": dict(self.result)})

    @property
    def signed(self) -> bool:
        return bool(self.signature.get("signature"))

    @property
    def trustworthy(self) -> bool:
        """Computed from three facts, none of them settable."""
        return (self.verdict is Verdict.COMPLETED
                and self.confinement.adequate_for_generated_code
                and bool(self.destruction.get("workdir_removed")))

    def verify(self) -> bool:
        if not self.signed:
            return False
        from .pipeline import verify_artifact
        return verify_artifact(self.result_digest, self.signature)

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version,
                "experiment_id": self.experiment_id,
                "verdict": self.verdict.value,
                "started_at": self.started_at.isoformat(),
                "finished_at": self.finished_at.isoformat(),
                "wall_seconds": round(self.wall_seconds, 4),
                "exit_code": self.exit_code,
                "input_digest": self.input_digest,
                "result": dict(self.result),
                "result_digest": self.result_digest,
                "signed": self.signed,
                "signature": dict(self.signature),
                "confinement": self.confinement.to_dict(),
                "destruction": dict(self.destruction),
                "trustworthy": self.trustworthy,
                "stdout": self.stdout[-4000:], "stderr": self.stderr[-4000:]}


# ---------------------------------------------------------------------------
# the runner, written into the worker directory
# ---------------------------------------------------------------------------

_RUNNER = '''\
"""Worker entry point. Confines itself, probes what held, runs the experiment.

The seccomp filter is installed *here* rather than in the parent's
`preexec_fn`, because the filter denies `execve` and `preexec_fn` runs before
the interpreter has been exec'd. Installing it there means the worker never
starts. Installing it here is also when it starts mattering: everything
before this line is code the isolation module wrote.
"""
import ctypes, hashlib, json, os, resource, sys, traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
BLOCKED = {blocked!r}
SYSCALLS = {syscalls!r}
AUDIT_ARCH = {audit_arch!r}


def install_seccomp():
    """Deny the listed syscalls for the rest of this process's life.

    Returns the errno string on failure rather than raising: a host without
    seccomp is a host with weaker confinement, which the parent decides what
    to do about. Deciding here would hide it.
    """
    if not SYSCALLS or AUDIT_ARCH is None:
        return "unsupported architecture"
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)

        class Filter(ctypes.Structure):
            _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8),
                        ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]

        class Prog(ctypes.Structure):
            _fields_ = [("len", ctypes.c_ushort),
                        ("filter", ctypes.POINTER(Filter))]

        ld_abs, jeq, ret = 0x20, 0x15, 0x06
        deny = 0x00050000 | 1                       # SECCOMP_RET_ERRNO | EPERM
        code = [Filter(ld_abs, 0, 0, 4),            # seccomp_data.arch
                Filter(jeq, 1, 0, AUDIT_ARCH),
                Filter(ret, 0, 0, deny),            # foreign arch: deny
                Filter(ld_abs, 0, 0, 0)]            # seccomp_data.nr
        for number in SYSCALLS.values():
            code.append(Filter(jeq, 0, 1, number))
            code.append(Filter(ret, 0, 0, deny))
        code.append(Filter(ret, 0, 0, 0x7FFF0000))  # SECCOMP_RET_ALLOW
        # The array is bound to a name that outlives the call: a Prog whose
        # filter array has been collected points at freed memory the kernel
        # would then read.
        array = (Filter * len(code))(*code)
        prog = Prog(len(code), array)
        install_seccomp.keepalive = (prog, array)
        libc.prctl(38, 1, 0, 0, 0)                  # PR_SET_NO_NEW_PRIVS
        rc = libc.prctl(22, 2, ctypes.byref(prog), 0, 0)
        return "" if rc == 0 else "prctl returned %s" % rc
    except Exception as error:
        return "%s: %s" % (type(error).__name__, error)


class _Blocker:
    """Refuses the blocked modules with a message naming the boundary.

    Defence in depth. Generated code can remove this; it cannot remove the
    empty network namespace it is running inside.
    """

    def find_module(self, name, path=None):          # pragma: no cover - py2 API
        return self.find_spec(name, path)

    def find_spec(self, name, path=None, target=None):
        for blocked in BLOCKED:
            if name == blocked or name.startswith(blocked + "."):
                raise ImportError(
                    "%s is outside the research boundary: an experiment may "
                    "not reach order submission, credentials, live mode or "
                    "risk configuration" % name)
        return None


def probe(seccomp_error):
    """What actually holds. Observed, not assumed."""
    out = {{"seccomp_error": seccomp_error}}
    try:
        import socket
        s = socket.socket()
        s.close()
        out["socket_created"] = True
    except Exception as error:
        out["socket_created"] = False
        out["socket_error"] = "%s: %s" % (type(error).__name__, error)
    # /proc/self/net/dev is namespace-aware; /sys/class/net is not — after an
    # unshare it still lists the *old* namespace's interfaces because sysfs was
    # not remounted. Reading the wrong one is how a failed unshare looks like a
    # successful one.
    interfaces = []
    try:
        with open("/proc/self/net/dev") as handle:
            for line in handle.read().splitlines()[2:]:
                name = line.split(":", 1)[0].strip()
                if name:
                    interfaces.append(name)
    except OSError:
        pass
    out["interfaces"] = sorted(interfaces)
    try:
        out["netns"] = os.readlink("/proc/self/ns/net")
    except OSError:
        out["netns"] = ""
    modes = {{}}
    inputs_dir = HERE / "inputs"
    if inputs_dir.is_dir():
        for entry in sorted(inputs_dir.iterdir()):
            modes[entry.name] = oct(entry.stat().st_mode & 0o777)
            try:
                with open(entry, "ab"):
                    modes[entry.name] += " WRITABLE"
            except OSError:
                modes[entry.name] += " read-only"
    out["input_modes"] = modes
    for name in ("RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_FSIZE"):
        try:
            soft, hard = resource.getrlimit(getattr(resource, name))
            out[name] = [soft, hard]
        except Exception:
            out[name] = None
    out["pid"] = os.getpid()
    out["ppid"] = os.getppid()
    out["cwd"] = os.getcwd()
    out["env_keys"] = sorted(os.environ)
    out["blocked_import_enforced"] = False
    try:
        __import__(BLOCKED[0])
    except ImportError:
        out["blocked_import_enforced"] = True
    except Exception:
        out["blocked_import_enforced"] = True
    return out


def main():
    seccomp_error = install_seccomp()
    sys.meta_path.insert(0, _Blocker())
    payload = json.loads((HERE / "payload.json").read_text())
    expected = payload.pop("__digest__")
    recomputed = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        default=str).encode("utf-8")).hexdigest()
    report = {{"probe": probe(seccomp_error), "input_digest": recomputed}}
    if recomputed != expected:
        report["error"] = "input digest mismatch: the payload changed between "\\
                          "signing and reading"
        (HERE / "result.json").write_text(json.dumps(report))
        return 3

    try:
        if payload.get("entrypoint"):
            module_name, _, function = payload["entrypoint"].partition(":")
            module = __import__(module_name, fromlist=[function])
            run = getattr(module, function)
        else:
            namespace = {{"__name__": "olympus_experiment"}}
            exec(compile((HERE / "experiment.py").read_text(),
                         "experiment.py", "exec"), namespace)
            run = namespace["run"]
        inputs = dict(payload.get("inputs") or {{}})
        inputs["__datasets__"] = {{
            name: str(HERE / "inputs" / name)
            for name in sorted(payload.get("dataset_digests") or {{}})}}
        report["result"] = run(inputs)
        report["ok"] = True
    except BaseException as error:
        report["ok"] = False
        report["error"] = "%s: %s" % (type(error).__name__, error)
        report["traceback"] = traceback.format_exc()[-4000:]
    (HERE / "result.json").write_text(json.dumps(report, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
'''


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------

class IsolatedWorker:
    """Runs one experiment, in one ephemeral process, then destroys it.

    Single-use by construction. A worker object that could run twice would be
    a worker whose second run inherits whatever the first left behind, and the
    destruction record would describe only the last one.
    """

    __slots__ = ("_used", "_root", "allow_repo_imports")

    def __init__(self, *, allow_repo_imports: bool = True):
        self._used = False
        self._root: Path | None = None
        #: When True the worker can import `olympus.trading.native` to build a
        #: model. The blocked-import hook and the network namespace are what
        #: make that safe; without them it would not be.
        self.allow_repo_imports = bool(allow_repo_imports)

    # -- environment -------------------------------------------------------

    @staticmethod
    def child_environment(root: Path, *, allow_repo_imports: bool) -> dict:
        """Rebuilt from an allowlist. Never the parent's environment filtered.

        The difference matters: a denylist forgets the variable somebody adds
        next month, and an allowlist does not.
        """
        env = {name: os.environ[name] for name in ENV_ALLOWLIST
               if name in os.environ}
        env.setdefault("PATH", "/usr/bin:/bin")
        env["HOME"] = str(root)
        env["TMPDIR"] = str(root / "tmp")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["OLYMPUS_RESEARCH_WORKER"] = "1"
        if allow_repo_imports:
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
        return env

    # -- the run -----------------------------------------------------------

    def run(self, spec: ExperimentSpec, *, signed: SignedInputs | None = None,
            trusted_reason: str = "") -> ResultManifest:
        """Execute, verify the confinement, sign the result, destroy the worker.

        Refuses to start generated code when confinement cannot be established.
        `trusted_reason` overrides that for hand-written experiments and is
        recorded in the manifest — an override nobody can find is an override
        that will be forgotten.
        """
        if self._used:
            raise ConfigurationError(
                "an isolated worker runs once; reusing one would let a second "
                "experiment inherit whatever the first left behind",
                experiment_id=spec.experiment_id)
        self._used = True

        inputs = signed or sign_inputs(spec)
        # Two checks, because they catch different tampering. The first: the
        # digest describes the spec that is about to run. The second: the
        # *payload* is the one that digest was taken over. Without the second,
        # a caller can keep a valid signature and swap the payload underneath
        # it — the worker would catch that on its own recomputation, but by
        # then it has already been given the altered instructions to read.
        if inputs.digest != digest_of(spec.payload()):
            raise ConfigurationError(
                "the signed payload does not describe this spec",
                experiment_id=spec.experiment_id)
        if digest_of(inputs.payload) != inputs.digest:
            raise ConfigurationError(
                "the payload does not match its own digest; it was altered "
                "after signing", experiment_id=spec.experiment_id)

        started = datetime.now(timezone.utc)
        root = Path(tempfile.mkdtemp(prefix="olympus-research-"))
        self._root = root
        try:
            self._materialise(root, spec, inputs)
            completed, timed_out = self._launch(root, spec)
            report = self._read_report(root)
            confinement = self._confinement(spec, inputs, report,
                                            timed_out=timed_out)
            verdict = self._verdict(spec, report, completed, timed_out,
                                    confinement, trusted_reason)
            finished = datetime.now(timezone.utc)
            manifest = ResultManifest(
                experiment_id=spec.experiment_id, verdict=verdict,
                started_at=started, finished_at=finished,
                confinement=confinement, input_digest=inputs.digest,
                result=dict(report.get("result") or {}),
                stdout=(completed.stdout or "")[-_OUTPUT_CAP:],
                stderr=(completed.stderr or "")[-_OUTPUT_CAP:],
                exit_code=completed.returncode,
                wall_seconds=(finished - started).total_seconds())
        finally:
            destruction = self._destroy(root)

        from .pipeline import sign_artifact
        signed_manifest = ResultManifest(
            experiment_id=manifest.experiment_id, verdict=manifest.verdict,
            started_at=manifest.started_at, finished_at=manifest.finished_at,
            confinement=manifest.confinement, input_digest=manifest.input_digest,
            result=manifest.result, stdout=manifest.stdout,
            stderr=manifest.stderr, exit_code=manifest.exit_code,
            wall_seconds=manifest.wall_seconds, destruction=destruction)
        return ResultManifest(
            experiment_id=signed_manifest.experiment_id,
            verdict=signed_manifest.verdict,
            started_at=signed_manifest.started_at,
            finished_at=signed_manifest.finished_at,
            confinement=signed_manifest.confinement,
            input_digest=signed_manifest.input_digest,
            result=signed_manifest.result, stdout=signed_manifest.stdout,
            stderr=signed_manifest.stderr, exit_code=signed_manifest.exit_code,
            wall_seconds=signed_manifest.wall_seconds,
            destruction=destruction,
            signature=sign_artifact(signed_manifest.result_digest))

    # -- steps -------------------------------------------------------------

    def _materialise(self, root: Path, spec: ExperimentSpec,
                     inputs: SignedInputs) -> None:
        (root / "tmp").mkdir()
        inputs_dir = root / "inputs"
        inputs_dir.mkdir()
        for name, path in spec.datasets.items():
            target = inputs_dir / name
            shutil.copyfile(path, target)
            target.chmod(0o444)
        payload = dict(inputs.payload)
        payload["__digest__"] = inputs.digest
        (root / "payload.json").write_text(json.dumps(payload, default=str))
        if spec.source:
            (root / "experiment.py").write_text(spec.source)
        machine = platform.machine()
        (root / "runner.py").write_text(_RUNNER.format(
            blocked=list(BLOCKED_IMPORTS),
            syscalls=dict(_SYSCALL_NUMBERS.get(machine) or {}),
            audit_arch=_AUDIT_ARCH.get(machine)))

    def _launch(self, root: Path, spec: ExperimentSpec):
        env = self.child_environment(
            root, allow_repo_imports=self.allow_repo_imports)
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(root / "runner.py")],
            cwd=str(root), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            preexec_fn=_apply_confinement(
                spec.budget,
                read_only_dir=str(root / "inputs") if spec.datasets else ""))
        timed_out = False
        try:
            stdout, stderr = process.communicate(
                timeout=spec.budget.wall_clock_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_group(process)
            stdout, stderr = process.communicate()

        class _Completed:
            pass

        completed = _Completed()
        completed.stdout, completed.stderr = stdout, stderr
        completed.returncode = process.returncode
        completed.pid = process.pid
        return completed, timed_out

    @staticmethod
    def _kill_group(process) -> None:
        """Kill the whole group. A worker that forked leaves children behind
        if only the leader is signalled."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(process.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.kill()
                except Exception:                        # noqa: BLE001
                    pass
                return
            time.sleep(0.05)
            if process.poll() is not None:
                return

    @staticmethod
    def _read_report(root: Path) -> dict:
        path = root / "result.json"
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError):
            return {}

    def _confinement(self, spec: ExperimentSpec, inputs: SignedInputs,
                     report: Mapping[str, Any], *, timed_out: bool
                     ) -> Confinement:
        """Merge what the parent asked for with what the worker observed."""
        probe = dict(report.get("probe") or {})
        observed = bool(probe)
        states: list[MechanismState] = []

        def add(mechanism, applied, detail, basis="observed"):
            states.append(MechanismState(mechanism=mechanism, applied=applied,
                                         detail=detail, basis=basis))

        add(Mechanism.SEPARATE_PROCESS, observed and probe.get("pid") != os.getpid(),
            f"worker pid {probe.get('pid')} against parent {os.getpid()}"
            if observed else "the worker produced no probe")

        leaked = sorted(set(probe.get("env_keys") or ())
                        - set(ENV_ALLOWLIST)
                        - {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
                           "OLYMPUS_RESEARCH_WORKER", "LC_CTYPE"})
        add(Mechanism.SCRUBBED_ENVIRONMENT, observed and not leaked,
            f"unexpected variables in the worker: {leaked}" if leaked
            else "only allowlisted variables present" if observed
            else "the worker produced no probe")

        add(Mechanism.EPHEMERAL_WORKDIR,
            observed and str(probe.get("cwd", "")).startswith(
                tempfile.gettempdir()),
            f"worker cwd {probe.get('cwd')}" if observed
            else "the worker produced no probe")

        # Judged on the namespace alone, deliberately. The seccomp filter also
        # stops a socket being created, and letting that count here would mean
        # a silently failed unshare reads as a working network namespace —
        # two mechanisms that are supposed to fail independently sharing one
        # observation.
        interfaces = list(probe.get("interfaces") or [])
        no_socket = probe.get("socket_created") is False
        own_namespace = bool(probe.get("netns")) and probe["netns"] != _own_netns()
        only_loopback = interfaces in ([], ["lo"])
        add(Mechanism.NETWORK_NAMESPACE,
            observed and own_namespace and only_loopback,
            f"worker netns {probe.get('netns')} against parent "
            f"{_own_netns()}; interfaces {interfaces}" if observed
            else "the worker produced no probe")

        seccomp_error = probe.get("seccomp_error", "no probe")
        add(Mechanism.SECCOMP_FILTER,
            observed and not seccomp_error and no_socket,
            f"filter install failed: {seccomp_error}" if seccomp_error
            else probe.get("socket_error", "the filter installed and socket "
                                           "creation still succeeded"))
        add(Mechanism.NO_NEW_PRIVS, True,
            "requested via prctl in preexec; not separately observable from "
            "inside the worker", basis="asserted")
        add(Mechanism.BLOCKED_IMPORTS,
            bool(probe.get("blocked_import_enforced")),
            f"the worker could not import {BLOCKED_IMPORTS[0]}"
            if probe.get("blocked_import_enforced")
            else "the import hook did not refuse a blocked module")

        cpu = (probe.get("RLIMIT_CPU") or [None])[0]
        add(Mechanism.CPU_LIMIT,
            cpu is not None and cpu <= spec.budget.cpu_seconds,
            f"RLIMIT_CPU soft={cpu} against a budget of "
            f"{spec.budget.cpu_seconds}s")
        memory = (probe.get("RLIMIT_AS") or [None])[0]
        add(Mechanism.MEMORY_LIMIT,
            memory is not None and 0 < memory <= spec.budget.memory_mb * 1024 * 1024,
            f"RLIMIT_AS soft={memory} against a budget of "
            f"{spec.budget.memory_mb}MB")
        fsize = (probe.get("RLIMIT_FSIZE") or [None])[0]
        add(Mechanism.FILE_SIZE_LIMIT,
            fsize is not None and 0 < fsize <= spec.budget.disk_mb * 1024 * 1024,
            f"RLIMIT_FSIZE soft={fsize}; this caps the largest single file, "
            f"not total disk")
        add(Mechanism.PROCESS_LIMIT, False,
            "RLIMIT_NPROC counts processes per real UID across the whole "
            "system rather than per process tree, so applying it here would "
            "count the parent's siblings; the wall clock and the process-group "
            "kill are what bound a runaway", basis="asserted")

        add(Mechanism.WALL_CLOCK_TIMEOUT, True,
            f"communicate() bounded at {spec.budget.wall_clock_seconds}s"
            + ("; it fired" if timed_out else "; it did not fire"),
            basis="asserted")
        modes = dict(probe.get("input_modes") or {})
        writable = sorted(name for name, mode in modes.items()
                          if "WRITABLE" in mode)
        add(Mechanism.READ_ONLY_INPUTS,
            not writable,
            f"{len(modes)} dataset(s), modes {modes}" if modes
            else "vacuous: no datasets were supplied, so there was nothing to "
                 "make read-only",
            basis="observed" if modes else "asserted")
        add(Mechanism.SIGNED_INPUTS, inputs.verify(),
            "input payload signature verifies" if inputs.verify()
            else "input payload is unsigned or does not verify")
        add(Mechanism.SIGNED_RESULTS, True,
            "the parent signs the result manifest; the worker holds no key, "
            "because a worker that could sign could sign a result it invented",
            basis="asserted")
        add(Mechanism.WORKER_DESTRUCTION, True,
            "the workdir is removed in a finally block and the removal is "
            "verified", basis="asserted")
        return Confinement(states=tuple(states))

    #: Signals the kernel raises when a resource limit is hit. Checked before
    #: the confinement verdict, because a worker killed by its own CPU limit
    #: never wrote a probe — and reading the missing probe as "confinement
    #: failed" would report the limit working as the limit broken.
    _LIMIT_SIGNALS = {signal.SIGXCPU, signal.SIGXFSZ, signal.SIGKILL,
                      signal.SIGSEGV}

    @classmethod
    def _verdict(cls, spec, report, completed, timed_out, confinement,
                 trusted_reason: str) -> Verdict:
        if timed_out:
            return Verdict.TIMED_OUT
        code = completed.returncode
        if code is not None and code < 0 and -code in {
                int(s) for s in cls._LIMIT_SIGNALS}:
            return Verdict.LIMIT_EXCEEDED
        if spec.generated and not confinement.adequate_for_generated_code:
            return Verdict.CONFINEMENT_FAILED
        if not report or not report.get("ok"):
            return Verdict.FAILED
        return Verdict.COMPLETED

    @staticmethod
    def _destroy(root: Path) -> dict:
        """Remove the worker directory and verify it is gone."""
        error = ""
        try:
            shutil.rmtree(root, ignore_errors=False)
        except OSError as failure:
            error = f"{type(failure).__name__}: {failure}"
            shutil.rmtree(root, ignore_errors=True)
        return {"workdir": str(root), "workdir_removed": not root.exists(),
                "error": error}


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------

PROBE_SOURCE = "def run(inputs):\n    return {'probed': True}\n"


def probe_confinement(*, budget: ComputeBudget | None = None) -> Confinement:
    """Run a do-nothing experiment to find out what this host can enforce.

    Called before a real experiment so the decision to run generated code is
    made on observation rather than on hope, and callable on its own so an
    operator can ask "would research be isolated here?" without running any.
    """
    spec = ExperimentSpec(experiment_id="confinement-probe",
                          source=PROBE_SOURCE,
                          budget=budget or ComputeBudget(cpu_seconds=10,
                                                         wall_clock_seconds=30))
    return IsolatedWorker().run(spec).confinement


def run_isolated(spec: ExperimentSpec, *, trusted_reason: str = ""
                 ) -> ResultManifest:
    """Sign, run, verify and destroy. The one entry point callers want.

    Generated code that cannot be confined is **refused before it starts**,
    with a manifest recording the shortfall. Running it anyway and labelling
    the result untrustworthy would still have run it.
    """
    signed = sign_inputs(spec)
    if spec.generated and not trusted_reason:
        available = probe_confinement(budget=spec.budget)
        if not available.adequate_for_generated_code:
            now = datetime.now(timezone.utc)
            return ResultManifest(
                experiment_id=spec.experiment_id, verdict=Verdict.REFUSED,
                started_at=now, finished_at=now, confinement=available,
                input_digest=signed.digest,
                stderr="this host cannot confine generated code: "
                       + ", ".join(available.shortfall),
                destruction={"workdir_removed": True,
                             "workdir": "(never created)"})
    return IsolatedWorker().run(spec, signed=signed,
                                trusted_reason=trusted_reason)


def isolation_report() -> dict:
    """The confinement this host offers, as data. Feeds the docs and the CLI."""
    confinement = probe_confinement()
    return {"schema_version": ISOLATION_SCHEMA_VERSION,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "seccomp_supported": platform.machine() in _SYSCALL_NUMBERS,
            "env_allowlist": list(ENV_ALLOWLIST),
            "blocked_imports": list(BLOCKED_IMPORTS),
            "blocked_syscalls": list(BLOCKED_SYSCALLS),
            "required_for_generated_code": sorted(
                m.value for m in REQUIRED_FOR_GENERATED_CODE),
            "confinement": confinement.to_dict()}


__all__ = ["ISOLATION_SCHEMA_VERSION", "ENV_ALLOWLIST", "BLOCKED_IMPORTS",
           "BLOCKED_SYSCALLS", "Mechanism", "REQUIRED_FOR_GENERATED_CODE",
           "MechanismState", "Confinement", "ExperimentSpec", "SignedInputs",
           "sign_inputs", "digest_of", "Verdict", "ResultManifest",
           "IsolatedWorker", "probe_confinement", "run_isolated",
           "isolation_report"]
