"""Phase 3: operator-editable configuration (opconfig + panel wiring).

The invariants that matter: strict allowlist (dangerous keys unreachable),
secrets go to the encrypted vault and NEVER to disk in the clear, boot
hydration restores them with env-wins precedence, restart-required honesty,
and the panel routes enforce the same gates as every other mutation."""

import json
import os
import pathlib

import pytest

from olympus import adminpanel, connectors, firstrun, opconfig, security, vault


@pytest.fixture(autouse=True)
def isolated_config_env(tmp_path, monkeypatch):
    """Never touch the real ~/.olympus/config.env."""
    monkeypatch.setattr(firstrun, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(firstrun, "CONFIG_ENV", tmp_path / "config.env")
    yield


@pytest.fixture()
def secret_key(monkeypatch):
    monkeypatch.setenv("OLYMPUS_SECRET_KEY", "test-master-pass")


# --- allowlist + validation ---------------------------------------------------

def test_only_allowlisted_keys_are_settable():
    for key in ("OLYMPUS_PLUGINS_DIR", "OLYMPUS_EXEC_BACKEND",
                "OLYMPUS_MEMORY_DIR", "PATH", "OLYMPUS_ACCESS_TOKEN",
                "OLYMPUS_API_KEYS", "LD_PRELOAD"):
        with pytest.raises(ValueError, match="not operator-configurable"):
            opconfig.set_value(key, "x")


def test_validation_refusals():
    cases = [("OLYMPUS_PROVIDER", "gemini"),
             ("OLYMPUS_CACHE_TTL", "2d"),
             ("SMTP_PORT", "not-a-port"),
             ("OLYMPUS_MODELS", "{}"),
             ("OLYMPUS_MODELS", '[{"provider": "x"}]'),
             ("OLYMPUS_MODELS", '[{"provider": "openai", "evil": 1}]'),
             ("OLYMPUS_BASE_URL", "ftp://x"),
             ("OLYMPUS_BASE_URL", "http://remote.example.com/v1"),
             ("OLYMPUS_WEBHOOKS", "bad entry no equals"),
             ("TELEGRAM_BOT_TOKEN", "   ")]
    for key, value in cases:
        with pytest.raises(ValueError):
            opconfig.set_value(key, value)


def test_http_base_url_allowed_for_localhost(monkeypatch, secret_key):
    msg = opconfig.set_value("OLYMPUS_BASE_URL", "http://localhost:11434/v1")
    assert "config.env" in msg
    assert os.environ["OLYMPUS_BASE_URL"] == "http://localhost:11434/v1"


# --- storage: config.env vs vault ---------------------------------------------

def test_non_secret_goes_to_config_env(monkeypatch):
    msg = opconfig.set_value("SLACK_NOTIFY_CHANNEL", "#ops")
    assert "config.env" in msg
    assert "SLACK_NOTIFY_CHANNEL=#ops" in firstrun.CONFIG_ENV.read_text()
    assert os.environ["SLACK_NOTIFY_CHANNEL"] == "#ops"


def test_non_secret_cannot_inject_an_unallowlisted_record():
    with pytest.raises(ValueError, match="newline|one line"):
        opconfig.set_value(
            "SLACK_NOTIFY_CHANNEL",
            "#ops\nOLYMPUS_EXEC_SECURITY=off")
    on_disk = firstrun.CONFIG_ENV.read_text() \
        if firstrun.CONFIG_ENV.exists() else ""
    assert "OLYMPUS_EXEC_SECURITY" not in on_disk


@pytest.mark.requires_crypto
def test_secret_goes_to_vault_never_plaintext(secret_key):
    opconfig.set_value("WHATSAPP_ACCESS_TOKEN", "wa-secret-9911")
    on_disk = firstrun.CONFIG_ENV.read_text() \
        if firstrun.CONFIG_ENV.exists() else ""
    assert "wa-secret-9911" not in on_disk
    assert vault.get("operator", "config:WHATSAPP_ACCESS_TOKEN") == "wa-secret-9911"
    # ...and nothing under MEMORY_DIR holds it in the clear either.
    from olympus import config
    for p in pathlib.Path(config.MEMORY_DIR).rglob("*"):
        if p.is_file():
            assert b"wa-secret-9911" not in p.read_bytes(), p


@pytest.mark.requires_crypto
def test_named_webhook_urls_are_vaulted_and_masked(secret_key):
    value = "ops=https://hooks.example.com/private-bearer-path"
    opconfig.set_value("OLYMPUS_WEBHOOKS", value)
    on_disk = firstrun.CONFIG_ENV.read_text() \
        if firstrun.CONFIG_ENV.exists() else ""
    assert value not in on_disk
    assert vault.get("operator", "config:OLYMPUS_WEBHOOKS") == value
    row = next(r for r in opconfig.status()
               if r["key"] == "OLYMPUS_WEBHOOKS")
    assert row["secret"] is True and row["value"] is None


def test_secret_without_vault_key_fails_closed(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)
    with pytest.raises(ValueError, match="OLYMPUS_SECRET_KEY"):
        opconfig.set_value("SMTP_PASS", "hunter2-longer")
    on_disk = firstrun.CONFIG_ENV.read_text() \
        if firstrun.CONFIG_ENV.exists() else ""
    assert "hunter2" not in on_disk           # no silent plaintext fallback


@pytest.mark.requires_crypto
def test_secret_set_removes_stale_plaintext_copy(secret_key):
    # The setup wizard may have written the token to config.env; storing it
    # from the panel must move ownership to the vault.
    firstrun.save_env_value("TELEGRAM_BOT_TOKEN", "old-plain-token")
    opconfig.set_value("TELEGRAM_BOT_TOKEN", "new-vault-token")
    assert "old-plain-token" not in firstrun.CONFIG_ENV.read_text()
    assert "TELEGRAM_BOT_TOKEN" not in firstrun._read_saved()


# --- boot hydration + precedence ------------------------------------------------

@pytest.mark.requires_crypto
def test_apply_secrets_hydrates_and_env_wins(monkeypatch, secret_key):
    opconfig.set_value("SLACK_BOT_TOKEN", "xoxb-fromvault")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert opconfig.apply_secrets() == 1
    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-fromvault"
    # A real env var wins over the vault at the next hydration.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fromenv")
    assert opconfig.apply_secrets() == 0
    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-fromenv"


def test_apply_secrets_without_key_is_quiet(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SECRET_KEY", raising=False)
    assert opconfig.apply_secrets() == 0      # nothing to do, no crash


# --- unset + status -------------------------------------------------------------

@pytest.mark.requires_crypto
def test_unset_clears_all_stores(monkeypatch, secret_key):
    opconfig.set_value("SMTP_HOST", "smtp.example.com")
    opconfig.set_value("SMTP_PASS", "hunter2-longer")
    assert "config.env" in opconfig.unset("SMTP_HOST")
    assert "vault" in opconfig.unset("SMTP_PASS")
    assert "SMTP_HOST" not in os.environ
    assert vault.get("operator", "config:SMTP_PASS") is None
    assert "not set" in opconfig.unset("SMTP_FROM")


@pytest.mark.requires_crypto
def test_status_is_honest_and_leak_free(monkeypatch, secret_key):
    opconfig.set_value("TELEGRAM_NOTIFY_CHAT_ID", "777")
    opconfig.set_value("ANTHROPIC_API_KEY", "sk-ant-vaulted-000")
    # A differing real env var shadows the saved value.
    firstrun.save_env_value("SLACK_NOTIFY_CHANNEL", "#saved")
    monkeypatch.setenv("SLACK_NOTIFY_CHANNEL", "#from-env")
    rows = {r["key"]: r for r in opconfig.status()}
    assert rows["TELEGRAM_NOTIFY_CHAT_ID"]["value"] == "777"
    assert rows["ANTHROPIC_API_KEY"]["set"] is True
    assert rows["ANTHROPIC_API_KEY"]["value"] is None        # secret
    assert "shadows" in rows["SLACK_NOTIFY_CHANNEL"]["source"]
    assert rows["SMTP_HOST"]["set"] is False
    assert rows["TELEGRAM_BOT_TOKEN"]["consumers"]           # restart honesty
    assert "sk-ant-vaulted-000" not in json.dumps(rows)


# --- panel wiring -----------------------------------------------------------------

@pytest.mark.requires_crypto
def test_snapshot_config_section_leak_free(monkeypatch, secret_key):
    opconfig.set_value("WHATSAPP_APP_SECRET", "meta-shhh-123")
    snap = adminpanel.snapshot()
    cfg = snap["config"]
    assert cfg["vault_ready"] is True
    assert any(s["key"] == "WHATSAPP_APP_SECRET" and s["set"]
               for s in cfg["settings"])
    assert "meta-shhh-123" not in json.dumps(snap)


def test_act_config_ops(monkeypatch, secret_key):
    r = adminpanel.act("config_set", {"key": "OLYMPUS_CACHE_TTL", "value": "1h"})
    assert r["ok"] and "config.env" in r["message"]
    assert os.environ["OLYMPUS_CACHE_TTL"] == "1h"
    r = adminpanel.act("config_set", {"key": "OLYMPUS_CACHE_TTL", "value": "2d"})
    assert not r["ok"]                        # validator, in-band
    r = adminpanel.act("config_unset", {"key": "OLYMPUS_CACHE_TTL"})
    assert r["ok"]
    r = adminpanel.act("config_set", {"key": "PATH", "value": "/evil"})
    assert not r["ok"] and "not operator-configurable" in r["message"]


def test_act_mcp_add_scan_gate_and_remove(monkeypatch):
    monkeypatch.setattr(security, "url_block_reason", lambda url: None)
    r = adminpanel.act("mcp_add", {"name": "gh", "url": "https://mcp.example/",
                                   "auth_env": "GH_TOKEN", "type": "data"})
    assert r["ok"] and "Live everywhere" in r["message"]
    assert any(s.name == "gh" for s in connectors.mcp_servers())
    # The Phase-1 security scan still gates panel adds.
    r = adminpanel.act("mcp_add", {"name": "bad", "url": "http://mcp.example/"})
    assert not r["ok"] and "https" in r["message"]
    r = adminpanel.act("mcp_remove", {"name": "gh"})
    assert r["ok"]
    assert not connectors.mcp_servers()
    r = adminpanel.act("mcp_remove", {"name": "gh"})
    assert not r["ok"]                        # already gone, in-band



@pytest.mark.parametrize('fault', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('variant', ['plain', 'missing_add_note', 'hostile_add_note',
                                   'hostile_dict', 'malformed_notes'])
@pytest.mark.parametrize('second_assignment_visible', [False, True])
def test_hydration_interrupt_retains_exact_identity_and_partial_environment_fact(
        monkeypatch, fault, variant, second_assignment_visible):
    """Actual env assignment can lose its acknowledgement; never invoke hooks."""
    keys = ('SLACK_BOT_TOKEN', 'SMTP_PASS')
    values = ('owned-hydration-first', 'owned-hydration-second')
    for key in keys:
        # Register original absence/value before direct environment writes.
        monkeypatch.setenv(key, 'owned-hydration-restore-sentinel')
        monkeypatch.delenv(key, raising=False)
    hooks = []
    if variant == 'plain':
        error = fault('owned-hydration-interruption')
    else:
        class AdversarialInterrupt(fault):
            def __getattribute__(self, name):
                if name == 'add_note' and variant == 'missing_add_note':
                    hooks.append('missing_add_note_lookup')
                    raise AttributeError('no add_note on Python 3.10')
                return super().__getattribute__(name)
            def add_note(self, value):
                hooks.append('add_note')
                raise AssertionError('exception callback must not run')
            @property
            def __dict__(self):
                hooks.append('__dict__')
                raise AssertionError('exception dict property must not run')
        error = AdversarialInterrupt('owned-hydration-interruption')
    attributes = BaseException.__dict__['__dict__'].__get__(error)
    notes = 73 if variant == 'malformed_notes' else ['owned-existing-private-note']
    attributes['__notes__'] = notes
    before = dict(attributes)
    snapshots = []
    def entries(owner):
        assert owner == opconfig.VAULT_USER
        snapshots.append(owner)
        return {opconfig.VAULT_PREFIX + key: value for key, value in zip(keys, values)}
    actual_assign = type(os.environ).__setitem__
    assignments = []
    def assign(environment, key, value):
        if key in keys:
            assignments.append(key)
            if key == keys[1]:
                if second_assignment_visible:
                    actual_assign(environment, key, value)
                raise error
        return actual_assign(environment, key, value)
    caught = None
    with monkeypatch.context() as patch:
        patch.setattr(vault, 'entries', entries)
        patch.setattr(type(os.environ), '__setitem__', assign)
        try:
            opconfig.apply_secrets()
        except BaseException as observed:
            caught = observed
    assert caught is error and type(caught) is type(error)
    assert assignments == list(keys) and snapshots == [opconfig.VAULT_USER]
    assert os.environ[keys[0]] == values[0]
    if second_assignment_visible:
        assert os.environ[keys[1]] == values[1]
    else:
        assert keys[1] not in os.environ
    assert hooks == []
    assert attributes == {**before, '_secret_hydration_environment_unconfirmed': True}
    assert attributes['__notes__'] is notes
    assert attributes['_secret_hydration_environment_unconfirmed'] is True
