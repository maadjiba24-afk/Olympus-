"""Domain lore — the web-context system's compounding memory.

Every scrape/map/crawl teaches Olympus something durable about a *domain*: where
its sitemap lives, whether it disallows crawling, whether interaction (actions)
or a mobile UA gets better content, how large its pages run, its brand identity,
and how reliably it can be fetched at all. That knowledge is stored per-domain
and fed back as *hints* to the next visit — so the more Olympus scrapes, the
better it scrapes. This is the data-network-effect moat of ADR 0009 applied to
web ingestion: a copy of the code starts this corpus at zero.

Design (mirrors the absorbed-capability contract, ADR 0008):

  * **Compounds, never regresses.** Hints only *add* candidates (a known sitemap
    URL to try first) or *bias* a choice; they never relax a safety gate — every
    fetch still goes through the SSRF/egress-pinned path regardless of lore.
  * **Replay-safe & opt-outable.** Learning is inert under `OLYMPUS_REPLAY` and
    can be disabled with `OLYMPUS_WEB_LORE=0`. It is off the council replay hot
    path (it lives inside tool execution, whose results the harness freezes).
  * **Bounded.** A hard cap on domains and per-field sizes; atomic JSON store
    under `MEMORY_DIR`, same pattern as goals/webmonitor.
  * **Feeds the self-tuner.** Each observation records an ok/fail outcome into
    `evolve`, so the heartbeat reviewer tunes the web-context knobs from real
    results rather than a fixed guess.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, fields
from urllib.parse import urlparse

from . import config

MAX_DOMAINS = 5000               # corpus ceiling — a bound, not an ambition
_STR_CAP = 300                   # per learned string field


@dataclass
class DomainRecord:
    domain: str
    first_seen: float = 0.0
    last_seen: float = 0.0
    scrapes: int = 0
    successes: int = 0
    failures: int = 0
    blocks: int = 0              # SSRF/robots/port refusals
    avg_bytes: float = 0.0       # running mean of fetched page size
    sitemap_url: str = ""        # discovered sitemap (map_urls)
    robots_disallow: bool = False
    actions_helped: int = 0      # times interaction yielded more content
    mobile_helped: int = 0       # times a mobile UA yielded more content
    verified: int = 0            # extractions the verify role confirmed
    flagged: int = 0             # extractions the verify role flagged
    site_name: str = ""          # from branding (og:site_name)
    has_jsonld: bool = False     # exposes JSON-LD structured data (LLM-free)
    feed_url: str = ""           # discovered RSS/Atom feed


def _path():
    return config.MEMORY_DIR / "domainlore.json"


def enabled() -> bool:
    """Learning runs unless replaying (a stateful, clock-touching behavior must
    not diverge a replay) or explicitly disabled."""
    if os.environ.get("OLYMPUS_REPLAY"):
        return False
    return os.environ.get("OLYMPUS_WEB_LORE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _mutex():
    from . import proclock
    return proclock.lock("domainlore")


def _load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        try:
            p.replace(p.with_name(p.name + ".corrupt"))   # quarantine, don't wipe
        except OSError:
            pass
        return {}
    if not isinstance(raw, dict):
        return {}
    known = {f.name for f in fields(DomainRecord)}
    out: dict = {}
    for dom, d in raw.items():
        if isinstance(d, dict):
            try:
                out[dom] = DomainRecord(**{k: v for k, v in d.items()
                                           if k in known})
            except TypeError:
                continue
    return out


def _save(records: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({k: asdict(v) for k, v in records.items()},
                              indent=0), encoding="utf-8")
    os.replace(tmp, p)


def observe(url: str, *, ok: bool = True, blocked: bool = False,
            bytes_: int = 0, sitemap: str = "", robots_disallow: bool = False,
            actions_helped: bool = False, mobile_helped: bool = False,
            verified: bool | None = None, site_name: str = "",
            has_jsonld: bool = False, feed_url: str = "",
            now: float | None = None) -> None:
    """Fold one visit's result into the domain's lore. Best-effort: never raises
    out of a scrape. Also records the ok/fail outcome into the self-tuner."""
    if not enabled():
        return
    dom = _domain(url)
    if not dom:
        return
    now = now or time.time()
    try:
        with _mutex():
            records = _load()
            r = records.get(dom)
            if r is None:
                if len(records) >= MAX_DOMAINS:
                    return                       # corpus full — bounded
                r = DomainRecord(domain=dom, first_seen=now)
                records[dom] = r
            r.last_seen = now
            r.scrapes += 1
            if blocked:
                r.blocks += 1
            elif ok:
                r.successes += 1
            else:
                r.failures += 1
            if bytes_ > 0:
                # running mean over successful fetches
                n = max(1, r.successes)
                r.avg_bytes = r.avg_bytes + (bytes_ - r.avg_bytes) / n
            if sitemap:
                r.sitemap_url = sitemap[:_STR_CAP]
            if robots_disallow:
                r.robots_disallow = True
            if actions_helped:
                r.actions_helped += 1
            if mobile_helped:
                r.mobile_helped += 1
            if verified is True:
                r.verified += 1
            elif verified is False:
                r.flagged += 1
            if site_name and not r.site_name:
                r.site_name = site_name[:_STR_CAP]
            if has_jsonld:
                r.has_jsonld = True
            if feed_url and not r.feed_url:
                r.feed_url = feed_url[:_STR_CAP]
            _save(records)
    except Exception:
        return                                   # lore is advisory; never fatal
    # Feed the self-tuner (bounded event log); drives the heartbeat reviewer.
    try:
        from . import evolve
        evolve.record("webctx", "ok" if (ok and not blocked) else "fail", dom)
    except Exception:
        pass


def hint(url: str) -> dict:
    """Learned, purely-additive hints for the next visit to this domain:
    a known sitemap URL, and whether interaction / a mobile UA has historically
    helped. Never relaxes a safety gate. Empty when disabled or unseen."""
    if not enabled():
        return {}
    dom = _domain(url)
    if not dom:
        return {}
    try:
        r = _load().get(dom)
    except Exception:
        return {}
    if r is None:
        return {}
    out: dict = {}
    if r.sitemap_url:
        out["sitemap_url"] = r.sitemap_url
    if r.robots_disallow:
        out["robots_disallow"] = True
    # only suggest a bias once there's a real signal (>=2 wins)
    if r.actions_helped >= 2:
        out["prefer_actions"] = True
    if r.mobile_helped >= 2:
        out["prefer_mobile"] = True
    return out


def known(url: str) -> "DomainRecord | None":
    if not _domain(url):
        return None
    try:
        return _load().get(_domain(url))
    except Exception:
        return None


def stats() -> dict:
    """Corpus-wide roll-up for the moat board."""
    try:
        records = _load()
    except Exception:
        return {"domains": 0}
    sitemaps = sum(1 for r in records.values() if r.sitemap_url)
    interactive = sum(1 for r in records.values() if r.actions_helped >= 2)
    disallowed = sum(1 for r in records.values() if r.robots_disallow)
    total = sum(r.scrapes for r in records.values())
    ok = sum(r.successes for r in records.values())
    return {"domains": len(records), "sitemaps": sitemaps,
            "interactive": interactive, "robots_disallowed": disallowed,
            "total_visits": total,
            "success_rate": round(ok / total, 3) if total else 0.0}


def report() -> str:
    s = stats()
    if not s.get("domains"):
        return "Web knowledge: nothing learned yet."
    return (f"Web knowledge: {s['domains']} domain(s) learned from "
            f"{s['total_visits']} visit(s) — {s['sitemaps']} sitemap(s) "
            f"discovered, {s['interactive']} site(s) known to need interaction, "
            f"{s['robots_disallowed']} disallowing crawl; "
            f"{int(s['success_rate'] * 100)}% fetch success.")
