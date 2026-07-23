"""Federation — mutually-authenticated links between Olympus instances.

`a2a.py` already lets Olympus answer another agent and (opt-in) call one, but a
peer there is just a URL: no identity, no trust model, no shared learning.
Federation adds the missing spine so two Olympus instances on different machines
can collaborate *safely*:

  * **Identity + handshake** — ed25519 challenge/response reusing the existing
    `witness` root of trust via a domain-separated subkey (`federation/v1`); no
    new crypto, no new dependency. A peer proves it holds the private key behind
    the public key you pinned.
  * **Signed envelopes** — every federation message is `witness`-signed over its
    `canonical_json`, so tampering is detectable and a peer's identity travels
    with its payload.
  * **Trust levels** — a pinned peer is `blocked`, `task` (may ask the council),
    or `trusted` (may also contribute lessons). Unknown peers get nothing.
  * **Governed transport** — outbound reuses `a2a`'s SSRF/egress-guarded,
    redirect-rechecking opener; the whole feature is opt-in (`OLYMPUS_FEDERATION`)
    and the inbound listener is bearer-token fail-closed.
  * **Shared-lesson sync** — a `trusted` peer's lessons are sanitised
    (`security.sanitize_for_memory`), wrapped as untrusted, and staged as memory
    CANDIDATES — never auto-committed — so cross-instance learning still passes
    Olympus's own gate.
  * **Capability discovery + multi-peer aggregation** — a pinned peer can fetch a
    SIGNED capabilities card (roster + skill count, no skill contents) to learn
    what another instance offers, and `call_peers` fans one task across several
    trusted peers, collecting each reply as untrusted data. Both reuse the same
    trust gate, signed envelope, and scrub machinery — deeper collaboration with
    no new trust surface.

The signing/parsing/dispatch core is PURE and dependency-injected (the inbound
`ask` and the outbound `fetcher` are parameters), so federation is unit-testable
with no sockets and no live council. Time and nonces live only at this network
boundary — off the council's replay hot path — exactly as `usermem`/`facts`
confine their clock use.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass

from . import a2a, security, store, witness

PROTOCOL = "olympus-federation/1"
LABEL = "federation/v1"            # witness domain-separated subkey label
_NS_PEERS = "federation.peers"
_NS_INBOX = "federation.lessons"
_MAX_LESSONS = 200
_MAX_SHARE = 1000                  # hard cap on domains in a shared-lore bundle
_MAX_INBOUND = 1_000_000           # hard cap on an inbound request body (F3)
_LOCK = threading.Lock()

TRUST_LEVELS = ("blocked", "task", "trusted")


def _max_age() -> int:
    """Optional freshness window for signed task/lesson envelopes, in seconds
    (`OLYMPUS_FEDERATION_MAX_AGE`). 0/unset disables the check (default), because
    a strict window is fragile under cross-instance clock skew; an operator who
    wants anti-replay on the wire sets it. Lesson-flood replay is defeated
    regardless by content dedup in `import_lessons`."""
    try:
        return max(0, int(os.environ.get("OLYMPUS_FEDERATION_MAX_AGE", "0")))
    except (TypeError, ValueError):
        return 0


def _too_old(payload: dict) -> bool:
    """True if `payload['at']` is older than the configured window (or malformed
    when a window is set). No window configured → never too old."""
    window = _max_age()
    if window <= 0:
        return False
    import calendar
    at = str((payload or {}).get("at") or "")
    try:
        epoch = calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return True                # a window is set but the stamp is unusable
    return (time.time() - epoch) > window


def enabled() -> bool:
    """Whether this instance may CALL peers and SERVE the federation endpoints.
    Default OFF — like A2A, reaching out and listening are both opt-in."""
    return os.environ.get("OLYMPUS_FEDERATION", "").strip().lower() in (
        "1", "on", "true", "yes")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- identity + signed envelopes (pure, reuse witness) -------------------

def public_key() -> str:
    """This instance's federation identity key (a witness subkey, distinct from
    the release/decision-log key)."""
    return witness.sub_public_key_hex(LABEL)


def identity() -> dict:
    """The public identity card a peer pins out of band."""
    return {"protocol": PROTOCOL, "name": _instance_name(),
            "publicKey": public_key(), "at": _now_iso()}


def _instance_name() -> str:
    return (os.environ.get("OLYMPUS_FEDERATION_NAME") or "olympus").strip()[:64]


def sign_envelope(payload: dict) -> dict:
    """Wrap `payload` with a detached ed25519 signature over its canonical JSON.
    Requires crypto (fail-closed — we never emit an unsigned federation message)."""
    if not witness.available():
        raise FederationError("cannot sign without the cryptography backend")
    body = witness.canonical_json(payload)
    return {"payload": payload,
            "integrity": {"publicKey": public_key(),
                          "signature": witness.sign_with(LABEL, body),
                          "protocol": PROTOCOL}}


def verify_envelope(envelope: dict, *, pin: str | None = None
                    ) -> tuple[bool, dict]:
    """Return `(ok, payload)`. `ok` iff the envelope is well-formed and its
    signature verifies against the enclosed public key — and, when `pin` is
    given, that key equals the pinned one. Fails closed without crypto."""
    if not isinstance(envelope, dict) or not witness.available():
        return False, {}
    payload = envelope.get("payload")
    integ = envelope.get("integrity") or {}
    pub = str(integ.get("publicKey") or "").strip()
    sig = str(integ.get("signature") or "")
    if not isinstance(payload, dict) or not pub or not sig:
        return False, {}
    if not witness.verify_signature(pub, witness.canonical_json(payload), sig):
        return False, {}
    if pin and pub.lower() != pin.lower():
        return False, {}
    return True, payload


class FederationError(ValueError):
    """A malformed or unauthorized federation exchange."""


# --- handshake (challenge / response) ------------------------------------

def make_challenge() -> dict:
    """A fresh nonce a peer must sign to prove key custody."""
    return {"protocol": PROTOCOL, "nonce": secrets.token_hex(16),
            "at": _now_iso()}


def answer_challenge(challenge: dict) -> dict:
    """Sign a peer's challenge nonce with our federation subkey (proves WE hold
    the private key behind our published public key)."""
    nonce = str((challenge or {}).get("nonce") or "")
    if not nonce:
        raise FederationError("challenge carries no nonce")
    return sign_envelope({"kind": "handshake", "nonce": nonce,
                          "name": _instance_name(), "at": _now_iso()})


def verify_challenge_response(response: dict, *, expected_nonce: str,
                              pin: str | None = None) -> bool:
    """True iff `response` is a valid signed handshake for `expected_nonce` from
    the pinned key. This is the gate that authenticates a peer's identity."""
    ok, payload = verify_envelope(response, pin=pin)
    if not ok or payload.get("kind") != "handshake":
        return False
    return str(payload.get("nonce") or "") == str(expected_nonce or "")


