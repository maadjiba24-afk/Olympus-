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

from . import config, memory

MAX_ACTIVE = 10            # per owner — one tenant cannot starve another
MAX_TOTAL_GOALS = 1000     # absolute storage ceiling; additions fail closed
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
    # Atomic publish (tmp + os.replace): readers run WITHOUT the mutex, and
    # _load maps a torn/truncated file to [] — a plain write_text here would
    # let a cross-process reader see "no goals" mid-write, and a crash
    # mid-save would wipe every goal (ADR 0005).
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    from . import atomicio
    atomicio.publish(tmp, p, json.dumps([asdict(g) for g in goals], indent=2))


def goals_every() -> int:
    try:
        return int(os.environ.get("OLYMPUS_GOALS_EVERY", str(6 * 3600)))
    except ValueError:
        return 6 * 3600


# --- lifecycle -------------------------------------------------------------

def _mutex():
    """Cross-process lock for every goals load-modify-save cycle (ADR 0005):
    the heartbeat process and the web process both mutate the same file."""
    from . import proclock
    return proclock.lock("goals")


def _owner(user: str) -> str:
    """The durable principal a goal belongs to, VERBATIM.

    The only transformation is that a missing/empty owner becomes "shared" —
    the legacy default, kept so records written before goals carried an owner
    stay addressable. Nothing is stripped, case-folded, or truncated, and
    `memory.safe_id` is deliberately not used: it collapses every run of
    non-[A-Za-z0-9_-] to a single "-" and cuts at 64 characters, which is right
    for building a filesystem path and wrong for deciding whether two
    principals are the same person.
    """
    return str(user or "shared")


def _matches(goal: Goal, goal_id: str, user: str) -> bool:
    """A goal id is an identifier, never an authorization credential."""
    return goal.id == goal_id and goal.user == _owner(user)


def add(user: str, text: str, contract: str = "") -> str:
    text = (text or "").strip()[:1000]          # bound: goal text can't bloat
    contract = (contract or "").strip()[:1000]
    if not text:
        return "Usage: goal add <what should stay true / get done>"
    owner = _owner(user)
    with _mutex():
        goals = _load()
        if sum(g.status == "active" and g.user == owner
               for g in goals) >= MAX_ACTIVE:
            return (f"There are already {MAX_ACTIVE} active goals — finish or "
                    "drop one first (`goal list`, `goal drop <id>`).")
        if len(goals) >= MAX_TOTAL_GOALS:
            return "Global goal storage capacity reached; no goal was added."
        g = Goal(id=uuid.uuid4().hex[:8], user=owner, text=text,
                 contract=contract or DEFAULT_CONTRACT,
                 created=time.time())
        goals.append(g)
        _save(goals)
    return (f"Goal {g.id} set: {g.text}\nDone means: {g.contract}\n"
            f"The heartbeat works active goals every "
            f"{max(goals_every(), 1) // 3600}h and closes them only on "
            "evidence.")


def get(goal_id: str, user: str = "shared") -> Goal | None:
    return next((g for g in _load() if _matches(g, goal_id, user)), None)


def active(user: str | None = None) -> list[Goal]:
    return [g for g in _load() if g.status == "active"
            and (user is None or g.user == user)]


def note_progress(goal_id: str, note: str, user: str = "shared") -> None:
    with _mutex():
        goals = _load()
        for g in goals:
            if _matches(g, goal_id, user):
                g.progress.append({"ts": time.time(),
                                   "note": str(note)[:2000]})
                g.progress = g.progress[-MAX_PROGRESS:]
                g.last_worked = time.time()
        _save(goals)


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

        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True

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


def wait_on(goal_id: str, pid: int, user: str = "shared") -> str:
    """Park a goal's work cycles while `pid` runs (Hermes `/goal wait`): the
    heartbeat skips it until the process exits, then resumes with a progress
    note — so a goal that spawned a long build/backtest doesn't burn cycles
    re-checking before there's anything new to judge."""
    with _mutex():
        goals = _load()
        for g in goals:
            if _matches(g, goal_id, user):
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


