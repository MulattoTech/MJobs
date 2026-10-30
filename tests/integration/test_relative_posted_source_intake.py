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
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_remotive import RAW_REMOTIVE_PAYLOAD
from tests.integration.test_remoteok import RAW_REMOTEOK_PAYLOAD
from tests.integration.test_wwr import RAW_WWR_RSS
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
        self.expected_host, self.expected_path = 'remotive.com', '/api/remote-jobs'
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
                             ('GET', self.expected_host, self.expected_path))
            self.requests.append(str(request.url))
            if isinstance(self.payload, bytes):
                return httpx.Response(200, content=self.payload,
                    headers={'content-type': 'application/rss+xml'})
            return httpx.Response(200, json=self.payload)
        with patch('httpx.HTTPTransport.handle_request', boundary), patch(
                'services.discovery.sources.collect.now', return_value=collected_at.isoformat()), patch(
                'services.discovery.sources.collect.datetime', CollectionClock):
            outcome = execute_operation(identifier)
            self.assertEqual(outcome['status'], 'COMPLETED', outcome)
        return identifier

    def _filtered(self, window, query_at=None):
        saved = self.client.put('/search-profiles', json={'id': 'date-filter', 'profile_id': 'fixture',
            'name': 'Synthetic date filter', 'date_window': window, 'enabled': True})
        self.assertEqual(saved.status_code, 200, saved.text)
        def query():
            return self.client.get('/jobs', params={
                'profile_id': 'fixture', 'search_profile_id': 'date-filter'})
        if query_at is None:
            response = query()
        else:
            class QueryClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return query_at.astimezone(tz) if tz else query_at.replace(tzinfo=None)
            with patch('apps.api.main.datetime', QueryClock), patch(
                    'services.discovery.search_semantics.datetime', QueryClock):
                response = query()
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
        # An explicit absolute source correction is stronger than another mutable
        # age label and must survive subsequent relative observations.
        corrected = (self.collected_at - timedelta(hours=3)).isoformat()
        self.payload['jobs'][0]['publication_date'] = corrected
        self._collect(self.collected_at + timedelta(days=3), 'explicit-date-correction')
        self.payload['jobs'][0]['publication_date'] = '2 hours ago'
        self._collect(self.collected_at + timedelta(days=4), 'relative-after-correction')
        self._reopen()
        corrected_attrs = self._filtered('LAST_24_HOURS')[0]['attributes']
        self.assertEqual(corrected_attrs['reported_posted_at'], corrected)
        self.assertEqual(corrected_attrs['reported_posted_at_precision'], 'SOURCE')

    def test_pagination_skips_newer_invalid_candidates_and_stays_posting_scoped(self):
        target_native_id = self.payload['jobs'][0]['id']
        other = deepcopy(RAW_REMOTIVE_PAYLOAD['jobs'][1])
        other_date = (self.collected_at - timedelta(minutes=15)).isoformat()
        other['publication_date'] = other_date
        self.payload['jobs'].append(other)
        self.payload['job-count'] = 2
        self.payload['jobs'][0]['publication_date'] = None
        self._collect()
        self.payload['jobs'][0]['publication_date'] = '2 hours ago'
        first_valid_at = self.collected_at + timedelta(hours=1)
        self._collect(first_valid_at, 'first-valid-before-recoverable-legacy')
        older_date = (self.collected_at - timedelta(hours=1)).isoformat()
        with self.store.engine.begin() as connection:
            postings = list(connection.execute(select(m.postings)).mappings())
            target_posting = next(row for row in postings if row['raw']['id'] == target_native_id)
            other_posting = next(row for row in postings if row['raw']['id'] == other['id'])
            target_candidates = list(connection.execute(select(m.posting_value_candidates).where(
                m.posting_value_candidates.c.posting_id == target_posting['id']).order_by(
                m.posting_value_candidates.c.observed_at.desc(),
                m.posting_value_candidates.c.id.desc())).mappings())
            older, initial_unknown = target_candidates
            self.assertNotEqual(older['id'], initial_unknown['id'])
            self.assertEqual(older['observed_at'], first_valid_at.isoformat())
            self.assertEqual(older['normalized']['attributes']['reported_posted_at'], older_date)
            self.assertIsNone(initial_unknown['normalized']['attributes']['reported_posted_at'])
            other_candidate = connection.execute(select(m.posting_value_candidates).where(
                m.posting_value_candidates.c.posting_id == other_posting['id']).order_by(
                m.posting_value_candidates.c.observed_at.desc())).mappings().first()
            self.assertNotEqual(other_candidate['posting_id'], older['posting_id'])
            self.assertEqual(other_candidate['normalized']['attributes']['reported_posted_at'], other_date)
            invalid_ids = []
            for offset in range(2, 67):
                invalid = dict(older)
                invalid['id'] = hashlib.sha256(
                    f"{target_posting['id']}:invalid:{offset}".encode()).hexdigest()
                invalid['observed_at'] = (self.collected_at + timedelta(hours=offset)).isoformat()
                invalid['normalized'] = deepcopy(older['normalized'])
                invalid['normalized']['attributes'].update(
                    reported_posted_at='not a timestamp',
                    reported_posted_at_raw='not a timestamp',
                    reported_posted_at_precision='UNKNOWN',
                    reported_posted_at_reference_at='also not a timestamp',
                )
                connection.execute(m.posting_value_candidates.insert().values(**invalid))
                invalid_ids.append(invalid['id'])
            invalid_rows = list(connection.execute(select(m.posting_value_candidates).where(
                m.posting_value_candidates.c.id.in_(invalid_ids))).mappings())
            self.assertEqual(len(invalid_rows), 65)
            self.assertEqual({row['id'] for row in invalid_rows}, set(invalid_ids))
            self.assertEqual({row['posting_id'] for row in invalid_rows}, {target_posting['id']})
            self.assertTrue(all(row['observed_at'] > older['observed_at'] for row in invalid_rows))
            self.assertEqual({row['normalized']['attributes']['reported_posted_at']
                              for row in invalid_rows}, {'not a timestamp'})
            canonical = connection.execute(select(m.jobs).where(
                m.jobs.c.id == target_posting['job_id'])).mappings().one()
            canonical_attributes = deepcopy(canonical['attributes'])
            canonical_attributes.update(source_id='different-source', reported_posted_at=other_date,
                reported_posted_at_raw=other_date, reported_posted_at_precision='SOURCE',
                reported_posted_at_reference_at=None)
            connection.execute(update(m.jobs).where(m.jobs.c.id == target_posting['job_id']).values(
                attributes=canonical_attributes))
        self.payload['jobs'][0]['publication_date'] = '2 hours ago'
        self._collect(self.collected_at + timedelta(days=4), 'relative-after-invalid-candidate-pages')
        self._reopen()
        rows = self.client.get('/jobs', params={'profile_id': 'fixture'}).json()['jobs']
        by_native = {row['attributes']['native_posting_id']: row for row in rows}
        attrs = by_native[target_native_id]['attributes']
        self.assertEqual(attrs['reported_posted_at'], older_date)
        self.assertEqual(attrs['reported_posted_at_raw'], '2 hours ago')
        self.assertEqual(attrs['reported_posted_at_reference_at'], first_valid_at.isoformat())
        self.assertEqual(by_native[other['id']]['attributes']['reported_posted_at'], other_date)
        filtered = {row['attributes']['native_posting_id'] for row in self._filtered('LAST_24_HOURS')}
        self.assertEqual(filtered, {target_native_id, other['id']})

    def test_remoteok_missing_or_invalid_date_retains_valid_epoch_after_public_collection(self):
        source = self.client.put('/sources', json={'provider': 'remoteok', 'tenant': 'default',
            'poll_interval': 300, 'request_timeout': 8, 'rate_limit': 60, 'jitter_seconds': 0})
        self.assertEqual(source.status_code, 200, source.text)
        self.source_id = source.json()['id']
        self.expected_host, self.expected_path = 'remoteok.com', '/api'
        self.payload = deepcopy(RAW_REMOTEOK_PAYLOAD)
        self.payload[1].pop('date')
        self.payload[2]['date'] = 'not a source date'
        expected = {}
        for row in self.payload[1:]:
            row['epoch'] = int((self.collected_at - timedelta(days=2)).timestamp())
            expected[row['id']] = datetime.fromtimestamp(row['epoch'], timezone.utc).isoformat()
        identifier = self._collect(request_id='remoteok-epoch-intake')
        self._reopen()
        response = self.client.get('/jobs', params={'profile_id': 'fixture'})
        self.assertEqual(response.status_code, 200)
        rows = response.json()['jobs']
        self.assertEqual(len(rows), 2)
        for row in rows:
            with self.subTest(native_id=row['requisition']):
                attrs = row['attributes']
                self.assertEqual(attrs['reported_posted_at'], expected[row['requisition']])
                self.assertEqual(attrs['reported_posted_at_precision'], 'SOURCE')
                self.assertEqual(attrs['reported_posted_at_raw'], None if row['requisition'] == '2001'
                                 else 'not a source date')
                detail = self.client.get('/jobs/' + row['id'], params={'profile_id': 'fixture'}).json()
                raw = detail['postings'][0]['raw']
                self.assertEqual(datetime.fromtimestamp(raw['epoch'], timezone.utc).isoformat(),
                                 attrs['reported_posted_at'])
        self.assertEqual(len(self._filtered('LAST_HOUR')), 0)
        self.assertEqual(len(self._filtered('LAST_3_DAYS')), 2)
        self.assertEqual(execute_operation(identifier)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        self.assertEqual(len(self.requests), 1)

    def test_wwr_rfc2822_pubdate_survives_public_collection_search_and_reopen(self):
        source = self.client.put('/sources', json={'provider': 'wwr', 'tenant': 'default',
            'poll_interval': 300, 'request_timeout': 8, 'rate_limit': 60, 'jitter_seconds': 0})
        self.assertEqual(source.status_code, 200, source.text)
        self.source_id = source.json()['id']
        self.expected_host, self.expected_path = 'weworkremotely.com', '/remote-jobs.rss'
        self.payload = RAW_WWR_RSS
        identifier = self._collect(request_id='wwr-rfc2822')
        self._reopen()
        # Keep the existing RSS fixture literal and its bounded windows stable
        # when this test is rerun after the fixture's publication month.
        query_at = datetime(2026, 10, 18, 20, tzinfo=timezone.utc)
        class RssQueryClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return query_at.astimezone(tz) if tz else query_at.replace(tzinfo=None)
        with patch('services.discovery.search_semantics.datetime', RssQueryClock):
            rows = self._filtered('LAST_30_DAYS')
            recent = self._filtered('LAST_24_HOURS')
        self.assertEqual(len(rows), 3)
        first = next(row for row in rows if row['requisition'] == 'acme-cloud-senior-python-engineer')
        self.assertEqual(first['attributes']['reported_posted_at'], '2026-10-06T12:00:00+00:00')
        self.assertEqual(first['attributes']['reported_posted_at_raw'], 'Tue, 06 Oct 2026 12:00:00 +0000')
        self.assertEqual(first['attributes']['reported_posted_at_precision'], 'SOURCE')
        self.assertEqual(len(recent), 0)
        detail = self.client.get('/jobs/' + first['id'], params={'profile_id': 'fixture'}).json()
        self.assertEqual(detail['postings'][0]['raw']['pubDate'], first['attributes']['reported_posted_at_raw'])
        self.assertEqual(execute_operation(identifier)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        self.assertEqual(len(self.requests), 1)

    def _emulate_preupgrade_dates(self, reported, *, first_seen=None):
        # Persist the legacy shape that predates precision/raw/reference fields.
        # New observations still enter through the actual public producer/worker.
        metadata = ('reported_posted_at_raw', 'reported_posted_at_precision',
                    'reported_posted_at_reference_at')
        with self.store.engine.begin() as connection:
            for row in connection.execute(select(m.jobs)).mappings():
                attrs = deepcopy(row['attributes'])
                for key in metadata:
                    attrs.pop(key, None)
                attrs['reported_posted_at'] = reported
                if first_seen:
                    attrs['first_seen_at'] = first_seen
                connection.execute(update(m.jobs).where(m.jobs.c.id == row['id']).values(attributes=attrs))
            for row in connection.execute(select(m.posting_value_candidates)).mappings():
                values = deepcopy(row['normalized'])
                for key in metadata:
                    values['attributes'].pop(key, None)
                values['attributes']['reported_posted_at'] = reported
                connection.execute(update(m.posting_value_candidates).where(
                    m.posting_value_candidates.c.id == row['id']).values(normalized=values))

    def test_preupgrade_absolute_date_without_precision_is_not_refreshed_by_relative_recollection(self):
        collected_at = datetime(2020, 1, 15, 12, tzinfo=timezone.utc)
        recollected_at = collected_at + timedelta(hours=1)
        query_at = recollected_at + timedelta(hours=1)
        self.collected_at = collected_at
        original = (collected_at - timedelta(days=10)).isoformat()
        self.payload['jobs'][0]['publication_date'] = original
        self._collect(collected_at, 'legacy-absolute')
        self._emulate_preupgrade_dates(original)
        self._reopen()
        self.assertEqual(len(self._filtered('LAST_24_HOURS', query_at)), 0)
        self.payload['jobs'][0]['publication_date'] = '2 hours ago'
        self._collect(recollected_at, 'relative-after-legacy-absolute')
        self._reopen()
        self.assertEqual(len(self._filtered('LAST_24_HOURS', query_at)), 0)
        job = self._filtered('LAST_30_DAYS', query_at)[0]
        self.assertEqual(job['attributes']['reported_posted_at'], original)
        self.assertIsNone(job['attributes']['reported_posted_at_raw'])
        self.assertEqual(job['attributes']['reported_posted_at_precision'], 'SOURCE')
        self.assertIsNone(job['attributes']['reported_posted_at_reference_at'])
        detail = self.client.get('/jobs/' + job['id'], params={'profile_id': 'fixture'}).json()
        self.assertEqual(detail['postings'][0]['raw']['publication_date'], '2 hours ago')
        with self.store.engine.connect() as connection:
            snapshots = list(connection.execute(select(m.posting_snapshots)).mappings())
        self.assertEqual({snapshot['raw']['publication_date'] for snapshot in snapshots},
                         {original, '2 hours ago'})
        self.assertEqual({snapshot['sha256'] for snapshot in snapshots},
                         {snapshot['sha256'] for snapshot in detail['postings'][0]['snapshots']})

    def test_preupgrade_relative_claim_uses_its_candidate_observation_and_remains_stable(self):
        self._collect(request_id='legacy-relative')
        self._emulate_preupgrade_dates('2 hours ago',
            first_seen=(self.collected_at - timedelta(hours=5)).isoformat())
        self._reopen()
        self.payload['jobs'][0]['publication_date'] = '3 hours ago'
        self._collect(self.collected_at + timedelta(hours=1), 'changed-age-after-legacy-relative')
        self._reopen()
        first = self._filtered('LAST_24_HOURS')[0]
        expected = (self.collected_at - timedelta(hours=2)).isoformat()
        self.assertEqual(first['attributes']['reported_posted_at'], expected)
        self.assertEqual(first['attributes']['reported_posted_at_reference_at'], self.collected_at.isoformat())
        self.assertEqual(first['attributes']['reported_posted_at_raw'], '2 hours ago')
        self.payload['jobs'][0]['publication_date'] = '2 hours ago'
        identifier = self._collect(self.collected_at + timedelta(days=2), 'stale-age-after-legacy-relative')
        self._reopen()
        job = self._filtered('LAST_24_HOURS')[0]
        self.assertEqual(job['attributes']['reported_posted_at'], expected)
        self.assertEqual(job['attributes']['reported_posted_at_reference_at'], self.collected_at.isoformat())
        self.assertEqual(job['attributes']['last_seen_at'], (self.collected_at + timedelta(days=2)).isoformat())
        self.assertEqual(execute_operation(identifier)['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        self.assertEqual(len(self.requests), 3)
