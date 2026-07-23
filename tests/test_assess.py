"""Aegis Assessment suite (olympus/assess.py) — native Strix-absorption.

Covers the moat inversions: scope enforced in code (fail-closed), an agent
cannot self-authorize, gated/scoped recon, deterministic audits, SAST/secret/dep
findings with computed CVSS, dedup, SARIF export, and the budget stop.
"""

import json

import pytest

from olympus import assess, security, tools


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(ws))
    return ws


# --- scope is enforced in CODE, fail-closed (the headline moat) -------------

def test_scope_fail_closed_without_authorization():
    assert assess.in_scope("example.com") is False
    with pytest.raises(assess.AssessScopeError):
        assess.require_scope("example.com")


def test_grant_then_in_scope_and_subdomains():
    assess.grant(["example.com"], expires_in=3600)
    assert assess.in_scope("https://example.com/path") is True
    assert assess.in_scope("api.example.com") is True          # subdomain
    assert assess.in_scope("evil.com") is False                # out of scope
    assess.require_scope("example.com")                        # no raise


def test_wildcard_and_cidr_targets():
    assess.grant(["*.internal.test", "10.0.0.0/24"], expires_in=3600)
    assert assess.in_scope("host.internal.test") is True
    assert assess.in_scope("internal.test") is True
    assert assess.in_scope("10.0.0.5") is True
    assert assess.in_scope("10.0.1.5") is False


def test_expired_authorization_is_out_of_scope():
    assess.grant(["example.com"], expires_in=60)               # clamped min 60s
    # Force expiry by rewriting the store with a past timestamp.
    path = assess._auth_path(assess._user())
    data = json.loads(path.read_text())
    data[0]["expires"] = 1.0
    path.write_text(json.dumps(data))
    assert assess.in_scope("example.com") is False
    assert assess.active_authorizations() == []


def test_grant_requires_a_target():
    with pytest.raises(ValueError):
        assess.grant([])


def test_agent_cannot_self_authorize():
    # There is NO tool that grants scope — authorization is operator-only, on the
    # approval spine. The inversion of Strix's self-authorization.
    assert "authorize_assessment" not in tools.HANDLERS
    assert "assess_authorize" not in tools.HANDLERS
    assert "assess_grant" not in tools.HANDLERS
    # The read-only scope tool exists and is trusted (own-state).
    assert "assess_scope" in tools.HANDLERS


def test_revoke():
    rec = assess.grant(["example.com"], expires_in=3600)
    assert assess.revoke(rec["id"]) is True
    assert assess.in_scope("example.com") is False
    assert assess.revoke("nope") is False


# --- classification: recon/http_audit ingest; scanners/findings are trusted --

def test_tool_classification():
    assert "assess_recon" in security.INGESTION_TOOLS
    assert "assess_http_audit" in security.INGESTION_TOOLS
    for t in ("assess_scope", "assess_sast", "assess_secrets", "assess_deps",
              "record_finding", "list_findings", "export_findings"):
        assert t in security.TRUSTED_TOOLS
    assert security.should_wrap("assess_recon") is True        # untrusted target
    assert security.should_wrap("assess_sast") is False        # own/local


# --- recon + http_audit (network mocked, always scope-gated) ----------------

def _fake_probe(headers, status=200, body="<title>Acme</title>"):
    def _probe(url, max_bytes=400_000):
        return {"status": status, "headers": headers, "body": body, "url": url}
    return _probe


def test_recon_is_scope_gated(monkeypatch):
    monkeypatch.setattr(tools, "_http_probe", _fake_probe({"server": "nginx"}))
    with pytest.raises(assess.AssessScopeError):
        assess.recon("example.com")
    assess.grant(["example.com"], expires_in=3600)
    r = assess.recon("example.com")
    assert r["server"] == "nginx"
    assert r["title"] == "Acme"
    assert "strict-transport-security" in r["security_headers"]


def test_http_audit_flags_missing_headers(monkeypatch):
    assess.grant(["example.com"], expires_in=3600)
    monkeypatch.setattr(tools, "_http_probe", _fake_probe(
        {"set-cookie": "sid=1", "access-control-allow-origin": "*",
         "access-control-allow-credentials": "true"}))
    r = assess.http_audit("example.com")
    titles = {f["title"] for f in r["findings"]}
    assert any("Strict-Transport-Security" in t for t in titles)
    assert any("Content-Security-Policy" in t for t in titles)
    assert any("Secure flag" in t for t in titles)
    assert any("HttpOnly" in t for t in titles)
    assert any("any origin with credentials" in t for t in titles)
    # Every finding carries a computed CVSS score.
    assert all(f["cvss_score"] is not None for f in r["findings"])


