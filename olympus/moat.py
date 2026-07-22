"""Moat — a unified status board for the absorbed, self-evolving capabilities.

The capabilities Olympus absorbed and made native (vector recall at scale, swarm
topologies, quorum consensus, bandit routing, file-defined agents, cross-instance
federation) are wired into Olympus's own evolution spine, so they measure
themselves and get stronger the more they are used. This module gathers their
live state — enabled?, health track-record, self-tuned parameters — into ONE
read-only board, so an operator can watch the moat compound over time.

Pure and best-effort: every probe is isolated, so a subsystem that is
misconfigured or empty degrades to a blank cell rather than breaking the view.
"""

from __future__ import annotations

import os


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "on", "true", "yes")


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def status() -> dict:
    """A structured snapshot of every absorbed capability: whether it is on, its
    self-evolution health, and its current self-tuned settings."""
    from . import (agentreg, annindex, bandit_routing, consensus, dytopo,
                   evolve, federation, specialists)

    health = _safe(evolve.health, {}) or {}
    tunables = (_safe(evolve.summary, {}) or {}).get("tunables", {})

    caps: dict[str, dict] = {}

    # Vector recall at scale.
    caps["vector_recall"] = {
        "enabled": _safe(annindex.enabled, False),
        "flag": "OLYMPUS_ANN",
        "detail": f"exact ≤ {annindex.EXACT_THRESHOLD} items, "
                  "persistent HNSW graph above (docrag)",
    }

    # Swarm topologies + consultation.
    caps["swarm"] = {
        "enabled": _safe(dytopo.swarm_enabled, False),
        "flag": "OLYMPUS_SWARM",
        "topology": _safe(dytopo.swarm_topology_kind, ""),
        "health": health.get("swarm", {}),
        "tuned": {k: v for k, v in tunables.items() if k.startswith("dytopo.")},
    }

    # Quorum consensus verification — the flagship self-tuning loop.
    caps["consensus"] = {
        "enabled": _safe(consensus.enabled, False),
        "flag": "OLYMPUS_CONSENSUS",
        "verifiers": _safe(consensus.verifier_count, None),
        "health": health.get("consensus", {}),
        "tuned": tunables.get("consensus.verifiers", {}),
    }

    # Semantic routing (relevance-orders the roster; earns its keep at scale).
    caps["semantic_routing"] = {
        "enabled": _safe(specialists.semantic_routing_enabled, False),
        "flag": "OLYMPUS_SEMANTIC_ROUTING",
        "roster": _safe(lambda: len(specialists.SPECIALISTS), 0),
    }

    # Semantic skill retrieval (scopes a specialist's in-prompt skill index to
    # the task; earns its keep once a library outgrows the prompt).
    from . import skills as _skills
    caps["semantic_skills"] = {
        "enabled": _safe(specialists.semantic_skills_enabled, False),
        "flag": "OLYMPUS_SEMANTIC_SKILLS",
        "skills": _safe(_skills.count, 0),
    }

    # Bandit routing (explores under-sampled models).
    bandit = _safe(bandit_routing.status, {}) or {}
    caps["bandit_routing"] = {
        "enabled": _safe(bandit_routing.enabled, False),
        "flag": "OLYMPUS_BANDIT_ROUTING",
        "active": bandit.get("active", False),
        "arms": len(bandit.get("arms", [])),
        "health": health.get("bandit", {}),
        "tuned": tunables.get("bandit.exploration", {}),
    }

    # File-defined agents.
    caps["file_agents"] = {
        "enabled": _safe(agentreg.enabled, False),
        "flag": "OLYMPUS_AGENTS",
        "installed": len(_safe(agentreg.installed_keys, []) or []),
    }

    # Cross-instance federation.
    caps["federation"] = {
        "enabled": _safe(federation.enabled, False),
        "flag": "OLYMPUS_FEDERATION",
        "peers": len(_safe(federation.peers, []) or []),
        "staged_lessons": len(_safe(federation.staged_lessons, []) or []),
    }

    return {"capabilities": caps}


def _health_flag(h: dict) -> str:
    """A compact health marker from an evolve health dict."""
    n = h.get("samples", 0)
    if not n:
        return "· no data yet"
    ok = h.get("ok_rate", 0.0)
    mark = "ok" if ok >= 0.75 else "DEGRADED"
    return f"{mark} — {int(ok * 100)}% ok / {n} runs"


def render() -> str:
    """A human-readable board for `olympus moat`."""
    s = status()["capabilities"]
    lines = ["Olympus moat — absorbed capabilities, self-evolving:", ""]

    def on(cap):
        return "ON " if cap.get("enabled") else "off"

    v = s["vector_recall"]
    lines.append(f"  [{on(v)}] vector recall ({v['flag']}) — {v['detail']}")

    sw = s["swarm"]
    lines.append(f"  [{on(sw)}] swarm ({sw['flag']}) — topology={sw['topology']}"
                 f"  {_health_flag(sw['health'])}")
    for k, t in sorted(sw["tuned"].items()):
        lines.append(f"          tuned {k} = {t.get('current')} "
                     f"[{t.get('lo')}..{t.get('hi')}]")

    c = s["consensus"]
    lines.append(f"  [{on(c)}] consensus ({c['flag']}) — panel={c['verifiers']} "
                 f"verifiers  {_health_flag(c['health'])}")
    if c["tuned"]:
        t = c["tuned"]
        lines.append(f"          self-tuned consensus.verifiers = "
                     f"{t.get('current')} [{t.get('lo')}..{t.get('hi')}] "
                     "(widens when quorum keeps failing)")

    sr = s["semantic_routing"]
    lines.append(f"  [{on(sr)}] semantic routing ({sr['flag']}) — "
                 f"relevance-orders the {sr['roster']}-agent roster")

    ss = s["semantic_skills"]
    lines.append(f"  [{on(ss)}] semantic skills ({ss['flag']}) — "
                 f"task-scopes the in-prompt skill index ({ss['skills']} skills)")

    b = s["bandit_routing"]
    lines.append(f"  [{on(b)}] bandit routing ({b['flag']}) — "
                 f"active={b['active']}, {b['arms']} arms observed"
                 f"  {_health_flag(b['health'])}")
    if b["tuned"]:
        t = b["tuned"]
        lines.append(f"          self-tuned bandit.exploration = "
                     f"{t.get('current')} [{t.get('lo')}..{t.get('hi')}] "
                     "(explores less when outcomes degrade)")

    fa = s["file_agents"]
    lines.append(f"  [{on(fa)}] file agents ({fa['flag']}) — "
                 f"{fa['installed']} installed")

    f = s["federation"]
    lines.append(f"  [{on(f)}] federation ({f['flag']}) — {f['peers']} peers, "
                 f"{f['staged_lessons']} staged lessons")

    lines += ["", "Enable any with its flag; health/tuning accrue as they run "
              "(see `olympus evolve` for the full self-evolution board)."]
    return "\n".join(lines)
