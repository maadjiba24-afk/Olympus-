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
**inside a cgroup that bounds the whole process tree**, with a scrubbed
environment, behind a seccomp filter, on a **sized tmpfs** that is unmounted
and destroyed afterwards.

Why a cgroup and not rlimits
----------------------------
Generated code can `fork()`. Every `RLIMIT_*` is **per process**, so a worker
that forks ten children gets ten times its memory allowance and ten times its
CPU allowance, and `RLIMIT_NPROC` — the one limit that sounds like the answer —
counts processes **per real UID across the whole system**, not per process
tree. Setting it to 8 in a container where that UID already runs more than
eight processes stops the worker starting; where it does apply it is shared
with every unrelated process the same UID owns. It is not a per-experiment
bound, and this module does not count it as one.

`pids.max`, `memory.max` and `cpu.max` in a cgroup bound the **tree**, and a
process cannot leave the cgroup it was put into. Termination is the same story:
`killpg` misses anything that called `setsid()`, whereas freezing a cgroup and
killing its members reaches every descendant and can be *proven* to have done
so, because the member list afterwards is empty. See `cgroups.py`.

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

Fail closed, and only on Linux
------------------------------
Every control above is Linux-specific. On Windows and macOS `import olympus`
still succeeds — nothing Unix-only is imported at module scope — and any
attempt to *run* generated code raises `IsolationUnavailable`, naming the
platform. On a Linux host that cannot delegate cgroup controllers, the same
refusal is raised and `run_isolated` turns it into a `REFUSED` manifest listing
the shortfall. There is no path that runs generated code with fewer controls
than `REQUIRED_FOR_GENERATED_CODE`.

Known limitations, stated because they are real
-----------------------------------------------
- **`RLIMIT_FSIZE` caps the largest single file, not total disk.** It is kept
  as defence in depth under its own name, `FILE_SIZE_LIMIT`; the actual disk
  bound, `DISK_QUOTA`, is a tmpfs mounted with `size=`, which caps the total.
- **The cgroup CPU quota is a rate, not a total.** `cpu.max` bounds the tree to
  one core; total CPU is bounded by the wall clock on top of it. Both are
  reported, neither is described as the other.
- **This is not a defence against a hostile human with a shell.** It is a
  defence against code Olympus generated, running unattended.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from ..errors import ConfigurationError
from .challengers import ComputeBudget

# `ctypes`, `resource`, `signal.SIGXCPU` and everything under `cgroups` are
# Unix-only or Linux-only. They are imported *inside* the functions that use
# them, so that `import olympus` succeeds on Windows and macOS and the refusal
# to run generated code there is typed and explicit rather than an ImportError
# at package load. `IsolationUnavailable` is re-exported from here because it
# is the exception callers on every platform need to catch.
from .cgroups import IsolationUnavailable                       # noqa: F401

#: Bumped when the confinement contract changes.
ISOLATION_SCHEMA_VERSION = 1

#: Environment variables the worker is given. An allowlist: anything not named
#: here is absent, so a new secret-bearing variable is excluded by default
#: rather than by somebody remembering to add it to a denylist.
ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "TZ", "HOME",
                                  "TMPDIR", "PYTHONHASHSEED")

#: Variables the parent *adds* rather than passes through. Named here so the
#: scrubbed-environment check can tell "Olympus set this" apart from "something
#: leaked in", which is the only distinction that check is trying to make.
ENV_SET_BY_PARENT: tuple[str, ...] = (
    "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "OLYMPUS_RESEARCH_WORKER",
    "OLYMPUS_REPORT_FD", "HOME", "TMPDIR", "PATH")

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
#: The runner's report is a probe plus whatever the experiment returned. Capped
#: so a generated experiment cannot fill the parent's memory through the pipe.
_REPORT_CAP = 4_000_000


class Mechanism(str, Enum):
    """Every confinement this module attempts, named individually.

    One enum member per mechanism rather than a single `isolated: bool`,
    because "isolated" is a summary and the interesting question is always
    *which one* is missing on a given host.
    """

    SEPARATE_PROCESS = "separate_process"
    SCRUBBED_ENVIRONMENT = "scrubbed_environment"
    NO_PRODUCTION_SECRETS = "no_production_secrets"
    EPHEMERAL_WORKDIR = "ephemeral_workdir"
    READ_ONLY_INPUTS = "read_only_inputs"
    NETWORK_NAMESPACE = "network_namespace"
    SECCOMP_FILTER = "seccomp_filter"
    NO_NEW_PRIVS = "no_new_privs"
    BLOCKED_IMPORTS = "blocked_imports"
    CPU_LIMIT = "cpu_limit"
    MEMORY_LIMIT = "memory_limit"
    FILE_SIZE_LIMIT = "file_size_limit"
    #: Descendant process-count limit, enforced by a cgroup. **Not**
    #: `RLIMIT_NPROC`, which counts per real UID across the whole system and is
    #: therefore neither a per-experiment bound nor safe to set in a container.
    PROCESS_LIMIT = "process_limit"
    #: A bounded writable filesystem — a sized tmpfs over the work directory.
    #: `RLIMIT_FSIZE` caps one file; this caps the total.
    DISK_QUOTA = "disk_quota"
    WALL_CLOCK_TIMEOUT = "wall_clock_timeout"
    #: Every descendant is dead afterwards, proven by an empty cgroup member
    #: list. A process-group kill misses anything that called `setsid()`.
    DESCENDANT_TERMINATION = "descendant_termination"
    SIGNED_INPUTS = "signed_inputs"
    SIGNED_RESULTS = "signed_results"
    WORKER_DESTRUCTION = "worker_destruction"


