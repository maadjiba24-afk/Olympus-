"""Aegis Assessment — Olympus's native authorized security-assessment suite.

A full inventory and security review of Strix (an open-source autonomous
offensive-security agent — see `docs/STRIX_TRACKING.md`) found a capable feature
surface (recon, source-aware SAST, dependency-CVE scanning, HTTP capture,
PoC-backed CVSS/SARIF findings, multi-agent orchestration, a USD budget stop)
sitting on a security model Olympus is built to beat: scope enforced only by a
*prompt* over a fully-open sandbox, a refusal-suppression system prompt, no
structural prompt-injection defense on ingested target content, opt-in
isolation, and a removed audit trail.

This module absorbs the *capabilities* natively, in Olympus's own idioms and
safety spine, and turns each of those weaknesses into a structural strength
(ADR 0011):

  * **Scope is enforced in CODE, not a prompt.** Every target-touching call
    goes through `require_scope()`, which fails closed against a signed,
    human-approved `authorize_assessment` grant (`authorizations.json`). No
    grant → nothing runs. Strix's "SYSTEM-VERIFIED SCOPE" is a text block a
    single bad render or injection collapses; here an out-of-scope host cannot
    be reached because the function refuses before any I/O.
  * **Authorization is a signed fact, not a suppressed refusal.** Strix's prompt
    orders the model to "never ask permission." Olympus does the inverse: an
    assessment can only run against a target the operator authorized through the
    approval spine (`builtin_actions.authorize_assessment`), recorded in the
    tamper-evident decision ledger — the audit trail Strix removed.
  * **Injection isolated structurally.** Every probe fetch goes through the
    egress-gated, DNS-rebinding-PINNED `tools._http_get` / `tools._http_probe`;
    attacker-controlled response bytes reach a model only through the INGESTION
    classification (`security.should_wrap` → `wrap_untrusted`, fail-closed) with
    action tools stripped from the run. Strix feeds target output straight into
    an actuation-live context.
  * **Findings are computed and evidenced, not asserted.** Severity is a
    CVSS 3.1 base score computed from a vector (`olympus/sarif.py`), findings
    carry evidence + remediation + a CWE, are deduped by fingerprint, and export
    as schema-valid SARIF 2.1.0 for CI — matching Strix's output discipline
    while adding the ledgered provenance it lacks.
  * **Defensive posture preserved.** This is authorized assessment of the
    operator's OWN assets to help them defend: recon, security-header/config
    audit, source SAST, secret and dependency scanning. It produces evidence,
    not weaponized exploits — consistent with Aegis's shield charter.

Everything is pure-stdlib and **degrades gracefully**: a blocked/failed probe
returns a clear error, never a raise that crashes a run.
"""

from __future__ import annotations

import contextvars
import fnmatch
import ipaddress
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import sarif, security

# ---------------------------------------------------------------------------
# Bounds — an assessment can never run away with the token budget or wall clock.
# ---------------------------------------------------------------------------
_MAX_SCAN_FILES = 2000          # source files a whitebox scan will read
_MAX_FILE_BYTES = 1_000_000     # per-file read cap for SAST/secret scans
_MAX_FINDINGS = 1000            # stored findings per run
_MAX_ACTIVE_PROBES = 40         # hard cap on active-validation requests (never a spray)
_MAX_EVIDENCE = 400            # chars of matched-line evidence kept per finding
_DEFAULT_EXPIRY = 24 * 3600     # a scope grant lasts a day unless overridden
_MAX_EXPIRY = 30 * 24 * 3600    # ...and never longer than 30 days
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "vendor", "dist", "build", ".mypy_cache", ".tox", "site-packages"}
_SOURCE_SUFFIXES = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb",
                    ".php", ".c", ".cc", ".cpp", ".cs", ".rs", ".sh", ".yaml",
                    ".yml", ".tf", ".html", ".sql"}


# ===========================================================================
# Scope authorization — the code-enforced boundary (the headline moat)
# ===========================================================================

class AssessScopeError(PermissionError):
    """An assessment step touched a target outside any active, signed
    authorization. Always fail-closed: absence of a grant is out-of-scope."""


def _user(user: str | None = None) -> str:
    if user:
        return user
    try:
        from . import memory
        return memory.current_user()
    except Exception:
        return "shared"


def _store_dir(user: str) -> Path:
    from . import config, memory
    d = config.MEMORY_DIR / "assess" / memory.safe_id(user)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _auth_path(user: str) -> Path:
    return _store_dir(user) / "authorizations.json"


def _load_auths(user: str) -> list[dict]:
    try:
        data = json.loads(_auth_path(user).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _now() -> float:
    return time.time()


def _normalize_targets(targets: list[str] | str) -> list[str]:
    if isinstance(targets, str):
        targets = [targets]
    out: list[str] = []
    for t in targets or []:
        s = str(t).strip()
        if not s:
            continue
        # A URL collapses to its host; a bare host/CIDR/"local" is kept as-is.
        if s.startswith(("http://", "https://")):
            host = urlparse(s).hostname or ""
            if host:
                out.append(host.lower())
        else:
            out.append(s.lower())
    return sorted(set(out))


def grant(targets: list[str] | str, *, expires_in: float = _DEFAULT_EXPIRY,
          note: str = "", approved_by: str = "operator",
          user: str | None = None) -> dict:
    """Record a signed assessment authorization. Called ONLY by the approval
    spine (`authorize_assessment` action) or the operator CLI — never by a tool
    an agent can call, so an agent can never authorize itself (the inversion of
    Strix's self-authorization). Returns the stored grant."""
    user = _user(user)
    norm = _normalize_targets(targets)
    if not norm:
        raise ValueError("an authorization must name at least one target")
    expires_in = max(60.0, min(_MAX_EXPIRY, float(expires_in or _DEFAULT_EXPIRY)))
    created = _now()
    rec = {
        "id": f"auth-{int(created)}-{os.urandom(3).hex()}",
        "targets": norm,
        "created": created,
        "expires": created + expires_in,
        "note": str(note or "")[:500],
        "approved_by": str(approved_by or "operator")[:120],
    }
    auths = [a for a in _load_auths(user) if a.get("expires", 0) > created]
    auths.append(rec)
    _atomic_write(_auth_path(user), json.dumps(auths, indent=2))
    return rec


def revoke(auth_id: str, user: str | None = None) -> bool:
    user = _user(user)
    auths = _load_auths(user)
    kept = [a for a in auths if a.get("id") != auth_id]
    if len(kept) == len(auths):
        return False
    _atomic_write(_auth_path(user), json.dumps(kept, indent=2))
    return True


def active_authorizations(user: str | None = None) -> list[dict]:
    """Non-expired grants for `user`, newest first."""
    now = _now()
    auths = [a for a in _load_auths(_user(user)) if a.get("expires", 0) > now]
    return sorted(auths, key=lambda a: a.get("created", 0), reverse=True)


def _host_matches(host: str, pattern: str) -> bool:
    """Whether an authorized `pattern` (exact host, `*.domain` wildcard, bare
    domain incl. subdomains, or IP/CIDR) covers `host`."""
    host = (host or "").strip().lower().strip("[]")
    pattern = (pattern or "").strip().lower()
    if not host or not pattern:
        return False
    # IP / CIDR pattern
    try:
        net = ipaddress.ip_network(pattern, strict=False)
    except ValueError:
        net = None
    if net is not None:
        try:
            return ipaddress.ip_address(host) in net
        except ValueError:
            return False
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    if pattern == host:
        return True
    # A bare domain authorizes the domain and its subdomains.
    return host.endswith("." + pattern)


def in_scope(target: str, user: str | None = None) -> bool:
    """Whether `target` (a URL, host, IP, or the marker 'local'/a workspace path)
    is covered by an active authorization. FAIL-CLOSED: no grant → False. This is
    the code check that replaces Strix's prompt-level scope."""
    auths = active_authorizations(user)
    if not auths:
        return False
    t = str(target or "").strip().lower()
    if not t:
        return False
    is_local = t in ("local", "workspace", ".") or not t.startswith(
        ("http://", "https://")) and "/" in t or t.startswith("/")
    if t.startswith(("http://", "https://")):
        host = (urlparse(t).hostname or "").lower()
    elif "/" in t or t in ("local", "workspace", "."):
        host = ""            # a local path / workspace marker
        is_local = True
    else:
        host = t             # a bare host or IP
    for a in auths:
        for pat in a.get("targets", []):
            if is_local and pat in ("local", "workspace", "."):
                return True
            if host and _host_matches(host, pat):
                return True
    return False


def require_scope(target: str, user: str | None = None) -> None:
    """Raise AssessScopeError unless `target` is in an active authorization.
    Every network/whitebox assessment entrypoint calls this FIRST, before any
    I/O — so an out-of-scope target is refused in code, not by prompt."""
    if not in_scope(target, user):
        raise AssessScopeError(
            f"'{target}' is not within any active assessment authorization. "
            "Authorize it first: `olympus assess authorize <target>` (operator) "
            "or the authorize_assessment action. Assessment scope is enforced in "
            "code, never by prompt.")


def scope_summary(user: str | None = None) -> str:
    auths = active_authorizations(user)
    if not auths:
        return ("No active assessment authorization. Nothing can be assessed "
                "until the operator authorizes a target "
                "(`olympus assess authorize <target>`).")
    lines = [f"{len(auths)} active assessment authorization(s):"]
    now = _now()
    for a in auths:
        mins = int((a.get("expires", now) - now) / 60)
        lines.append(f"- {a['id']}: {', '.join(a.get('targets', []))} "
                     f"(expires in {mins} min; approved_by "
                     f"{a.get('approved_by', '?')})"
                     + (f" — {a['note']}" if a.get("note") else ""))
    return "\n".join(lines)


# ===========================================================================
# Findings — computed severity, evidence, dedup, SARIF export, ledger record
# ===========================================================================

@dataclass
class Finding:
    title: str
    severity: str = "medium"          # critical/high/medium/low/info
    cwe: str = ""                     # e.g. "CWE-89"
    cvss_vector: str = ""             # CVSS:3.1/... — severity is computed from it
    location: str = ""               # file:line or URL
    evidence: str = ""               # the matched line / observed value (redacted)
    remediation: str = ""
    confidence: str = "medium"        # high/medium/low
    source: str = "assess"           # which scanner produced it
    cvss_score: float | None = None   # filled from the vector at record time
    id: str = ""

    def fingerprint(self) -> str:
        import hashlib
        key = f"{self.cwe}|{self.location}|{self.title}".lower()
        return hashlib.sha256(key.encode()).hexdigest()[:16]


def _findings_path(user: str) -> Path:
    return _store_dir(user) / "findings.json"


def _load_findings(user: str) -> list[dict]:
    try:
        data = json.loads(_findings_path(user).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def record_finding(finding: Finding | dict, user: str | None = None) -> dict:
    """Store a finding, computing its CVSS score and deduping by fingerprint.
    A duplicate (same CWE + location + title) updates the existing record rather
    than piling up. Returns the stored record (with `duplicate` flag)."""
    user = _user(user)
    f = finding if isinstance(finding, Finding) else Finding(**{
        k: v for k, v in dict(finding).items() if k in Finding.__annotations__})
    score, sev = sarif.score_or_none(f.cvss_vector)
    if score is not None:
        f.cvss_score = score
        f.severity = (sev or f.severity).lower()
    else:
        f.cvss_score = sarif.score_for_label(f.severity)
    f.evidence = (f.evidence or "")[:_MAX_EVIDENCE]
    f.id = f.fingerprint()

    records = _load_findings(user)
    by_fp = {r.get("id"): i for i, r in enumerate(records)}
    rec = asdict(f)
    if f.id in by_fp:
        records[by_fp[f.id]] = rec
        dup = True
    else:
        if len(records) >= _MAX_FINDINGS:
            return {"error": f"finding cap reached ({_MAX_FINDINGS})"}
        records.append(rec)
        dup = False
    _atomic_write(_findings_path(user), json.dumps(records, indent=2))
    # Best-effort ledger note (the audit trail Strix removed): the authoritative
    # record is the signed authorize_assessment action on the decision log; this
    # just annotates the active trace when one is present.
    try:
        from . import trace
        tr = trace.current()
        if tr is not None:
            tr.event("assess.finding", id=f.id, cwe=f.cwe,
                     severity=f.severity, location=f.location)
    except Exception:
        pass
    # Self-evolution: a finding from one of Olympus's OWN deterministic
    # scanners/validators accrues into durable assessment knowledge (see
    # _learn_from_finding). Agent-authored findings (source="agent") are excluded
    # so nothing an injected page steered into a finding can reach Aegis's prompt.
    if not dup:
        _learn_from_finding(rec, user)
    out = dict(rec)
    out["duplicate"] = dup
    return out


def list_findings(user: str | None = None) -> list[dict]:
    return sorted(_load_findings(_user(user)),
                  key=lambda r: (-(r.get("cvss_score") or 0.0),
                                 r.get("location", "")))


def clear_findings(user: str | None = None) -> int:
    user = _user(user)
    n = len(_load_findings(user))
    _atomic_write(_findings_path(user), "[]")
    return n


_MAX_SARIF_BYTES = 8 * 1024 * 1024     # cap on an ingested SARIF document
_MAX_IMPORT_FINDINGS = 5000            # cap on results absorbed in one import


def import_sarif(source: str, user: str | None = None) -> dict[str, Any]:
    """Absorb a THIRD-PARTY tool's SARIF 2.1.0 output into the findings store, so
    an operator's own scanners (semgrep/trivy/codeql/gitleaks/grype/…) flow into
    the same CVSS/dedup/export pipeline. Olympus NEVER runs the tool and opens no
    egress — the operator runs their scanner, this ingests the standard output.

    `source` is a local file path OR the raw SARIF text. The document is UNTRUSTED
    external data: size-capped, result-count-capped, and every stored text field
    is secret-redacted (`security.sanitize_for_prompt`) before it lands. Findings
    dedup by fingerprint exactly like native ones. Never raises: a bad document
    returns an error dict."""
    from . import security
    text = source
    try:
        p = Path(source)
        if len(source) < 4096 and p.is_file():
            if p.stat().st_size > _MAX_SARIF_BYTES:
                return {"error": f"SARIF file exceeds the "
                        f"{_MAX_SARIF_BYTES // (1024*1024)}MB cap", "imported": 0}
            text = p.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        text = source
    if len(text) > _MAX_SARIF_BYTES:
        return {"error": "SARIF input exceeds the size cap", "imported": 0}
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, ValueError) as err:
        return {"error": f"not valid JSON/SARIF: {str(err)[:120]}", "imported": 0}

    parsed = sarif.from_sarif(doc)
    capped = len(parsed) > _MAX_IMPORT_FINDINGS
    stored = []
    for fd in parsed[:_MAX_IMPORT_FINDINGS]:
        # Redact every text field a finding carries — the SARIF came from outside
        # and its strings (evidence, titles) may embed a secret or an injection.
        for key in ("title", "evidence", "remediation", "location", "source"):
            if fd.get(key):
                fd[key] = security.sanitize_for_prompt(str(fd[key]))
        stored.append(record_finding(fd, user))
    out: dict[str, Any] = {"imported": len(stored), "findings": stored,
                           "runs": len(doc.get("runs") or []) if isinstance(doc, dict) else 0}
    if capped:
        out["note"] = (f"capped at {_MAX_IMPORT_FINDINGS} findings; "
                       f"{len(parsed) - _MAX_IMPORT_FINDINGS} more were dropped")
    return out


def export_findings(fmt: str = "markdown", user: str | None = None) -> str:
    """Export stored findings as `markdown`, `json`, or `sarif` (SARIF 2.1.0,
    GitHub code-scanning compatible)."""
    findings = list_findings(user)
    fmt = (fmt or "markdown").strip().lower()
    if fmt == "sarif":
        return sarif.to_sarif_json(findings, info_uri=os.environ.get(
            "OLYMPUS_REPO_URL", "https://github.com/maadjiba24-afk/Olympus-"))
    if fmt == "json":
        return json.dumps(findings, indent=2, sort_keys=True)
    return _markdown_report(findings)


def _markdown_report(findings: list[dict]) -> str:
    if not findings:
        return "# Security Assessment\n\nNo findings recorded."
    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f.get("severity", "?")] = by_sev.get(f.get("severity", "?"), 0) + 1
    order = ["critical", "high", "medium", "low", "info"]
    summary = ", ".join(f"{by_sev[s]} {s}" for s in order if s in by_sev)
    lines = [f"# Security Assessment", "",
             f"**{len(findings)} finding(s):** {summary}", ""]
    for f in findings:
        score = f.get("cvss_score")
        head = f"## {f.get('title', 'finding')}  "
        lines.append(head)
        meta = [f"severity **{f.get('severity', '?')}**"]
        if score is not None:
            meta.append(f"CVSS {score}")
        if f.get("cwe"):
            meta.append(f.get("cwe"))
        if f.get("confidence"):
            meta.append(f"confidence {f.get('confidence')}")
        lines.append(" · ".join(meta))
        if f.get("location"):
            lines.append(f"- **Location:** `{f.get('location')}`")
        if f.get("cvss_vector"):
            lines.append(f"- **Vector:** `{f.get('cvss_vector')}`")
        if f.get("evidence"):
            lines.append(f"- **Evidence:** `{f.get('evidence')}`")
        if f.get("remediation"):
            lines.append(f"- **Remediation:** {f.get('remediation')}")
        lines.append("")
    return "\n".join(lines)


