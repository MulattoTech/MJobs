"""Actual React source/job views for one controlled same-employer worker chain."""
from pathlib import Path
import hashlib
import threading
import time

import pytest
import uvicorn

from apps.api.main import create_app
from cli.acceptance_public_employer import capture, browser_projection
from cli.acceptance_public_browser import run as public_browser
from cli.acceptance_public_source import _public_jobs
from fixtures.browser_ports import bound_browser_socket
from tests.integration.test_acceptance_public_employer import employer


def test_same_employer_source_and_both_jobs_visible_without_writes(employer):
    from tests.integration.test_bootstrap_source_attribution import selected_mock_application
    fixture, read, source, initial, evidence = employer
    dist = Path(__file__).resolve().parents[2] / 'apps/web/dist/index.html'
    if not dist.is_file():
        pytest.skip('Build apps/web before browser acceptance')
    store = fixture.case.store
    baseline = selected_mock_application({'store': store, 'trigger': fixture.request['trigger_job_id']})
    before = store.application(baseline['application_id'])
    result = capture(read, evidence, source, initial)
    assert result['status'] == 'COMPLETE'
    from tests.unit.test_acceptance_employer_verification_opt_in import _controlled_ui_artifact
    baseline.update(_controlled_ui_artifact(profile=store.profile('fixture'), source=baseline['source'],
        run_id='controlled-employer-browser'))
    operation = read('GET', '/operations/' + fixture.parent_lineage['parent_operation_id'])
    observations = _public_jobs(read, source, operation,
        dict(job_id=baseline['selected_application_job_id'], profile_version=initial['profile_version']),
        'fixture', evidence['endpoint'])
    public = {**evidence, **baseline, 'result': 'PASS', 'provider': 'remotive',
        'observations': observations, 'observed_real_job_ids': [row['job_id'] for row in observations],
        'employer_verification_requested': True, 'employer_observation': result}
    app = create_app(store, 'http://127.0.0.1:9', fixture.output_dir, operation_mode='FIXTURE')
    methods = []
    async def observed_app(scope, receive, send):
        if scope['type'] == 'http':
            methods.append(scope['method'])
        await app(scope, receive, send)
    sock = bound_browser_socket()
    origin = 'http://127.0.0.1:' + str(sock.getsockname()[1])
    baseline['target_origin'] = origin
    baseline['employer_verification_opt_in']['target_origin'] = origin
    public['target_origin'] = origin
    server = uvicorn.Server(uvicorn.Config(observed_app, log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(.02)
        assets = {path.relative_to(dist.parent).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in dist.parent.rglob('*') if path.is_file()}
        browser_result = public_browser(origin, baseline, public, runtime_identity={'frontend_assets': assets})
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive()
    assert browser_result['employer_observation'] == browser_projection(result)
    assert all(browser_result['employer_observation']['checks'].values())
    assert result['public_acceptance'] == 'NOT_RUN'
    assert set(methods) <= {'GET', 'HEAD'} and browser_result['browser']['external_requests'] == 0
    assert browser_result['browser']['blocked_write_attempts'] == 3
    assert all(row['path'].endswith('/view') for row in browser_result['browser']['blocked_write_paths'])
    assert browser_result['acceptance_observations'][0]['status'] == 'NOT_RUN'
    assert browser_result['baseline_readback']['unchanged'] is True
    assert store.application(baseline['application_id']) == before
