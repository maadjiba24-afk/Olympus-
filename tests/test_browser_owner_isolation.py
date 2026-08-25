"""P0: the installation's browser is leased to ONE authenticated owner.

The browser is a single credentialed resource — whoever is signed in through it
is signed in for everything that drives it. Before this lease, a second tenant
on the same instance could read the first tenant's authenticated pages with two
ungated tool calls (`browser_open` at a domain the shared browser holds cookies
for, then `browser_read`), enumerate their logged-in tabs, drive their session,
and copy their cookies into their own vault.

Each test here pins one clause of that boundary. The adversary throughout is
user B: a legitimate, fully onboarded second tenant — not an injected page.
"""

import json
import threading
import time

import pytest

from olympus import (browser, browserlease, config, memory, operator, tools,
                     webctx)

A = "tg-alice"          # platform-authenticated chat user
B = "tg-bob"            # a DIFFERENT platform-authenticated chat user


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr("olympus.security.url_block_reason",
                        lambda u, resolve=True: None)
    browser.set_transport_factory(None)
    browserlease.clear_for_tests()
    yield
    browser.set_transport_factory(None)
    browserlease.clear_for_tests()
    memory.set_user("shared")


def _fake(pages=None, targets=None, present=None):
    pages = pages or {"https://mail.corp.com/": {"title": "Inbox",
                                                 "text": "SECRET-INBOX-BODY"}}
    browser.set_transport_factory(
        lambda: browser.FakeTransport(pages=pages, targets=targets,
                                      present=present))


def _own_as(user, url="https://mail.corp.com/"):
    """Give `user` the lease with a loaded, 'authenticated' page."""
    sess = browser.session(user)
    sess.open(url)
    return sess


def _as_user(user):
    memory.set_user(user)


# --- A. cross-user access -------------------------------------------------

@pytest.mark.parametrize("handler,args", [
    (tools._browser_open, ("https://mail.corp.com/",)),
    (tools._browser_read, ()),
    (tools._browser_read_ax, ()),
    (tools._browser_screenshot, ()),
    (tools._browser_console, ()),
    (tools._browser_save_pdf, ()),
    (tools._browser_exists, ("#inbox",)),
])
def test_b_cannot_reach_any_tier1_reader_while_a_owns_the_browser(
        handler, args, monkeypatch):
    """The cheapest cross-tenant read in the system. None of these tools is
    operator-gated or stripped from an ingesting run, so the lease is the only
    thing standing between B and A's authenticated page."""
    _fake()
    _own_as(A)
    _as_user(B)
    out = handler(*args)
    assert out == browserlease.REFUSAL
    assert "SECRET-INBOX-BODY" not in out


def test_b_denied_open_does_not_move_a_page(monkeypatch):
    """A denial must have no side effect: B must not be able to navigate A's
    browser away, which would be both a leak vector and a DoS on A's run."""
    _fake(pages={"https://mail.corp.com/": {"title": "Inbox", "text": "body"},
                 "https://evil.test/": {"title": "Evil", "text": "pwn"}})
    sess = _own_as(A)
    before_url = sess.url
    before_ledger = len(sess.ledger)

    _as_user(B)
    assert tools._browser_open("https://evil.test/") == browserlease.REFUSAL

    assert sess.url == before_url
    assert len(sess.ledger) == before_ledger      # no CDP call was made at all


def test_b_cannot_list_or_switch_a_tabs(monkeypatch):
    """browser_tabs/switch_tab are gated only on operator-enabled, which B can
    self-grant. Tab titles and URLs are reconnaissance of A's logged-in life."""
    _fake(targets=[{"type": "page", "targetId": "t1",
                    "title": "Inbox — alice@corp", "url": "https://mail.corp.com/"}])
    _own_as(A)

    _as_user(B)
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")       # B self-enables the operator
    out = tools._browser_tabs()
    assert out == browserlease.REFUSAL
    assert "alice@corp" not in out and "mail.corp.com" not in out
    assert tools._browser_switch_tab(0) == browserlease.REFUSAL


def test_b_cannot_observe_or_act_after_self_authorizing_a_domain(monkeypatch):
    """Site authorization is self-service, so it protects nothing across
    tenants: B authorizes A's domain for themselves and the domain check
    passes. The lease is what refuses."""
    _fake(present=["#send"])
    _own_as(A)

    _as_user(B)
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    operator.authorize_site(B, "mail.corp.com")       # B grants it to B
    assert operator.authorized(B, "mail.corp.com")    # the gate really is open

    assert tools._browser_observe() == browserlease.REFUSAL
    assert tools._browser_act("click", selector="#send") == browserlease.REFUSAL


@pytest.mark.requires_crypto
def test_b_cannot_save_a_cookies_into_b_vault(monkeypatch):
    """The durable half of the breach: Storage.getCookies is browser-wide, so
    without the lease B harvests whatever session the shared browser holds and
    files it under B's own vault namespace — replayable long after A leaves."""
    from olympus import vault
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "a-test-passphrase")
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    monkeypatch.setenv("OLYMPUS_OPERATOR_DOMAINS", "mail.corp.com")
    _fake()
    sess = _own_as(A)
    sess.set_cookies([{"name": "sid", "value": "ALICE-SESSION",
                       "domain": "mail.corp.com"}])

    _as_user(B)
    out = operator.save_auth(B, "mail.corp.com")
    assert out == browserlease.REFUSAL
    # Assert on the VAULT, not the return string: the question is whether the
    # credential landed, not whether a message was printed.
    assert vault.get(B, "cookies:mail.corp.com") is None


@pytest.mark.requires_crypto
def test_b_cannot_inject_cookies_into_a_session(monkeypatch):
    """Planting a session into a browser someone else is driving is the mirror
    image of harvesting one."""
    from olympus import vault
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "a-test-passphrase")
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    monkeypatch.setenv("OLYMPUS_OPERATOR_DOMAINS", "mail.corp.com")
    _fake()
    sess = _own_as(A)
    vault.put(B, "cookies:mail.corp.com",
              {"cookies": [{"name": "sid", "value": "BOB-PLANTED",
                            "domain": "mail.corp.com"}]})

    _as_user(B)
    assert operator.restore_auth(B, "mail.corp.com") == browserlease.REFUSAL
    assert sess.get_cookies("mail.corp.com") == []       # nothing was planted