# ===========================================================================
# recon — gated fingerprint of an authorized target
# ===========================================================================

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_GENERATOR_RE = re.compile(
    r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE)


def recon(target: str, user: str | None = None) -> dict[str, Any]:
    """Fingerprint an authorized target: reachability, status, server/tech
    headers, page title, and a security-header snapshot. Scope-enforced in code;
    fetch is SSRF/egress-gated and IP-pinned. Returns structured data (the
    INGESTION classification wraps it untrusted on the way to a model)."""
    require_scope(target, user)
    url = target if target.startswith(("http://", "https://")) else f"https://{target}"
    from . import tools
    try:
        probe = tools._http_probe(url)
    except ValueError as err:
        return {"target": target, "error": f"blocked: {err}"}
    if probe.get("error") and not probe.get("headers"):
        return {"target": target, "url": url, "error": probe["error"]}
    headers = probe.get("headers", {})
    title_m = _TITLE_RE.search(probe.get("body", "") or "")
    gen_m = _GENERATOR_RE.search(probe.get("body", "") or "")
    tech = [v for k, v in (("server", headers.get("server")),
                           ("x-powered-by", headers.get("x-powered-by")),
                           ("generator", gen_m.group(1) if gen_m else None))
            if v]
    return {
        "target": target,
        "url": probe.get("url", url),
        "status": probe.get("status"),
        "title": (title_m.group(1).strip()[:200] if title_m else ""),
        "server": headers.get("server", ""),
        "technologies": tech,
        "security_headers": _security_header_snapshot(headers),
    }


# ===========================================================================
# http_audit — security-header / config weaknesses (deterministic, evidence-y)
# ===========================================================================

# Each rule: header, (severity, cwe, cvss_vector, title, remediation, checker).
# `checker(value)` returns None if OK, else a short evidence note. A missing
# header is a finding for the security headers whose absence is the weakness.
def _sh_missing_note(present: bool, value: str) -> str | None:
    return None if present else "header not set"


_SECURITY_HEADERS = {
    "strict-transport-security": (
        "medium", "CWE-319", "CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:L/A:N",
        "Missing HTTP Strict-Transport-Security (HSTS)",
        "Send `Strict-Transport-Security: max-age=63072000; includeSubDomains` "
        "so browsers refuse plaintext downgrade."),
    "content-security-policy": (
        "medium", "CWE-1021", "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:L/I:L/A:N",
        "Missing Content-Security-Policy",
        "Define a restrictive CSP to blunt XSS and clickjacking; start in "
        "report-only, then enforce."),
    "x-content-type-options": (
        "low", "CWE-16", "CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:N/A:N",
        "Missing X-Content-Type-Options: nosniff",
        "Send `X-Content-Type-Options: nosniff` to stop MIME sniffing."),
    "x-frame-options": (
        "low", "CWE-1021", "CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:U/C:N/I:L/A:N",
        "Missing X-Frame-Options / frame-ancestors",
        "Send `X-Frame-Options: DENY` (or a CSP `frame-ancestors`) to prevent "
        "clickjacking."),
    "referrer-policy": (
        "low", "CWE-200", "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N",
        "Missing Referrer-Policy",
        "Send `Referrer-Policy: strict-origin-when-cross-origin` to avoid "
        "leaking URLs to third parties."),
}


def _security_header_snapshot(headers: dict) -> dict[str, bool]:
    return {h: (h in headers) for h in _SECURITY_HEADERS}


def http_audit(target: str, record: bool = True,
               user: str | None = None) -> dict[str, Any]:
    """Audit an authorized target's HTTP security headers and cookie flags,
    emitting findings. Deterministic and non-intrusive — a single gated GET, no
    payloads. Scope-enforced."""
    require_scope(target, user)
    url = target if target.startswith(("http://", "https://")) else f"https://{target}"
    from . import tools
    try:
        probe = tools._http_probe(url)
    except ValueError as err:
        return {"target": target, "error": f"blocked: {err}"}
    headers = probe.get("headers", {})
    if not headers and probe.get("error"):
        return {"target": target, "url": url, "error": probe["error"]}

    findings: list[Finding] = []
    for header, (sev, cwe, vector, title, fix) in _SECURITY_HEADERS.items():
        if header not in headers:
            findings.append(Finding(
                title=title, severity=sev, cwe=cwe, cvss_vector=vector,
                location=url, evidence=f"response is missing `{header}`",
                remediation=fix, confidence="high", source="http_audit"))

    # Cookie flags: a Set-Cookie without Secure/HttpOnly is a real weakness.
    set_cookie = headers.get("set-cookie", "")
    if set_cookie:
        low = set_cookie.lower()
        if "secure" not in low:
            findings.append(Finding(
                title="Session cookie without Secure flag", severity="medium",
                cwe="CWE-614",
                cvss_vector="CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N",
                location=url, evidence="Set-Cookie lacks `Secure`",
                remediation="Set the `Secure` attribute so cookies are never "
                            "sent over plaintext HTTP.",
                confidence="high", source="http_audit"))
        if "httponly" not in low:
            findings.append(Finding(
                title="Session cookie without HttpOnly flag", severity="medium",
                cwe="CWE-1004",
                cvss_vector="CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:N/A:N",
                location=url, evidence="Set-Cookie lacks `HttpOnly`",
                remediation="Set `HttpOnly` so client JS cannot read the "
                            "session cookie (blunts XSS session theft).",
                confidence="high", source="http_audit"))

    # An over-broad CORS reflection is a classic misconfig.
    acao = headers.get("access-control-allow-origin", "")
    if acao == "*" and headers.get("access-control-allow-credentials", "").lower() == "true":
        findings.append(Finding(
            title="CORS allows any origin with credentials", severity="high",
            cwe="CWE-942",
            cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
            location=url,
            evidence="Access-Control-Allow-Origin: * with Allow-Credentials: true",
            remediation="Never combine a wildcard ACAO with credentials; echo a "
                        "vetted allowlisted origin instead.",
            confidence="high", source="http_audit"))

    stored = [record_finding(f, user) for f in findings] if record else \
        [asdict(f) for f in findings]
    return {"target": target, "url": url, "status": probe.get("status"),
            "findings": stored, "count": len(stored)}


