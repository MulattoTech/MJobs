"""Additive policy upgrade and fail-closed downgrade on disposable SQLite."""
from pathlib import Path
import tempfile
import unittest

from alembic import command
from sqlalchemy import create_engine, text

from tests.integration import test_company_source_monitoring_migration as helpers


class CompanyMonitoringPolicyMigrationTests(unittest.TestCase):
    def test_legacy_due_receipt_and_defaults_survive_upgrade_and_safe_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'policy.db').as_posix()}"
            config = helpers.CompanySourceMonitoringMigrationTests._config(self, url)
            engine = create_engine(url)
            try:
                command.upgrade(config, '20261014rh01')
                helpers.CompanySourceMonitoringMigrationTests._legacy_rows(self, engine)
                with engine.begin() as connection:
                    connection.execute(text("INSERT INTO company_source_monitoring "
                        "(id,source_id,source_snapshot,authorization,domain_evidence,next_due,last_attempt,created_at,updated_at) "
                        "VALUES ('monitor','source','{}','USER_REVIEWED','[]','2026-10-19T12:00:00Z',"
                        "'2026-10-18T12:00:00Z','2026-10-18T12:00:00Z','2026-10-18T12:00:00Z')"))
                command.upgrade(config, '20261018cm01')
                with engine.connect() as connection:
                    row = connection.execute(text('SELECT * FROM company_source_monitoring')).mappings().one()
                    self.assertEqual(tuple(row[k] for k in ('request_timeout','rate_limit','concurrency','jitter_seconds')),
                                     (10,60,1,0))
                    self.assertEqual(row['next_due'], '2026-10-19T12:00:00Z')
                    self.assertEqual(row['last_attempt'], '2026-10-18T12:00:00Z')
                    self.assertIsNone(row['request_not_before'])
                for mutation in ("rate_limit=1", "request_not_before='2026-10-19T12:00:00Z'"):
                    with engine.begin() as connection:
                        connection.execute(text('UPDATE company_source_monitoring SET ' + mutation))
                    with self.assertRaisesRegex(RuntimeError, 'Refusing to discard'):
                        command.downgrade(config, '20261014rh01')
                    with engine.begin() as connection:
                        connection.execute(text('UPDATE company_source_monitoring SET rate_limit=60, request_not_before=NULL'))
                command.downgrade(config, '20261014rh01')
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(text('SELECT next_due FROM company_source_monitoring')).scalar_one(),
                                     '2026-10-19T12:00:00Z')
            finally:
                engine.dispose()