def set_status(goal_id: str, status: str, evidence: str = "",
               user: str = "shared") -> str:
    with _mutex():
        goals = _load()
        for g in goals:
            if _matches(g, goal_id, user):
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
        note_progress(g.id, f"[cycle failed: {err}]", user=g.user)
        _telemetry(FAIL, str(err)[:120])
        return f"Goal {g.id}: work cycle failed ({str(err)[:120]})"
    note_progress(g.id, report, user=g.user)

    fresh = get(g.id, user=g.user) or g
    verdict = judge(fresh, judge_fn=judge_fn)
    # Behavioral contract at the completion chokepoint (defense in depth): a
    # goal may close ONLY against concrete evidence at the confidence floor.
    # Evaluated BEFORE the cross-process lock — enforce() can reach
    # errors.capture (a network alert) on an engine error, and network I/O
    # must never run while holding the goals flock (ADR 0005).
    from . import behavioral_contracts as _abc
    close_ok = True
    try:
        _abc.enforce("goal.complete",
                     {"done": bool(verdict["done"]),
                      "evidence": verdict.get("evidence", ""),
                      "confidence": verdict.get("confidence", 0),
                      "floor": CONFIDENCE_FLOOR})
    except _abc.ContractViolation:
        close_ok = False
    with _mutex():
        goals = _load()
        for stored in goals:
            if not _matches(stored, g.id, g.user):
                continue
            if stored.status != "active":
                # The other process closed/dropped this goal between our
                # judgment and this write — never overwrite its decision
                # with a stale verdict.
                return (f"Goal {g.id} became {stored.status} mid-cycle — "
                        "leaving it untouched.")
            stored.checks += 1
            if (close_ok and verdict["done"]
                    and verdict["confidence"] >= CONFIDENCE_FLOOR):
                stored.status = "done"
                stored.evidence = verdict["evidence"][:2000]
                _save(goals)
                _telemetry(OK)                    # closed on evidence: a win
                return (f"Goal {g.id} COMPLETE on evidence: "
                        f"{verdict['evidence'][:160]}")
            if stored.checks >= MAX_CHECKS:
                stored.status = "stalled"
                stored.evidence = (f"stalled after {MAX_CHECKS} cycles; "
                                   f"still missing: {verdict['missing']}")[:2000]
                _save(goals)
                _telemetry(FAIL, f"stalled: {verdict['missing'][:80]}")
                return (f"Goal {g.id} STALLED after {MAX_CHECKS} cycles "
                        f"(missing: {verdict['missing'][:120]})")
            _save(goals)
            _telemetry(DEGRADED)                  # progress but not yet done
            return (f"Goal {g.id}: progress logged; not done yet "
                    f"(missing: {verdict['missing'][:120]})")
    return f"Goal {g.id}: vanished mid-cycle"


# --- closure alerts: owner-bound, and fail-closed by default ---------------
#
# A closing goal's log line carries `verdict["evidence"][:160]` (or, on a stall,
# `verdict["missing"][:120]`) — the concrete artifacts a private objective
# produced. That is the goal OWNER's data. The heartbeat used to push it with a
# bare `gateway.notify_all("🎯 " + line)`: no owner argument at all, so the
# egress guard evaluated the "shared" namespace and the payload fanned out to
# every configured channel. Measured on this tree before the fix, two owners
# closing in one tick put BOTH evidence strings on all nine channels, and a
# secret from owner A's vault classified ALLOW under "shared" that classifies
# HOLD under A.
#
# `notify_all(text, user=owner)` fixes the CLASSIFICATION but not the
# DESTINATION: the owner picks which vault the guard checks, and the fan-out
# then calls every transport as `notify(text)`. No transport accepts an owner,
# and every proactive destination is one installation-global address. Olympus
# has no verified per-owner proactive route, so there is nothing correct to
# deliver to — and the default here is therefore to deliver NOWHERE.
#
# The closure itself is never affected. The transition is persisted, counters
# advance, telemetry fires and the heartbeat log still reports COMPLETE/STALLED.
# Only the external push is withheld, and the reason is stated in the log.

#: Explicit, goal-specific opt-in to installation-wide fan-out. Default off.
BROADCAST_ENV = "OLYMPUS_GOAL_BROADCAST"

