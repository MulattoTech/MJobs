"""MJ-REQ-010: public AI review -> real RQ workers -> durable documents.

Linux acceptance uses only the admitted disposable CI PostgreSQL/Redis services
and a synthetic loopback model. Windows runs admission controls only. No direct
task execution or mocked router substitutes for the connected oracle.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from redis import Redis
from rq import Queue, Worker
from rq.job import Job
from rq.registry import StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from packages.persistence import models as m
from packages.persistence.store import Store
from apps.api.main import create_app
from services.ai.consumer import AIJobEvaluation
from services.ai.providers.local_endpoint import LocalEndpointProvider
from tests.integration.test_spine import PROFILE

ROOT = Path(__file__).resolve().parents[2]
QUEUES = ('mjobs-ai', 'mjobs-document')
SYNTHETIC_KEY = 'synthetic-loopback-ai-only'


def _admit_postgres(value):
    url = make_url(value)
    if (url.drivername not in ('postgresql', 'postgresql+psycopg')
            or url.host not in ('127.0.0.1', 'localhost', '::1') or url.query
            or not url.database or not (url.database == 'mjobs_test'
                                       or url.database.startswith('mjobs_test_'))):
        raise ValueError('Disposable loopback mjobs_test PostgreSQL required')
    return url


def _admit_redis(value):
    target = urlsplit(value)
    if (target.scheme != 'redis' or target.hostname not in ('127.0.0.1', 'localhost')
            or target.port != 6379 or target.path != '/0' or target.username
            or target.password or target.query or target.fragment):
        raise ValueError('Disposable loopback Redis port6379 database0 required')
    return value


def _child_environment():
    return {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'TMPDIR',
                                           'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP')
            if key in os.environ}


@contextmanager
def _model_endpoint():
    # Capture only a finite request count and contract checks, never prompts,
    # credentials, response text or provider diagnostics in test failure output.
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.connection.settimeout(3)
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 512_000:
                self.send_error(413)
                return
            payload = json.loads(self.rfile.read(size))
            observed.append((self.path, self.headers.get('Authorization') ==
                             'Bearer ' + SYNTHETIC_KEY, payload.get('model')))
            if len(observed) > 1:
                self.send_error(429, 'Synthetic request budget exhausted')
                return
            answer = {'category': 'STRONG', 'rationale': 'Synthetic advisory result.',
                      'job_evidence': []}
            body = json.dumps({'choices': [{'message': {'content': json.dumps(answer)}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@unittest.skipUnless(sys.platform == 'linux', 'NOT_RUN: production RQ work horse requires Linux')
@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL') and os.environ.get('MJOBS_REDIS_URL'),
                     'NOT_RUN: admitted disposable CI PostgreSQL/Redis absent')
class PrimaryProductAIRealRQTests(unittest.TestCase):
    def setUp(self):
        # Validate every target before connecting, migrating or spawning anything.
        target = _admit_postgres(os.environ['MJOBS_TEST_POSTGRES_URL'])
        self.redis_url = _admit_redis(os.environ['MJOBS_REDIS_URL'])
        self.redis = Redis.from_url(self.redis_url, socket_connect_timeout=2, socket_timeout=2)
        self.addCleanup(self.redis.close)
        self.redis.ping()  # Configured but unavailable substrate is FAIL, not SKIP.
        for name in QUEUES:
            self.assertEqual(Queue(name, connection=self.redis).get_job_ids(), [],
                             'Refusing preexisting queue work')
            for registry in (StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry):
                self.assertEqual(registry(name, connection=self.redis).get_job_ids(cleanup=False), [],
                                 'Refusing preexisting active/scheduled work')
        self.assertFalse([w for w in Worker.all(connection=self.redis)
                          if set(w.queue_names()) & set(QUEUES)], 'Queue already has another consumer')
        self.directory = tempfile.TemporaryDirectory(prefix='mjobs-ai-public-rq-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.schema = 'mjobs_ai_rq_' + uuid4().hex
        self.admin = create_engine(target, connect_args={'connect_timeout': 3})
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.addCleanup(self._drop_schema)
        scoped = target.update_query_dict({'options': '-csearch_path=' + self.schema
            + ' -clock_timeout=5000 -cstatement_timeout=30000', 'connect_timeout': '3'})
        self.url = scoped.render_as_string(hide_password=False)
        self.environment = _child_environment()
        self.environment.update(MJOBS_DATABASE_URL=self.url, MJOBS_REDIS_URL=self.redis_url,
            MJOBS_OUTPUT_DIR=str(self.root / 'documents'), MJOBS_OPERATION_MODE='PERSONAL',
            MJOBS_MOCK_ORIGIN='http://127.0.0.1:1', MJOBS_OPERATION_TIMEOUT='60', PYTHONUNBUFFERED='1')
        with (self.root / 'migration.log').open('w') as log:
            migrated = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'],
                cwd=ROOT, env=self.environment, stdout=log, stderr=log, timeout=60)
        self.assertEqual(migrated.returncode, 0, 'Private schema migration failed')
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.owned_operations = set()
        self.addCleanup(self._delete_owned_jobs)
        self.api = None
        self.addCleanup(self._stop_api)
        self.client = httpx.Client(timeout=3, trust_env=False)
        self.addCleanup(self.client.close)

    def _drop_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))

    def _delete_owned_jobs(self):
        # Only IDs returned from public admission in this private schema.
        for identifier in self.owned_operations:
            if Job.exists(identifier, connection=self.redis):
                Job.fetch(identifier, connection=self.redis).delete()

    @staticmethod
    def _stop_process(process):
        if process is None:
            return
        # This process was created with its own session; include only its workhorse.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)

    def _stop_api(self):
        self._stop_process(self.api)
        self.api = None

    def _start_api(self, model_url):
        self._stop_api()
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        environment = dict(self.environment, MJOBS_LOCAL_AI_BASE_URL=model_url,
                           MJOBS_LOCAL_AI_API_KEY=SYNTHETIC_KEY)
        with (self.root / 'api.log').open('a') as log:
            self.api = subprocess.Popen([sys.executable, '-m', 'uvicorn',
                'apps.api.main:create_from_env', '--factory', '--host', '127.0.0.1',
                '--port', str(port), '--log-level', 'warning'], cwd=ROOT, env=environment,
                stdout=log, stderr=log, start_new_session=True)
        self.base = f'http://127.0.0.1:{port}'
        def ready():
            self.assertIsNone(self.api.poll(), 'Production API exited during startup')
            try:
                return self.client.get(self.base + '/health').status_code == 200
            except httpx.TransportError:
                return False
        self._until(ready, 'Production HTTP API did not start')

    def _until(self, predicate, message):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail(message)

    def _call(self, method, path, *, expected=200, **kwargs):
        response = self.client.request(method, self.base + path, **kwargs)
        self.assertEqual(response.status_code, expected, 'Public route failed: ' + path)
        result = response.json()
        if isinstance(result, dict) and result.get('operation_id'):
            self.owned_operations.add(result['operation_id'])
        return result

    def _seed_public_inputs(self):
        self.profile = deepcopy(PROFILE)
        self.profile['id'] = 'ai-rq-' + uuid4().hex
        self.profile['facts'].append({'id': 'unapproved', 'text': 'Kubernetes', 'kind': 'skill',
            'source': 'fictional-unapproved', 'verified': False, 'user_approved': False, 'confidence': 0})
        self.profile_path = '/profiles/' + self.profile['id']
        self._call('PUT', '/profiles', json=self.profile)
        self.saved_facts = self._call('GET', self.profile_path)['facts']
        job = self._call('POST', '/jobs/manual', expected=201, json={
            'request_id': 'manual-' + uuid4().hex, 'title': 'Synthetic Python engineer',
            'company': 'Synthetic AI Flow Employer',
            'description': 'Responsibilities:\n- Build Python SQL services for this synthetic employer.',
            'reference_url': 'https://jobs.example.test/ai-rq',
            'application_url': 'https://apply.example.test/ai-rq', 'source_details': {},
        })
        self.job_id = job['job']['id']
        self.ai_path = self.profile_path + '/ai-cost-plan'
        self._call('PUT', self.profile_path + '/ai-task-settings', json={
            'tasks': {'job_scoring': {'provider': 'local', 'model': 'synthetic-model'}}})
        self._call('PUT', self.profile_path + '/ai-cost-policy', json={
            'minimum_score': 75, 'max_input_candidates': 1,
            'max_ai_candidates': 1, 'max_document_candidates': 1})

    def _queue_ai(self, request_id):
        return self._call('POST', self.ai_path + '/evaluate/queued', expected=202,
                          json={'request_id': request_id})

    def _revise_public_profile(self):
        current = self._call('GET', self.profile_path)
        self.profile['name'] = 'Alex Synthetic Revision'
        saved = self._call('PUT', '/profiles', json=dict(
            self.profile, expected_version=current['version']))
        self.assertNotEqual(saved['version'], current['version'])
        readback = self._call('GET', self.profile_path)
        self.assertEqual(readback['version'], saved['version'])
        self.assertEqual(readback['name'], self.profile['name'])
        return saved

    @contextmanager
    def _worker(self, queue, model_url):
        environment = dict(self.environment, MJOBS_WORKER_QUEUE=queue,
            MJOBS_LOCAL_AI_BASE_URL=model_url, MJOBS_LOCAL_AI_API_KEY=SYNTHETIC_KEY)
        with (self.root / (queue + '.log')).open('a') as log:
            process = subprocess.Popen([sys.executable, '-m', 'workers.main'], cwd=ROOT,
                env=environment, stdout=log, stderr=log, start_new_session=True)
        registered = {}
        try:
            def ready():
                self.assertIsNone(process.poll(), 'Production worker exited during startup')
                for worker in Worker.all(connection=self.redis):
                    if worker.pid == process.pid:
                        self.assertEqual(worker.queue_names(), [queue])
                        registered[worker.name] = worker
                        return True
                return False
            self._until(ready, 'Production worker did not register')
            yield process, registered
        finally:
            self._stop_process(process)
            for worker in Worker.all(connection=self.redis):
                if worker.pid == process.pid:
                    registered[worker.name] = worker
            for worker in registered.values():
                worker.register_death()

    def _finished(self, identifier, queue, registered):
        def done():
            status = self._call('GET', '/operations/' + identifier)
            self.assertNotIn(status['state'], ('FAILED', 'BLOCKED', 'DEAD_LETTERED', 'CANCELLED'),
                             'Connected operation terminated without completing')
            if not Job.exists(identifier, connection=self.redis):
                return False
            job = Job.fetch(identifier, connection=self.redis)
            return status['state'] == 'COMPLETED' and job.get_status(refresh=True) == 'finished'
        self._until(done, 'Public operation / actual RQ job did not complete')
        job = Job.fetch(identifier, connection=self.redis)
        self.assertEqual(job.func_name, 'workers.tasks.execute_operation')
        self.assertEqual(job.args, (identifier,))
        self.assertEqual(job.origin, queue)
        self.assertIn(job.worker_name, registered)

    def test_public_ai_review_document_worker_and_restart_readback(self):
        with _model_endpoint() as (model_url, calls):
            self._start_api(model_url)
            self._seed_public_inputs()
            request_id = 'evaluation-' + uuid4().hex
            admitted = self._queue_ai(request_id)
            replay = self._queue_ai(request_id)
            identifier = admitted['operation_id']
            self.assertEqual(replay['operation_id'], identifier)
            self.assertTrue(replay['replayed'])
            self.assertEqual(calls, [])
            # API dies while the acknowledged operation is still in the outbox.
            self._start_api(model_url)
            self.assertEqual(self._call('GET', self.ai_path + '/operations/' + identifier)['state'], 'QUEUED')
            with self._worker('mjobs-ai', model_url) as (_, registered):
                self._finished(identifier, 'mjobs-ai', registered)
            result = self._call('GET', self.ai_path + '/operations/' + identifier)['result']
            self.assertEqual(len(result['evaluations']), 1)
            self.assertEqual(result['evaluations'][0]['job_id'], self.job_id)
            self.assertEqual(result['evaluations'][0]['category'], 'STRONG')
            self.assertEqual(result['evaluations'][0]['deterministic_score'], 100)
            self.assertEqual(result['documents_created'], 0)
            self.assertEqual(calls, [('/v1/chat/completions', True, 'synthetic-model')])
            preparation = {'profile_id': self.profile['id'], 'job_id': self.job_id,
                'ai_evaluation_run_id': identifier, 'request_id': 'documents-' + uuid4().hex}
            self._call('POST', '/applications', expected=409, json=preparation)
            review_path = self.ai_path + '/evaluations/' + identifier + '/review-documents'
            reviewed = self._call('POST', review_path, json={'job_ids': [self.job_id]})
            self.assertEqual(reviewed['reviewed_job_ids'], [self.job_id])
            self.assertEqual(self._call('POST', review_path, json={'job_ids': [self.job_id]})[
                'reviewed_candidate_count'], 1)
            prepared = self._call('POST', '/applications', expected=202, json=preparation)
            with self._worker('mjobs-document', model_url) as (_, registered):
                self._finished(prepared['operation_id'], 'mjobs-document', registered)
            again = self._call('POST', '/applications', expected=202, json=preparation)
            self.assertEqual(again['operation_id'], prepared['operation_id'])
            self.assertEqual(again['application_id'], prepared['application_id'])
            self.assertEqual(self._queue_ai(request_id)['operation_id'], identifier)
            self._start_api(model_url)
            history = self._call('GET', self.ai_path + '/evaluations')['runs']
            self.assertEqual([run['run_id'] for run in history], [identifier])
            application = self._call('GET', self.profile_path + '/applications/' + prepared['application_id'])
            self.assertEqual(application['state'], 'PREPARED')
            self.assertIsNone(application['confirmation'])
            # Read-only DB checks bind reviewed AI lineage to actual worker outputs.
            with self.store.engine.connect() as connection:
                reviews = connection.execute(select(m.ai_document_reviews)).mappings().all()
                documents = connection.execute(select(m.documents)).mappings().all()
                operations = connection.execute(select(m.operations)).mappings().all()
            self.assertEqual(len(reviews), 1)
            self.assertEqual(reviews[0]['run_id'], identifier)
            self.assertEqual({doc['kind'] for doc in documents}, {'resume', 'cover-letter'})
            self.assertEqual(len(documents), 2)
            self.assertTrue(all(op['state'] == 'COMPLETED' and op['attempt_count'] == 1 for op in operations))
            for doc in documents:
                response = self.client.get(self.base + self.profile_path + '/documents/' + doc['id'] + '/txt')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(hashlib.sha256(response.content).hexdigest(), doc['sha256']['txt'])
                self.assertIn('Python', response.text)
                self.assertNotIn('Kubernetes', response.text)
            self.assertEqual(self._call('GET', self.profile_path)['facts'],
                             self.saved_facts)
            self.assertEqual(len(calls), 1)

    def test_cancelled_admission_and_stale_review_cannot_prepare_documents(self):
        with _model_endpoint() as (model_url, calls):
            self._start_api(model_url)
            self._seed_public_inputs()
            cancel_request = 'cancel-' + uuid4().hex
            cancelled = self._queue_ai(cancel_request)['operation_id']
            cancel_path = self.ai_path + '/operations/' + cancelled
            self.assertEqual(self._call('POST', cancel_path + '/cancel')['state'], 'CANCELLED')
            self.assertEqual(self._queue_ai(cancel_request)['state'], 'CANCELLED')
            admitted = self._queue_ai('current-' + uuid4().hex)['operation_id']
            with self._worker('mjobs-ai', model_url) as (_, registered):
                self._finished(admitted, 'mjobs-ai', registered)
            self.assertEqual(len(calls), 1, 'Cancelled operation must not call the model')
            self._revise_public_profile()
            self._start_api(model_url)
            self.assertEqual(self._call('GET', cancel_path)['state'], 'CANCELLED')
            rejected = self._call('POST', self.ai_path + '/evaluations/' + admitted + '/review-documents',
                expected=409, json={'job_ids': [self.job_id]})
            self.assertEqual(rejected['detail']['code'], 'AI_EVALUATION_PROFILE_CHANGED')
            self._call('POST', '/applications', expected=409, json={
                'profile_id': self.profile['id'], 'job_id': self.job_id, 'ai_evaluation_run_id': admitted})
            self.assertEqual(self.store.count('ai_document_reviews'), 0)
            self.assertEqual(self.store.count('documents'), 0)
            self.assertEqual(self.store.count('applications'), 0)
            self.assertEqual(len(calls), 1)


class AIFlowAdmissionTests(unittest.TestCase):
    def test_rejects_non_disposable_postgres_and_url_options(self):
        for value in (
            'postgresql+psycopg://test:synthetic@remote.example/mjobs_test',
            'postgresql+psycopg://test:synthetic@127.0.0.1/personal',
            'postgresql+psycopg://test:synthetic@127.0.0.1/mjobs_test?options=-csearch_path=public',
            'sqlite:///personal.db',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _admit_postgres(value)

    def test_rejects_remote_authenticated_or_other_database_redis(self):
        for value in ('redis://remote.example:6379/0', 'redis://127.0.0.1:6379/1',
                      'redis://user:secret@127.0.0.1:6379/0',
                      'redis://127.0.0.1:6379/0?socket_timeout=99'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _admit_redis(value)

    def test_child_environment_drops_provider_secrets_proxies_and_personal_targets(self):
        with patch.dict(os.environ, {'PATH': 'synthetic-path', 'HOME': '/synthetic',
                'OPENAI_API_KEY': 'private-sentinel', 'MJOBS_DATABASE_URL': 'private-db',
                'HTTPS_PROXY': 'private-proxy', 'PYTHONPATH': 'private-modules'}, clear=True):
            self.assertEqual(_child_environment(), {'PATH': 'synthetic-path', 'HOME': '/synthetic'})

    def test_admits_only_explicit_local_test_targets(self):
        self.assertEqual(_admit_postgres(
            'postgresql+psycopg://test:synthetic@127.0.0.1:5432/mjobs_test').database, 'mjobs_test')
        self.assertEqual(_admit_redis('redis://127.0.0.1:6379/0'), 'redis://127.0.0.1:6379/0')


class AIFlowFixtureContractTests(unittest.TestCase):
    """Portable boundary/admission checks; these do not execute or certify RQ."""

    def test_loopback_response_uses_actual_local_provider_contract(self):
        with _model_endpoint() as (model_url, calls):
            provider = LocalEndpointProvider(base_url=model_url, api_key=SYNTHETIC_KEY)
            result = provider.generate(task='job_scoring', model='synthetic-model',
                prompt='Classify the fictional Python SQL job.', timeout_seconds=3,
                response_schema=AIJobEvaluation.model_json_schema())
            self.assertEqual(AIJobEvaluation.model_validate_json(result).category, 'STRONG')
            self.assertEqual(calls, [('/v1/chat/completions', True, 'synthetic-model')])

    @contextmanager
    def _public_fixture(self):
        with tempfile.TemporaryDirectory(prefix='mjobs-ai-admission-') as directory, \
                _model_endpoint() as (model_url, calls):
            root = Path(directory)
            store = Store('sqlite:///' + (root / 'admission.db').as_posix())
            try:
                store.migrate()
                # Reuse only the HTTP fixture/admission methods of the Linux oracle.
                case = PrimaryProductAIRealRQTests()
                case.base = 'http://testserver'
                case.owned_operations = set()
                with patch.dict(os.environ, {'MJOBS_LOCAL_AI_BASE_URL': model_url,
                        'MJOBS_LOCAL_AI_API_KEY': SYNTHETIC_KEY}), TestClient(
                        create_app(store, 'http://127.0.0.1:1', root, operation_mode='PERSONAL')) as client:
                    case.client = client
                    case._seed_public_inputs()
                    yield case, store, calls
            finally:
                store.engine.dispose()

    def test_public_synthetic_inputs_admit_one_candidate_and_cancel_without_provider_call(self):
        with self._public_fixture() as (case, store, calls):
            identifier = case._queue_ai('portable-synthetic-admission')['operation_id']
            self.assertEqual(case._queue_ai('portable-synthetic-admission')['operation_id'], identifier)
            with store.engine.connect() as connection:
                batch = connection.execute(select(m.ai_evaluation_batches)).mappings().one()
            self.assertEqual(len(batch['snapshot']['candidates']), 1)
            self.assertEqual(batch['snapshot']['candidates'][0]['deterministic_score'], 100)
            result = case._call('POST', case.ai_path + '/operations/' + identifier + '/cancel')
            self.assertEqual(result['state'], 'CANCELLED')
            self.assertEqual(calls, [])

    def test_public_profile_revision_changes_version_and_rejects_stale_updates(self):
        with self._public_fixture() as (case, _store, calls):
            before = case._call('GET', case.profile_path)
            case._call('PUT', '/profiles', expected=409,
                       json=dict(case.profile, name='Missing version synthetic edit'))
            saved = case._revise_public_profile()
            after = case._call('GET', case.profile_path)
            self.assertNotEqual(saved['version'], before['version'])
            self.assertEqual(after['version'], saved['version'])
            self.assertEqual(after['name'], 'Alex Synthetic Revision')
            # Revision-owned row IDs/version change; logical facts and approvals do not.
            def fact_values(profile):
                return {row['fact_id']: {key: row[key] for key in (
                    'text', 'kind', 'source', 'verified', 'user_approved', 'confidence')}
                    for row in profile['facts']}
            self.assertEqual(fact_values(after), fact_values(before))
            stale = dict(case.profile, name='Stale synthetic edit', expected_version=before['version'])
            case._call('PUT', '/profiles', expected=409, json=stale)
            self.assertEqual(case._call('GET', case.profile_path)['version'], saved['version'])
            self.assertEqual(calls, [])
