"""Native federation — identity, handshake, envelopes, trust, sync, dispatch."""

import json

import pytest

from olympus import federation, store, witness


@pytest.fixture(autouse=True)
def _fresh_store():
    store.reset()
    yield
    store.reset()


def _has_crypto():
    return witness.available()


needs_crypto = pytest.mark.skipif(not _has_crypto(),
                                  reason="cryptography backend required")


# --- gating --------------------------------------------------------------

def test_disabled_by_default():
    assert federation.enabled() is False


def test_enabled_via_env(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FEDERATION", "1")
    assert federation.enabled() is True


# --- identity + envelopes ------------------------------------------------

@needs_crypto
def test_identity_matches_public_key():
    ident = federation.identity()
    assert ident["publicKey"] == federation.public_key()
    assert ident["protocol"] == federation.PROTOCOL


@needs_crypto
def test_envelope_roundtrip_and_tamper():
    env = federation.sign_envelope({"kind": "x", "n": 1})
    ok, payload = federation.verify_envelope(env)
    assert ok and payload["n"] == 1
    # tamper the payload → signature no longer matches
    env["payload"]["n"] = 2
    ok2, _ = federation.verify_envelope(env)
    assert ok2 is False


@needs_crypto
def test_envelope_pin_mismatch_fails():
    env = federation.sign_envelope({"kind": "x"})
    ok, _ = federation.verify_envelope(env, pin="deadbeef")
    assert ok is False
    ok2, _ = federation.verify_envelope(env, pin=federation.public_key())
    assert ok2 is True


# --- handshake -----------------------------------------------------------

@needs_crypto
def test_handshake_challenge_response():
    challenge = federation.make_challenge()
    response = federation.answer_challenge(challenge)
    assert federation.verify_challenge_response(
        response, expected_nonce=challenge["nonce"],
        pin=federation.public_key()) is True
    # wrong nonce is rejected
    assert federation.verify_challenge_response(
        response, expected_nonce="not-the-nonce") is False


@needs_crypto
def test_answer_challenge_requires_nonce():
    with pytest.raises(federation.FederationError):
        federation.answer_challenge({})


# --- peer registry + trust ----------------------------------------------

def test_peer_registry_crud():
    federation.add_peer("alice", "aa11", url="https://a.example", trust="trusted")
    p = federation.get_peer("alice")
    assert p and p.trust == "trusted" and p.url == "https://a.example"
    assert federation.peer_by_key("AA11").name == "alice"    # case-insensitive
    assert [x.name for x in federation.peers()] == ["alice"]
    assert federation.remove_peer("alice") is True
    assert federation.get_peer("alice") is None


def test_add_peer_validates():
    with pytest.raises(federation.FederationError):
        federation.add_peer("", "key")
    # unknown/typo'd trust fails CLOSED to "blocked", never silently "task" (F5)
    assert federation.add_peer("bob", "bb22", trust="wizard").trust == "blocked"


# --- shared-lesson sync (sanitised, trusted-only, candidate-only) --------

@needs_crypto
def test_export_lessons_sanitises():
    env = federation.export_lessons(["call me at test@example.com always"])
    _, payload = federation.verify_envelope(env)
    assert "example.com" not in json.dumps(payload)          # PII stripped


@needs_crypto
def test_import_lessons_requires_trusted_peer():
    # peer pinned only at "task" trust → lesson import refused
    federation.add_peer("peer", federation.public_key(), trust="task")
    env = federation.export_lessons(["a useful lesson"])
    with pytest.raises(federation.FederationError):
        federation.import_lessons(env)


@needs_crypto
def test_import_lessons_stages_candidates():
    federation.add_peer("peer", federation.public_key(), trust="trusted")
    env = federation.export_lessons(["always double-check dates"])
    result = federation.import_lessons(env)
    assert result["staged"] == 1
    staged = federation.staged_lessons()
    assert staged and staged[0]["text"] == "always double-check dates"
    assert staged[0]["from"] == "peer"          # provenance kept


@needs_crypto
def test_import_lessons_bad_signature_rejected():
    federation.add_peer("peer", federation.public_key(), trust="trusted")
    env = federation.export_lessons(["x"])
    env["payload"]["lessons"] = ["tampered"]     # break the signature
    with pytest.raises(federation.FederationError):
        federation.import_lessons(env)


# --- inbound dispatcher --------------------------------------------------

@needs_crypto
def test_identity_route_needs_no_auth():
    status, doc = federation.handle_request("GET", "/federation/identity", {}, {})
    assert status == 200 and doc["publicKey"] == federation.public_key()


@needs_crypto
def test_handshake_route():
    challenge = federation.make_challenge()
    status, resp = federation.handle_request(
        "POST", "/federation/handshake", challenge, {})
    assert status == 200
    assert federation.verify_challenge_response(
        resp, expected_nonce=challenge["nonce"], pin=federation.public_key())


def test_task_route_requires_auth():
    status, _ = federation.handle_request("POST", "/federation/task", {}, {})
    assert status == 401                         # fail-closed, no token


@needs_crypto
def test_task_route_runs_pinned_peer_task(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FEDERATION_TOKEN", "s3cret")
    federation.add_peer("peer", federation.public_key(), trust="task")
    env = federation.sign_envelope({"kind": "task", "text": "hello council"})
    seen = {}

    def ask(text):
        seen["text"] = text
        return "council answer"

    status, resp = federation.handle_request(
        "POST", "/federation/task", env,
        {"Authorization": "Bearer s3cret"}, ask=ask)
    assert status == 200
    # peer text reached the council WRAPPED as untrusted
    assert "untrusted" in seen["text"] and "hello council" in seen["text"]
    ok, payload = federation.verify_envelope(resp)
    assert ok and payload["answer"] == "council answer"


@needs_crypto
def test_task_route_rejects_unpinned_peer(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FEDERATION_TOKEN", "s3cret")
    env = federation.sign_envelope({"kind": "task", "text": "hi"})  # signer unpinned
    status, _ = federation.handle_request(
        "POST", "/federation/task", env,
        {"Authorization": "Bearer s3cret"}, ask=lambda t: "x")
    assert status == 403


def test_bad_token_rejected(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FEDERATION_TOKEN", "right")
    status, _ = federation.handle_request(
        "POST", "/federation/task", {}, {"Authorization": "Bearer wrong"})
    assert status == 401


def test_unknown_route():
    status, _ = federation.handle_request("GET", "/nope", {}, {})
    assert status == 404


# --- outbound (governed, injected fetcher) -------------------------------

def test_call_peer_disabled():
    federation.add_peer("peer", "kk", url="https://p.example", trust="task")
    with pytest.raises(federation.FederationError):
        federation.call_peer("peer", "hi")       # OLYMPUS_FEDERATION off


@needs_crypto
def test_call_peer_signs_and_verifies(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FEDERATION", "1")
    federation.add_peer("peer", federation.public_key(),
                        url="https://p.example", trust="task")
    captured = {}

    def fake_fetch(url, data, headers, timeout):
        captured["url"] = url
        env = json.loads(data)
        ok, payload = federation.verify_envelope(env)
        assert ok and payload["text"] == "remote question"
        reply = federation.sign_envelope({"kind": "task_result",
                                          "answer": "remote answer"})
        return 200, json.dumps(reply).encode("utf-8")

    out = federation.call_peer("peer", "remote question", fetcher=fake_fetch)
    assert captured["url"].endswith("/federation/task")
    assert "remote answer" in out and "untrusted" in out    # wrapped untrusted


@needs_crypto
def test_call_peer_blocks_localhost(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FEDERATION", "1")
    federation.add_peer("evil", federation.public_key(),
                        url="http://localhost:22", trust="task")
    with pytest.raises(federation.FederationError):
        federation.call_peer("evil", "x", fetcher=lambda *a: (200, b"{}"))


# --- hardening: lesson-replay dedup (F4) --------------------------------

@needs_crypto
def test_lesson_replay_is_deduped():
    federation.add_peer("peer", federation.public_key(), trust="trusted")
    env = federation.export_lessons(["double-check dates", "cite sources"])
    first = federation.import_lessons(env)
    assert first["staged"] == 2
    # Replaying the SAME signed bundle stages nothing new — cannot flood/evict.
    second = federation.import_lessons(env)
    assert second["staged"] == 0
    assert len(federation.staged_lessons()) == 2


# --- hardening: optional freshness window (F4) --------------------------

@needs_crypto
def test_stale_lesson_bundle_rejected(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FEDERATION_MAX_AGE", "60")
    federation.add_peer("peer", federation.public_key(), trust="trusted")
    # sign a bundle then hand-age its timestamp past the window
    env = federation.export_lessons(["old lesson"])
    env["payload"]["at"] = "2000-01-01T00:00:00Z"
    # re-sign the tampered payload so it's validly signed but stale
    env = federation.sign_envelope(env["payload"])
    with pytest.raises(federation.FederationError):
        federation.import_lessons(env)


def test_max_age_default_off():
    assert federation._max_age() == 0
    assert federation._too_old({"at": "2000-01-01T00:00:00Z"}) is False


# --- hardening: token-file error fails closed to 401 (F7) ---------------

def test_unreadable_token_file_denies(monkeypatch, tmp_path):
    missing = tmp_path / "nope.token"
    monkeypatch.setenv("OLYMPUS_FEDERATION_TOKEN_FILE", str(missing))
    status, _ = federation.handle_request(
        "POST", "/federation/task", {}, {"Authorization": "Bearer x"})
    assert status == 401                       # fail-closed, not a 500


# --- federation depth: capability discovery + multi-peer aggregation -----

@needs_crypto
def test_capabilities_card_is_signed_and_scrubbed():
    card = federation.capabilities_card()
    ok, payload = federation.verify_envelope(card)
    assert ok and payload["kind"] == "capabilities"
    assert isinstance(payload["specialists"], list) and payload["specialists"]
    assert all("key" in s and "title" in s for s in payload["specialists"])
    assert isinstance(payload["skills"], int)


@needs_crypto
def test_capabilities_route_requires_pinned_peer(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FEDERATION_TOKEN", "s3cret")
    # signed by an UNPINNED key → 403 even with the right bearer token
    env = federation.sign_envelope({"kind": "capabilities_request"})
    status, _ = federation.handle_request(
        "POST", "/federation/capabilities", env,
        {"Authorization": "Bearer s3cret"})
    assert status == 403


@needs_crypto
def test_capabilities_route_needs_auth():
    status, _ = federation.handle_request(
        "POST", "/federation/capabilities", {}, {})
    assert status == 401                         # fail-closed, no token


@needs_crypto
def test_capabilities_route_serves_pinned_peer(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FEDERATION_TOKEN", "s3cret")
    federation.add_peer("peer", federation.public_key(), trust="task")
    env = federation.sign_envelope({"kind": "capabilities_request"})
    status, resp = federation.handle_request(
        "POST", "/federation/capabilities", env,
        {"Authorization": "Bearer s3cret"})
    assert status == 200
    ok, payload = federation.verify_envelope(resp)
    assert ok and payload["kind"] == "capabilities"


@needs_crypto
def test_discover_peer_verifies_and_returns_card(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FEDERATION", "1")
    federation.add_peer("peer", federation.public_key(),
                        url="https://p.example", trust="task")

    def fake_fetch(url, data, headers, timeout):
        assert url.endswith("/federation/capabilities")
        env = json.loads(data)
        ok, payload = federation.verify_envelope(env)
        assert ok and payload["kind"] == "capabilities_request"
        return 200, json.dumps(federation.capabilities_card()).encode("utf-8")

    card = federation.discover_peer("peer", fetcher=fake_fetch)
    assert card["peer"] == "peer"
    assert isinstance(card["specialists"], list) and card["specialists"]
    assert isinstance(card["skills"], int)


@needs_crypto
def test_discover_peer_rejects_wrong_signer(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FEDERATION", "1")
    # Pin a peer to a DIFFERENT key than the reply is signed with → pin mismatch.
    federation.add_peer("peer", "0" * 64, url="https://p.example", trust="task")

    def fake_fetch(url, data, headers, timeout):
        return 200, json.dumps(federation.capabilities_card()).encode("utf-8")

    with pytest.raises(federation.FederationError):
        federation.discover_peer("peer", fetcher=fake_fetch)


def test_discover_peer_disabled():
    federation.add_peer("peer", "kk", url="https://p.example", trust="task")
    with pytest.raises(federation.FederationError):
        federation.discover_peer("peer")            # OLYMPUS_FEDERATION off


@needs_crypto
def test_call_peers_aggregates_and_isolates_failures(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FEDERATION", "1")
    federation.add_peer("good", federation.public_key(),
                        url="https://good.example", trust="task")
    federation.add_peer("bad", federation.public_key(),
                        url="https://bad.example", trust="task")

    def fake_fetch(url, data, headers, timeout):
        if "bad.example" in url:
            raise federation.FederationError("boom")
        reply = federation.sign_envelope({"kind": "task_result",
                                          "answer": "ok from good"})
        return 200, json.dumps(reply).encode("utf-8")

    out = federation.call_peers(["good", "bad", "good"], "q", fetcher=fake_fetch)
    # deterministic order, duplicate 'good' collapsed, one failure isolated
    assert [r["peer"] for r in out] == ["good", "bad"]
    assert "ok from good" in out[0]["answer"] and "untrusted" in out[0]["answer"]
    assert "error" in out[1]


def test_call_peers_disabled():
    with pytest.raises(federation.FederationError):
        federation.call_peers(["p"], "q")           # OLYMPUS_FEDERATION off


@needs_crypto
def test_trusted_peer_names_filters_and_sorts():
    federation.add_peer("zeta", "k1", url="https://z.example", trust="task")
    federation.add_peer("alpha", "k2", url="https://a.example", trust="trusted")
    federation.add_peer("blocked", "k3", url="https://b.example", trust="blocked")
    federation.add_peer("nourl", "k4", trust="task")     # no url → excluded
    assert federation.trusted_peer_names() == ["alpha", "zeta"]
