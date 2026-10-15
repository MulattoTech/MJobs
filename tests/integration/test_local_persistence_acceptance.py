"""Real API/document/storage restart coverage on synthetic SQLite, not Compose proof."""
import json

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select

from apps.api.main import create_app
from cli import local_persistence_acceptance as persistence
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import start_attempt, finish_attempt
from services.applications.confirmation_recovery import reconcile_confirmation
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


def test_inventory_reads_actual_api_bytes_and_survives_store_reopen(tmp_path):
    url = 'sqlite:///' + (tmp_path / 'fixture.db').as_posix()
    (tmp_path / '.env').write_text('SYNTHETIC_PRIVATE_VALUE=never-ship-this\n')
    (tmp_path / 'compose.yml').write_text('synthetic configuration identity')
    store = Store(url)
    store.migrate()
    store.bind_workspace_mode('FIXTURE')
    store.save_profile(PROFILE)
    with fixture_server() as origin:
        try:
            discover(store, origin, PROFILE['id'])
            application_id = prepare(store, store.jobs(PROFILE['id'])[0]['id'], PROFILE['id'], tmp_path)
            application = store.application(application_id)
            with store.engine.connect() as connection:
                route = dict(connection.execute(select(m.routes).where(
                    m.routes.c.id == application['route_id'])).mappings().one())
                documents = list(connection.execute(select(m.documents).where(
                    m.documents.c.application_id == application_id)).mappings())
            attempt = start_attempt(store, application, route, documents, submit=True,
                                    request_id='synthetic-persistence-attempt')
            store.transition(application_id, ['PREPARED'], 'SUBMITTING')
            # Exercise the fictional provider HTTP interface without a browser.
            # This test does not certify browser execution or worker recovery.
            with httpx.Client(trust_env=False) as provider:
                response = provider.post(origin + '/ats/submit/' + route['requisition'], data={
                    'application_id': application_id,
                    'answer': 'Controlled fictional test application.', 'confirmed': 'yes'})
                response.raise_for_status()
            store.transition(application_id, ['SUBMITTING'], 'SUBMISSION_UNCONFIRMED')
            finish_attempt(store, attempt['id'], store.application(application_id))
            reconcile_confirmation(store, PROFILE['id'], application_id, origin,
                                   'synthetic-confirmation-read', operation_mode='FIXTURE')

            def observe(client):
                def response(path):
                    result = client.get(path)
                    assert result.is_success, str(result.status_code)
                    return result
                return persistence.collect(lambda p: response(p).json(), lambda p: response(p).content,
                                           PROFILE['id'], application_id, root=tmp_path)

            with TestClient(create_app(store, origin, tmp_path, operation_mode='FIXTURE')) as client:
                upload = client.post('/profiles/fixture/resumes', content=b'Synthetic original resume bytes',
                                     headers={'X-Filename': 'synthetic.txt', 'Content-Type': 'text/plain'})
                assert upload.status_code == 201
                sources = client.get('/company-sources').json()['company_sources']
                assert sources
                source_id = sources[0]['id']
                assert client.put('/company-sources/' + source_id + '/pin', json={'pinned': True}).status_code == 200
                before = observe(client)
            store.engine.dispose()
            store = Store(url)
            with TestClient(create_app(store, origin, tmp_path, operation_mode='FIXTURE')) as client:
                after = observe(client)
                assert persistence.compare(before, after)['result'] == 'PASS'
                assert all(row['count'] > 0 for row in after['domains'].values())
                serialized = json.dumps(after)
                assert 'never-ship-this' not in serialized and PROFILE['email'] not in serialized
                assert 'Synthetic original' not in serialized and application_id not in serialized
                assert client.put('/company-sources/' + source_id + '/pin', json={'pinned': False}).status_code == 200
                with pytest.raises(ValueError, match='company_source_catalog'):
                    persistence.compare(before, observe(client))
                (tmp_path / '.env').write_text('SYNTHETIC_PRIVATE_VALUE=changed\n')
                changed = observe(client)
                assert changed['domains']['configuration'] != before['domains']['configuration']
                # Loss/corruption of actual original bytes cannot become a PASS.
                (tmp_path / 'uploads' / (upload.json()['id'] + '.txt')).write_bytes(b'corrupted')
                with pytest.raises(AssertionError, match='409'):
                    observe(client)
        finally:
            store.engine.dispose()
