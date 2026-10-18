"""Synthetic policy, persisted rate admission and bounded-probe controls."""
import unittest
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.monitoring import (configure_monitoring, queue_refresh,
    reserve_request, schedule_company_sources, source_version)
from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError
from tests.integration.test_company_monitoring_recovery import MonitoringFixture
from workers.operations import claim, request_cancel
from workers.tasks import execute_operation

from services.company_discovery.monitoring_policy import MonitoringPolicy, next_due


class MonitoringPolicyRuntimeTests(unittest.TestCase):
    def test_defaults_and_strict_bounds(self):
        self.assertEqual(MonitoringPolicy().as_dict(), dict(request_timeout=10, rate_limit=60,
                                                         concurrency=1, jitter_seconds=0))
        for key, values in dict(request_timeout=[1, 11, True, 2.0], rate_limit=[0, 61, True],
                                concurrency=[0, 9, True], jitter_seconds=[-1, 301, False]).items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    MonitoringPolicy(**{key: value})

    def test_jitter_is_stable_nonnegative_and_never_shortens_backoff(self):
        stamp = datetime(2026, 10, 18, tzinfo=timezone.utc)
        monitor = dict(id='source', revision=2, poll_interval=300, jitter_seconds=17)
        due = next_due(monitor, stamp, failures=2)
        self.assertEqual(due, next_due(dict(monitor), stamp, failures=2))
        self.assertGreaterEqual(due, stamp + timedelta(seconds=1200))
        self.assertLessEqual(due, stamp + timedelta(seconds=1217))
        monitor['jitter_seconds'] = 0
        self.assertEqual(next_due(monitor, stamp, failures=2), stamp + timedelta(seconds=1200))


class DurableMonitoringPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = MonitoringFixture()
        self.addCleanup(self.fixture.close)
        self.store = self.fixture.store

    def configure(self, **policy):
        return configure_monitoring(self.store, self.fixture.source['id'],
                                    source_version(self.fixture.source), 0, 300, **policy)

    def queued(self):
        return queue_refresh(self.store, self.fixture.source['id'], 'fixture')['operation_id']

    def test_durable_rate_cursor_survives_fresh_store_retry_and_stale_owner(self):
        self.configure(rate_limit=1)
        operation_id = self.queued()
        with patch.dict(os.environ, self.fixture.env):
            operation = claim(self.store, operation_id)
        self.assertEqual(reserve_request(self.store, operation, 1, time.monotonic() + 90), 0)
        saved = self.fixture.monitor()
        self.assertIsNone(saved['last_attempt'], 'rate reservations are not refresh attempts')
        child = subprocess.run([sys.executable, '-c', """
import json, os, time
from sqlalchemy import select
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.monitoring import reserve_request
from services.company_discovery.public_client import PublicFetchError
store = Store(os.environ['MJOBS_DATABASE_URL'])
try:
    with store.engine.connect() as c:
        operation = dict(c.execute(select(m.operations)).mappings().one())
    try:
        reserve_request(store, operation, 1, time.monotonic() + 2)
    except PublicFetchError as error:
        print(json.dumps({'code':error.code,'pid':os.getpid()}))
    else:
        raise AssertionError('replacement reset persisted rate cursor')
finally:
    store.engine.dispose()
"""], env={**os.environ, **self.fixture.env}, capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr[-1000:])
        import json
        readback = json.loads(child.stdout)
        self.assertEqual(readback['code'], 'MONITOR_RATE_DEFERRED')
        self.assertNotEqual(readback['pid'], os.getpid())
        self.assertEqual(self.fixture.monitor()['request_not_before'], saved['request_not_before'])
        reopened = Store(self.store.url)
        try:
            # Model a replacement attempt with the durable operation ownership fence.
            with reopened.engine.begin() as connection:
                connection.execute(update(m.operations).where(m.operations.c.id == operation_id)
                                   .values(attempt_count=operation['attempt_count'] + 1))
            with self.assertRaisesRegex(ValueError, 'OWNERSHIP_CHANGED'):
                reserve_request(self.store, operation, 1, time.monotonic() + 90)
            replacement = {**operation, 'attempt_count': operation['attempt_count'] + 1}
            with self.assertRaisesRegex(PublicFetchError, 'MONITOR_RATE_DEFERRED'):
                reserve_request(reopened, replacement, 1, time.monotonic() + 2)
            self.assertEqual(self.fixture.monitor()['request_not_before'], saved['request_not_before'])
            wait = reserve_request(reopened, replacement, 1, time.monotonic() + 90)
            self.assertGreater(wait, 58)
            self.assertLessEqual(wait, 60)
            self.assertGreater(self.fixture.monitor()['request_not_before'], saved['request_not_before'])
        finally:
            reopened.engine.dispose()

    def test_public_worker_low_rate_defers_without_success_quality_or_retry_storm(self):
        self.configure(rate_limit=1)
        first = self.queued()
        with patch.dict(os.environ, self.fixture.env):
            operation = claim(self.store, first)
        reserve_request(self.store, operation, 1, time.monotonic() + 90)
        # End the prior synthetic attempt without consuming or refunding its slot.
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == first).values(state='CANCELLED'))
        second = self.queued()
        with patch.dict(os.environ, self.fixture.env), patch.object(
                PublicCompanyClient, '_resolve', side_effect=AssertionError('deferred before DNS')):
            result = execute_operation(second)
        self.assertEqual(result['status'], 'BLOCKED', result)
        self.assertEqual(result['error_category'], 'MONITOR_RATE_DEFERRED')
        monitor = self.fixture.monitor()
        self.assertEqual(monitor['status'], 'IDLE')
        self.assertEqual(monitor['failure_count'], 0)
        self.assertIsNone(monitor['completed_operation_id'])
        self.assertEqual(self.store.count('canonical_jobs'), 0)
        self.assertEqual(self.store.count('source_quality_observations'), 0)
        self.assertEqual(schedule_company_sources(self.store)['scheduled'], [])
        self.assertGreaterEqual(datetime.fromisoformat(monitor['next_due']),
                                datetime.fromisoformat(monitor['last_attempt']) + timedelta(seconds=300))

    def test_public_cancel_interrupts_persisted_rate_wait_and_joins_worker(self):
        self.configure()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_source_monitoring).values(
                request_not_before=(datetime.now(timezone.utc) + timedelta(seconds=8)).isoformat()))
        operation_id = self.queued()
        waiting = threading.Event()
        original = PublicCompanyClient._wait
        results = []
        def observe(client, deadline):
            waiting.set()
            return original(client, deadline)
        with patch.dict(os.environ, self.fixture.env), patch.object(PublicCompanyClient, '_wait', observe), patch.object(
                PublicCompanyClient, '_resolve', side_effect=AssertionError('cancel before DNS')):
            thread = threading.Thread(target=lambda: results.append(execute_operation(operation_id)))
            thread.start()
            try:
                self.assertTrue(waiting.wait(5))
                app = create_app(self.store, 'http://127.0.0.1:1', self.fixture.root,
                                 'redis://127.0.0.1:1/0', operation_mode='PERSONAL')
                with TestClient(app) as client:
                    started = time.monotonic()
                    self.assertEqual(client.post(f'/operations/{operation_id}/cancel').status_code, 202)
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                    self.assertLess(time.monotonic() - started, 2)
                    readback = client.get(f'/operations/{operation_id}').json()
                    self.assertEqual(readback['state'], 'CANCELLED')
            finally:
                request_cancel(self.store, operation_id)
                thread.join(10)
        self.assertEqual(results[0]['status'], 'CANCELLED')
        self.assertEqual(self.store.count('source_quality_observations'), 0)
        self.assertIsNone(self.fixture.monitor()['completed_operation_id'])

    def test_snapshot_active_cas_and_review_preserve_rate_and_due(self):
        monitor = self.configure(request_timeout=2, rate_limit=12, concurrency=8, jitter_seconds=300)
        due = monitor['next_due']
        cursor = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_source_monitoring).values(request_not_before=cursor))
        reviewed = configure_monitoring(self.store, self.fixture.source['id'],
            source_version(self.fixture.source), 1, 300)
        self.assertEqual(reviewed['request_not_before'], cursor)
        self.assertGreaterEqual(reviewed['next_due'], due)
        operation_id = self.queued()
        with self.store.engine.connect() as connection:
            params = connection.execute(select(m.operations.c.params).where(m.operations.c.id == operation_id)).scalar_one()
        self.assertEqual(params['monitoring_policy'], dict(request_timeout=2, rate_limit=12,
                                                         concurrency=8, jitter_seconds=300))
        with self.assertRaisesRegex(ValueError, 'ACTIVE_OPERATION'):
            configure_monitoring(self.store, self.fixture.source['id'], source_version(self.fixture.source), 2, 300)

    def test_mixed_source_defer_isolated_and_persisted_due_does_not_redraw_jitter(self):
        from services.company_discovery.sources import upsert_company_job_source
        self.configure(rate_limit=1, jitter_seconds=30)
        other = upsert_company_job_source(self.store, company_id=self.fixture.company, provider='rss',
            tenant_identifier=self.fixture.company, career_url='https://employer.example/careers',
            public_api_url='https://employer.example/feed.xml', collection_strategy='RSS',
            enabled=True, discovered_automatically=True, confidence=.45)
        configure_monitoring(self.store, other['id'], source_version(other), 0, 900, request_timeout=5)
        self.fixture.pages['/careers'] += '<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
        self.fixture.pages['/feed.xml'] = ('<rss><channel><item><guid>other</guid><title>Python Engineer</title>'
            '<description>Python SQL</description><link>https://employer.example/jobs/one</link></item></channel></rss>')
        with self.store.engine.begin() as connection:
            connection.execute(update(m.company_source_monitoring).where(
                m.company_source_monitoring.c.source_id == self.fixture.source['id']).values(
                    request_not_before=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()))
        first = self.queued()
        second = queue_refresh(self.store, other['id'], 'fixture')['operation_id']
        with patch.dict(os.environ, self.fixture.env):
            self.assertEqual(execute_operation(first)['error_category'], 'MONITOR_RATE_DEFERRED')
            with patch.object(PublicCompanyClient, '_request_once', self.fixture.fetch):
                self.assertEqual(execute_operation(second)['status'], 'COMPLETED')
        reopened = Store(self.store.url)
        try:
            with reopened.engine.connect() as connection:
                rows = {row['source_id']: dict(row) for row in connection.execute(
                    select(m.company_source_monitoring)).mappings()}
            deferred = rows[self.fixture.source['id']]
            healthy = rows[other['id']]
            self.assertEqual((deferred['status'], deferred['failure_count']), ('IDLE', 0))
            self.assertEqual((healthy['status'], healthy['request_timeout'], healthy['poll_interval']),
                             ('HEALTHY', 5, 900))
            delay = (datetime.fromisoformat(deferred['next_due']) - datetime.fromisoformat(deferred['updated_at'])).total_seconds()
            self.assertGreaterEqual(delay, 300)
            self.assertLessEqual(delay, 330)
            for _ in range(2):
                self.assertEqual(schedule_company_sources(reopened)['scheduled'], [])
            with reopened.engine.connect() as connection:
                self.assertEqual(connection.execute(select(m.company_source_monitoring.c.next_due).where(
                    m.company_source_monitoring.c.source_id == self.fixture.source['id'])).scalar_one(), deferred['next_due'])
            self.assertEqual(reopened.count('canonical_jobs'), 1)
            self.assertEqual(reopened.count('source_quality_observations'), 1)
        finally:
            reopened.engine.dispose()


