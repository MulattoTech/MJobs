"""Transactional, profile-scoped recovery from a sanitized export."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.profile_restore import create_profile_restore_router
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.data_export import export_profile_backup
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.uploads import read_resume, save_resume
from tests.integration.test_spine import PROFILE


class ProfileRestoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mjobs-profile-restore-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'restore.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode('FIXTURE')
        self.source_profile_id = 'source-profile'
        source = copy.deepcopy(PROFILE)
        source.update(id=self.source_profile_id, name='Synthetic Source',
                      email='source@example.invalid')
        self.store.save_profile(source)
        self.resume_bytes = {
            'synthetic.txt': b'Synthetic resume recovery fixture one\n' * 2300,
            'synthetic-second.txt': b'Synthetic resume recovery fixture two\n',
        }
        self.original_resume = save_resume(
            self.store, self.root, self.source_profile_id, 'synthetic.txt',
            'text/plain', self.resume_bytes['synthetic.txt'],
        )
        self.original_second_resume = save_resume(
            self.store, self.root, self.source_profile_id, 'synthetic-second.txt',
            'text/plain', self.resume_bytes['synthetic-second.txt'],
        )
        with fixture_server() as fixture:
            discover(self.store, fixture, self.source_profile_id)
        job = next(row for row in self.store.jobs(self.source_profile_id)
                   if row['requisition'] == 'B')
        self.application_id = prepare(
            self.store, job['id'], self.source_profile_id, self.root,
        )
        with self.store.engine.begin() as connection:
            connection.execute(m.search_profiles.insert().values(
                id='source-search-profile', profile_id=self.source_profile_id,
                name='Synthetic active search', enabled=True,
                criteria={'keywords': ['synthetic'], 'scheduler': {'enabled': True}},
                source_ids=['fixture'], created_at='2026-10-11T00:00:00+00:00',
                updated_at='2026-10-11T00:00:00+00:00',
            ))
        self.backup = export_profile_backup(
            self.store, self.source_profile_id, output_dir=self.root,
        )
        app = FastAPI()
        app.include_router(create_profile_restore_router(self.store, self.root))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_preview_explicit_import_readback_and_request_idempotency(self):
        before = self.store.profile(self.source_profile_id)
        preview_response = self.client.post('/profile-restores/preview', json={
            'backup': self.backup,
        })
        self.assertEqual(preview_response.status_code, 200, preview_response.text)
        preview = preview_response.json()
        self.assertEqual(preview['source_profile'], {
            'name': 'Synthetic Source', 'email': 'source@example.invalid',
        })
        self.assertEqual(preview['counts']['facts'], len(before['facts']))
        self.assertEqual(preview['counts']['uploaded_files'], 2)
        self.assertTrue(preview['omitted_sections'])
        self.assertTrue(any('review' in warning.casefold() for warning in preview['warnings']))

        request = {
            'backup': self.backup,
            'request_id': 'restore-request-synthetic-1',
            'expected_fingerprint': preview['fingerprint'],
            'confirmed': True,
        }
        restored_response = self.client.post('/profile-restores', json=request)
        self.assertEqual(restored_response.status_code, 201, restored_response.text)
        restored = restored_response.json()
        restored_id = restored['profile_id']
        self.assertNotEqual(restored_id, self.source_profile_id)
        self.assertEqual(restored['request_id'], request['request_id'])
        actual = self.store.profile(restored_id)
        self.assertEqual(actual['name'], before['name'])
        self.assertEqual(actual['email'], before['email'])
        self.assertEqual(len(actual['facts']), len(before['facts']))
        self.assertTrue(all(not fact['verified'] and not fact['user_approved']
                            for fact in actual['facts']))
        self.assertTrue(all(not answer['verified'] and not answer['user_approved']
                            for answer in actual['answers']))
        self.assertFalse(actual['preferences']['company_discovery']['company_discovery_enabled'])
        self.assertEqual(actual['preferences']['application_route_preference'], 'USER_CHOICE')
        self.assertTrue(all(policy == {'mode': 'ALWAYS_REQUIRE_REVIEW', 'response': None}
                            for policy in actual['preferences']['sensitive_answer_policies'].values()))
        self.assertEqual(self.store.profile(self.source_profile_id), before)
        self.assertTrue(self.store.jobs(self.source_profile_id))
        self.assertEqual(len(self.store.applications(self.source_profile_id)), 1)

        with self.store.engine.connect() as connection:
            restored_uploads = connection.execute(select(
                m.uploaded_resumes).where(m.uploaded_resumes.c.profile_id == restored_id)
            ).mappings().all()
        self.assertEqual(len(restored_uploads), 2)
        original_ids = {self.original_resume['id'], self.original_second_resume['id']}
        restored_names = set()
        for stored in restored_uploads:
            upload = dict(stored)
            self.assertNotIn(upload['id'], original_ids)
            self.assertEqual(upload['status'], 'STORED_UNREVIEWED')
            self.assertEqual(upload['sha256'], hashlib.sha256(
                self.resume_bytes[upload['filename']]).hexdigest())
            _, restored_bytes = read_resume(self.store, self.root, restored_id, upload['id'])
            self.assertEqual(restored_bytes, self.resume_bytes[upload['filename']])
            restored_names.add(upload['filename'])
        self.assertEqual(restored_names, set(self.resume_bytes))

        with self.store.engine.connect() as connection:
            searches = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.profile_id == restored_id,
            )).mappings().all()
        self.assertEqual(len(searches), 1)
        self.assertFalse(searches[0]['enabled'])
        self.assertEqual(searches[0]['source_ids'], [])
        self.assertNotIn('scheduler', searches[0]['criteria'])
        self.assertEqual(self.store.applications(restored_id), [])
        # Job postings are global records and remain visible to every profile.
        # The restore must not create profile-specific scoring or applications.
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.matches).where(
                m.matches.c.profile_id == restored_id,
            )).all(), [])
        self.assertTrue(self.store.jobs(restored_id))

        with patch('packages.persistence.profile_restore._stage_files',
                   side_effect=AssertionError('durable replay must precede upload staging')):
            replay = self.client.post('/profile-restores', json=request)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), restored)
        self.assertEqual(len(self.store.profiles()), 2)

        changed = copy.deepcopy(self.backup)
        changed['profile']['name'] = 'Changed Synthetic Source'
        for version in changed['profile_versions']:
            if version['version'] == changed['profile']['version']:
                version['contact']['name'] = changed['profile']['name']
        changed_preview = self.client.post('/profile-restores/preview', json={
            'backup': changed,
        })
        self.assertEqual(changed_preview.status_code, 200, changed_preview.text)
        changed_request = dict(request, backup=changed,
                               expected_fingerprint=changed_preview.json()['fingerprint'])
        conflict = self.client.post('/profile-restores', json=changed_request)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(len(self.store.profiles()), 2)

    def test_unconfirmed_and_stale_imports_do_not_create_profiles(self):
        preview = self.client.post('/profile-restores/preview', json={'backup': self.backup})
        self.assertEqual(preview.status_code, 200)
        body = {
            'backup': self.backup,
            'request_id': 'restore-request-not-confirmed',
            'expected_fingerprint': preview.json()['fingerprint'],
            'confirmed': False,
        }
        rejected = self.client.post('/profile-restores', json=body)
        self.assertEqual(rejected.status_code, 422, rejected.text)
        body['confirmed'] = True
        body['expected_fingerprint'] = '0' * 64
        stale = self.client.post('/profile-restores', json=body)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(len(self.store.profiles()), 1)

    def test_upload_file_failure_rolls_back_profile_and_preserves_unowned_files(self):
        preview = self.client.post('/profile-restores/preview', json={'backup': self.backup})
        self.assertEqual(preview.status_code, 200)
        uploads_root = self.root / 'uploads'
        uploads_root.mkdir(exist_ok=True)
        sentinel = uploads_root / 'keep-existing-fixture.txt'
        sentinel.write_bytes(b'leave this file untouched')
        existing_files = set(uploads_root.iterdir())
        request = {
            'backup': self.backup,
            'request_id': 'restore-request-file-failure',
            'expected_fingerprint': preview.json()['fingerprint'],
            'confirmed': True,
        }
        from os import link as real_link
        link_calls = []

        def fail_second_link(source, target):
            link_calls.append((source, target))
            if len(link_calls) == 2:
                raise OSError('fixture link failure')
            return real_link(source, target)

        with patch('packages.persistence.profile_restore.os.link', side_effect=fail_second_link):
            failed = self.client.post('/profile-restores', json=request)
        self.assertEqual(failed.status_code, 500, failed.text)
        self.assertEqual(failed.json()['detail']['code'], 'PROFILE_RESTORE_FILE_WRITE_FAILED')
        self.assertEqual(len(self.store.profiles()), 1)
        self.assertEqual(sentinel.read_bytes(), b'leave this file untouched')
        self.assertEqual(set(uploads_root.iterdir()), existing_files)


if __name__ == '__main__':
    unittest.main()
