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
_CRAWL_TIMEOUT = 8             # per-page fetch timeout (seconds)

_LOCAL_ONLY = (
    "self-assessment targets YOUR OWN app running locally "
    "(http://127.0.0.1:PORT or http://localhost:PORT). For any other target, "
    "use `assess run` with an explicit, signed authorization — Olympus never "
    "attacks a host you have not proven you own.")

_HREF_RE = re.compile(r"""(?:href|action)\s*=\s*["']([^"'#\s]+)["']""", re.I)
_FORM_RE = re.compile(r"<form\b[^>]*>(.*?)</form>", re.I | re.S)
_ACTION_RE = re.compile(r"""action\s*=\s*["']([^"']*)["']""", re.I)
_INPUT_NAME_RE = re.compile(r"""<(?:input|textarea|select)\b[^>]*\bname\s*=\s*["']([^"']+)["']""", re.I)


def is_local_target(url: str) -> bool:
    """True only for an http(s) URL whose host is loopback."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return (p.scheme.lower() in ("http", "https")
            and security._host_is_loopback(p.hostname or ""))


def _same_host(a: str, b: str) -> bool:
    try:
        return (urlparse(a).hostname or "").lower() == (urlparse(b).hostname or "").lower()
    except ValueError:
        return False


def _strip_fragment(url: str) -> str:
    p = urlparse(url)
    return urlunparse(p._replace(fragment=""))


def _discover(base_url: str, max_pages: int) -> list[str]:
    """Bounded same-origin crawl from `base_url`. Returns distinct param-bearing
    URLs (query strings + GET-form inputs synthesized as params) to validate.
    Every fetch is loopback-confined by the caller's allowance."""
    from . import tools
    seen: set[str] = set()
    queue: list[str] = [_strip_fragment(base_url)]
    param_urls: dict[str, str] = {}      # canonical (path+sorted params) -> url

    def _remember_params(url: str) -> None:
        p = urlparse(url)
        if not p.query:
            return
        keys = tuple(sorted(k for k, _ in parse_qsl(p.query, keep_blank_values=True)))
        if keys:
            param_urls.setdefault(f"{p.path}?{','.join(keys)}", url)

    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen or not _same_host(url, base_url):
            continue
        seen.add(url)
        _remember_params(url)
        try:
            probe = tools._http_probe(url, max_bytes=300_000,
                                      follow_redirects=True)
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
            if _same_host(nxt, base_url):
                _remember_params(nxt)
                if nxt not in seen and len(seen) + len(queue) < max_pages * 2:
                    queue.append(nxt)
        # GET forms → synthesize a param-bearing URL from the input names.
        for form in _FORM_RE.findall(body)[:40]:
            names = _INPUT_NAME_RE.findall(form)
            if not names:
                continue
            m = _ACTION_RE.search(form)
            action = urljoin(url, m.group(1)) if (m and m.group(1)) else url
            if not _same_host(action, base_url):
                continue
            fields = list(dict.fromkeys(names))[:8]      # de-dup, cap
            q = "&".join(f"{n}=test" for n in fields)
            synth = f"{action}{'&' if urlparse(action).query else '?'}{q}"
            _remember_params(synth)
    return list(param_urls.values())[:_MAX_VALIDATE]


def selfassess(base_url: str, *, source_path: str | None = None,
               max_pages: int = _MAX_PAGES, user: str | None = None) -> dict[str, Any]:
    """Attack YOUR OWN local app end-to-end and report its vulnerabilities.

    `base_url` must be a loopback URL (the app you built, running locally). Runs
    recon + HTTP-header audit + a bounded same-origin crawl + benign active
    validation across every discovered parameter, plus SAST/secret/dependency
    scans of `source_path` when given. Returns a structured report; findings are
    recorded to the store (CVSS-scored, dedup'd, SARIF-exportable). Refuses any
    non-loopback target."""
    if not is_local_target(base_url):
        return {"refused": True, "error": _LOCAL_ONLY, "base_url": base_url}

    p = urlparse(base_url)
    host = (p.hostname or "").lower()
    port = p.port or (443 if p.scheme.lower() == "https" else 80)

    # Authorize the loopback target (self-owned) and confine egress to it, so even
    # a hijacked crawl cannot leave the local app. The SSRF loopback allowance is
    # armed ONLY for this exact host:port (and can only be armed for loopback).
    assess.grant([host], note="self-assessment (local app)")
    phases: list[str] = []
    crawl_urls: list[str] = []
    try:
        with security.allow_local_target(host, port), \
                assess.confined_egress(user, targets=[host]):
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
            crawl_urls = _discover(base_url, max_pages)
            phases.append(f"crawl({len(crawl_urls)} param URLs)")
            validated = 0
            for url in crawl_urls:
                try:
                    assess.validate(url, user)
                    validated += 1
                except Exception:
                    continue
            phases.append(f"validate({validated})")
    finally:
        pass

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