# --- peer registry (store-backed; trust levels) --------------------------

@dataclass
class Peer:
    name: str
    public_key: str
    url: str = ""
    trust: str = "task"

    def as_dict(self) -> dict:
        return {"name": self.name, "public_key": self.public_key,
                "url": self.url, "trust": self.trust}


def _load_peers() -> dict:
    raw = store.backend().get(_NS_PEERS, "registry")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def add_peer(name: str, public_key: str, *, url: str = "",
             trust: str = "task") -> Peer:
    """Pin a peer's public key and trust level. Pinning is what makes a peer
    known; an unpinned caller is always untrusted regardless of what it signs."""
    name = (name or "").strip()[:64]
    public_key = (public_key or "").strip()
    if not name or not public_key:
        raise FederationError("a peer needs a name and a public key")
    if trust not in TRUST_LEVELS:
        trust = "blocked"          # fail-closed: an unknown/typo'd level grants
        #                            nothing, never silently "task" (F5)
    peer = Peer(name=name, public_key=public_key, url=url.strip(), trust=trust)
    with _LOCK:
        peers = _load_peers()
        peers[name] = peer.as_dict()
        store.backend().put(_NS_PEERS, "registry",
                            json.dumps(peers).encode("utf-8"))
    return peer


def get_peer(name: str) -> Peer | None:
    d = _load_peers().get((name or "").strip())
    return Peer(**d) if d else None


def peer_by_key(public_key: str) -> Peer | None:
    key = (public_key or "").strip().lower()
    for d in _load_peers().values():
        if str(d.get("public_key", "")).lower() == key:
            return Peer(**d)
    return None


def peers() -> list[Peer]:
    return [Peer(**d) for d in _load_peers().values()]


