"""MJ-REQ-030: source words become stable UTC dates through public intake."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class RelativePostedSourceIntakeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mjobs-relative-intake-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.url = 'sqlite:///' + (self.root / 'proof.db').as_posix()
        self.store = Store(self.url)
        self.addCleanup(lambda: self.store.engine.dispose())
        self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        self.collected_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.requests = []
        self.payload = deepcopy(RAW_REMOTIVE_PAYLOAD)
        self.payload['jobs'] = self.payload['jobs'][:1]
        self.payload['jobs'][0]['publication_date'] = '2 hours ago'
        self.payload['job-count'] = 1
        environment = {'MJOBS_DATABASE_URL': self.url, 'MJOBS_OPERATION_MODE': 'PERSONAL',
                       'MJOBS_REDIS_URL': 'redis://127.0.0.1:1/0', 'MJOBS_OUTPUT_DIR': str(self.root)}
        self.enterContext(patch.dict(os.environ, environment))
        self.client = self.enterContext(TestClient(self._app()))
        self.assertEqual(self.client.put('/profiles', json=PROFILE).status_code, 200)
        source = self.client.put('/sources', json={'provider': 'remotive', 'tenant': 'default',
            'poll_interval': 300, 'request_timeout': 8, 'rate_limit': 60, 'jitter_seconds': 0})
        self.assertEqual(source.status_code, 200, source.text)
        self.source_id = source.json()['id']

    def _app(self):
        return create_app(self.store, '', self.root, operation_mode='PERSONAL')

    def _collect(self, reference=None, request_id='relative-first'):
        collected_at = reference or self.collected_at + timedelta(minutes=10 * len(self.requests))
        class CollectionClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return collected_at.astimezone(tz) if tz else collected_at.replace(tzinfo=None)
        admitted = self.client.post('/sources/' + self.source_id + '/collect',
            json={'profile_id': 'fixture', 'request_id': request_id})
        self.assertEqual(admitted.status_code, 202, admitted.text)
        identifier = admitted.json()['operation_id']
        def boundary(_transport, request):
            self.assertEqual((request.method, request.url.host, request.url.path),
                             ('GET', 'remotive.com', '/api/remote-jobs'))
            self.requests.append(str(request.url))
            return httpx.Response(200, json=self.payload)
        with patch('httpx.HTTPTransport.handle_request', boundary), patch(
                'services.discovery.sources.collect.now', return_value=collected_at.isoformat()), patch(
                'services.discovery.sources.collect.datetime', CollectionClock):
            outcome = execute_operation(identifier)
            self.assertEqual(outcome['status'], 'COMPLETED', outcome)
        return identifier

    def _filtered(self, window):
        saved = self.client.put('/search-profiles', json={'id': 'date-filter', 'profile_id': 'fixture',
            'name': 'Synthetic date filter', 'date_window': window, 'enabled': True})
        self.assertEqual(saved.status_code, 200, saved.text)
        response = self.client.get('/jobs', params={'profile_id': 'fixture', 'search_profile_id': 'date-filter'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['jobs']

    def _reopen(self):
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.url)
        self.client = self.enterContext(TestClient(self._app()))

    def test_public_intake_excludes_two_hours_ago_from_last_hour(self):
        self._collect()
        self.assertEqual(len(self._filtered('LAST_HOUR')), 0)
        day = self._filtered('LAST_24_HOURS')
        self.assertEqual(len(day), 1)
        self.assertEqual(day[0]['attributes']['reported_posted_at'],
                         (self.collected_at - timedelta(hours=2)).isoformat())

    def test_reopen_replay_and_updated_age_label_preserve_first_normalized_claim(self):
        identifier = self._collect()
        original = self._filtered('LAST_24_HOURS')[0]
        self._reopen()
        restored = self._filtered('LAST_24_HOURS')[0]
        self.assertEqual(restored['attributes'], original['attributes'])
        self.assertEqual(execute_operation(identifier)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        self.assertEqual(len(self.requests), 1)
        # Source wording advances with collection time: both observations imply
        # the same publication time. Preserve first claim/reference explicitly.
        self.payload['jobs'][0]['publication_date'] = '3 hours ago'
        self._collect(self.collected_at + timedelta(hours=1), 'relative-age-updated')
        self._reopen()
        job = self._filtered('LAST_24_HOURS')[0]
        attrs = job['attributes']
        self.assertEqual(attrs['reported_posted_at'], original['attributes']['reported_posted_at'])
        self.assertEqual(attrs['reported_posted_at_raw'], '2 hours ago')
        self.assertEqual(attrs['reported_posted_at_reference_at'], self.collected_at.isoformat())
        self.assertEqual(attrs['reported_posted_at_precision'], 'HOUR')
        self.assertEqual(attrs['first_seen_at'], self.collected_at.isoformat())
        self.assertEqual(attrs['last_seen_at'], (self.collected_at + timedelta(hours=1)).isoformat())
        detail = self.client.get('/jobs/' + job['id'], params={'profile_id': 'fixture'}).json()
        posting = detail['postings'][0]
        self.assertEqual(posting['raw']['publication_date'], '3 hours ago')
        expected_hashes = set()
        for wording in ('2 hours ago', '3 hours ago'):
            raw = dict(posting['raw'], publication_date=wording)
            expected_hashes.add(hashlib.sha256(json.dumps(raw, sort_keys=True,
                ensure_ascii=False, separators=(',', ':')).encode()).hexdigest())
        self.assertEqual({snapshot['sha256'] for snapshot in posting['snapshots']}, expected_hashes)
        # A repeated stale label days later cannot move this posting forward.
        self.payload['jobs'][0]['publication_date'] = '2 hours ago'
        self._collect(self.collected_at + timedelta(days=2), 'relative-stale-label')
        self._reopen()
        again = self._filtered('LAST_24_HOURS')[0]
        self.assertEqual(again['attributes']['reported_posted_at'], attrs['reported_posted_at'])
        self.assertEqual(again['attributes']['last_seen_at'], (self.collected_at + timedelta(days=2)).isoformat())
        self.assertEqual(len(self.requests), 3)

    def test_absolute_invalid_unknown_and_future_claims_keep_raw_source_evidence(self):
        values = ('2026-10-18T12:00:00Z', 'not a date', None, 'in 2 hours',
                  '999999999999999 days ago', '9999-12-31T12:00:00Z')
        for index, value in enumerate(values):
            with self.subTest(value=value):
                self.payload['jobs'][0]['id'] = 2000 + index
                self.payload['jobs'][0]['publication_date'] = value
                self._collect(request_id='date-control-' + str(index))
        jobs = self.client.get('/jobs', params={'profile_id': 'fixture'}).json()['jobs']
        by_native = {row['attributes']['native_posting_id']: row for row in jobs}
        self.assertEqual(by_native[2000]['attributes']['reported_posted_at'], '2026-10-18T12:00:00+00:00')
        for index in (1, 2, 3, 4):
            self.assertIsNone(by_native[2000 + index]['attributes']['reported_posted_at'])
        filtered = {row['attributes']['native_posting_id'] for row in self._filtered('LAST_24_HOURS')}
        self.assertTrue({2001, 2003, 2004, 2005}.isdisjoint(filtered))
        for index, value in enumerate(values):
            self.assertEqual(by_native[2000 + index]['attributes']['reported_posted_at_raw'], value)

    def test_first_valid_date_after_unknown_is_not_reanchored_on_later_polls(self):
        self.payload['jobs'][0]['publication_date'] = None
        self._collect()
        self.payload['jobs'][0]['publication_date'] = '2 hours ago'
        self._collect(self.collected_at + timedelta(hours=1), 'first-valid-relative')
        first_valid = self._filtered('LAST_24_HOURS')[0]['attributes']['reported_posted_at']
        self.assertEqual(first_valid, (self.collected_at - timedelta(hours=1)).isoformat())
        self._collect(self.collected_at + timedelta(days=2), 'later-stale-relative')
        self._reopen()
        self.assertEqual(self._filtered('LAST_24_HOURS')[0]['attributes']['reported_posted_at'], first_valid)