#: The fifteen controls that must **all** hold before generated code may run.
#:
#: `READ_ONLY_INPUTS` is listed unconditionally rather than "when datasets are
#: supplied": with no datasets it is satisfied vacuously (there is nothing to
#: make writable), and with datasets it is satisfied only by the worker's own
#: write probe failing. Making the requirement conditional would put the
#: decision "were there datasets?" between the check and the guarantee, which is
#: exactly where a mistake hides.
#:
#: `SECCOMP_FILTER`, `NO_NEW_PRIVS`, `BLOCKED_IMPORTS` and `FILE_SIZE_LIMIT`
#: are deliberately absent: they are defence in depth, worth having, and not
#: what stands between a generated experiment and the host.
REQUIRED_FOR_GENERATED_CODE: frozenset[Mechanism] = frozenset({
    Mechanism.SEPARATE_PROCESS, Mechanism.SCRUBBED_ENVIRONMENT,
    Mechanism.NO_PRODUCTION_SECRETS, Mechanism.EPHEMERAL_WORKDIR,
    Mechanism.READ_ONLY_INPUTS, Mechanism.NETWORK_NAMESPACE,
    Mechanism.CPU_LIMIT, Mechanism.MEMORY_LIMIT,
    Mechanism.PROCESS_LIMIT, Mechanism.DISK_QUOTA,
    Mechanism.WALL_CLOCK_TIMEOUT, Mechanism.DESCENDANT_TERMINATION,
    Mechanism.WORKER_DESTRUCTION, Mechanism.SIGNED_INPUTS,
    Mechanism.SIGNED_RESULTS,
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
# what this host can enforce, decided before anything is launched
# ---------------------------------------------------------------------------

#: The only platform on which any of this is implemented. Named once, so a
#: future port has one place to change and the refusal has one reason to give.
SUPPORTED_PLATFORM = "linux"


@dataclass(frozen=True)
class HostSupport:
    """Whether this host can confine generated code, decided without running it.

    Answered from the platform, the cgroup hierarchy and a real tmpfs mount —
    not from a list of hosts believed to be fine. `can_run_generated_code` is
    computed; there is no argument that sets it.
    """

    platform_name: str
    supported_platform: bool
    cgroups: Any = None                 # cgroups.CgroupSupport | None
    tmpfs: bool = False
    seccomp_arch: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "detail", dict(self.detail))

    @property
    def missing(self) -> tuple[str, ...]:
        out: list[str] = []
        if not self.supported_platform:
            return (f"platform {self.platform_name!r} is not {SUPPORTED_PLATFORM}"
                    f": no confinement is implemented here",)
        if self.cgroups is None or not self.cgroups.complete:
            names = list(self.cgroups.missing) if self.cgroups is not None \
                else ["no cgroup hierarchy"]
            out.extend(f"cgroup:{name}" for name in names)
        if not self.tmpfs:
            out.append("disk_quota: no sized tmpfs could be mounted")
        return tuple(out)

    @property
    def can_run_generated_code(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict:
        return {"platform": self.platform_name,
                "supported_platform": self.supported_platform,
                "cgroups": self.cgroups.to_dict() if self.cgroups is not None
                else None,
                "tmpfs": self.tmpfs, "seccomp_arch": self.seccomp_arch,
                "can_run_generated_code": self.can_run_generated_code,
                "missing": list(self.missing), "detail": dict(self.detail)}


def platform_supported() -> bool:
    """True only on Linux. Every mechanism in this module is Linux-specific."""
    return sys.platform.startswith(SUPPORTED_PLATFORM)


def require_platform() -> None:
    """Raise the typed refusal on a host where confinement is not implemented.

    Called by everything that would launch a worker for generated code. The
    exception type matters: a caller's correct response is to *refuse*, and an
    `ImportError` from a Unix-only module at package load would instead make
    `import olympus` fail on Windows, which is a different and worse bug.
    """
    if not platform_supported():
        support = host_support()
        raise IsolationUnavailable(
            "generated research code can only be confined on Linux; this host "
            "offers no process-count limit, no descendant termination and no "
            "bounded writable filesystem",
            platform=sys.platform,
            required=SUPPORTED_PLATFORM,
            missing=list(support.missing),
        )


def _tmpfs_probe() -> tuple[bool, str]:
    """Mount a 1 MiB tmpfs and unmount it. The only honest test of the answer."""
    if not platform_supported():
        return False, f"not {SUPPORTED_PLATFORM}"
    root = tempfile.mkdtemp(prefix="olympus-tmpfs-probe-")
    try:
        _mount_tmpfs(Path(root), size_mb=1)
    except OSError as error:
        return False, f"{type(error).__name__}: {error}"
    finally:
        _umount(Path(root))
        try:
            os.rmdir(root)
        except OSError:
            pass
    return True, "a sized tmpfs mounted and unmounted"


def host_support() -> HostSupport:
    """What this host can enforce. Never raises; the caller decides."""
    name = sys.platform
    if not platform_supported():
        return HostSupport(platform_name=name, supported_platform=False)
    from . import cgroups as _cgroups
    tmpfs_ok, tmpfs_detail = _tmpfs_probe()
    return HostSupport(
        platform_name=name, supported_platform=True,
        cgroups=_cgroups.probe(), tmpfs=tmpfs_ok,
        seccomp_arch=platform.machine() in _SYSCALL_NUMBERS,
        detail={"tmpfs": tmpfs_detail, "machine": platform.machine()})


# ---------------------------------------------------------------------------
# the confinement, applied in the child before exec
# ---------------------------------------------------------------------------

def _libc():
    import ctypes                                       # Unix-only, lazy
    return ctypes.CDLL("libc.so.6", use_errno=True)


def _mount_tmpfs(root: Path, *, size_mb: int) -> None:
    """A bounded writable filesystem for the worker.

    `RLIMIT_FSIZE` caps the largest single file; ten files of the limit still
    fill the disk. `size=` on a tmpfs caps the total, and the worker gets
    `ENOSPC` at the boundary rather than the host filling up.

    Mounted in the **parent's** namespace on purpose: a tmpfs mounted inside
    the child's mount namespace would be invisible to the parent, which then
    could not read `result.json` back out of it.
    """
    import ctypes
    libc = _libc()
    options = f"size={int(size_mb)}m".encode("utf-8")
    if libc.mount(b"tmpfs", str(root).encode("utf-8"), b"tmpfs", 0, options) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(root))


def _umount(root: Path) -> bool:
    """Detach the tmpfs. `MNT_DETACH`, so a lingering open fd cannot pin it."""
    if not platform_supported():
        return False
    try:
        return _libc().umount2(str(root).encode("utf-8"), 2) == 0
    except OSError:
        return False


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


def _apply_confinement(budget: ComputeBudget, *, read_only_dir: str = "",
                       scope=None):
    """Run in the child between fork and exec. Best effort, silently.

    Silent because a `preexec_fn` that raised would abort the launch and tell
    us only that something failed. What is applied is established afterwards by
    the worker's own probe, which is a stronger claim than this function's
    return value could ever be.

    The **cgroup join is the exception**: it happens here because it must
    happen before the child execs anything, and every descendant then inherits
    membership. It is also the one step whose success the parent can verify
    independently — the member list contains the worker's pid or it does not.

    `RLIMIT_NPROC` is *not* set. It counts per real UID across the system, not
    per process tree; `pids.max` on the cgroup is what bounds forking.

    **The seccomp filter is not applied here**, and the reason is worth
    recording: this function runs *before* `execve`, and the filter denies
    `execve`. Installing it here means the worker can never start. The filter
    is therefore installed by the runner as its first act, after exec — which
    is also when it starts being useful, because everything before exec is code
    this module wrote.
    """
    import resource                                     # Unix-only, lazy

    def _preexec():
        try:
            os.setsid()                       # own group, so a kill reaches all
        except OSError:
            pass
        if scope is not None:
            # Deliberately not wrapped: a child that is outside its cgroup is
            # unbounded, and launching it anyway would produce a result the
            # manifest would have to call trustworthy. Raising here aborts the
            # launch, which is the fail-closed outcome.
            scope.join_current_process()
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

def _drain(fd: int, into: list) -> None:
    """Read a pipe to EOF. Runs on a thread so the runner cannot block on it."""
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            if sum(len(c) for c in into) < _REPORT_CAP:
                into.append(chunk)
    except OSError:
        return
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _parse_report(raw: bytes, nonce: str) -> dict:
    """The last line carrying the parent's nonce. Anything else is discarded.

    The nonce is read and deleted by the runner before it forks, so it exists
    only in the runner's memory. A line without it was not written by the
    runner — and a report the experiment wrote about its own confinement is
    exactly what this is here to throw away.
    """
    if not raw:
        return {}
    found: dict = {}
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            candidate = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(candidate, dict) and candidate.get("nonce") == nonce:
            found = candidate
    found.pop("nonce", None)
    return found


def _read_capped(path: Path, limit: int = _OUTPUT_CAP) -> str:
    """The tail of a worker output file. Bounded, because the worker's disk is."""
    try:
        return path.read_text(errors="replace")[-limit:]
    except OSError:
        return ""


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

    Two fields describe the run and neither is the same question:

    * **`outcome`** is what the *process* did — it completed, it failed, the
      kernel killed it, the wall clock fired. It is an observation.
    * **`verdict`** is what the *result is worth*, and it is a **computed
      property**. A run that completed under failed confinement reports
      `CONFINEMENT_FAILED`, and there is no argument to the constructor that
      can say otherwise. A stored verdict could be set to `COMPLETED` by
      anything that built a manifest; a computed one cannot.

    `trustworthy` is likewise computed, from the verdict, every load-bearing
    control, a signature that actually verifies, the removal of the work
    directory and the absence of surviving descendants. A result produced under
    failed confinement is not a result — it is an observation about a process
    that was not isolated, and treating it as evidence is how an experiment
    that reached production gets its findings believed.
    """

    experiment_id: str
    outcome: Verdict
    started_at: datetime
    finished_at: datetime
    confinement: Confinement
    input_digest: str
    #: Whether the code was generated. Generated code is held to
    #: `REQUIRED_FOR_GENERATED_CODE`; hand-written code is not.
    generated: bool = True
    result: Mapping[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    wall_seconds: float = 0.0
    signature: Mapping[str, str] = field(default_factory=dict)
    destruction: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = ISOLATION_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "outcome", Verdict(self.outcome))
        object.__setattr__(self, "result", dict(self.result))
        object.__setattr__(self, "signature", dict(self.signature))
        object.__setattr__(self, "destruction", dict(self.destruction))

    @property
    def verdict(self) -> Verdict:
        """Computed. There is no argument that sets this."""
        if self.outcome in (Verdict.REFUSED, Verdict.TIMED_OUT,
                            Verdict.LIMIT_EXCEEDED):
            return self.outcome
        if self.generated and not self.confinement.adequate_for_generated_code:
            return Verdict.CONFINEMENT_FAILED
        return self.outcome

    @property
    def result_digest(self) -> str:
        """What the parent signs.

        Covers the outcome, the inputs, the result and **the confinement** —
        so a stored manifest cannot have its mechanism states edited without
        breaking the signature, which is what makes `trustworthy` worth reading
        off disk.

        `SIGNED_RESULTS` is the one mechanism excluded, and the reason is
        structural rather than convenient: it is established *by* producing
        this signature and verifying it, so it cannot also be inside what the
        signature covers.
        """
        return digest_of({"experiment_id": self.experiment_id,
                          "outcome": self.outcome.value,
                          "generated": self.generated,
                          "input_digest": self.input_digest,
                          "exit_code": self.exit_code,
                          "result": dict(self.result),
                          "confinement": [
                              s.to_dict() for s in self.confinement.states
                              if s.mechanism is not Mechanism.SIGNED_RESULTS]})

    @property
    def signed(self) -> bool:
        return bool(self.signature.get("signature"))

    @property
    def survivors(self) -> tuple[int, ...]:
        """Descendants still alive afterwards. Empty is the only good answer."""
        return tuple(self.destruction.get("survivors") or ())

    @property
    def trustworthy(self) -> bool:
        """Computed from every load-bearing control. None of them settable.

        Not "the verdict was COMPLETED". A result is worth something only when
        the confinement held, the signature over it verifies *now*, the work
        directory is gone and nothing the experiment started is still running.
        """
        return (self.verdict is Verdict.COMPLETED
                and self.confinement.adequate_for_generated_code
                and self.confinement.applied(Mechanism.SIGNED_RESULTS)
                and self.signed and self.verify()
                and bool(self.destruction.get("workdir_removed"))
                and not self.survivors)

    @property
    def untrustworthy_because(self) -> tuple[str, ...]:
        """Why `trustworthy` is False. Empty when it is True."""
        out: list[str] = []
        if self.verdict is not Verdict.COMPLETED:
            out.append(f"verdict is {self.verdict.value}")
        out.extend(f"missing {name}" for name in self.confinement.shortfall)
        if not self.signed:
            out.append("the result manifest is unsigned")
        elif not self.verify():
            out.append("the result signature does not verify")
        if not self.destruction.get("workdir_removed"):
            out.append("the work directory was not removed")
        if self.survivors:
            out.append(f"descendants survived: {list(self.survivors)}")
        return tuple(out)

    def verify(self) -> bool:
        if not self.signed:
            return False
        from .pipeline import verify_artifact
        return verify_artifact(self.result_digest, self.signature)

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version,
                "experiment_id": self.experiment_id,
                "outcome": self.outcome.value,
                "verdict": self.verdict.value,
                "generated": self.generated,
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
                "survivors": list(self.survivors),
                "trustworthy": self.trustworthy,
                "untrustworthy_because": list(self.untrustworthy_because),
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

The experiment does not run in this process
-------------------------------------------
The first version of this runner probed its confinement and then `exec`'d the
generated code in the same interpreter, writing the report afterwards. An
adversarial test broke it in four lines: the experiment wrote its own
`result.json` claiming a Sharpe of 99 under a perfect probe, then called
`os._exit(0)` so nothing overwrote it.

So the experiment runs in a **forked child**, and the report is written by this
process — which the child cannot `os._exit`, cannot outlive by default, and
cannot rewrite. Three details make that hold:

* the report goes down a **pipe to the parent**, not to a file, so nothing that
  outlives this process can append to it;
* the child **closes that pipe fd** before any generated code runs;
* the report carries a **nonce** the parent generated and this process read and
  deleted before forking, so a line the child somehow injected is discarded
  rather than believed.

What the child still controls is its own return value, and that is not a
weakness — choosing what to return is what an experiment is for. The
confinement is established by the parent from outside regardless.
"""
import ctypes, hashlib, json, os, resource, sys, time, traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
BLOCKED = {blocked!r}
SYSCALLS = {syscalls!r}
AUDIT_ARCH = {audit_arch!r}
REPORT_FD = int(os.environ.get("OLYMPUS_REPORT_FD", "-1"))


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
    # Membership is what makes pids.max / memory.max / cpu.max mean anything.
    # Read from inside, because the parent's view of cgroup.procs is empty once
    # the worker has exited and sampling it while running is a race.
    try:
        with open("/proc/self/cgroup") as handle:
            out["cgroup"] = handle.read().strip()
    except OSError:
        out["cgroup"] = ""
    # Is the work directory actually a bounded filesystem? statvfs reports the
    # tmpfs size when one is mounted and the host filesystem's when not, so a
    # missing mount is visible as an implausibly large answer rather than as
    # nothing at all.
    try:
        stat = os.statvfs(str(HERE))
        out["workdir_bytes"] = stat.f_blocks * stat.f_frsize
        out["workdir_fstype_hint"] = stat.f_blocks
    except OSError:
        out["workdir_bytes"] = None
    out["pid"] = os.getpid()
    out["ppid"] = os.getppid()
    out["cwd"] = os.getcwd()
    out["env_keys"] = sorted(os.environ)
    # Reported, never the values. A worker that printed a secret's value into
    # its own result file would have exfiltrated it to the manifest.
    out["secret_shaped_env"] = sorted(
        name for name in os.environ
        if any(mark in name.upper() for mark in
               ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "API",
                "VAULT", "BROKER", "PRIVATE")))
    out["blocked_import_enforced"] = False
    try:
        __import__(BLOCKED[0])
    except ImportError:
        out["blocked_import_enforced"] = True
    except Exception:
        out["blocked_import_enforced"] = True
    return out


