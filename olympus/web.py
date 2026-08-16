"""Olympus web interface — zero dependencies, pure standard library.

`python -m olympus web` then open http://localhost:8484.

- BYOK: the ⚙ panel lets each visitor pick provider/model/key (kept in their
  browser, used in-memory per request, never stored or logged).
- Each browser gets a private memory namespace and a conversation that
  persists across server restarts.
- Abuse protection: per-IP rate limit (OLYMPUS_RATE_LIMIT/min, default 8), an
  optional per-user daily cap (OLYMPUS_DAILY_CHATS), and an optional shared
  access token (OLYMPUS_ACCESS_TOKEN) for hosted instances.
- Cost protection: OLYMPUS_FREE_CHATS gives each visitor N free chats/day on the
  operator's key, then they continue on their own (BYOK as a free allowance);
  OLYMPUS_REQUIRE_BYOK makes it all-or-nothing instead.
- Error visibility: unexpected 500s are captured (errors.capture) and pushed to
  the operator over Telegram, so failures don't vanish into the logs.
- 👍/👎 on every answer feeds the daily learning cycle.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import (accounts, actions, builtin_actions, config, metrics,  # noqa: F401
               openai_server, orchestrator, streamguard, usage)


def _user_for(sid: str) -> str:
    return f"web-{sid}"


def _memory_view(user: str) -> dict:
    """Everything Olympus knows about a user, for the web knowledge panel:
    profile, memories, candidates, playbooks, relationship graph, and its
    outcome track-record + insights."""
    from . import usermem, profile, playbooks, relgraph, outcomes
    mems = [{"id": m["id"], "type": m["type"], "content": m["content"],
             "confidence": round(usermem.effective_confidence(m), 2)}
            for m in usermem.active_memories(user)]
    cands = [{"id": c["id"], "type": c["type"], "content": c["content"],
              "reason": c.get("reason", "")} for c in usermem.candidates(user)]
    prof = profile.get(user)
    pbs = [{"id": p["id"], "name": p["name"], "steps": p["steps"],
            "status": p["status"], "use_count": p["use_count"]}
           for p in playbooks.list_all(user)]
    graph = [{"label": n["label"], "kind": n["kind"],
              "connections": [phrase for phrase, _ in relgraph.neighbors(user, n["id"])]}
             for n in relgraph.nodes(user)]
    return {"memories": mems, "candidates": cands,
            "profile": {"about": prof.get("about", ""),
                        "facts": prof.get("facts", {})},
            "playbooks": pbs, "graph": graph,
            "outcomes": outcomes.stats(user)["overall"],
            "insights": outcomes.insights(user)}


def _agenda_view(user: str) -> dict:
    """The agenda panel: the user's scheduled tasks (always available, run by
    the heartbeat) plus their upcoming calendar events (only when a Google
    account is connected — read-only, no side effect). Calendar reads resolve
    the token from the memory user, so scope it to this principal for the read.
    """
    from . import scheduler
    import time as _time
    now = _time.time()
    jobs = []
    for j in scheduler.jobs():
        if j.user not in (user, "shared"):
            continue                      # only this user's (and shared) tasks
        if getattr(j, "kind", "interval") == "on_exit":
            when = f"when {j.label or 'pid ' + str(j.watch_pid)} exits"
            due_in = None
        else:
            when = f"every {scheduler._human_interval(j.interval)}"
            due_in = max(0, int(j.interval - (now - j.last_run)))
        jobs.append({"name": j.name, "when": when, "prompt": j.prompt,
                     "enabled": j.enabled, "kind": getattr(j, "kind",
                                                            "interval"),
                     "deliver_to": j.deliver_to, "due_in": due_in})
    events, connected = None, False
    try:
        from . import calendar as gcal, google_oauth, memory
        if gcal.configured() and google_oauth.connected(user):
            connected = True
            memory.set_user(user)
            events = gcal.upcoming()
    except Exception:
        events = None
    from . import todos as _todos
    open_items = _todos.listing(user, include_done=False)
    due_ids = {it["id"] for it in _todos.due_items(user, now)}
    todo_view = [{"id": it["id"], "text": it["text"], "kind": it["kind"],
                  "due": it.get("due"), "overdue": it["id"] in due_ids}
                 for it in open_items]
    return {"tasks": jobs, "events": events, "calendar_connected": connected,
            "todos": todo_view}


def _actions_view(user: str) -> list[dict]:
    """Pending actions (awaiting approval) plus recently executed reversible
    ones (so the UI can offer undo)."""
    out = []
    for a in actions.pending(user):
        # expose the editable fields (internal "_" keys stay server-side)
        editable = {k: v for k, v in a.payload.items() if not k.startswith("_")}
        out.append({"id": a.id, "title": a.title, "risk": a.risk_class,
                    "preview": a.preview, "status": a.status,
                    "reversible": a.reversible, "why": a.why,
                    "payload": editable})
    for a in actions.history(user, limit=8):
        if a.status == actions.EXECUTED and a.reversible:
            out.append({"id": a.id, "title": a.title, "risk": a.risk_class,
                        "preview": a.preview, "status": a.status,
                        "reversible": True, "why": a.why})
    return out


class _Session:
    _EVENTS_CAP = 500

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.lock = threading.Lock()
        self.events: list[str] = []
        self.events_base = 0        # count of events dropped off the front
        self.fingerprint: tuple | None = None
        self.bot: orchestrator.Olympus | None = None

    def report(self, msg: str) -> None:
        """Append a progress event, trimming the oldest so a long-lived session
        can't grow the buffer without bound. `events_base` keeps the global
        index stable for the /api/status `since` cursor across trims."""
        self.events.append(msg)
        overflow = len(self.events) - self._EVENTS_CAP
        if overflow > 0:
            del self.events[:overflow]
            self.events_base += overflow

    def bot_for(self, pool: config.ModelPool,
                user: str | None = None) -> orchestrator.Olympus:
        user = user or f"web-{self.sid}"
        fp = tuple((m.provider, m.model, m.api_key, m.base_url)
                   for m in pool.members) + (user,)
        if self.bot is None or fp != self.fingerprint:
            self.fingerprint = fp
            # ask_user on the web is async (request/response), so a specialist
            # can't block for a live answer — install a surface-and-proceed
            # provider that streams the question to the UI, then proceeds. The
            # orchestrator captures the provider at construction, so install it
            # first.
            from . import interaction
            interaction.set_provider(interaction.reporting_provider(self.report))
            self.bot = orchestrator.Olympus(
                report=self.report,
                pool=pool,
                user=user,
                conversation_id=user,
            )
        return self.bot


_SESSIONS: dict[str, _Session] = {}
_SESSIONS_LOCK = threading.Lock()

# per-IP sliding-window rate limiter
_HITS: dict[str, deque] = {}
_HITS_LOCK = threading.Lock()

_MAX_BODY = 1_000_000          # 1 MB cap on request bodies (DoS guard)
_SID_RE = re.compile(r"olympus_sid=([A-Za-z0-9_-]{8,128})")

# Bounds for `Handler._drain_refused_body` — the post-refusal socket cleanup
# that keeps an HTTP/1.0 close from turning a written 401 into a TCP reset.
# Both bounds are needed and neither may be removed:
#   * _REFUSAL_DRAIN_READ_SECONDS caps ONE recv, so a sender that stops dead
#     cannot park the thread;
#   * _REFUSAL_DRAIN_SECONDS caps the WHOLE drain, so a sender that dribbles a
#     byte every few milliseconds — never tripping the per-read timeout —
#     cannot park it either.
# The refusal is already on the wire before any of this runs, so these budgets
# buy a clean connection close and nothing else; exceeding them costs only the
# courtesy, never the response.
_REFUSAL_DRAIN_SECONDS = 0.5          # total wall-clock budget for one drain
_REFUSAL_DRAIN_READ_SECONDS = 0.1     # per-recv budget inside that total
_REFUSAL_DRAIN_CHUNK = 65536          # bytes per recv


def _resolve_sid(handler: BaseHTTPRequestHandler,
                 provided: str | None) -> tuple[str, str | None]:
    """Determine the session id, preferring a server-issued HttpOnly cookie so
    browser users get an unguessable, isolated session automatically (no shared
    'default' namespace, no manual session strings). Returns (sid, cookie).

    WITH ACCOUNTS ON, THE COOKIE IS THE ONLY ACCEPTED SOURCE (W2-1b). A
    client-supplied `session=` was previously honoured on every call site, and
    the id is the account's session token: it selects the memory namespace. The
    ids are uuid4 so they are not guessable, but a value in a query string is a
    BEARER TOKEN IN A URL — it leaks through server access logs, `Referer`
    headers to third parties, browser history, and anything that records a URL.
    Not-guessable is not the same as not-disclosed.

    WHICH CASE IS WHICH:

      * `accounts.require_login()` ON — per-account identity exists and a
        session id IS a credential. Cookie only; a supplied value is ignored,
        so pasting another user's id in a URL cannot select their namespace.
      * `accounts.require_login()` OFF — there are no accounts, so a session id
        is a conversation selector rather than a credential, and the escape
        hatch stays for CLI/programmatic callers. It is not unguarded:
        `_authorized` has already required OLYMPUS_ACCESS_TOKEN, or proved the
        peer is on-box when no token is configured. So the hatch only exists
        where a real credential (or loopback) was already required.
    """
    m = _SID_RE.search(handler.headers.get("Cookie", "") or "")
    if m:
        return m.group(1), None
    if provided and not accounts.require_login():
        return provided[:64], None
    sid = uuid.uuid4().hex                       # fresh, random, server-issued
    return sid, sid


def _session(sid: str) -> _Session:
    with _SESSIONS_LOCK:
        if sid not in _SESSIONS:
            # Bound growth by FIFO-evicting the OLDEST session(s), not by
            # clearing the whole dict — a blanket clear at the 501st visitor
            # would drop every active user's bot and event stream at once.
            while len(_SESSIONS) >= 500:
                _SESSIONS.pop(next(iter(_SESSIONS)))
            _SESSIONS[sid] = _Session(sid)
        return _SESSIONS[sid]


def _rate_limited(key: str, limit: int) -> bool:
    """Per-key sliding-window limiter (60s). Separate keys let the expensive
    chat endpoint and the cheap write endpoints have independent budgets."""
    if limit <= 0:
        return False
    now = time.time()
    with _HITS_LOCK:
        if len(_HITS) > 5000:          # bound the limiter's own memory
            _HITS.clear()
        hits = _HITS.setdefault(key, deque())
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= limit:
            return True
        hits.append(now)
    return False


def _chat_limit() -> int:
    return int(os.environ.get("OLYMPUS_RATE_LIMIT", "8"))


def _v1_limit() -> int:
    """Per-IP per-minute ceiling for the /v1 dialects (OLYMPUS_V1_RATE_LIMIT).

    Deliberately far looser than `_chat_limit()`: /v1 is a PROGRAMMATIC surface
    and real clients burst (the compatibility campaign alone issues ~40 requests
    in a few seconds, including an 8-way concurrent case), so the browser-chat
    budget of 8/min would refuse legitimate use. This is a DoS/abuse ceiling,
    not a cost control — spend is bounded by the budget guard. <= 0 disables it,
    matching the other limiters in this module."""
    try:
        return int(os.environ.get("OLYMPUS_V1_RATE_LIMIT", "120"))
    except ValueError:
        return 120


# per-user daily counter (cost/abuse cap, separate from the per-minute limiter)
_DAILY: dict[str, int] = {}
_DAILY_LOCK = threading.Lock()


def _today() -> str:
    return time.strftime("%Y%m%d", time.gmtime())


def _daily_reset_if_new_day() -> None:
    day = _today()
    if _DAILY and not any(k.startswith(day) for k in _DAILY):
        _DAILY.clear()                     # counters are per-UTC-day


def _daily_count(key: str) -> int:
    """How many times `key` has been used today (read-only)."""
    with _DAILY_LOCK:
        _daily_reset_if_new_day()
        return _DAILY.get(f"{_today()}:{key}", 0)


def _daily_bump(key: str) -> int:
    with _DAILY_LOCK:
        _daily_reset_if_new_day()
        k = f"{_today()}:{key}"
        _DAILY[k] = _DAILY.get(k, 0) + 1
        return _DAILY[k]


def _daily_limited(key: str, limit: int) -> bool:
    """True once `key` has hit its daily allowance (and counts this call)."""
    if limit <= 0:
        return False
    with _DAILY_LOCK:
        _daily_reset_if_new_day()
        k = f"{_today()}:{key}"
        if _DAILY.get(k, 0) >= limit:
            return True
        _DAILY[k] = _DAILY.get(k, 0) + 1
    return False


def _brought_own_key(pset: dict) -> bool:
    """Did this request supply the user's own PRIMARY credential (BYOK)?

    Only a primary `api_key` counts — it is the key the main pipeline actually
    runs on. A bare `base_url` (no key) falls back to the operator's env key,
    and an `extra` second-model key doesn't pay for the primary member; treating
    either as BYOK let a keyless visitor run unlimited chats on the operator's
    key while bypassing OLYMPUS_REQUIRE_BYOK / the free-chat allowance.
    """
    if not isinstance(pset, dict):
        return False
    return bool((pset.get("api_key") or "").strip())


def _key_decision(brought: bool, free_used: int) -> str:
    """Policy for a chat request. Returns:
      ""          allow (BYOK, or within the free operator-funded allowance)
      "over_free" the user spent their free allowance — must bring a key now
      "byok"      BYOK required outright (no free allowance configured)
    A free allowance (OLYMPUS_FREE_CHATS > 0) means BYOK is a *limit*, not a
    wall: keyless users get N free chats/day, then continue on their own key."""
    if brought:
        return ""
    free = config.free_chats()
    if free > 0:
        return "over_free" if free_used >= free else ""
    return "byok" if config.require_byok() else ""


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    """Gate the browser API (`/api/*`).

    With OLYMPUS_ACCESS_TOKEN set, the token IS the credential and the peer
    does not matter. With no token configured, this used to return True
    unconditionally — so `/api/chat` was the ONE surface with no loopback
    fallback. `/v1/*` and `/api/admin` both refuse an off-box caller when no
    credential is configured; `/api/chat` handed any peer that could reach the
    port an anonymous namespace and a funded council run (Stage-B finding F4).

    The safe default always held — `serve()` binds 127.0.0.1 — so reaching it
    took an explicit `--host 0.0.0.0`. That is exactly the case where an
    operator most needs the server to refuse rather than to infer safety: the
    remoteness decision comes from the kernel peer socket, never a header, and
    a process bound off-loopback must carry a credential even for a
    loopback-looking peer (a reverse proxy connects from loopback while
    fronting the world).

    `OLYMPUS_REQUIRE_LOGIN` is a separate, additional gate (per-account
    sessions); this one is about whether the surface is exposed at all, so the
    two do not substitute for each other."""
    required = os.environ.get("OLYMPUS_ACCESS_TOKEN")
    if required:
        provided = handler.headers.get("X-Olympus-Token", "")
        return hmac.compare_digest(provided, required)
    if accounts.require_login():
        return True             # per-account auth is the configured credential
    addr = getattr(handler, "client_address", None)
    peer = (addr[0] if addr else "") or ""
    if not _v1_allowed(peer, handler.headers):
        return False
    try:
        return _is_loopback(handler.server.server_address[0])
    except Exception:
        return False            # unknown peer or bind ⇒ treat as exposed


def _readiness() -> tuple[bool, dict]:
    """(ready, payload) for GET /readyz.

    Ready means: the declared deployment mode's configuration is complete, and
    durable state is actually writable. Both are probed, not assumed — a
    read-only volume is the classic failure that leaves a process happily alive
    while silently dropping every journal append."""
    from . import shadow
    info = config.build_info()
    # BOTH named modes, so the docstring's "the declared deployment mode" is
    # true rather than staging-only. Each is a no-op outside its own mode, so at
    # most one contributes and a dev instance is unaffected.
    problems = list(config.staging_problems()) + list(
        config.production_problems())
    writable = config._writable_dir(config.MEMORY_DIR)
    if not writable and not any("not writable" in p for p in problems):
        problems.append(f"memory dir {config.MEMORY_DIR} is not writable")
    payload = {
        "status": "ready" if not problems else "not_ready",
        "env": info["env"],
        "version": info["version"],
        "commit": info["commit"],
        "shadow_mode": shadow.enabled(),
        "memory_dir_writable": writable,
        "uptime_seconds": metrics.snapshot()["uptime_seconds"],
    }
    if problems:
        payload["problems"] = problems
    return (not problems), payload


def _unauthorized_message() -> str:
    """Why the browser API refused — the two reasons need different operator
    actions, and "missing or wrong access token" is wrong for the second."""
    if os.environ.get("OLYMPUS_ACCESS_TOKEN"):
        return "missing or wrong access token"
    return ("set OLYMPUS_ACCESS_TOKEN (or OLYMPUS_REQUIRE_LOGIN): with no "
            "credential configured the browser API is loopback-only — it is "
            "never served to an off-box caller or through a reverse proxy.")


def _https_request(handler: BaseHTTPRequestHandler) -> bool:
    """Whether the original client request arrived over HTTPS, so the session
    cookie can be marked Secure. Honors X-Forwarded-Proto from a TLS-terminating
    reverse proxy (Caddy/nginx) and an explicit OLYMPUS_SECURE_COOKIES toggle for
    operators who know they're always behind TLS."""
    if os.environ.get("OLYMPUS_SECURE_COOKIES", "").lower() in (
            "1", "true", "yes", "on"):
        return True
    proto = handler.headers.get("X-Forwarded-Proto", "")
    return proto.split(",")[0].strip().lower() == "https"


