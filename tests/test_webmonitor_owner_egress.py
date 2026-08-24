"""P1: a web-monitor change alert must egress as its monitor's durable owner.

`Monitor.user` is bound correctly at creation (`webmonitor.add` takes it from
`memory.current_user()`), but `run_due` dropped it: the phase-one due snapshot
omitted the field, and the alert went out as a bare `notify(message)`. The
default notifier is `gateway.notify_all`, whose `user` defaults to "shared", so:

  * a monitor owned by A broadcast its URL and changed page content through the
    installation's globally-configured channels, reaching other tenants and
    every member of the configured group; and
  * `egress.guard(..., user="shared")` checked the SHARED vault for stored
    secrets instead of A's, so a watched page echoing A's own secret sailed
    past the exfiltration check that exists to catch exactly that.

This is the same class PR #280 closed for `agentbeat` and PR #281 for
`scheduler._deliver`; the sweep did not reach this module.
"""

import json

import pytest

from olympus import config, egress, security, webmonitor

A = "tg-alice"
B = "tg-bob"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR", "1")
    monkeypatch.delenv("OLYMPUS_REPLAY", raising=False)
    return tmp_path


class _Spy:
    """An owner-aware notifier. `user` is keyword-only and REQUIRED — there is
    deliberately no default and no TypeError-retry fallback, so a production
    path that forgets the owner fails loudly here instead of silently
    broadcasting as the shared principal again."""

    def __init__(self):
        self.calls = []

    def __call__(self, text, *, user):
        self.calls.append({"text": text, "user": user})
        return ["telegram"]

    @property
    def users(self):
        return [c["user"] for c in self.calls]


def _diff(monkeypatch, current_hash, markdown="v", diff="- a\n+ b"):
    from olympus import webctx
    monkeypatch.setattr(webctx, "diff",
                        lambda url, prev=None, **kw: {
                            "changed": True, "current_hash": current_hash,
                            "current_markdown": markdown, "diff": diff})


def _baseline_then_change(monkeypatch, spy, *, now_a=1000, now_b=2000):
    """Establish a baseline (silent), then a real change (alerts)."""
    _diff(monkeypatch, "h1")
    webmonitor.run_due(now=now_a, notify=spy)
    assert spy.calls == [], "baseline capture must be silent"
    _diff(monkeypatch, "h2")
    return webmonitor.run_due(now=now_b, notify=spy)


# --- 1-4: the owner reaches the notifier, exactly ---------------------------

def test_change_alert_carries_the_monitors_durable_owner(store, monkeypatch):
    """THE red test. Against the unfixed implementation the notifier is called
    positionally with no `user`, so this raises TypeError on a keyword-only
    required parameter — the failure mode that proves the owner was dropped."""
    webmonitor.add(A, "https://private.example/inbox", interval=1)
    spy = _Spy()

    log = _baseline_then_change(monkeypatch, spy)

    assert any("CHANGED" in ln for ln in log)
    assert len(spy.calls) == 1
    assert spy.calls[0]["user"] == A, "alert did not egress as its owner"
    assert "private.example/inbox" in spy.calls[0]["text"]


def test_two_owners_changing_in_one_tick_each_carry_their_own_owner(
        store, monkeypatch):
    """One heartbeat tick, two monitors, two owners. Each alert must be bound
    to its own monitor — not to whichever happened to be processed first."""
    webmonitor.add(A, "https://a.example/x", interval=1)
    webmonitor.add(B, "https://b.example/y", interval=1)
    spy = _Spy()

    _baseline_then_change(monkeypatch, spy)

    assert len(spy.calls) == 2
    assert sorted(spy.users) == sorted([A, B])
    # And each owner's alert names that owner's own URL, not the other's.
    by_user = {c["user"]: c["text"] for c in spy.calls}
    assert "a.example/x" in by_user[A] and "b.example" not in by_user[A]
    assert "b.example/y" in by_user[B] and "a.example" not in by_user[B]


def test_similar_urls_across_owners_do_not_cross_contaminate(
        store, monkeypatch):
    """Two tenants watching the SAME url. The alerts must stay separated by
    owner — a shared URL is not a shared monitor."""
    url = "https://status.example/service"
    webmonitor.add(A, url, interval=1)
    webmonitor.add(B, url, interval=1)
    spy = _Spy()

    _baseline_then_change(monkeypatch, spy)

    assert len(spy.calls) == 2
    assert sorted(spy.users) == sorted([A, B])