def wait_for_go():
    """Block until the parent has finished observing this process from outside.

    The parent reads /proc/<pid>/cgroup and /proc/<pid>/ns/net to establish the
    confinement itself rather than believing this worker's own report. Without
    a handshake that read races a fast experiment to exit, and a race the
    parent loses degrades to trusting the worker — which is exactly what an
    experiment that wanted to forge its confinement would arrange.
    """
    deadline = time.time() + 60
    while time.time() < deadline:
        if (HERE / "go").exists():
            return True
        time.sleep(0.002)
    return False


def take_nonce():
    """Read the parent's nonce and remove the file, before anything forks.

    In memory from here on. The child that runs the experiment never sees the
    file, so a report line it manufactures carries no nonce and is discarded.
    """
    path = HERE / "nonce"
    try:
        value = path.read_text().strip()
    except OSError:
        return ""
    try:
        path.unlink()
    except OSError:
        pass
    return value


def emit(report, nonce):
    """Write the report down the parent's pipe. One line, nonce first."""
    report["nonce"] = nonce
    line = json.dumps(report, default=str) + "\\n"
    if REPORT_FD >= 0:
        data = line.encode("utf-8")
        while data:
            data = data[os.write(REPORT_FD, data):]
        os.close(REPORT_FD)
    else:                       # pragma: no cover - the parent always passes one
        (HERE / "result.json").write_text(line)