def test_b_cannot_drive_the_browser_through_webctx_actions():
    """webctx's action-scrape path reaches the same shared session; it was the
    non-tool route to the same capability."""
    _fake(pages={"https://app/": {"title": "App", "text": "after click"}})
    _own_as(A, url="https://app/")

    res = webctx.scrape("https://app/", formats=("markdown",), owner=B,
                        actions=[{"type": "click", "selector": "#more"}])
    assert res.get("error") == browserlease.REFUSAL


# --- B. principal integrity -----------------------------------------------

@pytest.mark.parametrize("bad", [
    "", None, "shared",
    "web-abc123",                 # caller-selectable when REQUIRE_LOGIN is off
    "web-",
    "   ",
    "nobody", "admin", "root",    # default deny: unrecognised is untrusted
])
def test_untrusted_principals_cannot_claim(bad):
    _fake()
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session(bad)
    assert browserlease.current() is None          # and nothing was claimed


@pytest.mark.parametrize("good", [
    "cli",                # local terminal
    "tg-42", "dc-42", "sl-42", "sg-42", "wa-42", "email-42", "hook-42",
    "u:acct-1",           # accounts.namespace_for_token form
    "u-acct-1",           # its memory.safe_id normalization
])
def test_authenticated_principals_can_claim(good):
    _fake()
    assert browser.session(good) is not None
    assert browserlease.current()["owner"] == good


@pytest.mark.parametrize("bad", [
    "", "   ", "shared", "web-abc123", "web-", "nobody", "admin", "root",
])
def test_claim_enforces_the_principal_policy_itself(bad):
    """`claim` is a public entry point, so it must not trust its caller.

    `browser.session` validates before calling it, but a direct caller could
    otherwise mint a lease for `shared` or a `web-*` session id — and every
    later owner comparison would then honour that record as a real owner.
    """
    _fake()
    with pytest.raises(browserlease.OwnershipRefused):
        browserlease.claim(bad, "fake", "")
    assert browserlease.current() is None
    assert not browserlease._path().exists()


@pytest.mark.parametrize("good", ["cli", "tg-42", "u:acct-1", "u-acct-1",
                                  "email-someone", "hook-svc"])
def test_claim_accepts_authenticated_principals_directly(good):
    _fake()
    record = browserlease.claim(good, "fake", "")
    assert record["owner"] == good and record["lease_id"]
    assert browserlease.current()["owner"] == good


@pytest.mark.parametrize("bad", ["", "shared", "web-abc123", "nobody"])
def test_release_also_refuses_untrusted_principals(bad):
    """Symmetry: the write side of the API is closed at both ends."""
    _fake()
    _own_as(A)
    with pytest.raises(browserlease.OwnershipRefused):
        browserlease.release(bad, browserlease.current()["lease_id"])
    assert browserlease.current()["owner"] == A


def test_check_always_performs_its_own_locked_read():
    """`check` used to accept a caller-supplied record via `_loaded=True` — an
    escape hatch that trusted a record whose provenance it could not verify,
    and which nothing used. It now takes only the principal."""
    import inspect
    params = list(inspect.signature(browserlease.check).parameters)
    assert params == ["owner"], f"check() grew an escape hatch: {params}"

    _fake()
    _own_as(A)
    assert browserlease.check(A)["owner"] == A
    with pytest.raises(browserlease.OwnershipRefused):
        browserlease.check(B)
    with pytest.raises(browserlease.OwnershipRefused):
        browserlease.check("shared")


def test_heartbeat_ambient_namespace_cannot_claim():
    """The heartbeat runs under `shared`. Background maintenance must never end
    up owning — or driving — a person's authenticated browser."""
    _fake()
    memory.set_user("shared")
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session(memory.current_user())


def test_lossy_normalization_candidates_stay_distinct():
    """`memory.safe_id` maps "a.b", "a@b" and "a-b" onto one string and
    truncates at 64 chars. If the lease used it as the identity, distinct
    principals would share a browser. The lease compares exact strings."""
    collide = ["tg-a.b", "tg-a@b", "tg-a b"]
    for spelling in collide:
        assert memory.safe_id(spelling) == memory.safe_id("tg-a-b")
    assert browserlease.canonical("tg-a.b") != browserlease.canonical("tg-a-b")

    _fake()
    browser.session("tg-a.b")
    for other in ("tg-a-b", "tg-a@b", "tg-a b"):
        with pytest.raises(browserlease.OwnershipRefused):
            browser.session(other)

    # Truncation: two long principals sharing a 64-char prefix must not merge.
    long_a = "tg-" + ("x" * 70) + "AAA"
    long_b = "tg-" + ("x" * 70) + "BBB"
    assert memory.safe_id(long_a) == memory.safe_id(long_b)
    assert browserlease.canonical(long_a) != browserlease.canonical(long_b)


@pytest.mark.parametrize("configured", [
    "shared",                    # the heartbeat's ambient namespace
    "web-attacker",              # caller-controlled when REQUIRE_LOGIN is off
    "web-",
    "",
    "   ",
    "kiosk-owner",               # unrecognised namespace
    "admin", "root", "nobody",
])
def test_configured_owner_cannot_promote_an_untrusted_principal(
        configured, monkeypatch):
    """OLYMPUS_BROWSER_OWNER SELECTS which trusted principal owns a pre-existing
    remote browser. It must never PROMOTE one: an operator typo must not hand
    the browser to the heartbeat's namespace, and pasting a caller-controlled
    no-login web identity must not turn it into an authentication."""
    monkeypatch.setenv(browserlease.OWNER_ENV, configured)
    assert browserlease.is_trusted(configured) is False
    assert browserlease.configured_owner() == ""      # yields nothing usable

    _fake()
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session(configured)
    assert browserlease.current() is None


@pytest.mark.parametrize("configured", ["tg-kiosk", "u:acct-9", "cli"])
def test_configured_owner_selects_an_already_trusted_principal(
        configured, monkeypatch):
    monkeypatch.setenv(browserlease.OWNER_ENV, configured)
    assert browserlease.configured_owner() == configured
    assert browserlease.is_trusted(configured) is True


def test_configured_untrusted_owner_cannot_unlock_remote_cdp(monkeypatch):
    """The remote-CDP gate is the one place configured_owner() grants access.
    An untrusted value there must not open it — for anyone."""
    monkeypatch.setenv("OLYMPUS_BROWSER_CDP_URL", "http://127.0.0.1:59999")
    monkeypatch.setenv(browserlease.OWNER_ENV, "shared")
    browser.set_transport_factory(None)
    browser._session = None
    attempted = []
    monkeypatch.setattr(browser, "_build_transport",
                        lambda: attempted.append("built"))

    for who in ("shared", "web-attacker", A):
        with pytest.raises(browserlease.OwnershipRefused):
            browser.session(who)
    assert attempted == []
    assert browserlease.current() is None