def remove_peer(name: str) -> bool:
    with _LOCK:
        p = _load_peers()
        if name not in p:
            return False
        p.pop(name)
        store.backend().put(_NS_PEERS, "registry", json.dumps(p).encode("utf-8"))
    return True


def _trust_rank(trust: str) -> int:
    try:
        return TRUST_LEVELS.index(trust)
    except ValueError:
        return 0


# --- shared-lesson sync (sanitised, gated, candidate-only) ---------------

def _scrub(text: str) -> str:
    """Defang injection + redact secrets (`sanitize_for_memory`) AND strip PII
    (`anonymize`) — a lesson crossing to another instance enters a SHARED pool,
    which is exactly `anonymize`'s remit."""
    return security.anonymize(security.sanitize_for_memory(str(text)))[:2000]


def export_lessons(lessons: list[str]) -> dict:
    """A signed bundle of this instance's lessons for a trusted peer. Each lesson
    is scrubbed of secrets and PII before it leaves the box."""
    clean = [_scrub(t) for t in (lessons or []) if str(t).strip()]
    return sign_envelope({"kind": "lessons", "name": _instance_name(),
                          "lessons": clean[:_MAX_LESSONS], "at": _now_iso()})


def import_lessons(envelope: dict) -> dict:
    """Verify a peer's lesson bundle and STAGE the lessons as candidates for the
    operator's gate — never auto-committed. Rejects an envelope not signed by a
    `trusted` pinned peer. Returns a summary."""
    ok, payload = verify_envelope(envelope)
    if not ok or payload.get("kind") != "lessons":
        raise FederationError("lesson bundle failed signature verification")
    pub = str((envelope.get("integrity") or {}).get("publicKey") or "")
    peer = peer_by_key(pub)
    if not peer or _trust_rank(peer.trust) < _trust_rank("trusted"):
        raise FederationError("peer is not trusted for lesson sharing")
    if _too_old(payload):
        raise FederationError("lesson bundle is stale (outside freshness window)")
    with _LOCK:
        existing = []
        raw = store.backend().get(_NS_INBOX, "candidates")
        if raw:
            try:
                existing = json.loads(raw)
            except (ValueError, TypeError):
                existing = []
        # Dedupe by text against what's already staged. This defeats the
        # replay-flood: a captured, genuinely-signed bundle re-sent by a lesser
        # peer stages nothing new, so it cannot evict legitimate candidates out
        # of the _MAX_LESSONS window (F4).
        seen = {e.get("text") for e in existing if isinstance(e, dict)}
        staged = []
        for lesson in payload.get("lessons", [])[:_MAX_LESSONS]:
            text = _scrub(lesson)                # defense-in-depth re-scrub
            if text and text not in seen:
                seen.add(text)
                staged.append({"text": text, "from": peer.name})
        existing.extend(staged)
        store.backend().put(_NS_INBOX, "candidates",
                            json.dumps(existing[-_MAX_LESSONS:]).encode("utf-8"))
    return {"staged": len(staged), "from": peer.name}


def staged_lessons() -> list[dict]:
    """Peer lessons awaiting the operator's review (never applied automatically)."""
    raw = store.backend().get(_NS_INBOX, "candidates")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


# --- shared domain lore (raw per-domain facts, operator-gated) ------------
#
# Deeper than lesson sync: a `trusted` peer's *raw domain lore* — where a
# sitemap/feed lives, whether a site needs interaction (and the safe profile
# that works) or a mobile UA, its robots posture — crosses the wire, is scrubbed,
# and STAGES as candidates in the local corpus's own staging area. It is folded
# into the live corpus only by the operator's explicit merge. Nothing it carries
# can relax a fetch gate; every fetch is re-gated regardless of who contributed
# the hint. This is the domainlore moat compounding across a fleet without
# widening the trust surface.