def run_experiment(payload):
    """Resolve and call the experiment. Runs in the forked child only."""
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
    return run(inputs)


def main():
    seccomp_error = install_seccomp()
    if not wait_for_go():
        return 4
    nonce = take_nonce()
    # Nothing in this process's memory should be readable through /proc by the
    # child. Ineffective when the worker runs as uid 0 — root reads anything —
    # which is why no load-bearing mechanism depends on this report.
    try:
        ctypes.CDLL("libc.so.6").prctl(4, 0, 0, 0, 0)     # PR_SET_DUMPABLE
    except Exception:
        pass
    sys.meta_path.insert(0, _Blocker())
    payload = json.loads((HERE / "payload.json").read_text())
    expected = payload.pop("__digest__")
    recomputed = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        default=str).encode("utf-8")).hexdigest()
    report = {{"probe": probe(seccomp_error), "input_digest": recomputed}}
    if recomputed != expected:
        report["ok"] = False
        report["error"] = "input digest mismatch: the payload changed between "\\
                          "signing and reading"
        emit(report, nonce)
        return 3

    pid = os.fork()
    if pid == 0:
        # The child. Close the report pipe before a single line of generated
        # code runs: from here on this process cannot reach the channel the
        # parent believes.
        if REPORT_FD >= 0:
            try:
                os.close(REPORT_FD)
            except OSError:
                pass
        outcome = {{}}
        code = 0
        try:
            outcome = {{"ok": True, "result": run_experiment(payload)}}
        except BaseException as error:
            outcome = {{"ok": False,
                       "error": "%s: %s" % (type(error).__name__, error),
                       "traceback": traceback.format_exc()[-4000:]}}
            code = 1
        try:
            (HERE / "experiment.json").write_text(
                json.dumps(outcome, default=str))
        except OSError as error:
            code = 5
        os._exit(code)

    _, status = os.waitpid(pid, 0)
    report["child_exit"] = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
    report["child_signal"] = os.WTERMSIG(status) if os.WIFSIGNALED(status) else 0
    outcome = {{}}
    try:
        outcome = json.loads((HERE / "experiment.json").read_text())
    except (OSError, ValueError) as error:
        outcome = {{"ok": False,
                   "error": "the experiment produced no result: %s" % error}}
    if not isinstance(outcome, dict):
        outcome = {{"ok": False, "error": "the experiment result was not an object"}}
    if report["child_signal"]:
        outcome = {{"ok": False,
                   "error": "the experiment was killed by signal %s"
                            % report["child_signal"]}}
    elif outcome.get("ok") and report["child_exit"] not in (0, None):
        # The child wrote a success file and then exited badly. Believing the
        # file would report a run that did not finish as one that did, and an
        # experiment that wanted to be believed would do exactly this.
        outcome = {{"ok": False,
                   "error": "the experiment claimed success and then exited "
                            "with status %s" % report["child_exit"]}}
    report["ok"] = bool(outcome.get("ok"))
    if "result" in outcome:
        report["result"] = outcome["result"]
    for key in ("error", "traceback"):
        if outcome.get(key):
            report[key] = outcome[key]
    emit(report, nonce)
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

    __slots__ = ("_used", "_root", "_scope", "allow_repo_imports")

    def __init__(self, *, allow_repo_imports: bool = True):
        self._used = False
        self._root: Path | None = None
        self._scope = None
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

        **Raises `IsolationUnavailable` before launching anything** when the
        spec is generated and this host cannot enforce every control in
        `REQUIRED_FOR_GENERATED_CODE`. The check is on the host, not on the
        code: a host that cannot bound a process tree cannot bound a *harmless*
        experiment either, it just would not have needed to.

        `trusted_reason` exempts hand-written experiments from that check and
        is recorded in the manifest — an override nobody can find is an
        override that will be forgotten.
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

        # The platform check is unconditional. `trusted_reason` says "this code
        # was not generated", which is a statement about the code; it is not a
        # licence to launch a worker on a host where `preexec_fn`, network
        # namespaces and cgroups do not exist.
        require_platform()
        enforced = bool(spec.generated) and not trusted_reason
        if enforced:
            support = host_support()
            if not support.can_run_generated_code:
                raise IsolationUnavailable(
                    "this host cannot confine generated experiment code; "
                    "running it anyway would produce a result that no reader "
                    "could tell apart from one produced safely",
                    experiment_id=spec.experiment_id,
                    missing=list(support.missing))

        started = datetime.now(timezone.utc)
        root = Path(tempfile.mkdtemp(prefix="olympus-research-"))
        self._root = root
        quota_mb = 0
        report: dict = {}
        observed: dict = {}
        timed_out = False
        completed = None
        try:
            quota_mb = self._mount_quota(root, spec.budget)
            self._scope = self._make_scope(spec)
            self._materialise(root, spec, inputs)
            completed, timed_out = self._launch(root, spec, self._scope)
            report = self._read_report(completed)
            observed = self._observe(root, spec)
        finally:
            destruction = self._teardown(root, quota_mb=quota_mb)

        finished = datetime.now(timezone.utc)
        states = self._confinement_states(
            spec, inputs, report, completed=completed, observed=observed,
            timed_out=timed_out, quota_mb=quota_mb,
            destruction=destruction, trusted_reason=trusted_reason)
        outcome = self._outcome(report, completed, timed_out)
        stdout = (completed.stdout if completed else "") or ""
        stderr = (completed.stderr if completed else "") or ""

        common = dict(
            experiment_id=spec.experiment_id, outcome=outcome,
            started_at=started, finished_at=finished,
            input_digest=inputs.digest, generated=enforced,
            result=dict(report.get("result") or {}),
            stdout=stdout[-_OUTPUT_CAP:], stderr=stderr[-_OUTPUT_CAP:],
            exit_code=completed.returncode if completed else None,
            wall_seconds=(finished - started).total_seconds(),
            destruction=destruction)

        # SIGNED_RESULTS is *determined*, never assumed. The manifest is built
        # once without it, signed over its own digest, and the signature is
        # verified — and whichever way that comes out is what the mechanism
        # state says. Hard-coding True here would mean a host with no signing
        # key produced results that claimed to be signed.
        from .pipeline import sign_artifact, verify_artifact
        unsigned = ResultManifest(confinement=Confinement(states=tuple(states)),
                                  **common)
        digest = unsigned.result_digest
        signature = sign_artifact(digest)
        verified = bool(signature.get("signature")) and verify_artifact(
            digest, signature)
        states.append(MechanismState(
            mechanism=Mechanism.SIGNED_RESULTS, applied=verified,
            detail="the parent signed the result digest and the signature "
                   "verifies; the worker holds no key, because a worker that "
                   "could sign could sign a result it invented" if verified
                   else "the result manifest could not be signed, or the "
                        "signature produced does not verify"))
        return ResultManifest(confinement=Confinement(states=tuple(states)),
                              signature=signature, **common)

    # -- steps -------------------------------------------------------------

    @staticmethod
    def _mount_quota(root: Path, budget: ComputeBudget) -> int:
        """Bound the writable filesystem. Returns the size in MiB, 0 on failure.

        Zero is not fatal here — it is fatal in `_confinement_states`, where
        `DISK_QUOTA` reports unapplied and a generated run becomes
        `CONFINEMENT_FAILED`. Failing in one place keeps the decision in one
        place.
        """
        if not platform_supported():
            return 0
        try:
            _mount_tmpfs(root, size_mb=budget.disk_mb)
        except OSError:
            return 0
        return budget.disk_mb

    @staticmethod
    def _make_scope(spec: ExperimentSpec):
        """The cgroup that bounds the whole tree. None when unavailable."""
        if not platform_supported():
            return None
        from . import cgroups as _cgroups
        support = _cgroups.probe()
        if not support.complete:
            return None
        budget = spec.budget
        scope = _cgroups.CgroupScope(
            f"olympus-research-{os.getpid()}-{abs(hash(spec.experiment_id)):x}",
            support=support)
        try:
            # The CPU quota is a *rate*: one core, whatever the tree forks.
            # Total CPU is bounded by the wall clock on top of it, and both are
            # reported separately rather than one being described as the other.
            scope.create(max_processes=budget.max_processes,
                         memory_bytes=budget.memory_mb * 1024 * 1024,
                         cpu_quota_us=100_000, cpu_period_us=100_000)
        except (IsolationUnavailable, OSError):
            return None
        return scope

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

    def _launch(self, root: Path, spec: ExperimentSpec, scope):
        """Start the worker and wait for it, bounded by the wall clock.

        Output goes to **files, not pipes**, and that is not a style choice.
        `communicate()` returns when the pipes reach EOF, and a detached
        grandchild that inherited the write end keeps them open long after the
        worker has exited — so an experiment that leaks a descendant would hang
        the parent until the timeout and then be reported as a timeout, hiding
        the leak behind a less interesting failure. Waiting on the *process*
        and reading the files afterwards separates the two.
        """
        env = self.child_environment(
            root, allow_repo_imports=self.allow_repo_imports)
        nonce = secrets.token_hex(16)
        (root / "nonce").write_text(nonce)
        read_fd, write_fd = os.pipe()
        os.set_inheritable(write_fd, True)
        env["OLYMPUS_REPORT_FD"] = str(write_fd)
        out_path, err_path = root / "stdout.txt", root / "stderr.txt"
        collected: list[bytes] = []
        try:
            with open(out_path, "w") as out_handle, \
                    open(err_path, "w") as err_handle:
                process = subprocess.Popen(
                    [sys.executable, "-I", "-B", str(root / "runner.py")],
                    cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                    stdout=out_handle, stderr=err_handle,
                    pass_fds=(write_fd,),
                    preexec_fn=_apply_confinement(
                        spec.budget, scope=scope,
                        read_only_dir=str(root / "inputs")
                        if spec.datasets else ""))
        except BaseException:
            # A launch that never happened still opened a pipe. Both ends are
            # closed here because the drain thread — which would have closed
            # the read end — was never started.
            os.close(write_fd)
            os.close(read_fd)
            raise
        os.close(write_fd)
        # Drained on a thread. The pipe holds 64KB; a report larger than that
        # would block the runner mid-write while the parent sat in `wait()`,
        # and the deadlock would surface as a timeout — a confinement failure
        # invented by the plumbing.
        reader = threading.Thread(target=_drain, args=(read_fd, collected),
                                  daemon=True)
        reader.start()

        # Observe the worker from *outside* before letting it run. Everything
        # the worker reports about itself is a claim; these two reads are
        # facts, and they are the ones a forged report cannot touch.
        sample = self._sample_worker(process.pid)
        (root / "go").write_text("1")
        timed_out = False
        try:
            process.wait(timeout=spec.budget.wall_clock_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            if scope is not None:
                scope.kill_all()
            else:
                self._kill_group(process)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:            # pragma: no cover
                pass
        # The write end is held by the worker *and* by any descendant that
        # inherited it, so a leaked process keeps the pipe open. Bounded, and
        # the kill above has already run.
        reader.join(timeout=5)

        class _Completed:
            pass

        completed = _Completed()
        completed.stdout = _read_capped(out_path)
        completed.stderr = _read_capped(err_path)
        completed.returncode = process.returncode
        completed.pid = process.pid
        completed.sample = sample
        completed.report = _parse_report(b"".join(collected), nonce)
        return completed, timed_out

    @staticmethod
    def _sample_worker(pid: int) -> dict:
        """The parent's own view of the worker, taken before it runs anything.

        `preexec_fn` joins the cgroup and unshares the network namespace before
        `execve`, so both are already true of the process by the time `Popen`
        returns — and both are readable from `/proc` by the parent, which is a
        different and stronger kind of evidence than the worker's self-report.
        """
        out: dict[str, Any] = {"pid": pid}
        try:
            out["cgroup"] = Path(f"/proc/{pid}/cgroup").read_text().strip()
        except OSError:
            out["cgroup"] = ""
        try:
            out["netns"] = os.readlink(f"/proc/{pid}/ns/net")
        except OSError:
            out["netns"] = ""
        return out

    @staticmethod
    def _observe(root: Path, spec: ExperimentSpec) -> dict:
        """Parent-side facts gathered after the worker exits, before teardown.

        `datasets_intact` is the check that matters: the worker's own write
        probe can be lied about by a forged report, whereas re-digesting the
        copies the parent made cannot. A dataset whose digest moved was
        modified, whatever the report says.
        """
        out: dict[str, Any] = {}
        try:
            stat = os.statvfs(str(root))
            out["workdir_bytes"] = stat.f_blocks * stat.f_frsize
        except OSError:
            out["workdir_bytes"] = None
        changed: list[str] = []
        for name, digest in spec.dataset_digests().items():
            copy = root / "inputs" / name
            try:
                actual = hashlib.sha256(copy.read_bytes()).hexdigest()
            except OSError:
                changed.append(name)
                continue
            if actual != digest:
                changed.append(name)
        out["datasets_changed"] = changed
        out["datasets_intact"] = not changed
        return out

    @staticmethod
    def _kill_group(process) -> None:
        """Fallback for a host with no cgroup: kill the process group.

        Weaker on purpose, and only reachable for hand-written experiments
        carrying a `trusted_reason`, because a generated run on a host with no
        cgroup is refused before it starts. A group kill misses anything that
        called `setsid()`, which is exactly why it is not what bounds generated
        code.
        """
        import signal                                    # Unix-only, lazy
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
    def _read_report(completed) -> dict:
        """The runner's report, taken from the pipe and nowhere else.

        Deliberately not a file in the work directory: a file is writable by
        the experiment, by anything the experiment forked, and by anything
        still running after the runner exited. The pipe is not.
        """
        return dict(getattr(completed, "report", None) or {})

    def _confinement_states(self, spec: ExperimentSpec, inputs: SignedInputs,
                            report: Mapping[str, Any], *, completed,
                            observed: Mapping[str, Any], timed_out: bool,
                            quota_mb: int, destruction: Mapping[str, Any],
                            trusted_reason: str) -> list[MechanismState]:
        """Merge what the parent established with what the worker reported.

        Three sources, and the order of preference is deliberate:

        1. **What the parent knows.** The worker's pid, the work directory, the
           environment it was handed, the tmpfs size, the dataset digests
           afterwards, the cgroup member list. Not forgeable by the experiment.
        2. **What the parent read from `/proc/<pid>` before releasing the
           worker.** Its cgroup line and its network namespace. Also not
           forgeable — the handshake in the runner exists so this read cannot
           lose a race to a fast experiment.
        3. **What the worker reported about itself.** Useful, corroborating,
           and the only source for things only visible from inside (whether a
           `socket()` call fails, whether a blocked import raises). Treated as
           a claim: every load-bearing mechanism requires (1) or (2) as well.

        Returns a list rather than a `Confinement` because `SIGNED_RESULTS` is
        appended by the caller after the signature has been produced and
        checked — see `run`.
        """
        probe = dict(report.get("probe") or {})
        sample = dict(getattr(completed, "sample", None) or {})
        worker_pid = getattr(completed, "pid", None)
        scope = self._scope
        states: list[MechanismState] = []

        def add(mechanism, applied, detail, basis="observed"):
            states.append(MechanismState(mechanism=mechanism, applied=applied,
                                         detail=detail, basis=basis))

        # The parent's own fact, not the worker's claim: `Popen` returned this
        # pid, and it is not this process's.
        add(Mechanism.SEPARATE_PROCESS,
            worker_pid is not None and worker_pid != os.getpid(),
            f"worker pid {worker_pid} against parent {os.getpid()}; the worker "
            f"reported {probe.get('pid')}")

        # Likewise: the parent built this environment, so it does not need the
        # worker to tell it what was in it. The worker's report is compared
        # against it and a disagreement counts against the mechanism, because
        # the only way the two differ is that something added a variable.
        handed = self.child_environment(
            Path(tempfile.gettempdir()),
            allow_repo_imports=self.allow_repo_imports)
        permitted = (set(ENV_ALLOWLIST) | set(handed) | set(ENV_SET_BY_PARENT)
                     | {"LC_CTYPE"})
        leaked = sorted(set(probe.get("env_keys") or ()) - permitted)
        outside_allowlist = sorted(set(handed) - set(ENV_ALLOWLIST)
                                   - set(ENV_SET_BY_PARENT))
        add(Mechanism.SCRUBBED_ENVIRONMENT,
            not leaked and not outside_allowlist,
            f"unexpected variables in the worker: {leaked}" if leaked
            else f"the parent handed variables outside the allowlist: "
                 f"{outside_allowlist}" if outside_allowlist
            else f"the environment was rebuilt from the allowlist "
                 f"({len(handed)} variables) and the worker reported no others")

        # Two independent tests, because they fail differently: the parent
        # checks what it handed over, the worker checks what it can see. A
        # secret that arrived some other way — an inherited fd, a mount — would
        # show up in the second and not the first.
        secret_marks = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL",
                        "API", "VAULT", "BROKER", "PRIVATE")
        handed_secrets = sorted(name for name in handed
                                if any(m in name.upper() for m in secret_marks))
        seen_secrets = sorted(probe.get("secret_shaped_env") or ())
        add(Mechanism.NO_PRODUCTION_SECRETS,
            not handed_secrets and not seen_secrets,
            f"secret-shaped variables handed over: {handed_secrets}"
            if handed_secrets else
            f"secret-shaped variables visible in the worker: {seen_secrets}"
            if seen_secrets else
            "nothing named like a key, token, credential, vault or broker "
            "setting was handed to the worker, and the worker saw none")

        root_dir = str(destruction.get("workdir") or "")
        add(Mechanism.EPHEMERAL_WORKDIR,
            root_dir.startswith(tempfile.gettempdir())
            and bool(destruction.get("workdir_removed")),
            f"work directory {root_dir}, removed="
            f"{destruction.get('workdir_removed')}; the worker reported cwd "
            f"{probe.get('cwd')}")

        # Judged on the namespace alone, deliberately. The seccomp filter also
        # stops a socket being created, and letting that count here would mean
        # a silently failed unshare reads as a working network namespace —
        # two mechanisms that are supposed to fail independently sharing one
        # observation.
        #
        # And judged on the *parent's* readlink of /proc/<pid>/ns/net, taken
        # before the worker was released. A result file is written by the
        # experiment; a namespace inode read from /proc is not.
        interfaces = list(probe.get("interfaces") or [])
        no_socket = probe.get("socket_created") is False
        sampled_ns = str(sample.get("netns") or "")
        own_namespace = bool(sampled_ns) and sampled_ns != _own_netns()
        only_loopback = interfaces in ([], ["lo"])
        add(Mechanism.NETWORK_NAMESPACE, own_namespace and only_loopback,
            f"parent read worker netns {sampled_ns or '(unreadable)'} against "
            f"its own {_own_netns()}; the worker reported interfaces "
            f"{interfaces} and netns {probe.get('netns')}")

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

        # Every tree-wide limit is one cgroup fact and one membership fact.
        # The limit without the membership bounds nothing, and the membership
        # without the limit bounds nothing, so both are required and the detail
        # names which one is missing.
        # Membership is read by the *parent* from /proc/<pid>/cgroup before the
        # worker is released. The worker also reports its own line; the two are
        # required to agree, so an experiment that rewrote its report has
        # changed nothing.
        sampled_cgroup = str(sample.get("cgroup") or "")
        in_cgroup = bool(
            scope is not None and scope.name
            and scope.name in sampled_cgroup
            and scope.name in str(probe.get("cgroup") or sampled_cgroup))
        cgroup_detail = (f"parent read the worker as a member of cgroup "
                         f"{scope.name!r} ({scope.support.hierarchy})"
                         if in_cgroup
                         else f"cgroup line {sampled_cgroup!r} (parent) / "
                              f"{probe.get('cgroup')!r} (worker) does not name "
                              f"the scope this run created")

        cpu = (probe.get("RLIMIT_CPU") or [None])[0]
        add(Mechanism.CPU_LIMIT, in_cgroup,
            f"{cgroup_detail}; cpu quota one core over the tree, RLIMIT_CPU "
            f"soft={cpu}s over the worker against a budget of "
            f"{spec.budget.cpu_seconds}s; total CPU is bounded by the wall "
            f"clock, and the quota is a rate rather than a total")
        memory = (probe.get("RLIMIT_AS") or [None])[0]
        add(Mechanism.MEMORY_LIMIT, in_cgroup,
            f"{cgroup_detail}; memory quota {spec.budget.memory_mb}MB over the "
            f"tree, RLIMIT_AS soft={memory} over the worker alone")
        add(Mechanism.PROCESS_LIMIT, in_cgroup,
            f"{cgroup_detail}; pids.max={spec.budget.max_processes} over the "
            f"tree. Not RLIMIT_NPROC, which counts per real UID across the "
            f"whole system and is neither a per-experiment bound nor safe to "
            f"set in a container")

        fsize = (probe.get("RLIMIT_FSIZE") or [None])[0]
        add(Mechanism.FILE_SIZE_LIMIT,
            fsize is not None and 0 < fsize <= spec.budget.disk_mb * 1024 * 1024,
            f"RLIMIT_FSIZE soft={fsize}; this caps the largest single file, "
            f"not total disk — defence in depth behind DISK_QUOTA")

        # statvfs on the work directory, run by the parent after the worker
        # exited: it reports the tmpfs size when one is mounted and the host
        # filesystem's when not, so a mount that silently did not happen shows
        # up as an implausibly large number rather than as a boolean nobody
        # checked. The worker's own statvfs is kept for the detail line.
        seen_bytes = observed.get("workdir_bytes")
        want_bytes = spec.budget.disk_mb * 1024 * 1024
        add(Mechanism.DISK_QUOTA,
            bool(quota_mb) and isinstance(seen_bytes, int)
            and 0 < seen_bytes <= want_bytes * 2,
            f"work directory is a tmpfs of {seen_bytes} bytes against a "
            f"{spec.budget.disk_mb}MB budget; the worker measured "
            f"{probe.get('workdir_bytes')}" if quota_mb else
            "no sized tmpfs could be mounted over the work directory; "
            "RLIMIT_FSIZE alone caps one file, not the total")

        # The proof, not the intention. `survivors` is the cgroup's own member
        # list after a freeze-then-kill, so an empty list means no descendant
        # outlived the experiment — including anything that called setsid(),
        # which a process-group kill would have missed entirely.
        survivors = list(destruction.get("survivors") or ())
        add(Mechanism.DESCENDANT_TERMINATION,
            scope is not None and not survivors
            and bool(destruction.get("cgroup_removed")),
            f"cgroup emptied and removed; peak "
            f"{destruction.get('peak_processes')} process(es), zero survivors"
            if scope is not None and not survivors
            else f"survivors {survivors}" if survivors
            else "no cgroup bounded this run, so complete descendant "
                 "termination cannot be demonstrated"
                 + (f" (permitted by trusted_reason: {trusted_reason})"
                    if trusted_reason else ""))

        add(Mechanism.WALL_CLOCK_TIMEOUT, True,
            f"wait() bounded at {spec.budget.wall_clock_seconds}s"
            + ("; it fired" if timed_out else "; it did not fire"),
            basis="asserted")
        # Two checks again, and the second is the one that cannot be talked
        # out of: the worker tries to open each dataset for append and reports
        # what happened, and the parent re-digests its own copies afterwards.
        # A report claiming "read-only" over a file whose digest moved is a
        # report about a modified dataset.
        modes = dict(probe.get("input_modes") or {})
        writable = sorted(name for name, mode in modes.items()
                          if "WRITABLE" in mode)
        changed = list(observed.get("datasets_changed") or ())
        intact = not changed
        add(Mechanism.READ_ONLY_INPUTS,
            not writable and intact,
            f"datasets modified during the run: {changed}" if changed
            else f"{len(modes)} dataset(s) opened for append and refused, "
                 f"digests unchanged afterwards; modes {modes}" if modes
            else "vacuous: no datasets were supplied, so there was nothing to "
                 "make read-only",
            basis="observed" if modes else "asserted")
        add(Mechanism.SIGNED_INPUTS, inputs.verify(),
            "input payload signature verifies" if inputs.verify()
            else "input payload is unsigned or does not verify")
        add(Mechanism.WORKER_DESTRUCTION,
            bool(destruction.get("workdir_removed"))
            and (not quota_mb or destruction.get("unmounted")),
            f"workdir removed={destruction.get('workdir_removed')}, "
            f"tmpfs unmounted={destruction.get('unmounted')}, "
            f"cgroup removed={destruction.get('cgroup_removed')}"
            + (f", error={destruction['error']}"
               if destruction.get("error") else ""))
        return states

    #: Signals the kernel raises when a resource limit is hit. Read as an
    #: outcome in its own right, because a worker killed by its own memory
    #: quota never wrote a probe — and reading the missing probe as "the
    #: confinement failed" would report the limit working as the limit broken.
    #: `SIGSEGV` is deliberately absent: a segfault is a crash, and calling it
    #: "limit exceeded" would describe a broken experiment as a working
    #: quota. Memory exhaustion arrives as `SIGKILL` from the cgroup OOM killer
    #: or as `MemoryError` from `RLIMIT_AS`, both of which are covered.
    _LIMIT_SIGNAL_NAMES = ("SIGXCPU", "SIGXFSZ", "SIGKILL")

    @classmethod
    def _limit_signals(cls) -> frozenset[int]:
        import signal                                    # Unix-only, lazy
        return frozenset(int(getattr(signal, name))
                         for name in cls._LIMIT_SIGNAL_NAMES
                         if hasattr(signal, name))

    @classmethod
    def _outcome(cls, report, completed, timed_out) -> Verdict:
        """What the process did. **Not** what the result is worth.

        The trust decision lives in `ResultManifest.verdict`, which is computed
        from this plus the confinement, so there is no path where a caller can
        hand a manifest a `COMPLETED` verdict it did not earn.
        """
        if timed_out:
            return Verdict.TIMED_OUT
        limits = cls._limit_signals()
        code = completed.returncode if completed else None
        if code is not None and code < 0 and -code in limits:
            return Verdict.LIMIT_EXCEEDED
        # The experiment runs in a child of the runner, so a resource kill
        # lands on the *child* and the runner exits 1 with the signal in its
        # report. Without this the CPU limit doing its job would read as an
        # experiment that merely failed.
        if int(report.get("child_signal") or 0) in limits and report.get(
                "child_signal"):
            return Verdict.LIMIT_EXCEEDED
        if not report or not report.get("ok"):
            return Verdict.FAILED
        return Verdict.COMPLETED

    def _teardown(self, root: Path, *, quota_mb: int) -> dict:
        """Kill the tree, unmount the quota, remove the directory, prove it.

        Order matters and is not negotiable: a live descendant holds the tmpfs
        busy, so the kill has to come first, and the unmount has to come before
        the `rmtree` or the removal walks a mounted filesystem.
        """
        # The scope reference is kept, not cleared: `_confinement_states` runs
        # after this and needs the cgroup's name to decide whether the worker
        # was actually a member of it. `destroy()` is idempotent, so holding
        # the reference costs nothing.
        scope = self._scope
        if scope is not None:
            termination = scope.destroy()
        else:
            termination = {"survivors": [], "removed": False,
                           "peak_processes": 0, "atomic_kill": False,
                           "unavailable": True}
        unmounted = _umount(root) if quota_mb else True
        error = ""
        try:
            shutil.rmtree(root, ignore_errors=False)
        except OSError as failure:
            error = f"{type(failure).__name__}: {failure}"
            shutil.rmtree(root, ignore_errors=True)
        return {"workdir": str(root), "workdir_removed": not root.exists(),
                "unmounted": bool(unmounted), "quota_mb": quota_mb,
                "survivors": list(termination.get("survivors") or ()),
                "cgroup_removed": bool(termination.get("removed")),
                "peak_processes": termination.get("peak_processes", 0),
                "atomic_kill": bool(termination.get("atomic_kill")),
                "error": error}


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------

