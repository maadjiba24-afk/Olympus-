"""Shared channel base: access control (untrusted-by-default), rate limiting,
pairing, and the routing flow — the security spine every channel reuses."""

import pytest

from olympus import chanbase, gateway, pairing


# --- access: untrusted by default ------------------------------------------

def test_default_policy_is_pairing_and_denies(monkeypatch):
    monkeypatch.delenv("OLYMPUS_MATRIX_DM_POLICY", raising=False)
    monkeypatch.delenv("OLYMPUS_CHANNEL_DM_POLICY", raising=False)
    assert chanbase.policy("matrix") == "pairing"
    assert chanbase.allowed("matrix", "@stranger:server") is False


def test_pairing_grants_access(monkeypatch):
    monkeypatch.delenv("OLYMPUS_MATRIX_ALLOWED_IDS", raising=False)
    code = pairing.issue_code("matrix")
    pairing.redeem("matrix", code, "@me:server")
    assert chanbase.allowed("matrix", "@me:server") is True
    assert chanbase.allowed("matrix", "@other:server") is False


def test_env_allowlist_and_notify_id(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MATRIX_ALLOWED_IDS", "@a:s, @b:s")
    monkeypatch.setenv("OLYMPUS_MATRIX_NOTIFY_ID", "@owner:s")
    for who in ("@a:s", "@b:s", "@owner:s"):
        assert chanbase.allowed("matrix", who)
    assert not chanbase.allowed("matrix", "@c:s")


def test_open_policy(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MATRIX_DM_POLICY", "open")
    assert chanbase.allowed("matrix", "anyone")


def test_per_channel_policy_overrides_global(monkeypatch):
    monkeypatch.setenv("OLYMPUS_CHANNEL_DM_POLICY", "open")
    monkeypatch.setenv("OLYMPUS_MATRIX_DM_POLICY", "allowlist")
    assert chanbase.policy("matrix") == "allowlist"
    assert chanbase.policy("mattermost") == "open"      # inherits global


# --- rate limiting ----------------------------------------------------------

def test_rate_limiter_blocks_over_limit():
    lim = chanbase.RateLimiter(per_minute=3)
    assert [lim.hit("s") for _ in range(4)] == [False, False, False, True]
    assert lim.hit("other") is False        # per-sender, independent


def test_rate_limiter_disabled_when_zero():
    lim = chanbase.RateLimiter(per_minute=0)
    assert all(lim.hit("s") is False for _ in range(100))


# --- pairing command --------------------------------------------------------

def test_try_pair_redeems(monkeypatch):
    code = pairing.issue_code("matrix")
    out = chanbase.try_pair("matrix", "@x:s", f"/pair {code}")
    assert "Paired" in out and pairing.is_paired("matrix", "@x:s")


def test_try_pair_bad_code():
    assert "didn't match" in chanbase.try_pair("matrix", "@x:s", "/pair NOPE")


def test_try_pair_non_command_returns_none():
    assert chanbase.try_pair("matrix", "@x:s", "hello there") is None


# --- the routing flow -------------------------------------------------------

def test_route_denies_stranger_once(monkeypatch):
    monkeypatch.delenv("OLYMPUS_MATRIX_ALLOWED_IDS", raising=False)
    chanbase._DENIED_AT.clear()
    sent = []
    ok = chanbase.route("matrix", "mx", "@stranger:s", "hi", {}, sent.append)
    ok2 = chanbase.route("matrix", "mx", "@stranger:s", "hi again", {},
                         sent.append)
    assert ok is False and ok2 is False
    assert len(sent) == 1 and "private" in sent[0]      # hint once, not twice


def test_route_delivers_for_allowed(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MATRIX_DM_POLICY", "open")
    monkeypatch.setattr(gateway, "reply_for",
                        lambda bots, key, text, prefix="ol", uid=None: [f"re:{text}"])
    sent = []
    ok = chanbase.route("matrix", "mx", "@a:s", "question", {}, sent.append)
    assert ok is True and sent == ["re:question"]


def test_route_rate_limits(monkeypatch):
    monkeypatch.setenv("OLYMPUS_MATRIX_DM_POLICY", "open")
    monkeypatch.setattr(gateway, "reply_for",
                        lambda *a, **k: ["ok"])
    lim = chanbase.RateLimiter(per_minute=1)
    sent = []
    chanbase.route("matrix", "mx", "@a:s", "1", {}, sent.append, limiter=lim)
    chanbase.route("matrix", "mx", "@a:s", "2", {}, sent.append, limiter=lim)
    assert "faster than I can safely run" in sent[-1]


def test_route_pair_command_bypasses_access(monkeypatch):
    # /pair must work even before the sender is allowed.
    code = pairing.issue_code("matrix")
    sent = []
    ok = chanbase.route("matrix", "mx", "@new:s", f"/pair {code}", {},
                        sent.append)
    assert ok and pairing.is_paired("matrix", "@new:s")


def test_access_never_implies_authority():
    # Being allowlisted grants ACCESS only; authority lives in the capability
    # profile / autonomy spine, which the channel base never touches. This is
    # the separation the surveyed gateway's allowlist-grants-owner CVE lacked. Verify by
    # AST: the module calls no authority-granting function.
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(chanbase))
    banned = {"set_autonomy", "grant_scope", "grant", "set_advanced_mode"}
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not (calls & banned), f"channel base must not grant authority: {calls & banned}"
    # and it must not import the authority modules
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    names = {a.name for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "actions" not in names and "capprofile" not in names
