"""Operator dashboard — one consolidated health view.

Folds the pieces an operator otherwise has to check separately (`olympus
reports`, `olympus errors`, instance metrics, today's spend, and the cost
policy) into a single snapshot for `olympus dashboard`. Read-only: it never
changes state, so it is safe to run anytime on a live instance.
"""

from __future__ import annotations

from . import config, errors, metrics, support, usage


def summary() -> dict:
    """A consolidated, JSON-able operator snapshot. Best-effort: a missing piece
    degrades to a safe default rather than failing the whole view."""
    reports = support.recent(100000)
    errs = errors.recent(100000)
    snap = metrics.snapshot()
    try:
        spend = round(usage.today_spend(), 4)
    except Exception:
        spend = 0.0
    try:
        budget = usage.daily_budget()
    except Exception:
        budget = 0.0
    return {
        "uptime_seconds": snap.get("uptime_seconds"),
        "requests": snap.get("requests"),
        "client_errors": snap.get("client_errors"),
        "server_errors": snap.get("server_errors"),
        "spend_today": spend,
        "daily_budget": budget,
        "reports_total": len(reports),
        "errors_total": len(errs),
        "latest_report": reports[-1] if reports else None,
        "latest_error": errs[-1] if errs else None,
        "cost_policy": {
            "require_byok": config.require_byok(),
            "free_chats": config.free_chats(),
            "daily_chat_limit": config.daily_chat_limit(),
        },
    }


def _budget_line(spend: float, budget: float) -> str:
    if budget and budget > 0:
        pct = (spend / budget * 100) if budget else 0
        flag = "  ⚠ OVER CAP" if spend > budget else ""
        return f"Spend today:   ${spend:.4f} / ${budget:.2f} ({pct:.0f}%){flag}"
    return f"Spend today:   ${spend:.4f}  (no daily cap set)"


def _policy_line(p: dict) -> str:
    if p.get("free_chats"):
        cost = f"{p['free_chats']} free chats/day, then bring-your-own-key"
    elif p.get("require_byok"):
        cost = "bring-your-own-key required (no free allowance)"
    else:
        cost = "operator-funded (no BYOK requirement)"
    daily = p.get("daily_chat_limit") or 0
    cap = f"; {daily}/user/day cap" if daily else "; no per-user daily cap"
    return f"Cost policy:   {cost}{cap}"


def render(s: dict | None = None) -> str:
    """Human-readable operator dashboard."""
    s = s or summary()
    up = s.get("uptime_seconds") or 0
    hours = up / 3600
    lines = [
        "⚡ OLYMPUS — operator dashboard",
        "=" * 44,
        f"Uptime:        {hours:.1f}h  ({s.get('requests', 0)} requests, "
        f"{s.get('client_errors', 0)} 4xx, {s.get('server_errors', 0)} 5xx)",
        _budget_line(s.get("spend_today", 0.0), s.get("daily_budget", 0.0)),
        _policy_line(s.get("cost_policy", {})),
        "",
        f"Problem reports: {s['reports_total']}   "
        f"Captured errors: {s['errors_total']}",
    ]
    rep = s.get("latest_report")
    if rep:
        who = rep.get("user", "?") + (f" ({rep['contact']})"
                                      if rep.get("contact") else "")
        msg = (rep.get("message", "") or "").replace("\n", " ")[:100]
        lines.append(f"  latest report  [{rep.get('ts', '?')}] {who}: {msg}")
    err = s.get("latest_error")
    if err:
        lines.append(f"  latest error   [{err.get('ts', '?')}] "
                     f"{err.get('where', '?')}: {err.get('error', '')[:100]}")
    if not rep and not err:
        lines.append("  all quiet — no reports or errors. 🎉")
    lines.append("")
    lines.append("Detail: `olympus reports` · `olympus errors` · "
                 "verify integrity with `olympus verify`.")
    return "\n".join(lines)
