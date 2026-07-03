"""The operator admin panel — Phase 1: see everything, change nothing.

`/admin` (served by web.py) is a read-only overview of a running Olympus
instance: what's configured, what's healthy, what's pending, what the
autonomous loop is doing and when it runs next. It exists because every
one of those answers previously lived in a different CLI command on the
box — this is the single pane of glass, and deliberately NOT a settings
editor (mutations stay behind the CLI and the approval spine until a
later phase).

Security posture:
  * `snapshot()` is strictly read-only — it calls the same read paths the
    CLI surfaces use and never mutates state.
  * Secrets never appear in the payload: credentials are reported as
    booleans ("configured"), pool members expose provider/model/host only.
  * The HTML shell contains no data; the browser fetches `/api/admin`,
    which web.py gates on the operator access token (or loopback when no
    token is configured — same posture as the /v1 endpoint).

Every section collector is isolated: one broken subsystem reports an
`error` string for its section instead of taking down the whole panel.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable
from urllib.parse import urlparse

from . import config


def _host_only(url: str | None) -> str | None:
    """Reduce a base_url to scheme://host[:port] — never userinfo/path."""
    if not url:
        return None
    try:
        p = urlparse(url)
        host = p.hostname or ""
        port = f":{p.port}" if p.port else ""
        return f"{p.scheme}://{host}{port}" if host else None
    except (ValueError, AttributeError):
        return None


def _section(fn: Callable[[], Any]):
    """Run one collector; a failure becomes visible data, not a 500."""
    try:
        return fn()
    except Exception as err:                       # pragma: no cover - defensive
        return {"error": f"{type(err).__name__}: {str(err)[:200]}"}


# --- collectors -------------------------------------------------------------

def _instance() -> dict:
    from . import __version__, firstrun, metrics
    snap = metrics.snapshot()
    return {
        "version": __version__,
        "provider_configured": firstrun.configured(),
        "memory_dir": str(config.MEMORY_DIR),
        "uptime_seconds": snap.get("uptime_seconds"),
        "requests": snap.get("requests"),
        "client_errors": snap.get("client_errors"),
        "server_errors": snap.get("server_errors"),
    }


def _flags() -> dict:
    return {
        "fast_mode": config.fast_mode(),
        "sovereign_mode": config.sovereign_mode(),
        "egress_guard": config.egress_guard_enabled(),
        "contracts": config.contracts_enabled(),
        "require_byok": config.require_byok(),
        "require_login": _require_login(),
        "prompt_cache_ttl": config.prompt_cache_ttl(),
        "fallback_enabled": os.environ.get("OLYMPUS_FALLBACK", "1").lower()
        not in ("0", "false", "no", "off"),
        "access_token_set": bool(os.environ.get("OLYMPUS_ACCESS_TOKEN")),
        "api_keys": len(config.api_keys()),
    }


def _require_login() -> bool:
    from . import accounts
    return accounts.require_login()


def _models() -> dict:
    pool = config.ModelPool.from_env()
    members = [{
        "provider": m.provider,
        "model": m.model or "(env default)",
        "has_key": bool(m.api_key),
        "base_url": _host_only(m.base_url),
        "local": config.member_is_local(m),
    } for m in pool.members]
    roles = {}
    for role in ("reasoning", "coding", "verify"):
        s = pool.for_role(role)
        roles[role] = f"{s.provider}/{s.model or '(env default)'}"
    fastest = pool.fastest()
    return {
        "members": members,
        "roles": roles,
        "fastest": f"{fastest.provider}/{fastest.model or '(env default)'}",
        "moa": pool.primary().provider == "moa",
    }


def _budget() -> dict:
    from . import usage
    out = dict(usage.budget_status())
    out["per_model_today"] = _usage_today_models()
    return out


def _usage_today_models() -> list[dict]:
    """Per-model rows from today's usage ledger (read-only file read)."""
    day = time.strftime("%Y-%m-%d")
    path = config.MEMORY_DIR / "usage" / f"{day}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for key, row in sorted(data.items()):
        if key.startswith("model:") and isinstance(row, dict):
            out.append({"model": key[len("model:"):],
                        "calls": row.get("calls", 0),
                        "in_tokens": row.get("in", 0),
                        "out_tokens": row.get("out", 0),
                        "cost": round(float(row.get("cost", 0)), 4)})
    return out


