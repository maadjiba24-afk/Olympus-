"""Tests for operational metrics, /healthz, and /api/metrics."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from olympus import metrics, config, store


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    store.reset()
    metrics.reset()
    yield
    store.reset()


# --- the collector -------------------------------------------------------

def test_counts_requests_and_errors():
    metrics.record_response("/api/chat", 200)
    metrics.record_response("/api/chat", 200)
    metrics.record_response("/api/memory", 400)
    metrics.record_response("/api/chat", 500)
    snap = metrics.snapshot()
    assert snap["requests"] == 4
    assert snap["client_errors"] == 1 and snap["server_errors"] == 1
    assert snap["by_path"]["/api/chat"] == 3


def test_snapshot_has_uptime_and_spend():
    snap = metrics.snapshot()
    assert snap["uptime_seconds"] >= 0
    assert "budget" in snap and "spend_today" in snap


def test_path_table_bounded():
    for i in range(200):
        metrics.record_response(f"/p{i}", 200)
    assert len(metrics.snapshot()["by_path"]) <= 64
    assert metrics.snapshot()["requests"] == 200      # totals still accurate


# --- web endpoints -------------------------------------------------------

@pytest.fixture()
def server(monkeypatch):
    from olympus import web
    monkeypatch.setattr(web, "_SESSIONS", {})
    monkeypatch.setattr(web, "_HITS", {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_healthz_is_unauthenticated(server, monkeypatch):
    monkeypatch.setenv("OLYMPUS_ACCESS_TOKEN", "secret")   # even when gated
    d = json.loads(urllib.request.urlopen(server + "/healthz").read())
    assert d["status"] == "ok" and "uptime_seconds" in d


def test_metrics_endpoint_requires_token(server, monkeypatch):
    monkeypatch.setenv("OLYMPUS_ACCESS_TOKEN", "secret")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(server + "/api/metrics")
    assert e.value.code == 401


def test_metrics_endpoint_reports(server):
    d = json.loads(urllib.request.urlopen(server + "/api/metrics").read())
    assert "requests" in d and "uptime_seconds" in d and "by_path" in d