# ===========================================================================
# SAST — source-aware weakness patterns over confined local code
# ===========================================================================

@dataclass(frozen=True)
class _Rule:
    id: str
    cwe: str
    severity: str
    vector: str
    pattern: re.Pattern
    title: str
    remediation: str
    suffixes: tuple[str, ...] = ()


def _rule(id, cwe, severity, vector, pat, title, fix, suffixes=()):
    return _Rule(id, cwe, severity, vector, re.compile(pat), title, fix, suffixes)


# A pragmatic, high-signal rule set mapped to CWE + a representative CVSS vector.
# Deliberately conservative (medium confidence) — it flags *sinks* to review,
# not confirmed exploits.
_SAST_RULES: tuple[_Rule, ...] = (
    _rule("py-eval-exec", "CWE-95", "high",
          "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
          r"\b(?:eval|exec)\s*\(", "Dynamic code execution (eval/exec)",
          "Avoid eval/exec on any value that can be influenced by input; use a "
          "safe parser or explicit dispatch.", (".py",)),
    _rule("py-os-system", "CWE-78", "high",
          "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
          r"\bos\.system\s*\(|subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True",
          "OS command execution / shell=True",
          "Pass argument lists (never a shell string); validate/allowlist any "
          "external input reaching the command.", (".py",)),
    _rule("py-pickle", "CWE-502", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
          r"\bpickle\.loads?\s*\(|yaml\.load\s*\((?![^)]*Loader\s*=\s*yaml\.SafeLoader)",
          "Unsafe deserialization (pickle / yaml.load)",
          "Never unpickle untrusted data; use `yaml.safe_load`. Prefer JSON for "
          "untrusted input.", (".py",)),
    _rule("py-sql-format", "CWE-89", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N",
          r"(?:execute|executemany)\s*\(\s*(?:f[\"']|[\"'][^\"']*[\"']\s*(?:%|\+)|[\"'][^\"']*\{)",
          "Possible SQL injection (string-built query)",
          "Use parameterized queries / bound parameters; never format values "
          "into SQL text.", (".py",)),
    _rule("py-verify-false", "CWE-295", "medium",
          "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N",
          r"verify\s*=\s*False", "TLS certificate verification disabled",
          "Never set verify=False in production; fix the trust store instead.",
          (".py",)),
    _rule("py-weak-hash", "CWE-327", "medium",
          "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:N",
          r"hashlib\.(?:md5|sha1)\s*\(",
          "Weak hash (MD5/SHA-1)",
          "Use SHA-256+; for passwords use a KDF (argon2/bcrypt/scrypt).",
          (".py",)),
    _rule("py-flask-debug", "CWE-489", "medium",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N",
          r"debug\s*=\s*True", "Debug mode enabled",
          "Disable debug in any deployed environment (it exposes a console / "
          "stack traces).", (".py",)),
    _rule("py-ssti", "CWE-1336", "high",
          "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
          r"render_template_string\s*\(", "Possible server-side template injection",
          "Never build a template from input; render fixed templates with "
          "escaped variables.", (".py",)),
    _rule("js-innerhtml", "CWE-79", "medium",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N",
          r"\.innerHTML\s*=|dangerouslySetInnerHTML",
          "Possible DOM XSS (innerHTML sink)",
          "Set textContent, or sanitize with a vetted library before "
          "assigning HTML.", (".js", ".ts", ".jsx", ".tsx")),
    _rule("js-child-exec", "CWE-78", "high",
          "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
          r"child_process\.[A-Za-z]*exec\s*\(",
          "OS command execution (child_process.exec)",
          "Use execFile with an argument array; validate any input.",
          (".js", ".ts")),
    # --- extra Python ---
    _rule("py-jwt-noverify", "CWE-347", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N",
          r"jwt\.decode\([^)]*verify\w*\"?\s*[=:]\s*False",
          "JWT signature verification disabled",
          "Never disable JWT signature verification; verify with the expected "
          "algorithm(s) and key.", (".py",)),
    _rule("py-tar-extractall", "CWE-22", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:H/A:N",
          r"\.extractall\((?![^)]*filter)",
          "Unfiltered archive extraction (path traversal)",
          "Pass `filter='data'` (Py3.12+) or validate each member path before "
          "extraction to prevent '../' escapes.", (".py",)),
    _rule("py-mark-safe", "CWE-79", "medium",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N",
          r"\bmark_safe\s*\(",
          "Django mark_safe on possibly-untrusted content (XSS)",
          "Do not mark_safe user-influenced strings; rely on template "
          "auto-escaping or sanitize with a vetted library.", (".py",)),
    # --- Go ---
    _rule("go-insecure-tls", "CWE-295", "high",
          "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:N",
          r"InsecureSkipVerify\s*:\s*true",
          "TLS certificate verification disabled (InsecureSkipVerify)",
          "Never set InsecureSkipVerify:true in production; fix the trust "
          "store.", (".go",)),
    _rule("go-cmd-shell", "CWE-78", "high",
          "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
          r"exec\.Command\(\s*\"(?:sh|bash)\"\s*,\s*\"-c\"",
          "OS command execution via a shell (exec.Command sh -c)",
          "Invoke the binary directly with an argument slice; never build a "
          "shell string from input.", (".go",)),
    _rule("go-weak-hash", "CWE-327", "medium",
          "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:N",
          r"\b(?:md5|sha1)\.New\s*\(",
          "Weak hash (MD5/SHA-1)",
          "Use SHA-256+; for passwords use a KDF (argon2/bcrypt/scrypt).",
          (".go",)),
    _rule("go-sql-sprintf", "CWE-89", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N",
          r"\.Query(?:Row|Context)?\(\s*fmt\.Sprintf\(",
          "SQL query built with fmt.Sprintf (injection)",
          "Use parameterized queries (`$1`/`?` placeholders), never Sprintf "
          "into SQL.", (".go",)),
    # --- PHP ---
    _rule("php-eval", "CWE-95", "high",
          "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
          r"\beval\s*\(",
          "Dynamic code execution (eval)",
          "Avoid eval entirely; use a safe dispatch table.", (".php",)),
    _rule("php-shell", "CWE-78", "high",
          "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
          r"\b(?:system|exec|shell_exec|passthru|popen)\s*\(\s*\$",
          "OS command execution with a variable argument",
          "Never pass input to a shell; use escapeshellarg + a fixed command, "
          "or a safe API.", (".php",)),
    _rule("php-include-input", "CWE-98", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
          r"\b(?:include|require)(?:_once)?\s*\(?\s*\$_(?:GET|POST|REQUEST)",
          "File inclusion from request input (LFI/RFI)",
          "Never include a path from input; map an allowlisted key to a fixed "
          "file.", (".php",)),
    _rule("php-echo-input", "CWE-79", "medium",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N",
          r"\becho\s+\$_(?:GET|POST|REQUEST)",
          "Reflected request input echoed without encoding (XSS)",
          "Encode with htmlspecialchars() before output.", (".php",)),
    _rule("php-unserialize", "CWE-502", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
          r"\bunserialize\s*\(\s*\$",
          "Unsafe deserialization of a variable (unserialize)",
          "Never unserialize untrusted input; use json_decode.", (".php",)),
    # --- Java ---
    _rule("java-runtime-exec", "CWE-78", "high",
          "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
          r"Runtime\.getRuntime\(\)\.exec\s*\(",
          "OS command execution (Runtime.exec)",
          "Use ProcessBuilder with an argument list and validate input; avoid "
          "a shell.", (".java",)),
    _rule("java-sql-concat", "CWE-89", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N",
          r"\.execute(?:Query|Update)?\(\s*\"[^\"]*\"\s*\+",
          "SQL built by string concatenation (injection)",
          "Use a PreparedStatement with bound parameters.", (".java",)),
    _rule("java-deserialize", "CWE-502", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
          r"new\s+ObjectInputStream\s*\(",
          "Unsafe Java deserialization (ObjectInputStream)",
          "Avoid native deserialization of untrusted data; use a safe format "
          "(JSON) or a strict allowlist filter.", (".java",)),
    _rule("java-weak-hash", "CWE-327", "medium",
          "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:N",
          r"MessageDigest\.getInstance\(\s*\"(?:MD5|SHA-1)\"",
          "Weak hash (MD5/SHA-1)",
          "Use SHA-256+; for passwords use a KDF (argon2/bcrypt/PBKDF2).",
          (".java",)),
    # --- Ruby ---
    _rule("ruby-eval", "CWE-95", "high",
          "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
          r"\b(?:eval|instance_eval|class_eval)\s*\(",
          "Dynamic code execution (eval)",
          "Avoid eval on any input; use explicit dispatch.", (".rb",)),
    _rule("ruby-system-interp", "CWE-78", "high",
          "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
          r"(?:system|exec)\s*\(?\s*\"[^\"]*#\{",
          "OS command with string interpolation",
          "Pass an argument array (`system('cmd', arg)`); never interpolate "
          "input into a command string.", (".rb",)),
    _rule("ruby-marshal", "CWE-502", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
          r"Marshal\.load\s*\(",
          "Unsafe deserialization (Marshal.load)",
          "Never Marshal.load untrusted data; use JSON.", (".rb",)),
    _rule("ruby-html-safe", "CWE-79", "medium",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N",
          r"\.html_safe\b",
          "Rails html_safe on possibly-untrusted content (XSS)",
          "Do not html_safe user input; rely on ERB auto-escaping or sanitize.",
          (".rb",)),
    # --- C# / .NET ---
    _rule("cs-sql-concat", "CWE-89", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N",
          r"new\s+SqlCommand\s*\(\s*(?:\$?\"[^\"]*\"\s*\+|\$\"[^\"]*\{)",
          "SQL command built by string concatenation / interpolation (injection)",
          "Use parameterized queries: add SqlParameter values, never concatenate "
          "or interpolate input into the SQL text.", (".cs",)),
    _rule("cs-process-shell", "CWE-78", "high",
          "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
          r"Process\.Start\s*\(\s*\"(?:cmd(?:\.exe)?|/bin/sh|bash|powershell)\b",
          "OS command execution via a shell (Process.Start)",
          "Invoke the target binary directly with an argument list; never launch "
          "a shell with an input-built command string.", (".cs",)),
    _rule("cs-binaryformatter", "CWE-502", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
          r"new\s+BinaryFormatter\s*\(",
          "Unsafe deserialization (BinaryFormatter)",
          "BinaryFormatter is insecure and deprecated; use System.Text.Json or a "
          "contract serializer over trusted data only.", (".cs",)),
    _rule("cs-weak-hash", "CWE-327", "medium",
          "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:N",
          r"(?:MD5|SHA1)\.Create\s*\(",
          "Weak hash (MD5/SHA-1)",
          "Use SHA-256+; for passwords use a KDF (PBKDF2/Argon2/bcrypt).",
          (".cs",)),
    # --- Rust ---
    _rule("rust-command-shell", "CWE-78", "high",
          "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
          r"Command::new\s*\(\s*\"(?:sh|bash)\"\s*\)[^;]*\.arg\s*\(\s*\"-c\"",
          "OS command execution via a shell (Command sh -c)",
          "Invoke the binary directly with .args([...]); never build a shell "
          "string from input.", (".rs",)),
    _rule("rust-sql-format", "CWE-89", "high",
          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N",
          r"(?:query|query_as|execute)\s*\(\s*&?\s*format!\s*\(",
          "SQL query built with format! (injection)",
          "Use bound parameters (sqlx `?`/`$1` binds); never format! input into "
          "SQL text.", (".rs",)),
)


def _iter_source_files(root: Path):
    count = 0
    for path in sorted(root.rglob("*")):
        if count >= _MAX_SCAN_FILES:
            break
        if not path.is_file() or path.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        count += 1
        yield path


