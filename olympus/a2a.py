"""A2A — agent-to-agent interop: a discovery agent card + a governed task client.

Two capabilities, both interoperability layers over what Olympus already has:

  * **Agent card** — an A2A-style discovery document
    (`/.well-known/agent.json`-shaped) describing Olympus's identity and
    capability surface, built purely from the live capabilities manifest.
  * **Task client** — inbound: map an A2A `task` envelope onto the existing
    council pipeline; outbound: call another A2A agent — but **governed**:
    every outbound URL passes the existing SSRF/egress guard first, the whole
    outbound path is opt-in (`OLYMPUS_A2A`, off by default), and any response is
    treated as untrusted external content (capability separation).

No DID/verifiable-credential stack and no new dependency: the card is JSON, the
outbound fetch uses stdlib `urllib`, and the fetcher is injectable so the core is
testable without a network. The building/parsing core is pure.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

PROTOCOL = "a2a/0.1"
_OUT_TIMEOUT = 15
_MAX_RESPONSE = 200_000


def outbound_enabled() -> bool:
    """Whether Olympus may CALL another A2A agent (default OFF). Serving our own
    card is always allowed — it is just public metadata; reaching out is not."""
    return os.environ.get("OLYMPUS_A2A", "").strip().lower() in (
        "1", "on", "true", "yes")


# --- agent card (pure) ----------------------------------------------------

def build_card(manifest: dict, *, base_url: str = "", version: str = "") -> dict:
    """The A2A discovery document, built from a capabilities manifest. Pure."""
    m = manifest or {}
    base = str(base_url or "").rstrip("/")
    caps = {
        "agents": (m.get("agents") or {}).get("count", 0),
        "tools": (m.get("tools") or {}).get("count", 0),
        "actions": (m.get("actions") or {}).get("count", 0),
        "commands": (m.get("commands") or {}).get("count", 0),
    }
    skills = list((m.get("agents") or {}).get("names", []))[:32]
    return {
        "protocol": PROTOCOL,
        "name": "Olympus Council",
        "description": "A council of specialist agents with DAG orchestration, "
                       "governed tools, and a signed decision log.",
        "version": str(version or ""),
        "url": base,
        "capabilities": caps,
        "skills": skills,
        "endpoints": {
            "card": f"{base}/.well-known/agent.json" if base else
                    "/.well-known/agent.json",
            "task": f"{base}/a2a/task" if base else "/a2a/task",
        },
        "authentication": {"schemes": ["bearer"]},
    }


def card(base_url: str = "") -> dict:
    """The live agent card for this instance."""
    from . import capabilities
    try:
        from . import __version__ as ver
    except Exception:
        ver = ""
    return build_card(capabilities.manifest(), base_url=base_url, version=ver)


# --- inbound task mapping (pure) ------------------------------------------

@dataclass(frozen=True)
class Task:
    id: str
    text: str
    role: str = "user"


class A2AError(ValueError):
    """A malformed A2A task envelope."""


def parse_task(payload: dict) -> Task:
    """Validate + normalize an inbound A2A task envelope into a Task. Accepts the
    message/parts shape ({message:{role, parts:[{type:text, text}]}}) and a
    simplified {id, input} shape. Raises A2AError on anything unusable."""
    if not isinstance(payload, dict):
        raise A2AError("task must be a JSON object")
    tid = str(payload.get("id") or payload.get("taskId") or "").strip()
    text = ""
    role = "user"
    msg = payload.get("message")
    if isinstance(msg, dict):
        role = str(msg.get("role", "user"))
        parts = msg.get("parts")
        if isinstance(parts, list):
            chunks = [str(p.get("text", "")) for p in parts
                      if isinstance(p, dict) and p.get("type") in (None, "text")]
            text = "\n".join(c for c in chunks if c)
    if not text:
        text = str(payload.get("input") or payload.get("text") or "").strip()
    if not text:
        raise A2AError("task carries no text to act on")
    return Task(id=tid or "task", text=text[:100_000], role=role)


def to_internal_request(task: Task) -> str:
    """Map an inbound A2A Task to the string Olympus's pipeline consumes. The
    task text is another agent's message — untrusted — so it is wrapped as data
    before it can reach the council (capability separation at the boundary)."""
    from . import security
    return security.wrap_untrusted(task.text, source="a2a-peer")


def task_response(task: Task, answer: str) -> dict:
    """The A2A task-result envelope for a completed task."""
    return {
        "protocol": PROTOCOL,
        "id": task.id,
        "status": "completed",
        "message": {"role": "agent",
                    "parts": [{"type": "text", "text": str(answer)}]},
    }


# --- outbound task client (governed) --------------------------------------

FetchFn = "callable"        # (url, data, headers, timeout) -> (status, bytes)


def _default_fetch(url: str, data: bytes | None, headers: dict, timeout: int):
    # Route through the SAME hardened opener the rest of the codebase uses
    # (tools._pinned_opener): it dials the SSRF-validated IP itself
    # (DNS-rebinding defense — no second resolution to flip after `_guard`'s
    # check) and re-runs `url_block_reason` on EVERY 3xx hop, so a peer at an
    # allowed URL cannot 302-redirect us to loopback / cloud-metadata / an
    # internal host. A one-shot `urllib.urlopen` (the old code) does neither.
    # Proxied egress uses the proxy opener (the proxy is the control point).
    import urllib.request as _r
    from . import tools
    req = _r.Request(url, data=data, headers=headers,
                     method="POST" if data else "GET")
    opener = tools._proxy_opener() if tools._proxied(url) else tools._pinned_opener()
    with opener.open(req, timeout=timeout) as resp:
        return resp.status, resp.read(_MAX_RESPONSE + 1)


def _guard(url: str) -> None:
    """Refuse an outbound A2A call that is disabled or fails the egress guard."""
    if not outbound_enabled():
        raise A2AError("outbound A2A is disabled (set OLYMPUS_A2A=1)")
    from . import security
    reason = security.url_block_reason(str(url))
    if reason:
        raise A2AError(f"egress refused: {reason}")


def fetch_card(url: str, *, fetcher=None, timeout: int = _OUT_TIMEOUT) -> dict:
    """Fetch and parse another agent's discovery card. Egress-gated; the parsed
    card is metadata (not executed)."""
    _guard(url)
    fetch = fetcher or _default_fetch
    status, body = fetch(url, None, {"Accept": "application/json"}, timeout)
    if int(status) >= 400:
        raise A2AError(f"peer returned HTTP {status}")
    try:
        doc = json.loads(bytes(body)[:_MAX_RESPONSE].decode("utf-8", "replace"))
    except (ValueError, json.JSONDecodeError) as err:
        raise A2AError(f"peer card is not valid JSON: {err}")
    if not isinstance(doc, dict):
        raise A2AError("peer card is not a JSON object")
    return doc


def call_agent(task_url: str, message: str, *, fetcher=None,
               timeout: int = _OUT_TIMEOUT) -> str:
    """Send a task to another A2A agent and return its answer AS UNTRUSTED DATA.
    Egress-gated and opt-in. The peer's reply is wrapped in the untrusted
    envelope — a remote agent's output can never be trusted as instructions."""
    _guard(task_url)
    fetch = fetcher or _default_fetch
    envelope = {
        "protocol": PROTOCOL,
        "message": {"role": "user",
                    "parts": [{"type": "text", "text": str(message)}]},
    }
    data = json.dumps(envelope).encode("utf-8")
    status, body = fetch(task_url, data,
                         {"Content-Type": "application/json"}, timeout)
    from . import security
    if int(status) >= 400:
        return security.wrap_untrusted(f"[peer returned HTTP {status}]",
                                       source="a2a-peer")
    text = bytes(body)[:_MAX_RESPONSE].decode("utf-8", "replace")
    answer = _extract_answer(text)
    return security.wrap_untrusted(answer, source="a2a-peer")


def _extract_answer(raw: str) -> str:
    """Pull the text answer out of a peer's task-result envelope, tolerating a
    plain-text reply."""
    try:
        doc = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return raw[:_MAX_RESPONSE]
    if isinstance(doc, dict):
        msg = doc.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("parts"), list):
            parts = [str(p.get("text", "")) for p in msg["parts"]
                     if isinstance(p, dict)]
            joined = "\n".join(p for p in parts if p)
            if joined:
                return joined
    return raw[:_MAX_RESPONSE]
