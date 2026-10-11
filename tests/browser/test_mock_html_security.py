from urllib.parse import quote

import httpx
from playwright.sync_api import sync_playwright

from fixtures.mock_server import fixture_server


def test_untrusted_fixture_values_cannot_execute_in_chromium():
    payload = '<img src=x onerror=window.pwned=1>'
    with fixture_server() as origin, httpx.Client(base_url=origin) as client, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(origin + '/ats/apply/' + quote(payload, safe=''))
            assert page.locator('h1').inner_text() == 'Mock ATS application ' + payload
            assert page.locator('img, svg').count() == 0
            assert page.evaluate('window.pwned === undefined')

            # Render the actual multipart review response and confirmation
            # response, then inspect Chromium's parsed DOM and script state.
            response = client.post('/ats/review/' + quote('" onmouseover="window.pwned=1', safe=''),
                                   data={key: 'yes' for key in
                                         ('name', 'email', 'country', 'remote', 'reviewed')},
                                   files={'resume': ('resume.pdf', b'%PDF-test'),
                                          'cover': ('cover.pdf', b'%PDF-test')})
            assert response.status_code == 200
            page.set_content(response.text)
            assert page.locator('form').get_attribute('onmouseover') is None
            assert page.get_by_role('button', name='Submit mock application').count() == 1
            assert page.evaluate('window.pwned === undefined')

            response = client.post('/ats/submit/A', data={
                'application_id': '<svg onload=x>', 'confirmed': 'yes',
                'answer': 'Controlled fictional test application.'})
            assert response.status_code == 200
            page.set_content(response.text)
            assert page.locator('img, svg').count() == 0
            assert page.locator('h1').get_attribute('data-confirmation') == 'MOCK-<svg onload=x>'
            assert 'MOCK-<svg onload=x>' in page.locator('body').inner_text()
        finally:
            browser.close()
