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
    except Exception:
        pass
    return existed


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
    sess = browser.session()
    result = sess.run_template(tmpl.get("steps", []), params)
    ok = True
    if tmpl.get("success_selector"):
        ok = sess.exists(tmpl["success_selector"])
        result["verified"] = ok
    browser.mark_profile_outcome(domain, ok)
    if not ok:
        raise RuntimeError("template ran but the success marker never appeared")
    return result


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

def review_profiles(min_runs: int = _REVIEW_MIN_RUNS,
                    floor: float = _REVIEW_FLOOR) -> str:
    """METIS hook: prune site profiles that fail consistently (enough runs, low
    reliability) so the operator stops trusting drifted recipes. Returns a
    short report."""
    profiles = browser._load_profiles()
    keep, pruned = [], []
    for p in profiles:
        if p.runs >= min_runs and p.reliability < floor:
            pruned.append(f"{p.domain} ({p.reliability} over {p.runs} runs)")
        else:
            keep.append(p)
    if pruned:
        browser._store_profiles(keep)
        return "Pruned flaky site profiles: " + "; ".join(pruned)
    return f"Reviewed {len(profiles)} site profile(s); all within tolerance."


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
