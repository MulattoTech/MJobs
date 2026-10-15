"""Transactional, profile-scoped recovery from a sanitized export."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.profile_restore import create_profile_restore_router
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.data_export import export_profile_backup
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.uploads import read_resume, save_resume
from tests.integration.test_spine import PROFILE


# Keep every persisted top-level backup section accounted for as recovery is
# extended. `partial` rows are surfaced in preview.omitted_sections.
BACKUP_SECTION_ACCOUNTING = {
    'profile': 'recovered', 'profile_versions': 'all versions with remapped IDs',
    'facts': 'all versions imported unverified',
    'settings': 'search profiles and policies recovered; model providers disabled',
    'career_stories': 'recovered pending review',
    'ai_evaluation_runs': 'recovered immutable historical metadata only',
    'ai_job_evaluations': 'recovered remapped inert evaluation output',
    'ai_document_reviews': 'recovered remapped historical review marker',
    'source_candidates': 'recovered with collection disabled and review required',
    'source_candidate_reviews': 'recovered remapped immutable review history',
    'company_discovery_requests': 'converted to inert, profile-owned archive snapshots',
    'company_discovery_history': 'preserved safe history with remapped references and authorization required',
    'jobs': 'recovered remapped profile-associated jobs',
    'job_matches': 'recovered remapped profile matches',
    'applications': 'recovered inert profile applications',
    'application_routes': 'recovered safe routes',
    'application_route_choices': 'recovered remapped choices',
    'documents': 'recovered verified bytes and metadata', 'attempts': 'recovered without execution steps',
    'resume_revisions': 'recovered remapped version and series lineage',
    'uploaded_resumes': 'recovered unreviewed bytes', 'document_files': 'recovered verified bytes',
    'resume_workspace_revisions': 'recovered inert revision',
    'interview_prep_packs': 'recovered inert revision',
}


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
        discovery_profile_version = self.store.profile(self.source_profile_id)['version']
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id='synthetic-discovery-company', name='Synthetic History Employer', domain=''))
            connection.execute(m.company_sources.insert().values(
                id='synthetic-discovery-source', company_id='synthetic-discovery-company',
                provider='rss', tenant='synthetic-history', endpoint='https://history.example/feed',
                discovered_automatically=False, collection_strategy='RSS', confidence=0.9,
                enabled=True))
            connection.execute(m.jobs.insert().values(
                id='synthetic-discovery-job', company_id='synthetic-discovery-company',
                requisition='history-req', title='Synthetic History Job', description='Synthetic only',
                skills=[], location='Remote', remote_type='REMOTE', attributes={}))
            connection.execute(m.routes.insert().values(
                id='synthetic-discovery-route', job_id='synthetic-discovery-job',
                company_id='synthetic-discovery-company', provider='synthetic', tenant='history',
                requisition='history-req', url='https://history.example/apply/1'))
            connection.execute(m.matches.insert().values(
                id='synthetic-discovery-match', job_id='synthetic-discovery-job',
                profile_id=self.source_profile_id, profile_version=discovery_profile_version,
                score=77.5, missing=[], components={'synthetic': True}))
            connection.execute(m.postings.insert().values(
                id='synthetic-discovery-posting', job_id='synthetic-discovery-job', source='synthetic',
                source_job_id='history-req', source_url='https://history.example/feed',
                application_url='https://history.example/jobs/1', raw={},
                fetched_at='2026-10-11T00:00:00+00:00'))
            connection.execute(m.posting_snapshots.insert().values(
                id='synthetic-discovery-snapshot', posting_id='synthetic-discovery-posting', raw={},
                sha256='a' * 64, retrieved_at='2026-10-11T00:00:00+00:00'))
            connection.execute(m.source_quality_observations.insert().values(
                id='synthetic-discovery-quality', company_source_id='synthetic-discovery-source',
                source_endpoint='https://history.example/feed', observed_at='2026-10-11T00:00:00+00:00',
                succeeded=True, available=True))
            connection.execute(m.posting_value_candidates.insert().values(
                id='synthetic-discovery-candidate', posting_id='synthetic-discovery-posting',
                posting_snapshot_id='synthetic-discovery-snapshot',
                company_source_id='synthetic-discovery-source',
                quality_observation_id='synthetic-discovery-quality',
                observed_at='2026-10-11T00:00:00+00:00', normalized={'synthetic': True},
                application_url='https://history.example/jobs/1'))
            connection.execute(m.company_discovery_requests.insert().values(
                id='synthetic-discovery-request', company_id='synthetic-discovery-company',
                company_source_id='synthetic-discovery-source', candidate_id='synthetic-discovery-candidate',
                trigger_job_id='synthetic-discovery-job', profile_id=self.source_profile_id,
                profile_version=discovery_profile_version,
                reasons=['HIGH_MATCH_JOB'], highest_match_score=77.5,
                created_at='2026-10-11T00:00:00+00:00', last_seen_at='2026-10-12T00:00:00+00:00',
                state='NEEDS_ORIGIN_REVIEW', revision=2,
                selected_website='https://history.example/careers?token=SYNTHETIC_SECRET_DO_NOT_EXPORT',
                authorization='USER_CONFIRMED', domain_evidence=[], source_snapshot={'private': 'excluded'},
            ))
        self.application_id = prepare(
            self.store, job['id'], self.source_profile_id, self.root,
        )
        source_version = self.store.profile(self.source_profile_id)['version']
        with self.store.engine.begin() as connection:
            from services.profile_restore import _profile_fact_id, _profile_version_id
            current_snapshot = dict(connection.execute(select(m.profile_versions).where(
                m.profile_versions.c.profile_id == self.source_profile_id,
                m.profile_versions.c.version == source_version,
            )).mappings().one())
            old_version = 'synthetic-old-profile-version'
            old_contact = copy.deepcopy(current_snapshot['contact'])
            old_contact['name'] = 'Earlier Synthetic Name'
            connection.execute(m.profile_versions.insert().values(
                id=_profile_version_id(self.source_profile_id, old_version),
                profile_id=self.source_profile_id, version=old_version,
                contact=old_contact, answers=current_snapshot['answers'],
            ))
            current_fact = connection.execute(select(m.facts).where(
                m.facts.c.profile_id == self.source_profile_id,
                m.facts.c.version == source_version,
            )).mappings().first()
            if current_fact:
                connection.execute(m.facts.insert().values(
                    id=_profile_fact_id(self.source_profile_id, old_version, current_fact['fact_id']),
                    profile_id=self.source_profile_id, version=old_version,
                    fact_id=current_fact['fact_id'], kind=current_fact['kind'],
                    text='Earlier synthetic fact version', source='Synthetic prior snapshot',
                    verified=True, user_approved=True, confidence=95,
                ))
            connection.execute(m.career_stories.insert().values(
                id='synthetic-story-rev-1', profile_id=self.source_profile_id,
                story_key='synthetic-impact', revision=1, profile_version=source_version,
                kind='STAR_STORY', title='Synthetic recovery story',
                text='Synthetic content retained through recovery.', fact_ids=[],
                fact_fingerprints=[], tags=['synthetic'], allowed_uses=['INTERVIEW'],
                review_status='PENDING', review_note=None, created_at='2026-10-11T00:00:00+00:00',
                reviewed_at=None,
            ))
            connection.execute(m.ai_evaluation_runs.insert().values(
                id='synthetic-ai-run', profile_id=self.source_profile_id,
                profile_version=source_version, policy={'minimum_score': 55},
                task_provider='disabled', task_model='historical-only',
                evaluated_candidate_count=1, created_at='2026-10-11T00:00:00+00:00',
            ))
            connection.execute(m.ai_job_evaluations.insert().values(
                id='synthetic-ai-evaluation', run_id='synthetic-ai-run',
                profile_id=self.source_profile_id, job_id=job['id'], title=job['title'],
                company=job['company'], deterministic_score=72, category='STRONG',
                rationale='Synthetic historical evaluation', job_fingerprint='e' * 64,
                created_at='2026-10-11T00:00:00+00:00',
            ))
            connection.execute(m.ai_document_reviews.insert().values(
                id='synthetic-ai-document-review', profile_id=self.source_profile_id,
                run_id='synthetic-ai-run', job_id=job['id'],
                reviewed_at='2026-10-11T00:00:00+00:00',
            ))
            connection.execute(m.source_candidates.insert().values(
                id='synthetic-source-candidate', profile_id=self.source_profile_id,
                domain='jobs.example.test', display_name='Synthetic Jobs Site',
                site_type='JOB_BOARD', state='INTEGRATED',
                first_seen_at='2026-10-10T00:00:00+00:00',
                last_seen_at='2026-10-11T00:00:00+00:00', collection_enabled=True,
                review_reason='Synthetic candidate', integration_issue=None,
            ))
            connection.execute(m.source_candidate_reviews.insert().values(
                id='synthetic-source-review', candidate_id='synthetic-source-candidate',
                profile_id=self.source_profile_id, state='REJECTED', site_type='JOB_BOARD',
                reason='Synthetic rejected fixture', api_feed_evidence_status='UNKNOWN',
                api_feed_evidence_reference=None, api_feed_evidence_note='Synthetic evidence note',
                access_terms_note='Synthetic terms note', capabilities_limits_note='Synthetic limits note',
                expected_value_maintenance_note='Synthetic maintenance note', integration_issue=None,
                reviewed_at='2026-10-11T00:00:00+00:00',
            ))
            connection.execute(m.interview_prep_packs.insert().values(
                id='synthetic-interview-pack', profile_id=self.source_profile_id,
                job_id=job['id'], pack_version=1, profile_version=source_version,
                job_fingerprint='a' * 64, selected_story_revision_ids=['synthetic-story-rev-1'],
                questions=['Synthetic question'], talking_points=['Synthetic point'],
                star_prompts=['Synthetic prompt'], missing_evidence=[],
                export_text='Synthetic interview preparation content',
                created_at='2026-10-11T00:00:00+00:00',
            ))
            from packages.persistence import resume_workspace as workspace
            from datetime import datetime, timezone
            connection.execute(workspace.resume_workspace_revisions.insert().values(
                id='synthetic-workspace-revision', profile_id=self.source_profile_id,
                job_id=job['id'], version=1, profile_version=source_version,
                job_fingerprint='b' * 64, template_id='default',
                sections=[{'id': 'summary', 'text': 'Synthetic workspace text'}],
                preview_fingerprint='c' * 64, rendered_text='Synthetic workspace rendered text',
                source_refs=[], request_id='source-request', payload_fingerprint='d' * 64,
                created_at=datetime(2026, 10, 11, tzinfo=timezone.utc),
            ))
        with self.store.engine.begin() as connection:
            connection.execute(m.search_profiles.insert().values(
                id='source-search-profile', profile_id=self.source_profile_id,
                name='Synthetic active search', enabled=True,
                criteria={'keywords': ['synthetic'], 'scheduler': {'enabled': True}},
                source_ids=['fixture'], created_at='2026-10-11T00:00:00+00:00',
                updated_at='2026-10-11T00:00:00+00:00',
            ))
            connection.execute(m.ai_task_settings.insert().values(
                id='synthetic-ai-task', profile_id=self.source_profile_id,
                task='job_match', provider='openai', model='synthetic-model-disabled-on-restore',
                updated_at='2026-10-11T00:00:00+00:00',
            ))
            connection.execute(m.ai_cost_policies.insert().values(
                id='synthetic-ai-cost', profile_id=self.source_profile_id,
                minimum_score=50, max_input_candidates=100, max_ai_candidates=0,
                max_document_candidates=0, updated_at='2026-10-11T00:00:00+00:00',
            ))
            connection.execute(m.answer_confidence_policies.insert().values(
                id='synthetic-answer-policy', profile_id=self.source_profile_id,
                minimum_saved_answer_confidence=85, minimum_question_match_confidence=0.8,
                updated_at='2026-10-11T00:00:00+00:00',
            ))
        self.backup = export_profile_backup(
            self.store, self.source_profile_id, output_dir=self.root,
        )
        self.assertEqual(set(self.backup) - {'schema_version', 'export_type', 'profile_id'},
                         set(BACKUP_SECTION_ACCOUNTING))
        self.assertEqual(len(self.backup['company_discovery_requests']), 1)
        self.assertEqual(len(self.backup['company_discovery_history']), 1)
        self.assertNotIn('authorization', self.backup['company_discovery_requests'][0])
        self.assertNotIn('source_snapshot', self.backup['company_discovery_requests'][0])
        self.assertEqual(self.backup['company_discovery_requests'][0]['selected_website'],
                         'https://history.example/careers')
        self.assertNotIn('SYNTHETIC_SECRET_DO_NOT_EXPORT', str(self.backup))
        self.assertEqual(self.backup['company_discovery_history'][0]['payload']['reasons'],
                         ['HIGH_MATCH_JOB'])
        app = FastAPI()
        app.include_router(create_profile_restore_router(self.store, self.root))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_preview_explicit_import_readback_and_request_idempotency(self):
        before = self.store.profile(self.source_profile_id)
        with self.store.engine.connect() as connection:
            source_discovery_before = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == 'synthetic-discovery-request',
            )).mappings().one())
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
        self.assertEqual(preview['counts']['company_discovery_history'], 1)
        self.assertIn('COMPANY_DISCOVERY_HISTORY_ARCHIVED', preview['warnings'])
        self.assertNotIn('DISCOVERY_HISTORY_METADATA_PARTIAL', preview['warnings'])
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
            source_discovery_after = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == 'synthetic-discovery-request',
            )).mappings().one())
            self.assertEqual(source_discovery_after, source_discovery_before)
            self.assertEqual(connection.execute(select(
                m.profile_recovery_history_snapshots,
            ).where(m.profile_recovery_history_snapshots.c.profile_id == self.source_profile_id)
            ).all(), [])

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
        restored_applications = self.store.applications(restored_id)
        self.assertEqual(len(restored_applications), len(self.backup['applications']))
        self.assertEqual(
            [application['state'] for application in restored_applications],
            [application['state'] for application in self.backup['applications']],
        )
        source_job_ids = {job['id'] for job in self.backup['jobs']}
        restored_job_ids = {application['job_id'] for application in restored_applications}
        self.assertTrue(restored_job_ids)
        self.assertTrue(source_job_ids.isdisjoint(restored_job_ids))
        with self.store.engine.connect() as connection:
            restored_matches = connection.execute(select(m.matches).where(
                m.matches.c.profile_id == restored_id,
            )).mappings().all()
            restored_documents = connection.execute(select(m.documents).join(
                m.applications, m.documents.c.application_id == m.applications.c.id,
            ).where(m.applications.c.profile_id == restored_id)).mappings().all()
        self.assertEqual(len(restored_matches), len(self.backup['job_matches']))
        self.assertEqual(len(restored_documents), len(self.backup['documents']))
        self.assertTrue(all(document['application_id'] in {
            application['id'] for application in restored_applications
        } for document in restored_documents))

        with patch('packages.persistence.profile_restore._stage_files',
                   side_effect=AssertionError('durable replay must precede upload staging')):
            replay = self.client.post('/profile-restores', json=request)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), restored)
        self.assertEqual(len(self.store.profiles()), 2)
        with self.store.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(
                m.profile_recovery_history_snapshots,
            ).where(m.profile_recovery_history_snapshots.c.profile_id == restored_id)
            ).all()), 1)

        changed = copy.deepcopy(self.backup)
        changed['profile']['name'] = 'Changed Synthetic Source'
        for version in changed['profile_versions']:
            if version['version'] == changed['profile']['version']:
                version['contact']['name'] = changed['profile']['name']
        changed_preview = self.client.post('/profile-restores/preview', json={'backup': changed})
        self.assertEqual(changed_preview.status_code, 200, changed_preview.text)
        changed_request = dict(request, backup=changed,
                               expected_fingerprint=changed_preview.json()['fingerprint'])
        conflict = self.client.post('/profile-restores', json=changed_request)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(len(self.store.profiles()), 2)

    def test_export_restores_into_empty_separate_database_and_reloads(self):
        target_root = self.root / 'isolated-target'
        target_root.mkdir()
        target_db = target_root / 'target.db'
        target = Store(f"sqlite:///{target_db.as_posix()}")
        self.addCleanup(target.engine.dispose)
        target.migrate()
        target.bind_workspace_mode('FIXTURE')
        api = FastAPI()
        api.include_router(create_profile_restore_router(target, target_root))
        backup = copy.deepcopy(self.backup)
        backup['applications'][0]['state'] = 'SUBMITTING'
        with TestClient(api) as client:
            preview = client.post('/profile-restores/preview', json={'backup': backup})
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertNotIn('historical_profile_versions', preview.json()['omitted_sections'])
            restored = client.post('/profile-restores', json={
                'backup': backup, 'request_id': 'fresh-target-recovery',
                'expected_fingerprint': preview.json()['fingerprint'], 'confirmed': True,
            })
            self.assertEqual(restored.status_code, 201, restored.text)
            profile_id = restored.json()['profile_id']
        target.engine.dispose()

        # Reopen the physical target database and verify durable rows and
        # document bytes, while proving the source database remains unchanged.
        reloaded = Store(f"sqlite:///{target_db.as_posix()}")
        reloaded.migrate()
        try:
            self.assertEqual(len(reloaded.profiles()), 1)
            restored_apps = reloaded.applications(profile_id)
            self.assertEqual(len(restored_apps), len(self.backup['applications']))
            self.assertEqual(restored_apps[0]['state'], 'SUBMISSION_UNCONFIRMED')
            with reloaded.engine.connect() as connection:
                jobs = connection.execute(select(m.jobs)).mappings().all()
                docs = connection.execute(select(m.documents).join(
                    m.applications, m.documents.c.application_id == m.applications.c.id,
                ).where(m.applications.c.profile_id == profile_id)).mappings().all()
            self.assertEqual(len(jobs), len(self.backup['jobs']))
            self.assertEqual(len(docs), len(self.backup['documents']))
            with reloaded.engine.connect() as connection:
                versions = connection.execute(select(m.profile_versions).where(
                    m.profile_versions.c.profile_id == profile_id,
                )).mappings().all()
                restored_facts = connection.execute(select(m.facts).where(
                    m.facts.c.profile_id == profile_id,
                )).mappings().all()
            self.assertEqual(len(versions), len(self.backup['profile_versions']))
            self.assertEqual(len(restored_facts), len(self.backup['facts']))
            self.assertTrue(all(not item['verified'] and not item['user_approved']
                                for item in restored_facts))
            self.assertTrue(all(Path(target_root, app['id']).is_dir() for app in restored_apps))
            for document in docs:
                for file_format, relative in document['files'].items():
                    document_path = target_root / document['application_id'] / relative
                    contents = document_path.read_bytes()
                    self.assertEqual(hashlib.sha256(contents).hexdigest(), document['sha256'][file_format])
            with reloaded.engine.connect() as connection:
                from packages.persistence import resume_workspace as workspace
                stories = connection.execute(select(m.career_stories).where(
                    m.career_stories.c.profile_id == profile_id,
                )).mappings().all()
                packs = connection.execute(select(m.interview_prep_packs).where(
                    m.interview_prep_packs.c.profile_id == profile_id,
                )).mappings().all()
                workspaces = connection.execute(select(workspace.resume_workspace_revisions).where(
                    workspace.resume_workspace_revisions.c.profile_id == profile_id,
                )).mappings().all()
                operations = connection.execute(select(m.operations)).mappings().all()
                archived_history = connection.execute(select(
                    m.profile_recovery_history_snapshots,
                ).where(m.profile_recovery_history_snapshots.c.profile_id == profile_id)
                ).mappings().all()
                target_sources = connection.execute(select(m.company_sources)).mappings().all()
                target_discovery = connection.execute(select(m.company_discovery_requests)).mappings().all()
                ai_runs = connection.execute(select(m.ai_evaluation_runs).where(
                    m.ai_evaluation_runs.c.profile_id == profile_id,
                )).mappings().all()
                ai_evals = connection.execute(select(m.ai_job_evaluations).where(
                    m.ai_job_evaluations.c.profile_id == profile_id,
                )).mappings().all()
                ai_reviews = connection.execute(select(m.ai_document_reviews).where(
                    m.ai_document_reviews.c.profile_id == profile_id,
                )).mappings().all()
                source_candidates = connection.execute(select(m.source_candidates).where(
                    m.source_candidates.c.profile_id == profile_id,
                )).mappings().all()
                source_reviews = connection.execute(select(m.source_candidate_reviews).where(
                    m.source_candidate_reviews.c.profile_id == profile_id,
                )).mappings().all()
                ai_task_settings = connection.execute(select(m.ai_task_settings).where(
                    m.ai_task_settings.c.profile_id == profile_id,
                )).mappings().all()
                ai_cost = connection.execute(select(m.ai_cost_policies).where(
                    m.ai_cost_policies.c.profile_id == profile_id,
                )).mappings().all()
                answer_policy = connection.execute(select(m.answer_confidence_policies).where(
                    m.answer_confidence_policies.c.profile_id == profile_id,
                )).mappings().all()
                resume_revisions = connection.execute(select(m.resume_revisions).where(
                    m.resume_revisions.c.profile_id == profile_id,
                )).mappings().all()
            self.assertEqual(len(stories), len(self.backup['career_stories']))
            self.assertEqual(stories[0]['text'], 'Synthetic content retained through recovery.')
            self.assertEqual(stories[0]['review_status'], 'PENDING')
            self.assertEqual(len(packs), len(self.backup['interview_prep_packs']))
            self.assertEqual(packs[0]['export_text'], 'Synthetic interview preparation content')
            self.assertNotEqual(packs[0]['job_id'], self.backup['interview_prep_packs'][0]['job_id'])
            self.assertEqual(packs[0]['selected_story_revision_ids'], [stories[0]['id']])
            self.assertNotEqual(stories[0]['id'], self.backup['career_stories'][0]['id'])
            self.assertEqual(len(workspaces), len(self.backup['resume_workspace_revisions']))
            self.assertEqual(workspaces[0]['rendered_text'], 'Synthetic workspace rendered text')
            self.assertEqual(operations, [])
            self.assertEqual(target_sources, [])
            self.assertEqual(target_discovery, [])
            self.assertEqual(len(archived_history), 1)
            self.assertEqual(archived_history[0]['profile_id'], profile_id)
            self.assertEqual(archived_history[0]['source_identity'], 'synthetic-discovery-request')
            self.assertEqual(archived_history[0]['payload']['historical_refs']['profile_id'],
                             self.source_profile_id)
            self.assertEqual(archived_history[0]['payload']['reference_status'], 'LINKED')
            self.assertEqual(archived_history[0]['payload']['metadata_status'], 'COMPLETE')
            self.assertNotEqual(archived_history[0]['payload']['target_job_id'],
                                'synthetic-discovery-job')
            self.assertNotEqual(archived_history[0]['payload']['target_company_id'],
                                'synthetic-discovery-company')
            self.assertEqual(len(ai_runs), len(self.backup['ai_evaluation_runs']))
            self.assertEqual(len(ai_evals), len(self.backup['ai_job_evaluations']))
            self.assertEqual(len(ai_reviews), len(self.backup['ai_document_reviews']))
            self.assertNotEqual(ai_runs[0]['id'], self.backup['ai_evaluation_runs'][0]['id'])
            self.assertNotEqual(ai_evals[0]['job_id'], self.backup['ai_job_evaluations'][0]['job_id'])
            self.assertEqual(len(source_candidates), len(self.backup['source_candidates']))
            self.assertFalse(source_candidates[0]['collection_enabled'])
            self.assertEqual(source_candidates[0]['state'], 'NEEDS_REVIEW')
            self.assertEqual(len(source_reviews), len(self.backup['source_candidate_reviews']))
            self.assertEqual(source_reviews[0]['candidate_id'], source_candidates[0]['id'])
            self.assertEqual(ai_task_settings[0]['provider'], 'disabled')
            self.assertIsNone(ai_task_settings[0]['model'])
            self.assertEqual(ai_cost[0]['minimum_score'], 50)
            self.assertEqual(answer_policy[0]['minimum_saved_answer_confidence'], 85)
            self.assertEqual(len(resume_revisions), len(self.backup['resume_revisions']))
            self.assertTrue({row['series_id'] for row in resume_revisions}.isdisjoint(
                {row['series_id'] for row in self.backup['resume_revisions']}))
            reloaded.bind_workspace_mode('FIXTURE')
            reloaded.save_profile({**PROFILE, 'id': 'other-profile',
                                   'name': 'Other Synthetic', 'email': 'other@example.invalid'})
            api = FastAPI()
            api.include_router(create_profile_restore_router(reloaded, target_root))
            with TestClient(api) as client:
                history_response = client.get(f'/profile-restores/{profile_id}/history')
                self.assertEqual(history_response.status_code, 200, history_response.text)
                history_body = history_response.json()
                self.assertEqual(history_body['mode'], 'READ_ONLY_HISTORICAL_ARCHIVE')
                self.assertTrue(history_body['authorization_required_for_future_live_action'])
                self.assertEqual(len(history_body['history']), 1)
                self.assertEqual(history_body['history'][0]['source_identity'],
                                 'synthetic-discovery-request')
                with reloaded.engine.begin() as connection:
                    damaged = dict(archived_history[0]['payload'])
                    damaged['target_job_id'] = 'foreign-profile-job'
                    connection.execute(update(m.profile_recovery_history_snapshots).where(
                        m.profile_recovery_history_snapshots.c.profile_id == profile_id,
                    ).values(payload=damaged))
                corrupt = client.get(f'/profile-restores/{profile_id}/history')
                self.assertEqual(corrupt.status_code, 500)
                self.assertEqual(corrupt.json()['detail']['code'], 'RECOVERY_HISTORY_UNAVAILABLE')
                with reloaded.engine.begin() as connection:
                    connection.execute(update(m.profile_recovery_history_snapshots).where(
                        m.profile_recovery_history_snapshots.c.profile_id == profile_id,
                    ).values(payload=archived_history[0]['payload']))
                other_history = client.get('/profile-restores/other-profile/history')
                self.assertEqual(other_history.status_code, 200)
                self.assertEqual(other_history.json()['history'], [])

            recovered_backup = export_profile_backup(reloaded, profile_id, output_dir=target_root)
            self.assertEqual(len(recovered_backup['company_discovery_history']), 1)
            second_root = self.root / 'second-isolated-target'
            second_root.mkdir()
            second = Store(f"sqlite:///{(second_root / 'second.db').as_posix()}")
            second.migrate()
            second.bind_workspace_mode('FIXTURE')
            second_api = FastAPI()
            second_api.include_router(create_profile_restore_router(second, second_root))
            try:
                with TestClient(second_api) as client:
                    second_preview = client.post('/profile-restores/preview',
                                                json={'backup': recovered_backup})
                    self.assertEqual(second_preview.status_code, 200, second_preview.text)
                    second_restore = client.post('/profile-restores', json={
                        'backup': recovered_backup, 'request_id': 'recovery-reexport-roundtrip',
                        'expected_fingerprint': second_preview.json()['fingerprint'], 'confirmed': True,
                    })
                    self.assertEqual(second_restore.status_code, 201, second_restore.text)
                    second_profile = second_restore.json()['profile_id']
                    second_history = client.get(f'/profile-restores/{second_profile}/history')
                    self.assertEqual(second_history.status_code, 200)
                    self.assertEqual(len(second_history.json()['history']), 1)
                    self.assertEqual(second_history.json()['history'][0]['source_identity'],
                                     'synthetic-discovery-request')
                with second.engine.connect() as connection:
                    self.assertEqual(connection.execute(select(m.company_sources)).all(), [])
                    self.assertEqual(connection.execute(select(m.company_discovery_requests)).all(), [])
                    self.assertEqual(connection.execute(select(m.operations)).all(), [])
                    second_snapshot = connection.execute(select(
                        m.profile_recovery_history_snapshots,
                    ).where(m.profile_recovery_history_snapshots.c.profile_id == second_profile)
                    ).mappings().one()
                self.assertNotEqual(second_snapshot['payload']['target_job_id'],
                                    archived_history[0]['payload']['target_job_id'])
                self.assertEqual(second_snapshot['payload']['historical_refs']['profile_id'],
                                 self.source_profile_id)
            finally:
                second.engine.dispose()
            self.assertEqual(len(self.store.profiles()), 1)
            self.assertEqual(len(self.store.applications(self.source_profile_id)), 1)
        finally:
            reloaded.engine.dispose()

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
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.profile_recovery_history_snapshots)).all(), [])
        self.assertEqual(sentinel.read_bytes(), b'leave this file untouched')
        self.assertEqual(set(uploads_root.iterdir()), existing_files)


if __name__ == '__main__':
    unittest.main()