@pytest.mark.parametrize("owner", ["tg-a.b", "tg-a@b", "tg-a b",
                                   "tg-" + "x" * 70 + "AAA", "u:acct-1"])
def test_owner_is_passed_through_exactly(store, monkeypatch, owner):
    """The owner must arrive VERBATIM. `memory.safe_id` collapses punctuation
    ("tg-a.b", "tg-a@b", "tg-a b" all become "tg-a-b") and truncates at 64
    characters, so normalizing here would hand the egress guard a different
    principal's identity — and check the wrong vault for secrets."""
    from olympus import memory
    webmonitor.add(owner, "https://ex.example/x", interval=1)
    spy = _Spy()

    _baseline_then_change(monkeypatch, spy)

    assert len(spy.calls) == 1
    got = spy.calls[0]["user"]
    assert got == owner, f"owner was normalized: {got!r} != {owner!r}"
    if memory.safe_id(owner) != owner:                 # the lossy cases
        assert got != memory.safe_id(owner), "owner was passed through safe_id"


# --- 5-7: silence is preserved ---------------------------------------------

def test_baseline_capture_produces_no_notification(store, monkeypatch):
    webmonitor.add(A, "https://ex.example/x", interval=1)
    spy = _Spy()
    _diff(monkeypatch, "h1")

    log = webmonitor.run_due(now=1000, notify=spy)

    assert any("baseline captured" in ln for ln in log)
    assert spy.calls == []


def test_unchanged_page_produces_no_notification(store, monkeypatch):
    webmonitor.add(A, "https://ex.example/x", interval=1)
    spy = _Spy()
    _diff(monkeypatch, "h1")
    webmonitor.run_due(now=1000, notify=spy)           # baseline
    webmonitor.run_due(now=2000, notify=spy)           # same hash

    assert spy.calls == []


def test_disabled_monitoring_is_a_no_op(store, monkeypatch):
    webmonitor.add(A, "https://ex.example/x", interval=1)
    monkeypatch.delenv("OLYMPUS_WEB_MONITOR", raising=False)
    spy = _Spy()

    assert webmonitor.run_due(now=1000, notify=spy) == []
    assert spy.calls == []


def test_replay_forces_the_monitor_inert(store, monkeypatch):
    webmonitor.add(A, "https://ex.example/x", interval=1)
    monkeypatch.setenv("OLYMPUS_REPLAY", "1")
    spy = _Spy()

    assert webmonitor.run_due(now=1000, notify=spy) == []
    assert spy.calls == []


# --- 8: the injected-callback contract -------------------------------------

def test_notifier_contract_requires_the_owner_keyword(store, monkeypatch):
    """An owner-less callback must FAIL, not be silently accommodated.

    A TypeError-retry fallback (`except TypeError: notify(text)`) would let any
    legacy notifier keep receiving un-owned alerts — reopening the exact defect
    this patch closes. The failure is surfaced in the log line instead.
    """
    webmonitor.add(A, "https://ex.example/x", interval=1)
    legacy_calls = []

    def legacy_notifier(text):                 # the OLD, owner-less contract
        legacy_calls.append(text)

    _diff(monkeypatch, "h1")
    webmonitor.run_due(now=1000, notify=legacy_notifier)      # baseline: silent
    _diff(monkeypatch, "h2")
    log = webmonitor.run_due(now=2000, notify=legacy_notifier)

    assert legacy_calls == [], "an owner-less notifier still received an alert"
    assert any("NOT delivered" in ln for ln in log), log


# --- the installation-wide fan-out: fail closed by default -----------------
#
# `gateway.notify_all(text, user=A)` uses A ONLY to pick the vault the egress
# guard checks. It then calls every configured transport as `notify(text)` —
# no transport accepts an owner, and every destination is a single global
# operator-set value (TELEGRAM_NOTIFY_CHAT_ID, DISCORD_WEBHOOK_URL, ...). So
# `user=` never selects a recipient, and with the guard off (the default) it is
# not read at all. Owner-derived monitor content therefore must not take that
# path by default: it would disclose one tenant's private watched page to every
# other tenant and every member of those shared channels.

def _stub_channels(monkeypatch):
    """Two installation-wide destinations that ACCEPT, the rest declining.
    Returns the accept log for each. These are shared channels — not A's."""
    from olympus import gateway
    got = {"telegram": [], "discord": []}
    monkeypatch.setattr("olympus.telegram.notify",
                        lambda t, *a, **k: (got["telegram"].append(t), True)[1])
    monkeypatch.setattr("olympus.discord.notify",
                        lambda t, *a, **k: (got["discord"].append(t), True)[1])
    for name in gateway.NOTIFY_CHANNELS:
        if name in ("telegram", "discord"):
            continue
        mod = "signal" if name == "signal" else name
        monkeypatch.setattr(f"olympus.{mod}.notify",
                            lambda t, *a, **k: False, raising=False)
    return got


