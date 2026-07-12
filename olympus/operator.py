"""HERMES operator orchestration — Phases 2-4 of docs/DESIGN_OPERATOR.md.

Credentialed browser actions never bypass governance: each operate runs as an
`actions.ActionType`, so it inherits the whole spine — deny-first scopes,
autonomy gating, IRREVERSIBLE→approval, daily runaway caps, and the immutable
audit log. This module is the thin glue between the browser harness and that
spine, plus the always-on heartbeat jobs (Phase 3) and the METIS/Prometheus
weave (Phase 4).

Nothing here runs unless `OLYMPUS_OPERATOR` is on and the domain is authorized.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import actions, browser, config, memory, security

OPERATE_SCOPE = "browser.operate"      # one coarse scope enables the operator
_REVIEW_MIN_RUNS = 3
_REVIEW_FLOOR = 0.34                    # prune profiles that fail >2/3 of the time
_SETTINGS_KEY = "operator"             # per-user prefs blob
_PROMOTE_MIN_RUNS = 4                  # a learned skill must be tried this often…
_PROMOTE_RELIABILITY = 0.75            # …and land this reliably to graduate


# --- per-user settings (the conversational surface) ----------------------
#
# A normal person never sets an env var. They authorize a site by *asking*
# Olympus to set it up; that writes here. The OLYMPUS_OPERATOR / _DOMAINS env
# vars remain as an engineer/admin override that is always additive.

def _settings(user: str) -> dict:
    from . import prefs
    s = prefs.get(user, _SETTINGS_KEY, {}) or {}
    s.setdefault("sites", {})
    return s


def _save_settings(user: str, s: dict) -> None:
    from . import prefs
    prefs.set(user, _SETTINGS_KEY, s)


def enabled(user: str) -> bool:
    """Operator on for this user — via the env switch OR their own opt-in."""
    return browser.operator_enabled() or bool(_settings(user).get("enabled"))


def advanced(user: str) -> bool:
    """Whether to surface engineer controls (CLI/env). Off = plain-English only."""
    return bool(_settings(user).get("advanced"))


def set_advanced(user: str, on: bool) -> None:
    s = _settings(user)
    s["advanced"] = bool(on)
    _save_settings(user, s)


def sites(user: str) -> dict:
    return dict(_settings(user).get("sites", {}))


def login_mode(user: str, domain: str) -> str:
    site = _settings(user).get("sites", {}).get((domain or "").strip().lower(), {})
    return (site or {}).get("login", "manual")


def authorize_site(user: str, domain: str, login: str = "manual") -> str:
    """Turn the operator on for `user` and authorize one site. `login` is
    'manual' (they sign in themselves; we never see the password) or 'remember'
    (we store credentials in the vault for auto-login)."""
    domain = (domain or "").strip().lower()
    if not domain:
        raise ValueError("a domain is required")
    if login not in ("manual", "remember"):
        raise ValueError("login must be 'manual' or 'remember'")
    s = _settings(user)
    s["enabled"] = True
    s["sites"][domain] = {"login": login, "created": int(time.time())}
    _save_settings(user, s)
    return domain


def forget_site(user: str, domain: str) -> bool:
    """Remove a site's authorization and any stored credentials for it."""
    domain = (domain or "").strip().lower()
    s = _settings(user)
    existed = s.get("sites", {}).pop(domain, None) is not None
    _save_settings(user, s)
    try:
        from . import vault
        if vault.available():
            vault.delete(user, f"site:{domain}")
            vault.delete(user, f"cookies:{domain}")   # drop any saved session too
    except Exception:
        pass
    return existed


def save_auth(user: str, domain: str) -> str:
    """Persist the current browser session for `domain` (its cookies) in the
    ENCRYPTED vault, so a later run can restore it instead of logging in again.
    Cookies are session credentials: operator-gated, domain-authorized, and never
    surfaced to the model. Returns a short status."""
    domain = (domain or "").strip().lower()
    if not enabled(user):
        return "Error: the operator isn't set up for this."
    if not authorized(user, domain):
        return f"Error: '{domain or 'unknown'}' isn't an authorized site."
    from . import vault
    if not vault.available():
        return "Error: the secure vault is not configured (set OLYMPUS_SECRET_KEY)."
    try:
        cookies = browser.session().get_cookies(domain)
    except browser.BrowserUnavailable as err:
        return f"No browser attached: {err}"
    if not cookies:
        return f"No session cookies to save for {domain}."
    vault.put(user, f"cookies:{domain}", {"cookies": cookies})
    return f"Saved the {domain} session ({len(cookies)} cookie(s), encrypted)."


