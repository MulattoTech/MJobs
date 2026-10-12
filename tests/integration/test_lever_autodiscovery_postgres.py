"""Run the connected Lever discovery contract in an isolated PostgreSQL schema."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import OperationalError

from packages.persistence import models as m
from services.company_discovery.ats_enrollment import bound_company_source
from tests.integration import test_lever_autodiscovery as contract
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'),
                     'Set MJOBS_TEST_POSTGRES_URL to disposable loopback mjobs_test PostgreSQL')
class LeverAutodiscoveryPostgresTests(contract.LeverAutodiscoveryTests):
    def setUp(self):
        url=_disposable_postgres_url(os.environ['MJOBS_TEST_POSTGRES_URL'])
        self.schema='mjobs_lever_discovery_'+uuid4().hex
        self.admin=create_engine(url)
        self.addCleanup(self.cleanup_schema)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "'+self.schema+'"'))
        scoped=url.update_query_dict({'options':'-csearch_path='+self.schema+' -clock_timeout=5000 -cstatement_timeout=30000'})
        self.database_url=scoped.render_as_string(hide_password=False)
        with patch.dict(os.environ,{'MJOBS_DATABASE_URL':self.database_url}):
            command.upgrade(Config('alembic.ini'),'head')
        super().setUp()

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA IF EXISTS "'+self.schema+'" CASCADE'))
        self.admin.dispose()

    def test_import_holds_review_and_original_collector_locks(self):
        request,source=self.enroll()
        with self.store.engine.begin() as connection:
            collector=connection.execute(select(m.job_sources).where(
                m.job_sources.c.id==source['collector_source_id'])).mappings().one()
            bound_company_source(self.store,connection,collector,lock=True)
            for table,row_id,values in ((m.company_discovery_requests,request['id'],{'domain_evidence':[]}),
                                        (m.job_sources,self.source,{'enabled':False})):
                with self.subTest(table=table.name), self.assertRaises(OperationalError) as failure:
                    with self.store.engine.begin() as other:
                        other.execute(text("SET LOCAL lock_timeout = '200ms'"))
                        other.execute(update(table).where(table.c.id==row_id).values(**values))
                self.assertEqual(failure.exception.orig.sqlstate,'55P03')
