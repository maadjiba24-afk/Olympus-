"""Olympus Matrix channel — zero dependencies, raw client-server API.

Matrix is an open, federated chat network (matrix.org and thousands of
homeservers). Olympus joins as a bot user and talks over the client-server
HTTP API with `urllib` — no SDK, in keeping with the dependency-free channels.

Setup:
  1. Register a bot user on your homeserver and get an access token.
  2. export MATRIX_HOMESERVER=https://matrix.org
     export MATRIX_ACCESS_TOKEN=syt_...
  3. python -m olympus matrix
  4. Invite the bot to a room, then pair: run `olympus pair matrix` and send
     the bot `/pair <code>` — senders are untrusted by default.

Optional:
  OLYMPUS_MATRIX_DM_POLICY=pairing|allowlist|open   (default: pairing)
  OLYMPUS_MATRIX_ALLOWED_IDS=@you:server,@me:server  always-allowed senders
  OLYMPUS_MATRIX_NOTIFY_ROOM=!room:server            heartbeat pushes here

Transport only: this module long-polls /sync, extracts text messages, and
delegates access control + routing to chanbase/gateway (the same council
pipeline every channel shares).
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import chanbase, orchestrator  # noqa: F401  (orchestrator: type parity)

_API = "/_matrix/client/v3"
_limiter = chanbase.RateLimiter(per_minute=12)


def _cfg() -> tuple[str, str]:
    from . import secretref
    base = os.environ.get("MATRIX_HOMESERVER", "").rstrip("/")
    token = secretref.getenv("MATRIX_ACCESS_TOKEN")
    return base, token


def _require() -> tuple[str, str]:
    base, token = _cfg()
    if not base or not token:
        raise SystemExit("Set MATRIX_HOMESERVER and MATRIX_ACCESS_TOKEN "
                         "(register a bot user on your homeserver).")
    return base, token


def _req(base: str, token: str, method: str, path: str,
         params: dict | None = None, body: dict | None = None,
         timeout: int = 60) -> dict:
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def whoami(base: str, token: str) -> str:
    return _req(base, token, "GET", f"{_API}/account/whoami").get("user_id", "")


def send(base: str, token: str, room_id: str, text: str) -> None:
    """Send a plain-text message to a room (chunked to a sane size). The txn id
    keeps retries idempotent on the server side."""
    text = (text or "").strip() or "(empty reply)"
    for i in range(0, len(text), 8000):
        txn = f"olympus-{int(time.time()*1000)}-{i}"
        path = (f"{_API}/rooms/{urllib.parse.quote(room_id)}"
                f"/send/m.room.message/{txn}")
        _req(base, token, "PUT", path,
             body={"msgtype": "m.text", "body": text[i:i + 8000]})


def notify(text: str) -> bool:
    """Push to the configured notify room (used by the heartbeat). Best-effort."""
    base, token = _cfg()
    room = os.environ.get("OLYMPUS_MATRIX_NOTIFY_ROOM", "")
    if not base or not token or not room:
        return False
    try:
        send(base, token, room, text)
        return True
    except Exception as err:
        import logging
        logging.getLogger("olympus.matrix").warning("notify failed: %s", err)
        return False


def extract_messages(sync: dict, self_id: str) -> list[tuple[str, str, str]]:
    """Pull (room_id, sender, body) for each m.text message in a /sync payload,
    skipping the bot's own messages. Pure — the unit-testable core."""
    out = []
    rooms = ((sync.get("rooms") or {}).get("join") or {})
    for room_id, room in rooms.items():
        for ev in ((room.get("timeline") or {}).get("events") or []):
            if ev.get("type") != "m.room.message":
                continue
            sender = ev.get("sender", "")
            if sender == self_id or not sender:
                continue
            content = ev.get("content") or {}
            if content.get("msgtype") != "m.text":
                continue
            body = (content.get("body") or "").strip()
            if body:
                out.append((room_id, sender, body))
    return out


class _RoomWorker(threading.Thread):
    """One serial worker per room: ordered within a room, concurrent across."""

    def __init__(self, base, token, bots, room_id):
        super().__init__(daemon=True)
        self.base, self.token, self.bots, self.room_id = base, token, bots, room_id
        self.q: queue.Queue = queue.Queue()

    def run(self):
        while True:
            sender, text = self.q.get()
            send_fn = lambda t: send(self.base, self.token, self.room_id, t)
            try:
                chanbase.route("matrix", "mx", sender, text, self.bots,
                               send_fn, limiter=_limiter)
            except Exception:
                import traceback
                traceback.print_exc()


def run_bot() -> None:
    base, token = _require()
    self_id = whoami(base, token)
    print(f"⚡ Olympus is live on Matrix as {self_id}  (Ctrl-C to stop)")
    if chanbase.policy("matrix") == "pairing":
        print("   DM policy: pairing (untrusted by default). "
              "Run `olympus pair matrix` to let a sender in.")
    bots: dict = {}
    workers: dict[str, _RoomWorker] = {}
    since = ""
    # Skip the backlog on first sync: only react to messages from now on.
    try:
        since = _req(base, token, "GET", f"{_API}/sync",
                     {"timeout": 0}).get("next_batch", "")
    except Exception:
        since = ""
    while True:
        try:
            params = {"timeout": 30000}
            if since:
                params["since"] = since
            sync = _req(base, token, "GET", f"{_API}/sync", params, timeout=60)
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(5)
            continue
        since = sync.get("next_batch", since)
        for room_id, sender, body in extract_messages(sync, self_id):
            w = workers.get(room_id)
            if w is None:
                w = workers[room_id] = _RoomWorker(base, token, bots, room_id)
                w.start()
            w.q.put((sender, body))