def restore_auth(user: str, domain: str) -> str:
    """Restore a previously-saved session for `domain` by injecting its
    vault-stored cookies, so the operator skips re-login. Returns a status."""
    domain = (domain or "").strip().lower()
    if not enabled(user):
        return "Error: the operator isn't set up for this."
    if not authorized(user, domain):
        return f"Error: '{domain or 'unknown'}' isn't an authorized site."
    from . import vault
    if not vault.available():
        return "Error: the secure vault is not configured (set OLYMPUS_SECRET_KEY)."
    blob = vault.get(user, f"cookies:{domain}")
    cookies = blob.get("cookies") if isinstance(blob, dict) else None
    if not cookies:
        return f"No saved session for {domain} — log in once, then save it."
    try:
        n = browser.session().set_cookies(cookies)
    except browser.BrowserUnavailable as err:
        return f"No browser attached: {err}"
    return f"Restored the {domain} session ({n} cookie(s))."


def has_saved_auth(user: str, domain: str) -> bool:
    """True if a saved session exists for `domain` (used to auto-restore)."""
    from . import vault
    if not vault.available():
        return False
    blob = vault.get(user, f"cookies:{(domain or '').strip().lower()}")
    return bool(isinstance(blob, dict) and blob.get("cookies"))


def authorized(user: str, domain: str) -> bool:
    """The domain must be authorized (by this user OR the env override). Under
    sovereign mode it must ALSO pass the network egress allowlist — a second,
    independent fence. With sovereign mode OFF (the default) egress_allowed is a
    no-op, so domain authorization is the sole gate; the allowlist fence only
    engages once sovereign mode is enabled."""
    d = (domain or "").strip().lower()
    if not d:
        return False
    authorized_domains = (set(browser.operator_domains())
                          | set(_settings(user).get("sites", {})))
    # Match the exact domain OR any subdomain of it: authorizing 'amazon.com'
    # covers 'www.amazon.com' / regional subdomains, which is where a login
    # almost always lands. (A bare exact match made the actuator unusable on
    # the very site the user just authorized.)
    listed = any(d == a or d.endswith("." + a) for a in authorized_domains)
    return listed and security.egress_allowed(d)


def remember_credentials(user: str, domain: str, username: str,
                         password: str) -> str:
    """Store site credentials in the encrypted vault and switch the site to
    auto-login. The password is passed in by a SECURE LOCAL PROMPT, never by the
    model — it must never appear as a tool argument."""
    from . import vault
    if not vault.available():
        raise RuntimeError("the secure vault is not configured")
    domain = (domain or "").strip().lower()
    vault.put(user, f"site:{domain}", {"username": username,
                                        "password": password})
    authorize_site(user, domain, "remember")
    return domain


# --- review surface: what the operator did on your accounts ---------------
#
# The data already exists (every operate is an Action with an immutable audit
# trail); this surfaces it in plain English so a non-technical user can see and
# trust what ran on their accounts.

# The three operate ActionTypes plus the credentialed login actuator.
OPERATOR_ACTION_TYPES = frozenset({
    "browser_operate", "browser_operate_irreversible",
    "browser_operate_financial",
})


def history(user: str, limit: int = 20) -> list["actions.Action"]:
    """Recent operator actions for `user`, newest first — filtered from the
    action history to just the credentialed-operate types."""
    return [a for a in actions.history(user, limit=max(limit * 4, 80))
            if a.type in OPERATOR_ACTION_TYPES][:limit]


def render_history(user: str, limit: int = 20) -> str:
    """Plain-English 'what Olympus did on my accounts' report."""
    items = history(user, limit)
    if not items:
        return "No operator actions yet."
    lines = []
    for a in items:
        domain = (a.payload or {}).get("domain", "?")
        tmpl = (a.payload or {}).get("template", a.type)
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(getattr(a, "created_at", 0) or 0))
        mark = {"executed": "✓", "failed": "✗", "rejected": "⊘",
                "prepared": "…", "approved": "→", "undone": "↺"}.get(
                    a.status, "•")
        lines.append(f"{mark} {when}  {domain}  {tmpl}  [{a.status}]")
    return "\n".join(lines)


