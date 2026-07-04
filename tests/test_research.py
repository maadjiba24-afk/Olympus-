"""Deep Research: plan → iterative search/read/extract → cited synthesis →
claim-check. All model and network calls are stubbed; what's under test is
the loop's behavior — stopping when done, wrapping fetched pages as
untrusted, deduplicating queries/sources, citing sources, and never
laundering verification flags.
"""

import pytest

from olympus import backend, config, research

POOL = config.ModelPool.of(
    config.Settings(provider="openai", model="claude-opus-4-8",
                    api_key="k", base_url="http://localhost:1"))


class _Backend:
    """Scripted stand-in for backend.complete_json / complete_text."""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.extract_prompts = []
        self.synth_prompt = None

    def complete_json(self, settings, system, messages, schema, effort="high"):
        props = schema.get("properties", {})
        if "sub_questions" in props:
            return {"objective": "obj", "sub_questions": ["q1", "q2"]}
        if "searches" in props:
            return self.decisions.pop(0)
        if "relevant" in props:
            self.extract_prompts.append(messages[0]["content"])
            return {"relevant": True,
                    "findings": [{"claim": "fact from the page"}]}
        if "flags" in props:
            return {"flags": [{"claim": "shaky claim",
                               "note": "no source states this"}]}
        raise AssertionError(f"unexpected schema: {props.keys()}")

    def complete_text(self, settings, system, messages, effort="high"):
        self.synth_prompt = messages[0]["content"]
        return "Report body citing [1]."


@pytest.fixture()
def net(monkeypatch):
    calls = {"searches": [], "fetches": []}

    def fake_search(query):
        calls["searches"].append(query)
        return [{"title": "T", "url": f"https://ex.com/{len(calls['searches'])}",
                 "snippet": "s"},
                {"title": "Junk", "url": "https://pinterest.com/x",
                 "snippet": "s"}]

    def fake_fetch(url):
        calls["fetches"].append(url)
        return "PAGE TEXT about the topic"

    monkeypatch.setattr(research, "_search", fake_search)
    monkeypatch.setattr(research, "_fetch", fake_fetch)
    return calls


def _wire(monkeypatch, decisions):
    b = _Backend(decisions)
    monkeypatch.setattr(backend, "complete_json", b.complete_json)
    monkeypatch.setattr(backend, "complete_text", b.complete_text)
    return b


def test_report_carries_citations_sources_and_flags(monkeypatch, net):
    _wire(monkeypatch, [
        {"done": False, "searches": [{"query": "alpha", "goal": "g"}]},
        {"done": True, "searches": []},
    ])
    out = research.run("the question", pool=POOL)
    assert "Report body citing [1]." in out
    assert "## Sources" in out and "https://ex.com/1" in out
    assert "## Verification notes" in out and "shaky claim" in out


def test_stops_when_model_says_done(monkeypatch, net):
    _wire(monkeypatch, [
        {"done": False, "searches": [{"query": "alpha", "goal": "g"}]},
        {"done": True, "searches": []},
        {"done": False, "searches": [{"query": "never-run", "goal": "g"}]},
    ])
    research.run("q", pool=POOL, rounds=5)
    assert "never-run" not in net["searches"]


def test_round_budget_bounds_the_loop(monkeypatch, net):
    _wire(monkeypatch, [
        {"done": False, "searches": [{"query": f"q{i}", "goal": "g"}]}
        for i in range(10)
    ])
    research.run("q", pool=POOL, rounds=2)
    assert len(net["searches"]) == 2


def test_fetched_pages_are_wrapped_untrusted(monkeypatch, net):
    b = _wire(monkeypatch, [
        {"done": False, "searches": [{"query": "alpha", "goal": "g"}]},
        {"done": True, "searches": []},
    ])
    research.run("q", pool=POOL)
    assert b.extract_prompts, "extraction never ran"
    assert "<untrusted_external_content" in b.extract_prompts[0]
    assert "PAGE TEXT" in b.extract_prompts[0]


def test_low_quality_domains_are_never_fetched(monkeypatch, net):
    _wire(monkeypatch, [
        {"done": False, "searches": [{"query": "alpha", "goal": "g"}]},
        {"done": True, "searches": []},
    ])
    research.run("q", pool=POOL)
    assert all("pinterest" not in u for u in net["fetches"])


def test_repeated_queries_are_skipped(monkeypatch, net):
    _wire(monkeypatch, [
        {"done": False, "searches": [{"query": "same", "goal": "g"}]},
        {"done": False, "searches": [{"query": "SAME", "goal": "g"}]},
        {"done": True, "searches": []},
    ])
    research.run("q", pool=POOL, rounds=5)
    assert len(net["searches"]) == 1


def test_no_evidence_yields_honest_failure(monkeypatch):
    _wire(monkeypatch, [{"done": False,
                         "searches": [{"query": "a", "goal": "g"}]}])
    monkeypatch.setattr(research, "_search", lambda q: [])
    out = research.run("q", pool=POOL, rounds=1)
    assert "No usable evidence" in out


def test_cli_has_research_command():
    from olympus import cli
    parser = cli.build_parser()
    args = parser.parse_args(["research", "what", "is", "x", "--rounds", "2"])
    assert args.command == "research"
    assert args.question == ["what", "is", "x"] and args.rounds == 2
