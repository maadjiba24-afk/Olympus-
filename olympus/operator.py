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


# --- risk → spine ActionType ---------------------------------------------

def type_for_risk(risk: str) -> str:
    """Reversible/notable templates use the auto-eligible type; everything more
    serious uses the irreversible type that always requires approval."""
    return ("browser_operate" if (risk or "").lower() == "notable"
            else "browser_operate_irreversible")


def _gate(domain: str) -> str | None:
    """Return a refusal reason if the operator may not act on `domain`."""
    if not browser.operator_enabled():
        return "the operator is disabled (set OLYMPUS_OPERATOR=1)"
    if not browser.domain_allowed(domain):
        return (f"'{domain}' is not authorized (add it to "
                "OLYMPUS_OPERATOR_DOMAINS and the egress allowlist)")
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
    reason = _gate(domain)               # re-check at execution time (defense in depth)
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


def run(user: str, domain: str, template: str, params: dict) -> actions.Action:
    """Prepare an operate action and run-or-hold it per policy. The returned
    Action's status says what happened (EXECUTED / FAILED / PREPARED=held)."""
    type_name = type_for_risk(_template(domain, template)[1].get("risk", "notable")
                              if _template(domain, template)[1] else "notable")
    action = actions.prepare(
        user, type_name,
        {"domain": domain, "template": template, "params": params},
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
    """Run any operator jobs whose interval has elapsed. Off entirely unless the
    operator is enabled. Each job goes through the spine: it auto-executes only
    within granted scope+autonomy+budget, otherwise it's left pending for
    approval. Returns human-readable log lines."""
    if not browser.operator_enabled():
        return []
    now = now or time.time()
    jobs = _load_jobs()
    out: list[str] = []
    changed = False
    for j in jobs:
        if not j.get("enabled", True):
            continue
        if now - float(j.get("last_run", 0.0)) < int(j.get("interval", 300)):
            continue
        j["last_run"] = now
        changed = True
        reason = _gate(j.get("domain", ""))
        if reason:
            out.append(f"job '{j.get('name')}' skipped: {reason}")
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
