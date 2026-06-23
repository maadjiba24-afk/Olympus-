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
import re
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import accounts, actions, builtin_actions, config, metrics, orchestrator, usage  # noqa: F401


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
    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.lock = threading.Lock()
        self.events: list[str] = []
        self.fingerprint: tuple | None = None
        self.bot: orchestrator.Olympus | None = None

    def bot_for(self, pool: config.ModelPool,
                user: str | None = None) -> orchestrator.Olympus:
        user = user or f"web-{self.sid}"
        fp = tuple((m.provider, m.model, m.api_key, m.base_url)
                   for m in pool.members) + (user,)
        if self.bot is None or fp != self.fingerprint:
            self.fingerprint = fp
            self.bot = orchestrator.Olympus(
                report=self.events.append,
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


def _resolve_sid(handler: BaseHTTPRequestHandler,
                 provided: str | None) -> tuple[str, str | None]:
    """Determine the session id, preferring a server-issued HttpOnly cookie so
    browser users get an unguessable, isolated session automatically (no shared
    'default' namespace, no manual session strings). Falls back to an explicit
    `session` value for programmatic/CLI use. Returns (sid, cookie_to_set)."""
    m = _SID_RE.search(handler.headers.get("Cookie", "") or "")
    if m:
        return m.group(1), None
    if provided:
        return provided[:64], None
    sid = uuid.uuid4().hex                       # fresh, random, server-issued
    return sid, sid


def _session(sid: str) -> _Session:
    with _SESSIONS_LOCK:
        if sid not in _SESSIONS:
            if len(_SESSIONS) > 500:  # crude cap against unbounded growth
                _SESSIONS.clear()
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
</style>
</head>
<body>
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
  <span>main agent · supervisor · hallucination controller · 12 specialists</span>
  <button id="connect" title="Connect your Google account" style="display:none">🔗 connect Google</button>
  <button id="actbtn" title="Actions awaiting your approval">📋 actions
    <span id="actcount"></span></button>
  <button id="membtn" title="What Olympus remembers about you">🧠 memory
    <span id="memcount"></span></button>
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
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        try:
            metrics.record_response(urlparse(self.path).path, code)
        except Exception:
            pass
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        cookie = getattr(self, "_set_cookie", None)
        if cookie:
            self.send_header("Set-Cookie",
                             f"olympus_sid={cookie}; HttpOnly; SameSite=Strict; "
                             "Path=/; Max-Age=31536000")
            self._set_cookie = None       # set once per response
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > _MAX_BODY:        # reject oversized bodies (DoS guard)
                return None
            return json.loads(self.rfile.read(length))
        except Exception:
            return None

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
            # Liveness probe for load balancers / uptime checks — no auth, no
            # data, just "the process is serving".
            self._json({"status": "ok",
                        "uptime_seconds": metrics.snapshot()["uptime_seconds"]})
            return
        if url.path == "/":
            self._session_id()           # issue the session cookie up front
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if url.path == "/oauth/google/start":
            self._oauth_start(url)
            return
        if url.path == "/oauth/google/callback":
            self._oauth_callback(url)
            return
        if not _authorized(self):
            self._json({"error": "missing or wrong access token"}, 401)
            return
        if url.path == "/api/metrics":
            self._json(metrics.snapshot())     # instance ops, not per-user
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
            events = _session(sid).events
            self._json({"events": events[since:], "next": len(events)})
            return
        user = self._principal(sid)
        if user is None:
            self._json({"error": "login required"}, 401)
            return
        if url.path == "/api/actions":
            self._json({"actions": _actions_view(user),
                        "budget": usage.budget_status()})
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
        try:
            target = google_oauth.authorize_url(state=sid)
        except google_oauth.OAuthError as err:
            self._send(400, str(err).encode(), "text/plain; charset=utf-8")
            return
        self.send_response(302)
        self.send_header("Location", target)
        self.end_headers()

    def _oauth_callback(self, url) -> None:
        from . import google_oauth
        params = parse_qs(url.query)
        sid = self._session_id(params.get("state", [None])[0])
        code = params.get("code", [""])[0]
        body = "<p>Google account connected. You can close this tab.</p>"
        if not code:
            body = "<p>Connection cancelled or failed.</p>"
        else:
            try:
                google_oauth.exchange_code(
                    accounts.namespace_for_token(sid) or _user_for(sid), code)
            except Exception as err:
                body = f"<p>Could not connect: {err}</p>"
        self._send(200, f"<!doctype html><meta charset=utf-8>{body}".encode(),
                   "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/api/chat", "/api/feedback", "/api/action",
                        "/api/memory", "/api/register", "/api/login",
                        "/api/logout", "/api/report"):
            self._json({"error": "not found"}, 404)
            return
        if not _authorized(self):
            self._json({"error": "missing or wrong access token"}, 401)
            return
        payload = self._read_json()
        if payload is None:
            self._json({"error": "bad request"}, 400)
            return
        if path in ("/api/register", "/api/login", "/api/logout"):
            self._handle_auth(path, payload)
            return
        # Cheap write endpoints get a generous per-IP budget (DoS guard); the
        # expensive /api/chat keeps its own stricter limit further down.
        if path != "/api/chat" and _rate_limited(
                "w:" + self.client_address[0], 60):
            self._json({"error": "rate limit exceeded — slow down"}, 429)
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
        settings = config.Settings.from_env().merged(pset)
        error = settings.validate()
        if error:
            self._json({"error": error}, 400)
            return

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
