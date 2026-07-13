"""Standing goals with completion contracts — the agent doesn't forget.

A goal is a user-stated objective that outlives the conversation turn:
"get the newsletter signup flow live", "find me three grant programs worth
applying to". Each goal carries a **completion contract** — what "done"
means, stated up front — and the heartbeat keeps working active goals on a
cadence: one concrete unit of work per cycle, progress logged, then a
completion judgment **against evidence, not assertions**. The judge only
accepts concrete artifacts (outputs produced, messages sent, numbers
measured); "I did it" is not evidence. That contract-then-evidence loop is
what turns "remember to..." into something that actually finishes.

Storage: MEMORY_DIR/goals.json (same JSON-list pattern as scheduler.py).
Surfaces: `olympus goal ...` CLI, `/goal` in chat, and the heartbeat cycle
(OLYMPUS_GOALS_EVERY seconds, default 6h, 0 disables).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from . import config

MAX_ACTIVE = 10            # per instance — a goal list, not a backlog dump
MAX_PROGRESS = 40          # progress notes kept per goal (oldest folded away)
MAX_CHECKS = 30            # judgments before a goal is marked stalled
CONFIDENCE_FLOOR = 0.7     # judge must be this sure before a goal closes

DEFAULT_CONTRACT = ("The goal as stated has verifiably happened, with "
                    "concrete evidence in the progress log (artifacts, "
                    "outputs, confirmations — not assertions).")

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "done": {"type": "boolean",
                 "description": "True ONLY if the contract is met by evidence"},
        "confidence": {"type": "number",
                       "description": "0-1: how well the evidence supports done"},
        "evidence": {"type": "string",
                     "description": "The concrete evidence that satisfies the "
                                    "contract (empty if not done)"},
        "missing": {"type": "string",
                    "description": "What is still missing for completion "
                                   "(empty if done)"},
    },
    "required": ["done", "confidence", "evidence", "missing"],
}


@dataclass
class Goal:
    id: str
    user: str
    text: str
    contract: str
    status: str = "active"        # active | done | stalled | dropped
    created: float = 0.0
    last_worked: float = 0.0
    checks: int = 0
    progress: list = field(default_factory=list)   # [{"ts", "note"}]
    evidence: str = ""            # what closed it (or why it stalled)
    wait_pid: int = 0             # /goal wait: park cycles while this runs


def _path():
    return config.MEMORY_DIR / "goals.json"


def _load() -> list[Goal]:
    p = _path()
    if not p.exists():
        return []
    try:
        return [Goal(**d) for d in json.loads(p.read_text(encoding="utf-8"))]
    except (json.JSONDecodeError, TypeError, OSError):
        return []


def _save(goals: list[Goal]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([asdict(g) for g in goals], indent=2),
                 encoding="utf-8")


def goals_every() -> int:
    try:
        return int(os.environ.get("OLYMPUS_GOALS_EVERY", str(6 * 3600)))
    except ValueError:
        return 6 * 3600


# --- lifecycle -------------------------------------------------------------

def add(user: str, text: str, contract: str = "") -> str:
    text = (text or "").strip()
    if not text:
        return "Usage: goal add <what should stay true / get done>"
    goals = _load()
    if sum(g.status == "active" for g in goals) >= MAX_ACTIVE:
        return (f"There are already {MAX_ACTIVE} active goals — finish or "
                "drop one first (`goal list`, `goal drop <id>`).")
    g = Goal(id=uuid.uuid4().hex[:8], user=user, text=text,
             contract=(contract or "").strip() or DEFAULT_CONTRACT,
             created=time.time())
    goals.append(g)
    _save(goals)
    return (f"Goal {g.id} set: {g.text}\nDone means: {g.contract}\n"
            f"The heartbeat works active goals every "
            f"{max(goals_every(), 1) // 3600}h and closes them only on "
            "evidence.")


def get(goal_id: str) -> Goal | None:
    return next((g for g in _load() if g.id == goal_id), None)


def active(user: str | None = None) -> list[Goal]:
    return [g for g in _load() if g.status == "active"
            and (user is None or g.user == user)]


def note_progress(goal_id: str, note: str) -> None:
    goals = _load()
    for g in goals:
        if g.id == goal_id:
            g.progress.append({"ts": time.time(), "note": str(note)[:2000]})
            g.progress = g.progress[-MAX_PROGRESS:]
            g.last_worked = time.time()
    _save(goals)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)            # signal 0: existence check, no effect
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                # exists, owned by someone else
    except OSError:
        return False


def wait_on(goal_id: str, pid: int) -> str:
    """Park a goal's work cycles while `pid` runs (Hermes `/goal wait`): the
    heartbeat skips it until the process exits, then resumes with a progress
    note — so a goal that spawned a long build/backtest doesn't burn cycles
    re-checking before there's anything new to judge."""
    goals = _load()
    for g in goals:
        if g.id == goal_id:
            if g.status != "active":
                return f"Goal {goal_id} is {g.status} — nothing to wait on."
            if not _pid_alive(pid):
                return f"Process {pid} isn't running — goal left as-is."
            g.wait_pid = int(pid)
            g.progress.append({"ts": time.time(),
                               "note": f"[waiting on process {pid}]"})
            _save(goals)
            return (f"Goal {g.id} is parked on process {pid}; work cycles "
                    "resume when it exits.")
    return f"No goal with id '{goal_id}'."