def _signing_posture() -> dict:
    """Verification posture for /api/status — lets a prospective buyer confirm
    the audit guarantee from the running server. Never exposes the seed itself.
    `posture` is 'production' (a secret signing seed is configured) or 'dev'
    (the public default key — integrity only); `pinned` is whether a trusted
    public key is pinned; `public_key` is the derived verifying key (public)."""
    from . import witness
    if not witness.available():
        return {"posture": "unavailable", "pinned": False, "public_key": None,
                "verify_hint": "cryptography backend unavailable — cannot sign "
                               "or verify on this instance."}
    pub = None
    try:
        pub = witness.public_key_hex()
    except Exception:
        pass
    return {
        "posture": witness.posture(),
        "pinned": bool(witness.pinned_pubkey()),
        "public_key": pub,
        "verify_hint": "Verify any answer's reasoning with: "
                       "`olympus verify --run <run_id>` (replays the decision "
                       "path AND checks the decision-log signature). The run id "
                       "is returned in the X-Olympus-Run-Id response header.",
    }


# --- /v1/* loopback boundary (security primitive, header-independent) --------
# The remoteness decision for the OpenAI-compatible endpoints is made from the
# kernel-reported peer address ONLY — never from a client-controllable header
# (Host / X-Forwarded-For / X-Real-IP / Forwarded), which would be spoofable and
# would turn the "loopback-only" guarantee into an open relay. This module-level
# predicate is the single source of truth for that decision.

# Header names that signal a reverse proxy is relaying a request. Their PRESENCE
# (not their value — values are attacker-controlled) means the loopback peer is
# a proxy fronting a real remote client; see _forwarding_headers_present.
_FORWARDING_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded",
                       "X-Forwarded-Host", "X-Forwarded-Proto")


def _is_loopback(ip: str) -> bool:
    """True iff `ip` (a peer address from self.client_address[0]) is loopback.

    Decided purely from the kernel-reported peer address. IPv4 loopback is the
    whole 127.0.0.0/8 block; IPv6 loopback is ::1; IPv4-mapped-IPv6 forms
    (::ffff:127.x) are unwrapped first so a mapped public address can't sneak
    through. No header is consulted."""
    ip = (ip or "").strip().lower()
    if ip.startswith("::ffff:"):          # IPv4-mapped IPv6 → compare the v4 part
        ip = ip[len("::ffff:"):]
    if ip in ("::1", "0:0:0:0:0:0:0:1"):
        return True
    return ip.startswith("127.")


def _forwarding_headers_present(headers) -> bool:
    """Whether any reverse-proxy forwarding header is present. A loopback peer
    that carries one of these is a proxy relaying an external client — so a
    loopback peer alone must NOT be treated as 'trusted local' when it appears.
    Only presence is checked; the (spoofable) value is never trusted."""
    if not headers:
        return False
    return any(headers.get(h) for h in _FORWARDING_HEADERS)


def _v1_allowed(peer_ip: str, headers) -> bool:
    """The /v1/* allow decision when OLYMPUS_API_KEYS is unset, as a pure
    function of the kernel peer address and the request headers' shape.

    This is the single predicate both the request handler (`_v1_authorized`)
    and the boundary tests consult. A no-key deployment only ever serves when
    the server is bound to loopback, so this assumes that binding and answers
    the two remaining dimensions:
      * the peer must be loopback (decided from client_address, never a header);
      * no reverse-proxy forwarding header may be present (its presence proves
        the loopback peer is a proxy relaying an off-box client — the Caddy
        trap), so we refuse and require a key instead.
    Header *values* are never trusted; only the *presence* of a forwarding
    header is used, and only to deny. Returns True iff the request may be served
    without a configured API key."""
    return _is_loopback(peer_ip) and not _forwarding_headers_present(headers)


# --- Anthropic Messages dialect (W3-C5) -------------------------------------
# Pure translation helpers for `POST /v1/messages`. They are the testable core;
# `Handler._handle_v1_messages` is a thin shell around them and reuses EVERY
# seam the OpenAI dialect uses (auth, admission, sovereignty, streamguard, usage
# accounting, cancellation). There is no second server and no second generation
# path — the Anthropic request is normalised into the SAME internal message list
# `openai_server.messages_to_prompt` already consumes, and the council's answer
# is rendered back in Anthropic shape (W3-I1).
#
# Refusal over silent divergence (W3-I2): a field Olympus cannot honour is a
# typed 400, never a quiet drop.

_ANTHROPIC_ERROR_KINDS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    529: "overloaded_error",
}


class _AnthropicRefusal(Exception):
    """A typed refusal from the Anthropic translation layer (W3-I2).

    Carries the HTTP status and the Anthropic error `type` so the handler can
    render it in the native envelope without re-deciding policy."""

    def __init__(self, message: str, *, status: int = 400,
                 kind: str = "invalid_request_error") -> None:
        self.status = int(status)
        self.kind = str(kind)
        super().__init__(str(message))


def _anthropic_error_kind(status: int) -> str:
    """The Anthropic error `type` for an HTTP status (5xx ⇒ api_error)."""
    if status in _ANTHROPIC_ERROR_KINDS:
        return _ANTHROPIC_ERROR_KINDS[status]
    return "api_error" if int(status) >= 500 else "invalid_request_error"


def _anthropic_error(kind: str, message: str) -> dict:
    """The native Anthropic error envelope (W3-I4) — deliberately NOT the
    OpenAI `{"error": {"message", "type", "code"}}` shape."""
    return {"type": "error",
            "error": {"type": str(kind), "message": str(message)}}


def _anthropic_text(blocks, *, where: str) -> str:
    """Flatten an Anthropic `system` value (string or block list) to text.

    Only `text` blocks exist for `system`; anything else is refused rather than
    dropped (W3-I2)."""
    if blocks is None:
        return ""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        raise _AnthropicRefusal(
            f"invalid request: {where} must be a string or a list of text "
            "blocks.")
    parts = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            raise _AnthropicRefusal(
                f"invalid request: {where} blocks must be objects.")
        if block.get("type") != "text":
            raise _AnthropicRefusal(
                f"unsupported content block type "
                f"{str(block.get('type'))!r} in {where}: this endpoint carries "
                "text (and tool blocks in `messages`) only. Refused rather "
                "than silently dropped.")
        parts.append(str(block.get("text", "")))
    return "\n".join(p for p in parts if p)


def _anthropic_blocks_to_internal(content, role: str) -> tuple[str, list, list]:
    """Split one Anthropic message `content` into (text, tool_calls, results).

    `tool_calls` are in the OpenAI internal shape the existing path already
    speaks (`{"id", "type": "function", "function": {"name", "arguments"}}`)
    and `results` are OpenAI `tool` messages — the SAME internal representation
    `openai_server.messages_to_prompt` consumes, so both dialects collapse to a
    prompt through one function. Any other block type is refused (W3-I2)."""
    if isinstance(content, str):
        return content, [], []
    if content is None:
        return "", [], []
    if not isinstance(content, list):
        raise _AnthropicRefusal(
            "invalid request: message `content` must be a string or a list of "
            "content blocks.")
    texts: list[str] = []
    calls: list[dict] = []
    results: list[dict] = []
    for block in content:
        if isinstance(block, str):
            texts.append(block)
            continue
        if not isinstance(block, dict):
            raise _AnthropicRefusal(
                "invalid request: content blocks must be objects.")
        kind = block.get("type")
        if kind == "text":
            texts.append(str(block.get("text", "")))
        elif kind == "tool_use":
            try:
                args = json.dumps(block.get("input") or {})
            except (TypeError, ValueError):
                raise _AnthropicRefusal(
                    "invalid request: tool_use `input` must be JSON-encodable.")
            calls.append({"id": str(block.get("id", "")),
                          "type": "function",
                          "function": {"name": str(block.get("name", "")),
                                       "arguments": args}})
        elif kind == "tool_result":
            results.append({
                "role": "tool",
                "tool_call_id": str(block.get("tool_use_id", "")),
                "content": _anthropic_result_text(block.get("content")),
            })
        else:
            raise _AnthropicRefusal(
                f"unsupported content block type {str(kind)!r} in a {role} "
                "message: this endpoint carries `text`, `tool_use` and "
                "`tool_result` blocks only (no image/document/thinking "
                "blocks). Refused rather than silently dropped.")
    return "\n".join(t for t in texts if t), calls, results


