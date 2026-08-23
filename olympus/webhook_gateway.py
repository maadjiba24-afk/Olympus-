"""Inbound webhook gateway — a generic HTTP entry point into Olympus.

Any system that can POST JSON can talk to the council: a form, a cron on another
host, a Zapier/n8n step, a custom app. It POSTs ``{"text": "..."}`` and gets
back ``{"reply": "..."}`` — routed through the SAME shared gateway pipeline
(per-user memory, slash commands, verified answers) as every messaging
platform.  The caller never selects that memory namespace: the operator binds
the endpoint to one owner with ``OLYMPUS_WEBHOOK_USER``.

Auth: set OLYMPUS_WEBHOOK_SECRET and callers must send it in the
``X-Olympus-Secret`` header.  Both that secret and ``OLYMPUS_WEBHOOK_USER`` are
required; the server refuses to start if either is absent.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import gateway

_MAX_BODY = 100_000

# Per-caller sliding-window rate limit. A webhook is a public entry point that
# runs the FULL council on the operator's key — without a limit, a leaked (or
# unset) secret is a key-burn DoS. Keyed by client IP; window is 60s.
_HITS: dict[str, deque] = {}
_HITS_LOCK = threading.Lock()


def _rate_limit() -> int:
    try:
        return int(os.environ.get("OLYMPUS_WEBHOOK_RATE_LIMIT", "20"))
    except ValueError:
        return 20


def _rate_limited(key: str, limit: int | None = None) -> bool:
    limit = _rate_limit() if limit is None else limit
    if limit <= 0:
        return False
    now = time.time()
    with _HITS_LOCK:
        if len(_HITS) > 5000:            # bound the limiter's own memory
            _HITS.clear()
        hits = _HITS.setdefault(key, deque())
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= limit:
            return True
        hits.append(now)
    return False


def _secret_ok(supplied: str) -> bool:
    """Whether a request may drive the council. FAIL CLOSED when unconfigured.

    This used to return True when `OLYMPUS_WEBHOOK_SECRET` was unset, which —
    combined with a default bind of 0.0.0.0 — meant `olympus webhook` with no
    further configuration handed any peer on the network an unauthenticated,
    unmetered channel into the full council on the operator's API key. An
    optional credential that defaults to "no credential required" is not an
    optional credential; a2a_server, mcp_server and federation all refuse to
    serve without their token, and this surface is no less sensitive."""
    want = os.environ.get("OLYMPUS_WEBHOOK_SECRET", "")
    if not want:
        return False
    import hmac
    return hmac.compare_digest(want, supplied or "")


def _configured_user() -> str:
    """The server-owned tenant bound to the one configured webhook secret."""
    return os.environ.get("OLYMPUS_WEBHOOK_USER", "").strip()


def configured() -> bool:
    """Whether the inbound webhook has both halves of its identity binding."""
    return bool(os.environ.get("OLYMPUS_WEBHOOK_SECRET") and _configured_user())


def handle_payload(bots: dict, payload: dict, *, owner: str) -> dict:
    """Core: turn a {text} payload into a {reply} dict for trusted ``owner``.

    ``owner`` is supplied by server configuration, never by the request.  A
    public ``user`` field is rejected instead of ignored so an integration
    cannot mistakenly believe it selected a tenant when it did not.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if "user" in payload:
        raise ValueError("caller-supplied 'user' is forbidden; configure "
                         "OLYMPUS_WEBHOOK_USER on the server")
    owner = str(owner or "").strip()
    if not owner:
        raise ValueError("webhook owner is not configured")
    text = (payload.get("text") or "").strip()
    if not text:
        raise ValueError("missing 'text'")
    chunks = gateway.reply_for(bots, owner, text, prefix="hook")
    return {"reply": "\n\n".join(chunks)}


def _make_handler(bots: dict, owner: str):
    class _Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 (stdlib naming)
            # Header only. The old `?secret=` fallback put the credential in
            # the request line, where it lands in access logs, proxy logs and
            # anything else that records a URL.
            supplied = self.headers.get("X-Olympus-Secret", "")
            if not _secret_ok(supplied):
                return self._send(401, {"error": "unauthorized"})
            client = self.client_address[0] if self.client_address else "?"
            if _rate_limited(client):
                return self._send(429, {"error": "rate limit exceeded — "
                                        "slow down"})
            try:
                length = min(int(self.headers.get("Content-Length", 0)), _MAX_BODY)
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = handle_payload(bots, payload, owner=owner)
            except ValueError as err:
                return self._send(400, {"error": str(err)})
            except Exception as err:  # never leak a stack trace
                return self._send(500, {"error": str(err)[:200]})
            self._send(200, result)

        def log_message(self, *a):   # keep stdout quiet
            pass

    return _Handler


def run_server(host: str = "127.0.0.1", port: int = 8487) -> None:
    """Serve the generic webhook endpoint.

    Binds loopback by DEFAULT (was 0.0.0.0) and refuses to start at all without
    a secret and server-owned user: an endpoint that runs the council on the
    operator's key is not something to expose by omission. Exposing it requires
    setting both bindings and passing an explicit --host."""
    bots: dict = {}
    if not os.environ.get("OLYMPUS_WEBHOOK_SECRET"):
        raise SystemExit(
            "refusing to start: OLYMPUS_WEBHOOK_SECRET is not set.\n"
            "This endpoint runs the full council on your API key, so it has no "
            "safe unauthenticated mode. Generate one with:\n"
            "    export OLYMPUS_WEBHOOK_SECRET=$(python3 -c "
            "'import secrets; print(secrets.token_urlsafe(32))')\n"
            "and send it in the X-Olympus-Secret header.")
    owner = _configured_user()
    if not owner:
        raise SystemExit(
            "refusing to start: OLYMPUS_WEBHOOK_USER is not set.\n"
            "The shared webhook secret must map to one server-owned memory "
            "namespace; request bodies are not allowed to choose a user.")
    print(f"⚡ Olympus webhook gateway on {host}:{port}  (auth: on)")
    print('   POST {"text":"..."}  →  {"reply":"..."}')
    HTTPServer((host, port), _make_handler(bots, owner)).serve_forever()
