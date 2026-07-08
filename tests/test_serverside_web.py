"""Server-side web search/fetch on Anthropic (egress-policy independent) and
Deep Research's routing to it. The Anthropic client is faked — no network."""

import types

import pytest

from olympus import config, llm, research

ANTHROPIC = config.Settings(provider="anthropic", model="claude-opus-4-8",
                            api_key="k")
OPENAI = config.Settings(provider="openai", model="m", api_key="k",
                         base_url="http://localhost:1")


def _blk(**kw):
    return types.SimpleNamespace(**kw)


def _fake_client(response):
    class _Msgs:
        def create(self, **kw):
            self.kw = kw
            return response
    client = types.SimpleNamespace(messages=_Msgs())
    return client


# --- server_web_search -----------------------------------------------------

def test_server_web_search_parses_result_blocks(monkeypatch):
    resp = _blk(content=[
        _blk(type="text", text="searching"),
        _blk(type="web_search_tool_result", content=[
            _blk(url="https://a.example", title="A", page_age="2026"),
            _blk(url="https://b.example", title="B", page_age=""),
            _blk(url=None, title="skip"),
        ]),
    ])
    monkeypatch.setattr(llm, "client", lambda s: _fake_client(resp))
    out = llm.server_web_search(ANTHROPIC, "query")
    assert [r["url"] for r in out] == ["https://a.example", "https://b.example"]
    assert out[0]["title"] == "A"


def test_server_web_search_noop_on_non_anthropic():
    assert llm.server_web_search(OPENAI, "q") == []


def test_server_web_search_swallows_errors(monkeypatch):
    def boom(s):
        raise RuntimeError("down")
    monkeypatch.setattr(llm, "client", boom)
    assert llm.server_web_search(ANTHROPIC, "q") == []


def test_server_web_search_respects_max_results(monkeypatch):
    items = [_blk(url=f"https://x{i}.example", title=str(i), page_age="")
             for i in range(20)]
    resp = _blk(content=[_blk(type="web_search_tool_result", content=items)])
    monkeypatch.setattr(llm, "client", lambda s: _fake_client(resp))
    assert len(llm.server_web_search(ANTHROPIC, "q", max_results=5)) == 5


# --- server_web_fetch ------------------------------------------------------

def test_server_web_fetch_extracts_document_text(monkeypatch):
    doc = _blk(type="web_fetch_tool_result",
               content=_blk(content=[_blk(type="text",
                                          text="the real page body " * 10)]))
    resp = _blk(content=[doc])
    monkeypatch.setattr(llm, "client", lambda s: _fake_client(resp))
    out = llm.server_web_fetch(ANTHROPIC, "https://x.example")
    assert "the real page body" in out


def test_server_web_fetch_falls_back_to_model_text(monkeypatch):
    resp = _blk(content=[_blk(type="text", text="summarized page content here")])
    monkeypatch.setattr(llm, "client", lambda s: _fake_client(resp))
    out = llm.server_web_fetch(ANTHROPIC, "https://x.example")
    assert "summarized page content" in out


def test_server_web_fetch_noop_on_non_anthropic():
    assert llm.server_web_fetch(OPENAI, "https://x.example") == ""


# --- research routing ------------------------------------------------------

def test_run_activates_server_web_when_pool_has_anthropic(monkeypatch):
    seen = {}

    def fake_run(*a, **k):
        seen["active"] = research._active_web_settings
        return "report"

    monkeypatch.setattr(research, "_run", fake_run)
    pool = config.ModelPool.of(ANTHROPIC, OPENAI)
    research.run("q", pool=pool)
    assert seen["active"] is not None and seen["active"].provider == "anthropic"
    assert research._active_web_settings is None      # restored after run


def test_run_no_server_web_for_local_only_pool(monkeypatch):
    seen = {}
    monkeypatch.setattr(research, "_run",
                        lambda *a, **k: seen.__setitem__("active",
                                                         research._active_web_settings))
    research.run("q", pool=config.ModelPool.of(OPENAI))
    assert seen["active"] is None


def test_search_seam_prefers_server_side(monkeypatch):
    monkeypatch.setattr(research, "_active_web_settings", ANTHROPIC)
    monkeypatch.setattr(llm, "server_web_search",
                        lambda s, q, **k: [{"title": "T", "url": "https://s.example",
                                            "snippet": ""}])
    assert research._search("q")[0]["url"] == "https://s.example"


def test_search_seam_falls_back_when_server_empty(monkeypatch):
    monkeypatch.setattr(research, "_active_web_settings", ANTHROPIC)
    monkeypatch.setattr(llm, "server_web_search", lambda s, q, **k: [])
    monkeypatch.setattr("olympus.websearch.results",
                        lambda q, **k: [{"title": "C", "url": "https://c.example",
                                         "snippet": ""}])
    assert research._search("q")[0]["url"] == "https://c.example"
