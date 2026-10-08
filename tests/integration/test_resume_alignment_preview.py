import unittest
from sqlalchemy import func, select, update
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence import models as m
from services.discovery.pipeline import discover
from services.documents.tailor import target_fingerprint
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE
from fixtures.mock_server import fixture_server


class ResumeAlignmentPreviewIntegrationTests(unittest.TestCase):
    setUp = fixtures.DocumentProvenanceTests.setUp
    tearDown = fixtures.DocumentProvenanceTests.tearDown

    def row_counts(self):
        with self.store.engine.connect() as connection:
            return {table.name: connection.execute(
                select(func.count()).select_from(table)).scalar_one()
                for table in (m.applications, m.documents, m.operations)}

    def test_preview_is_read_only_profile_scoped_and_rejects_stale_snapshots(self):
        facts = [
            dict(PROFILE['facts'][0], id='python', text='Python'),
            dict(PROFILE['facts'][0], id='duty', kind='responsibility',
                 text='Administer Linux servers and restore production services.'),
        ]
        self.store.save_profile(dict(PROFILE, facts=facts))
        self.store.save_profile(dict(PROFILE, id='other', facts=[
            dict(PROFILE['facts'][0], id='other-python', text='Python')]))
        with fixture_server() as origin:
            discover(self.store, origin, 'fixture')
            job = self.store.jobs()[0]
            description = ('Responsibilities:\n- Administer Linux servers and restore production services.\n'
                           '- Coordinate cross-team incident communication and status updates.\nRequirements:\n')
            with self.store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == job['id']).values(
                    skills=['Python', 'Kubernetes'], description=description))
            job = self.store.job(job['id'])
            fingerprint = target_fingerprint(job)
            app = create_app(self.store, origin, self.root, operation_mode='FIXTURE')
            with TestClient(app) as client:
                saved_job = client.get(f"/jobs/{job['id']}")
                self.assertEqual(saved_job.status_code, 200)
                self.assertEqual(saved_job.json()['alignment_fingerprint'], fingerprint)
                request = {'expected_profile_version': self.store.profile('fixture')['version'],
                           'expected_job_fingerprint': fingerprint}
                before = self.row_counts()
                response = client.post(
                    f"/profiles/fixture/jobs/{job['id']}/resume-alignment-preview", json=request)
                self.assertEqual(response.status_code, 200, response.text)
                preview = response.json()
                self.assertEqual(preview['profile_id'], 'fixture')
                self.assertEqual(preview['matches']['skills'][0]['fact_ids'], ['python'])
                self.assertEqual(preview['matches']['skills'][1]['fact_ids'], [])
                self.assertEqual(preview['matches']['responsibilities'][0]['fact_ids'], ['duty'])
                self.assertIn('Kubernetes', preview['unmatched']['skills'])
                self.assertTrue(any('coordinate cross team' in term
                                    for term in preview['unmatched']['responsibilities']))
                self.assertNotIn('other-sql', str(preview))
                self.assertNotIn('AWS certification', str(preview))
                self.assertEqual(client.post(
                    f"/profiles/other/jobs/{job['id']}/resume-alignment-preview",
                    json={**request, 'expected_profile_version': self.store.profile('other')['version']}
                ).json()['matches']['skills'][0]['fact_ids'], ['other-python'])
                self.assertEqual(client.post(
                    f"/profiles/fixture/jobs/{job['id']}/resume-alignment-preview",
                    json={**request, 'expected_profile_version': '0' * 64}).status_code, 409)
                self.assertEqual(client.post(
                    f"/profiles/fixture/jobs/{job['id']}/resume-alignment-preview",
                    json={**request, 'expected_job_fingerprint': '0' * 64}).status_code, 409)
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.jobs).where(m.jobs.c.id == job['id']).values(
                        skills=['Python', 'Kubernetes', 'Terraform']))
                stale_prepare = client.post('/applications', json={
                    'profile_id': 'fixture', 'job_id': job['id'], 'request_id': 'stale-alignment',
                    'tailor_resume': True, 'tailor_fact_ids': ['python'],
                    'expected_profile_version': request['expected_profile_version'],
                    'expected_job_fingerprint': fingerprint,
                })
                self.assertEqual(stale_prepare.status_code, 409, stale_prepare.text)
                after = self.row_counts()
                self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
