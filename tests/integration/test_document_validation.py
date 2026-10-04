"""Generation and automatic use reject unsupported or corrupted assertions."""
import hashlib
import unittest
from unittest.mock import patch
from pathlib import Path

from docx import Document
from pypdf import PdfReader
from sqlalchemy import select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from services.discovery.pipeline import discover
from services.documents import generate
from services.documents.history import application_documents
from services.applications.mock_adapter import apply_mock
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE


class DocumentValidationTests(unittest.TestCase):
    setUp = fixtures.DocumentProvenanceTests.setUp
    tearDown = fixtures.DocumentProvenanceTests.tearDown

    def test_unicode_readback_and_validation_receipts(self):
        self.store.save_profile(dict(PROFILE, facts=[dict(PROFILE['facts'][0], text='Δοκιμή Python Система résumé')]))
        with fixture_server() as origin:
            discover(self.store, origin, 'fixture')
            app = generate.prepare(self.store, self.store.jobs()[0]['id'], 'fixture', self.root)
        for row in application_documents(self.store, app):
            paths = {ext: self.root/app/file for ext,file in row['files'].items()}
            expected = paths['txt'].read_text(encoding='utf-8').split()
            self.assertEqual(' '.join(p.text for p in Document(paths['docx']).paragraphs).split(), expected)
            self.assertEqual(' '.join(p.extract_text() for p in PdfReader(paths['pdf']).pages).split(), expected)
        with self.store.engine.connect() as c:
            receipts = c.execute(select(m.audit).where(m.audit.c.entity_id==app,m.audit.c.action=='DOCUMENTS_VALIDATED')).mappings().all()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['payload']['validator'], 'exact-approved-facts-v3')
        self.assertEqual(len(receipts[0]['payload']['documents']), 2)

    def test_unsupported_claim_or_glyph_preserves_prior_documents(self):
        with fixture_server() as origin:
            discover(self.store, origin, 'fixture'); job=self.store.jobs()[0]
            app=generate.prepare(self.store, job['id'], 'fixture', self.root)
            before=application_documents(self.store,app); old_app=self.store.application(app)
            files={str(p):p.read_bytes() for p in (self.root/app).rglob('*') if p.is_file()}
            self.store.save_profile(dict(PROFILE, facts=[dict(PROFILE['facts'][0],text='Python systems')]))
            with patch('services.documents.generate.compose_paragraphs',return_value={'resume':['Alex Fixture','alex@example.test','Invented certification'], 'cover-letter':['Alex Fixture']}):
                with self.assertRaises(generate.PreparationError) as error:
                    generate.prepare(self.store,job['id'],'fixture',self.root)
            self.assertEqual(error.exception.code,'UNSUPPORTED_CLAIM')
            self.store.save_profile(dict(PROFILE, facts=[dict(PROFILE['facts'][0],text='中文 systems')]))
            with self.assertRaises(generate.PreparationError) as error:
                generate.prepare(self.store,job['id'],'fixture',self.root)
            self.assertEqual(error.exception.code,'DOCUMENT_UNSUPPORTED_GLYPH')
            self.assertEqual(application_documents(self.store,app),before)
            self.assertEqual(self.store.application(app),old_app)
            self.assertEqual(files,{str(p):p.read_bytes() for p in (self.root/app).rglob('*') if p.is_file()})

    def test_legacy_hash_valid_unsupported_resume_never_reaches_browser(self):
        with fixture_server() as origin:
            discover(self.store,origin,'fixture')
            app=generate.prepare(self.store,self.store.jobs()[0]['id'],'fixture',self.root)
            row=next(r for r in application_documents(self.store,app) if r['kind']=='resume')
            path=self.root/app/row['files']['docx']
            doc=Document(path);doc.add_paragraph('Invented certification');doc.save(path)
            with self.store.engine.begin() as c:
                c.execute(update(m.documents).where(m.documents.c.id==row['id']).values(sha256={**row['sha256'],'docx':hashlib.sha256(path.read_bytes()).hexdigest()}))
            with patch('services.applications.mock_adapter.sync_playwright') as browser:
                with self.assertRaises(generate.PreparationError) as error:
                    apply_mock(self.store,app,origin,self.root)
                browser.assert_not_called()
            self.assertEqual(error.exception.code,'UNSUPPORTED_CLAIM')
            self.assertEqual(self.store.application(app)['state'],'REVIEW_REQUIRED')
            generate.prepare(self.store,self.store.application(app)['job_id'],'fixture',self.root)
            self.assertEqual(self.store.application(app)['state'],'PREPARED')
            self.assertEqual(self.store.application(app)['document_version'],2)
