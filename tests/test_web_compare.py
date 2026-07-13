"""Phase 3 — blind compare over the web UI: the panel, GET (models + tally),
POST run (blind answers, no mapping leaked), and POST reveal (mapping + pick).
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import backend, compare, config, web


def _settings(model):
    return config.Settings(provider="anthropic", model=model, api_key="k")


@pytest.fixture()
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    ms = [_settings("claude-a"), _settings("gpt-b")]
    monkeypatch.setattr(compare, "available_models", lambda: ms)
    monkeypatch.setattr(compare.random, "shuffle", lambda seq: None)
    monkeypatch.setattr(
        backend, "complete_text_once",
        lambda s, system, messages, effort="high": f"answer from {s.model}")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _get(url, path):
    return json.loads(urllib.request.urlopen(url + path).read())


def _post(url, path, payload):
    req = urllib.request.Request(
        url + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req).read())


def test_page_has_compare_surface(server):
    html = urllib.request.urlopen(server + "/").read().decode()
    assert "⚖ compare" in html and "/api/compare" in html
    assert "renderCompare" in html


def test_get_lists_models_and_tally(server):
    out = _get(server, "/api/compare?session=s1")
    assert out["models"] == ["anthropic/claude-a", "anthropic/gpt-b"]
    assert out["tally"] == {}


def test_run_returns_blind_answers_no_mapping(server):
    out = _post(server, "/api/compare",
                {"session": "s1", "op": "run", "prompt": "hi"})
    assert [a["label"] for a in out["answers"]] == ["A", "B"]
    assert "mapping" not in out
    assert out["id"]


def test_run_requires_prompt(server):
    try:
        _post(server, "/api/compare", {"session": "s1", "op": "run",
                                       "prompt": ""})
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_reveal_maps_and_records_pick(server):
    run = _post(server, "/api/compare",
                {"session": "s1", "op": "run", "prompt": "hi"})
    rev = _post(server, "/api/compare",
                {"session": "s1", "op": "reveal", "id": run["id"],
                 "choice": "A"})
    assert rev["mapping"]["A"] == "anthropic/claude-a"
    assert rev["choice"] == "A"
    assert rev["chosen_model"] == "anthropic/claude-a"
    # tally now reflects the pick on the next GET
    assert _get(server, "/api/compare?session=s1")["tally"] == {
        "anthropic/claude-a": 1}


def test_reveal_unknown_id_is_404(server):
    try:
        _post(server, "/api/compare",
              {"session": "s1", "op": "reveal", "id": "nope", "choice": "A"})
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_single_model_get_returns_setup_snippet(server, monkeypatch):
    monkeypatch.setattr(compare, "available_models",
                        lambda: [_settings("only-one")])
    out = _get(server, "/api/compare?session=s1")
    assert out["models"] == ["anthropic/only-one"]
    assert out["snippet"].startswith("OLYMPUS_MODELS=")


def test_two_models_get_omits_snippet(server):
    out = _get(server, "/api/compare?session=s1")
    assert "snippet" not in out           # pool is complete; no nudge needed
