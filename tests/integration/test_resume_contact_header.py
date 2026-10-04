"""Contact disclosure is explicit, snapshot-bound and separate from facts."""
import unittest
from docx import Document
from fixtures.mock_server import fixture_server
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from services.documents.comparison import compare_resumes
from services.documents.validation import validate_stored_resume
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE


class ResumeContactHeaderTests(unittest.TestCase):
    setUp=fixtures.DocumentProvenanceTests.setUp
    tearDown=fixtures.DocumentProvenanceTests.tearDown

    def test_long_profile_url_wraps_without_losing_or_changing_characters(self):
        url='https://example.test/'+'long-path-'*18
        self.store.save_profile(dict(PROFILE,contact={'portfolio':url}))
        with fixture_server() as origin:discover(self.store,origin,'fixture')
        app=prepare(self.store,self.store.jobs()[0]['id'],'fixture',self.root,include_resume_contact=True)
        row=next(r for r in application_documents(self.store,app) if r['kind']=='resume')
        paths={ext:self.root/app/path for ext,path in row['files'].items()}
        self.assertIn(url,paths['txt'].read_text())
        validate_stored_resume(paths,self.store.profile('fixture'),row['fact_ids'],row['template'],row['generation_model'])

    def test_explicit_contact_header_preserves_originals_and_fact_emphasis(self):
        contact={'phones':['+1 555 0100'],'city':'Tulsa','state':'OK','country':'US',
            'address_line1':'Private street','postal_code':'99999','linkedin':'https://example.test/alex',
            'github':'https://github.com/example','portfolio':'https://example.test/work?a=1&b=2'}
        self.store.save_profile(dict(PROFILE,contact=contact))
        with fixture_server() as origin:discover(self.store,origin,'fixture')
        job=self.store.jobs()[0];before=self.store.profile('fixture')
        app=prepare(self.store,job['id'],'fixture',self.root)
        base=next(r for r in application_documents(self.store,app) if r['kind']=='resume')
        original={ext:(self.root/app/path).read_bytes() for ext,path in base['files'].items()}
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,include_resume_contact=True)
        row=next(r for r in application_documents(self.store,app) if r['kind']=='resume' and r['version']==2)
        paths={ext:self.root/app/path for ext,path in row['files'].items()}
        text=paths['txt'].read_text(encoding='utf-8')
        for value in ['+1 555 0100','Tulsa','OK','US',contact['linkedin'],contact['github'],contact['portfolio']]:self.assertIn(value,text)
        self.assertNotIn('Private street',text);self.assertNotIn('99999',text)
        self.assertNotIn('+1 555 0100',original['txt'].decode())
        validate_stored_resume(paths,before,row['fact_ids'],row['template'],row['generation_model'])
        doc=Document(paths['docx'])
        self.assertFalse(any(r.bold for p in doc.paragraphs if '+1 555 0100' in p.text for r in p.runs))
        comparison=compare_resumes(self.store,self.root,app,base['id'],row['id'])
        self.assertTrue(comparison['contact_changed']);self.assertEqual(comparison['omitted'],[])
        self.assertEqual(self.store.profile('fixture'),before)
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,include_resume_contact=True)
        self.assertEqual(self.store.application(app)['document_version'],2)
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,include_resume_contact=False)
        last=next(r for r in application_documents(self.store,app) if r['kind']=='resume' and r['version']==3)
        self.assertNotIn('+1 555 0100',(self.root/app/last['files']['txt']).read_text())
        for ext,path in base['files'].items():self.assertEqual((self.root/app/path).read_bytes(),original[ext])

    def test_contact_option_reaches_worker_with_strict_boolean_and_profile_binding(self):
        from fastapi.testclient import TestClient
        from unittest.mock import patch
        from apps.api.main import create_app
        from workers.tasks import execute_operation
        self.store.save_profile(dict(PROFILE,contact={'phones':['555 0100']}))
        with fixture_server() as origin:discover(self.store,origin,'fixture')
        job=self.store.jobs()[0]
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            body={'profile_id':'fixture','job_id':job['id'],'include_resume_contact':True}
            self.assertEqual(client.post('/applications',json={**body,'include_resume_contact':'true'}).status_code,422)
            response=client.post('/applications',json=body);self.assertEqual(response.status_code,202,response.text)
            with patch.dict('os.environ',MJOBS_DATABASE_URL=self.store.url,MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',MJOBS_OUTPUT_DIR=str(self.root),MJOBS_OPERATION_MODE='FIXTURE'):
                self.assertEqual(execute_operation(response.json()['operation_id'])['status'],'COMPLETED')
            row=next(r for r in application_documents(self.store,response.json()['application_id']) if r['kind']=='resume')
            self.assertIn('Phone: 555 0100',row['generation_context']['contact_header'])
            later=client.post('/applications',json={**body,'request_id':'later'}).json()
            self.store.save_profile(dict(PROFILE,contact={'phones':['555 0200']}))
            with patch.dict('os.environ',MJOBS_DATABASE_URL=self.store.url,MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',MJOBS_OUTPUT_DIR=str(self.root),MJOBS_OPERATION_MODE='FIXTURE'):
                self.assertEqual(execute_operation(later['operation_id'])['error_category'],'DOCUMENT_PROFILE_CHANGED')
            self.assertEqual(self.store.application(response.json()['application_id'])['document_version'],1)

    def test_selected_contact_header_uses_immutable_snapshot_and_detects_tampering(self):
        from sqlalchemy import update
        from packages.persistence import models as m
        from services.documents.comparison import ComparisonUnavailable
        self.store.save_profile(dict(PROFILE,contact={'phones':['555 0100']}))
        with fixture_server() as origin:discover(self.store,origin,'fixture')
        job=self.store.jobs()[0];app=prepare(self.store,job['id'],'fixture',self.root,include_resume_contact=True)
        base=next(r for r in application_documents(self.store,app) if r['kind']=='resume')
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,tailor_fact_ids=['python'],include_resume_contact=True)
        row=next(r for r in application_documents(self.store,app) if r['kind']=='resume' and r['version']==2)
        self.store.save_profile(dict(PROFILE,contact={'phones':['555 0200']}))
        compared=compare_resumes(self.store,self.root,app,base['id'],row['id'])
        self.assertFalse(compared['contact_changed']);self.assertIn('555 0100',compared['tailored']['text']);self.assertNotIn('555 0200',compared['tailored']['text'])
        with self.store.engine.begin() as c:c.execute(update(m.documents).where(m.documents.c.id==row['id']).values(generation_context={**row['generation_context'],'contact_header':['invented']}))
        with self.assertRaises(ComparisonUnavailable):compare_resumes(self.store,self.root,app,base['id'],row['id'])
