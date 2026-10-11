from html.parser import HTMLParser
from urllib.parse import quote, urlsplit, unquote

import pytest
import httpx
from fastapi.testclient import TestClient

from fixtures.mock_server import create_mock_app, fixture_server


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


PAYLOADS = ['<img src=x onerror=window.pwned=1>', '" autofocus onfocus="window.pwned=1',
            "A?challenge=true&dynamic=unknown", "café&company"]


def assert_inert(text):
    markup = Markup(text)
    assert not any(tag in {'img', 'svg', 'script'} for tag, _ in markup.elements)
    assert not any(key.lower().startswith('on') for _, attrs in markup.elements for key in attrs)
    return markup


def review(client, requisition):
    return client.post('/ats/review/' + quote(requisition, safe=''),
                       data={key: 'yes' for key in ('name', 'email', 'country', 'remote', 'reviewed')},
                       files={'resume': ('resume.pdf', b'%PDF-test'),
                              'cover': ('cover.pdf', b'%PDF-test')})


@pytest.mark.parametrize('requisition', PAYLOADS)
@pytest.mark.parametrize('phase', ['application', 'auth', 'review'])
def test_requisition_is_data_in_markup_and_route(requisition, phase):
    with TestClient(create_mock_app()) as client:
        if phase == 'review':
            response = review(client, requisition)
            target = '/ats/submit/'
        else:
            suffix = '?auth_expired=true' if phase == 'auth' else ''
            response = client.get('/ats/apply/' + quote(requisition, safe='') + suffix)
            target = '/ats/login/' if phase == 'auth' else '/ats/review/'
        assert response.status_code == 200
        markup = assert_inert(response.text)
        links = [attrs.get('action') or attrs.get('href') for _, attrs in markup.elements
                 if 'action' in attrs or 'href' in attrs]
        assert len(links) == 1
        parsed = urlsplit(links[0])
        assert not parsed.query and not parsed.fragment
        assert unquote(parsed.path) == target + requisition


@pytest.mark.parametrize('application_id', ['<svg onload=x>', '" onmouseover=x', 'A&"B'])
def test_confirmation_is_inert_and_receipt_remains_exact(application_id):
    with TestClient(create_mock_app()) as client:
        response = client.post('/ats/submit/A', data={
            'application_id': application_id, 'answer': 'Controlled fictional test application.',
            'confirmed': 'yes'})
        assert response.status_code == 200
        markup = assert_inert(response.text)
        assert dict(markup.elements[0][1])['data-confirmation'] == 'MOCK-' + application_id[:16]
        receipt = client.get('/ats/receipts/' + quote(application_id, safe='')).json()
        assert receipt['reference_number'] == 'MOCK-' + application_id[:16]
        assert receipt['submission_count'] == 1


def test_normal_review_and_confirmation_keep_fixture_contract():
    with TestClient(create_mock_app()) as client:
        application = client.get('/ats/apply/A?dynamic=known')
        markup = Markup(application.text)
        assert [attrs['action'] for tag, attrs in markup.elements if tag == 'form'] == [
            '/ats/review/A?dynamic=known']
        assert len([tag for tag, _ in markup.elements if tag == 'script']) == 1
        assert review(client, 'A').status_code == 200
        for _ in range(2):
            response = client.post('/ats/submit/A', data={
                'application_id': 'fixture-123', 'answer': 'Controlled fictional test application.',
                'confirmed': 'yes'})
            assert 'Application reference: MOCK-fixture-123' in response.text
        assert client.get('/ats/receipts/fixture-123').json()['submission_count'] == 1


def test_percent_encoded_requisition_keeps_identity_over_real_http():
    # TestClient decodes percent escapes twice; the real HTTP boundary must
    # preserve a literal percent escape across rendered route transitions.
    with fixture_server() as origin, httpx.Client(base_url=origin) as client:
        for phase, target in [('application', '/ats/review/'), ('auth', '/ats/login/'),
                              ('review', '/ats/submit/')]:
            requisition = 'A%26B'
            response = (review(client, requisition) if phase == 'review' else
                        client.get('/ats/apply/' + quote(requisition, safe='') +
                                   ('?auth_expired=true' if phase == 'auth' else '')))
            assert response.status_code == 200
            markup = assert_inert(response.text)
            link = next(attrs.get('action') or attrs.get('href') for _, attrs in markup.elements
                        if 'action' in attrs or 'href' in attrs)
            parsed = urlsplit(link)
            assert not parsed.query and not parsed.fragment
            assert unquote(parsed.path) == target + requisition


def test_restored_receipt_reference_is_encoded_at_output(tmp_path):
    import json
    reference = '<svg onload=window.pwned=1>"&'
    receipt_file = tmp_path / 'receipts.json'
    receipt_file.write_text(json.dumps({'fixture-123': {'reference_number': reference}}))
    with TestClient(create_mock_app(receipt_file)) as client:
        response = client.post('/ats/submit/A', data={
            'application_id': 'fixture-123', 'answer': 'Controlled fictional test application.',
            'confirmed': 'yes'})
        assert response.status_code == 200
        markup = assert_inert(response.text)
        assert markup.elements[0][1]['data-confirmation'] == reference
