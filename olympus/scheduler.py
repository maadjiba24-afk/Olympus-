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
MAX_JOBS = 200             # bound stored jobs so the file can't grow unbounded


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


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    import os
    try:
        os.kill(pid, 0)            # signal 0: existence check, no effect
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                # exists, owned by someone else
    except OSError:
        return False


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
    kind: str = "interval"       # "interval" | "on_exit" (event-driven)
    watch_pid: int = 0           # on_exit: fire when this process exits
    label: str = ""              # on_exit: human name for the watched work
    started_at: float = 0.0      # run in progress (crash/upgrade detection)
    resume_attempts: int = 0     # interrupted-run retries (bounded at 1)

    def due(self, now: float) -> bool:
        if not self.enabled:
            return False
        if self.kind == "on_exit":
            # Event-driven: due the moment the watched process is gone.
            return self.watch_pid > 0 and not _pid_alive(self.watch_pid)
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
    _path().write_text(json.dumps([asdict(j) for j in jobs], indent=2),
                       encoding="utf-8")


def add(name: str, interval: str | int, prompt: str,
        deliver_to: str = "", user: str = "shared",
        now: float | None = None, skill: str = "") -> Job:
    now = now if now is not None else time.time()
    secs = interval if isinstance(interval, int) else parse_interval(interval)
    jobs = [j for j in _load() if j.name != name]    # replace by name
    job = Job(name=name, interval=max(MIN_INTERVAL, int(secs)), prompt=prompt,
              deliver_to=deliver_to.strip().lower(), user=user, created=now,
              skill=(skill or "").strip())
    jobs.append(job)
    # Bound storage: keep the most-recently-created MAX_JOBS.
    if len(jobs) > MAX_JOBS:
        jobs = sorted(jobs, key=lambda j: j.created)[-MAX_JOBS:]
    _save(jobs)
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
    jobs_ = [j for j in _load() if j.name != name]    # replace by name
    job = Job(name=name, interval=0, prompt=prompt, kind="on_exit",
              watch_pid=int(pid), label=(label or "").strip(),
              deliver_to=deliver_to.strip().lower(), user=user, created=now)
    jobs_.append(job)
    if len(jobs_) > MAX_JOBS:
        jobs_ = sorted(jobs_, key=lambda j: j.created)[-MAX_JOBS:]
    _save(jobs_)
    return job


def remove(name: str) -> bool:
    jobs = _load()
    kept = [j for j in jobs if j.name != name]
    _save(kept)
    return len(kept) != len(jobs)


def set_enabled(name: str, on: bool) -> bool:
    jobs = _load()
    found = False
    for j in jobs:
        if j.name == name:
            j.enabled, found = on, True
    _save(jobs)
    return found


def jobs() -> list[Job]:
    return _load()


def due(now: float | None = None) -> list[Job]:
    now = now if now is not None else time.time()
    return [j for j in _load() if j.due(now)]


def next_due_in(now: float | None = None) -> float | None:
    """Seconds until the soonest enabled job is due (None if no enabled jobs).
    An on-exit watch has no cadence: it costs a liveness poll, so it asks to
    be re-checked shortly (0 the moment the watched process is gone)."""
    now = now if now is not None else time.time()
    waits = []
    for j in _load():
        if not j.enabled:
            continue
        if j.kind == "on_exit":
            waits.append(0.0 if j.due(now) else float(ON_EXIT_POLL))
        else:
            waits.append(max(0.0, j.interval - (now - j.last_run)))
    return min(waits) if waits else None


# A run that started this long ago and never finished is presumed dead
# (the process was killed/upgraded mid-run) and eligible for one resume.
RESUME_AFTER = int(os.environ.get("OLYMPUS_JOB_RESUME_AFTER", "3600"))


def _mark_started(name: str, now: float) -> None:
    jobs_ = _load()
    for j in jobs_:
        if j.name == name:
            j.started_at = now
    _save(jobs_)


def _mark_ran(name: str, now: float) -> None:
    jobs_ = _load()
    for j in jobs_:
        if j.name == name:
            j.last_run = now
            j.resume_attempts = 0       # a finished run clears resume state
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
    resumed_names = set()
    for job in interrupted(now):
        if all(j.name != job.name for j in ready):
            resumed_names.add(job.name)
            ready.append(job)
    if not ready:
        return log
    if runner is None:
        def runner(prompt: str, user: str) -> str:
            from . import orchestrator
            return orchestrator.Olympus(user=user).ask(prompt)
    for job in ready:
        prompt = _effective_prompt(job)
        if job.name in resumed_names:
            _bump_resume(job.name)
            prompt = ("A previous attempt at this task was interrupted by a "
                      "restart or upgrade before it finished. Redo it in "
                      "full now.\n\n" + prompt)
        _mark_started(job.name, now)
        try:
            answer = runner(prompt, job.user)
            _deliver(job, answer)
            memory.set_user("shared")
            memory.save("reports", f"Scheduled: {job.name}", answer)
            log.append(("resumed interrupted job" if job.name in resumed_names
                        else "ran scheduled job") + f" '{job.name}'")
        except Exception as err:
            log.append(f"scheduled job '{job.name}' failed: {str(err)[:120]}")
        finally:
            _mark_ran(job.name, now)
            if job.kind == "on_exit":
                set_enabled(job.name, False)     # one-shot: fired, now dormant
    return log


def _bump_resume(name: str) -> None:
    jobs_ = _load()
    for j in jobs_:
        if j.name == name:
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


def summary() -> str:
    js = _load()
    if not js:
        return "No scheduled jobs. Add one: olympus schedule add <name> <interval> <prompt>"
    lines = ["Scheduled jobs:"]
    for j in js:
        state = "" if j.enabled else " (disabled)"
        to = f" → {j.deliver_to}" if j.deliver_to else ""
        using = f" [skill: {j.skill}]" if getattr(j, "skill", "") else ""
        if getattr(j, "kind", "interval") == "on_exit":
            what = j.label or f"pid {j.watch_pid}"
            when = f"when {what} exits"
        else:
            when = f"every {_human_interval(j.interval)}"
        lines.append(f"  {j.name}: {when}{to}{using}{state}"
                     f"\n     {j.prompt[:80]}")
    return "\n".join(lines)
