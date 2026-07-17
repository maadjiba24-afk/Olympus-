"""Parked-item 4 — payment rail, SANDBOX MODE ONLY (moves no real money).

The executor that sits behind a mandate. Every safety rail is proven here:
the mandate gate refuses a defective authorization, hard caps fail closed,
charges are idempotent by nonce (never double-charge), every attempt is a
signed ledger record — and the LIVE (real-money) path fails closed and does not
exist in this build.
"""

import pytest

from olympus import config, deltas, identity, mandate, payrail, witness

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "parked4-seed")
    monkeypatch.delenv("OLYMPUS_PAYMENT_LIVE", raising=False)
    monkeypatch.delenv("OLYMPUS_MANDATE_USER_PUBKEY", raising=False)
    monkeypatch.setattr(deltas, "_locks", {})
    mandate.reset_user_signer()
    yield
    mandate.reset_user_signer()


def _intent(cap=15000, user="alice"):
    return mandate.create_intent(user, amount_cap=cap, currency="USD",
                                 merchants=["acme"], item="widgets",
                                 trusted=True, now=1000.0)


def _grant(jti_ok=True):
    return identity.grant("alice", scopes=[mandate.PAYMENT_SCOPE],
                          risk_ceiling=mandate.FINANCIAL_LEGAL, ttl=3600.0)


def _mandate(im, *, amount=10000, jti=""):
    cart = mandate.create_cart(im, amount=amount, currency="USD",
                               merchant="acme", items=["widget"],
                               capability_jti=jti, now=1000.0)
    return mandate.co_sign(mandate.sign(cart))


# --- happy path: a fully-verified mandate charges in sandbox ----------------

@_signing
def test_verified_mandate_charges_in_sandbox():
    im = _intent()
    tok = _grant()
    m = _mandate(im, jti=tok["payload"]["jti"])
    res = payrail.charge(m, im, capability=tok, user="alice", now=1000.0)
    assert res.ok and res.status == "charged" and res.mode == "sandbox"
    assert res.charge_id.startswith("sbx_") and res.amount == 10000
    # A signed ledger record exists and verifies.
    assert res.ledger_hash
    assert deltas.verify_history("payment:alice")["ok"]


# --- the mandate gate refuses a defective authorization ---------------------

@_signing
def test_missing_cosignature_is_refused_no_charge():
    im = _intent()
    system_only = mandate.sign(mandate.create_cart(
        im, amount=10000, currency="USD", merchant="acme", items=["w"],
        now=1000.0))
    res = payrail.charge(system_only, im, user="alice", now=1000.0)
    assert not res.ok and res.status == "refused" and "mandate refused" in res.reason
    # Nothing recorded as CHARGED (only a signed 'refused' audit event).
    assert not any(s["state"]["event"] == "charged"
                   for s in payrail.attempts("alice"))


@_signing
def test_capability_out_of_bound_is_refused():
    im = _intent()
    tok = identity.grant("alice", scopes=["browser.click"],   # not payment scope
                         risk_ceiling=mandate.FINANCIAL_LEGAL, ttl=3600.0)
    m = _mandate(im, jti=tok["payload"]["jti"])
    res = payrail.charge(m, im, capability=tok, user="alice", now=1000.0)
    assert not res.ok and res.status == "refused"


# --- hard caps fail closed --------------------------------------------------