# --- C. lifecycle ----------------------------------------------------------

def test_concurrent_thread_claim_has_exactly_one_winner():
    _fake()
    results, errors = [], []
    barrier = threading.Barrier(2)

    def claim(user):
        barrier.wait()
        try:
            browser.session(user)
            results.append(user)
        except browserlease.OwnershipRefused:
            errors.append(user)

    threads = [threading.Thread(target=claim, args=(u,)) for u in (A, B)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1 and len(errors) == 1
    assert browserlease.current()["owner"] == results[0]


def test_concurrent_process_claim_has_exactly_one_winner(tmp_path):
    """Threads alone do not prove this: the heartbeat, web, and CLI are separate
    OS processes sharing MEMORY_DIR, so the claim must be cross-process.

    This holds even where `proclock` has no teeth. `proclock` degrades to
    in-process locking wherever `fcntl` is unavailable (Windows, ADR 0005), so
    the claim does NOT rest on it: the lease record is created with
    O_CREAT|O_EXCL, which the filesystem makes atomic on every supported
    platform. The winner is decided by the create, not by the lock.
    """
    import subprocess
    import sys
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    script = tmp_path / "claim.py"
    script.write_text(
        "import sys, pathlib\n"
        "from olympus import config\n"
        f"config.MEMORY_DIR = pathlib.Path({str(tmp_path)!r})\n"
        "from olympus import browserlease\n"
        "try:\n"
        "    rec = browserlease.claim(sys.argv[1], 'fake', '')\n"
        "    print('WON' if rec['owner'] == sys.argv[1] else 'LOST')\n"
        "except browserlease.OwnershipRefused:\n"
        "    print('LOST')\n", encoding="utf-8")

    procs = [subprocess.Popen([sys.executable, str(script), u],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              cwd=str(repo))
             for u in (A, B)]
    outs, errs = [], []
    for p in procs:
        out, err = p.communicate(timeout=180)
        outs.append(out.decode(errors="replace").strip())
        errs.append(err.decode(errors="replace").strip())

    assert all(o in ("WON", "LOST") for o in outs), (outs, errs)
    assert outs.count("WON") == 1, (outs, errs)
    assert outs.count("LOST") == 1, (outs, errs)

    # And exactly one owner is recorded — no interleaved half-write.
    record = json.loads((tmp_path / "browser_lease.json").read_text(
        encoding="utf-8"))
    assert record["owner"] in (A, B)


# --- C2. cross-process lease mutations (the release race) ------------------
#
# These drive REAL subprocesses. `O_CREAT|O_EXCL` makes the initial claim
# atomic but protects CREATION ONLY — release is a read-validate-then-unlink,
# and without a genuine cross-process lock this interleaving destroys a live
# lease:
#
#     R1 reads and validates A's lease, then pauses.
#     R2 completes A's release.
#     B claims; a new lease exists.
#     R1 resumes and unlinks — deleting B's lease.
#     C claims a browser that still holds B's session state.

_CHILD_PREAMBLE = """
import json, os, pathlib, sys, time
from olympus import config
config.MEMORY_DIR = pathlib.Path(sys.argv[1])
from olympus import browserlease
"""


def _run_children(tmp_path, bodies, timeout=180):
    """Run each body as its own OS process against the same MEMORY_DIR."""
    import subprocess
    import sys
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    procs = []
    for i, body in enumerate(bodies):
        script = tmp_path / f"child{i}.py"
        script.write_text(_CHILD_PREAMBLE + body, encoding="utf-8")
        procs.append(subprocess.Popen(
            [sys.executable, str(script), str(tmp_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(repo)))
    out = []
    for p in procs:
        o, e = p.communicate(timeout=timeout)
        out.append((o.decode(errors="replace").strip(),
                    e.decode(errors="replace").strip()))
    return out


_CLAIM = """
try:
    rec = browserlease.claim(sys.argv[2], 'fake', '')
    print('WON' if rec['owner'] == sys.argv[2] else 'LOST')
except browserlease.OwnershipRefused:
    print('LOST')
"""


def test_lock_genuinely_excludes_across_processes(tmp_path):
    """Prove the lock has real cross-process teeth on THIS platform.

    Everything else in this section would still pass if `_lock()` were a no-op
    and the operations merely happened not to interleave. This one cannot: a
    second process must actually BLOCK while the first holds the lock. It is the
    test that distinguishes a working lock from a decorative one — and the
    reason `proclock` (in-process only without fcntl) was not reused here.
    """
    import subprocess
    import sys
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    hold_seconds = 3.0

    holder = tmp_path / "holder.py"
    holder.write_text(_CHILD_PREAMBLE + f"""
with browserlease._lock():
    print('HELD', flush=True)
    time.sleep({hold_seconds})
""", encoding="utf-8")
    waiter = tmp_path / "waiter.py"
    waiter.write_text(_CHILD_PREAMBLE + """
t0 = time.monotonic()
with browserlease._lock():
    print('WAITED %.2f' % (time.monotonic() - t0), flush=True)
""", encoding="utf-8")

    hp = subprocess.Popen([sys.executable, str(holder), str(tmp_path)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          cwd=str(repo))
    # Wait until the holder reports it actually owns the lock, so the waiter's
    # measured delay is lock contention and not process startup.
    assert hp.stdout.readline().strip() == b"HELD"

    t0 = time.monotonic()
    wp = subprocess.run([sys.executable, str(waiter), str(tmp_path)],
                        capture_output=True, timeout=120, cwd=str(repo))
    elapsed = time.monotonic() - t0
    hp.communicate(timeout=60)

    out = wp.stdout.decode(errors="replace").strip()
    assert out.startswith("WAITED"), (out, wp.stderr.decode(errors="replace"))
    blocked = float(out.split()[1])
    # It must have blocked for a substantial part of the hold. Generous floor:
    # the waiter still has to start Python and import olympus first.
    assert blocked > hold_seconds * 0.4, (
        f"lock did not block across processes (waited {blocked:.2f}s of a "
        f"{hold_seconds}s hold) — it has no cross-process teeth here")
    assert elapsed > hold_seconds * 0.4


def test_process_simultaneous_initial_claim_has_one_winner(tmp_path):
    import subprocess
    import sys
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    script = tmp_path / "claim.py"
    script.write_text(_CHILD_PREAMBLE + _CLAIM, encoding="utf-8")
    procs = [subprocess.Popen(
        [sys.executable, str(script), str(tmp_path), u],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(repo))
        for u in (A, B)]
    outs = []
    for p in procs:
        o, e = p.communicate(timeout=180)
        outs.append((o.decode(errors="replace").strip(),
                     e.decode(errors="replace").strip()))
    results = [o for o, _ in outs]
    assert results.count("WON") == 1, outs
    assert results.count("LOST") == 1, outs
    record = json.loads((tmp_path / "browser_lease.json").read_text("utf-8"))
    assert record["owner"] in (A, B) and record["lease_id"]


def test_process_paused_release_cannot_delete_a_relaimed_lease(tmp_path):
    """THE blocker interleaving, driven for real.

    R1 validates A's lease and holds the token, then sleeps. Meanwhile another
    process releases A and B claims. R1 then attempts its delete. The lease that
    survives must be B's — not absent, and not deleted by R1.
    """
    # Seed A's lease and capture its generation token.
    out = _run_children(tmp_path, ["""
rec = browserlease.claim(sys.argv[2] if len(sys.argv) > 2 else 'tg-alice',
                         'fake', '')
print(json.dumps(rec))
"""])
    seeded = json.loads(out[0][0])
    assert seeded["owner"] == "tg-alice"
    stale_id = seeded["lease_id"]

    # R2 releases A; B then claims. Both in one child, strictly ordered.
    _run_children(tmp_path, [f"""
browserlease.release('tg-alice', {stale_id!r})
rec = browserlease.claim('tg-bob', 'fake', '')
print('BCLAIMED', rec['lease_id'])
"""])
    live = json.loads((tmp_path / "browser_lease.json").read_text("utf-8"))
    assert live["owner"] == "tg-bob"
    assert live["lease_id"] != stale_id

    # R1 finally resumes with the STALE token and tries to delete.
    res = _run_children(tmp_path, [f"""
try:
    browserlease.release('tg-alice', {stale_id!r})
    print('DELETED')
except browserlease.OwnershipRefused:
    print('REFUSED')
"""])
    assert res[0][0] == "REFUSED", res

    # B's lease is intact — never absent, never someone else's.
    after = json.loads((tmp_path / "browser_lease.json").read_text("utf-8"))
    assert after == live


def test_process_simultaneous_same_owner_releases(tmp_path):
    """Two concurrent releases of the SAME lease: idempotent, never an error
    that leaves state ambiguous, and the record ends up gone exactly once."""
    out = _run_children(tmp_path, ["""
print(json.dumps(browserlease.claim('tg-alice', 'fake', '')))
"""])
    lease_id = json.loads(out[0][0])["lease_id"]

    body = f"""
try:
    browserlease.release('tg-alice', {lease_id!r})
    print('OK')
except browserlease.OwnershipRefused:
    print('REFUSED')
"""
    res = _run_children(tmp_path, [body, body])
    codes = [o for o, _ in res]
    assert all(c in ("OK", "REFUSED") for c in codes), res
    assert "OK" in codes, res                    # at least one really released
    assert not (tmp_path / "browser_lease.json").exists()


def test_release_then_reclaim_mints_a_fresh_generation(tmp_path):
    """The generation semantics the race test rests on, pinned without a race.

    `claim()` PRESERVES `lease_id` while a lease exists — that is what makes a
    fingerprint backfill safe. But once `release()` has removed a generation,
    the next `claim()` is a genuinely new lease and MUST get a new `lease_id`;
    reusing the released one would resurrect a generation a holder had already
    been told was gone, and would let a stale release delete the new lease.
    """
    first = browserlease.claim(A, "fake", "")
    # Backfill while the lease lives: same generation, fingerprint updated.
    same = browserlease.claim(A, "fake", "ws://fp/1")
    assert same["lease_id"] == first["lease_id"], "backfill moved the generation"

    browserlease.release(A, first["lease_id"])
    assert not browserlease._path().exists()

    second = browserlease.claim(A, "fake", "ws://fp/2")
    assert second["lease_id"], "a re-claim must have a non-empty generation"
    assert second["lease_id"] != first["lease_id"], (
        "a post-release re-claim reused the RELEASED generation")
    assert second["owner"] == A

    # And the released generation cannot reach back and delete the new one.
    with pytest.raises(browserlease.OwnershipRefused):
        browserlease.release(A, first["lease_id"])
    assert browserlease.current()["lease_id"] == second["lease_id"]


def test_process_fingerprint_backfill_races_release(tmp_path):
    """A cross-process release landing BETWEEN two backfill claims.

    THE OLD ASSERTION WAS WRONG, and CI caught it on a healthy tree: it required
    `rec["lease_id"] == original`, commented "generation never moved". The
    generation legitimately moves. The release removes the original generation
    while the backfill child is still looping, so its next `claim()` finds no
    record and correctly mints a NEW one — CI observed exactly that (initial
    `1a198c6b…`, final `55e6452a…`, owner unchanged). Asserting a fixed
    `lease_id` forbade a correct schedule.

    THE ORDERING IS PINNED, NOT SAMPLED. `time.sleep(0.05)` is gone, and so is
    the one-way barrier that replaced it: signalling only "the loop has begun"
    still allowed the backfill child to finish all forty claims before the
    release child got a timeslice, which would have made the interleaving this
    test exists for merely likely. The two children now hand off both ways —
    backfill claims once and waits; release fires and waits; backfill resumes —
    so the release provably lands between claim #0 and claim #1, and every
    outcome below is a single determined state rather than a set of
    alternatives.

    Because the original generation is known to exist at the handoff, the
    release MUST succeed, a record MUST survive, and it must be a NEW
    generation — the released one must never come back.
    """
    out = _run_children(tmp_path, ["""
print(json.dumps(browserlease.claim('tg-alice', 'fake', '')))
"""])
    original = json.loads(out[0][0])["lease_id"]

    res = _run_children(tmp_path, [
        # Backfill: claim once, hand off, wait for the release, then resume.
        """
started = pathlib.Path(sys.argv[1]) / 'backfill-started'
finished = pathlib.Path(sys.argv[1]) / 'release-finished'
browserlease.claim('tg-alice', 'fake', 'ws://fp/0')
started.write_text('go', encoding='utf-8')
deadline = time.monotonic() + 60
while not finished.exists():
    if time.monotonic() > deadline:
        print('HANDSHAKE-TIMEOUT')
        raise SystemExit(1)
    time.sleep(0.01)
for i in range(1, 40):
    try:
        browserlease.claim('tg-alice', 'fake', 'ws://fp/' + str(i))
    except browserlease.OwnershipRefused:
        pass
print('BACKFILL-DONE')
""",
        # Release: wait for the first claim, release, then release the backfill.
        f"""
started = pathlib.Path(sys.argv[1]) / 'backfill-started'
finished = pathlib.Path(sys.argv[1]) / 'release-finished'
deadline = time.monotonic() + 60
while not started.exists():
    if time.monotonic() > deadline:
        print('HANDSHAKE-TIMEOUT')
        raise SystemExit(1)
    time.sleep(0.01)
try:
    browserlease.release('tg-alice', {original!r})
    print('RELEASED')
except browserlease.OwnershipRefused:
    print('REFUSED')
finished.write_text('go', encoding='utf-8')
"""])
    assert res[0][0] == "BACKFILL-DONE", res
    assert res[1][0] == "RELEASED", (
        f"the original generation was live at the handoff, so the release had "
        f"to succeed: {res}")

    # The backfill resumed after the release, so a record MUST exist.
    path = tmp_path / "browser_lease.json"
    assert path.exists(), "the post-release backfill did not re-claim"

    # It must decode COMPLETELY — `_decode` rejects a torn write, an unknown
    # schema, a missing/blank owner and a missing lease_id, so this is the
    # "never half-written or ownerless" assertion.
    with browserlease._lock():
        rec = browserlease._decode(path.read_text("utf-8"))
    assert rec["owner"] == "tg-alice", rec
    assert rec["transport"] == "fake", rec
    assert isinstance(rec["lease_id"], str) and rec["lease_id"], rec
    assert rec["lease_id"] != original, (
        "the RELEASED generation was resurrected — a holder told its lease was "
        "gone would still match this record")

    # And the stale generation cannot reach back and delete the live one.
    with pytest.raises(browserlease.OwnershipRefused):
        browserlease.release(A, original)
    assert browserlease.current()["lease_id"] == rec["lease_id"]


def test_process_crash_while_holding_the_lock_releases_it(tmp_path):
    """A byte-range lock is owned by the OS: a holder that dies mid-transaction
    must not wedge the lease forever. If it did, one crashed heartbeat would
    take the browser out of service permanently."""
    import subprocess
    import sys
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    script = tmp_path / "crasher.py"
    script.write_text(_CHILD_PREAMBLE + """
with browserlease._lock():
    print('LOCKED', flush=True)
    os._exit(9)                      # die hard, inside the transaction
""", encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(script), str(tmp_path)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=str(repo))
    out, _err = p.communicate(timeout=120)
    assert b"LOCKED" in out
    assert p.returncode == 9

    # The lock is reclaimable and the lease is still coherent (absent here).
    res = _run_children(tmp_path, ["""
rec = browserlease.claim('tg-bob', 'fake', '')
print('CLAIMED', rec['owner'])
"""])
    assert res[0][0].startswith("CLAIMED tg-bob"), res


def test_exception_inside_the_lock_still_releases_it():
    """Same guarantee in-process: the context manager must unwind on an
    exception, or one failed transaction deadlocks every later one."""
    _fake()
    with pytest.raises(RuntimeError):
        with browserlease._lock():
            raise RuntimeError("boom")
    assert not browserlease._holding_lock()
    assert browser.session(A) is not None          # lock is free again


def test_mutations_refuse_outside_the_lock():
    """The transaction discipline is asserted at each mutation rather than
    trusted to call sites — a future caller that forgets the lock fails closed
    instead of racing."""
    _fake()
    assert not browserlease._holding_lock()
    for fn in (browserlease._read,
               lambda: browserlease._write({"owner": A}),
               lambda: browserlease._create_exclusive({"owner": A}),
               lambda: browserlease._remove_if(A, "x")):
        with pytest.raises(browserlease.OwnershipRefused):
            fn()


def test_release_without_a_lease_id_is_refused():
    """Defence in depth: no generation token means nothing safe to delete."""
    _fake()
    _own_as(A)
    with pytest.raises(browserlease.OwnershipRefused):
        browserlease.release(A, "")
    assert browserlease.current()["owner"] == A


def test_release_with_a_wrong_lease_id_is_refused():
    _fake()
    _own_as(A)
    with pytest.raises(browserlease.OwnershipRefused):
        browserlease.release(A, "deadbeef" * 4)
    assert browserlease.current()["owner"] == A


def test_lock_file_survives_release():
    """Unlinking a lock file is itself a race — two processes can end up
    locking different inodes. Only the lease record is removed."""
    _fake()
    _own_as(A)
    lock_file = browserlease._lock_path()
    assert lock_file.exists()
    assert browser.relinquish(A) == ""
    assert lock_file.exists()
    assert not browserlease._path().exists()


def test_reset_preserves_ownership_and_retained_state():
    """`reset()` is disconnect-only. On every real transport `close()` merely
    drops the connection — the autolaunched Chromium keeps running and a remote
    browser is external — so releasing here would hand a fully credentialed
    browser to the next caller."""
    _fake()
    _own_as(A)
    assert browserlease.current()["owner"] == A

    browser.reset()

    assert browserlease.current()["owner"] == A          # still owned
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session(B)
    assert browser.session(A) is not None                # A reconnects fine


def test_reset_does_not_wipe_a_retaining_transport():
    """Pins the semantics `reset()` actually has against a transport whose
    close() RETAINS state — the shape every production transport except
    Playwright has. Asserting this with a fresh-per-build fake would encode the
    opposite belief (which is exactly what the old cookie test did)."""
    retained = browser.FakeTransport()
    browser.set_transport_factory(lambda: retained)      # same object each build
    sess = browser.session(A)
    sess.set_cookies([{"name": "sid", "value": "ALICE", "domain": "corp.com"}])

    browser.reset()

    assert browserlease.current()["owner"] == A
    assert browser.session(A).get_cookies("corp.com")[0]["value"] == "ALICE"


@pytest.mark.requires_crypto
def test_set_transport_factory_preserves_ownership_and_lease_id(monkeypatch):
    """Reconfiguring the transport is NOT a teardown.

    `set_transport_factory` is a public test/embedder hook, so it is reachable
    by callers this codebase does not control — "production does not normally
    call it" is not a security boundary. An earlier revision dropped the lease
    here for test convenience, which handed anyone able to swap a transport a
    way to transfer a credentialed browser without destroying its state.

    Driven against a transport that RETAINS state across close(), because that
    is the shape every production transport except Playwright has: swapping the
    factory does not destroy what the old one was connected to.
    """
    # Fully arm B's own gates, so the refusals below are the LEASE refusing —
    # not the operator switch or the domain allowlist getting there first.
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "a-test-passphrase")
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    monkeypatch.setenv("OLYMPUS_OPERATOR_DOMAINS", "mail.corp.com")

    retained = browser.FakeTransport(
        pages={"https://mail.corp.com/": {"title": "Inbox",
                                          "text": "ALICE-PAGE-BODY"}})
    browser.set_transport_factory(lambda: retained)   # same object every build
    browserlease.clear_for_tests()

    sess = browser.session(A)
    sess.open("https://mail.corp.com/")
    sess.set_cookies([{"name": "sid", "value": "ALICE-SESSION",
                       "domain": "mail.corp.com"}])
    before = browserlease.current()
    assert before["owner"] == A and before["lease_id"]

    # Reconfigure. Same retaining transport, so the credentialed state survives.
    browser.set_transport_factory(lambda: retained)

    after = browserlease.current()
    assert after["owner"] == A, "reconfiguration transferred ownership"
    assert after["lease_id"] == before["lease_id"], \
        "reconfiguration replaced the lease generation"
    assert after["claimed_at"] == before["claimed_at"]

    # B is refused BEFORE observing or mutating any retained state.
    _as_user(B)
    operator.authorize_site(B, "mail.corp.com")       # B's own gates are open
    assert operator.enabled(B) and operator.authorized(B, "mail.corp.com")

    with pytest.raises(browserlease.OwnershipRefused):
        browser.session(B)
    for out in (tools._browser_read(), tools._browser_open("https://x.test/"),
                tools._browser_exists("#x"), tools._browser_tabs()):
        assert out == browserlease.REFUSAL
    assert operator.save_auth(B, "mail.corp.com") == browserlease.REFUSAL
    from olympus import vault
    assert vault.get(B, "cookies:mail.corp.com") is None   # nothing harvested
    assert retained.cookies[0]["value"] == "ALICE-SESSION"  # nothing mutated

    # A reconnects and the retained state is still there, still A-only.
    _as_user(A)
    again = browser.session(A)
    assert again.get_cookies("mail.corp.com")[0]["value"] == "ALICE-SESSION"
    assert browserlease.current()["lease_id"] == before["lease_id"]


def test_set_transport_factory_none_also_preserves_ownership():
    """The teardown spelling tests use most often — clearing the factory — must
    behave identically. It is the same hook."""
    _fake()
    _own_as(A)
    before = browserlease.current()

    browser.set_transport_factory(None)

    after = browserlease.current()
    assert after is not None, "clearing the factory released the lease"
    assert after["owner"] == A
    assert after["lease_id"] == before["lease_id"]


def test_clear_for_tests_has_no_production_callers():
    """`clear_for_tests` is the one unconditional lease drop in the codebase.

    Any production path reaching it is a way to transfer a credentialed browser
    without destroying its state — exactly the `set_transport_factory` defect.
    Calls from `tests/` fixtures are fine; calls from `olympus/` are not.
    """
    import ast
    import inspect
    import pathlib

    root = pathlib.Path(inspect.getfile(browserlease)).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else fn.id if isinstance(fn, ast.Name) else "")
            if name != "clear_for_tests":
                continue
            # Its own definition module may not call it either.
            offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "clear_for_tests() is called from production code at: "
        + ", ".join(offenders))


def test_simulated_application_restart_preserves_the_lease():
    """Process globals die on restart; the lease must not. Otherwise the first
    caller after a restart inherits whatever authenticated state the browser
    still holds."""
    _fake()
    _own_as(A)

    # Simulate a fresh process: every browser module global goes back to zero.
    browser._session = None
    browser._launched = None

    with pytest.raises(browserlease.OwnershipRefused):
        browser.session(B)
    assert browserlease.current()["owner"] == A


def test_matching_and_changed_fingerprint_both_preserve_ownership():
    """The fingerprint is diagnostic, never authority. A mismatch must not
    release the lease and must not admit a different owner — reassignment
    happens only through verified destruction."""
    _fake()
    _own_as(A)
    record = browserlease.current()
    record["fingerprint"] = "ws://127.0.0.1:9222/devtools/browser/TOTALLY-DIFFERENT"
    with browserlease._lock():
        browserlease._write(record)

    assert browserlease.current()["owner"] == A
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session(B)
    assert browser.session(A) is not None


def test_no_ttl_or_idle_expiry_transfers_ownership():
    """A lease that lapsed on a timer would hand a credentialed browser to the
    next caller precisely when the owner stopped watching."""
    _fake()
    _own_as(A)
    record = browserlease.current()
    record["claimed_at"] = 0.0                 # epoch: maximally 'stale'
    with browserlease._lock():
        browserlease._write(record)

    with pytest.raises(browserlease.OwnershipRefused):
        browser.session(B)
    assert browserlease.current()["owner"] == A


@pytest.mark.parametrize("corrupt", [
    "",                                   # torn write → present but empty
    "   ",
    "{ not json",
    "[]",                                 # right JSON, wrong shape
    '{"schema_version": 999, "owner": "tg-bob", "transport": "fake"}',
    '{"schema_version": 1, "transport": "fake"}',            # no owner
    '{"schema_version": 1, "owner": "", "transport": "fake"}',
    '{"schema_version": 1, "owner": "tg-bob"}',              # no transport
])
def test_malformed_lease_fails_closed(corrupt):
    """An unreadable lease must never read as 'unowned' — that is the one
    interpretation that hands the browser away."""
    _fake()
    browserlease._path().write_text(corrupt, encoding="utf-8")
    with pytest.raises(browserlease.OwnershipRefused):
        browserlease.current()
    for user in (A, B):
        with pytest.raises(browserlease.OwnershipRefused):
            browser.session(user)
    assert tools._browser_read() == browserlease.REFUSAL


def test_lease_record_is_durable_and_restrictive(tmp_path):
    _fake()
    _own_as(A)
    raw = json.loads(browserlease._path().read_text(encoding="utf-8"))
    assert raw["schema_version"] == browserlease.SCHEMA_VERSION
    assert raw["owner"] == A
    assert raw["transport"] == browser.FAKE
    assert raw["claimed_at"] > 0
    assert "fingerprint" in raw


# --- D. transport release --------------------------------------------------

def test_remote_cdp_refuses_release_and_transfer(monkeypatch):
    """Olympus will not wipe a browser profile it does not own. Reassigning a
    personal browser is an operator action, not an automatic one."""
    _fake()
    _own_as(A)
    record = browserlease.current()
    record["transport"] = browser.REMOTE_CDP
    with browserlease._lock():
        browserlease._write(record)

    out = browser.relinquish(A)
    assert out == browserlease.REASSIGN_NOTICE
    assert browserlease.current()["owner"] == A          # still held
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session(B)


def test_remote_cdp_without_lease_or_configured_owner_refuses_before_attaching(
        monkeypatch):
    """The refusal must land BEFORE `_resolve_page_ws` makes its HTTP request
    to the browser's /json endpoint — inspecting a stranger's targets is
    already a leak, and attaching to pages[0] is how a restart inherits an
    authenticated tab."""
    monkeypatch.setenv("OLYMPUS_BROWSER_CDP_URL", "http://127.0.0.1:59999")
    monkeypatch.delenv(browserlease.OWNER_ENV, raising=False)
    browser.set_transport_factory(None)
    browser._session = None

    attempted = []
    monkeypatch.setattr(browser, "_resolve_page_ws",
                        lambda v: attempted.append(v))
    monkeypatch.setattr(browser, "_build_transport",
                        lambda: attempted.append("built"))

    with pytest.raises(browserlease.OwnershipRefused):
        browser.session(A)
    assert attempted == []                   # nothing was attached or inspected
    assert browserlease.current() is None


def test_remote_cdp_with_configured_owner_may_claim(monkeypatch):
    monkeypatch.setenv("OLYMPUS_BROWSER_CDP_URL", "http://127.0.0.1:59999")
    monkeypatch.setenv(browserlease.OWNER_ENV, A)
    browser._session = None
    built = []
    monkeypatch.setattr(browser, "_build_transport",
                        lambda: built.append(1) or browser.FakeTransport())
    monkeypatch.setattr(browser, "_fingerprint", lambda kind: "")

    assert browser.session(A) is not None
    assert built == [1]
    assert browserlease.current()["owner"] == A

    browser._session = None
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session(B)


def test_playwright_release_requires_verified_destruction(monkeypatch):
    """Playwright is the one transport whose close() genuinely destroys state
    (an ephemeral context, no profile on disk) — but it still has to PROVE it."""
    class StubBrowser:
        def __init__(self): self.connected = True
        def is_connected(self): return self.connected

    class StubTransport:
        def __init__(self, honest=True):
            self._closed = False
            self._browser = StubBrowser()
            self._honest = honest
        def send(self, method, params=None, session_id=None): return {}
        def close(self):
            if self._honest:
                self._closed = True
                self._browser.connected = False

    lying = StubTransport(honest=False)
    browser.set_transport_factory(lambda: lying)
    browser.session(A)
    with browserlease._lock():
        rec = browserlease._read()
        rec["transport"] = browser.PLAYWRIGHT
        browserlease._write(rec)

    out = browser.relinquish(A)
    assert out.startswith("Error: ownership retained")
    assert browserlease.current()["owner"] == A          # NOT released
    with pytest.raises(browserlease.OwnershipRefused):
        browser.session(B)

    # Now an honest teardown: destruction verified, lease released.
    honest = StubTransport(honest=True)
    browser._session = browser.BrowserSession(honest, owner=A)
    assert browser.relinquish(A) == ""
    assert browserlease.current() is None


def test_autolaunch_release_requires_exact_termination_and_deletion(
        monkeypatch, tmp_path):
    """Release must terminate the exact child Olympus started and delete the
    exact mkdtemp profile it created — and verify both."""
    import tempfile
    profile = tmp_path / "olympus-browser-abc"
    profile.mkdir()
    (profile / "Cookies").write_text("sqlite", encoding="utf-8")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    class StubProc:
        def __init__(self): self.alive = True; self.terminated = False
        def poll(self): return None if self.alive else 0
        def terminate(self): self.terminated = True; self.alive = False
        def kill(self): self.alive = False
        def wait(self, timeout=None): return 0

    proc = StubProc()
    _fake()
    browser.session(A)
    with browserlease._lock():
        rec = browserlease._read()
        rec["transport"] = browser.AUTOLAUNCH
        browserlease._write(rec)
    browser._launched = (proc, "http://127.0.0.1:9222", str(profile))

    assert browser.relinquish(A) == ""
    assert proc.terminated is True
    assert not profile.exists()                  # cookies really are gone
    assert browser._launched is None
    assert browserlease.current() is None


def test_autolaunch_release_retains_lease_when_termination_fails(
        monkeypatch, tmp_path):
    import tempfile
    profile = tmp_path / "olympus-browser-stuck"
    profile.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    class WedgedProc:
        def poll(self): return None              # never exits
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): return None

    _fake()
    browser.session(A)
    with browserlease._lock():
        rec = browserlease._read()
        rec["transport"] = browser.AUTOLAUNCH
        browserlease._write(rec)
    browser._launched = (WedgedProc(), "http://127.0.0.1:9222", str(profile))

    out = browser.relinquish(A)
    assert out.startswith("Error: ownership retained")
    assert browserlease.current()["owner"] == A
    assert profile.exists()                      # not deleted behind a live proc
    browser._launched = None


