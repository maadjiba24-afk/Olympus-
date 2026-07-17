"""L1 — the LIVE payment path: fully built, shipped INERT (ADR 0003).

The complete live-cutover flow exists and is exercised here end-to-end against
a FAKE adapter — while the shipped state (no flag, no adapter) is pinned
incapable of moving money. Live mode forces co-signature, requires an
out-of-band pinned user key, applies lower caps, audits attempt-first
fail-closed, and accepts only the operator-registered adapter by identity.
"""

import pytest

from olympus import (behavioral_contracts as abc, config, deltas, identity,
                     mandate, payrail, paylive, witness)

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "live-l1-seed")
    monkeypatch.delenv("OLYMPUS_PAYMENT_LIVE", raising=False)
    monkeypatch.delenv("OLYMPUS_MANDATE_USER_PUBKEY", raising=False)
    monkeypatch.delenv("OLYMPUS_PAYMENT_LIVE_MAX_TX", raising=False)
    monkeypatch.delenv("OLYMPUS_PAYMENT_LIVE_MAX_DAILY", raising=False)
    monkeypatch.setattr(deltas, "_locks", {})
    mandate.reset_user_signer()
    paylive.reset_live_adapter()
    yield
    paylive.reset_live_adapter()
    mandate.reset_user_signer()


class _FakeLiveAdapter(paylive.LiveAdapter):
    """A FAKE live adapter for tests — deterministic, no network."""
    name = "fake-live"

    def __init__(self):
        self.calls = []
        self.records = {}

    def charge(self, *, amount, currency, merchant, idempotency_key):
        self.calls.append((amount, currency, merchant, idempotency_key))
        cid = f"live_{len(self.calls)}"
        self.records[cid] = {"amount": amount, "currency": currency}
        return {"ok": True, "charge_id": cid}

    def lookup(self, charge_id):
        return self.records[charge_id]


def _intent(cap=15000):
    return mandate.create_intent("alice", amount_cap=cap, currency="USD",
                                 merchants=["acme"], item="widgets",
                                 trusted=True, now=1000.0)


def _mandate(im, *, amount=5000, cosign=True):
    cart = mandate.create_cart(im, amount=amount, currency="USD",
                               merchant="acme", items=["widget"], now=1000.0)
    signed = mandate.sign(cart)
    return mandate.co_sign(signed) if cosign else signed


def _pin_user_key(monkeypatch, m):
    """Pin the co-signing key the test vault actually used — the out-of-band
    custody act, simulated."""
    monkeypatch.setenv("OLYMPUS_MANDATE_USER_PUBKEY", m.user_public_key)


def _cutover(monkeypatch, m=None):
    """Simulate the full HUMAN cutover: flag + registered adapter (+ pin when
    a mandate is given). Returns the registered adapter."""
    monkeypatch.setenv("OLYMPUS_PAYMENT_LIVE", "1")
    adapter = _FakeLiveAdapter()
    paylive.register_live_adapter(adapter)
    if m is not None:
        _pin_user_key(monkeypatch, m)
    return adapter


# --- the SHIPPED state is inert ----------------------------------------------

@_signing
def test_shipped_state_flag_unset_refuses():
    im = _intent()
    with pytest.raises(payrail.PaymentLiveError):
        payrail.charge(_mandate(im), im, processor=_FakeLiveAdapter(),
                       user="alice", now=1000.0)