def _channels() -> dict:
    """Configured/not per platform — booleans only, never values."""
    env = os.environ.get
    from . import gmail
    return {
        "telegram": {"configured": bool(env("TELEGRAM_BOT_TOKEN")),
                     "notify": bool(env("TELEGRAM_NOTIFY_CHAT_ID")),
                     "allowlist": bool(env("TELEGRAM_ALLOWED_CHAT_IDS"))},
        "discord": {"configured": bool(env("DISCORD_PUBLIC_KEY")),
                    "notify": bool(env("DISCORD_WEBHOOK_URL"))},
        "slack": {"configured": bool(env("SLACK_SIGNING_SECRET")),
                  "notify": bool(env("SLACK_BOT_TOKEN")
                                 and env("SLACK_NOTIFY_CHANNEL"))},
        "whatsapp": {"configured": bool(env("WHATSAPP_ACCESS_TOKEN")
                                        and env("WHATSAPP_PHONE_NUMBER_ID")),
                     "allowlist": bool(env("WHATSAPP_ALLOWED_NUMBERS"))},
        "signal": {"configured": bool(env("SIGNAL_NUMBER")),
                   "notify": bool(env("SIGNAL_NOTIFY_RECIPIENT"))},
        "email": {"configured": gmail.configured(),
                  "smtp_send": bool(env("SMTP_HOST")),
                  "send_allowlist": bool(env("OLYMPUS_EMAIL_ALLOWLIST"))},
        "webhooks": {"configured": bool(env("OLYMPUS_WEBHOOKS"))},
    }


def _heartbeat() -> dict:
    from . import goals, hibernate, memory, scheduler
    state = memory.load_state()
    now = time.time()
    cycles = []
    for name, interval in sorted(hibernate._cadences().items()):
        last = float(state.get(name, 0.0))
        cycles.append({
            "name": name,
            "interval_seconds": interval,
            "last_run": last or None,
            "due_in_seconds": max(0, round(interval - (now - last))),
        })
    sched_due = scheduler.next_due_in(now)
    goal_due = goals.next_due_in(now)
    return {
        "cycles": cycles,
        "scheduler_due_in": None if sched_due is None else round(sched_due),
        "goals_due_in": None if goal_due is None else round(goal_due),
        "next_wake_seconds": round(hibernate.next_due_in(state, now)),
    }


def _goals() -> list[dict]:
    from . import goals
    out = []
    for g in goals._load():
        last = g.progress[-1]["note"][:160] if g.progress else None
        out.append({"id": g.id, "user": g.user, "status": g.status,
                    "text": g.text[:200], "checks": g.checks,
                    "created": g.created, "last_progress": last,
                    "evidence": (g.evidence or "")[:160] or None})
    return out


def _approvals() -> dict:
    from . import actions
    held = [{
        "id": a.id, "user": a.user, "type": a.type, "status": a.status,
        "title": a.title[:120], "risk": a.risk_class,
        "reversible": a.reversible, "why": (a.why or "")[:160],
        "created_at": a.created_at,
    } for a in actions.pending_all()]
    return {"pending": held, "autonomy_default": actions.DEFAULT_AUTONOMY}


def _skills() -> dict:
    from . import skills
    items = skills.list_all()
    return {"count": len(items),
            "provisional": sum(1 for s in items if s["provisional"]),
            "items": [{"name": s["name"], "specialist": s["specialist"],
                       "provisional": s["provisional"],
                       "updated": s["updated"],
                       "description": s["description"][:140]}
                      for s in items]}


def _schedules() -> list[dict]:
    from . import scheduler
    return [{"name": j.name, "interval_seconds": j.interval,
             "prompt": j.prompt[:140], "deliver_to": j.deliver_to or None,
             "user": j.user, "enabled": j.enabled,
             "last_run": j.last_run or None}
            for j in scheduler._load()]


def _connectors() -> dict:
    from . import connectors
    allowed = connectors.action_allowlist()
    servers = [{
        "name": s.name, "type": s.type,
        "url": _host_only(s.url),
        "active": s.type != "action" or s.name in allowed,
        "specialists": list(s.specialists) or ["all"],
        "auth": bool(s.auth_env),
    } for s in connectors.mcp_servers()]
    connectors.load_plugins()
    plugins = [{"name": p.name, "action": p.action,
                "specialists": list(p.specialists) or ["all"]}
               for p in connectors._PLUGINS.values()]
    hooks = {event: len(connectors._HOOKS.get(event, ()))
             for event in connectors.HOOK_EVENTS
             if connectors._HOOKS.get(event)}
    return {"mcp_servers": servers, "plugins": plugins, "hooks": hooks}


def _security() -> dict:
    status = dict(config.sovereign_status())
    # Belt and braces: the sovereign snapshot lists pool members — make sure
    # only provider/model/host shapes ride along, never keys.
    status.pop("members", None)
    return {
        "sovereign": status,
        "egress_allowlist_size": len(config.egress_allowlist()),
        "egress_guard": config.egress_guard_enabled(),
    }