def set_status(goal_id: str, status: str, evidence: str = "") -> str:
    goals = _load()
    for g in goals:
        if g.id == goal_id:
            g.status = status
            if evidence:
                g.evidence = str(evidence)[:2000]
            _save(goals)
            return f"Goal {g.id} is now {status}: {g.text}"
    return f"No goal with id '{goal_id}'."


def summary(user: str | None = None) -> str:
    goals = [g for g in _load() if user is None or g.user == user]
    if not goals:
        return ("No goals yet. Set one with `goal add <objective>` "
                "(optionally `:: <what done means>`).")
    lines = ["Standing goals:"]
    for g in goals:
        flag = {"active": "▸", "done": "✓", "stalled": "⚠", "dropped": "✗"}\
            .get(g.status, "?")
        lines.append(f"  {flag} [{g.id}] ({g.status}) {g.text}")
        if g.status == "active" and g.progress:
            last = g.progress[-1]["note"]
            lines.append(f"      last progress: {last[:100]}")
        if g.status in ("done", "stalled") and g.evidence:
            lines.append(f"      {g.evidence[:120]}")
    return "\n".join(lines)


# --- the work/judge loop -----------------------------------------------------

def _progress_log(g: Goal) -> str:
    if not g.progress:
        return "(no progress yet)"
    return "\n".join(f"- {p['note']}" for p in g.progress)


def judge(g: Goal, judge_fn: Callable[..., dict] | None = None) -> dict:
    """Evidence-based completion judgment. Returns the JUDGE_SCHEMA dict.
    `judge_fn(system, messages, schema)` is injectable for tests; production
    uses the pool's verify-role model (the hallucination-controller seat)."""
    if judge_fn is None:
        from . import firstrun
        if not firstrun.configured():
            return {"done": False, "confidence": 0.0, "evidence": "",
                    "missing": "no model configured — set a provider key to "
                               "judge goal completion"}
        from . import backend
        pool = config.ModelPool.from_env()

        def judge_fn(system, messages, schema):
            return backend.complete_json(pool.for_role("verify"), system,
                                         messages, schema, effort="medium")
    system = (
        "You judge whether a standing goal is COMPLETE, strictly against its "
        "completion contract and the evidence in the progress log. Assertions "
        "('done', 'I did it') are NOT evidence — only concrete artifacts "
        "count: produced outputs, delivered messages, measured numbers, "
        "dates, links, file paths. When evidence is thin, done=false and say "
        "exactly what is missing.")
    messages = [{"role": "user", "content":
                 f"Goal: {g.text}\n\nCompletion contract: {g.contract}\n\n"
                 f"Progress log:\n{_progress_log(g)}\n\n"
                 "Judge completion now."}]
    try:
        verdict = judge_fn(system, messages, JUDGE_SCHEMA)
    except Exception as err:
        return {"done": False, "confidence": 0.0, "evidence": "",
                "missing": f"judgment failed: {err}"}
    return {"done": bool(verdict.get("done")),
            "confidence": float(verdict.get("confidence") or 0.0),
            "evidence": str(verdict.get("evidence") or ""),
            "missing": str(verdict.get("missing") or "")}


