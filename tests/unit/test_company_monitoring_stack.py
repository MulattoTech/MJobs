"""Fail-closed guards for the opt-in synthetic company-monitoring stack."""
import ast
import copy
import inspect
import io
import json
import tarfile
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from fixtures.company_monitoring_stack import (
    FixtureError, IMAGE_PINS, origin_path, remaining, resource_owned,
    validate_configuration, validate_fixture_mode, source_files,
)

PROJECT = 'mjobs-fixture-012345abcdef'


class CompanyMonitoringStackGuardTests(unittest.TestCase):
    def test_initial_refresh_is_observed_from_public_scheduler_pointer_without_manual_enqueue(self):
        from fixtures.company_monitoring_stack import run_stack
        tree = ast.parse(inspect.getsource(run_stack))
        requests = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == 'request']
        self.assertFalse(any(node.args and isinstance(node.args[0], ast.Constant)
                             and node.args[0].value == 'POST'
                             and '/refresh' in ast.unparse(node) for node in requests))
        probe = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                     and node.name == 'scheduled_refresh')
        observed = []
        state = {'monitoring': {'operation_id': None}}
        namespace = {'view': lambda: state, 'completed': lambda identity: observed.append(identity) or {'id': identity}}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[probe], type_ignores=[])), '<scheduler-probe>', 'exec'), namespace)
        self.assertFalse(namespace['scheduled_refresh']())
        self.assertEqual(observed, [])
        state['monitoring']['operation_id'] = 'scheduler-owned-operation'
        self.assertEqual(namespace['scheduled_refresh'](), {'id': 'scheduler-owned-operation'})
        self.assertEqual(observed, ['scheduler-owned-operation'])

    def test_only_exact_synthetic_origin_and_known_paths_are_transportable(self):
        self.assertEqual(origin_path('https://employer.example/jobs/one'), '/jobs/one')
        for url in ['http://employer.example/jobs/one', 'https://other.example/jobs/one',
                    'https://employer.example:443/jobs/one', 'https://user@employer.example/jobs/one',
                    'https://employer.example/jobs/one?q=secret', 'https://employer.example/jobs/one#x',
                    'https://employer.example/jobs/one?', 'https://employer.example/jobs/one#',
                    'https://employer.example/%2e%2e/private', 'https://127.0.0.1/jobs/one']:
            with self.subTest(url=url), self.assertRaises(FixtureError):
                origin_path(url)

    def test_deadline_keeps_cleanup_reserve_and_never_increases_command_bound(self):
        self.assertEqual(remaining(200, now=100, reserve=30, ceiling=15), 15)
        self.assertEqual(remaining(150, now=100, reserve=30, ceiling=90), 20)
        for deadline in [99, 100, 129, float('nan'), float('inf')]:
            with self.subTest(deadline=deadline), self.assertRaises(FixtureError):
                remaining(deadline, now=100, reserve=30)

    def test_fixture_mode_requires_personal_synthetic_gate_and_project(self):
        env = {'MJOBS_STK01_ENABLE': '1', 'MJOBS_STK01_PROJECT': PROJECT,
               'MJOBS_OPERATION_MODE': 'PERSONAL', 'MJOBS_COMPOSE_MOCK': '0'}
        validate_fixture_mode(env)
        for key, value in [('MJOBS_STK01_ENABLE', '0'), ('MJOBS_STK01_PROJECT', 'mjobs-personal'),
                           ('MJOBS_OPERATION_MODE', 'FIXTURE'), ('MJOBS_COMPOSE_MOCK', '1')]:
            with self.subTest(key=key), self.assertRaises(FixtureError):
                validate_fixture_mode(dict(env, **{key: value}))

    def test_resource_ownership_rejects_missing_or_foreign_labels(self):
        labels = {'ao.session': 'mjobs-85', 'ao.run': PROJECT,
                  'com.docker.compose.project': PROJECT}
        resource_owned(labels, PROJECT)
        for key in labels:
            bad = dict(labels); bad[key] = 'foreign'
            with self.subTest(key=key), self.assertRaises(FixtureError):
                resource_owned(bad, PROJECT)

    def test_actual_container_requires_exact_frozen_read_only_source_and_mode(self):
        from fixtures.company_monitoring_stack import validate_runtime_container
        obj = {'Config': {'Image': IMAGE_PINS['runtime'], 'WorkingDir': '/workspace',
            'Labels': {'ao.session': 'mjobs-85', 'ao.run': PROJECT, 'com.docker.compose.project': PROJECT},
            'Env': ['MJOBS_STK01_ENABLE=1', 'MJOBS_STK01_PROJECT='+PROJECT,
                    'MJOBS_OPERATION_MODE=PERSONAL', 'MJOBS_COMPOSE_MOCK=0']},
            'Mounts': [{'Type': 'volume', 'Name': PROJECT+'_stk-source', 'Destination': '/workspace', 'RW': False}]}
        validate_runtime_container(obj, 'api', PROJECT)
        for mutate in [lambda o: o['Config'].update(WorkingDir='/app'),
                       lambda o: o['Mounts'][0].update(RW=True),
                       lambda o: o['Mounts'][0].update(Name='foreign-source'),
                       lambda o: o['Config'].update(Image='old:source')]:
            bad = copy.deepcopy(obj); mutate(bad)
            with self.subTest(mutate=mutate), self.assertRaises(FixtureError):
                validate_runtime_container(bad, 'api', PROJECT)

    def test_source_manifest_is_committed_content_not_ambient_files(self):
        files = source_files(Path(__file__).resolve().parents[2])
        self.assertIn('services/company_discovery/monitoring.py', files)
        self.assertFalse(any('.env' == Path(name).name or name.startswith('.git/') for name in files))
        self.assertTrue(all(len(value) == 64 for value in files.values()))

    def test_source_archive_rejects_traversal_symlinks_and_ambient_env(self):
        for name, symbolic in [('../outside', False), ('.env', False), ('safe-link', True)]:
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w') as archive:
                entry = tarfile.TarInfo(name)
                if symbolic:
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = '/private'
                archive.addfile(entry)
            with self.subTest(name=name), patch('fixtures.company_monitoring_stack.subprocess.check_output',
                    return_value=stream.getvalue()), self.assertRaises(FixtureError):
                source_files(Path.cwd())

    def test_foreign_resource_prevents_cleanup_down_command(self):
        from fixtures.company_monitoring_stack import cleanup_stack
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if args[:2] == ['docker', 'ps']:
                return 'foreign-container'
            if args[:2] == ['docker', 'inspect']:
                return json.dumps([{'Config': {'Labels': {'ao.session': 'someone-else'}}}])
            self.fail('No teardown command may follow a failed ownership check')
        with patch('fixtures.company_monitoring_stack.recovery._run_bounded', side_effect=command), \
                self.assertRaises(FixtureError):
            cleanup_stack(PROJECT, 23456, (), time.monotonic()+60)
        self.assertFalse(any('down' in call for call in calls))

    def test_private_client_rejects_absolute_urls_and_unbounded_inputs_without_http(self):
        from fixtures.company_monitoring_stack import private_http_client
        gate = {'MJOBS_STK01_PROJECT': PROJECT, 'MJOBS_STK01_ENABLE': '1',
                'MJOBS_OPERATION_MODE': 'PERSONAL', 'MJOBS_COMPOSE_MOCK': '0'}
        for payload in [{'method':'GET','path':'http://other/','timeout':1},
                        {'method':'GET','path':'//other','timeout':1},
                        {'method':'GET','path':'/health','timeout':float('inf')},
                        {'method':'GET','path':'/health','timeout':6},
                        {'method':'DELETE','path':'/health','timeout':1},
                        {'method':'GET','path':'/health','timeout':1,'code':'private'}]:
            with self.subTest(payload=payload), patch.dict('os.environ',gate,clear=True), \
                    patch('sys.stdin', buffer=io.BytesIO(json.dumps(payload).encode())), \
                    patch('httpx.Client') as client, self.assertRaises(FixtureError):
                private_http_client()
            client.assert_not_called()

    def test_private_client_uses_fixed_real_http_endpoint_and_bounded_stream(self):
        from fixtures.company_monitoring_stack import private_http_client
        from unittest.mock import MagicMock
        gate = {'MJOBS_STK01_PROJECT': PROJECT, 'MJOBS_STK01_ENABLE': '1',
                'MJOBS_OPERATION_MODE': 'PERSONAL', 'MJOBS_COMPOSE_MOCK': '0'}
        response=MagicMock(status_code=200);response.iter_bytes.return_value=[b'{"status":"ok"}']
        with patch.dict('os.environ',gate,clear=True), \
                patch('sys.stdin',buffer=io.BytesIO(b'{"method":"GET","path":"/health","timeout":2}')), \
                patch('httpx.Client') as factory, patch('builtins.print') as output, \
                patch('signal.setitimer',create=True) as timer, patch('signal.signal'), \
                patch('signal.SIGALRM',14,create=True), patch('signal.ITIMER_REAL',0,create=True):
            factory.return_value.__enter__.return_value.stream.return_value.__enter__.return_value=response
            private_http_client()
            factory.assert_called_once_with(base_url='http://api:8000',trust_env=False,follow_redirects=False,timeout=2)
            value=json.loads(output.call_args.args[0]);self.assertEqual(value['status'],200)
            self.assertEqual(timer.call_args_list[0].args,(0,2))
            self.assertEqual(timer.call_args_list[-1].args,(0,0))
        source=inspect.getsource(__import__('fixtures.company_monitoring_stack',fromlist=['run_stack']).run_stack)
        self.assertIn("inspect_service('mock')",source)
        self.assertIn("'exec', '-T', 'mock'",source)
        self.assertIn("get('com.docker.compose.service') != 'mock'",source)
        self.assertNotIn("base_url=f'http://127.0.0.1",source)

    def config(self):
        labels = {'ao.session': 'mjobs-85', 'ao.run': PROJECT}
        services = {}
        from cli.compose_recovery import RECOVERY_RESTART_SERVICES
        for name in (*RECOVERY_RESTART_SERVICES, 'source-init', 'migrate'):
            image = IMAGE_PINS['postgres' if name == 'postgres' else 'redis' if name == 'redis' else 'runtime']
            services[name] = {'image': image, 'pull_policy': 'never', 'cpus': .125,
                'mem_limit': 128 * 1024**2, 'labels': labels,
                'environment': {'MJOBS_OPERATION_MODE': 'PERSONAL', 'MJOBS_COMPOSE_MOCK': '0'},
                'networks': {'private': {}}, 'volumes': [{'type': 'volume', 'source': 'documents',
                    'target': '/data/documents'}]}
        for name, service in services.items():
            if name not in {'postgres','redis','source-init'}:
                service['working_dir']='/workspace'
                service['volumes'].append({'type':'volume','source':'stk-source',
                                          'target':'/workspace','read_only':True})
        return {'services': services, 'networks': {'private': {'internal': True, 'labels': labels}},
                'volumes': {'documents': {'labels': labels},'stk-source': {'labels': labels}}}

    def test_configuration_accepts_only_pinned_private_capped_owned_scenario(self):
        validate_configuration(self.config(), PROJECT, 23456)

    def test_configuration_rejects_egress_binds_builds_caps_and_foreign_images(self):
        mutations = [lambda c: c['services']['api'].update(image='unreviewed:latest'),
                     lambda c: c['services']['api'].update(build={'context': '.'}),
                     lambda c: c['services']['api'].update(pull_policy='always'),
                     lambda c: c['services']['api'].update(cpus=4),
                     lambda c: c['services']['api'].update(privileged=True),
                     lambda c: c['services']['api'].update(network_mode='host'),
                     lambda c: c['services']['api']['volumes'][1].update(read_only=False),
                     lambda c: c['services']['api'].update(mem_limit=8*1024**3),
                     lambda c: c['services']['api'].update(ports=[{'host_ip':'127.0.0.1','published':'23456','target':8000}]),
                     lambda c: c['services']['api']['volumes'][0].update(type='bind'),
                     lambda c: c['networks']['private'].update(internal=False),
                     lambda c: c['volumes']['documents'].update(external=True),
                     lambda c: c['services']['scheduler']['environment'].update(MJOBS_OPERATION_MODE='FIXTURE'),
                     lambda c: c['services']['migrate']['environment'].update(MJOBS_COMPOSE_MOCK='1')]
        for mutate in mutations:
            c = copy.deepcopy(self.config()); mutate(c)
            with self.subTest(mutation=mutate), self.assertRaises(FixtureError):
                validate_configuration(c, PROJECT, 23456)


if __name__ == '__main__':
    unittest.main()
