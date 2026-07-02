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
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
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
    watched: tuple[str, ...] = field(default=())

    def render(self) -> str:
        head = f"[exit {self.code}]" if not self.ok else "[ok]"
        body = f"{head}\n{self.output}".strip()
        if self.watched:
            body += "\n[watch] " + "\n[watch] ".join(self.watched)
        return body


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


def _kill(proc: subprocess.Popen) -> None:
    """Kill the process (group, where we started one) and reap it."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run(command: str, *, timeout: int | None = None,
        be: str | None = None, watch: str | None = None) -> Result:
    """Run a shell command in the confined workspace. Never raises on a
    non-zero exit — that's reported in the Result; only truly broken setups
    (missing docker, etc.) surface as ok=False with the error in output.

    The timeout is activity-based: `timeout` seconds of *silence* kills the
    command, but a command that is still producing output keeps its lease,
    up to the MAX_TIMEOUT wall-clock ceiling. A silent `sleep 5` with
    timeout=1 still dies at 1s; a chatty build with timeout=60 can run to
    the ceiling. On timeout the partial output captured so far is returned
    instead of being discarded.

    `watch` is an optional regex; output lines matching it are collected
    into Result.watched so a caller (or the agent) can alert on markers
    like "ERROR" or "listening on" without re-parsing the full log."""
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
        proc = subprocess.Popen(
            argv, shell=shell, cwd=str(root), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
            env={**os.environ, "OLYMPUS_IN_SANDBOX": "1"})
    except FileNotFoundError as err:               # e.g. docker not installed
        return Result(False, 127, f"backend '{be}' unavailable: {err}")

    out_lines: list[str] = []
    err_lines: list[str] = []
    lock = threading.Lock()
    last_activity = [time.monotonic()]

    def _pump(stream, sink):
        for line in stream:
            with lock:
                sink.append(line)
                last_activity[0] = time.monotonic()
        stream.close()

    pumps = [threading.Thread(target=_pump, args=(proc.stdout, out_lines), daemon=True),
             threading.Thread(target=_pump, args=(proc.stderr, err_lines), daemon=True)]
    for t in pumps:
        t.start()

    start = time.monotonic()
    timed_out = False
    while proc.poll() is None:
        now = time.monotonic()
        with lock:
            idle_deadline = last_activity[0] + timeout
        if now >= min(start + MAX_TIMEOUT, idle_deadline):
            timed_out = True
            _kill(proc)
            break
        time.sleep(0.05)
    for t in pumps:
        t.join(timeout=2)

    with lock:
        out = "".join(out_lines) + (("\n" + "".join(err_lines)) if err_lines else "")
    watched: tuple[str, ...] = ()
    if watch:
        try:
            pattern = re.compile(watch)
            watched = tuple(ln.rstrip("\n") for ln in out.splitlines()
                            if pattern.search(ln))[:50]
        except re.error as err:
            watched = (f"invalid watch pattern: {err}",)
    if timed_out:
        elapsed = int(time.monotonic() - start)
        msg = (f"timed out after {elapsed}s "
               f"(no output for {timeout}s; ceiling {MAX_TIMEOUT}s)")
        partial = out.strip()
        if partial:
            if len(partial) > OUTPUT_CAP:
                partial = partial[:OUTPUT_CAP] + "\n…[truncated]"
            msg += "\npartial output:\n" + partial
        return Result(False, 124, msg, watched)
    if len(out) > OUTPUT_CAP:
        out = out[:OUTPUT_CAP] + f"\n…[truncated, {len(out)} bytes total]"
    return Result(proc.returncode == 0, proc.returncode, out.strip(), watched)


def check_written(target: Path, content: str) -> str:
    """Post-write verification: confirm the bytes actually landed, then run a
    parse-only syntax check for the formats we can validate without executing
    anything (py/json/toml/yaml). A silent partial write or a file that no
    longer parses is exactly the failure an agent won't notice on its own —
    the result string is surfaced to it in the action result."""
    try:
        on_disk = target.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        return f"write verification FAILED: cannot read back file ({err})"
    if on_disk != content:
        return (f"write verification FAILED: file has {len(on_disk)} chars, "
                f"expected {len(content)}")
    suffix = target.suffix.lower()
    try:
        if suffix == ".py":
            compile(content, str(target), "exec")
            return "verified: python syntax OK"
        if suffix == ".json":
            import json
            json.loads(content)
            return "verified: valid JSON"
        if suffix == ".toml":
            import tomllib
            tomllib.loads(content)
            return "verified: valid TOML"
        if suffix in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError:
                return "verified: written (yaml parser unavailable)"
            yaml.safe_load(content)
            return "verified: valid YAML"
    except SyntaxError as err:
        return f"syntax check FAILED: line {err.lineno}: {err.msg}"
    except Exception as err:
        return f"syntax check FAILED: {err}"
    return "verified: written"


def write_file(path: str, content: str) -> dict:
    """Create/overwrite a file inside the workspace. Returns a result dict
    carrying the prior content (if any) so the action can be undone, plus a
    post-write verification (`check`) so silent failures and syntax errors
    are visible to the agent in the same turn."""
    target = _confine(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    prior = target.read_text(encoding="utf-8", errors="replace") if existed else None
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "existed": existed, "prior": prior,
            "bytes": len(content.encode("utf-8")),
            "check": check_written(target, content)}


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
