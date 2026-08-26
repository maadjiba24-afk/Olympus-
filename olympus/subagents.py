"""Scriptable subagents — isolated, parallel workstreams on demand.

Olympus already runs its *planned* specialist DAG concurrently (see
orchestrator._dispatch_dag). What it lacked was Hermes-style **ad-hoc** subagent
spawning: the ability for the council (or a script) to fan a job out to several
isolated specialist runs and collect the results, without threading them through
the full route→plan→verify pipeline.

Each spawned subagent is isolated: it runs a single specialist with its own
tool loadout and returns only its final text — no shared mutable state, failures
contained per branch. `spawn_many` runs a batch concurrently with the same
backpressure cap the orchestrator uses.

Exposed to agents as the `spawn_subagent` tool so a specialist can delegate a
self-contained sub-task and get the answer back inline (a cheap way to collapse
a multi-step pipeline into one turn).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import config, memory
from .specialists import SPECIALISTS


@dataclass(frozen=True)
class SubTask:
    specialist: str
    task: str


def _is_privileged(key: str) -> bool:
    """A specialist that must NOT be reachable via delegation: a system
    specialist (self-modifying / bypasses capability separation) or one that
    would hold an action tool in a fresh, non-ingesting run (the operator
    Hermes, Chronos's send_email/call_webhook, ...). Spawn runs a specialist
    with its FULL loadout, so letting an (untrusted-content-ingesting) caller
    spawn one of these would launder an injection into a real-world action or a
    self-modification — the exact escalation capability separation exists to
    prevent."""
    from . import security
    spec = SPECIALISTS[key]
    if spec.system:
        return True
    names = {d.get("name") for d in spec.tool_defs("anthropic")}
    return bool(names & security.ACTION_TOOLS)


def spawn(specialist: str, task: str,
          settings: config.Settings | None = None) -> str:
    """Run one specialist in isolation; return its final text. Failures are
    contained and returned as a readable note rather than raised."""
    key = (specialist or "").strip().lower()
    if key not in SPECIALISTS:
        return (f"[unknown specialist '{specialist}'. Available: "
                f"{', '.join(sorted(SPECIALISTS))}]")
    if _is_privileged(key):
        return (f"[refused: '{key}' cannot be spawned as a subagent — "
                "self-modifying and credentialed specialists run only through "
                "their own governed routines, never via delegation.]")
    try:
        return SPECIALISTS[key].run(task, settings=settings)
    except Exception as err:                       # isolate this branch
        return f"[subagent {key} failed: {str(err)[:200]}]"


def spawn_many(tasks: list[SubTask],
               settings: config.Settings | None = None,
               max_workers: int | None = None) -> list[tuple[str, str]]:
    """Run several subagents concurrently. Returns [(specialist, output), ...]
    in the same order as `tasks`. Concurrency is capped like the orchestrator."""
    if not tasks:
        return []
    # The EXACT principal, not the normalized namespace: a worker thread that
    # inherited `current_user()` would run as a COLLIDING principal and could
    # reach that principal's private memory.
    user = memory.current_owner()
    cap = max_workers or min(config.MAX_CONCURRENT_CALLS, len(tasks))

    def _one(t: SubTask) -> tuple[str, str]:
        memory.set_user(user)                      # worker threads inherit user
        return (t.specialist, spawn(t.specialist, t.task, settings=settings))

    with ThreadPoolExecutor(max_workers=max(1, cap)) as pool:
        return list(pool.map(_one, tasks))


def spawn_tool(specialist: str, task: str) -> str:
    """Handler for the `spawn_subagent` tool.

    Inherit the *calling* run's credentials (config.active_settings) so a BYOK
    visitor's delegated subagent runs on their key, not the operator's env key.
    Falls back to env when there is no active run (e.g. a script-level spawn)."""
    out = spawn(specialist, task, settings=config.active_settings())
    return f"### {specialist} subagent result\n{out}"
