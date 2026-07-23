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
_PROFILE_CAP = 1200              # per stored action profile (JSON)
_BIAS_CAP = 100                  # ceiling on a learned bias counter
_MAX_STAGED = 2000               # ceiling on peer-shared candidates awaiting review


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
    action_profile: str = ""     # JSON of the safe action steps that helped here


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


_PROFILE_STEP_KEYS = ("selector", "text", "key", "value")
_MAX_PROFILE_STEPS = 12


def _valid_domain(dom: str) -> bool:
    """A hostname we're willing to store as a key: ASCII letters/digits/dot/dash
    only, dotted, bounded. Rejects paths, spaces, control chars, `@`, schemes —
    anything that isn't a bare host. A junk key would never match a real lookup,
    but we refuse it so a peer can't pollute the corpus with display/JSON junk."""
    if not dom or len(dom) > 253 or "." not in dom:
        return False
    if dom[0] in ".-" or dom[-1] in ".-":
        return False
    return all(c.isalnum() or c in ".-" for c in dom)


def _sanitize_profile(raw) -> str:
    """Reduce an action-profile (JSON string or list) to well-formed, bounded
    safe steps and re-serialize. Verb *safety* is enforced authoritatively at USE
    (webctx `_ACTION_VERBS`); this bounds structure and size AT REST so a stored
    or peer-merged profile can never carry oversized/garbage payloads. Returns ""
    if nothing valid survives."""
    if not raw:
        return ""
    try:
        steps = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return ""
    if not isinstance(steps, list):
        return ""
    clean: list = []
    for s in steps[:_MAX_PROFILE_STEPS]:
        if not isinstance(s, dict):
            continue
        verb = str(s.get("type", "")).strip().lower()[:32]
        if not verb or not all(c.isalpha() or c == "_" for c in verb):
            continue
        step: dict = {"type": verb}
        for k in _PROFILE_STEP_KEYS:
            v = s.get(k)
            if isinstance(v, str) and v.strip():
                step[k] = v[:200]
        for k in ("x", "y"):
            try:
                n = int(s.get(k, 0) or 0)
            except (TypeError, ValueError):
                n = 0
            if n:
                step[k] = n
        clean.append(step)
    # Serialize within the cap by dropping whole steps — never string-slicing a
    # JSON dump (which would yield invalid JSON and silently lose the profile).
    while clean:
        try:
            out = json.dumps(clean)
        except (TypeError, ValueError):
            return ""
        if len(out) <= _PROFILE_CAP:
            return out
        clean.pop()
    return ""


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
            used_mobile: bool = False, used_actions: bool = False,
            action_profile: str = "", now: float | None = None) -> None:
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
            old_avg = r.avg_bytes
            if bytes_ > 0:
                # running mean over successful fetches
                n = max(1, r.successes)
                r.avg_bytes = r.avg_bytes + (bytes_ - r.avg_bytes) / n
            # Learn that mobile helps this domain: a mobile fetch that beats the
            # domain's established baseline by a clear margin is a real win.
            if used_mobile and old_avg > 0 and bytes_ > 1.2 * old_avg:
                r.mobile_helped = min(_BIAS_CAP, r.mobile_helped + 1)
            # Learn that interaction helps: an actioned fetch that beats the
            # baseline is a real win, and we remember the *profile* that did it
            # so it can be re-applied (purely additive) on the next visit.
            if used_actions and old_avg > 0 and bytes_ > 1.2 * old_avg:
                r.actions_helped = min(_BIAS_CAP, r.actions_helped + 1)
                prof = _sanitize_profile(action_profile)
                if prof:
                    r.action_profile = prof
            if sitemap:
                r.sitemap_url = sitemap[:_STR_CAP]
            if robots_disallow:
                r.robots_disallow = True
            if actions_helped:
                r.actions_helped = min(_BIAS_CAP, r.actions_helped + 1)
                prof = _sanitize_profile(action_profile)
                if prof:
                    r.action_profile = prof
            if mobile_helped:
                r.mobile_helped = min(_BIAS_CAP, r.mobile_helped + 1)
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
        # The concrete safe-verb profile that earned those wins, if one was
        # recorded — parsed and re-validated by the caller before use.
        if r.action_profile:
            try:
                steps = json.loads(r.action_profile)
                if isinstance(steps, list) and steps:
                    out["action_profile"] = steps
            except (ValueError, TypeError):
                pass
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