@_signing
def test_flag_alone_is_not_enough_no_adapter(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PAYMENT_LIVE", "1")
    im = _intent()
    with pytest.raises(payrail.PaymentLiveError):
        payrail.charge(_mandate(im), im, processor=_FakeLiveAdapter(),
                       user="alice", now=1000.0)


@_signing
def test_adapter_alone_is_not_enough_flag_unset():
    adapter = _FakeLiveAdapter()
    paylive.register_live_adapter(adapter)
    im = _intent()
    with pytest.raises(payrail.PaymentLiveError):
        payrail.charge(_mandate(im), im, processor=adapter,
                       user="alice", now=1000.0)
    assert adapter.calls == []                    # money never moved


def test_default_adapter_is_disabled_and_refuses():
    assert not paylive.adapter_ready()
    with pytest.raises(payrail.PaymentLiveError):
        paylive.DisabledLiveAdapter().charge(amount=1, currency="USD",
                                             merchant="x", idempotency_key="k")


def test_register_rejects_non_live_adapter():
    with pytest.raises(payrail.PaymentError):
        paylive.register_live_adapter(payrail.SandboxProcessor())


# --- smuggling: only the REGISTERED adapter executes -------------------------

@_signing
def test_unregistered_live_processor_is_refused_by_identity(monkeypatch):
    im = _intent()
    m = _mandate(im)
    _cutover(monkeypatch, m)                      # full human cutover done…
    smuggled = _FakeLiveAdapter()                 # …but a DIFFERENT instance
    with pytest.raises(payrail.PaymentLiveError):
        payrail.charge(m, im, processor=smuggled, user="alice", now=1000.0)
    assert smuggled.calls == []


# --- the full (simulated) cutover: the live path actually works --------------

@_signing
def test_full_cutover_charges_through_registered_adapter(monkeypatch):
    im = _intent()
    m = _mandate(im, amount=5000)
    adapter = _cutover(monkeypatch, m)
    res = payrail.charge(m, im, processor=adapter, user="alice", now=1000.0)
    assert res.ok and res.status == "charged" and res.mode == "live"
    assert res.charge_id == "live_1" and adapter.calls[0][0] == 5000
    # The signed ledger records the live charge and verifies.
    assert res.ledger_hash and deltas.verify_history("payment:alice")["ok"]
    last = payrail.attempts("alice")[-1]["state"]
    assert last["event"] == "charged" and last["live"] is True
    assert last["adapter"] == "fake-live"


@_signing
def test_live_attempt_is_recorded_before_anything(monkeypatch):
    im = _intent()
    m = _mandate(im)
    _cutover(monkeypatch, m)
    smuggled = _FakeLiveAdapter()
    with pytest.raises(payrail.PaymentLiveError):
        payrail.charge(m, im, processor=smuggled, user="aud", now=1000.0)
    events = [s["state"]["event"] for s in payrail.attempts("aud")]
    assert "live-attempt" in events and "refused" in events


# --- live invariants ---------------------------------------------------------

@_signing
def test_missing_user_key_pin_refuses_live(monkeypatch):
    im = _intent()
    m = _mandate(im)
    adapter = _cutover(monkeypatch)               # flag + adapter, NO pin
    res = payrail.charge(m, im, processor=adapter, user="alice", now=1000.0)
    assert not res.ok and res.status == "refused"
    assert "pinned user co-signing key" in res.reason
    assert adapter.calls == []


@_signing
def test_cosignature_waiver_is_ignored_on_the_live_path(monkeypatch):
    im = _intent()
    cosigned = _mandate(im)                       # only to pin the user key
    system_only = _mandate(im, cosign=False)
    adapter = _cutover(monkeypatch, cosigned)
    # The caller tries the sandbox convenience — live FORCES co-signature.
    res = payrail.charge(system_only, im, processor=adapter, user="alice",
                         require_cosignature=False, now=1000.0)
    assert not res.ok and res.status == "refused"
    assert "mandate refused" in res.reason
    assert adapter.calls == []


@_signing
def test_live_caps_are_lower_and_fail_closed(monkeypatch):
    im = _intent(cap=100000)
    m = _mandate(im, amount=12000)                # over the $100 live ceiling…
    adapter = _cutover(monkeypatch, m)
    res = payrail.charge(m, im, processor=adapter, user="alice", now=1000.0)
    # …though well under the sandbox ceiling: blocked on the LIVE cap.
    assert not res.ok and res.status == "blocked" and "live" in res.reason
    assert adapter.calls == []


@_signing
def test_live_daily_cap_tallies_only_live_charges(monkeypatch):
    im = _intent(cap=100000)
    m1 = _mandate(im, amount=9000)
    adapter = _cutover(monkeypatch, m1)
    assert payrail.charge(m1, im, processor=adapter, user="bob", now=1000.0).ok
    # 9000 + 9000 = 18000 < live ceiling? No: 25000 daily; second passes...
    m2 = _mandate(im, amount=9000)
    assert payrail.charge(m2, im, processor=adapter, user="bob", now=1000.0).ok
    m3 = _mandate(im, amount=9000)                # 27000 > 25000 → blocked
    res = payrail.charge(m3, im, processor=adapter, user="bob", now=1000.0)
    assert not res.ok and res.status == "blocked" and "daily" in res.reason


@_signing
def test_env_can_only_lower_live_ceilings(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PAYMENT_LIVE_MAX_TX", "999999999")
    assert paylive.live_max_tx() == paylive._LIVE_MAX_TX_CEIL


@_signing
def test_live_charges_are_idempotent(monkeypatch):
    im = _intent()
    m = _mandate(im, amount=5000)
    adapter = _cutover(monkeypatch, m)
    r1 = payrail.charge(m, im, processor=adapter, user="carol", now=1000.0)
    r2 = payrail.charge(m, im, processor=adapter, user="carol", now=1000.0)
    assert r1.ok and r1.status == "charged"
    assert r2.status == "duplicate" and r2.charge_id == r1.charge_id
    assert len(adapter.calls) == 1                # never double-charged


@_signing
def test_adapter_error_is_refused_and_recorded(monkeypatch):
    im = _intent()
    m = _mandate(im, amount=5000)
    adapter = _cutover(monkeypatch, m)
    adapter.charge = lambda **kw: (_ for _ in ()).throw(RuntimeError("wire down"))
    res = payrail.charge(m, im, processor=adapter, user="dave", now=1000.0)
    assert not res.ok and "adapter error" in res.reason
    events = [s["state"]["event"] for s in payrail.attempts("dave")]
    assert "processor-error" in events and "charged" not in events


# --- the payment.live ABC contract ------------------------------------------

def test_contract_blocks_on_each_missing_gate():
    ok_ctx = {"payment_live_enabled": True, "payment_live_adapter_ready": True,
              "payment_user_key_pinned": True, "payment_live_within_caps": True,
              "payment_live_cosign_enforced": True}
    assert abc.check("payment.live", ok_ctx).ok
    for miss in ok_ctx:
        v = abc.check("payment.live", {**ok_ctx, miss: False})
        assert v.blocking and v.recovery == abc.BLOCK, miss


# --- cutover status + reconciliation ----------------------------------------

def test_cutover_status_lists_missing_human_acts():
    st = paylive.cutover_status()
    assert st["ready"] is False and len(st["missing"]) == 3
    assert st["adapter"]["name"] == "disabled"
    assert st["caps"]["live_max_tx"] <= st["caps"]["sandbox_max_tx"]


@_signing
def test_cutover_status_ready_after_all_acts(monkeypatch):
    im = _intent()
    m = _mandate(im)
    _cutover(monkeypatch, m)
    st = paylive.cutover_status()
    assert st["ready"] is True and st["missing"] == []


def test_reconcile_unavailable_when_inert():
    rep = paylive.reconcile("alice")
    assert rep["available"] is False and rep["checked"] == 0


@_signing
def test_reconcile_matches_ledger_to_adapter(monkeypatch):
    im = _intent()
    m = _mandate(im, amount=5000)
    adapter = _cutover(monkeypatch, m)
    payrail.charge(m, im, processor=adapter, user="erin", now=1000.0)
    rep = paylive.reconcile("erin")
    assert rep["available"] and rep["checked"] == 1 and rep["matched"] == 1
    assert rep["mismatched"] == []


# --- the module itself can never flip the switch or reach a network ----------

def test_paylive_never_writes_env_and_has_no_network():
    import inspect
    src = inspect.getsource(paylive)
    assert "os.environ[" not in src and "setenv" not in src \
        and "putenv" not in src
    for forbidden in ("urllib", "requests", "http.client", "socket",
                      "stripe", "paypal"):
        assert forbidden not in src, f"paylive must not reference {forbidden!r}"


def test_no_real_adapter_ships_in_the_module():
    # Every LiveAdapter subclass in the module is the base or the disabled one.
    subs = [obj for name in dir(paylive)
            if isinstance((obj := getattr(paylive, name)), type)
            and issubclass(obj, paylive.LiveAdapter)]
    assert set(subs) == {paylive.LiveAdapter, paylive.DisabledLiveAdapter}


@_signing
def test_direct_execute_live_cannot_double_charge(monkeypatch):
    """execute_live trusts nothing from its caller: even a DIRECT call that
    bypasses payrail.charge re-derives the signed ledger and dedupes the
    nonce — a replayed mandate returns the prior charge, never a second one."""
    im = _intent()
    m = _mandate(im, amount=5000)
    adapter = _cutover(monkeypatch, m)
    r1 = payrail.charge(m, im, processor=adapter, user="gina", now=1000.0)
    assert r1.ok and r1.status == "charged"
    r2 = paylive.execute_live(m, im, processor=adapter, user="gina",
                              nonce=str(m.payload["nonce"]), now=1000.0)
    assert r2.status == "duplicate" and r2.charge_id == r1.charge_id
    assert len(adapter.calls) == 1                # money moved exactly once


@_signing
def test_reconcile_refuses_a_tampered_ledger(monkeypatch):
    import json
    im = _intent()
    m = _mandate(im, amount=5000)
    adapter = _cutover(monkeypatch, m)
    payrail.charge(m, im, processor=adapter, user="hank", now=1000.0)
    path = deltas._path("payment:hank")
    recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    recs[-1]["state"]["amount"] = 1
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    rep = paylive.reconcile("hank")
    assert rep["available"] is False and "verification" in rep["reason"]


@_signing
def test_sandbox_path_is_unchanged(monkeypatch):
    # The delegation must not disturb the sandbox rail.
    im = _intent()
    m = _mandate(im, amount=5000)
    res = payrail.charge(m, im, user="fred", now=1000.0)
    assert res.ok and res.mode == "sandbox" and res.charge_id.startswith("sbx_")