def _config() -> dict:
    from . import opconfig
    return {"vault_ready": opconfig.vault_ready(),
            "sections": opconfig.SECTIONS,
            "settings": opconfig.status()}


def _errors() -> list[dict]:
    from . import errors
    recent = errors.recent(10)
    return [{"ts": e.get("ts"), "where": e.get("where"),
             "error": str(e.get("error", ""))[:200]}
            for e in recent[-10:]]


def _dashboard() -> dict:
    from . import dashboard
    s = dashboard.summary()
    return {"spend_today": s.get("spend_today"),
            "daily_budget": s.get("daily_budget"),
            "reports_total": s.get("reports_total"),
            "errors_total": s.get("errors_total"),
            "latest_report": s.get("latest_report"),
            "latest_error": s.get("latest_error"),
            "cost_policy": s.get("cost_policy")}


def snapshot() -> dict:
    """The whole read-only operator overview, one JSON-able dict."""
    return {
        "generated_at": time.time(),
        "instance": _section(_instance),
        "flags": _section(_flags),
        "models": _section(_models),
        "budget": _section(_budget),
        "channels": _section(_channels),
        "heartbeat": _section(_heartbeat),
        "goals": _section(_goals),
        "approvals": _section(_approvals),
        "skills": _section(_skills),
        "schedules": _section(_schedules),
        "connectors": _section(_connectors),
        "config": _section(_config),
        "security": _section(_security),
        "health": _section(_dashboard),
        "errors": _section(_errors),
    }


# --- the HTML shell ---------------------------------------------------------
# Contains NO data: the browser fetches /api/admin with the operator access
# token (reused from the main UI's saved "access" setting) and renders
# client-side. On 401/403 it shows a token prompt and stores the token under
# the same localStorage key the chat UI uses.

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Olympus — operator panel</title>
<style>
:root { --bg:#14120f; --card:#1d1a15; --line:#2c2820; --fg:#e8e2d4;
        --dim:#9a917d; --gold:#d9b44a; --bad:#d96a4a; --ok:#7db464; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.5 Georgia, 'Times New Roman', serif; padding:24px; }
h1 { font-weight:normal; letter-spacing:.12em; }
h1 span { color:var(--gold); }
h2 { font-size:1.05em; letter-spacing:.08em; color:var(--gold);
     border-bottom:1px solid var(--line); padding-bottom:6px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr));
        gap:16px; max-width:1400px; }
.card { background:var(--card); border:1px solid var(--line);
        border-radius:10px; padding:14px 18px; }
table { width:100%; border-collapse:collapse; font-size:.92em; }
td, th { text-align:left; padding:3px 8px 3px 0; vertical-align:top; }
th { color:var(--dim); font-weight:normal; }
tr + tr td { border-top:1px solid var(--line); }
.ok { color:var(--ok); } .off { color:var(--dim); } .bad { color:var(--bad); }
.badge { border:1px solid var(--line); border-radius:6px; padding:0 6px;
         font-size:.85em; color:var(--dim); }
.muted { color:var(--dim); font-size:.9em; }
#gate { max-width:420px; margin:80px auto; text-align:center; display:none; }
#gate input { width:100%; padding:8px; background:var(--bg); color:var(--fg);
              border:1px solid var(--line); border-radius:6px; }
#gate button { margin-top:10px; padding:8px 22px; background:var(--gold);
               border:0; border-radius:6px; cursor:pointer; }
#err { color:var(--bad); }
button.act { background:none; border:1px solid var(--line); color:var(--fg);
             border-radius:6px; padding:1px 10px; cursor:pointer;
             font:inherit; font-size:.85em; }
button.act:hover { border-color:var(--gold); color:var(--gold); }
button.act.danger:hover { border-color:var(--bad); color:var(--bad); }
input.act, select.act { background:var(--bg); color:var(--fg); padding:3px 6px;
             border:1px solid var(--line); border-radius:6px; font:inherit;
             font-size:.85em; }
#flash { position:fixed; bottom:18px; right:18px; max-width:440px;
         background:var(--card); border:1px solid var(--gold);
         border-radius:8px; padding:10px 14px; display:none; }
#flash.bad { border-color:var(--bad); }
</style></head><body>
<h1>&#9889; <span>OLYMPUS</span> &mdash; operator panel
  <span class="badge" id="stamp"></span></h1>
<p class="muted">Approvals, goals, schedules and maintenance can be driven
from here; configuration still changes via the CLI.</p>
<div id="flash"></div>
<div id="gate">
  <p>This instance requires the operator access token.</p>
  <input id="tok" type="password" placeholder="OLYMPUS_ACCESS_TOKEN">
  <button onclick="saveTok()">Unlock</button>
  <p id="err"></p>
