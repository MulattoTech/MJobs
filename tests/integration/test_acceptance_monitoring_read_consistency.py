"""Real SQLite/API/worker interleaving; no Docker or public provider calls."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from cli import acceptance_monitoring as acceptance
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery import fixture_monitoring as monitor
from services.company_discovery.monitoring import source_version
from services.discovery.pipeline import discover
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class MonitoringReadConsistencyTests(unittest.TestCase):
    def test_completion_between_monitor_and_operation_reads_after_store_reopen(self):
        with tempfile.TemporaryDirectory() as directory, fixture_server() as origin:
            url = 'sqlite:///' + (Path(directory) / 'monitor.db').as_posix()
            store = Store(url)
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            store.save_profile(PROFILE)
            save_search_profile(store, dict(id='all', profile_id='fixture', name='All', enabled=True, source_ids=[]))
            discover(store, origin, 'fixture')
            # Arrange an immediately due review, without changing any production clock.
            with store.engine.begin() as connection:
                source = dict(connection.execute(select(m.company_sources)).mappings().one())
                connection.execute(update(m.company_sources).where(m.company_sources.c.id == source['id']).values(
                    last_success=(datetime.now(timezone.utc)-timedelta(seconds=600)).isoformat()))
            expected = monitor.configure_fixture_monitoring(store, origin, 'fixture', source['id'], source_version(source), 0, 300)
            store.engine.dispose()
            store = Store(url)
            try:
                scheduled = monitor.schedule_fixture_sources(store, origin)['scheduled']
                self.assertEqual(len(scheduled), 1)
                identifier = scheduled[0]['operation_id']
                context = dict(source_id=source['id'], source_version=expected['source_version'],
                    profile_version=expected['profile_version'])
                path = '/profiles/fixture/company-sources/' + source['id'] + '/fixture-monitoring'
                reads, commit_boundary = [], []
                with TestClient(create_app(store, origin, Path(directory), operation_mode='FIXTURE')) as api:
                    original = monitor.refresh_fixture_source
                    def receipt_then_finalize(*args, **kwargs):
                        result = original(*args, **kwargs)
                        # Import/receipt transaction commits before the worker's terminal transaction.
                        receipt = api.get(path).json()
                        operation = api.get('/operations/' + identifier).json()
                        commit_boundary.append((receipt['status'], receipt['completed_operation_id'], operation['state']))
                        return result
                    def request(method, request_path, **kwargs):
                        if request_path == '/operations/' + identifier and not commit_boundary:
                            with patch.dict(os.environ, dict(MJOBS_OPERATION_MODE='FIXTURE', MJOBS_DATABASE_URL=url,
                                    MJOBS_MOCK_ORIGIN=origin, MJOBS_OUTPUT_DIR=directory)), \
                                    patch.object(monitor, 'refresh_fixture_source', receipt_then_finalize):
                                self.assertEqual(execute_operation(identifier)['status'], 'COMPLETED')
                        response = api.request(method, request_path, **kwargs)
                        response.raise_for_status()
                        value = response.json()
                        reads.append((request_path, value.get('status') or value.get('state')))
                        return value
                    seconds = [0]
                    def advance(duration):
                        seconds[0] += duration
                    with acceptance._Session('http://127.0.0.1:18700', {'profile_id': 'fixture'},
                            Path(directory)/'unused.json', 'test', request=request,
                            monotonic=lambda: seconds[0], sleep=advance, scheduler_tick_seconds=5) as session:
                        row, operation = acceptance._wait_refresh(session, context, expected, None)
                    self.assertEqual(reads[:2], [(path, 'SCHEDULED'), ('/operations/'+identifier, 'COMPLETED')])
                    self.assertEqual(commit_boundary, [('HEALTHY', identifier, 'RUNNING')])
                    self.assertEqual(row['completed_operation_id'], operation['id'])
                    self.assertEqual(operation['attempt_count'], 1)
                    self.assertEqual(monitor.schedule_fixture_sources(store, origin)['scheduled'], [])
            finally:
                store.engine.dispose()
