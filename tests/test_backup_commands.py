"""Owned command and scheduler controls; no provider dispatch or notifications."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import uuid

import pytest
from olympus import backup, backup_commands as commands, backup_workflow as workflow
from olympus import backup_authority as authority, backup_evidence as evidence
from olympus import backup_publication as publication, config
from test_backup_delivery import owned, uploader, files, native_io as native


@pytest.fixture(autouse=True)
def local_policy(monkeypatch):
    monkeypatch.setattr(config, 'backup_keep', lambda: 2)
    monkeypatch.setattr(config, 'build_info', lambda: {'commit': '1' * 40})


def args(*values):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command')
    commands.add_backup_parser(sub)
    commands.add_recovery_parsers(sub)
    return parser.parse_args(values)


def forbid(*_args, **_kwargs):
    pytest.fail('forbidden dispatch in owned local control')


def output_rows(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.mark.parametrize('arguments', [('backup', '--list'), ('backup-evidence', 'receipts')])
def test_local_inspection_reachable_before_hydration(owned, monkeypatch, capsys, arguments):
    from olympus import cli, opconfig, firstrun, witness
    before = files(owned)
    monkeypatch.setattr(firstrun, 'load_env_file', lambda: None)
    monkeypatch.setattr(opconfig, 'apply_secrets', forbid)
    monkeypatch.setattr(config, 'production_problems', forbid)
    monkeypatch.setattr(witness, 'require_production_seed', forbid)
    monkeypatch.setattr(config, 'require_staging_config', forbid)
    monkeypatch.setattr(config, 'require_production_config', forbid)
    assert cli.main(list(arguments)) == 0
    assert output_rows(capsys)[0]['state'] == 'observed'
    assert files(owned) == before


@pytest.mark.parametrize('drill', [False, True])
def test_offline_restore_reachable_before_hydration(owned, tmp_path, monkeypatch, capsys, drill):
    from olympus import cli, opconfig, firstrun, witness
    created = backup.create()
    monkeypatch.setattr(firstrun, 'load_env_file', lambda: None)
    monkeypatch.setattr(opconfig, 'apply_secrets', forbid)
    monkeypatch.setattr(config, 'production_problems', forbid)
    monkeypatch.setattr(witness, 'require_production_seed', forbid)
    monkeypatch.setattr(config, 'require_staging_config', forbid)
    monkeypatch.setattr(config, 'require_production_config', forbid)
    target, operation = tmp_path / 'offline', uuid.uuid4().hex
    command = ['backup', '--drill', created['path']] if drill else ['restore', created['path']]
    assert cli.main(command + ['--into', str(target), '--operation-id', operation]) == 0
    rows = output_rows(capsys)
    assert rows[0]['operation_id'] == operation and rows[-1]['state'] == 'complete'
    assert native(target / 'data.txt').read_bytes() == b'owned delivery image'


@pytest.mark.parametrize('local_only', [False, True])
def test_ordinary_backup_keeps_hydration_failure_gate(owned, monkeypatch, capsys, local_only):
    from olympus import cli, opconfig, firstrun, witness
    before = files(owned)
    def unavailable():
        raise RuntimeError('owned-private-hydration-detail')
    monkeypatch.setattr(firstrun, 'load_env_file', lambda: None)
    monkeypatch.setattr(opconfig, 'apply_secrets', unavailable)
    monkeypatch.setattr(backup, 'run', forbid)
    assert cli.main(['backup'] + (['--no-deliver'] if local_only else [])) == 1
    output = capsys.readouterr()
    assert 'No command was started' in output.err
    assert 'owned-private-hydration-detail' not in output.out + output.err
    assert files(owned) == before


def test_restore_requires_explicit_offline_target(capsys):
    with pytest.raises(SystemExit) as raised:
        args('restore', 'owned-archive')
    assert raised.value.code == 2 and '--into' in capsys.readouterr().err


@pytest.mark.parametrize('arguments', [
    ('backup', '--no-deliver', '--delivery-id', 'a' * 32),
    ('backup', '--list', '--operation-id', 'a' * 32),
    ('backup', '--drill', '--external'),
    ('backup', '--operation-id', 'invalid'),
    ('backup-evidence', 'publication-resume'),
    ('backup-evidence', 'restore-resume', '--operation-id', 'a' * 32),
    ('backup-evidence', 'delivery-inspect'),
    ('backup-evidence', 'receipts', '--operation-id', 'a' * 32),
])
def test_invalid_options_refuse_without_mutation(owned, monkeypatch, capsys, arguments):
    before = files(owned)
    for name in ('run', 'restore', 'drill', 'resume_restore', 'inspect_delivery'):
        monkeypatch.setattr(backup, name, forbid)
    monkeypatch.setattr(commands, '_publication', forbid)
    assert commands.command(args(*arguments)) == 1
    assert output_rows(capsys)[0]['state'] == 'refused'
    assert files(owned) == before


def test_ids_are_emitted_before_actual_mutation(owned, monkeypatch, capsys):
    original = backup.run
    operation, attempt = uuid.uuid4().hex, uuid.uuid4().hex
    uploads, calls = uploader(monkeypatch), []
    def run(**kwargs):
        rows = output_rows(capsys)
        assert rows == [{'state': 'requested', 'action': 'backup', 'operation_id': operation,
                         'delivery_id': attempt, 'remote_retention_verified': False}]
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(backup, 'run', run)
    assert commands.command(args('backup', '--operation-id', operation, '--delivery-id', attempt)) == 0
    output = output_rows(capsys)[0]
    assert len(calls) == 1 and len(uploads) == 2
    assert output['workflow']['publication_complete'] and output['workflow']['evidence_recorded']
    assert output['workflow']['remote_retention_verified'] is False


def test_first_output_failure_prevents_mutation(owned, monkeypatch):
    before = files(owned)
    failure, outputs = BrokenPipeError('owned-private-output-detail'), []
    def emit(value):
        if not outputs:
            outputs.append(value)
            raise failure
        outputs.append(value)
    monkeypatch.setattr(commands, '_emit', emit)
    monkeypatch.setattr(backup, 'run', forbid)
    assert commands.command(args('backup', '--no-deliver')) == 1
    assert files(owned) == before and len(outputs) == 2
    assert outputs[-1]['state'] == 'completion_unconfirmed'
    assert 'owned-private-output-detail' not in json.dumps(outputs)


@pytest.mark.parametrize('persistent', [False, True])
def test_final_output_failure_preserves_actual_return_without_repeating(owned, monkeypatch, capsys, persistent):
    original, calls = backup.run, []
    failure, emissions = BrokenPipeError('owned-private-final-output'), []
    def run(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    def emit(value):
        emissions.append(value)
        if len(emissions) == 2 or persistent and len(emissions) > 1:
            raise failure
    monkeypatch.setattr(backup, 'run', run)
    monkeypatch.setattr(commands, '_emit', emit)
    assert commands.command(args('backup', '--no-deliver')) == 1
    report = failure.backup_command_report
    assert report['state'] == 'output_unconfirmed' and report['operation_repeated'] is False
    assert report['returned_result']['workflow']['ok'] is True and len(calls) == 1
    assert len(evidence.inspect()['receipts']) == 1
    captured = capsys.readouterr()
    if persistent:
        assert json.loads(captured.err) == report
    else:
        assert emissions[-1] == report and captured.err == ''
    assert 'owned-private-final-output' not in json.dumps(report) + captured.out + captured.err


class BrokenStream:
    def __init__(self, error):
        self.error = error
    def write(self, _value):
        raise self.error
    def flush(self):
        raise self.error


@pytest.mark.parametrize('stage', ['create', 'deliver', 'restore'])
@pytest.mark.parametrize('interrupt_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('broken_reporting', [False, True])
def test_actual_stage_exit_interrupt_retains_facts_and_primary(owned, tmp_path, monkeypatch, stage,
                                                             interrupt_type, broken_reporting):
    created = backup.create() if stage == 'restore' else None
    uploads, stages, emissions = uploader(monkeypatch), [], []
    operation, observed = uuid.uuid4().hex, uuid.uuid4().hex
    interruption = interrupt_type('owned-private-primary-interruption')
    interruption.operation_id = observed
    original = authority._WorkflowAuthority.stage
    @contextmanager
    def interrupted_exit(self, name):
        stages.append(name)
        with original(self, name):
            yield self
        if name == stage:
            raise interruption
    def emit(value):
        emissions.append(value)
        if broken_reporting and len(emissions) > 1:
            raise SystemExit('owned-private-secondary-reporting')
    monkeypatch.setattr(authority._WorkflowAuthority, 'stage', interrupted_exit)
    monkeypatch.setattr(commands, '_emit', emit)
    values = (['backup', '--drill', created['path'], '--into', str(tmp_path / 'offline')]
              if stage == 'restore' else ['backup'])
    values += ['--operation-id', operation]
    with monkeypatch.context() as reporting:
        if broken_reporting:
            reporting.setattr(commands.sys, 'stderr', BrokenStream(KeyboardInterrupt('owned-private-secondary-stderr')))
        with pytest.raises(interrupt_type) as raised:
            commands.command(args(*values))
    assert raised.value is interruption
    report = interruption.backup_command_report
    retained = report['workflow']
    assert retained['ok'] is False and retained['stage'] == stage
    assert retained['requested_operation_id'] == operation
    assert retained['failure']['observed_operation_id'] == observed
    assert report['observed_operation_id'] == observed
    assert retained['remote_retention_verified'] is False
    if stage == 'restore':
        assert retained['restore_complete'] and retained['restore']['archive_sha256'] == created['sha256']
        assert native(tmp_path / 'offline' / 'data.txt').read_bytes() == b'owned delivery image'
        assert stages == ['restore'] and len(uploads) == 0
    else:
        assert retained['publication_complete']
        assert len(retained['publication']['archive_sha256']) == 64
        assert retained['delivery_commands_complete'] is (stage == 'deliver')
        assert stages == (['create'] if stage == 'create' else ['create', 'deliver'])
        assert len(uploads) == (0 if stage == 'create' else 2)
    assert len(retained['confirmed_stages']) == (2 if stage == 'deliver' else 1)
    assert all(row['completion_acknowledgment_observed'] and not row['enclosing_operation_complete']
               for row in retained['confirmed_stages'])
    assert 'owned-private-' not in json.dumps(report)


def test_actual_old_publication_identity_survives_cli_and_scheduler(owned, monkeypatch, capsys):
    old, requested = uuid.uuid4().hex, uuid.uuid4().hex
    original = publication._Root.publish
    def interrupt(self, path, raw):
        if Path(path).name == 'complete.json':
            raise KeyboardInterrupt('owned-private-pending-publication')
        return original(self, path, raw)
    with monkeypatch.context() as patch:
        patch.setattr(publication._Root, 'publish', interrupt)
        with pytest.raises(KeyboardInterrupt):
            backup.create(operation_id=old)
    before = files(owned)
    assert commands.command(args('backup', '--operation-id', requested)) == 1
    report = output_rows(capsys)[-1]['workflow']
    assert report['failure']['observed_operation_id'] == old
    status = commands.scheduled_status(report)
    assert not status['ok'] and old in status['message'] and requested in status['message']
    assert files(owned) == before and 'owned-private-' not in json.dumps(status)


@pytest.mark.parametrize('failure_mode', ['notification', 'backup', 'none'])
def test_heartbeat_retains_status_and_does_not_repeat_backup(owned, monkeypatch, failure_mode):
    from olympus import heartbeat, firstrun, gateway, memory
    requested, old, attempt, blocking = (uuid.uuid4().hex for _ in range(4))
    returned = workflow._initial('backup')
    returned.update(requested_operation_id=requested, stage='deliver', publication_complete=True,
        failure={'observed_operation_id': old, 'observed_delivery_id': attempt,
                 'observed_blocking_publication_id': blocking, 'raw': 'owned-private-untrusted-detail'})
    calls, notices, saves = [], [], []
    def run():
        calls.append(True)
        if failure_mode == 'backup':
            raise RuntimeError('owned-private-backup-detail')
        return returned
    def notify(message):
        notices.append(message)
        if failure_mode == 'notification':
            raise RuntimeError('owned-private-notification-detail')
    monkeypatch.setattr(backup, 'run', run)
    monkeypatch.setattr(gateway, 'notify_all', notify)
    monkeypatch.setattr(firstrun, 'configured', lambda: False)
    monkeypatch.setattr(heartbeat, '_watch_job', lambda name, fn: fn() if name == 'backup' else None)
    monkeypatch.setattr(heartbeat, '_due', lambda state, name, interval, now: name == 'backup')
    monkeypatch.setattr(config, 'BACKUP_EVERY', 60)
    monkeypatch.setattr(memory, 'save_state', lambda state: saves.append(dict(state)))
    state = {}
    log = heartbeat.tick(state, now=1234)
    assert calls == [True] and state['backup'] == 1234 and saves == [state]
    assert 'owned-private-' not in json.dumps(log + notices)
    if failure_mode == 'backup':
        assert notices == [] and 'status unavailable' in log[-1]
    else:
        assert len(notices) == 1 and all(value in notices[0] for value in (requested, old, attempt, blocking))
        assert log[0] == notices[0]
        if failure_mode == 'notification':
            assert log[-1] == 'Backup failure notification was not confirmed.'


def test_reporting_does_not_reclassify_malformed_or_remote_claims():
    for value in (None, {}, {'schema': workflow.SCHEMA, 'kind': 'backup', 'ok': 'true'}):
        assert commands.scheduled_status(value)['ok'] is False
        assert commands._workflow_report(value) is None
    value = workflow._initial('backup')
    value.update(requested_operation_id=uuid.uuid4().hex, stage='complete', ok=True,
                 publication_complete=True, retirement_batch_complete=True, evidence_recorded=True,
                 remote_retention_verified=True)
    assert commands.scheduled_status(value)['ok'] is False
    assert commands._workflow_report(value) is None


@pytest.mark.parametrize('local_only', [False, True])
def test_ordinary_backup_keeps_boot_refusal_after_successful_hydration(owned, monkeypatch, capsys, local_only):
    from olympus import cli, opconfig, firstrun, witness
    calls = []
    monkeypatch.setattr(firstrun, 'load_env_file', lambda: None)
    monkeypatch.setattr(opconfig, 'apply_secrets', lambda: calls.append('hydrated'))
    def refused():
        calls.append('boot')
        raise witness.WitnessError('owned boot refusal')
    monkeypatch.setattr(witness, 'require_production_seed', refused)
    monkeypatch.setattr(backup, 'run', forbid)
    assert cli.main(['backup'] + (['--no-deliver'] if local_only else [])) == 1
    assert calls == ['hydrated', 'boot'] and 'owned boot refusal' in capsys.readouterr().err


@pytest.mark.parametrize('kind', ['backup', 'restore'])
@pytest.mark.parametrize('location', ['evidence', 'release_authority'])
@pytest.mark.parametrize('interrupt_type', [KeyboardInterrupt, SystemExit])
def test_returned_evidence_hash_survives_final_interruption_and_broken_reporting(owned, tmp_path,
        monkeypatch, kind, location, interrupt_type):
    created = backup.create() if kind == 'restore' else None
    interruption = interrupt_type('owned-private-final-interruption')
    operation, emissions, stages = uuid.uuid4().hex, [], []
    original_stage, original_close = authority._WorkflowAuthority.stage, authority._WorkflowAuthority.close
    @contextmanager
    def stage(self, name):
        stages.append(name)
        with original_stage(self, name):
            yield self
        if location == 'evidence' and name == 'evidence':
            raise interruption
    closes = []
    def close(self):
        original_close(self)
        closes.append(self)
        if location == 'release_authority':
            raise interruption
    def emit(value):
        emissions.append(value)
        if len(emissions) > 1:
            raise KeyboardInterrupt('owned-private-secondary-output')
    monkeypatch.setattr(authority._WorkflowAuthority, 'stage', stage)
    monkeypatch.setattr(authority._WorkflowAuthority, 'close', close)
    monkeypatch.setattr(commands, '_emit', emit)
    values = (['backup', '--drill', created['path'], '--into', str(tmp_path / 'offline')]
              if kind == 'restore' else ['backup', '--no-deliver'])
    values += ['--operation-id', operation]
    with monkeypatch.context() as reporting:
        reporting.setattr(commands.sys, 'stderr', BrokenStream(SystemExit('owned-private-secondary-stderr')))
        with pytest.raises(interrupt_type) as raised:
            commands.command(args(*values))
    assert raised.value is interruption
    result = interruption.backup_command_report['workflow']
    assert not result['ok'] and result['stage'] == location and result['evidence_recorded']
    acknowledgment = result['returned_evidence_acknowledgment']
    retained = evidence.inspect()['receipts']
    assert len(retained) == 1 and acknowledgment['receipt_sha256'] == retained[0]['receipt_sha256']
    assert acknowledgment['operation_id'] == operation and acknowledgment['kind'] == kind
    assert acknowledgment['receipt_write_acknowledgment_observed'] is True
    assert acknowledgment['enclosing_workflow_complete'] is False
    assert len(closes) == 1 and not closes[0].active and closes[0].memory is None
    assert stages == (['restore', 'evidence'] if kind == 'restore' else ['create', 'retire', 'evidence'])
    assert 'owned-private-' not in json.dumps(interruption.backup_command_report)


@pytest.mark.parametrize('ack_kind,field', [
    ('stage', 'completion_acknowledgment_observed'), ('stage', 'enclosing_operation_complete'),
    ('stage', 'current_state_revalidated'), ('stage', 'remote_retention_verified'),
    ('evidence', 'receipt_write_acknowledgment_observed'), ('evidence', 'enclosing_workflow_complete'),
    ('evidence', 'current_archive_availability_verified'), ('evidence', 'remote_retention_verified'),
])
def test_numeric_boolean_acknowledgments_are_rejected(ack_kind, field):
    from olympus import backup_facts
    operation = uuid.uuid4().hex
    value = workflow._initial('backup')
    value.update(requested_operation_id=operation, stage='create',
                 failure={'stage': 'create', 'status': 'completion_unconfirmed'})
    if ack_kind == 'stage':
        row = backup_facts.ConfirmedStage('publication', operation, 'a' * 64, 'b' * 64, 'c' * 64).report()
        value['confirmed_stages'] = [row]
    else:
        row = evidence.RecordedEvidence('backup', operation, 'a' * 64, 'b' * 64).report()
        value['evidence_acknowledgment'] = row
    assert commands._workflow_report(value) is not None
    row[field] = int(row[field])
    assert commands._workflow_report(value) is None


def test_actual_cli_publication_resume_then_retire_keeps_both_images_and_uses_no_keys(owned, monkeypatch, capsys):
    from olympus import archive_io as aio, backup_signing as signing
    operation, actions = uuid.uuid4().hex, []
    original_publish = publication._Root.publish
    def interrupt(self, path, raw):
        if Path(path).name == 'complete.json':
            raise KeyboardInterrupt('owned interrupted publication')
        return original_publish(self, path, raw)
    with monkeypatch.context() as patch:
        patch.setattr(publication._Root, 'publish', interrupt)
        with pytest.raises(KeyboardInterrupt):
            backup.create(operation_id=operation)
    image = owned / 'backups' / publication.archive_name(operation, False)
    pair = (native(image).read_bytes(), native(image.with_name(image.name + '.sig.json')).read_bytes())
    monkeypatch.setattr(signing, 'capture_policy', forbid)
    monkeypatch.setattr(aio.custody, 'capture', forbid)
    original_recovery = commands._publication
    def recovery(action, selected):
        requested = output_rows(capsys)
        assert requested == [{'state': 'requested', 'action': action, 'operation_id': operation}]
        actions.append(action)
        return original_recovery(action, selected)
    monkeypatch.setattr(commands, '_publication', recovery)
    for action in ('publication-resume', 'publication-retire'):
        assert commands.command(args('backup-evidence', action, '--operation-id', operation)) == 0
        result = output_rows(capsys)[0]['result']
        assert result['operation_id'] == operation and result['remote_retention_verified'] is False
        assert result['current_signer_verification_performed'] is False
        assert result['current_decryption_performed'] is False
        if action == 'publication-resume':
            assert result['publication_complete'] is True
        else:
            assert result['retirement_complete'] is True and result['files_deleted'] == 0
        import hashlib
        assert result['archive_sha256'] == hashlib.sha256(pair[0]).hexdigest()
        assert result['signature_sha256'] == hashlib.sha256(pair[1]).hexdigest()
    assert actions == ['publication-resume', 'publication-retire']
    assert (native(image).read_bytes(), native(image.with_name(image.name + '.sig.json')).read_bytes()) == pair