def _anthropic_result_text(content) -> str:
    """A tool_result's payload as text (string, block list, or JSON value)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                raise _AnthropicRefusal(
                    "unsupported tool_result content: only text blocks are "
                    "carried. Refused rather than silently dropped.")
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    try:
        return json.dumps(content)
    except (TypeError, ValueError):
        return str(content)


# Anthropic fields Olympus cannot honour. Silently ignoring them would change
# the meaning of the request, so they are refused with a typed 400 (W3-I2).
_ANTHROPIC_REFUSED_FIELDS = {
    "stop_sequences": "custom stop sequences are not applied by the council "
                      "pipeline",
    "top_k": "top-k sampling is not exposed by the council pipeline",
}


def _anthropic_to_internal(payload) -> tuple[list, str, dict]:
    """Normalise an Anthropic Messages request into (messages, system, opts).

    `messages` is the OpenAI-shaped internal list the existing `/v1` path
    already consumes (one generation path, two dialects — W3-I1); `system` is
    the flattened system prompt (string OR block list accepted, W3-I3); `opts`
    carries the honoured request knobs.

    Raises `_AnthropicRefusal` for a malformed body, an unsupported field
    (`stop_sequences`, `top_k`), an unsupported content block, or a missing
    `max_tokens` (required by the Anthropic protocol — W3-I3)."""
    if not isinstance(payload, dict):
        raise _AnthropicRefusal("invalid request: body must be a JSON object.")
    for field, why in _ANTHROPIC_REFUSED_FIELDS.items():
        value = payload.get(field)
        if field in payload and value not in (None, [], ()):
            raise _AnthropicRefusal(
                f"unsupported parameter `{field}`: {why}. Refused rather than "
                "silently ignored.")
    if "max_tokens" not in payload or payload.get("max_tokens") is None:
        raise _AnthropicRefusal(
            "invalid request: `max_tokens` is required by the Anthropic "
            "Messages protocol.")
    try:
        max_tokens = int(payload["max_tokens"])
    except (TypeError, ValueError, OverflowError):
        # OverflowError matters: json.loads accepts the non-standard literals
        # Infinity/-Infinity (and 1e400 overflows to inf), and int(float("inf"))
        # raises OverflowError — which is NOT a ValueError. Unhandled it escaped
        # the handler and dropped the connection with a logged traceback, i.e. a
        # trivially reachable pre-generation crash on a network-facing route
        # (Phase-4 Stage-B finding F1).
        raise _AnthropicRefusal(
            "invalid request: `max_tokens` must be an integer.")
    if max_tokens <= 0:
        raise _AnthropicRefusal(
            "invalid request: `max_tokens` must be a positive integer.")
    raw = payload.get("messages")
    if not isinstance(raw, list) or not raw:
        raise _AnthropicRefusal(
            "invalid request: `messages` must be a non-empty list.")
    system = _anthropic_text(payload.get("system"), where="`system`")
    messages: list[dict] = []
    for msg in raw:
        if not isinstance(msg, dict):
            raise _AnthropicRefusal(
                "invalid request: each message must be an object.")
        role = msg.get("role")
        if role not in ("user", "assistant"):
            raise _AnthropicRefusal(
                f"invalid request: unsupported message role {str(role)!r} "
                "(only `user` and `assistant` exist in this protocol).")
        text, calls, results = _anthropic_blocks_to_internal(
            msg.get("content"), role)
        # Tool results belong to the turn they answer, so they precede the
        # user's new text — the order the OpenAI dialect uses too.
        messages.extend(results)
        entry: dict = {"role": role, "content": text}
        if calls:
            entry["tool_calls"] = calls
        if text or calls or not results:
            messages.append(entry)
    opts = {
        "model": payload.get("model") or openai_server.MODEL_ID,
        "max_tokens": max_tokens,
        "stream": bool(payload.get("stream")),
    }
    for knob in ("temperature", "top_p", "tools", "tool_choice", "metadata"):
        if payload.get(knob) is not None:
            opts[knob] = payload[knob]
    return messages, system, opts


def _anthropic_usage(usage) -> dict:
    """Map a usage block onto Anthropic's `input_tokens`/`output_tokens`.

    Accepts the OpenAI-shaped block produced by `openai_server.usage_block`
    (the existing estimator seam — no second token counter) or an already
    Anthropic-shaped dict."""
    usage = usage or {}
    return {
        "input_tokens": int(usage.get("input_tokens",
                                      usage.get("prompt_tokens", 0)) or 0),
        "output_tokens": int(usage.get("output_tokens",
                                       usage.get("completion_tokens", 0)) or 0),
    }


def _anthropic_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def _internal_to_anthropic(reply: str, *, model: str, usage: dict | None = None,
                           stop_reason: str = "end_turn",
                           message_id: str | None = None) -> dict:
    """Render a council answer as an Anthropic `Message` object (W3-I3/§5)."""
    return {
        "id": message_id or _anthropic_message_id(),
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": reply or ""}],
        "model": model or openai_server.MODEL_ID,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _anthropic_usage(usage),
    }


def _anthropic_stop_reason(*, truncated: bool = False,
                           tool_use: bool = False) -> str:
    """Map the internal outcome onto Anthropic's stop reasons: a tool call ⇒
    `tool_use`, a length-limited answer ⇒ `max_tokens`, otherwise `end_turn`."""
    if tool_use:
        return "tool_use"
    if truncated:
        return "max_tokens"
    return "end_turn"


def _anthropic_cap(text: str, max_tokens: int) -> tuple[str, bool]:
    """Apply the caller's `max_tokens` budget to an answer.

    The council does not take a token cap, so the cap is enforced at this
    boundary using the SAME estimator the OpenAI surface reports with
    (`openai_server.estimate_tokens`, ~4 chars/token). Returns
    (text, truncated); a truncated answer is reported with
    `stop_reason="max_tokens"`, never as a complete `end_turn` reply."""
    text = text or ""
    budget = max(1, int(max_tokens)) * 4
    if len(text) > budget:
        return text[:budget], True
    return text, False


def _sse_event(name: str, data: dict) -> str:
    """One named SSE frame, in the framing style `_stream_v1` already writes."""
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


# A `ping` every N content deltas (the protocol's keepalive). Small enough that
# an idle-ish stream still emits one, large enough not to spam a fast stream.
_ANTHROPIC_PING_EVERY = 16


def _anthropic_sse(pieces, *, model: str, max_tokens: int,
                   input_tokens: int = 0, message_id: str | None = None,
                   tool_use: bool = False, ping_every: int | None = None):
    """Yield the Anthropic streaming event sequence for `pieces` (W3-I6).

    Order: `message_start` → `content_block_start` → `content_block_delta`*
    → `content_block_stop` → `message_delta` → `message_stop`, with periodic
    `ping` keepalives.

    `pieces` is expected to be the streamguard-wrapped generator
    (`Handler._guarded_pieces`), so a degenerate stream arrives here as one
    final disclosure fragment: it is emitted as an ordinary `text_delta` and
    the envelope is still closed properly — a disclosed abort, never a silent
    truncation (W2-I8.3)."""
    ping_every = ping_every or _ANTHROPIC_PING_EVERY
    mid = message_id or _anthropic_message_id()
    model = model or openai_server.MODEL_ID
    yield _sse_event("message_start", {
        "type": "message_start",
        "message": {"id": mid, "type": "message", "role": "assistant",
                    "content": [], "model": model, "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": int(input_tokens or 0),
                              "output_tokens": 0}},
    })
    yield _sse_event("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""}})
    yield _sse_event("ping", {"type": "ping"})
    budget = max(1, int(max_tokens)) * 4
    written: list[str] = []
    size = 0
    truncated = False
    seen = 0
    for piece in pieces:
        if not piece:
            continue
        if size + len(piece) > budget:
            piece = piece[:max(0, budget - size)]
            truncated = True
        if piece:
            written.append(piece)
            size += len(piece)
            seen += 1
            yield _sse_event("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": piece}})
            if seen % ping_every == 0:
                yield _sse_event("ping", {"type": "ping"})
        if truncated:
            break
    yield _sse_event("content_block_stop",
                     {"type": "content_block_stop", "index": 0})
    yield _sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": _anthropic_stop_reason(truncated=truncated,
                                                        tool_use=tool_use),
                  "stop_sequence": None},
        "usage": {"output_tokens": openai_server.estimate_tokens(
            "".join(written))}})
    yield _sse_event("message_stop", {"type": "message_stop"})


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>⚡ OLYMPUS</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: Georgia, 'Times New Roman', serif;
         background: #0e1116; color: #e8e3d8; display: flex;
         flex-direction: column; height: 100vh; }
  header { padding: 14px 20px; border-bottom: 1px solid #2a2f3a;
           display: flex; align-items: baseline; gap: 12px; }
  header h1 { margin: 0; font-size: 20px; letter-spacing: 4px; color: #d9b44a; }
  header span { color: #6b7280; font-size: 13px; font-style: italic; flex: 1; }
  #gear { background: none; border: 1px solid #2a2f3a; color: #d9b44a;
          border-radius: 8px; padding: 4px 12px; cursor: pointer; font: inherit; }
  #panel { display: none; border-bottom: 1px solid #2a2f3a; padding: 14px 20px;
           background: #11151d; }
  #panel.open { display: block; }
  #panel .row { display: flex; gap: 10px; flex-wrap: wrap; max-width: 860px;
                margin: 0 auto 8px; align-items: center; }
  #panel label { font-size: 13px; color: #9aa3b2; min-width: 70px; }
  #panel input, #panel select { background: #161b24; color: #e8e3d8;
        border: 1px solid #2a2f3a; border-radius: 6px; padding: 7px 10px;
        font: inherit; font-size: 14px; flex: 1; min-width: 180px; }
  #panel .hint { font-size: 12px; color: #6b7280; font-style: italic;
                 max-width: 860px; margin: 0 auto; }
  #log { flex: 1; overflow-y: auto; padding: 20px; max-width: 860px;
         width: 100%; margin: 0 auto; }
  .msg { margin: 0 0 6px; line-height: 1.55; white-space: pre-wrap;
         word-wrap: break-word; }
  .user { color: #9fb4d0; margin-bottom: 16px; }
  .user::before { content: "you ▸ "; color: #51607a; }
  .bot::before { content: "olympus ▸ "; color: #d9b44a; }
  .sys { color: #6b7280; font-size: 13px; font-style: italic; margin: 4px 0; }
  .rate { margin: 0 0 16px; }
  .rate button { background: none; border: 1px solid #2a2f3a; color: #6b7280;
                 border-radius: 6px; padding: 2px 10px; cursor: pointer;
                 font-size: 13px; margin-right: 6px; }
  .rate button:hover { border-color: #d9b44a; color: #d9b44a; }
  .rate button.done { border-color: #d9b44a; color: #d9b44a; cursor: default; }
  form { display: flex; gap: 10px; padding: 16px 20px; max-width: 860px;
         width: 100%; margin: 0 auto; }
  #attach { background: none; border: 1px solid #2a2f3a; color: #9aa3b2;
            border-radius: 8px; padding: 0 14px; cursor: pointer; font: inherit; }
  #attach.has { color: #d9b44a; border-color: #d9b44a; }
  input.q { flex: 1; background: #161b24; color: #e8e3d8; font: inherit;
            border: 1px solid #2a2f3a; border-radius: 8px; padding: 12px 14px; }
  input.q:focus { outline: none; border-color: #d9b44a; }
  button.send { background: #d9b44a; color: #0e1116; border: 0;
                border-radius: 8px; padding: 0 22px; font: inherit;
                font-weight: bold; cursor: pointer; }
  button.send:disabled { opacity: .4; cursor: wait; }
  #actbtn, #connect { background: none; border: 1px solid #2a2f3a; color: #d9b44a;
            border-radius: 8px; padding: 4px 12px; cursor: pointer; font: inherit; }
  #connect.done { color: #7bbf7b; border-color: #2f4a2f; }
  #actcount { color: #0e1116; background: #d9b44a; border-radius: 10px;
              padding: 0 7px; font-size: 12px; font-weight: bold; display: none; }
  #actcount.show { display: inline; }
  #actions { display: none; border-bottom: 1px solid #2a2f3a; background: #11151d;
             max-height: 50vh; overflow-y: auto; padding: 12px 20px; }
  #actions.open { display: block; }
  #memory { display: none; border-bottom: 1px solid #2a2f3a; background: #11151d;
            max-height: 50vh; overflow-y: auto; padding: 12px 20px; }
  #memory.open { display: block; }
  #memcount { color: #0e1116; background: #7bbf7b; border-radius: 10px;
              padding: 0 7px; font-size: 12px; font-weight: bold; display: none; }
  #memcount.show { display: inline; }
  #documents { display: none; border-bottom: 1px solid #2a2f3a;
               background: #11151d; max-height: 60vh; overflow-y: auto;
               padding: 12px 20px; }
  #documents.open { display: block; }
  .docwrap { max-width: 860px; margin: 0 auto; }
  .docitem { display: flex; gap: 10px; align-items: baseline;
             justify-content: space-between; border-bottom: 1px solid #20242e;
             padding: 6px 0; cursor: pointer; }
  .docitem:hover .dt { color: #d9b44a; }
  .docitem .dt { color: #c8cdd6; flex: 1; }
  .docitem .dm { color: #6b7280; font-size: 12px; }
  #doctitle { width: 100%; box-sizing: border-box; margin-bottom: 6px; }
  #docbody { width: 100%; box-sizing: border-box; min-height: 40vh;
             font-family: ui-monospace, monospace; font-size: 13px; }
  #gallery { display: none; border-bottom: 1px solid #2a2f3a;
             background: #11151d; max-height: 60vh; overflow-y: auto;
             padding: 12px 20px; }
  #gallery.open { display: block; }
  .galgrid { max-width: 860px; margin: 0 auto; display: grid;
             grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
             gap: 12px; }
  .galcard { border: 1px solid #20242e; border-radius: 6px; overflow: hidden;
             background: #0d1017; }
  .galcard img { width: 100%; height: 130px; object-fit: cover; display: block;
                 cursor: pointer; }
  .galcard .gm { padding: 5px 7px; font-size: 11px; color: #6b7280;
                 display: flex; justify-content: space-between; gap: 6px; }
  .galcard .gm b { color: #c8cdd6; font-weight: normal; overflow: hidden;
                   text-overflow: ellipsis; white-space: nowrap; }
  #agenda { display: none; border-bottom: 1px solid #2a2f3a;
            background: #11151d; max-height: 60vh; overflow-y: auto;
            padding: 12px 20px; }
  #agenda.open { display: block; }
  .agwrap { max-width: 860px; margin: 0 auto; }
  .agwrap h4 { margin: 12px 0 6px; color: #d9b44a; font-size: 13px;
               font-weight: 600; }
  .agrow { display: flex; gap: 10px; align-items: baseline;
           border-bottom: 1px solid #20242e; padding: 6px 0; }
  .agrow .at { color: #c8cdd6; flex: 1; }
  .agrow .aw { color: #6b7280; font-size: 12px; white-space: nowrap; }
  .agrow.off .at { color: #6b7280; text-decoration: line-through; }
  .agrow .ap { color: #8b93a0; font-size: 12px; display: block;
               margin-top: 2px; }
  #compare { display: none; border-bottom: 1px solid #2a2f3a;
             background: #11151d; max-height: 70vh; overflow-y: auto;
             padding: 12px 20px; }
  #compare.open { display: block; }
  .cmpwrap { max-width: 860px; margin: 0 auto; }
  .cmpbar { display: flex; gap: 8px; margin-bottom: 10px; }
  #cmpprompt { flex: 1; box-sizing: border-box; }
  .cmpcard { border: 1px solid #20242e; border-radius: 6px; margin-bottom: 10px;
             background: #0d1017; }
  .cmpcard .ch { display: flex; justify-content: space-between;
                 align-items: center; padding: 7px 10px;
                 border-bottom: 1px solid #20242e; }
  .cmpcard .ch b { color: #d9b44a; }
  .cmpcard .ch .who { color: #6b7280; font-size: 12px; }
  .cmpcard .cb { padding: 10px; white-space: pre-wrap; color: #c8cdd6;
                 font-size: 13px; }
  .cmpcard.picked { border-color: #4a7a3f; }
  .cmpcard.picked .ch b::after { content: '  ✓ your pick'; color: #7bbf6a;
                                 font-size: 12px; }
  #cmptally { color: #6b7280; font-size: 12px; margin-top: 6px; }
  .cmpsnippet { background: #0d1017; border: 1px solid #20242e; border-radius: 6px;
                padding: 8px 10px; font-family: ui-monospace, monospace;
                font-size: 12px; color: #c8cdd6; white-space: pre-wrap;
                word-break: break-all; overflow-x: auto; }
  .mem { max-width: 860px; margin: 0 auto 8px; display: flex; gap: 10px;
         align-items: baseline; justify-content: space-between;
         border-bottom: 1px solid #20242e; padding-bottom: 6px; }
  .mem .t { color: #6b7280; font-size: 12px; }
  .mem .c { color: #c8cdd6; flex: 1; }
  #budget { max-width: 860px; margin: 0 auto 10px; font-size: 13px; }
  #budget.ok { color: #6b7280; }
  #budget.over { color: #e0884a; border: 1px solid #4a392f; background: #1d1712;
                 border-radius: 8px; padding: 8px 12px; }
  #budget:empty { display: none; }
  .card { max-width: 860px; margin: 0 auto 12px; border: 1px solid #2a2f3a;
          border-radius: 10px; padding: 12px 14px; background: #161b24; }
  .card .top { display: flex; justify-content: space-between; align-items: baseline; }
  .card .ttl { color: #e8e3d8; font-weight: bold; }
  .card .risk { font-size: 12px; color: #6b7280; }
  .card .risk.irreversible_financial_legal, .card .risk.irreversible { color: #e0884a; }
  .card .why { color: #9aa3b2; font-size: 13px; font-style: italic; margin: 6px 0 0; }
  .card pre { white-space: pre-wrap; word-wrap: break-word; color: #c8cdd6;
              font: 13px/1.5 ui-monospace, monospace; margin: 8px 0; }
  .card .btns { display: flex; gap: 8px; }
  .card button { border: 0; border-radius: 7px; padding: 5px 14px; cursor: pointer;
                 font: inherit; font-size: 13px; }
  .card .ok { background: #d9b44a; color: #0e1116; font-weight: bold; }
  .card .no { background: none; border: 1px solid #2a2f3a; color: #9aa3b2; }
  .card .un { background: none; border: 1px solid #2a2f3a; color: #9aa3b2; }
  #auth { position: fixed; inset: 0; background: #0e1116; z-index: 50;
          display: flex; align-items: center; justify-content: center; }
  .authbox { width: 320px; max-width: 90vw; background: #161b24;
             border: 1px solid #2a2f3a; border-radius: 12px; padding: 24px; }
  .authbox h2 { margin: 0 0 12px; color: #d9b44a; }
  .authbox input { width: 100%; box-sizing: border-box; margin: 6px 0;
                   padding: 9px 11px; background: #0e1116; color: #e8e3d8;
                   border: 1px solid #2a2f3a; border-radius: 8px; }
  .authbtns { display: flex; gap: 8px; margin-top: 10px; }
  .authbtns button { flex: 1; border: 0; border-radius: 8px; padding: 9px;
                     cursor: pointer; font: inherit; }
  #welcome { max-width: 860px; margin: 0 auto; }
  #cost { margin: 10px 0 0; font-size: 13px; color: #d9b44a; }
  #cost:empty { display: none; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .chip { background: #161b24; border: 1px solid #2a2f3a; color: #c8cdd6;
          border-radius: 16px; padding: 6px 13px; cursor: pointer; font: inherit;
          font-size: 13px; text-align: left; }
  .chip:hover { border-color: #d9b44a; color: #e8e3d8; }
</style>
</head>
<body>
<script>window.OLYMPUS_CFG = __OLYMPUS_CFG__;</script>
<div id="auth" style="display:none">
  <div class="authbox">
    <h2>⚡ OLYMPUS</h2>
    <p class="sys" id="autherr"></p>
    <input id="authuser" placeholder="username" autocomplete="username">
    <input id="authpass" type="password" placeholder="password" autocomplete="current-password">
    <div class="authbtns">
      <button id="loginbtn" class="ok">Log in</button>
      <button id="registerbtn" class="no">Create account</button>
    </div>
  </div>
</div>
<header>
  <h1>OLYMPUS</h1>
  <span>main agent · supervisor · hallucination controller · __OLYMPUS_NSPEC__ specialists</span>
  <button id="connect" title="Connect your Google account" style="display:none">🔗 connect Google</button>
  <button id="actbtn" title="Actions awaiting your approval">📋 actions
    <span id="actcount"></span></button>
  <button id="membtn" title="What Olympus remembers about you">🧠 memory
    <span id="memcount"></span></button>
  <button id="docbtn" title="Your documents workspace">📄 docs</button>
  <button id="galbtn" title="Images generated into the workspace">🖼 gallery</button>
  <button id="agbtn" title="Your scheduled tasks and upcoming calendar">📅 agenda</button>
  <button id="cmpbtn" title="Blind compare across your configured models">⚖ compare</button>
  <button id="gear" title="Bring your own model & key">⚙ model</button>
  <button id="reportbtn" title="Report a problem to the operator">📣 report</button>
</header>
<div id="reportbox" style="display:none;padding:12px 20px;border-bottom:1px solid #2a2f3a">
  <textarea id="reporttext" rows="3" placeholder="Describe the problem you hit — what you did and what went wrong..." style="width:100%;box-sizing:border-box"></textarea>
  <input id="reportcontact" placeholder="how to reach you (optional — email/handle)" style="margin-top:6px">
  <div style="display:flex;gap:8px;margin-top:6px">
    <button id="reportsend" class="ok">Send report</button>
    <button id="reportcancel" class="no">Cancel</button>
  </div>
  <p class="sys" id="reportmsg"></p>
</div>
<div id="actions"><div id="budget"></div><div id="cards"></div></div>
<div id="memory"></div>
<div id="documents"></div>
<div id="gallery"></div>
<div id="agenda"></div>
<div id="compare"></div>
<div id="panel">
  <p class="hint" style="margin-top:0"><b>Bring your own model &amp; key.</b>
  1) pick a provider · 2) type the model name · 3) paste your API key from that
  provider's dashboard, then send. Your key stays in this browser and is sent
  only with your own requests. Leave blank to use the server's model.</p>
  <div class="row">
    <label>Provider</label>
    <select id="provider">
      <option value="">server default</option>
      <option value="anthropic">Anthropic (Claude)</option>
      <option value="openai">OpenAI-compatible (OpenAI, Gemini, Groq, Ollama…)</option>
    </select>
    <label>Model</label>
    <input id="model" placeholder="e.g. claude-opus-4-8 / gpt-4o / llama3">
  </div>
  <div class="row">
    <label>API key</label>
    <input id="key" type="password" placeholder="stays in your browser">
    <label>Base URL</label>
    <input id="base" placeholder="optional, e.g. http://localhost:11434/v1">
  </div>
  <div class="row">
    <label>+ Model 2</label>
    <select id="provider2">
      <option value="">none</option>
      <option value="anthropic">Anthropic (Claude)</option>
      <option value="openai">OpenAI-compatible</option>
    </select>
    <input id="model2" placeholder="2nd model, e.g. gpt-4o">
    <input id="key2" type="password" placeholder="2nd key">
    <input id="base2" placeholder="2nd base URL (optional)">
  </div>
  <div class="row">
    <span class="hint" style="margin:0">Add a second frontier key and Olympus
      uses both together — each part of the pipeline runs on whichever model is
      strongest for it (e.g. one for reasoning, the other for coding). Not a
      switch; they compose.</span>
  </div>
  <div class="row">
    <label>Access</label>
    <input id="access" type="password"
           placeholder="instance access token (only if the host set one)">
    <label>Language</label>
    <input id="lang" placeholder="auto (or e.g. Spanish, 日本語, Arabic)">
  </div>
  <div class="row">
    <label><input type="checkbox" id="contribute" style="flex:none;width:auto;min-width:0">
      Contribute anonymized insights</label>
    <span class="hint" style="margin:0">Opt in to let Olympus distill
      anonymized, PII-stripped insights from your chats into shared skills that
      improve it for everyone — only proven skills are kept. Off by default.</span>
  </div>
  <p class="hint">Your key lives in this browser only and is sent solely with
  your own requests; the server never stores or logs it. Leave everything
  blank to use the server's configured model.</p>
</div>
<div id="log"><div id="welcome">
  <p class="sys">The council is assembled — a main agent, a supervisor, a
  hallucination-checker, and __OLYMPUS_NSPEC__ specialists. Ask anything; rate answers with
  👍/👎 so Olympus learns. It <b>prepares</b> actions like sending email and
  waits for your approval before doing anything irreversible.</p>
  <div id="cost"></div>
  <p class="sys" style="margin-bottom:0">Try one:</p>
  <div class="chips" id="chips">
    <button class="chip" type="button">Research small modular nuclear reactor startups and give me a five-bullet investment brief</button>
    <button class="chip" type="button">Write a Python function that parses ISO-8601 durations into seconds, then review it for edge cases</button>
    <button class="chip" type="button">Draft a 30-day go-to-market plan for a B2B scheduling tool</button>
    <button class="chip" type="button">Summarize this YouTube video and what I should take away from it</button>
  </div>
</div></div>
<form id="f">
  <button id="attach" type="button" title="Attach a text/CSV/code file">📎</button>
  <input id="file" type="file" hidden>
  <input id="q" class="q" autocomplete="off" placeholder="Ask the council..." autofocus>
  <button id="b" class="send" type="submit">Send</button>
</form>
<div style="text-align:center;padding:6px;font-size:12px;color:#6b7280">
  <a href="/privacy" target="_blank" style="color:#6b7280">Privacy</a> ·
  <a href="/terms" target="_blank" style="color:#6b7280">Terms</a>
</div>
<script>
const log = document.getElementById('log'), f = document.getElementById('f'),
      q = document.getElementById('q'), b = document.getElementById('b'),
      panel = document.getElementById('panel'),
      fileIn = document.getElementById('file'),
      attach = document.getElementById('attach');
const fields = ['provider', 'model', 'key', 'base', 'access', 'lang',
                'provider2', 'model2', 'key2', 'base2'];
fields.forEach(id => {
  const el = document.getElementById(id);
  el.value = localStorage.getItem('olympus_' + id) || '';
  el.addEventListener('change',
    () => localStorage.setItem('olympus_' + id, el.value));
});
const contribEl = document.getElementById('contribute');
contribEl.checked = localStorage.getItem('olympus_contribute') === '1';
contribEl.addEventListener('change',
  () => localStorage.setItem('olympus_contribute', contribEl.checked ? '1' : '0'));
document.getElementById('gear').onclick = () => panel.classList.toggle('open');

// --- first-run onboarding -----------------------------------------------
const CFG = window.OLYMPUS_CFG || {};
(function onboarding() {
  const cost = document.getElementById('cost');
  if (cost) {
    if (CFG.free_chats > 0)
      cost.innerHTML = 'You get <b>' + CFG.free_chats + ' free chats per day</b>'
        + ' on us — after that, add your own API key in <b>⚙ model</b> to keep going.';
    else if (CFG.require_byok)
      cost.innerHTML = 'This instance runs on <b>your</b> API key. Open '
        + '<b>⚙ model</b>, choose a provider, and paste your key (it stays in '
        + 'your browser).';
  }
  document.querySelectorAll('#chips .chip').forEach(btn => {
    btn.onclick = () => { q.value = btn.textContent; q.focus(); };
  });
})();
function hideWelcome() {
  const w = document.getElementById('welcome');
  if (w) w.remove();
}
const reportbox = document.getElementById('reportbox');
const reportmsg = document.getElementById('reportmsg');
document.getElementById('reportbtn').onclick = () => {
  reportbox.style.display = reportbox.style.display === 'none' ? 'block' : 'none';
  reportmsg.textContent = '';
};
document.getElementById('reportcancel').onclick = () => {
  reportbox.style.display = 'none';
};
document.getElementById('reportsend').onclick = async () => {
  const text = document.getElementById('reporttext').value.trim();
  if (!text) { reportmsg.textContent = 'Please describe the problem.'; return; }
  reportmsg.textContent = 'Sending...';
  try {
    const r = await fetch('/api/report', {method: 'POST', headers: hdrs(),
      body: JSON.stringify({session, message: text,
        contact: document.getElementById('reportcontact').value})});
    const d = await r.json();
    reportmsg.textContent = d.ok ? 'Thank you — your report was sent.'
                                 : (d.error || 'Could not send.');
    if (d.ok) {
      document.getElementById('reporttext').value = '';
      document.getElementById('reportcontact').value = '';
      setTimeout(() => { reportbox.style.display = 'none'; reportmsg.textContent = ''; }, 1800);
    }
  } catch (e) { reportmsg.textContent = 'Network error — try again.'; }
};
let session = localStorage.getItem('olympus_session');
if (!session) {
  session = (crypto.randomUUID ? crypto.randomUUID() :
             String(Math.random()).slice(2));
  localStorage.setItem('olympus_session', session);
}
let seen = 0, attached = null;
attach.onclick = () => attached ? clearFile() : fileIn.click();
function clearFile() {
  attached = null; fileIn.value = '';
  attach.classList.remove('has'); attach.textContent = '📎';
}
fileIn.onchange = () => {
  const file = fileIn.files[0];
  if (!file) return;
  if (file.size > 200 * 1024) { alert('Max 200 KB (text files only).'); return; }
  const reader = new FileReader();
  reader.onload = () => {
    attached = {name: file.name, text: String(reader.result)};
    attach.classList.add('has'); attach.textContent = '📎 ' + file.name;
  };
  reader.readAsText(file);
};
function hdrs() {
  const h = {'Content-Type': 'application/json'};
  const tok = document.getElementById('access').value;
  if (tok) h['X-Olympus-Token'] = tok;
  return h;
}
function cfg() {
  return {
    provider: document.getElementById('provider').value,
    model: document.getElementById('model').value,
    api_key: document.getElementById('key').value,
    base_url: document.getElementById('base').value,
    language: document.getElementById('lang').value,
    contribute: document.getElementById('contribute').checked,
    extra: {
      provider: document.getElementById('provider2').value,
      model: document.getElementById('model2').value,
      api_key: document.getElementById('key2').value,
      base_url: document.getElementById('base2').value
    }
  };
}
function add(cls, text) {
  const p = document.createElement('p');
  p.className = cls; p.textContent = text;
  log.appendChild(p); log.scrollTop = log.scrollHeight;
  return p;
}
function addRating() {
  const div = document.createElement('div');
  div.className = 'rate';
  ['👍', '👎'].forEach((emo, i) => {
    const btn = document.createElement('button');
    btn.textContent = emo;
    btn.onclick = async () => {
      div.querySelectorAll('button').forEach(x => x.disabled = true);
      btn.classList.add('done');
      await fetch('/api/feedback', {method: 'POST', headers: hdrs(),
        body: JSON.stringify({session: session,
                              verdict: i === 0 ? 'up' : 'down'})});
    };
    div.appendChild(btn);
  });
  log.appendChild(div); log.scrollTop = log.scrollHeight;
}
async function poll() {
  try {
    const r = await fetch('/api/status?since=' + seen +
                          '&session=' + encodeURIComponent(session),
                          {headers: hdrs()});
    const d = await r.json();
    d.events.forEach(e => add('sys', e));
    seen = d.next;
  } catch (e) {}
}

const actionsEl = document.getElementById('actions');
const cardsEl = document.getElementById('cards');
const budgetEl = document.getElementById('budget');
const actBtn = document.getElementById('actbtn');
const actCount = document.getElementById('actcount');
actBtn.onclick = () => { actionsEl.classList.toggle('open'); renderActions(); };

function renderBudget(b) {
  if (!b || !b.enabled) { budgetEl.className = ''; budgetEl.textContent = ''; return; }
  budgetEl.className = b.exceeded ? 'over' : 'ok';
  budgetEl.textContent = b.exceeded
    ? 'Daily budget reached: $' + b.spent.toFixed(2) + ' / $' + b.limit.toFixed(2) +
      ' — Olympus paused new requests to protect your API bill.'
    : 'Today on your API key: $' + b.spent.toFixed(2) + ' / $' + b.limit.toFixed(2);
}

function renderCards(list) {
  const pending = list.filter(a => a.status === 'prepared');
  actCount.textContent = pending.length;
  actCount.classList.toggle('show', pending.length > 0);
  cardsEl.innerHTML = '';
  if (!list.length) {
    cardsEl.innerHTML = '<p class="sys" style="max-width:860px;margin:0 auto">' +
      'No actions to review. When Olympus prepares one (e.g. an email to send), ' +
      'it appears here for your approval.</p>';
    return;
  }
  list.forEach(a => {
    const card = document.createElement('div'); card.className = 'card';
    const executed = a.status === 'executed';
    card.innerHTML =
      '<div class="top"><span class="ttl"></span>' +
      '<span class="risk ' + a.risk + '">' + a.risk.replace(/_/g,' ') +
      (executed ? ' · done' : '') + '</span></div>' +
      '<pre></pre><div class="why"></div><div class="btns"></div>';
    card.querySelector('.ttl').textContent = a.title;
    card.querySelector('pre').textContent = a.preview;
    if (a.why) card.querySelector('.why').textContent = 'why: ' + a.why;
    const btns = card.querySelector('.btns');
    if (a.status === 'prepared') {
      const ok = document.createElement('button'); ok.className='ok'; ok.textContent='Approve';
      ok.onclick = () => act(a.id, 'approve');
      const ed = document.createElement('button'); ed.className='no'; ed.textContent='Edit';
      ed.onclick = () => editAction(a);
      const no = document.createElement('button'); no.className='no'; no.textContent='Reject';
      no.onclick = () => act(a.id, 'reject');
      btns.append(ok, ed, no);
    } else if (executed && a.reversible) {
      const un = document.createElement('button'); un.className='un'; un.textContent='Undo';
      un.onclick = () => act(a.id, 'undo');
      btns.append(un);
    }
    cardsEl.appendChild(card);
  });
}

async function renderActions() {
  try {
    const r = await fetch('/api/actions?session=' + encodeURIComponent(session),
                          {headers: hdrs()});
    const d = await r.json();
    renderBudget(d.budget);
    renderCards(d.actions || []);
  } catch (e) {}
}

async function act(id, op) {
  const body = {session: session, action_id: id, op: op};
  if (op === 'reject') body.reason = prompt('Why reject? (optional)') || '';
  const r = await fetch('/api/action', {method:'POST', headers: hdrs(),
                                        body: JSON.stringify(body)});
  const d = await r.json();
  if (d.budget) renderBudget(d.budget);
  if (d.actions) renderCards(d.actions); else renderActions();
}

async function editAction(a) {
  const txt = prompt('Edit the fields, then approve when it looks right:',
                     JSON.stringify(a.payload || {}, null, 1));
  if (txt === null) return;                       // cancelled
  let changes;
  try { changes = JSON.parse(txt); }
  catch (e) { alert('Not valid JSON — nothing changed.'); return; }
  const r = await fetch('/api/action', {method:'POST', headers: hdrs(),
    body: JSON.stringify({session: session, action_id: a.id,
                          op: 'edit', changes: changes})});
  const d = await r.json();
  if (d.budget) renderBudget(d.budget);
  if (d.actions) renderCards(d.actions); else renderActions();
}
setInterval(renderActions, 4000);
renderActions();

const memoryEl = document.getElementById('memory');
const memBtn = document.getElementById('membtn');
const memCount = document.getElementById('memcount');
memBtn.onclick = () => { memoryEl.classList.toggle('open'); renderMemory(); };

function memHeader(text, margin) {
  const h = document.createElement('p'); h.className = 'sys';
  h.style = 'max-width:860px;margin:' + (margin || '12px auto 4px');
  h.textContent = text;
  return h;
}

function memRow(label, content, buttons) {
  const row = document.createElement('div'); row.className = 'mem';
  const t = document.createElement('span'); t.className = 't'; t.textContent = label;
  const c = document.createElement('span'); c.className = 'c'; c.textContent = content;
  const span = document.createElement('span');
  (buttons || []).forEach(b => {
    const el = document.createElement('button'); el.className = b.cls;
    el.textContent = b.text; el.onclick = b.fn; span.appendChild(el);
  });
  row.append(t, c, span);
  return row;
}

function renderMemoryData(d) {
  const cands = d.candidates || [], mems = d.memories || [], pbs = d.playbooks || [],
        graph = d.graph || [], insights = d.insights || [], prof = d.profile || {},
        oc = d.outcomes || {};
  const pendingPb = pbs.filter(p => p.status === 'proposed').length;
  memCount.textContent = cands.length + pendingPb;
  memCount.classList.toggle('show', (cands.length + pendingPb) > 0);
  memoryEl.innerHTML = '';

  insights.forEach(i => {
    const p = document.createElement('p'); p.className = 'sys';
    p.style = 'max-width:860px;margin:4px auto;color:#d9b44a';
    p.textContent = '💡 ' + i.message;
    memoryEl.appendChild(p);
  });
  if (oc.total) {
    memoryEl.appendChild(memHeader('Track record: ' + oc.total + ' actions — ' +
      oc.approved + ' approved as-is, ' + oc.approved_after_edit +
      ' after edit, ' + oc.rejected + ' rejected.', '4px auto'));
  }

  memoryEl.appendChild(memHeader('Your profile:'));
  memoryEl.appendChild(memRow('about', prof.about || '(not set)', [
    {cls: 'no', text: 'Edit', fn: () => {
      const v = prompt('About you (how Olympus should treat you):', prof.about || '');
      if (v !== null) memact({kind: 'profile', op: 'set', value: v});
    }}]));
  Object.keys(prof.facts || {}).forEach(k =>
    memoryEl.appendChild(memRow(k, prof.facts[k], [])));

  if (cands.length) {
    memoryEl.appendChild(memHeader(
      'Awaiting your approval (sensitive/uncertain — not saved automatically):'));
    cands.forEach(m => memoryEl.appendChild(memRow(m.type, m.content, [
      {cls: 'ok', text: 'Approve', fn: () => memact({kind: 'memory', op: 'approve', id: m.id})},
      {cls: 'no', text: 'Dismiss', fn: () => memact({kind: 'memory', op: 'reject', id: m.id})}])));
  }

  if (pbs.length) {
    memoryEl.appendChild(memHeader('Playbooks (saved workflows):'));
    pbs.forEach(p => {
      const proposed = p.status === 'proposed';
      const btns = proposed
        ? [{cls: 'ok', text: 'Approve', fn: () => memact({kind: 'playbook', op: 'approve', id: p.id})},
           {cls: 'no', text: 'Dismiss', fn: () => memact({kind: 'playbook', op: 'reject', id: p.id})}]
        : [{cls: 'no', text: 'Forget', fn: () => memact({kind: 'playbook', op: 'forget', id: p.id})}];
      memoryEl.appendChild(memRow(proposed ? 'proposed' : 'playbook',
        p.name + ' — ' + p.steps.join(' → '), btns));
    });
  }

  if (graph.length) {
    memoryEl.appendChild(memHeader('People & companies:'));
    graph.forEach(n => memoryEl.appendChild(memRow(n.kind,
      n.label + (n.connections.length ? ' (' + n.connections.join('; ') + ')' : ''),
      [{cls: 'no', text: 'Forget', fn: () => memact({kind: 'graph', op: 'forget', label: n.label})}])));
  }

  if (mems.length) {
    memoryEl.appendChild(memHeader('What Olympus remembers about you:'));
    mems.forEach(m => memoryEl.appendChild(memRow(m.type, m.content, [
      {cls: 'no', text: 'Forget', fn: () => memact({kind: 'memory', op: 'forget', id: m.id})}])));
  }
}

async function renderMemory() {
  try {
    const r = await fetch('/api/memory?session=' + encodeURIComponent(session),
                          {headers: hdrs()});
    renderMemoryData(await r.json());
  } catch (e) {}
}

async function memact(payload) {
  const r = await fetch('/api/memory', {method: 'POST', headers: hdrs(),
    body: JSON.stringify(Object.assign({session: session}, payload))});
  renderMemoryData(await r.json());
}
setInterval(renderMemory, 8000);
renderMemory();

// --- documents workspace ---
const docsEl = document.getElementById('documents');
const docBtn = document.getElementById('docbtn');
let docOpenName = null;
docBtn.onclick = () => {
  docsEl.classList.toggle('open');
  if (docsEl.classList.contains('open')) renderDocuments();
};

async function renderDocuments(editing) {
  const wrap = document.createElement('div'); wrap.className = 'docwrap';
  if (editing !== undefined) { docsEl.innerHTML = ''; docsEl.appendChild(docEditor(editing)); return; }
  const bar = document.createElement('div');
  bar.style = 'display:flex;justify-content:space-between;margin-bottom:8px';
  const h = document.createElement('b'); h.textContent = 'Your documents';
  const nw = document.createElement('button'); nw.className = 'ok';
  nw.textContent = '＋ New'; nw.onclick = () => { docOpenName = null; renderDocuments(''); };
  bar.append(h, nw); wrap.appendChild(bar);
  let d = {documents: []};
  try {
    d = await (await fetch('/api/documents?session=' + encodeURIComponent(session),
      {headers: hdrs()})).json();
  } catch (e) {}
  if (!d.documents || !d.documents.length) {
    const p = document.createElement('p'); p.className = 'sys';
    p.textContent = 'No documents yet. Click ＋ New, or ask the assistant to draft one.';
    wrap.appendChild(p);
  }
  (d.documents || []).forEach(doc => {
    const row = document.createElement('div'); row.className = 'docitem';
    const t = document.createElement('span'); t.className = 'dt'; t.textContent = doc.name;
    const m = document.createElement('span'); m.className = 'dm';
    m.textContent = doc.chars + ' chars';
    row.append(t, m);
    row.onclick = () => openDoc(doc.name);
    wrap.appendChild(row);
  });
  docsEl.innerHTML = ''; docsEl.appendChild(wrap);
}

function docEditor(content) {
  const wrap = document.createElement('div'); wrap.className = 'docwrap';
  const title = document.createElement('input'); title.id = 'doctitle';
  title.placeholder = 'Document name'; title.value = docOpenName || '';
  const body = document.createElement('textarea'); body.id = 'docbody';
  body.value = content || '';
  const bar = document.createElement('div');
  bar.style = 'display:flex;gap:8px;margin-top:6px';
  const save = document.createElement('button'); save.className = 'ok';
  save.textContent = 'Save'; save.onclick = saveDoc;
  const back = document.createElement('button'); back.className = 'no';
  back.textContent = 'Back'; back.onclick = () => renderDocuments();
  bar.append(save, back);
  if (docOpenName) {
    const del = document.createElement('button'); del.className = 'no';
    del.textContent = 'Delete'; del.onclick = deleteDoc;
    bar.appendChild(del);
  }
  wrap.append(title, body, bar);
  return wrap;
}

async function openDoc(name) {
  docOpenName = name;
  let content = '';
  try {
    const d = await (await fetch('/api/documents?name=' + encodeURIComponent(name) +
      '&session=' + encodeURIComponent(session), {headers: hdrs()})).json();
    content = d.content || '';
  } catch (e) {}
  renderDocuments(content);
}

async function docPost(payload) {
  return fetch('/api/documents', {method: 'POST', headers: hdrs(),
    body: JSON.stringify(Object.assign({session: session}, payload))});
}
async function saveDoc() {
  const name = document.getElementById('doctitle').value.trim();
  const content = document.getElementById('docbody').value;
  if (!name) { alert('Give the document a name.'); return; }
  const r = await docPost({op: 'save', name: name, content: content});
  if (r.ok) { docOpenName = null; renderDocuments(); } else { alert('Save failed.'); }
}
async function deleteDoc() {
  if (!docOpenName || !confirm('Delete "' + docOpenName + '"?')) return;
  await docPost({op: 'delete', name: docOpenName});
  docOpenName = null; renderDocuments();
}

// --- gallery (workspace images) ---
const galEl = document.getElementById('gallery');
const galBtn = document.getElementById('galbtn');
galBtn.onclick = () => {
  galEl.classList.toggle('open');
  if (galEl.classList.contains('open')) renderGallery();
};
function imgSrc(name) {
  return '/api/gallery/image?name=' + encodeURIComponent(name) +
    '&session=' + encodeURIComponent(session);
}
async function renderGallery() {
  galEl.innerHTML = '';
  let d = {images: []};
  try {
    d = await (await fetch('/api/gallery?session=' + encodeURIComponent(session),
      {headers: hdrs()})).json();
  } catch (e) {}
  if (!d.images || !d.images.length) {
    const p = document.createElement('p'); p.className = 'sys';
    p.textContent = 'No images yet. Ask the assistant to generate one and it will appear here.';
    galEl.appendChild(p); return;
  }
  const grid = document.createElement('div'); grid.className = 'galgrid';
  d.images.forEach(im => {
    const card = document.createElement('div'); card.className = 'galcard';
    const img = document.createElement('img');
    img.loading = 'lazy'; img.src = imgSrc(im.name); img.alt = im.name;
    img.onclick = () => window.open(imgSrc(im.name), '_blank');
    const meta = document.createElement('div'); meta.className = 'gm';
    const nm = document.createElement('b'); nm.textContent = im.name;
    nm.title = im.name;
    const sz = document.createElement('span');
    sz.textContent = Math.max(1, Math.round(im.bytes / 1024)) + 'KB';
    meta.append(nm, sz);
    const actions = document.createElement('div'); actions.className = 'gm';
    const edit = document.createElement('a'); edit.textContent = 'Edit';
    edit.style.cursor = 'pointer'; edit.style.color = '#d9b44a';
    edit.onclick = () => editImage(im.name);
    const del = document.createElement('a'); del.textContent = 'Delete';
    del.style.cursor = 'pointer'; del.style.color = '#8b93a0';
    del.onclick = () => galPost({op: 'delete', name: im.name});
    actions.append(edit, del);
    card.append(img, meta, actions); grid.appendChild(card);
  });
  galEl.appendChild(grid);
}
async function galPost(payload) {
  const r = await (await fetch('/api/gallery', {method: 'POST', headers: hdrs(),
    body: JSON.stringify(Object.assign({session: session}, payload))})).json();
  if (r.message && !r.ok) alert(r.message);
  renderGallery();
}
function editImage(name) {
  const prompt = window.prompt('Describe the edit for "' + name + '":');
  if (!prompt) return;
  galPost({op: 'edit', name: name, prompt: prompt});
}

// --- agenda (scheduled tasks + upcoming calendar) ---
const agEl = document.getElementById('agenda');
const agBtn = document.getElementById('agbtn');
agBtn.onclick = () => {
  agEl.classList.toggle('open');
  if (agEl.classList.contains('open')) renderAgenda();
};
function fmtDue(secs) {
  if (secs == null) return '';
  if (secs < 60) return 'due now';
  if (secs < 3600) return 'in ' + Math.round(secs / 60) + 'm';
  if (secs < 86400) return 'in ' + Math.round(secs / 3600) + 'h';
  return 'in ' + Math.round(secs / 86400) + 'd';
}
function agRow(title, when, sub, off) {
  const row = document.createElement('div');
  row.className = 'agrow' + (off ? ' off' : '');
  const t = document.createElement('div'); t.className = 'at';
  const b = document.createElement('b'); b.textContent = title; b.style.fontWeight = 'normal';
  t.appendChild(b);
  if (sub) { const s = document.createElement('span'); s.className = 'ap'; s.textContent = sub; t.appendChild(s); }
  const w = document.createElement('span'); w.className = 'aw'; w.textContent = when;
  row.append(t, w); return row;
}
async function renderAgenda() {
  agEl.innerHTML = '';
  const wrap = document.createElement('div'); wrap.className = 'agwrap';
  let d = {tasks: [], events: null, calendar_connected: false, todos: []};
  try {
    d = await (await fetch('/api/agenda?session=' + encodeURIComponent(session),
      {headers: hdrs()})).json();
  } catch (e) {}
  // --- your list (notes/todos/reminders) ---
  const lh = document.createElement('h4'); lh.textContent = 'Your list';
  wrap.appendChild(lh);
  const addbar = document.createElement('div'); addbar.className = 'cmpbar';
  const ti = document.createElement('input'); ti.id = 'todoinput';
  ti.placeholder = 'Add a todo, note, or reminder…';
  const di = document.createElement('input'); di.id = 'tododue'; di.type = 'text';
  di.placeholder = 'due (optional)'; di.style.maxWidth = '150px';
  const ab = document.createElement('button'); ab.className = 'ok'; ab.textContent = 'Add';
  ab.onclick = () => addTodo(ti.value, di.value);
  ti.onkeydown = (e) => { if (e.key === 'Enter') addTodo(ti.value, di.value); };
  addbar.append(ti, di, ab); wrap.appendChild(addbar);
  if (!d.todos || !d.todos.length) {
    const p = document.createElement('p'); p.className = 'sys';
    p.textContent = 'Nothing on your list yet.';
    wrap.appendChild(p);
  } else {
    d.todos.forEach(it => {
      const row = document.createElement('div'); row.className = 'agrow';
      const t = document.createElement('div'); t.className = 'at';
      if (it.kind === 'todo') {
        const cb = document.createElement('input'); cb.type = 'checkbox';
        cb.style.marginRight = '8px'; cb.onchange = () => completeTodo(it.id);
        t.appendChild(cb);
      }
      const b = document.createElement('span'); b.textContent = it.text;
      if (it.kind === 'note') b.style.fontStyle = 'italic';
      t.appendChild(b);
      const w = document.createElement('span'); w.className = 'aw';
      if (it.due != null) {
        w.textContent = '⏰ ' + new Date(it.due * 1000).toLocaleString();
        if (it.overdue) w.style.color = '#e0884a';
      }
      const del = document.createElement('span'); del.className = 'aw';
      del.textContent = '✕'; del.style.cursor = 'pointer'; del.style.marginLeft = '8px';
      del.onclick = () => deleteTodo(it.id);
      row.append(t, w, del); wrap.appendChild(row);
    });
  }
  const th = document.createElement('h4'); th.textContent = 'Scheduled tasks';
  wrap.appendChild(th);
  if (!d.tasks || !d.tasks.length) {
    const p = document.createElement('p'); p.className = 'sys';
    p.textContent = 'No scheduled tasks. Add one with `olympus schedule add`, or ask the assistant.';
    wrap.appendChild(p);
  } else {
    d.tasks.forEach(j => {
      const to = j.deliver_to ? '  →' + j.deliver_to : '';
      const when = (j.kind === 'on_exit' ? j.when : j.when + (j.enabled ? '  (' + fmtDue(j.due_in) + ')' : '')) + to;
      wrap.appendChild(agRow(j.name, when, j.prompt, !j.enabled));
    });
  }
  const eh = document.createElement('h4'); eh.textContent = 'Upcoming calendar';
  wrap.appendChild(eh);
  if (!d.calendar_connected) {
    const p = document.createElement('p'); p.className = 'sys';
    p.textContent = 'Connect a Google account to see your upcoming events here.';
    wrap.appendChild(p);
  } else if (!d.events || !d.events.length) {
    const p = document.createElement('p'); p.className = 'sys';
    p.textContent = 'No events in the next two weeks.';
    wrap.appendChild(p);
  } else {
    d.events.forEach(e => {
      const when = e.all_day ? e.start : new Date(e.start).toLocaleString();
      const sub = e.attendees && e.attendees.length ? 'with ' + e.attendees.join(', ') : '';
      wrap.appendChild(agRow(e.summary, when, sub, false));
    });
  }
  agEl.appendChild(wrap);
}
async function todoPost(payload) {
  await fetch('/api/todos', {method: 'POST', headers: hdrs(),
    body: JSON.stringify(Object.assign({session: session}, payload))});
  renderAgenda();
}
function addTodo(text, due) {
  text = (text || '').trim(); if (!text) return;
  const kind = /^note:/i.test(text) ? 'note' : 'todo';
  if (kind === 'note') text = text.replace(/^note:\\s*/i, '');
  todoPost({op: 'add', text: text, kind: kind, due: (due || '').trim()});
}
function completeTodo(id) { todoPost({op: 'complete', id: id, done: true}); }
function deleteTodo(id) { todoPost({op: 'delete', id: id}); }

// --- compare (blind multi-model) ---
const cmpEl = document.getElementById('compare');
const cmpBtn = document.getElementById('cmpbtn');
let cmpId = null, cmpRevealed = false;
cmpBtn.onclick = () => {
  cmpEl.classList.toggle('open');
  if (cmpEl.classList.contains('open')) renderCompare();
};
async function renderCompare() {
  cmpEl.innerHTML = '';
  const wrap = document.createElement('div'); wrap.className = 'cmpwrap';
  let info = {models: [], tally: {}};
  try {
    info = await (await fetch('/api/compare?session=' + encodeURIComponent(session),
      {headers: hdrs()})).json();
  } catch (e) {}
  const solo = info.models.length < 2;
  const bar = document.createElement('div'); bar.className = 'cmpbar';
  const inp = document.createElement('input'); inp.id = 'cmpprompt';
  inp.placeholder = solo
    ? 'Add a second model to compare — see below'
    : 'Ask the same thing of ' + info.models.length + ' models, blind…';
  inp.disabled = solo;
  const go = document.createElement('button'); go.className = 'ok';
  go.textContent = 'Compare'; go.disabled = solo;
  go.onclick = () => runCompare(inp.value);
  inp.onkeydown = (e) => { if (e.key === 'Enter') runCompare(inp.value); };
  bar.append(inp, go); wrap.appendChild(bar);
  if (solo) {
    const only = info.models.length === 1 ? 'Only ' + info.models[0] + ' is configured. ' : '';
    const hint = document.createElement('div'); hint.className = 'cmphint';
    const p = document.createElement('p'); p.className = 'sys';
    p.textContent = only + 'Blind compare needs two. A second Anthropic model reuses your key and egress — add this env var and reopen:';
    const pre = document.createElement('pre'); pre.className = 'cmpsnippet';
    pre.textContent = info.snippet || '';
    const copy = document.createElement('button'); copy.className = 'ok';
    copy.textContent = 'Copy'; copy.style.marginTop = '6px';
    copy.onclick = () => { navigator.clipboard && navigator.clipboard.writeText(pre.textContent); copy.textContent = 'Copied'; };
    hint.append(p, pre, copy); wrap.appendChild(hint);
  }
  const results = document.createElement('div'); results.id = 'cmpresults';
  wrap.appendChild(results);
  const tally = document.createElement('div'); tally.id = 'cmptally';
  tally.textContent = tallyText(info.tally);
  wrap.appendChild(tally);
  cmpEl.appendChild(wrap);
}
function tallyText(t) {
  const keys = Object.keys(t || {});
  if (!keys.length) return 'No blind picks yet.';
  return 'Your blind picks: ' + keys.sort((a, b) => t[b] - t[a])
    .map(k => k + ' ' + t[k]).join(', ');
}
async function runCompare(prompt) {
  prompt = (prompt || '').trim();
  if (!prompt) return;
  const results = document.getElementById('cmpresults');
  results.innerHTML = '<p class="sys">Running across your models…</p>';
  cmpId = null; cmpRevealed = false;
  let d = {};
  try {
    d = await (await fetch('/api/compare', {method: 'POST', headers: hdrs(),
      body: JSON.stringify({session: session, op: 'run', prompt: prompt})})).json();
  } catch (e) { results.innerHTML = '<p class="sys">Compare failed.</p>'; return; }
  if (d.error) { results.innerHTML = '<p class="sys">' + d.error + '</p>'; return; }
  cmpId = d.id;
  results.innerHTML = '';
  d.answers.forEach(a => results.appendChild(cmpCard(a)));
}
function cmpCard(a) {
  const card = document.createElement('div'); card.className = 'cmpcard';
  card.dataset.label = a.label;
  const head = document.createElement('div'); head.className = 'ch';
  const b = document.createElement('b'); b.textContent = 'Answer ' + a.label;
  const right = document.createElement('span');
  const who = document.createElement('span'); who.className = 'who';
  const pick = document.createElement('button'); pick.className = 'ok';
  pick.textContent = 'Pick this'; pick.onclick = () => revealCompare(a.label);
  right.appendChild(pick); right.appendChild(who);
  head.append(b, right);
  const body = document.createElement('div'); body.className = 'cb';
  body.textContent = a.text;
  card.append(head, body); return card;
}
async function revealCompare(choice) {
  if (!cmpId) return;
  let d = {};
  try {
    d = await (await fetch('/api/compare', {method: 'POST', headers: hdrs(),
      body: JSON.stringify({session: session, op: 'reveal', id: cmpId,
                            choice: choice || ''})})).json();
  } catch (e) { return; }
  if (d.error) return;
  cmpRevealed = true;
  document.querySelectorAll('.cmpcard').forEach(card => {
    const label = card.dataset.label;
    card.querySelector('.who').textContent = d.mapping[label] || '';
    card.querySelectorAll('.ch button').forEach(btn => btn.remove());
    if (d.choice && label === d.choice) card.classList.add('picked');
  });
  const tally = document.getElementById('cmptally');
  if (tally) tally.textContent = tallyText(d.tally);
}

const connectBtn = document.getElementById('connect');
connectBtn.onclick = () => window.open(
  '/oauth/google/start?session=' + encodeURIComponent(session),
  'olympus_connect', 'width=520,height=640');
async function refreshConnected() {
  try {
    const d = await (await fetch('/api/connected?session=' +
      encodeURIComponent(session), {headers: hdrs()})).json();
    if (!d.configured) { connectBtn.style.display = 'none'; return; }
    connectBtn.style.display = '';
    connectBtn.textContent = d.connected ? '🔗 Google connected' : '🔗 connect Google';
    connectBtn.classList.toggle('done', d.connected);
  } catch (e) {}
}
setInterval(refreshConnected, 5000);
refreshConnected();
f.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  let text = q.value.trim();
  if (!text && !attached) return;
  if (attached) {
    text += '\\n\\n[Attached file: ' + attached.name + ']\\n```\\n' +
            attached.text.slice(0, 100000) + '\\n```';
  }
  hideWelcome();
  add('msg user', q.value.trim() + (attached ? '  📎 ' + attached.name : ''));
  q.value = ''; clearFile(); b.disabled = true; q.disabled = true;
  const timer = setInterval(poll, 1200);
  try {
    const r = await fetch('/api/chat?stream=1', {
      method: 'POST', headers: hdrs(),
      body: JSON.stringify({message: text, session: session, settings: cfg()})
    });
    clearInterval(timer); await poll();
    if (r.ok && r.body && (r.headers.get('Content-Type') || '').includes('text/plain')) {
      const reader = r.body.getReader(), dec = new TextDecoder();
      const p = add('msg bot', '');
      let acc = '';
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        acc += dec.decode(value, {stream: true});
        p.textContent = acc; log.scrollTop = log.scrollHeight;
      }
      if (acc.trim()) addRating();
    } else {
      const d = await r.json();
      add('msg bot', d.reply || d.error || '(no reply)');
      if (d.reply) addRating();
      if (d.need_key) {                              // BYOK required — guide the user
        panel.classList.add('open');
        panel.scrollIntoView({behavior: 'smooth', block: 'center'});
        document.getElementById('key').focus();
      }
    }
  } catch (e) {
    clearInterval(timer);
    add('sys', 'error: ' + e);
  } finally {
    b.disabled = false; q.disabled = false; q.focus();
    const before = parseInt(actCount.textContent || '0', 10);
    await renderActions();
    // auto-open the panel if the reply prepared a new action to review
    if (parseInt(actCount.textContent || '0', 10) > before)
      actionsEl.classList.add('open');
  }
});

// --- accounts (only gates the UI when the server requires login) ---------
const authEl = document.getElementById('auth');
const authErr = document.getElementById('autherr');
async function doAuth(path) {
  authErr.textContent = '';
  const r = await fetch(path, {method: 'POST', headers: hdrs(),
    body: JSON.stringify({username: document.getElementById('authuser').value,
                          password: document.getElementById('authpass').value})});
  const d = await r.json();
  if (d.ok) { authEl.style.display = 'none'; location.reload(); }
  else authErr.textContent = d.error || 'failed';
}
document.getElementById('loginbtn').onclick = () => doAuth('/api/login');
document.getElementById('registerbtn').onclick = () => doAuth('/api/register');
async function checkAuth() {
  try {
    const d = await (await fetch('/api/me?session=' +
      encodeURIComponent(session), {headers: hdrs()})).json();
    if (d.login_required && !d.logged_in) authEl.style.display = 'flex';
  } catch (e) {}
}
checkAuth();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str,
              extra_headers: dict | None = None) -> None:
        try:
            metrics.record_response(urlparse(self.path).path, code)
        except Exception:
            pass
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        cookie = getattr(self, "_set_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", self._cookie_header(cookie))
            self._set_cookie = None       # set once per response
        self.end_headers()
        self.wfile.write(body)

    def _cookie_header(self, value: str) -> str:
        """Set-Cookie for the session id. SameSite=Lax (not Strict) so the
        cookie rides the top-level redirect back from Google's consent screen,
        which lets the OAuth callback bind to the originating browser. Lax still
        withholds the cookie on cross-site POST/fetch, so CSRF on the
        state-changing API stays closed. Secure is added behind TLS."""
        secure = "; Secure" if _https_request(self) else ""
        return (f"olympus_sid={value}; HttpOnly; SameSite=Lax; "
                f"Path=/; Max-Age=31536000{secure}")

    def _json(self, payload: dict, code: int = 200,
              extra_headers: dict | None = None) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json",
                   extra_headers=extra_headers)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > _MAX_BODY:        # reject oversized bodies (DoS guard)
                return None
            return json.loads(self.rfile.read(length))
        except Exception:
            return None

    def _drain_refused_body(self) -> None:
        """Best-effort, strictly bounded cleanup AFTER a pre-body refusal.

        WHY THIS EXISTS. `Handler` inherits `protocol_version = "HTTP/1.0"`, so
        `close_connection` is true for every request and the socket is closed as
        soon as the response is written. Closing a TCP socket that still has
        unread bytes in its receive queue makes the stack send RST instead of
        FIN (RFC 1122 §4.2.2.13), and an arriving RST lets the peer discard data
        already sitting in its own receive buffer. A correctly written 401 could
        therefore be destroyed in flight: the real OpenAI SDK intermittently
        reported `APIConnectionError: Connection error.` for a wrong-key request
        that the server had in fact refused with a well-formed 401. Reading the
        abandoned body off the socket first lets the close emit a clean FIN.

        WHY IT RUNS AFTER THE RESPONSE, NEVER BEFORE. The obvious "read the body
        before answering" would hand an UNAUTHENTICATED client a slowloris: it
        could advertise a large Content-Length, dribble it out a byte at a time,
        and pin a handler thread for as long as it liked — turning a reliability
        fix into resource exhaustion, reachable with no credential at all. The
        refusal is fully written and flushed before this is called, so a slow or
        hostile sender is answered promptly and merely forfeits the clean close.
        Draining is cleanup, never a precondition.

        Every bound here is fixed, cheap and auditable:
          * only a valid, positive `Content-Length` is drained — no header, a
            malformed one, or a non-positive one means there is nothing to do;
          * never more than `_MAX_BODY`, the same ceiling `_read_json` enforces;
          * a small per-recv timeout AND a total wall-clock deadline, so neither
            a stalled sender nor a dribbling one can extend this;
          * the connection's previous timeout is restored;
          * only transport/timeout failures are swallowed;
          * nothing is parsed, no orchestrator or provider is touched, and no
            failure in here can turn a 401/429 into a 500.
        """
        raw = self.headers.get("Content-Length")
        if not raw:
            return
        try:
            length = int(raw)
        except (TypeError, ValueError):
            return                      # malformed header: nothing to trust
        if length <= 0:
            return
        remaining = min(length, _MAX_BODY)

        conn = getattr(self, "connection", None)
        rfile = getattr(self, "rfile", None)
        if conn is None or rfile is None:
            return
        # `read1` performs at most ONE underlying recv, which is what makes the
        # deadline below real; `read` would loop inside the buffered reader and
        # could outrun it. Resolved up front so a handler whose `rfile` is not a
        # BufferedReader returns quietly instead of raising an AttributeError
        # that would turn this cleanup into a 500.
        read1 = getattr(rfile, "read1", None)
        if read1 is None:
            return

        # The refusal is already written; make that explicit and ordered even if
        # `wbufsize` ever changes from its unbuffered default, so the client can
        # never be waiting on bytes we are holding while we read from it.
        try:
            self.wfile.flush()
        except (OSError, ValueError):
            return                      # peer already gone: nothing to drain

        try:
            previous = conn.gettimeout()
        except OSError:
            return
        deadline = time.monotonic() + _REFUSAL_DRAIN_SECONDS
        try:
            while remaining > 0:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    break               # total deadline: stop, never retry
                conn.settimeout(min(_REFUSAL_DRAIN_READ_SECONDS, budget))
                chunk = read1(min(remaining, _REFUSAL_DRAIN_CHUNK))
                if not chunk:
                    break               # clean EOF, or peer stopped sending
                remaining -= len(chunk)
        except (OSError, EOFError, ValueError):
            pass                        # transport gave up first — that is fine
        finally:
            try:
                conn.settimeout(previous)
            except OSError:
                pass

    def _session_id(self, provided: str | None = None) -> str:
        """Resolve the session id (cookie-preferred) and arrange to set the
        cookie on the response if a fresh one was minted."""
        sid, cookie = _resolve_sid(self, provided)
        if cookie:
            self._set_cookie = cookie
        return sid

    def _handle_auth(self, path: str, payload: dict) -> None:
        """Register / log in / log out. On success, set the session-token
        cookie so every later request is authenticated as this account."""
        if path == "/api/logout":
            cookie_sid = _resolve_sid(self, None)[0]
            accounts.logout(cookie_sid)
            self._set_cookie = uuid.uuid4().hex      # rotate to a fresh anon id
            self._json({"ok": True, "logged_in": False})
            return
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        try:
            if path == "/api/register":
                token = accounts.register(username, password)
            else:
                token = accounts.authenticate(username, password)
                if not token:
                    self._json({"error": "wrong username or password"}, 401)
                    return
        except ValueError as err:
            self._json({"error": str(err)}, 400)
            return
        self._set_cookie = token
        self._json({"ok": True, "logged_in": True,
                    "username": username.strip()})

    def _principal(self, sid: str) -> str | None:
        """The per-user namespace for this request. A logged-in session token
        (the cookie) maps to an account; otherwise, if login isn't required,
        fall back to the anonymous per-cookie namespace. Returns None when
        login is required and the caller isn't authenticated (→ 401)."""
        ns = accounts.namespace_for_token(sid)
        if ns:
            return ns
        if accounts.require_login():
            return None
        return _user_for(sid)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/healthz":
            # LIVENESS: "the process is serving". No auth, no data. Must stay
            # cheap and must NOT consult config or disk — a liveness probe that
            # fails on a full disk causes a restart loop instead of an alert.
            self._json({"status": "ok",
                        "uptime_seconds": metrics.snapshot()["uptime_seconds"]})
            return
        if url.path == "/readyz":
            # READINESS: "this instance can serve a correct request." Distinct
            # from liveness on purpose — a config-incomplete or read-only
            # instance is alive but must be taken out of rotation, not killed.
            # No auth (an orchestrator probes it before credentials are in
            # play) and NO user data: only booleans, the declared mode, and the
            # build. Staging problems are named because the operator reading a
            # failed probe needs the reason, and they contain no secrets.
            ready, detail = _readiness()
            self._json(detail, 200 if ready else 503)
            return
        if url.path == "/":
            self._session_id()           # issue the session cookie up front
            cfg = {"free_chats": config.free_chats(),
                   "require_byok": config.require_byok(),
                   "has_server_key": config.Settings.from_env().usable()}
            from .specialists import SPECIALISTS
            from . import pwa
            page = (PAGE.replace("__OLYMPUS_CFG__", json.dumps(cfg))
                        .replace("__OLYMPUS_NSPEC__", str(len(SPECIALISTS))))
            page = pwa.inject(page)          # make the UI installable
            self._send(200, page.encode(), "text/html; charset=utf-8")
            return
        if url.path in ("/manifest.webmanifest", "/sw.js") \
                or url.path.startswith("/icons/"):
            # PWA assets: public, no auth — the shell must load before login,
            # exactly like "/". Served by the pure-Python pwa layer.
            from . import pwa
            if url.path == "/manifest.webmanifest":
                self._send(200, pwa.manifest().encode(),
                           "application/manifest+json")
                return
            if url.path == "/sw.js":
                self._send(200, pwa.service_worker().encode(),
                           "application/javascript")
                return
            _icons = {"/icons/olympus-192.png": (192, False),
                      "/icons/olympus-512.png": (512, False),
                      "/icons/olympus-maskable-512.png": (512, True)}
            spec = _icons.get(url.path)
            if spec is None:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            self._send(200, pwa.icon(spec[0], maskable=spec[1]), "image/png")
            return
        if url.path in ("/privacy", "/terms"):
            from . import legal               # public: legal pages need no auth
            html = (legal.privacy_html() if url.path == "/privacy"
                    else legal.terms_html())
            self._send(200, html.encode(), "text/html; charset=utf-8")
            return
        if url.path == "/admin":
            # The operator panel SHELL: static HTML/JS with NO data in it.
            # The data lives behind /api/admin below, which carries the real
            # gate — so serving the shell itself is as safe as serving "/".
            from . import adminpanel
            self._send(200, adminpanel.page().encode(),
                       "text/html; charset=utf-8")
            return
        if url.path == "/oauth/google/start":
            self._oauth_start(url)
            return
        if url.path == "/oauth/google/callback":
            self._oauth_callback(url)
            return
        if url.path == "/v1/models":
            # OpenAI-compatible: bearer-gated (its own scheme), not the
            # dashboard's X-Olympus-Token.
            ok, code, msg = self._v1_authorized()
            if not ok:
                self._v1_error(code, msg)
                return
            self._json(openai_server.models_response())
            return
        if not _authorized(self):
            self._json({"error": _unauthorized_message()}, 401)
            return
        if url.path == "/api/admin":
            # Operator overview (read-only). _authorized above already
            # enforced the access token when one is configured; without a
            # token this endpoint is additionally loopback-only — an admin
            # surface must never be open just because auth was left unset.
            ok, code, msg = self._admin_authorized()
            if not ok:
                self._json({"error": msg}, code)
                return
            from . import adminpanel
            self._json(adminpanel.snapshot())
            return
        if url.path == "/api/metrics":
            snap = metrics.snapshot()          # instance ops, not per-user
            snap["sovereignty"] = config.sovereign_status()
            # Build + mode, so a measurement can be tied to the exact code that
            # produced it (PHASE5 §15). No secrets: version, commit, mode only.
            snap["build"] = config.build_info()
            self._json(snap)
            return
        params = parse_qs(url.query)
        sid = self._session_id(params.get("session", [None])[0])
        if url.path == "/api/me":
            sess = accounts.account_for_token(sid)
            self._json({"login_required": accounts.require_login(),
                        "logged_in": sess is not None,
                        "username": sess["username"] if sess else None})
            return
        if url.path == "/api/status":
            since = int(params.get("since", ["0"])[0])
            sess_obj = _session(sid)
            events, base = sess_obj.events, sess_obj.events_base
            # `since` is a global index; translate through the trim offset so the
            # cursor stays correct even after old events were dropped.
            start = max(0, since - base)
            self._json({"events": events[start:], "next": base + len(events),
                        "sovereignty": config.sovereign_status(),
                        "signing": _signing_posture()})
            return
        user = self._principal(sid)
        if user is None:
            self._json({"error": "login required"}, 401)
            return
        if url.path == "/api/actions":
            self._json({"actions": _actions_view(user),
                        "budget": usage.budget_status()})
        elif url.path == "/api/documents":
            from . import documents
            name = params.get("name", [None])[0]
            if name:                              # read one document
                body = documents.read(user, name)
                if body is None:
                    self._json({"error": "not found"}, 404)
                else:
                    self._json({"name": name, "content": body})
            else:                                 # list documents
                self._json({"documents": documents.listing(user)})
        elif url.path == "/api/gallery":
            from . import gallery
            self._json({"images": gallery.list_images(user)})
        elif url.path == "/api/gallery/image":
            from . import gallery
            got = gallery.read_image(params.get("name", [""])[0], user)
            if got is None:
                self._json({"error": "not found"}, 404)
            else:
                raw, ctype = got
                self._send(200, raw, ctype,
                           extra_headers={"Cache-Control": "no-store"})
        elif url.path == "/api/agenda":
            self._json(_agenda_view(user))
        elif url.path == "/api/health":
            from . import health
            self._json(health.report())
        elif url.path == "/api/compare":
            from . import compare
            models = [compare.model_label(m)
                      for m in compare.available_models()]
            out = {"models": models, "tally": compare.tally(user)}
            if len(models) < 2:              # tell the UI how to enable it
                out["snippet"] = compare._SNIPPET
            self._json(out)
        elif url.path == "/api/memory":
            self._json(_memory_view(user))
        elif url.path == "/api/connected":
            from . import google_oauth
            self._json({"configured": google_oauth.configured(),
                        "connected": google_oauth.connected(user)})
        else:
            self._json({"error": "not found"}, 404)

    def _oauth_start(self, url) -> None:
        from . import google_oauth
        sid = self._session_id(parse_qs(url.query).get("session", [None])[0])
        user = accounts.namespace_for_token(sid) or _user_for(sid)
        try:
            state = google_oauth.issue_state(user, sid)
            target = google_oauth.authorize_url(state=state)
        except google_oauth.OAuthError as err:
            self._send(400, str(err).encode(), "text/plain; charset=utf-8")
            return
        self.send_response(302)
        self.send_header("Location", target)
        cookie = getattr(self, "_set_cookie", None)
        if cookie:                       # carry the sid through the round-trip
            self.send_header("Set-Cookie", self._cookie_header(cookie))
            self._set_cookie = None
        self.end_headers()

    def _oauth_callback(self, url) -> None:
        from . import google_oauth
        params = parse_qs(url.query)
        state = params.get("state", [""])[0]
        code = params.get("code", [""])[0]
        rec = google_oauth.consume_state(state)
        # The browser finishing the flow must be the one that started it: the
        # session cookie (sent on this top-level navigation under SameSite=Lax)
        # must match the sid bound to the state at /start. This closes
        # login-CSRF — an attacker-started flow cannot be completed in a
        # victim's browser, and the returned `state` is never trusted as
        # identity on its own.
        m = _SID_RE.search(self.headers.get("Cookie", "") or "")
        cookie_sid = m.group(1) if m else None
        if not rec or not cookie_sid or rec.get("sid") != cookie_sid:
            self._send(400, b"<!doctype html><meta charset=utf-8>"
                       b"<p>Invalid or expired authorization request \xe2\x80\x94 "
                       b"please start the connection again in this browser.</p>",
                       "text/html; charset=utf-8")
            return
        body = "<p>Google account connected. You can close this tab.</p>"
        if not code:
            body = "<p>Connection cancelled or failed.</p>"
        else:
            try:
                google_oauth.exchange_code(rec["user"], code)
            except Exception as err:
                body = f"<p>Could not connect: {err}</p>"
        self._send(200, f"<!doctype html><meta charset=utf-8>{body}".encode(),
                   "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/v1/chat/completions", "/v1/messages"):
            # Both /v1 dialects run the SAME council pipeline as /api/chat, but
            # they returned here before ever reaching the per-IP limiter below,
            # so the most expensive endpoint in the process was the only one
            # with no throttle at all. Same key space and budget as /api/chat.
            if _rate_limited("v1:" + self.client_address[0], _v1_limit()):
                self._v1_error(429, "rate limit exceeded — slow down")
                # Refused before the body was read, so the same HTTP/1.0
                # close-with-unread-bytes reset that could destroy a 401 can
                # destroy this 429. Bounded cleanup, after the response.
                self._drain_refused_body()
                return
        if path == "/v1/chat/completions":
            self._handle_v1_chat()
            return
        if path == "/v1/messages":        # Anthropic dialect, same council path
            self._handle_v1_messages()
            return
        if path not in ("/api/chat", "/api/feedback", "/api/action",
                        "/api/memory", "/api/documents", "/api/compare",
                        "/api/todos", "/api/gallery", "/api/register",
                        "/api/login", "/api/logout", "/api/report",
                        "/api/admin/act"):
            self._json({"error": "not found"}, 404)
            return
        if not _authorized(self):
            self._json({"error": _unauthorized_message()}, 401)
            return
        payload = self._read_json()
        if payload is None:
            self._json({"error": "bad request"}, 400)
            return
        if path == "/api/admin/act":
            # Operator mutations (Phase 2). Same gate as the snapshot, PLUS a
            # required custom header: browsers only attach custom headers after
            # a CORS preflight we never approve, so a hostile page cannot fire
            # cross-origin mutations at a loopback panel (CSRF defense).
            ok, code, msg = self._admin_authorized()
            if not ok:
                self._json({"error": msg}, code)
                return
            if self.headers.get("X-Olympus-Admin") != "1":
                self._json({"error": "missing X-Olympus-Admin header"}, 403)
                return
            from . import adminpanel
            result = adminpanel.act(str(payload.get("op", "")),
                                    payload.get("params") or {})
            self._json(result, 200 if result["ok"] else 400)
            return
        # Cheap write endpoints get a generous per-IP budget (DoS guard); the
        # expensive /api/chat keeps its own stricter limit further down. Auth
        # endpoints MUST be throttled too — they run a costly PBKDF2 per attempt,
        # so an unthrottled /api/login is both password brute-force and CPU DoS.
        if path != "/api/chat" and _rate_limited(
                "w:" + self.client_address[0], 60):
            self._json({"error": "rate limit exceeded — slow down"}, 429)
            return
        if path in ("/api/register", "/api/login", "/api/logout"):
            self._handle_auth(path, payload)
            return
        sid = self._session_id(payload.get("session"))
        session = _session(sid)

        # A problem report works even before login (e.g. "I can't log in") — it
        # only needs the access token, already checked above.
        if path == "/api/report":
            from . import support
            try:
                support.report(str(payload.get("message", "")),
                               user=self._principal(sid) or "anon",
                               contact=str(payload.get("contact", "")),
                               context=str(payload.get("context", "")))
            except ValueError:
                self._json({"error": "please describe the problem"}, 400)
                return
            self._json({"ok": True,
                        "note": "Thanks — your report was sent to the operator."})
            return

        user = self._principal(sid)
        if user is None:
            self._json({"error": "login required"}, 401)
            return

        if path == "/api/feedback":
            if session.bot is None:
                self._json({"error": "nothing to rate yet"}, 400)
                return
            note = session.bot.feedback(str(payload.get("verdict", "up")),
                                        str(payload.get("comment", ""))[:500])
            self._json({"ok": True, "note": note})
            return

        if path == "/api/action":
            op = str(payload.get("op", ""))
            aid = str(payload.get("action_id", ""))
            try:
                if op == "approve":
                    a = actions.approve(user, aid)
                    msg = a.error or "executed"
                elif op == "reject":
                    actions.reject(user, aid, str(payload.get("reason", "")))
                    msg = "rejected"
                elif op == "undo":
                    a = actions.undo(user, aid)
                    msg = a.error or "reversed"
                elif op == "edit":
                    changes = payload.get("changes")
                    if not isinstance(changes, dict):
                        self._json({"error": "changes must be an object"}, 400)
                        return
                    actions.edit(user, aid, changes,
                                 title=payload.get("title"))
                    msg = "edited — still awaiting your approval"
                else:
                    self._json({"error": "unknown op"}, 400)
                    return
            except ValueError as err:
                self._json({"error": str(err)}, 400)
                return
            self._json({"ok": True, "message": msg,
                        "actions": _actions_view(user),
                        "budget": usage.budget_status()})
            return

        if path == "/api/documents":
            # The user editing their OWN document in the UI is a direct action
            # (they clicked Save) — saved immediately, not staged. The agent's
            # write_document tool is what routes through the approval spine.
            from . import documents
            op = str(payload.get("op", "save"))
            name = str(payload.get("name", "")).strip()
            if not name:
                self._json({"error": "a document name is required"}, 400)
                return
            try:
                if op == "save":
                    documents.save(user, name, str(payload.get("content", "")))
                elif op == "delete":
                    documents.delete(user, name)
                else:
                    self._json({"error": "unknown op"}, 400)
                    return
            except ValueError as err:
                self._json({"error": str(err)}, 400)
                return
            self._json({"ok": True, "documents": documents.listing(user)})
            return

        if path == "/api/compare":
            from . import compare
            op = str(payload.get("op", "run"))
            if op == "run":
                prompt = str(payload.get("prompt", "")).strip()
                if not prompt:
                    self._json({"error": "a prompt is required"}, 400)
                    return
                self._json(compare.run(user, prompt))
            elif op == "reveal":
                out = compare.reveal(user, str(payload.get("id", "")),
                                     str(payload.get("choice", "")))
                if out is None:
                    self._json({"error": "comparison not found"}, 404)
                else:
                    self._json(out)
            else:
                self._json({"error": "unknown op"}, 400)
            return

        if path == "/api/gallery":
            from . import gallery
            op = str(payload.get("op", ""))
            if op == "delete":
                ok = gallery.delete_image(str(payload.get("name", "")), user)
                self._json({"ok": ok, "images": gallery.list_images(user)})
            elif op == "edit":
                from . import media
                # edit_image resolves its source through the gallery, which
                # reads memory.current_user(); set it for this request.
                from . import memory
                memory.set_user(user)
                msg = media.edit_image(str(payload.get("prompt", "")),
                                       str(payload.get("name", "")))
                self._json({"ok": msg.startswith("Edited"), "message": msg,
                            "images": gallery.list_images(user)})
            else:
                self._json({"error": "unknown op"}, 400)
            return

        if path == "/api/todos":
            from . import todos
            op = str(payload.get("op", "add"))
            try:
                if op == "add":
                    todos.add(user, str(payload.get("text", "")),
                              kind=str(payload.get("kind", "todo")),
                              due=payload.get("due") or None)
                elif op == "complete":
                    todos.complete(user, str(payload.get("id", "")),
                                   bool(payload.get("done", True)))
                elif op == "delete":
                    todos.delete(user, str(payload.get("id", "")))
                elif op == "clear_done":
                    todos.clear_done(user)
                else:
                    self._json({"error": "unknown op"}, 400)
                    return
            except ValueError as err:
                self._json({"error": str(err)}, 400)
                return
            self._json({"ok": True,
                        "todos": todos.listing(user, include_done=True)})
            return

        if path == "/api/memory":
            from . import usermem, profile, playbooks, relgraph
            kind = str(payload.get("kind", "memory"))
            op = str(payload.get("op", ""))
            mid = str(payload.get("id", ""))
            try:
                if kind == "memory":
                    if op == "approve":
                        c = usermem.pop_candidate(user, mid)
                        if c:
                            usermem.add_memory(
                                user, type=c["type"], content=c["content"],
                                confidence=c.get("confidence", 0.7),
                                key=c.get("key"),
                                importance=c.get("importance", 0.5),
                                sensitivity=c.get("sensitivity", "normal"),
                                provenance=c.get("provenance", []))
                    elif op == "reject":
                        usermem.pop_candidate(user, mid)
                    elif op == "forget":
                        usermem.tombstone(user, mid)
                    else:
                        raise ValueError("unknown op")
                elif kind == "profile":
                    if op == "set":
                        profile.set_about(user, str(payload.get("value", "")))
                    else:
                        raise ValueError("unknown op")
                elif kind == "playbook":
                    if op == "approve":
                        playbooks.approve(user, mid)
                    elif op in ("forget", "reject"):
                        playbooks.delete(user, mid)
                    else:
                        raise ValueError("unknown op")
                elif kind == "graph":
                    if op == "forget":
                        relgraph.forget(user, str(payload.get("label", "")))
                    else:
                        raise ValueError("unknown op")
                else:
                    raise ValueError("unknown kind")
            except ValueError as err:
                self._json({"error": str(err)}, 400)
                return
            self._json({"ok": True, **_memory_view(user)})
            return

        # /api/chat
        if _rate_limited("chat:" + self.client_address[0], _chat_limit()):
            self._json({"error": "rate limit exceeded — slow down"}, 429)
            return
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            self._json({"error": "bad request"}, 400)
            return
        pset = payload.get("settings") or {}
        # Cost policy: BYOK can be a free *allowance* (OLYMPUS_FREE_CHATS) rather
        # than a wall — keyless users get N free chats/day on the operator's key,
        # then continue on their own. With no allowance, OLYMPUS_REQUIRE_BYOK
        # makes it all-or-nothing.
        brought = _brought_own_key(pset)
        # Read-only pre-check, so an exhausted/blocked caller gets the right
        # message before anything is charged. The AUTHORITATIVE consume happens
        # below, after validation, as one atomic check-and-increment.
        decision = _key_decision(brought, _daily_count("free:" + user))
        if decision == "over_free":
            self._json({"error": f"You've used your {config.free_chats()} free "
                                 "chats for today — add your own API key (⚙ model) "
                                 "to keep going.", "need_key": True}, 402)
            return
        if decision == "byok":
            self._json({"error": "This instance requires your own API key. "
                                 "Open ⚙ model and add your provider key.",
                        "need_key": True}, 402)
            return
        # Validate BEFORE charging. The quota comment below has always said a
        # rejected request must not burn quota, but `settings.validate()` used to
        # run *after* the charge, so a 400 here still cost the user a chat (and a
        # free, operator-funded one). Build and validate first; charge second.
        settings = config.Settings.from_env().merged(pset)
        error = settings.validate()
        if error:
            self._json({"error": error}, 400)
            return
        # Charge the per-account daily quota only now that the request has passed
        # validation and the key/free-allowance wall — a rejected request (400 /
        # 402) must not burn one of the user's OLYMPUS_DAILY_CHATS.
        if _daily_limited("u:" + user, config.daily_chat_limit()):
            self._json({"error": "daily limit reached for your account — "
                                 "try again tomorrow."}, 429)
            return
        if not brought and config.free_chats() > 0:
            # A free chat spends the OPERATOR's key, so it needs a limit the
            # caller cannot mint. `user` is `web-<sid>` for an anonymous visitor
            # and the sid is their own cookie, so clearing it used to hand out a
            # fresh allowance indefinitely. The per-IP ceiling is checked first
            # and read-only: it is a coarse abuse ceiling rather than a precise
            # quota, and peeking keeps a legitimate user from being charged for
            # a request the ceiling is about to refuse.
            ip_cap = config.free_chats_per_ip()
            if ip_cap > 0 and _daily_count(
                    "ipfree:" + self.client_address[0]) >= ip_cap:
                self._json({"error": "the free allowance for your network has "
                                     "been used up today — add your own API key "
                                     "(⚙ model) to keep going.",
                            "need_key": True}, 402)
                return
            # Atomic check-and-consume: `_daily_count` above and a later
            # `_daily_bump` were two separate operations, so concurrent requests
            # each read the same pre-charge count and every one of them got a
            # free chat. `_daily_limited` decides and charges under one lock.
            if _daily_limited("free:" + user, config.free_chats()):
                self._json({"error": f"You've used your {config.free_chats()} "
                                     "free chats for today — add your own API "
                                     "key (⚙ model) to keep going.",
                            "need_key": True}, 402)
                return
            _daily_bump("ipfree:" + self.client_address[0])

        # Optional second frontier model — used together with the first, each
        # stage on its strongest. Build a pool; invalid second model is ignored.
        members = [settings]
        extra = pset.get("extra") or {}
        if isinstance(extra, dict) and (extra.get("api_key") or extra.get("model")):
            second = config.Settings(
                provider=(extra.get("provider") or "openai").lower(),
                model=(extra.get("model") or "").strip(),
                api_key=(extra.get("api_key") or "").strip() or None,
                base_url=(extra.get("base_url") or "").strip() or None)
            if second.usable():
                members.append(second)
        pool = config.ModelPool.of(*members)

        want_stream = parse_qs(urlparse(self.path).query).get("stream", ["0"])[0] == "1"
        language = pset.get("language")
        try:
            with session.lock:
                bot = session.bot_for(pool, user=user)
                if isinstance(language, str) and language.strip():
                    bot.set_language(language)
                bot.set_contribute(bool(pset.get("contribute")))
                if want_stream:
                    self._stream_reply(bot, message)
                else:
                    self._json({"reply": bot.ask(message)})
        except usage.AdmissionRefused as err:
            # W2-C7 admission: overload is an honest 429 + Retry-After with a
            # machine-readable reason — never a silent downgrade of the answer
            # (invariant W2-I7.1, ruling R6). Rendered by usage so every
            # channel emits one overload shape.
            code, body, hdrs = usage.admission_http_response(err)
            try:
                self._json(body, code, extra_headers=hdrs)
            except Exception:
                pass                  # a stream already committed its headers
        except Exception as err:
            from . import errors
            errors.capture("web /api/chat", err, context=message[:200])
            try:
                self._json({"error": str(err)}, 500)
            except Exception:
                pass

    # --- OpenAI-compatible inbound endpoint (/v1/*) ----------------------

    def _peer_is_loopback(self) -> bool:
        """Whether the request came from this machine — decided from the kernel
        peer socket (self.client_address[0]) ONLY, never a header. Delegates to
        the module-level `_is_loopback`, the single source of truth."""
        peer = (self.client_address[0] if self.client_address else "") or ""
        return _is_loopback(peer)

    def _bound_to_loopback(self) -> bool:
        """Whether the server socket is bound to a loopback address. If it's
        bound to anything else (0.0.0.0, a LAN IP, ...), the process is reachable
        off-box and 'no key' can't be safe — we don't infer safety from the
        per-connection peer. Defaults to False (treat as exposed) if unknown."""
        try:
            return _is_loopback(self.server.server_address[0])
        except Exception:
            return False

    def _bearer_token(self) -> str:
        auth = self.headers.get("Authorization", "") or ""
        if auth[:7].lower() == "bearer ":
            return auth[7:].strip()
        return ""

    def _admin_authorized(self) -> tuple[bool, int, str]:
        """Gate /api/admin and /api/admin/act on a SEPARATE operator secret.

        This used to return True whenever OLYMPUS_ACCESS_TOKEN was set, on the
        reasoning that the access token IS the operator credential. That holds
        for a single-operator box and is PRIVILEGE ESCALATION BY REGISTRATION
        in the multi-user posture deploy/README.md documents: the access token
        is the entry gate, so every user holds it, and `_authorized` has
        already checked it before this function runs. Any account that could
        log in was therefore a full operator — and /api/admin/act is not
        read-only. It carries `config_set`, `set_autonomy` (on OTHER users),
        approve/reject of pending actions, `schedule_add` and `mcp_add`.
        `accounts.py` has no role field, so there was nothing else to check.

        The operator credential is now OLYMPUS_OPERATOR_TOKEN, presented in its
        own `X-Olympus-Operator` header. It is a different secret in a
        different header from the entry gate, so holding one does not confer
        the other.

        UNSET FAILS CLOSED TO LOOPBACK-ONLY — deliberately NOT back to the
        access token. A fallback would preserve the hole for every operator who
        does not read the changelog, which is precisely the population the fix
        exists for. Remote administration now REQUIRES the operator token.

        The `X-Olympus-Admin: 1` requirement on /api/admin/act is unchanged and
        separate: that is CSRF defence (browsers only attach custom headers
        after a preflight we never approve) and it defends a different thing.
        """
        operator = os.environ.get("OLYMPUS_OPERATOR_TOKEN", "")
        access = os.environ.get("OLYMPUS_ACCESS_TOKEN", "")
        if operator:
            # Setting the two to the same value recreates the escalation while
            # LOOKING configured: every user holds the access token, so an
            # identical operator token hands every account the panel again. A
            # config that is wrong in a way that reads as right is worse than
            # one that is obviously unset, so this refuses rather than serves.
            if access and hmac.compare_digest(operator, access):
                return (False, 500,
                        "OLYMPUS_OPERATOR_TOKEN is set to the same value as "
                        "OLYMPUS_ACCESS_TOKEN. Every user holds the access "
                        "token — it is the entry gate — so an identical "
                        "operator token grants every account full operator "
                        "rights while appearing configured. Set it to a "
                        "different secret.")
            provided = self.headers.get("X-Olympus-Operator", "")
            if hmac.compare_digest(provided, operator):
                return True, 200, ""
            return (False, 403,
                    "the admin panel requires the operator credential in the "
                    "X-Olympus-Operator header. Holding OLYMPUS_ACCESS_TOKEN "
                    "is not sufficient: it is the entry gate every user has.")
        if not self._peer_is_loopback():
            return (False, 403,
                    "the admin panel is loopback-only until "
                    "OLYMPUS_OPERATOR_TOKEN is set.")
        if not self._bound_to_loopback():
            return (False, 401,
                    "set OLYMPUS_OPERATOR_TOKEN: the server is bound to a "
                    "non-loopback address, so the admin panel needs the "
                    "operator token.")
        if _forwarding_headers_present(self.headers):
            return (False, 401,
                    "set OLYMPUS_OPERATOR_TOKEN: a reverse-proxy forwarding "
                    "header is present, so this request is relayed from "
                    "off-box and needs the operator token.")
        return True, 200, ""

    def _v1_credential(self) -> str:
        """The API key presented on a /v1 request, from either header form:
        `Authorization: Bearer <key>` (OpenAI dialect) or `x-api-key`
        (Anthropic dialect). Extraction only — the single constant-time
        comparison stays in `_v1_authorized`, so both dialects authenticate
        through exactly one check (W3-I5)."""
        token = self._bearer_token()
        if token:
            return token
        return str(self.headers.get("x-api-key", "") or "").strip()

    def _v1_authorized(self) -> tuple[bool, int, str]:
        """Gate the /v1/* endpoints. With OLYMPUS_API_KEYS set, require a valid
        key (bearer or `x-api-key`). With none set, serve loopback-only — and
        never an open relay: the remoteness decision comes from the peer socket
        (not headers), and a process bound off-loopback must carry a key even
        for a 'local'-looking peer (a reverse proxy connects from loopback while
        fronting the world). Returns (ok, http_status, message)."""
        keys = config.api_keys()
        if keys:
            token = self._v1_credential()
            if token and any(hmac.compare_digest(token, k) for k in keys):
                return True, 200, ""
            return (False, 401,
                    "missing or invalid API key — pass a configured "
                    "OLYMPUS_API_KEYS value as 'Authorization: Bearer <key>' "
                    "or 'x-api-key: <key>'.")
        # No keys configured: loopback-only, and never an open relay. The
        # peer/header decision funnels through the module-level `_v1_allowed`
        # predicate — the SAME function the boundary tests assert — so the
        # production gate and the test can't drift. Granular status/messages and
        # the bind check are layered on top of that single source of truth.
        peer = (self.client_address[0] if self.client_address else "") or ""
        if not _v1_allowed(peer, self.headers):
            if not _is_loopback(peer):
                return (False, 403,
                        "the OpenAI-compatible API is loopback-only until "
                        "OLYMPUS_API_KEYS is configured (no open relay).")
            return (False, 401,
                    "set OLYMPUS_API_KEYS: a reverse-proxy forwarding header is "
                    "present, so this request is being relayed from off-box and "
                    "needs an API key (no open relay behind a proxy).")
        if not self._bound_to_loopback():
            return (False, 401,
                    "set OLYMPUS_API_KEYS: the server is bound to a non-loopback "
                    "address, so the OpenAI-compatible API needs an API key "
                    "(safety is not inferred from the connection).")
        return True, 200, ""

    def _v1_principal(self) -> str:
        """The memory namespace for this /v1 request.

        `Olympus(user=...)` scopes long-term memory — lessons, corrections,
        profile card, recall, playbooks, relationship graph. Every /v1 request
        used to run as the single literal principal `"api-v1"`, so two holders
        of two different OLYMPUS_API_KEYS shared one namespace: B's answers were
        conditioned on context A had written, and B's `recall` could surface A's
        material. Authentication distinguished the callers; the memory scope
        threw that distinction away.

        Each configured key now gets its own namespace, derived as a
        domain-separated SHA-256 prefix of the key. Properties that matter:
          * one-way — the namespace appears in on-disk paths and in traces, and
            must never be reversible into the credential;
          * stable across restarts — a caller's memory has to survive a bounce,
            so no per-process salt;
          * domain-separated — the digest is useless against any other system
            that hashes the same key.

        The keyless loopback case (no OLYMPUS_API_KEYS, local operator only)
        keeps a single stable namespace: there is exactly one principal, and
        inventing per-request namespaces would silently discard its memory.

        Migration: existing `api-v1` memory is NOT rewritten or deleted. Keyed
        deployments start from an empty per-key namespace — which is the point;
        the old shared pool is the defect being removed. Operators who want the
        old material under a specific key must copy it deliberately."""
        token = self._v1_credential()
        if not token:
            return "api-local"
        digest = hashlib.sha256(
            b"olympus-v1-principal|" + token.encode("utf-8", "replace")
        ).hexdigest()
        return "api-" + digest[:16]

    def _v1_error(self, code: int, message: str) -> None:
        """An OpenAI-shaped error envelope."""
        self._json({"error": {"message": message, "type": "invalid_request_error",
                              "code": None}}, code)

    # --- seams shared by BOTH /v1 dialects (W3-I1: one generation path) ---

    def _v1_bot(self):
        """Build the council bot for a /v1 request — the single entry point
        both dialects use.

        Data-class routing: an X-Olympus-Data-Class header (public/internal/
        restricted) selects the destination policy. `restricted` stays local
        even with sovereign mode off; an unspecified class defaults to
        local-only when sovereign mode is on. Called INSIDE `_v1_run` so a
        sovereignty refusal from `local_only()` is rendered, not raised."""
        data_class = config.normalize_data_class(
            self.headers.get("X-Olympus-Data-Class"))
        pool = config.ModelPool.from_env()
        if config.data_class_local_only(data_class):
            pool = pool.local_only()            # fail-closed if no local member
        return orchestrator.Olympus(pool=pool, user=self._v1_principal())

    @staticmethod
    def _v1_audit_headers(bot) -> dict:
        """Audit headers: let the caller locate and verify the reasoning behind
        this answer — `olympus verify --run <X-Olympus-Run-Id>`."""
        from . import witness
        from . import shadow
        # A shadow response must never be mistakable for a production one
        # (PHASE5 §7). Stamped on BOTH /v1 dialects because both build their
        # audit headers here.
        hdrs = {"X-Olympus-Audit": "signed-" + witness.posture(),
                "X-Olympus-Mode": shadow.mode()}
        run_id = getattr(bot, "last_run_id", None)
        if run_id:
            hdrs["X-Olympus-Run-Id"] = run_id
        return hdrs

    def _v1_run(self, work, *, error, admission_body, label: str,
                context: str = "") -> None:
        """Run a /v1 request body under the shared failure policy.

        One copy of the policy for both dialects (W3-I1) — only the *rendering*
        differs, and that is what `error(code, message)` and
        `admission_body(exc, body)` inject:
          * sovereignty/egress refusal → 403, fail closed, never a downgrade;
          * admission refusal (W2-C7) → 429 + Retry-After + machine-readable
            reason; refusal over degradation (ruling R6), the council never
            silently answers with a cheaper model to fit under load;
          * anything else → captured and rendered as a 500 with no stack trace.
        """
        from . import security
        try:
            work()
        except security.SovereigntyError as err:
            error(403, str(err))
        except usage.AdmissionRefused as err:
            code, body, hdrs = usage.admission_http_response(err)
            try:
                self._json(admission_body(err, body), code, extra_headers=hdrs)
            except Exception:
                pass                  # a stream already committed its headers
        except Exception as err:
            from . import errors
            errors.capture(label, err, context=(context or "")[:200])
            try:
                error(500, str(err))
            except Exception:
                pass

    def _handle_v1_chat(self) -> None:
        ok, code, msg = self._v1_authorized()
        if not ok:
            # Auth stays BEFORE any parsing, and the refusal goes out FIRST.
            # The drain is bounded post-response cleanup so the HTTP/1.0 close
            # emits FIN rather than a reset that would destroy this 401.
            self._v1_error(code, msg)
            self._drain_refused_body()
            return
        payload = self._read_json()
        if not isinstance(payload, dict) or not isinstance(
                payload.get("messages"), list):
            self._v1_error(400, "invalid request: 'messages' must be a list.")
            return
        model = payload.get("model") or openai_server.MODEL_ID
        stream = bool(payload.get("stream"))
        prompt = openai_server.messages_to_prompt(payload["messages"])
        if not prompt.strip():
            self._v1_error(400, "invalid request: no user message content.")
            return

        # Any `model` value maps to the one council pipeline for v1; unsupported
        # params (temperature, tools, ...) are accepted and ignored by design.
        def work() -> None:
            bot = self._v1_bot()
            if stream:
                self._stream_v1(bot, prompt, model)
            else:
                answer = bot.ask(prompt)
                self._json(openai_server.completion_response(
                    answer, model, prompt_text=prompt),
                    extra_headers=self._v1_audit_headers(bot))

        self._v1_run(work, error=self._v1_error,
                     admission_body=lambda _exc, body: body,
                     label="web /v1/chat/completions", context=prompt)

    # --- Anthropic Messages dialect (W3-C5) ------------------------------

    def _anthropic_fail(self, code: int, message: str,
                        kind: str | None = None) -> None:
        """Render a refusal in the NATIVE Anthropic envelope (W3-I4)."""
        self._json(_anthropic_error(kind or _anthropic_error_kind(code),
                                    message), code)

    @staticmethod
    def _anthropic_admission_body(exc, body: dict) -> dict:
        """Re-render an admission refusal in the Anthropic envelope, keeping
        `usage.admission_http_response`'s status and headers (Retry-After,
        X-Olympus-Admission) as the single source of the overload shape."""
        retry = body.get("retry_after_s", getattr(exc, "retry_after_s", 0))
        reason = body.get("reason", getattr(exc, "reason", "no_capacity"))
        detail = body.get("message") or str(exc)
        return _anthropic_error(
            "rate_limit_error",
            f"admission refused ({reason}): {detail} "
            f"Retry after {retry}s. Nothing was downgraded.")

    def _handle_v1_messages(self) -> None:
        """`POST /v1/messages` — the Anthropic Messages dialect.

        A translation layer, not a second server: the request is normalised
        into the same internal message list the OpenAI dialect uses, run
        through the same council entry (`_v1_bot` → `bot.ask`/`ask_stream`),
        and rendered back in Anthropic shape. Auth, admission, sovereignty,
        usage accounting, streamguard and cancellation are the same seams
        (W3-I1)."""
        ok, code, msg = self._v1_authorized()
        if not ok:
            # Same structural hazard as the OpenAI dialect, same treatment:
            # refuse first in the NATIVE Anthropic envelope, then drain within
            # fixed bounds so the close is a FIN and the 401 survives.
            self._anthropic_fail(code, msg)
            self._drain_refused_body()
            return
        payload = self._read_json()
        if payload is None:
            self._anthropic_fail(
                400, "invalid request: body must be JSON and within the "
                     "size cap.")
            return
        try:
            messages, system, opts = _anthropic_to_internal(payload)
        except _AnthropicRefusal as err:
            self._anthropic_fail(err.status, str(err), kind=err.kind)
            return
        # The system prompt rejoins the message list as a `system` turn, so
        # BOTH dialects collapse to a prompt through the one existing function.
        internal = ([{"role": "system", "content": system}] if system else [])
        internal += messages
        prompt = openai_server.messages_to_prompt(internal)
        if not prompt.strip():
            self._anthropic_fail(400,
                                 "invalid request: no user message content.")
            return
        model, max_tokens = opts["model"], opts["max_tokens"]

        def work() -> None:
            bot = self._v1_bot()
            if opts["stream"]:
                self._stream_v1_messages(bot, prompt, model,
                                         max_tokens=max_tokens)
            else:
                answer = bot.ask(prompt)
                text, truncated = _anthropic_cap(answer, max_tokens)
                # The council does not surface structured tool calls at this
                # boundary today, so `tool_use` is False here; the mapping
                # itself lives in the pure helper and is tested there.
                stop = _anthropic_stop_reason(
                    truncated=truncated,
                    tool_use=bool(getattr(bot, "last_tool_calls", None)))
                self._json(
                    _internal_to_anthropic(
                        text, model=model, stop_reason=stop,
                        usage=openai_server.usage_block(prompt, text)),
                    extra_headers=self._v1_audit_headers(bot))

        self._v1_run(work, error=self._anthropic_fail,
                     admission_body=self._anthropic_admission_body,
                     label="web /v1/messages", context=prompt)

    # --- C8 degenerate-stream defense at the HTTP egress seam (W2-PR14) ---
    #
    # `llm.stream_text` already guards the ANTHROPIC provider stream. These two
    # handlers are the last seam before bytes leave the process, and they see
    # every streamed answer regardless of which provider produced it — including
    # the non-Anthropic branch of `orchestrator._synthesize_stream`, which has
    # no provider-level guard at all. Wiring here is therefore not a duplicate:
    # it is the only place a degenerate stream from any other backend can be
    # stopped before the client reads it as a finished reply.
    #
    # What this seam carries is TEXT — the council yields answer fragments, not
    # provider events — so the text detectors (token loop, empty progress,
    # whitespace flood, invalid unicode) are what run here. The event-order,
    # tool-delta and usage detectors need provider events, which this seam
    # genuinely does not have; they are reached at the `openai_compat`
    # response-parse seam instead. Saying so is the point: a guard wired where
    # the information isn't would be theatre.

    def _guarded_pieces(self, pieces, *, model: str):
        """Yield the council's answer fragments through a C8 monitor.

        Flag off ⇒ `monitor()` returns an inert NullMonitor whose `feed()`
        cannot raise, so this generator yields exactly the fragments it was
        given, in order, and the response is byte-identical to the unwired one
        (A17 rollback).

        On a trip the offending fragment is NOT written (it is fed before it is
        yielded), the pathology is recorded as provider-decay evidence, and a
        DISCLOSURE fragment is yielded in its place before the stream ends —
        the user gets a reply that says it is unfinished, never a truncated one
        that looks complete (W2-I8.3)."""
        guard = streamguard.monitor(provider="olympus-web", model=model)
        for piece in pieces:
            try:
                guard.feed(piece)
            except streamguard.StreamPathology as exc:
                yield self._disclose_stream_abort(guard, exc, model)
                return
            yield piece

    @staticmethod
    def _disclose_stream_abort(guard, exc, model: str) -> str:
        """Record the pathology and render the user-visible notice. Mirrors the
        wording committed in `orchestrator._synthesize_stream` so one abort
        reads the same on every channel. Evidence writing never raises."""
        kind = getattr(exc, "kind", "pathology")
        try:
            streamguard.record_pathology(streamguard.pathology_record(
                guard, exc, provider="olympus-web", model=model))
        except Exception:                 # noqa: BLE001 — evidence is best-effort
            pass
        return ("\n\n[Response incomplete: the output stream was aborted by "
                f"the degenerate-stream guard ({kind}). The text above is "
                "unfinished and unverified.]")

    def _sse_open(self) -> None:
        """Commit the SSE response headers — one seam for both /v1 dialects."""
        from . import witness
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # Posture is known up front; the run id is only assigned once the run
        # completes, so it isn't available as a header on the streamed response
        # (use /api/status or a non-streaming request to obtain it).
        self.send_header("X-Olympus-Audit", "signed-" + witness.posture())
        self.end_headers()

    def _sse_write(self, frames) -> None:
        for frame in frames:
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()

    def _stream_v1(self, bot, prompt: str, model: str) -> None:
        self._sse_open()
        # Guarding the PIECES (not the SSE frames) keeps the envelope
        # well-formed: a disclosure is just another content delta, so the stop
        # frame and the `[DONE]` terminator still close the stream and no client
        # sees a half-written event.
        pieces = self._guarded_pieces(bot.ask_stream(prompt), model=model)
        self._sse_write(openai_server.stream_events(pieces, model))

    def _stream_v1_messages(self, bot, prompt: str, model: str, *,
                            max_tokens: int) -> None:
        """The Anthropic streaming dialect (W3-I6) over the SAME seams as
        `_stream_v1`: same council entry (`bot.ask_stream`), same
        streamguard-wrapped pieces, same SSE headers. Only the event framing
        differs, and that lives in the pure `_anthropic_sse` generator — so a
        guard trip arrives as a disclosure `text_delta` and the envelope is
        still closed by `content_block_stop`/`message_delta`/`message_stop`
        (W2-I8.3)."""
        self._sse_open()
        pieces = self._guarded_pieces(bot.ask_stream(prompt), model=model)
        self._sse_write(_anthropic_sse(
            pieces, model=model, max_tokens=max_tokens,
            input_tokens=openai_server.estimate_tokens(prompt)))

    def _stream_reply(self, bot, message: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in self._guarded_pieces(bot.ask_stream(message),
                                          model="council"):
            if chunk:
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()

    def log_message(self, *args) -> None:  # silence default request logging
        pass


def serve(host: str = "127.0.0.1", port: int = 8484) -> None:
    # Boot invariant (P5-A2): a staging profile that cannot serve safely must
    # not start at all. Raises StagingConfigError listing EVERY problem, so the
    # operator fixes the profile in one pass instead of one restart per typo.
    # No-op unless OLYMPUS_ENV names staging — dev and production are unchanged.
    config.require_staging_config(bind_host=host)

    server = ThreadingHTTPServer((host, port), Handler)
    # An in-flight council run can hold a socket for a long time; without this,
    # a container stop waits for the daemon-thread pool and SIGKILLs at the end
    # of the grace period, mid-journal-append. daemon_threads=False + an
    # explicit shutdown lets `server_close()` join them.
    server.daemon_threads = False
    info = config.build_info()
    print(f"⚡ Olympus web UI: http://{host}:{port}  (Ctrl-C to stop)")
    print(f"   build: v{info['version']} {info['commit'][:12]} env={info['env']}")
    print(f"   OpenAI-compatible API: http://{host}:{port}/v1  "
          + ("(bearer-gated via OLYMPUS_API_KEYS)" if config.api_keys()
             else "(loopback-only — set OLYMPUS_API_KEYS to expose it)"))
    if os.environ.get("OLYMPUS_ACCESS_TOKEN"):
        print("   access token required (OLYMPUS_ACCESS_TOKEN is set)")

    _install_shutdown(server)
    try:
        server.serve_forever()
    finally:
        # Reached on SIGTERM/SIGINT via _install_shutdown, or on any error.
        # server_close() joins the request threads, so a run that is mid-append
        # finishes its write instead of being torn in half — the journal's
        # torn-tail path exists for crashes, not for routine restarts.
        server.server_close()


def _install_shutdown(server) -> None:
    """Stop `server` on SIGTERM/SIGINT so a container stop is graceful.

    `docker stop` and every orchestrator send SIGTERM first and SIGKILL after a
    grace period. Python's default SIGTERM handling terminates immediately, so
    without this an in-flight request dies wherever it happened to be.

    Best-effort by design: signal handlers can only be installed on the main
    thread, and `serve()` is also called from worker threads in tests and from
    the TUI. A failure to install is not a reason to refuse to serve — it just
    means shutdown stays as abrupt as it was before."""
    import signal

    def _stop(signum, _frame):
        print(f"\n… received {signal.Signals(signum).name}, "
              f"finishing in-flight requests")
        # shutdown() blocks until serve_forever() exits, so it must not run on
        # the signal-handler stack — that would deadlock against the very loop
        # it is trying to stop.
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError, AttributeError):
            pass                        # not the main thread, or not POSIX
