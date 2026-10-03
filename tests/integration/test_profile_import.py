"""Resume-led profiles retain review boundaries and immutable source versions."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from apps.api.main import create_app
from packages.persistence.store import Store
from services.documents.generate import prepare, PreparationError
from services.documents.uploads import save_resume
from fixtures.mock_server import fixture_server
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


class ProfileImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode('PERSONAL')
        self.client = TestClient(create_app(self.store, 'http://127.0.0.1:1', self.root))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def upload(self, profile='fixture', content=b'Example Applicant\nSkills\nPython, SQL', **headers):
        return self.client.post(f'/profiles/{profile}/resumes', content=content,
                                headers={'X-Filename': 'base.txt', 'Content-Type': 'text/plain', **headers})

    def test_initial_profile_can_be_saved_without_inventing_a_fact(self):
        response = self.client.put('/profiles', json=dict(PROFILE, facts=[]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.profile('fixture')['facts'], [])
        self.assertEqual(self.upload().status_code, 201)

    def test_contact_and_preferences_survive_updates_in_immutable_snapshots(self):
        profile = dict(PROFILE, contact={'phones':['+1 202 555 0142'], 'address_line1':'10 Example Lane',
                                        'linkedin':'https://www.linkedin.com/in/example'},
                       preferences={'employment_types':['PART_TIME','PER_DIEM'], 'work_arrangements':['HYBRID'],
                                    'max_commute_miles':25, 'max_commute_minutes':40, 'shifts':['DAY']})
        first = self.client.put('/profiles', json=profile)
        self.assertEqual(first.status_code, 200, first.text)
        previous = first.json()['version']
        profile['contact']['phones'] = ['+1 202 555 0199']
        self.assertEqual(self.client.put('/profiles', json=profile).status_code, 200)
        old = self.store.profile('fixture', previous)
        self.assertEqual(old['contact']['phones'], ['+1 202 555 0142'])
        self.assertEqual(old['preferences']['max_commute_miles'], 25)
        self.assertEqual(self.store.profile('fixture')['contact']['phones'], ['+1 202 555 0199'])

    def test_invalid_preferences_and_unsafe_links_are_rejected(self):
        for change in ({'contact':{'linkedin':'javascript:alert(1)'}},
                       {'preferences':{'max_commute_miles':-1}},
                       {'preferences':{'work_arrangements':['UNKNOWN']}}):
            self.assertEqual(self.client.put('/profiles', json=dict(PROFILE, **change)).status_code, 422)

    def test_empty_and_imported_pending_profiles_cannot_generate_documents(self):
        self.store.save_profile(dict(PROFILE, facts=[]))
        with fixture_server() as origin:
            discover(self.store, origin, 'fixture')
        job = self.store.jobs()[0]
        with self.assertRaisesRegex(PreparationError, 'Approve and verify'):
            prepare(self.store, job['id'], 'fixture', self.root)
        uploaded = self.upload().json()
        facts = self.client.post(f"/profiles/fixture/resumes/{uploaded['id']}/extract").json()['facts']
        self.assertTrue(facts)
        self.assertEqual(self.client.put('/profiles', json=dict(PROFILE, facts=facts)).status_code, 200)
        with self.assertRaisesRegex(PreparationError, 'Approve and verify'):
            prepare(self.store, job['id'], 'fixture', self.root)
        facts[0].update(verified=True, user_approved=True)
        self.client.put('/profiles', json=dict(PROFILE, facts=facts))
        application = self.store.application(prepare(self.store, job['id'], 'fixture', self.root))
        self.assertEqual(application['fact_ids'], [facts[0]['id']])

    def test_concurrent_version_uploads_allocate_unique_versions(self):
        self.store.save_profile(PROFILE)
        first = self.upload().json()
        def upload_version(index):
            return save_resume(self.store, self.root, 'fixture', 'base.txt', 'text/plain',
                               f'Example revision {index}'.encode(), series_id=first['series_id'])
        with ThreadPoolExecutor(max_workers=3) as pool:
            revisions = list(pool.map(upload_version, range(3)))
        self.assertEqual(sorted(row['version'] for row in revisions), [2,3,4])

    def test_extract_pdf_populates_review_candidates_without_saving_or_approving(self):
        self.store.save_profile(PROFILE)
        before = self.store.profile('fixture')
        stream = BytesIO()
        pdf = canvas.Canvas(stream)
        text = pdf.beginText(40, 780)
        for line in ['Example Applicant', 'example@example.invalid', '+1 202 555 0142',
                     'Skills', 'Python, SQL', 'Experience', 'Example Co | Engineer | 2020-2024']:
            text.textLine(line)
        pdf.drawText(text)
        pdf.save()
        uploaded = self.client.post('/profiles/fixture/resumes', content=stream.getvalue(),
                                    headers={'X-Filename':'base.pdf','Content-Type':'application/pdf'}).json()
        endpoint = f"/profiles/fixture/resumes/{uploaded['id']}/extract"
        response = self.client.post(endpoint)
        self.assertEqual(response.status_code, 200, response.text)
        extracted = response.json()
        self.assertEqual(extracted['status'], 'READY_FOR_REVIEW')
        self.assertEqual(extracted['contact']['email'], 'example@example.invalid')
        self.assertIn('Python', [fact['text'] for fact in extracted['facts']])
        self.assertTrue(all(not f['verified'] and not f['user_approved'] for f in extracted['facts']))
        self.assertTrue(all(uploaded['id'] in f['source'] for f in extracted['facts']))
        self.assertEqual(self.store.profile('fixture'), before)
        self.assertEqual(self.client.post(endpoint).json(), extracted)
        self.store.save_profile(dict(PROFILE, id='other'))
        self.assertEqual(self.client.post(endpoint.replace('/fixture/', '/other/')).status_code, 404)
        path = self.root / 'uploads' / (uploaded['id'] + '.pdf')
        path.write_bytes(b'corrupt')
        self.assertEqual(self.client.post(endpoint).status_code, 409)

    def test_named_resume_versions_are_idempotent_scoped_and_downloadable(self):
        self.store.save_profile(PROFILE)
        first = self.upload(**{'X-Resume-Label':'Engineering'}).json()
        self.assertEqual(first.get('version'), 1)
        second = self.upload(content=b'Example Applicant\nSkills\nPython, SQL, Linux',
                             **{'X-Resume-Series':first['series_id']}).json()
        retry = self.upload(content=b'Example Applicant\nSkills\nPython, SQL, Linux',
                            **{'X-Resume-Series':first['series_id']}).json()
        self.assertEqual(second['version'], 2)
        self.assertEqual(retry, second)
        self.assertEqual(second['label'], 'Engineering')
        independent = self.upload(content=b'Example Applicant\nSkills\nWriting',
                                  **{'X-Resume-Label':'Writing'}).json()
        self.assertNotEqual(independent['series_id'], first['series_id'])
        rows = self.client.get('/profiles/fixture/resumes').json()['resumes']
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(self.client.get(f"/profiles/fixture/resumes/{row['id']}").status_code, 200)
        self.store.save_profile(dict(PROFILE, id='other'))
        self.assertEqual(self.upload(profile='other', **{'X-Resume-Series':first['series_id']}).status_code, 404)
        self.assertEqual(self.upload(**{'X-Resume-Series':'missing'}).status_code, 404)


if __name__ == '__main__':
    unittest.main()
