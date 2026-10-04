"""Real offline OCR comparison of immutable synthetic uploaded originals."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from fixtures.resume_scans import scanned_resume
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents


def run():
    checks = []
    with tempfile.TemporaryDirectory(prefix='mjobs-uploaded-comparison-') as directory:
        root = Path(directory)
        store = Store(f'sqlite:///{(root/"test.db").as_posix()}')
        store.migrate()
        store.bind_workspace_mode('FIXTURE')
        profile_id = 'ocr-comparison'
        store.save_profile(dict(id=profile_id, name='Example Applicant', email='example@example.invalid',
            answers=[], facts=[dict(id='python', text='Python', kind='skill', source='synthetic-fixture',
                verified=True, user_approved=True, confidence=100)]))
        try:
            with fixture_server() as origin:
                discover(store, origin, profile_id)
            application_id = prepare(store, store.jobs()[0]['id'], profile_id, root, tailor_resume=True)
            documents = application_documents(store, application_id)
            target = next(row for row in documents if row['kind'] == 'resume')
            before = store.profile(profile_id)
            application_before = store.application(application_id)
            with TestClient(create_app(store, 'http://127.0.0.1:1', root, operation_mode='FIXTURE')) as client:
                for mixed in (False, True):
                    original = scanned_resume(mixed=mixed)
                    response = client.post(f'/profiles/{profile_id}/resumes', content=original,
                        headers={'X-Filename':'synthetic-scan.pdf', 'Content-Type':'application/pdf'})
                    assert response.status_code == 201, response.text
                    upload = response.json()
                    response = client.get(f'/applications/{application_id}/uploaded-resume-comparison',
                        params={'base_revision_id':upload['revision_id'], 'tailored_document_id':target['id']})
                    assert response.status_code == 200, response.text
                    result = response.json()
                    assert result['base']['source_kind'] == 'UPLOADED'
                    assert result['base']['id'] == upload['revision_id']
                    assert result['tailored']['id'] == target['id'] and result['tailored']['version'] == target['version']
                    assert 'Python' in result['tailored']['text']
                    assert any(row['text'] == 'Python' for row in result['emphasized_skills']), result
                    assert all(row['text'] != 'Python' for row in result['omitted']), result
                    assert result['base']['extraction']['used_ocr'] is True, result
                    assert any('OCR text may contain mistakes' in value for value in result['base']['extraction']['warnings'])
                    assert 'Python' in result['base']['text'], result
                    assert result['base']['sha256'] == hashlib.sha256(original).hexdigest()
                    assert client.get(f'/profiles/{profile_id}/resumes/'+upload['id']).content == original
                    assert store.profile(profile_id) == before
                    assert store.application(application_id) == application_before
                    assert application_documents(store, application_id) == documents
                    assert client.get('/health').json()['external_submission_enabled'] is False
                    checks.append({'document':'mixed native/scanned' if mixed else 'scanned', 'result':'PASS',
                        'original_revision':upload['revision_id'], 'original_sha256':result['base']['sha256'],
                        'ocr_uncertainty_visible':True, 'profile_and_application_unchanged':True})
        finally:
            store.engine.dispose()
    return {'result':'PASS', 'timestamp':datetime.now(timezone.utc).isoformat(),
        'command':'python -m cli.uploaded_resume_comparison_e2e',
        'target':'Isolated Linux app image, real Poppler/Tesseract and SQLite API',
        'checks':checks, 'external_submission_enabled':False}


if __name__ == '__main__':
    print(json.dumps(run(), indent=2))
