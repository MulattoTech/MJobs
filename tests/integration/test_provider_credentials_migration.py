"""Encrypted provider credential migration is additive and refuses lossy downgrade."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select

from packages.persistence import models as m


class ProviderCredentialMigrationTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def _profile(self, engine):
        with engine.begin() as connection:
            connection.execute(m.profiles.insert().values(
                id="profile", name="Synthetic", email="synthetic@example.test", version="v1"))

    def _credential(self, engine):
        with engine.begin() as connection:
            connection.execute(m.provider_credentials.insert().values(
                id="credential", profile_id="profile", provider="openai",
                encrypted_secret=b"MJCR1synthetic-ciphertext-only",
                created_at="2026-10-07T00:00:00Z", updated_at="2026-10-07T00:00:00Z"))

    def test_upgrade_and_empty_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'empty.db').as_posix()}"
            config, engine = self._config(url), create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "c90d4a12fe37")
                    command.upgrade(config, "head")
                    with engine.connect() as connection:
                        self.assertEqual(connection.execute(select(m.provider_credentials)).all(), [])
                    command.downgrade(config, "c90d4a12fe37")
            finally:
                engine.dispose()

    def test_populated_credentials_refuse_downgrade_and_preserve_ciphertext(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'populated.db').as_posix()}"
            config, engine = self._config(url), create_engine(url)
            try:
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "c90d4a12fe37")
                    command.upgrade(config, "head")
                    self._profile(engine)
                    self._credential(engine)
                    with self.assertRaisesRegex(RuntimeError, "Refusing to discard provider credentials"):
                        command.downgrade(config, "c90d4a12fe37")
                    with engine.connect() as connection:
                        record = connection.execute(select(m.provider_credentials)).mappings().one()
                        self.assertEqual(record["encrypted_secret"], b"MJCR1synthetic-ciphertext-only")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
