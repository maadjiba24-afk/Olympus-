"""Phase 5 P5-8 — the external-client compatibility campaign, as a CI gate.

P5-A8 requires the real-client HTTP harness to EXIST. This file additionally
RUNS it, because in this environment it can be run: `anthropic` and `openai`
are both installed and loopback sockets work.

What is real here: the TCP socket, the HTTP framing, the SSE framing, the
vendor SDKs' own deserialization and retry logic, and `olympus.web.Handler`.
What is stubbed: the upstream provider. There is no credential for one, so no
real model is called, and nothing here says anything about answer quality,
latency or cost.

Claim ceiling, enforced by a test below: **real-SDK-over-HTTP verified in
staging**, never "production-client verified".
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

anthropic = pytest.importorskip("anthropic")
openai = pytest.importorskip("openai")

import client_compat_campaign as cc  # noqa: E402

from olympus import config, web  # noqa: E402


@pytest.fixture(scope="module")
def campaign(tmp_path_factory):
    """Run the whole campaign once; every test below reads its results."""
    import os
    prev = dict(os.environ)
    os.environ["OLYMPUS_API_KEYS"] = f"{cc.KEY_A},{cc.KEY_B}"
    os.environ.setdefault("OLYMPUS_MODEL", "claude-opus-4-8")
    real_dir = config.MEMORY_DIR
    config.MEMORY_DIR = tmp_path_factory.mktemp("compat") / "mem"
    srv, base, restore = cc.start_server()
    try:
        yield cc.run(base)
    finally:
        # `restore()` puts the real orchestrator.Olympus back. Without it the
        # stub leaks onto a module attribute for the rest of the session — the
        # first version of this fixture did exactly that and failed 100
        # unrelated tests downstream.
        restore()
        config.MEMORY_DIR = real_dir
        os.environ.clear()
        os.environ.update(prev)


def _by_dialect(campaign, dialect):
    return [r for r in campaign.results if r["dialect"] == dialect]


# ── the campaign itself ────────────────────────────────────────────────────

def test_the_campaign_executes_and_every_case_passes(campaign):
    failures = [r for r in campaign.results if r["status"] != "PASS"]
    assert not failures, json.dumps(failures, indent=1)[:4000]
    assert len(campaign.results) >= 20


def test_both_dialects_are_covered(campaign):
    assert len(_by_dialect(campaign, "anthropic")) >= 14
    assert len(_by_dialect(campaign, "openai")) >= 8


@pytest.mark.parametrize("area", [
    "authenticated request", "bad key is rejected", "streaming",
    "usage reporting", "8 concurrent requests", "client timeout",
])
def test_the_required_step8_areas_were_exercised_on_both_dialects(campaign,
                                                                  area):
    """Step 8 names the areas that must be covered. A campaign that quietly
    dropped one would still report 'all passed'."""
    for dialect in ("anthropic", "openai"):
        got = [r for r in _by_dialect(campaign, dialect) if r["case"] == area]
        assert got, f"{dialect} never exercised {area!r}"
        assert got[0]["status"] == "PASS", got[0]


@pytest.mark.parametrize("case", [
    "malformed: no max_tokens", "unknown field refused",
    "hostile scalar (Infinity)", "large payload 200 KiB",
    "oversize payload refused", "disconnect mid-stream", "audit headers",
    "principal isolation", "multi-turn", "system prompt",
])
def test_anthropic_specific_areas_were_exercised(campaign, case):
    got = [r for r in _by_dialect(campaign, "anthropic") if r["case"] == case]
    assert got and got[0]["status"] == "PASS", got


def test_every_result_carries_real_client_provenance(campaign):
    """P5-A6. These are the only records in Phase 5 entitled to
    `real-client-staging`, and they must say so."""
    from olympus import shadow
    for rec in campaign.results:
        assert rec["provenance"] == shadow.REAL_CLIENT_STAGING, rec


def test_principal_isolation_holds_over_the_wire(campaign):
    """P5-A14. Phase-4 B-F2 proved the DERIVATION offline; this proves two
    real SDK clients presenting two different keys land on two different
    memory principals through a real socket."""
    rec = [r for r in campaign.results
           if r["case"] == "principal isolation"][0]
    assert rec["status"] == "PASS", rec
    assert "distinct principals" in rec["detail"]


# ── the claim ceiling ──────────────────────────────────────────────────────

def test_the_harness_states_its_own_limits(campaign):
    """Rule 1: never claim something was tested that was not. The campaign must
    carry, in its own output, the fact that the provider was stubbed."""
    src = (_REPO / "scripts" / "client_compat_campaign.py").read_text(
        encoding="utf-8")
    assert "NOT production-client verified" in src
    assert "STUBBED" in src
    assert "no real model" in src.lower()


def test_no_report_upgrades_the_compatibility_claim():
    """A guard on the reports, not the code: no document may ASSERT that the
    surface is production-client verified, because it is not.

    The distinction between asserting and mentioning matters — the spec and
    every report state the ceiling by naming exactly what was NOT reached, and
    a naive substring ban would forbid the disclaimers along with the claims.
    So a hit is a violation only when the 120 characters before it carry no
    negation. That is a heuristic, and it is the conservative direction: an
    unhedged occurrence fails and has to be reworded."""
    import re

    forbidden = ("production-client verified", "verified with real clients",
                 "real-client verified in production",
                 "verified against real third-party clients")
    negations = ("not", "never", "n't", "cannot", "without", "no ", "unreached",
                 "short of", "rather than", "instead of", "does not reach")
    offences = []
    for doc in sorted((_REPO / "docs" / "absorption").glob("*.md")):
        text = doc.read_text(encoding="utf-8").lower()
        for phrase in forbidden:
            for m in re.finditer(re.escape(phrase), text):
                window = text[max(0, m.start() - 120):m.start()]
                if not any(n in window for n in negations):
                    offences.append(
                        f"{doc.name}: …{text[max(0, m.start()-80):m.end()+20]}…")
    assert not offences, (
        "a document asserts production-client verification, which the campaign "
        "does not support (the upstream provider is stubbed and nothing "
        "crossed a real network):\n" + "\n".join(offences))


def test_the_negation_guard_actually_catches_an_unhedged_claim(tmp_path):
    """The guard above passes trivially if its detection is broken. Prove it
    fires on a bare claim and stays quiet on a hedged one."""
    import re

    def offends(sentence: str) -> bool:
        text = sentence.lower()
        phrase = "production-client verified"
        for m in re.finditer(re.escape(phrase), text):
            window = text[max(0, m.start() - 120):m.start()]
            if not any(n in window for n in
                       ("not", "never", "n't", "cannot", "without", "no ",
                        "unreached", "short of", "rather than", "instead of",
                        "does not reach")):
                return True
        return False

    assert offends("The /v1 surface is production-client verified.")
    assert offends("Status: production-client verified as of today.")
    assert not offends("This does not reach production-client verified.")
    assert not offends("It is NOT production-client verified.")
    assert not offends("Never production-client verified in this phase.")


# ── the shadow marker, over a real socket ──────────────────────────────────

def test_shadow_mode_is_visible_to_a_real_client(monkeypatch,
                                                 tmp_path_factory):
    """PHASE5 §7: a shadow response must never be mistakable for a production
    one. Proven at the HTTP layer a real client actually sees."""
    monkeypatch.setenv("OLYMPUS_API_KEYS", cc.KEY_A)
    monkeypatch.setenv("OLYMPUS_MODEL", "claude-opus-4-8")
    monkeypatch.setattr(config, "MEMORY_DIR",
                        tmp_path_factory.mktemp("shadowhdr") / "mem")
    monkeypatch.setattr(web.orchestrator, "Olympus", cc.StubCouncil)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        def mode_header():
            req = urllib.request.Request(
                base + "/v1/messages",
                data=json.dumps({"model": "m", "max_tokens": 16,
                                 "messages": [{"role": "user",
                                               "content": "hi"}]}).encode(),
                headers={"content-type": "application/json",
                         "x-api-key": cc.KEY_A})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.headers.get("X-Olympus-Mode")

        monkeypatch.delenv("OLYMPUS_SHADOW_MODE", raising=False)
        assert mode_header() == "live"
        monkeypatch.setenv("OLYMPUS_SHADOW_MODE", "1")
        assert mode_header() == "shadow"
    finally:
        srv.shutdown()
        srv.server_close()