def _scrub_share_row(row: dict) -> dict | None:
    """Scrub one shareable lore row before it leaves (or as it enters): the
    free-text `site_name` is defanged + PII-stripped; the domain is validated as
    a bare hostname; URLs pass through (a mangled URL simply fails the fetch gate
    later — safe); the action profile is bounded *structurally* (well-formed safe
    steps only). Verb safety is re-enforced at use, so a whole-string PII scrub —
    which would mangle the JSON and silently drop legitimate profiles — is the
    wrong tool here; the structural sanitizer is."""
    if not isinstance(row, dict):
        return None
    from . import domainlore
    dom = str(row.get("domain", "")).strip().lower()[:253]
    if not domainlore._valid_domain(dom):
        return None

    def _cnt(v):
        try:
            return max(0, min(_MAX_SHARE, int(v)))
        except (TypeError, ValueError):
            return 0
    return {"domain": dom,
            "sitemap_url": str(row.get("sitemap_url", ""))[:300],
            "feed_url": str(row.get("feed_url", ""))[:300],
            "site_name": _scrub(str(row.get("site_name", "")))[:200],
            "action_profile": domainlore._sanitize_profile(
                str(row.get("action_profile", ""))),
            "robots_disallow": bool(row.get("robots_disallow")),
            "has_jsonld": bool(row.get("has_jsonld")),
            "mobile_helped": _cnt(row.get("mobile_helped")),
            "actions_helped": _cnt(row.get("actions_helped"))}


def export_domainlore(min_visits: int = 2, limit: int = 500) -> dict:
    """A signed bundle of this instance's durable per-domain facts for a trusted
    peer. Only domains with a real track record cross; every field is scrubbed."""
    from . import domainlore
    rows = []
    try:
        rows = domainlore.shareable(min_visits=min_visits, limit=limit)
    except Exception:
        rows = []
    clean = [c for c in (_scrub_share_row(r) for r in rows) if c]
    return sign_envelope({"kind": "domainlore", "name": _instance_name(),
                          "domains": clean[:_MAX_SHARE], "at": _now_iso()})


def import_domainlore(envelope: dict) -> dict:
    """Verify a peer's lore bundle and STAGE its rows as candidates in the local
    corpus's staging area — never folded into the live corpus without the
    operator's merge. Rejects anything not signed by a `trusted` pinned peer."""
    ok, payload = verify_envelope(envelope)
    if not ok or payload.get("kind") != "domainlore":
        raise FederationError("lore bundle failed signature verification")
    pub = str((envelope.get("integrity") or {}).get("publicKey") or "")
    peer = peer_by_key(pub)
    if not peer or _trust_rank(peer.trust) < _trust_rank("trusted"):
        raise FederationError("peer is not trusted for lore sharing")
    if _too_old(payload):
        raise FederationError("lore bundle is stale (outside freshness window)")
    raw = payload.get("domains")
    rows = [c for c in (_scrub_share_row(r)
                        for r in (raw if isinstance(raw, list) else [])) if c]
    from . import domainlore
    staged = domainlore.stage_shared(rows[:_MAX_SHARE], source=peer.name)
    return {"staged": staged, "from": peer.name}


def push_domainlore(name: str, *, token: str | None = None, fetcher=None,
                    min_visits: int = 2, limit: int = 500) -> dict:
    """Send this instance's scrubbed domain lore to a pinned `trusted` peer.
    Opt-in and egress-gated exactly like `call_peer`; sharing raw lore requires
    the highest trust level (a `task` peer cannot receive it)."""
    if not enabled():
        raise FederationError("federation is disabled (set OLYMPUS_FEDERATION=1)")
    peer = get_peer(name)
    if not peer or not peer.url:
        raise FederationError(f"unknown or URL-less peer: {name}")
    if _trust_rank(peer.trust) < _trust_rank("trusted"):
        raise FederationError(f"peer {name} is not trusted for lore sharing")
    url = peer.url.rstrip("/") + "/federation/domainlore"
    reason = security.url_block_reason(url, resolve=False)
    if reason:
        raise FederationError(f"egress refused: {reason}")
    envelope = export_domainlore(min_visits=min_visits, limit=limit)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    fetch = fetcher or a2a._default_fetch
    status, body = fetch(url, json.dumps(envelope).encode("utf-8"), headers,
                         a2a._OUT_TIMEOUT)
    if int(status) >= 400:
        raise FederationError(f"peer {name} returned HTTP {status}")
    try:
        resp = json.loads(bytes(body)[:a2a._MAX_RESPONSE].decode("utf-8",
                                                                 "replace"))
    except (ValueError, json.JSONDecodeError):
        resp = {}
    return {"peer": peer.name,
            "sent": len(envelope["payload"]["domains"]),
            "staged": int(resp.get("staged", 0) or 0)
            if isinstance(resp, dict) else 0}


# --- capability discovery (signed card of what this instance can do) ------

