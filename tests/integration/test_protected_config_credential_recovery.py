"""Recovered protected configuration decrypts unchanged database credentials."""
import base64
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from cli import protected_config_backup as backup
from services.credentials.vault import CredentialCipher


DEPENDENCIES = all(importlib.util.find_spec(name) is not None for name in ("sqlalchemy", "alembic"))
ORIGINAL_KEY = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
SECRET = "synthetic-config-restored-provider-secret"


@unittest.skipUnless(DEPENDENCIES, "Credential recovery requires SQLAlchemy and Alembic")
class ProtectedConfigCredentialRecoverySQLiteTests(unittest.TestCase):
    def setUp(self):
        from packages.persistence import models
        from packages.persistence.store import Store

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("source", "keys", "archives", "recovery"):
            (self.root / name).mkdir(mode=0o700)
        self.url = self.database_url()
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        models.metadata.create_all(self.store.engine, tables=[models.profiles, models.provider_credentials])
        self.profile_id = "synthetic-config-" + uuid4().hex
        self.credential_id = "synthetic-config-credential-" + uuid4().hex
        self.ciphertext = CredentialCipher.from_key_material(ORIGINAL_KEY).encrypt(self.profile_id, "openai", SECRET)
        with self.store.engine.begin() as connection:
            connection.execute(models.profiles.insert().values(
                id=self.profile_id, name="Synthetic", email="synthetic@example.invalid", version="v1"))
            connection.execute(models.provider_credentials.insert().values(
                id=self.credential_id, profile_id=self.profile_id, provider="openai", encrypted_secret=self.ciphertext,
                created_at="2026-11-02T00:00:00Z", updated_at="2026-11-02T00:00:00Z",
                last_test_status="NOT_TESTED", rotation_count=0))

    def database_url(self):
        return f"sqlite:///{self.root / 'credentials.sqlite'}"

    @staticmethod
    def write_private(path, value):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)

    def test_recovered_exact_configuration_is_the_key_to_unchanged_database_ciphertext(self):
        from cli.local_backup import BackupError, verify_credential_recoverability
        from packages.persistence import models

        source = self.root / "source"
        original = ("POSTGRES_PASSWORD=synthetic-database-password\r\n"
                    f"MJOBS_CREDENTIAL_ENCRYPTION_KEY={ORIGINAL_KEY}\r\n").encode("ascii")
        override = b"services:\n  api:\n    environment:\n      PRIVATE_SETTING: synthetic-restored-setting\n"
        self.write_private(source / ".env", original)
        self.write_private(source / "compose.private.yml", override)
        key = self.root / "keys/recovery.key"
        archive = self.root / "archives/config.mjpc"
        restored = self.root / "recovery/restored"
        backup.generate_key(key)
        self.assertNotEqual(key.read_bytes(), base64.urlsafe_b64decode(ORIGINAL_KEY))
        self.assertEqual(backup.backup_config(source, [".env", "compose.private.yml"], key, archive), 2)

        # Remove only the synthetic fixture's original configuration. The proof
        # must actually recover the credential key from the encrypted archive.
        (source / ".env").unlink()
        (source / "compose.private.yml").unlink()
        with patch.dict(os.environ, {"MJOBS_CREDENTIAL_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"w" * 32).decode()}):
            with self.assertRaises(BackupError):
                verify_credential_recoverability(self.url)
        self.assertEqual(backup.restore_config(archive, key, restored), 2)
        self.assertEqual((restored / ".env").read_bytes(), original)
        self.assertEqual((restored / "compose.private.yml").read_bytes(), override)

        # Parse only this controlled synthetic fixture. Production restore never
        # interprets dotenv files, sources a shell, or applies configuration.
        recovered = dict(line.split("=", 1) for line in (restored / ".env").read_text().splitlines())
        with patch.dict(os.environ, {"MJOBS_CREDENTIAL_ENCRYPTION_KEY": recovered["MJOBS_CREDENTIAL_ENCRYPTION_KEY"]}):
            self.assertEqual(verify_credential_recoverability(self.url), 1)
        with self.store.engine.connect() as connection:
            row = connection.execute(models.provider_credentials.select()).mappings().one()
        self.assertEqual(row["encrypted_secret"], self.ciphertext)
        self.assertEqual(row["rotation_count"], 0)
        self.assertEqual(CredentialCipher.from_key_material(recovered["MJOBS_CREDENTIAL_ENCRYPTION_KEY"]).decrypt(
            self.profile_id, "openai", row["encrypted_secret"]), SECRET)
        self.assertNotIn(ORIGINAL_KEY.encode(), archive.read_bytes())
        self.assertNotIn(SECRET.encode(), archive.read_bytes())


@unittest.skipUnless(DEPENDENCIES and os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                     "Requires explicit disposable loopback MJOBS_TEST_POSTGRES_URL and database dependencies")
class ProtectedConfigCredentialRecoveryPostgreSQLTests(ProtectedConfigCredentialRecoverySQLiteTests):
    def database_url(self):
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine.url import make_url

        url = make_url(os.environ["MJOBS_TEST_POSTGRES_URL"])
        if (url.drivername not in {"postgresql", "postgresql+psycopg"}
                or url.host not in {"localhost", "127.0.0.1", "::1"} or url.query
                or not url.database or not (url.database == "mjobs_test" or url.database.startswith("mjobs_test_"))):
            raise RuntimeError("Disposable loopback mjobs_test[_*] PostgreSQL without URL overrides required")
        schema = "protected_config_" + uuid4().hex
        admin = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5})
        self.addCleanup(admin.dispose)
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))

        def cleanup_schema():
            with admin.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))

        self.addCleanup(cleanup_schema)
        return url.update_query_dict({"options": "-csearch_path=" + schema +
            " -clock_timeout=5000 -cstatement_timeout=30000"}).render_as_string(hide_password=False)


if __name__ == "__main__":
    unittest.main()
