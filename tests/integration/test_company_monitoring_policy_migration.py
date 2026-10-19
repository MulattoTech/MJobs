"""Additive policy migration on disposable SQLite and guarded PostgreSQL schema."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

from packages.persistence import models as m
from tests.integration import test_company_source_monitoring_migration as helpers
from tests.integration import test_profile_recovery_history_migration as guarded_pg


class CompanyMonitoringPolicyMigrationTests(unittest.TestCase):
    expected_dialect = 'sqlite'

    def database_url(self, directory, filename):
        return f"sqlite:///{(Path(directory) / filename).as_posix()}"

    def _exercise(self, url):
        config = helpers.CompanySourceMonitoringMigrationTests._config(self, url)
        engine = create_engine(url, poolclass=NullPool)
        try:
            self.assertEqual(engine.dialect.name, self.expected_dialect)
            if self.expected_dialect == 'postgresql':
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(text('SELECT current_schema()')).scalar_one(), self.schema)
            # Alembic env.py honors this variable over its Config URL. Pin every
            # migration invocation to this disposable database/owned schema.
            with patch.dict(os.environ, {'MJOBS_DATABASE_URL': url}):
                command.upgrade(config, '20261014rh01')
                with engine.begin() as connection:
                    connection.execute(text("INSERT INTO companies (id,name,domain) VALUES "
                        "('company','Employer','employer.example')"))
                    connection.execute(text("INSERT INTO company_job_sources "
                        "(id,company_id,provider,tenant,endpoint,discovered_automatically,last_success) VALUES "
                        "('source','company','rss','tenant','https://employer.example/feed',:automatic,"
                        "'2026-10-03T00:00:00Z')"), {'automatic':True})
                    connection.execute(m.operations.insert().values(id='finished-refresh',kind='COMPANY_SOURCE_REFRESH',
                        entity_id='source',params={},state='COMPLETED',updated_at='2026-10-18T12:00:00Z'))
                    connection.execute(text("INSERT INTO company_source_monitoring "
                        '(id,source_id,source_snapshot,"authorization",domain_evidence,revision,poll_interval,status,'
                        "next_due,last_attempt,operation_id,completed_operation_id,created_at,updated_at) VALUES "
                        "('monitor','source','{}','USER_REVIEWED','[]',7,604800,'HEALTHY','2026-10-25T12:00:00Z',"
                        "'2026-10-18T12:00:00Z','finished-refresh','finished-refresh',"
                        "'2026-10-18T12:00:00Z','2026-10-18T12:00:00Z')"))
                    legacy = dict(connection.execute(text('SELECT * FROM company_source_monitoring')).mappings().one())
                command.upgrade(config, '20261018cm01')
                with engine.connect() as connection:
                    row = dict(connection.execute(text('SELECT * FROM company_source_monitoring')).mappings().one())
                    self.assertEqual({key:row[key] for key in legacy}, legacy)
                    self.assertEqual(tuple(row[k] for k in ('request_timeout','rate_limit','concurrency','jitter_seconds')),
                                     (10,60,1,0))
                    self.assertIsNone(row['request_not_before'])
                for key, invalid in (('request_timeout',1),('request_timeout',11),('rate_limit',0),('rate_limit',1),('rate_limit',2),('rate_limit',14),
                                     ('rate_limit',61),('concurrency',0),('concurrency',9),
                                     ('jitter_seconds',-1),('jitter_seconds',301)):
                    with self.subTest(constraint=key, invalid=invalid), self.assertRaises(IntegrityError):
                        with engine.begin() as connection:
                            connection.execute(text('UPDATE company_source_monitoring SET '+key+'=:value'), {'value':invalid})
                for values in (dict(request_timeout=5,rate_limit=30,concurrency=2,jitter_seconds=17,request_not_before=None),
                               dict(request_timeout=10,rate_limit=60,concurrency=1,jitter_seconds=0,
                                    request_not_before='2026-10-19T12:00:00Z')):
                    with engine.begin() as connection:
                        connection.execute(m.company_source_monitoring.update().values(**values))
                    with self.assertRaisesRegex(RuntimeError, 'Refusing to discard'):
                        command.downgrade(config, '20261014rh01')
                    with engine.connect() as connection:
                        saved = dict(connection.execute(text('SELECT * FROM company_source_monitoring')).mappings().one())
                        self.assertEqual({key:saved[key] for key in legacy}, legacy)
                        self.assertEqual({key:saved[key] for key in values}, values)
                        self.assertEqual(connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one(),
                                         '20261018cm01')
                    with engine.begin() as connection:
                        connection.execute(m.company_source_monitoring.update().values(
                            request_timeout=10,rate_limit=60,concurrency=1,jitter_seconds=0,request_not_before=None))
                command.downgrade(config, '20261014rh01')
                with engine.connect() as connection:
                    saved = dict(connection.execute(text('SELECT * FROM company_source_monitoring')).mappings().one())
                    self.assertEqual(saved, legacy)
                    self.assertEqual(connection.execute(select(m.operations.c.state).where(
                        m.operations.c.id == 'finished-refresh')).scalar_one(), 'COMPLETED')
        finally:
            engine.dispose()

    def test_legacy_due_receipt_and_defaults_survive_upgrade_and_safe_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            self._exercise(self.database_url(directory, 'policy.db'))


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'),
                     'Requires guarded disposable loopback MJOBS_TEST_POSTGRES_URL PostgreSQL fixture')
class CompanyMonitoringPolicyPostgresMigrationTests(CompanyMonitoringPolicyMigrationTests):
    expected_dialect = 'postgresql'
    # Reuse the existing loopback/database/query guard, UUID schema creation,
    # schema-only search_path and ownership-local cleanup; never downgrade a
    # shared database or its public schema.
    setUp = guarded_pg.PostgreSQLProfileRecoveryHistoryMigrationTests.setUp
    cleanup_schema = guarded_pg.PostgreSQLProfileRecoveryHistoryMigrationTests.cleanup_schema
    database_url = guarded_pg.PostgreSQLProfileRecoveryHistoryMigrationTests.database_url
