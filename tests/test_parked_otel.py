"""Parked-item 3 — OpenTelemetry (OTLP) export of the signed decision log.

Opt-in, dependency-free (stdlib OTLP/HTTP-JSON), and content-safe: only the
reasoning STRUCTURE + content-addressed hashes leave the host — never the
verbatim rationale, the user input, event text, or error strings.
"""

import json

import pytest

from olympus import otel


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("OLYMPUS_OTLP_ENDPOINT", "OLYMPUS_OTLP_SERVICE",
              "OLYMPUS_OTLP_HEADERS", "OLYMPUS_REPLAY"):
        monkeypatch.delenv(k, raising=False)


# A run record with SENSITIVE content in every place it can hide.
_SECRET_INPUT = "my password is hunter2 and my SSN is 123-45-6789"
_SECRET_RATIONALE = "the user's bank balance is $9,999 at Chase"
_SECRET_ERROR = "failed on card 4111-1111-1111-1111"


def _record():
    return {
        "id": "run123abc", "kind": "chat", "user": "alice",
        "total_secs": 2.5,
        "meta": {"input": _SECRET_INPUT},          # the raw user message
        "events": [{"stage": "verify.failed", "error": _SECRET_ERROR}],
        "decisions": [
            {"record_id": "dec0001", "decision_type": "route",
             "agent": {"name": "Zeus", "role": "router", "key": "zeus"},
             "rationale": {"note": _SECRET_RATIONALE},   # model output — sensitive
             "inputs_hash": "abc123", "model_request_hash": "req456",
             "outcome": {"status": "ok", "error": ""},
             "cost": 0.01, "duration_ms": 1200, "ts": 0.1},
            {"record_id": "dec0002", "decision_type": "contract",
             "agent": {"name": "Plutus", "role": "specialist", "key": "plutus"},
             "rationale": {"violations": [_SECRET_ERROR]},
             "outcome": {"status": "violation", "error": _SECRET_ERROR},
             "cost": 0.0, "duration_ms": 300, "ts": 1.5},
        ],
        "log_signature": "deadbeef",
    }


# --- content safety: the crux ----------------------------------------------

def test_no_sensitive_content_in_the_payload():
    blob = json.dumps(otel.build_payload(_record(), now_ns=10**18))
    for secret in (_SECRET_INPUT, _SECRET_RATIONALE, _SECRET_ERROR,
                   "hunter2", "123-45-6789", "4111-1111-1111-1111",
                   "9,999", "Chase"):
        assert secret not in blob, f"{secret!r} leaked into the OTLP payload"


def test_only_structure_and_hashes_are_exported():
    payload = otel.build_payload(_record(), now_ns=10**18)
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    # root + one span per decision
    assert len(spans) == 3
    attr_keys = {a["key"] for s in spans for a in s["attributes"]}
    # Hashes + structure are present...
    assert "olympus.inputs_hash" in attr_keys
    assert "olympus.model_request_hash" in attr_keys
    assert "olympus.agent.key" in attr_keys
    assert "olympus.outcome.status" in attr_keys
    # ...error is only a BOOLEAN, never the text.
    has_error = [a for s in spans for a in s["attributes"]
                 if a["key"] == "olympus.outcome.has_error"]
    assert has_error and all("boolValue" in a["value"] for a in has_error)
    # No attribute anywhere carries a rationale/input/error string value.
    for s in spans:
        for a in s["attributes"]:
            val = a["value"].get("stringValue", "")
            assert "hunter2" not in val and "Chase" not in val


def test_span_structure_and_status_mapping():
    payload = otel.build_payload(_record(), now_ns=10**18)
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root = spans[0]
    assert root["name"] == "olympus.run:chat"
    assert root["parentSpanId"] == "" if "parentSpanId" in root else True
    # decisions are children of the root
    assert all(s["parentSpanId"] == root["spanId"] for s in spans[1:])
    # violation → ERROR status (2), ok → OK (1)
    statuses = {s["name"]: s["status"]["code"] for s in spans[1:]}
    assert statuses["route"] == 1 and statuses["contract"] == 2
    # resource carries the service name
    res_attrs = payload["resourceSpans"][0]["resource"]["attributes"]
    assert any(a["key"] == "service.name" for a in res_attrs)


# --- enablement + export ---------------------------------------------------

def test_disabled_by_default_is_a_noop():
    assert not otel.enabled()
    assert otel.export_run(_record()) is False


def test_export_posts_to_v1_traces(monkeypatch):
    monkeypatch.setenv("OLYMPUS_OTLP_ENDPOINT", "http://collector:4318")
    captured = {}

    def poster(url, payload, timeout=5.0):
        captured["url"] = url
        captured["payload"] = payload
        return 200

    assert otel.export_run(_record(), poster=poster) is True
    assert captured["url"] == "http://collector:4318/v1/traces"
    assert captured["payload"]["resourceSpans"]


def test_endpoint_already_ending_in_v1_traces_not_doubled(monkeypatch):
    monkeypatch.setenv("OLYMPUS_OTLP_ENDPOINT", "http://c:4318/v1/traces")
    urls = []
    otel.export_run(_record(), poster=lambda u, p, timeout=5.0: urls.append(u))
    assert urls == ["http://c:4318/v1/traces"]


def test_export_skipped_under_replay(monkeypatch):
    monkeypatch.setenv("OLYMPUS_OTLP_ENDPOINT", "http://c:4318")
    monkeypatch.setenv("OLYMPUS_REPLAY", "run123abc")
    called = []
    assert otel.export_run(_record(),
                           poster=lambda *a, **k: called.append(1)) is False
    assert not called


def test_export_failure_is_best_effort(monkeypatch):
    monkeypatch.setenv("OLYMPUS_OTLP_ENDPOINT", "http://c:4318")

    def boom(url, payload, timeout=5.0):
        raise ConnectionError("collector down")

    assert otel.export_run(_record(), poster=boom) is False   # no raise


def test_custom_headers_are_parsed(monkeypatch):
    monkeypatch.setenv("OLYMPUS_OTLP_ENDPOINT", "http://c:4318")
    monkeypatch.setenv("OLYMPUS_OTLP_HEADERS", "x-api-key=secret,x-tenant=acme")
    # Exercise the real _post header assembly via a stub urlopen.
    seen = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=5.0):
        seen["headers"] = req.headers
        return _Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert otel.export_run(_record()) is True
    # urllib capitalizes header keys
    assert seen["headers"].get("X-api-key") == "secret"
    assert seen["headers"].get("X-tenant") == "acme"


def test_malformed_decisions_do_not_crash():
    rec = {"id": "r", "kind": "chat", "total_secs": 1.0,
           "decisions": ["not-a-dict", 5, {"decision_type": "x"}]}
    payload = otel.build_payload(rec, now_ns=10**18)
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 2                         # root + the one valid dict
