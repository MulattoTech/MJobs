"""Scoped cancellation and explicit retry must preserve durable fixture history."""
import os
import json
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from sqlalchemy import select, update
from packages.persistence import models as m
from tests.integration import test_controlled_application_dry_run as contract
from services.applications import controlled_dry_run as service
from workers.operations import recover_expired


class ControlledDryRunRecoveryTests(unittest.TestCase):
    _new_store = contract.ControlledApplicationDryRunTests._new_store
    setUp = contract.ControlledApplicationDryRunTests.setUp
    tearDown = contract.ControlledApplicationDryRunTests.tearDown
    path = contract.ControlledApplicationDryRunTests.path
    execute = contract.ControlledApplicationDryRunTests.execute
    review_and_queue = contract.ControlledApplicationDryRunTests.review_and_queue

    def test_cancel_reload_reviewed_retry_and_receipts_keep_distinct_identity(self):
        preview, body, original = self.review_and_queue()
        path = self.path()+'/runs/'+body['request_id']
        cancelled = self.client.post(path+'/cancel')
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()['state'], 'CANCELLED')
        self.assertTrue(cancelled.json()['cancel_requested'])
        self.assertEqual(self.execute(original['operation_id'])['status'], 'ALREADY_CLAIMED_OR_CAPACITY')
        retry = {'request_id':'reviewed-retry-0001','fingerprint':preview['fingerprint'],'acknowledged':True}
        for acknowledgement in (False, 1, 'true'):
            self.assertEqual(self.client.post(path+'/retry',json={**retry,'acknowledged':acknowledgement}).status_code,422)
        queued = self.client.post(path+'/retry',json=retry)
        self.assertEqual(queued.status_code,202,queued.text)
        self.assertNotEqual(queued.json()['operation_id'],original['operation_id'])
        self.assertEqual(self.client.post(path+'/retry',json=retry).json()['operation_id'],queued.json()['operation_id'])
        self.store.engine.dispose()
        self.assertEqual(self.execute(queued.json()['operation_id'])['status'],'COMPLETED')
        saved = self.client.get(self.path()+'/runs/'+retry['request_id']).json()
        self.assertEqual(saved['retry_of'],body['request_id'])
        self.assertFalse(saved['receipt']['submission_enabled'])
        completed_cancel=self.client.post(self.path()+'/runs/'+retry['request_id']+'/cancel').json()
        self.assertEqual(completed_cancel['state'],'COMPLETED')
        self.assertFalse(completed_cancel['cancel_requested'])
        self.assertEqual(completed_cancel['receipt'],saved['receipt'])
        self.assertEqual(self.client.get(path).json()['state'],'CANCELLED')
        self.assertIsNone(self.client.get(path).json()['receipt'])
        self.assertEqual(self.client.post(path+'/retry',json=retry).json()['operation_id'],queued.json()['operation_id'])
        self.assertEqual(self.store.count('application_attempts'),0)

    def test_concurrent_distinct_admission_allows_only_one_active_run(self):
        preview, body, _ = self.review_and_queue()
        self.client.post(self.path()+'/runs/'+body['request_id']+'/cancel')
        def retry(number):
            return self.client.post(self.path()+'/runs/'+body['request_id']+'/retry',json={
                'request_id':'concurrent-retry-'+str(number),'fingerprint':preview['fingerprint'],'acknowledged':True})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(retry,[1,2]))
        self.assertEqual(sorted(r.status_code for r in responses),[202,409])
        failure=next(r for r in responses if r.status_code==409)
        self.assertEqual(failure.json()['detail']['code'],'CONTROLLED_DRY_RUN_ACTIVE')

    def test_scope_stale_retry_and_ordinary_duplicate_are_fenced(self):
        preview, body, _ = self.review_and_queue()
        path=self.path()+'/runs/'+body['request_id']
        self.assertEqual(self.client.post(path.replace('/fixture/','/other/')+'/cancel').status_code,404)
        self.assertEqual(self.client.post(self.path()+'/runs',json={**body,'request_id':'second-active'}).status_code,409)
        self.client.post(path+'/cancel')
        self.assertEqual(self.client.post(self.path()+'/runs',json={**body,'request_id':'unreviewed-retry'}).status_code,409)
        profile=self.store.profile('fixture'); profile['name']='Changed after cancellation'
        self.store.save_profile(profile,expected_version=profile['version'])
        retry={'request_id':'stale-retry','fingerprint':preview['fingerprint'],'acknowledged':True}
        self.assertEqual(self.client.post(path+'/retry',json=retry).status_code,409)

    def test_running_browser_cancellation_fences_receipt(self):
        _, body, queued=self.review_and_queue()
        original=service.attach_verified_files
        reached=[]
        def cancel_then_attach(*args,**kwargs):
            reached.append(True)
            result=self.client.post(self.path()+'/runs/'+body['request_id']+'/cancel')
            self.assertEqual(result.status_code,200,result.text)
            self.assertTrue(result.json()['cancel_requested'])
            return original(*args,**kwargs)
        with patch.object(service,'attach_verified_files',cancel_then_attach):
            result=self.execute(queued['operation_id'])
        self.assertTrue(reached,'Real Chromium never reached upload')
        self.assertEqual(result['status'],'CANCELLED',result)
        self.assertIsNone(self.client.get(self.path()+'/runs/'+body['request_id']).json()['receipt'])
        self.assertEqual(self.store.count('application_attempts'),0)

    def test_crashed_worker_saved_receipt_is_reconciled_without_browser_replay(self):
        _, body, queued=self.review_and_queue()
        environment={**os.environ,'MJOBS_OPERATION_MODE':'FIXTURE','MJOBS_DATABASE_URL':self.database_url,
            'MJOBS_REDIS_URL':'redis://127.0.0.1:1/0','MJOBS_MOCK_ORIGIN':'http://127.0.0.1:1',
            'MJOBS_OUTPUT_DIR':str(self.root)}
        crash="""import os,sys
from workers import tasks
original=tasks.execute_controlled_dry_run
def crash_after_receipt(*args,**kwargs):
    original(*args,**kwargs)
    os._exit(23)
tasks.execute_controlled_dry_run=crash_after_receipt
tasks.execute_operation(sys.argv[1])
"""
        result=subprocess.run([sys.executable,'-c',crash,queued['operation_id']],env=environment,capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,23,result.stderr)
        path=self.path()+'/runs/'+body['request_id']
        saved=self.client.get(path).json()
        self.assertEqual(saved['state'],'RUNNING')
        self.assertIsNotNone(saved['receipt'])
        cancelled=self.client.post(path+'/cancel').json()
        self.assertFalse(cancelled['cancel_requested'],'A saved verification must not be relabeled as cancelled')
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id==queued['operation_id']).values(lease_expires_at='2000-01-01T00:00:00+00:00'))
        recover_expired(self.store)
        recovered = self.client.get(path).json()
        self.assertEqual(recovered['state'], 'COMPLETED')
        self.assertEqual(recovered['receipt'], saved['receipt'])
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.operations.c.attempt_count).where(
                m.operations.c.id == queued['operation_id'])).scalar_one(), 1)
        resume="""import json,sys
from unittest.mock import patch
from workers.tasks import execute_operation
with patch('services.applications.controlled_dry_run.run_browser',side_effect=AssertionError('must not replay browser')):
    print(json.dumps(execute_operation(sys.argv[1])))
"""
        result=subprocess.run([sys.executable,'-c',resume,queued['operation_id']],env=environment,capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'],'ALREADY_CLAIMED_OR_CAPACITY')
        self.assertEqual(self.client.get(path).json()['receipt'],saved['receipt'])


@unittest.skipUnless(os.environ.get('MJOBS_TEST_POSTGRES_URL'),'Requires isolated disposable PostgreSQL')
class ControlledDryRunRecoveryPostgresTests(ControlledDryRunRecoveryTests):
    _new_store = contract.ControlledApplicationDryRunPostgresTests._new_store
    _drop_schema = contract.ControlledApplicationDryRunPostgresTests._drop_schema