PROBE_SOURCE = "def run(inputs):\n    return {'probed': True}\n"


#: Why the probe is allowed to run without the generated-code gate: it is this
#: file's own three-line function, not generated, and running it is the only
#: way to find out what a *real* worker would get. Recorded rather than passed
#: as a bare string, so the one exemption in the module has a name.
PROBE_TRUSTED_REASON = (
    "the confinement probe is PROBE_SOURCE, defined in this module and not "
    "generated; it exists to measure the host and returns a constant")


def probe_confinement(*, budget: ComputeBudget | None = None) -> Confinement:
    """Run a do-nothing experiment to find out what this host can enforce.

    Called before a real experiment so the decision to run generated code is
    made on observation rather than on hope, and callable on its own so an
    operator can ask "would research be isolated here?" without running any.

    Raises `IsolationUnavailable` on a host with no confinement at all — there
    is nothing to measure there, and returning an empty `Confinement` would
    read as "measured, and everything is missing", which is a different claim.
    """
    require_platform()
    spec = ExperimentSpec(experiment_id="confinement-probe",
                          source=PROBE_SOURCE, generated=False,
                          budget=budget or ComputeBudget(cpu_seconds=10,
                                                         wall_clock_seconds=30))
    return IsolatedWorker().run(
        spec, trusted_reason=PROBE_TRUSTED_REASON).confinement


