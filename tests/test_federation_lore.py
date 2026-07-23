"""Federation of raw domain lore — signed, scrubbed, trusted-only, operator-gated.

The deepest fleet-compounding seam: a trusted peer's per-domain facts cross the
wire and STAGE in the local corpus's staging area, folded into the live corpus
only by the operator's explicit merge. Mirrors the lesson-sync guarantees.
"""

import json

import pytest

from olympus import config, domainlore, federation, store, witness


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    store.reset()
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    monkeypatch.delenv("OLYMPUS_WEB_LORE", raising=False)
    yield
    store.reset()


needs_crypto = pytest.mark.skipif(not witness.available(),
                                  reason="cryptography backend required")


@needs_crypto
def test_export_scrubs_and_signs():
    domainlore.observe("https://good.co/a", ok=True, bytes_=1000,
                       sitemap="https://good.co/s.xml",
                       site_name="Contact us at spy@secret.example")
    domainlore.observe("https://good.co/b", ok=True, bytes_=1000)
    env = federation.export_domainlore(min_visits=2)
    ok, payload = federation.verify_envelope(env)
    assert ok and payload["kind"] == "domainlore"
    # PII in a free-text field is stripped before it leaves the box
    assert "secret.example" not in json.dumps(payload)


@needs_crypto
def test_import_requires_trusted_peer():
    federation.add_peer("peer", federation.public_key(), trust="task")
    domainlore.observe("https://x.co/a", ok=True, bytes_=10, sitemap="https://x.co/s")
    domainlore.observe("https://x.co/b", ok=True, bytes_=10)
    env = federation.export_domainlore(min_visits=1)
    with pytest.raises(federation.FederationError):
        federation.import_domainlore(env)          # task trust is not enough


@needs_crypto
def test_import_stages_never_auto_applies():
    # Build a bundle as if from a peer (self-signed; pin our own key as trusted).
    federation.add_peer("peer", federation.public_key(), trust="trusted")
    domainlore.observe("https://shared.co/a", ok=True, bytes_=1000,
                       sitemap="https://shared.co/s.xml")
    domainlore.observe("https://shared.co/b", ok=True, bytes_=1000)
    env = federation.export_domainlore(min_visits=2)

    # Simulate a fresh receiver: drop the LIVE corpus so the shared domain isn't
    # already known, while keeping the pinned peer (its registry lives under the
    # store, not the domainlore file — so we must NOT relocate MEMORY_DIR here).
    (config.MEMORY_DIR / "domainlore.json").unlink()
    assert domainlore.known("https://shared.co/x") is None

    res = federation.import_domainlore(env)
    assert res["staged"] >= 1 and res["from"] == "peer"
    # staged, NOT live
    assert domainlore.known("https://shared.co/x") is None
    assert len(domainlore.staged_shared()) >= 1
    # operator merge folds it in
    assert domainlore.merge_staged() >= 1
    assert domainlore.known("https://shared.co/x").sitemap_url \
        == "https://shared.co/s.xml"


@needs_crypto
def test_import_bad_signature_rejected():
    federation.add_peer("peer", federation.public_key(), trust="trusted")
    domainlore.observe("https://y.co/a", ok=True, bytes_=10, sitemap="https://y.co/s")
    domainlore.observe("https://y.co/b", ok=True, bytes_=10)
    env = federation.export_domainlore(min_visits=1)
    env["payload"]["domains"].append({"domain": "injected.co"})   # break signature
    with pytest.raises(federation.FederationError):
        federation.import_domainlore(env)


def test_route_recognized_and_auth_gated():
    # Even without crypto, the new route must be recognized (401, not 404) so it
    # fails closed rather than masquerading as unknown.
    status, _ = federation.handle_request("POST", "/federation/domainlore", {}, {})
    assert status == 401


def test_scrub_row_structural_validation():
    assert federation._scrub_share_row({"domain": "ex.com/x"}) is None   # path
    assert federation._scrub_share_row({"domain": "no-dot"}) is None     # 1 label
    assert federation._scrub_share_row({"nope": 1}) is None              # no domain
    good = federation._scrub_share_row({"domain": "Ex.COM", "mobile_helped": 3})
    assert good["domain"] == "ex.com" and good["mobile_helped"] == 3


def test_scrub_keeps_valid_action_profile_across_the_wire():
    # A structural sanitizer preserves a legitimate profile (a whole-string PII
    # scrub would mangle the JSON and silently drop it).
    import json
    row = federation._scrub_share_row(
        {"domain": "shop.co",
         "action_profile": json.dumps([{"type": "click", "selector": "#more"}])})
    steps = json.loads(row["action_profile"])
    assert steps == [{"type": "click", "selector": "#more"}]
