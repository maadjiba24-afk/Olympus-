"""Olympus web interface — zero dependencies, pure standard library.

`python -m olympus web` then open http://localhost:8484. A single-page chat
UI streams pipeline progress (which god is working) while the council answers.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import orchestrator

_LOCK = threading.Lock()          # one pipeline run at a time (shared history)
_EVENTS: list[str] = []           # pipeline progress feed
_BOT: orchestrator.Olympus | None = None


def _bot() -> orchestrator.Olympus:
    global _BOT
    if _BOT is None:
        _BOT = orchestrator.Olympus(report=_EVENTS.append)
    return _BOT


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
  header span { color: #6b7280; font-size: 13px; font-style: italic; }
  #log { flex: 1; overflow-y: auto; padding: 20px; max-width: 860px;
         width: 100%; margin: 0 auto; }
  .msg { margin: 0 0 16px; line-height: 1.55; white-space: pre-wrap;
         word-wrap: break-word; }
  .user { color: #9fb4d0; }
  .user::before { content: "you ▸ "; color: #51607a; }
  .bot::before { content: "olympus ▸ "; color: #d9b44a; }
  .sys { color: #6b7280; font-size: 13px; font-style: italic; margin: 4px 0; }
  form { display: flex; gap: 10px; padding: 16px 20px; max-width: 860px;
         width: 100%; margin: 0 auto; }
  input { flex: 1; background: #161b24; color: #e8e3d8; font: inherit;
          border: 1px solid #2a2f3a; border-radius: 8px; padding: 12px 14px; }
  input:focus { outline: none; border-color: #d9b44a; }
  button { background: #d9b44a; color: #0e1116; border: 0; border-radius: 8px;
           padding: 0 22px; font: inherit; font-weight: bold; cursor: pointer; }
  button:disabled { opacity: .4; cursor: wait; }
</style>
</head>
<body>
<header><h1>OLYMPUS</h1><span>main agent · supervisor · hallucination
controller · 10 specialists</span></header>
<div id="log"><p class="sys">The council is assembled. Ask anything.</p></div>
<form id="f">
  <input id="q" autocomplete="off" placeholder="Ask the council..." autofocus>
  <button id="b" type="submit">Send</button>
</form>
<script>
const log = document.getElementById('log'), f = document.getElementById('f'),
      q = document.getElementById('q'), b = document.getElementById('b');
let seen = 0;
function add(cls, text) {
  const p = document.createElement('p');
  p.className = cls; p.textContent = text;
  log.appendChild(p); log.scrollTop = log.scrollHeight;
  return p;
}
async function poll() {
  try {
    const r = await fetch('/api/status?since=' + seen);
    const d = await r.json();
    d.events.forEach(e => add('sys', e));
    seen = d.next;
  } catch (e) {}
}
f.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const text = q.value.trim();
  if (!text) return;
  add('msg user', text);
  q.value = ''; b.disabled = true; q.disabled = true;
  const timer = setInterval(poll, 1200);
  try {
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text})
    });
    const d = await r.json();
    clearInterval(timer); await poll();
    add('msg bot', d.reply || d.error || '(no reply)');
  } catch (e) {
    clearInterval(timer);
    add('sys', 'error: ' + e);
  } finally {
    b.disabled = false; q.disabled = false; q.focus();
  }
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif url.path == "/api/status":
            since = int(parse_qs(url.query).get("since", ["0"])[0])
            self._json({"events": _EVENTS[since:], "next": len(_EVENTS)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/chat":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            message = json.loads(self.rfile.read(length))["message"]
        except Exception:
            self._json({"error": "bad request"}, 400)
            return
        try:
            with _LOCK:
                reply = _bot().ask(message)
            self._json({"reply": reply})
        except Exception as err:
            self._json({"error": str(err)}, 500)

    def log_message(self, *args) -> None:  # silence default request logging
        pass


def serve(host: str = "127.0.0.1", port: int = 8484) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"⚡ Olympus web UI: http://{host}:{port}  (Ctrl-C to stop)")
    server.serve_forever()