def run_isolated(spec: ExperimentSpec, *, trusted_reason: str = ""
                 ) -> ResultManifest:
    """Sign, run, verify and destroy. The one entry point callers want.

    Generated code that cannot be confined is **refused before it starts**,
    with a manifest recording the shortfall. Running it anyway and labelling
    the result untrustworthy would still have run it — the label is for the
    reader, and the process would already have had the host.

    The refusal is returned rather than raised because a caller iterating over
    a batch of challengers wants a manifest per challenger, including for the
    ones that never ran. `IsolatedWorker.run` raises `IsolationUnavailable` for
    a caller who would rather have the exception.
    """
    signed = sign_inputs(spec)
    try:
        return IsolatedWorker().run(spec, signed=signed,
                                    trusted_reason=trusted_reason)
    except IsolationUnavailable as refusal:
        now = datetime.now(timezone.utc)
        missing = list(getattr(refusal, "details", {}).get("missing") or ())
        return ResultManifest(
            experiment_id=spec.experiment_id, outcome=Verdict.REFUSED,
            started_at=now, finished_at=now,
            confinement=Confinement(states=(MechanismState(
                mechanism=Mechanism.SEPARATE_PROCESS, applied=False,
                detail="no worker was started", basis="asserted"),)),
            input_digest=signed.digest, generated=bool(spec.generated),
            stderr=f"{refusal}" + (f": {', '.join(missing)}" if missing else ""),
            destruction={"workdir_removed": True, "workdir": "(never created)",
                         "survivors": []})


