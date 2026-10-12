"""Owned local recovery command controls; no private input or providers."""
import builtins
import hashlib
import json
import os
import threading

import pytest

from olympus import cli, config, prefs, prefs_commands as commands, prefs_native as native

OWNER = 'owned-recovery-cli'
OP = '3' * 32
NEXT = '4' * 32


@pytest.fixture
def owned(tmp_path, monkeypatch):
    root = tmp_path / 'owned-cli'
    root.mkdir()
    monkeypatch.setattr(config, 'MEMORY_DIR', root)
    monkeypatch.setattr(native, '_GATE', threading.RLock())
    monkeypatch.setattr(native, '_LOCAL', threading.local())
    monkeypatch.setattr(native, '_IN_FLIGHT', False)
    monkeypatch.setattr(native, '_POISONED', False)
    monkeypatch.setattr(prefs, '_TRANSFORM', threading.local())
    return root


def _args(*parts):
    return cli.build_parser().parse_args(['prefs-evidence', *parts])


def _files(root):
    io_root = prefs._io(root)
    return {p.relative_to(io_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in io_root.rglob('*') if p.is_file()}


def _seed(root, kind='repair'):
    current, legacy = prefs._relative_paths(OWNER)
    path = prefs._io(root.joinpath(*(current if kind == 'repair' else legacy)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{owned-private-corruption' if kind == 'repair' else b'{"language":"fr"}')
    return path


def _call_parts(kind, identifier=OP):
    if kind == 'repair':
        return [OWNER, '--repair', '--operation-id', identifier]
    prefix = [OWNER] if kind == 'migrate' else []
    return prefix + ['--' + kind + '-legacy', OWNER, '--operator-reviewed', '--operation-id', identifier]


@pytest.mark.parametrize('kind', ['repair', 'migrate', 'discard'])
def test_actual_operation_prints_id_before_effects_and_exact_retry_is_historical(owned, monkeypatch, capsys, kind):
    path = _seed(owned, kind)
    original_bytes = path.read_bytes()
    before = _files(owned)
    output = []
    real_emit = commands._emit

    def emit(value):
        if not output:
            assert value['state'] == 'requested' and value['operation_id'] == OP
            assert _files(owned) == before
            assert not (owned / 'locks').exists()
            assert getattr(native._LOCAL, 'current', None) is None
        output.append(dict(value))
        return real_emit(value)

    monkeypatch.setattr(commands, '_emit', emit)
    assert commands.command(_args(*_call_parts(kind))) == 0
    assert output[-1]['completed'] and output[-1]['operation_id'] == OP
    assert output[-1]['requested_operation_id'] == OP and not output[-1]['historical']
    assert original_bytes in [p.read_bytes() for p in prefs._io(owned).rglob('*.bin')]
    assert 'owned-private-corruption' not in capsys.readouterr().out
    sealed = _files(owned)

    def deny(*args, **kwargs):
        pytest.fail('historical replay attempted a new publication or retirement')

    for method in ('publish', 'write_new', 'retire'):
        monkeypatch.setattr(native.Transaction, method, deny)
    assert commands.command(_args(*_call_parts(kind))) == 0
    assert output[-1]['historical'] and output[-1]['operation_id'] == OP
    assert _files(owned) == sealed


@pytest.mark.parametrize('kind', ['repair', 'discard'])
def test_inert_request_retains_requested_id_without_claiming_a_completed_plan(owned, capsys, kind):
    assert commands.command(_args(*_call_parts(kind))) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]['operation_id'] == OP
    assert rows[-1]['operation_id'] is None and rows[-1]['requested_operation_id'] == OP
    assert not rows[-1]['completed'] and not (owned / 'prefs-recovery').exists()


def test_flushed_output_keeps_original_root_when_ambient_selection_changes(owned, tmp_path, monkeypatch, capsys):
    path = _seed(owned)
    foreign = tmp_path / 'foreign-selection'
    foreign.mkdir()
    foreign_file = _seed(foreign)
    foreign_before = _files(foreign)
    real_print = builtins.print
    seen = []

    def output(*values, **options):
        if not seen:
            assert options.get('flush') is True
            row = json.loads(values[0])
            assert row['state'] == 'requested'
            assert getattr(native._LOCAL, 'current', None) is None
            monkeypatch.setattr(config, 'MEMORY_DIR', foreign)
            seen.append(True)
        return real_print(*values, **options)

    monkeypatch.setattr(builtins, 'print', output)
    assert commands.command(_args(*_call_parts('repair'))) == 0
    assert seen == [True] and path.read_bytes() == b'{}'
    assert _files(foreign) == foreign_before and foreign_file.read_bytes() != b'{}'
    assert not (foreign / 'locks').exists()
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['repaired']


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_failed_requested_output_never_dispatches_and_preserves_actual_interrupt(owned, monkeypatch, fault_type):
    _seed(owned)
    before = _files(owned)
    fault = fault_type('owned-private-output-fault')
    seen = []

    def emit(value):
        seen.append(value)
        if len(seen) == 1:
            raise fault

    monkeypatch.setattr(commands, '_emit', emit)
    monkeypatch.setattr(prefs, '_run_recovery', lambda *a, **k: pytest.fail('dispatch after output failure'))
    if fault_type is OSError:
        assert commands.command(_args(*_call_parts('repair'))) == 1
        assert seen[-1]['operation_id'] == OP
        assert 'owned-private-output-fault' not in json.dumps(seen)
    else:
        with pytest.raises(fault_type) as observed:
            commands.command(_args(*_call_parts('repair')))
        assert observed.value is fault and fault.__dict__['_preference_recovery_operation'] == OP
        assert fault.__dict__['_preference_recovery_dispatch_started'] is False
        assert len(seen) == 1
    assert _files(owned) == before and not (owned / 'locks').exists()


def test_absent_root_is_not_created_or_adopted_after_output(monkeypatch, tmp_path):
    absent = tmp_path / 'absent-cli'
    monkeypatch.setattr(config, 'MEMORY_DIR', absent)
    rows = []

    def emit(value):
        assert value['state'] != 'requested'
        rows.append(value)
        absent.mkdir()
        _seed(absent)

    monkeypatch.setattr(commands, '_emit', emit)
    monkeypatch.setattr(prefs, '_run_recovery', lambda *a, **k: pytest.fail('absent-root dispatch'))
    assert commands.command(_args(*_call_parts('repair'))) == 1
    assert len(rows) == 1 and rows[0]['operation_id'] == OP
    assert not (absent / 'locks').exists() and not (absent / 'prefs-recovery').exists()


def test_root_replacement_at_output_refuses_before_transaction_creation(owned, tmp_path, monkeypatch):
    _seed(owned)
    before = _files(owned)
    retained = tmp_path / 'retained-original-cli'
    rows, moved = [], []

    def emit(value):
        rows.append(value)
        if len(rows) == 1:
            # Windows can refuse the rename under retained native share flags.
            # That refusal aborts output; POSIX must detect the replaced entry.
            os.rename(owned, retained)
            moved.append(True)
            owned.mkdir()
            _seed(owned)

    monkeypatch.setattr(commands, '_emit', emit)
    monkeypatch.setattr(prefs, '_run_recovery', lambda *a, **k: pytest.fail('replaced-root dispatch'))
    assert commands.command(_args(*_call_parts('repair'))) == 1
    assert _files(retained if moved else owned) == before
    assert not (owned / 'locks').exists()
    assert rows[-1]['state'] == 'unavailable' and not rows[-1]['dispatch_started']


@pytest.mark.parametrize('parts', [
    ['--operation-id', OP], ['--overwrite'], ['--operator-reviewed'],
    ['--migrate-legacy', OWNER, '--operator-reviewed'],
    [OWNER, '--migrate-legacy', OWNER], ['--discard-legacy', OWNER],
    [OWNER, '--discard-legacy', OWNER, '--operator-reviewed'],
    [OWNER, '--repair', '--operation-id', 'BAD-PRIVATE-ID'],
    [OWNER, '--repair', '--operation-id', '3' * 31],
])
def test_invalid_request_is_refused_before_root_acquisition(owned, monkeypatch, parts):
    rows = []
    monkeypatch.setattr(commands, '_emit', rows.append)
    monkeypatch.setattr(commands, '_existing_root', lambda: pytest.fail('invalid request acquired root'))
    assert commands.command(_args(*parts)) == 1
    assert rows[-1]['state'] == 'refused' and rows[-1]['operation_id'] is None
    assert 'BAD-PRIVATE-ID' not in json.dumps(rows)
    assert not list(owned.iterdir())


@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('body_fails', [False, True])
def test_root_cleanup_uncertainty_never_reports_success_or_masks_primary(owned, monkeypatch, fault_type, body_fails):
    real_init = native.storage._Directory.__init__
    closes = []
    primary = fault_type('owned-private-body')
    cleanup = fault_type('owned-private-close')

    def initialize(directory, *args, **kwargs):
        real_init(directory, *args, **kwargs)
        real_close = directory.close

        def close():
            closes.append(True)
            real_close()
            raise cleanup

        directory.close = close

    def run(*args, **kwargs):
        if body_fails:
            raise primary
        return {'operation_id': None, 'completed': False, 'historical': False, 'count': 0}

    rows = []
    monkeypatch.setattr(native.storage._Directory, '__init__', initialize)
    monkeypatch.setattr(prefs, '_run_recovery', run)
    monkeypatch.setattr(commands, '_emit', rows.append)
    args = _args(*_call_parts('discard'))
    if fault_type is OSError:
        assert commands.command(args) == 1
        assert rows[-1]['native_cleanup_unconfirmed'] is True
        assert 'owned-private' not in json.dumps(rows)
    else:
        with pytest.raises(fault_type) as observed:
            commands.command(args)
        assert observed.value is (primary if body_fails else cleanup)
        assert native.cleanup_unknown(observed.value)
        assert observed.value.__dict__['_preference_recovery_operation'] == OP
        assert len(rows) == 1
    assert closes == [True] and native._POISONED is True
    assert not any(row.get('state') == 'complete' for row in rows)


def test_cli_recovery_dispatch_is_reachable_before_secret_hydration_or_boot(owned, monkeypatch, capsys):
    from olympus import firstrun, opconfig, witness
    _seed(owned)
    monkeypatch.setattr(firstrun, 'load_env_file', lambda: None)

    def deny(*args, **kwargs):
        pytest.fail('local preference recovery reached secret hydration or signing boot')

    monkeypatch.setattr(opconfig, 'apply_secrets', deny)
    monkeypatch.setattr(witness, 'require_production_seed', deny)
    monkeypatch.setattr(config, 'sovereign_mode', deny)
    monkeypatch.setattr(witness, 'check_sovereign_seed', deny)
    monkeypatch.setattr(config, 'require_staging_config', deny)
    assert cli.main(['prefs-evidence', *_call_parts('repair')]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]['state'] == 'requested' and rows[-1]['repaired']


def test_blocking_operation_id_is_distinct_from_new_requested_id(owned, monkeypatch):
    _seed(owned)
    real_publish = native.Transaction.publish

    def interrupt(tx, parts, raw):
        result = real_publish(tx, parts, raw)
        if parts == prefs._recovery_layout()['head'] and json.loads(raw)['state'] == 'pending':
            raise OSError('owned-private-lost-head-ack')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(native.Transaction, 'publish', interrupt)
        with pytest.raises(prefs.PreferenceOperationError) as observed:
            prefs.repair(OWNER, operation_id=OP)
        assert observed.value.operation_id == OP
    before = _files(owned)
    rows = []
    monkeypatch.setattr(commands, '_emit', rows.append)
    assert commands.command(_args(*_call_parts('repair', NEXT))) == 1
    assert rows[0]['operation_id'] == NEXT
    assert rows[-1]['operation_id'] == OP and rows[-1]['requested_operation_id'] == NEXT
    assert _files(owned) == before


@pytest.mark.parametrize('boundary', ['postcheck', 'close', 'final-output'])
@pytest.mark.parametrize('fault_type', [OSError, KeyboardInterrupt, SystemExit])
def test_actual_returned_completion_survives_later_failure(owned, monkeypatch, boundary, fault_type):
    path = _seed(owned)
    real_init, real_run = native.storage._Directory.__init__, prefs._run_recovery
    acquired, completed, rows, fired = [], [], [], []
    fault = fault_type('owned-private-after-completion')

    def initialize(directory, *args, **kwargs):
        real_init(directory, *args, **kwargs)
        if not acquired:
            acquired.append(directory)
            real_check, real_close = directory.check, directory.close

            def check():
                if completed and boundary == 'postcheck' and not fired:
                    fired.append(True)
                    raise fault
                return real_check()

            def close():
                real_close()
                if completed and boundary == 'close' and not fired:
                    fired.append(True)
                    raise fault

            directory.check, directory.close = check, close

    def run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        assert result['completed'] and result['operation_id'] == OP
        completed.append(True)
        return result

    def emit(value):
        if completed and boundary == 'final-output' and not fired:
            fired.append(True)
            raise fault
        rows.append(value)

    monkeypatch.setattr(native.storage._Directory, '__init__', initialize)
    monkeypatch.setattr(prefs, '_run_recovery', run)
    monkeypatch.setattr(commands, '_emit', emit)
    if fault_type is OSError:
        assert commands.command(_args(*_call_parts('repair'))) == 1
        facts = rows[-1]['recovery_returned']
        assert rows[-1]['state'] == 'recovery_required' and rows[-1]['dispatch_started']
        assert 'owned-private' not in json.dumps(rows)
        if boundary == 'close':
            assert rows[-1]['native_cleanup_unconfirmed'] is True
    else:
        with pytest.raises(fault_type) as observed:
            commands.command(_args(*_call_parts('repair')))
        assert observed.value is fault
        facts = fault.__dict__['_preference_recovery_returned']
        assert fault.__dict__['_preference_recovery_requested_operation'] == OP
        assert fault.__dict__['_preference_recovery_dispatch_started'] is True
        assert len(rows) == 1
    assert facts == {'operation_id': OP, 'completed': True, 'historical': False, 'count': 1}
    assert completed == [True] and fired == [True] and path.read_bytes() == b'{}'
    assert acquired[0]._closed


@pytest.mark.parametrize('secondary_type', [OSError, KeyboardInterrupt, SystemExit])
def test_second_reporting_failure_preserves_primary_and_returns_failure(owned, monkeypatch, secondary_type):
    _seed(owned)
    before = _files(owned)
    primary = OSError('owned-private-first-output')
    secondary = secondary_type('owned-private-second-output')
    calls = []

    def emit(value):
        calls.append(value)
        raise primary if len(calls) == 1 else secondary

    monkeypatch.setattr(commands, '_emit', emit)
    monkeypatch.setattr(prefs, '_run_recovery', lambda *a, **k: pytest.fail('dispatch after failed output'))
    assert commands.command(_args(*_call_parts('repair'))) == 1
    assert len(calls) == 2 and primary.__dict__['_preference_reporting_unconfirmed'] is True
    assert primary.__dict__['_preference_recovery_requested_operation'] == OP
    assert _files(owned) == before


@pytest.mark.parametrize('field', ['operation_id', '_preference_recovery_operation'])
def test_valid_blocking_identity_survives_actual_interrupt_and_malformed_other_field(owned, monkeypatch, field):
    error = KeyboardInterrupt('owned-private-primary')
    other = '_preference_recovery_operation' if field == 'operation_id' else 'operation_id'
    error.__dict__[field] = NEXT
    error.__dict__[other] = {'malformed': True}
    rows = []

    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(prefs, '_run_recovery', run)
    monkeypatch.setattr(commands, '_emit', rows.append)
    with pytest.raises(KeyboardInterrupt) as observed:
        commands.command(_args(*_call_parts('repair')))
    assert observed.value is error
    assert error.__dict__['_preference_recovery_operation'] == NEXT
    assert error.__dict__['_preference_recovery_requested_operation'] == OP
    assert len(rows) == 1


def test_bound_root_identity_is_checked_before_coordination_creation(owned):
    _seed(owned)
    before = _files(owned)
    request = prefs._recovery_request('repair', OWNER, None, False, OP)
    with commands._existing_root() as (root, identity, _directory):
        wrong = (identity[0], identity[1] + 1)
        with pytest.raises(prefs.PreferenceOperationError) as observed:
            prefs._run_recovery(request, root=root, root_identity=wrong)
    assert observed.value.operation_id == OP
    assert _files(owned) == before and not (owned / 'locks').exists()


@pytest.mark.parametrize('kind', ['repair', 'migrate', 'discard'])
def test_interrupted_cli_operation_refuses_mismatched_retry_and_resumes_original_id(owned, monkeypatch, kind):
    _seed(owned, kind)
    real_publish = native.Transaction.publish
    rows, fired = [], []
    monkeypatch.setattr(commands, '_emit', rows.append)

    def interrupt(tx, parts, raw):
        result = real_publish(tx, parts, raw)
        if (parts == prefs._recovery_layout()['head'] and json.loads(raw)['state'] == 'pending'
                and not fired):
            fired.append(True)
            raise OSError('owned-private-cli-lost-ack')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(native.Transaction, 'publish', interrupt)
        assert commands.command(_args(*_call_parts(kind))) == 1
    assert fired == [True] and rows[-1]['operation_id'] == OP
    assert rows[-1]['state'] == 'recovery_required'
    retained = _files(owned)
    mismatched = _call_parts(kind)
    if kind in ('repair', 'migrate'):
        mismatched[0] = 'different-exact-owner'
    else:
        mismatched = _call_parts('repair')
    assert commands.command(_args(*mismatched)) == 1
    assert rows[-1]['operation_id'] == OP and _files(owned) == retained
    assert commands.command(_args(*_call_parts(kind))) == 0
    assert rows[-1]['completed'] and rows[-1]['operation_id'] == OP
    assert rows[-1]['requested_operation_id'] == OP
    assert {p.name for p in prefs._io(owned / 'prefs-recovery/operations').iterdir()} == {OP}


def test_generated_printed_id_is_the_actual_retained_plan_identity(owned, monkeypatch):
    _seed(owned)
    rows = []
    monkeypatch.setattr(commands, '_emit', rows.append)
    assert commands.command(_args(OWNER, '--repair')) == 0
    identifier = rows[0]['operation_id']
    assert commands._valid_id(identifier)
    assert rows[-1]['operation_id'] == rows[-1]['requested_operation_id'] == identifier
    assert {p.name for p in prefs._io(owned / 'prefs-recovery/operations').iterdir()} == {identifier}
