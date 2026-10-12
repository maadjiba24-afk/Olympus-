"""HERMES operator, Phase 1: site profiles + structured predicates + vault-backed
login, all behind the OLYMPUS_OPERATOR master switch and deny-first gates.

These tests pin the safety properties from docs/DESIGN_OPERATOR.md:
- the operator is off by default and fails closed at every missing gate;
- capability separation still holds (the actuator never coexists with web
  ingestion);
- credentials come from the vault and the password never appears in output;
- site profiles carry provenance + a reliability score.
"""

import hashlib
import re
import socket

import pytest

from olympus import browser, config, security, tools, vault
from olympus.specialists import SPECIALISTS


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    browser.set_transport_factory(None)
    # The browser is leased to an AUTHENTICATED principal (browserlease): these
    # tests run as the local terminal owner. The ambient default ("shared") is
    # the heartbeat's namespace and is refused by design.
    from olympus import browserlease, memory
    memory.set_user("cli")
    browserlease.clear_for_tests()
    yield
    browserlease.clear_for_tests()
    memory.set_user("shared")
    browser.set_transport_factory(None)


def _enable(monkeypatch, domain="example.com"):
    monkeypatch.setenv("OLYMPUS_OPERATOR", "1")
    monkeypatch.setenv("OLYMPUS_OPERATOR_DOMAINS", domain)
    # egress allowlist gate is a no-op unless sovereign mode is on; keep it off.


# --- capability separation: HERMES holds the actuator, Argus never does ----

def test_hermes_holds_login_but_argus_does_not():
    hermes = {d.get("name") for d in SPECIALISTS["hermes"].tool_defs("anthropic")}
    argus = {d.get("name") for d in SPECIALISTS["argus"].tool_defs("anthropic")}
    assert "browser_login" in hermes               # non-ingesting → keeps actuator
    assert "browser_open" not in hermes and "browser_read" not in hermes
    assert "browser_login" not in argus            # ingesting → no actuator


def test_browser_login_is_an_action_tool_and_not_ingestion():
    assert "browser_login" in security.ACTION_TOOLS
    assert not security.should_wrap("browser_exists")   # bool predicate, not prose


# --- deny-first: every missing gate fails closed --------------------------

def test_login_refused_when_operator_disabled(owned_operator_vault, monkeypatch):
    _, site = owned_operator_vault
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('dispatch')
        pytest.fail('an unauthorized login reached vault or transport')
    monkeypatch.setattr(vault, 'snapshots', forbidden)
    browser.set_transport_factory(forbidden)
    assert tools._browser_login(site) == 'Error: the site is not authorized for this operation.'
    assert calls == []


def test_login_refused_when_domain_not_authorized(owned_operator_vault, monkeypatch):
    _, site = owned_operator_vault
    monkeypatch.setenv('OLYMPUS_OPERATOR', '1')
    monkeypatch.setenv('OLYMPUS_OPERATOR_DOMAINS', 'other.test')
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('dispatch')
        pytest.fail('an unauthorized domain reached vault or transport')
    monkeypatch.setattr(vault, 'snapshots', forbidden)
    browser.set_transport_factory(forbidden)
    assert tools._browser_login(site) == 'Error: the site is not authorized for this operation.'
    assert calls == []


def test_login_refused_without_a_profile(monkeypatch):
    _enable(monkeypatch)
    assert "sign-in recipe" in tools._browser_login("example.com").lower()


def test_login_refused_without_vault_credentials(owned_operator_vault, monkeypatch):
    _, site = owned_operator_vault
    _owned_login_profile(monkeypatch, site)
    calls = []
    def forbidden():
        calls.append('factory')
        pytest.fail('absent credentials reached the transport')
    browser.set_transport_factory(forbidden)
    out = tools._browser_login(site)
    assert out == f'Error: no saved credentials are available for {site}.'
    assert calls == []
    batch = vault.snapshots('cli', [f'site:{site}', f'cookies:{site}'])
    assert all(snapshot.value is None for snapshot in batch.values())


# --- the happy path: vaulted login, password never surfaced ---------------

def test_login_succeeds_and_hides_password(owned_operator_vault, monkeypatch):
    root, site = owned_operator_vault
    _owned_login_profile(monkeypatch, site)
    resolutions = _owned_resolver(monkeypatch, site)
    credentials = {'username': 'owned-user', 'password': _OWNED_PASSWORD}
    vault.put('cli', f'site:{site}', credentials)
    fake = browser.FakeTransport(present=['#user', '#pass', '#go', '#account'])
    fake.cookies = [{'name': 'sid', 'value': _OWNED_COOKIE, 'domain': site}]
    factories = []
    def factory():
        factories.append('factory')
        return fake
    browser.set_transport_factory(factory)
    out = tools._browser_login(site)
    facts = _owned_workflow_status(out, site, True)
    assert facts['cookies'] == facts['vault'] == 'confirmed'
    assert factories == ['factory'] and resolutions
    assert fake.filled == {'#user': 'set', '#pass': 'set'}
    assert [call['params']['url'] for call in fake.calls if call['method'] == 'Page.navigate'] == [f'https://{site}/login']
    assert sum(call['method'] == 'Storage.getCookies' for call in fake.calls) == 1
    _, entries = _owned_vault_image(root)
    assert entries == {f'site:{site}': credentials, f'cookies:{site}': {'cookies': fake.cookies}}
    assert browser.get_profile(site).reliability == 1.0