def capabilities_card() -> dict:
    """A SIGNED summary of what this instance offers, so a peer can route by it:
    the instance name, its specialist roster (key + scrubbed title), and how many
    skills it has learned. Skill *contents* are deliberately NOT included — those
    cross only through the gated lesson sync. Titles are scrubbed because a
    file-defined agent's title is operator-supplied text."""
    specs: list[dict] = []
    try:
        from . import specialists
        for s in specialists.SPECIALISTS.values():
            specs.append({"key": str(s.key),
                          "title": _scrub(s.title)[:120]})
    except Exception:
        specs = []
    n_skills = 0
    try:
        from . import skills
        n_skills = int(skills.count())
    except Exception:
        n_skills = 0
    return sign_envelope({"kind": "capabilities", "name": _instance_name(),
                          "specialists": specs, "skills": n_skills,
                          "at": _now_iso()})


def _clean_card(payload: dict) -> dict:
    """Reduce a peer's capabilities payload to scrubbed, well-typed fields — a
    peer's roster text is untrusted, so it is sanitised before we return it."""
    raw_specs = payload.get("specialists")
    specs: list[dict] = []
    if isinstance(raw_specs, list):
        for s in raw_specs[:200]:
            if isinstance(s, dict):
                specs.append({"key": _scrub(str(s.get("key", "")))[:64],
                              "title": _scrub(str(s.get("title", "")))[:120]})
    try:
        n_skills = max(0, int(payload.get("skills", 0)))
    except (TypeError, ValueError):
        n_skills = 0
    return {"name": _scrub(str(payload.get("name", "")))[:64],
            "specialists": specs, "skills": n_skills}


# --- outbound (governed, injectable fetcher) -----------------------------

def call_peer(name: str, message: str, *, token: str | None = None,
              fetcher=None) -> str:
    """Send a SIGNED task to a pinned peer and return its answer AS UNTRUSTED
    DATA. Opt-in (`OLYMPUS_FEDERATION`) and egress-gated. The peer must be pinned
    with at least `task` trust and carry a URL; its reply envelope is verified
    against the pinned key before the (still untrusted) answer is returned.

    The authoritative SSRF / DNS-rebinding defense is the hardened opener inside
    `a2a._default_fetch` (it dials the validated IP and re-guards every redirect
    hop); the name-level check here refuses the obvious localhost/metadata URLs
    up front. `token` is the peer's inbound bearer credential; `fetcher` is
    injectable for tests."""
    if not enabled():
        raise FederationError("federation is disabled (set OLYMPUS_FEDERATION=1)")
    peer = get_peer(name)
    if not peer or not peer.url:
        raise FederationError(f"unknown or URL-less peer: {name}")
    if _trust_rank(peer.trust) < _trust_rank("task"):
        raise FederationError(f"peer {name} is blocked")
    url = peer.url.rstrip("/") + "/federation/task"
    reason = security.url_block_reason(url, resolve=False)
    if reason:
        raise FederationError(f"egress refused: {reason}")
    envelope = sign_envelope({"kind": "task", "text": str(message),
                              "at": _now_iso()})
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    fetch = fetcher or a2a._default_fetch
    status, body = fetch(url, json.dumps(envelope).encode("utf-8"), headers,
                         a2a._OUT_TIMEOUT)
    if int(status) >= 400:
        return security.wrap_untrusted(f"[peer returned HTTP {status}]",
                                       source="federation-peer")
    try:
        resp = json.loads(bytes(body)[:a2a._MAX_RESPONSE].decode("utf-8",
                                                                 "replace"))
    except (ValueError, json.JSONDecodeError):
        resp = {}
    ok, payload = verify_envelope(resp, pin=peer.public_key)
    answer = str(payload.get("answer", "")) if ok else "[unverified peer reply]"
    return security.wrap_untrusted(answer, source="federation-peer")