def _alerts(got):
    return {c: [m for m in got[c] if "Page changed" in m] for c in got}


def test_default_does_not_broadcast_to_shared_channels_guard_off(
        store, monkeypatch):
    """A. THE red test. Default config, guard off (the shipped default).

    Against v1 both shared channels receive A's private watched content,
    because `run_due` fell through to `gateway.notify_all` whose `user=` is a
    routing no-op — and with the guard disabled is not even read.
    """
    monkeypatch.delenv("OLYMPUS_EGRESS_GUARD", raising=False)
    monkeypatch.delenv("OLYMPUS_WEB_MONITOR_BROADCAST", raising=False)
    got = _stub_channels(monkeypatch)
    webmonitor.add(A, "https://private.example/inbox", interval=1)

    _diff(monkeypatch, "h1")
    webmonitor.run_due(now=1000)                       # baseline
    _diff(monkeypatch, "h2", diff="ALICE PRIVATE INBOX CONTENT")
    log = webmonitor.run_due(now=2000)

    alerts = _alerts(got)
    assert alerts["telegram"] == [], "A's content reached a shared channel"
    assert alerts["discord"] == [], "A's content reached a shared channel"
    assert any("CHANGED" in ln for ln in log)          # still detected
    assert any("no owner-targeted route" in ln for ln in log), log


def test_default_does_not_broadcast_to_shared_channels_guard_on(
        store, monkeypatch):
    """B. Enabling the guard does not make broadcast acceptable: the guard
    decides WHETHER to send, never TO WHOM. Benign content passes it, so
    without the fail-closed default it would still fan out."""
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    monkeypatch.delenv("OLYMPUS_WEB_MONITOR_BROADCAST", raising=False)
    got = _stub_channels(monkeypatch)
    webmonitor.add(A, "https://private.example/inbox", interval=1)

    _diff(monkeypatch, "h1")
    webmonitor.run_due(now=1000)
    _diff(monkeypatch, "h2", diff="- old price\n+ new price")
    log = webmonitor.run_due(now=2000)

    alerts = _alerts(got)
    assert alerts["telegram"] == [] and alerts["discord"] == []
    assert any("no owner-targeted route" in ln for ln in log), log


def test_broadcast_optin_without_the_guard_fails_closed(store, monkeypatch):
    """C. The opt-in alone is not enough. Shared fan-out is C0-only territory,
    so it is permitted only while the guard can hold sensitive payloads; asking
    for broadcast with the guard off delivers nowhere and says why."""
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR_BROADCAST", "1")
    monkeypatch.delenv("OLYMPUS_EGRESS_GUARD", raising=False)
    got = _stub_channels(monkeypatch)
    webmonitor.add(A, "https://private.example/inbox", interval=1)

    _diff(monkeypatch, "h1")
    webmonitor.run_due(now=1000)
    _diff(monkeypatch, "h2", diff="ALICE PRIVATE INBOX CONTENT")
    log = webmonitor.run_due(now=2000)

    alerts = _alerts(got)
    assert alerts["telegram"] == [] and alerts["discord"] == []
    assert any("requires OLYMPUS_EGRESS_GUARD" in ln for ln in log), log


def test_broadcast_optin_with_guard_reaches_installation_shared_channels(
        store, monkeypatch):
    """D. Both flags set: the operator has explicitly accepted installation-wide
    fan-out. These are the INSTALLATION'S shared channels — every configured
    destination, visible to everyone on them. This is not owner routing, and
    the assertions below deliberately name that."""
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR_BROADCAST", "1")
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    got = _stub_channels(monkeypatch)

    seen_guard_users = []
    real = egress.guard
    monkeypatch.setattr(egress, "guard",
                        lambda text, ch, *, user, **kw: (
                            seen_guard_users.append(user)
                            or real(text, ch, user=user, **kw)))

    webmonitor.add(A, "https://ex.example/x", interval=1)
    _diff(monkeypatch, "h1")
    webmonitor.run_due(now=1000)
    _diff(monkeypatch, "h2", diff="- old price\n+ new price")
    webmonitor.run_due(now=2000)

    alerts = _alerts(got)
    # BOTH shared channels received it — that is what this mode means.
    assert len(alerts["telegram"]) == 1 and len(alerts["discord"]) == 1
    assert seen_guard_users == [A], "guard did not evaluate the exact owner"


