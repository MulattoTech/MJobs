"""Typed collections reuse immutable bytes without granting import authority."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest

from tests.integration import test_resume_upload as upload_tests
from tests.integration.test_spine import PROFILE

KINDS = ('BASE_RESUME','MASTER_RESUME','TAILORED_RESUME','COVER_LETTER',
         'APPLICATION_ANSWER_EXPORT','SUPPORTING_DOCUMENT','CERTIFICATION')


class DocumentLibraryTests(unittest.TestCase):
    setUp = upload_tests.ResumeUploadTests.setUp
    tearDown = upload_tests.ResumeUploadTests.tearDown

    def upload(self, kind, data=b'Synthetic original', profile='fixture', **headers):
        return self.client.post(f'/profiles/{profile}/document-library', content=data,
            headers={'X-Filename':'original.txt','Content-Type':'text/plain','X-Document-Kind':kind,**headers})

    def test_all_seven_kinds_share_bytes_but_keep_distinct_collections_and_versions(self):
        before=self.store.profile('fixture')
        rows=[]
        for kind in KINDS:
            response=self.upload(kind, **{'X-Document-Label':kind+' example'})
            self.assertEqual(response.status_code,201,response.text)
            row=response.json();rows.append(row)
            self.assertEqual(row['document_kind'],kind)
            self.assertEqual(row,self.upload(kind).json())
            self.assertEqual(self.client.get('/profiles/fixture/document-library/'+row['revision_id']).content,b'Synthetic original')
        self.assertEqual(len({row['id'] for row in rows}),1)
        self.assertEqual(len({row['series_id'] for row in rows}),7)
        self.assertEqual(len({row['revision_id'] for row in rows}),7)
        self.assertEqual(self.store.count('uploaded_resumes'),1)
        self.assertEqual(len(list((self.root/'uploads').iterdir())),1)
        listing=self.client.get('/profiles/fixture/document-library').json()
        self.assertEqual(len(listing['uploaded']),7)
        self.assertEqual(listing['generated'],[])
        self.assertEqual(len(self.client.get('/profiles/fixture/resumes').json()['resumes']),1)
        for row in rows:
            revised=self.upload(row['document_kind'],b'Synthetic revision 2',**{'X-Document-Series':row['series_id']}).json()
            self.assertEqual(revised['version'],2)
            self.assertEqual(revised['label'],row['label'])
        self.assertEqual(self.store.profile('fixture'),before)

    def test_kind_profile_import_and_integrity_boundaries(self):
        cert=self.upload('CERTIFICATION').json()
        self.store.save_profile(dict(PROFILE,id='other',email='other@example.invalid'))
        for endpoint in [f"/profiles/other/document-library/{cert['revision_id']}",
                         f"/profiles/fixture/resumes/{cert['id']}"]:
            self.assertEqual(self.client.get(endpoint).status_code,404)
        self.assertEqual(self.client.post(f"/profiles/fixture/resumes/{cert['id']}/extract").status_code,404)
        self.assertEqual(self.upload('BASE_RESUME',**{'X-Document-Series':cert['series_id']}).status_code,422)
        self.assertEqual(self.upload('CERTIFICATION',profile='other',**{'X-Document-Series':cert['series_id']}).status_code,404)
        self.assertEqual(self.upload('INVENTED').status_code,422)
        self.assertEqual(self.client.get('/profiles/other/document-library').json(),{'uploaded':[],'generated':[]})
        self.assertEqual(self.client.get('/profiles/fixture/document-library',headers={'Sec-Fetch-Site':'cross-site'}).status_code,403)
        (self.root/'uploads'/(cert['id']+'.txt')).write_bytes(b'corrupted')
        self.assertEqual(self.client.get('/profiles/fixture/document-library/'+cert['revision_id']).status_code,409)

    def test_concurrent_different_kinds_share_blob_without_mixing_version_counters(self):
        from services.documents.uploads import save_resume
        barrier=Barrier(2)
        def upload(kind):
            barrier.wait(timeout=10)
            return save_resume(self.store,self.root,'fixture','shared.txt','text/plain',b'Shared synthetic',document_kind=kind)
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows=list(pool.map(upload,('BASE_RESUME','CERTIFICATION')))
        self.assertEqual(len({row['id'] for row in rows}),1)
        self.assertEqual({row['version'] for row in rows},{1})
        self.assertEqual({row['document_kind'] for row in rows},{'BASE_RESUME','CERTIFICATION'})
        self.assertEqual(self.store.count('resume_revisions'),2)

    def test_generated_library_is_profile_scoped_and_does_not_claim_tailoring(self):
        import httpx
        from services.discovery.sources.collect import configure_source, collect_source
        from services.documents.generate import prepare
        from tests.integration.test_greenhouse import RAW
        source=configure_source(self.store,'example')
        transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'jobs':[RAW]})
            if request.url.path.endswith('/jobs') else httpx.Response(200,json={'name':'Example'}))
        collect_source(self.store,source,'fixture',transport=transport)
        self.store.save_profile(dict(PROFILE,id='other',email='other@example.invalid'))
        job=self.store.jobs()[0]
        apps={profile:prepare(self.store,job['id'],profile,self.root) for profile in ('fixture','other')}
        for profile in apps:
            data=self.client.get(f'/profiles/{profile}/document-library').json()
            self.assertEqual(len(data['generated']),2)
            self.assertEqual({r['application_id'] for r in data['generated']},{apps[profile]})
            self.assertEqual({r['document_kind'] for r in data['generated']},{'GENERATED_RESUME','COVER_LETTER'})
            self.assertTrue(all(r['generation_model'] and r['created_at'] for r in data['generated']))


class DocumentCollectionMigrationTests(unittest.TestCase):
    from tests.integration import test_document_provenance as migration_fixtures
    setUp = migration_fixtures.DocumentProvenanceTests.setUp
    tearDown = migration_fixtures.DocumentProvenanceTests.tearDown

    def test_base_ids_survive_upgrade_and_new_types_prevent_lossy_downgrade(self):
        import os
        from unittest.mock import patch
        from alembic import command
        from alembic.config import Config
        from services.documents.uploads import save_resume,list_resumes,read_resume
        from sqlalchemy import inspect
        original=save_resume(self.store,self.root,'fixture','legacy.txt','text/plain',b'Legacy original')
        config=Config('alembic.ini')
        url=str(self.store.engine.url)
        config.set_main_option('sqlalchemy.url',url.replace('%','%%'))
        with patch.dict(os.environ,{'MJOBS_DATABASE_URL':url}):
            command.downgrade(config,'af2049e17b63')
            self.assertNotIn('document_kind',{c['name'] for c in inspect(self.store.engine).get_columns('resume_revisions')})
            command.upgrade(config,'head')
            self.assertEqual(list_resumes(self.store,'fixture'),[original])
            self.assertEqual(read_resume(self.store,self.root,'fixture',original['id'])[1],b'Legacy original')
            save_resume(self.store,self.root,'fixture','certificate.txt','text/plain',b'Legacy original',document_kind='CERTIFICATION')
            with self.assertRaisesRegex(RuntimeError,'Refusing to discard document collection types'):
                command.downgrade(config,'af2049e17b63')
