"""Owned credential-consumer controls; no provider or browser session access."""
import os
import json
import uuid

import pytest

from olympus import config, firstrun, opconfig, operator, secretref, security, store, vault


@pytest.fixture(autouse=True)
def owned_consumer_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MEMORY_DIR', tmp_path/'owned-memory')
    monkeypatch.setattr(firstrun, 'CONFIG_DIR', tmp_path/'owned-config')
    monkeypatch.setattr(firstrun, 'CONFIG_ENV', tmp_path/'owned-config/config.env')
    for key in ('OLYMPUS_SECRET_KEY_FILE', 'OLYMPUS_DATABASE_URL',
                'SLACK_BOT_TOKEN', 'SMTP_PASS', 'OLYMPUS_MODELS'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-consumer-custody')
    store.reset()
    yield
    store.reset()


def test_hydration_uses_one_snapshot_and_keeps_environment_precedence(monkeypatch):
    vault.put('operator', 'config:SLACK_BOT_TOKEN', 'owned-stored-slack')
    vault.put('operator', 'config:SMTP_PASS', 'owned-stored-smtp')
    monkeypatch.setenv('SMTP_PASS', 'owned-environment-wins')
    original = vault.entries
    owners = []
    def entries(owner):
        owners.append(owner)
        return original(owner)
    monkeypatch.setattr(vault, 'entries', entries)
    assert opconfig.apply_secrets() == 1
    assert owners == ['operator']
    assert os.environ['SLACK_BOT_TOKEN'] == 'owned-stored-slack'
    assert os.environ['SMTP_PASS'] == 'owned-environment-wins'


@pytest.mark.parametrize('invalid', [{}, '', 'owned\x00invalid'])
def test_hydration_invalid_value_cannot_partially_apply_snapshot(invalid):
    vault.put('operator', 'config:SLACK_BOT_TOKEN', 'owned-valid-first')
    vault.put('operator', 'config:SMTP_PASS', invalid)
    with pytest.raises(vault.VaultError, match='stored configuration secret is invalid'):
        opconfig.apply_secrets()
    assert 'SLACK_BOT_TOKEN' not in os.environ and 'SMTP_PASS' not in os.environ


def test_hydration_runs_registered_validator_before_any_environment_change():
    vault.put('operator', 'config:SLACK_BOT_TOKEN', 'owned-valid-first')
    vault.put('operator', 'config:OLYMPUS_MODELS', 'not-owned-model-json')
    with pytest.raises(vault.VaultError):
        opconfig.apply_secrets()
    assert 'SLACK_BOT_TOKEN' not in os.environ and 'OLYMPUS_MODELS' not in os.environ


@pytest.mark.parametrize('oversized', ['x' * 40000, '\U0001f600' * 16384],
                         ids=['ascii-oversize', 'nonbmp-oversize'])
def test_hydration_rejects_native_oversize_before_any_assignment(monkeypatch, oversized):
    # The emoji case fits the 64 KiB UTF-8 record bound but exceeds Windows'
    # UTF-16 environment-entry policy once NAME= and terminator are included.
    vault.put('operator', 'config:SLACK_BOT_TOKEN', 'owned-valid-first')
    vault.put('operator', 'config:SMTP_PASS', oversized)
    assignments = []
    original = type(os.environ).__setitem__
    def assign(self, key, value):
        if key in ('SLACK_BOT_TOKEN', 'SMTP_PASS'):
            assignments.append(key)
        return original(self, key, value)
    monkeypatch.setattr(type(os.environ), '__setitem__', assign)
    with pytest.raises(vault.VaultError):
        opconfig.apply_secrets()
    assert assignments == []
    assert 'SLACK_BOT_TOKEN' not in os.environ and 'SMTP_PASS' not in os.environ


def test_hydration_reports_native_assignment_failure_as_partial(monkeypatch):
    vault.put('operator', 'config:SLACK_BOT_TOKEN', 'owned-valid-first')
    vault.put('operator', 'config:SMTP_PASS', 'owned-valid-second')
    original = type(os.environ).__setitem__
    def assign(self, key, value):
        if key == 'SMTP_PASS':
            raise OSError('owned-private-native-detail')
        return original(self, key, value)
    monkeypatch.setattr(type(os.environ), '__setitem__', assign)
    with pytest.raises(opconfig.SecretHydrationError, match='partially updated') as failure:
        opconfig.apply_secrets()
    assert failure.value.applied_keys == ('SLACK_BOT_TOKEN',)
    assert failure.value.uncertain_key == 'SMTP_PASS'
    assert failure.value.__context__ is None
    assert 'owned-private-native-detail' not in str(failure.value)
    assert os.environ['SLACK_BOT_TOKEN'] == 'owned-valid-first'
    assert 'SMTP_PASS' not in os.environ


def test_setting_native_oversize_refuses_before_persistence(monkeypatch):
    writes = []
    monkeypatch.setattr(vault, 'put', lambda *args: writes.append('vault'))
    monkeypatch.setattr(firstrun, 'save_env_value', lambda *args: writes.append('config'))
    for key in ('SMTP_PASS', 'SMTP_HOST'):
        with pytest.raises(ValueError, match='bounded process environment'):
            opconfig.set_value(key, 'x' * 40000)
    assert writes == []


def test_setting_reports_persisted_but_unconfirmed_environment_application(monkeypatch):
    original = type(os.environ).__setitem__
    def assign(self, key, value):
        if key == 'SMTP_PASS':
            raise OSError('owned-private-native-detail')
        return original(self, key, value)
    monkeypatch.setattr(type(os.environ), '__setitem__', assign)
    with pytest.raises(ValueError, match='setting was saved') as failure:
        opconfig.set_value('SMTP_PASS', 'owned-persisted-value')
    assert failure.value.__context__ is None
    assert 'owned-private-native-detail' not in str(failure.value)
    assert vault.get('operator', 'config:SMTP_PASS') == 'owned-persisted-value'


def test_nonsecret_setting_reports_nested_save_application_failure(monkeypatch):
    original = type(os.environ).__setitem__
    def assign(self, key, value):
        if key == 'SMTP_HOST':
            raise OSError('owned-private-nested-detail')
        return original(self, key, value)
    monkeypatch.setattr(type(os.environ), '__setitem__', assign)
    with pytest.raises(ValueError, match='persistence/application is unconfirmed') as failure:
        opconfig.set_value('SMTP_HOST', 'owned.smtp.example')
    assert failure.value.__context__ is None
    assert 'owned-private-nested-detail' not in str(failure.value)
    assert firstrun._read_saved()['SMTP_HOST'] == 'owned.smtp.example'


def test_secret_setting_reports_nested_plaintext_cleanup_application_failure(monkeypatch):
    firstrun._save({'SMTP_PASS':'owned-old-plaintext', 'SMTP_HOST':'owned.smtp.example'})
    original = type(os.environ).__setitem__
    def assign(self, key, value):
        if key == 'SMTP_HOST':
            raise OSError('owned-private-nested-detail')
        return original(self, key, value)
    monkeypatch.setattr(type(os.environ), '__setitem__', assign)
    with pytest.raises(ValueError, match='encrypted setting stored') as failure:
        opconfig.set_value('SMTP_PASS', 'owned-new-encrypted')
    assert failure.value.__context__ is None
    assert 'owned-private-nested-detail' not in str(failure.value)
    assert vault.get('operator', 'config:SMTP_PASS') == 'owned-new-encrypted'
    assert 'SMTP_PASS' not in firstrun._read_saved()
    assert os.environ['SMTP_PASS'] == 'owned-old-plaintext'


def test_hydration_skips_unknown_and_nonsecret_registry_entries():
    vault.put('operator', 'config:PATH', 'owned-not-allowed')
    vault.put('operator', 'config:SMTP_HOST', 'owned-nonsecret')
    before = dict(os.environ)
    assert opconfig.apply_secrets() == 0
    assert dict(os.environ) == before


def test_hydration_and_status_refuse_unavailable_ciphertext():
    store.backend().put('vault', vault._key('operator'), b'owned-corrupt-ciphertext')
    before = dict(os.environ)
    with pytest.raises(vault.VaultError):
        opconfig.apply_secrets()
    with pytest.raises(vault.VaultError):
        opconfig.status()
    assert dict(os.environ) == before


def test_outbound_secret_inventory_reads_one_snapshot(monkeypatch):
    owners = []
    def entries(owner):
        owners.append(owner)
        return {'one': {'credential':'owned-private-token-111'},
                'two': 'owned-private-token-222'}
    def repeated(*args):
        raise AssertionError('separate entry reads would tear the inventory')
    monkeypatch.setattr(vault, 'entries', entries)
    monkeypatch.setattr(vault, 'names', repeated)
    monkeypatch.setattr(vault, 'get', repeated)
    observed = security._held_secrets('owned-exact-user')
    assert owners == ['owned-exact-user']
    assert ('vault:one', 'owned-private-token-111') in observed
    assert ('vault:two', 'owned-private-token-222') in observed


def test_secret_reference_distinguishes_absence_wrong_type_and_unavailability():
    assert secretref.resolve('vault:absent', 'owned-default') == 'owned-default'
    vault.put('operator', 'secretref:bad-type', {'credential':'owned-value'})
    with pytest.raises(vault.VaultError, match='not a string'):
        secretref.resolve('vault:bad-type', 'owned-default')
    store.backend().put('vault', vault._key('operator'), b'owned-corrupt-ciphertext')
    with pytest.raises(vault.VaultError):
        secretref.resolve('vault:absent', 'owned-default')


def test_saved_session_absence_is_distinct_from_unavailable_vault(monkeypatch):
    from olympus import browserlease

    # Browser-owner refusal precedes vault inspection for an untrusted principal.
    calls = []
    def unexpected_read(*args, **kwargs):
        calls.append('vault')
        raise AssertionError('untrusted saved-session inspection reached the vault')
    with monkeypatch.context() as patch:
        patch.setattr(vault, 'snapshot', unexpected_read)
        with pytest.raises(browserlease.OwnershipRefused):
            operator.has_saved_auth('owned-user', 'owned.example')
    assert calls == []

    # The original absence/unavailability control needs an admitted browser owner.
    assert operator.has_saved_auth('cli', 'owned.example') is False
    store.backend().put('vault', vault._key('cli'), b'owned-corrupt-ciphertext')
    with pytest.raises(vault.VaultError):
        operator.has_saved_auth('cli', 'owned.example')


def test_forget_site_clears_both_credentials_in_one_publication(monkeypatch):
    from olympus import vault_storage
    operator.authorize_site('owned-user', 'owned.example', 'remember')
    vault.put('owned-user', 'site:owned.example', {'username':'owned', 'password':'owned-pass'})
    vault.put('owned-user', 'cookies:owned.example', {'cookies':[]})
    original = vault_storage.FileTransaction.put
    writes = []
    def put(self, ns, key, blob):
        writes.append((ns,key))
        return original(self, ns, key, blob)
    monkeypatch.setattr(vault_storage.FileTransaction, 'put', put)
    assert operator.forget_site('owned-user', 'owned.example') is True
    assert writes == [('vault', vault._key('owned-user'))]
    assert vault.entries('owned-user') == {}
    assert 'owned.example' not in operator.sites('owned-user')


def test_forget_site_reports_partial_cleanup_without_underlying_details(monkeypatch):
    operator.authorize_site('owned-user', 'owned.example', 'remember')
    vault.put('owned-user', 'site:owned.example', 'owned-preserved')
    def unavailable(*args):
        raise vault.VaultError('owned-private-driver-detail')
    monkeypatch.setattr(vault, 'delete_many', unavailable)
    with pytest.raises(vault.VaultError, match='authorization removed') as failure:
        operator.forget_site('owned-user', 'owned.example')
    assert 'owned-private-driver-detail' not in str(failure.value)
    assert failure.value.__context__ is None
    assert 'owned.example' not in operator.sites('owned-user')
    assert vault.get('owned-user', 'site:owned.example') == 'owned-preserved'


@pytest.mark.parametrize('key', ['SMTP_PASS', 'SMTP_HOST'])
def test_unset_unavailable_vault_leaves_other_stores_untouched(monkeypatch, key):
    monkeypatch.setenv(key, 'owned-environment-value')
    monkeypatch.setattr(opconfig, '_saved_env', lambda: {key:'owned-saved'})
    saves = []
    monkeypatch.setattr(firstrun, '_save', lambda value: saves.append(value))
    def unavailable(*args):
        raise vault.VaultError('owned-vault-unavailable')
    monkeypatch.setattr(vault, 'delete', unavailable)
    with pytest.raises(vault.VaultError):
        opconfig.unset(key)
    assert saves == [] and os.environ[key] == 'owned-environment-value'


def test_unset_reports_configuration_failure_after_confirmed_vault_cleanup(monkeypatch):
    vault.put('operator', 'config:SMTP_PASS', 'owned-vault-value')
    monkeypatch.setenv('SMTP_PASS', 'owned-environment-value')
    monkeypatch.setattr(opconfig, '_saved_env', lambda: {'SMTP_PASS':'owned-saved'})
    def fail_save(value):
        raise RuntimeError('owned-private-config-detail')
    monkeypatch.setattr(firstrun, '_save', fail_save)
    with pytest.raises(ValueError, match='cleanup is unconfirmed') as failure:
        opconfig.unset('SMTP_PASS')
    assert failure.value.__context__ is None
    assert 'owned-private-config-detail' not in str(failure.value)
    assert vault.get('operator', 'config:SMTP_PASS') is None
    assert os.environ['SMTP_PASS'] == 'owned-environment-value'


def test_atomic_delete_reports_whether_an_entry_was_removed():
    assert vault.delete('owned-user', 'absent') is False
    vault.put('owned-user', 'one', 'owned-value')
    assert vault.delete_many('owned-user', ['one', 'absent']) is True
    assert vault.delete_many('owned-user', ['one']) is False


def test_readiness_uses_actual_versioned_encryption_check(monkeypatch):
    calls = []
    monkeypatch.setattr(vault, 'check_ready', lambda: calls.append('checked'))
    assert opconfig.vault_ready() is True
    assert calls == ['checked']


def _vault_args(*arguments):
    import argparse
    from olympus import vault_commands
    parser = argparse.ArgumentParser()
    vault_commands.add_parser(parser.add_subparsers(dest='command'))
    return parser.parse_args(['vault-evidence', *arguments])


def test_cli_refuses_command_when_hydration_is_unavailable(monkeypatch, capsys):
    from olympus import cli, gateway
    def unavailable():
        raise vault.VaultError('owned-private-detail')
    def dispatched():
        raise AssertionError('command must not start after unavailable hydration')
    monkeypatch.setattr(opconfig, 'apply_secrets', unavailable)
    monkeypatch.setattr(gateway, 'configured_channels', dispatched)
    assert cli.main(['gateway', '--list']) == 1
    captured = capsys.readouterr()
    assert 'No command was started' in captured.err
    assert 'owned-private-detail' not in captured.out + captured.err


def test_cli_local_recovery_remains_reachable_without_hydration(monkeypatch, capsys):
    from olympus import cli
    def forbidden():
        raise AssertionError('local vault inspection must not hydrate config')
    monkeypatch.setattr(opconfig, 'apply_secrets', forbidden)
    assert cli.main(['vault-evidence', 'legacy-list']) == 0
    assert json.loads(capsys.readouterr().out)['legacy_keys'] == []


def test_cli_operation_identity_is_visible_before_mutation(monkeypatch, capsys):
    from olympus import vault_commands
    operation_id = uuid.uuid4().hex
    called = []
    def rotate(owner, **kwargs):
        assert json.loads(capsys.readouterr().out) == {'state':'requested', 'operation_id':operation_id}
        called.append((owner, kwargs['operation_id']))
        return {'committed':True, 'operation_id':operation_id}
    monkeypatch.setattr(vault, 'rotate', rotate)
    assert vault_commands.command(_vault_args('migrate-format', '--owner', 'owned-user',
        '--operation-id', operation_id)) == 0
    assert called == [('owned-user', operation_id)]
    assert json.loads(capsys.readouterr().out)['committed'] is True


@pytest.mark.parametrize('arguments', [
    ['migrate-format'], ['rotate', '--owner', 'owned-user'],
    ['claim', '--owner', 'owned-user', '--legacy-key', 'owned-legacy'],
    ['discard', '--legacy-key', 'owned-legacy'], ['recover'],
    ['migrate-format', '--owner', 'owned-user', '--operation-id', 'not-valid'],
], ids=['owner-required','replacement-required','claim-review','discard-review','recovery-id','bad-id'])
def test_cli_invalid_request_refuses_without_vault_mutation(monkeypatch, capsys, arguments):
    from olympus import vault_commands
    def forbidden(*args, **kwargs):
        raise AssertionError('invalid request must not call vault mutation')
    for name in ('rotate', 'migrate_legacy', 'discard_legacy', 'resume_change'):
        monkeypatch.setattr(vault, name, forbidden)
    assert vault_commands.command(_vault_args(*arguments)) == 1
    output = json.loads(capsys.readouterr().out)
    assert output['state'] == 'refused'


def test_cli_explicit_rotation_and_recovery_preserve_custody_sources(tmp_path, monkeypatch, capsys):
    from olympus import vault_commands, vault_custody, vault_records
    vault.put('owned-user', 'one', 'owned-value')
    old_blob = store.backend().get('vault', vault._key('owned-user'))
    replacement = tmp_path/'owned-replacement-key'
    replacement.write_bytes(b'owned-replacement-custody')
    replacement.chmod(0o600)
    before_environment = dict(os.environ)
    before_mode = replacement.stat().st_mode
    operation_id = uuid.uuid4().hex
    assert vault_commands.command(_vault_args('rotate', '--owner', 'owned-user',
        '--operation-id', operation_id, '--replacement-key-file', str(replacement))) == 0
    output = capsys.readouterr().out
    assert 'owned-replacement-custody' not in output and 'owned-value' not in output
    assert dict(os.environ) == before_environment
    assert replacement.read_bytes() == b'owned-replacement-custody'
    assert replacement.stat().st_mode == before_mode
    assert store.backend().get('vault_recovery', 'change-'+operation_id+'-source') == old_blob
    values, metadata = vault_records.decode('owned-user',
        store.backend().get('vault', vault._key('owned-user')), b'owned-replacement-custody')
    assert values == {'one':'owned-value'} and metadata.context_authenticated
    monkeypatch.delenv('OLYMPUS_SECRET_KEY')
    monkeypatch.setenv('OLYMPUS_SECRET_KEY_FILE', str(tmp_path/'absent-old-custody'))
    assert vault_commands.command(_vault_args('recover', '--owner', 'owned-user',
        '--operation-id', operation_id, '--replacement-key-file', str(replacement))) == 0
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[-1]['already_committed'] is True


def test_cli_hidden_prompt_refuses_nonterminal(monkeypatch, capsys):
    from olympus import vault_commands
    from types import SimpleNamespace
    import getpass
    monkeypatch.setattr(vault_commands.sys, 'stdin', SimpleNamespace(isatty=lambda:False))
    def forbidden(*args):
        raise AssertionError('nonterminal must never request a plaintext prompt')
    monkeypatch.setattr(getpass, 'getpass', forbidden)
    assert vault_commands.command(_vault_args('rotate', '--owner', 'owned-user',
        '--prompt-replacement-key')) == 1
    assert json.loads(capsys.readouterr().out)['state'] == 'refused'
