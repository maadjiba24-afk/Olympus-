"""Linux cgroup control for research workers. Linux-only, imported lazily.

`isolation.py` needs four guarantees that `resource.setrlimit` cannot give,
because rlimits are **per-process** and generated code can fork:

* **a descendant process-count limit** — `RLIMIT_NPROC` counts processes per
  real UID across the whole system, not per process tree. In a container where
  the same UID already runs more than the limit, setting it stops the worker
  starting; where it does apply, it is shared with every other process that UID
  owns. It is not a per-experiment bound and this module does not pretend it is.
* **a memory quota over the tree** — `RLIMIT_AS` bounds one process's address
  space. Ten forked children each get their own allowance.
* **a CPU quota over the tree** — likewise `RLIMIT_CPU`.
* **complete descendant termination** — killing a process group misses anything
  that called `setsid()`. A cgroup cannot be escaped by a process that is
  already in it.

All four are cgroup properties, so this module manages cgroups.

Both hierarchies
----------------
`v2` (unified) is preferred: one directory, `pids.max` / `memory.max` /
`cpu.max`, and `cgroup.kill` which terminates every member atomically. `v1`
(legacy, one mount per controller) is supported because it is what many
container hosts still present — including the one this was written on. The two
are probed and reported separately, so a host offering half of one and half of
the other is described accurately rather than averaged.

Nothing here degrades silently
------------------------------
`probe()` reports exactly which controllers are writable. A caller that cannot
get all of them is expected to refuse to run generated code, not to run it with
fewer. `CgroupScope.create()` raises `IsolationUnavailable` rather than
continuing without a controller it was asked for.

Termination is freeze-then-kill
-------------------------------
Reading the member list and signalling it is a race: a fork bomb creates
processes faster than the reader enumerates them. So the scope is **frozen
first** (v1 `freezer.state`, v2 `cgroup.freeze`), which stops every member
before any signal is sent, and only then killed. v2's `cgroup.kill` does both
in one write and is used when present.
"""

from __future__ import annotations

import errno
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import TradingError

#: Where the kernel usually mounts things. Probed, never assumed.
_V2_CANDIDATES = ("/sys/fs/cgroup", "/sys/fs/cgroup/unified")
_V1_ROOT = "/sys/fs/cgroup"
_V1_CONTROLLERS = ("pids", "memory", "cpu", "freezer")

#: How long to wait for a frozen, killed cgroup to empty before reporting that
#: it did not. Generous: an unkillable process is a finding, not a timeout.
_DRAIN_SECONDS = 5.0
_DRAIN_POLL = 0.02


class IsolationUnavailable(TradingError):
    """This host cannot enforce a control that was asked for.

    Typed, because the caller's correct response is to *refuse to run* rather
    than to catch-and-continue, and a bare `OSError` invites the second.
    """


def _readable(path: Path) -> bool:
    try:
        path.read_text()
        return True
    except OSError:
        return False


def _writable(path: Path) -> bool:
    """Can this control file actually be written?

    Tested by writing its own current value back. Checking `os.access` would
    report the mode bits, and a read-only bind mount or a delegated subtree
    both present writable mode bits on a file the kernel will refuse.
    """
    try:
        current = path.read_text().strip()
    except OSError:
        return False
    try:
        with path.open("w") as handle:
            handle.write(current)
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class CgroupSupport:
    """What this host will actually let a research worker be bounded by."""

    available: bool
    hierarchy: str = ""          # "v2", "v1", or ""
    root: str = ""
    process_limit: bool = False
    memory_quota: bool = False
    cpu_quota: bool = False
    freeze: bool = False
    atomic_kill: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "detail", dict(self.detail))

    @property
    def complete(self) -> bool:
        """Every control `isolation.py` treats as load-bearing.

        `atomic_kill` is absent on purpose: v1 has no `cgroup.kill`, and
        freeze-then-kill over the member list is an adequate substitute. Freeze
        is *not* absent — without it, termination races a fork bomb.
        """
        return bool(self.available and self.process_limit
                    and self.memory_quota and self.cpu_quota and self.freeze)

    @property
    def missing(self) -> tuple[str, ...]:
        out = []
        if not self.available:
            out.append("no writable cgroup hierarchy")
            return tuple(out)
        for name, ok in (("process_limit", self.process_limit),
                         ("memory_quota", self.memory_quota),
                         ("cpu_quota", self.cpu_quota),
                         ("freeze", self.freeze)):
            if not ok:
                out.append(name)
        return tuple(out)

    def to_dict(self) -> dict:
        return {"available": self.available, "hierarchy": self.hierarchy,
                "root": self.root, "process_limit": self.process_limit,
                "memory_quota": self.memory_quota, "cpu_quota": self.cpu_quota,
                "freeze": self.freeze, "atomic_kill": self.atomic_kill,
                "complete": self.complete, "missing": list(self.missing),
                "detail": dict(self.detail)}