def test_autolaunch_release_retains_lease_when_profile_deletion_fails(
        monkeypatch, tmp_path):
    import tempfile
    profile = tmp_path / "olympus-browser-locked"
    profile.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    class DeadProc:
        def poll(self): return 0
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): return 0

    def boom(path, ignore_errors=False):
        raise OSError("profile is locked")
    monkeypatch.setattr("shutil.rmtree", boom)

    _fake()
    browser.session(A)
    with browserlease._lock():
        rec = browserlease._read()
        rec["transport"] = browser.AUTOLAUNCH
        browserlease._write(rec)
    browser._launched = (DeadProc(), "http://127.0.0.1:9222", str(profile))

    out = browser.relinquish(A)
    assert out.startswith("Error: ownership retained")
    assert browserlease.current()["owner"] == A
    browser._launched = None


def test_relinquish_refuses_a_non_owner():
    _fake()
    _own_as(A)
    with pytest.raises(browserlease.OwnershipRefused):
        browser.relinquish(B)
    assert browserlease.current()["owner"] == A


def test_release_lets_the_next_owner_in():
    """The lease is exclusive, not permanent: after a verified teardown the
    browser is genuinely available again."""
    _fake()
    _own_as(A)
    assert browser.relinquish(A) == ""
    assert browserlease.current() is None
    assert browser.session(B) is not None
    assert browserlease.current()["owner"] == B


