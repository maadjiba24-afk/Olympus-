"""Owned local readiness controls with no remote recovery qualification.

No test contacts a network service, restarts a real container or reboots a host.
Lifecycle identities and mount/disk conditions are mocked. Backup integration
uses actual owned filesystem publication, encryption and offline restore, with
uploader dispatch mocked. M14 target-bound remote proof remains unavailable.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from olympus import backup, config, deployreadiness, backup_evidence, backup_workflow
from test_backup_delivery import owned, uploader, files, native_io as native


COMMIT = "a" * 40


class DeploymentReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Path(self.temp.name) / "memory"
        self.memory.mkdir(mode=0o700)
        if os.name == "posix":
            os.chmod(self.memory, 0o700)
        self.memory_mode = 0o700
        real_path_stat = Path.stat

        def controlled_path_stat(path, *args, **kwargs):
            """Give the mock memory mount deterministic POSIX mode bits.

            Windows reports temporary directories as mode 0777 regardless of
            the requested mkdir mode.  P2A production checks must keep failing
            closed there; only this local/mock fixture supplies the Linux
            permission contract that the container deployment guarantees.
            """
            result = real_path_stat(path, *args, **kwargs)
            if path == self.memory:
                values = list(result)
                values[0] = ((values[0] & ~0o777)
                             | self.memory_mode)
                return os.stat_result(values)
            return result

        self.old_memory = config.MEMORY_DIR
        config.MEMORY_DIR = self.memory
        self.env = mock.patch.dict(os.environ, {
            "OLYMPUS_ENV": "production",
            "OLYMPUS_MEMORY_DIR": str(self.memory),
            "OLYMPUS_BUILD_COMMIT": COMMIT,
            "OLYMPUS_SECRET_KEY": "test-only-secret-0123456789abcdef",
            "OLYMPUS_SECRET_KEY_FILE": "",
            "OLYMPUS_BACKUP_EVERY": "86400",
            "OLYMPUS_DATABASE_URL": "",
            "OLYMPUS_MIN_FREE_BYTES": "1024",
            "OLYMPUS_BACKUP_MAX_AGE": "172800",
            "OLYMPUS_DURABILITY_EVIDENCE_MAX_AGE": "2592000",
            "OLYMPUS_DURABILITY_CHALLENGE_MAX_AGE": "86400",
        }, clear=False)
        self.env.start()
        self.patches = [
            mock.patch.object(config, "build_info", return_value={
                "version": "test", "commit": COMMIT, "env": "production"}),
            mock.patch.object(config, "backup_command",
                              return_value="mock-uploader {path}"),
            mock.patch("os.path.ismount", return_value=True),
            mock.patch.object(deployreadiness.shutil, "disk_usage",
                              return_value=SimpleNamespace(free=1 << 30)),
            mock.patch.object(Path, "stat", autospec=True,
                              side_effect=controlled_path_stat),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.env.stop()
        config.MEMORY_DIR = self.old_memory
        self.temp.cleanup()

    @staticmethod
    def _check(report, name):
        return next(item for item in report["checks"] if item["name"] == name)

    def _record_lifecycle(self):
        with mock.patch.object(deployreadiness, "container_id",
                               return_value="container-old"):
            deployreadiness.challenge("container")
        with mock.patch.object(deployreadiness, "container_id",
                               return_value="container-new"):
            deployreadiness.verify("container")
        with mock.patch.object(deployreadiness, "host_boot_id",
                               return_value="boot-old"):
            deployreadiness.challenge("host")
        with mock.patch.object(deployreadiness, "host_boot_id",
                               return_value="boot-new"):
            deployreadiness.verify("host")

    def _record_recovery(self, *, delivered=True, encrypted=True, signed=True,
                         restore_sha="b" * 64):
        # Retain the old on-disk schema as an explicit compatibility fixture.
        # These caller assertions must never qualify the replacement contract.
        common = {"schema": deployreadiness.SCHEMA, "recorded_at": time.time(), "commit": COMMIT}
        deployreadiness._atomic_json(deployreadiness.evidence_dir() / deployreadiness.BACKUP_RECEIPT,
            {**common, "record_type": "backup_receipt", "archive_name": "legacy.enc",
             "sha256": "b" * 64, "encrypted": encrypted, "signed": signed,
             "delivered": delivered, "signature_delivered": signed and delivered})
        deployreadiness._atomic_json(deployreadiness.evidence_dir() / deployreadiness.RESTORE_RECEIPT,
            {**common, "record_type": "restore_receipt", "archive_name": "legacy.enc",
             "sha256": restore_sha, "restored_files": 3, "signed": True, "signature_ok": True})

    def test_complete_legacy_mock_evidence_cannot_qualify_remote_recovery(self):
        self._record_lifecycle()
        self._record_recovery()
        before = files(self.memory)
        report = deployreadiness.report()
        self.assertFalse(report["ready"])
        self.assertEqual({item["name"] for item in report["checks"] if item["status"] != "pass"},
                         {"recoverable_off_machine_backup", "restore_drill"})
        self.assertEqual(files(self.memory), before)
        for name in ("recoverable_off_machine_backup", "restore_drill"):
            check = self._check(report, name)
            self.assertFalse(check["remote_recovery_verified"])
            self.assertTrue(check["local_receipts"]["observational_only"])

    def test_placeholder_vault_key_cannot_satisfy_encryption_readiness(self):
        os.environ["OLYMPUS_SECRET_KEY"] = "REPLACE_WITH_RANDOM_HEX"

        check = self._check(deployreadiness.report(), "backup_encryption")

        self.assertEqual(check["status"], "fail")
        self.assertIn("unusable", check["detail"])
        self.assertNotIn("REPLACE_WITH_RANDOM_HEX", check["detail"])

    def test_named_mode_mount_permissions_disk_and_commit_fail_closed(self):
        self.memory_mode = 0o755
        os.environ["OLYMPUS_BUILD_COMMIT"] = "unknown"
        config.build_info.return_value = {  # type: ignore[attr-defined]
            "version": "test", "commit": "unknown", "env": "production"}
        with mock.patch("os.path.ismount", return_value=False), \
             mock.patch.object(deployreadiness.shutil, "disk_usage",
                               return_value=SimpleNamespace(free=1)):
            report = deployreadiness.report()

        for name in ("memory_mount", "private_memory_permissions",
                     "disk_headroom", "build_commit"):
            self.assertEqual(self._check(report, name)["status"], "fail")

    def test_database_backend_never_gets_false_file_backup_green(self):
        os.environ["OLYMPUS_DATABASE_URL"] = "postgresql://local/mock"
        self._record_lifecycle()
        self._record_recovery()

        report = deployreadiness.report()

        check = self._check(report, "database_coverage")
        self.assertEqual(check["status"], "fail")
        self.assertIn("database-backup receipt", check["detail"])

    def test_production_boot_requires_explicit_memory_dir(self):
        del os.environ["OLYMPUS_MEMORY_DIR"]

        problems = config.production_problems()

        self.assertTrue(any("OLYMPUS_MEMORY_DIR is unset" in item
                            for item in problems), problems)

    def test_same_identity_and_stale_challenge_are_refused(self):
        with mock.patch.object(deployreadiness, "container_id",
                               return_value="same"):
            deployreadiness.challenge("container")
            with self.assertRaisesRegex(
                    deployreadiness.DeploymentEvidenceError,
                    "identity did not change"):
                deployreadiness.verify("container")

        challenge = json.loads(
            deployreadiness._challenge_path("container").read_text())
        challenge["created_at"] = time.time() - 90000
        deployreadiness._atomic_json(
            deployreadiness._challenge_path("container"), challenge)
        with mock.patch.object(deployreadiness, "container_id",
                               return_value="different"):
            with self.assertRaisesRegex(
                    deployreadiness.DeploymentEvidenceError, "stale"):
                deployreadiness.verify("container")

    def test_legacy_plain_unsigned_receipts_do_not_qualify(self):
        self._record_lifecycle()
        self._record_recovery(delivered=False, encrypted=False, signed=False)

        report = deployreadiness.report()

        self.assertEqual(
            self._check(report, "recoverable_off_machine_backup")["status"],
            "fail")

    def test_legacy_mismatched_restore_receipt_does_not_qualify(self):
        self._record_lifecycle()
        self._record_recovery(restore_sha="c" * 64)

        report = deployreadiness.report()

        self.assertEqual(self._check(report, "restore_drill")["status"],
                         "fail")

    def test_legacy_malformed_receipt_does_not_qualify_or_break_readiness(self):
        self._record_lifecycle()
        self._record_recovery()
        path = (deployreadiness.evidence_dir()
                / deployreadiness.RESTORE_RECEIPT)
        receipt = json.loads(path.read_text())
        receipt["restored_files"] = "not-a-number"
        deployreadiness._atomic_json(path, receipt)

        report = deployreadiness.report()

        self.assertFalse(report["ready"])
        self.assertEqual(self._check(report, "restore_drill")["status"],
                         "fail")

    def test_legacy_receipts_are_never_read_for_qualification_and_remain_intact(self):
        self._record_lifecycle()
        self._record_recovery()
        before = files(self.memory)
        read_json = deployreadiness._read_json
        def only_lifecycle(path):
            self.assertNotIn(path.name, (deployreadiness.BACKUP_RECEIPT, deployreadiness.RESTORE_RECEIPT))
            return read_json(path)
        with mock.patch.object(deployreadiness, '_read_json', side_effect=only_lifecycle):
            report = deployreadiness.report()
        self.assertFalse(report['ready'])
        self.assertEqual(files(self.memory), before)

    @unittest.skipUnless(os.name == "posix", "POSIX mode contract")
    def test_lifecycle_evidence_directory_and_receipts_are_private(self):
        self._record_lifecycle()
        directory = deployreadiness.evidence_dir()
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for kind in ("host", "container"):
            self.assertEqual(deployreadiness._receipt_path(kind).stat().st_mode & 0o777, 0o600)


@pytest.fixture
def backup_policy(monkeypatch):
    monkeypatch.setattr(config, 'backup_keep', lambda: 2)
    monkeypatch.setattr(config, 'build_info', lambda: {'commit': COMMIT})


def test_run_records_actual_stage_results_without_legacy_writer(owned, monkeypatch, backup_policy):
    def forbidden(*args, **kwargs):
        pytest.fail('legacy caller-asserted writer used')
    monkeypatch.setattr(deployreadiness, 'record_backup', forbidden)
    result = backup.run(deliver_off=False)
    assert result['ok'] and result['evidence_recorded']
    observed = backup_evidence.inspect()
    assert len(observed['receipts']) == 1
    receipt = observed['receipts'][0]['receipt']
    assert receipt['publication']['archive_sha256'] == result['sha256']
    assert receipt['delivery']['status'] == 'explicitly_skipped'
    assert receipt['remote_retention_verified'] is False
    assert not native(owned / 'deployment' / deployreadiness.BACKUP_RECEIPT).exists()


def test_delivery_sends_actual_archive_and_signature_without_remote_claim(owned, monkeypatch, backup_policy):
    created = backup.create()
    calls = uploader(monkeypatch)
    result = backup.deliver(created['path'])
    assert [row[0] for row in calls] == ['archive', 'signature']
    image = Path(created['path'])
    assert calls[0][2] == native(image).read_bytes()
    assert calls[1][2] == native(image.with_name(image.name + '.sig.json')).read_bytes()
    assert result['delivery_commands_complete'] is True and result['remote_retention_verified'] is False


def test_drill_retains_owned_target_and_records_actual_consumed_hash(owned, tmp_path, monkeypatch, backup_policy):
    created = backup.create()
    parent = tmp_path / 'retained-drill'
    def allocate(**kwargs):
        native(parent).mkdir()
        return str(parent)
    def forbidden(*args, **kwargs):
        pytest.fail('legacy writer or separate archive verification used')
    monkeypatch.setattr(backup_workflow.tempfile, 'mkdtemp', allocate)
    monkeypatch.setattr(backup, 'verify_archive', forbidden)
    monkeypatch.setattr(deployreadiness, 'record_restore', forbidden)
    result = backup.drill(created['path'])
    assert result['ok'] and result['restore_complete'] and result['evidence_recorded']
    assert native(parent / 'restored' / 'data.txt').read_bytes() == b'owned delivery image'
    assert result['sha256'] == created['sha256'] == result['restore']['consumed_archive_sha256']
    receipt = backup_evidence.inspect()['receipts'][0]['receipt']
    assert receipt['restore']['archive_sha256'] == created['sha256']
    assert receipt['enclosing_workflow_complete'] is False


@pytest.mark.parametrize('kind', ['backup', 'restore'])
def test_legacy_writer_refuses_without_coercion_or_files(owned, kind):
    class NoRead(dict):
        def get(self, *args, **kwargs):
            pytest.fail('legacy writer inspected caller data')
    before = files(owned)
    with pytest.raises(deployreadiness.DeploymentEvidenceError, match='legacy caller-supplied'):
        if kind == 'backup':
            deployreadiness.record_backup(NoRead())
        else:
            deployreadiness.record_restore(NoRead(), archive_name='legacy.enc', sha256='a' * 64)
    assert files(owned) == before


def test_native_receipt_observation_never_claims_workflow_or_remote_recovery(owned, tmp_path, monkeypatch, backup_policy):
    result = backup.run(deliver_off=False)
    backup.drill(result['path'], into=tmp_path / 'offline')
    before = files(owned)
    observed = deployreadiness._local_backup_observation()
    assert observed['state'] == 'observed' and observed['visible_receipts'] == {'backup': 1, 'restore': 1}
    assert observed['observational_only'] is True and observed['write_acknowledgment_observed'] is None
    for name in ('enclosing_workflow_complete', 'current_archive_availability_verified',
                 'remote_retention_verified', 'deployment_target_binding_verified'):
        assert observed[name] is False
    assert files(owned) == before


def test_unavailable_native_evidence_is_generic_and_not_empty(owned, monkeypatch):
    def unavailable():
        raise RuntimeError('owned-private-observer-detail')
    monkeypatch.setattr(backup_evidence, 'inspect', unavailable)
    result = deployreadiness._local_backup_observation()
    assert result['state'] == 'unavailable' and 'visible_receipts' not in result
    assert 'owned-private-observer-detail' not in json.dumps(result)
    assert result['remote_retention_verified'] is False



class SurfaceIntegrationTests(unittest.TestCase):
    def test_cli_and_web_are_wired_to_deployment_readiness(self):
        root = Path(__file__).resolve().parent.parent
        cli = (root / "olympus" / "cli.py").read_text(encoding="utf-8")
        web = (root / "olympus" / "web.py").read_text(encoding="utf-8")
        self.assertIn('"deployment", help="evidence-backed', cli)
        self.assertIn('backup_commands.add_backup_parser(sub)', cli)
        from olympus import cli as cli_module
        self.assertEqual(cli_module.build_parser().parse_args(['backup', '--drill']).drill, '')
        self.assertIn("deployreadiness.report()", web)
        self.assertIn('payload["deployment_readiness"]', web)

    def test_production_profile_structurally_pins_mode_mount_and_readiness(self):
        root = Path(__file__).resolve().parent.parent
        compose = (root / "deploy" / "docker-compose.yml").read_text(
            encoding="utf-8")
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        self.assertGreaterEqual(compose.count("OLYMPUS_ENV: production"), 3)
        self.assertGreaterEqual(compose.count("OLYMPUS_MEMORY_DIR: /app/memory"),
                                3)
        self.assertIn("http://127.0.0.1:8484/readyz", compose)
        self.assertIn('profiles: ["autonomy"]', compose)
        self.assertIn("chmod 0700 /app/memory", dockerfile)


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize('encrypted', [False, True])
def test_actual_local_command_pair_and_drill_still_cannot_qualify_remote_readiness(owned, tmp_path,
        monkeypatch, backup_policy, encrypted):
    if encrypted:
        monkeypatch.setenv('OLYMPUS_SECRET_KEY', 'owned-readiness-encryption-custody')
        monkeypatch.delenv('OLYMPUS_BACKUP_ALLOW_PLAINTEXT')
    calls = uploader(monkeypatch)
    result = backup.run()
    drilled = backup.drill(result['path'], into=tmp_path / 'offline')
    assert result['ok'] and result['delivery_commands_complete'] and drilled['ok']
    assert result['signer_authenticated'] and not result['signer_default']
    assert drilled['restore']['signer_authenticated'] and not drilled['restore']['signature_override']
    for metadata in (result['archive_encryption'], drilled['restore']['archive_encryption']):
        assert metadata['encrypted'] is encrypted and metadata['context_authenticated'] is encrypted
        assert metadata['format_version'] == (1 if encrypted else None)
    assert len(calls) == 2
    before = files(owned)
    report = deployreadiness.report()
    assert not report['ready']
    for name in ('recoverable_off_machine_backup', 'restore_drill'):
        check = DeploymentReadinessTests._check(report, name)
        assert check['status'] == 'fail' and check['remote_recovery_verified'] is False
        assert check['local_receipts']['visible_receipts'] == {'backup': 1, 'restore': 1}
        assert check['local_receipts']['deployment_target_binding_verified'] is False
    assert files(owned) == before and len(calls) == 2


def test_actual_corrupt_evidence_is_unavailable_without_overwrite_or_error_text(owned, monkeypatch, backup_policy):
    result = backup.run(deliver_off=False)
    path = owned / backup_evidence.CONTROL / ('backup-' + result['requested_operation_id'] + '.json')
    native(path).write_bytes(b'owned-private-malformed-receipt')
    before = files(owned)
    observed = deployreadiness._local_backup_observation()
    assert observed['state'] == 'unavailable' and 'visible_receipts' not in observed
    assert 'owned-private-malformed-receipt' not in json.dumps(observed)
    assert files(owned) == before


@pytest.mark.parametrize('interrupt_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('entrypoint', ['_local_backup_observation', 'report'])
def test_readiness_does_not_swallow_system_interruption(owned, monkeypatch, interrupt_type, entrypoint):
    original = interrupt_type('owned-private-readiness-interruption')
    def interrupt():
        raise original
    monkeypatch.setattr(backup_evidence, 'inspect', interrupt)
    with pytest.raises(interrupt_type) as raised:
        getattr(deployreadiness, entrypoint)()
    assert raised.value is original
