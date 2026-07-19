"""Per-agent heartbeats — periodic autonomous wake-ups with a compact context.

The global heartbeat (heartbeat.py) runs Olympus's own maintenance. This is
the *user-facing* counterpart: each beat wakes on its own cadence, looks at a
small dedicated context (the beat's charter plus that user's relevant durable
memory), and either stays quiet or pushes a note to chat.

Quietness is the contract: a wake-up whose honest answer is "nothing needs
attention" replies with the literal token HB_OK and nothing is delivered —
so a beat can run hourly without turning the owner's chat into a log file.

A beat is NOT a scheduled job (scheduler.py runs the full pipeline and always
delivers). It is deliberately small: one fast-model call, low effort, compact
context — cheap enough to run often, which is what makes it a heartbeat.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field

from . import config, scheduler

QUIET_TOKEN = "HB_OK"

SYSTEM = (
    "You are the heartbeat of Olympus, a personal AI system. You wake on a "
    "cadence to check one standing concern for your user. You are not "
    "answering a chat message; you are deciding whether anything needs the "
    "user's attention right now.\n\n"
    f"If nothing does, reply with exactly {QUIET_TOKEN} and nothing else.\n"
    "If something does, reply with a short, concrete note (2-6 sentences) "
    "the user will read on their phone. No preamble, no headers."
)


@dataclass
class Beat:
    id: str
    user: str
    every: int                   # seconds between wake-ups
    prompt: str                  # the standing concern this beat checks
    deliver_to: str = ""         # "" = every configured channel (notify_all)
    enabled: bool = True
    last_run: float = 0.0
    created: float = field(default=0.0)

    def due(self, now: float) -> bool:
        return self.enabled and (now - self.last_run) >= self.every


def _path():
    p = config.MEMORY_DIR / "heartbeats.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> list[Beat]:
    try:
        return [Beat(**d) for d in json.loads(
            _path().read_text(encoding="utf-8"))]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _save(beats: list[Beat]) -> None:
    # Atomic publish: _load maps a torn file to [], so a cross-process reader
    # mid-write would see "no beats" (ADR 0005).
    p = _path()
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps([asdict(b) for b in beats], indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)


def _mutex():
    """Cross-process lock for beat load-modify-save cycles (ADR 0005): the
    gateway/chat process adds and removes beats while the heartbeat process
    marks them run."""
    from . import proclock
    return proclock.lock("agentbeat")


def add(user: str, every: str | int, prompt: str,
        deliver_to: str = "", now: float | None = None) -> Beat:
    now = now if now is not None else time.time()
    secs = every if isinstance(every, int) else scheduler.parse_interval(every)
    beat = Beat(id=uuid.uuid4().hex[:8], user=user, every=secs,
                prompt=prompt.strip(), deliver_to=deliver_to,
                created=now, last_run=now)   # first wake one cadence from now
    with _mutex():
        beats = _load()
        beats.append(beat)
        _save(beats)
    return beat


def remove(user: str, beat_id: str) -> bool:
    with _mutex():
        beats = _load()
        kept = [b for b in beats if not (b.id == beat_id and b.user == user)]
        if len(kept) == len(beats):
            return False
        _save(kept)
        return True


def for_user(user: str) -> list[Beat]:
    return [b for b in _load() if b.user == user]


def summary(user: str) -> str:
    beats = for_user(user)
    if not beats:
        return ("No heartbeats. Add one with:\n"
                "/heartbeat add <every> <what to check>\n"
                "e.g. /heartbeat add 2h anything urgent in my goals or memory?")
    lines = ["Your heartbeats:"]
    for b in beats:
        state = "on" if b.enabled else "off"
        lines.append(f"#{b.id} every {b.every // 60}m [{state}] — {b.prompt}")
    lines.append("Drop one with /heartbeat drop <id>.")
    return "\n".join(lines)


def _compact_context(beat: Beat) -> str:
    """The small, dedicated context a wake-up sees: standing goals plus the
    durable memory most relevant to the beat's charter. Deliberately bounded —
    a heartbeat that needs the full pipeline should be a scheduler job."""
    parts: list[str] = []
    try:
        from . import goals
        text = goals.summary(beat.user)
        if text and "No goals" not in text:
            parts.append("Standing goals:\n" + text[:1500])
    except Exception:
        pass
    try:
        from . import recall
        block = recall.context_block(beat.user, beat.prompt)
        if block:
            parts.append(block[:2000])
    except Exception:
        pass
    return "\n\n".join(parts)


def _default_runner(beat: Beat) -> str:
    """One fast-model, low-effort call — the compact wake-up, not the council."""
    from . import backend
    settings = config.ModelPool.from_env().fastest()
    context = _compact_context(beat)
    prompt = beat.prompt if not context else (
        f"{beat.prompt}\n\n--- context ---\n{context}")
    return backend.complete_text(
        settings, SYSTEM, [{"role": "user", "content": prompt}], effort="low")


def is_quiet(reply: str) -> bool:
    """A wake-up is quiet when the model answered 'nothing needs attention'.
    Tolerates trailing punctuation/whitespace but nothing substantive."""
    t = (reply or "").strip()
    return t == "" or (t.upper().startswith(QUIET_TOKEN)
                       and len(t) <= len(QUIET_TOKEN) + 2)


def _deliver(beat: Beat, text: str) -> str:
    from . import gateway
    note = f"💓 heartbeat #{beat.id}: {text}"
    # Egress Phase C: a heartbeat push to a chat channel is a BROADCAST sink.
    # The direct `deliver_to` path below bypasses gateway.notify_all's guard, so
    # gate here too — sensitive beat output is held, not pushed to a group.
    from . import config, egress
    if config.egress_guard_enabled():
        d = egress.guard(note, egress.ChannelKind.BROADCAST, user="shared")
        if d.verdict is egress.Verdict.HOLD:
            return "nowhere (held: sensitive content)"
    if beat.deliver_to:
        fns = {}
        try:
            from . import discord, ntfy, signal as signal_gw, slack, telegram
            fns = {"telegram": telegram.notify, "discord": discord.notify,
                   "slack": slack.notify, "signal": signal_gw.notify,
                   "ntfy": ntfy.notify}
        except Exception:
            pass
        fn = fns.get(beat.deliver_to)
        if fn:
            try:
                return beat.deliver_to if fn(note) else "nowhere (channel down)"
            except Exception:
                return "nowhere (channel errored)"
    delivered = gateway.notify_all(note)
    return ", ".join(delivered) if delivered else "nowhere (no channel)"


def run_due(now: float | None = None, runner=None) -> list[str]:
    """Wake every due beat once; returns log lines. Failures mark the beat as
    run (so a broken beat can't spin every tick) and report the error."""
    now = now if now is not None else time.time()
    # Mark the due beats as run UNDER the lock, BEFORE running them, then run
    # the (minutes-long) LLM beats with no lock held. The old shape loaded the
    # list, ran every beat, then blind-saved the stale list at the end — a
    # beat added from the chat process mid-run was silently deleted
    # (ADR 0005 second-ring sweep). Marking first also preserves the
    # broken-beat guarantee: a crash mid-run can't respin every tick.
    due: list[Beat] = []
    with _mutex():
        beats = _load()
        for beat in beats:
            if beat.due(now):
                beat.last_run = now
                due.append(beat)
        if due:
            _save(beats)
    log: list[str] = []
    for beat in due:
        try:
            reply = (runner or _default_runner)(beat)
        except Exception as err:
            log.append(f"beat #{beat.id} failed: {err}")
            continue
        if is_quiet(reply):
            log.append(f"beat #{beat.id}: quiet ({QUIET_TOKEN})")
            continue
        where = _deliver(beat, reply.strip())
        log.append(f"beat #{beat.id}: delivered to {where}")
    return log


def next_due_in(now: float | None = None) -> float | None:
    """Seconds until the soonest enabled beat is due (None when there are no
    beats) — feeds the serverless next-wake computation."""
    now = now if now is not None else time.time()
    waits = [max(0.0, b.every - (now - b.last_run))
             for b in _load() if b.enabled]
    return min(waits) if waits else None
