"""The chat cost controls must actually bind.

Three defects, all pre-existing, all on the path that spends the OPERATOR's key:

1. **The quota identity was caller-chosen.** `_principal` falls back to
   `_user_for(sid)` = ``web-<sid>`` whenever `accounts.require_login()` is false
   (the default), and the sid comes from the caller's own cookie
   (`_resolve_sid`). Clearing the cookie minted a brand-new identity with a
   brand-new `OLYMPUS_FREE_CHATS` allowance, so a keyless visitor could spend the
   operator's key without any limit at all — on a single node, no replicas
   needed. The ceiling has to be keyed on something the caller cannot mint.
2. **The free allowance was check-then-act.** `_daily_count("free:"+user)` and
   `_daily_bump("free:"+user)` were separate operations with the whole key wall
   between them, so N concurrent requests all read the same pre-charge count and
   every one of them was granted a free chat.
3. **Quota was charged before validation could still fail.** The code comment
   said "a rejected request must not burn one of the user's OLYMPUS_DAILY_CHATS",
   but `settings.validate()` ran *after* the charge, so a 400 still cost a chat.

Plus: both `/v1` dialects returned before the per-IP limiter, leaving the most
expensive endpoint in the process as the only one with no throttle.

These are offline unit tests against the limiter/quota helpers — no server, no
model.
"""

from __future__ import annotations

import pytest

from olympus import config, web


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Each test gets empty counters and a known allowance."""
    monkeypatch.setattr(web, "_DAILY", {})
    monkeypatch.setattr(web, "_HITS", {})
    monkeypatch.setenv("OLYMPUS_FREE_CHATS", "3")
    monkeypatch.delenv("OLYMPUS_FREE_CHATS_PER_IP", raising=False)


# --- 1. the caller cannot mint a fresh operator-funded allowance -----------

def test_the_free_allowance_is_capped_per_ip_not_only_per_cookie():
    """The regression: burn the allowance under many minted identities from one
    address and the ceiling must eventually refuse. Before the fix this loop ran
    forever — every new cookie was a new `web-<sid>` with a full allowance."""
    ip = "203.0.113.9"
    cap = config.free_chats_per_ip()
    assert cap > 0, "a derived ceiling must exist by default"

    granted = 0
    for i in range(cap * 5):                       # far more cookies than the cap
        if web._daily_count("ipfree:" + ip) >= cap:
            break
        web._daily_limited(f"free:web-sid{i}", config.free_chats())
        web._daily_bump("ipfree:" + ip)
        granted += 1
    assert granted == cap, (
        f"minted {granted} free chats from one IP; the ceiling is {cap}")


def test_the_per_ip_ceiling_defaults_to_a_multiple_of_the_per_user_allowance(
        monkeypatch):
    monkeypatch.setenv("OLYMPUS_FREE_CHATS", "5")
    assert config.free_chats_per_ip() == 15        # room for NAT, not unlimited


def test_the_per_ip_ceiling_is_explicitly_overridable(monkeypatch):
    monkeypatch.setenv("OLYMPUS_FREE_CHATS_PER_IP", "2")
    assert config.free_chats_per_ip() == 2
    monkeypatch.setenv("OLYMPUS_FREE_CHATS_PER_IP", "0")
    assert config.free_chats_per_ip() == 0         # <=0 disables, like the rest
    monkeypatch.setenv("OLYMPUS_FREE_CHATS_PER_IP", "not-a-number")
    assert config.free_chats_per_ip() == config.free_chats() * 3   # fails safe


def test_no_free_allowance_means_no_ip_ceiling_to_enforce(monkeypatch):
    """With OLYMPUS_FREE_CHATS unset there is no operator-funded chat at all, so
    the derived ceiling is 0 and the check is inert — it must not accidentally
    block a BYOK-only instance."""
    monkeypatch.delenv("OLYMPUS_FREE_CHATS", raising=False)
    assert config.free_chats() == 0
    assert config.free_chats_per_ip() == 0


# --- 2. the free allowance is consumed atomically -------------------------

def test_free_allowance_is_check_and_consume_in_one_step():
    """`_daily_limited` must decide AND charge under one lock: three calls at an
    allowance of 3 succeed, the fourth is refused, and the refusal does not
    inflate the counter."""
    user = "web-abc"
    assert [web._daily_limited("free:" + user, 3) for _ in range(4)] == \
        [False, False, False, True]
    assert web._daily_count("free:" + user) == 3     # refusal did not increment


def test_concurrent_consumers_cannot_all_win(monkeypatch):
    """The TOCTOU. Under check-then-act every thread read the same pre-charge
    count; with an atomic consume exactly `limit` callers can win."""
    import threading
    user, limit, n = "web-race", 3, 40
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def attempt():
        barrier.wait()
        refused = web._daily_limited("free:" + user, limit)
        with lock:
            results.append(refused)

    threads = [threading.Thread(target=attempt) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(False) == limit, (
        f"{results.count(False)} callers got a free chat; the allowance is {limit}")
    assert web._daily_count("free:" + user) == limit


# --- 3. validation happens before the charge ------------------------------

def test_validation_runs_before_the_quota_charge():
    """Source-level guard for the ordering. `settings.validate()` must appear
    BEFORE `_daily_limited("u:" ...)` in the /api/chat path — it used to run
    after, so a 400 still burned one of the user's daily chats despite the
    comment promising it would not."""
    import inspect
    src = inspect.getsource(web.Handler.do_POST)
    validate_at = src.index("error = settings.validate()")
    charge_at = src.index('_daily_limited("u:" + user')
    assert validate_at < charge_at, (
        "the daily quota is charged before settings.validate() can reject the "
        "request — a 400 would burn a chat")


# --- 4. /v1 is throttled at all -------------------------------------------

def test_v1_dialects_are_rate_limited():
    """Both /v1 dialects used to return before the limiter, so the most
    expensive endpoint had no throttle."""
    import inspect
    src = inspect.getsource(web.Handler.do_POST)
    guard = src.index('_rate_limited("v1:"')
    for dispatch in ('self._handle_v1_chat()', 'self._handle_v1_messages()'):
        assert guard < src.index(dispatch), f"{dispatch} runs before the limiter"


def test_the_v1_budget_is_looser_than_the_browser_chat_budget():
    """A programmatic client bursts — the compatibility campaign alone issues
    ~40 requests in seconds. Reusing the 8/min browser budget would refuse
    legitimate use, which is why this is a separate knob."""
    assert web._v1_limit() > web._chat_limit()
    assert web._v1_limit() >= 40


def test_v1_limit_is_overridable_and_fails_safe(monkeypatch):
    monkeypatch.setenv("OLYMPUS_V1_RATE_LIMIT", "7")
    assert web._v1_limit() == 7
    monkeypatch.setenv("OLYMPUS_V1_RATE_LIMIT", "garbage")
    assert web._v1_limit() == 120                  # default, not a crash


def test_the_limiter_still_treats_a_non_positive_limit_as_unlimited():
    """Existing contract used by the other limiters — do not regress it."""
    assert web._rate_limited("k", 0) is False
    assert web._daily_limited("k", 0) is False