def test_login_reports_blocked_on_missing_success_marker(owned_operator_vault, monkeypatch):
    root, site = owned_operator_vault
    _owned_login_profile(monkeypatch, site)
    resolutions = _owned_resolver(monkeypatch, site)
    credentials = {'username': 'owned-user', 'password': _OWNED_PASSWORD}
    vault.put('cli', f'site:{site}', credentials)
    before, _ = _owned_vault_image(root)
    fake = browser.FakeTransport(present=['#user', '#pass', '#go'])
    fake.cookies = [{'name': 'sid', 'value': _OWNED_COOKIE, 'domain': site}]
    factories = []
    def factory():
        factories.append('factory')
        return fake
    browser.set_transport_factory(factory)
    out = tools._browser_login(site)
    facts = _owned_workflow_status(out, site, False)
    assert facts['cookies'] == facts['vault'] == 'not_attempted'
    assert factories == ['factory'] and resolutions
    assert fake.filled == {'#user': 'set', '#pass': 'set'}
    assert [call['params']['url'] for call in fake.calls if call['method'] == 'Page.navigate'] == [f'https://{site}/login']
    assert not any(call['method'] == 'Storage.getCookies' for call in fake.calls)
    after, entries = _owned_vault_image(root)
    assert after == before and entries == {f'site:{site}': credentials}
    assert browser.get_profile(site).reliability == 0.0


# --- structured predicate + profile provenance ----------------------------

def test_browser_exists_returns_yes_no(monkeypatch):
    browser.set_transport_factory(lambda: browser.FakeTransport(present=["#x"]))
    assert tools._browser_exists("#x") == "yes"
    assert tools._browser_exists("#missing") == "no"


def test_site_profile_has_provenance_and_score():
    p = browser.record_profile("shop.com", login_url="http://shop.com/in",
                               success_selector="#me")
    assert p.content_hash.startswith("sha256:")
    assert p.created and p.reliability == 0.0
    listing = tools._site_profiles("shop.com")
    assert "shop.com" in listing and "reliability" in listing


def test_profile_outcomes_drive_reliability():
    browser.record_profile("s.com", login_url="x")
    browser.mark_profile_outcome("s.com", True)
    browser.mark_profile_outcome("s.com", False)
    assert browser.get_profile("s.com").reliability == 0.5


def test_operator_tools_are_threat_modeled():
    from olympus import threatmodel
    documented = threatmodel.documented_tools(
        threatmodel.doc_path().read_text(encoding="utf-8"))
    for name in ("browser_login", "browser_exists", "site_profile_record",
                 "site_profiles"):
        assert name in tools.HANDLERS and name in documented
    assert threatmodel.check_repo() == []


_OWNED_KEY = 'owned-operator-fixture-key'
_OWNED_PASSWORD = 'owned-operator-fixture-password'
_OWNED_COOKIE = 'owned-operator-fixture-cookie'


@pytest.fixture
def owned_operator_vault(_isolate, tmp_path, monkeypatch, request):
    from olympus import store
    for name in ('OLYMPUS_SECRET_KEY_FILE', 'OLYMPUS_DATABASE_URL',
                 'OLYMPUS_OPERATOR', 'OLYMPUS_OPERATOR_DOMAINS',
                 'OLYMPUS_SOVEREIGN', 'OLYMPUS_EGRESS_ALLOWLIST'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', _OWNED_KEY)
    monkeypatch.setenv('OLYMPUS_ENV', 'test')
    monkeypatch.setattr(store, '_backend', store.FileStore())
    site = 'owned-' + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16] + '.test'
    return tmp_path, site


def _owned_vault_image(root):
    from olympus import store
    with vault.storage.transaction(store.FileStore(), root) as transaction:
        blob = transaction.get(vault._NS, vault._key('cli'), cap=vault._ROW_CAP)
    assert type(blob) is bytes and blob
    assert all(secret.encode() not in blob for secret in
               (_OWNED_KEY, _OWNED_PASSWORD, _OWNED_COOKIE))
    entries, metadata = vault.records.decode('cli', blob, _OWNED_KEY.encode())
    assert metadata.context_authenticated
    return blob, entries


def _owned_login_profile(monkeypatch, site):
    _enable(monkeypatch, site)
    browser.record_profile(site, login_url=f'https://{site}/login',
                           username_selector='#user', password_selector='#pass',
                           submit_selector='#go', success_selector='#account')


def _owned_resolver(monkeypatch, site):
    resolutions = []
    def resolve(host, port, family=0, type=0, proto=0, flags=0):
        assert host == site and port == 443
        assert family in (0, socket.AF_INET) and type in (0, socket.SOCK_STREAM)
        assert proto in (0, socket.IPPROTO_TCP) and flags == 0
        resolutions.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                 '', ('93.184.216.34', port))]
    monkeypatch.setattr(security.socket, 'getaddrinfo', resolve)
    return resolutions


def _owned_workflow_status(message, site, expected):
    from olympus import operator
    match = re.match(r'^Credential workflow ([0-9a-f]{32}) for ', message)
    assert match is not None
    facts = operator.auth_workflow_status('cli', match.group(1))
    assert facts['domain'] == site and facts['operation'] == 'login'
    assert facts['login_result'] is expected and facts['complete'] is True
    assert facts['busy'] is False and facts['cleanup_unconfirmed'] is False
    assert all(facts[name] == 'confirmed' for name in ('factory', 'login', 'telemetry'))
    assert all(secret not in message and secret not in repr(facts)
               for secret in (_OWNED_KEY, _OWNED_PASSWORD, _OWNED_COOKIE))
    assert ('login=succeeded' if expected else 'login=failed') in message
    return facts
