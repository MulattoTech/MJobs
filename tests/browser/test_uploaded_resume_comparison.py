"""Connected browser acceptance for a durable uploaded-base comparison."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import update
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from services.documents.uploads import save_resume
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class UploadedResumeComparisonBrowserTests(unittest.TestCase):
    def test_v5_selected_target_bullets_compare_uploaded_base_after_store_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'uploaded-comparison.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode('FIXTURE')

            target_duty = 'Manage backup systems across regional offices and document recovery plans'
            excluded_duty = 'Maintained office printers and arranged supply deliveries.'
            excluded_summary = 'Experienced retail sales associate.'
            invented_job_only_text = 'Lead nationwide lunar satellite telemetry.'
            facts = [
                dict(PROFILE['facts'][0], id='python', kind='skill', text='Python'),
                dict(PROFILE['facts'][1], id='sql', kind='skill', text='SQL'),
                dict(PROFILE['facts'][0], id='summary-backup', kind='summary',
                     text=f'Infrastructure administrator. {target_duty}.'),
                dict(PROFILE['facts'][0], id='summary-sales', kind='summary', text=excluded_summary),
                dict(PROFILE['facts'][0], id='duty-backup', kind='responsibility', text=target_duty + '.'),
                dict(PROFILE['facts'][0], id='duty-office', kind='responsibility', text=excluded_duty),
                dict(PROFILE['facts'][0], id='unapproved-telemetry', kind='project',
                     text='Built a lunar satellite telemetry platform.', verified=False, user_approved=False),
            ]
            store.save_profile(dict(PROFILE, facts=facts))
            profile_before = store.profile('fixture')
            with fixture_server() as fixture:
                discover(store, fixture, 'fixture')
            job = store.jobs()[0]
            description = (f'Responsibilities:\n- {target_duty}.\n'
                           f'Requirements:\n- {invented_job_only_text}')
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == job['id']).values(
                    skills=['Python'], description=description))
            application_id = prepare(store, job['id'], 'fixture', root)

            original = (f'Alex Fixture\nalex@example.test\n'
                        f'Summary\nSource summary text.\n{excluded_summary}\n'
                        f'Skills\nPython; SQL\nResponsibilities\n{target_duty}.\n{excluded_duty}\n').encode('utf-8')
            uploaded = save_resume(store, root, 'fixture', 'uploaded-original.txt', 'text/plain', original)

            store.engine.dispose()
            store = Store(database_url)
            self.assertEqual(store.application(application_id)['id'], application_id)
            self.assertEqual(store.profile('fixture'), profile_before)

            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            app = create_app(store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0',
                             operation_mode='FIXTURE')
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Uploaded-comparison API did not start after Store reopen')
                    time.sleep(.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(accept_downloads=True)
                    context.set_default_timeout(5000)
                    external = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.goto(origin, wait_until='domcontentloaded')
                    page.get_by_label('Active profile', exact=True).select_option('fixture')
                    job_card = page.get_by_role('button').filter(
                        has=page.get_by_role('heading', name=job['title'], exact=True))
                    expect(job_card).to_be_visible()
                    job_card.click()
                    page.get_by_label('Order approved skills for this job', exact=True).check()
                    with page.expect_response(lambda response: response.url == origin + '/applications' and
                                              response.request.method == 'POST') as event:
                        page.get_by_role('button', name='Prepare documents', exact=True).click()
                    operation = event.value.json()
                    with patch.dict('os.environ', MJOBS_DATABASE_URL=database_url,
                                    MJOBS_MOCK_ORIGIN='http://127.0.0.1:1', MJOBS_OUTPUT_DIR=str(root),
                                    MJOBS_OPERATION_MODE='FIXTURE'):
                        self.assertEqual(execute_operation(operation['operation_id'])['status'], 'COMPLETED')

                    generated = next(row for row in application_documents(store, application_id)
                                     if row['kind'] == 'resume' and row['version'] == 2)
                    self.assertEqual((generated['template'], generated['generation_model']),
                        ('approved-target-relevant-bullets-summary-skill-responsibility-and-bullet-emphasis-v5',
                         'deterministic-target-relevant-bullets-summary-skill-responsibility-order-v4'))
                    self.assertEqual(generated['generation_context']['bullet_adjustment']['selected_bullet_fact_ids'],
                                     ['duty-backup'])
                    self.assertEqual(generated['generation_context']['bullet_adjustment']['omitted_bullet_fact_ids'],
                                     ['duty-office'])
                    self.assertEqual(generated['generation_context']['summary_adjustment']['selected_summary_fact_ids'],
                                     ['summary-backup'])

                    resume = page.get_by_role('region', name='resume version 2', exact=True)
                    with page.expect_download() as download:
                        resume.get_by_role('link', name='Download TXT', exact=True).click()
                    tailored_text = Path(download.value.path()).read_text(encoding='utf-8')
                    self.assertIn(target_duty, tailored_text)
                    self.assertIn('SQL', tailored_text)
                    self.assertNotIn(excluded_duty, tailored_text)
                    self.assertNotIn(excluded_summary, tailored_text)
                    self.assertNotIn(invented_job_only_text, tailored_text)
                    self.assertNotIn('Built a lunar satellite telemetry platform.', tailored_text)
                    self.assertEqual(store.profile('fixture'), profile_before)

                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('navigation').get_by_role('button', name='Applications', exact=False).first.click()
                    page.locator('.job-card').filter(has_text=job['title']).first.click()
                    comparison = page.get_by_role('region', name='Compare base and tailored resumes', exact=True)
                    comparison.get_by_label('Base resume', exact=True).select_option('u:' + uploaded['revision_id'])
                    comparison.get_by_label('Tailored version', exact=True).select_option(generated['id'])
                    with page.expect_response(lambda response: response.url.startswith(
                            origin + f'/profiles/fixture/applications/{application_id}/uploaded-resume-comparison?')) as compared:
                        comparison.get_by_role('button', name='Compare selected resumes', exact=True).click()
                    self.assertEqual(compared.value.status, 200, compared.value.text())
                    self.assertEqual(compared.value.json()['section_availability'],
                                     {'summary_changes': True, 'bullet_changes': True})

                    result = comparison.get_by_role('region', name='Resume comparison result', exact=True)
                    expect(result.get_by_role('heading', name='Uploaded base · v1', exact=True)).to_be_visible()
                    expect(result).to_contain_text('Uploaded text is unreviewed.')
                    expect(result).to_contain_text('Unmatched text is not proof of semantic omission;')
                    expect(result.get_by_role('region', name='Summary changes', exact=True)).to_contain_text(
                        'Source summary text.')
                    expect(result.get_by_role('region', name='Summary changes', exact=True)).to_contain_text(
                        excluded_summary)
                    expect(result.get_by_role('region', name='Summary changes', exact=True)).to_contain_text(
                        target_duty)
                    expect(result.get_by_role('region', name='Bullet and fact changes', exact=True)).to_contain_text(
                        excluded_duty)
                    self.assertNotIn('Source section unavailable or ambiguous', result.inner_text())
                    omitted = result.get_by_role('region', name='Content omitted', exact=True)
                    self.assertNotIn('SQL', omitted.inner_text())
                    expect(omitted).to_contain_text(excluded_duty)
                    expect(omitted).to_contain_text(excluded_summary)
                    self.assertNotIn(invented_job_only_text, result.inner_text())
                    self.assertNotIn('Built a lunar satellite telemetry platform.', result.inner_text())
                    self.assertFalse(external)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

    def test_tampered_omitted_bullet_fingerprint_fails_closed_after_store_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'tampered-omitted-fingerprint.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode('FIXTURE')

            target_duty = 'Manage backup systems across regional offices and document recovery plans'
            omitted_duty = 'Maintained office printers and arranged supply deliveries.'
            omitted_summary = 'Experienced retail sales associate.'
            job_only_text = 'Lead nationwide lunar satellite telemetry.'
            unapproved_text = 'Built a lunar satellite telemetry platform.'
            uploaded_sentinel = 'Uploaded-only recovery sentinel.'
            facts = [
                dict(PROFILE['facts'][0], id='python', kind='skill', text='Python'),
                dict(PROFILE['facts'][1], id='sql', kind='skill', text='SQL'),
                dict(PROFILE['facts'][0], id='summary-backup', kind='summary',
                     text=f'Infrastructure administrator. {target_duty}.'),
                dict(PROFILE['facts'][0], id='summary-sales', kind='summary', text=omitted_summary),
                dict(PROFILE['facts'][0], id='duty-backup', kind='responsibility', text=target_duty + '.'),
                dict(PROFILE['facts'][0], id='duty-office', kind='responsibility', text=omitted_duty),
                dict(PROFILE['facts'][0], id='unapproved-telemetry', kind='project', text=unapproved_text,
                     verified=False, user_approved=False),
            ]
            store.save_profile(dict(PROFILE, facts=facts))
            profile_before = store.profile('fixture')
            with fixture_server() as fixture:
                discover(store, fixture, 'fixture')
            job = store.jobs()[0]
            description = (f'Responsibilities:\n- {target_duty}.\n'
                           f'Requirements:\n- {job_only_text}')
            with store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == job['id']).values(
                    skills=['Python'], description=description))
            application_id = prepare(store, job['id'], 'fixture', root)
            original = (f'Alex Fixture\nalex@example.test\nSummary\nSource summary text.\n{omitted_summary}\n'
                        f'Skills\nPython; SQL\nResponsibilities\n{target_duty}.\n'
                        f'{omitted_duty}\n{uploaded_sentinel}\n').encode('utf-8')
            uploaded = save_resume(store, root, 'fixture', 'tampered-original.txt', 'text/plain', original)
            prepare(store, job['id'], 'fixture', root, tailor_resume=True)

            generated = next(row for row in application_documents(store, application_id)
                             if row['kind'] == 'resume' and row['version'] == 2)
            self.assertEqual((generated['template'], generated['generation_model']),
                ('approved-target-relevant-bullets-summary-skill-responsibility-and-bullet-emphasis-v5',
                 'deterministic-target-relevant-bullets-summary-skill-responsibility-order-v4'))
            adjustment = dict(generated['generation_context']['bullet_adjustment'])
            self.assertEqual(adjustment['omitted_bullet_fact_ids'], ['duty-office'])
            fingerprints = dict(adjustment['source_fact_fingerprints'])
            original_fingerprint = fingerprints['duty-office']
            tampered_fingerprint = '0' * 64
            self.assertNotEqual(original_fingerprint, tampered_fingerprint)
            fingerprints['duty-office'] = tampered_fingerprint
            adjustment['source_fact_fingerprints'] = fingerprints
            generation_context = dict(generated['generation_context'])
            generation_context['bullet_adjustment'] = adjustment
            with store.engine.begin() as connection:
                connection.execute(update(m.documents).where(m.documents.c.id == generated['id']).values(
                    generation_context=generation_context))
            persisted = next(row for row in application_documents(store, application_id)
                             if row['id'] == generated['id'])
            self.assertEqual(persisted['generation_context']['bullet_adjustment']['source_fact_fingerprints'][
                'duty-office'], tampered_fingerprint)

            store.engine.dispose()
            store = Store(database_url)
            reopened = next(row for row in application_documents(store, application_id)
                            if row['id'] == generated['id'])
            self.assertEqual(reopened['generation_context']['bullet_adjustment']['source_fact_fingerprints'][
                'duty-office'], tampered_fingerprint)
            self.assertEqual(store.profile('fixture'), profile_before)

            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            app = create_app(store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0',
                             operation_mode='FIXTURE')
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Tampered-comparison API did not start after Store reopen')
                    time.sleep(.02)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    context.set_default_timeout(5000)
                    external = []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.goto(origin, wait_until='domcontentloaded')
                    page.get_by_label('Active profile', exact=True).select_option('fixture')
                    page.get_by_role('navigation').get_by_role('button', name='Applications', exact=False).first.click()
                    page.locator('.job-card').filter(has_text=job['title']).first.click()
                    comparison = page.get_by_role('region', name='Compare base and tailored resumes', exact=True)
                    comparison.get_by_label('Base resume', exact=True).select_option('u:' + uploaded['revision_id'])
                    comparison.get_by_label('Tailored version', exact=True).select_option(generated['id'])
                    with page.expect_response(lambda response: response.url.startswith(
                            origin + f'/profiles/fixture/applications/{application_id}/uploaded-resume-comparison?')) as compared:
                        comparison.get_by_role('button', name='Compare selected resumes', exact=True).click()
                    self.assertEqual(compared.value.status, 409, compared.value.text())
                    alert = comparison.get_by_role('alert')
                    expect(alert).to_contain_text('Comparison unavailable: Error: Original target-bullet adjustment context is unavailable')
                    expect(comparison.get_by_role('region', name='Resume comparison result', exact=True)).to_have_count(0)
                    comparison_text = comparison.inner_text()
                    self.assertNotIn(uploaded_sentinel, comparison_text)
                    self.assertNotIn(omitted_duty, comparison_text)
                    self.assertNotIn(omitted_summary, comparison_text)
                    self.assertNotIn(job_only_text, comparison_text)
                    self.assertNotIn(unapproved_text, comparison_text)
                    self.assertEqual(store.profile('fixture'), profile_before)
                    self.assertFalse(external)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
