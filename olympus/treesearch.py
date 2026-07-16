"""Best-first tree search — a runtime planner that explores, scores, and
backtracks over candidate action sequences, layered on top of the existing DAG
orchestration and the governed browser harness (arXiv 2407.01476: inference-time
tree search is complementary to, not a replacement for, an agent).

Safety is the whole point of this being *ours* rather than an open web agent:

  * Exploration touches only READ-ONLY or REVERSIBLE steps. The engine NEVER
    calls `problem.apply()` for a side-effectful step — it cannot mutate the
    world while "thinking".
  * Any SIDE-EFFECTFUL step is a halt: it is recorded on `pending_approval` and
    handed to the approval gate, never auto-taken. A plan that can only progress
    through a side-effectful action ends `halted_for_approval`.
  * Hard, non-negotiable caps bound every search — nodes, tokens, wall-clock,
    and depth. Whichever trips first stops the search and returns the best node
    found so far (`node_cap` / `token_cap` / `time_cap` / `depth_cap`).

The engine is generic (a `Problem` is duck-typed: expand / apply / evaluate /
is_goal); `classify_browser` + `browser_step` adapt it to the browser harness.
The clock is injectable so tests are deterministic with no real waiting.
"""

from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# --- effect classes -------------------------------------------------------
READ_ONLY = "read_only"
REVERSIBLE = "reversible"
SIDE_EFFECTFUL = "side_effectful"
SAFE = frozenset({READ_ONLY, REVERSIBLE})   # the only classes we auto-explore

# --- statuses -------------------------------------------------------------
GOAL = "goal"
EXHAUSTED = "exhausted"
HALTED = "halted_for_approval"
NODE_CAP = "node_cap"
TOKEN_CAP = "token_cap"
TIME_CAP = "time_cap"
DEPTH_CAP = "depth_cap"


@dataclass(frozen=True)
class Step:
    action: str
    args: dict = field(default_factory=dict)
    effect: str = READ_ONLY
    cost_tokens: int = 0

    def to_dict(self) -> dict:
        return {"action": self.action, "args": self.args,
                "effect": self.effect, "cost_tokens": self.cost_tokens}


