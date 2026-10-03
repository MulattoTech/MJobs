"""Collection observations survive failures without inventing missing evidence."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import collect_source, configure_source
from services.company_discovery.quality import record_observation, source_quality
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE


class SourceQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'quality.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.store.save_profile(PROFILE)
        self.source = configure_source(self.store, "example")

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def collect(self, jobs):
        def response(request):
            return httpx.Response(200, json={"jobs": jobs} if request.url.path.endswith('/jobs') else {"name": "Example"})
        return collect_source(self.store, self.source, "fixture", transport=httpx.MockTransport(response))

    def observations(self):
        with self.store.engine.connect() as c:
            return list(c.execute(select(m.source_quality_observations)).mappings())

    def test_api_unknown_before_collection_and_linked_evidence_after_collection(self):
        with TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root, operation_mode="FIXTURE")) as client:
            unknown = client.get('/sources').json()['sources'][0]['quality']
            self.assertIsNone(unknown['score'])
            self.assertEqual(unknown['coverage'], 0)
            self.collect([RAW, dict(RAW, id=99, location=['invalid'])])
            quality = client.get('/sources').json()['sources'][0]['quality']
            company = client.get('/company-sources/view').json()['company_sources'][0]['quality']
            self.assertEqual(quality['components']['parser_reliability'], 50)
            for component, value in quality['components'].items():
                if value is None:
                    self.assertIsNone(company['components'][component])
                else:
                    self.assertAlmostEqual(company['components'][component], value, places=4)
            self.assertEqual(company['scope'], 'CONFIGURED_SOURCE')
            self.assertEqual(company['source_id'], self.source)
            self.assertIsNone(quality['components']['application_link_accuracy'])
            self.assertLess(quality['coverage'], 100)

    def test_success_is_durable_and_contains_actual_counts(self):
        self.collect([RAW, dict(RAW, id=99, location=['invalid'])])
        row = self.observations()[0]
        self.assertEqual((row['enumerated'], row['accepted'], row['salary_known']), (2, 1, 0))
        self.assertTrue(row['succeeded'])
        self.assertTrue(row['available'])
        self.assertEqual(row['completeness_sum'], 1)
        self.assertIsNone(row['links_checked'])
        self.store.engine.dispose()
        reopened = Store(self.store.url)
        with reopened.engine.connect() as c:
            self.assertEqual(c.execute(select(m.source_quality_observations.c.id)).scalar_one(), row['id'])
        reopened.engine.dispose()

    def test_offset_timestamp_is_normalized_before_window_filtering(self):
        with self.store.engine.begin() as c:
            record_observation(c, configured_source_id=self.source, observed_at='2026-10-03T19:00:00-05:00',
                               succeeded=True, available=True)
            record_observation(c, configured_source_id=self.source, observed_at='2026-10-04T00:00:01Z',
                               succeeded=False, available=False)
        with self.store.engine.connect() as c:
            result = source_quality(c, configured_source_id=self.source, at='2026-10-04T00:00:00Z')
            self.assertEqual(result['window']['observation_count'], 1)
            self.assertEqual(result['components']['availability'], 100)
            self.assertEqual(c.execute(select(m.source_quality_observations.c.observed_at).order_by(
                m.source_quality_observations.c.observed_at)).scalars().first(), '2026-10-04T00:00:00+00:00')

    def test_fetch_failure_is_persisted_without_jobs(self):
        with self.assertRaisesRegex(ValueError, 'SOURCE_HTTP_503'):
            collect_source(self.store, self.source, 'fixture', transport=httpx.MockTransport(lambda _: httpx.Response(503)))
        row = self.observations()[0]
        self.assertFalse(row['succeeded'])
        self.assertFalse(row['available'])
        self.assertIsNone(row['enumerated'])
        self.assertEqual(row['error_code'], 'SOURCE_HTTP_503')
        self.assertEqual(self.store.count('canonical_jobs'), 0)

    def test_parser_failure_keeps_fetch_availability_and_rejections(self):
        with self.assertRaisesRegex(ValueError, 'SOURCE_NO_VALID_JOBS'):
            self.collect([dict(RAW, location=['invalid'])])
        row = self.observations()[0]
        self.assertTrue(row['available'])
        self.assertFalse(row['succeeded'])
        self.assertEqual((row['enumerated'], row['accepted']), (1, 0))

    def test_import_rollback_keeps_failure_and_no_partial_postings(self):
        with patch('services.discovery.sources.collect.persist_posting', side_effect=RuntimeError('private diagnostic')):
            with self.assertRaisesRegex(RuntimeError, 'private diagnostic'):
                self.collect([RAW])
        row = self.observations()[0]
        self.assertFalse(row['succeeded'])
        self.assertTrue(row['available'])
        self.assertEqual(row['error_code'], 'RuntimeError')
        self.assertEqual(self.store.count('canonical_jobs'), 0)
        self.assertEqual(self.store.count('job_postings'), 0)

    def test_empty_feed_is_success_with_unknown_quality_denominators(self):
        self.collect([])
        row = self.observations()[0]
        self.assertEqual((row['enumerated'], row['accepted']), (0, 0))
        self.assertTrue(row['succeeded'])

    def test_policy_rejection_does_not_create_an_attempt(self):
        self.collect([RAW])
        with self.assertRaisesRegex(ValueError, 'SOURCE_COOLDOWN'):
            self.collect([RAW])
        self.assertEqual(len(self.observations()), 1)

    def test_failure_after_success_does_not_erase_history_or_company_link(self):
        self.collect([RAW])
        with self.store.engine.begin() as c:
            c.execute(update(m.job_sources).values(last_attempt=None))
        with self.assertRaises(ValueError):
            collect_source(self.store, self.source, 'fixture', transport=httpx.MockTransport(lambda _: httpx.Response(429)))
        self.assertEqual(len(self.observations()), 2)
        with TestClient(create_app(self.store, 'http://127.0.0.1:8090', self.root, operation_mode='FIXTURE')) as client:
            quality = client.get('/company-sources/view').json()['company_sources'][0]['quality']
            self.assertEqual(quality['components']['availability'], 0)
            self.assertEqual(quality['components']['historical_uptime'], 50)
        self.assertEqual(self.store.count('canonical_jobs'), 1)
