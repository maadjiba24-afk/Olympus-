"""Web build-proposals — turn discoveries into auto-drafted, human-actionable plans.

`webreflect` notices *patterns* in the domain-lore corpus (a cohort of sites that
need interaction, a cohort that fetches poorly). This module turns each such
**proposal-kind discovery** into a structured, evidence-backed *build proposal*:
a title, the motivation, the evidence (which domains, what they show), a concrete
proposed change *in Olympus's own idioms*, its safety posture, and acceptance
criteria — then stages it for a human to accept, decline, or build.

It is deliberately NOT a code generator. A proposal is a queued engineering
artifact the operator reviews on `olympus webknowledge --proposals`; nothing here
writes code, changes behavior, or acts on its own. This is Olympus "discovering
new features over time" as reviewable proposals, not autonomous self-modification.

Design (absorbed-capability contract, ADR 0008):
  * **Replay-inert & bounded.** Drafting is a no-op under `OLYMPUS_REPLAY`; the
    store is capped and corrupt-quarantining (never wiped).
  * **Deterministic.** A proposal's id is a hash of its discovery key, so the
    same standing pattern drafts once and re-drafting is idempotent — no clock,
    no randomness in the identity.
  * **Additive only.** A proposal never mutates the corpus or any behavior; the
    operator's decision (accept/decline/built) is the only state it carries.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field, fields

from . import config

MAX_PROPOSALS = 500
_STR_CAP = 2000
STATUSES = ("drafted", "accepted", "declined", "built")


@dataclass
class ProposalRecord:
    id: str
    key: str
    kind: str
    title: str
    motivation: str = ""
    evidence: str = ""
    proposal: str = ""
    safety: str = ""
    acceptance: list = field(default_factory=list)
    status: str = "drafted"
    created: float = 0.0
    updated: float = 0.0


# A per-discovery-kind template: how a raw discovery becomes a full proposal.
# Each entry supplies the durable framing; the evidence is filled from the live
# discovery so the proposal always cites real domains.
_TEMPLATES = {
    "interactive": {
        "title": "Promote learned action-profiles to a default for "
                 "interaction-gated domains",
        "motivation": "A cohort of domains consistently yields more content only "
                      "after interaction (scroll/expand/click). Today a caller "
                      "must pass `actions` by hand, or the operator must enable "
                      "`OLYMPUS_WEB_AUTO_ACTIONS` globally.",
        "proposal": "Curate the per-domain action-profiles the corpus has already "
                    "learned into a reviewed default set, and consider enabling "
                    "auto-application (`OLYMPUS_WEB_AUTO_ACTIONS`) for that vetted "
                    "cohort — so a plain `scrape` of these domains gets the full "
                    "page with no manual `actions` argument.",
        "safety": "Every profile runs only through the governed browser harness "
                  "(SSRF-gated navigation, safe-verb allowlist, ledgered CDP); "
                  "ungoverned JS is refused. Each step is re-validated on use and "
                  "the whole behavior is opt-in and replay-inert.",
        "acceptance": ["A plain scrape (no `actions`) of each listed domain "
                       "returns >=1.2x its byte baseline.",
                       "No profile executes a verb outside the safe allowlist.",
                       "Behavior is off by default and inert under replay."],
    },
    "flaky": {
        "title": "Fetch-resilience escalation for low-success domains",
        "motivation": "A cohort of domains fetches below 50% success under the "
                      "default request posture — likely UA/timeout/anti-bot "
                      "sensitivity that a single fixed strategy can't cover.",
        "proposal": "Add a per-domain resilience escalation the corpus drives: on "
                    "repeated failure for a domain, try a mobile UA, then a longer "
                    "(already self-tuned) timeout, then an egress proxy — learning "
                    "which rung restores success and biasing to it next time, the "
                    "same learn->apply loop already used for the mobile bias.",
        "safety": "Every rung still routes through the IP-pinned, egress-gated "
                  "fetch path; a proxy is used only when explicitly configured. "
                  "Purely additive — an escalation never opens a fetch the gate "
                  "would refuse.",
        "acceptance": ["Fetch success for each listed domain rises above 50% "
                       "over a trailing window.",
                       "No new egress path bypasses `url_block_reason`/the pinned "
                       "opener.",
                       "The escalation is bounded and self-limiting."],
    },
}


def _path():
    return config.MEMORY_DIR / "webproposals.json"


def enabled() -> bool:
    """Drafting mutates state and reads the wall clock, so it is inert under
    replay. It has no separate opt-in — it is driven by `webreflect`, which is
    itself opt-in (`OLYMPUS_WEB_REFLECT`)."""
    return not os.environ.get("OLYMPUS_REPLAY")


def _mutex():
    from . import proclock
    return proclock.lock("webproposals")


def _load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        try:
            p.replace(p.with_name(p.name + ".corrupt"))    # quarantine, don't wipe
        except OSError:
            pass
        return {}
    if not isinstance(raw, dict):
        return {}
    known = {f.name for f in fields(ProposalRecord)}
    out: dict = {}
    for pid, d in raw.items():
        if isinstance(d, dict):
            try:
                out[pid] = ProposalRecord(**{k: v for k, v in d.items()
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


def _pid(key: str) -> str:
    """Deterministic id for a discovery key (no clock/randomness — replay-safe)."""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def draft_from_discoveries(discoveries: list[dict],
                           now: float | None = None) -> list["ProposalRecord"]:
    """Draft a build proposal for each proposal-kind discovery not already on
    file. Idempotent (deduped by discovery key), bounded, replay-inert. Returns
    the newly-drafted proposals."""
    if not enabled():
        return []
    now = now or time.time()
    drafted: list[ProposalRecord] = []
    try:
        with _mutex():
            records = _load()
            have = {r.key for r in records.values()}
            for d in discoveries or []:
                if not isinstance(d, dict) or d.get("kind") != "proposal":
                    continue
                key = str(d.get("key", ""))
                if not key or key in have:
                    continue
                if len(records) >= MAX_PROPOSALS:
                    break                        # bounded
                tmpl = _template_for(key)
                pid = _pid(key)
                if pid in records:               # id collision guard
                    continue
                rec = ProposalRecord(
                    id=pid, key=key, kind="proposal",
                    title=tmpl["title"][:_STR_CAP],
                    motivation=tmpl["motivation"][:_STR_CAP],
                    evidence=str(d.get("detail", ""))[:_STR_CAP],
                    proposal=tmpl["proposal"][:_STR_CAP],
                    safety=tmpl["safety"][:_STR_CAP],
                    acceptance=list(tmpl["acceptance"])[:10],
                    status="drafted", created=now, updated=now)
                records[pid] = rec
                have.add(key)
                drafted.append(rec)
            if drafted:
                _save(records)
    except Exception:
        return []
    return drafted


def _template_for(key: str) -> dict:
    """Pick the drafting template for a discovery key (its prefix names the kind).
    Falls back to a generic frame so an unknown proposal is still well-formed."""
    prefix = key.split(":", 1)[0]
    tmpl = _TEMPLATES.get(prefix)
    if tmpl:
        return tmpl
    return {"title": f"Investigate standing web pattern: {prefix}",
            "motivation": "The corpus surfaced a recurring pattern worth a "
                          "deliberate capability review.",
            "proposal": "Review the cited evidence and decide whether a native "
                        "capability should grow to address it.",
            "safety": "No behavior changes until a human builds and gates it.",
            "acceptance": ["A decision is recorded (build / decline)."]}


def all_() -> list["ProposalRecord"]:
    """Every proposal, newest first (deterministic tie-break by id)."""
    try:
        recs = list(_load().values())
    except Exception:
        return []
    return sorted(recs, key=lambda r: (-r.created, r.id))


def pending() -> list["ProposalRecord"]:
    """Proposals still awaiting a decision."""
    return [r for r in all_() if r.status == "drafted"]


def get(pid: str) -> "ProposalRecord | None":
    try:
        return _load().get((pid or "").strip())
    except Exception:
        return None


def set_status(pid: str, status: str, now: float | None = None) -> bool:
    """Record the operator's decision on a proposal. Never generates or runs
    code — only the review state changes."""
    status = (status or "").strip().lower()
    if status not in STATUSES:
        return False
    now = now or time.time()
    try:
        with _mutex():
            records = _load()
            r = records.get((pid or "").strip())
            if r is None:
                return False
            r.status = status
            r.updated = now
            _save(records)
    except Exception:
        return False
    return True


def stats() -> dict:
    recs = all_()
    by = {s: sum(1 for r in recs if r.status == s) for s in STATUSES}
    return {"total": len(recs), **by}


def render() -> str:
    recs = all_()
    if not recs:
        return "Build proposals: none drafted yet."
    lines = [f"Build proposals ({len(recs)}):"]
    for r in recs:
        lines.append(f"  [{r.id}] ({r.status}) {r.title}")
    return "\n".join(lines)
