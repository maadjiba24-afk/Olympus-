"""Gap 5 — /api/health over the web UI: the structured runtime report, behind
the normal authenticated-principal gate like the other /api endpoints.
"""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import config, web


@pytest.fixture()
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_health_endpoint_reports_components(server):
    out = json.loads(urllib.request.urlopen(server + "/api/health?session=s1").read())
    assert "ok" in out and "components" in out
    assert "models" in out["components"] and "memory" in out["components"]


def test_health_ok_flag_is_bool(server):
    out = json.loads(urllib.request.urlopen(server + "/api/health?session=s1").read())
    assert isinstance(out["ok"], bool)
