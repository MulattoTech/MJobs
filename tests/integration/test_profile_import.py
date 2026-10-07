"""Resume-led profiles retain review boundaries and immutable source versions."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import asyncio
import inspect
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.career_profile.extraction import ResumeExtractionCancelled
from services.documents.generate import prepare, PreparationError
from services.documents.uploads import save_resume
from fixtures.mock_server import fixture_server
from services.discovery.pipeline import discover
from workers.operations import admit_inline
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

    def put_profile(self, profile):
        payload = dict(profile)
        current = next((row for row in self.store.profiles() if row['id'] == payload['id']), None)
        if current and 'expected_version' not in payload:
            payload['expected_version'] = current['version']
        return self.client.put('/profiles', json=payload)

    def test_initial_profile_can_be_saved_without_inventing_a_fact(self):
        response = self.put_profile(dict(PROFILE, facts=[]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.profile('fixture')['facts'], [])
        self.assertEqual(self.upload().status_code, 201)

    def test_contact_and_preferences_survive_updates_in_immutable_snapshots(self):
        profile = dict(PROFILE, contact={'phones':['+1 202 555 0142'], 'address_line1':'10 Example Lane',
                                        'linkedin':'https://www.linkedin.com/in/example'},
                       preferences={'employment_types':['PART_TIME','PER_DIEM'], 'work_arrangements':['HYBRID'],
                                    'max_commute_miles':25, 'max_commute_minutes':40, 'shifts':['DAY']})
        first = self.put_profile(profile)
        self.assertEqual(first.status_code, 200, first.text)
        previous = first.json()['version']
        profile['contact']['phones'] = ['+1 202 555 0199']
        self.assertEqual(self.put_profile(profile).status_code, 200)
        old = self.store.profile('fixture', previous)
        self.assertEqual(old['contact']['phones'], ['+1 202 555 0142'])
        self.assertEqual(old['preferences']['max_commute_miles'], 25)
        self.assertEqual(self.store.profile('fixture')['contact']['phones'], ['+1 202 555 0199'])

    def test_invalid_preferences_and_unsafe_links_are_rejected(self):
        for change in ({'contact':{'linkedin':'javascript:alert(1)'}},
                       {'preferences':{'max_commute_miles':-1}},
                       {'preferences':{'work_arrangements':['UNKNOWN']}}):
            self.assertEqual(self.put_profile(dict(PROFILE, **change)).status_code, 422)

    def test_complete_career_profile_survives_restart_without_approving_answers(self):
        contact = dict(first_name='Alex', middle_name='Q', last_name='Example', preferred_name='Al',
                       phones=['+1 202 555 0142'], city='Tulsa', state='OK', postal_code='74101',
                       country='US', linkedin='https://linkedin.com/in/example',
                       github='https://github.com/example', portfolio='https://example.invalid')
        preferences = dict(desired_roles=['Systems Administrator'], work_arrangements=['REMOTE'],
                           employment_types=['FULL_TIME'], salary_expectations='USD 65000 annually',
                           work_authorization='Authorized to work in the United States',
                           sponsorship_needs='NOT_REQUIRED', travel_preference='Up to 10%',
                           career_goals='Develop senior administration skills')
        kinds = ['experience', 'responsibility', 'education', 'certification', 'skill', 'project',
                 'accomplishment', 'technology_experience']
        facts = [dict(id=kind, kind=kind, text='User supplied ' + kind, source='Manual entry',
                      confidence=100, verified=False, user_approved=False) for kind in kinds]
        response = self.put_profile(dict(PROFILE, contact=contact,
                                  preferences=preferences, facts=facts, answers=[]))
        self.assertEqual(response.status_code, 200, response.text)
        version = response.json()['version']
        # A changed preference must not rewrite the immutable earlier profile.
        preferences['sponsorship_needs'] = 'REQUIRED'
        self.assertEqual(self.put_profile(dict(PROFILE, contact=contact,
                         preferences=preferences, facts=facts, answers=[])).status_code, 200)
        restarted = Store(self.store.url)
        try:
            saved = restarted.profile('fixture')
            self.assertEqual({k:saved['contact'][k] for k in contact}, contact)
            self.assertEqual({k:saved['preferences'][k] for k in preferences}, preferences)
            self.assertEqual(saved['answers'], [])
            self.assertEqual({f['kind'] for f in saved['facts']}, set(kinds))
            self.assertTrue(all(not f['verified'] and not f['user_approved'] for f in saved['facts']))
            self.assertEqual(restarted.profile('fixture', version)['preferences']['sponsorship_needs'], 'NOT_REQUIRED')
        finally:
            restarted.engine.dispose()

    def test_authorization_is_optional_and_rejects_invented_sponsorship_states(self):
        self.assertEqual(self.put_profile(dict(PROFILE, facts=[])).status_code, 200)
        preferences = self.client.get('/profiles/fixture').json()['preferences']
        self.assertEqual(preferences['work_authorization'], '')
        self.assertEqual(preferences['sponsorship_needs'], 'NOT_SPECIFIED')
        for preferences in ({'sponsorship_needs': 'probably'}, {'work_authorization': True}):
            self.assertEqual(self.put_profile(dict(PROFILE, preferences=preferences)).status_code, 422)

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
        self.assertEqual(self.put_profile(dict(PROFILE, facts=facts)).status_code, 200)
        with self.assertRaisesRegex(PreparationError, 'Approve and verify'):
            prepare(self.store, job['id'], 'fixture', self.root)
        facts[0].update(verified=True, user_approved=True)
        self.put_profile(dict(PROFILE, facts=facts))
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
        dated = next(fact for fact in extracted['facts'] if '2020-2024' in fact['text'])
        self.assertEqual(extracted['date_ranges'], [{'fact_id': dated['id'], 'raw': '2020-2024',
                                                    'start': '2020', 'end': '2024'}])
        self.assertTrue(all(not f['verified'] and not f['user_approved'] for f in extracted['facts']))
        self.assertTrue(all(uploaded['id'] in f['source'] for f in extracted['facts']))
        self.assertEqual(self.store.profile('fixture'), before)
        self.assertEqual(self.client.post(endpoint).json(), extracted)
        self.store.save_profile(dict(PROFILE, id='other'))
        self.assertEqual(self.client.post(endpoint.replace('/fixture/', '/other/')).status_code, 404)
        path = self.root / 'uploads' / (uploaded['id'] + '.pdf')
        path.write_bytes(b'corrupt')
        self.assertEqual(self.client.post(endpoint).status_code, 409)

    def test_disconnected_resume_extract_cancels_child_work_before_returning(self):
        self.store.save_profile(PROFILE)
        uploaded = self.upload().json()
        route = next(route for route in self.client.app.routes
                     if getattr(route, 'path', None) == '/profiles/{profile_id}/resumes/{resume_id}/extract')
        self.assertTrue(inspect.iscoroutinefunction(route.endpoint))
        request = type('RequestProbe', (), {})()
        request.is_disconnected = AsyncMock(side_effect=[False, True])
        observed = threading.Event()

        def wait_for_cancel(data, format, source, cancellation_event):
            deadline = time.monotonic() + 2
            while not cancellation_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.005)
            if cancellation_event.is_set():
                observed.set()
                raise ResumeExtractionCancelled()
            raise AssertionError('client disconnect was not propagated to extraction')

        with patch('apps.api.main.extract_resume', side_effect=wait_for_cancel):
            response = asyncio.run(route.endpoint('fixture', uploaded['id'], request))
        self.assertEqual(response.status_code, 499)
        self.assertTrue(observed.is_set())

    def test_live_http_disconnect_kills_native_resume_parser_child_without_profile_mutation(self):
        import socket
        import subprocess
        import sys
        import uvicorn
        from starlette.requests import Request as StarletteRequest
        from fixtures.browser_ports import bound_browser_socket

        self.store.save_profile(PROFILE)
        pdf_stream = BytesIO()
        pdf = canvas.Canvas(pdf_stream)
        pdf.drawString(72, 720, 'Synthetic profile extraction disconnect test')
        pdf.save()
        resume_bytes = pdf_stream.getvalue()
        uploaded = self.upload(content=resume_bytes, **{
            'X-Filename': 'disconnect-test.pdf', 'Content-Type': 'application/pdf',
        }).json()
        profile_before = self.store.profile('fixture')
        child_started = threading.Event()
        disconnect_observed = threading.Event()
        children = []
        original_popen = subprocess.Popen
        original_is_disconnected = StarletteRequest.is_disconnected

        def slow_parser_child(command, **kwargs):
            if 'services.career_profile.parser_worker' not in command:
                return original_popen(command, **kwargs)
            child = original_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            children.append(child)
            child_started.set()
            return child

        async def observe_disconnect(request):
            disconnected = await original_is_disconnected(request)
            if disconnected:
                disconnect_observed.set()
            return disconnected

        app = create_app(self.store, 'http://127.0.0.1:1', self.root, operation_mode='PERSONAL')
        sock = bound_browser_socket()
        origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        server_thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        server_thread.start()
        peer = None
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not server_thread.is_alive() or time.monotonic() > deadline:
                    self.fail('Resume extraction disconnect API did not start')
                time.sleep(0.02)

            peer = socket.create_connection(('127.0.0.1', sock.getsockname()[1]), timeout=5)
            peer.settimeout(5)
            request_path = f"/profiles/fixture/resumes/{uploaded['id']}/extract"
            peer.sendall((f'POST {request_path} HTTP/1.1\r\n'
                          f'Host: 127.0.0.1:{sock.getsockname()[1]}\r\n'
                          f'Origin: {origin}\r\nContent-Length: 0\r\n'
                          'Connection: keep-alive\r\n\r\n').encode('ascii'))
            with patch.object(subprocess, 'Popen', slow_parser_child), patch.object(
                    StarletteRequest, 'is_disconnected', observe_disconnect):
                self.assertTrue(child_started.wait(5), 'native parser child did not start')
                peer.shutdown(socket.SHUT_WR)
                self.assertTrue(disconnect_observed.wait(3),
                                'API did not observe the actual closed client request transport')
                child_deadline = time.monotonic() + 3
                while children and children[0].poll() is None and time.monotonic() < child_deadline:
                    time.sleep(0.01)
                self.assertTrue(children and children[0].poll() is not None,
                                'cancelled parser child was not reaped')

            self.assertEqual(self.store.profile('fixture'), profile_before)
            readback = self.client.get(f"/profiles/fixture/resumes/{uploaded['id']}")
            self.assertEqual(readback.status_code, 200, readback.text)
            self.assertEqual(readback.content, resume_bytes)
            with self.store.engine.connect() as connection:
                operation = connection.execute(select(m.operations).where(
                    m.operations.c.kind == 'RESUME_EXTRACT',
                    m.operations.c.entity_id == uploaded['id'],
                )).mappings().one()
            operation_deadline = time.monotonic() + 3
            while operation['state'] != 'CANCELLED' and time.monotonic() < operation_deadline:
                time.sleep(0.01)
                with self.store.engine.connect() as connection:
                    operation = connection.execute(select(m.operations).where(
                        m.operations.c.kind == 'RESUME_EXTRACT',
                        m.operations.c.entity_id == uploaded['id'],
                    )).mappings().one()
            self.assertEqual(operation['state'], 'CANCELLED')
            self.assertEqual(self.store.count('resume_revisions'), 1)
        finally:
            if peer is not None:
                peer.close()
            server.should_exit = True
            server_thread.join(timeout=10)
            sock.close()
            self.assertFalse(server_thread.is_alive(), 'Resume extraction disconnect API did not stop')

    def test_explicit_extract_cancel_is_profile_scoped(self):
        self.store.save_profile(PROFILE)
        uploaded = self.upload().json()
        admit_inline(self.store, 'RESUME_EXTRACT', uploaded['id'])
        active = threading.Event()
        self.client.app.state.resume_extraction_events[('fixture', uploaded['id'])] = {active}

        denied = self.client.post(f"/profiles/another/resumes/{uploaded['id']}/extract/cancel")
        self.assertEqual(denied.status_code, 404)
        self.assertFalse(active.is_set())

        response = self.client.post(f"/profiles/fixture/resumes/{uploaded['id']}/extract/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['cancelled'])
        self.assertTrue(active.is_set())

    def test_extract_cancel_from_another_api_instance_stops_work_without_profile_changes(self):
        self.store.save_profile(PROFILE)
        uploaded = self.upload().json()
        other_store = Store(self.store.url)
        other_client = TestClient(create_app(other_store, 'http://127.0.0.1:1', self.root))
        started = threading.Event()
        cleanup = threading.Event()
        observed = threading.Event()
        endpoint = f"/profiles/fixture/resumes/{uploaded['id']}/extract"
        cancel_endpoint = endpoint + "/cancel"
        before = self.store.profile('fixture')

        def wait_for_durable_cancel(_data, _format, _source, cancellation_event):
            started.set()
            deadline = time.monotonic() + 3
            while (not cancellation_event.is_set() and not cleanup.is_set()
                   and time.monotonic() < deadline):
                time.sleep(0.005)
            if cancellation_event.is_set():
                observed.set()
                raise ResumeExtractionCancelled()
            if cleanup.is_set():
                raise ResumeExtractionCancelled()
            raise AssertionError('cross-instance cancellation was not propagated')

        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch('apps.api.main.extract_resume', side_effect=wait_for_durable_cancel):
                work = pool.submit(self.client.post, endpoint)
                try:
                    self.assertTrue(started.wait(2), 'resume extraction did not start')
                    denied = other_client.post('/profiles/another' + cancel_endpoint[len('/profiles/fixture'):])
                    self.assertEqual(denied.status_code, 404)
                    cancelled = other_client.post(cancel_endpoint)
                    self.assertEqual(cancelled.status_code, 200, cancelled.text)
                    self.assertTrue(cancelled.json()['cancelled'])
                    response = work.result(timeout=3)
                    self.assertEqual(response.status_code, 499, response.text)
                finally:
                    cleanup.set()
                    if not work.done():
                        work.result(timeout=4)
                    other_client.close()
                    other_store.engine.dispose()

        self.assertTrue(observed.is_set())
        self.assertEqual(self.store.profile('fixture'), before)
        self.assertEqual(self.client.get('/profiles/fixture/resumes').json()['resumes'][0]['id'], uploaded['id'])
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(
                m.operations.c.kind == 'RESUME_EXTRACT',
                m.operations.c.entity_id == uploaded['id'],
            )).mappings().one()
        self.assertEqual(row['state'], 'CANCELLED')

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