def _sast_findings_for_text(text: str, suffix: str,
                            location_prefix: str) -> list[Finding]:
    """Apply the SAST rule set to one source string. Shared by `sast_scan`
    (per file) and `bench` (per fixture) so the benchmark measures EXACTLY the
    detection logic that runs in production — the score can't drift from reality."""
    out: list[Finding] = []
    suffix = (suffix or "").lower()
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 2000:
            continue
        # Skip pure-comment lines — a comment that merely NAMES a sink
        # ("# any other frame eval → benign") is not a sink. This is the cheapest
        # high-value precision win; mid-line comments are left for a future pass.
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "/*", "* ", "*/", "--")):
            continue
        for rule in _SAST_RULES:
            if rule.suffixes and suffix not in rule.suffixes:
                continue
            if rule.pattern.search(line):
                out.append(Finding(
                    title=rule.title, severity=rule.severity, cwe=rule.cwe,
                    cvss_vector=rule.vector,
                    location=f"{location_prefix}:{i}" if location_prefix else f"line {i}",
                    evidence=line.strip()[:_MAX_EVIDENCE],
                    remediation=rule.remediation, confidence="medium",
                    source="sast"))
    return out


def sast_scan(path: str = ".", record: bool = True,
              user: str | None = None) -> dict[str, Any]:
    """Pattern-based SAST over a workspace-confined source tree. Requires an
    active authorization (whitebox review is consent-gated too) and never
    escapes the sandbox workspace root. Flags sinks to review (medium
    confidence), each mapped to a CWE + CVSS vector."""
    require_scope("local", user)
    from . import sandbox
    try:
        root = sandbox._confine(path)
    except Exception as err:
        return {"path": path, "error": f"path refused: {str(err)[:150]}"}
    if not root.exists():
        return {"path": path, "error": "path does not exist in the workspace"}

    findings: list[Finding] = []
    scanned = 0
    scan_root = root.parent if root.is_file() else root
    files = [root] if root.is_file() else list(_iter_source_files(root))
    for fpath in files:
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_BYTES]
        except Exception:
            continue
        scanned += 1
        rel = _rel(fpath, scan_root)
        findings.extend(_sast_findings_for_text(text, fpath.suffix, rel))
    stored = [record_finding(f, user) for f in findings] if record else \
        [asdict(f) for f in findings]
    return {"path": str(path), "files_scanned": scanned,
            "findings": stored, "count": len(stored)}


def _rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return p.name


# ===========================================================================
# secret_scan — credential-shaped strings in confined source (CWE-798)
# ===========================================================================

_SECRET_PATTERNS = (
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Generic API key assignment", re.compile(
        r"(?i)(?:api[_-]?key|secret|token|passwd|password)\s*[:=]\s*"
        r"[\"'][A-Za-z0-9_\-/+]{16,}[\"']")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\."
                       r"[A-Za-z0-9_\-]{10,}\b")),
)


def _redact_secret(line: str) -> str:
    """Show the shape without emitting the secret itself (evidence must never
    become an exfil channel — the lesson from Strix's info-disclosure skill,
    applied to Olympus's own output)."""
    s = line.strip()[:_MAX_EVIDENCE]
    # Reuse the spine's redactors so a matched secret is defanged in the report.
    return security.anonymize(s)


def secret_scan(path: str = ".", record: bool = True,
                user: str | None = None) -> dict[str, Any]:
    """Scan confined source for hardcoded credentials (CWE-798). Evidence is
    redacted before storage so the finding never leaks the secret. Scope-gated."""
    require_scope("local", user)
    from . import sandbox
    try:
        root = sandbox._confine(path)
    except Exception as err:
        return {"path": path, "error": f"path refused: {str(err)[:150]}"}
    if not root.exists():
        return {"path": path, "error": "path does not exist in the workspace"}

    findings: list[Finding] = []
    scan_root = root if root.is_dir() else root.parent
    files = [root] if root.is_file() else list(_iter_source_files(root))
    for fpath in files:
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_BYTES]
        except Exception:
            continue
        rel = _rel(fpath, scan_root)
        for i, line in enumerate(text.splitlines(), 1):
            for label, pat in _SECRET_PATTERNS:
                if pat.search(line):
                    findings.append(Finding(
                        title=f"Hardcoded secret: {label}", severity="high",
                        cwe="CWE-798",
                        cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
                        location=f"{rel}:{i}",
                        evidence=_redact_secret(line),
                        remediation="Remove the secret from source; load it from "
                                    "a secret store / env at runtime and rotate "
                                    "the exposed value.",
                        confidence="medium", source="secret_scan"))
                    break
    stored = [record_finding(f, user) for f in findings] if record else \
        [asdict(f) for f in findings]
    return {"path": str(path), "findings": stored, "count": len(stored)}


# ===========================================================================
# dep_audit — vulnerable dependency detection from manifests (offline-first)
# ===========================================================================

# A small, bundled advisory index: {ecosystem: {package: [(spec, id, cwe,
# severity, note)]}}. Offline and deterministic — a demonstration/seed set, not
# a live feed. `spec` is an exact version or a `<x.y.z` upper bound. An operator
# can extend it via OLYMPUS_ASSESS_ADVISORIES (a JSON file, same shape).
_BUNDLED_ADVISORIES: dict[str, dict[str, list[tuple]]] = {
    "pypi": {
        "pyyaml": [("<5.4", "CVE-2020-14343", "CWE-20", "critical",
                    "Arbitrary code execution via yaml.load on untrusted input")],
        "requests": [("<2.31.0", "CVE-2023-32681", "CWE-200", "medium",
                      "Proxy-Authorization header leak on redirect")],
        "jinja2": [("<2.11.3", "CVE-2020-28493", "CWE-400", "medium",
                    "ReDoS in the urlize filter")],
        "flask": [("<2.2.5", "CVE-2023-30861", "CWE-539", "high",
                   "Session cookie disclosure via caching proxies")],
        "cryptography": [("<41.0.0", "CVE-2023-38325", "CWE-347", "high",
                          "Malformed certificate parsing issues")],
        "django": [("<3.2.20", "CVE-2023-36053", "CWE-1333", "high",
                    "ReDoS in EmailValidator/URLValidator")],
        "urllib3": [("<1.26.18", "CVE-2023-45803", "CWE-200", "medium",
                     "Request body leak on redirect after 303")],
        "werkzeug": [("<2.3.8", "CVE-2023-46136", "CWE-400", "high",
                      "Resource exhaustion parsing multipart form-data")],
        "aiohttp": [("<3.9.2", "CVE-2024-23334", "CWE-22", "high",
                     "Directory traversal via follow_symlinks static routes")],
    },
    "npm": {
        "lodash": [("<4.17.21", "CVE-2021-23337", "CWE-77", "high",
                    "Command injection via template")],
        "minimist": [("<1.2.6", "CVE-2021-44906", "CWE-1321", "critical",
                      "Prototype pollution")],
        "axios": [("<1.6.0", "CVE-2023-45857", "CWE-200", "medium",
                   "SSRF / credential leak via absolute URL")],
        "express": [("<4.19.2", "CVE-2024-29041", "CWE-601", "medium",
                     "Open redirect via malformed URLs in res.location")],
        "jsonwebtoken": [("<9.0.0", "CVE-2022-23529", "CWE-347", "high",
                          "Insecure key handling allows signature bypass")],
        "ws": [("<8.17.1", "CVE-2024-37890", "CWE-400", "high",
                "DoS via many HTTP headers")],
    },
}


def _load_advisories() -> dict:
    adv = {k: dict(v) for k, v in _BUNDLED_ADVISORIES.items()}
    extra = os.environ.get("OLYMPUS_ASSESS_ADVISORIES", "").strip()
    if extra:
        try:
            from . import sandbox
            data = json.loads(sandbox._confine(extra).read_text(encoding="utf-8"))
            for eco, pkgs in (data or {}).items():
                dest = adv.setdefault(eco, {})
                for name, rows in pkgs.items():
                    dest.setdefault(name, []).extend(tuple(r) for r in rows)
        except Exception:
            pass
    return adv


def _ver_tuple(v: str) -> tuple:
    parts = re.split(r"[.\-+]", str(v).strip())
    out = []
    for p in parts:
        m = re.match(r"\d+", p)
        out.append(int(m.group()) if m else 0)
    return tuple(out) or (0,)


def _spec_matches(version: str, spec: str) -> bool:
    spec = spec.strip()
    if spec.startswith("<"):
        return _ver_tuple(version) < _ver_tuple(spec[1:])
    if spec.startswith("<="):
        return _ver_tuple(version) <= _ver_tuple(spec[2:])
    return _ver_tuple(version) == _ver_tuple(spec)


