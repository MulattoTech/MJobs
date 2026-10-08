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
            url=f"/profiles/fixture/applications/{app}/document-comparison?base_document_id={base['id']}&tailored_document_id={target['id']}"
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
            self.store.save_profile(dict(PROFILE,id='other'))
            foreign_url=url.replace('/profiles/fixture/', '/profiles/other/')
            self.assertEqual(client.get(foreign_url).status_code,404)
            self.assertEqual(client.get(url.replace('/profiles/fixture', '')).status_code,404)

    def test_explicit_revision_changes_are_classified_without_semantic_inference(self):
        app,base,target=self.pair(changed=True)
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            result=client.get(f'/profiles/fixture/applications/{app}/document-comparison',params={'base_document_id':base['id'],'tailored_document_id':target['id']}).json()
        self.assertEqual(result['summary_changes'][0]['after']['text'],'Revised Systems engineer')
        self.assertEqual(result['bullet_changes'][0]['before']['text'],'Maintained backups 2020-2024')
        self.assertEqual([r['fact_id'] for r in result['omitted']],['omit'])
        self.assertEqual([r['fact_id'] for r in result['added']],['new'])

    def test_user_reviewed_rephrase_is_source_bound_and_comparison_survives_profile_changes(self):
        facts=[dict(PROFILE['facts'][0],id='duty',kind='responsibility',
                    text='Built Python services for 4 clinics in 2020-2022.'),PROFILE['facts'][1]]
        original_profile=dict(PROFILE,facts=facts)
        self.store.save_profile(original_profile)
        with fixture_server() as origin:discover(self.store,origin,'fixture')
        job=self.store.jobs()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(skills=['Python']))
        app=prepare(self.store,job['id'],'fixture',self.root)
        base=next(row for row in application_documents(self.store,app) if row['kind']=='resume')
        wording='Delivered Python services for 4 clinics in 2020-2022.'
        rephrases=[{'fact_id':'duty','text':wording,'reviewed':True}]
        result=prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,
            tailor_fact_ids=['duty'],tailor_fact_rewrites=rephrases,include_resume_contact=True)
        self.assertEqual(result,app)
        rows=application_documents(self.store,app)
        target=next(row for row in rows if row['kind']=='resume' and row['version']==2)
        cover=next(row for row in rows if row['kind']=='cover-letter' and row['version']==2)
        self.assertEqual(target['generation_context']['fact_rephrases'][0]['fact_id'],'duty')
        self.assertEqual(target['generation_context']['claim_validation']['strategy'],
            'USER_REVIEWED_FACTUAL_PARAPHRASE_WITH_EXACT_NUMERIC_PRESERVATION')
        self.assertIn(wording,(self.root/app/target['files']['txt']).read_text(encoding='utf-8'))
        self.assertNotIn(wording,(self.root/app/cover['files']['txt']).read_text(encoding='utf-8'))
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            url=f'/profiles/fixture/applications/{app}/document-comparison'
            params={'base_document_id':base['id'],'tailored_document_id':target['id']}
            response=client.get(url,params=params)
            self.assertEqual(response.status_code,200,response.text)
            comparison=response.json()
            self.assertEqual(comparison['bullet_changes'][0]['before']['text'],facts[0]['text'])
            self.assertEqual(comparison['bullet_changes'][0]['after']['text'],wording)
            self.store.save_profile(dict(original_profile,name='Changed live name',facts=[
                dict(facts[0],text='Changed live career fact'),facts[1]]))
            self.assertEqual(client.get(url,params=params).json(),comparison)
            invalid=client.post('/applications',json={'profile_id':'fixture','job_id':job['id'],
                'tailor_resume':True,'tailor_fact_ids':['duty'],'expected_profile_version':self.store.profile('fixture')['version'],
                'tailor_fact_rewrites':[{'fact_id':'duty','text':'Delivered Python services for 40 clinics in 2020-2022.','reviewed':True}]})
            self.assertEqual(invalid.status_code,409)
            self.assertIn('preserve every source date and number',invalid.json()['detail'])
        self.assertEqual(self.store.profile('fixture')['facts'][0]['text'],'Changed live career fact')

    def test_user_reviewed_summary_adjustment_is_historical_and_cover_letter_is_unchanged(self):
        summary=dict(PROFILE['facts'][0],id='summary',kind='summary',
            text='Systems engineer with 12 years of Linux experience, 2012-2024.')
        skill=dict(PROFILE['facts'][0],id='linux',kind='skill',text='Linux')
        original_profile=dict(PROFILE,facts=[summary,skill])
        self.store.save_profile(original_profile)
        with fixture_server() as origin:
            discover(self.store,origin,'fixture')
        job=self.store.jobs()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(skills=['Linux']))
        app=prepare(self.store,job['id'],'fixture',self.root)
        base=next(row for row in application_documents(self.store,app) if row['kind']=='resume')
        wording='Linux systems engineer with 12 years of experience, 2012-2024.'
        rewrites=[{'fact_id':'summary','text':wording,'reviewed':True}]
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,
            tailor_fact_ids=['summary'],tailor_fact_rewrites=rewrites,
            profile_version=self.store.profile('fixture')['version'])
        rows=application_documents(self.store,app)
        target=next(row for row in rows if row['kind']=='resume' and row['version']==2)
        cover=next(row for row in rows if row['kind']=='cover-letter' and row['version']==2)
        self.assertEqual(target['generation_context']['fact_rephrases'][0]['fact_kind'],'summary')
        target_text=(self.root/app/target['files']['txt']).read_text(encoding='utf-8')
        cover_text=(self.root/app/cover['files']['txt']).read_text(encoding='utf-8')
        self.assertIn(wording,target_text)
        self.assertNotIn(summary['text'],target_text)
        self.assertNotIn(wording,cover_text)
        self.assertEqual(self.store.profile('fixture')['facts'][0]['text'],summary['text'])
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            url=f'/profiles/fixture/applications/{app}/document-comparison'
            params={'base_document_id':base['id'],'tailored_document_id':target['id']}
            response=client.get(url,params=params)
            self.assertEqual(response.status_code,200,response.text)
            comparison=response.json()
            self.assertEqual(comparison['summary_changes'][0]['before']['text'],summary['text'])
            self.assertEqual(comparison['summary_changes'][0]['after']['text'],wording)
            self.assertEqual(comparison['bullet_changes'],[])
            changed=dict(summary,text='A different current profile summary')
            self.store.save_profile(dict(original_profile,facts=[changed,skill]))
            self.assertEqual(client.get(url,params=params).json(),comparison)

    def test_user_reviewed_project_and_accomplishment_rewrites_survive_reload_and_comparison(self):
        project=dict(PROFILE['facts'][0],id='project',kind='project',
            text='Built a local-first recovery harness for 3 environments in 2021.')
        achievement=dict(PROFILE['facts'][0],id='achievement',kind='accomplishment',
            text='Reduced recovery time by 25% in 2022.')
        technology=dict(PROFILE['facts'][0],id='technology',kind='technology_experience',
            text='Administered PostgreSQL databases for 6 teams from 2020-2024.')
        skill=dict(PROFILE['facts'][0],id='linux',kind='skill',text='Linux')
        original_profile=dict(PROFILE,facts=[project,achievement,technology,skill])
        self.store.save_profile(original_profile)
        profile_version=self.store.profile('fixture')['version']
        with fixture_server() as origin:
            discover(self.store,origin,'fixture')
        job=self.store.jobs()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(skills=['Linux']))
        app=prepare(self.store,job['id'],'fixture',self.root)
        base=next(row for row in application_documents(self.store,app) if row['kind']=='resume')
        rewrites=[
            {'fact_id':'project','text':'Created a recovery harness for 3 environments in 2021.','reviewed':True},
            {'fact_id':'achievement','text':'Cut recovery time by 25% in 2022.','reviewed':True},
            {'fact_id':'technology','text':'Managed PostgreSQL databases for 6 teams from 2020-2024.','reviewed':True},
        ]
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,
            tailor_fact_ids=['project','achievement','technology'],tailor_fact_rewrites=rewrites,
            profile_version=self.store.profile('fixture')['version'])
        rows=application_documents(self.store,app)
        target=next(row for row in rows if row['kind']=='resume' and row['version']==2)
        cover=next(row for row in rows if row['kind']=='cover-letter' and row['version']==2)
        self.assertEqual([item['fact_kind'] for item in target['generation_context']['fact_rephrases']],
            ['project','accomplishment','technology_experience'])
        target_text=(self.root/app/target['files']['txt']).read_text(encoding='utf-8')
        cover_text=(self.root/app/cover['files']['txt']).read_text(encoding='utf-8')
        for item,source in zip(rewrites,[project,achievement,technology]):
            self.assertIn(item['text'],target_text)
            self.assertNotIn(source['text'],target_text)
            self.assertNotIn(item['text'],cover_text)
        self.assertEqual([row['text'] for row in self.store.profile('fixture')['facts']],
            [row['text'] for row in original_profile['facts']])
        self.assertEqual(self.store.profile('fixture')['version'],profile_version)
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            url=f'/profiles/fixture/applications/{app}/document-comparison'
            params={'base_document_id':base['id'],'tailored_document_id':target['id']}
            response=client.get(url,params=params)
            self.assertEqual(response.status_code,200,response.text)
            comparison=response.json()
            changes={item['before']['fact_id']:item for item in comparison['bullet_changes']}
            for item,source in zip(rewrites,[project,achievement,technology]):
                self.assertEqual(changes[source['id']]['before']['text'],source['text'])
                self.assertEqual(changes[source['id']]['after']['text'],item['text'])
            current=dict(project,text='Changed current project profile text')
            self.store.save_profile(dict(original_profile,facts=[current,achievement,technology,skill]))
            self.assertEqual(client.get(url,params=params).json(),comparison)

    def test_user_reviewed_experience_rewrite_survives_history_and_comparison(self):
        import hashlib
        experience=dict(PROFILE['facts'][0],id='experience',kind='experience',
            text='Supported 12 clinics with Linux systems from 2020-2024.')
        skill=dict(PROFILE['facts'][0],id='linux',kind='skill',text='Linux')
        original_profile=dict(PROFILE,facts=[experience,skill])
        self.store.save_profile(original_profile)
        profile_version=self.store.profile('fixture')['version']
        with fixture_server() as origin:
            discover(self.store,origin,'fixture')
        job=self.store.jobs()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(skills=['Linux']))
        app=prepare(self.store,job['id'],'fixture',self.root)
        base=next(row for row in application_documents(self.store,app) if row['kind']=='resume')
        wording='Maintained Linux systems for 12 clinics from 2020-2024.'
        rewrites=[{'fact_id':'experience','text':wording,'reviewed':True}]
        prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True,
            tailor_fact_ids=['experience'],tailor_fact_rewrites=rewrites,
            profile_version=profile_version)
        rows=application_documents(self.store,app)
        target=next(row for row in rows if row['kind']=='resume' and row['version']==2)
        cover=next(row for row in rows if row['kind']=='cover-letter' and row['version']==2)
        context=target['generation_context']['fact_rephrases'][0]
        self.assertEqual(context,{
            'fact_id':'experience','fact_kind':'experience',
            'source_text_sha256':hashlib.sha256(experience['text'].encode()).hexdigest(),
            'text':wording,'reviewed':True,
            'review_policy':'USER_CONFIRMED_FACTUAL_PARAPHRASE',
        })
        target_text=(self.root/app/target['files']['txt']).read_text(encoding='utf-8')
        cover_text=(self.root/app/cover['files']['txt']).read_text(encoding='utf-8')
        self.assertIn(wording,target_text)
        self.assertNotIn(experience['text'],target_text)
        self.assertNotIn(wording,cover_text)
        self.assertEqual(self.store.profile('fixture')['facts'][0]['text'],experience['text'])
        self.assertEqual(self.store.profile('fixture')['version'],profile_version)
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            url=f'/profiles/fixture/applications/{app}/document-comparison'
            params={'base_document_id':base['id'],'tailored_document_id':target['id']}
            comparison_response=client.get(url,params=params)
            self.assertEqual(comparison_response.status_code,200,comparison_response.text)
            comparison=comparison_response.json()
            change=next(item for item in comparison['bullet_changes'] if item['before']['fact_id']=='experience')
            self.assertEqual(change['before']['text'],experience['text'])
            self.assertEqual(change['after']['text'],wording)
            self.store.save_profile(dict(original_profile,facts=[
                dict(experience,text='Supported 12 clinics with Linux platforms from 2020-2024.'),skill]))
            self.assertEqual(client.get(url,params=params).json(),comparison)
            stale=client.post('/applications',json={
                'profile_id':'fixture','job_id':job['id'],'tailor_resume':True,
                'tailor_fact_ids':['experience'],'expected_profile_version':profile_version,
                'tailor_fact_rewrites':rewrites,
            })
            self.assertEqual(stale.status_code,409)
            unreviewed=client.post('/applications',json={
                'profile_id':'fixture','job_id':job['id'],'tailor_resume':True,
                'tailor_fact_ids':['experience'],
                'expected_profile_version':self.store.profile('fixture')['version'],
                'tailor_fact_rewrites':[dict(rewrites[0],reviewed=False)],
            })
            self.assertEqual(unreviewed.status_code,409)
            unselected=client.post('/applications',json={
                'profile_id':'fixture','job_id':job['id'],'tailor_resume':True,
                'tailor_fact_ids':['linux'],
                'expected_profile_version':self.store.profile('fixture')['version'],
                'tailor_fact_rewrites':rewrites,
            })
            self.assertEqual(unselected.status_code,409)
        self.assertEqual(self.store.profile('fixture')['facts'][0]['text'],
            'Supported 12 clinics with Linux platforms from 2020-2024.')

    def test_unavailable_or_wrong_history_is_not_reported_as_unchanged(self):
        app,base,target=self.pair()
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            url=f'/profiles/fixture/applications/{app}/document-comparison'
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