class MonitoringFanoutTests(unittest.TestCase):
    def test_parallel_probes_are_bounded_deterministic_and_joined_on_cancel(self):
        from services.company_discovery.application_links import sample_feed_links
        from tests.unit.test_application_links import item, page, ENDPOINT
        from workers.operations import OperationCancelled
        for concurrency in (2, 8):
            with self.subTest(concurrency=concurrency):
                entered = threading.Event()
                release = threading.Event()
                lock = threading.Lock()
                active = []
                seen = []
                peak = []
                class Client:
                    def __init__(self):
                        self.concurrency = concurrency
                    def get(self, url):
                        with lock:
                            active.append(url)
                            seen.append(url)
                            peak.append(len(active))
                            if len(active) == min(concurrency, 5):
                                entered.set()
                        try:
                            self.assert_release()
                            return page(url.rsplit('/', 1)[-1])
                        finally:
                            with lock:
                                active.remove(url)
                    def assert_release(self):
                        if not release.wait(3):
                            raise AssertionError('owned probes not released')
                    def close(self):
                        release.set()
                rows = [item(str(index)) for index in range(8)]
                client = Client()
                result = []
                failures = []
                cancelled = threading.Event()
                def check():
                    if cancelled.is_set():
                        raise OperationCancelled()
                def run():
                    try:
                        result.extend(sample_feed_links(rows, 'RSS', ENDPOINT, client,
                            at='2026-10-18T12:00:00Z', check=check))
                    except BaseException as error:
                        failures.append(error)
                thread = threading.Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(entered.wait(3))
                    cancelled.set()
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(active, [], 'all submitted probes must exit before caller returns')
                    self.assertIsInstance(failures[0], OperationCancelled)
                    self.assertLessEqual(len(seen), min(concurrency, 5))
                    self.assertEqual(max(peak), min(concurrency, 5))
                finally:
                    release.set()
                    thread.join(4)

    def test_parallel_reports_match_serial_and_defer_cannot_be_swallowed(self):
        from services.company_discovery.application_links import sample_feed_links
        from tests.unit.test_application_links import Client, item, page, ENDPOINT
        rows = [item(str(index)) for index in range(8)]
        responses = {row['link']: page(row['guid']) for row in rows}
        serial = Client(responses)
        parallel = Client(responses)
        parallel.concurrency = 8
        at = '2026-10-18T12:00:00Z'
        expected = sample_feed_links(rows, 'RSS', ENDPOINT, serial, at=at)
        self.assertEqual(sample_feed_links(rows, 'RSS', ENDPOINT, parallel, at=at), expected)
        self.assertEqual(len(parallel.calls), 5)
        deferred = Client({row['link']: PublicFetchError('MONITOR_RATE_DEFERRED') for row in rows})
        with self.assertRaisesRegex(PublicFetchError, 'MONITOR_RATE_DEFERRED'):
            sample_feed_links(rows, 'RSS', ENDPOINT, deferred, at=at)