@dataclass
class SearchCaps:
    """Hard resource ceilings. All are enforced; the first to trip wins."""
    max_nodes: int = 50
    max_tokens: int = 100_000
    max_seconds: float = 30.0
    max_depth: int = 12

    def __post_init__(self) -> None:
        # Clamp to sane, strictly-positive bounds — a zero/negative cap would
        # disable a guard, which is exactly what a runaway search exploits.
        self.max_nodes = max(1, min(int(self.max_nodes), 100_000))
        self.max_tokens = max(1, min(int(self.max_tokens), 100_000_000))
        self.max_seconds = max(0.001, min(float(self.max_seconds), 3600.0))
        self.max_depth = max(1, min(int(self.max_depth), 1_000))

    @classmethod
    def from_env(cls) -> "SearchCaps":
        import os

        def _int(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        def _flt(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default
        return cls(
            max_nodes=_int("OLYMPUS_TREESEARCH_MAX_NODES", 50),
            max_tokens=_int("OLYMPUS_TREESEARCH_MAX_TOKENS", 100_000),
            max_seconds=_flt("OLYMPUS_TREESEARCH_MAX_SECONDS", 30.0),
            max_depth=_int("OLYMPUS_TREESEARCH_MAX_DEPTH", 12))


@dataclass
class SearchResult:
    status: str
    path: list[Step]
    best_state: Any
    best_score: float
    pending_approval: list[dict]
    stats: dict

    @property
    def reached_goal(self) -> bool:
        return self.status == GOAL

    @property
    def needs_approval(self) -> bool:
        return self.status == HALTED or bool(self.pending_approval)


@dataclass(order=True)
class _Node:
    # ordered by (-score, counter) via the heap tuple; fields here are data only
    state: Any = field(compare=False)
    path: tuple = field(compare=False, default=())
    score: float = field(compare=False, default=0.0)
    depth: int = field(compare=False, default=0)


def _effect_of(step: Any, classify: Callable[[Any], str] | None) -> str:
    if classify is not None:
        try:
            return classify(step)
        except Exception:
            return SIDE_EFFECTFUL          # fail closed: unknown ⇒ treat as unsafe
    eff = getattr(step, "effect", None)
    return eff if eff in (READ_ONLY, REVERSIBLE, SIDE_EFFECTFUL) else SIDE_EFFECTFUL


def effect_of(step: Any, classify: Callable[[Any], str] | None = None) -> str:
    """Public: the effect class of a step (fail-closed to side_effectful)."""
    return _effect_of(step, classify)


def _notify(observer: Any, event: str, info: dict) -> None:
    """Fire an optional observer hook (`branch` / `prune`) — never let an
    observation error break the search (the search is the source of truth)."""
    if observer is None:
        return
    fn = getattr(observer, event, None)
    if fn is None:
        return
    try:
        fn(info)
    except Exception as err:
        from . import errors
        errors.capture(f"treesearch.observer.{event}", err)


def _safe_eval(problem: Any, state: Any) -> float:
    try:
        return float(problem.evaluate(state))
    except Exception as err:
        from . import errors
        errors.capture("treesearch.evaluate", err)
        return float("-inf")


def search(problem: Any, initial: Any, caps: SearchCaps | None = None, *,
           clock: Callable[[], float] | None = None,
           classify: Callable[[Any], str] | None = None,
           observer: Any = None) -> SearchResult:
    """Best-first search from `initial`. `problem` provides expand(state) →
    steps, apply(state, step) → state (called ONLY for safe steps), evaluate(
    state) → float (higher is better), is_goal(state) → bool.

    Returns the best node found plus any side-effectful steps that were withheld
    for approval. Never raises for a misbehaving problem — expand/evaluate/apply
    failures are captured and that branch is skipped.

    `observer` (optional) records the exploration as it happens: `.branch(info)`
    fires for every generated child (an uncommitted checkpoint branch) and every
    withheld side-effectful step; `.prune(info)` fires when a branch is discarded
    (depth cap, apply failure, or a resource cap trimming the frontier). This is
    what lets a caller keep a signed ledger record of pruned branches."""
    caps = caps or SearchCaps()
    clock = clock or time.monotonic
    if not _has_problem_api(problem):
        raise TypeError("problem must define expand, apply, evaluate, is_goal")

    start = clock()
    stats = {"nodes": 0, "expanded": 0, "tokens": 0, "pending": 0,
             "generated": 0}
    pending: list[dict] = []

    root = _Node(initial, (), _safe_eval(problem, initial), 0)
    best = root
    if _is_goal(problem, initial):
        return _result(GOAL, root, pending, stats, start, clock)

    counter = 0
    frontier: list[tuple] = [(-root.score, counter, root)]
    status = EXHAUSTED

    while frontier:
        now = clock()
        if stats["nodes"] >= caps.max_nodes:
            status = NODE_CAP
            break
        if stats["tokens"] >= caps.max_tokens:
            status = TOKEN_CAP
            break
        if (now - start) >= caps.max_seconds:
            status = TIME_CAP
            break

        _, _, node = heapq.heappop(frontier)
        stats["nodes"] += 1
        if node.score > best.score:
            best = node
        if _is_goal(problem, node.state):
            best = node
            status = GOAL
            break
        if node.depth >= caps.max_depth:
            status = DEPTH_CAP if status == EXHAUSTED else status
            _notify(observer, "prune", {
                "path": [_step_dict(s) for s in node.path], "reason": DEPTH_CAP})
            continue

        try:
            steps = list(problem.expand(node.state))
        except Exception as err:
            from . import errors
            errors.capture("treesearch.expand", err)
            continue
        stats["expanded"] += 1

        for step in steps:
            stats["generated"] += 1
            stats["tokens"] += max(0, int(getattr(step, "cost_tokens", 0) or 0))
            effect = _effect_of(step, classify)
            base = [_step_dict(s) for s in node.path]
            if effect not in SAFE:
                # HALT: withhold the side-effectful step for the approval gate;
                # NEVER apply it during exploration.
                pending.append({
                    "path": base, "step": _step_dict(step), "effect": effect,
                    "reason": "side-effectful step requires approval"})
                stats["pending"] += 1
                _notify(observer, "branch", {
                    "path": base, "step": _step_dict(step), "effect": effect,
                    "explored": False, "reason": "withheld for approval"})
                continue
            try:
                child_state = problem.apply(node.state, step)
            except Exception as err:
                from . import errors
                errors.capture("treesearch.apply", err)
                _notify(observer, "prune", {
                    "path": base, "step": _step_dict(step),
                    "reason": "apply failed"})
                continue
            counter += 1
            child = _Node(child_state, node.path + (step,),
                          _safe_eval(problem, child_state), node.depth + 1)
            heapq.heappush(frontier, (-child.score, counter, child))
            _notify(observer, "branch", {
                "path": base, "step": _step_dict(step), "effect": effect,
                "explored": True, "score": child.score})

    if status in (NODE_CAP, TOKEN_CAP, TIME_CAP):
        # A resource cap tripped mid-search: every unexpanded frontier node is a
        # pruned branch — record each so the audit trail shows what was cut.
        for _, _, n in frontier:
            _notify(observer, "prune", {
                "path": [_step_dict(s) for s in n.path], "reason": status})
    if status == EXHAUSTED and pending:
        status = HALTED       # safe exploration is done but a gate blocks further
    return _result(status, best, pending, stats, start, clock)


def _result(status: str, best: _Node, pending: list, stats: dict,
            start: float, clock) -> SearchResult:
    stats = {**stats, "elapsed": round(clock() - start, 4),
             "depth": best.depth, "best_score": best.score}
    try:
        from . import evolve
        ok = status in (GOAL, EXHAUSTED, HALTED)
        evolve.record("treesearch", evolve.OK if ok else evolve.DEGRADED,
                      f"status={status} nodes={stats['nodes']} "
                      f"pending={stats['pending']}")
    except Exception:
        pass
    return SearchResult(status, list(best.path), best.state, best.score,
                        pending, stats)


def _has_problem_api(problem: Any) -> bool:
    return all(callable(getattr(problem, m, None))
               for m in ("expand", "apply", "evaluate", "is_goal"))


def _is_goal(problem: Any, state: Any) -> bool:
    try:
        return bool(problem.is_goal(state))
    except Exception as err:
        from . import errors
        errors.capture("treesearch.is_goal", err)
        return False


def _step_dict(step: Any) -> dict:
    if hasattr(step, "to_dict"):
        return step.to_dict()
    return {"action": str(getattr(step, "action", step))}


# --- browser adapter ------------------------------------------------------

# Perception verbs never mutate the page/server. Navigation is reversible (a GET
# + back). Everything that could submit a form, click a control, or type into a
# field is side-effectful and must not be auto-explored.
_BROWSER_READ_ONLY = frozenset({
    "observe", "read", "read_ax", "screenshot", "console", "save_pdf",
    "scroll", "hover", "wait_for", "read_dom", "get"})
_BROWSER_REVERSIBLE = frozenset({"navigate", "back", "forward", "reload"})
_BROWSER_SIDE_EFFECTFUL = frozenset({
    "click", "type", "select", "press", "drag", "rightclick", "submit",
    "upload", "accept", "dismiss", "authorize", "act"})


def classify_browser(step: Any) -> str:
    """Conservative effect class for a browser step. Anything not explicitly a
    known read-only/reversible verb is treated as side-effectful (fail closed)."""
    action = str(getattr(step, "action", step)).lower()
    if action in _BROWSER_READ_ONLY:
        return READ_ONLY
    if action in _BROWSER_REVERSIBLE:
        return REVERSIBLE
    return SIDE_EFFECTFUL


def browser_step(action: str, cost_tokens: int = 0, **args) -> Step:
    """Build a browser Step with its effect class filled in from the verb."""
    s = Step(action=action, args=args, effect=READ_ONLY, cost_tokens=cost_tokens)
    return Step(action=s.action, args=s.args, effect=classify_browser(s),
                cost_tokens=cost_tokens)


# --- approval-gate handoff ------------------------------------------------

def to_approvals(result: SearchResult, user: str,
                 mapper: Callable[[dict], dict | None]) -> list:
    """Hand a search's withheld side-effectful steps to the approval spine.
    `mapper(pending_entry) -> {"type","payload","title"} | None` turns a pending
    step into an action to PREPARE (a human then approves/denies it via the
    normal spine). Returns the prepared actions. Steps the mapper declines
    (returns None) are skipped. This is the ONLY bridge from a search to a
    world-affecting action — and it prepares, never executes."""
    from . import actions
    prepared = []
    for entry in result.pending_approval:
        spec = mapper(entry)
        if not spec or not spec.get("type"):
            continue
        try:
            act = actions.prepare(user, spec["type"], spec.get("payload", {}),
                                  why=spec.get("title", "proposed by tree search"))
            prepared.append(act)
        except Exception as err:
            from . import errors
            errors.capture("treesearch.to_approvals", err)
    return prepared
