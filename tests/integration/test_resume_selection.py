"""Explicit resume subsets remain approved, version-bound and resume-only."""
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import update
from packages.persistence import models as m

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from services.discovery.pipeline import discover
from services.documents.generate import prepare, PreparationError
from services.documents.history import application_documents
from services.documents.comparison import compare_resumes, ComparisonUnavailable
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class ResumeSelectionTests(unittest.TestCase):
    setUp=fixtures.DocumentProvenanceTests.setUp
    tearDown=fixtures.DocumentProvenanceTests.tearDown

    def setup_job(self):
        facts=[*PROFILE['facts'],dict(PROFILE['facts'][0],id='duty',kind='responsibility',text='Build Python systems'),
            dict(PROFILE['facts'][0],id='pending',text='Unapproved claim',user_approved=False),
            dict(PROFILE['facts'][0],id='unverified',verified=False),
            dict(PROFILE['facts'][0],id='uncertain',confidence=70),
            dict(PROFILE['facts'][0],id='unsourced',source='')]
        self.store.save_profile(dict(PROFILE,facts=facts))
        with fixture_server() as origin:discover(self.store,origin,'fixture')
        return self.store.jobs()[0]

    def test_subset_is_resume_only_and_changed_selection_versions_once(self):
        job=self.setup_job();before=self.store.profile('fixture')
        app=prepare(self.store,job['id'],'fixture',self.root)
        base=next(r for r in application_documents(self.store,app) if r['kind']=='resume')
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,tailor_fact_ids=['duty','python'])
        rows=[r for r in application_documents(self.store,app) if r['version']==2]
        resume=next(r for r in rows if r['kind']=='resume');cover=next(r for r in rows if r['kind']=='cover-letter')
        self.assertEqual(resume['fact_ids'],['python','duty'])
        self.assertCountEqual(cover['fact_ids'],['python','sql','duty'])
        self.assertCountEqual(self.store.application(app)['fact_ids'],cover['fact_ids'])
        self.assertEqual(resume['generation_model'],'deterministic-fact-selection-and-bullet-emphasis-v1')
        self.assertEqual(resume['generation_context']['selected_fact_ids'],['python','duty'])
        self.assertEqual(resume['generation_context']['omitted_fact_ids'],['sql'])
        self.assertEqual([r['fact_id'] for r in compare_resumes(self.store,self.root,app,base['id'],resume['id'])['omitted']],['sql'])
        self.assertEqual(self.store.profile('fixture'),before)
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,tailor_fact_ids=['python','duty'])
        self.assertEqual(self.store.application(app)['document_version'],2)
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,tailor_fact_ids=['python'])
        self.assertEqual(self.store.application(app)['document_version'],3)
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True)
        self.assertEqual(self.store.application(app)['document_version'],4)
        self.assertEqual(next(r for r in application_documents(self.store,app) if r['id']==resume['id']),resume)

    def test_invalid_or_ineligible_selection_cannot_reuse_prior_or_create_files(self):
        job=self.setup_job();app=prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True)
        self.store.save_profile(dict(PROFILE,id='other',facts=[dict(PROFILE['facts'][0],id='foreign')]))
        prior=application_documents(self.store,app);files={str(p):p.read_bytes() for p in (self.root/app).rglob('*') if p.is_file()}
        for ids in ([],['python','python'],['missing'],['foreign'],['pending'],['unverified'],['uncertain'],['unsourced'],['python',1],'python'):
            with self.subTest(ids=ids),self.assertRaises(PreparationError) as error:
                prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,tailor_fact_ids=ids)
            self.assertEqual(error.exception.code,'DOCUMENT_SELECTION_INVALID')
        with self.assertRaises(PreparationError):prepare(self.store,job['id'],'fixture',self.root,tailor_fact_ids=['python'])
        self.assertEqual(application_documents(self.store,app),prior)
        self.assertEqual(files,{str(p):p.read_bytes() for p in (self.root/app).rglob('*') if p.is_file()})

    def test_modified_selection_history_is_unavailable_instead_of_invented_omissions(self):
        job=self.setup_job();app=prepare(self.store,job['id'],'fixture',self.root)
        base=next(r for r in application_documents(self.store,app) if r['kind']=='resume')
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,tailor_fact_ids=['python'])
        target=next(r for r in application_documents(self.store,app) if r['kind']=='resume' and r['version']==2)
        for altered in ({'selected_fact_ids':['pending']},{'selection_fingerprint':'bad'},
                        {'available_fact_ids':['python'],'omitted_fact_ids':[]}):
            with self.store.engine.begin() as c:c.execute(update(m.documents).where(m.documents.c.id==target['id']).values(generation_context={**target['generation_context'],**altered}))
            with self.subTest(altered=altered),self.assertRaises(ComparisonUnavailable):
                compare_resumes(self.store,self.root,app,base['id'],target['id'])

    def test_api_selection_binds_observed_profile_and_survives_worker_delivery(self):
        job=self.setup_job();version=self.store.profile('fixture')['version']
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            body={'profile_id':'fixture','job_id':job['id'],'tailor_resume':True,'tailor_fact_ids':['python'],'expected_profile_version':version}
            for changed in ({'tailor_resume':False},{'tailor_fact_ids':[]},{'tailor_fact_ids':[1]},{'expected_profile_version':None}):
                response=client.post('/applications',json={**body,**changed});self.assertEqual(response.status_code,422,response.text)
            response=client.post('/applications',json={**body,'expected_profile_version':'0'*64})
            self.assertEqual(response.status_code,409,response.text)
            response=client.post('/applications',json=body);self.assertEqual(response.status_code,202,response.text)
            operation=response.json()
            with patch.dict('os.environ',MJOBS_DATABASE_URL=self.store.url,MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',MJOBS_OUTPUT_DIR=str(self.root),MJOBS_OPERATION_MODE='FIXTURE'):
                self.assertEqual(execute_operation(operation['operation_id'])['status'],'COMPLETED')
            resume=next(r for r in application_documents(self.store,operation['application_id']) if r['kind']=='resume')
            self.assertEqual(resume['fact_ids'],['python'])
            response=client.post('/applications',json={**body,'tailor_fact_ids':['sql']});self.assertEqual(response.status_code,202)
            self.store.save_profile(dict(PROFILE,name='Changed profile'))
            with patch.dict('os.environ',MJOBS_DATABASE_URL=self.store.url,MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',MJOBS_OUTPUT_DIR=str(self.root),MJOBS_OPERATION_MODE='FIXTURE'):
                result=execute_operation(response.json()['operation_id'])
            self.assertEqual(result['error_category'],'DOCUMENT_PROFILE_CHANGED')
            self.assertEqual(self.store.application(operation['application_id'])['document_version'],1)
