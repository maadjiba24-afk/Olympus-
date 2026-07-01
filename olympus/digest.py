"""A plain-language readout of what Olympus has been doing on its own.

The heartbeat learns and acts while you're away (world scans, video learning,
daily skill distillation, weekly self-audit). This turns the records it leaves —
the heartbeat's cycle timestamps plus the memory it wrote — into a short,
glanceable summary. Read-only; makes no model calls, so it's cheap and safe to
run anytime (`olympus learned`, `/learned`, or by asking in chat).
"""

from __future__ import annotations

import time

from . import config, memory, skills

# (label, heartbeat_state_key, config_toggle) for each autonomous cycle, in
# reading order. `config_toggle` names a config attribute that must be truthy for
# the cycle to run at all (None = always on); when it's off the digest says
# "disabled" instead of a misleading "not yet".
_CYCLES = [
    ("World scan for opportunities (Argus)", "opportunity_scan", None),
    ("Learning from queued videos (Mnemosyne)", "watchlist", None),
    ("Daily skill distillation (Metis)", "daily_learning", None),
    ("Specialist training", "train", "TRAIN_EVERY"),
    ("Self-audit & self-upgrade (Prometheus)", "evolution_audit", None),
]

# (heading, memory category) sections to surface, newest titles only.
_SECTIONS = [
    ("Latest world / opportunity reports", "reports"),
    ("Recent lessons learned", "lessons"),
    ("Recent self-upgrades", "upgrades"),
]


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "not yet"
    d = max(0.0, now - ts)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def learned_recently(now: float | None = None) -> str:
    """A short summary of the autonomous loop's recent activity and learnings."""
    now = now or time.time()
    state = memory.load_state()
    lines = ["What Olympus has been doing on its own:", ""]

    def _enabled(toggle) -> bool:
        return toggle is None or bool(getattr(config, toggle, 0))

    if not any(state.get(key) for _, key, _ in _CYCLES):
        lines.append("  The autonomous loop hasn't run yet. Run the `heartbeat` "
                     "service (on by default in the cloud deploy) and Olympus "
                     "keeps learning while you're away.")
    else:
        for label, key, toggle in _CYCLES:
            if not _enabled(toggle):
                lines.append(f"  • {label}: disabled")
            else:
                lines.append(f"  • {label}: last ran {_ago(state.get(key), now)}")

    try:
        n_skills = skills.count()
    except Exception:
        n_skills = 0
    lines += ["", f"Skill library: {n_skills} learned skill(s)."]

    for heading, category in _SECTIONS:
        try:
            titles = memory.recent_titles(category, 3)
        except Exception:
            titles = []
        if titles:
            lines.append("")
            lines.append(f"{heading}:")
            lines += [f"  - {t}" for t in titles]

    return "\n".join(lines)