def discover_peer(name: str, *, token: str | None = None, fetcher=None) -> dict:
    """Ask a pinned peer for its capabilities card and return it as scrubbed,
    well-typed data. Sends a SIGNED request (so the peer can trust-check us),
    verifies the reply against the pinned key, and sanitises every text field —
    a peer's roster is untrusted. Opt-in and egress-gated, exactly like
    `call_peer`; the peer must be pinned with at least `task` trust and a URL."""
    if not enabled():
        raise FederationError("federation is disabled (set OLYMPUS_FEDERATION=1)")
    peer = get_peer(name)
    if not peer or not peer.url:
        raise FederationError(f"unknown or URL-less peer: {name}")
    if _trust_rank(peer.trust) < _trust_rank("task"):
        raise FederationError(f"peer {name} is blocked")
    url = peer.url.rstrip("/") + "/federation/capabilities"
    reason = security.url_block_reason(url, resolve=False)
    if reason:
        raise FederationError(f"egress refused: {reason}")
    envelope = sign_envelope({"kind": "capabilities_request", "at": _now_iso()})
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    fetch = fetcher or a2a._default_fetch
    status, body = fetch(url, json.dumps(envelope).encode("utf-8"), headers,
                         a2a._OUT_TIMEOUT)
    if int(status) >= 400:
        raise FederationError(f"peer {name} returned HTTP {status}")
    try:
        resp = json.loads(bytes(body)[:a2a._MAX_RESPONSE].decode("utf-8",
                                                                 "replace"))
    except (ValueError, json.JSONDecodeError):
        resp = {}
    ok, payload = verify_envelope(resp, pin=peer.public_key)
    if not ok or payload.get("kind") != "capabilities":
        raise FederationError(f"peer {name}: unverified capabilities reply")
    card = _clean_card(payload)
    card["peer"] = peer.name          # the local pinned name, authoritative
    return card


def call_peers(names: list[str], message: str, *, token: str | None = None,
               fetcher=None) -> list[dict]:
    """Fan the same task out to several pinned peers and collect their answers.

    Deterministic: peers are queried in the given order and each result is
    `{"peer": name, "answer": <untrusted-wrapped>}`; a peer that errors yields
    `{"peer": name, "error": "..."}` instead of aborting the aggregation, so one
    unreachable peer never sinks the rest. Every answer stays wrapped-untrusted —
    the caller (the council) treats the federation's replies as data, never
    instructions. `token` is applied to every peer (per-peer tokens are a future
    refinement); `fetcher` is injectable for tests."""
    if not enabled():
        raise FederationError("federation is disabled (set OLYMPUS_FEDERATION=1)")
    out: list[dict] = []
    seen: set[str] = set()
    for name in names or []:
        name = (name or "").strip()
        if not name or name in seen:
            continue                      # skip blanks + duplicates deterministically
        seen.add(name)
        try:
            answer = call_peer(name, message, token=token, fetcher=fetcher)
            out.append({"peer": name, "answer": answer})
        except Exception as err:      # incl. network errors from the fetcher —
            #                           isolate so one dead peer never aborts the
            #                           fan-out (best-effort at the net boundary)
            out.append({"peer": name, "error": str(err) or type(err).__name__})
    return out


def trusted_peer_names(min_trust: str = "task") -> list[str]:
    """Pinned peers with a URL and at least `min_trust`, sorted by name — the
    default fan-out set for `call_peers`/`ask-all`."""
    floor = _trust_rank(min_trust)
    return sorted(p.name for p in peers()
                  if p.url and _trust_rank(p.trust) >= floor)


# --- inbound dispatcher (pure; ask + auth injected) ----------------------

def _bearer_token() -> str | None:
    path = os.environ.get("OLYMPUS_FEDERATION_TOKEN_FILE")
    if path:
        try:
            return open(path, encoding="utf-8").read().strip() or None
        except OSError as err:
            raise FederationError(f"federation token file unreadable: {err}")
    tok = os.environ.get("OLYMPUS_FEDERATION_TOKEN")
    return tok.strip() if tok and tok.strip() else None


def _auth_ok(headers: dict) -> bool:
    """Constant-time bearer check. Fail-closed: serving with no token configured
    — OR a transiently-unreadable token file — refuses every authenticated route
    (a clean 401, never a 500 that could mask monitoring) (F7)."""
    import hmac
    try:
        want = _bearer_token()
    except FederationError:
        return False
    if not want:
        return False
    got = ""
    for k, v in (headers or {}).items():
        if str(k).lower() == "authorization":
            got = str(v)
            break
    if got.lower().startswith("bearer "):
        got = got[7:]
    return hmac.compare_digest(got.strip(), want)


