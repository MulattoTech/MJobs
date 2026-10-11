"""Opt-in provider credential restore-key check against a loopback disposable PostgreSQL database."""
import base64
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy.engine.url import make_url

from packages.persistence import models
from packages.persistence.store import Store
from services.credentials.vault import CredentialCipher
from cli.local_backup import BackupError, verify_credential_recoverability


POSTGRES_URL = os.environ.get("MJOBS_BACKUP_CREDENTIAL_TEST_POSTGRES_URL")
ORIGINAL_KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
WRONG_KEY = base64.urlsafe_b64encode(b"fedcba9876543210fedcba9876543210").decode("ascii")
SECRET = "synthetic-postgres-provider-secret"


@unittest.skipUnless(POSTGRES_URL, "Set MJOBS_BACKUP_CREDENTIAL_TEST_POSTGRES_URL to a disposable loopback database")
class PostgreSQLCredentialRecoverabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(POSTGRES_URL)
        if (not url.drivername.startswith("postgresql")
                or url.host not in ("localhost", "127.0.0.1", "::1")
                or not url.database
                or not url.database.startswith("mjobs_test_")):
            raise RuntimeError(
                "Credential recoverability PostgreSQL tests require a loopback database named mjobs_test_*"
            )
        cls.store = Store(POSTGRES_URL)
        models.metadata.create_all(cls.store.engine, tables=[models.profiles, models.provider_credentials])

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "store"):
            cls.store.engine.dispose()

    def test_restored_ciphertext_requires_original_key_and_stays_unchanged(self):
        profile_id = "synthetic-restore-" + uuid4().hex
        credential_id = "synthetic-credential-" + uuid4().hex
        ciphertext = CredentialCipher.from_key_material(ORIGINAL_KEY).encrypt(profile_id, "openai", SECRET)
        with self.store.engine.begin() as connection:
            connection.execute(models.profiles.insert().values(
                id=profile_id, name="Synthetic", email="synthetic@example.invalid", version="v1"))
            connection.execute(models.provider_credentials.insert().values(
                id=credential_id, profile_id=profile_id, provider="openai", encrypted_secret=ciphertext,
                created_at="2026-10-11T00:00:00Z", updated_at="2026-10-11T00:00:00Z",
                last_test_status="NOT_TESTED", rotation_count=0))
        try:
            with patch.dict(os.environ, {"MJOBS_CREDENTIAL_ENCRYPTION_KEY": ORIGINAL_KEY}):
                self.assertGreaterEqual(verify_credential_recoverability(POSTGRES_URL), 1)
            for key in (WRONG_KEY, None):
                environment = {"MJOBS_CREDENTIAL_ENCRYPTION_KEY": key} if key else {}
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(BackupError) as error:
                        verify_credential_recoverability(POSTGRES_URL)
                self.assertNotIn(SECRET, str(error.exception))
                self.assertNotIn(ORIGINAL_KEY, str(error.exception))
                self.assertNotIn(WRONG_KEY, str(error.exception))
            with self.store.engine.connect() as connection:
                row = connection.execute(models.provider_credentials.select().where(
                    models.provider_credentials.c.id == credential_id)).mappings().one()
            self.assertEqual(row["encrypted_secret"], ciphertext)
            self.assertEqual(row["rotation_count"], 0)
        finally:
            with self.store.engine.begin() as connection:
                connection.execute(models.provider_credentials.delete().where(
                    models.provider_credentials.c.id == credential_id))
                connection.execute(models.profiles.delete().where(models.profiles.c.id == profile_id))


if __name__ == "__main__":
    unittest.main()
