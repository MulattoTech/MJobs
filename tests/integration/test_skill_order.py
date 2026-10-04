"""Explicit tailoring survives queue delivery without mutating historical facts."""
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from services.discovery.pipeline import discover
from services.documents.generate import prepare,PreparationError
from services.documents.history import application_documents
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class SkillOrderIntegrationTests(unittest.TestCase):
    setUp=fixtures.DocumentProvenanceTests.setUp
    tearDown=fixtures.DocumentProvenanceTests.tearDown

    def test_explicit_queue_option_versions_preserves_and_labels_skill_order(self):
        facts=[PROFILE['facts'][1],dict(PROFILE['facts'][0],id='role',kind='experience',text='Example Engineer 2020-2024'),
            dict(PROFILE['facts'][0],id='duty',kind='responsibility',text='Maintained services.'),PROFILE['facts'][0],
            dict(PROFILE['facts'][0],id='pending',text='AWS',user_approved=False),
            dict(PROFILE['facts'][0],id='unverified',text='PowerShell',verified=False),
            dict(PROFILE['facts'][0],id='uncertain',text='Linux',confidence=70)]
        self.store.save_profile(dict(PROFILE,facts=facts))
        with fixture_server() as origin,TestClient(create_app(self.store,origin,self.root,operation_mode='FIXTURE')) as client:
            discover(self.store,origin,'fixture');job=self.store.jobs()[0]
            with self.store.engine.begin() as c:c.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(skills=['Python']))
            app=prepare(self.store,job['id'],'fixture',self.root)
            original=application_documents(self.store,app)
            old_files={str(p):p.read_bytes() for p in (self.root/app).rglob('*') if p.is_file()}
            request={'profile_id':'fixture','job_id':job['id'],'tailor_resume':True}
            self.assertEqual(client.post('/applications',json={**request,'tailor_resume':'true'}).status_code,422)
            response=client.post('/applications',json=request)
            self.assertEqual(response.status_code,202,response.text)
            with patch.dict('os.environ',MJOBS_DATABASE_URL=self.store.url,MJOBS_MOCK_ORIGIN=origin,
                            MJOBS_OUTPUT_DIR=str(self.root),MJOBS_OPERATION_MODE='FIXTURE'):
                self.assertEqual(execute_operation(response.json()['operation_id'])['status'],'COMPLETED')
            rows=application_documents(self.store,app)
            resume=next(r for r in rows if r['version']==2 and r['kind']=='resume')
            self.assertEqual(resume['fact_ids'],['python','role','duty','sql'])
            self.assertEqual(resume['generation_context']['job_skills'],['Python'])
            self.assertEqual(resume['generation_context']['original_fact_ids'],['sql','role','duty','python'])
            self.assertEqual(resume['generation_model'],'deterministic-skill-order-v2')
            self.assertEqual([r for r in rows if r['version']==1],original)
            for fmt in ('txt','docx','pdf'):
                self.assertEqual(client.get(f"/documents/{resume['id']}/{fmt}").status_code,200)
            text=client.get(f"/documents/{resume['id']}/txt").text
            self.assertEqual(text.splitlines(),['Alex Fixture','alex@example.test','Skills','Python','Experience','Example Engineer 2020-2024','Maintained services.','Skills','SQL'])
            generated=client.get('/profiles/fixture/document-library').json()['generated']
            self.assertEqual(next(r for r in generated if r['id']==resume['id'])['document_kind'],'TAILORED_RESUME')
            prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True)
            self.assertEqual(self.store.application(app)['document_version'],2)
            with self.store.engine.begin() as c:c.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(skills=['SQL']))
            prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True)
            self.assertEqual(self.store.application(app)['document_version'],3)
            self.assertEqual(next(r for r in application_documents(self.store,app) if r['id']==resume['id']),resume)
            prepare(self.store,job['id'],'fixture',self.root)
            self.assertEqual(self.store.application(app)['document_version'],4)
            for path,data in old_files.items():self.assertEqual(__import__('pathlib').Path(path).read_bytes(),data)
            with self.store.engine.begin() as c:c.execute(update(m.applications).where(m.applications.c.id==app).values(state='CONFIRMED'))
            with self.assertRaises(PreparationError) as error:prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True)
            self.assertEqual(error.exception.code,'DOCUMENT_HISTORY_LOCKED')

    def test_migration_refuses_loss_of_target_context(self):
        import os
        from alembic import command
        from alembic.config import Config
        with fixture_server() as origin:
            discover(self.store,origin,'fixture')
            app=prepare(self.store,self.store.jobs()[0]['id'],'fixture',self.root,tailor_resume=True)
        before=application_documents(self.store,app)
        config=Config('alembic.ini');config.set_main_option('sqlalchemy.url',self.store.url.replace('%','%%'))
        with patch.dict(os.environ,{'MJOBS_DATABASE_URL':self.store.url}):
            with self.assertRaisesRegex(RuntimeError,'Refusing to discard document skill-order context'):
                command.downgrade(config,'be3105f82a74')
        self.assertEqual(application_documents(self.store,app),before)