def _probe_v2() -> CgroupSupport | None:
    """Unified hierarchy. Needs the controllers *delegated* into a subtree."""
    for candidate in _V2_CANDIDATES:
        root = Path(candidate)
        controllers = root / "cgroup.controllers"
        subtree = root / "cgroup.subtree_control"
        if not controllers.is_file():
            continue
        probe = root / f"olympus-probe-{os.getpid()}"
        try:
            probe.mkdir(exist_ok=True)
        except OSError:
            continue
        try:
            enabled = set((controllers.read_text() or "").split())
            # A controller only reaches a child when the parent delegates it.
            wanted = {"pids", "memory", "cpu"} & enabled
            if wanted and _writable(subtree):
                try:
                    subtree.write_text(
                        " ".join(f"+{name}" for name in sorted(wanted)))
                except OSError:
                    pass
            support = CgroupSupport(
                available=True, hierarchy="v2", root=str(root),
                process_limit=(probe / "pids.max").is_file()
                and _writable(probe / "pids.max"),
                memory_quota=(probe / "memory.max").is_file()
                and _writable(probe / "memory.max"),
                cpu_quota=(probe / "cpu.max").is_file()
                and _writable(probe / "cpu.max"),
                freeze=(probe / "cgroup.freeze").is_file()
                and _writable(probe / "cgroup.freeze"),
                atomic_kill=(probe / "cgroup.kill").is_file(),
                detail={"root_controllers": sorted(enabled)})
        finally:
            try:
                probe.rmdir()
            except OSError:
                pass
        if support.available:
            return support
    return None


def _probe_v1() -> CgroupSupport | None:
    """Legacy hierarchy: one mount per controller, each probed separately."""
    root = Path(_V1_ROOT)
    if not root.is_dir():
        return None
    found: dict[str, bool] = {}
    detail: dict[str, Any] = {}
    for controller in _V1_CONTROLLERS:
        base = root / controller
        if not base.is_dir():
            found[controller] = False
            continue
        probe = base / f"olympus-probe-{os.getpid()}"
        try:
            probe.mkdir(exist_ok=True)
        except OSError as error:
            found[controller] = False
            detail[controller] = f"mkdir failed: {error.strerror}"
            continue
        try:
            knob = {"pids": "pids.max", "memory": "memory.limit_in_bytes",
                    "cpu": "cpu.cfs_quota_us",
                    "freezer": "freezer.state"}[controller]
            target = probe / knob
            found[controller] = target.is_file() and _writable(target)
            detail[controller] = knob
        finally:
            try:
                probe.rmdir()
            except OSError:
                pass
    if not any(found.values()):
        return None
    return CgroupSupport(
        available=True, hierarchy="v1", root=str(root),
        process_limit=found.get("pids", False),
        memory_quota=found.get("memory", False),
        cpu_quota=found.get("cpu", False),
        freeze=found.get("freezer", False),
        atomic_kill=False, detail=detail)


def probe() -> CgroupSupport:
    """What this host offers. Never raises; the caller decides what to do."""
    if not os.name == "posix" or not Path("/sys/fs/cgroup").is_dir():
        return CgroupSupport(available=False,
                             detail={"reason": "no /sys/fs/cgroup on this host"})
    for probe_fn in (_probe_v2, _probe_v1):
        try:
            support = probe_fn()
        except Exception as error:                       # noqa: BLE001
            support = None
            _ = error
        if support is not None and support.complete:
            return support
    # Nothing complete: report the better of the two so the shortfall is named.
    partial = None
    for probe_fn in (_probe_v2, _probe_v1):
        try:
            candidate = probe_fn()
        except Exception:                                # noqa: BLE001
            continue
        if candidate is None:
            continue
        if partial is None or len(candidate.missing) < len(partial.missing):
            partial = candidate
    return partial or CgroupSupport(
        available=False, detail={"reason": "no writable cgroup hierarchy"})


