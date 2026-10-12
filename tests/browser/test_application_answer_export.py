"""Browser acceptance for a reviewed answer export and durable download history."""
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
import uvicorn

from apps.api.main import create_app
from fixtures.browser_ports import bound_browser_socket
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless((ROOT / 'apps/web/dist/index.html').is_file(), 'Build apps/web before browser acceptance')
class ApplicationAnswerExportBrowserTests(unittest.TestCase):
    def test_opposite_polarity_answer_is_not_suggested_but_manual_review_remains_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root/'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            store.save_profile(dict(PROFILE, answers=[{
                'id': 'production-systems',
                'question': 'Have you supported production systems?',
                'text': 'I have supported synthetic production systems.',
                'source': 'Synthetic profile', 'verified': True,
                'user_approved': True, 'confidence': 95,
            }]))
            with fixture_server() as fixture:
                discover(store, fixture, 'fixture')
            prepare(store, store.jobs()[0]['id'], 'fixture', root)
            app = create_app(store, 'http://127.0.0.1:1', root,
                             'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated answer API did not start')
                    time.sleep(.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    external, errors = [], []

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload()
                    page.get_by_role('navigation', name='Main navigation').get_by_role('button', name='Applications', exact=False).click()
                    page.locator('.job-card').first.click()
                    page.get_by_role('button', name='Add question', exact=True).click()
                    page.get_by_label('Employer question').fill(
                        'Have you not supported production systems?')
                    self.assertEqual(page.get_by_role(
                        'button', name='Use saved answer (review required)', exact=True).count(), 0)
                    answer = 'I have not supported synthetic production systems.'
                    page.get_by_role('textbox', name='Answer', exact=True).fill(answer)
                    page.get_by_label('I reviewed this exact answer for this job').check()
                    export = page.get_by_role(
                        'button', name='Save reviewed answers and export TXT', exact=True)
                    expect(export).to_be_enabled()
                    with page.expect_response(lambda response: response.url.endswith('/answers/export')) as sent:
                        export.click()
                    self.assertEqual(sent.value.status, 201, sent.value.text)
                    self.assertEqual(sent.value.json()['external_submission'], 'OFF')
                    self.assertFalse(external)
                    self.assertFalse(errors, errors)
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

    def test_profile_scoped_confidence_threshold_controls_reuse_without_blocking_manual_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root/'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            store.save_profile(dict(PROFILE, answers=[{
                'id': 'languages', 'question': 'Which programming languages have you used?',
                'text': 'I used Python in a synthetic project.', 'source': 'Synthetic profile',
                'verified': True, 'user_approved': True, 'confidence': 85,
            }]))
            store.save_profile(dict(PROFILE, id='other', name='Other fixture',
                                    email='other@example.test', answers=[]))
            with fixture_server() as fixture:
                discover(store, fixture, 'fixture')
            job_id = store.jobs()[0]['id']
            prepare(store, job_id, 'fixture', root)
            prepare(store, job_id, 'other', root)
            app = create_app(store, 'http://127.0.0.1:1', root,
                             'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated confidence-threshold API did not start')
                    time.sleep(.02)
                outside, errors = [], []
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()

                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            outside.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin, wait_until='domcontentloaded')
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('navigation', name='Main navigation').get_by_role('button', name='Applications', exact=False).click()
                    page.locator('.job-card').first.click()
                    page.get_by_role('button', name='Add question', exact=True).click()
                    page.get_by_label('Employer question').fill('What coding languages have you worked with?')
                    expect(page.get_by_role('button', name='Use saved answer (review required)', exact=True)).to_be_visible()

                    def save_thresholds():
                        # HTTP completion alone does not prove React applied the policy.
                        save = page.get_by_role('button', name='Save answer thresholds', exact=True)
                        with page.expect_response(lambda response:
                            response.url.endswith('/application-answer-confidence-thresholds')
                            and response.request.method == 'PUT'
                        ) as saved:
                            save.click()
                        self.assertEqual(saved.value.status, 200)
                        policy = saved.value.json()['policy']
                        expect(save).to_be_disabled()
                        expect(page.get_by_text(
                            f"Current thresholds: answer {policy['minimum_saved_answer_confidence']}%; "
                            f"question match {round(policy['minimum_question_match_confidence'] * 100)}%. "
                            "Changes apply after saving. The supported range preserves the existing "
                            "80% answer and 75% question-match floors.", exact=True,
                        )).to_be_visible()

                    page.get_by_label('Minimum saved answer confidence').fill('86')
                    save_thresholds()
                    expect(page.get_by_role('status').filter(
                        has_text='Application answer thresholds saved for this profile')).to_be_visible()
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('navigation', name='Main navigation').get_by_role('button', name='Applications', exact=False).click()
                    page.locator('.job-card').first.click()
                    expect(page.get_by_label('Minimum saved answer confidence')).to_have_value('86')
                    page.get_by_role('button', name='Add question', exact=True).click()
                    page.get_by_label('Employer question').fill('What coding languages have you worked with?')
                    expect(page.get_by_role('button', name='Use saved answer (review required)', exact=True)).to_have_count(0)

                    page.get_by_label('Minimum saved answer confidence').fill('80')
                    page.get_by_label('Minimum question match confidence').fill('96')
                    save_thresholds()
                    # The paraphrase scores 95%; an exact question scores 100%.
                    expect(page.get_by_label('Employer question')).to_have_value('What coding languages have you worked with?')
                    expect(page.get_by_role('button', name='Use saved answer (review required)', exact=True)).to_have_count(0)
                    page.get_by_label('Employer question').fill('Which programming languages have you used?')
                    expect(page.get_by_role('button', name='Use saved answer (review required)', exact=True)).to_be_visible()
                    page.get_by_label('Employer question').fill('What coding languages have you worked with?')
                    expect(page.get_by_role('button', name='Use saved answer (review required)', exact=True)).to_have_count(0)

                    page.get_by_label('Minimum question match confidence').fill('75')
                    save_thresholds()
                    expect(page.get_by_role('button', name='Use saved answer (review required)', exact=True)).to_be_visible()
                    page.get_by_label('Active profile', exact=True).select_option('other')
                    page.locator('.job-card').first.click()
                    expect(page.get_by_label('Minimum saved answer confidence')).to_have_value('80')
                    expect(page.get_by_label('Minimum question match confidence')).to_have_value('75')

                    page.get_by_label('Active profile', exact=True).select_option('fixture')
                    page.locator('.job-card').first.click()
                    page.get_by_label('Minimum saved answer confidence').fill('100')
                    page.get_by_label('Minimum question match confidence').fill('100')
                    save_thresholds()
                    page.get_by_role('button', name='Add question', exact=True).click()
                    page.get_by_label('Employer question').fill('What did you learn from a synthetic project?')
                    page.get_by_label('Answer', exact=True).fill('I learned to test carefully.')
                    page.get_by_label('I reviewed this exact answer for this job').check()
                    with page.expect_response(lambda response: response.url.endswith('/answers/export')) as exported:
                        page.get_by_role('button', name='Save reviewed answers and export TXT', exact=True).click()
                    self.assertEqual(exported.value.status, 201)
                    self.assertEqual(exported.value.json()['external_submission'], 'OFF')
                    self.assertEqual(outside, [])
                    self.assertEqual(errors, [])
                    context.close()
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()
                store.engine.dispose()
            self.assertFalse(thread.is_alive(), 'Isolated confidence-threshold API did not stop')

    def test_avoid_terms_save_and_block_reviewed_answer_with_safe_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root/'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            store.save_profile(dict(PROFILE, answers=[]))
            with fixture_server() as fixture:
                discover(store, fixture, 'fixture')
            prepare(store, store.jobs()[0]['id'], 'fixture', root)
            app = create_app(store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated avoid-list API did not start')
                    time.sleep(.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context()
                    outside, errors = [], []
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            outside.append(route.request.url); route.abort()
                        else:
                            route.continue_()
                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload()
                    page.get_by_role('button', name='Profile', exact=True).click()
                    terms = page.get_by_label('Avoid terms in generated career content (one per line)')
                    terms.fill('North Star')
                    save = page.get_by_role('button', name='Save profile and answers', exact=True)
                    expect(save).to_be_enabled(timeout=10000)
                    save.click()
                    expect(page.get_by_role('status').filter(has_text='Reusable answers saved')).to_be_visible()
                    self.assertEqual(store.profile('fixture')['preferences']['avoid_terms'], ['North Star'])
                    page.get_by_role('navigation', name='Main navigation').get_by_role('button', name='Applications', exact=False).click()
                    page.locator('.job-card').first.click()
                    page.get_by_role('button', name='Add question', exact=True).click()
                    page.get_by_label('Employer question').fill('What did you learn?')
                    page.get_by_label('Answer', exact=True).fill('I supported North Star deployments.')
                    page.get_by_label('I reviewed this exact answer for this job').check()
                    page.get_by_role('button', name='Save reviewed answers and export TXT', exact=True).click()
                    alert = page.get_by_role('alert').filter(has_text='Action needs attention')
                    expect(alert).to_contain_text('North Star')
                    expect(alert).to_contain_text('application answer 1')
                    self.assertEqual(store.count('documents'), 2)
                    self.assertFalse(outside)
                    self.assertFalse(errors, errors)
                    context.close(); browser.close()
            finally:
                server.should_exit = True; thread.join(timeout=10); sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())

    def test_fuzzy_suggestion_requires_review_and_export_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root/'browser.db').as_posix()}")
            store.migrate()
            store.bind_workspace_mode('FIXTURE')
            store.save_profile(dict(PROFILE, answers=[]))
            with fixture_server() as fixture:
                discover(store, fixture, 'fixture')
            prepare(store, store.jobs()[0]['id'], 'fixture', root)
            app = create_app(store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0', operation_mode='FIXTURE')
            sock = bound_browser_socket()
            origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
            server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
            thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail('Isolated answer export API did not start')
                    time.sleep(.02)
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(accept_downloads=True)
                    external, errors, requests = [], [], []
                    def local_only(route):
                        if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                            external.append(route.request.url); route.abort()
                        else:
                            route.continue_()
                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.on('request', lambda request: requests.append((request.method, urlsplit(request.url).path)))
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(origin)
                    page.evaluate("localStorage.setItem('mjobs-profile','fixture')")
                    page.reload()
                    question = 'How many years of professional experience do you have?'
                    answer = 'Five years of user-confirmed experience.'
                    page.get_by_role('button', name='Profile', exact=True).click()
                    expect(page.get_by_role('button', name='Add answer', exact=True)).to_be_enabled(timeout=10000)
                    page.get_by_role('button', name='Add answer', exact=True).click()
                    answer_card = page.locator('.fact').last
                    answer_card.get_by_label('Question this answer addresses').fill(question)
                    answer_card.get_by_label('Answer text', exact=True).fill(answer)
                    answer_card.get_by_label('Provenance').fill('Reviewed synthetic profile')
                    answer_card.get_by_label('I reviewed and verified this answer').check()
                    answer_card.get_by_label('I approve exact reuse for this question').check()
                    page.get_by_role('button', name='Add answer', exact=True).click()
                    sensitive_card = page.locator('.fact').last
                    sensitive_card.get_by_label('Question this answer addresses').fill('What is your ethnic origin?')
                    sensitive_card.get_by_label('Answer text', exact=True).fill('Synthetic sensitive answer')
                    sensitive_card.get_by_label('Provenance').fill('Reviewed synthetic profile')
                    sensitive_card.get_by_label('I reviewed and verified this answer').check()
                    sensitive_card.get_by_label('I approve exact reuse for this question').check()
                    page.get_by_role('button', name='Add answer', exact=True).click()
                    conflict_card = page.locator('.fact').last
                    conflict_card.get_by_label('Question this answer addresses').fill('What are your conflicts of interest?')
                    conflict_card.get_by_label('Answer text', exact=True).fill('Synthetic conflict response')
                    conflict_card.get_by_label('Provenance').fill('Reviewed synthetic profile')
                    conflict_card.get_by_label('I reviewed and verified this answer').check()
                    conflict_card.get_by_label('I approve exact reuse for this question').check()
                    page.get_by_role('button', name='Save profile and answers', exact=True).click()
                    expect(page.get_by_role('status').filter(has_text='Reusable answers saved')).to_be_visible()
                    self.assertTrue(store.profile('fixture')['answers'][0]['user_approved'])
                    page.get_by_role('button', name='Profile', exact=True).click()
                    page.get_by_role('button', name='Add answer', exact=True).click()
                    itsm_card = page.locator('.fact').last
                    itsm_card.get_by_label('Question this answer addresses').fill(
                        'Which IT service management tools have you used?')
                    itsm_answer = 'Synthetic experience with a reviewed service platform.'
                    itsm_card.get_by_label('Answer text', exact=True).fill(itsm_answer)
                    itsm_card.get_by_label('Provenance').fill('Synthetic IT operations inventory')
                    itsm_card.get_by_label('I reviewed and verified this answer').check()
                    itsm_card.get_by_label('I approve exact reuse for this question').check()
                    page.get_by_role('button', name='Save profile and answers', exact=True).click()
                    expect(page.get_by_role('status').filter(has_text='Reusable answers saved')).to_be_visible()
                    page.get_by_role('button', name='Profile', exact=True).click()
                    page.get_by_role('button', name='Add answer', exact=True).click()
                    weekend_card = page.locator('.fact').last
                    weekend_card.get_by_label('Question this answer addresses').fill('Can you work weekends?')
                    weekend_answer = 'Yes, I am available for weekend shifts.'
                    weekend_card.get_by_label('Answer text', exact=True).fill(weekend_answer)
                    weekend_card.get_by_label('Provenance').fill('Synthetic availability profile')
                    weekend_card.get_by_label('I reviewed and verified this answer').check()
                    weekend_card.get_by_label('I approve exact reuse for this question').check()
                    page.get_by_role('button', name='Save profile and answers', exact=True).click()
                    expect(page.get_by_role('status').filter(has_text='Reusable answers saved')).to_be_visible()
                    self.assertNotIn(('POST', '/matches'), requests)
                    page.get_by_role('navigation', name='Main navigation').get_by_role('button', name='Applications', exact=False).click()
                    page.locator('.job-card').first.click()
                    page.get_by_role('button', name='Add question', exact=True).click()
                    page.get_by_label('Employer question').fill('What is your ethnic origin?')
                    self.assertEqual(page.get_by_role('button', name='Use saved answer (review required)', exact=True).count(), 0)
                    page.get_by_role('button', name='Remove question', exact=True).click()
                    page.get_by_role('button', name='Add question', exact=True).click()
                    page.get_by_label('Employer question').fill('What are your conflicts of interest?')
                    self.assertEqual(page.get_by_role('button', name='Use saved answer (review required)', exact=True).count(), 0)
                    page.get_by_role('button', name='Remove question', exact=True).click()
                    page.get_by_role('button', name='Add question', exact=True).click()
                    page.get_by_label('Employer question').fill(
                        'How many years of professional experience have you had?')
                    expect(page.get_by_text('Fuzzy wording match · match score 78%', exact=False)).to_be_visible()
                    expect(page.get_by_text('Saved question: How many years of professional experience do you have? · Provenance: Reviewed synthetic profile', exact=False)).to_be_visible()
                    expect(page.get_by_role('textbox', name='Answer', exact=True)).to_have_value('')
                    page.get_by_role('button', name='Use saved answer (review required)', exact=True).click()
                    expect(page.get_by_role('textbox', name='Answer', exact=True)).to_have_value(answer)
                    export = page.get_by_role('button', name='Save reviewed answers and export TXT', exact=True)
                    expect(export).to_be_disabled()
                    reviewed = page.get_by_label('I reviewed this exact answer for this job')
                    reviewed.check()
                    expect(export).to_be_enabled()
                    prompt = page.get_by_label('Employer question')
                    prompt.fill('How many years of professional experience have you gained?')
                    expect(reviewed).not_to_be_checked()
                    expect(export).to_be_disabled()
                    prompt.fill('How many years of professional experience have you had?')
                    page.get_by_role('button', name='Use saved answer (review required)', exact=True).click()
                    expect(reviewed).not_to_be_checked()
                    page.get_by_role('textbox', name='Answer', exact=True).fill('Edited synthetic answer')
                    expect(reviewed).not_to_be_checked()
                    expect(export).to_be_disabled()
                    page.get_by_role('textbox', name='Answer', exact=True).fill(answer)
                    page.get_by_role('button', name='Use saved answer (review required)', exact=True).click()
                    reviewed.check()
                    expect(export).to_be_enabled()
                    page.get_by_role('button', name='Add question', exact=True).click()
                    page.get_by_label('Employer question').last.fill('Which ITSM tools have you used?')
                    itsm_question = page.get_by_role('group', name='Question 2')
                    expect(itsm_question.get_by_text(
                        'Curated semantic synonym match (local rules) · match score 95% · saved-answer confidence 100%',
                        exact=False)).to_be_visible()
                    expect(itsm_question.get_by_text(
                        'Saved question: Which IT service management tools have you used? · Provenance: Synthetic IT operations inventory',
                        exact=False)).to_be_visible()
                    expect(itsm_question.get_by_role('textbox', name='Answer', exact=True)).to_have_value('')
                    page.get_by_role('button', name='Use saved answer (review required)', exact=True).last.click()
                    expect(itsm_question.get_by_role('textbox', name='Answer', exact=True)).to_have_value(itsm_answer)
                    itsm_review = page.get_by_label('I reviewed this exact answer for this job').last
                    itsm_review.check()
                    expect(export).to_be_enabled()
                    page.get_by_role('button', name='Add question', exact=True).click()
                    page.get_by_label('Employer question').last.fill('Are you available to work weekends?')
                    weekend_question = page.get_by_role('group', name='Question 3')
                    expect(weekend_question.get_by_text(
                        'Curated semantic synonym match (local rules) · match score 95% · saved-answer confidence 100%',
                        exact=False)).to_be_visible()
                    expect(weekend_question.get_by_text(
                        'Saved question: Can you work weekends? · Provenance: Synthetic availability profile',
                        exact=False)).to_be_visible()
                    expect(weekend_question.get_by_role('textbox', name='Answer', exact=True)).to_have_value('')
                    page.get_by_role('button', name='Use saved answer (review required)', exact=True).last.click()
                    expect(weekend_question.get_by_role('textbox', name='Answer', exact=True)).to_have_value(weekend_answer)
                    weekend_review = page.get_by_label('I reviewed this exact answer for this job').last
                    weekend_review.check()
                    expect(export).to_be_enabled()
                    export.click()
                    document = page.get_by_role('region', name='application-answer-export version 1', exact=True)
                    expect(document.get_by_text('Manually reviewed application answers', exact=False)).to_be_visible()
                    with page.expect_download() as downloaded:
                        document.get_by_role('link', name='Download TXT', exact=True).click()
                    first_bytes = Path(downloaded.value.path()).read_bytes()
                    self.assertIn(answer.encode(), first_bytes)
                    self.assertIn(itsm_answer.encode(), first_bytes)
                    self.assertIn(weekend_answer.encode(), first_bytes)
                    digest = hashlib.sha256(first_bytes).hexdigest()
                    self.assertEqual(store.count('documents'), 3)

                    page.reload()
                    page.get_by_role('navigation', name='Main navigation').get_by_role('button', name='Applications', exact=False).click()
                    page.locator('.job-card').first.click()
                    document = page.get_by_role('region', name='application-answer-export version 1', exact=True)
                    with page.expect_download() as downloaded_again:
                        document.get_by_role('link', name='Download TXT', exact=True).click()
                    self.assertEqual(hashlib.sha256(Path(downloaded_again.value.path()).read_bytes()).hexdigest(), digest)
                    self.assertFalse(external)
                    self.assertFalse(any('/submit' in path.lower() for _, path in requests))
                    self.assertFalse(errors, errors)
                    context.close(); browser.close()
            finally:
                server.should_exit = True; thread.join(timeout=10); sock.close()
                store.engine.dispose()
                self.assertFalse(thread.is_alive())