def handle_request(method: str, path: str, body: dict, headers: dict,
                   *, ask=None) -> tuple[int, dict]:
    """Route one inbound federation request. Pure + injectable:

      GET  /federation/identity      → public identity card (no auth)
      POST /federation/handshake     → sign the peer's challenge (no auth; proves us)
      POST /federation/task          → run a peer task on the council (auth + pinned)
      POST /federation/lessons       → stage a trusted peer's lessons (auth + signed)
      POST /federation/capabilities  → signed capabilities card (auth + pinned)

    `ask(text) -> str` is injected (the council entry) so this is testable with no
    live pipeline. Authenticated routes fail closed without a configured token."""
    method = (method or "GET").upper()
    path = (path or "").rstrip("/") or "/"

    if method == "GET" and path == "/federation/identity":
        return 200, identity()

    if method == "POST" and path == "/federation/handshake":
        try:
            return 200, answer_challenge(body or {})
        except FederationError as err:
            return 400, {"error": str(err)}

    # Authenticated routes: gate ONLY the known ones, so a genuinely unknown
    # path 404s (doesn't masquerade as an auth failure).
    if (method, path) not in (("POST", "/federation/task"),
                              ("POST", "/federation/lessons"),
                              ("POST", "/federation/domainlore"),
                              ("POST", "/federation/capabilities")):
        return 404, {"error": "unknown federation route"}
    if not _auth_ok(headers):
        return 401, {"error": "federation auth required"}

    if method == "POST" and path == "/federation/capabilities":
        # Signed by a pinned peer with >= task trust (same gate as a task) — a
        # peer proves who it is before it can enumerate what we offer.
        ok, payload = verify_envelope(body or {})
        pub = str(((body or {}).get("integrity") or {}).get("publicKey") or "")
        peer = peer_by_key(pub) if ok else None
        if not ok or not peer or _trust_rank(peer.trust) < _trust_rank("task"):
            return 403, {"error": "unknown, unsigned, or blocked peer"}
        if payload.get("kind") != "capabilities_request":
            return 400, {"error": "not a capabilities request"}
        if _too_old(payload):
            return 408, {"error": "capabilities request is stale"}
        return 200, capabilities_card()

    if method == "POST" and path == "/federation/task":
        # Envelope-signed by a pinned peer with >= task trust.
        ok, payload = verify_envelope(body or {})
        pub = str(((body or {}).get("integrity") or {}).get("publicKey") or "")
        peer = peer_by_key(pub) if ok else None
        if not ok or not peer or _trust_rank(peer.trust) < _trust_rank("task"):
            return 403, {"error": "unknown, unsigned, or blocked peer"}
        if _too_old(payload):
            return 408, {"error": "task envelope is stale"}
        text = str(payload.get("text") or "")
        if not text:
            return 400, {"error": "task carries no text"}
        # Peer text is untrusted — wrap before it reaches the council.
        wrapped = security.wrap_untrusted(text, source="federation-peer")
        answer = ask(wrapped) if callable(ask) else ""
        return 200, sign_envelope({"kind": "task_result", "answer": str(answer),
                                   "at": _now_iso()})

    if method == "POST" and path == "/federation/lessons":
        try:
            return 200, import_lessons(body or {})
        except FederationError as err:
            return 403, {"error": str(err)}

    if method == "POST" and path == "/federation/domainlore":
        try:
            return 200, import_domainlore(body or {})
        except FederationError as err:
            return 403, {"error": str(err)}

    return 404, {"error": "unknown federation route"}


def run_server(ask, *, host: str = "127.0.0.1", port: int = 8489) -> None:      # pragma: no cover
    """Minimal HTTP listener over `handle_request`. Refuses to start unless
    federation is enabled AND a bearer token is configured (fail-closed). Bound
    to loopback by default — expose only behind a reverse proxy you control."""
    if not enabled():
        raise FederationError("federation is disabled (set OLYMPUS_FEDERATION=1)")
    if not _bearer_token():
        raise FederationError("refusing to serve without OLYMPUS_FEDERATION_TOKEN")
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method):
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                length = 0
            # Cap the pre-auth read so an oversized Content-Length can't exhaust
            # memory before any check runs (F3). Reject rather than truncate.
            if length > _MAX_INBOUND:
                self.send_response(413)
                self.end_headers()
                return
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw) if raw else {}
                if not isinstance(data, dict):
                    data = {}
            except (ValueError, RecursionError):   # incl. deeply-nested JSON
                data = {}
            hdrs = {k: v for k, v in self.headers.items()}
            status, payload = handle_request(method, self.path, data, hdrs,
                                             ask=ask)
            blob = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *_):
            pass

    HTTPServer((host, port), _Handler).serve_forever()