#: Log reasons. Deliberately free of owner ids, evidence, missing-text and
#: destination names — a refusal must not become the leak it prevents.
_NO_ROUTE = ("closure alert not delivered: no owner-targeted route "
             f"(set {BROADCAST_ENV}=1 with OLYMPUS_EGRESS_GUARD=1 to accept "
             "installation-wide fan-out)")
_NEEDS_GUARD = (f"closure alert not delivered: {BROADCAST_ENV} requires "
                "OLYMPUS_EGRESS_GUARD=1")

def _closed_this_cycle(goal: Goal, line: str) -> bool:
    """Did THIS cycle close THIS goal?

    Anchored to the exact prefixes `work_one` emits, and bound to the goal's own
    id — not a substring search. `work_one`'s non-closing line is

        Goal <id>: progress logged; not done yet (missing: <verdict.missing>)

    and `missing` is free-form model output. A marker test that matched
    ANYWHERE in the line would therefore fire on a progress cycle whose
    `missing` text merely happened to contain " COMPLETE on evidence:" or
    " STALLED after " — a model can produce either, and an injected page could
    aim for it — delivering a goal that had not closed at all.

    The closing lines put a SPACE after the id, the progress line a COLON, so
    an anchored prefix separates them exactly. Including the id also keeps one
    goal's line from ever being credited to another, and the lowercase
    "became done mid-cycle" line — another process's close, not ours — matches
    neither prefix and so never re-alerts.
    """
    return line.startswith((
        f"Goal {goal.id} COMPLETE on evidence:",
        f"Goal {goal.id} STALLED after {MAX_CHECKS} cycles (missing:",
    ))


