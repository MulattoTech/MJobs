"""Restore verifies saved credential ciphertext with the configured original key."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packages.persistence import models
from packages.persistence.store import Store
from services.credentials.vault import CredentialCipher, CredentialConfigurationError

from cli import local_backup


SECRET = "synthetic-provider-secret-for-restore"
ORIGINAL_KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
WRONG_KEY = base64.urlsafe_b64encode(b"fedcba9876543210fedcba9876543210").decode("ascii")


class BackupCredentialRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "restore.sqlite"
        self.store = Store(f"sqlite:///{self.database}")
        self.store.migrate()
        with self.store.engine.begin() as connection:
            connection.execute(models.profiles.insert().values(
                id="synthetic-profile", name="Synthetic", email="synthetic@example.invalid", version="v1"))
            connection.execute(models.provider_credentials.insert().values(
                id="synthetic-credential", profile_id="synthetic-profile", provider="openai",
                encrypted_secret=CredentialCipher.from_key_material(ORIGINAL_KEY).encrypt(
                    "synthetic-profile", "openai", SECRET),
                created_at="2026-10-11T00:00:00Z", updated_at="2026-10-11T00:00:00Z",
                last_test_status="NOT_TESTED", rotation_count=0))

    def tearDown(self):
        self.store.engine.dispose()
        self.temporary.cleanup()

    def test_recoverability_check_accepts_original_key_and_rejects_wrong_or_missing_key_safely(self):
        with self.store.engine.connect() as connection:
            original_ciphertext = connection.execute(models.provider_credentials.select()).mappings().one()[
                "encrypted_secret"]
        with patch.dict(os.environ, {"MJOBS_CREDENTIAL_ENCRYPTION_KEY": ORIGINAL_KEY}):
            self.assertEqual(local_backup.verify_credential_recoverability(
                f"sqlite:///{self.database}"), 1)

        for key in (WRONG_KEY, None):
            with self.subTest(key_present=key is not None):
                environment = {"MJOBS_CREDENTIAL_ENCRYPTION_KEY": key} if key else {}
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(local_backup.BackupError) as error:
                        local_backup.verify_credential_recoverability(f"sqlite:///{self.database}")
                self.assertNotIn(SECRET, str(error.exception))
                self.assertNotIn(ORIGINAL_KEY, str(error.exception))
                self.assertNotIn(WRONG_KEY, str(error.exception))
                self.assertNotIn("CREDENTIAL_STORE_UNAVAILABLE", str(error.exception))

        with self.store.engine.connect() as connection:
            row = connection.execute(models.provider_credentials.select()).mappings().one()
        encrypted = row["encrypted_secret"]
        self.assertEqual(encrypted, original_ciphertext)
        self.assertEqual(row["rotation_count"], 0)
        with self.assertRaises(CredentialConfigurationError):
            CredentialCipher.from_key_material(WRONG_KEY).decrypt(
                "synthetic-profile", "openai", encrypted)

    def test_restore_checks_credentials_before_starting_application(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "archive"
            (archive / "documents").mkdir(parents=True)
            (archive / "database.dump").write_bytes(b"synthetic-database-dump")
            document = archive / "documents" / "synthetic.pdf"
            document.write_bytes(b"synthetic-document")
            manifest = {
                "schema_version": 1,
                "source_project": "synthetic-source",
                "operation_mode": "PERSONAL",
                "alembic_revision": "synthetic-revision",
                "compose_sha256": local_backup.checksum(local_backup.ROOT / "compose.yml"),
                "files": {
                    "database.dump": hashlib.sha256(b"synthetic-database-dump").hexdigest(),
                    "documents/synthetic.pdf": hashlib.sha256(b"synthetic-document").hexdigest(),
                },
            }
            (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            compose_calls = []

            def fake_compose(_project, *arguments, **_kwargs):
                compose_calls.append(arguments)
                return b""

            with patch.object(local_backup, "verify_backup", return_value=manifest), \
                 patch.object(local_backup, "_config", return_value={}), \
                 patch.object(local_backup, "_compose", side_effect=fake_compose), \
                 patch.object(local_backup, "_sql", side_effect=("0", "synthetic-revision", "PERSONAL")), \
                 patch.object(local_backup, "_volumes", return_value=[]), \
                 patch.object(local_backup, "_run", return_value=b""), \
                 patch.object(local_backup, "_container", return_value="synthetic-api-container"), \
                 patch.object(local_backup, "_wait_ready"):
                local_backup.restore_backup(archive, "synthetic-recovered", 18712)

            credential_check = next(index for index, call in enumerate(compose_calls)
                                    if "verify-credentials" in call)
            runtime_provision = next(index for index, call in enumerate(compose_calls)
                                     if "cli.provision_database" in call)
            self.assertLess(runtime_provision, credential_check)
            self.assertIn("--provision-only", compose_calls[runtime_provision])
            application_start = next(index for index, call in enumerate(compose_calls)
                                     if "up" in call and "--no-build" in call)
            self.assertLess(credential_check, application_start)

    def test_restore_stops_before_document_or_application_start_when_key_check_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "archive"
            (archive / "documents").mkdir(parents=True)
            (archive / "database.dump").write_bytes(b"synthetic-database-dump")
            (archive / "manifest.json").write_text("{}", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "source_project": "synthetic-source",
                "operation_mode": "PERSONAL",
                "alembic_revision": "synthetic-revision",
                "compose_sha256": local_backup.checksum(local_backup.ROOT / "compose.yml"),
                "files": {"database.dump": hashlib.sha256(b"synthetic-database-dump").hexdigest()},
            }
            compose_calls = []

            def fail_credential_check(_project, *arguments, **_kwargs):
                compose_calls.append(arguments)
                if "verify-credentials" in arguments:
                    raise local_backup.BackupError("synthetic key verification failure")
                return b""

            with patch.object(local_backup, "verify_backup", return_value=manifest), \
                 patch.object(local_backup, "_config", return_value={}), \
                 patch.object(local_backup, "_compose", side_effect=fail_credential_check), \
                 patch.object(local_backup, "_sql", side_effect=("0", "synthetic-revision", "PERSONAL")), \
                 patch.object(local_backup, "_volumes", return_value=[]), \
                 patch.object(local_backup, "_run", return_value=b""):
                with self.assertRaisesRegex(local_backup.BackupError, "key verification failure"):
                    local_backup.restore_backup(archive, "synthetic-recovered", 18712)

            self.assertTrue(any("verify-credentials" in call for call in compose_calls))
            self.assertFalse(any("create" in call and "api" in call for call in compose_calls))
            self.assertFalse(any("--no-build" in call for call in compose_calls))


if __name__ == "__main__":
    unittest.main()