# --- E. non-leakage --------------------------------------------------------

def test_denial_leaks_nothing_about_the_current_session(monkeypatch):
    """A refusal must not be usable to probe what the browser is doing or for
    whom. Every seeded distinctive value must be absent from every denial."""
    seeded = ["tg-alice", "mail.corp.com", "Inbox — alice@corp",
              "ALICE-SESSION", "SECRET-INBOX-BODY", "devtools/browser"]
    _fake(pages={"https://mail.corp.com/": {"title": "Inbox — alice@corp",
                                            "text": "SECRET-INBOX-BODY"}},
          targets=[{"type": "page", "targetId": "t1",
                    "title": "Inbox — alice@corp",
                    "url": "https://mail.corp.com/"}],
          present=["#send"])
    sess = _own_as(A)
    sess.set_cookies([{"name": "sid", "value": "ALICE-SESSION",
                       "domain": "mail.corp.com"}])

    _as_user(B)
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    operator.authorize_site(B, "mail.corp.com")

    denials = [
        tools._browser_open("https://x.test/"), tools._browser_read(),
        tools._browser_read_ax(), tools._browser_screenshot(),
        tools._browser_console(), tools._browser_save_pdf(),
        tools._browser_exists("#send"), tools._browser_tabs(),
        tools._browser_switch_tab(0), tools._browser_observe(),
        tools._browser_act("click", selector="#send"),
        tools._browser_checkpoint(), tools._browser_frames(),
        tools._browser_dialog(True), tools._browser_download(),
        tools._browser_save_auth("mail.corp.com"),
        tools._browser_restore_auth("mail.corp.com"),
        operator.save_auth(B, "mail.corp.com"),
        operator.restore_auth(B, "mail.corp.com"),
    ]
    for out in denials:
        low = str(out).lower()
        for secret in seeded:
            assert secret.lower() not in low, (secret, out)
        assert str(browserlease._path()) not in str(out)