def top(n: int = 15) -> list["DomainRecord"]:
    """The most-visited domains, for the CLI view (deterministic order)."""
    try:
        records = list(_load().values())
    except Exception:
        return []
    return sorted(records, key=lambda r: (-r.scrapes, r.domain))[:max(1, n)]


def prune(retain_days: int = 120, now: float | None = None) -> int:
    """Drop domains not seen within `retain_days` — corpus hygiene, run from the
    heartbeat's maintenance sweep. Returns the number removed. Inert under
    replay / when disabled."""
    if not enabled():
        return 0
    now = now or time.time()
    cutoff = now - max(1, retain_days) * 86400
    removed = 0
    try:
        with _mutex():
            records = _load()
            keep = {d: r for d, r in records.items()
                    if r.last_seen >= cutoff or r.last_seen == 0.0}
            removed = len(records) - len(keep)
            if removed:
                _save(keep)
    except Exception:
        return 0
    return removed


# --- fleet sharing (operator-gated) --------------------------------------
#
# Raw per-domain lore can travel between trusted fleet peers, but it enters as
# *candidates* — never folded into the live corpus without the operator's merge.
# Only durable, additive facts cross: where a sitemap/feed lives, whether a site
# needs interaction or a mobile UA (as a bias + the safe action profile), its
# brand, its robots posture. Never local truth (visit counts, byte averages) and
# never anything that could relax a fetch gate — every fetch is re-gated
# regardless of who contributed the hint.

_SHARE_FIELDS = ("domain", "sitemap_url", "robots_disallow", "has_jsonld",
                 "feed_url", "action_profile", "site_name", "mobile_helped",
                 "actions_helped")


def _staged_path():
    return config.MEMORY_DIR / "domainlore.staged.json"


def shareable(min_visits: int = 2, limit: int = 1000) -> list[dict]:
    """This instance's durable, additive per-domain facts, for a trusted peer.
    Only domains with a real track record (`min_visits`) and at least one useful
    learned fact are included; local-truth counters never leave. Deterministic."""
    try:
        records = _load()
    except Exception:
        return []
    out: list[dict] = []
    for r in sorted(records.values(), key=lambda x: (-x.scrapes, x.domain)):
        if r.scrapes < max(1, min_visits):
            continue
        useful = (r.sitemap_url or r.feed_url or r.robots_disallow
                  or r.has_jsonld or r.action_profile
                  or r.mobile_helped >= 2 or r.actions_helped >= 2)
        if not useful:
            continue
        out.append({
            "domain": r.domain,
            "sitemap_url": r.sitemap_url,
            "robots_disallow": bool(r.robots_disallow),
            "has_jsonld": bool(r.has_jsonld),
            "feed_url": r.feed_url,
            "action_profile": r.action_profile,
            "site_name": r.site_name,
            # share only the *fact of a bias*, capped — never inflated internals
            "mobile_helped": min(_BIAS_CAP, r.mobile_helped),
            "actions_helped": min(_BIAS_CAP, r.actions_helped),
        })
        if len(out) >= max(1, limit):
            break
    return out


def _load_staged() -> list:
    p = _staged_path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        try:
            p.replace(p.with_name(p.name + ".corrupt"))   # quarantine, don't wipe
        except OSError:
            pass
        return []
    return raw if isinstance(raw, list) else []


def _save_staged(rows: list) -> None:
    p = _staged_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rows[-_MAX_STAGED:], indent=0), encoding="utf-8")
    os.replace(tmp, p)


