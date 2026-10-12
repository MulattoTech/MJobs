"""Upgrade preserves existing sources; rollback cannot discard consent history."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.pool import NullPool

from packages.persistence import models as m


class ReviewedSourceAuthorizationMigrationTests(unittest.TestCase):
    def test_upgrade_empty_rollback_and_populated_rollback_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'migration.db').as_posix()}"
            engine = create_engine(url, poolclass=NullPool)
            self.addCleanup(engine.dispose)
            with patch.dict(os.environ, {'MJOBS_DATABASE_URL': url}):
                config = Config('alembic.ini')
                command.upgrade(config, '20261012ai01')
                with engine.begin() as connection:
                    connection.execute(text("INSERT INTO companies(id,name,domain) VALUES ('company','Employer','')"))
                    connection.execute(text("INSERT INTO company_job_sources(id,company_id,provider,tenant,endpoint,discovered_automatically) VALUES ('source','company','greenhouse','tenant','https://employer.example/jobs',true)"))
                command.upgrade(config, 'head')
                with engine.connect() as connection:
                    row = connection.execute(select(m.company_sources)).mappings().one()
                self.assertIsNone(row['identity_authorization'])
                self.assertEqual(row['company_id'], 'company')
                command.downgrade(config, '20261012ai01')
                command.upgrade(config, 'head')
                with engine.begin() as connection:
                    connection.execute(update(m.company_sources).values(identity_authorization={'request_id': 'retained-review'}))
                with self.assertRaisesRegex(RuntimeError, 'Refusing to discard recurring employer identity authorization'):
                    command.downgrade(config, '20261012ai01')
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(select(m.company_sources.c.identity_authorization)).scalar_one(),
                        {'request_id': 'retained-review'})