</div>
<div class="grid" id="grid" style="display:none"></div>
<script>
const KEY = 'olympus_access';
function saveTok() {
  localStorage.setItem(KEY, document.getElementById('tok').value.trim());
  load();
}
function esc(x) { const d = document.createElement('div');
  d.textContent = x == null ? '' : String(x); return d.innerHTML; }
function yn(b, on, off) { return b ? '<span class="ok">' + (on||'yes') + '</span>'
                                  : '<span class="off">' + (off||'no') + '</span>'; }
function ago(ts) { if (!ts) return '<span class="off">never</span>';
  const s = Math.max(0, Date.now()/1000 - ts);
  if (s < 90) return Math.round(s) + 's ago';
  if (s < 5400) return Math.round(s/60) + 'm ago';
  if (s < 172800) return (s/3600).toFixed(1) + 'h ago';
  return (s/86400).toFixed(1) + 'd ago'; }
function dur(s) { if (s == null) return '&mdash;';
  if (s < 90) return Math.round(s) + 's';
  if (s < 5400) return Math.round(s/60) + 'm';
  if (s < 172800) return (s/3600).toFixed(1) + 'h';
  return (s/86400).toFixed(1) + 'd'; }
function table(rows) { return '<table>' + rows.join('') + '</table>'; }
function tr(k, v) { return '<tr><th>' + k + '</th><td>' + v + '</td></tr>'; }
function card(title, body) {
  return '<div class="card"><h2>' + title + '</h2>' + body + '</div>'; }