def isolation_report() -> dict:
    """The confinement this host offers, as data. Feeds the docs and the CLI.

    Safe to call on any platform: on a host where nothing is implemented it
    reports that, rather than raising, because "what would happen here?" is a
    question an operator on a laptop is entitled to ask.
    """
    support = host_support()
    report = {"schema_version": ISOLATION_SCHEMA_VERSION,
              "platform": platform.platform(),
              "machine": platform.machine(),
              "supported_platform": support.supported_platform,
              "seccomp_supported": platform.machine() in _SYSCALL_NUMBERS,
              "env_allowlist": list(ENV_ALLOWLIST),
              "blocked_imports": list(BLOCKED_IMPORTS),
              "blocked_syscalls": list(BLOCKED_SYSCALLS),
              "required_for_generated_code": sorted(
                  m.value for m in REQUIRED_FOR_GENERATED_CODE),
              "host_support": support.to_dict(),
              "can_run_generated_code": support.can_run_generated_code,
              "confinement": None}
    if support.supported_platform:
        try:
            report["confinement"] = probe_confinement().to_dict()
        except (IsolationUnavailable, OSError) as error:
            report["confinement_error"] = f"{type(error).__name__}: {error}"
    return report


__all__ = ["ISOLATION_SCHEMA_VERSION", "ENV_ALLOWLIST",
           "ENV_SET_BY_PARENT", "BLOCKED_IMPORTS",
           "BLOCKED_SYSCALLS", "SUPPORTED_PLATFORM", "Mechanism",
           "REQUIRED_FOR_GENERATED_CODE", "MechanismState", "Confinement",
           "ExperimentSpec", "SignedInputs", "sign_inputs", "digest_of",
           "Verdict", "ResultManifest", "IsolatedWorker", "HostSupport",
           "host_support", "platform_supported", "require_platform",
           "IsolationUnavailable", "PROBE_TRUSTED_REASON",
           "probe_confinement", "run_isolated", "isolation_report"]