def test_refusal_string_is_constant_and_content_free():
    assert "{" not in browserlease.REFUSAL and "%" not in browserlease.REFUSAL
    _fake()
    _own_as(A)
    _as_user(B)
    assert tools._browser_read() == tools._browser_console()
    assert tools._browser_read() == browserlease.REFUSAL


# --- F. coverage: no new ungated call site ---------------------------------

def test_every_production_session_call_site_names_an_owner():
    """Structural guard. The Tier-1 readers were the actual breach precisely
    because a handler could reach `browser.session()` without thinking about
    identity. `session()` now requires the principal positionally, so this test
    fails the moment a new call site forgets it."""
    import ast
    import inspect
    import pathlib

    root = pathlib.Path(inspect.getfile(browser)).parent
    bad = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            # `browser.session(...)` or a bare `session(...)` inside browser.py
            is_session = (
                (isinstance(fn, ast.Attribute) and fn.attr == "session"
                 and isinstance(fn.value, ast.Name) and fn.value.id == "browser")
                or (path.name == "browser.py" and isinstance(fn, ast.Name)
                    and fn.id == "session"))
            if not is_session:
                continue
            named = bool(node.args) or any(k.arg == "owner" for k in node.keywords)
            if not named:
                bad.append(f"{path.name}:{node.lineno}")
    assert not bad, ("browser.session() reached without naming an owner at: "
                     + ", ".join(bad))


