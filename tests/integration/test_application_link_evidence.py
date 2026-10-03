"""Link observations belong to one immutable feed candidate, never a mutable URL."""
import hashlib
import json
import unittest
from types import SimpleNamespace

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, now
from services.company_discovery.quality import source_quality
from services.discovery.company_feed import import_company_feed_jobs
from tests.integration import test_company_feed_quality as feed_tests


class ApplicationLinkEvidenceTests(unittest.TestCase):
    setUp = feed_tests.CompanyFeedQualityTests.setUp
    tearDown = feed_tests.CompanyFeedQualityTests.tearDown
    _item = feed_tests.CompanyFeedQualityTests._item
    def report(self, item, **changes):
        values = dict(external_key=item['id'],
            raw_sha256=hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
            requested_url='https://employer.example' + item['url'], endpoint=self.endpoint,
            observed_at=now(), final_url='https://employer.example' + item['url'],
            result='ACCURATE', reason='DECLARED_JOB_IDENTITY_MATCH', response_status=200,
            response_sha256='a' * 64, identity_evidence=('DECLARED_IDENTITY_ONLY',))
        return SimpleNamespace(**(values | changes))

    def ingest(self, items, reports):
        return import_company_feed_jobs(self.store, company_id='company', source_id=self.source['id'],
            provider='rss', tenant='tenant', endpoint=self.endpoint, strategy='PUBLIC_JSON',
            items=items, fetch_available=True, link_reports=reports)

    def quality(self):
        with self.store.engine.connect() as c:
            return source_quality(c, company_source_id=self.source['id'], endpoint=self.endpoint)

    def test_unknown_samples_excluded_from_rate_but_visible_and_durable(self):
        items = [dict(self._item(link=f'/jobs/{i}'), id=f'job-{i}') for i in range(7)]
        reports = [self.report(items[0])] + [self.report(item, result='UNKNOWN',
            reason='JOB_IDENTITY_UNPROVEN') for item in items[1:5]]
        self.ingest(items, reports)
        quality = self.quality()
        self.assertEqual(quality['components']['application_link_accuracy'], 100)
        evidence = quality['link_verification']
        self.assertEqual([evidence[key] for key in ('sampled_count', 'accepted_posting_count',
            'conclusive_sample_count', 'accurate_sample_count', 'unknown_sample_count')], [5, 7, 1, 1, 4])
        self.assertEqual(len(evidence['latest']), 5)
        with self.store.engine.connect() as c:
            rows = c.execute(select(m.source_application_link_observations.c.requested_url,
                m.posting_value_candidates.c.application_url, m.posting_snapshots.c.sha256)
                .select_from(m.source_application_link_observations).join(m.posting_value_candidates).join(m.posting_snapshots)).all()
            self.assertEqual(len(rows), 5)
            self.assertTrue(all(row[0] == row[1] for row in rows))
        self.store.engine.dispose()
        self.store = Store(self.store.url)
        self.assertEqual(self.quality()['link_verification'], evidence)

    def test_mismatched_snapshot_url_endpoint_or_unaccepted_row_rolls_back(self):
        item = self._item()
        for change in ({'raw_sha256': '0' * 64}, {'requested_url': 'https://employer.example/other'},
                       {'endpoint': 'https://employer.example/other-feed'}, {'external_key': 'missing'},
                       {'observed_at': '2099-01-01T00:00:00Z'}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'LINK_EVIDENCE'):
                self.ingest([item], [self.report(item, **change)])
            self.assertEqual(self.store.count('canonical_jobs'), 0)
            self.assertEqual(self.store.count('source_application_link_observations'), 0)
        with self.store.engine.connect() as c:
            failures = c.execute(select(m.source_quality_observations)).mappings().all()
            self.assertEqual(len(failures), 5)
            self.assertTrue(all(not row['succeeded'] and row['available'] and row['accepted'] is None for row in failures))

    def test_source_success_commits_with_evidence_and_failure_cannot_rewind_it(self):
        from unittest.mock import patch
        item = self._item()
        self.ingest([item], [self.report(item)])
        with self.store.engine.connect() as c:
            last = c.execute(select(m.company_sources.c.last_success)).scalar_one()
            observed = c.execute(select(m.source_quality_observations.c.observed_at)).scalar_one()
            self.assertEqual(last, observed)
        with patch('services.discovery.company_feed.record_link_evidence', side_effect=RuntimeError('synthetic storage failure')):
            with self.assertRaises(RuntimeError):
                self.ingest([item], [self.report(item)])
        with self.store.engine.connect() as c:
            self.assertEqual(c.execute(select(m.company_sources.c.last_success)).scalar_one(), last)
            self.assertEqual(len(c.execute(select(m.source_application_link_observations)).all()), 1)

    def test_repeat_snapshots_keep_separate_observations_without_rewriting_route(self):
        item = self._item()
        self.ingest([item, item], [self.report(item)])
        self.ingest([item], [self.report(item, result='INACCURATE', reason='OBSERVED_HTTP_NOT_FOUND',
                                       response_status=404)])
        quality = self.quality()
        self.assertEqual(quality['components']['application_link_accuracy'], 50)
        self.assertEqual(quality['link_verification']['sampled_count'], 2)
        with self.store.engine.connect() as c:
            self.assertEqual(c.execute(select(m.routes.c.url)).scalar_one(), 'https://employer.example/jobs/1')
            self.assertEqual(len(c.execute(select(m.posting_snapshots)).all()), 1)
        with self.store.engine.begin() as c:
            c.execute(update(m.company_sources).where(m.company_sources.c.id == self.source['id'])
                      .values(endpoint='https://employer.example/new-feed'))
        with self.store.engine.connect() as c:
            quality = source_quality(c, company_source_id=self.source['id'], endpoint='https://employer.example/new-feed')
        self.assertEqual(quality['link_verification']['sampled_count'], 0)
        self.assertIsNone(quality['components']['application_link_accuracy'])