@pytest.mark.requires_crypto
def test_broadcast_with_guard_holds_content_carrying_the_owners_secret(
        store, monkeypatch):
    """E. Even in the accepted-broadcast mode, the guard still evaluates the
    OWNER's vault — the part v1 got right and this patch keeps."""
    from olympus import vault
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "a-test-passphrase")
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR_BROADCAST", "1")
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    vault.put(A, "api", {"token": "ALICE-SUPER-SECRET-TOKEN-VALUE"})
    got = _stub_channels(monkeypatch)

    seen_guard_users = []
    real = egress.guard
    monkeypatch.setattr(egress, "guard",
                        lambda text, ch, *, user, **kw: (
                            seen_guard_users.append(user)
                            or real(text, ch, user=user, **kw)))

    webmonitor.add(A, "https://ex.example/x", interval=1)
    _diff(monkeypatch, "h1")
    webmonitor.run_due(now=1000)
    _diff(monkeypatch, "h2", diff="token=ALICE-SUPER-SECRET-TOKEN-VALUE")
    webmonitor.run_due(now=2000)

    alerts = _alerts(got)
    assert seen_guard_users == [A]
    assert alerts["telegram"] == [] and alerts["discord"] == []


def test_injected_callback_is_used_and_never_falls_back_to_broadcast(
        store, monkeypatch):
    """F. An explicit owner-aware callback works with BOTH flags off — it is the
    extension point a future `notify_owner` plugs into — and the global
    channels are never touched."""
    monkeypatch.delenv("OLYMPUS_EGRESS_GUARD", raising=False)
    monkeypatch.delenv("OLYMPUS_WEB_MONITOR_BROADCAST", raising=False)
    got = _stub_channels(monkeypatch)
    webmonitor.add(A, "https://ex.example/x", interval=1)
    spy = _Spy()

    _baseline_then_change(monkeypatch, spy)

    assert len(spy.calls) == 1 and spy.calls[0]["user"] == A
    assert _alerts(got)["telegram"] == [] and _alerts(got)["discord"] == []


def test_failing_callback_never_falls_back_to_broadcast(store, monkeypatch):
    """A callback that RAISES must not be compensated for by the shared fan-out
    — that would turn a delivery failure into a disclosure."""
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR_BROADCAST", "1")
    monkeypatch.setenv("OLYMPUS_EGRESS_GUARD", "1")
    got = _stub_channels(monkeypatch)
    webmonitor.add(A, "https://ex.example/x", interval=1)

    def boom(text, *, user):
        raise RuntimeError("channel down")

    _diff(monkeypatch, "h1")
    webmonitor.run_due(now=1000, notify=boom)
    _diff(monkeypatch, "h2")
    log = webmonitor.run_due(now=2000, notify=boom)

    assert _alerts(got)["telegram"] == [] and _alerts(got)["discord"] == []
    assert any("NOT delivered" in ln for ln in log), log


def test_change_state_advances_even_when_delivery_is_refused(
        store, monkeypatch):
    """J. Fail-closed delivery must not break detection: the hash and counter
    advance, so the same change does not re-alert every tick."""
    monkeypatch.delenv("OLYMPUS_EGRESS_GUARD", raising=False)
    monkeypatch.delenv("OLYMPUS_WEB_MONITOR_BROADCAST", raising=False)
    _stub_channels(monkeypatch)
    webmonitor.add(A, "https://ex.example/x", interval=1)

    _diff(monkeypatch, "h1")
    webmonitor.run_due(now=1000)
    _diff(monkeypatch, "h2")
    log2 = webmonitor.run_due(now=2000)
    assert any("CHANGED" in ln for ln in log2)
    mon = webmonitor.list_for(A)[0]
    assert mon.changes == 1 and mon.last_hash == "h2"

    # Same content again → no further CHANGED, counter stays put.
    log3 = webmonitor.run_due(now=3000)
    assert not any("CHANGED" in ln for ln in log3)
    assert webmonitor.list_for(A)[0].changes == 1


# --- the egress guard on the accepted-broadcast path -----------------------

def _arm_guard(monkeypatch):
    monkeypatch.setattr("olympus.config.egress_guard_enabled", lambda: True)
    monkeypatch.setenv("OLYMPUS_WEB_MONITOR_BROADCAST", "1")


