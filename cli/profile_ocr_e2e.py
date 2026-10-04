"""Prove real local OCR and API review boundaries in an isolated app container."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect
import uvicorn

from apps.api.main import create_app
from fixtures.resume_scans import scanned_resume
from fixtures.browser_ports import bound_browser_socket
from packages.persistence.store import Store


def browser_review(store, root):
    """Use the built UI with a temporary API and the actual OCR tools."""
    app = create_app(store, 'http://127.0.0.1:1', root, 'redis://127.0.0.1:1/0', operation_mode='PERSONAL')
    sock = bound_browser_socket()
    origin = 'http://127.0.0.1:'+str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic()+15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('Isolated OCR browser API failed to start')
            time.sleep(.02)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context()
            external = []
            def local_only(route):
                if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                    external.append(route.request.url)
                    route.abort()
                else:
                    route.continue_()
            context.route('**/*', local_only)
            page = context.new_page()
            page.goto(origin)
            page.evaluate("localStorage.setItem('mjobs-profile','ocr-synthetic')")
            page.reload()
            page.get_by_role('button', name='Profile', exact=True).click()
            page.get_by_role('button', name='Extract and review', exact=True).first.click()
            review = page.get_by_role('region', name='Resume extraction review')
            expect(review.get_by_text('OCR text may contain mistakes.', exact=False)).to_be_visible(timeout=60000)
            expect(review.get_by_text('Unverified suggestion · confidence 70%', exact=True).first).to_be_visible()
            page.get_by_role('button', name='Add selected to profile editor', exact=True).click()
            page.get_by_role('button', name='Save profile', exact=True).click()
            expect(page.get_by_role('status').filter(has_text='Profile saved')).to_be_visible()
            page.reload()
            page.get_by_role('button', name='Profile', exact=True).click()
            expect(page.get_by_role('textbox', name='Fact text', exact=True).first).to_be_visible()
            facts = store.profile('ocr-synthetic')['facts']
            assert 'Python' in {fact['text'] for fact in facts}
            assert all(not f['verified'] and not f['user_approved'] and f['confidence'] == 70 for f in facts)
            assert not external, external
            assert store.count('applications') == 0
            context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def run():
    checks = []
    with tempfile.TemporaryDirectory(prefix='mjobs-ocr-acceptance-') as directory:
        root = Path(directory)
        store = Store(f'sqlite:///{(root/"test.db").as_posix()}')
        store.migrate()
        store.bind_workspace_mode('PERSONAL')
        profile = dict(id='ocr-synthetic', name='Example Applicant', email='example@example.invalid', facts=[], answers=[])
        try:
            with TestClient(create_app(store, 'http://127.0.0.1:1', root, operation_mode='PERSONAL')) as client:
                assert client.put('/profiles', json=profile).status_code == 200
                before = store.profile(profile['id'])
                for mixed in (False, True):
                    data = scanned_resume(mixed=mixed)
                    response = client.post('/profiles/ocr-synthetic/resumes', content=data,
                        headers={'X-Filename':'synthetic-scan.pdf', 'Content-Type':'application/pdf'})
                    assert response.status_code == 201, response.text
                    uploaded = response.json()
                    response = client.post('/profiles/ocr-synthetic/resumes/'+uploaded['id']+'/extract')
                    assert response.status_code == 200, response.text
                    result = response.json()
                    assert result['status'] == 'READY_FOR_REVIEW', result
                    # OCR is uncertain (this fixture can read SQL as SOL).
                    # Verify actual recognition, dates and review gates without
                    # silently correcting or treating imperfect text as truth.
                    assert 'Python' in {fact['text'] for fact in result['facts']}, result
                    assert any('2020 - 2024' in fact['text'] for fact in result['facts']), result
                    assert all(not f['verified'] and not f['user_approved'] and f['confidence'] <= 70
                               for f in result['facts']), result
                    if mixed:
                        assert result['contact']['email'] == profile['email'], result
                    assert store.profile(profile['id']) == before
                    assert client.get('/profiles/ocr-synthetic/resumes/'+uploaded['id']).content == data
                    checks.append({'document':'mixed native/scanned' if mixed else 'scanned',
                                   'result':'PASS','pending_fact_count':len(result['facts']),
                                   'synthetic_recognized_text':[f['text'] for f in result['facts']]})
            browser_review(store, root)
            checks.append({'document':'scanned resume browser review, explicit save and reload', 'result':'PASS'})
        finally:
            store.engine.dispose()
    return {'result':'PASS','timestamp':datetime.now(timezone.utc).isoformat(),
            'command':'python -m cli.profile_ocr_e2e','target':'Isolated Linux app image, real Poppler/Tesseract, SQLite API and Chromium UI',
            'checks':checks,'profile_mutated_by_extraction':False,'external_submission_enabled':False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args()
    evidence = run()
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(evidence, indent=2))
