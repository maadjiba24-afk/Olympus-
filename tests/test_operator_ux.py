"""Conversational operator setup — a non-engineer authorizes sites by asking,
never by touching env vars or a CLI. Per-user, persisted in prefs.
"""

import hashlib

import pytest

from olympus import (actions, browser, builtin_actions, config, memory,  # noqa: F401
                     operator, security, tools)
from olympus.specialists import SPECIALISTS

builtin_actions.register_builtins()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    # The browser is leased to an AUTHENTICATED principal (browserlease).
    # These tests run as the local terminal owner; the ambient default
    # ("shared", the heartbeat namespace) is refused by design.
    memory.set_user("cli")
    from olympus import browserlease
    browserlease.clear_for_tests()
    # No OLYMPUS_OPERATOR / _DOMAINS env — prove it works without them.
    monkeypatch.delenv("OLYMPUS_OPERATOR", raising=False)
    monkeypatch.delenv("OLYMPUS_OPERATOR_DOMAINS", raising=False)
    browser.set_transport_factory(None)
    yield
    browserlease.clear_for_tests()
    memory.set_user("shared")
    browser.set_transport_factory(None)


# --- off by default, friendly refusals ------------------------------------

def test_operator_off_by_default(monkeypatch):
    from olympus import vault
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('dispatch')
        pytest.fail('disabled operator reached vault or transport')
    monkeypatch.setattr(vault, 'snapshots', forbidden)
    browser.set_transport_factory(forbidden)
    assert operator.enabled('cli') is False
    assert tools._browser_login('off-by-default.test') == 'Error: the site is not authorized for this operation.'
    assert calls == []
    assert 'off' in tools._operator_status().lower()


# --- conversational authorize turns it on (no env, no CLI) -----------------

def test_authorize_manual_enables_and_persists(monkeypatch):
    out = tools._operator_authorize_site("Amazon.com", "manual")
    assert "set up" in out.lower() and "sign in yourself" in out.lower()
    assert operator.enabled("cli") is True
    assert operator.authorized("cli", "amazon.com") is True
    assert operator.login_mode("cli", "amazon.com") == "manual"
    # manual sign-in never asks for a password — it points the user to log in
    assert "manual sign-in" in tools._browser_login("amazon.com").lower()


def test_status_lists_authorized_sites():
    tools._operator_authorize_site("amazon.com", "manual")
    tools._operator_authorize_site("github.com", "manual")
    s = tools._operator_status()
    assert "amazon.com" in s and "github.com" in s


def test_forget_site_revokes(monkeypatch):
    tools._operator_authorize_site("amazon.com", "manual")
    assert operator.authorized("cli", "amazon.com")
    out = tools._operator_forget_site("amazon.com")
    assert "removed" in out.lower()
    assert operator.authorized("cli", "amazon.com") is False


def test_authorization_is_per_user():
    operator.authorize_site("alice", "amazon.com", "manual")
    assert operator.authorized("alice", "amazon.com") is True
    assert operator.authorized("bob", "amazon.com") is False     # not shared


# --- end to end with NO env: authorize + operate auto-runs within policy ---

def test_operate_works_without_env_after_authorize(monkeypatch):
    monkeypatch.setattr(security, "url_block_reason", lambda u: None)
    user = memory.current_user()
    tools._operator_authorize_site("shop.com", "manual")          # conversational
    actions.set_autonomy(user, actions.L4_STANDING)
    actions.grant_scope(user, operator.OPERATE_SCOPE)
    browser.set_template("shop.com", "buy", "notable",
                         [{"op": "click", "selector": "#buy"}],
                         success_selector="#done")
    browser.set_transport_factory(
        lambda: browser.FakeTransport(present=["#buy", "#done"]))
    out = tools._browser_operate("shop.com", "buy")
    assert "Executed 'buy' on shop.com" in out


# --- 'remember' mode stores creds in the vault, switches login mode --------

