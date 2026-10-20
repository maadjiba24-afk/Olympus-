"""P2R: preference-policy evidence is strict, visible, and repair-only.

The preferences blob contains authorization and spend policy.  A malformed
current-v2 blob must therefore never be interpreted as ordinary defaults,
silently overwritten by a later write, or repaired by a background reader.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from olympus import (actions, atomicio, capprofile, cli, config, dashboard,
                     operator, prefs, usage)


def _corrupt(user: str, raw: bytes = b'{"capability_profile":'):
    path = prefs._io(prefs._path(user))
    path.write_bytes(raw)
    return path


def test_corrupt_current_preferences_clamp_every_authority_consumer(monkeypatch):
    user = "local-restricted"
    monkeypatch.setattr(actions, "_REGISTRY", {})
    actions.register(actions.ActionType(
        name="p2r_note", risk_class=actions.TRIVIAL, scope="notes",
        preview=lambda payload: "note", execute=lambda payload: {"ok": True}))

    prefs.set(user, "autonomy", 4)
    prefs.set(user, "scopes", ["email", "notes"])
    prefs.set(user, "capability_profile", "guest")
    prefs.set(user, "action_limits", {"p2r_note": 0})
    prefs.set(user, "operator", {
        "enabled": True,
        "advanced": True,
        "sites": {"example.com": {"login": "remember"}},
    })
    prefs.set(user, "earned_autonomy", True)
    prefs.set(user, "pending_secure_login", {"domain": "example.com"})
    _corrupt(user)

    assert prefs.is_quarantined(user) is True
    assert capprofile.of_user(user) == "guest"
    assert actions.autonomy_level(user) == actions.L0_SUGGEST
    assert actions.granted_scopes(user) == set()
    assert actions.daily_limit(user, "p2r_note") == 1
    assert operator.enabled(user) is False
    assert operator.advanced(user) is False
    assert operator.sites(user) == {}
    assert prefs.get(user, "earned_autonomy") is False
    assert prefs.get(user, "pending_secure_login") is None


@pytest.mark.parametrize("raw", [
    b"{not-json",
    b"[]",
    b"\xff\xfe\x00",
])
def test_strict_load_rejects_malformed_bytes_and_root(raw):
    user = "bad-state"
    path = _corrupt(user, raw)

    with pytest.raises(prefs.PreferencesStateError,
                       match="(?i)preference policy evidence"):
        prefs.load(user)

    # Cosmetic callers can still take their explicit display default, while a
    # security key is clamped and the source bytes remain untouched.
    assert prefs.get(user, "language", "auto") == "auto"
    assert prefs.get(user, "capability_profile") == "guest"
    assert path.read_bytes() == raw


def test_write_refuses_to_erase_unavailable_policy_evidence():
    user = "write-guard"
    raw = b'{"autonomy": 0'
    path = _corrupt(user, raw)

    with pytest.raises(prefs.PreferencesStateError,
                       match="prefs-evidence"):
        prefs.set(user, "language", "fr")

    assert path.read_bytes() == raw


def test_state_status_surfaces_recovery_without_sensitive_values():
    user = "inspect-me"
    _corrupt(user, b'{"private": "do-not-print"')

    status = prefs.state_status(user)

    assert status["state"] == "unavailable"
    assert status["quarantined"] is True
    assert status["repair_command"] == (
        "olympus prefs-evidence inspect-me --repair")
    assert "do-not-print" not in json.dumps(status)


def test_explicit_repair_preserves_exact_blob_then_resets_to_safe_empty_state():
    user = "repair-me"
    raw = b'{"capability_profile":"guest"'
    path = _corrupt(user, raw)

    result = prefs.repair(user)

    assert result["repaired"] is True
    assert result["state"] == "valid"
    assert result["quarantined_sha256"] == hashlib.sha256(raw).hexdigest()
    archive_digest = hashlib.sha256(raw).hexdigest()[
        :prefs._QUARANTINE_DIGEST_HEX]
    quarantine = path.with_name(
        f"prefs.corrupt.{archive_digest}.json")
    assert result["quarantine_file"] == quarantine.name
    assert quarantine.read_bytes() == raw
    assert prefs.load(user) == {}
    assert prefs.state_status(user)["state"] == "valid"


def test_repair_never_rewrites_valid_or_missing_state():
    user = "already-valid"
    prefs.set(user, "language", "fr")
    path = prefs._path(user)
    before = path.read_bytes()

    valid = prefs.repair(user)
    missing = prefs.repair("first-run")

    assert valid["repaired"] is False and valid["state"] == "valid"
    assert missing["repaired"] is False and missing["state"] == "missing"
    assert path.read_bytes() == before
    assert not list(path.parent.glob("prefs.corrupt.*.json"))


@pytest.mark.parametrize('failure_kind', [OSError, KeyboardInterrupt, SystemExit])
def test_repair_preserves_source_and_archive_if_reset_publish_fails(monkeypatch, failure_kind):
    from olympus import prefs_native, prefs_records
    user = 'interrupted-repair'
    operation_id = '1' * 32
    raw = b'broken-evidence'
    path = _corrupt(user, raw)
    current = prefs.credential_paths(user)[0]
    digest = hashlib.sha256(raw).hexdigest()
    retained = current[:-1] + (f'prefs.corrupt.{digest[:prefs._QUARANTINE_DIGEST_HEX]}.json',)
    archive = prefs._io(config.MEMORY_DIR.joinpath(*retained))
    injected = failure_kind('owned reset publication fault')
    actual_publish = prefs_native.Transaction.publish
    observations = []

    def fail_reset(transaction, parts, data):
        if tuple(parts) == current:
            observations.append((transaction.read(retained), transaction.read(current), data))
            raise injected
        return actual_publish(transaction, parts, data)

    with monkeypatch.context() as patch:
        patch.setattr(prefs_native.Transaction, 'publish', fail_reset)
        expected = prefs.PreferenceOperationError if failure_kind is OSError else failure_kind
        with pytest.raises(expected) as caught:
            prefs.repair(user, operation_id=operation_id)
        if failure_kind is OSError:
            assert type(caught.value) is prefs.PreferenceOperationError
            assert caught.value.operation_id == operation_id
            assert str(caught.value) == f'preference recovery result is unconfirmed; operation {operation_id}'
            assert caught.value.__cause__ is None and caught.value.__context__ is None
        else:
            assert caught.value is injected
            assert caught.value.__dict__['_preference_recovery_operation'] == operation_id
        assert len(observations) == 1
        archived, original, intended = observations[0]
        assert archived[0] == original[0] == raw
        assert archived[1] is not None and original[1] is not None
        assert intended == prefs_records.encode({})
        assert path.read_bytes() == archive.read_bytes() == raw
        locations = prefs._recovery_locations(operation_id)
        assert prefs._io(config.MEMORY_DIR.joinpath(*locations['source'])).read_bytes() == raw
        assert prefs._io(config.MEMORY_DIR.joinpath(*locations['before'])).read_bytes() == raw
        assert prefs._io(config.MEMORY_DIR.joinpath(*locations['after'])).read_bytes() == intended
        io_root = prefs._io(config.MEMORY_DIR)
        before = {p.relative_to(io_root).as_posix(): p.read_bytes()
                  for p in io_root.rglob('*') if p.is_file()}
        with pytest.raises(prefs.PreferenceOperationError) as blocked:
            prefs.repair(user, operation_id='2' * 32)
        assert blocked.value.operation_id == operation_id
        assert len(observations) == 1
        assert {p.relative_to(io_root).as_posix(): p.read_bytes()
                for p in io_root.rglob('*') if p.is_file()} == before

    # Exact-ID retry resumes the fixed plan and reconfirms the existing archive.
    actual_write_new = prefs_native.Transaction.write_new
    recreated = []
    def no_archive_recreation(transaction, parts, data):
        if tuple(parts) == retained:
            recreated.append(tuple(parts))
            raise AssertionError('owned archive must be reconfirmed, not recreated')
        return actual_write_new(transaction, parts, data)
    with monkeypatch.context() as patch:
        patch.setattr(prefs_native.Transaction, 'write_new', no_archive_recreation)
        result = prefs.repair(user, operation_id=operation_id)
    assert not recreated
    assert result['repaired'] is True and result['completed'] is True
    assert result['operation_id'] == operation_id and result['quarantined_sha256'] == digest
    assert result['quarantine_file'] == archive.name
    assert prefs.load(user) == {} and path.read_bytes() == intended
    assert archive.read_bytes() == raw
    with prefs_native.transaction(config.MEMORY_DIR) as transaction:
        assert transaction.read(retained) == archived


def test_malformed_legacy_migration_preserves_source_until_explicit_discard(monkeypatch):
    from olympus import prefs_native
    legacy_id = 'legacy-owner'
    operation_id = '3' * 32
    path = config.MEMORY_DIR / 'users' / legacy_id / 'prefs.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b'{"autonomy": 0'
    path.write_bytes(raw)
    # Coordination first-use is an independent native effect, not a migration.
    # Establish it before the no-publication/no-data-change observation.
    with prefs_native.transaction(config.MEMORY_DIR):
        pass
    before = {p.relative_to(config.MEMORY_DIR).as_posix(): p.read_bytes()
              for p in config.MEMORY_DIR.rglob('*') if p.is_file()}
    before_directories = {p.relative_to(config.MEMORY_DIR).as_posix()
                          for p in config.MEMORY_DIR.rglob('*') if p.is_dir()}
    effects = []
    def forbidden(*args, **kwargs):
        effects.append('mutation')
        raise AssertionError('malformed legacy evidence must not dispatch a mutation')
    with monkeypatch.context() as patch:
        for name in ('publish', 'write_new', 'retire'):
            patch.setattr(prefs_native.Transaction, name, forbidden)
        with pytest.raises(prefs.PreferenceOperationError) as caught:
            prefs.migrate_legacy(legacy_id, 'verified-owner', operation_id=operation_id)
    assert type(caught.value) is prefs.PreferenceOperationError
    assert caught.value.operation_id == operation_id
    assert str(caught.value) == f'preference recovery evidence requires explicit reconciliation; operation {operation_id}'
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not effects and path.read_bytes() == raw
    assert {p.relative_to(config.MEMORY_DIR).as_posix(): p.read_bytes()
            for p in config.MEMORY_DIR.rglob('*') if p.is_file()} == before
    assert {p.relative_to(config.MEMORY_DIR).as_posix()
            for p in config.MEMORY_DIR.rglob('*') if p.is_dir()} == before_directories


@pytest.mark.parametrize('saved', ['broken', True, [], float('inf')])
def test_malformed_saved_budget_refuses_new_model_work(saved):
    if type(saved) is float:
        assert saved == float('inf')
        path = _corrupt('shared', b'{"daily_budget": Infinity}')
    else:
        prefs.set('shared', 'daily_budget', saved)
        path = prefs._path('shared')
    before = path.read_bytes()
    with pytest.raises(usage.BudgetPolicyUnavailable,
                       match='(?i)daily-budget policy evidence'):
        usage.check_budget()
    status = usage.budget_status()
    assert status['evidence_state'] == 'unavailable'
    assert status['enabled'] is True
    assert status['exceeded'] is True
    assert status['remaining'] == 0.0
    assert usage.budget_headroom_low() is True
    assert path.read_bytes() == before


def test_corrupt_shared_preferences_pause_work_and_surface_operator_status(
        monkeypatch):
    raw = b'{"daily_budget": 2.0'
    path = _corrupt("shared", raw)
    called = False

    def model_must_not_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("model boundary reached")

    from olympus import backend, orchestrator
    monkeypatch.setattr(backend, "complete_json", model_must_not_run)
    monkeypatch.setattr(backend, "complete_text", model_must_not_run)

    reply = orchestrator.Olympus(user="cli").ask("hello")
    view = dashboard.render()
    report = usage.report()

    assert called is False
    assert "daily-budget policy evidence" in reply.lower()
    assert "evidence unavailable" in view.lower()
    assert "new model work paused" in view.lower()
    assert "evidence unavailable" in report.lower()
    assert path.read_bytes() == raw


def test_cli_exposes_status_and_explicit_repair(capsys):
    user = "cli-repair"
    raw = b"not-json"
    _corrupt(user, raw)

    assert cli.main(["prefs-evidence", user]) == 1
    status_out = capsys.readouterr().out
    assert '"state": "unavailable"' in status_out
    assert "not-json" not in status_out

    assert cli.main(["prefs-evidence", user, "--repair"]) == 0
    repair_out = capsys.readouterr().out
    assert '"repaired": true' in repair_out
    assert prefs.load(user) == {}


def test_background_readers_never_invoke_preference_repair(monkeypatch):
    user = "no-auto-repair"
    raw = b"broken"
    path = _corrupt(user, raw)

    def forbidden(*args, **kwargs):
        raise AssertionError("background repair attempted")

    monkeypatch.setattr(prefs, "repair", forbidden)

    assert capprofile.of_user(user) == "guest"
    assert actions.autonomy_level(user) == 0
    assert operator.sites(user) == {}
    assert path.read_bytes() == raw


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')], ids=['nan', 'positive-infinity', 'negative-infinity'])
def test_nonfinite_budget_writer_refuses_without_replacing_valid_evidence(value):
    from olympus import prefs_records
    prefs.set('shared', 'daily_budget', 4.0)
    path = prefs._path('shared')
    before = path.read_bytes()
    with pytest.raises(prefs_records.PreferenceRecordError):
        prefs.set('shared', 'daily_budget', value)
    assert path.read_bytes() == before
    assert prefs.get('shared', 'daily_budget') == 4.0