class MonitoringTransportWaitTests(unittest.TestCase):
    def test_dns_capacity_client_capacity_and_robots_lock_waits_cancel_promptly(self):
        from workers.operations import OperationCancelled
        for kind in ('dns', 'capacity', 'robots', 'rate'):
            with self.subTest(kind=kind):
                entered = threading.Event()
                cancelled = threading.Event()
                errors = []
                def check():
                    if cancelled.is_set():
                        raise OperationCancelled()
                client = PublicCompanyClient('https://employer.example', check=check,
                    policy=MonitoringPolicy(request_timeout=2))
                class Held:
                    def acquire(self, timeout):
                        entered.set()
                        time.sleep(timeout)
                        return False
                held = Held()
                def run():
                    try:
                        if kind == 'dns':
                            client._resolve('employer.example', 443)
                        elif kind == 'capacity':
                            client.get('https://employer.example/jobs')
                        elif kind == 'robots':
                            client._load_robots(time.monotonic() + 10)
                        else:
                            client._wait(time.monotonic() + 10)
                    except BaseException as error:
                        errors.append(error)
                with patch('services.company_discovery.public_client._DNS_SLOTS', held), patch.object(
                        client, {'dns':'_capacity','capacity':'_capacity','robots':'_robots_lock','rate':'_rate_lock'}[kind], held):
                    thread = threading.Thread(target=run)
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        start = time.monotonic()
                        cancelled.set()
                        thread.join(1)
                        self.assertFalse(thread.is_alive())
                        self.assertLess(time.monotonic()-start, .5)
                        self.assertIsInstance(errors[0], OperationCancelled)
                    finally:
                        cancelled.set()
                        thread.join(2)
                        client.close()

    def test_owned_pending_dns_future_releases_capacity_after_cancel(self):
        from concurrent.futures import ThreadPoolExecutor
        from workers.operations import OperationCancelled
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        slots = threading.BoundedSemaphore(1)
        errors = []
        def check():
            if cancelled.is_set():
                raise OperationCancelled()
        def lookup(*_args, **_kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('owned resolver not released')
            return []
        client = PublicCompanyClient('https://employer.example', check=check, policy=MonitoringPolicy(request_timeout=2))
        def run():
            try:
                client._resolve('employer.example', 443)
            except BaseException as error:
                errors.append(error)
        with ThreadPoolExecutor(max_workers=1) as executor, patch(
                'services.company_discovery.public_client._DNS_EXECUTOR', executor), patch(
                'services.company_discovery.public_client._DNS_SLOTS', slots), patch(
                'services.company_discovery.public_client.socket.getaddrinfo', lookup):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                cancelled.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(errors[0], OperationCancelled)
            finally:
                release.set()
                thread.join(3)
                client.close()
        self.assertTrue(slots.acquire(blocking=False), 'finished native lookup returns its bounded slot')
        slots.release()
