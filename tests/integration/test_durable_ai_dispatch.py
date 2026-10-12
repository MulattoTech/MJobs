"""Opt-in disposable Redis proof that advisory evaluation uses the real outbox."""
import os
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from redis import Redis
from rq import Queue
from rq.job import Job

from tests.integration import test_durable_ai_evaluation as contract
from workers.dispatch import dispatch_pending

REDIS_URL = os.environ.get('MJOBS_AI_REDIS_TEST_URL')


@unittest.skipUnless(REDIS_URL, 'Set MJOBS_AI_REDIS_TEST_URL to disposable loopback Redis database 15')
class DurableAIDispatchTests(unittest.TestCase):
    def test_actual_redis_outbox_carries_only_identity_and_completes_one_saved_run(self):
        parsed = urlsplit(REDIS_URL)
        if (parsed.scheme != 'redis' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                or parsed.path != '/15' or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise RuntimeError('Disposable loopback Redis database 15 is required')
        redis = Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        self.addCleanup(redis.close)
        queue = Queue('mjobs-ai', connection=redis)
        if queue.count:
            raise RuntimeError('Refusing to consume an occupied AI test queue')
        case = contract.DurableAIEvaluationTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        request_id = 'redis-ai-' + uuid4().hex
        response = case.client.post('/profiles/fixture/ai-cost-plan/evaluate/queued', json={'request_id': request_id})
        self.assertEqual(response.status_code, 202, response.text)
        operation_id = response.json()['operation_id']
        job = None
        try:
            self.assertEqual(dispatch_pending(case.store, REDIS_URL), {'status': 'DISPATCHED', 'count': 1})
            self.assertEqual(dispatch_pending(case.store, REDIS_URL)['count'], 0)
            job = Job.fetch(operation_id, connection=redis)
            self.assertEqual(job.origin, 'mjobs-ai')
            self.assertEqual(job.args, (operation_id,))
            self.assertEqual(job.kwargs, {})
            self.assertEqual(case.calls, [])
            with patch.dict(os.environ, {'MJOBS_DATABASE_URL': case.url, 'MJOBS_OPERATION_MODE': 'PERSONAL'}), \
                    patch('services.ai.durable_evaluation.AITaskRouter', case.router):
                self.assertEqual(job.perform()['status'], 'COMPLETED')
            self.assertEqual(len(case.calls), 1)
            self.assertEqual(len(case.store.ai_evaluation_history('fixture')), 1)
            self.assertEqual(dispatch_pending(case.store, REDIS_URL)['count'], 0)
        finally:
            queue.remove(operation_id)
            if job is not None:
                job.delete()