def test_session_requires_an_explicit_principal():
    """No default, and specifically no fallback to memory.current_user(): the
    ambient contextvar defaults to 'shared', so an implicit default is how a
    background job would end up owning somebody's logged-in browser."""
    import inspect
    sig = inspect.signature(browser.session)
    assert list(sig.parameters) == ["owner"]
    assert sig.parameters["owner"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        browser.session()


# --- G. same-owner regression ---------------------------------------------

def test_same_owner_keeps_full_use_of_the_browser(monkeypatch):
    """The single-user path — the overwhelmingly common one — is unchanged."""
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    monkeypatch.setenv("OLYMPUS_OPERATOR_DOMAINS", "mail.corp.com")
    _fake(targets=[{"type": "page", "targetId": "t1", "title": "Inbox",
                    "url": "https://mail.corp.com/"}],
          present=["#send"])
    _as_user(A)
    _own_as(A)

    assert "Inbox" in tools._browser_read() or tools._browser_read() != \
        browserlease.REFUSAL
    for out in (tools._browser_read(), tools._browser_read_ax(),
                tools._browser_console(), tools._browser_exists("#send"),
                tools._browser_tabs(), tools._browser_observe()):
        assert out != browserlease.REFUSAL


@pytest.mark.requires_crypto
def test_same_owner_cookie_roundtrip_still_works(monkeypatch):
    from olympus import vault
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "a-test-passphrase")
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    monkeypatch.setenv("OLYMPUS_OPERATOR_DOMAINS", "shop.com")
    _fake()
    sess = _own_as(A)
    sess.set_cookies([{"name": "sid", "value": "xyz", "domain": "shop.com"}])
    assert "Saved the shop.com session" in operator.save_auth(A, "shop.com")

    browser.reset()                                # reconnect, still owned by A
    assert "Restored the shop.com session" in operator.restore_auth(A, "shop.com")
    assert browser.session(A).get_cookies("shop.com")[0]["value"] == "xyz"