# --- SAST / secret / dependency scans (workspace-confined) ------------------

def test_sast_scan_finds_sinks(workspace):
    assess.grant(["local"], expires_in=3600)
    (workspace / "bad.py").write_text(
        "import os\n"
        "os.system('rm -rf ' + user)\n"
        "eval(payload)\n"
        "import yaml; yaml.load(data)\n"
        "requests.get(u, verify=False)\n")
    r = assess.sast_scan(".")
    cwes = {f["cwe"] for f in r["findings"]}
    assert {"CWE-78", "CWE-95", "CWE-502", "CWE-295"} <= cwes
    assert all(":" in f["location"] for f in r["findings"])     # file:line


def test_sast_scan_scope_gated(workspace):
    with pytest.raises(assess.AssessScopeError):
        assess.sast_scan(".")


def test_secret_scan_redacts_evidence(workspace):
    assess.grant(["local"], expires_in=3600)
    (workspace / "cfg.py").write_text('AWS = "AKIAIOSFODNN7EXAMPLE"\n')
    r = assess.secret_scan(".")
    assert r["count"] >= 1
    assert r["findings"][0]["cwe"] == "CWE-798"
    # The raw secret must never appear in the stored evidence.
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(r["findings"])


def test_dep_audit_matches_bundled_advisories(workspace):
    assess.grant(["local"], expires_in=3600)
    (workspace / "requirements.txt").write_text("pyyaml==5.1\nrequests==2.20.0\n")
    r = assess.dep_audit(".")
    assert r["count"] == 2
    assert any("CVE-2020-14343" in f["title"] for f in r["findings"])


def test_dep_audit_clean_when_current(workspace):
    assess.grant(["local"], expires_in=3600)
    (workspace / "requirements.txt").write_text("pyyaml==6.0.1\n")
    assert assess.dep_audit(".")["count"] == 0


# --- findings: computed severity, dedup, export -----------------------------

