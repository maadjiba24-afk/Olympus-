"""A2A inbound server — let another agent hand Olympus a task, safely.

`a2a.py` already builds the discovery card and the OUTBOUND task client. The
missing half (DEFERRED #12) is the INBOUND server: an HTTP surface a remote A2A
agent can POST a task to. This module is that surface — and it is deliberately a
**text-task funnel, never a tool surface**:

  * the only side-effecting route delivers the task TEXT to the council (`ask`),
    exactly like `federation`'s `/federation/task`; the council then routes any
    actuation through its own approval spine, so A2A never becomes a direct
    actuation channel;
  * inbound text is wrapped UNTRUSTED (`a2a.to_internal_request`) before it can
    reach a model — a peer's message is data, never instructions;
  * the task route is bearer-authenticated **fail-closed** (no token configured →
    every task refused), mirroring `mcp_server`'s discipline; only the public
    agent card is served without auth (it is just metadata);
  * serving is opt-in (`OLYMPUS_A2A_SERVER`, off by default) and refuses to start
    without a token.

The dispatch core (`handle_request`) is PURE and dependency-injected (the council
`ask` is a parameter), so it is unit-testable with no sockets and no live
pipeline — the same shape as `federation.handle_request`.
"""

from __future__ import annotations

import hmac
import json
import os

from . import a2a

_MAX_INBOUND = 1_000_000           # hard cap on an inbound request body


class A2AServerError(RuntimeError):
    """The server is exposed but not safely authenticated (fail-closed)."""


def inbound_enabled() -> bool:
    """Whether Olympus SERVES the A2A task endpoint (default OFF). Serving the
    card is always allowed — it is public metadata; accepting tasks is not."""
    return os.environ.get("OLYMPUS_A2A_SERVER", "").strip().lower() in (
        "1", "on", "true", "yes")


def _configured_token() -> str | None:
    """The expected bearer token: OLYMPUS_A2A_TOKEN_FILE (preferred — the token
    isn't in the process env) or OLYMPUS_A2A_TOKEN. A set-but-unreadable/empty
    token file is a hard error, never a silent 'no auth'."""
    path = (os.environ.get("OLYMPUS_A2A_TOKEN_FILE") or "").strip()
    if path:
        try:
            tok = open(path, encoding="utf-8").read().strip()
        except OSError as err:
            raise A2AServerError(
                f"OLYMPUS_A2A_TOKEN_FILE {path!r} is unreadable: {err}")
        if not tok:
            raise A2AServerError(f"OLYMPUS_A2A_TOKEN_FILE {path!r} is empty")
        return tok
    tok = (os.environ.get("OLYMPUS_A2A_TOKEN") or "").strip()
    return tok or None


def _auth_ok(headers: dict) -> bool:
    """Constant-time bearer check. Fail-closed: serving with no token configured
    — or a transiently-unreadable token file — refuses every task route (a clean
    401, never a 500 that could mask monitoring)."""
    try:
        want = _configured_token()
    except A2AServerError:
        return False
    if not want:
        return False
    got = ""
    for k, v in (headers or {}).items():
        if str(k).lower() == "authorization":
            got = str(v)
            break
    if got.lower().startswith("bearer "):
        got = got[7:]
    return hmac.compare_digest(got.strip().encode("utf-8"),
                               want.encode("utf-8"))


def handle_request(method: str, path: str, body: dict, headers: dict,
                   *, ask=None, base_url: str = "") -> tuple[int, dict]:
    """Route one inbound A2A request. Pure + injectable:

      GET  /.well-known/agent.json → the public discovery card (no auth)
      POST /a2a/task               → run a peer task on the council (auth required)

    `ask(text) -> str` is the injected council entry, so this is testable with no
    live pipeline. The task route fails closed without a configured token."""
    method = (method or "GET").upper()
    path = (path or "").rstrip("/") or "/"

    if method == "GET" and path in ("/.well-known/agent.json", "/a2a/card"):
        return 200, a2a.card(base_url)

    if (method, path) != ("POST", "/a2a/task"):
        return 404, {"error": "unknown A2A route"}

    if not _auth_ok(headers):
        return 401, {"error": "A2A task auth required"}

    try:
        task = a2a.parse_task(body or {})
    except a2a.A2AError as err:
        return 400, {"error": str(err)}

    # The peer's text is UNTRUSTED — wrap before it can reach the council, which
    # then routes any actuation through its own approval spine.
    wrapped = a2a.to_internal_request(task)
    try:
        answer = ask(wrapped) if callable(ask) else ""
    except Exception as err:                     # never leak a stack over the wire
        return 500, {"error": f"task failed: {type(err).__name__}"}
    return 200, a2a.task_response(task, str(answer))


def _default_ask(message: str) -> str:          # pragma: no cover
    """Bridge to the real council pipeline (used by the live server), mirroring
    `mcp_server._default_ask`."""
    from . import config, orchestrator
    bot = orchestrator.Olympus(pool=config.ModelPool.from_env(),
                               user="a2a", conversation_id="a2a-default")
    return bot.ask(message)


def run_server(ask=None, *, host: str = "127.0.0.1", port: int = 8490
               ) -> None:                        # pragma: no cover
    """Minimal HTTP listener over `handle_request`. Refuses to start unless the
    A2A server is enabled AND a bearer token is configured (fail-closed). Bound
    to loopback by default — expose only behind a reverse proxy you control."""
    if not inbound_enabled():
        raise A2AServerError(
            "A2A server is disabled (set OLYMPUS_A2A_SERVER=1)")
    if not _configured_token():
        raise A2AServerError(
            "refusing to serve A2A tasks without OLYMPUS_A2A_TOKEN[_FILE]")
    ask = ask or _default_ask
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method):
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                length = 0
            if length > _MAX_INBOUND:            # reject oversized before reading
                self.send_response(413)
                self.end_headers()
                return
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw) if raw else {}
                if not isinstance(data, dict):
                    data = {}
            except (ValueError, RecursionError):
                data = {}
            hdrs = {k: v for k, v in self.headers.items()}
            try:
                status, payload = handle_request(method, self.path, data, hdrs,
                                                 ask=ask)
            except Exception as err:             # never crash the handler / leak
                status, payload = 500, {"error": f"internal: {type(err).__name__}"}
            blob = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *_):
            pass

    HTTPServer((host, port), _Handler).serve_forever()
