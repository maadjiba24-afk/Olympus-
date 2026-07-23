"""Web reflection — turn accumulated domain lore into discoveries.

The compounding corpus (`domainlore`) is only half of getting stronger over
time; the other half is *noticing what it implies*. This heartbeat routine
periodically reads the corpus + the self-tuner's health and surfaces
**discoveries** — capabilities the observed web is asking Olympus to use or grow:

  * domains that expose JSON-LD (use the LLM-free `jsonld` format there),
  * domains that publish RSS/Atom feeds (watch the feed instead of re-scraping),
  * domains that consistently need interaction (a candidate for a default
    action profile),
  * domains whose fetch success is poor (try mobile/a proxy),
  * domains disallowing crawl (respected; recorded).

It never changes behavior on its own beyond the already-safe evolve self-tuning:
a discovery is *surfaced* (returned in the heartbeat log, notified once, and
saved as a lesson) so a human — or a later, deliberately-built feature — can act
on it. This is Olympus "discovering new features over time" as evidence-backed
proposals, not autonomous code generation.

Opt-in (`OLYMPUS_WEB_REFLECT`, default OFF, per the absorbed-capability
contract), replay-inert, and deduplicated so a standing pattern is reported once,
not every cycle.
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable

from . import config

_DEFAULT_EVERY = 24 * 3600        # once a day is plenty for a slow-moving corpus
_MIN_DOMAINS = 3                  # need a little corpus before a pattern is real


def enabled() -> bool:
    if os.environ.get("OLYMPUS_REPLAY"):
        return False
    return os.environ.get("OLYMPUS_WEB_REFLECT", "").strip().lower() in (
        "1", "true", "yes", "on")


def _every() -> int:
    try:
        return max(3600, int(os.environ.get("OLYMPUS_WEB_REFLECT_EVERY",
                                            str(_DEFAULT_EVERY))))
    except ValueError:
        return _DEFAULT_EVERY


def _state_path():
    return config.MEMORY_DIR / "webreflect.json"


def _load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {"seen": [], "last_run": 0.0}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {"seen": list(d.get("seen", []))[:2000],
                "last_run": float(d.get("last_run", 0.0))}
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return {"seen": [], "last_run": 0.0}


def _save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"seen": state["seen"][-2000:],
                               "last_run": state["last_run"]}), encoding="utf-8")
    os.replace(tmp, p)


def discoveries() -> list[dict]:
    """Compute the current discovery set from the corpus. Pure over a lore
    snapshot, deterministic (sorted), each with a stable `key` for dedup."""
    from . import domainlore
    try:
        records = domainlore._load()
    except Exception:
        return []
    if len(records) < _MIN_DOMAINS:
        return []

    def doms(pred) -> list[str]:
        return sorted(d for d, r in records.items() if pred(r))

    out: list[dict] = []
    jsonld = doms(lambda r: r.has_jsonld)
    if jsonld:
        out.append({"key": f"jsonld:{len(jsonld)}", "kind": "capability",
                    "title": f"{len(jsonld)} domain(s) expose JSON-LD",
                    "detail": "Use the `jsonld` scrape format for deterministic, "
                              "LLM-free structured data on: "
                              + ", ".join(jsonld[:8])})
    feeds = doms(lambda r: r.feed_url)
    if feeds:
        out.append({"key": f"feeds:{len(feeds)}", "kind": "capability",
                    "title": f"{len(feeds)} domain(s) publish a feed",
                    "detail": "Watch the RSS/Atom feed instead of re-scraping: "
                              + ", ".join(feeds[:8])})
    interactive = doms(lambda r: r.actions_helped >= 2)
    if interactive:
        out.append({"key": f"interactive:{len(interactive)}", "kind": "proposal",
                    "title": f"{len(interactive)} domain(s) need interaction",
                    "detail": "Consider a default action profile (scroll/expand) "
                              "for: " + ", ".join(interactive[:8])})
    disallow = doms(lambda r: r.robots_disallow)
    if disallow:
        out.append({"key": f"robots:{len(disallow)}", "kind": "compliance",
                    "title": f"{len(disallow)} domain(s) disallow crawl",
                    "detail": "Honored (excluded from crawl): "
                              + ", ".join(disallow[:8])})
    flaky = doms(lambda r: r.scrapes >= 3
                 and r.successes / max(1, r.scrapes) < 0.5)
    if flaky:
        out.append({"key": f"flaky:{sorted(flaky)[0]}:{len(flaky)}",
                    "kind": "proposal",
                    "title": f"{len(flaky)} domain(s) fetch poorly",
                    "detail": "Low fetch success — try `mobile=True` or a proxy "
                              "for: " + ", ".join(flaky[:8])})
    return out


def run_due(now: float | None = None,
            notify: Callable[[str], object] | None = None) -> list[str]:
    """Heartbeat entry. No-op unless opted in and the cadence has elapsed.
    Surfaces only NEW discoveries (deduped), saves them as a lesson, notifies
    once. Never raises out."""
    if not enabled():
        return []
    now = now or time.time()
    try:
        state = _load_state()
        if (now - state["last_run"]) < _every():
            return []
        found = discoveries()
        fresh = [d for d in found if d["key"] not in set(state["seen"])]
        state["last_run"] = now
        state["seen"] = state["seen"] + [d["key"] for d in fresh]
        _save_state(state)
        if not fresh:
            return []
        # Persist as a lesson (sanitized at the memory sink) so the knowledge
        # outlives the process, and notify the operator once.
        body = "\n".join(f"- [{d['kind']}] {d['title']}: {d['detail']}"
                         for d in fresh)
        try:
            from . import memory
            memory.save("lessons", "web discoveries", body)
        except Exception:
            pass
        if notify is None:
            from . import gateway
            notify = gateway.notify_all
        try:
            notify("🔎 Web-context discoveries:\n\n" + body)
        except Exception:
            pass
        return [f"web reflection: {d['title']}" for d in fresh]
    except Exception:
        return []