def _clean_share_row(d: dict) -> dict | None:
    """Reduce an inbound shared row to well-typed, bounded fields. Returns None
    if it carries no usable domain."""
    if not isinstance(d, dict):
        return None
    dom = str(d.get("domain", "")).strip().lower()[:_STR_CAP]
    if not _valid_domain(dom):
        return None
    profile = _sanitize_profile(str(d.get("action_profile", "")))

    def _cnt(v):
        try:
            return max(0, min(_BIAS_CAP, int(v)))
        except (TypeError, ValueError):
            return 0
    return {"domain": dom,
            "sitemap_url": str(d.get("sitemap_url", ""))[:_STR_CAP],
            "robots_disallow": bool(d.get("robots_disallow")),
            "has_jsonld": bool(d.get("has_jsonld")),
            "feed_url": str(d.get("feed_url", ""))[:_STR_CAP],
            "action_profile": profile,
            "site_name": str(d.get("site_name", ""))[:_STR_CAP],
            "mobile_helped": _cnt(d.get("mobile_helped")),
            "actions_helped": _cnt(d.get("actions_helped"))}


def stage_shared(rows: list, source: str = "") -> int:
    """Stage a peer's shared lore rows as candidates for the operator's merge.
    Never touches the live corpus. Deduped by (source, domain); bounded. Returns
    how many new candidates were staged. Inert when disabled/replaying."""
    if not enabled():
        return 0
    source = str(source or "peer")[:_STR_CAP]
    staged = 0
    try:
        with _mutex():
            existing = _load_staged()
            seen = {(e.get("from"), e.get("domain")) for e in existing
                    if isinstance(e, dict)}
            for row in (rows or [])[:_MAX_STAGED]:
                clean = _clean_share_row(row)
                if not clean:
                    continue
                key = (source, clean["domain"])
                if key in seen:
                    continue
                seen.add(key)
                clean["from"] = source
                existing.append(clean)
                staged += 1
            if staged:
                _save_staged(existing)
    except Exception:
        return 0
    return staged


def staged_shared() -> list[dict]:
    """Peer-shared lore awaiting the operator's merge (never auto-applied)."""
    try:
        return [e for e in _load_staged() if isinstance(e, dict)]
    except Exception:
        return []


def clear_staged() -> None:
    try:
        with _mutex():
            _save_staged([])
    except Exception:
        pass


def merge_staged(now: float | None = None) -> int:
    """OPERATOR-GATED: fold every staged peer row into the live corpus, purely
    additively — fill empty fields, OR-in booleans, raise a bias toward its cap,
    never overwrite local truth (visit/byte counts) and never relax a gate. Then
    clear the staging area. Returns the number of domains touched."""
    if not enabled():
        return 0
    now = now or time.time()
    touched = 0
    try:
        with _mutex():
            rows = _load_staged()
            if not rows:
                return 0
            records = _load()
            for row in rows:
                clean = _clean_share_row(row)
                if not clean:
                    continue
                dom = clean["domain"]
                r = records.get(dom)
                if r is None:
                    if len(records) >= MAX_DOMAINS:
                        continue                 # corpus full — bounded
                    r = DomainRecord(domain=dom, first_seen=now)
                    records[dom] = r
                # additive fill: only ever ADD knowledge, never subtract
                if clean["sitemap_url"] and not r.sitemap_url:
                    r.sitemap_url = clean["sitemap_url"]
                if clean["feed_url"] and not r.feed_url:
                    r.feed_url = clean["feed_url"]
                if clean["site_name"] and not r.site_name:
                    r.site_name = clean["site_name"]
                if clean["action_profile"] and not r.action_profile:
                    r.action_profile = clean["action_profile"]
                if clean["robots_disallow"]:
                    r.robots_disallow = True
                if clean["has_jsonld"]:
                    r.has_jsonld = True
                r.mobile_helped = min(_BIAS_CAP,
                                      max(r.mobile_helped, clean["mobile_helped"]))
                r.actions_helped = min(_BIAS_CAP,
                                       max(r.actions_helped, clean["actions_helped"]))
                # keep a merged gift fresh enough to survive the next prune
                r.last_seen = max(r.last_seen, now)
                touched += 1
            _save(records)
            _save_staged([])
    except Exception:
        return 0
    return touched


def report() -> str:
    s = stats()
    if not s.get("domains"):
        return "Web knowledge: nothing learned yet."
    return (f"Web knowledge: {s['domains']} domain(s) learned from "
            f"{s['total_visits']} visit(s) — {s['sitemaps']} sitemap(s) "
            f"discovered, {s['interactive']} site(s) known to need interaction, "
            f"{s['robots_disallowed']} disallowing crawl; "
            f"{int(s['success_rate'] * 100)}% fetch success.")
