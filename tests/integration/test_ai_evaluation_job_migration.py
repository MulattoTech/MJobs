"""Historical AI results stay unknown; rollback cannot erase input provenance."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from packages.persistence import models as m


class AIEvaluationJobMigrationTests(unittest.TestCase):
    def database_url(self, directory):
        return 'sqlite:///' + (Path(directory) / 'migration.db').as_posix()

    def test_legacy_upgrade_empty_rollback_and_populated_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            url = self.database_url(directory)
            engine = create_engine(url, poolclass=NullPool)
            try:
                with patch.dict(os.environ, MJOBS_DATABASE_URL=url):
                    config = Config('alembic.ini')
                    command.upgrade(config, '20261012ca01')
                    with engine.begin() as connection:
                        connection.execute(m.profiles.insert().values(
                            id='legacy', name='Synthetic', email='legacy@example.test', version='v1'))
                        connection.execute(m.ai_evaluation_runs.insert().values(
                            id='legacy-run', profile_id='legacy', profile_version='v1', policy={},
                            task_provider='ollama', task_model='synthetic', evaluated_candidate_count=1,
                            created_at='2026-10-01T00:00:00Z'))
                        connection.execute(text("INSERT INTO ai_job_evaluations "
                            "(id,run_id,profile_id,job_id,title,company,deterministic_score,category,rationale,created_at) "
                            "VALUES ('legacy-evaluation','legacy-run','legacy','job','Role','Example',80,'REVIEW',"
                            "'Original historical advice','2026-10-01T00:00:00Z')"))
                    command.upgrade(config, 'head')
                    with engine.connect() as connection:
                        row = connection.execute(select(m.ai_job_evaluations)).mappings().one()
                    self.assertIsNone(row['job_fingerprint'])
                    self.assertEqual(row['rationale'], 'Original historical advice')
                    command.downgrade(config, '20261012ca01')
                    command.upgrade(config, 'head')
                    with engine.begin() as connection:
                        connection.execute(update(m.ai_job_evaluations).values(job_fingerprint='a' * 64))
                    with self.assertRaisesRegex(RuntimeError, 'Refusing to discard AI evaluation input provenance'):
                        command.downgrade(config, '20261012ca01')
                    with engine.connect() as connection:
                        self.assertEqual(connection.execute(select(m.ai_job_evaluations.c.job_fingerprint)).scalar_one(), 'a' * 64)
            finally:
                engine.dispose()


@unittest.skipUnless(os.environ.get('MJOBS_AI_EVALUATION_POSTGRES_URL'), 'Requires disposable AI PostgreSQL fixture')
class PostgreSQLAIEvaluationJobMigrationTests(AIEvaluationJobMigrationTests):
    def setUp(self):
        url = make_url(os.environ['MJOBS_AI_EVALUATION_POSTGRES_URL'])
        if (not url.drivername.startswith('postgresql') or url.host not in ('localhost', '127.0.0.1', '::1')
                or not url.database or not url.database.startswith('mjobs_test_')):
            raise ValueError('Requires a disposable loopback mjobs_test_* database')
        self.schema = 'ai_job_migration_' + uuid4().hex
        self.admin = create_engine(url)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.addCleanup(self.cleanup_schema)
        self.url = url.update_query_dict({'options': '-csearch_path=' + self.schema}).render_as_string(hide_password=False)

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
        self.admin.dispose()

    def database_url(self, directory):
        return self.url
