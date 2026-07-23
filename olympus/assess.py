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

import fnmatch
import ipaddress
import json
import os
import re
import time
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
_MAX_ACTIVE_PROBES = 20         # hard cap on active-validation requests (never a spray)
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
    },
    "npm": {
        "lodash": [("<4.17.21", "CVE-2021-23337", "CWE-77", "high",
                    "Command injection via template")],
        "minimist": [("<1.2.6", "CVE-2021-44906", "CWE-1321", "critical",
                      "Prototype pollution")],
        "axios": [("<1.6.0", "CVE-2023-45857", "CWE-200", "medium",
                   "SSRF / credential leak via absolute URL")],
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


def dep_audit(path: str = ".", record: bool = True,
              user: str | None = None) -> dict[str, Any]:
    """Audit declared dependencies (requirements.txt / package.json) against the
    bundled advisory index. Offline, deterministic, scope-gated, workspace-
    confined. Absorbs Strix's dependency-CVE scan without a live network feed
    (an operator can point OLYMPUS_ASSESS_ADVISORIES at a richer index)."""
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
    findings: list[Finding] = []
    for eco, name, text in manifests:
        deps = _parse_package_json(text) if eco == "npm" else _parse_requirements(text)
        for pkg, version in deps:
            for spec, adv_id, cwe, sev, note in advisories.get(eco, {}).get(pkg, []):
                if _spec_matches(version, spec):
                    findings.append(Finding(
                        title=f"Vulnerable dependency: {pkg} {version} ({adv_id})",
                        severity=sev, cwe=cwe,
                        cvss_vector="", location=f"{name}",
                        evidence=f"{pkg}=={version} matches {spec}: {note}",
                        remediation=f"Upgrade {pkg} past {spec.lstrip('<=')}.",
                        confidence="high", source="dep_audit"))
    stored = [record_finding(f, user) for f in findings] if record else \
        [asdict(f) for f in findings]
    return {"path": str(path), "manifests": [m[1] for m in manifests],
            "findings": stored, "count": len(stored)}


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


# The registry — extended over successive loop iterations (self-evolving moat).
# Every entry is benign, scope-locked, parameter-directed, and capped.
_ACTIVE_CHECKS: tuple[tuple[str, Any], ...] = (
    ("reflection", _reflection_check),
    ("open_redirect", _open_redirect_check),
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
    {"name": "deps_py_vuln", "kind": "deps", "ecosystem": "pypi",
     "expected": {"CWE-20", "CWE-200"},
     "text": "pyyaml==5.1\nrequests==2.20.0\n"},
    {"name": "deps_npm_vuln", "kind": "deps", "ecosystem": "npm",
     "expected": {"CWE-77", "CWE-1321"},
     "text": '{"dependencies": {"lodash": "4.17.0", "minimist": "1.2.0"}}'},
    {"name": "deps_py_clean", "kind": "deps", "ecosystem": "pypi",
     "expected": set(), "text": "pyyaml==6.0.1\nrequests==2.31.0\n"},
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
