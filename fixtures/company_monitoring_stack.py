"""Opt-in, synthetic-only company-monitoring restart scenario.

Production Compose/target/security functions are reused unchanged. Nothing in
this module runs on the normal application entrypoints. STK01 execution needs
its explicit resource grant, frozen source and a common outer watchdog.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tarfile
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from cli import compose_recovery as recovery

ROOT = Path(__file__).resolve().parents[1]
OVERRIDE = ROOT / 'infra/compose.monitoring-recovery.yml'
IMAGE_PINS = {
    'runtime': 'sha256:af02cdeaa62f8fd3a8ba079ea4616a0e7912887d283ae58908cf69298157de98',
    'postgres': 'sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f',
    'redis': 'sha256:c6eabf748fc7a61dbb5a705c78bcf3d6377b1127a97d0ce965c11c44ba46896f',
}
RUNTIME = set(recovery.RECOVERY_RESTART_SERVICES) - {'postgres', 'redis'}
SESSION = 'mjobs-85'
ALLOWED_PATHS = {'/', '/robots.txt', '/careers', '/jobs.json', '/jobs/one', '/jobs/two'}


class FixtureError(RuntimeError):
    """A fixture cannot establish its safety or acceptance oracle."""


def remaining(deadline, *, now=None, reserve=30, ceiling=90):
    now = time.monotonic() if now is None else now
    if not math.isfinite(deadline) or deadline - now <= reserve:
        raise FixtureError('STK01_DEADLINE')
    return min(ceiling, deadline - now - reserve)


def origin_path(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'employer.example'
            or parsed.query or parsed.fragment or parsed.path not in ALLOWED_PATHS
            or any(ord(char) < 33 for char in url)
            or url != 'https://employer.example' + parsed.path):
        raise FixtureError('STK01_ORIGIN_REJECTED')
    return parsed.path


def validate_fixture_mode(env=None):
    env = os.environ if env is None else env
    try:
        recovery.validate_project_name(env.get('MJOBS_STK01_PROJECT'))
    except ValueError as error:
        raise FixtureError('STK01_PROJECT_REJECTED') from error
    if (env.get('MJOBS_STK01_ENABLE') != '1' or env.get('MJOBS_OPERATION_MODE') != 'PERSONAL'
            or env.get('MJOBS_COMPOSE_MOCK') != '0'):
        raise FixtureError('STK01_SYNTHETIC_PERSONAL_GATE_REQUIRED')


def resource_owned(labels, project):
    recovery.validate_project_name(project)
    if (labels.get('ao.session') != SESSION or labels.get('ao.run') != project
            or labels.get('com.docker.compose.project') != project
            or labels.get('ao.spare') not in (None, 'false')):
        raise FixtureError('STK01_RESOURCE_NOT_OWNED')



def validate_runtime_container(obj, name, project):
    resource_owned(obj['Config']['Labels'], project)
    expected = IMAGE_PINS['postgres' if name == 'postgres' else 'redis' if name == 'redis' else 'runtime']
    if obj['Config']['Image'] != expected or any(m['Type'] != 'volume' for m in obj['Mounts']):
        raise FixtureError('STK01_RUNTIME_IMAGE_OR_MOUNT_CHANGED')
    if name not in {'postgres', 'redis'}:
        env = dict(item.split('=', 1) for item in obj['Config']['Env'])
        validate_fixture_mode(env)
    if name in RUNTIME | {'migrate'}:
        sources = [m for m in obj['Mounts'] if m['Destination'] == '/workspace']
        if (obj['Config']['WorkingDir'] != '/workspace' or len(sources) != 1
                or sources[0]['Name'] != project+'_stk-source' or sources[0]['RW']):
            raise FixtureError('STK01_RUNTIME_SOURCE_CHANGED')

def validate_configuration(config, project, port):
    recovery.validate_project_name(project)
    recovery.validate_port(port)
    expected = set(recovery.RECOVERY_RESTART_SERVICES) | {'migrate', 'source-init'}
    if set(config['services']) != expected:
        raise FixtureError('STK01_SERVICE_SET_CHANGED')
    cpu, memory = 0.0, 0
    for name, service in config['services'].items():
        pin = IMAGE_PINS['postgres' if name == 'postgres' else 'redis' if name == 'redis' else 'runtime']
        if service.get('image') != pin or service.get('build') or service.get('pull_policy') != 'never':
            raise FixtureError('STK01_IMAGE_OR_BUILD_UNSAFE')
        labels = service.get('labels', {})
        if labels.get('ao.session') != SESSION or labels.get('ao.run') != project:
            raise FixtureError('STK01_SERVICE_NOT_OWNED')
        cap = float(service.get('cpus', 99))
        size = int(service.get('mem_limit', 99 * 1024**3))
        if (not math.isfinite(cap) or not 0 < cap <= (.5 if name == 'postgres' else .25)
                or not 0 < size <= 512 * 1024**2 or service.get('privileged')
                or service.get('network_mode') or service.get('pid') or service.get('cap_add')):
            raise FixtureError('STK01_PRIVILEGE_OR_RESOURCE_UNSAFE')
        cpu += cap
        memory += size
        if name not in {'postgres', 'redis'}:
            env = service.get('environment', {})
            if env.get('MJOBS_OPERATION_MODE') != 'PERSONAL' or str(env.get('MJOBS_COMPOSE_MOCK')) != '0':
                raise FixtureError('STK01_MODE_MISMATCH')
            if any(value for key, value in env.items() if key.endswith('_API_KEY')):
                raise FixtureError('STK01_PROVIDER_CREDENTIAL_REJECTED')
        ports = service.get('ports', [])
        if name == 'api':
            if (len(ports) != 1 or ports[0].get('host_ip') != '127.0.0.1'
                    or str(ports[0].get('published')) != str(port) or ports[0].get('target') != 8000):
                raise FixtureError('STK01_API_PORT_UNSAFE')
        elif ports:
            raise FixtureError('STK01_UNEXPECTED_HOST_PORT')
        if name in RUNTIME | {'migrate'}:
            source = [v for v in service.get('volumes', []) if v.get('target') == '/workspace']
            if (len(source) != 1 or source[0].get('source') != 'stk-source'
                    or source[0].get('read_only') is not True or service.get('working_dir') != '/workspace'):
                raise FixtureError('STK01_SOURCE_NOT_READ_ONLY')
        for volume in service.get('volumes', []):
            if volume.get('type') != 'volume' or volume.get('source') not in config.get('volumes', {}):
                raise FixtureError('STK01_BIND_OR_FOREIGN_VOLUME')
    if cpu > 3.00001 or memory > 5.25 * 1024**3:
        raise FixtureError('STK01_RESOURCE_CAP_EXCEEDED')
    for collection in ['networks', 'volumes']:
        for value in config[collection].values():
            labels = value.get('labels', {})
            if (value.get('external') or labels.get('ao.session') != SESSION
                    or labels.get('ao.run') != project):
                raise FixtureError('STK01_FOREIGN_RESOURCE')
            if collection == 'networks' and value.get('internal') is not True:
                raise FixtureError('STK01_NETWORK_EGRESS')
    return {'cpu_cap': cpu, 'memory_cap_bytes': memory, 'service_count': len(expected)}


def source_files(root, revision='HEAD'):
    """Hash Git-exported bytes; never include an ambient .env or worktree cache."""
    data = subprocess.check_output(['git', 'archive', '--format=tar', revision], cwd=root)
    result = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            path = Path(member.name)
            if (path.is_absolute() or '..' in path.parts or member.issym() or member.islnk()
                    or path.name in {'.env', '.env.local'} or '.git' in path.parts):
                raise FixtureError('STK01_SOURCE_ARCHIVE_UNSAFE')
            if member.isfile():
                result[member.name] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
    return result


def loopback_transport(client, url, deadline):
    """Only the exact fictional origin is mapped; no production policy is edited."""
    import httpx
    path = origin_path(url)
    client._check()
    client._budget()
    response = httpx.get('http://mock:8090/monitor' + path,
                         timeout=remaining(deadline, reserve=0, ceiling=5),
                         follow_redirects=False, trust_env=False)
    return httpx.Response(response.status_code, content=response.content,
                          headers=response.headers, request=httpx.Request('GET', url))


def mock_main():
    validate_fixture_mode()
    from fixtures.mock_server import create_mock_app
    from fastapi.responses import HTMLResponse
    import uvicorn
    state_path = Path('/data/monitoring-feed.json')
    lock = threading.Lock()
    if not state_path.exists():
        state_path.write_text(json.dumps({'revision': 1, 'feed_gets': 0}), encoding='utf-8')
    app = create_mock_app('/data/receipts.json')

    @app.get('/monitor/robots.txt', response_class=HTMLResponse)
    def robots():
        return 'User-agent: *\nAllow: /'

    @app.get('/monitor/', response_class=HTMLResponse)
    def home():
        return '<h1>Synthetic Monitoring Employer</h1><a rel="careers" href="/careers">Careers</a>'

    @app.get('/monitor/careers', response_class=HTMLResponse)
    def careers():
        return '<link rel="alternate" type="application/feed+json" href="/jobs.json">'

    @app.get('/monitor/jobs.json')
    def feed():
        with lock:
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state['feed_gets'] += 1
            state_path.write_text(json.dumps(state), encoding='utf-8')
        return {'version': 'https://jsonfeed.org/version/1.1', 'items': [
            {'id': key, 'title': title, 'content_text': 'Python SQL synthetic fixture role',
             'url': 'https://employer.example/jobs/' + key}
            for key, title in [('one', 'Python Engineer'), ('two', 'Python Reliability Engineer')][:state['revision']]]}

    @app.get('/monitor/jobs/{key}', response_class=HTMLResponse)
    def posting(key: str):
        if key not in {'one', 'two'}:
            return HTMLResponse('Unknown fixture posting', status_code=404)
        title = 'Python Engineer' if key == 'one' else 'Python Reliability Engineer'
        value = {'@context': 'https://schema.org', '@type': 'JobPosting', 'title': title,
                 'identifier': {'name': 'Synthetic Monitoring Employer', 'value': key},
                 'hiringOrganization': {'@type': 'Organization', 'name': 'Synthetic Monitoring Employer',
                                        'sameAs': 'https://employer.example/'},
                 'url': 'https://employer.example/jobs/' + key, 'description': 'Python SQL synthetic fixture role'}
        return '<script type="application/ld+json">' + json.dumps(value) + '</script>'

    @app.get('/_test/monitoring')
    def state():
        return json.loads(state_path.read_text(encoding='utf-8'))

    @app.post('/_test/monitoring/revision-two')
    def second_feed():
        with lock:
            value = json.loads(state_path.read_text(encoding='utf-8'))
            value['revision'] = 2
            state_path.write_text(json.dumps(value), encoding='utf-8')
        return {'revision': 2}

    uvicorn.run(app, host='0.0.0.0', port=8090, log_level='error')


def worker_main():
    validate_fixture_mode()
    from unittest.mock import patch
    from services.company_discovery.public_client import PublicCompanyClient
    from workers.company_discovery_worker import main
    with patch.object(PublicCompanyClient, '_request_once', loopback_transport):
        main()



def synthetic_environment(artifacts, project, port, head, archive_hash):
    """OS executable paths only; never forward user provider/database settings."""
    from cryptography.fernet import Fernet
    env = {key: os.environ[key] for key in (
        'PATH', 'SYSTEMROOT', 'SystemDrive', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE',
        'LOCALAPPDATA', 'APPDATA', 'ProgramFiles', 'ProgramData') if key in os.environ}
    client = artifacts / 'docker-client'
    client.mkdir(exist_ok=True)
    if list(client.iterdir()):
        raise FixtureError('STK01_DOCKER_CONFIG_NOT_EMPTY')
    env.update(DOCKER_CONFIG=str(client), PYTHONUTF8='1', MJOBS_STK01_PROJECT=project,
               COMPOSE_PROJECT_NAME=project, MJOBS_PORT=str(port),
               POSTGRES_PASSWORD=secrets.token_hex(24),
               MJOBS_CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode(),
               MJOBS_STK01_DB='mjobs_test_monitor_' + project.rsplit('-', 1)[1],
               MJOBS_STK01_SOURCE_HEAD=head, MJOBS_STK01_SOURCE_SHA256=archive_hash,
               MJOBS_STK01_ENABLE='1', MJOBS_OPERATION_MODE='PERSONAL', MJOBS_COMPOSE_MOCK='0')
    return env


def configuration_files():
    for name in ['.env', '.env.local']:
        if (ROOT / name).exists():
            raise FixtureError('STK01_AMBIENT_ENV_REJECTED')
    return recovery.resolve_compose_files(os.pathsep.join((str(ROOT / 'compose.yml'), str(OVERRIDE))))


def owned_resources(project, deadline):
    """Read only this random project, then verify every returned ownership label."""
    result = {}
    for kind, listing in [('container', ['ps', '-a']), ('volume', ['volume', 'ls']),
                          ('network', ['network', 'ls'])]:
        args = ['docker', *listing, '--filter', 'label=com.docker.compose.project=' + project,
                '--format', '{{.ID}}' if kind != 'volume' else '{{.Name}}']
        ids = recovery._run_bounded(args, timeout=remaining(deadline, reserve=5, ceiling=10),
                                    action='Read owned STK01 resource IDs').split()
        objects = []
        if ids:
            command = ['docker', 'inspect', *ids] if kind == 'container' else ['docker', kind, 'inspect', *ids]
            objects = json.loads(recovery._run_bounded(command,
                timeout=remaining(deadline, reserve=5, ceiling=10), action='Inspect owned STK01 resources'))
            for value in objects:
                labels = value['Config']['Labels'] if kind == 'container' else value['Labels']
                resource_owned(labels, project)
        result[kind] = objects
    return result


def cleanup_stack(project, port, config_files, deadline):
    """Never remove an unchecked container, network or volume, including on failure."""
    resources = owned_resources(project, deadline)
    if any(resources.values()):
        command = ['docker', 'compose', *[part for path in config_files for part in ('--file', str(path))],
                   '--project-directory', str(ROOT), '--profile', 'fixture', '--project-name', project,
                   'down', '--volumes', '--timeout', '5']
        recovery._run_bounded(command, timeout=remaining(deadline, reserve=15, ceiling=60),
                              action='Remove only verified STK01 project')
    residual = owned_resources(project, deadline)
    return {kind: [obj.get('Id', obj.get('Name')) for obj in rows] for kind, rows in residual.items()}


def run_stack(artifacts):
    """One fixed monitoring/persistence scenario; outer owned watchdog is mandatory."""
    import httpx
    from unittest.mock import patch
    from tests.integration.test_spine import PROFILE

    artifacts = Path(artifacts).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    project = recovery.validate_project_name(os.environ['MJOBS_STK01_PROJECT'])
    port = recovery.validate_port(int(os.environ['MJOBS_STK01_PORT']))
    deadline = float(os.environ['MJOBS_STK01_COMMON_DEADLINE'])
    if remaining(deadline, reserve=0, ceiling=1000) > 720:
        raise FixtureError('STK01_DEADLINE_TOO_LONG')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if head != os.environ['MJOBS_STK01_APPROVED_HEAD'] or subprocess.check_output(
            ['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise FixtureError('STK01_FROZEN_CLEAN_SOURCE_REQUIRED')
    files = configuration_files()
    archive = artifacts / 'source.tar'
    archive.write_bytes(subprocess.check_output(['git', 'archive', '--format=tar', head], cwd=ROOT))
    hashes = source_files(ROOT, head)
    manifest = artifacts / 'source-manifest.json'
    manifest.write_text(json.dumps({'head': head, 'files': hashes}), encoding='utf-8')
    env = synthetic_environment(artifacts, project, port, head, hashlib.sha256(archive.read_bytes()).hexdigest())
    report = {'project': project, 'head': head, 'archive_sha256': env['MJOBS_STK01_SOURCE_SHA256'],
              'source_files': hashes, 'scope': 'synthetic PERSONAL stack54-60 only; parent292PARTIAL',
              'actions': [], 'result': 'NOT_RUN'}

    started = time.monotonic()

    def checkpoint(phase):
        (artifacts / 'stack-progress.json').write_text(json.dumps({
            'head': head, 'project': project, 'phase': phase,
            'elapsed_seconds': round(time.monotonic()-started, 2)}), encoding='utf-8')

    def budget():
        remaining(deadline, reserve=120)

    def compose(*args):
        budget()
        return recovery.compose(project, port, *args, config_files=files)

    def wait(predicate, seconds=90):
        until = min(time.monotonic() + seconds, deadline - 120)
        while time.monotonic() < until:
            try:
                result = predicate()
                if result:
                    return result
            except (httpx.HTTPError, ConnectionError):
                pass
            time.sleep(.5)
        raise FixtureError('STK01_EXPECTED_STATE_DEADLINE')

    def inspect_service(name):
        ids = compose('ps', '--all', '-q', name).split()
        if len(ids) != 1:
            raise FixtureError('STK01_SERVICE_ID_AMBIGUOUS')
        obj = json.loads(recovery._run_bounded(['docker', 'inspect', ids[0]],
            timeout=remaining(deadline, reserve=120, ceiling=10), action='Inspect STK01 service'))[0]
        validate_runtime_container(obj, name, project)
        return obj

    def exec_python(service, program):
        return json.loads(compose('exec', '-T', service, 'python', '-m', 'cli.runtime_database', 'python', '-c', program))

    def service_snapshot(targets):
        result = {}
        for name, target in targets.items():
            obj = inspect_service(name)
            if obj['Id'] != target['container_id']:
                raise FixtureError('STK01_TARGET_CHANGED')
            result[name] = {'id': obj['Id'], 'started_at': obj['State']['StartedAt'],
                            'pid': obj['State']['Pid'],
                            'volumes': sorted((m['Name'], m['Destination'], m['RW'])
                                              for m in obj['Mounts'] if m['Type'] == 'volume')}
        return result

    with patch.dict(os.environ, env, clear=True):
        try:
            config = json.loads(compose('config', '--format', 'json'))
            report['configuration'] = validate_configuration(config, project, port)
            # Exact source is copied into a stopped owned initializer, not mounted from the host.
            checkpoint('creating-owned-project')
            compose('create', '--no-build', '--pull', 'never')
            owned_resources(project, deadline)
            initializer = inspect_service('source-init')
            for path, dest in [(archive, '/tmp/source.tar'), (manifest, '/tmp/source-manifest.json')]:
                recovery._run_bounded(['docker', 'cp', str(path), initializer['Id'] + ':' + dest],
                    timeout=remaining(deadline, reserve=120, ceiling=20), action='Copy exact owned source input')
            compose('start', 'source-init')
            def exited(name):
                state = inspect_service(name)['State']
                if state['Running']:
                    return False
                if state['ExitCode'] != 0:
                    raise FixtureError('STK01_' + name.upper() + '_EXIT_' + str(state['ExitCode']))
                return True
            wait(lambda: exited('source-init'))
            current_source = artifacts / 'current-source-imports.json'
            recovery._run_bounded(['docker', 'cp', initializer['Id'] + ':/workspace/.stk01-imports.json',
                                   str(current_source)], timeout=remaining(deadline, reserve=120, ceiling=10),
                                  action='Read exact-source import proof')
            report['source_imports'] = json.loads(current_source.read_text(encoding='utf-8'))
            if report['source_imports']['head'] != head or not report['source_imports']['all_file_hashes_match']:
                raise FixtureError('STK01_CURRENT_SOURCE_UNPROVEN')
            checkpoint('current-source-imports-verified')
            compose('start', 'postgres', 'redis')
            wait(lambda: all(inspect_service(n)['State'].get('Health', {}).get('Status') == 'healthy'
                             for n in ['postgres', 'redis']))
            gucs = compose('exec', '-T', 'postgres', 'psql', '-U', 'mjobs', '-d', env['MJOBS_STK01_DB'],
                           '-Atc', "SELECT current_setting('max_connections'),current_setting('lock_timeout'),"
                           "current_setting('statement_timeout'),current_setting('idle_in_transaction_session_timeout')").strip()
            if gucs != '40|3s|15s|20s':
                raise FixtureError('STK01_EFFECTIVE_GUC_MISMATCH')
            report['effective_gucs'] = gucs
            compose('start', 'migrate')
            wait(lambda: exited('migrate'))
            checkpoint('migration-provision-complete')
            compose('start', *sorted(RUNTIME))
            budget()
            targets = wait(lambda: recovery.preflight_recovery_targets(project, port, config_files=files))
            wait(lambda: recovery.healthy_containers(targets))
            before_services = service_snapshot(targets)
            checkpoint('all-ten-services-healthy')

            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=10, trust_env=False) as client:
                def request(method, path, **kwargs):
                    budget()
                    response = client.request(method, path, **kwargs)
                    if response.status_code >= 400:
                        detail = response.json().get('detail', '')
                        code = detail.get('code', '') if isinstance(detail, dict) else detail
                        safe_code = code if isinstance(code, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,127}', code) else 'UNCLASSIFIED'
                        raise FixtureError('STK01_PUBLIC_REQUEST_REJECTED:' + method + ':' + path + ':' + str(response.status_code) + ':' + safe_code)
                    return response.json()

                health = request('GET', '/health')
                settings = request('GET', '/settings')
                if (health['operation_mode'] != 'PERSONAL' or settings['operation_mode'] != 'PERSONAL'
                        or settings['external_submission_enabled'] is not False):
                    raise FixtureError('STK01_PERSISTED_PERSONAL_MODE_REQUIRED')
                profile_id = 'stk01-' + project.rsplit('-', 1)[1]
                request('PUT', '/profiles', json=dict(PROFILE, id=profile_id, email='synthetic@example.invalid'))
                request('PUT', '/search-profiles', json={'id': 'stk01-all', 'profile_id': profile_id,
                        'name': 'Synthetic monitor only', 'enabled': True, 'source_ids': []})
                queued = request('POST', '/companies/discovery', json={
                    'company_name': 'Synthetic Monitoring Employer', 'company_website': 'https://employer.example/',
                    'reason': 'USER_ADDED_COMPANY', 'profile_id': profile_id, 'request_id': 'stk01-source'})
                def completed(operation_id):
                    row = request('GET', '/operations/' + operation_id)
                    if row['state'] in {'BLOCKED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED'}:
                        raise FixtureError('STK01_OPERATION_NOT_COMPLETED:' + row['state'])
                    return row if row['state'] == 'COMPLETED' else False
                wait(lambda: completed(queued['operation_id']))
                def view():
                    rows = request('GET', '/company-sources/view', params={'profile_id': profile_id})['company_sources']
                    return next(row for row in rows if row['company_id'] == queued['company_id'])
                source = view()
                if source['collection_strategy'] != 'PUBLIC_JSON' or source['provider'] != 'json_feed':
                    raise FixtureError('STK01_PUBLIC_SOURCE_NOT_DISCOVERED')
                source_id = source['id']
                request('PUT', '/company-sources/' + source_id + '/monitoring', json={
                    'source_version': source['source_version'], 'revision': 0, 'poll_interval': 300})
                refresh = request('POST', '/company-sources/' + source_id + '/refresh', json={'profile_id': profile_id})
                wait(lambda: completed(refresh['operation_id']))
                first = view()
                checkpoint('first-real-monitor-refresh-completed')
                if first['monitoring']['status'] != 'HEALTHY' or not first['next_check']:
                    raise FixtureError('STK01_HEALTHY_NEXT_DUE_MISSING')
                job = request('GET', '/jobs', params={'profile_id': profile_id})['jobs'][0]
                prepared = request('POST', '/applications', json={'profile_id': profile_id, 'job_id': job['id']})
                wait(lambda: completed(prepared['operation_id']))
                app_path = '/profiles/' + profile_id + '/applications/' + prepared['application_id']

                def inventory():
                    application = request('GET', app_path)
                    docs = request('GET', app_path + '/documents')['documents']
                    if not docs:
                        raise FixtureError('STK01_PREPARED_DOCUMENTS_MISSING')
                    document_rows = []
                    for doc in docs:
                        values = {}
                        for extension in ['txt', 'docx', 'pdf']:
                            response = client.get('/profiles/' + profile_id + '/documents/' + doc['id'] + '/' + extension)
                            response.raise_for_status()
                            values[extension] = hashlib.sha256(response.content).hexdigest()
                            if not response.content or values[extension] != doc['sha256'][extension]:
                                raise FixtureError('STK01_DOCUMENT_BYTES_CHANGED')
                        document_rows.append({'id': doc['id'], 'version': doc['version'], 'sha256': values})
                    company = view()
                    return {'job_ids': sorted(row['id'] for row in request('GET', '/jobs', params={'profile_id': profile_id})['jobs']),
                            'source': {k: company[k] for k in ['id', 'company_id', 'provider', 'tenant', 'collection_strategy',
                                                               'enabled', 'career_url', 'last_success', 'next_check']},
                            'application': {k: application[k] for k in ['id', 'profile_id', 'job_id', 'state', 'route_id']},
                            'documents': sorted(document_rows, key=lambda row: row['id'])}

                baseline = inventory()
                checkpoint('public-jobs-source-documents-application-baseline-saved')
                feed_before = exec_python('mock', "import httpx,json;print(json.dumps(httpx.get('http://127.0.0.1:8090/_test/monitoring',trust_env=False).json()))")
                exec_python('mock', "import httpx,json;print(json.dumps(httpx.post('http://127.0.0.1:8090/_test/monitoring/revision-two',trust_env=False).json()))")
                # Existing targeting and restart primitives remain unmodified.
                owned_resources(project, deadline)
                budget()
                recovery.preflight_recovery_targets(project, port, config_files=files)
                checkpoint('restarting-all-ten-owned-services')
                recovery.restart_recovery_services(project, port, config_files=files)
                after_targets = wait(lambda: recovery.preflight_recovery_targets(project, port, config_files=files))
                wait(lambda: recovery.healthy_containers(after_targets))
                after_services = service_snapshot(after_targets)
                for name in before_services:
                    old, new = before_services[name], after_services[name]
                    if (old['id'] != new['id'] or old['started_at'] == new['started_at']
                            or not new['pid'] or old['pid'] == new['pid'] or old['volumes'] != new['volumes']):
                        raise FixtureError('STK01_ALL_SERVICE_RESTART_UNPROVEN:' + name)
                wait(lambda: request('GET', '/readiness')['status'] == 'ready')
                if inventory() != baseline:
                    raise FixtureError('STK01_RESTART_PERSISTENCE_MISMATCH')
                report['persistence_domains'] = ['jobs', 'company source', 'documents', 'application']
                report['services_before'] = before_services
                report['services_after'] = after_services
                report['baseline_sha256'] = hashlib.sha256(json.dumps(baseline, sort_keys=True).encode()).hexdigest()
                report['first_refresh_operation'] = refresh['operation_id']
                report['next_due_before_restart'] = first['next_check']
                # The real 300-second due time is not edited or mocked.
                def new_posting():
                    rows = request('GET', '/jobs', params={'profile_id': profile_id})['jobs']
                    return rows if any(row['title'] == 'Python Reliability Engineer' for row in rows) else False
                checkpoint('restart-persistence-verified-waiting-real-300-second-nextdue')
                wait(new_posting, seconds=340)
                final = view()
                if final['monitoring']['status'] != 'HEALTHY' or final['next_check'] <= first['next_check']:
                    raise FixtureError('STK01_MONITOR_NOT_RESUMED')
                operation = wait(lambda: completed(final['monitoring']['operation_id']))
                if operation['state'] != 'COMPLETED' or operation['attempt_count'] != 1:
                    raise FixtureError('STK01_SECOND_REFRESH_NOT_COMPLETED')
                query = ("import json,os;from sqlalchemy import select;from packages.persistence.store import Store;"
                         "from packages.persistence import models as m;s=Store(os.environ['MJOBS_DATABASE_URL']);"
                         "c=s.engine.connect();rows=[dict(r) for r in c.execute(select(m.operations).where("
                         "m.operations.c.kind=='COMPANY_SOURCE_REFRESH')).mappings()];"
                         "print(json.dumps([{'id':r['id'],'state':r['state'],'attempt_count':r['attempt_count'],"
                         "'predecessor':r['params'].get('predecessor_operation_id')} for r in rows]));c.close();s.engine.dispose()")
                rows = exec_python('api', query)
                if (len(rows) != 2 or any(row['state'] != 'COMPLETED' or row['attempt_count'] != 1 for row in rows)
                        or next(row for row in rows if row['id'] != refresh['operation_id'])['predecessor'] != refresh['operation_id']):
                    raise FixtureError('STK01_REFRESH_DUPLICATE_OR_IDENTITY_CHANGED')
                time.sleep(6)  # one actual production scheduler pass, still within the new interval
                if exec_python('api', query) != rows:
                    raise FixtureError('STK01_DUPLICATE_AFTER_NEXT_TICK')
                feed_after = exec_python('mock', "import httpx,json;print(json.dumps(httpx.get('http://127.0.0.1:8090/_test/monitoring',trust_env=False).json()))")
                if feed_after['feed_gets'] != feed_before['feed_gets'] + 1:
                    raise FixtureError('STK01_UNEXPECTED_EXTRA_FEED_FETCH')
                checkpoint('second-scheduler-rq-refresh-and-no-duplicate-verified')
                report.update(result='PASS', second_refresh_operation=operation['id'],
                              next_due_after_restart=final['next_check'], refresh_operations=rows,
                              feed_gets_before=feed_before['feed_gets'], feed_gets_after=feed_after['feed_gets'],
                              external_submission_enabled=False)
        except BaseException as error:
            # Never retain exception values which might contain generated credentials.
            report['result'] = 'NOT_PASS'
            report['failure_type'] = type(error).__name__
            if isinstance(error, FixtureError):
                report['failure_code'] = str(error)
            try:
                objects = owned_resources(project, deadline)['container']
                report['failure_container_states'] = {o['Name']: {k: o['State'].get(k) for k in ['Status','Running','ExitCode','OOMKilled']} for o in objects}
                failed = [o for o in objects if o['State'].get('ExitCode') or o['State'].get('Health', {}).get('Status') == 'unhealthy']
                for obj in failed[:2]:
                    logs = subprocess.run(['docker','logs','--tail','80',obj['Id']], capture_output=True, text=True,
                        timeout=remaining(deadline, reserve=70, ceiling=5))
                    text = (logs.stdout+logs.stderr).replace(env['POSTGRES_PASSWORD'], '[SYNTHETIC_REDACTED]').replace(env['MJOBS_CREDENTIAL_ENCRYPTION_KEY'], '[SYNTHETIC_REDACTED]')
                    text = re.sub(r'postgresql(?:\+psycopg)?://[^\s]+', '[REDACTED_DSN]', text)
                    (artifacts / (obj['Name'].strip('/')+'.log')).write_text(text[-20000:], encoding='utf-8')
            except Exception:
                report['failure_diagnostics_incomplete'] = True
            raise
        finally:
            try:
                checkpoint('ownership-checked-teardown')
                report['residual_resources'] = cleanup_stack(project, port, files, deadline)
            finally:
                (artifacts / 'stack-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    if any(report['residual_resources'].values()):
        raise FixtureError('STK01_OWN_RESIDUAL_RESOURCES')
    return report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['mock', 'worker'])
    args = parser.parse_args()
    (mock_main if args.action == 'mock' else worker_main)()


if __name__ == '__main__':
    main()