def test_remember_credentials_uses_vault(owned_operator_capture):
    from olympus import prefs, securecapture, store, vault
    root, owner, site = owned_operator_capture
    assert operator.enabled(owner) is False
    request_id = securecapture.request(owner, site)
    ready = securecapture.begin(owner)
    assert ready['request_id'] == request_id and ready['state'] == 'ready'
    assert ready['claim'] == 'confirmed' and ready['prompt_dispatched'] is False
    assert operator.enabled(owner) is False
    prompting = securecapture.start_prompt(owner, ready['workflow_id'])
    assert prompting['state'] == 'prompting' and prompting['prompt_dispatched'] is True
    assert operator.enabled(owner) is False
    facts = operator.remember_credentials(owner, site, 'owned-user', _CAPTURE_PASSWORD,
                                          workflow_id=ready['workflow_id'])
    assert facts == securecapture.status(owner, ready['workflow_id'])
    assert facts['state'] == 'complete' and facts['complete'] is True
    assert facts['busy'] is False and facts['cleanup_unconfirmed'] is False
    assert all(facts[name] == 'confirmed' for name in ('claim', 'saving', 'vault', 'authorization'))
    assert prefs.get(owner, 'pending_secure_login') is None
    assert operator.enabled(owner) is True and operator.login_mode(owner, site) == 'remember'
    with vault.storage.transaction(store.FileStore(), root) as transaction:
        blob = transaction.get(vault._NS, vault._key(owner), cap=vault._ROW_CAP)
    assert type(blob) is bytes and blob
    assert all(secret.encode() not in blob for secret in (_CAPTURE_KEY, _CAPTURE_PASSWORD))
    entries, metadata = vault.records.decode(owner, blob, _CAPTURE_KEY.encode())
    assert metadata.context_authenticated
    assert entries == {f'site:{site}': {'username': 'owned-user', 'password': _CAPTURE_PASSWORD}}
    assert all(secret not in repr(facts) for secret in (_CAPTURE_KEY, _CAPTURE_PASSWORD))


# --- the new tools stay bound to the threat model -------------------------

def test_onboarding_tools_threat_modeled():
    from olympus import threatmodel
    documented = threatmodel.documented_tools(
        threatmodel.doc_path().read_text(encoding="utf-8"))
    for name in ("operator_authorize_site", "operator_forget_site",
                 "operator_status"):
        assert name in tools.HANDLERS and name in documented
    assert threatmodel.check_repo() == []


def test_hermes_has_onboarding_tools():
    names = {d.get("name") for d in SPECIALISTS["hermes"].tool_defs("anthropic")}
    assert {"operator_authorize_site", "operator_forget_site",
            "operator_status"} <= names


_CAPTURE_KEY = 'owned-operator-capture-fixture-key'
_CAPTURE_PASSWORD = 'owned-operator-capture-fixture-password'


@pytest.fixture
def owned_operator_capture(_isolate, tmp_path, monkeypatch):
    from olympus import store
    for name in ('OLYMPUS_SECRET_KEY_FILE', 'OLYMPUS_DATABASE_URL',
                 'OLYMPUS_SOVEREIGN', 'OLYMPUS_EGRESS_ALLOWLIST'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', _CAPTURE_KEY)
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setattr(store, '_backend', store.FileStore())
    owner = 'owned-operator-' + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16]
    return tmp_path, owner, 'owned-capture.test'


def test_remember_credentials_requires_prepared_prompt(owned_operator_capture):
    from olympus import securecapture
    root, owner, site = owned_operator_capture
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises(securecapture.SecureCaptureError) as caught:
        operator.remember_credentials(owner, site, 'owned-user', _CAPTURE_PASSWORD)
    assert _CAPTURE_PASSWORD not in str(caught.value) and _CAPTURE_KEY not in str(caught.value)
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
    assert operator.enabled(owner) is False
