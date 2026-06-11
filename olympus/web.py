"""Olympus web interface — zero dependencies, pure standard library.

`python -m olympus web` then open http://localhost:8484.

Bring-your-own-key: the ⚙ panel lets each visitor pick a provider
(Anthropic or any OpenAI-compatible endpoint), model, and API key. Keys are
kept in the visitor's browser (localStorage), sent only with their own
requests, used in-memory, and never logged or written to disk.
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, orchestrator


class _Session:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[str] = []
        self.fingerprint: tuple | None = None
        self.bot: orchestrator.Olympus | None = None

    def bot_for(self, settings: config.Settings) -> orchestrator.Olympus:
        fp = (settings.provider, settings.model, settings.api_key,
              settings.base_url)
        if self.bot is None or fp != self.fingerprint:
            self.fingerprint = fp
            self.bot = orchestrator.Olympus(report=self.events.append,
                                            settings=settings)
        return self.bot


_SESSIONS: dict[str, _Session] = {}
_SESSIONS_LOCK = threading.Lock()


def _session(sid: str) -> _Session:
    with _SESSIONS_LOCK:
        if sid not in _SESSIONS:
            if len(_SESSIONS) > 500:  # crude cap against unbounded growth
                _SESSIONS.clear()
            _SESSIONS[sid] = _Session()
        return _SESSIONS[sid]


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
  .msg { margin: 0 0 16px; line-height: 1.55; white-space: pre-wrap;
         word-wrap: break-word; }
  .user { color: #9fb4d0; }
  .user::before { content: "you ▸ "; color: #51607a; }
  .bot::before { content: "olympus ▸ "; color: #d9b44a; }
  .sys { color: #6b7280; font-size: 13px; font-style: italic; margin: 4px 0; }
  form { display: flex; gap: 10px; padding: 16px 20px; max-width: 860px;
         width: 100%; margin: 0 auto; }
  input.q { flex: 1; background: #161b24; color: #e8e3d8; font: inherit;
            border: 1px solid #2a2f3a; border-radius: 8px; padding: 12px 14px; }
  input.q:focus { outline: none; border-color: #d9b44a; }
  button.send { background: #d9b44a; color: #0e1116; border: 0;
                border-radius: 8px; padding: 0 22px; font: inherit;
                font-weight: bold; cursor: pointer; }
  button.send:disabled { opacity: .4; cursor: wait; }
</style>
</head>
<body>
<header>
  <h1>OLYMPUS</h1>
  <span>main agent · supervisor · hallucination controller · 10 specialists</span>
  <button id="gear" title="Bring your own model & key">⚙ model</button>
</header>
<div id="panel">
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
  <p class="hint">Your key lives in this browser only and is sent solely with
  your own requests; the server never stores or logs it. Leave everything
  blank to use the server's configured model.</p>
</div>
<div id="log"><p class="sys">The council is assembled. Ask anything.</p></div>
<form id="f">
  <input id="q" class="q" autocomplete="off" placeholder="Ask the council..." autofocus>
  <button id="b" class="send" type="submit">Send</button>
</form>
<script>
const log = document.getElementById('log'), f = document.getElementById('f'),
      q = document.getElementById('q'), b = document.getElementById('b'),
      panel = document.getElementById('panel');
const fields = ['provider', 'model', 'key', 'base'];
fields.forEach(id => {
  const el = document.getElementById(id);
  el.value = localStorage.getItem('olympus_' + id) || '';
  el.addEventListener('change',
    () => localStorage.setItem('olympus_' + id, el.value));
});
document.getElementById('gear').onclick = () => panel.classList.toggle('open');
let session = localStorage.getItem('olympus_session');
if (!session) {
  session = (crypto.randomUUID ? crypto.randomUUID() :
             String(Math.random()).slice(2));
  localStorage.setItem('olympus_session', session);
}
let seen = 0;
function cfg() {
  return {
    provider: document.getElementById('provider').value,
    model: document.getElementById('model').value,
    api_key: document.getElementById('key').value,
    base_url: document.getElementById('base').value
  };
}
function add(cls, text) {
  const p = document.createElement('p');
  p.className = cls; p.textContent = text;
  log.appendChild(p); log.scrollTop = log.scrollHeight;
  return p;
}
async function poll() {
  try {
    const r = await fetch('/api/status?since=' + seen +
                          '&session=' + encodeURIComponent(session));
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
      body: JSON.stringify({message: text, session: session, settings: cfg()})
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
            params = parse_qs(url.query)
            since = int(params.get("since", ["0"])[0])
            sid = params.get("session", ["default"])[0][:64]
            events = _session(sid).events
            self._json({"events": events[since:], "next": len(events)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/chat":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            message = payload["message"]
        except Exception:
            self._json({"error": "bad request"}, 400)
            return

        sid = str(payload.get("session") or uuid.uuid4())[:64]
        settings = config.Settings.from_env().merged(payload.get("settings") or {})
        error = settings.validate()
        if error:
            self._json({"error": error}, 400)
            return

        session = _session(sid)
        try:
            with session.lock:
                reply = session.bot_for(settings).ask(message)
            self._json({"reply": reply})
        except Exception as err:
            self._json({"error": str(err)}, 500)

    def log_message(self, *args) -> None:  # silence default request logging
        pass


def serve(host: str = "127.0.0.1", port: int = 8484) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"⚡ Olympus web UI: http://{host}:{port}  (Ctrl-C to stop)")
    server.serve_forever()