def _parse_requirements(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([0-9][\w.\-]*)", line)
        if m:
            out.append((m.group(1).lower().replace("_", "-"), m.group(2)))
    return out


def _parse_package_json(text: str) -> list[tuple[str, str]]:
    out = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return out
    for section in ("dependencies", "devDependencies"):
        for name, spec in (data.get(section) or {}).items():
            ver = re.sub(r"^[\^~>=<\s]+", "", str(spec))
            if re.match(r"\d", ver):
                out.append((name.lower(), ver))
    return out


# --- live CVE feed (OSV.dev) — opt-in, cached, gated, offline-first -----------
# Bundled advisories are the default (offline + deterministic). When an operator
# opts in (OLYMPUS_ASSESS_OSV), dep_audit ALSO queries OSV.dev for each declared
# package/version, MERGING live results with the bundled index. Every query goes
# through the gated tools._http_post_json (SSRF-pinned, confinement permits the
# trusted api.osv.dev infra); results are cached with a TTL so repeat audits are
# cheap; any failure degrades silently to the bundled index. Off during replay.

_OSV_TTL = 24 * 3600
_OSV_MAX_DEPS = 60              # cap live lookups per audit (bounded egress)
_OSV_ECOSYSTEM = {"pypi": "PyPI", "npm": "npm"}


def _osv_enabled() -> bool:
    if _replaying():
        return False
    return os.environ.get("OLYMPUS_ASSESS_OSV", "").strip().lower() in (
        "1", "true", "yes", "on")


def _osv_cache_path(user: str) -> Path:
    return _store_dir(user) / "osv_cache.json"


def _osv_cache_load(user: str) -> dict:
    try:
        data = json.loads(_osv_cache_path(user).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _osv_parse_vuln(v: dict) -> dict:
    """Pull the fields we report from an OSV vuln object (defensive — OSV's
    shape varies by advisory source).

    OSV text is EXTERNAL and attacker-influenceable, but `assess_deps` is a
    TRUSTED tool (its result reaches the model unwrapped — the static index was
    first-party). So every OSV-derived string is neutralized HERE at the source:
    id/cwe/vector are restricted to their safe charset/shape and the free-text
    summary is injection-defanged (`sanitize_for_memory`) — closing a
    prompt-injection channel into a trusted finding."""
    aliases = v.get("aliases") or []
    ident = next((a for a in aliases if str(a).startswith("CVE-")),
                 v.get("id", "?"))
    ident = re.sub(r"[^A-Za-z0-9:._-]", "", str(ident))[:64] or "?"
    vector = ""
    for s in v.get("severity") or []:
        if str(s.get("type", "")).upper().startswith("CVSS_V3"):
            vector = re.sub(r"[^A-Za-z0-9:./]", "", str(s.get("score", "")))[:128]
            break
    dbs = v.get("database_specific") or {}
    cwes = dbs.get("cwe_ids") or []
    cwe_m = re.search(r"CWE-\d+", str(cwes[0]).upper()) if cwes else None
    cwe = cwe_m.group(0) if cwe_m else ""
    score, sev_from_vec = sarif.score_or_none(vector)
    severity = (sev_from_vec or dbs.get("severity") or "high").lower()
    if severity not in ("critical", "high", "medium", "low", "info"):
        severity = "medium"
    summary = security.sanitize_for_memory(str(v.get("summary", "")))[:200]
    return {"id": ident, "cwe": cwe, "cvss_vector": vector,
            "severity": severity, "summary": summary}


def _osv_lookup(ecosystem: str, pkg: str, version: str,
                user: str | None = None) -> list[dict]:
    """Cached OSV.dev lookup for one package/version. Returns parsed vuln dicts;
    [] on any failure (offline-first). Never raises."""
    user = _user(user)
    osv_eco = _OSV_ECOSYSTEM.get(ecosystem)
    if not osv_eco or not pkg or not version:
        return []
    key = f"{ecosystem}:{pkg}:{version}"
    cache = _osv_cache_load(user)
    ent = cache.get(key)
    if isinstance(ent, dict) and (_now() - ent.get("ts", 0)) < _OSV_TTL:
        return ent.get("vulns", [])
    try:
        from . import tools
        resp = tools._http_post_json(
            "https://api.osv.dev/v1/query",
            {"version": version, "package": {"name": pkg, "ecosystem": osv_eco}})
    except Exception:
        return ent.get("vulns", []) if isinstance(ent, dict) else []
    if not isinstance(resp, dict) or resp.get("_error"):
        return ent.get("vulns", []) if isinstance(ent, dict) else []
    vulns = [_osv_parse_vuln(v) for v in (resp.get("vulns") or [])
             if isinstance(v, dict)]
    cache[key] = {"ts": _now(), "vulns": vulns}
    try:
        if len(cache) > 5000:                         # bound the cache
            cache = dict(sorted(cache.items(),
                                key=lambda kv: kv[1].get("ts", 0),
                                reverse=True)[:5000])
        _atomic_write(_osv_cache_path(user), json.dumps(cache))
    except Exception:
        pass
    return vulns


def dep_audit(path: str = ".", record: bool = True,
              user: str | None = None) -> dict[str, Any]:
    """Audit declared dependencies (requirements.txt / package.json) against the
    bundled advisory index — and, when OLYMPUS_ASSESS_OSV is set, the live
    OSV.dev feed too (cached, gated, offline-first). Scope-gated, workspace-
    confined. Absorbs Strix's dependency-CVE scan."""
    require_scope("local", user)
    from . import sandbox
    try:
        root = sandbox._confine(path)
    except Exception as err:
        return {"path": path, "error": f"path refused: {str(err)[:150]}"}

    manifests: list[tuple[str, str, str]] = []      # (ecosystem, rel, text)
    candidates = []
    if root.is_file():
        candidates = [root]
    else:
        for name in ("requirements.txt", "package.json"):
            p = root / name
            if p.exists():
                candidates.append(p)
    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_BYTES]
        except Exception:
            continue
        eco = "npm" if p.name == "package.json" else "pypi"
        manifests.append((eco, p.name, text))
    if not manifests:
        return {"path": str(path),
                "error": "no requirements.txt or package.json found"}

    advisories = _load_advisories()
    osv_on = _osv_enabled()
    findings: list[Finding] = []
    osv_queried = 0
    for eco, name, text in manifests:
        deps = _parse_package_json(text) if eco == "npm" else _parse_requirements(text)
        for pkg, version in deps:
            seen_ids: set[str] = set()
            for spec, adv_id, cwe, sev, note in advisories.get(eco, {}).get(pkg, []):
                if _spec_matches(version, spec):
                    seen_ids.add(adv_id)
                    findings.append(Finding(
                        title=f"Vulnerable dependency: {pkg} {version} ({adv_id})",
                        severity=sev, cwe=cwe,
                        cvss_vector="", location=f"{name}",
                        evidence=f"{pkg}=={version} matches {spec}: {note}",
                        remediation=f"Upgrade {pkg} past {spec.lstrip('<=')}.",
                        confidence="high", source="dep_audit"))
            # Live OSV feed (opt-in, bounded, deduped against the bundled index).
            if osv_on and osv_queried < _OSV_MAX_DEPS:
                osv_queried += 1
                for v in _osv_lookup(eco, pkg, version, user):
                    if v["id"] in seen_ids:
                        continue
                    seen_ids.add(v["id"])
                    findings.append(Finding(
                        title=f"Vulnerable dependency: {pkg} {version} ({v['id']})",
                        severity=v["severity"], cwe=v["cwe"],
                        cvss_vector=v["cvss_vector"], location=f"{name}",
                        evidence=f"OSV.dev: {v['summary'] or v['id']}",
                        remediation=f"Upgrade {pkg}; see advisory {v['id']}.",
                        confidence="high", source="dep_audit"))
    stored = [record_finding(f, user) for f in findings] if record else \
        [asdict(f) for f in findings]
    return {"path": str(path), "manifests": [m[1] for m in manifests],
            "findings": stored, "count": len(stored), "osv": osv_on}


# ===========================================================================
# run_assessment — orchestrate the phases under a USD budget stop
# ===========================================================================

def _spent_usd() -> float:
    """Best-effort session spend in USD (0.0 when usage accounting is absent),
    so the budget stop degrades to advisory rather than crashing."""
    try:
        from . import memory, usage
        return float(usage.session_totals(memory.current_user()).get("cost", 0.0))
    except Exception:
        return 0.0


def run_assessment(target: str, *, source_path: str | None = None,
                   budget_usd: float | None = None,
                   user: str | None = None) -> dict[str, Any]:
    """Run a bounded assessment against an AUTHORIZED target: recon + HTTP audit
    (network), plus SAST + secret + dependency scans when `source_path` is given
    (whitebox). Scope is enforced in code before any phase. A USD budget, if
    given, halts further phases once session spend exceeds it (Strix's budget
    stop, made structural). Findings are recorded and deduped."""
    require_scope(target, user)
    start_spent = _spent_usd()
    phases: list[dict] = []

    def _over_budget() -> bool:
        return budget_usd is not None and (_spent_usd() - start_spent) >= budget_usd

    # Confine ALL outbound network for the whole run to the signed scope — a
    # hijacked assessment cannot leave the authorized target set even if a new
    # network path is added (blast-radius containment, ADR 0013).
    with confined_egress(user):
        if not target.lower().startswith(("local", "workspace")):
            phases.append({"phase": "recon", **recon(target, user)})
            if not _over_budget():
                phases.append({"phase": "http_audit", **http_audit(target, user=user)})

        if source_path and not _over_budget():
            phases.append({"phase": "sast", **sast_scan(source_path, user=user)})
            if not _over_budget():
                phases.append({"phase": "secret_scan", **secret_scan(source_path, user=user)})
            if not _over_budget():
                phases.append({"phase": "dep_audit", **dep_audit(source_path, user=user)})

    findings = list_findings(user)
    return {
        "target": target,
        "source_path": source_path,
        "phases": [p.get("phase") for p in phases],
        "phase_results": phases,
        "total_findings": len(findings),
        "budget_usd": budget_usd,
        "budget_stopped": _over_budget(),
        "spent_usd": round(_spent_usd() - start_spent, 4),
    }


# ===========================================================================
# Active validation — confirm a finding with a BENIGN, scope-locked probe
# ===========================================================================
# The moat over Strix: Strix "confirms" by throwing arbitrary/weaponized payloads
# from an open-egress box at arbitrary targets — powerful but undeployable and
# unsafe. Olympus confirms with a benign marker sent ONLY to a parameter the
# operator named, ONLY against a code-authorized target, through the SSRF-pinned
# gated fetch, hard-capped so it can never spray. It upgrades a finding from
# "potential (static)" to "confirmed (observed)" — a real proof — while being
# safe to run unattended. That is *stronger than Strix* on the axis that
# matters (deployable confirmation), not weaker.
#
# Deliberate boundaries (do not cross in any future check):
#   * Only parameters PRESENT in the caller's URL are tested — never guessed /
#     fuzzed parameter names, so this is operator-directed, not a spray.
#   * Payloads are BENIGN markers (a random canary + a few special characters) —
#     never a working exploit, shell, or destructive input.
#   * Scope is enforced in code (`require_scope`), egress is pinned/gated, and
#     the total probe count is capped (`_MAX_ACTIVE_PROBES`).
# New check functions are added to `_ACTIVE_CHECKS` over time (the self-evolving
# moat); each MUST honour the three boundaries above.

_CANARY = "olympuscanary"


def _set_param(url: str, name: str, value: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q[name] = value
    return urlunparse(p._replace(query=urlencode(q)))


def _reflection_check(url: str, name: str) -> tuple["Finding | None", str]:
    """Send a BENIGN marker (`olympuscanary<rand><">`) into `name` and check
    whether the special characters come back UNESCAPED — proof that reflected
    input is not output-encoded (an XSS surface). No script, no exploit: just a
    marker that confirms missing encoding. Raises ValueError if the fetch is
    blocked (scope/SSRF)."""
    from . import tools
    tok = os.urandom(4).hex()
    marker = f"{_CANARY}{tok}"
    payload = marker + '<">'                      # benign special chars, never a tag
    probe = tools._http_probe(_set_param(url, name, payload))
    body = probe.get("body", "") or ""
    if marker not in body:
        return None, f"param '{name}': not reflected"
    idx = body.find(marker)
    window = body[idx: idx + len(payload) + 8]
    unescaped = ("<" in window and "&lt;" not in window) or \
                ('"' in window and "&quot;" not in window and "&#34;" not in window)
    if not unescaped:
        return None, f"param '{name}': reflected but encoded (safe)"
    finding = Finding(
        title="Reflected input without output encoding (XSS surface)",
        severity="medium", cwe="CWE-79",
        cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N",
        location=f"{url} [param: {name}]",
        evidence=f"benign marker reflected unescaped: ...{window[:80]}...",
        remediation="Context-encode reflected input (HTML-entity-encode <, >, "
                    "\", '); add a restrictive Content-Security-Policy.",
        confidence="high", source="active_validation")
    return finding, f"param '{name}': reflected UNESCAPED — confirmed XSS surface"


def _open_redirect_check(url: str, name: str) -> tuple["Finding | None", str]:
    """Set `name` to a BENIGN off-site canary host and read the Location header
    WITHOUT following the redirect. If the app echoes the canary host into
    Location, it will redirect a user to attacker-controlled input — an open
    redirect (CWE-601). The `.invalid` TLD is reserved and never resolves, and
    the redirect is never followed, so no request ever reaches the canary: this
    reads where the app WANTS to send us, it never goes there. Raises ValueError
    if blocked."""
    from . import tools
    tok = os.urandom(4).hex()
    canary_host = f"olympus-canary-{tok}.invalid"
    target = f"https://{canary_host}/"
    probe = tools._http_probe(_set_param(url, name, target), follow_redirects=False)
    status = probe.get("status")
    location = (probe.get("headers", {}) or {}).get("location", "") or ""
    if isinstance(status, int) and 300 <= status < 400 and canary_host in location:
        finding = Finding(
            title="Open redirect (unvalidated redirect target)",
            severity="medium", cwe="CWE-601",
            cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:N/A:N",
            location=f"{url} [param: {name}]",
            evidence=f"benign canary redirected to: {location[:120]}",
            remediation="Never redirect to a raw request parameter. Allowlist "
                        "internal paths, or map an opaque key to a known "
                        "destination server-side.",
            confidence="high", source="active_validation")
        return finding, f"param '{name}': open redirect CONFIRMED (Location -> canary)"
    if isinstance(status, int) and 300 <= status < 400:
        return None, f"param '{name}': redirects, but not to our input (safe)"
    return None, f"param '{name}': no redirect"


def _ssti_check(url: str, name: str) -> tuple["Finding | None", str]:
    """Send a BENIGN arithmetic template expression (`{{a*b}}`, random factors)
    into `name`. If the response contains the PRODUCT where our braces were — and
    the braces are gone — the template engine EVALUATED the expression: a
    server-side template injection surface (CWE-1336). It is pure arithmetic:
    no code, no shell, no data access — just proof the engine executed input.
    Targets `{{ }}` engines (Jinja2/Twig/Nunjucks/Handlebars/Angular). Raises
    ValueError if the fetch is blocked (scope/SSRF)."""
    from . import tools
    tok = os.urandom(4).hex()
    a = 900 + (int(tok[:2], 16) % 90)         # distinctive 3-digit factors
    b = 900 + (int(tok[2:4], 16) % 90)
    product = str(a * b)
    marker = f"{_CANARY}{tok}"
    literal = f"{a}*{b}"
    payload = f"{marker}{{{{{literal}}}}}"     # e.g. olympuscanaryAB{{931*907}}
    probe = tools._http_probe(_set_param(url, name, payload))
    body = probe.get("body", "") or ""
    if marker not in body:
        return None, f"param '{name}': not reflected"
    idx = body.find(marker)
    window = body[idx: idx + len(payload) + 16]
    if product in window and "{{" not in window and literal not in window:
        finding = Finding(
            title="Server-side template injection (expression evaluated)",
            severity="high", cwe="CWE-1336",
            cvss_vector="CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
            location=f"{url} [param: {name}]",
            evidence=f"benign expression {{{{{literal}}}}} evaluated to {product}",
            remediation="Never build a template from request input. Render fixed "
                        "templates with auto-escaped variables; sandbox or remove "
                        "the expression engine on the input path.",
            confidence="high", source="active_validation")
        return finding, f"param '{name}': SSTI CONFIRMED ({literal} -> {product})"
    return None, f"param '{name}': not evaluated (reflected literally / not at all)"


def _header_injection_check(url: str, name: str) -> tuple["Finding | None", str]:
    """Put a BENIGN CRLF + a unique marker header into `name`'s value. If the
    server URL-decodes it into the response headers, our `X-Olympus-Canary-<tok>`
    header appears on the response — proof of CRLF response-header injection
    (CWE-113). The injected header is inert (`: 1`); no cache-poisoning or
    smuggling payload is ever sent. Read WITHOUT following redirects so the
    split header on the immediate response is visible. Raises ValueError if
    blocked."""
    from . import tools
    tok = os.urandom(4).hex()
    hdr = f"x-olympus-canary-{tok}"
    payload = f"{_CANARY}{tok}\r\n{hdr}: 1"     # _set_param URL-encodes the CRLF
    probe = tools._http_probe(_set_param(url, name, payload), follow_redirects=False)
    headers = probe.get("headers", {}) or {}
    if hdr in headers:
        finding = Finding(
            title="HTTP response-header injection (CRLF)",
            severity="high", cwe="CWE-113",
            cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:H/A:N",
            location=f"{url} [param: {name}]",
            evidence=f"injected header '{hdr}' reflected into the response",
            remediation="Strip CR/LF from any value written into a response "
                        "header; use a framework API that rejects control "
                        "characters in header values.",
            confidence="high", source="active_validation")
        return finding, f"param '{name}': header injection CONFIRMED ({hdr} echoed)"
    return None, f"param '{name}': no CRLF header injection"


def _cors_check(url: str, name: str) -> tuple["Finding | None", str]:
    """Send an arbitrary off-site `Origin:` and see if the app REFLECTS it into
    `Access-Control-Allow-Origin` — meaning any site can read authenticated
    responses (CWE-942), critical if `Access-Control-Allow-Credentials: true`.
    Benign: one GET with a canary `.invalid` Origin, nothing written. This check
    is URL-level (independent of `name`); `record_finding` dedups the per-param
    repeats to a single finding. Raises ValueError if blocked."""
    from . import tools
    tok = os.urandom(4).hex()
    origin = f"https://olympus-canary-{tok}.evil.invalid"
    probe = tools._http_probe(url, extra_headers={"Origin": origin})
    headers = probe.get("headers", {}) or {}
    acao = (headers.get("access-control-allow-origin", "") or "").strip()
    creds = (headers.get("access-control-allow-credentials", "") or "").strip().lower()
    if acao == origin:                          # arbitrary origin reflected
        with_creds = creds == "true"
        finding = Finding(
            title="CORS misconfiguration (arbitrary Origin reflected"
                  + (" with credentials)" if with_creds else ")"),
            severity="high" if with_creds else "medium",
            cwe="CWE-942",
            cvss_vector=("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:H/I:N/A:N"
                         if with_creds else
                         "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:N/A:N"),
            location=f"{url} [CORS]",
            evidence=f"Origin {origin} reflected in Access-Control-Allow-Origin"
                     + (" with Allow-Credentials: true" if with_creds else ""),
            remediation="Never reflect the request Origin. Allowlist trusted "
                        "origins explicitly and only send Allow-Credentials to "
                        "them.",
            confidence="high", source="active_validation")
        note = f"CORS origin reflection CONFIRMED" + (" + credentials" if with_creds else "")
        return finding, note
    return None, "Origin not reflected (safe)"


# High-precision, DB-specific SQL parse-error signatures. Matching one of these
# proves the value crossed into a query context unescaped — the SQL-injection
# SURFACE — without ever extracting a row. Kept specific (engine + "syntax"/
# quote wording) so a page that merely contains the word "sql" never matches.
_SQL_ERROR_SIGNATURES: tuple[str, ...] = (
    "you have an error in your sql syntax",          # MySQL / MariaDB
    "warning: mysqli", "warning: mysql_", "mysql_fetch",
    "unclosed quotation mark after the character string",  # MSSQL
    "incorrect syntax near",                         # MSSQL
    "quoted string not properly terminated",         # Oracle
    "ora-00933", "ora-01756", "ora-00920",           # Oracle
    "syntax error at or near",                       # PostgreSQL
    "pg::syntaxerror", "psycopg2.errors",            # PostgreSQL drivers
    "sqlite3.operationalerror", "sqlitesyntaxerror", "near \"'\": syntax error",
    "sqlstate[",                                     # PDO / JDBC
    "odbc sql server driver",
)

# Framework verbose-error / interactive-debugger signatures (CWE-209). These
# mean the app leaked its internals — stack frames, source, or a live debugger.
_ERROR_DISCLOSURE_SIGNATURES: tuple[str, ...] = (
    "traceback (most recent call last)",             # Python
    "werkzeug/debugger", "werkzeug.debug", "the console is locked",  # Flask
    "you're seeing this error because you have debug = true",        # Django
    "django.core.exceptions", "exception value:",    # Django
    "at com.", "at org.", "javax.servlet",           # Java stack frames
    "system.web.httpexception", "asp.net is configured to show",     # ASP.NET
    "microsoft ole db provider",
    "fatal error:", "stack trace:",                  # PHP / generic
    "actioncontroller::", "rails.application",        # Rails
)


def _error_probe(url: str, name: str) -> tuple[str, int | None]:
    """Send `name`'s value with ONE trailing single-quote (an inert boundary
    char, like the reflection check's `<">`) and return (lower-cased body,
    status). A quote is the minimal, non-destructive probe that surfaces a
    string-context parse error: it can only cause a SELECT-side syntax error
    (read path), never completes a write, and extracts nothing. Raises
    ValueError if the fetch is blocked (scope/SSRF)."""
    from . import tools
    tok = os.urandom(4).hex()
    payload = f"{_CANARY}{tok}'"                       # canary marker + one quote
    probe = tools._http_probe(_set_param(url, name, payload))
    return (probe.get("body", "") or "").lower(), probe.get("status")


def _sql_error_check(url: str, name: str) -> tuple["Finding | None", str]:
    """Confirm a SQL-injection SURFACE by DETECTION only: a single benign quote,
    then match a DB-specific parse-error signature. No UNION, no boolean/time
    extraction, no data pulled — this proves the parameter reaches a query
    unescaped, nothing more. Raises ValueError if blocked (scope/SSRF)."""
    body, _ = _error_probe(url, name)
    hit = next((s for s in _SQL_ERROR_SIGNATURES if s in body), None)
    if not hit:
        return None, f"param '{name}': no SQL error signature (safe)"
    finding = Finding(
        title="SQL injection surface (database parse error on a benign quote)",
        severity="critical", cwe="CWE-89",
        cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
        location=f"{url} [param: {name}]",
        evidence=f"DB parse error surfaced by a single quote: '…{hit}…'",
        remediation="Use parameterized queries / prepared statements; never "
                    "build SQL by string concatenation. Disable verbose DB "
                    "errors in production.",
        confidence="high", source="active_validation")
    return finding, f"param '{name}': SQL injection surface CONFIRMED (error-based)"


def _error_disclosure_check(url: str, name: str) -> tuple["Finding | None", str]:
    """Detect verbose-error / stack-trace disclosure (CWE-209): the same inert
    boundary quote, then match a framework stack-trace or interactive-debugger
    signature in the response. Purely observational — it reads what the app
    volunteered about its internals. Raises ValueError if blocked."""
    body, status = _error_probe(url, name)
    hit = next((s for s in _ERROR_DISCLOSURE_SIGNATURES if s in body), None)
    if not hit:
        return None, f"param '{name}': no verbose-error disclosure (safe)"
    finding = Finding(
        title="Verbose error / stack-trace disclosure",
        severity="medium", cwe="CWE-209",
        cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N",
        location=f"{url} [param: {name}]",
        evidence=f"internal error detail leaked (status {status}): '…{hit}…'",
        remediation="Return generic error pages to clients; log details "
                    "server-side only. Disable debug mode and framework "
                    "interactive debuggers in production.",
        confidence="high", source="active_validation")
    return finding, f"param '{name}': verbose-error disclosure CONFIRMED"


# The registry — extended over successive loop iterations (self-evolving moat).
# Every entry is benign, scope-locked, parameter-directed, and capped.
_ACTIVE_CHECKS: tuple[tuple[str, Any], ...] = (
    ("reflection", _reflection_check),
    ("open_redirect", _open_redirect_check),
    ("ssti", _ssti_check),
    ("header_injection", _header_injection_check),
    ("cors", _cors_check),
    ("sql_error", _sql_error_check),
    ("error_disclosure", _error_disclosure_check),
)


def active_check_names() -> list[str]:
    return [name for name, _ in _ACTIVE_CHECKS]


def validate(url: str, user: str | None = None) -> dict[str, Any]:
    """Actively CONFIRM weaknesses on an AUTHORIZED target using benign, non-
    destructive probes against the query parameters PRESENT in `url`. Scope is
    enforced in code; every probe is SSRF-pinned/gated and the total is capped
    at `_MAX_ACTIVE_PROBES`. This is the deployable, safe superset of Strix's
    exploitation phase — it proves findings without arbitrary payloads, spraying,
    or open egress. Records confirmed findings; returns a per-parameter log."""
    require_scope(url, user)
    if not str(url).startswith(("http://", "https://")):
        return {"url": url, "error": "validate needs a full http(s) URL with the "
                                     "parameter(s) to test, e.g. "
                                     "https://app.example/search?q=test"}
    p = urlparse(url)
    params = list(dict.fromkeys(k for k, _ in parse_qsl(p.query,
                                                        keep_blank_values=True)))
    if not params:
        return {"url": url, "error": "no query parameters to validate — provide a "
                                     "URL with the parameter(s) to test, e.g. "
                                     "https://app.example/search?q=test"}

    findings: list[Finding] = []
    log: list[dict[str, str]] = []
    probes = 0
    for name in params:
        for check_name, fn in _ACTIVE_CHECKS:
            if probes >= _MAX_ACTIVE_PROBES:
                log.append({"note": f"probe cap ({_MAX_ACTIVE_PROBES}) reached — "
                                    "remaining parameters skipped"})
                break
            probes += 1
            try:
                finding, note = fn(url, name)
            except ValueError as err:                 # scope / SSRF / blocked
                log.append({"check": check_name, "param": name,
                            "note": f"blocked: {err}"})
                continue
            except Exception as err:                  # network — never crash a run
                log.append({"check": check_name, "param": name,
                            "note": f"probe failed: {str(err)[:120]}"})
                continue
            log.append({"check": check_name, "param": name, "note": note})
            if finding is not None:
                findings.append(finding)
        else:
            continue
        break

    stored = [record_finding(f, user) for f in findings]
    return {"url": url, "params_tested": len(params), "probes": probes,
            "log": log, "findings": stored, "count": len(stored)}


# ===========================================================================
# Self-benchmark — measured, regression-gated evolution (the moat's engine)
# ===========================================================================
# Olympus improves the way it improves everything else: measured, with a
# regression gate (Prometheus upgrades a prompt only if a before/after benchmark
# shows no regression, else rolls back). The assessment engine gets the same
# spine — a labeled corpus scored on the EXACT production detection logic
# (`_sast_findings_for_text`, the dep matcher), so precision/recall are real and
# every rule/check added over time is measured. `test_assess.py` asserts a
# quality floor, so a change that misses a known bug or fires on clean code fails
# CI. This is what makes the self-evolving loop safe: capability can grow, but
# detection quality cannot silently regress.

_BENCH_CORPUS: tuple[dict, ...] = (
    {"name": "py_sinks", "kind": "sast", "suffix": ".py",
     "expected": {"CWE-78", "CWE-95", "CWE-502", "CWE-327", "CWE-295", "CWE-89"},
     "text": (
         "import os, pickle, hashlib, requests\n"
         "os.system('ping ' + host)\n"
         "eval(user_code)\n"
         "data = pickle.loads(blob)\n"
         "h = hashlib.md5(pw).hexdigest()\n"
         "requests.get(url, verify=False)\n"
         "cur.execute(f'SELECT * FROM t WHERE id={uid}')\n")},
    {"name": "js_sinks", "kind": "sast", "suffix": ".js",
     "expected": {"CWE-79", "CWE-78"},
     "text": ("el.innerHTML = userInput;\n"
              "child_process.exec(cmd);\n")},
    {"name": "py_ssti_debug", "kind": "sast", "suffix": ".py",
     "expected": {"CWE-1336", "CWE-489"},
     "text": ("render_template_string(tpl)\n"
              "app.run(debug=True)\n")},
    {"name": "py_clean", "kind": "sast", "suffix": ".py", "expected": set(),
     "text": ("import subprocess, json, hashlib, yaml\n"
              "subprocess.run(['ls', '-l'])\n"
              "obj = json.loads(payload)\n"
              "h = hashlib.sha256(pw).hexdigest()\n"
              "cfg = yaml.safe_load(stream)\n")},
    {"name": "js_clean", "kind": "sast", "suffix": ".js", "expected": set(),
     "text": ("el.textContent = userInput;\n"
              "const x = JSON.parse(data);\n")},
    # Comments that merely NAME a sink must never be flagged (precision guard).
    {"name": "py_comment_clean", "kind": "sast", "suffix": ".py", "expected": set(),
     "text": ("# any other frame eval (e.g. document.eval) is benign here\n"
              "    # os.system('x') would be dangerous, but this is just a note\n"
              "        # pickle.loads(x) documented for reviewers\n")},
    {"name": "js_comment_clean", "kind": "sast", "suffix": ".js", "expected": set(),
     "text": ("// child_process.exec(cmd) would be unsafe; we don't do that\n"
              "/* innerHTML = x is an XSS sink, documented for reviewers */\n")},
    {"name": "deps_py_vuln", "kind": "deps", "ecosystem": "pypi",
     "expected": {"CWE-20", "CWE-200"},
     "text": "pyyaml==5.1\nrequests==2.20.0\n"},
    {"name": "deps_npm_vuln", "kind": "deps", "ecosystem": "npm",
     "expected": {"CWE-77", "CWE-1321"},
     "text": '{"dependencies": {"lodash": "4.17.0", "minimist": "1.2.0"}}'},
    {"name": "deps_py_clean", "kind": "deps", "ecosystem": "pypi",
     "expected": set(), "text": "pyyaml==6.0.1\nrequests==2.31.0\n"},
    # --- extra Python ---
    {"name": "py_extra_vuln", "kind": "sast", "suffix": ".py",
     "expected": {"CWE-347", "CWE-22", "CWE-79"},
     "text": ('jwt.decode(token, options={"verify_signature": False})\n'
              "tar.extractall(dest)\n"
              "return mark_safe(user_bio)\n")},
    {"name": "py_extra_clean", "kind": "sast", "suffix": ".py", "expected": set(),
     "text": ('jwt.decode(token, key, algorithms=["HS256"])\n'
              'tar.extractall(dest, filter="data")\n'
              "return escape(user_bio)\n")},
    # --- Go ---
    {"name": "go_vuln", "kind": "sast", "suffix": ".go",
     "expected": {"CWE-295", "CWE-78", "CWE-327", "CWE-89"},
     "text": ("cfg := tls.Config{InsecureSkipVerify: true}\n"
              'out, _ := exec.Command("sh", "-c", userCmd).Output()\n'
              "h := md5.New()\n"
              'rows, _ := db.Query(fmt.Sprintf("SELECT * FROM u WHERE n=%s", x))\n')},
    {"name": "go_clean", "kind": "sast", "suffix": ".go", "expected": set(),
     "text": ("cfg := tls.Config{MinVersion: tls.VersionTLS13}\n"
              'out, _ := exec.Command("ls", "-l").Output()\n'
              "h := sha256.New()\n"
              'rows, _ := db.Query("SELECT * FROM u WHERE n = ?", id)\n')},
    # --- PHP ---
    {"name": "php_vuln", "kind": "sast", "suffix": ".php",
     "expected": {"CWE-95", "CWE-78", "CWE-98", "CWE-79", "CWE-502"},
     "text": ("eval($code);\n"
              "system($_GET['cmd']);\n"
              "include($_GET['page']);\n"
              "echo $_GET['name'];\n"
              "unserialize($data);\n")},
    {"name": "php_clean", "kind": "sast", "suffix": ".php", "expected": set(),
     "text": ("$x = json_decode($data, true);\n"
              'system("ls -l");\n'
              'include("header.php");\n'
              "echo htmlspecialchars($name);\n")},
    # --- Java ---
    {"name": "java_vuln", "kind": "sast", "suffix": ".java",
     "expected": {"CWE-78", "CWE-89", "CWE-502", "CWE-327"},
     "text": ("Runtime.getRuntime().exec(cmd);\n"
              'ResultSet rs = stmt.executeQuery("SELECT * FROM u WHERE id=" + id);\n'
              "ObjectInputStream ois = new ObjectInputStream(in);\n"
              'MessageDigest md = MessageDigest.getInstance("MD5");\n')},
    {"name": "java_clean", "kind": "sast", "suffix": ".java", "expected": set(),
     "text": ('Process p = new ProcessBuilder("ls").start();\n'
              "ResultSet rs = pstmt.executeQuery();\n"
              'MessageDigest md = MessageDigest.getInstance("SHA-256");\n')},
    # --- Ruby ---
    {"name": "ruby_vuln", "kind": "sast", "suffix": ".rb",
     "expected": {"CWE-95", "CWE-78", "CWE-502", "CWE-79"},
     "text": ("eval(params[:code])\n"
              'system("ping #{host}")\n'
              "obj = Marshal.load(data)\n"
              "@bio = user_bio.html_safe\n")},
    {"name": "ruby_clean", "kind": "sast", "suffix": ".rb", "expected": set(),
     "text": ("obj = JSON.parse(data)\n"
              'system("ls", "-l")\n'
              "@bio = h(user_bio)\n")},
    # --- C# / .NET ---
    {"name": "cs_vuln", "kind": "sast", "suffix": ".cs",
     "expected": {"CWE-89", "CWE-78", "CWE-502", "CWE-327"},
     "text": ('var cmd = new SqlCommand("SELECT * FROM u WHERE n=" + name, conn);\n'
              'Process.Start("cmd.exe", "/c " + userArg);\n'
              "var fmt = new BinaryFormatter();\n"
              "var md = MD5.Create();\n")},
    {"name": "cs_clean", "kind": "sast", "suffix": ".cs", "expected": set(),
     "text": ('var cmd = new SqlCommand("SELECT * FROM u WHERE n=@n", conn);\n'
              'Process.Start("mytool", args);\n'
              "var md = SHA256.Create();\n")},
    # --- Rust ---
    {"name": "rust_vuln", "kind": "sast", "suffix": ".rs",
     "expected": {"CWE-78", "CWE-89"},
     "text": ('let out = Command::new("sh").arg("-c").arg(user_cmd).output();\n'
              'let rows = sqlx::query(&format!("SELECT * FROM u WHERE n={}", n));\n')},
    {"name": "rust_clean", "kind": "sast", "suffix": ".rs", "expected": set(),
     "text": ('let out = Command::new("ls").args(["-l"]).output();\n'
              'let rows = sqlx::query("SELECT * FROM u WHERE n = ?").bind(n);\n')},
)


def _dep_cwes_for_text(text: str, ecosystem: str) -> set[str]:
    advisories = _load_advisories()
    deps = _parse_package_json(text) if ecosystem == "npm" else _parse_requirements(text)
    out: set[str] = set()
    for pkg, ver in deps:
        for spec, _adv, cwe, _sev, _note in advisories.get(ecosystem, {}).get(pkg, []):
            if _spec_matches(ver, spec):
                out.add(cwe)
    return out


def bench() -> dict[str, Any]:
    """Score the engine's detection against the labeled corpus. Pure (no scope,
    network, or memory) — Olympus measuring itself. Returns precision / recall /
    F1 plus per-sample TP/FP/FN, so a rule change that regresses is visible and
    caught by the test-suite floor."""
    tp = fp = fn = 0
    per_sample: list[dict] = []
    for s in _BENCH_CORPUS:
        expected: set[str] = set(s["expected"])
        if s["kind"] == "sast":
            detected = {f.cwe for f in _sast_findings_for_text(s["text"],
                                                               s["suffix"], "")}
        elif s["kind"] == "deps":
            detected = _dep_cwes_for_text(s["text"], s.get("ecosystem", "pypi"))
        else:
            detected = set()
        stp, sfp, sfn = (len(detected & expected), len(detected - expected),
                         len(expected - detected))
        tp += stp
        fp += sfp
        fn += sfn
        per_sample.append({"name": s["name"], "expected": sorted(expected),
                           "detected": sorted(detected),
                           "tp": stp, "fp": sfp, "fn": sfn})
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"samples": len(_BENCH_CORPUS), "tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 3), "recall": round(recall, 3),
            "f1": round(f1, 3), "per_sample": per_sample}


def bench_scorecard() -> str:
    r = bench()
    lines = [
        "# Aegis Assessment self-benchmark",
        f"{r['samples']} labeled sample(s) · precision {r['precision']} · "
        f"recall {r['recall']} · F1 {r['f1']}",
        f"true-pos {r['tp']} · false-pos {r['fp']} · false-neg {r['fn']}",
        "",
    ]
    for s in r["per_sample"]:
        flag = "ok" if (s["fp"] == 0 and s["fn"] == 0) else "MISS"
        lines.append(f"- [{flag}] {s['name']}: expected {s['expected'] or '(clean)'} "
                     f"→ detected {s['detected'] or '(none)'}")
    return "\n".join(lines)


# ===========================================================================
# Assessment experience — durable, self-sharpening knowledge (the memory moat)
# ===========================================================================
# The genuine "gets stronger over time" loop: every weakness Olympus's OWN
# scanners/validators confirm accrues into a compact knowledge record, and that
# record is injected into Aegis's system prompt (specialists._extra_context), so
# future assessments PRIORITISE the classes most often present. It is the
# experience→knowledge→better-future-performance cycle Metis runs for the whole
# council, scoped to security assessment.
#
# Safety: only findings whose `source` is one of Olympus's deterministic
# producers are learned from — NEVER a finding an agent recorded via the
# record_finding tool (source="agent"), whose text could carry content an
# injected page steered in. So nothing untrusted can reach the self-evolving
# prompt. Knowledge is CWE-class aggregates (name + count + method), replay-inert,
# and bounded.

_MAX_KNOWLEDGE_CWES = 50
_MAX_KNOWLEDGE_FPS = 200          # per-CWE fingerprint set for dedup (bounded)
_LEARN_SOURCES = frozenset({"sast", "http_audit", "dep_audit", "secret_scan",
                            "active_validation"})


def _replaying() -> bool:
    return os.environ.get("OLYMPUS_REPLAY", "").strip().lower() in (
        "1", "true", "yes", "on")


def _knowledge_path(user: str) -> Path:
    return _store_dir(user) / "knowledge.json"


def _load_knowledge(user: str) -> dict:
    try:
        data = json.loads(_knowledge_path(user).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _learn_from_finding(rec: dict, user: str | None = None) -> None:
    """Accrue a confirmed, Olympus-produced finding into durable knowledge.
    No-op during replay, for agent-authored findings, or without a CWE — so the
    self-evolving prompt only ever reflects Olympus's own deterministic output.
    Best-effort: any failure is swallowed (learning must never break a scan)."""
    try:
        if _replaying() or rec.get("source") not in _LEARN_SOURCES:
            return
        cwe = rec.get("cwe") or ""
        if not cwe:
            return
        user = _user(user)
        know = _load_knowledge(user)
        entry = know.get(cwe) or {
            "cwe": cwe, "title": rec.get("title", ""),
            "severity": rec.get("severity", ""), "count": 0,
            "sources": [], "fingerprints": []}
        fp = rec.get("id") or ""
        if fp and fp in entry["fingerprints"]:
            return                                    # already counted
        if fp:
            entry["fingerprints"] = (entry["fingerprints"] + [fp])[-_MAX_KNOWLEDGE_FPS:]
        entry["count"] = int(entry.get("count", 0)) + 1
        src = rec.get("source", "")
        if src and src not in entry["sources"]:
            entry["sources"].append(src)
        entry["title"] = entry.get("title") or rec.get("title", "")
        know[cwe] = entry
        if len(know) > _MAX_KNOWLEDGE_CWES:           # keep the most-seen classes
            know = dict(sorted(know.items(),
                               key=lambda kv: kv[1].get("count", 0),
                               reverse=True)[:_MAX_KNOWLEDGE_CWES])
        _atomic_write(_knowledge_path(user), json.dumps(know, indent=2))
    except Exception:
        pass


def knowledge(user: str | None = None) -> list[dict]:
    """Accumulated assessment knowledge, most-confirmed class first."""
    know = _load_knowledge(_user(user))
    rows = [{"cwe": v.get("cwe", k), "title": v.get("title", ""),
             "severity": v.get("severity", ""), "count": int(v.get("count", 0)),
             "sources": list(v.get("sources", []))}
            for k, v in know.items()]
    return sorted(rows, key=lambda r: -r["count"])


def insights_block(user: str | None = None, limit: int = 12) -> str:
    """A compact 'what you've confirmed before' block for Aegis's prompt — the
    self-sharpening context. Empty until Olympus has confirmed something, so a
    fresh install carries no block. CWE-class aggregates only (no target data)."""
    rows = knowledge(user)[:max(1, limit)]
    if not rows:
        return ""
    lines = ["\n\n## Assessment experience (self-evolving — what you've confirmed "
             "before)\nPrioritise checking for the weakness classes you have most "
             "often confirmed on authorized assessments:"]
    for r in rows:
        methods = ", ".join(r["sources"]) or "scan"
        lines.append(f"- {r['cwe']} — {r['title']} ({r['count']}×, via {methods})")
    lines.append("Use this as a prior to focus recon/validation; it never widens "
                 "scope (scope is still the signed authorization list).")
    return "\n".join(lines)


def insights_summary(user: str | None = None) -> str:
    rows = knowledge(user)
    if not rows:
        return ("No assessment experience yet. Confirmed findings from Olympus's "
                "own scanners/validators will accrue here and sharpen Aegis over "
                "time.")
    out = [f"Assessment experience — {len(rows)} weakness class(es) confirmed:"]
    for r in rows:
        out.append(f"- {r['cwe']} {r['title']}: {r['count']}× "
                   f"(via {', '.join(r['sources']) or 'scan'})")
    return "\n".join(out)


# ===========================================================================
# Blast-radius containment — turn Strix's damage vectors into owned controls
# ===========================================================================
# Strix's "blast radius" is what an autonomous security agent can reach/break if
# it is wrong, injected, or misused. Olympus owns a named control for each
# vector; this section makes two of them FIRST-CLASS:
#
#   (a) Active egress confinement. Per-tool `require_scope` already gates WHICH
#       target each call may touch. `confined_egress` goes further: for the whole
#       duration of an assessment it pins outbound network to ONLY the signed
#       authorization's hosts, enforced at the gated-fetch layer (fail-closed).
#       So even a hijacked assessment physically cannot reach an out-of-scope
#       host, the operator's LAN, or a metadata endpoint — the inversion of
#       Strix's open-egress sandbox. A strict NO-OP when no assessment is active,
#       so ordinary Olympus fetches are byte-for-byte unchanged.
#   (b) A containment self-check that PROVES each vector is contained (like the
#       self-benchmark proves detection quality), so the guardrails are
#       demonstrable, not merely asserted.

# The active assessment egress scope: a frozenset of authorized host patterns, or
# None when no assessment is confining egress (the default → every check no-ops).
_ACTIVE_SCOPE: contextvars.ContextVar[frozenset | None] = contextvars.ContextVar(
    "assess_active_scope", default=None)


@contextmanager
def confined_egress(user: str | None = None,
                    targets: list[str] | None = None):
    """While this context is active, outbound network is confined to the signed
    authorization's hosts (or `targets` if given) — enforced at the gated-fetch
    layer, fail-closed. Nests safely (restores the prior scope on exit)."""
    if targets is not None:
        scope = frozenset(_normalize_targets(targets))
    else:
        scope = frozenset(
            pat for a in active_authorizations(user) for pat in a.get("targets", []))
    token = _ACTIVE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE_SCOPE.reset(token)


# Trusted advisory/infrastructure hosts that confinement always permits: they
# are Olympus's OWN dependency-vulnerability feed, NOT a target's LAN / metadata
# endpoint / out-of-scope host — so allowing them does not widen the blast
# radius the confinement contains (a hijacked run still cannot reach the
# operator's network or an arbitrary target). Kept tiny and explicit.
_CONFINEMENT_INFRA_ALLOW = frozenset({"api.osv.dev"})


def egress_confined_reason(host: str) -> str | None:
    """Reason string if `host` is refused by the active assessment egress
    confinement, else None. NO-OP (None) when no assessment is confining egress,
    so every non-assessment fetch is unaffected. The gated fetch path in tools.py
    calls this in addition to its SSRF/egress preamble. Loopback and a tiny set
    of trusted advisory-infra hosts are always permitted (see above)."""
    scope = _ACTIVE_SCOPE.get()
    if scope is None:
        return None                                   # no confinement active
    h = (host or "").strip().lower().strip("[]")
    if not h:
        return "assessment egress confinement: empty host refused"
    if security._host_is_loopback(h) or h in _CONFINEMENT_INFRA_ALLOW:
        return None                                   # trusted infra, not a target
    for pat in scope:
        if pat in ("local", "workspace", "."):
            continue                                  # not a network host
        if _host_matches(h, pat):
            return None
    return (f"assessment egress confinement: '{host}' is not in the authorized "
            "scope — refusing to leave the signed target set")


# --- containment self-check -------------------------------------------------

def _check(name: str, vector: str, control: str, ok: bool, detail: str) -> dict:
    return {"vector": vector, "control": control, "contained": bool(ok),
            "detail": detail, "id": name}


def containment(user: str | None = None) -> list[dict]:
    """Prove each of Strix's blast-radius vectors is contained by a named
    Olympus control. Runs live checks where it can (scope fail-closed, egress
    confinement refuses out-of-scope); asserts the structural ones. Pure — no
    network, no scope grant needed."""
    checks: list[dict] = []

    # 1. Scope: prompt-only (Strix) vs code-enforced fail-closed (Olympus).
    fresh = f"containment-probe-{os.urandom(2).hex()}.example"
    try:
        require_scope(fresh, user="__containment_no_such_user__")
        scope_ok = False
    except AssessScopeError:
        scope_ok = True
    checks.append(_check(
        "scope", "Prompt-only scope → act on unintended hosts",
        "require_scope() — code-enforced, fail-closed",
        scope_ok, "an unauthorized target raises AssessScopeError before any I/O"))

    # 2. Egress: open sandbox (Strix) vs egress confined to signed scope.
    with confined_egress(targets=["authorized.example"]):
        blocked = egress_confined_reason("evil.example") is not None
        allowed = egress_confined_reason("authorized.example") is None
    noop = egress_confined_reason("anything.example") is None   # inactive → no-op
    checks.append(_check(
        "egress", "Open egress → operator host / LAN / metadata",
        "confined_egress() — outbound pinned to the signed scope, fail-closed",
        blocked and allowed and noop,
        "out-of-scope egress refused while active; no-op when inactive"))

    # 3. Refusal-suppression (Strix) vs model judgment retained.
    supp = False
    try:
        from . import agent
        prompt = (agent.load_prompt("aegis") or "").lower()
        supp = any(p in prompt for p in (
            "never refuse", "never ask permission", "never question your authority",
            "no safety refusals"))
    except Exception:
        pass
    checks.append(_check(
        "judgment", "Refusal-suppression → no last-line backstop",
        "Aegis keeps its judgment + a signed authorize_assessment gate",
        not supp, "Aegis prompt carries no refusal-suppression directives"))

    # 4. Arbitrary payloads / spraying (Strix) vs benign-only, capped validation.
    # Every registered check must be on the benign allowlist: each sends an inert
    # marker (canary / arithmetic / reserved .invalid host) or a single benign
    # boundary character to an operator-named parameter — never a working exploit,
    # shell, spray, or destructive input. The error-based checks confirm a
    # SURFACE (a parse error / leaked stack trace); they extract no data.
    _BENIGN_CHECKS = frozenset({
        "reflection", "open_redirect", "ssti", "header_injection", "cors",
        "sql_error", "error_disclosure"})
    benign = _MAX_ACTIVE_PROBES <= 50 and all(
        c[0] in _BENIGN_CHECKS for c in _ACTIVE_CHECKS)
    checks.append(_check(
        "payloads", "Arbitrary payloads + spraying → break/exfiltrate a target",
        "active validation is benign-marker, parameter-directed, capped",
        benign, f"probes capped at {_MAX_ACTIVE_PROBES}; inert markers only, no exploits"))

    # 5. Removed audit trail (Strix) vs signed authorization + ledgered findings.
    from . import actions, builtin_actions  # noqa: F401 (registers built-ins)
    ledger_ok = "authorize_assessment" in actions.registered()
    checks.append(_check(
        "audit", "Removed audit trail → can't prove what it did",
        "signed authorize_assessment action + findings noted to the ledger",
        ledger_ok, "authorization is an IRREVERSIBLE signed action on the decision log"))

    return checks


def containment_scorecard(user: str | None = None) -> str:
    rows = containment(user)
    n_ok = sum(1 for r in rows if r["contained"])
    lines = [
        "# Blast-radius containment (Strix's damage vectors → Olympus controls)",
        f"{n_ok}/{len(rows)} vectors contained by a named, live control.", ""]
    for r in rows:
        mark = "✓" if r["contained"] else "✗ UNCONTAINED"
        lines.append(f"[{mark}] {r['vector']}")
        lines.append(f"    control: {r['control']}")
        lines.append(f"    proof:   {r['detail']}")
    return "\n".join(lines)