def status_summary(user: str) -> str:
    """Plain-English operator status: on/off, authorized sites (+ login mode),
    pending approvals, and the last few actions."""
    on = enabled(user)
    lines = [f"Operator: {'ON' if on else 'off'}"]
    if not on:
        lines.append("  Ask me to do something on a site, or run "
                     "`olympus operator enable`, to turn it on.")
    site_map = sites(user)
    if site_map:
        lines.append("Authorized sites:")
        for d in sorted(site_map):
            lines.append(f"  • {d} (login: {login_mode(user, d)})")
    else:
        lines.append("Authorized sites: none yet.")
    pend = [a for a in actions.pending(user) if a.type in OPERATOR_ACTION_TYPES]
    if pend:
        lines.append(f"Awaiting your approval: {len(pend)} operator "
                     "action(s) — run `olympus actions` to review.")
    recent = render_history(user, 5)
    if recent != "No operator actions yet.":
        lines.append("Recent actions:")
        lines += [f"  {ln}" for ln in recent.splitlines()]
    return "\n".join(lines)


# --- risk → spine ActionType ---------------------------------------------

def type_for_risk(risk: str) -> str:
    """Map a template's risk to its spine ActionType. Notable can auto-run within
    scope; irreversible always needs approval; financial_legal maps to the
    tightest tier (pay/sign/delete — cap 10), not the looser irreversible one."""
    r = (risk or "").lower()
    if r == "notable":
        return "browser_operate"
    if r == "financial_legal":
        return "browser_operate_financial"
    return "browser_operate_irreversible"


def _gate(user: str, domain: str) -> str | None:
    """Return a refusal reason if the operator may not act on `domain`."""
    if not enabled(user):
        return ("the operator isn't set up yet — ask me to set it up for this "
                "site")
    if not authorized(user, domain):
        return f"'{domain}' isn't authorized yet — ask me to set it up first"
    return None


def _template(domain: str, name: str):
    prof = browser.get_profile(domain)
    if prof is None:
        return None, None
    return prof, (prof.templates or {}).get(name)


# --- spine callbacks (preview = dry-run, execute = perform) --------------

def preview(payload: dict) -> str:
    domain = payload.get("domain", "")
    name = payload.get("template", "")
    _, tmpl = _template(domain, name)
    if not tmpl:
        return f"(no template '{name}' for {domain})"
    lines = [f"On {domain}, run template '{name}' "
             f"({tmpl.get('risk', 'notable')}):"]
    for s in tmpl.get("steps", []):
        lines.append(f"  - {s.get('op', '?')} {s.get('selector', '')}".rstrip())
    return "\n".join(lines)


def execute(payload: dict) -> dict:
    domain = payload.get("domain", "")
    name = payload.get("template", "")
    params = payload.get("params", {}) or {}
    # Re-check authorization for the acting user at execution time (defense in
    # depth — covers a job prepared earlier whose authorization was since pulled).
    reason = _gate(payload.get("user", ""), domain)
    if reason:
        raise RuntimeError(reason)
    _, tmpl = _template(domain, name)
    if not tmpl:
        raise RuntimeError(f"no template '{name}' for {domain}")
    # The two highest-risk action types are DISABLED BY DEFAULT and fail
    # closed at the execution door (mirroring the sovereign gates): even a
    # previously prepared/approved action refuses without the explicit opt-in.
    if type_for_risk(tmpl.get("risk", "notable")) in (
            "browser_operate_financial", "browser_operate_irreversible") \
            and not config.browser_financial_enabled():
        raise RuntimeError(
            "financial/irreversible browser templates are disabled by "
            "default — set OLYMPUS_ENABLE_BROWSER_FINANCIAL=1 to enable them.")
    sess = browser.session()
    try:
        result = sess.run_template(tmpl.get("steps", []), params)
    except browser.TemplateStepError as err:
        # Self-heal: the site likely drifted. Re-observe, look for the moved
        # control, and file a human-reviewed proposal — never auto-rewrite a
        # credentialed template. The outcome is still an honest FAILED.
        browser.mark_profile_outcome(domain, False)
        note = _heal_and_propose(domain, name, err)
        raise RuntimeError(f"{err}. {note}") from err
    ok = True
    if tmpl.get("success_selector"):
        ok = sess.exists(tmpl["success_selector"])
        result["verified"] = ok
    browser.mark_profile_outcome(domain, ok)
    if not ok:
        raise RuntimeError("template ran but the success marker never appeared")
    return result


