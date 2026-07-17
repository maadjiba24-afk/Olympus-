"""Browser-exploration planner — best-first read-only navigation toward a goal.

This is the *live-planner* integration of `treesearch` into the governed browser
harness (inference-time tree search for web agents, arXiv 2407.01476). Given a
goal and a starting page, it explores the site by navigating (a reversible GET +
back) and perceiving, scoring each page for relevance, and backtracking — but it
NEVER auto-takes a side-effectful step (click/type/submit). Those are withheld
and handed to the approval gate via `treesearch.to_approvals`, exactly like the
generic engine.

The core is pure and injected: the caller supplies `navigate` (perform a
read-only navigation → PageState), `perceive` (a PageState → candidate steps),
and `score` (a PageState → relevance in [0, 1]). Tests drive these with fakes so
no real browser is needed; `plan_with_browser` adapts a live `browser.Browser`.

Everything is bounded by `treesearch.SearchCaps` (nodes / tokens / wall-clock /
depth), so a runaway crawl is structurally impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import treesearch

# A page is "good enough" to stop at when its relevance clears this floor.
_GOAL_SCORE = 0.85
_MAX_CANDIDATE_LINKS = 8       # branching cap per page (bounds the frontier)


@dataclass(frozen=True)
class PageState:
    """An immutable perception snapshot of one page."""
    url: str
    title: str = ""
    text: str = ""              # a short text summary used for scoring
    links: tuple[str, ...] = ()  # candidate onward URLs (same-site navigations)
    depth: int = 0

    def key(self) -> str:
        return self.url


NavigateFn = Callable[[str], "PageState | None"]
PerceiveFn = Callable[["PageState"], list]      # → list[treesearch.Step]
ScoreFn = Callable[["PageState"], float]


@dataclass
class WebProblem:
    """A `treesearch.Problem` over read-only web navigation. `goal_score` is the
    relevance floor at which a page counts as the goal."""
    navigate: NavigateFn
    perceive: PerceiveFn
    score: ScoreFn
    goal_score: float = _GOAL_SCORE
    _visited: set = field(default_factory=set)

    def expand(self, state: PageState) -> list:
        # Perceive candidate steps; the perceiver classifies effect (links →
        # reversible navigate; forms/buttons → side-effectful, withheld).
        return list(self.perceive(state))[: _MAX_CANDIDATE_LINKS * 2]

    def apply(self, state: PageState, step) -> PageState:
        # Only ever called for SAFE steps (the engine guarantees it). A reversible
        # navigation to the step's url; loops are avoided by depth + the engine's
        # frontier, and re-visits are cheap (scored, not acted on).
        url = (step.args or {}).get("url", "")
        nxt = self.navigate(url)
        if nxt is None:
            raise ValueError(f"navigation to {url!r} failed")
        self._visited.add(nxt.key())
        return nxt

    def evaluate(self, state: PageState) -> float:
        try:
            return float(self.score(state))
        except Exception:
            return 0.0

    def is_goal(self, state: PageState) -> bool:
        return self.evaluate(state) >= self.goal_score


def links_to_steps(state: PageState, *, per_link_tokens: int = 50) -> list:
    """Default perceiver: turn a page's candidate links into reversible navigate
    steps (deduped, bounded). Side-effectful controls are the caller's to add."""
    steps, seen = [], set()
    for url in state.links:
        u = str(url).strip()
        if not u or u in seen:
            continue
        seen.add(u)
        steps.append(treesearch.browser_step("navigate", cost_tokens=per_link_tokens,
                                              url=u))
        if len(steps) >= _MAX_CANDIDATE_LINKS:
            break
    return steps


def explore(start: PageState, *, navigate: NavigateFn, perceive: PerceiveFn,
            score: ScoreFn, caps: treesearch.SearchCaps | None = None,
            goal_score: float = _GOAL_SCORE) -> treesearch.SearchResult:
    """Plan a read-only path from `start` toward the highest-scoring page. Returns
    a `treesearch.SearchResult`: the best path (a sequence of navigations), the
    best page found, and any side-effectful steps withheld for approval."""
    if not isinstance(start, PageState):
        raise TypeError("start must be a PageState")
    problem = WebProblem(navigate=navigate, perceive=perceive, score=score,
                         goal_score=goal_score)
    return treesearch.search(problem, start,
                             caps or treesearch.SearchCaps.from_env(),
                             classify=treesearch.classify_browser)


def path_urls(result: treesearch.SearchResult) -> list[str]:
    """The URLs the winning plan would navigate through, in order."""
    return [(s.args or {}).get("url", "") for s in result.path
            if getattr(s, "action", "") in ("navigate", "back", "forward")]


# --- live-browser adapter -------------------------------------------------

def plan_with_browser(browser: Any, goal: str, start_url: str, *,
                      score: ScoreFn, caps: treesearch.SearchCaps | None = None
                      ) -> treesearch.SearchResult:
    """Drive `explore` with a live `browser.Browser`. Read-only: it navigates and
    reads the accessibility tree / page text to perceive; it NEVER clicks or types
    (those steps are withheld for approval). `score(PageState) -> float` is the
    caller's goal-relevance heuristic (e.g. a cheap model or keyword scorer)."""
    def navigate(url: str) -> PageState | None:
        try:
            browser.open(url)                 # the read-only navigation verb
            return _perceive_live(browser, url)
        except Exception as err:
            from . import errors
            errors.capture("webplan.navigate", err, context=str(url)[:120])
            return None

    def perceive(state: PageState) -> list:
        return links_to_steps(state)

    start = navigate(start_url)
    if start is None:
        return treesearch.SearchResult(
            treesearch.EXHAUSTED, [], None, float("-inf"), [],
            {"error": "start navigation failed"})
    return explore(start, navigate=navigate, perceive=perceive, score=score,
                   caps=caps)


# JS to collect same-origin anchor hrefs (absolute, deduped, capped) as newline
# text — read-only perception, no DOM mutation.
_LINKS_JS = (
    "(function(){var o=location.origin,s=new Set(),a=document.querySelectorAll("
    "'a[href]');for(var i=0;i<a.length&&s.size<40;i++){try{var u=new URL("
    "a[i].href,location.href);if(u.origin===o){u.hash='';s.add(u.href);}}"
    "catch(e){}}return Array.from(s).join('\\n');})()")


def _perceive_live(browser: Any, url: str) -> PageState:
    """Read a live page into a PageState using only read-only browser verbs
    (`open` already navigated; here we only read text, title, and anchor hrefs)."""
    def _safe_eval(expr: str) -> str:
        try:
            return str(browser._eval(expr) or "")
        except Exception:
            return ""
    title = _safe_eval("document.title")[:200]
    try:
        text = str(browser.read())[:2000]
    except Exception:
        text = ""
    raw = _safe_eval(_LINKS_JS)
    links = tuple(u for u in (raw.split("\n") if raw else [])
                  if u.strip())[: _MAX_CANDIDATE_LINKS * 3]
    return PageState(url=url, title=title, text=text, links=links)
