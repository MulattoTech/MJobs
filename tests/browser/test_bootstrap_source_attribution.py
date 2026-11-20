"""Real cached React + loopback API after a controlled bootstrap source transition."""
from pathlib import Path
import hashlib
import pytest
import threading
import time

import uvicorn

from cli.acceptance_public_browser import run as observe_browser
from cli.acceptance_public_source import _public_jobs
from fixtures.browser_ports import bound_browser_socket
from tests.integration.test_bootstrap_source_attribution import attributed, distinct_attributed, selected_mock_application
from tests.integration.test_collection_inventory_proofs import staged


@pytest.mark.parametrize('scene', ['attributed', 'distinct_attributed'])
def test_react_preserves_aggregator_provenance_with_native_primary_values(request, scene):
    case = request.getfixturevalue(scene)
    baseline = selected_mock_application(case)
    source = next(row for row in case['request']('GET', '/sources')['sources'] if row['id'] == case['source_id'])
    parent = case['fixture'].parent_lineage['parent_operation_id']
    operation = case['request']('GET', '/operations/'+parent)
    profile = case['request']('GET', '/profiles/fixture')
    observations = _public_jobs(case['request'], source, operation,
        dict(job_id=baseline['selected_application_job_id'], profile_version=profile['version']),
        'fixture', 'https://remotive.com/api/remote-jobs')
    artifact = dict(result='PASS', **baseline, acceptance_run_id='controlled-bootstrap-browser',
        execution_scope='CONTROLLED_TRANSPORT', profile_version=profile['version'], provider='remotive',
        endpoint='https://remotive.com/api/remote-jobs', source_id=case['source_id'],
        observed_real_job_ids=[case['trigger']], observations=observations,
        collection_operation_id=parent, collection_request_id=operation['params']['request_id'])
    sock = bound_browser_socket()
    origin = 'http://127.0.0.1:'+str(sock.getsockname()[1])
    received_methods = []
    async def observed_api(scope, receive, send):
        if scope['type'] == 'http':
            received_methods.append(scope['method'])
        await case['app'](scope, receive, send)
    server = uvicorn.Server(uvicorn.Config(observed_api, log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    try:
        thread.start()
        deadline = time.monotonic()+10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, 'Owned loopback API did not start'
            time.sleep(.02)
        dist = Path(__file__).resolve().parents[2]/'apps/web/dist'
        if not (dist/'index.html').is_file():
            pytest.skip('Build apps/web before browser acceptance')
        assets = {path.relative_to(dist).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in dist.rglob('*') if path.is_file()}
        assert 1 <= len(assets) <= 32
        evidence = observe_browser(origin, baseline, artifact, runtime_identity={'frontend_assets': assets})
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive()
    assert evidence['result'] == 'PASS'
    assert evidence['baseline_readback']['unchanged'] is True
    assert evidence['observed_real_job_ids'] == [case['trigger']]
    row = evidence['observations'][0]
    assert row['title'] == 'Verified Native Platform Engineer'
    assert row['provider'] == row['primary_provider'] == 'greenhouse'
    assert row['primary_source_id'] != row['source_id'] == case['source_id']
    assert row['native_posting_id'] == str(case['native_id'])
    assert row['checks']['source_provider_visible'] is True
    assert row['checks']['snapshot_hash_visible'] is True
    assert row['checks']['canonical_job_id_bound_to_selected_detail_readback'] is True
    assert evidence['browser']['get_head_only'] and evidence['browser']['external_requests'] == 0
    assert set(received_methods) <= {'GET', 'HEAD'}
    # The ordinary UI records a view on selection. Observation's unchanged
    # GET/HEAD gate must block that write before it reaches the real API.
    assert evidence['browser']['blocked_write_attempts'] == 1
    assert evidence['browser']['blocked_write_paths'] == [dict(method='POST',
        path='/profiles/fixture/jobs/'+case['trigger']+'/view')]
    assert evidence['acceptance_observations'][0]['status'] == 'NOT_RUN'
