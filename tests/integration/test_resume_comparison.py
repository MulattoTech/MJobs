"""Historical comparisons use recorded bytes/facts, never live profile text."""
import unittest
from sqlalchemy import delete,update
from fastapi.testclient import TestClient
from apps.api.main import create_app
from packages.persistence import models as m
from fixtures.mock_server import fixture_server
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE


class ResumeComparisonTests(unittest.TestCase):
    setUp=fixtures.DocumentProvenanceTests.setUp
    tearDown=fixtures.DocumentProvenanceTests.tearDown

    def pair(self,changed=False):
        facts=[PROFILE['facts'][1],dict(PROFILE['facts'][0],id='summary',kind='summary',text='Systems engineer'),
            dict(PROFILE['facts'][0],id='duty',kind='responsibility',text='Maintained backups 2020-2024'),PROFILE['facts'][0],
            dict(PROFILE['facts'][0],id='omit',kind='certification',text='Example certificate')]
        self.store.save_profile(dict(PROFILE,facts=facts))
        with fixture_server() as origin:discover(self.store,origin,'fixture')
        job=self.store.jobs()[0]
        with self.store.engine.begin() as c:c.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(skills=['Python']))
        app=prepare(self.store,job['id'],'fixture',self.root)
        if changed:
            revised=[dict(row,text='Revised '+row['text']) if row['id'] in ('summary','duty') else row for row in facts if row['id']!='omit']
            revised.append(dict(PROFILE['facts'][0],id='new',kind='project',text='Approved project'))
            self.store.save_profile(dict(PROFILE,facts=revised))
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True)
        rows=application_documents(self.store,app)
        base=next(r for r in rows if r['kind']=='resume' and r['version']==1)
        target=next(r for r in rows if r['kind']=='resume' and r['version']==2)
        return app,base,target

    def test_comparison_uses_immutable_inputs_after_live_profile_changes(self):
        app,base,target=self.pair()
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            url=f"/applications/{app}/document-comparison?base_document_id={base['id']}&tailored_document_id={target['id']}"
            response=client.get(url);self.assertEqual(response.status_code,200,response.text)
            result=response.json()
            self.assertEqual(result['summary_changes'],[]);self.assertEqual(result['bullet_changes'],[])
            self.assertEqual(result['omitted'],[]);self.assertEqual(result['added'],[])
            self.assertEqual({r['fact_id'] for r in result['reordered']},{'python','sql'})
            self.assertEqual(result['emphasized_skills'][0]['job_skills'],['Python'])
            self.assertEqual(result['base']['text'],(self.root/app/base['files']['txt']).read_text(encoding='utf-8'))
            self.store.save_profile(dict(PROFILE,name='Changed live identity',facts=[dict(PROFILE['facts'][0],text='Changed live text')]))
            self.assertEqual(client.get(url).json(),result)
            self.assertNotIn('Changed live',str(result))

    def test_explicit_revision_changes_are_classified_without_semantic_inference(self):
        app,base,target=self.pair(changed=True)
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            result=client.get(f'/applications/{app}/document-comparison',params={'base_document_id':base['id'],'tailored_document_id':target['id']}).json()
        self.assertEqual(result['summary_changes'][0]['after']['text'],'Revised Systems engineer')
        self.assertEqual(result['bullet_changes'][0]['before']['text'],'Maintained backups 2020-2024')
        self.assertEqual([r['fact_id'] for r in result['omitted']],['omit'])
        self.assertEqual([r['fact_id'] for r in result['added']],['new'])

    def test_unavailable_or_wrong_history_is_not_reported_as_unchanged(self):
        app,base,target=self.pair()
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            url=f'/applications/{app}/document-comparison'
            params={'base_document_id':base['id'],'tailored_document_id':target['id']}
            self.assertEqual(client.get(url,params={**params,'base_document_id':target['id']}).status_code,409)
            other_job=next(j for j in self.store.jobs() if j['id']!=self.store.application(app)['job_id'])
            other=prepare(self.store,other_job['id'],'fixture',self.root)
            foreign=next(r for r in application_documents(self.store,other) if r['kind']=='resume')
            self.assertEqual(client.get(url,params={**params,'base_document_id':foreign['id']}).status_code,409)
            path=self.root/app/base['files']['txt'];original=path.read_bytes();path.write_bytes(b'Changed')
            self.assertEqual(client.get(url,params=params).status_code,409)
            path.write_bytes(original)
            (self.root/'outside.txt').write_text('PRIVATE TEST MARKER',encoding='utf-8')
            with self.store.engine.begin() as c:
                c.execute(update(m.documents).where(m.documents.c.id==base['id']).values(files={**base['files'],'txt':'../outside.txt'}))
            response=client.get(url,params=params)
            self.assertEqual(response.status_code,409)
            self.assertNotIn('PRIVATE TEST MARKER',response.text)
            with self.store.engine.begin() as c:
                c.execute(update(m.documents).where(m.documents.c.id==base['id']).values(files=base['files']))
            with self.store.engine.begin() as c:
                c.execute(delete(m.facts).where(m.facts.c.profile_id=='fixture',m.facts.c.version==base['profile_version'],m.facts.c.fact_id=='python'))
            response=client.get(url,params=params)
            self.assertEqual(response.status_code,409)
            self.assertIn('unavailable',response.json()['detail'].lower())
