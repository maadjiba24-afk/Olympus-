"""OpenTelemetry (OTLP) export of the signed decision log — opt-in, dependency-
free, and content-safe.

Olympus already records a signed, tamper-evident decision log per run
(`trace.py`). This exports its STRUCTURE to any OTLP/HTTP collector so a run
shows up in standard observability tooling (Jaeger, Tempo, Honeycomb, an
OpenTelemetry Collector, …) as a trace: a root span per run, a child span per
decision.

Two deliberate properties:

  * **Dependency-free.** OTLP has a JSON-over-HTTP encoding; this emits it with
    the standard library (`urllib`) — no `opentelemetry` package required. An
    operator who prefers the official SDK can point their own pipeline at the
    same data; nothing here pulls a new dependency into the core.
  * **Content-safe.** Only the reasoning *shape* is exported — run/decision
    ids, decision type, agent name/role/key, outcome status, cost, timing,
    model name, and the content-addressed HASHES already in the log. The
    verbatim rationale, the user input (`meta`), event text, and error strings
    are NEVER exported — they can contain user data or model output over
    untrusted content, which must not leave for a third-party backend. The
    signing seed is never anywhere near a trace record and is never exported.

Off by default: export runs only when `OLYMPUS_OTLP_ENDPOINT` is set. It is
best-effort — an export failure is logged/degraded, never allowed to break a
run — and is skipped under replay (an export is a side effect, not part of the
byte-identical decision path).
"""

from __future__ import annotations

import json
import os
import time

# The ONLY decision fields that leave the host — structure + hashes, no content.
_SAFE_DECISION_ATTRS = (
    "decision_type", "model_request_hash", "model_response_ref", "inputs_hash")


def endpoint() -> str | None:
    ep = (os.environ.get("OLYMPUS_OTLP_ENDPOINT") or "").strip()
    return ep or None


def enabled() -> bool:
    return endpoint() is not None


def _service() -> str:
    return os.environ.get("OLYMPUS_OTLP_SERVICE", "olympus").strip() or "olympus"


def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _decision_span(rec: dict, trace_id: str, parent_id: str, span_id: str,
                   start_ns: int, base_ns: int) -> dict:
    agent = rec.get("agent") if isinstance(rec.get("agent"), dict) else {}
    outcome = rec.get("outcome") if isinstance(rec.get("outcome"), dict) else {}
    # SAFE attributes only — never rationale / inputs / error text.
    attrs = [
        _attr("olympus.agent.name", str(agent.get("name", ""))),
        _attr("olympus.agent.role", str(agent.get("role", ""))),
        _attr("olympus.agent.key", str(agent.get("key", ""))),
        _attr("olympus.outcome.status", str(outcome.get("status", ""))),
        _attr("olympus.outcome.has_error", bool(outcome.get("error"))),
        _attr("olympus.cost", float(rec.get("cost", 0) or 0)),
    ]
    for k in _SAFE_DECISION_ATTRS:
        if rec.get(k) is not None:
            attrs.append(_attr(f"olympus.{k}", str(rec.get(k))))
    dur_ns = max(0, int(rec.get("duration_ms", 0) or 0)) * 1_000_000
    status_code = 2 if outcome.get("status") not in ("ok", "hold", "redact") else 1
    return {
        "traceId": trace_id, "spanId": span_id, "parentSpanId": parent_id,
        "name": str(rec.get("decision_type", "decision")),
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(start_ns + dur_ns),
        "attributes": attrs,
        "status": {"code": status_code},
    }


def build_payload(record: dict, *, now_ns: int | None = None) -> dict:
    """Build the OTLP/HTTP-JSON resourceSpans payload for one flushed run record.
    Pure and content-safe. `now_ns` is the run's end time (defaults to now)."""
    now_ns = int(now_ns if now_ns is not None else time.time_ns())
    total_ns = int(float(record.get("total_secs", 0) or 0) * 1e9)
    start_ns = now_ns - total_ns
    run_id = str(record.get("id", ""))
    trace_id = (run_id * 2)[:32].ljust(32, "0")     # 16-byte hex trace id
    root_id = run_id[:16].ljust(16, "0")            # 8-byte hex span id

    decisions = record.get("decisions") if isinstance(
        record.get("decisions"), list) else []
    root = {
        "traceId": trace_id, "spanId": root_id,
        "name": f"olympus.run:{record.get('kind', 'run')}",
        "kind": 1,
        "startTimeUnixNano": str(start_ns), "endTimeUnixNano": str(now_ns),
        "attributes": [
            _attr("olympus.run.id", run_id),
            _attr("olympus.run.kind", str(record.get("kind", ""))),
            _attr("olympus.run.decisions", len(decisions)),
            _attr("olympus.run.signed", bool(record.get("log_signature"))),
        ],
        "status": {"code": 1},
    }
    spans = [root]
    for i, d in enumerate(decisions):
        if not isinstance(d, dict):
            continue
        rid = str(d.get("record_id", "")) or f"{run_id}-{i}"
        span_id = rid[:16].ljust(16, "0")
        d_start = start_ns + int(float(d.get("ts", 0) or 0) * 1e9)
        spans.append(_decision_span(d, trace_id, root_id, span_id,
                                    d_start, start_ns))
    return {
        "resourceSpans": [{
            "resource": {"attributes": [_attr("service.name", _service())]},
            "scopeSpans": [{
                "scope": {"name": "olympus"},
                "spans": spans,
            }],
        }],
    }


def _post(url: str, payload: dict, timeout: float = 5.0) -> int:
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    extra = (os.environ.get("OLYMPUS_OTLP_HEADERS") or "").strip()
    for pair in extra.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            headers[k.strip()] = v.strip()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def export_run(record: dict, *, poster=None, now_ns: int | None = None) -> bool:
    """Export one run record to the configured OTLP endpoint. No-op (returns
    False) when disabled or under replay. Best-effort: an export failure is
    captured/degraded, never raised into the run. `poster` is injectable."""
    if not enabled() or not isinstance(record, dict):
        return False
    try:
        from . import replaystore
        if replaystore.replaying():
            return False
    except Exception:
        pass
    url = endpoint().rstrip("/")
    if not url.endswith("/v1/traces"):
        url = url + "/v1/traces"
    try:
        # Funnel through the SAME sovereign egress choke every other outbound
        # call uses (llm / tools / browser / a2a): a no-op when sovereign mode
        # is off (a local OTLP collector on loopback is unaffected), but under
        # sovereign mode a telemetry export to a NON-allowlisted host is
        # refused — run metadata (agents, timing, costs, hashes) must not leak
        # past the allowlist just because it is "structure". Enforced here, at
        # the export decision, so even a custom `poster` is gated.
        from urllib.parse import urlparse
        from . import security
        security.assert_egress_allowed(urlparse(url).hostname or "")
        payload = build_payload(record, now_ns=now_ns)
        send = poster or _post
        send(url, payload)
        return True
    except Exception as err:
        from . import errors, evolve
        errors.capture("otel.export", err)
        try:
            evolve.record("otel", evolve.DEGRADED, f"OTLP export failed: {err}")
        except Exception:
            pass
        return False