def test_background_operator_job_runs_as_its_durable_owner(monkeypatch):
    """`operator.execute` is an action callback: it must lease the browser as
    the job's server-owned `_user`, not as whatever namespace is ambient when
    the heartbeat happens to run it."""
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    monkeypatch.setenv("OLYMPUS_OPERATOR_DOMAINS", "shop.com")
    browser.record_profile("shop.com", login_url="https://shop.com/login")
    browser.set_template("shop.com", "reorder", "notable",
                         [{"op": "click", "selector": "#buy"}])
    _fake(pages={"https://shop.com/": {"title": "Shop", "text": "ok"}},
          present=["#buy"])
    _own_as(A, url="https://shop.com/")

    memory.set_user("shared")                      # heartbeat's ambient context
    out = operator.execute({"domain": "shop.com", "template": "reorder",
                            "params": {}, "_user": A})
    assert isinstance(out, dict)                   # ran as A despite ambient
    assert browserlease.current()["owner"] == A

    # A job belonging to B is refused while A holds the browser.
    with pytest.raises(Exception) as excinfo:
        operator.execute({"domain": "shop.com", "template": "reorder",
                          "params": {}, "_user": B})
    assert browserlease.REFUSAL in str(excinfo.value) or \
        "isn't an authorized site" in str(excinfo.value)
