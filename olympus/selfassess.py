"""Self-assessment — Olympus attacks the app YOU built, on your own machine.

When you create an app or site in Olympus and run it LOCALLY (in the confined
sandbox, bound to loopback), this drives Olympus's benign-but-real assessment
suite against it end-to-end: recon → HTTP-header audit → a bounded same-origin
crawl that discovers endpoints/parameters → the active-validation checks
(reflection/XSS, SSTI, open redirect, CRLF, CORS, SQL-injection surface, verbose-
error disclosure) across everything it found → optional SAST/secret/dependency
scan of the app's source. It produces the same CVSS-scored, SARIF-exportable
findings as the rest of Aegis.

The line that keeps this YOURS and not a weapon:

  * **Loopback-only, enforced in code.** A self-assessment target MUST be a
    loopback host (127.0.0.1 / localhost / ::1). Anything routable is refused
    here AND at the SSRF layer (`security.allow_local_target` can only be armed
    for a loopback host, and the pin still requires a loopback resolution). So
    this can test the app on your machine and *nothing else* — never a third
    party.
  * **Confirmation, not weaponization.** Every check confirms a weakness with an
    inert marker (a benign quote, an arithmetic template, a reserved-TLD canary)
    — it proves WHERE the hole is without dumping your database, dropping a
    shell, or persisting. That is what "find the vulnerabilities" needs.
  * **Bounded + audited.** The crawl is page-capped, each validation is probe-
    capped, egress is confined to the target, and every finding is ledgered.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlparse, urlunparse

from . import assess, security

# Bounds — a self-assessment is thorough but never unbounded.
_MAX_PAGES = 40                 # pages fetched during the discovery crawl
_MAX_VALIDATE = 60              # distinct param-bearing URLs actively validated

_LOCAL_ONLY = (
    "self-assessment targets YOUR OWN app running locally "
    "(http://127.0.0.1:PORT or http://localhost:PORT). For any other target, "
    "use `assess run` with an explicit, signed authorization — Olympus never "
    "attacks a host you have not proven you own.")

_HREF_RE = re.compile(r"""(?:href|action)\s*=\s*["']([^"'#\s]+)["']""", re.I)
# Capture the opening-tag attributes (g1) and the form body (g2) separately so we
# can read the method and scan for an anti-CSRF token field.
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)
_ACTION_RE = re.compile(r"""action\s*=\s*["']([^"']*)["']""", re.I)
_METHOD_RE = re.compile(r"""method\s*=\s*["']?\s*(post|get)""", re.I)
_INPUT_NAME_RE = re.compile(r"""<(?:input|textarea|select)\b[^>]*\bname\s*=\s*["']([^"']+)["']""", re.I)
# Substrings that mark a hidden anti-CSRF token field. If a state-changing POST
# form carries none of these, it's a CSRF surface (heuristic — header/SameSite
# protection is also valid, so this is reported at medium confidence).
_CSRF_HINTS = ("csrf", "xsrf", "authenticity_token", "_token", "requestverification",
               "nonce", "anti-forgery", "antiforgery")


def is_local_target(url: str) -> bool:
    """True only for an http(s) URL whose host is loopback."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return (p.scheme.lower() in ("http", "https")
            and security._host_is_loopback(p.hostname or ""))


def _origin(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) or None if unparseable."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if not host:
        return None
    scheme = p.scheme.lower()
    try:
        port = p.port or (443 if scheme == "https" else 80)
    except ValueError:                          # malformed port
        return None
    return (scheme, host, port)


def _same_origin(a: str, b: str) -> bool:
    """Strict same-origin: scheme + host + PORT. Comparing the port too keeps the
    crawl on the exact app under test and refuses a crafted link that would make
    the crawler probe another local port (the allowance blocks it anyway)."""
    oa, ob = _origin(a), _origin(b)
    return oa is not None and oa == ob


def _strip_fragment(url: str) -> str:
    try:
        p = urlparse(url)
        return urlunparse(p._replace(fragment=""))
    except ValueError:
        return url


def _csrf_finding(page_url: str, action: str):
    """A CWE-352 finding for a state-changing POST form lacking an anti-CSRF token."""
    return assess.Finding(
        title="State-changing form without an anti-CSRF token",
        severity="medium", cwe="CWE-352",
        cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:N/I:L/A:N",
        location=f"{action} [POST form]",
        evidence="a POST form carries no hidden anti-CSRF token field",
        remediation="Add a per-session anti-CSRF token to state-changing forms "
                    "and verify it server-side, or protect the session cookie with "
                    "SameSite=Lax/Strict. Heuristic: header/SameSite-based CSRF "
                    "defense is also valid, so confirm before treating as a bug.",
        confidence="medium", source="active_validation")


def _discover(base_url: str, max_pages: int) -> tuple[list[str], list]:
    """Bounded same-origin crawl from `base_url`. Returns
    (param-bearing URLs to validate, CSRF findings for token-less POST forms).
    Every fetch is loopback-confined by the caller's allowance."""
    seen: set[str] = set()
    queue: list[str] = [_strip_fragment(base_url)]
    param_urls: dict[str, str] = {}      # canonical (path+sorted params) -> url
    csrf: dict[str, object] = {}         # location -> Finding (dedup)

    def _remember_params(url: str) -> None:
        p = urlparse(url)
        if not p.query:
            return
        keys = tuple(sorted(k for k, _ in parse_qsl(p.query, keep_blank_values=True)))
        if keys:
            param_urls.setdefault(f"{p.path}?{','.join(keys)}", url)

    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen or not _same_origin(url, base_url):
            continue
        seen.add(url)
        _remember_params(url)
        try:
            # via assess._probe so the auth session (if any) reaches behind-login
            # pages during discovery, exactly as it does during validation.
            probe = assess._probe(url, max_bytes=300_000, follow_redirects=True)
        except ValueError:
            continue                     # blocked (shouldn't happen for loopback)
        body = probe.get("body", "") or ""
        if "html" not in (probe.get("headers", {}) or {}).get("content-type", "html"):
            # still scan the body for links; many dev servers omit content-type
            pass
        for raw in _HREF_RE.findall(body)[:200]:
            if raw.lower().startswith(("mailto:", "javascript:", "tel:", "data:")):
                continue
            nxt = _strip_fragment(urljoin(url, raw))
            if _same_origin(nxt, base_url):
                _remember_params(nxt)
                if nxt not in seen and len(seen) + len(queue) < max_pages * 2:
                    queue.append(nxt)
        for attrs, form_body in _FORM_RE.findall(body)[:40]:
            names = _INPUT_NAME_RE.findall(form_body)
            m = _ACTION_RE.search(attrs) or _ACTION_RE.search(form_body)
            action = urljoin(url, m.group(1)) if (m and m.group(1)) else url
            if not _same_origin(action, base_url):
                continue
            mm = _METHOD_RE.search(attrs)
            is_post = bool(mm) and mm.group(1).lower() == "post"
            if is_post and names:
                # State-changing form → is there an anti-CSRF token field?
                blob = form_body.lower()
                if not any(h in blob for h in _CSRF_HINTS):
                    # dedup by the form ENDPOINT (action), not the embedding page,
                    # so one token-less form is one finding however many pages show it
                    csrf.setdefault(action, _csrf_finding(url, action))
            elif names:
                # GET form → synthesize a param-bearing URL from the input names.
                fields = list(dict.fromkeys(names))[:8]      # de-dup, cap
                q = "&".join(f"{n}=test" for n in fields)
                synth = f"{action}{'&' if urlparse(action).query else '?'}{q}"
                _remember_params(synth)
    return list(param_urls.values())[:_MAX_VALIDATE], list(csrf.values())