def _heal_and_propose(domain: str, template: str,
                      err: "browser.TemplateStepError") -> str:
    """After a template step failed, re-observe and, if a likely-moved control is
    found, file a Prometheus-style proposal to patch the template. Returns a
    short human-facing note. Files nothing (beyond recording the drift) if no
    confident candidate is found."""
    try:
        cand = browser.session().heal_candidate(err.selector)
    except Exception:
        cand = None
    if cand and cand.get("selector"):
        body = (f"Template '{template}' on {domain} drifted.\n"
                f"Step {err.index} ({err.op}) selector {err.selector!r} no "
                f"longer resolves.\n"
                f"Likely moved to: {cand['selector']!r} "
                f"(label {cand.get('label','')!r}, "
                f"match {cand.get('score')}).\n"
                "Review and, if correct, update the template with "
                "site_template_record.")
        _file_proposal(domain, f"template drift: {domain}/{template}", body)
        return (f"Filed a self-heal proposal: {err.selector!r} may have moved to "
                f"{cand['selector']!r} (for human review).")
    _file_proposal(domain, f"template drift: {domain}/{template}",
                   f"Template '{template}' step {err.index} selector "
                   f"{err.selector!r} no longer resolves and no confident "
                   "replacement was found on the page. Needs human attention.")
    return "Filed a self-heal proposal (no confident replacement found)."


def _file_proposal(domain: str, title: str, body: str) -> None:
    """Record a human-reviewable operator proposal in memory (never auto-applied)."""
    user = memory.current_user()
    memory.set_user(user)
    memory.save("upgrades", title, security.sanitize_for_memory(body))


def register_operator_actions() -> None:
    """Register the two operate ActionTypes on the spine (called from
    builtin_actions so the capability count includes them)."""
    actions.register(actions.ActionType(
        name="browser_operate", risk_class=actions.NOTABLE, scope=OPERATE_SCOPE,
        preview=preview, execute=execute,
        description="Run a reversible/notable operator template on an "
                    "authorized site."))
    actions.register(actions.ActionType(
        name="browser_operate_irreversible", risk_class=actions.IRREVERSIBLE,
        scope=OPERATE_SCOPE, preview=preview, execute=execute,
        description="Run an irreversible operator template; always requires "
                    "explicit approval."))
    actions.register(actions.ActionType(
        name="browser_operate_financial", risk_class=actions.FINANCIAL_LEGAL,
        scope=OPERATE_SCOPE, preview=preview, execute=execute,
        description="Run a financial/legal operator template (pay/sign/delete): "
                    "always explicit approval, tightest daily cap."))


def run(user: str, domain: str, template: str, params: dict) -> actions.Action:
    """Prepare an operate action and run-or-hold it per policy. The returned
    Action's status says what happened (EXECUTED / FAILED / PREPARED=held)."""
    type_name = type_for_risk(_template(domain, template)[1].get("risk", "notable")
                              if _template(domain, template)[1] else "notable")
    action = actions.prepare(
        user, type_name,
        {"domain": domain, "template": template, "params": params,
         "user": user},
        title=f"Operate '{template}' on {domain}", why="HERMES operator")
    return actions.auto_or_hold(action)


# --- Phase 3: always-on operator jobs (run by the heartbeat) -------------

def _jobs_path():
    return config.MEMORY_DIR / "operator_jobs.json"


def _load_jobs() -> list[dict]:
    path = _jobs_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [j for j in raw if isinstance(j, dict)] if isinstance(raw, list) else []


def _save_jobs(jobs: list[dict]) -> None:
    path = _jobs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    tmp.replace(path)


def schedule(user: str, name: str, domain: str, template: str,
             interval_secs: int, params: dict | None = None) -> dict:
    """Create/replace a standing operator job. Stored only; it runs on the
    heartbeat, still through the spine (so approval/scope/budget all apply)."""
    job = {"name": name, "user": user, "domain": domain, "template": template,
           "params": params or {}, "interval": max(300, int(interval_secs)),
           "last_run": 0.0, "enabled": True}
    jobs = [j for j in _load_jobs() if j.get("name") != name]
    jobs.append(job)
    _save_jobs(jobs)
    return job


