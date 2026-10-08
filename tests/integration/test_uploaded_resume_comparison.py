"""Uploaded originals remain unreviewed and immutable during comparison."""
from io import BytesIO
import asyncio
import inspect
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import _uploaded_comparison_entity_id, create_app
from fixtures.mock_server import fixture_server
from fixtures.resume_scans import scanned_resume
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from services.documents.render import render_pdf
from services.documents.uploads import save_resume,read_resume
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE

SOURCE='Alex Fixture\nalex@example.test\nSummary\nOriginal summary\nSkills\nSQL; Python\nResponsibilities\nOriginal responsibility\nOmitted statement\nOmitted statement\n'


class UploadedResumeComparisonTests(unittest.TestCase):
    setUp=fixtures.DocumentProvenanceTests.setUp
    tearDown=fixtures.DocumentProvenanceTests.tearDown

    def target(self):
        self.original_profile=dict(PROFILE,facts=[PROFILE['facts'][1],PROFILE['facts'][0],
            dict(PROFILE['facts'][0],id='summary',kind='summary',text='Revised summary'),
            dict(PROFILE['facts'][0],id='duty',kind='responsibility',text='Revised responsibility')])
        self.store.save_profile(self.original_profile)
        with fixture_server() as origin:discover(self.store,origin,'fixture')
        job=self.store.jobs()[0]
        with self.store.engine.begin() as c:c.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(skills=['Python']))
        app=prepare(self.store,job['id'],'fixture',self.root,tailor_resume=True)
        target=next(row for row in application_documents(self.store,app) if row['kind']=='resume')
        return app,target

    def upload(self,fmt='txt',kind='BASE_RESUME',profile='fixture'):
        if fmt=='txt':data=SOURCE.encode()
        elif fmt=='docx':
            doc=Document()
            for line in SOURCE.splitlines():doc.add_paragraph(line)
            stream=BytesIO();doc.save(stream);data=stream.getvalue()
        else:
            path=self.root/'synthetic.pdf';render_pdf(path,SOURCE.splitlines());data=path.read_bytes()
        media={'txt':'text/plain','pdf':'application/pdf','docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
        return save_resume(self.store,self.root,profile,'original.'+fmt,media[fmt],data,document_kind=kind)

    def test_native_formats_compare_exact_revision_without_profile_mutation(self):
        app,target=self.target();before=self.store.profile('fixture')
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            for fmt in ('txt','docx','pdf'):
                with self.subTest(format=fmt):
                    upload=self.upload(fmt)
                    url=f'/profiles/fixture/applications/{app}/uploaded-resume-comparison'
                    params={'base_revision_id':upload['revision_id'],'tailored_document_id':target['id']}
                    response=client.get(url,params=params);self.assertEqual(response.status_code,200,response.text)
                    result=response.json()
                    self.assertEqual(result['base']['sha256'],upload['sha256'])
                    self.assertEqual(result['base']['source_kind'],'UPLOADED')
                    self.assertIn('Original summary',result['summary_changes'][0]['before']['text'])
                    self.assertIn('Revised responsibility',result['bullet_changes'][0]['after']['text'])
                    self.assertEqual(len([row for row in result['omitted'] if row['text']=='Omitted statement']),2)
                    self.assertTrue(result['reordered'])
                    self.assertEqual(result['emphasized_skills'][0]['job_skills'],['Python'])
                    self.assertEqual(self.store.profile('fixture'),before)
                    self.store.save_profile(dict(PROFILE,name='Changed live identity'))
                    self.assertEqual(client.get(url,params=params).json(),result)
                    self.store.save_profile(self.original_profile)

    def test_uploaded_comparison_reads_durable_skill_order_target_without_profile_mutation(self):
        source=PROFILE['facts'][0]
        duty=dict(source,id='python-duty',kind='responsibility',text='Built Python APIs for internal teams.')
        self.store.save_profile(dict(PROFILE,facts=[PROFILE['facts'][1],PROFILE['facts'][0],duty],
                                     contact={'phones':['555 0100'],'city':'Tulsa'}))
        with fixture_server() as origin:discover(self.store,origin,'fixture')
        job=self.store.jobs()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id==job['id']).values(skills=['Python']))
        app=prepare(self.store,job['id'],'fixture',self.root)
        original=b'Alex Fixture\nalex@example.test\nSkills\nSQL; Python\nResponsibilities\nMaintained servers\n'
        upload=save_resume(self.store,self.root,'fixture','original.txt','text/plain',original)
        before=self.store.profile('fixture')
        self.store.save_profile(dict(PROFILE,id='other'))
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            queued=client.post('/applications',json={
                'profile_id':'fixture','job_id':job['id'],'tailor_resume':True,
            })
            self.assertEqual(queued.status_code,202,queued.text)
            operation=queued.json()
            self.assertEqual(operation['application_id'],app)
        from workers.tasks import execute_operation
        with patch.dict('os.environ',MJOBS_DATABASE_URL=str(self.store.engine.url),
                        MJOBS_MOCK_ORIGIN='http://127.0.0.1:1',MJOBS_OUTPUT_DIR=str(self.root),
                        MJOBS_OPERATION_MODE='FIXTURE'):
            self.assertEqual(execute_operation(operation['operation_id'])['status'],'COMPLETED')
        target=next(row for row in application_documents(self.store,app)
                    if row['kind']=='resume' and row['version']==2)
        self.assertEqual((target['template'],target['generation_model']),(
            'approved-target-relevant-bullets-summary-skill-responsibility-and-bullet-emphasis-v5',
            'deterministic-target-relevant-bullets-summary-skill-responsibility-order-v4'))
        database_url=str(self.store.engine.url)
        self.store.engine.dispose()
        reloaded_store=Store(database_url)
        url=f'/profiles/fixture/applications/{app}/uploaded-resume-comparison'
        params={'base_revision_id':upload['revision_id'],'tailored_document_id':target['id']}
        try:
            with TestClient(create_app(reloaded_store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
                response=client.get(url,params=params)
                self.assertEqual(response.status_code,200,response.text)
                result=response.json()
                self.assertEqual(result['base']['id'],upload['revision_id'])
                self.assertEqual(result['base']['source_kind'],'UPLOADED')
                self.assertEqual(result['base']['sha256'],upload['sha256'])
                self.assertEqual(result['base']['text'],original.decode())
                self.assertFalse(result['section_availability']['summary_changes'])
                self.assertTrue(result['section_availability']['bullet_changes'])
                self.assertEqual(result['summary_changes'],[])
                self.assertIn('Maintained servers',{row['text'] for row in result['omitted']})
                foreign_url=url.replace('/profiles/fixture/','/profiles/other/')
                self.assertEqual(client.get(foreign_url,params=params).status_code,404)
            self.assertEqual(reloaded_store.profile('fixture'),before)
        finally:
            reloaded_store.engine.dispose()

    def test_wrong_membership_and_tampered_original_fail_closed(self):
        app,target=self.target()
        self.store.save_profile(dict(PROFILE,id='other'))
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            url=f'/profiles/fixture/applications/{app}/uploaded-resume-comparison'
            for upload in (self.upload(kind='CERTIFICATION'),self.upload(profile='other')):
                response=client.get(url,params={'base_revision_id':upload['revision_id'],'tailored_document_id':target['id']})
                self.assertEqual(response.status_code,409)
            upload=self.upload()
            record,data=read_resume(self.store,self.root,'fixture',upload['id'])
            (self.root/'uploads'/f"{record['id']}.txt").write_bytes(b'Changed private bytes')
            response=client.get(url,params={'base_revision_id':upload['revision_id'],'tailored_document_id':target['id']})
            self.assertEqual(response.status_code,409)
            self.assertNotIn('Changed private bytes',response.text)

    def test_disconnected_uploaded_comparison_cancels_extraction_without_profile_mutation(self):
        app,target=self.target();upload=self.upload();before=self.store.profile('fixture')
        application=self.store.application(app)
        endpoint=f"/profiles/{{profile_id}}/applications/{{application_id}}/uploaded-resume-comparison"
        api=create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')
        route=next(route for route in api.routes if getattr(route,'path',None)==endpoint)
        self.assertTrue(inspect.iscoroutinefunction(route.endpoint))
        request=type('RequestProbe',(),{})()
        request.is_disconnected=AsyncMock(side_effect=[False,True])
        observed=threading.Event()

        def wait_for_cancel(*args):
            cancellation_event=args[-1]
            deadline=time.monotonic()+2
            while not cancellation_event.is_set() and time.monotonic()<deadline:
                time.sleep(0.005)
            if cancellation_event.is_set():
                observed.set()
                from services.career_profile.extraction import ResumeExtractionCancelled
                raise ResumeExtractionCancelled()
            raise AssertionError('client disconnect was not propagated to uploaded-resume comparison')

        with patch('services.documents.uploaded_comparison.compare_uploaded_resume',side_effect=wait_for_cancel):
            response=asyncio.run(route.endpoint(
                'fixture',app,upload['revision_id'],target['id'],request))
        self.assertEqual(response.status_code,499)
        self.assertTrue(observed.is_set())
        self.assertEqual(self.store.profile('fixture'),before)
        self.assertEqual(self.store.application(app),application)

    def test_second_api_instance_durably_cancels_uploaded_comparison(self):
        app,target=self.target();upload=self.upload();before=self.store.profile('fixture')
        application=self.store.application(app)
        self.store.save_profile(dict(PROFILE,id='other'))
        database_url=str(self.store.engine.url)
        second_store=Store(database_url)
        first_app=create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')
        second_app=create_app(second_store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')
        url=f'/profiles/fixture/applications/{app}/uploaded-resume-comparison'
        cancel_url=url+'/cancel'
        params={'base_revision_id':upload['revision_id'],'tailored_document_id':target['id']}
        entered=threading.Event();cancel_events=[];responses=[];worker_errors=[];thread=None

        def wait_for_durable_cancel(*args):
            cancel_events.append(args[-1])
            entered.set()
            deadline=time.monotonic()+3
            while not args[-1].is_set() and time.monotonic()<deadline:
                time.sleep(0.005)
            if args[-1].is_set():
                from services.career_profile.extraction import ResumeExtractionCancelled
                raise ResumeExtractionCancelled()
            raise AssertionError('second API instance did not durably cancel comparison')

        try:
            with patch('services.documents.uploaded_comparison.compare_uploaded_resume',
                       side_effect=wait_for_durable_cancel):
                with TestClient(first_app) as first, TestClient(second_app) as second:
                    def compare():
                        try:
                            responses.append(first.get(url,params=params))
                        except BaseException as error:
                            worker_errors.append(error)

                    thread=threading.Thread(target=compare,daemon=True)
                    thread.start()
                    self.assertTrue(entered.wait(2),'comparison extraction did not start')
                    other_profile_url=cancel_url.replace('/profiles/fixture/', '/profiles/other/')
                    self.assertEqual(second.post(other_profile_url,params=params).status_code,404)
                    wrong_document={**params,'tailored_document_id':'another-document'}
                    self.assertFalse(second.post(cancel_url,params=wrong_document).json()['cancelled'])
                    self.assertFalse(cancel_events[0].is_set())
                    cancel_response=second.post(cancel_url,params=params)
                    self.assertEqual(cancel_response.status_code,200,cancel_response.text)
                    cancel_ack=cancel_response.json()['cancelled']
                    if not cancel_ack and cancel_events:
                        cancel_events[0].set()
                    thread.join(timeout=4)
                    self.assertFalse(thread.is_alive(),'comparison did not stop after cancellation')
                    self.assertEqual(worker_errors,[])
                    self.assertTrue(cancel_ack,'second API instance did not report the durable cancellation')
                    self.assertEqual(responses[0].status_code,499,responses[0].text)
                    with second_store.engine.connect() as connection:
                        operations=list(connection.execute(select(m.operations).where(
                            m.operations.c.kind=='UPLOADED_RESUME_COMPARE',
                            m.operations.c.entity_id==_uploaded_comparison_entity_id(
                                'fixture',app,upload['revision_id'],target['id']))).mappings())
                    self.assertEqual(len(operations),1)
                    self.assertEqual(operations[0]['state'],'CANCELLED')
                    self.assertTrue(operations[0]['cancel_requested'])
                    self.assertEqual(operations[0]['error'],'CANCEL_REQUESTED')
                    self.assertEqual(operations[0]['entity_id'],_uploaded_comparison_entity_id(
                        'fixture',app,upload['revision_id'],target['id']))
                    self.assertNotIn(upload['revision_id'],operations[0]['entity_id'])
                    self.assertEqual(operations[0]['params'],{})
                    self.assertEqual(self.store.profile('fixture'),before)
                    self.assertEqual(self.store.application(app),application)
        finally:
            if cancel_events and thread is not None and thread.is_alive():
                cancel_events[0].set()
                thread.join(timeout=4)
            second_store.engine.dispose()

    def test_combined_approved_skill_list_and_bullet_markers_do_not_create_false_omissions(self):
        app,target=self.target();upload=self.upload()
        self.store.save_profile(dict(PROFILE,facts=[dict(PROFILE['facts'][0],text='Python, SQL'),
            dict(PROFILE['facts'][0],id='duty',kind='responsibility',text='- Original responsibility')]))
        prepare(self.store,self.store.application(app)['job_id'],'fixture',self.root,tailor_resume=True)
        target=next(row for row in application_documents(self.store,app) if row['kind']=='resume' and row['version']==2)
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            response=client.get(f'/profiles/fixture/applications/{app}/uploaded-resume-comparison',params={'base_revision_id':upload['revision_id'],'tailored_document_id':target['id']})
            self.assertEqual(response.status_code,200,response.text)
            result=response.json()
            self.assertFalse({'Python','SQL','Original responsibility'}&{row['text'] for row in result['omitted']})
            self.assertEqual([row['text'] for row in result['emphasized_skills']],['Python, SQL'])

    def test_scanned_original_is_unavailable_or_explicitly_uncertain_without_fact_changes(self):
        app,target=self.target();before=self.store.profile('fixture')
        upload=save_resume(self.store,self.root,'fixture','scan.pdf','application/pdf',scanned_resume())
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            url=f'/profiles/fixture/applications/{app}/uploaded-resume-comparison'
            params={'base_revision_id':upload['revision_id'],'tailored_document_id':target['id']}
            with patch('services.career_profile.ocr.ocr_pdf_pages',return_value=({},['Local OCR unavailable'])):
                response=client.get(url,params=params)
                self.assertEqual(response.status_code,409)
                self.assertIn('unavailable',response.json()['detail'])
            with patch('services.career_profile.ocr.ocr_pdf_pages',return_value=({1:SOURCE},[])):
                response=client.get(url,params=params)
                self.assertEqual(response.status_code,200,response.text)
                extraction=response.json()['base']['extraction']
                self.assertTrue(extraction['used_ocr'])
                self.assertTrue(any('OCR text may contain mistakes' in warning for warning in extraction['warnings']))
            self.assertEqual(self.store.profile('fixture'),before)

    def test_foreign_application_and_missing_upload_fail_closed(self):
        app,target=self.target();upload=self.upload()
        other_job=next(job for job in self.store.jobs() if job['id']!=self.store.application(app)['job_id'])
        other=prepare(self.store,other_job['id'],'fixture',self.root,tailor_resume=True)
        foreign=next(row for row in application_documents(self.store,other) if row['kind']=='resume')
        with TestClient(create_app(self.store,'http://127.0.0.1:1',self.root,operation_mode='FIXTURE')) as client:
            url=f'/profiles/fixture/applications/{app}/uploaded-resume-comparison'
            self.assertEqual(client.get(url,params={'base_revision_id':upload['revision_id'],'tailored_document_id':foreign['id']}).status_code,409)
            self.store.save_profile(dict(PROFILE,id='other'))
            self.assertEqual(client.get(url.replace('/profiles/fixture/', '/profiles/other/'),params={'base_revision_id':upload['revision_id'],'tailored_document_id':target['id']}).status_code,404)
            self.assertEqual(client.get(url.replace('/profiles/fixture', ''),params={'base_revision_id':upload['revision_id'],'tailored_document_id':target['id']}).status_code,404)
            (self.root/'uploads'/f"{upload['id']}.txt").unlink()
            self.assertEqual(client.get(url,params={'base_revision_id':upload['revision_id'],'tailored_document_id':target['id']}).status_code,409)
