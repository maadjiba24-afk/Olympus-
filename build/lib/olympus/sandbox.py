"""Real execution environment — the operator surface Olympus was missing.

Until now Hephaestus could only run code in Anthropic's *server-side* sandbox:
it could never touch a real file, run a real shell command, or operate on a
host. This module adds a host-side execution environment so Olympus can actually
*do* things — edit files, run commands, build and test.

Be precise about what protects you, because the two mechanisms cover different
threats and the **local backend is NOT an OS sandbox**:

1. **The Action spine is the primary control.** `run_command` / `write_file`
   are registered as approval-gated ActionTypes (see builtin_actions), so an
   agent that has ingested untrusted content physically cannot execute — it can
   only PREPARE an action a human (or an explicit policy) approves. Reads
   (`read_file`, `list_dir`) are side-effect-free and exposed as plain tools.
   This is what makes the feature safe to ship; do not rely on OS confinement.
2. **Path confinement covers the file tools only.** `read_file` / `write_file`
   / `list_dir` resolve their path under a single workdir root
   (`OLYMPUS_EXEC_WORKDIR`, default `<MEMORY_DIR>/workspace`) and refuse — not
   clamp — a path that escapes it. This bounds *where files land*; it does NOT
   sandbox a shell. A `run_command` command runs with `cwd` set to that root
   and a wall-clock timeout + output cap, but on the **local** backend it has
   the invoking user's full OS privileges and network — it can read outside the
   root, reach the network, and spawn processes. Confinement here means the
   file-path tools and the process's starting directory, nothing stronger.

For genuine OS-level isolation of untrusted code, use the **docker** backend,
which runs the command inside `docker run --rm --network none` with only the
workdir bind-mounted at /work.

Backends (`OLYMPUS_EXEC_BACKEND`):
    local   subprocess started in the workdir (default) — NOT OS-isolated;
            safety rests on the Action spine approval gate above.
    docker  the same command inside `docker run --rm --network none`, the
            workdir bind-mounted at /work — real isolation for untrusted builds.
The remaining Hermes backends (ssh / modal / daytona / singularity) are thin
transports over the same `run()` contract; `OLYMPUS_EXEC_DOCKER_IMAGE` and
`OLYMPUS_EXEC_NETWORK=1` tune the container.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

# Hard ceilings so a runaway command can't hang the heartbeat or blow up memory.
DEFAULT_TIMEOUT = int(os.environ.get("OLYMPUS_EXEC_TIMEOUT", "60"))
MAX_TIMEOUT = 600
OUTPUT_CAP = 20_000


def backend() -> str:
    return os.environ.get("OLYMPUS_EXEC_BACKEND", "local").strip().lower() or "local"


def workdir() -> Path:
    """The root the file tools confine paths to, and the starting `cwd` for
    commands. Note: it bounds the file-path tools and where a command begins,
    not what a local shell command can reach (see the module docstring)."""
    d = Path(os.environ.get("OLYMPUS_EXEC_WORKDIR",
                            str(config.MEMORY_DIR / "workspace")))
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def _confine(path: str) -> Path:
    """Resolve `path` under the workdir; raise if it escapes the root."""
    root = workdir()
    target = (root / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes the workspace root: {path}")
    return target


@dataclass(frozen=True)
class Result:
    ok: bool
    code: int
    output: str

    def render(self) -> str:
        head = f"[exit {self.code}]" if not self.ok else "[ok]"
        return f"{head}\n{self.output}".strip()


def _docker_cmd(command: str, root: Path, timeout: int) -> list[str]:
    image = os.environ.get("OLYMPUS_EXEC_DOCKER_IMAGE", "python:3.11-slim")
    net = os.environ.get("OLYMPUS_EXEC_NETWORK", "").strip().lower() in (
        "1", "true", "yes", "on")
    args = ["docker", "run", "--rm",
            "-v", f"{root}:/work", "-w", "/work"]
    if not net:
        args += ["--network", "none"]
    args += [image, "sh", "-c", command]
    return args


def run(command: str, *, timeout: int | None = None,
        be: str | None = None) -> Result:
    """Run a shell command in the confined workspace. Never raises on a
    non-zero exit — that's reported in the Result; only truly broken setups
    (missing docker, etc.) surface as ok=False with the error in output."""
    if not (command or "").strip():
        return Result(False, 2, "empty command")
    timeout = max(1, min(MAX_TIMEOUT, timeout or DEFAULT_TIMEOUT))
    root = workdir()
    be = (be or backend()).lower()
    if be == "docker":
        argv, shell = _docker_cmd(command, root, timeout), False
    else:
        argv, shell = command, True
    try:
        proc = subprocess.run(
            argv, shell=shell, cwd=str(root), capture_output=True, text=True,
            timeout=timeout,
            env={**os.environ, "OLYMPUS_IN_SANDBOX": "1"})
    except subprocess.TimeoutExpired:
        return Result(False, 124, f"timed out after {timeout}s")
    except FileNotFoundError as err:               # e.g. docker not installed
        return Result(False, 127, f"backend '{be}' unavailable: {err}")
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    if len(out) > OUTPUT_CAP:
        out = out[:OUTPUT_CAP] + f"\n…[truncated, {len(out)} bytes total]"
    return Result(proc.returncode == 0, proc.returncode, out.strip())


def write_file(path: str, content: str) -> dict:
    """Create/overwrite a file inside the workspace. Returns a result dict
    carrying the prior content (if any) so the action can be undone."""
    target = _confine(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    prior = target.read_text(encoding="utf-8", errors="replace") if existed else None
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "existed": existed, "prior": prior,
            "bytes": len(content.encode("utf-8"))}


def undo_write(result: dict) -> str:
    """Reverse a write_file: restore the prior content, or delete a new file."""
    path = Path(result.get("path", ""))
    if result.get("existed") and result.get("prior") is not None:
        path.write_text(result["prior"], encoding="utf-8")
        return f"restored previous contents of {path.name}"
    if path.exists():
        path.unlink()
        return f"deleted new file {path.name}"
    return "nothing to undo"


def read_file(path: str) -> str:
    try:
        target = _confine(path)
    except ValueError as err:
        return f"Error: {err}"
    if not target.is_file():
        return f"Error: no such file in workspace: {path}"
    return target.read_text(encoding="utf-8", errors="replace")[:OUTPUT_CAP]


def list_dir(path: str = ".") -> str:
    try:
        target = _confine(path)
    except ValueError as err:
        return f"Error: {err}"
    if not target.exists():
        return f"Error: no such path in workspace: {path}"
    if target.is_file():
        return target.name
    entries = []
    for p in sorted(target.iterdir()):
        entries.append(f"{p.name}/" if p.is_dir() else p.name)
    return "\n".join(entries) or "(empty)"
