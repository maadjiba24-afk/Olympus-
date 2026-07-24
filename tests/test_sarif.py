"""CVSS 3.1 scoring + SARIF 2.1.0 export (olympus/sarif.py)."""

import json

import pytest

from olympus import sarif


# --- CVSS 3.1 base score against the spec's reference vectors ---------------

@pytest.mark.parametrize("vector,score,severity", [
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8, "Critical"),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1, "Medium"),
    ("CVSS:3.1/AV:N/AC:H/PR:L/UI:N/S:U/C:L/I:N/A:N", 3.1, "Low"),
    ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H", 7.8, "High"),
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0, "Critical"),
    ("CVSS:3.1/AV:P/AC:H/PR:H/UI:R/S:U/C:N/I:N/A:N", 0.0, "None"),
])
def test_base_score_matches_reference(vector, score, severity):
    assert sarif.base_score(vector) == pytest.approx(score, abs=0.05)
    assert sarif.severity_of(sarif.base_score(vector)) == severity


def test_bare_vector_without_prefix_scores():
    assert sarif.base_score("AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == pytest.approx(9.8)


def test_malformed_vector_raises():
    with pytest.raises(ValueError):
        sarif.base_score("CVSS:3.1/AV:N/AC:L")            # missing base metrics
    with pytest.raises(ValueError):
        sarif.base_score("CVSS:3.1/AV:Z/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")  # bad value


def test_score_or_none_is_graceful():
    assert sarif.score_or_none(None) == (None, None)
    assert sarif.score_or_none("garbage") == (None, None)
    score, sev = sarif.score_or_none("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
    assert score == pytest.approx(9.8) and sev == "Critical"


def test_normalized_vector_is_stable():
    a = sarif.normalized_vector("CVSS:3.1/A:H/C:H/I:H/S:U/UI:N/PR:N/AC:L/AV:N/extra:X")
    assert a == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"


# --- SARIF 2.1.0 -----------------------------------------------------------

def _finding(**kw):
    base = {"title": "SQL injection", "severity": "critical", "cwe": "CWE-89",
            "cvss_vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            "location": "app.py:42", "evidence": "execute(f'... {x}')",
            "remediation": "Use bound parameters."}
    base.update(kw)
    return base


def test_sarif_document_shape():
    doc = sarif.to_sarif([_finding()])
    assert doc["version"] == "2.1.0"
    assert doc["$schema"].endswith("sarif-2.1.0.json")
    run = doc["runs"][0]
    assert run["tool"]["driver"]["name"] == "Olympus Aegis"
    assert len(run["results"]) == 1
    res = run["results"][0]
    assert res["ruleId"] == "CWE-89"
    assert res["level"] == "error"                       # critical -> error
    assert res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "app.py"
    assert res["locations"][0]["physicalLocation"]["region"]["startLine"] == 42
    assert res["properties"]["cvssScore"] == pytest.approx(9.8)


def test_sarif_level_mapping():
    levels = {f["severity"]: sarif.to_sarif([f])["runs"][0]["results"][0]["level"]
              for f in (_finding(severity="critical"), _finding(severity="high"),
                        _finding(severity="medium"), _finding(severity="low"),
                        _finding(severity="info"))}
    assert levels == {"critical": "error", "high": "error", "medium": "warning",
                      "low": "note", "info": "note"}


def test_sarif_rule_dedup_by_cwe():
    doc = sarif.to_sarif([_finding(location="a.py:1"), _finding(location="b.py:2")])
    run = doc["runs"][0]
    assert len(run["results"]) == 2
    assert len(run["tool"]["driver"]["rules"]) == 1      # same CWE -> one rule


def test_sarif_without_cwe_falls_back_to_title_slug():
    doc = sarif.to_sarif([_finding(cwe="", title="Weird Thing")])
    assert doc["runs"][0]["results"][0]["ruleId"] == "weird-thing"


def test_sarif_json_is_deterministic():
    a = sarif.to_sarif_json([_finding()])
    b = sarif.to_sarif_json([_finding()])
    assert a == b
    json.loads(a)                                        # valid JSON


# --- extended finding contract (GitHub code-scanning interop) ---------------

def test_sarif_rule_has_security_severity_and_cwe_taxonomy():
    rule = sarif.to_sarif([_finding()])["runs"][0]["tool"]["driver"]["rules"][0]
    # security-severity: the numeric string GitHub ranks on (matches our CVSS)
    assert rule["properties"]["security-severity"] == "9.8"
    # CWE linkage: tags in GitHub's documented convention + a MITRE helpUri
    assert "security" in rule["properties"]["tags"]
    assert "external/cwe/cwe-89" in rule["properties"]["tags"]
    assert rule["helpUri"] == "https://cwe.mitre.org/data/definitions/89.html"


def test_sarif_result_has_partial_fingerprint():
    f = _finding(id="abc123def456")
    res = sarif.to_sarif([f])["runs"][0]["results"][0]
    assert res["partialFingerprints"]["olympusFingerprint/v1"] == "abc123def456"


def test_sarif_no_cwe_omits_taxonomy_but_keeps_security_tag():
    rule = sarif.to_sarif([_finding(cwe="")])["runs"][0]["tool"]["driver"]["rules"][0]
    assert rule["properties"]["tags"] == ["security"]     # no cwe tag
    assert "helpUri" not in rule                          # no MITRE link
    # security-severity still emitted from the CVSS vector
    assert rule["properties"]["security-severity"] == "9.8"


def test_sarif_missing_vector_omits_security_severity():
    # A finding with no vector and no score: no security-severity to assert.
    f = {"title": "Info leak", "severity": "low", "cwe": "CWE-200",
         "location": "x.py:1"}
    rule = sarif.to_sarif([f])["runs"][0]["tool"]["driver"]["rules"][0]
    assert "security-severity" not in rule["properties"]
    assert "external/cwe/cwe-200" in rule["properties"]["tags"]


# --- SARIF ingestion (from_sarif — the inverse of to_sarif) -----------------

def test_from_sarif_roundtrips_our_own_export():
    # A finding exported by to_sarif re-parses back to the same core fields.
    findings = [_finding()]
    doc = sarif.to_sarif(findings)
    back = sarif.from_sarif(doc)
    assert len(back) == 1
    f = back[0]
    assert f["cwe"] == "CWE-89"
    assert f["severity"] == "critical"                 # from security-severity 9.8
    assert f["location"] == "app.py:42"
    assert f["cvss_vector"] == findings[0]["cvss_vector"]  # carried in properties
    assert f["source"].startswith("imported:")


def test_from_sarif_parses_third_party_shape():
    # A GitHub-style SARIF from another tool (no cvss vector, cwe only in tags).
    doc = {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "semgrep", "rules": [{
                "id": "py.sqli",
                "name": "SQL injection",
                "shortDescription": {"text": "SQL injection"},
                "properties": {"tags": ["security", "external/cwe/cwe-89"],
                               "security-severity": "8.8"},
                "help": {"text": "Use parameterized queries."},
            }]}},
            "results": [{
                "ruleId": "py.sqli",
                "level": "error",
                "message": {"text": "tainted input into execute()"},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": "db.py"},
                    "region": {"startLine": 7}}}],
            }],
        }],
    }
    out = sarif.from_sarif(doc)
    assert len(out) == 1
    f = out[0]
    assert f["cwe"] == "CWE-89"
    assert f["severity"] == "high"                     # 8.8 → High band
    assert f["location"] == "db.py:7"
    assert f["remediation"] == "Use parameterized queries."
    assert f["source"] == "imported:semgrep"


def test_from_sarif_level_fallback_without_security_severity():
    doc = {"runs": [{"tool": {"driver": {"name": "t", "rules": []}},
                     "results": [{"ruleId": "r", "level": "warning",
                                  "message": {"text": "m"}}]}]}
    assert sarif.from_sarif(doc)[0]["severity"] == "medium"   # warning → medium


def test_from_sarif_is_total_on_malformed_input():
    # Never raises on junk — returns [] or skips malformed runs/results.
    assert sarif.from_sarif({}) == []
    assert sarif.from_sarif({"runs": "nope"}) == []
    assert sarif.from_sarif({"runs": [{"results": ["bad", 3, None]}]}) == []
    assert sarif.from_sarif("not a dict") == []
