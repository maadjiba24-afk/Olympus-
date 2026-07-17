"""Browser-exploration planner: best-first read-only navigation, side effects
withheld for approval, everything bounded — all driven by fakes (no browser)."""

import re

import pytest

from olympus import webplan, treesearch as ts


# A tiny fake site: url -> (text, links). One page is the goal.
_SITE = {
    "/": ("home", ["/a", "/b", "/pricing"]),
    "/a": ("about us", ["/a/team"]),
    "/a/team": ("the team", []),
    "/b": ("blog", ["/b/1"]),
    "/b/1": ("a post", []),
    "/pricing": ("pricing plans: $10 and $20 per month", ["/pricing/faq"]),
    "/pricing/faq": ("pricing faq", []),
}


def _navigate(url):
    if url not in _SITE:
        return None
    text, links = _SITE[url]
    return webplan.PageState(url=url, text=text, links=tuple(links))


def _perceive(state):
    return webplan.links_to_steps(state)


def _score_for(goal_words):
    def score(state):
        words = set(re.findall(r"[a-z0-9]+", state.text.lower()))
        hits = len(words & goal_words)
        return min(1.0, hits / max(1, len(goal_words)))
    return score


def test_finds_the_goal_page_read_only():
    # goal: the pricing page (its text contains 'pricing' and 'plans')
    result = webplan.explore(
        _navigate("/"), navigate=_navigate, perceive=_perceive,
        score=_score_for({"pricing", "plans"}),
        caps=ts.SearchCaps(max_nodes=50, max_depth=6), goal_score=0.9)
    assert result.reached_goal
    assert result.best_state.url == "/pricing"
    assert webplan.path_urls(result)[-1] == "/pricing"


def test_never_takes_side_effectful_steps():
    # a perceiver that also surfaces a 'buy' button (side-effectful) each page
    def perceive_with_button(state):
        steps = webplan.links_to_steps(state)
        steps.append(ts.browser_step("click", selector="#buy"))
        return steps

    result = webplan.explore(
        _navigate("/"), navigate=_navigate, perceive=perceive_with_button,
        score=_score_for({"pricing", "plans"}),
        caps=ts.SearchCaps(max_nodes=50))
    # the click steps were withheld, never applied (navigate reached the goal)
    assert result.pending_approval
    assert all(p["step"]["action"] == "click" for p in result.pending_approval)


def test_to_approvals_prepares_withheld_steps(monkeypatch):
    from olympus import actions
    monkeypatch.setattr(actions, "_REGISTRY", {})
    ran = {"n": 0}
    actions.register(actions.ActionType(
        name="wp_click", risk_class=actions.IRREVERSIBLE, scope="web",
        preview=lambda p: "click",
        execute=lambda p: ran.__setitem__("n", ran["n"] + 1)))

    def perceive_with_button(state):
        steps = webplan.links_to_steps(state)
        steps.append(ts.browser_step("click", selector="#buy"))
        return steps

    result = webplan.explore(
        _navigate("/"), navigate=_navigate, perceive=perceive_with_button,
        score=_score_for({"nonexistent"}), caps=ts.SearchCaps(max_nodes=30))
    prepared = ts.to_approvals(
        result, "u", lambda e: {"type": "wp_click", "payload": {}})
    assert prepared and all(a.status == actions.PREPARED for a in prepared)
    assert ran["n"] == 0            # prepared, never executed


def test_bounded_by_node_cap():
    # unreachable goal → must stop at the node cap, not crawl forever
    result = webplan.explore(
        _navigate("/"), navigate=_navigate, perceive=_perceive,
        score=_score_for({"unreachable"}),
        caps=ts.SearchCaps(max_nodes=3, max_depth=100))
    assert result.status in (ts.NODE_CAP, ts.EXHAUSTED)
    assert result.stats["nodes"] <= 3


def test_failed_navigation_skips_branch():
    def flaky_navigate(url):
        if url == "/b":
            return None            # simulate a dead link
        return _navigate(url)

    result = webplan.explore(
        _navigate("/"), navigate=flaky_navigate, perceive=_perceive,
        score=_score_for({"pricing", "plans"}), caps=ts.SearchCaps(max_nodes=50),
        goal_score=0.9)
    assert result.reached_goal          # still reaches goal via a live branch


def test_explore_rejects_non_pagestate():
    with pytest.raises(TypeError):
        webplan.explore("/", navigate=_navigate, perceive=_perceive,
                        score=_score_for(set()))


def test_links_to_steps_dedups_and_bounds():
    state = webplan.PageState(url="/", links=tuple(["/x"] * 3 + [f"/{i}"
                                                    for i in range(20)]))
    steps = webplan.links_to_steps(state)
    assert len(steps) <= webplan._MAX_CANDIDATE_LINKS
    assert all(s.effect == ts.REVERSIBLE for s in steps)   # navigations
    urls = [s.args["url"] for s in steps]
    assert len(urls) == len(set(urls))                     # deduped


# --- live-browser adapter over a fake Browser -----------------------------

class _FakeBrowser:
    """Mirrors the real Browser API the adapter uses: open() + _eval() + read()."""

    def __init__(self):
        self.at = "/"
        self.clicks = 0

    def open(self, url):
        if url not in _SITE:
            raise ValueError(f"no such page {url!r}")   # a real dead link errors
        self.at = url

    def read(self, selector=""):
        return _SITE.get(self.at, ("", []))[0]

    def _eval(self, expr):
        if "document.title" in expr:
            return f"title:{self.at}"
        # the links JS → newline-joined same-site hrefs for the current page
        return "\n".join(_SITE.get(self.at, ("", []))[1])

    def click(self, *a, **k):
        self.clicks += 1           # must never be called by the planner


def test_plan_with_browser_is_read_only():
    b = _FakeBrowser()
    result = webplan.plan_with_browser(
        b, goal="pricing", start_url="/",
        score=_score_for({"pricing", "plans"}),
        caps=ts.SearchCaps(max_nodes=40, max_depth=6))
    assert result.best_state.url == "/pricing"
    assert b.clicks == 0           # the planner never clicked anything


def test_plan_with_browser_handles_bad_start():
    b = _FakeBrowser()
    result = webplan.plan_with_browser(
        b, goal="x", start_url="/missing", score=_score_for(set()))
    assert result.stats.get("error")