class CgroupScope:
    """One experiment's cgroup. Created, joined, killed, destroyed.

    Single-use, like the worker it bounds. `destroy()` is idempotent so it can
    live in a `finally` without a guard.
    """

    __slots__ = ("name", "support", "_paths", "_created", "_destroyed",
                 "_peak_pids")

    def __init__(self, name: str, *, support: CgroupSupport | None = None):
        self.name = str(name)
        self.support = support or probe()
        self._paths: dict[str, Path] = {}
        self._created = False
        self._destroyed = False
        self._peak_pids = 0

    # -- lifecycle ---------------------------------------------------------

    def create(self, *, max_processes: int, memory_bytes: int,
               cpu_quota_us: int, cpu_period_us: int = 100_000) -> "CgroupScope":
        """Create the cgroup and write every limit. Refuses on any shortfall."""
        if not self.support.complete:
            raise IsolationUnavailable(
                "this host cannot bound a research worker with cgroups",
                missing=list(self.support.missing),
                hierarchy=self.support.hierarchy or "none")
        if self.support.hierarchy == "v2":
            self._create_v2(max_processes, memory_bytes, cpu_quota_us,
                            cpu_period_us)
        else:
            self._create_v1(max_processes, memory_bytes, cpu_quota_us,
                            cpu_period_us)
        self._created = True
        return self

    def _create_v2(self, max_processes, memory_bytes, cpu_quota_us,
                   cpu_period_us) -> None:
        path = Path(self.support.root) / self.name
        path.mkdir(parents=True, exist_ok=True)
        self._paths["unified"] = path
        (path / "pids.max").write_text(str(int(max_processes)))
        (path / "memory.max").write_text(str(int(memory_bytes)))
        (path / "cpu.max").write_text(f"{int(cpu_quota_us)} {int(cpu_period_us)}")

    def _create_v1(self, max_processes, memory_bytes, cpu_quota_us,
                   cpu_period_us) -> None:
        root = Path(self.support.root)
        for controller in _V1_CONTROLLERS:
            path = root / controller / self.name
            path.mkdir(parents=True, exist_ok=True)
            self._paths[controller] = path
        (self._paths["pids"] / "pids.max").write_text(str(int(max_processes)))
        memory = self._paths["memory"]
        (memory / "memory.limit_in_bytes").write_text(str(int(memory_bytes)))
        swap = memory / "memsw.limit_in_bytes"
        if swap.is_file():
            # Without this a bounded process can spill into swap and the memory
            # limit becomes a limit on residency rather than on consumption.
            try:
                swap.write_text(str(int(memory_bytes)))
            except OSError:
                pass
        (self._paths["cpu"] / "cpu.cfs_period_us").write_text(str(int(cpu_period_us)))
        (self._paths["cpu"] / "cpu.cfs_quota_us").write_text(str(int(cpu_quota_us)))

    def join_current_process(self) -> None:
        """Put the calling process into every controller. Runs in the child.

        Called from `preexec_fn`, so it happens after fork and before exec —
        the child is inside the cgroup before any generated code runs, and
        every descendant inherits membership.
        """
        for path in self._paths.values():
            (path / "cgroup.procs").write_text(str(os.getpid()))

    # -- observation -------------------------------------------------------

    def members(self) -> tuple[int, ...]:
        """Every pid in the cgroup, including descendants that called setsid."""
        for path in self._paths.values():
            try:
                text = (path / "cgroup.procs").read_text()
            except OSError:
                continue
            return tuple(int(line) for line in text.split() if line.strip())
        return ()

    @property
    def peak_processes(self) -> int:
        """High-water mark, so a fork bomb that was stopped is still visible."""
        for key in ("pids", "unified"):
            path = self._paths.get(key)
            if path is None:
                continue
            for name in ("pids.peak", "pids.current"):
                candidate = path / name
                if candidate.is_file():
                    try:
                        return max(self._peak_pids,
                                   int(candidate.read_text().strip()))
                    except (OSError, ValueError):
                        continue
        return self._peak_pids

    # -- termination -------------------------------------------------------

    def freeze(self, frozen: bool = True) -> bool:
        if self.support.hierarchy == "v2":
            path = self._paths.get("unified")
            target = path / "cgroup.freeze" if path else None
            value = "1" if frozen else "0"
        else:
            path = self._paths.get("freezer")
            target = path / "freezer.state" if path else None
            value = "FROZEN" if frozen else "THAWED"
        if target is None or not target.is_file():
            return False
        try:
            target.write_text(value)
            return True
        except OSError:
            return False

    def kill_all(self) -> dict:
        """Freeze, kill every member, thaw, and verify the cgroup is empty.

        Returns the evidence rather than a bool: `survivors` is what a caller
        asserts on, and an empty tuple there is the proof that no descendant
        outlived the experiment.
        """
        started = time.monotonic()
        self._peak_pids = max(self._peak_pids, self.peak_processes)
        atomic = False

        if self.support.hierarchy == "v2" and self.support.atomic_kill:
            path = self._paths.get("unified")
            if path is not None and (path / "cgroup.kill").is_file():
                try:
                    (path / "cgroup.kill").write_text("1")
                    atomic = True
                except OSError:
                    atomic = False

        if not atomic:
            # Freeze first. Enumerating and signalling a live cgroup is a race
            # a fork bomb wins; a frozen one cannot create anything.
            froze = self.freeze(True)
            for _ in range(200):
                members = self.members()
                if not members:
                    break
                for pid in members:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError as error:
                        if error.errno not in (errno.ESRCH, errno.EPERM):
                            raise
                if froze:
                    # A frozen process does not die until it is thawed: SIGKILL
                    # is delivered but cannot be handled while the task is
                    # stopped in the freezer.
                    self.freeze(False)
                    froze = False
                time.sleep(_DRAIN_POLL)
            self.freeze(False)

        deadline = time.monotonic() + _DRAIN_SECONDS
        survivors = self.members()
        while survivors and time.monotonic() < deadline:
            time.sleep(_DRAIN_POLL)
            survivors = self.members()

        return {"atomic_kill": atomic,
                "survivors": list(survivors),
                "peak_processes": self._peak_pids,
                "seconds": round(time.monotonic() - started, 4)}

    def destroy(self) -> dict:
        """Kill everything, then remove the directories. Idempotent."""
        if self._destroyed or not self._created:
            return {"survivors": [], "removed": True,
                    "peak_processes": self._peak_pids}
        report = self.kill_all()
        removed = True
        for path in self._paths.values():
            for _ in range(50):
                try:
                    path.rmdir()
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(_DRAIN_POLL)
            else:
                removed = False
        self._destroyed = True
        report["removed"] = removed and not any(p.exists()
                                                for p in self._paths.values())
        return report

    def __enter__(self) -> "CgroupScope":
        return self

    def __exit__(self, *exc) -> None:
        self.destroy()

    def to_dict(self) -> dict:
        return {"name": self.name, "hierarchy": self.support.hierarchy,
                "paths": {k: str(v) for k, v in self._paths.items()},
                "peak_processes": self.peak_processes}


def orphan_check(pids: Sequence[int]) -> tuple[int, ...]:
    """Which of these pids are still *running*. For post-test proof.

    Zombies are excluded, and that distinction matters: a zombie has already
    died and is holding a slot until its parent reaps it. Counting one as a
    survivor would make every test that does not call `wait()` report a leak
    that is not there — the first version of this function did exactly that.

    Deliberately not `os.kill(pid, 0)` alone: a reaped pid can be reused. The
    cgroup member list is the primary evidence; this is a cross-check.
    """
    alive = []
    for pid in pids:
        stat = Path(f"/proc/{pid}/stat")
        if not stat.exists():
            continue
        try:
            # "pid (comm) state ..." — comm can contain spaces and brackets,
            # so the state is the field after the last ')'.
            text = stat.read_text()
            state = text[text.rindex(")") + 1:].split()[0]
        except (OSError, ValueError, IndexError):
            continue
        if state not in ("Z", "X", "x"):
            alive.append(int(pid))
    return tuple(alive)


__all__ = ["IsolationUnavailable", "CgroupSupport", "probe", "CgroupScope",
           "orphan_check"]