def _broadcast_opt_in() -> bool:
    return os.environ.get(BROADCAST_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")


def _shared_broadcast_enabled() -> bool:
    """Whether the operator has explicitly accepted installation-wide fan-out.

    Requires BOTH the goal-specific opt-in and the egress guard. The guard is
    not optional here: with it off, `notify_all`'s `user` argument is never
    read, so the payload is never classified and a goal whose evidence quotes
    the owner's own stored secret would go out unexamined. Broadcast without
    the guard is the weakest possible mode, so it fails closed instead.
    """
    if not _broadcast_opt_in():
        return False
    return bool(config.egress_guard_enabled())


def _broadcast_requested_without_guard() -> bool:
    """Opt-in set, guard off — worth its own log reason, so an operator who
    asked for delivery learns why it did not happen."""
    return _broadcast_opt_in() and not config.egress_guard_enabled()


def _deliver_closure(goal: Goal, line: str,
                     notify: Callable[..., object] | None) -> str:
    """Deliver one closure alert as its DURABLE owner, or say why it was not.

    Returns a log line; never raises. Decision order:

      1. an injected `notify` callback  → `notify(text, user=goal.user)`. The
         owner is passed through EXACTLY as `_owner` returns it — the durable
         principal verbatim, with only a missing/empty owner mapped to
         "shared", and never `memory.safe_id` (which collapses punctuation and
         truncates at 64 characters). This is the extension point a real
         per-owner route plugs into.
      2. installation-wide fan-out, explicitly accepted → `gateway.notify_all`
         with the same exact owner, so the guard checks the OWNER's vault.
      3. otherwise → nothing leaves, and the reason is logged.

    THE STATUS LINE SAYS ONLY WHAT IS KNOWN. `gateway.notify_all` returns the
    channels that ACCEPTED the alert, and returns `[]` both when the egress
    guard HOLDS it and when no channel is configured or accepting — so
    reporting "delivered" on a bare call would claim delivery for a payload
    that was withheld precisely because it carried the owner's secret. The
    broadcast path therefore reads the returned list. An injected callback has
    no such contract, so its success is described as HANDED to the notifier,
    which is all this function can honestly attest.

    A callback that raises is NEVER retried through the fan-out: that would
    turn a delivery failure into the disclosure this exists to prevent.
    """
    text = "🎯 " + line
    owner = _owner(goal.user)
    if notify is not None:
        try:
            notify(text, user=owner)
        except Exception as err:
            # Surfaced, not swallowed, and not compensated for by a broadcast.
            # The goal stays closed — delivery is downstream of the transition.
            return (f"Goal {goal.id}: closure alert NOT delivered "
                    f"({type(err).__name__})")
        return (f"Goal {goal.id}: closure alert handed to the owner-targeted "
                "notifier")

    if not _shared_broadcast_enabled():
        reason = (_NEEDS_GUARD if _broadcast_requested_without_guard()
                  else _NO_ROUTE)
        return f"Goal {goal.id}: {reason}"

    from . import gateway
    try:
        accepted = gateway.notify_all(text, user=owner)
    except Exception as err:
        return (f"Goal {goal.id}: closure alert NOT delivered "
                f"({type(err).__name__})")
    if not accepted:
        # Held by the guard, or no channel configured/accepting. Which of those
        # it was is deliberately NOT reported here: naming the destinations, or
        # distinguishing "held" from "nobody listening", would leak exactly what
        # the guard withheld the payload to protect.
        return (f"Goal {goal.id}: closure alert NOT delivered "
                "(withheld or no channel accepted it)")
    return (f"Goal {goal.id}: closure alert broadcast to "
            f"{len(accepted)} installation-wide channel(s)")


OK, FAIL, DEGRADED = "ok", "fail", "degraded"


def _telemetry(outcome: str, detail: str = "") -> None:
    """Feed the goal cycle's outcome into the self-evolution loop (best-effort;
    the check_backoff tunable widens the cadence for goals that keep failing)."""
    try:
        from . import evolve
        evolve.record("goals", outcome, detail)
    except Exception:
        pass


def next_due_in(now: float | None = None) -> float | None:
    """Seconds until the soonest active goal wants a work cycle (0.0 = now);
    None when the cycle is disabled or nothing is active. Mirrors
    scheduler.next_due_in for the hibernate wake computation."""
    interval = goals_every()
    if interval <= 0:
        return None
    goals = [g for g in active()
             if not memory.is_ambiguous_gateway_owner(g.user)]
    if not goals:
        return None
    now = now or time.time()
    return min(max(0.0, interval - (now - max(g.last_worked, g.created)))
               if g.progress else 0.0
               for g in goals)


def run_due(now: float | None = None,
            runner: Callable[[str, str], str] | None = None,
            judge_fn: Callable[..., dict] | None = None,
            notify: Callable[..., object] | None = None) -> list[str]:
    """Work every active goal whose cadence has elapsed. Called by the
    heartbeat; returns log lines (empty when nothing was due).

    CLOSURE ALERTS ARE SENT FROM HERE, not by the caller. The heartbeat used to
    scan the returned strings for "COMPLETE"/"STALLED" and push them itself —
    by which point `Goal.user` was gone, so the alert went out un-owned and
    installation-wide. Delivery lives where the owner still does; the caller
    only logs what it is given.

    `notify(text, *, user)` is the owner-aware extension point, invoked with the
    goal's exact durable owner. Left unset, delivery is fail-closed unless an
    operator has explicitly accepted installation-wide fan-out — see
    `_deliver_closure`. Each closure appends one extra log line describing the
    delivery outcome; the transition line itself is unchanged.
    """
    interval = goals_every()
    if interval <= 0:
        return []
    # Self-tuned backoff: when goal cycles keep failing to close, the reviewer
    # widens the cadence (up to 4x) so a doomed goal stops burning cycles.
    try:
        from . import evolve
        interval = int(interval * evolve.current("goals", "check_backoff"))
    except Exception:
        pass
    now = now or time.time()
    out = []
    for g in active():
        if memory.is_ambiguous_gateway_owner(g.user):
            continue
        if g.wait_pid:
            if _pid_alive(g.wait_pid):
                continue           # parked on a still-running process
            note_progress(g.id, f"[process {g.wait_pid} finished — resuming]",
                          user=g.user)
            with _mutex():
                goals = _load()
                for stored in goals:
                    if _matches(stored, g.id, g.user):
                        stored.wait_pid = 0
                _save(goals)
            g = get(g.id, user=g.user) or g
            line = work_one(g, runner=runner, judge_fn=judge_fn)
            out.append(line)
            if _closed_this_cycle(g, line):
                out.append(_deliver_closure(g, line, notify))
            continue
        if now - max(g.last_worked, g.created) >= interval or not g.progress:
            line = work_one(g, runner=runner, judge_fn=judge_fn)
            out.append(line)
            if _closed_this_cycle(g, line):
                out.append(_deliver_closure(g, line, notify))
    return out
