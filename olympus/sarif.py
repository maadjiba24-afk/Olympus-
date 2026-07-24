"""CVSS 3.1 scoring + SARIF 2.1.0 export — the finding output contract.

A survey of Strix (an autonomous offensive-security agent — see
`docs/STRIX_TRACKING.md`) found a strong *output* discipline worth absorbing:
findings carry a working proof, a CVSS score, and export as schema-valid SARIF
so CI can ingest them. This module is Olympus's native, dependency-free version
of that contract, used by `olympus/assess.py`:

  * **CVSS 3.1 base scoring**, pure-Python, implementing the official spec
    (metric weights + the 3.1 `Roundup`), so a finding's severity is *computed*
    from a vector, not asserted by a model.
  * **SARIF 2.1.0** output (`github/codeql-action/upload-sarif`-compatible),
    keyed on CWE, so an Olympus assessment drops straight into GitHub
    code-scanning — the same CI surface Strix targets. Rules carry the
    `security-severity` GitHub ranks on (from the CVSS score), `security` +
    `external/cwe/cwe-N` tags, and a MITRE `helpUri`; each result carries a
    `partialFingerprints` so a consumer tracks the SAME finding across commits.

No external dependency: Strix pulls the `cvss` library and a reporting stack;
Olympus keeps its three-dep footprint. Everything here is pure arithmetic and
JSON, deterministic (sorted keys, no clock), so a finding's exported artifact is
stable and diffable.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

# ---------------------------------------------------------------------------
# CVSS 3.1 base score (spec: https://www.first.org/cvss/v3.1/specification-document)
# ---------------------------------------------------------------------------

# Metric weights. PR depends on Scope, so it is a nested map resolved at scoring.
_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.20}
_AC = {"L": 0.77, "H": 0.44}
_UI = {"N": 0.85, "R": 0.62}
_CIA = {"N": 0.0, "L": 0.22, "H": 0.56}
_PR_U = {"N": 0.85, "L": 0.62, "H": 0.27}   # Scope Unchanged
_PR_C = {"N": 0.85, "L": 0.68, "H": 0.50}   # Scope Changed

_REQUIRED = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")

_SEVERITY_BANDS = (
    (0.0, "None"), (0.1, "Low"), (4.0, "Medium"), (7.0, "High"), (9.0, "Critical"),
)


def _roundup(value: float) -> float:
    """The CVSS 3.1 `Roundup`: the smallest one-decimal number >= value, with the
    spec's integer-arithmetic definition (avoids binary-float surprises like
    4.02 -> 4.1)."""
    int_input = round(value * 100_000)
    if int_input % 10_000 == 0:
        return int_input / 100_000
    return (math.floor(int_input / 10_000) + 1) / 10.0


def severity_of(score: float) -> str:
    """Qualitative CVSS 3.1 severity rating for a base score."""
    label = "None"
    for threshold, name in _SEVERITY_BANDS:
        if score >= threshold:
            label = name
    return label


def parse_vector(vector: str) -> dict[str, str]:
    """Parse a `CVSS:3.1/AV:N/...` (or bare `AV:N/...`) string into its base
    metrics. Unknown/extra metrics are ignored; missing base metrics raise."""
    metrics: dict[str, str] = {}
    for part in (vector or "").strip().split("/"):
        if ":" not in part:
            continue
        key, _, val = part.partition(":")
        key = key.strip().upper()
        if key in _REQUIRED:
            metrics[key] = val.strip().upper()
    missing = [m for m in _REQUIRED if m not in metrics]
    if missing:
        raise ValueError(f"CVSS vector missing base metrics: {', '.join(missing)}")
    return metrics


def base_score(vector: str) -> float:
    """Compute the CVSS 3.1 *base score* (0.0-10.0) from a vector string.
    Raises ValueError on a malformed vector."""
    m = parse_vector(vector)
    scope_changed = m["S"] == "C"
    try:
        iss = 1 - (1 - _CIA[m["C"]]) * (1 - _CIA[m["I"]]) * (1 - _CIA[m["A"]])
        if scope_changed:
            impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
        else:
            impact = 6.42 * iss
        pr = (_PR_C if scope_changed else _PR_U)[m["PR"]]
        exploitability = 8.22 * _AV[m["AV"]] * _AC[m["AC"]] * pr * _UI[m["UI"]]
    except KeyError as err:
        raise ValueError(f"CVSS vector has an invalid metric value: {err}") from err
    if impact <= 0:
        return 0.0
    raw = impact + exploitability
    if scope_changed:
        raw *= 1.08
    return _roundup(min(raw, 10.0))


def normalized_vector(vector: str) -> str:
    """Canonical `CVSS:3.1/...base metrics...` string (base metrics only,
    ordered), so stored vectors compare and diff stably."""
    m = parse_vector(vector)
    return "CVSS:3.1/" + "/".join(f"{k}:{m[k]}" for k in _REQUIRED)


def score_or_none(vector: str | None) -> tuple[float | None, str | None]:
    """(base_score, severity) for a vector, or (None, None) if absent/invalid —
    never raises, so a finding with a bad/absent vector degrades gracefully to
    its declared severity label instead of crashing a report."""
    if not vector:
        return None, None
    try:
        s = base_score(vector)
    except ValueError:
        return None, None
    return s, severity_of(s)


# A representative CVSS band midpoint per qualitative label, used when a finding
# declares only a severity (no vector) — so SARIF still carries a numeric score.
_LABEL_SCORE = {"critical": 9.0, "high": 7.5, "medium": 5.0, "low": 3.0,
                "info": 0.0, "informational": 0.0, "none": 0.0}


def score_for_label(label: str) -> float:
    return _LABEL_SCORE.get((label or "").strip().lower(), 0.0)


# ---------------------------------------------------------------------------
# SARIF 2.1.0
# ---------------------------------------------------------------------------

_SARIF_LEVEL = {"critical": "error", "high": "error", "medium": "warning",
                "low": "note", "info": "note", "informational": "note",
                "none": "note"}

_CWE_RE = re.compile(r"CWE-(\d+)", re.IGNORECASE)


def _sarif_level(severity: str) -> str:
    return _SARIF_LEVEL.get((severity or "").strip().lower(), "warning")


def _cwe_number(cwe: Any) -> str | None:
    """The bare numeric id from a `CWE-89` string (or None), for helpUri + tags."""
    m = _CWE_RE.search(str(cwe or ""))
    return m.group(1) if m else None


def _rule_id(finding: dict) -> str:
    cwe = str(finding.get("cwe") or "").strip()
    m = _CWE_RE.search(cwe)
    if m:
        return f"CWE-{m.group(1)}"
    # Fall back to a stable slug of the title so every result has a rule.
    slug = re.sub(r"[^a-z0-9]+", "-", str(finding.get("title", "finding")).lower())
    return slug.strip("-")[:60] or "finding"


def to_sarif(findings: list[dict], *, tool_name: str = "Olympus Aegis",
             tool_version: str = "1", info_uri: str = "") -> dict[str, Any]:
    """Build a SARIF 2.1.0 document from a list of finding dicts. Rules are keyed
    on CWE (falling back to a title slug); each finding becomes one result with
    its level, message, location, and CVSS in properties. Deterministic."""
    rules: dict[str, dict] = {}
    results: list[dict] = []
    for f in findings:
        rid = _rule_id(f)
        score, sev = score_or_none(f.get("cvss_vector"))
        if score is None:
            score = f.get("cvss_score")
            sev = f.get("severity")
        if rid not in rules:
            cwe_num = _cwe_number(f.get("cwe"))
            # tags: GitHub code scanning filters on these; "external/cwe/cwe-N" is
            # its documented convention for CWE linkage.
            tags = ["security"]
            if cwe_num:
                tags.append(f"external/cwe/cwe-{cwe_num}")
            rule: dict[str, Any] = {
                "id": rid,
                "name": str(f.get("cwe") or f.get("title") or rid),
                "shortDescription": {"text": str(f.get("title", rid))[:200]},
                "defaultConfiguration": {"level": _sarif_level(f.get("severity", ""))},
                "properties": {"tags": tags},
            }
            # security-severity: the numeric string GitHub code scanning reads to
            # rank a rule (it does NOT parse a CVSS vector). Emit the finding's
            # own CVSS base score so the ecosystem severity matches ours.
            if score is not None:
                rule["properties"]["security-severity"] = f"{float(score):.1f}"
            if cwe_num:
                rule["helpUri"] = \
                    f"https://cwe.mitre.org/data/definitions/{cwe_num}.html"
            help_text = str(f.get("remediation") or "").strip()
            if help_text:
                rule["help"] = {"text": help_text[:2000]}
            rules[rid] = rule
        loc = str(f.get("location") or "").strip()
        result: dict[str, Any] = {
            "ruleId": rid,
            "level": _sarif_level(f.get("severity", "")),
            "message": {"text": str(f.get("evidence") or f.get("title", ""))[:2000]},
            "properties": {
                "severity": str(f.get("severity", "")),
                "cvssScore": score,
                "cvssVector": str(f.get("cvss_vector") or ""),
                "cwe": str(f.get("cwe") or ""),
                "confidence": str(f.get("confidence") or ""),
            },
        }
        # partialFingerprints: stable result identity across runs, so a SARIF
        # consumer (e.g. GitHub code scanning) tracks the SAME finding across
        # commits instead of re-opening it. Uses the finding's own fingerprint.
        fp = str(f.get("id") or "").strip()
        if fp:
            result["partialFingerprints"] = {"olympusFingerprint/v1": fp}
        if loc:
            artifact = loc.split(":", 1)[0]
            region = None
            m = re.search(r":(\d+)$", loc)
            if m:
                region = {"startLine": int(m.group(1))}
            phys: dict[str, Any] = {"artifactLocation": {"uri": artifact}}
            if region:
                phys["region"] = region
            result["locations"] = [{"physicalLocation": phys}]
        results.append(result)

    driver: dict[str, Any] = {
        "name": tool_name,
        "version": str(tool_version),
        "rules": [rules[k] for k in sorted(rules)],
    }
    if info_uri:
        driver["informationUri"] = info_uri
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": driver}, "results": results}],
    }


def to_sarif_json(findings: list[dict], **kwargs: Any) -> str:
    return json.dumps(to_sarif(findings, **kwargs), indent=2, sort_keys=True) + "\n"