def selfassess(base_url: str, *, source_path: str | None = None,
               cookie: str | None = None, max_pages: int = _MAX_PAGES,
               user: str | None = None) -> dict[str, Any]:
    """Attack YOUR OWN local app end-to-end and report its vulnerabilities.

    `base_url` must be a loopback URL (the app you built, running locally). Runs
    recon + HTTP-header audit + a bounded same-origin crawl + benign active
    validation across every discovered parameter, plus SAST/secret/dependency
    scans of `source_path` when given. Returns a structured report; findings are
    recorded to the store (CVSS-scored, dedup'd, SARIF-exportable). Refuses any
    non-loopback target.

    `cookie` (optional): a session cookie for YOUR local app (e.g.
    "session=abc123"), so the crawl and validation reach BEHIND a login — how you
    tell Olympus to test authenticated pages of the app you built."""
    if not is_local_target(base_url):
        return {"refused": True, "error": _LOCAL_ONLY, "base_url": base_url}

    p = urlparse(base_url)
    host = (p.hostname or "").lower()
    port = p.port or (443 if p.scheme.lower() == "https" else 80)
    auth_headers = {"Cookie": cookie} if cookie else None

    # Authorize the loopback target (self-owned) and confine egress to it, so even
    # a hijacked crawl cannot leave the local app. The SSRF loopback allowance is
    # armed ONLY for this exact host:port (and can only be armed for loopback).
    assess.grant([host], note="self-assessment (local app)")
    phases: list[str] = []
    crawl_urls: list[str] = []
    with security.allow_local_target(host, port), \
            assess.confined_egress(user, targets=[host]), \
            assess.auth_session(auth_headers):
        if auth_headers:
            phases.append("authenticated")
        try:
            assess.recon(base_url, user)
            phases.append("recon")
        except Exception:
            pass
        try:
            assess.http_audit(base_url, user=user)
            phases.append("http_audit")
        except Exception:
            pass
        crawl_urls, csrf_findings = _discover(base_url, max_pages)
        phases.append(f"crawl({len(crawl_urls)} param URLs)")
        for f in csrf_findings:
            try:
                assess.record_finding(f, user)
            except Exception:
                continue
        if csrf_findings:
            phases.append(f"csrf({len(csrf_findings)})")
        validated = 0
        for url in crawl_urls:
            try:
                assess.validate(url, user)
                validated += 1
            except Exception:
                continue
        phases.append(f"validate({validated})")

    # Whitebox scans run on local source — no network, no allowance needed.
    if source_path:
        assess.grant(["local"], note="self-assessment (source)")
        for label, fn in (("sast", assess.sast_scan), ("secrets", assess.secret_scan),
                          ("deps", assess.dep_audit)):
            try:
                fn(source_path, user=user)
                phases.append(label)
            except Exception:
                continue

    findings = assess.list_findings(user)
    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f.get("severity", "?")] = by_sev.get(f.get("severity", "?"), 0) + 1
    return {
        "base_url": base_url,
        "phases": phases,
        "crawled_param_urls": len(crawl_urls),
        "findings": findings,
        "total_findings": len(findings),
        "by_severity": by_sev,
    }
