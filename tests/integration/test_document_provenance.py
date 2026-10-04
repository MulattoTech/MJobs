"""Generated artifacts retain factual inputs, timestamps and identical outputs."""
from datetime import datetime
import hashlib
import unittest
import tempfile
from pathlib import Path
from io import BytesIO
from unittest.mock import patch

from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfReader

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration import test_user_read_models as read_models
from tests.integration.test_spine import PROFILE
from packages.persistence.store import Store


class DocumentProvenanceTests(unittest.TestCase):
    def setUp(self):
        from alembic import command
        from alembic.config import Config
        import os
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        url = f"sqlite:///{(self.root/'test.db').as_posix()}"
        config = Config('alembic.ini')
        config.set_main_option('sqlalchemy.url', url.replace('%','%%'))
        with patch.dict(os.environ, {'MJOBS_DATABASE_URL':url}):
            command.upgrade(config, 'head')
        self.store = Store(url)
        self.store.bind_workspace_mode('FIXTURE')
        self.store.save_profile(PROFILE)

    tearDown = read_models.UserReadModelTests.tearDown

    def test_three_formats_and_historical_provenance_survive_profile_changes(self):
        profile = dict(PROFILE, facts=[PROFILE['facts'][0], dict(PROFILE['facts'][1], user_approved=False)])
        version = self.store.save_profile(profile)
        with fixture_server() as origin, TestClient(create_app(self.store, origin, self.root, operation_mode='FIXTURE')) as client:
            discover(self.store, origin, 'fixture')
            job = self.store.jobs()[0]
            with patch('services.documents.generate.now', return_value='2026-10-04T12:00:00+00:00'):
                app_id = prepare(self.store, job['id'], 'fixture', self.root)
            first = client.get(f'/applications/{app_id}/documents').json()['documents']
            before = {}
            for row in first:
                self.assertEqual(row['generation_model'], 'deterministic-fact-copy-v2' if row['kind']=='resume' else 'deterministic-fact-copy-v1')
                self.assertEqual(row['created_at'], '2026-10-04T12:00:00+00:00')
                datetime.fromisoformat(row['created_at'])
                self.assertEqual(row['job_id'], job['id'])
                self.assertEqual(row['profile_version'], version)
                self.assertEqual(row['fact_ids'], ['python'])
                self.assertEqual([f['text'] for f in row['facts_used']], ['Python'])
                self.assertEqual(row['facts_used'][0]['source'], 'fictional-test-profile')
                self.assertEqual(row['missing_fact_ids'], [])
                self.assertEqual(set(row['files']), {'pdf','docx','txt'})
                outputs = {}
                for fmt in row['files']:
                    response = client.get(f"/documents/{row['id']}/{fmt}")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(hashlib.sha256(response.content).hexdigest(), row['sha256'][fmt])
                    outputs[fmt] = response.content
                    before[(row['id'],fmt)] = response.content
                txt = outputs['txt'].decode('utf-8').strip()
                docx = '\n'.join(p.text for p in Document(BytesIO(outputs['docx'])).paragraphs)
                pdf = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(outputs['pdf'])).pages).strip()
                self.assertEqual(txt, docx)
                self.assertEqual(txt.split(), pdf.split())
                self.assertNotIn('SQL', txt)
            changed = dict(PROFILE, facts=[dict(PROFILE['facts'][0], text='Python systems'), PROFILE['facts'][1]])
            self.store.save_profile(changed)
            with patch('services.documents.generate.now', return_value='2026-10-04T13:00:00+00:00'):
                prepare(self.store, job['id'], 'fixture', self.root)
            rows = client.get(f'/applications/{app_id}/documents').json()['documents']
            self.assertEqual(len(rows), 4)
            self.assertEqual([r for r in rows if r['version']==1], first)
            self.assertEqual({r['created_at'] for r in rows}, {'2026-10-04T12:00:00+00:00','2026-10-04T13:00:00+00:00'})
            for (identifier,fmt), data in before.items():
                self.assertEqual(client.get(f'/documents/{identifier}/{fmt}').content, data)
            for row in rows:
                if row['version']==2:
                    self.assertEqual([f['text'] for f in row['facts_used']], ['Python systems','SQL'])

    def test_migration_preserves_legacy_bytes_and_refuses_new_provenance_loss(self):
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import text, inspect
        import os
        with fixture_server() as origin:
            discover(self.store, origin, 'fixture')
            app_id = prepare(self.store, self.store.jobs()[0]['id'], 'fixture', self.root)
        files = {str(p):p.read_bytes() for p in (self.root/app_id).rglob('*') if p.is_file()}
        config = Config('alembic.ini')
        config.set_main_option('sqlalchemy.url', str(self.store.engine.url).replace('%','%%'))
        with patch.dict(os.environ, {'MJOBS_DATABASE_URL':str(self.store.engine.url)}):
            with self.assertRaisesRegex(RuntimeError, 'Refusing to discard document generation provenance'):
                command.downgrade(config, 'e9024a1b7c30')
            # Simulate genuine pre-feature rows by removing only this fixture's
            # new metadata, then downgrade to the actual predecessor schema.
            with self.store.engine.begin() as c:
                c.execute(text('UPDATE documents SET created_at=NULL, generation_model=NULL'))
            command.downgrade(config, 'e9024a1b7c30')
            self.assertNotIn('created_at', {c['name'] for c in inspect(self.store.engine).get_columns('documents')})
            command.upgrade(config, 'head')
            with self.store.engine.connect() as c:
                rows = c.execute(text('SELECT created_at,generation_model FROM documents')).all()
            self.assertEqual(rows, [(None,None),(None,None)])
            self.assertEqual(files, {str(p):p.read_bytes() for p in (self.root/app_id).rglob('*') if p.is_file()})
            command.downgrade(config, 'e9024a1b7c30')
            command.upgrade(config, 'head')
