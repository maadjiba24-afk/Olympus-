"""Natural-language cron — user-defined scheduled tasks.

The heartbeat already runs Olympus's *own* maintenance on fixed cadences. This
adds the Hermes-style capability the README's competitor table flagged as
missing: **arbitrary tasks, defined in plain English, running unattended**, with
results delivered to any connected platform.

    olympus schedule add "daily 8am market scan" "every 24h" \
        "Scan AI tooling for new opportunities and summarize the top 3" --to telegram

A job is just data (name, interval, prompt, target, last-run). `due()` reports
which jobs are ready; `run_due()` executes each ready job through the full
Olympus pipeline and delivers the answer. The heartbeat calls `run_due()` every
tick, so scheduled work piggybacks on the same loop with no new process.

Two schedule kinds exist: `interval` (the cadence jobs above) and `on_exit` —
an event-driven one-shot that fires the moment a watched process exits
("when the training run finishes, summarize the log"), added with
`add_on_exit()` / `olympus schedule add --on-exit <pid>` / `/onexit` in chat.

Interval parsing is deliberately forgiving — it understands the phrases people
actually type ("hourly", "every 30m", "daily", "weekly", "every 2 days") and
falls back to a daily cadence rather than failing.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field

from . import config, memory

_UNITS = {"s": 1, "sec": 1, "second": 1, "seconds": 1,
          "m": 60, "min": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
          "d": 86400, "day": 86400, "days": 86400,
          "w": 604800, "week": 604800, "weeks": 604800}

DAY = 86400
MIN_INTERVAL = 60          # never busier than once a minute
MAX_JOBS = 200             # per-owner cap; one tenant cannot evict another
MAX_TOTAL_JOBS = 2000      # absolute cap; additions fail closed at capacity


def parse_interval(text: str) -> int:
    """Turn a natural phrase into seconds. Always returns a sane positive int."""
    t = (text or "").strip().lower()
    if t in ("hourly", "every hour"):
        return 3600
    if t in ("daily", "every day", "nightly"):
        return DAY
    if t in ("weekly", "every week"):
        return 7 * DAY
    # "every 30m", "every 2 days", "30m", "2 hours", "6h"
    m = re.search(r"(\d+)\s*([a-z]+)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        secs = n * _UNITS.get(unit, _UNITS.get(unit.rstrip("s"), 0))
        if secs:
            return max(MIN_INTERVAL, secs)
    return DAY                                       # forgiving default


def _path():
    p = config.MEMORY_DIR / "schedule.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


ON_EXIT_POLL = 30      # how often an on-exit watch is worth re-checking
ON_CHANGE_POLL = int(os.environ.get("OLYMPUS_ONCHANGE_POLL", "60"))
_WATCH_MAX_BYTES = 1_000_000     # cap watched output so a chatty command
                                 # can't blow up memory or the state file


def _watch_hash(cmd: str) -> str | None:
    """Hash the stdout of a watched command (the change signal). Returns None
    when the command can't be run or fails — treated as 'no observation this
    tick', never as a change, so a flaky watch command doesn't spam runs."""
    if not cmd.strip():
        return None
    import hashlib
    import subprocess
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True,
                             timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return hashlib.sha256(out.stdout[:_WATCH_MAX_BYTES]).hexdigest()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        error_access_denied = 5

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            exit_code = wintypes.DWORD()
            checked = kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            )
            kernel32.CloseHandle(handle)
            return bool(checked and exit_code.value == 259)

        return ctypes.get_last_error() == error_access_denied

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class Job:
    name: str
    interval: int                # seconds (interval jobs)
    prompt: str
    deliver_to: str = ""         # "", "telegram", "discord", "slack", "signal"
    user: str = "shared"
    enabled: bool = True
    last_run: float = 0.0
    created: float = field(default=0.0)
    skill: str = ""              # optional skill loaded before the job's prompt
    kind: str = "interval"       # "interval" | "on_exit" | "on_change"
    watch_pid: int = 0           # on_exit: fire when this process exits
    label: str = ""              # on_exit/on_change: human name for the watch
    started_at: float = 0.0      # run in progress (crash/upgrade detection)
    resume_attempts: int = 0     # interrupted-run retries (bounded at 1)
    watch_cmd: str = ""          # on_change: command whose output is watched
    last_hash: str = ""          # on_change: last observed output hash

    def due(self, now: float) -> bool:
        if not self.enabled:
            return False
        if self.kind == "on_exit":
            # Event-driven: due the moment the watched process is gone.
            return self.watch_pid > 0 and not _pid_alive(self.watch_pid)
        if self.kind == "on_change":
            # Change-driven jobs never fire on a cadence; run_due checks their
            # watched output on a throttled poll (keeps due() pure + cheap —
            # it must not spawn a subprocess).
            return False
        return (now - self.last_run) >= self.interval


def _load() -> list[Job]:
    p = _path()
    if not p.exists():
        return []
    try:
        return [Job(**d) for d in json.loads(p.read_text(encoding="utf-8"))]
    except (json.JSONDecodeError, TypeError, OSError):
        return []


def _save(jobs: list[Job]) -> None:
    # Atomic publish: _load maps a torn file to [], so a plain write_text
    # would let a cross-process reader see "no jobs" mid-write (ADR 0005).
    p = _path()
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    from . import atomicio
    atomicio.publish(tmp, p, json.dumps([asdict(j) for j in jobs], indent=2))


def _principal(user: str) -> str:
    return memory.safe_id(user or "shared")


def _key(job: Job) -> tuple[str, str]:
    """A job name is unique only inside its authenticated owner namespace."""
    return _principal(job.user), job.name


def _matches(job: Job, name: str, user: str) -> bool:
    return _key(job) == (_principal(user), name)


def _bounded(jobs: list[Job]) -> list[Job]:
    """Apply per-owner quotas without silently evicting another tenant."""
    by_owner: dict[str, list[Job]] = {}
    for job in jobs:
        by_owner.setdefault(_principal(job.user), []).append(job)
    kept: list[Job] = []
    for owned in by_owner.values():
        kept.extend(sorted(owned, key=lambda job: job.created)[-MAX_JOBS:])
    if len(kept) > MAX_TOTAL_JOBS:
        raise ValueError("global schedule capacity reached")
    return kept


def _mutex():
    """Cross-process lock for every jobs load-modify-save cycle (ADR 0005):
    the heartbeat process runs/marks jobs while web/CLI/gateway processes
    add and remove them — the same shared-JSON pattern as goals.py."""
    from . import proclock
    return proclock.lock("scheduler")


def add(name: str, interval: str | int, prompt: str,
        deliver_to: str = "", user: str = "shared",
        now: float | None = None, skill: str = "") -> Job:
    now = now if now is not None else time.time()
    secs = interval if isinstance(interval, int) else parse_interval(interval)
    owner = _principal(user)
    with _mutex():
        jobs = [j for j in _load() if not _matches(j, name, owner)]
        job = Job(name=name, interval=max(MIN_INTERVAL, int(secs)),
                  prompt=prompt, deliver_to=deliver_to.strip().lower(),
                  user=owner, created=now, skill=(skill or "").strip())
        jobs.append(job)
        _save(_bounded(jobs))
    return job


def add_on_exit(name: str, pid: int, prompt: str,
                deliver_to: str = "", user: str = "shared",
                label: str = "", now: float | None = None) -> Job:
    """Event-driven schedule kind: run `prompt` once when process `pid` exits
    ("kick off the report when the training run finishes"). The job disables
    itself after firing — it is a one-shot wake, not a cadence."""
    now = now if now is not None else time.time()
    if not _pid_alive(pid):
        raise ValueError(f"process {pid} isn't running")
    owner = _principal(user)
    with _mutex():
        jobs_ = [j for j in _load() if not _matches(j, name, owner)]
        job = Job(name=name, interval=0, prompt=prompt, kind="on_exit",
                  watch_pid=int(pid), label=(label or "").strip(),
                  deliver_to=deliver_to.strip().lower(), user=owner,
                  created=now)
        jobs_.append(job)
        _save(_bounded(jobs_))
    return job


def add_on_change(name: str, watch_cmd: str, prompt: str,
                  deliver_to: str = "", user: str = "shared",
                  label: str = "", now: float | None = None) -> Job:
    """Change-driven schedule kind: run `prompt` whenever the stdout of
    `watch_cmd` changes ("summarize the deploy log when it changes", "alert me
    when this API's response differs"). The current output is captured as the
    baseline at creation, so only a SUBSEQUENT change fires — never the first
    observation. Unlike on_exit it is recurring: it fires on every change."""
    now = now if now is not None else time.time()
    if not watch_cmd.strip():
        raise ValueError("a watched command is required")
    baseline = _watch_hash(watch_cmd)      # capture now so first change fires
    owner = _principal(user)
    with _mutex():
        jobs_ = [j for j in _load() if not _matches(j, name, owner)]
        job = Job(name=name, interval=0, prompt=prompt, kind="on_change",
                  watch_cmd=watch_cmd.strip(), last_hash=baseline or "",
                  label=(label or "").strip(),
                  deliver_to=deliver_to.strip().lower(), user=owner,
                  created=now, last_run=now)
        jobs_.append(job)
        _save(_bounded(jobs_))
    return job


def remove(name: str, user: str = "shared") -> bool:
    with _mutex():
        jobs = _load()
        kept = [j for j in jobs if not _matches(j, name, user)]
        _save(kept)
        return len(kept) != len(jobs)


def set_enabled(name: str, on: bool, user: str = "shared") -> bool:
    with _mutex():
        jobs = _load()
        found = False
        for j in jobs:
            if _matches(j, name, user):
                j.enabled, found = on, True
        _save(jobs)
        return found


def jobs(user: str | None = None) -> list[Job]:
    loaded = _load()
    if user is None:
        return loaded
    owner = _principal(user)
    return [job for job in loaded if _principal(job.user) == owner]


def due(now: float | None = None) -> list[Job]:
    now = now if now is not None else time.time()
    return [j for j in _load() if j.due(now)]


def next_due_in(now: float | None = None) -> float | None:
    """Seconds until the soonest enabled job is due (None if no enabled jobs).
    An on-exit/on-change watch has no cadence: it costs a poll, so it asks to
    be re-checked shortly (0 the moment its trigger has fired)."""
    now = now if now is not None else time.time()
    waits = []
    for j in _load():
        if not j.enabled:
            continue
        if j.kind == "on_exit":
            waits.append(0.0 if j.due(now) else float(ON_EXIT_POLL))
        elif j.kind == "on_change":
            # Re-poll the watched command at most every ON_CHANGE_POLL seconds.
            waits.append(max(0.0, ON_CHANGE_POLL - (now - j.last_run)))
        else:
            waits.append(max(0.0, j.interval - (now - j.last_run)))
    return min(waits) if waits else None


def changed(now: float | None = None) -> list[tuple]:
    """on_change jobs whose watched output differs from the last observation,
    throttled to one check per ON_CHANGE_POLL. Returns (job, new_hash) pairs;
    the caller persists new_hash so the same change fires only once."""
    now = now if now is not None else time.time()
    out = []
    for j in _load():
        if not j.enabled or j.kind != "on_change":
            continue
        if now - j.last_run < ON_CHANGE_POLL:
            continue                       # not time to re-poll yet
        cur = _watch_hash(j.watch_cmd)
        if cur is None:                    # couldn't observe — try next tick
            continue
        if cur != j.last_hash:
            out.append((j, cur))
    return out


def _mark_hash(name: str, new_hash: str, now: float,
               user: str = "shared") -> None:
    with _mutex():
        jobs_ = _load()
        for j in jobs_:
            if _matches(j, name, user):
                j.last_hash = new_hash
                j.last_run = now
        _save(jobs_)


# A run that started this long ago and never finished is presumed dead
# (the process was killed/upgraded mid-run) and eligible for one resume.
RESUME_AFTER = int(os.environ.get("OLYMPUS_JOB_RESUME_AFTER", "3600"))


def _mark_started(name: str, now: float, user: str = "shared") -> None:
    with _mutex():
        jobs_ = _load()
        for j in jobs_:
            if _matches(j, name, user):
                j.started_at = now
        _save(jobs_)


def _mark_ran(name: str, now: float, user: str = "shared") -> None:
    with _mutex():
        jobs_ = _load()
        for j in jobs_:
            if _matches(j, name, user):
                j.last_run = now
                j.resume_attempts = 0   # a finished run clears resume state
        _save(jobs_)


def interrupted(now: float | None = None) -> list[Job]:
    """Jobs whose last run STARTED but never finished (a restart or upgrade
    killed the process mid-run) — eligible for one resume with a continuation
    note, instead of silently waiting a whole cadence."""
    now = now if now is not None else time.time()
    out = []
    for j in _load():
        if (j.enabled and j.started_at > j.last_run
                and now - j.started_at >= RESUME_AFTER
                and j.resume_attempts < 1):
            out.append(j)
    return out


def _deliver(job: Job, answer: str) -> None:
    """Best-effort delivery to the chosen platform (no-op if unconfigured)."""
    target = job.deliver_to
    msg = f"⏰ Scheduled — {job.name}\n\n{answer}"
    try:
        if target == "telegram":
            from . import telegram
            telegram.notify(msg)
        elif target == "discord":
            from . import discord
            discord.notify(msg)
        elif target == "slack":
            from . import slack
            slack.notify(msg)
        elif target == "signal":
            from . import signal as signal_gw
            signal_gw.notify(msg)
        elif target == "ntfy":
            from . import ntfy
            ntfy.notify(msg, title=f"Scheduled — {job.name}")
    except Exception:
        pass


def _effective_prompt(job: Job) -> str:
    """The prompt actually run for a job. An on-exit job is told what it was
    waiting for, so the model knows the trigger context. When a job has an
    attached skill, its instructions are loaded and prepended so the run starts
    already primed with that playbook."""
    if job.kind == "on_exit":
        what = job.label or f"process {job.watch_pid}"
        return (f"The watched work has finished: {what} (pid {job.watch_pid}) "
                f"has exited.\n\n{job.prompt}")
    if job.kind == "on_change":
        what = job.label or f"`{job.watch_cmd}`"
        return (f"The watched output changed: {what}.\n\n{job.prompt}")
    if not job.skill:
        return job.prompt
    try:
        from . import skills
        body = skills.read(job.skill)
    except Exception:
        body = ""
    if not body or body.lower().startswith(("no skill", "error")):
        return job.prompt
    return (f"Use the '{job.skill}' skill for this task:\n\n{body}\n\n"
            f"---\nTask:\n{job.prompt}")


def run_due(now: float | None = None, runner=None) -> list[str]:
    """Execute every due job through the Olympus pipeline; deliver + log.
    `runner(prompt, user) -> str` is injectable for tests."""
    now = now if now is not None else time.time()
    log: list[str] = []
    ready = due(now)
    # Interrupted runs (process died mid-job) get one resume with a
    # continuation note instead of silently waiting a whole cadence.
    resumed_keys: set[tuple[str, str]] = set()
    ready_keys = {_key(job) for job in ready}
    for job in interrupted(now):
        if _key(job) not in ready_keys:
            resumed_keys.add(_key(job))
            ready.append(job)
            ready_keys.add(_key(job))
    # Change-driven jobs: poll their watched output and fire on a difference.
    # The new hash is persisted BEFORE running so the same change fires once,
    # even if the run itself fails.
    for job, new_hash in changed(now):
        if _key(job) not in ready_keys:
            _mark_hash(job.name, new_hash, now, user=job.user)
            ready.append(job)
            ready_keys.add(_key(job))
    if not ready:
        return log
    if runner is None:
        def runner(prompt: str, user: str) -> str:
            from . import orchestrator
            return orchestrator.Olympus(user=user).ask(prompt)
    for job in ready:
        prompt = _effective_prompt(job)
        if _key(job) in resumed_keys:
            _bump_resume(job.name, user=job.user)
            prompt = ("A previous attempt at this task was interrupted by a "
                      "restart or upgrade before it finished. Redo it in "
                      "full now.\n\n" + prompt)
        _mark_started(job.name, now, user=job.user)
        try:
            answer = runner(prompt, job.user)
            _deliver(job, answer)
            memory.set_user("shared")
            memory.save("reports", f"Scheduled: {job.name}", answer)
            log.append(("resumed interrupted job" if _key(job) in resumed_keys
                        else "ran scheduled job") + f" '{job.name}'")
        except Exception as err:
            log.append(f"scheduled job '{job.name}' failed: {str(err)[:120]}")
        finally:
            _mark_ran(job.name, now, user=job.user)
            if job.kind == "on_exit":
                set_enabled(job.name, False, user=job.user)
    return log


def _bump_resume(name: str, user: str = "shared") -> None:
    with _mutex():
        jobs_ = _load()
        for j in jobs_:
            if _matches(j, name, user):
                j.resume_attempts += 1
        _save(jobs_)


def _human_interval(secs: int) -> str:
    """Render an interval in its coarsest whole unit: a weekly job reads as '7d'
    and a daily one as '1d', not '168h'/'24h'; sub-minute intervals keep their
    seconds instead of collapsing to '0m'."""
    secs = int(secs)
    if secs % DAY == 0:
        return f"{secs // DAY}d"
    if secs % 3600 == 0:
        return f"{secs // 3600}h"
    if secs % 60 == 0:
        return f"{secs // 60}m"
    return f"{secs}s"


def summary(user: str | None = None) -> str:
    js = jobs(user)
    if not js:
        return "No scheduled jobs. Add one: olympus schedule add <name> <interval> <prompt>"
    lines = ["Scheduled jobs:"]
    for j in js:
        state = "" if j.enabled else " (disabled)"
        to = f" → {j.deliver_to}" if j.deliver_to else ""
        using = f" [skill: {j.skill}]" if getattr(j, "skill", "") else ""
        kind = getattr(j, "kind", "interval")
        if kind == "on_exit":
            what = j.label or f"pid {j.watch_pid}"
            when = f"when {what} exits"
        elif kind == "on_change":
            what = j.label or f"`{j.watch_cmd}`"
            when = f"when {what} changes"
        else:
            when = f"every {_human_interval(j.interval)}"
        lines.append(f"  {j.name}: {when}{to}{using}{state}"
                     f"\n     {j.prompt[:80]}")
    return "\n".join(lines)
