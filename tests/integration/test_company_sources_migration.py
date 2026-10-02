"""Upgrade safety for pre-existing employer source rows."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text

from packages.persistence import models as m


class CompanySourceMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_legacy_source_and_rollback_refuses_tenant_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite:///{(Path(directory) / 'legacy.db').as_posix()}"
            root = Path(__file__).resolve().parents[2]
            config = Config(str(root / "alembic.ini"))
            config.set_main_option("script_location", str(root / "migrations"))
            config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
            with patch.dict(os.environ, {"MJOBS_DATABASE_URL": database_url}):
                command.upgrade(config, "cb72f9041d6a")
                engine = create_engine(database_url)
                with engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO companies (id, name, domain) VALUES ('legacy-co', 'Legacy Company', '')"
                    ))
                    connection.execute(text(
                        "INSERT INTO company_job_sources (id, company_id, provider, tenant, endpoint, discovered_automatically) "
                        "VALUES ('legacy-source', 'legacy-co', 'mock-ats', 'legacy-board', 'http://fixture/ats/jobs', 1)"
                    ))
                    connection.execute(text(
                        "INSERT INTO company_job_sources (id, company_id, provider, tenant, endpoint, discovered_automatically) "
                        "VALUES ('manual-source', 'legacy-co', 'greenhouse', 'manual-board', 'https://careers.example/jobs', 0)"
                    ))
                command.upgrade(config, "head")
                with engine.connect() as connection:
                    source = connection.execute(select(m.company_sources).where(
                        m.company_sources.c.id == "legacy-source"
                    )).mappings().one()
                    tenant = connection.execute(select(m.company_ats_tenants).where(
                        m.company_ats_tenants.c.id == source["ats_tenant_id"]
                    )).mappings().one()
                    manual = connection.execute(select(m.company_sources).where(
                        m.company_sources.c.id == "manual-source"
                    )).mappings().one()
                self.assertEqual(source["endpoint"], "http://fixture/ats/jobs")
                self.assertEqual(source["provider"], "mock-ats")
                self.assertIsNone(source["career_url"])
                self.assertEqual(source["collection_strategy"], "UNKNOWN")
                self.assertTrue(source["enabled"])
                self.assertEqual(source["origin"], "AUTO_DISCOVERED")
                self.assertEqual(tenant["tenant_identifier"], "legacy-board")
                self.assertEqual(tenant["confidence"], 0.45)
                self.assertEqual(manual["origin"], "USER_WATCHED")
                self.assertFalse(manual["discovered_automatically"])
                with self.assertRaisesRegex(RuntimeError, "Refusing to discard company source watch or pin decisions"):
                    command.downgrade(config, "e14ac893620f")
                with engine.begin() as connection:
                    connection.execute(text("UPDATE company_job_sources SET origin = 'AUTO_DISCOVERED'"))
                with self.assertRaisesRegex(RuntimeError, "Refusing to discard company ATS tenant evidence"):
                    command.downgrade(config, "cb72f9041d6a")
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
