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

Interval parsing is deliberately forgiving — it understands the phrases people
actually type ("hourly", "every 30m", "daily", "weekly", "every 2 days") and
falls back to a daily cadence rather than failing.
"""

from __future__ import annotations

import json
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


@dataclass
class Job:
    name: str
    interval: int                # seconds
    prompt: str
    deliver_to: str = ""         # "", "telegram", "discord", "slack", "signal"
    user: str = "shared"
    enabled: bool = True
    last_run: float = 0.0
    created: float = field(default=0.0)

    def due(self, now: float) -> bool:
        return self.enabled and (now - self.last_run) >= self.interval


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
        now: float | None = None) -> Job:
    now = now if now is not None else time.time()
    secs = interval if isinstance(interval, int) else parse_interval(interval)
    jobs = [j for j in _load() if j.name != name]    # replace by name
    job = Job(name=name, interval=max(MIN_INTERVAL, int(secs)), prompt=prompt,
              deliver_to=deliver_to.strip().lower(), user=user, created=now)
    jobs.append(job)
    _save(jobs)
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
    """Seconds until the soonest enabled job is due (None if no enabled jobs)."""
    now = now if now is not None else time.time()
    waits = [max(0.0, j.interval - (now - j.last_run))
             for j in _load() if j.enabled]
    return min(waits) if waits else None


def _mark_ran(name: str, now: float) -> None:
    jobs_ = _load()
    for j in jobs_:
        if j.name == name:
            j.last_run = now
    _save(jobs_)


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
    except Exception:
        pass


def run_due(now: float | None = None, runner=None) -> list[str]:
    """Execute every due job through the Olympus pipeline; deliver + log.
    `runner(prompt, user) -> str` is injectable for tests."""
    now = now if now is not None else time.time()
    log: list[str] = []
    ready = due(now)
    if not ready:
        return log
    if runner is None:
        def runner(prompt: str, user: str) -> str:
            from . import orchestrator
            return orchestrator.Olympus(user=user).ask(prompt)
    for job in ready:
        try:
            answer = runner(job.prompt, job.user)
            _deliver(job, answer)
            memory.set_user("shared")
            memory.save("reports", f"Scheduled: {job.name}", answer)
            log.append(f"ran scheduled job '{job.name}'")
        except Exception as err:
            log.append(f"scheduled job '{job.name}' failed: {str(err)[:120]}")
        finally:
            _mark_ran(job.name, now)
    return log


def summary() -> str:
    js = _load()
    if not js:
        return "No scheduled jobs. Add one: olympus schedule add <name> <interval> <prompt>"
    lines = ["Scheduled jobs:"]
    for j in js:
        every = (f"{j.interval // 3600}h" if j.interval % 3600 == 0
                 else f"{j.interval // 60}m")
        state = "" if j.enabled else " (disabled)"
        to = f" → {j.deliver_to}" if j.deliver_to else ""
        lines.append(f"  {j.name}: every {every}{to}{state}\n     {j.prompt[:80]}")
    return "\n".join(lines)