@pytest.mark.requires_crypto
def test_guard_evaluates_the_monitor_owner_not_shared(store, monkeypatch):
    """The security consequence, end to end through `gateway.notify_all`.

    A's vault holds a secret. A watched page starts echoing it. The guard must
    check A's vault — as "shared" it sees nothing, the payload classifies clean,
    and the secret is broadcast to every configured channel.
    """
    from olympus import gateway, vault
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "a-test-passphrase")
    vault.put(A, "api", {"token": "ALICE-SUPER-SECRET-TOKEN-VALUE"})
    _arm_guard(monkeypatch)

    seen_users = []
    real_guard = egress.guard

    def spy_guard(text, channel, *, user, **kw):
        seen_users.append(user)
        return real_guard(text, channel, user=user, **kw)
    monkeypatch.setattr(egress, "guard", spy_guard)

    delivered = []
    for name in gateway.NOTIFY_CHANNELS:
        monkeypatch.setattr(f"olympus.{'signal' if name == 'signal' else name}"
                            ".notify",
                            lambda t, *a, **k: delivered.append(t) or True,
                            raising=False)

    webmonitor.add(A, "https://ex.example/x", interval=1)
    _diff(monkeypatch, "h1")
    webmonitor.run_due(now=1000)                       # baseline, silent
    _diff(monkeypatch, "h2", diff="token=ALICE-SUPER-SECRET-TOKEN-VALUE")
    webmonitor.run_due(now=2000)

    assert seen_users == [A], f"guard evaluated {seen_users}, expected [{A!r}]"
    assert delivered == [], "a payload carrying the owner's vault secret egressed"


@pytest.mark.requires_crypto
def test_benign_change_reaches_installation_shared_channels_only_under_optin(
        store, monkeypatch):
    """Renamed from `test_benign_change_still_reaches_the_owners_channel`.

    That name asserted something Olympus cannot do. The old test stubbed every
    channel except Telegram to decline and then asserted one delivery — which
    proved only that the test configured one channel, not that delivery was
    owner-targeted. There is no "owner's channel": `TELEGRAM_NOTIFY_CHAT_ID` is
    one installation-global destination shared by every tenant.

    What is actually true, and what this asserts: under the explicit
    broadcast opt-in the guard does not blanket-block a clean payload, it
    evaluates the exact owner, and the message lands on the INSTALLATION'S
    shared channels — plural, and visible to everyone on them.
    """
    from olympus import vault
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "a-test-passphrase")
    vault.put(A, "api", {"token": "ALICE-SUPER-SECRET-TOKEN-VALUE"})
    _arm_guard(monkeypatch)                     # guard ON + broadcast opt-in
    got = _stub_channels(monkeypatch)           # TWO shared destinations accept

    seen_users = []
    real_guard = egress.guard

    def spy_guard(text, channel, *, user, **kw):
        seen_users.append(user)
        return real_guard(text, channel, user=user, **kw)
    monkeypatch.setattr(egress, "guard", spy_guard)

    webmonitor.add(A, "https://ex.example/x", interval=1)
    _diff(monkeypatch, "h1")
    webmonitor.run_due(now=1000)
    _diff(monkeypatch, "h2", diff="- old price\n+ new price")
    webmonitor.run_due(now=2000)

    assert seen_users == [A], "the guard must evaluate the exact monitor owner"
    alerts = _alerts(got)
    # BOTH shared channels got it. That is the semantics of this mode, and the
    # reason it is off by default on a multi-tenant install.
    assert len(alerts["telegram"]) == 1
    assert len(alerts["discord"]) == 1


# --- store contract the fix must not disturb -------------------------------

def test_owner_survives_the_persisted_round_trip(store):
    webmonitor.add(A, "https://ex.example/x", interval=1)
    raw = json.loads((store / "webmonitors.json").read_text(encoding="utf-8"))
    assert raw[0]["user"] == A                          # format unchanged
    assert webmonitor._load()[0].user == A


def test_ownerless_legacy_record_is_dropped_not_relabelled(store):
    """`Monitor.user` has no default, so `_load`'s `except TypeError: continue`
    SKIPS an owner-less record. That is the pre-existing contract and this patch
    keeps it: inventing an owner for such a record would relabel it as somebody,
    and defaulting it to "shared" would broadcast it exactly as before."""
    path = store / "webmonitors.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([
        {"id": "legacy01", "url": "https://ex.example/x", "interval": 3600,
         "active": True},
        {"id": "modern01", "user": A, "url": "https://ex.example/y",
         "interval": 3600, "active": True},
    ]), encoding="utf-8")

    loaded = webmonitor._load()
    assert [m.id for m in loaded] == ["modern01"]
    assert loaded[0].user == A