def work_one(g: Goal, runner: Callable[[str, str], str] | None = None,
             judge_fn: Callable[..., dict] | None = None) -> str:
    """One heartbeat cycle for one goal: a unit of work, then a judgment.
    `runner(user, prompt)` is injectable for tests; production runs the full
    council pipeline. Returns a log line."""
    if runner is None:
        def runner(user: str, prompt: str) -> str:
            from . import orchestrator
            return orchestrator.Olympus(user=user).ask(prompt)
    prompt = (
        f"You are working a STANDING GOAL for the user.\n"
        f"Goal: {g.text}\n"
        f"Done means: {g.contract}\n"
        f"Progress so far:\n{_progress_log(g)}\n\n"
        "Do the next single most useful unit of work toward this goal NOW, "
        "using your tools. Then report exactly what you produced this cycle "
        "as evidence (artifacts, findings, drafts, confirmations) — never "
        "just say work is done without showing it.")
    try:
        report = runner(g.user, prompt)
    except Exception as err:
        note_progress(g.id, f"[cycle failed: {err}]")
        return f"Goal {g.id}: work cycle failed ({str(err)[:120]})"
    note_progress(g.id, report)

    fresh = get(g.id) or g
    verdict = judge(fresh, judge_fn=judge_fn)
    goals = _load()
    for stored in goals:
        if stored.id == g.id:
            stored.checks += 1
            if verdict["done"] and verdict["confidence"] >= CONFIDENCE_FLOOR:
                stored.status = "done"
                stored.evidence = verdict["evidence"][:2000]
                _save(goals)
                return (f"Goal {g.id} COMPLETE on evidence: "
                        f"{verdict['evidence'][:160]}")
            if stored.checks >= MAX_CHECKS:
                stored.status = "stalled"
                stored.evidence = (f"stalled after {MAX_CHECKS} cycles; "
                                   f"still missing: {verdict['missing']}")[:2000]
                _save(goals)
                return (f"Goal {g.id} STALLED after {MAX_CHECKS} cycles "
                        f"(missing: {verdict['missing'][:120]})")
            _save(goals)
            return (f"Goal {g.id}: progress logged; not done yet "
                    f"(missing: {verdict['missing'][:120]})")
    return f"Goal {g.id}: vanished mid-cycle"


def next_due_in(now: float | None = None) -> float | None:
    """Seconds until the soonest active goal wants a work cycle (0.0 = now);
    None when the cycle is disabled or nothing is active. Mirrors
    scheduler.next_due_in for the hibernate wake computation."""
    interval = goals_every()
    if interval <= 0:
        return None
    goals = active()
    if not goals:
        return None
    now = now or time.time()
    return min(max(0.0, interval - (now - max(g.last_worked, g.created)))
               if g.progress else 0.0
               for g in goals)


def run_due(now: float | None = None,
            runner: Callable[[str, str], str] | None = None,
            judge_fn: Callable[..., dict] | None = None) -> list[str]:
    """Work every active goal whose cadence has elapsed. Called by the
    heartbeat; returns log lines (empty when nothing was due)."""
    interval = goals_every()
    if interval <= 0:
        return []
    now = now or time.time()
    out = []
    for g in active():
        if g.wait_pid:
            if _pid_alive(g.wait_pid):
                continue           # parked on a still-running process
            note_progress(g.id, f"[process {g.wait_pid} finished — resuming]")
            goals = _load()
            for stored in goals:
                if stored.id == g.id:
                    stored.wait_pid = 0
            _save(goals)
            g = get(g.id) or g
            out.append(work_one(g, runner=runner, judge_fn=judge_fn))
            continue
        if now - max(g.last_worked, g.created) >= interval or not g.progress:
            out.append(work_one(g, runner=runner, judge_fn=judge_fn))
    return out
