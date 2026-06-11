"""Olympus web interface — zero dependencies, pure standard library.

`python -m olympus web` then open http://localhost:8484.

- BYOK: the ⚙ panel lets each visitor pick provider/model/key (kept in their
  browser, used in-memory per request, never stored or logged).
- Each browser gets a private memory namespace and a conversation that
  persists across server restarts.
- Abuse protection: per-IP rate limit (OLYMPUS_RATE_LIMIT/min, default 8) and
  an optional shared access token (OLYMPUS_ACCESS_TOKEN) for hosted instances.
- 👍/👎 on every answer feeds the daily learning cycle.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, orchestrator


class _Session:
    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.lock = threading.Lock()
        self.events: list[str] = []
        self.fingerprint: tuple | None = None
        self.bot: orchestrator.Olympus | None = None

    def bot_for(self, settings: config.Settings) -> orchestrator.Olympus:
        fp = (settings.provider, settings.model, settings.api_key,
              settings.base_url)
        if self.bot is None or fp != self.fingerprint:
            self.fingerprint = fp
            self.bot = orchestrator.Olympus(
                report=self.events.append,
                settings=settings,
                user=f"web-{self.sid}",
                conversation_id=f"web-{self.sid}",
            )
        return self.bot


_SESSIONS: dict[str, _Session] = {}
_SESSIONS_LOCK = threading.Lock()

# per-IP sliding-window rate limiter
_HITS: dict[str, deque] = {}
_HITS_LOCK = threading.Lock()


def _session(sid: str) -> _Session:
    with _SESSIONS_LOCK:
        if sid not in _SESSIONS:
            if len(_SESSIONS) > 500:  # crude cap against unbounded growth
                _SESSIONS.clear()
            _SESSIONS[sid] = _Session(sid)
        return _SESSIONS[sid]


def _rate_limited(ip: str) -> bool:
    limit = int(os.environ.get("OLYMPUS_RATE_LIMIT", "8"))
    if limit <= 0:
        return False
    now = time.time()
    with _HITS_LOCK:
        hits = _HITS.setdefault(ip, deque())
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= limit:
            return True
        hits.append(now)
    return False


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    required = os.environ.get("OLYMPUS_ACCESS_TOKEN")
    if not required:
        return True
    return handler.headers.get("X-Olympus-Token", "") == required


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
</style>
</head>
<body>
<header>
  <h1>OLYMPUS</h1>
  <span>main agent · supervisor · hallucination controller · 11 specialists</span>
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
  <div class="row">
    <label>Access</label>
    <input id="access" type="password"
           placeholder="instance access token (only if the host set one)">
  </div>
  <p class="hint">Your key lives in this browser only and is sent solely with
  your own requests; the server never stores or logs it. Leave everything
  blank to use the server's configured model.</p>
</div>
<div id="log"><p class="sys">The council is assembled. Ask anything — and rate
answers with 👍/👎 so Olympus learns.</p></div>
<form id="f">
  <button id="attach" type="button" title="Attach a text/CSV/code file">📎</button>
  <input id="file" type="file" hidden>
  <input id="q" class="q" autocomplete="off" placeholder="Ask the council..." autofocus>
  <button id="b" class="send" type="submit">Send</button>
</form>
<script>
const log = document.getElementById('log'), f = document.getElementById('f'),
      q = document.getElementById('q'), b = document.getElementById('b'),
      panel = document.getElementById('panel'),
      fileIn = document.getElementById('file'),
      attach = document.getElementById('attach');
const fields = ['provider', 'model', 'key', 'base', 'access'];
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
    base_url: document.getElementById('base').value
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
f.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  let text = q.value.trim();
  if (!text && !attached) return;
  if (attached) {
    text += '\\n\\n[Attached file: ' + attached.name + ']\\n```\\n' +
            attached.text.slice(0, 100000) + '\\n```';
  }
  add('msg user', q.value.trim() + (attached ? '  📎 ' + attached.name : ''));
  q.value = ''; clearFile(); b.disabled = true; q.disabled = true;
  const timer = setInterval(poll, 1200);
  try {
    const r = await fetch('/api/chat?stream=1', {
      method: 'POST', headers: hdrs(),
      body: JSON.stringify({message: text, session: session, settings: cfg()})
    });
    clearInterval(timer); await poll();
    if (r.ok && r.body && r.headers.get('Content-Type') === 'text/plain') {
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
    }
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

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length))
        except Exception:
            return None

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if not _authorized(self):
            self._json({"error": "missing or wrong access token"}, 401)
            return
        if url.path == "/api/status":
            params = parse_qs(url.query)
            since = int(params.get("since", ["0"])[0])
            sid = params.get("session", ["default"])[0][:64]
            events = _session(sid).events
            self._json({"events": events[since:], "next": len(events)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/api/chat", "/api/feedback"):
            self._json({"error": "not found"}, 404)
            return
        if not _authorized(self):
            self._json({"error": "missing or wrong access token"}, 401)
            return
        payload = self._read_json()
        if payload is None:
            self._json({"error": "bad request"}, 400)
            return
        sid = str(payload.get("session") or uuid.uuid4())[:64]
        session = _session(sid)

        if path == "/api/feedback":
            if session.bot is None:
                self._json({"error": "nothing to rate yet"}, 400)
                return
            note = session.bot.feedback(str(payload.get("verdict", "up")),
                                        str(payload.get("comment", ""))[:500])
            self._json({"ok": True, "note": note})
            return

        # /api/chat
        if _rate_limited(self.client_address[0]):
            self._json({"error": "rate limit exceeded — slow down"}, 429)
            return
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            self._json({"error": "bad request"}, 400)
            return
        settings = config.Settings.from_env().merged(payload.get("settings") or {})
        error = settings.validate()
        if error:
            self._json({"error": error}, 400)
            return

        want_stream = parse_qs(urlparse(self.path).query).get("stream", ["0"])[0] == "1"
        try:
            with session.lock:
                bot = session.bot_for(settings)
                if want_stream:
                    self._stream_reply(bot, message)
                else:
                    self._json({"reply": bot.ask(message)})
        except Exception as err:
            try:
                self._json({"error": str(err)}, 500)
            except Exception:
                pass

    def _stream_reply(self, bot, message: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in bot.ask_stream(message):
            if chunk:
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()

    def log_message(self, *args) -> None:  # silence default request logging
        pass


def serve(host: str = "127.0.0.1", port: int = 8484) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"⚡ Olympus web UI: http://{host}:{port}  (Ctrl-C to stop)")
    if os.environ.get("OLYMPUS_ACCESS_TOKEN"):
        print("   access token required (OLYMPUS_ACCESS_TOKEN is set)")
    server.serve_forever()