@_signing
def test_over_per_transaction_cap_is_blocked(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PAYMENT_MAX_TX", "5000")
    im = _intent(cap=100000)
    m = _mandate(im, amount=10000)
    res = payrail.charge(m, im, user="alice", now=1000.0)
    assert not res.ok and res.status == "blocked" and "per-transaction" in res.reason


@_signing
def test_daily_cap_is_enforced(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PAYMENT_MAX_DAILY", "12000")
    im = _intent(cap=100000, user="bob")
    r1 = payrail.charge(_mandate(im, amount=8000), im, user="bob", now=1000.0)
    assert r1.ok
    r2 = payrail.charge(_mandate(im, amount=8000), im, user="bob", now=1000.0)
    assert not r2.ok and r2.status == "blocked" and "daily" in r2.reason


@_signing
def test_env_can_only_lower_the_hard_ceiling(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PAYMENT_MAX_TX", "999999999")   # above the ceiling
    assert payrail.max_tx() == payrail._MAX_TX_CEIL             # clamped down


# --- idempotency: never double-charge ---------------------------------------

@_signing
def test_retry_same_mandate_is_idempotent():
    im = _intent(user="carol")
    m = _mandate(im, amount=10000)
    r1 = payrail.charge(m, im, user="carol", now=1000.0)
    r2 = payrail.charge(m, im, user="carol", now=1000.0)      # retry, same nonce
    assert r1.ok and r1.status == "charged"
    assert r2.status == "duplicate" and r2.charge_id == r1.charge_id
    # The daily tally (derived from the SIGNED ledger) advanced only ONCE.
    recs = payrail.attempts("carol")
    assert payrail._daily_spent(recs, payrail._day(1000.0)) == 10000
    assert sum(1 for s in recs if s["state"]["event"] == "charged") == 1


# --- the LIVE path fails closed and does not exist --------------------------

class _LiveProcessor(payrail.Processor):
    name = "pretend-live"
    live = True
    def charge(self, **kw):                       # pragma: no cover - never reached
        return {"ok": True, "charge_id": "REAL_MONEY"}


@_signing
def test_live_processor_refused_when_flag_unset():
    im = _intent()
    m = _mandate(im)
    with pytest.raises(payrail.PaymentLiveError):
        payrail.charge(m, im, processor=_LiveProcessor(), user="alice",
                       now=1000.0)
    # It never reached the processor: no "REAL_MONEY" charge recorded.
    assert not any(s["state"].get("charge_id") == "REAL_MONEY"
                   for s in payrail.attempts("alice"))


@_signing
def test_live_still_unimplemented_even_with_flag(monkeypatch):
    # Even if an operator sets the flag, THIS build has no live processor path —
    # it raises rather than moving money.
    monkeypatch.setenv("OLYMPUS_PAYMENT_LIVE", "1")
    im = _intent()
    with pytest.raises(payrail.PaymentLiveError):
        payrail.charge(_mandate(im), im, processor=_LiveProcessor(),
                       user="alice", now=1000.0)


def test_default_processor_is_sandbox_never_live():
    assert payrail.SandboxProcessor().live is False
    assert payrail.Processor.live is False
    # No live processor class ships in the module.
    assert not any(getattr(getattr(payrail, n), "live", False) is True
                   for n in dir(payrail)
                   if isinstance(getattr(payrail, n, None), type)
                   and issubclass(getattr(payrail, n), payrail.Processor))


def test_source_has_no_real_money_network_call():
    import inspect
    src = inspect.getsource(payrail)
    # The module makes no outbound network call (sandbox is a pure fake).
    for forbidden in ("urllib", "requests", "http.client", "socket.",
                      "stripe", "paypal"):
        assert forbidden not in src, f"payrail must not reference {forbidden!r}"


# --- idempotency/tally source of truth is the SIGNED ledger -----------------

@_signing
def test_duplicate_attempt_is_recorded():
    im = _intent(user="erin")
    m = _mandate(im, amount=10000)
    payrail.charge(m, im, user="erin", now=1000.0)
    payrail.charge(m, im, user="erin", now=1000.0)          # duplicate
    events = [s["state"]["event"] for s in payrail.attempts("erin")]
    assert events.count("charged") == 1 and "duplicate" in events


@_signing
def test_tampered_payment_ledger_fails_closed(monkeypatch):
    im = _intent(user="frank")
    payrail.charge(_mandate(im, amount=10000), im, user="frank", now=1000.0)
    # Forge the ledger: bump a recorded amount (as an attacker with FS access).
    import json
    path = deltas._path("payment:frank")
    recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    recs[-1]["state"]["amount"] = 1
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    assert not deltas.verify_history("payment:frank")["ok"]
    # A charge now REFUSES rather than trusting an unverifiable history.
    with pytest.raises(payrail.PaymentError):
        payrail.charge(_mandate(im, amount=5000), im, user="frank", now=1000.0)


# --- every attempt is on the signed ledger ----------------------------------

@_signing
def test_blocked_and_refused_attempts_are_recorded(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PAYMENT_MAX_TX", "1")
    im = _intent(cap=100000, user="dave")
    payrail.charge(_mandate(im, amount=10000), im, user="dave", now=1000.0)  # blocked
    recs = payrail.attempts("dave")
    assert recs and recs[-1]["state"]["event"] == "blocked"
    assert deltas.verify_history("payment:dave")["ok"]