function flash(msg, bad) {
  const el = document.getElementById('flash');
  el.textContent = msg; el.className = bad ? 'bad' : '';
  el.style.display = 'block';
  clearTimeout(el._t); el._t = setTimeout(() => el.style.display = 'none', 6000);
}
async function act(op, params) {
  const h = {'Content-Type': 'application/json', 'X-Olympus-Admin': '1'};
  const tok = localStorage.getItem(KEY) || '';
  if (tok) h['X-Olympus-Token'] = tok;
  let r, body;
  try {
    r = await fetch('/api/admin/act', {method: 'POST', headers: h,
      body: JSON.stringify({op: op, params: params || {}})});
    body = await r.json();
  } catch (e) { flash('request failed: ' + e, true); return; }
  flash(body.message || body.error || ('HTTP ' + r.status), !body.ok);
  if (body.ok) load();
}
function btn(label, op, params, danger) {
  return '<button class="act' + (danger ? ' danger' : '') +
    '" onclick=\\'act("' + op + '", ' +
    JSON.stringify(params || {}).replace(/'/g, '&#39;') + ')\\'>' +
    label + '</button>';
}
function val(id) { return (document.getElementById(id) || {value:''}).value.trim(); }

function render(d) {
  const g = [];
  const i = d.instance || {}, f = d.flags || {};
  g.push(card('Instance', table([
    tr('version', esc(i.version)),
    tr('provider key', yn(i.provider_configured, 'configured', 'MISSING')),
    tr('uptime', dur(i.uptime_seconds)),
    tr('requests', esc(i.requests) + ' <span class="muted">(' +
       esc(i.client_errors) + ' 4xx / ' + esc(i.server_errors) + ' 5xx)</span>'),
    tr('memory dir', '<span class="muted">' + esc(i.memory_dir) + '</span>'),
  ])));
  g.push(card('Flags', table([
    tr('sovereign mode', yn(f.sovereign_mode, 'ON', 'off')),
    tr('egress guard', yn(f.egress_guard, 'ON', 'off')),
    tr('output contracts', yn(f.contracts, 'ON', 'off')),
    tr('fast mode', yn(f.fast_mode, 'ON', 'off')),
    tr('require BYOK', yn(f.require_byok, 'ON', 'off')),
    tr('require login', yn(f.require_login, 'ON', 'off')),
    tr('prompt cache TTL', esc(f.prompt_cache_ttl)),
    tr('provider fallback', yn(f.fallback_enabled, 'on', 'OFF')),
    tr('access token', yn(f.access_token_set, 'set', 'not set')),
    tr('v1 API keys', esc(f.api_keys)),
  ])));
  const m = d.models || {};
  g.push(card('Model pool' + (m.moa ? ' <span class="badge">MoA</span>' : ''),
    table((m.members || []).map(x => tr(
      esc(x.provider) + '/' + esc(x.model),
      yn(x.has_key || x.provider !== 'openai', 'key', 'no key') +
      (x.local ? ' <span class="badge">local</span>' : '') +
      (x.base_url ? ' <span class="muted">' + esc(x.base_url) + '</span>' : '')
    ))) + '<p class="muted">roles: ' +
    Object.entries(m.roles || {}).map(([r, v]) =>
      esc(r) + ' &rarr; ' + esc(v)).join(' &middot; ') +
    '<br>fastest: ' + esc(m.fastest) + '</p>'));
  const b = d.budget || {};
  g.push(card('Budget &amp; spend', table([
    tr('today', '$' + Number(b.spent || 0).toFixed(4)),
    tr('daily budget', b.enabled ? '$' + esc(b.limit) +
       (b.exceeded ? ' <span class="bad">EXCEEDED</span>' : '') : 'no cap'),
  ].concat((b.per_model_today || []).map(r => tr(
    '<span class="muted">' + esc(r.model) + '</span>',
    r.calls + ' calls &middot; ' + r.in_tokens + '&rarr;' + r.out_tokens +
    ' tok &middot; $' + r.cost)))));
  const ch = d.channels || {};
  g.push(card('Channels', table(Object.entries(ch).map(([name, v]) => tr(
    esc(name), yn(v.configured, 'configured', 'not configured') +
    Object.entries(v).filter(([k]) => k !== 'configured')
      .map(([k, val]) => ' <span class="badge">' + esc(k) + ': ' +
           (val ? 'yes' : 'no') + '</span>').join('')
  )))));
  const hb = d.heartbeat || {};
  g.push(card('Autonomous loop',
    table((hb.cycles || []).map(c => tr(esc(c.name),
      'every ' + dur(c.interval_seconds) + ' &middot; last ' +
      ago(c.last_run) + ' &middot; due in ' + dur(c.due_in_seconds)))) +
    '<p class="muted">next wake: ' + dur(hb.next_wake_seconds) +
    (hb.scheduler_due_in != null ?
      ' &middot; scheduled task due in ' + dur(hb.scheduler_due_in) : '') +
    (hb.goals_due_in != null ?
      ' &middot; goal cycle due in ' + dur(hb.goals_due_in) : '') + '</p>'));
  const goals = d.goals || [];
  g.push(card('Standing goals (' + goals.length + ')',
    (goals.length ? table(goals.map(x => tr(
      '[' + esc(x.id) + '] <span class="badge">' + esc(x.status) + '</span>',
      esc(x.text) + (x.last_progress ? '<br><span class="muted">last: ' +
        esc(x.last_progress) + '</span>' : '') +
      (x.evidence ? '<br><span class="muted">' + esc(x.evidence) + '</span>'
                  : '') +
      (x.status === 'active' ? '<br>' +
        btn('mark done', 'goal_done', {id: x.id}) + ' ' +
        btn('drop', 'goal_drop', {id: x.id}, true) : '')))) :
      '<p class="muted">none</p>') +
    '<p><input class="act" id="goal_text" size="26" ' +
    'placeholder="new goal"> <input class="act" id="goal_contract" ' +
    'size="20" placeholder="done means (optional)"> ' +
    '<button class="act" onclick="act(\\'goal_add\\', {text: val(\\'goal_text\\'),' +
    ' contract: val(\\'goal_contract\\')})">add goal</button></p>'));
  const ap = d.approvals || {}; const held = ap.pending || [];
  g.push(card('Pending approvals (' + held.length + ')',
    (held.length ? table(held.map(a => tr(
      esc(a.type) + ' <span class="badge">' + esc(a.risk) + '</span>',
      esc(a.title) + '<br><span class="muted">' + esc(a.user) + ' &middot; ' +
      ago(a.created_at) + (a.why ? ' &middot; ' + esc(a.why) : '') +
      '</span>' + (a.status === 'prepared' ? '<br>' +
        btn('approve &amp; execute', 'approve_action',
            {user: a.user, id: a.id}) + ' ' +
        btn('deny', 'reject_action', {user: a.user, id: a.id}, true) : '')))) :
      '<p class="muted">nothing waiting</p>')));
  const sk = d.skills || {};
  g.push(card('Skills (' + esc(sk.count) + ', ' + esc(sk.provisional) +
    ' provisional)',
    table((sk.items || []).slice(0, 30).map(s => tr(
      esc(s.name) + (s.provisional ? ' <span class="badge">prov</span>' : ''),
      '<span class="muted">' + esc(s.specialist || 'all') + ' &middot; ' +
      esc(s.description) + '</span>')))));
  const sc = d.schedules || [];
  g.push(card('Scheduled tasks (' + sc.length + ')',
    (sc.length ? table(sc.map(j => tr(
      esc(j.name) + (j.enabled ? '' : ' <span class="off">(off)</span>'),
      'every ' + dur(j.interval_seconds) + ' &middot; ' + esc(j.prompt) +
      (j.deliver_to ? ' &rarr; ' + esc(j.deliver_to) : '') +
      '<br><span class="muted">last ' + ago(j.last_run) + '</span><br>' +
      (j.enabled ? btn('disable', 'schedule_disable', {name: j.name})
                 : btn('enable', 'schedule_enable', {name: j.name})) + ' ' +
      btn('remove', 'schedule_remove', {name: j.name}, true)))) :
      '<p class="muted">none</p>') +
    '<p><input class="act" id="sch_name" size="10" placeholder="name"> ' +
    '<input class="act" id="sch_interval" size="8" placeholder="daily"> ' +
    '<input class="act" id="sch_prompt" size="24" placeholder="prompt"> ' +
    '<button class="act" onclick="act(\\'schedule_add\\', ' +
    '{name: val(\\'sch_name\\'), interval: val(\\'sch_interval\\') || \\'daily\\',' +
    ' prompt: val(\\'sch_prompt\\')})">add task</button></p>'));
  g.push(card('Maintenance',
    '<p>' + btn('run skill gate now', 'run_gate') + ' ' +
    btn('run curation now', 'run_curate') + ' ' +
    btn('run backup now', 'run_backup') + '</p>' +
    '<p class="muted">long jobs run in the background; results land in ' +
    'reports/health.</p>' +
    '<p><input class="act" id="aut_user" size="14" placeholder="user"> ' +
    '<select class="act" id="aut_level">' +
    '<option value="0">L0 suggest</option><option value="1" selected>' +
    'L1 prepare</option><option value="2">L2 approve-to-act</option>' +
    '<option value="3">L3 auto safe</option><option value="4">L4 standing' +
    '</option></select> ' +
    '<button class="act" onclick="act(\\'set_autonomy\\', ' +
    '{user: val(\\'aut_user\\'), level: val(\\'aut_level\\')})">set autonomy' +
    '</button></p>'));
  const co = d.connectors || {};
  g.push(card('Connectors',
    table((co.mcp_servers || []).map(s => tr(
      'MCP: ' + esc(s.name),
      (s.active ? '<span class="ok">active</span>'
                : '<span class="off">inert</span>') +
      ' <span class="badge">' + esc(s.type) + '</span> ' +
      '<span class="muted">' + esc(s.url) + ' &middot; ' +
      esc((s.specialists || []).join(', ')) + '</span> ' +
      btn('remove', 'mcp_remove', {name: s.name}, true)))
    .concat((co.plugins || []).map(p => tr(
      'plugin: ' + esc(p.name),
      '<span class="badge">' + (p.action ? 'action' : 'data') + '</span> ' +
      '<span class="muted">' + esc((p.specialists || []).join(', ')) +
      '</span>')))) +
    (Object.keys(co.hooks || {}).length ? '<p class="muted">hooks: ' +
      Object.entries(co.hooks).map(([k, n]) => esc(k) + '&times;' + n)
        .join(' &middot; ') + '</p>' : '') +
    '<p><input class="act" id="mcp_name" size="8" placeholder="name"> ' +
    '<input class="act" id="mcp_url" size="22" placeholder="https://..."> ' +
    '<select class="act" id="mcp_type"><option value="data">data</option>' +
    '<option value="action">action</option></select> ' +
    '<input class="act" id="mcp_auth" size="12" ' +
    'placeholder="auth env NAME (opt)"> ' +
    '<button class="act" onclick="act(\\'mcp_add\\', {name: val(\\'mcp_name\\'),' +
    ' url: val(\\'mcp_url\\'), type: val(\\'mcp_type\\'),' +
    ' auth_env: val(\\'mcp_auth\\')})">add MCP server</button></p>' +
    '<p class="muted">definitions are security-scanned on save and live in ' +
    'every process immediately; action servers stay inert until allowlisted ' +
    '(OLYMPUS_MCP_ACTION_ALLOWLIST in Configuration).</p>'));
  const cf = d.config || {};
  const secTitles = {channels: 'Channels', email: 'Email &amp; webhooks',
                     models: 'Model pool', connectors: 'Connector policy'};
  (cf.sections || []).forEach(sec => {
    const rows = (cf.settings || []).filter(s => s.section === sec);
    if (!rows.length) return;
    g.push(card('Configure: ' + (secTitles[sec] || esc(sec)),
      table(rows.map(s => {
        const id = 'cfg_' + s.key;
        const src = s.set
          ? '<span class="ok">' + esc(s.source || 'set') + '</span>'
          : '<span class="off">unset</span>';
        const shadowed = (s.source || '').indexOf('shadows') >= 0
          ? ' <span class="bad">shadowed by process env</span>' : '';
        const restart = s.consumers && s.consumers.length
          ? '<br><span class="muted">restart to apply: ' +
            esc(s.consumers.join(', ')) + '</span>' : '';
        return tr(esc(s.label) + '<br><span class="muted">' + esc(s.key) +
          '</span>',
          src + shadowed + '<br>' +
          '<input class="act" id="' + id + '" size="24" type="' +
          (s.secret ? 'password' : 'text') + '" placeholder="' +
          (s.secret ? (s.set ? 'set — enter to replace' : 'enter value')
                    : esc(s.value != null ? s.value : 'enter value')) + '"> ' +
          '<button class="act" onclick=\\'act("config_set", {key: "' + s.key +
          '", value: val("' + id + '")})\\'>set</button> ' +
          '<button class="act danger" onclick=\\'act("config_unset", ' +
          '{key: "' + s.key + '"})\\'>clear</button>' + restart);
      })) +
      (sec === 'models' && !cf.vault_ready
        ? '<p class="bad">Secrets need OLYMPUS_SECRET_KEY set on the server ' +
          'before they can be stored (encrypted vault). Non-secret settings ' +
          'work now.</p>' : '')));
  });
  const se = d.security || {}, he = d.health || {};
  g.push(card('Security posture', table([
    tr('sovereign', yn((se.sovereign || {}).sovereign, 'ON', 'off')),
    tr('egress allowlist', esc(se.egress_allowlist_size) + ' entries'),
    tr('egress guard', yn(se.egress_guard, 'ON', 'off')),
  ])));
  const errs = d.errors || [];
  g.push(card('Recent errors (' + (he.errors_total != null ?
      esc(he.errors_total) + ' total' : errs.length) + ')',
    errs.length ? table(errs.map(e => tr(ago(e.ts),
      esc(e.where) + ': <span class="muted">' + esc(e.error) + '</span>'))) :
    '<p class="muted">none recorded</p>'));
  document.getElementById('grid').innerHTML = g.join('');
  document.getElementById('grid').style.display = 'grid';
  document.getElementById('gate').style.display = 'none';
  document.getElementById('stamp').textContent =
    new Date(d.generated_at * 1000).toLocaleString();
}

async function load() {
  const h = {};
  const tok = localStorage.getItem(KEY) || '';
  if (tok) h['X-Olympus-Token'] = tok;
  let r;
  try { r = await fetch('/api/admin', {headers: h}); }
  catch (e) {
    document.getElementById('err').textContent = 'unreachable: ' + e;
    document.getElementById('gate').style.display = 'block'; return;
  }
  if (r.status === 401 || r.status === 403) {
    document.getElementById('grid').style.display = 'none';
    document.getElementById('gate').style.display = 'block';
    document.getElementById('err').textContent =
      tok ? 'That token was refused.' : '';
    return;
  }
  render(await r.json());
}
load();
setInterval(load, 30000);
</script></body></html>
"""


def page() -> str:
    """The data-free HTML shell for /admin."""
    return PAGE


# --- Phase 2: act on running state -------------------------------------------
#
# A small, closed set of operations — each one is a code path the CLI already
# exposes to the operator, surfaced behind the same gate as the snapshot.
# Approving a held action here IS the approval spine's human step (the panel
# is where the human says yes); nothing bypasses a gate that existed before.
# Configuration (credentials, channels, models) is deliberately NOT here —
# that's Phase 3.

def _act_approve(p: dict) -> str:
    from . import actions
    a = actions.approve(str(p.get("user", "")), str(p.get("id", "")))
    return f"Approved and executed '{a.title}' ({a.status})."


def _act_reject(p: dict) -> str:
    from . import actions
    a = actions.reject(str(p.get("user", "")), str(p.get("id", "")),
                       str(p.get("reason", "") or "declined from admin panel"))
    return f"Rejected '{a.title}'."


def _act_goal_add(p: dict) -> str:
    from . import goals
    return goals.add(str(p.get("user") or "admin"),
                     str(p.get("text", "")), str(p.get("contract", "")))


def _act_goal_drop(p: dict) -> str:
    from . import goals
    return goals.set_status(str(p.get("id", "")), "dropped")


def _act_goal_done(p: dict) -> str:
    from . import goals
    return goals.set_status(str(p.get("id", "")), "done",
                            evidence="closed manually from the admin panel")


def _act_schedule_add(p: dict) -> str:
    from . import scheduler
    name = str(p.get("name", "")).strip()
    prompt = str(p.get("prompt", "")).strip()
    if not name or not prompt:
        raise ValueError("schedule_add needs 'name' and 'prompt'")
    job = scheduler.add(name, str(p.get("interval", "daily")), prompt,
                        deliver_to=str(p.get("deliver_to", "")),
                        user=str(p.get("user") or "admin"))
    return f"Scheduled '{job.name}'."


def _act_schedule_remove(p: dict) -> str:
    from . import scheduler
    ok = scheduler.remove(str(p.get("name", "")))
    if not ok:
        raise ValueError(f"no schedule named '{p.get('name')}'")
    return f"Removed schedule '{p.get('name')}'."


def _act_schedule_enabled(p: dict, on: bool) -> str:
    from . import scheduler
    ok = scheduler.set_enabled(str(p.get("name", "")), on)
    if not ok:
        raise ValueError(f"no schedule named '{p.get('name')}'")
    return f"Schedule '{p.get('name')}' {'enabled' if on else 'disabled'}."


def _act_set_autonomy(p: dict) -> str:
    from . import actions
    user = str(p.get("user", "")).strip()
    if not user:
        raise ValueError("set_autonomy needs 'user'")
    return actions.set_autonomy(user, int(p.get("level", 1)))


def _spawn(label: str, fn) -> str:
    """Run a long maintenance job off-thread; the panel is not a job queue,
    so we return immediately and the result lands in memory/reports (visible
    on the next snapshot refresh)."""
    import threading

    def runner():
        try:
            fn()
        except Exception as err:                   # pragma: no cover
            try:
                from . import errors
                errors.capture(f"admin:{label}", err,
                               context="triggered from the admin panel")
            except Exception:
                pass

    threading.Thread(target=runner, daemon=True, name=f"admin-{label}").start()
    return (f"{label} started in the background — results appear in "
            "reports/health on the next refresh.")


def _act_run_gate(_p: dict) -> str:
    from . import orchestrator
    return _spawn("skill gate", orchestrator.gate_skills)


def _act_run_curate(_p: dict) -> str:
    from . import curator
    return _spawn("skill curation", curator.curate)


def _act_run_backup(_p: dict) -> str:
    from . import backup
    return _spawn("backup", backup.run)


def _act_config_set(p: dict) -> str:
    from . import opconfig
    return opconfig.set_value(str(p.get("key", "")), str(p.get("value", "")))


def _act_config_unset(p: dict) -> str:
    from . import opconfig
    return opconfig.unset(str(p.get("key", "")))


def _act_mcp_add(p: dict) -> str:
    from . import connectors
    out = connectors.add_mcp_server(
        str(p.get("name", "")).strip(), str(p.get("url", "")).strip(),
        type=str(p.get("type") or "data").strip().lower(),
        auth_env=(str(p.get("auth_env", "")).strip() or None),
        specialists=[s.strip() for s in str(p.get("specialists", "")).split(",")
                     if s.strip()] or None)
    if out.startswith("Error"):
        raise ValueError(out[len("Error: "):].rstrip("."))
    return out + " Live everywhere immediately (definitions are read per call)."


def _act_mcp_remove(p: dict) -> str:
    from . import connectors
    out = connectors.remove_mcp_server(str(p.get("name", "")).strip())
    if out.startswith("Error"):
        raise ValueError(out[len("Error: "):].rstrip("."))
    return out


_OPS = {
    "approve_action": _act_approve,
    "reject_action": _act_reject,
    "goal_add": _act_goal_add,
    "goal_drop": _act_goal_drop,
    "goal_done": _act_goal_done,
    "schedule_add": _act_schedule_add,
    "schedule_remove": _act_schedule_remove,
    "schedule_enable": lambda p: _act_schedule_enabled(p, True),
    "schedule_disable": lambda p: _act_schedule_enabled(p, False),
    "set_autonomy": _act_set_autonomy,
    "run_gate": _act_run_gate,
    "run_curate": _act_run_curate,
    "run_backup": _act_run_backup,
    "config_set": _act_config_set,
    "config_unset": _act_config_unset,
    "mcp_add": _act_mcp_add,
    "mcp_remove": _act_mcp_remove,
}


def act(op: str, params: dict | None = None) -> dict:
    """Execute one admin operation. Returns {"ok", "message"} — errors are
    reported in-band (the panel shows them), never as a 500."""
    fn = _OPS.get(op)
    if fn is None:
        return {"ok": False,
                "message": f"unknown op '{op}' "
                           f"(expected one of {', '.join(sorted(_OPS))})"}
    try:
        return {"ok": True, "message": fn(params or {})}
    except Exception as err:
        return {"ok": False, "message": f"{type(err).__name__}: {err}"}
