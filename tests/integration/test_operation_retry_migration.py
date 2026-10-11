"""Retry deadlines upgrade existing data and cannot be silently discarded."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


class OperationRetryMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_history_and_downgrade_refuses_pending_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'migration.db').as_posix()}"
            root = Path(__file__).resolve().parents[2]
            config = Config(str(root / 'alembic.ini'))
            config.set_main_option('script_location', str(root / 'migrations'))
            config.set_main_option('sqlalchemy.url', url)
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {'MJOBS_DATABASE_URL': url}):
                    command.upgrade(config, '20261011op01')
                    with engine.begin() as connection:
                        connection.execute(text(
                            "INSERT INTO worker_operations (id,kind,entity_id,params,state,error,updated_at,attempt_count) "
                            "VALUES ('existing','DISCOVER','synthetic','{}','DEAD_LETTERED','SOURCE_HTTP_503','2026-10-11T00:00:00+00:00',3)"))
                    command.upgrade(config, 'head')
                    with engine.connect() as connection:
                        row = connection.execute(text('SELECT * FROM worker_operations WHERE id=\'existing\'')).mappings().one()
                    self.assertEqual(row['state'], 'DEAD_LETTERED')
                    self.assertEqual(row['error'], 'SOURCE_HTTP_503')
                    self.assertEqual(row['attempt_count'], 3)
                    self.assertIsNone(row['next_attempt_at'])
                    self.assertIn('ix_worker_operations_retry_due', {i['name'] for i in inspect(engine).get_indexes('worker_operations')})
                    with engine.begin() as connection:
                        connection.execute(text("UPDATE worker_operations SET state='QUEUED', next_attempt_at='2026-10-11T01:00:00+00:00'"))
                    with self.assertRaisesRegex(RuntimeError, 'Refusing to discard pending retry deadlines'):
                        command.downgrade(config, '20261011op01')
                    with engine.connect() as connection:
                        self.assertEqual(connection.execute(text('SELECT next_attempt_at FROM worker_operations')).scalar_one(),
                                         '2026-10-11T01:00:00+00:00')
                    with engine.begin() as connection:
                        connection.execute(text('UPDATE worker_operations SET next_attempt_at=NULL'))
                    command.downgrade(config, '20261011op01')
                    self.assertNotIn('next_attempt_at', {c['name'] for c in inspect(engine).get_columns('worker_operations')})
                    with engine.connect() as connection:
                        self.assertEqual(connection.execute(text('SELECT attempt_count FROM worker_operations')).scalar_one(), 3)
            finally:
                engine.dispose()