def run_due(now: float | None = None) -> list[str]:
    """Run any operator jobs whose interval has elapsed. Each job is gated per
    user (env switch OR that user's opt-in) via _gate, and goes through the
    spine: it auto-executes only within granted scope+autonomy+budget, otherwise
    it's left pending for approval. Returns human-readable log lines."""
    now = now or time.time()
    jobs = _load_jobs()
    if not jobs:
        return []
    out: list[str] = []
    changed = False
    for j in jobs:
        if not j.get("enabled", True):
            continue
        ju = j.get("user", "")
        if not enabled(ju):              # operator off for this user → silent no-op
            continue
        if now - float(j.get("last_run", 0.0)) < int(j.get("interval", 300)):
            continue
        j["last_run"] = now
        changed = True
        if not authorized(ju, j.get("domain", "")):
            out.append(f"job '{j.get('name')}' skipped: "
                       f"'{j.get('domain')}' isn't authorized")
            continue
        try:
            action = run(j["user"], j["domain"], j["template"],
                         j.get("params", {}))
        except Exception as err:
            out.append(f"job '{j.get('name')}' error: {err}")
            continue
        if action.status == actions.EXECUTED:
            out.append(f"job '{j['name']}' executed on {j['domain']}")
        elif action.status == actions.FAILED:
            out.append(f"job '{j['name']}' failed: {action.error}")
        else:
            out.append(f"job '{j['name']}' awaiting approval (id {action.id})")
    if changed:
        _save_jobs(jobs)
    return out


# --- Phase 4: METIS review + Prometheus proposals ------------------------

def promote_ready(min_runs: int = _PROMOTE_MIN_RUNS,
                  min_reliability: float = _PROMOTE_RELIABILITY) -> list[str]:
    """METIS hook: auto-graduate learned skills that have proven reliable into
    declarative action templates. A skill qualifies once it carries a structured
    recipe, has been tried >= min_runs times, and lands >= min_reliability of the
    time; skills whose template already exists are skipped (idempotent). The
    template rides the governed browser_operate path, so graduation formalizes a
    proven flow without widening the trust boundary. Returns report lines."""
    out: list[str] = []
    for s in browser.list_skills():
        if not s.recipe or s.runs < min_runs or s.reliability < min_reliability:
            continue
        prof = browser.get_profile(s.domain)
        if prof is not None and s.name in (prof.templates or {}):
            continue                    # already graduated — idempotent
        promoted = browser.promote_skill(s.domain, s.name)
        if promoted is not None:
            out.append(f"{s.domain} · {s.name} → template "
                       f"({s.reliability} over {s.runs} runs)")
    return out


def review_profiles(min_runs: int = _REVIEW_MIN_RUNS,
                    floor: float = _REVIEW_FLOOR) -> str:
    """METIS hook: prune site profiles that fail consistently (enough runs, low
    reliability) so the operator stops trusting drifted recipes, and graduate
    learned skills that have proven reliable into declarative templates. Returns
    a short report."""
    profiles = browser._load_profiles()
    keep, pruned = [], []
    for p in profiles:
        if p.runs >= min_runs and p.reliability < floor:
            pruned.append(f"{p.domain} ({p.reliability} over {p.runs} runs)")
        else:
            keep.append(p)
    if pruned:
        browser._store_profiles(keep)
    graduated = promote_ready()
    parts = []
    if pruned:
        parts.append("Pruned flaky site profiles: " + "; ".join(pruned))
    if graduated:
        parts.append("Graduated proven skills to templates: "
                     + "; ".join(graduated))
    if not parts:
        return f"Reviewed {len(profiles)} site profile(s); all within tolerance."
    return " ".join(parts)


def propose_profile(domain: str, rationale: str, *, login_url: str = "",
                    username_selector: str = "", password_selector: str = "",
                    submit_selector: str = "", success_selector: str = "") -> str:
    """Prometheus hook: file a human-reviewable proposal to add/patch a site
    profile. It does NOT apply anything — credentialed recipes are never
    self-modified; a human enacts them with site_profile_record."""
    user = memory.current_user()
    memory.set_user(user)
    body = (f"Proposed site-profile patch for {domain}\n"
            f"Rationale: {rationale}\n"
            f"login_url: {login_url}\nusername_selector: {username_selector}\n"
            f"password_selector: {password_selector}\n"
            f"submit_selector: {submit_selector}\n"
            f"success_selector: {success_selector}\n")
    memory.save("upgrades", f"site profile proposal: {domain}",
                security.sanitize_for_memory(body))
    return (f"Filed a site-profile proposal for {domain} "
            "(memory/operator_proposals) for human review.")