def test_record_finding_computes_severity_from_vector():
    rec = assess.record_finding(assess.Finding(
        title="X", severity="low",                             # will be overridden
        cwe="CWE-89",
        cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"))
    assert rec["cvss_score"] == pytest.approx(9.8)
    assert rec["severity"] == "critical"                       # from the vector


def test_record_finding_dedups():
    f = assess.Finding(title="dup", cwe="CWE-79", location="a.js:1",
                       severity="medium")
    a = assess.record_finding(f)
    b = assess.record_finding(f)
    assert a["duplicate"] is False and b["duplicate"] is True
    assert len(assess.list_findings()) == 1


def test_export_findings_formats():
    assess.record_finding(assess.Finding(
        title="SQLi", cwe="CWE-89", location="app.py:1",
        cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"))
    md = assess.export_findings("markdown")
    assert "SQLi" in md and "CVSS" in md
    js = json.loads(assess.export_findings("json"))
    assert js[0]["cwe"] == "CWE-89"
    sarif_doc = json.loads(assess.export_findings("sarif"))
    assert sarif_doc["version"] == "2.1.0"
    assert sarif_doc["runs"][0]["results"][0]["ruleId"] == "CWE-89"


# --- run_assessment orchestration + budget stop -----------------------------

def test_run_assessment_whitebox(workspace, monkeypatch):
    assess.grant(["example.com", "local"], expires_in=3600)
    monkeypatch.setattr(tools, "_http_probe", _fake_probe({"server": "nginx"}))
    (workspace / "app.py").write_text("eval(x)\n")
    (workspace / "requirements.txt").write_text("pyyaml==5.1\n")
    r = assess.run_assessment("example.com", source_path=".")
    assert "recon" in r["phases"] and "sast" in r["phases"]
    assert r["total_findings"] >= 2                            # http headers + eval + dep
    assert r["budget_stopped"] is False


# --- active validation: benign, scope-locked, non-destructive ---------------

def _reflect_probe(reflect="raw"):
    """Fake probe: reflect the 'q' param value raw / encoded / not at all."""
    from urllib.parse import parse_qsl, urlparse

    def _probe(url, max_bytes=400_000):
        q = dict(parse_qsl(urlparse(url).query))
        val = q.get("q", "")
        if reflect == "raw":
            body = f"<div>results for {val}</div>"           # unescaped
        elif reflect == "encoded":
            enc = val.replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            body = f"<div>results for {enc}</div>"
        else:
            body = "<div>no results</div>"                   # not reflected
        return {"status": 200, "headers": {}, "body": body, "url": url}
    return _probe


def test_validate_scope_gated(monkeypatch):
    monkeypatch.setattr(tools, "_http_probe", _reflect_probe("raw"))
    with pytest.raises(assess.AssessScopeError):
        assess.validate("https://app.example/s?q=x")


def test_validate_confirms_unescaped_reflection(monkeypatch):
    assess.grant(["app.example"], expires_in=3600)
    monkeypatch.setattr(tools, "_http_probe", _reflect_probe("raw"))
    r = assess.validate("https://app.example/s?q=hi")
    assert r["count"] == 1
    f = r["findings"][0]
    assert f["cwe"] == "CWE-79" and f["source"] == "active_validation"
    assert f["confidence"] == "high"


def test_validate_ignores_encoded_and_absent_reflection(monkeypatch):
    assess.grant(["app.example"], expires_in=3600)
    monkeypatch.setattr(tools, "_http_probe", _reflect_probe("encoded"))
    assert assess.validate("https://app.example/s?q=hi")["count"] == 0
    monkeypatch.setattr(tools, "_http_probe", _reflect_probe("none"))
    assert assess.validate("https://app.example/s?q=hi")["count"] == 0


def test_validate_only_tests_present_params_no_spray(monkeypatch):
    # It must never guess/fuzz parameter names — only the ones in the URL.
    assess.grant(["app.example"], expires_in=3600)
    seen = []

    def _probe(url, max_bytes=400_000):
        from urllib.parse import parse_qsl, urlparse
        seen.extend(k for k, _ in parse_qsl(urlparse(url).query))
        return {"status": 200, "headers": {}, "body": "x", "url": url}
    monkeypatch.setattr(tools, "_http_probe", _probe)
    assess.validate("https://app.example/s?q=hi&page=2")
    # Exactly the two present params were probed — nothing invented.
    assert set(seen) == {"q", "page"}


def test_validate_requires_params_and_is_benign(monkeypatch):
    assess.grant(["app.example"], expires_in=3600)
    sent = []

    def _probe(url, max_bytes=400_000):
        sent.append(url)
        return {"status": 200, "headers": {}, "body": "x", "url": url}
    monkeypatch.setattr(tools, "_http_probe", _probe)
    assert "error" in assess.validate("https://app.example/s")     # no params
    assert sent == []                                              # no probe sent
    # The payload is a benign marker, never a script/exploit.
    assess.validate("https://app.example/s?q=hi")
    assert sent and "olympuscanary" in sent[0]
    assert "<script" not in sent[0].lower()


def test_validate_out_of_scope_refused(monkeypatch):
    assess.grant(["app.example"], expires_in=3600)
    monkeypatch.setattr(tools, "_http_probe", _reflect_probe("raw"))
    with pytest.raises(assess.AssessScopeError):
        assess.validate("https://evil.example/s?q=x")


def test_validate_tool_is_ingestion():
    assert "assess_validate" in security.INGESTION_TOOLS
    assert security.should_wrap("assess_validate") is True


def test_active_check_registry_extensible():
    # The self-evolving moat: checks live in a registry that compounds.
    assert set(assess.active_check_names()) >= {"reflection", "open_redirect"}


def _redirect_probe(kind):
    """Fake probe respecting follow_redirects=False for the open-redirect check."""
    def _probe(url, max_bytes=400_000, follow_redirects=True):
        from urllib.parse import parse_qsl, urlparse
        q = dict(parse_qsl(urlparse(url).query))
        canary = next((v for v in q.values() if "olympus-canary" in v), "")
        if kind == "open" and canary:
            return {"status": 302, "headers": {"location": canary}, "body": "", "url": url}
        if kind == "safe_redirect" and canary:
            return {"status": 302, "headers": {"location": "/home"}, "body": "", "url": url}
        return {"status": 200, "headers": {}, "body": "ok", "url": url}
    return _probe


def test_validate_confirms_open_redirect(monkeypatch):
    assess.grant(["app.example"], expires_in=3600)
    monkeypatch.setattr(tools, "_http_probe", _redirect_probe("open"))
    r = assess.validate("https://app.example/go?next=/home")
    assert r["count"] == 1
    assert r["findings"][0]["cwe"] == "CWE-601"


def test_validate_ignores_safe_redirect(monkeypatch):
    assess.grant(["app.example"], expires_in=3600)
    monkeypatch.setattr(tools, "_http_probe", _redirect_probe("safe_redirect"))
    assert assess.validate("https://app.example/go?next=/home")["count"] == 0


def test_open_redirect_probe_does_not_follow(monkeypatch):
    # The canary target (.invalid) must never actually be requested — the check
    # reads the Location header with follow_redirects=False.
    assess.grant(["app.example"], expires_in=3600)
    seen_follow = []

    def _probe(url, max_bytes=400_000, follow_redirects=True):
        seen_follow.append(follow_redirects)
        return {"status": 200, "headers": {}, "body": "ok", "url": url}
    monkeypatch.setattr(tools, "_http_probe", _probe)
    assess.validate("https://app.example/go?next=/x")
    # The open-redirect check must call with follow_redirects=False.
    assert False in seen_follow


# --- self-benchmark: measured, regression-gated evolution -------------------

def test_bench_meets_quality_floor():
    r = assess.bench()
    # The regression gate: the engine must detect EVERY known-vulnerable fixture
    # (no false negatives) and produce ZERO false positives on the clean fixtures
    # (including comment-only lines). A rule change that regresses either fails here.
    assert r["samples"] >= 20
    assert r["fn"] == 0, r["per_sample"]                    # misses nothing known
    assert r["fp"] == 0, r["per_sample"]                    # no noise on clean code
    assert r["recall"] == 1.0
    assert r["precision"] == 1.0


def test_sast_covers_multiple_languages():
    # Detection breadth: the rule set spans Python, JS, Go, PHP, Java, Ruby.
    checks = {
        ".go": ("cfg := tls.Config{InsecureSkipVerify: true}", "CWE-295"),
        ".php": ("include($_GET['page']);", "CWE-98"),
        ".java": ("Runtime.getRuntime().exec(cmd);", "CWE-78"),
        ".rb": ("obj = Marshal.load(data)", "CWE-502"),
    }
    for suffix, (line, cwe) in checks.items():
        hits = {f.cwe for f in assess._sast_findings_for_text(line, suffix, "f")}
        assert cwe in hits, (suffix, line, hits)


def test_sast_skips_full_line_comments():
    # A comment that merely names a sink is not a sink (precision guard).
    assert assess._sast_findings_for_text("# eval(x) is dangerous\n", ".py", "f") == []
    assert assess._sast_findings_for_text("eval(x)\n", ".py", "f")  # real sink still flagged


def test_bench_is_pure_no_scope_needed():
    # bench() is Olympus measuring itself — it needs no authorization and touches
    # no network/memory, so it can run in CI unconditionally.
    r = assess.bench()
    assert isinstance(r["per_sample"], list) and r["per_sample"]
    assert "self-benchmark" in assess.bench_scorecard().lower()


def test_bench_uses_production_detection_logic(workspace):
    # The corpus is scored on the same _sast_findings_for_text the real scanner
    # uses, so the score can't drift from what production does. Prove the shared
    # helper detects a known sink.
    hits = assess._sast_findings_for_text("eval(x)\n", ".py", "f.py")
    assert any(h.cwe == "CWE-95" for h in hits)


# --- self-evolution: durable assessment knowledge sharpens Aegis over time ---

def test_findings_accrue_into_knowledge(workspace):
    assess.grant(["local"], expires_in=3600)
    (workspace / "a.py").write_text("eval(x)\nimport os\nos.system('x' + y)\n")
    assess.sast_scan(".")
    cwes = {r["cwe"] for r in assess.knowledge()}
    assert {"CWE-95", "CWE-78"} <= cwes
    block = assess.insights_block()
    assert "Assessment experience" in block and "CWE-95" in block


def test_knowledge_does_not_double_count(workspace):
    assess.grant(["local"], expires_in=3600)
    (workspace / "a.py").write_text("eval(x)\n")
    assess.sast_scan(".")
    assess.sast_scan(".")                                   # same finding again
    row = next(r for r in assess.knowledge() if r["cwe"] == "CWE-95")
    assert row["count"] == 1                                # deduped by fingerprint


def test_agent_findings_are_not_learned():
    # Injection safety: a finding the agent recorded (source="agent") whose text
    # could be attacker-steered must NEVER reach the self-evolving prompt.
    assess.record_finding(assess.Finding(title="steered", cwe="CWE-1337",
                                         source="agent"))
    assert all(r["cwe"] != "CWE-1337" for r in assess.knowledge())


def test_learning_is_replay_inert(workspace, monkeypatch):
    assess.grant(["local"], expires_in=3600)
    monkeypatch.setenv("OLYMPUS_REPLAY", "1")
    (workspace / "a.py").write_text("eval(x)\n")
    assess.sast_scan(".")
    assert assess.knowledge() == []                         # nothing learned in replay


def test_insights_block_empty_on_fresh_install():
    assert assess.insights_block() == ""                    # no experience yet


def test_aegis_prompt_includes_insights(workspace):
    from olympus.specialists import SPECIALISTS
    assess.grant(["local"], expires_in=3600)
    (workspace / "a.py").write_text("eval(x)\n")
    assess.sast_scan(".")
    prompt = SPECIALISTS["aegis"].system_prompt()
    assert "Assessment experience" in prompt and "CWE-95" in prompt


# --- blast-radius containment: egress confinement + the self-check -----------

def test_containment_all_vectors_contained():
    rows = assess.containment()
    assert len(rows) == 5
    assert all(r["contained"] for r in rows), \
        [r for r in rows if not r["contained"]]
    vectors = {r["id"] for r in rows}
    assert vectors == {"scope", "egress", "judgment", "payloads", "audit"}
    assert "5/5 vectors contained" in assess.containment_scorecard()


def test_egress_confinement_is_noop_when_inactive():
    # No assessment confining egress → every host passes (ordinary fetches
    # unaffected).
    assert assess.egress_confined_reason("anything.example") is None
    assert assess.egress_confined_reason("169.254.169.254") is None


def test_egress_confinement_blocks_out_of_scope_and_allows_in_scope():
    with assess.confined_egress(targets=["app.example"]):
        assert assess.egress_confined_reason("app.example") is None
        assert assess.egress_confined_reason("api.app.example") is None   # subdomain
        assert assess.egress_confined_reason("evil.example") is not None
        assert assess.egress_confined_reason("169.254.169.254") is not None
    # restored on exit
    assert assess.egress_confined_reason("evil.example") is None


def test_confined_egress_from_authorization():
    assess.grant(["app.example"], expires_in=3600)
    with assess.confined_egress():
        assert assess.egress_confined_reason("app.example") is None
        assert assess.egress_confined_reason("elsewhere.example") is not None


def test_gated_fetch_refuses_out_of_scope_when_confined(monkeypatch):
    from olympus import tools
    # Prove the confinement is enforced at the fetch layer, not just advisory.
    with assess.confined_egress(targets=["app.example"]):
        with pytest.raises(ValueError, match="egress confinement"):
            tools._http_probe("https://evil.example/x")


def test_run_assessment_confines_egress(workspace, monkeypatch):
    from olympus import tools
    assess.grant(["app.example", "local"], expires_in=3600)
    seen = {}

    def _probe(url, max_bytes=400_000, follow_redirects=True):
        # If confinement is active this is only reached for in-scope hosts.
        from urllib.parse import urlparse
        seen[urlparse(url).hostname] = True
        return {"status": 200, "headers": {"server": "x"}, "body": "", "url": url}
    monkeypatch.setattr(tools, "_http_probe", _probe)
    assess.run_assessment("app.example")
    assert "app.example" in seen and "evil.example" not in seen


def test_budget_stop_halts_phases(workspace, monkeypatch):
    assess.grant(["example.com", "local"], expires_in=3600)
    monkeypatch.setattr(tools, "_http_probe", _fake_probe({"server": "nginx"}))
    # Budget is measured as DELTA spend during the run: baseline low, then spend
    # jumps past the budget after the first phase → later phases are skipped.
    spends = iter([0.0] + [100.0] * 20)
    monkeypatch.setattr(assess, "_spent_usd", lambda: next(spends))
    r = assess.run_assessment("example.com", source_path=".", budget_usd=1.0)
    assert r["budget_stopped"] is True
    assert r["phases"] == ["recon"]                            # stops after first check
