"""Legacy source preservation and non-destructive quality migrations."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text

from packages.persistence import models as m
from services.company_discovery.quality import record_observation


class SourceQualityMigrationTests(unittest.TestCase):
    def test_existing_source_preserved_without_inferred_quality_and_downgrade_guarded(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'legacy.db').as_posix()}"
            root = Path(__file__).resolve().parents[2]
            config = Config(str(root / 'alembic.ini'))
            config.set_main_option('script_location', str(root / 'migrations'))
            config.set_main_option('sqlalchemy.url', url.replace('%', '%%'))
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {'MJOBS_DATABASE_URL': url}):
                    command.upgrade(config, 'd21b76ce903a')
                    with engine.begin() as c:
                        c.execute(text("INSERT INTO companies (id,name,domain) VALUES ('legacy','Legacy','')"))
                        c.execute(text("INSERT INTO company_job_sources (id,company_id,provider,tenant,endpoint,discovered_automatically,last_success) VALUES ('source','legacy','rss','legacy','https://example.test/jobs',1,'2026-01-01T00:00:00+00:00')"))
                    command.upgrade(config, 'head')
                    with engine.connect() as c:
                        row = c.execute(select(m.company_sources)).mappings().one()
                        self.assertIsNone(row['collector_source_id'])
                        self.assertEqual(row['last_success'], '2026-01-01T00:00:00+00:00')
                        self.assertEqual(row['endpoint'], 'https://example.test/jobs')
                        self.assertEqual(list(c.execute(select(m.source_quality_observations))), [])
                    command.downgrade(config, 'd21b76ce903a')
                    command.upgrade(config, 'head')
                    with engine.begin() as c:
                        observation_id = record_observation(c, company_source_id='source', succeeded=False, available=False)
                    with self.assertRaisesRegex(RuntimeError, 'Refusing to discard source quality evidence'):
                        command.downgrade(config, 'd21b76ce903a')
                    with engine.connect() as c:
                        self.assertEqual(c.execute(select(m.source_quality_observations.c.id)).scalar_one(), observation_id)
            finally:
                engine.dispose()
