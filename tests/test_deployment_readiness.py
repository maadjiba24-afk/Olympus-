"""Local/mock contracts for P2A deployment durability readiness.

No test contacts a network service, restarts a real container, or reboots a
host. Runtime identities, mount detection, disk usage, delivery, and restore are
all controlled local doubles; the operator procedure collects the real proof.
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

from olympus import backup, config, deployreadiness


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
        backup_sha = "b" * 64
        deployreadiness.record_backup({
            "name": "olympus-backup-test.tar.gz.enc",
            "sha256": backup_sha,
            "delivered": delivered,
            "encrypted": encrypted,
            "signed": signed,
            "signature_delivered": signed and delivered,
            "via": "mock-uploader",
        })
        deployreadiness.record_restore(
            {"restored": 3, "signed": True, "signature_ok": True},
            archive_name="olympus-backup-test.tar.gz.enc",
            sha256=restore_sha)

    def test_complete_mock_evidence_is_ready(self):
        self._record_lifecycle()
        self._record_recovery()

        report = deployreadiness.report()

        self.assertTrue(report["ready"], report["problems"])
        self.assertEqual(report["problems"], [])
        self.assertTrue(all(item["status"] == "pass"
                            for item in report["checks"]))

    def test_placeholder_vault_key_cannot_satisfy_encryption_readiness(self):
        os.environ["OLYMPUS_SECRET_KEY"] = "REPLACE_WITH_RANDOM_HEX"

        check = self._check(deployreadiness.report(), "backup_encryption")

        self.assertEqual(check["status"], "fail")
        self.assertIn("placeholder", check["detail"])

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

    def test_local_plain_or_unsigned_backup_cannot_pass(self):
        self._record_lifecycle()
        self._record_recovery(delivered=False, encrypted=False, signed=False)

        report = deployreadiness.report()

        self.assertEqual(
            self._check(report, "recoverable_off_machine_backup")["status"],
            "fail")

    def test_restore_receipt_must_match_delivered_archive(self):
        self._record_lifecycle()
        self._record_recovery(restore_sha="c" * 64)

        report = deployreadiness.report()

        self.assertEqual(self._check(report, "restore_drill")["status"],
                         "fail")

    def test_malformed_receipt_fails_instead_of_breaking_readiness(self):
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

    @unittest.skipUnless(os.name == "posix", "POSIX mode contract")
    def test_evidence_directory_and_receipts_are_private(self):
        receipt = deployreadiness.record_backup({
            "name": "x.enc", "sha256": "b" * 64, "encrypted": True,
            "signed": True, "delivered": True, "signature_delivered": True,
            "via": "mock"})
        self.assertEqual(receipt["schema"], deployreadiness.SCHEMA)
        directory = deployreadiness.evidence_dir()
        path = directory / deployreadiness.BACKUP_RECEIPT
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class BackupEvidenceIntegrationTests(unittest.TestCase):
    def test_run_records_actual_backup_result(self):
        created = {
            "path": "/mock/archive.enc", "name": "archive.enc",
            "sha256": "d" * 64, "encrypted": True, "signed": True,
            "files": 4, "bytes": 100, "full": False,
        }
        with mock.patch.object(backup, "create", return_value=created), \
             mock.patch.object(backup, "deliver",
                               return_value={"delivered": True,
                                             "signature_delivered": True,
                                             "via": "mock"}), \
             mock.patch.object(backup, "prune", return_value=0), \
             mock.patch.object(deployreadiness, "record_backup") as record:
            result = backup.run()

        self.assertTrue(result["ok"] and result["evidence_recorded"])
        record.assert_called_once()
        self.assertTrue(record.call_args.args[0]["delivered"])

    def test_delivery_sends_archive_and_signature_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "archive.tar.gz.enc"
            sidecar = Path(str(archive) + ".sig.json")
            archive.write_bytes(b"archive")
            sidecar.write_text("{}")
            sent = []

            def fake_run(argv, **_kwargs):
                sent.append(argv[-1])
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with mock.patch.object(config, "backup_command",
                                   return_value="mock-uploader {path}"), \
                 mock.patch.object(backup.subprocess, "run",
                                   side_effect=fake_run):
                result = backup.deliver(str(archive))

        self.assertEqual(sent, [str(archive), str(sidecar)])
        self.assertTrue(result["delivered"])
        self.assertTrue(result["signature_delivered"])

    def test_drill_uses_temporary_target_and_records_archive_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "archive.enc"
            archive.write_bytes(b"mock")
            seen = {}

            def fake_restore(_archive, into=None, **_kwargs):
                seen["into"] = Path(into)
                self.assertTrue(seen["into"].exists())
                return {"restored": 2, "signed": True, "signature_ok": True,
                        "mismatched": [], "into": str(into)}

            with mock.patch.object(backup, "verify_archive", return_value={
                    "signed": True, "signature_ok": True,
                    "sha256": "e" * 64}), \
                 mock.patch.object(backup, "restore", side_effect=fake_restore), \
                 mock.patch.object(deployreadiness, "record_restore",
                                   return_value={"schema": deployreadiness.SCHEMA}) \
                        as record:
                result = backup.drill(str(archive))

        self.assertFalse(seen["into"].exists())
        self.assertEqual(result["sha256"], "e" * 64)
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["sha256"], "e" * 64)


class SurfaceIntegrationTests(unittest.TestCase):
    def test_cli_and_web_are_wired_to_deployment_readiness(self):
        root = Path(__file__).resolve().parent.parent
        cli = (root / "olympus" / "cli.py").read_text(encoding="utf-8")
        web = (root / "olympus" / "web.py").read_text(encoding="utf-8")
        self.assertIn('"deployment", help="evidence-backed', cli)
        self.assertIn('"--drill"', cli)
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
